"""Everything that stands between the model and the machine.

Three layers, strongest first:

1. The OS sandbox (macOS Seatbelt). Enforced by the kernel on the process and
   everything it spawns, so it holds no matter how a command is spelled: writes
   land only in the task's own folder, secrets cannot be read, and code Claire
   wrote herself gets no network and no view of the home directory at all.
2. A forbidden list. Commands that could destroy the machine are refused
   outright. No approval, flag or mode turns this off.
3. Approval. Risky-looking commands, and any request to step outside the
   sandbox, stop and wait for a human.

Layers 2 and 3 are pattern matches and can be evaded by a determined command;
that is why layer 1 exists and why nothing model-written ever runs without it.
"""

import os
import re
import signal
import subprocess
import sys
import tempfile

from . import host

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Claire's own code: the engine, the guards, the prompts, config.py. A task may
# read it (it holds no secrets) but never change it, whatever folder it works in.
CODE_DIR = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def code_dir(settings):
    return os.path.realpath(getattr(settings, "code_dir", None) or CODE_DIR)


def secret_paths(settings):
    """Never readable by a task: the machine's credentials, and Claire's own
    state (her service token, task database, keychain-backed connectors)."""
    home = os.path.realpath(os.path.expanduser("~"))
    rels = list(SECRET_PATHS) + list(getattr(settings, "secret_paths", []) or [])
    out = [os.path.join(home, r) if not os.path.isabs(os.path.expanduser(r))
           else os.path.expanduser(r) for r in rels]
    out.append(settings.state_dir)
    if sys.platform == "win32":
        for variable, names in (
            ("LOCALAPPDATA", ("Google/Chrome/User Data", "Microsoft/Edge/User Data", "BraveSoftware/Brave-Browser/User Data", "Microsoft/Credentials", "Microsoft/Vault")),
            ("APPDATA", ("Mozilla/Firefox", "Microsoft/Credentials", "Microsoft/Protect", "gcloud", "gh")),
        ):
            base = os.environ.get(variable)
            if base:
                out.extend(os.path.join(base, *name.split('/')) for name in names)
    return [os.path.realpath(p) for p in out]


def _inside(path, roots):
    if sys.platform == "win32":
        return any(host.is_within(path, r) for r in roots)
    return any(path == r or path.startswith(r + os.sep) for r in roots)

# Never run, whatever the mode and whoever approves. Kept short and specific:
# these are the commands with no legitimate use inside an agent's task.
_HOME_OR_ROOT = r"(?:/|/\*|~|~/|~/\*|\$HOME/?|\$\{HOME\}/?|/Users/?|/Users/[\w.-]+/?|/System\S*|/Library\S*|/usr\S*|/bin\S*|/etc\S*|/var\S*|/Applications\S*)"
FORBIDDEN = [
    (re.compile(rf"\brm\s+(?:-\S+\s+)*(?:--\s+)?[\"']?{_HOME_OR_ROOT}[\"']?(?:\s|$|;|&|\|)"),
     "deletes the home directory or a system directory"),
    (re.compile(r"--no-preserve-root"), "removes the root filesystem guard"),
    (re.compile(r"\bmkfs\b|\bnewfs(_\w+)?\b|\bfdisk\b|\bgpt\s+destroy"),
     "formats or repartitions a disk"),
    (re.compile(r"\bdiskutil\s+\S*(erase|partition|zerodisk|secureerase|apfs\s+delete)", re.I),
     "erases a disk or volume"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/(?!null|zero|stdout|stderr)"),
     "writes raw data to a device"),
    (re.compile(r">\s*/dev/(disk|rdisk|sd[a-z]|nvme)"), "writes raw data to a disk"),
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"), "is a fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "shuts the machine down"),
    (re.compile(r"\bcsrutil\b|\bnvram\b|\bspctl\s+--master-disable|\bbless\b"),
     "changes system integrity settings"),
    (re.compile(r"\bsudo\b|\bdoas\b|\bsu\s+-"), "asks for administrator rights"),
    (re.compile(r"\bchmod\s+(?:-\S+\s+)*\S+\s+[\"']?" + _HOME_OR_ROOT + r"[\"']?(?:\s|$)"),
     "changes permissions on the home directory or a system directory"),
    (re.compile(r"\bsecurity\s+(dump-keychain|find-(generic|internet)-password|delete-keychain)"),
     "reads or deletes the keychain"),
]

# Run only after a human says yes, regardless of mode.
DANGEROUS = re.compile(
    r"(\brm\s+-[rf]|\brmdir\b|\bdd\s"
    r"|\bkill(all)?\s|\bpkill\b|\bchmod\s+777|\bchown\b"
    r"|curl\s+[^|]*\|\s*(ba|z|fi)?sh|wget\s+[^|]*\|\s*(ba|z|fi)?sh"
    r"|\bgit\s+push|\bgit\s+reset\s+--hard|\bgit\s+clean\b"
    r"|>\s*/dev/(?!null\b|stdout\b|stderr\b|fd/)|\bnpm\s+publish|\bpip3?\s+install"
    r"|\bfind\b[^|;]*-delete|\bfind\b[^|;]*-exec\s+rm"
    r"|\bmv\b[^|;]*\s/(dev|etc|usr|bin|System)/"
    r"|os\.(remove|unlink|rmdir)|shutil\.rmtree"
    r"|\bcrontab\b|\blaunchctl\b|\bdefaults\s+write|\bdiskutil\b"
    r"|\bhistory\s+-c|\btruncate\b|\bshred\b|\bosascript\b|\bssh\b|\bscp\b)"
)

# A command that looks like it writes to an absolute path outside the project.
OUTSIDE_WRITE = re.compile(
    r"(>{1,2}\s*|(?:^|\s)(?:rm|mv|cp|touch|tee|truncate)\s+(?:-\S+\s+)*)"
    r"(~|/(?!tmp/|private/tmp/|var/folders/|dev/(?:null|stdout|stderr|fd/)))"
)

# Readable by nothing Claire runs. Paths are relative to the home directory.
SECRET_PATHS = [
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".netrc", ".npmrc",
    ".pypirc", ".git-credentials", ".config/gh", ".config/gcloud",
    ".config/op", ".password-store", ".claude", ".claude.json",
    "Library/Keychains", "Library/Cookies", "Library/Messages", "Library/Mail",
    "Library/Safari", "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Application Support/com.apple.TCC",
    "Library/Application Support/1Password",
]

# Environment variables withheld from every child process.
_SECRET_ENV = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|SESSION|COOKIE)|^(AWS|GH|GITHUB|"
    r"OPENAI|ANTHROPIC|GOOGLE|AZURE|HF|HUGGING|NPM|PYPI|DOCKER|SSH)_", re.I)

# Programs that hand work to something outside the sandbox, which would then
# run with none of its limits.
_ESCAPE_HATCHES = ["/usr/bin/osascript", "/usr/bin/open", "/bin/launchctl",
                   "/usr/bin/crontab", "/usr/bin/at", "/usr/bin/batch",
                   "/usr/bin/automator", "/usr/bin/shortcuts",
                   "/usr/bin/security"]

# The keychain holds the tokens for connected services. Its files are already
# unreadable (they are in SECRET_PATHS); this also closes the door to asking
# the keychain service for an item directly.
_KEYCHAIN_SERVICES = ["com.apple.SecurityServer", "com.apple.securityd",
                      "com.apple.secd", "com.apple.securityd.xpc"]


class Verdict:
    def __init__(self, level, reason="", escapes=False):
        self.level = level          # "allow" | "ask" | "deny"
        self.reason = reason
        self.escapes = escapes


def classify(command):
    """Judge a shell command by its text alone."""
    if sys.platform == "win32":
        if re.search(r"\b(?:format(?:\.com)?|clear-disk|initialize-disk|remove-partition|diskpart|bcdedit|restart-computer|stop-computer|runas)\b", command, re.I):
            return Verdict("deny", "changes disks, boot configuration, power or administrator identity")
        if re.search(r"\b(?:remove-item|del|erase|rd|rmdir|stop-process|taskkill|set-acl|icacls)\b", command, re.I):
            return Verdict("ask", "changes or deletes Windows files, permissions or processes")
    for pattern, why in FORBIDDEN:
        if pattern.search(command):
            return Verdict("deny", why)
    escapes = bool(OUTSIDE_WRITE.search(command))
    if DANGEROUS.search(command):
        return Verdict("ask", "matches a dangerous pattern", escapes)
    if escapes:
        return Verdict("ask", "writes outside the project", True)
    return Verdict("allow", "", False)


class PathEscape(ValueError):
    """A tool was asked to touch something outside its working directory."""


def safe_path(workdir, path, settings=None, writing=False):
    """Resolve path, refusing anything outside the working directory -- and,
    given the settings, Claire's state and secrets always and her own code
    when writing, even when they sit inside the working directory."""
    root = os.path.realpath(workdir)
    given = os.path.expanduser(path or "")
    full = os.path.realpath(os.path.join(root, given))
    if not (_inside(full, [root]) if sys.platform == "win32" else
            full == root or full.startswith(root + os.sep)):
        raise PathEscape(f"path escapes working directory: {path}")
    first = given.replace("\\", "/").lstrip("./").split("/", 1)
    if writing and len(first) == 2 and first[0] == os.path.basename(root) \
            and not os.path.exists(os.path.join(root, first[0])):
        # Writing <workdir name>/x from inside the workdir makes a nested
        # copy nobody will look at. Paths are relative to the work folder.
        raise PathEscape(f"you are already inside {first[0]}/ -- paths are relative to your "
                         f"working directory, so write {first[1]!r}, not {path!r}")
    if settings is not None:
        if _inside(full, secret_paths(settings)):
            raise PathEscape(f"{path} is Claire's private state or a credential store; "
                             f"it is off limits")
        if writing and _inside(full, [code_dir(settings)]):
            raise PathEscape(f"{path} is part of Claire's own code, which tasks cannot "
                             f"change. Read it if you need to; build new tools and "
                             f"integrations in their own folders.")
    return full


def clean_env(extra=None):
    """The parent environment with anything secret-looking removed."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env.update(extra or {})
    return env


def _q(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


class RunResult:
    def __init__(self, returncode, output, timed_out=False):
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out


class Sandbox:
    """Runs a process under a Seatbelt profile built for that one run."""

    def __init__(self, settings):
        self.s = settings
        mode = (settings.sandbox or "auto").lower()
        self.available = (mode != "off" and sys.platform == "darwin"
                          and os.access(SANDBOX_EXEC, os.X_OK))
        self.home = os.path.realpath(os.path.expanduser("~"))

    # -- profiles ------------------------------------------------------------

    def _secrets(self):
        # Claire's own state is among these: the service token, the task
        # database, and the workshop registry. A task that could read or
        # rewrite them could approve its own requests or register a tool that
        # skipped its test.
        return secret_paths(self.s)

    def profile(self, write_dirs, strict=False, network=True, read_dirs=()):
        """Build the profile text. Later rules override earlier ones.

        Normal profile, for shell commands in a task: reads are open except for
        secrets, writes are confined to write_dirs and temp.

        Strict profile, for code Claire wrote: the home directory is unreadable
        except for read_dirs, and there is no network unless asked for.
        """
        writes = [os.path.realpath(d) for d in write_dirs]
        if not strict:
            writes += [os.path.realpath(tempfile.gettempdir()), "/private/tmp"]
            writes += list(self.s.sandbox_extra_write)
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            "(allow file-write*",
        ]
        lines += [f"    (subpath {_q(d)})" for d in writes]
        lines += [
            '    (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty")',
            '    (literal "/dev/stdout") (literal "/dev/stderr")',
            '    (literal "/dev/dtracehelper")',
            '    (regex #"^/dev/fd/") (regex #"^/dev/ttys[0-9]+$"))',
        ]
        # Whatever folder a task was given, it cannot change Claire's own code
        # or state. A folder it was given inside her state (a workshop scratch
        # folder, a connector's home) is handed back below.
        guarded = [code_dir(self.s), os.path.realpath(self.s.state_dir)]
        lines.append("(deny file-write*")
        lines += [f"    (subpath {_q(p)})" for p in guarded]
        lines.append(")")
        given = [d for d in writes if any(d.startswith(p + os.sep) for p in guarded)]
        if given:
            lines.append("(allow file-write*")
            lines += [f"    (subpath {_q(d)})" for d in given]
            lines.append(")")
        if strict:
            lines.append(f"(deny file-read* (subpath {_q(self.home)}))")
            # Resolving a path needs to stat each folder above it.
            lines.append(f"(allow file-read-metadata (subpath {_q(self.home)}))")
            reads = [os.path.realpath(d) for d in list(read_dirs) + list(write_dirs)]
            reads += [p for p in _python_dirs() if p.startswith(self.home + os.sep)]
            lines.append("(allow file-read*")
            lines += [f"    (subpath {_q(d)})" for d in reads]
            lines.append(")")
        secrets = self._secrets()
        lines.append("(deny file-read*")
        lines += [f"    (subpath {_q(p)})" for p in secrets]
        lines.append(")")
        # A folder this run was explicitly given (a workshop scratch folder
        # lives inside Claire's state directory) stays readable; everything
        # around it in the secret path does not.
        granted = [os.path.realpath(d) for d in list(read_dirs) + list(write_dirs)]
        inside = [d for d in granted
                  if any(d.startswith(p + os.sep) for p in secrets)]
        if inside:
            lines.append("(allow file-read*")
            lines += [f"    (subpath {_q(d)})" for d in inside]
            lines.append(")")
        # Node realpath walks each ancestor of its entrypoint. Expose only
        # metadata of those directories, never their listings or file contents.
        ancestors = set()
        for directory in inside:
            parent = os.path.dirname(directory)
            while any(parent == p or parent.startswith(p + os.sep) for p in secrets):
                ancestors.add(parent)
                parent = os.path.dirname(parent)
        if ancestors:
            lines.append("(allow file-read-metadata")
            lines += [f"    (literal {_q(p)})" for p in sorted(ancestors)]
            lines.append(")")
        lines.append("(deny process-exec")
        lines += [f"    (literal {_q(p)})" for p in _ESCAPE_HATCHES]
        lines.append(")")
        lines.append("(deny appleevent-send)")
        lines.append("(deny mach-lookup")
        lines += [f"    (global-name {_q(n)})" for n in _KEYCHAIN_SERVICES]
        lines.append(")")
        if not network:
            lines.append("(deny network*)")
        return "\n".join(lines) + "\n"

    # -- running -------------------------------------------------------------

    def wrap(self, argv, write_dirs, strict=False, network=True, read_dirs=()):
        """argv prefixed so that it runs inside the sandbox, for callers that
        need to hold the process open and talk to it."""
        if not self.available:
            raise RuntimeError("no OS sandbox is available on this machine")
        text = self.profile(write_dirs, strict=strict, network=network,
                            read_dirs=read_dirs)
        return [SANDBOX_EXEC, "-p", text] + list(argv)

    def run(self, argv, cwd, write_dirs=None, strict=False, network=True,
            read_dirs=(), timeout=120, stdin=None, env=None, sandboxed=True,
            max_output=15000):
        """Run argv (a list, or a string for the shell) and capture its output.

        sandboxed=False is the one way out of the sandbox, and the caller must
        have obtained a human's approval before passing it.
        """
        shell_cmd = argv if isinstance(argv, str) else None
        if shell_cmd is not None:
            cmd = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", shell_cmd]
                   if sys.platform == "win32" else ["/bin/sh", "-c", shell_cmd])
        else:
            cmd = list(argv)
        if sandboxed:
            if not self.available:
                raise RuntimeError("no OS sandbox is available on this machine")
            text = self.profile(write_dirs or [cwd], strict=strict,
                                network=network, read_dirs=read_dirs)
            cmd = [SANDBOX_EXEC, "-p", text] + cmd
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=clean_env(env),
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", start_new_session=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except OSError as exc:
            return RunResult(127, f"could not start: {exc}")
        timed_out = False
        try:
            out, _ = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The whole process group, so a child the command spawned cannot
            # outlive it.
            if sys.platform == "win32":
                subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                if proc.poll() is None:
                    proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            out, _ = proc.communicate()
        out = out or ""
        if len(out) > max_output:
            out = out[:max_output] + "\n... [truncated]"
        return RunResult(proc.returncode, out, timed_out)


def _python_dirs():
    """Where this interpreter and its packages live, for the strict profile."""
    import site
    dirs = {sys.prefix, sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))}
    try:
        dirs.add(site.getusersitepackages())
    except Exception:
        pass
    return sorted(os.path.realpath(d) for d in dirs if d)
