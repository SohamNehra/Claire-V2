"""Live local-model integration eval: python3 -m tests.live_integration_eval.

Uses fresh temporary state, synthetic API data and an independent acceptance
oracle. Never installs an integration or uses the user's saved credentials.
"""
import argparse
import json, os, tempfile, time
from pathlib import Path
from claire_core.engine import Core, Session
from claire_core.llm import LLM
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


class EvaluationSession(Session):
    def step_budget(self):
        return min(32, super().step_budget())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="Local API base; defaults to Claire's configuration")
    parser.add_argument("--model", help="Model ID; defaults to Claire's configuration")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='claire-live-skills-', dir='/private/tmp'))
    work = root/'work'; work.mkdir()
    settings = Settings(state_dir=str(root/'state'), vault=str(root/'vault'), secret_backend='file',
                        api_base=args.api, model=args.model, roles={},
                        auto_parallel=False, max_steps=32, thinking=False)
    release_splash_on_exit(settings)
    settings.prompt_override = ''
    core = Core(settings)
    folder = work/'catalog_api'; folder.mkdir()
    manifest = {'name':'catalog_api','title':'Catalog API','description':'Read entries from the local evaluation catalog.',
     'hosts':['api.example.com'], 'docs':['https://api.example.com/docs'], 'credentials':[],
     'actions':[{'name':'list_items','effect':'read','description':'Return compact catalog entries.',
                 'params':{'count':'Maximum items, integer 1-20; default 5.'},'required':[]}],
     'check':{'action':'list_items','args':{'count':'1'}}}
    (folder/'integration.json').write_text(json.dumps(manifest, indent=2))
    (folder/'server.py').write_text('''from claire_kit import action, http, Fail, run
@action("list_items")
def list_items(count="5"):
    data = http("GET", "https://api.example.com/v1/items", params={"limit": count}).json()
    return data["items"]
if __name__ == "__main__":
    run()
''')
    (folder/'test.py').write_text('''from claire_kit import testing, call
import server
with testing.fixtures({"GET https://api.example.com/v1/items": {"items": ["old shape"]}}):
    assert call("list_items") == ["old shape"]
print("all tests passed")
''')
    (work/'API_CONTRACT.md').write_text('''# Authoritative contract for this synthetic, offline-only evaluation service
No internet research is needed. This is the sole API specification for this fixture.
GET https://api.example.com/v1/items accepts query `limit`: an integer string 1..20.
It returns {"data": [{"id":"a1","title":"Alpha","large_blob":"omit"}], "next":null}.
Empty success is {"data":[], "next":null}. Return only id/title for at most `count` items.
No authentication. No writes. Invalid count must raise Fail before any HTTP request;
do not silently clamp. HTTP 429 must raise Fail. No restart, install or live calls.
The documentation URL in the manifest labels this synthetic fixture, not a real API.
''')
    (folder/'claire_kit.py').write_bytes(Path('claire_core/connectors/kit/claire_kit.py').read_bytes())
    events=[]
    start=time.monotonic()
    def sink(e):
        events.append(e)
        if e['type'] in ('tool_call','tool_result','notice','model_call','end'):
            print(json.dumps({k:v for k,v in e.items() if k not in ('thread','ts','answer')}, ensure_ascii=False), flush=True)
    session=EvaluationSession(core, workdir=str(work),
                    profile="worker" if settings.controller_enabled else "", skills=['building-integrations'], sink=sink,
                    approver=lambda r: r.get('kind') == 'command' and not r.get('flag'),
                    auto_approve=False, persist=False)
    # This fixture requires no web access, app integrations, scouts or spawned tasks.
    session.tools=[n for n in session.tools if n not in ('web_search','fetch_url','scout','make_tool','run_code')]
    # refresh() on run restores built-ins, so constrain the registry too.
    for n in ('web_search','fetch_url','scout','make_tool','run_code'):
        core.registry.unregister(n)
    request='''Repair the existing catalog_api integration against API_CONTRACT.md, the authoritative
local fixture specification. Read the files, fix the response mapping and count validation,
and add offline tests for normal, empty, invalid/boundary counts, and HTTP 429. Existing test.py
models the obsolete API response and should be corrected to the supplied contract, preserving
useful coverage. Exercise the integration with build_integration check. Do not submit/install,
restart Claire, browse the web, or call a real API. Finish with the actual verification results.'''
    outcome=session.run(request)
    # Independent oracle: model-authored tests are insufficient to score the result.
    oracle='''from claire_kit import testing, call, Fail
import server
URL="GET https://api.example.com/v1/items"
with testing.fixtures({URL:{"data":[{"id":"a1","title":"Alpha","large_blob":"omit"}],"next":None}}) as sent:
    assert call("list_items",count="1") == [{"id":"a1","title":"Alpha"}]
    assert sent[0].params == {"limit":"1"}
with testing.fixtures({URL:{"data":[],"next":None}}):
    assert call("list_items") == []
for count in ("0","21","abc","1.5"):
    with testing.fixtures({URL:{"data":[],"next":None}}) as sent:
        try: call("list_items",count=count)
        except Fail: pass
        else: raise AssertionError("invalid count accepted: "+count)
        assert not sent
with testing.fixtures({URL:testing.Reply(429,{"error":"rate limited"})}):
    try: call("list_items")
    except Fail: pass
    else: raise AssertionError("429 swallowed")
print("Independent acceptance cases passed")
'''
    (folder/'oracle.py').write_text(oracle)
    result=core.sandbox.run([os.sys.executable,'oracle.py'],cwd=str(folder),timeout=30)
    report={'model':settings.model,'outcome':outcome,'independent_pass':result.returncode==0,
            'oracle_output':result.output[-2500:],'elapsed_seconds':round(time.monotonic()-start,1),
            'steps':sum(e['type']=='step' for e in events),'artifacts':str(root)}
    (root/'report.json').write_text(json.dumps(report,indent=2))
    (root/'events.json').write_text(json.dumps(events,indent=2))
    print('LIVE_EVAL_RESULT '+json.dumps(report),flush=True)
    core.connectors.close(); core.store.close()
    return 0 if report["independent_pass"] and outcome["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
