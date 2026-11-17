"""Real local model + native Chrome verification, preserving existing user tabs.

Opens only a disposable new tab, verifies navigation, and closes that owned tab.
Run explicitly: python3 -m tests.live_computer_eval
"""
import json
import tempfile
import time
from pathlib import Path

from claire_core import boot
from claire_core.client import Client
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core.settings import Settings
from claire_core.tools import ToolContext
from tests.live_cleanup import release_splash_on_exit


def main():
    live=Client(Settings());status=live.get('/status')
    if status.get('power',{}).get('busy') or status['pending_approvals']:
        raise RuntimeError('Claire has pending work; do not run competing GUI verification')
    root=Path(tempfile.mkdtemp(prefix='claire-computer-live-',dir='/private/tmp'))
    settings=Settings(state_dir=str(root/'state'),vault=str(root/'vault'),secret_backend='file',
                      controller_enabled=True,skill_router=False,auto_parallel=False)
    release_splash_on_exit(settings)
    core=Core(settings);hub=Hub(core,start_brain=False)
    # Nothing else loads the conversation model here; it is no longer a
    # separate server that is always running.
    problem = boot.ensure_role(settings, settings.controller_role)
    if problem: raise SystemExit(problem)
    provider=core.connectors.providers['mac'];row=None;session=None
    before=None
    try:
        provider.claim({},'live-check')
        time.sleep(.5)
        iid=core.connectors.create('mac','Mac apps',{'apps':'Google Chrome'})['id']
        view=core.connectors.test(iid)
        if view['status']!='connected':raise RuntimeError(view['error'])
        provider._run(op='open',app='Google Chrome')
        try:before=provider._run(op='browser_state',app='Google Chrome')
        except Exception:before={'tabs':[]}
        problem=boot.ensure_role(settings,'talk')
        if problem:raise RuntimeError(problem)
        session=hub._session('home')
        session.approver=lambda _:False # test never authorizes changing/closing existing user tabs
        events=[];session.sink=events.append
        start=time.monotonic()
        result=session.run('Open a new tab in this current browser, enter google.com, and close only that new tab. Leave every existing tab unchanged.')
        try:after=provider._run(op='browser_state',app='Google Chrome')
        except Exception:after={'tabs':[]}
        actions=[{'tool':n,'action':a.get('action'),'failed':f} for n,a,_,f in session._turn_outcomes]
        expected=['new_tab','close_tab']
        performed=[a['action'] for a in actions if not a['failed'] and a['tool']=='computer']
        passed=(result['status']=='done' and all(a in performed for a in expected)
                and [t['title'] for t in before['tabs']]==[t['title'] for t in after['tabs']])
        report={'pass':passed,'seconds':round(time.monotonic()-start,3),'answer':result['answer'],
                'actions':actions,'model_calls':sum(e['type']=='model_call' for e in events),
                'original_tab_count':len(before['tabs']),'final_tab_count':len(after['tabs']),
                'total_ttft':round(sum(e.get('ttft',0) for e in events if e['type']=='model_call'),3)}
        (root/'result.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report),flush=True)
        return 0 if passed else 1
    finally:
        if session is not None:
            # Close only still-owned fixture tabs. Changed state is refused.
            for ref,item in list(core.computer.tabs.items()):
                if item['owner']==session.thread_id and item['owned']:
                    try:core.computer.call(ToolContext(session),{'action':'close_tab','tab':ref})
                    except Exception as exc:print('Cleanup stopped safely: '+str(exc),flush=True)
        hub.stop();core.store.close()
        print('Artifacts: '+str(root),flush=True)


if __name__=='__main__':raise SystemExit(main())
