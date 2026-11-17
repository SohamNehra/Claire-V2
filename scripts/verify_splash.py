#!/usr/bin/env python3
"""Live Splash integration checks using disposable Claire state and projects."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from claire_core import boot
from claire_core.bench import BRAIN_TASKS
from claire_core.engine import Core, Session
from claire_core.settings import Settings


def listening(port):
    with socket.socket() as s:
        s.settimeout(.5)
        return s.connect_ex(('127.0.0.1',port))==0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profiles',nargs='+',default=['uncensored27','standard27','fast35'])
    p.add_argument('--worker-checks',action='store_true',help='Run seven focused coding checks on uncensored27')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    results=[]
    for i,name in enumerate(a.profiles):
        port=18010+i
        if listening(port):raise RuntimeError('Validation port is already occupied')
        folder=a.output/name;folder.mkdir(exist_ok=True)
        settings=Settings(worker_profile=name,state_dir=str(folder/'state'),vault=str(folder/'vault'),
                          secret_backend='file',skill_router=False,auto_parallel=False)
        settings.roles={}
        settings.brain_profile['port']=port
        settings.api_base=f'http://127.0.0.1:{port}/v1'
        core=Core(settings)
        row={'profile':name,'model':settings.model}
        start=time.monotonic()
        try:
            problem=boot.ensure_brain(settings,say=lambda text:print(text,flush=True))
            if problem:raise RuntimeError(problem)
            row['load_seconds']=round(time.monotonic()-start,2)
            info=core.llm.describe(refresh=True)
            assert info['live'] and info['backend']=='splash' and info['ctx']==32768,info
            row['metadata']=info
            normal=core.llm.chat([{'role':'user','content':'What is 2 + 2? Reply with only the number.'}],
                                  thinking=False,max_tokens=64,temperature=0)
            assert normal.content.strip()=='4' and not normal.reasoning,(normal.content,normal.reasoning)
            row['normal_mode']={'answer':normal.content,'reasoning_tokens_present':bool(normal.reasoning)}
            thought=core.llm.chat([{'role':'user','content':'What is 2 + 2? Reply with only the number.'}],
                                   thinking=True,max_tokens=256,temperature=0)
            assert thought.reasoning and thought.content.strip()=='4',(thought.content,thought.reasoning)
            row['thinking_mode']={'answer':thought.content,'reasoning_tokens_present':bool(thought.reasoning)}
            # A separate Claire process must adopt the verified same instance,
            # rather than spawn a duplicate after a service restart.
            code='''from claire_core.settings import Settings
from claire_core import boot
import sys
s=Settings(worker_profile=sys.argv[1],state_dir=sys.argv[2],auto_parallel=False)
s.brain_profile['port']=int(sys.argv[3]);s.api_base='http://127.0.0.1:'+sys.argv[3]+'/v1'
problem=boot.ensure_brain(s)
if problem:raise SystemExit(problem)
'''
            subprocess.run([sys.executable,'-c',code,name,settings.state_dir,str(port)],
                           env=dict(os.environ,PYTHONPATH=str(ROOT)),check=True,timeout=20)
            row['restart_adoption']=True
            if a.worker_checks and name=='uncensored27':
                checks=[]
                for task,files,check,prompt in BRAIN_TASKS:
                    work=folder/'projects'/task;work.mkdir(parents=True,exist_ok=True)
                    for filename,body in files.items():(work/filename).write_text(body)
                    before={f:hashlib.sha1((work/f).read_bytes()).hexdigest() for f in files}
                    session=Session(core,kind='agent',workdir=str(work),profile='worker',skills=['coding'],
                                    auto_approve=True,approver=lambda request:False,persist=False)
                    begin=time.monotonic();outcome=session.run(prompt)
                    try:
                        passed,why=check(str(work),outcome['answer'],before) if outcome['status']=='done' else (False,outcome['status'])
                    except Exception as e:passed,why=False,str(e)
                    result=dict(task=task,passed=bool(passed),why=why,seconds=round(time.monotonic()-begin,2))
                    checks.append(result);print(json.dumps(result),flush=True)
                    (work/'transcript.json').write_text(json.dumps(session.messages,indent=2))
                    (folder/'worker-checks.json').write_text(json.dumps(checks,indent=2))
                row['worker_checks']=checks
            # Fast-forward only the disposable power manager's idle clock.
            core.power._clock=lambda:10000.
            core.power._last_activity=0.
            assert core.power.sleep_if_idle(),core.power.status()
            assert not listening(port),'Splash remained loaded after idle sleep'
            row['idle_unload']=True
            if name=='uncensored27':
                # The next real model call must load it again automatically.
                woke=core.llm.chat([{'role':'user','content':'What is 2 + 2? Reply with only the number.'}],
                                    max_tokens=64,temperature=0)
                assert woke.content.strip()=='4' and listening(port)
                row['wake_on_next_request']=True
            row['integration_passed']=True
        except Exception as e:
            row['integration_passed']=False;row['error']=repr(e)
            print(json.dumps(row),flush=True)
        finally:
            cleanup=boot.unload_models(settings)
            row['cleanup']=cleanup
            core.connectors.close();core.store.close()
            row['total_seconds']=round(time.monotonic()-start,2)
            results.append(row)
            (a.output/'results.json').write_text(json.dumps(results,indent=2))
            print(json.dumps(row),flush=True)
            if cleanup['errors'] or listening(port):raise RuntimeError('Validation cleanup failed')
    return 0 if all(row.get('integration_passed') for row in results) else 1


if __name__=='__main__':raise SystemExit(main())
