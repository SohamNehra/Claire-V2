"""Real conversation-model decisions with simulated Mac/browser actions.

Runs in disposable state. No real browser, app, keyboard or screenshot is used.
Only the configured conversation model is contacted. A second browser connector
checks that 'current browser' routes to the user's Mac app rather than its own
separate profile. Run: python3 -m tests.live_browser_eval
"""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from claire_core.connectors import ConnectorError
from claire_core.connectors.browser import Browser
from claire_core.connectors.mac import Mac
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


def main():
    root=Path(tempfile.mkdtemp(prefix='claire-browser-eval-',dir='/private/tmp'))
    settings=Settings(state_dir=str(root/'state'),vault=str(root/'vault'),secret_backend='file',
                      controller_enabled=True,skill_router=False,auto_parallel=False)
    release_splash_on_exit(settings)
    core=Core(settings)
    hub=Hub(core,start_brain=True)
    rows=[]
    try:
        iid=core.connectors.create('mac','Mac apps',{'apps':'TextEdit, Notes, Google Chrome'})['id']
        core.store.update_integration(iid,status='connected',account_label='3 apps')
        other=core.connectors.create('browser','Browser',{})['id']
        row=core.store.integration(other)
        cfg=dict(row['config'],tools=[
            {'name':'browser_tabs','description':'List, create, select or close tabs in the separate browser.',
             'params':{'action':'list, new, close or select','index':'tab index'},'required':['action']},
            {'name':'browser_navigate','description':'Navigate the separate browser.',
             'params':{'url':'The requested URL'},'required':['url']}])
        core.store.update_integration(other,status='connected',config=cfg,account_label='Fixture browser')
        for deny_close in (False,True):
            tabs=['Claire'];current=[0];sent=[];approvals=[]
            def run(**request):
                sent.append(request)
                op=request['op']
                if op=='apps':return {'running':['Google Chrome','Notes','TextEdit']}
                if op=='windows':return {'windows':[{'title':tabs[current[0]],'minimized':False}]}
                if op=='tabs':return {'tabs':[{'title':t,'selected':i==current[0]} for i,t in enumerate(tabs)]}
                if op=='pick_tab':current[0]=request['index']
                if op=='key':
                    key=request['key'].lower()
                    if key=='t' and 'cmd' in request['modifiers']:
                        tabs.append('New Tab');current[0]=len(tabs)-1
                    elif key=='w' and 'cmd' in request['modifiers']:
                        if current[0]==0:raise ConnectorError('Refused fixture attempt to close an existing user tab')
                        tabs.pop(current[0]);current[0]=0
                if op=='type':
                    if current[0]==0:raise ConnectorError('Refused fixture attempt to change the existing user tab')
                    tabs[current[0]]='Google — https://google.com'
                if op=='snapshot':
                    return {'title':tabs[current[0]],'full':False,'nodes':[
                        {'path':[0],'depth':0,'role':'AXTextField','sub':'','name':'Address bar',
                         'desc':'Address and search bar','value':('https://google.com' if tabs[current[0]].startswith('Google') else '') if current[0] else 'http://127.0.0.1:8765',
                         'enabled':True}]}
                return {'ok':True}
            def approve(request):
                approvals.append(request['title'])
                return not deny_close
            tid=hub.new_chat()['id']
            session=hub._session(tid)
            session.approver=approve
            events=[]
            session.sink=events.append
            started=time.monotonic()
            # The accessibility bridge and browser transport are both replaced,
            # so even choosing the wrong connector cannot touch a real browser.
            with patch.object(Mac,'_run',side_effect=run),patch.object(Mac,'claim'), \
                 patch.object(Browser,'call',side_effect=ConnectorError('Wrong browser profile chosen')):
                result=session.run('hey can you open a new tab in this current browser just enter google.com and close that tab')
            actions=[r for r in sent if r['op'] in ('key','type')]
            opened=any(r['op']=='key' and r['key']=='t' for r in actions)
            navigated=any(r['op']=='type' and 'google.com' in r['text'] for r in actions)
            closed=any(r['op']=='key' and r['key']=='w' for r in actions)
            passed=(opened and navigated and (not closed if deny_close else closed)
                    and tabs==(['Claire','Google — https://google.com'] if deny_close else ['Claire'])
                    and (not deny_close or any(word in result['answer'].lower() for word in ('denied','approve','approval','not close','not closed'))))
            entry={'case':'close_denied' if deny_close else 'close_approved','pass':passed,
                   'status':result['status'],'seconds':round(time.monotonic()-started,3),
                   'answer':result['answer'],'actions':actions,'approval_count':len(approvals),
                   'model_calls':len([e for e in events if e['type']=='model_call']),
                   'tool_calls':[{'name':n,'action':a.get('action'),'connector':a.get('connector'),'failed':f,'arguments':a,'result':text[:900]}
                                 for n,a,text,f in session._turn_outcomes]}
            rows.append(entry)
            print(json.dumps(entry),flush=True)
        (root/'results.json').write_text(json.dumps(rows,indent=2))
        print(json.dumps({'passed':sum(r['pass'] for r in rows),'total':len(rows),'artifacts':str(root)}),flush=True)
        return 0 if all(r['pass'] for r in rows) else 1
    finally:
        hub.stop()
        core.store.close()


if __name__=='__main__':
    raise SystemExit(main())
