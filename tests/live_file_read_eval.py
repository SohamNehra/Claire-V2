"""Real-model file discovery and paged reading; writes confined to fixtures."""
import json
import re
import os
import tempfile
import time
import uuid
from pathlib import Path
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core import boot
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


def main():
    root=Path(tempfile.mkdtemp(prefix='claire-file-read-eval-',dir='/private/tmp'))
    work=root/'documents';work.mkdir()
    (work/'FABLE5_CAPABILITIES.md').write_text('# AsterDesk scheduler reference\nThis document describes the fictional AsterDesk scheduling tool, its queues and retry policy.\n')
    codes=['ASTER-'+uuid.uuid4().hex[:8] for _ in range(3)]
    body='# Long operations report\nStart code: '+codes[0]+'\n'
    body+='Routine operational notes for the scheduling service.\n'*170
    body+='\nMiddle code: '+codes[1]+'\n'
    body+='Additional background notes about retries and queues.\n'*170
    body+='\nFinal code: '+codes[2]+'\n'
    (work/'Operations Report.md').write_text(body)
    settings=Settings(state_dir=str(root/'state'),vault=str(root/'vault'),secret_backend='file',controller_enabled=True,skill_router=False,auto_parallel=False)
    release_splash_on_exit(settings)
    core=Core(settings);hub=Hub(core,start_brain=False);events=[];publish=hub.publish
    # Nothing else loads the conversation model here; it is no longer a
    # separate server that is always running.
    problem = boot.ensure_role(settings, settings.controller_role)
    if problem: raise SystemExit(problem)
    def capture(event):events.append(event);publish(event)
    hub.publish=capture
    tid=core.store.create_thread('home','File reading check',str(work));s=hub._session(tid)
    rows=[]
    def turn(name,text,check):
        offset=len(events);start=time.monotonic();result=s.run(text)
        seen=events[offset:];tools=[e['name'] for e in seen if e['type']=='tool_call'];failed=[e for e in seen if e['type']=='tool_result' and e['failed']]
        coverage=True
        if name=='full_paged_document':
            end=0
            spans=[]
            for tool,args,receipt,bad in s._turn_outcomes:
                if tool=='local_files' and args.get('action')=='read_text' and not bad:
                    header,data=receipt.split('\n',1)
                    match=re.search(r'offset=(\d+), characters=(\d+)',header)
                    begin,n=map(int,match.groups());coverage=coverage and n==len(data);spans.append((begin,begin+n))
            for begin,stop in sorted(spans):
                coverage=coverage and begin<=end;end=max(end,stop)
            coverage=coverage and end==len(body)
        good=coverage and result['status']=='done' and not failed and all(t=='local_files' for t in tools) and check(result.get('answer',''))
        row={'case':name,'pass':bool(good),'seconds':round(time.monotonic()-start,3),'tools':tools,'failed_calls':len(failed),'answer':result.get('answer','')[:600]};rows.append(row)
        print(json.dumps(row),flush=True);(root/'results.json').write_text(json.dumps(rows,indent=2))
    try:
        turn('list_documents',f'List files in {work}',lambda a:'FABLE5_CAPABILITIES.md' in a)
        turn('natural_file_reference','read fable5 capabilities file and tell me what it is. Only read and describe it.',lambda a:'AsterDesk' in a)
        turn('full_paged_document',f'Read the entire file "{work}/Operations Report.md", including the final section. Tell me the exact start, middle and final codes. Read all required pages; do not guess or delegate.',lambda a:all(code in a for code in codes))
        print(json.dumps({'passed':sum(r['pass'] for r in rows),'total':len(rows),'characters':len(body),'results':str(root/'results.json')}),flush=True)
    finally:hub.stop();core.store.close()
    return 0 if all(r['pass'] for r in rows) else 1


if __name__=='__main__':raise SystemExit(main())
