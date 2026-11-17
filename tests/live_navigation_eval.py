"""Exercise real controller navigation and recovery in disposable state.

Read-only host listings; all writes and chat history stay in a temporary fixture.
Run: python3 -m tests.live_navigation_eval [--api URL --model ID]
"""
import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core import boot
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--api')
    ap.add_argument('--model', default='')
    args = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix='claire-navigation-eval-', dir='/private/tmp'))
    settings = Settings(state_dir=str(root/'state'), vault=str(root/'vault'),
                        controller_enabled=True, skill_router=False, auto_parallel=False,
                        secret_backend='file', controller_max_steps=6)
    release_splash_on_exit(settings)
    if args.api:
        settings.roles['talk'] = {'api_base': args.api, 'model': args.model}
    core = Core(settings)
    hub = Hub(core, start_brain=False)
    # Nothing else loads the conversation model here; it is no longer a
    # separate server that is always running.
    problem = boot.ensure_role(settings, settings.controller_role)
    if problem: raise SystemExit(problem)
    events = []
    inference = []
    real_chat = core.llm.chat
    def measured_chat(*args, **kwargs):
        result = real_chat(*args, **kwargs)
        inference.append({'reasoning_chars':len(result.reasoning or ''), 'completion_tokens':result.completion_tokens})
        return result
    core.llm.chat = measured_chat
    publish = hub.publish
    def collect(event):
        events.append(event)
        publish(event)
    hub.publish = collect
    rows = []

    def chat():
        tid = core.store.create_thread('home', 'Navigation evaluation', str(root))
        return hub._session(tid)

    def turn(s, name, request, expected_path=None, kind=None, must_call=True):
        start = time.monotonic()
        offset = len(events)
        call_offset = len(inference)
        before = len(s.messages)
        outcome = s.run(request)
        seen = events[offset:]
        receipts = [m['content'].partition('\n')[2] for m in s.messages[before:]
                    if m['role']=='user' and isinstance(m['content'],str)
                    and m['content'].startswith('[local_files result]\n')]
        failures = [r for r in receipts if r.startswith(('ERROR:', 'REFUSED:'))]
        calls = [e for e in seen if e['type']=='model_call']
        answer = outcome.get('answer', '')
        good = (outcome['status']=='done' and not failures
                and not any(e['type']=='tool_call' and e['name']=='spawn_task' for e in seen))
        if must_call:
            good = good and bool(receipts)
        if expected_path:
            good = good and any(r.startswith('Verified: listed '+expected_path+' (') for r in receipts)
        if kind:
            good = good and any((' '+kind+' entries;') in r for r in receipts)
        if name.startswith('recovery'):
            good = good and 'pending' not in answer.lower() and 'please wait' not in answer.lower()
        row = {'case':name, 'pass':bool(good), 'seconds':round(time.monotonic()-start,3),
               'ttft':round(calls[0]['ttft'],3) if calls else None,
               'steps':len(calls), 'reasoning_chars':sum(r['reasoning_chars'] for r in inference[call_offset:]), 'failures':failures,
               'tools':[e['name'] for e in seen if e['type']=='tool_call'],
               'answer':answer[:600]}
        rows.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
        (root/'results.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
        return outcome

    try:
        s=chat()
        turn(s,'greeting','hey claire',must_call=False)
        turn(s,'lowercase_desktop','which folders are on desktop?',os.path.expanduser('~/Desktop'),'directories')
        turn(s,'root_after_desktop','tell me all paths at root','/')
        turn(s,'home_after_root','now list all contents of my home directory',''+os.path.expanduser('~'),'all')
        turn(chat(),'pc_desktop','umm my pc desktop — show me its folders',os.path.expanduser('~/Desktop'),'directories')
        turn(chat(),'downloads','which folders are in my downloads?',os.path.expanduser('~/Downloads'),'directories')
        turn(chat(),'natural_location','show me the download folder contents',os.path.expanduser('~/Downloads'),'all')
        turn(chat(),'system_directory','what is in /Applications?','/Applications')
        # A stale failed call and false promise reproduce the original follow-up.
        s=chat()
        s.messages += [
            {'role':'user','content':'tell me all paths at root'},
            {'role':'assistant','content':json.dumps({'tool':'local_files','arguments':{'action':'list_directory','path':'~'}})},
            {'role':'user','content':'[local_files result]\nREFUSED: give an absolute path or ~/path'},
            {'role':'assistant','content':json.dumps({'tool':'done','arguments':{'summary':'To list root I need /Users/soham. Let me try that now.'}})}]
        turn(s,'recovery_false_promise','um okay, so where are the results?','/')
        # Preserve follow-up context while navigating an actual listed fixture.
        fixture=root/'projects'; fixture.mkdir(); (fixture/'alpha').mkdir(); (fixture/'note.txt').write_text('hello')
        s=chat()
        turn(s,'fixture_folders',f'which folders are in {fixture}?',str(fixture),'directories')
        turn(s,'fixture_all_after_folders',f'Now list all contents of {fixture}, including files',str(fixture),'all')
        turn(s,'navigate_listed_child','show me what is inside alpha',str(fixture/'alpha'))
        print(json.dumps({'results':str(root/'results.json'),'passed':sum(r['pass'] for r in rows),'total':len(rows)}),flush=True)
    finally:
        hub.stop()
    if not all(r['pass'] for r in rows):
        raise SystemExit(1)


if __name__=='__main__':
    main()
