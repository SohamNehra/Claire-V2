"""Playwright with a dedicated Chrome profile and Chrome's native sandbox.

macOS cannot initialize Chrome's Seatbelt sandbox inside a second Seatbelt
sandbox. The trusted service owns Chrome; the MCP process stays in Claire's
sandbox and connects over a loopback-only, ephemeral DevTools port.
"""
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import ConnectorError
from .mcp import Mcp
from .. import guard

PACKAGE = '@playwright/mcp@0.0.85'

# Looking and moving around: never asks.
LOOK = {'navigate', 'navigate_back', 'snapshot', 'find', 'take_screenshot', 'wait_for', 'hover',
        'resize', 'close', 'console_messages', 'network_requests', 'network_request',
        'emulate_media', 'tabs', 'fill_form', 'select_option', 'drag'}
# Reaches outside the page: always asks.
ALWAYS = {'file_upload': 'uploads a file from this Mac to the site',
          'drop': 'drops files or data onto the page',
          'run_code_unsafe': 'runs code outside the page, in the browser controller'}
# What a control says when pressing it is not a small thing.
RISKY = re.compile(
    r'\b(delet\w*|remov\w*|eras\w*|discard\w*|trash|destroy\w*|clear all|empty (trash|bin|cart)|'
    r'pay\w*|buy\w*|purchas\w*|order\w*|check ?out|subscrib\w*|unsubscrib\w*|upgrad\w*|donat\w*|'
    r'transfer\w*|withdraw\w*|send\w*|submit\w*|post\w*|publish\w*|shar\w*|tweet\w*|repl(y|ies)|'
    r'comment\w*|confirm\w*|accept\w*|agree\w*|authori[sz]\w*|grant\w*|approv\w*|sign ?up|register\w*|'
    r'create account|sign ?in|log ?in|log ?out|sign ?out|deactivat\w*|close account|cancel\w*|'
    r'sav\w*|apply\w*|install\w*|download\w*|upload\w*|merg\w*|deploy\w*|revok\w*|reset\w*|'
    r'book\w*|reserv\w*|place|password)\b', re.I)
SEARCH = re.compile(r'search|find|filter|query|look ?up|address bar', re.I)
# Page script that does more than read.
MUTATES = re.compile(
    r'fetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|\.submit\s*\(|\.click\s*\(|\.remove\s*\(|'
    r'dispatchEvent|innerHTML\s*=|outerHTML\s*=|\.value\s*=|\.checked\s*=|\.href\s*=|'
    r'location\s*=|location\.(assign|replace|reload)|document\.(write|cookie)|localStorage|'
    r'sessionStorage|indexedDB|window\.open|\beval\s*\(|new\s+Function|\bimport\s*\(|'
    r'setAttribute|removeAttribute|appendChild|insertBefore|replaceWith|\.delete\s*\(|\.clear\s*\(')
_YAML = re.compile(r'```yaml\n(.*?)```', re.S)
_LINK = re.compile(r'- \[Snapshot\]\((\.playwright-mcp/[\w.:-]+\.yml)\)')
MANUAL = Path(__file__).with_name('browser_manual.md')


class Chrome:
    def __init__(self, home, headless=True):
        candidates = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'] if sys.platform == 'darwin' else [shutil.which('google-chrome'), shutil.which('chromium')]
        executable = next((p for p in candidates if p and os.path.isfile(p)), None)
        if not executable:
            raise ConnectorError('Google Chrome is not installed. Install Chrome, then Connect & test again.')
        profile = Path(home) / 'browser-profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        port_file = profile / 'DevToolsActivePort'
        port_file.unlink(missing_ok=True)
        self._closed = False
        self._pid_file = Path(home) / 'chrome.pid'
        self._reap(str(profile))
        argv = [executable, '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--user-data-dir='+str(profile), '--no-first-run', '--no-default-browser-check',
                '--disable-extensions', '--password-store=basic', '--use-mock-keychain',
                '--disable-background-networking', '--disable-component-update']
        if headless:
            argv.append('--headless=new')
        argv.append('about:blank')
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=guard.clean_env(), start_new_session=True)
        except OSError as exc:
            raise ConnectorError(f'Could not start Chrome: {exc}')
        try:
            self._pid_file.write_text(str(self.proc.pid))
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                if self.proc.poll() is not None:
                    raise ConnectorError('Chrome stopped before it was ready. Close any other browser using this connector profile and reconnect.')
                try:
                    port = int(port_file.read_text().splitlines()[0])
                    if 0 < port < 65536:
                        self.endpoint = f'http://127.0.0.1:{port}'
                        return
                except (OSError, ValueError, IndexError):
                    pass
                time.sleep(.1)
            raise ConnectorError('Chrome did not become ready within 15 seconds.')
        except BaseException:
            self.close()
            raise

    def _reap(self, profile):
        """Kill a Chrome an earlier service left on this profile (it was
        killed before it could close its browser). Only a process that is
        provably ours -- its command line names this profile -- is touched."""
        try:
            pid = int(self._pid_file.read_text())
            command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                                     text=True, timeout=5).stdout
            if '--user-data-dir=' + profile in command:
                os.killpg(pid, signal.SIGKILL)
                time.sleep(.3)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self._pid_file.unlink(missing_ok=True)

    def close(self):
        """Stop Chrome and its helpers. Safe to call twice, from any thread,
        and on a browser that already died."""
        if self._closed:
            return
        self._closed = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            self._pid_file.unlink(missing_ok=True)
        except OSError:
            pass


def _short(name):
    return name[8:] if name.startswith('browser_') else name


class Browser(Mcp):
    id = 'browser'
    label = 'Browser · Playwright'
    summary = ('A dedicated Chrome profile controlled by Playwright. Looking and moving around '
               'goes ahead; anything major asks first.')
    job_scoped = True            # Chrome runs only while a task is using it
    fields = (('headless', 'Run in the background (uncheck to show Chrome)', False, False),
              ('ask_every', 'Ask before every browser action (otherwise only major ones)', False, False),
              ('notes', 'What Claire should use this browser for', False, False))

    def validate(self, config, secrets):
        if secrets:
            raise ConnectorError('Browser setup does not accept credentials. Sign in on the website in the dedicated browser.')
        clean, _, scopes = super().validate({'transport':'stdio', 'command':['npx','-y',PACKAGE], 'keep_alive':True}, {})
        clean['headless'] = config.get('headless', True) is not False
        clean['ask_every'] = config.get('ask_every') is True
        return clean, {}, ['Open and read web pages', 'Act on pages (major actions ask first)']

    def __init__(self, core):
        super().__init__(core)
        self._pages = {}          # integration id -> the last page snapshot Claire was shown
        self._focus = {}          # integration id -> the field she last typed in or clicked

    def manual(self, integration):
        return MANUAL.read_text(encoding='utf-8')

    def operations(self, integration):
        ops = super().operations(integration)
        every = integration['config'].get('ask_every')
        for op in ops:
            op.writes = bool(every) or _short(op.name) in ALWAYS
        return ops

    def _line(self, iid, target):
        """What the page itself calls the element behind a reference. The
        model's own description of it is not evidence of what it is."""
        target = str(target or '')
        if not re.fullmatch(r'\w{1,12}', target):
            return ''
        for line in self._pages.get(iid, '').splitlines():
            if f'[ref={target}]' in line:
                return line.strip(' -')[:200]
        return ''

    def review(self, integration, op, args):
        """Why this call needs sir's approval, or None when it does not."""
        if integration['config'].get('ask_every'):
            return 'CHANGES DATA'
        iid, name = integration['id'], _short(op.name)
        if name in ALWAYS:
            return 'MAJOR ACTION: ' + ALWAYS[name]
        if name in LOOK:
            return None
        seen = self._line(iid, args.get('target'))
        what = ' '.join(x for x in (seen, str(args.get('element') or ''), str(args.get('target') or '')) if x)
        if name == 'click':
            hit = RISKY.search(what)
            return f'MAJOR ACTION: clicks “{(seen or what)[:90]}”' if hit else None
        if name == 'type':
            if args.get('submit') is True and not SEARCH.search(what):
                return f'MAJOR ACTION: types into “{(seen or what)[:70]}” and submits it'
            return None
        if name == 'press_key':
            if str(args.get('key') or '').lower() in ('enter', 'return', 'numpadenter') \
                    and not SEARCH.search(self._focus.get(iid, '')):
                return 'MAJOR ACTION: presses Enter, which may submit a form'
            return None
        if name == 'evaluate':
            if MUTATES.search(str(args.get('function') or '')):
                return 'MAJOR ACTION: runs a script that can change the page or send data'
            return None
        if name == 'handle_dialog':
            return None if args.get('accept') is False else 'MAJOR ACTION: accepts a confirmation dialog'
        return 'MAJOR ACTION: an action there is no rule for yet'

    def call(self, integration, operation, args, keys):
        iid, name = integration['id'], _short(operation.name)
        if name in ('click', 'type'):
            self._focus[iid] = ' '.join((self._line(iid, args.get('target')), str(args.get('element') or '')))
        text = self._inline(iid, super().call(integration, operation, args, keys))
        pages = _YAML.findall(text)
        if pages:
            self._pages[iid] = pages[-1]
        return text

    def _inline(self, iid, text):
        """Playwright saves a large snapshot to a file and returns a link.
        Claire cannot open that file; show her the page itself."""
        home = os.path.realpath(os.path.join(self.core.settings.state_dir, 'connectors', iid))

        def page(match):
            path = os.path.realpath(os.path.join(home, match.group(1)))
            if not path.startswith(os.path.join(home, '.playwright-mcp') + os.sep):
                return match.group(0)
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    body = f.read(24000)
                os.unlink(path)
            except OSError:
                return match.group(0)
            return '```yaml\n' + body.rstrip() + '\n```'
        return _LINK.sub(page, text)

    def disconnect(self, integration, keys):
        super().disconnect(integration, keys)
        self._pages.pop(integration['id'], None)
        self._focus.pop(integration['id'], None)

    def test(self, integration, keys):
        try:
            return super().test(integration, keys)
        finally:
            if not self._owners.get(integration['id']):     # unless a task is mid-use,
                self.disconnect(integration, keys)          # a check must not leave Chrome running

    def _open(self, integration, keys):
        home = os.path.join(self.core.settings.state_dir, 'connectors', integration['id'])
        browser = Chrome(home, integration['config'].get('headless', True))
        config = dict(integration['config'], command=['npx','-y',PACKAGE,'--cdp-endpoint='+browser.endpoint])
        try:
            session = super()._open(dict(integration, config=config), keys)
        except BaseException:
            browser.close()
            raise
        close_stdio = session.close
        def close():
            try:
                close_stdio()
            finally:
                browser.close()
        session.close = close
        return session
