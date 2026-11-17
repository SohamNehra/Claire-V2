"""Real-model UAT against a disposable local form and screenshot-only canvas.

Run explicitly: python3 -m tests.live_computer_uat. Existing user tabs are guarded;
only the owned fixture tab can change. This contacts the installed local models.
"""
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

from claire_core import boot
from claire_core.client import Client
from claire_core.connectors import ConnectorError
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core.settings import Settings
from claire_core.tools import ToolContext
from tests.live_cleanup import release_splash_on_exit

PAGE=r'''<!doctype html><meta charset="utf-8"><title>Claire UI Check</title>
<style>body{margin:24px;background:white;color:#111;font:22px Arial}input,button{font:22px Arial;padding:10px}canvas{display:block;margin-top:26px;border:2px solid #333}</style>
<label>Project name <input id="name" aria-label="Project name"></label> <button id="save">Save</button><p id="status">Nothing saved.</p>
<canvas id="canvas" width="900" height="460"></canvas>
<script>
const canvas=document.querySelector('#canvas'),ctx=canvas.getContext('2d');let active=false;
function draw(){ctx.fillStyle='white';ctx.fillRect(0,0,900,460);ctx.fillStyle='#145adc';ctx.fillRect(60,60,280,240);ctx.fillStyle='#eb7814';ctx.beginPath();ctx.arc(650,190,110,0,Math.PI*2);ctx.fill();ctx.font='bold 44px Arial';ctx.fillStyle='#111';ctx.fillText(active?'ACTIVATED 5926':'CODE 5926',60,390);}
function send(path,data){return fetch(path,{method:'POST',body:JSON.stringify(data)});}
function geometry(){const r=canvas.getBoundingClientRect();send('/geometry',{x:r.x,y:r.y,w:r.width,h:r.height,vh:innerHeight,vw:innerWidth});}
save.onclick=()=>{document.querySelector('#status').textContent='Saved: '+document.querySelector('#name').value;send('/saved',{value:document.querySelector('#name').value});};
canvas.onclick=e=>{const r=canvas.getBoundingClientRect(),x=(e.clientX-r.x)*900/r.width,y=(e.clientY-r.y)*460/r.height;if(Math.hypot(x-650,y-190)<110){active=true;draw();send('/activated',{active:true});}};
draw();setTimeout(geometry,100);onresize=geometry;
</script>'''


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):
        data=PAGE.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        self.server.state[self.path]=data
        self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')


def main():
    live=Client(Settings());status=live.get('/status')
    if status.get('power',{}).get('busy') or status['pending_approvals']:raise RuntimeError('Claire is busy; do not compete for its GUI')
    root=Path(tempfile.mkdtemp(prefix='claire-computer-uat-',dir='/private/tmp'))
    server=ThreadingHTTPServer(('127.0.0.1',0),Fixture);server.state={}
    threading.Thread(target=server.serve_forever,daemon=True).start()
    settings=Settings(state_dir=str(root/'state'),vault=str(root/'vault'),secret_backend='file',controller_enabled=True,skill_router=False,auto_parallel=False)
    release_splash_on_exit(settings)
    core=Core(settings);hub=Hub(core,start_brain=False);provider=core.connectors.providers['mac']
    # Nothing else loads the conversation model here; it is no longer a
    # separate server that is always running.
    problem = boot.ensure_role(settings, settings.controller_role)
    if problem: raise SystemExit(problem)
    native=provider._run;session=None;ref=None;reports=[]
    try:
        provider.claim({},'live-check')
        time.sleep(.5)
        iid=core.connectors.create('mac','Mac apps',{'apps':'Google Chrome'})['id']
        assert core.connectors.test(iid)['status']=='connected'
        problem=boot.ensure_role(settings,'talk')
        if problem:raise RuntimeError(problem)
        session=hub._session('home');ctx=ToolContext(session)
        native(op='open',app='Google Chrome')
        try:before=native(op='browser_state',app='Google Chrome')
        except ConnectorError:before={'tabs':[]}
        url=f'http://127.0.0.1:{server.server_address[1]}/'
        opened=core.computer.call(ctx,{'action':'new_tab','app':'Google Chrome','url':url});ref=opened['tab']
        assert opened['verified'],opened
        def fixture_selected():
            item=core.computer.tabs.get(ref);state=native(op='browser_state',app='Google Chrome',process_id=(item or {}).get('state',{}).get('window',{}).get('process_id'))
            return (item is not None and len(state['tabs'])==len(before['tabs'])+1 and state['tabs'][item['index']]['selected']
                    and state['url'].replace('http://','').replace('https://','').rstrip('/')==url.replace('http://','').rstrip('/'))
        session.approver=lambda r:'click' in r['title'] and 'Save' in r['title'] and fixture_selected()
        def guarded(**args):
            if args['op'] in ('key','type','click','set_value','click_at') and not fixture_selected():
                raise ConnectorError('UAT refused a change outside its owned fixture tab')
            if args['op']=='click_at':
                geo=server.state.get('/geometry');win=native(op='window',app='Google Chrome')
                if not geo:raise ConnectorError('UAT has no verified canvas bounds')
                left=win['x']+geo['x'];top=win['y']+win['h']-geo['vh']+geo['y']
                if not(left<args['x']<left+geo['w'] and top<args['y']<top+geo['h']):
                    raise ConnectorError('UAT refused a visual click outside its disposable canvas')
            return native(**args)
        provider._run=guarded
        cases=[('form','In this current Google Chrome tab, set Project name to ClaireCheck7349, click the Save button, and verify the saved value.',lambda a:server.state.get('/saved',{}).get('value')=='ClaireCheck7349'),
               ('vision_read','In this current Google Chrome tab, what color is the rectangle drawn in the canvas and what code is printed inside the canvas? Use actual vision rather than guessing.',lambda a:'blue' in a.lower() and '5926' in a),
               ('vision_click','In this current Google Chrome tab, click the orange circle drawn in the canvas, then read and report the resulting text displayed inside the canvas. Use actual visual inspection.',lambda a:server.state.get('/activated',{}).get('active') and 'ACTIVATED' in a.upper())]
        for name,request,accept in cases:
            events=[];session.sink=events.append;start=time.monotonic()
            outcome=session.run(request)
            report={'case':name,'pass':bool(outcome['status']=='done' and accept(outcome['answer'])),
                    'seconds':round(time.monotonic()-start,3),'status':outcome['status'],'answer':outcome['answer'],
                    'actions':[{'tool':n,'action':a.get('action'),'failed':f,'result':t[:200] if f else ''} for n,a,t,f in session._turn_outcomes],
                    'model_calls':sum(e['type']=='model_call' for e in events)}
            reports.append(report);print(json.dumps(report),flush=True)
        return 0 if all(r['pass'] for r in reports) else 1
    finally:
        provider._run=native
        if session is not None:
            for owned_ref,item in list(core.computer.tabs.items()):
                if item['owner']!=session.thread_id or not item['owned']:continue
                try:print(json.dumps(core.computer.call(ToolContext(session),{'action':'close_tab','tab':owned_ref})),flush=True)
                except Exception as exc:print('Cleanup refused changed browser state: '+str(exc),flush=True)
        hub.stop();core.store.close();server.shutdown();server.server_close()
        (root/'results.json').write_text(json.dumps(reports,indent=2))
        print('Artifacts: '+str(root),flush=True)


if __name__=='__main__':raise SystemExit(main())
