"""Batch-review regressions: observable tool behavior and recoverable errors."""
import io
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from claire_core import protocol
from claire_core.connectors import ConnectorError
from claire_core.llm import Cancelled
from claire_core.tools import Tool, ToolContext
from claire_core.tools import files, scout, shell, web, integrations, connector, memory, schedule, tasks
from tests.helpers import Env, symlink_or_skip


class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.env=Env();self.addCleanup(self.env.close)
        self.s=self.env.session();self.ctx=ToolContext(self.s)

    def file(self,name,body):
        path=os.path.join(self.env.work,name)
        os.makedirs(os.path.dirname(path),exist_ok=True)
        with open(path,'w') as f:f.write(body)
        return path

    def test_numeric_file_arguments_are_not_silently_replaced_with_defaults(self):
        self.file('a.py','one\ntwo\n')
        self.assertTrue(files.read_file(self.ctx,{'path':'a.py','start':'second line'}).startswith('ERROR:'))
        self.assertTrue(files.search_files(self.ctx,{'path':'a.py','pattern':'one','context':'three'}).startswith('ERROR:'))

    def test_scheduled_project_work_recovers_an_omitted_workdir(self):
        from claire_core.scheduler import ScheduleError
        self.s._active_request='Run the tests every morning in '+self.env.work+'.'
        scheduler=Mock();scheduler.validate.side_effect=ScheduleError('fixture stop')
        self.ctx.hub=SimpleNamespace(scheduler=scheduler)
        schedule.schedule_task(self.ctx,{'task':'Run tests','cron':'0 9 * * *'})
        self.assertEqual(scheduler.validate.call_args.args[-1],os.path.realpath(self.env.work))

    def test_unknown_skills_are_rejected_before_creating_a_workspace(self):
        resolve=Mock();self.ctx.hub=SimpleNamespace(resolve_workdir=resolve)
        result=tasks.spawn_task(self.ctx,{'title':'Task','task':'Do it','skills':'missing-skill'})
        self.assertTrue(result.startswith('ERROR:'));resolve.assert_not_called()

    def test_empty_file_is_a_successful_read(self):
        self.file('empty.txt','')
        result,failed,fault=self.s._execute('read_file',{'path':'empty.txt'})
        self.assertFalse(failed);self.assertIsNone(fault);self.assertIn('empty file',result)

    def test_large_file_line_ranges_are_not_limited_to_an_unreported_prefix(self):
        body=''.join(f'line {i} '+('x'*60)+'\n' for i in range(10000))
        self.file('large.txt',body)
        text=files.read_file(self.ctx,{'path':'large.txt','start':'9500','end':'9501'})
        self.assertIn('of 10000',text);self.assertIn('line 9499',text);self.assertNotIn('ERROR:',text)

    def test_oversized_line_reports_an_actionable_error_instead_of_repeating_start(self):
        self.env.core.settings.max_file_chars=40;self.file('line.txt','x'*100+'\nnext\n')
        text=files.read_file(self.ctx,{'path':'line.txt'})
        self.assertTrue(text.startswith('ERROR:'));self.assertIn('line 1 exceeds',text)
        self.assertIn('next',files.read_file(self.ctx,{'path':'line.txt','start':'2'}))

    def test_missing_write_or_replacement_arguments_cannot_erase_content(self):
        path=self.file('keep.py','original\n')
        self.assertTrue(files.write_file(self.ctx,{'path':'keep.py'}).startswith('ERROR:'))
        self.assertTrue(files.edit_file(self.ctx,{'path':'keep.py','search':'original'}).startswith('ERROR:'))
        with open(path) as f:self.assertEqual(f.read(),'original\n')
        files.write_file(self.ctx,{'path':'keep.py','content':''})
        self.assertEqual(os.path.getsize(path),0)

    def test_flexible_edits_reject_ambiguous_matches_and_preserve_relative_indent(self):
        result,reason=files.flexible_replace('  answer=1\n    answer=1\n','answer=1 ','answer=2')
        self.assertIsNone(result);self.assertIn('2 times',reason)
        result,_=files.flexible_replace('def f():\n    answer=1\n',' answer=1 ', '    if ready:\n        return 2')
        self.assertEqual(result,'def f():\n    if ready:\n        return 2\n')

    def test_search_and_listing_support_an_explicit_single_file(self):
        self.file('a.py','NEEDLE = 42\n')
        self.assertIn('a.py:1: NEEDLE',files.search_files(self.ctx,{'path':'a.py','pattern':'NEEDLE'}))
        self.assertEqual(files.list_files(self.ctx,{'path':'a.py'}),'a.py')
        self.assertTrue(files.search_files(self.ctx,{'path':'missing','pattern':'x'}).startswith('ERROR:'))

    def test_recursive_search_and_scout_do_not_follow_outside_or_protected_file_links(self):
        outside=os.path.join(self.env.root,'outside.txt')
        protected=os.path.join(self.env.core.settings.state_dir,'private.txt')
        for path in [outside,protected]:
            with open(path,'w') as f:f.write('PRIVATE_SENTINEL\n')
        symlink_or_skip(outside,os.path.join(self.env.work,'outside-link'))
        symlink_or_skip(protected,os.path.join(self.env.work,'state-link'))
        self.file('safe.txt','public data')
        self.assertNotIn('PRIVATE_SENTINEL',files.search_files(self.ctx,{'pattern':'PRIVATE_SENTINEL'}))
        sources,problems=scout.gather_files(self.env.work,'.',self.ctx.settings.skip_dirs,self.ctx.settings)
        self.assertEqual([name for name,_ in sources],['safe.txt']);self.assertEqual(len(problems),2)

    def test_scout_file_limit_is_enforced_before_unbounded_reads(self):
        for i in range(5):self.file(f'f{i}.txt','public')
        with patch.object(scout,'MAX_FILES',2):
            with self.assertRaisesRegex(ValueError,'more than 2'):
                scout.gather_files(self.env.work,'.',self.ctx.settings.skip_dirs,self.ctx.settings)

    def test_scout_reports_truncated_sources_and_rejects_unfittable_lines(self):
        self.file('long.txt','a'*100)
        with patch.object(scout,'MAX_FILE_CHARS',20):
            sources,problems=scout.gather_files(self.env.work,'long.txt',self.ctx.settings.skip_dirs,self.ctx.settings)
            self.assertEqual(len(sources[0][1]),20);self.assertIn('tail omitted',problems[0])
        with self.assertRaisesRegex(ValueError,'exceeds the scout context budget'):
            scout.bundle([('long.txt','a'*1000)],300)

    def test_scout_single_path_does_not_split_a_filename_containing_commas(self):
        self.file('report, final.txt','HELLO')
        stats={'calls':1,'model':'fixture','completion_tokens':1,'gen_seconds':1}
        with patch.object(scout,'run_scouts',return_value=('Read it',stats)) as run:
            result=scout.scout(self.ctx,{'question':'What is there?','path':'report, final.txt'})
            self.assertIn('Read it',result)
            self.assertEqual(run.call_args.args[3][0],('report, final.txt','HELLO'))

    def test_execution_classifies_denials_negative_exits_and_operational_errors(self):
        for message in ['User DENIED this command.','Sir did not approve this schedule.','exit=-9\nkilled','exit=2\nfailed']:
            self.s.registry.register(Tool('fixture',lambda ctx,args,value=message:value,doc='Fixture'))
            _,failed,fault=self.s._execute('fixture',{})
            self.assertTrue(failed,message);self.assertIsNone(fault)
        for error in [FileNotFoundError('missing'),Cancelled()]:
            def handler(ctx,args,error=error):raise error
            self.s.registry.register(Tool('fixture',handler,doc='Fixture'))
            text,failed,fault=self.s._execute('fixture',{})
            self.assertTrue(failed);self.assertIsNone(fault);self.assertTrue(text.startswith('ERROR:'))
        text,failed,fault=self.s._execute('unregistered',{})
        self.assertTrue(failed);self.assertIsNone(fault);self.assertIn('not available',text)

    def test_readonly_git_rejects_option_injection(self):
        with patch('claire_core.tools.shell.subprocess.run') as run:
            result=shell.git(self.ctx,{'what':'show','path':'--output=/tmp/unsafe'})
            self.assertTrue(result.startswith('ERROR:'));run.assert_not_called()

    def test_branch_filter_cannot_create_a_branch(self):
        result=subprocess.CompletedProcess([],0,'','')
        with patch('claire_core.tools.shell.subprocess.run',return_value=result) as run:
            shell.git(self.ctx,{'what':'branch','path':'feature-name'})
            self.assertEqual(run.call_args.args[0][-2:],['--list','feature-name'])

    def test_failed_git_queries_are_not_reported_as_success(self):
        result=subprocess.CompletedProcess([],128,'','fatal: ambiguous argument')
        with patch('claire_core.tools.shell.subprocess.run',return_value=result):
            self.assertTrue(shell.git(self.ctx,{'what':'show'}).startswith('ERROR:'))

    def test_markdown_fences_and_think_literals_survive_file_tool_parsing(self):
        body='# Guide\n```python\nprint(1)\n```\nLiteral <think>data</think>\n'
        reply='<write_file><path>guide.md</path><content>'+body+'</content></write_file>'
        for wrapped in [reply,'```xml\n'+reply+'\n```']:
            calls=protocol.parse_tool_calls(wrapped,['write_file'])
            self.assertEqual(calls[0][1]['content'],body)

    def test_literal_closing_tool_tag_cannot_execute_a_truncated_write(self):
        reply='<write_file><path>a.py</path><content>text = "</write_file>"\n</content></write_file>'
        self.assertEqual(protocol.parse_tool_calls(reply,['write_file']),[])

    def test_future_fix_promise_is_not_accepted_as_completed_work(self):
        from claire_core.hub import Hub
        from tests.test_controller import decision
        self.env.core.settings.controller_enabled=True
        hub=Hub(self.env.core,start_brain=False);self.addCleanup(hub.stop)
        self.env.llm.replies=[decision('local_files',action='list_directory',path=self.env.work),
                              decision('done',summary='I will fix the parser now.'),
                              decision('spawn_task',title='Fix parser',task='Fix the parser and run tests.',workdir=self.env.work),
                              decision('done',summary='The task is under way.')]
        result=hub._session('home').run('Fix the parser in '+self.env.work)
        self.assertEqual(len(self.env.core.store.threads(kind='task')),1)
        self.assertEqual(result['answer'],'The task is under way.')

    def test_repeated_unfinished_plan_escalates_once_instead_of_ending_done(self):
        from claire_core.hub import Hub
        from tests.test_controller import decision
        self.env.core.settings.controller_enabled=True
        hub=Hub(self.env.core,start_brain=False);self.addCleanup(hub.stop)
        self.env.llm.replies=[decision('local_files',action='list_directory',path=self.env.work),
                              decision('done',summary='I will fix the parser.'),
                              decision('done',summary='I will fix the parser now.')]
        hub._run_turn('home','Fix the parser in '+self.env.work,1)
        tasks=self.env.core.store.threads(kind='task')
        self.assertEqual(len(tasks),1);self.assertEqual(tasks[0]['workdir'],os.path.realpath(self.env.work))

    def test_denial_followed_by_a_promise_does_not_trigger_another_attempt(self):
        from claire_core.hub import Hub
        from tests.test_controller import decision
        self.env.core.settings.controller_enabled=True
        hub=Hub(self.env.core,start_brain=False);self.addCleanup(hub.stop)
        self.env.llm.replies=[decision('local_files',action='write_text',path=self.env.work+'/keep.txt',content='replacement'),
                              decision('done',summary='I will write the file now.')]
        self.file('keep.txt','original')
        result=hub._session('home').run('Create '+self.env.work+'/keep.txt containing replacement')
        self.assertIn('REFUSED:',result['answer']);self.assertEqual(len(self.env.llm.calls),2)

    def test_unknown_memory_kind_is_not_silently_saved_elsewhere(self):
        with patch.object(self.ctx.vault,'remember') as save:
            result=memory.remember(self.ctx,{'kind':'preference','content':'note'})
            self.assertTrue(result.startswith('ERROR:'));save.assert_not_called()

    def test_installed_but_failed_connection_is_not_a_success_result(self):
        stage=os.path.join(self.env.work,'stage');os.mkdir(stage)
        with open(os.path.join(stage,'server.py'),'w') as f:f.write('pass\n')
        view={'id':'fixture','status':'error','error':'offline','credentials':[]}
        with patch.object(integrations.builder,'check',return_value=({},stage,'')), \
             patch.object(integrations.builder,'is_template',return_value=False), \
             patch.object(integrations.builder,'installed_manifest',return_value=None), \
             patch.object(integrations.builder,'summary',return_value='Approve'), \
             patch.object(integrations.builder,'install'), \
             patch.object(self.ctx,'ask',return_value=True), \
             patch.object(self.s.core.connectors,'upsert_built',return_value=view), \
             patch.object(self.s,'refresh'):
            result=integrations._submit(self.ctx,{'name':'fixture'})
        self.assertTrue(result.startswith('ERROR:'));self.assertIn('offline',result)

    def test_integration_notices_do_not_mask_permission_denials(self):
        skills=self.s.core.skills
        with patch.object(skills,'get',return_value=True),patch.object(self.s,'load_skill'),patch.object(integrations,'_run',return_value='Sir did not approve installing this integration.'):
            self.assertTrue(integrations.build_integration(self.ctx,{'action':'submit'}).startswith('Sir did not approve'))

    def test_unavailable_connector_service_is_not_a_broken_tool_fault(self):
        client=self.s.core.connectors
        with patch.object(client,'call',side_effect=ConnectorError('could not reach server')),patch.object(client,'help',return_value='Help'):
            result=connector.use_connector(self.ctx,{'connector':'fixture','action':'list'})
            self.assertTrue(result.startswith('ERROR:'));self.assertIn('service is unavailable',result)

    def test_local_web_fetch_requires_approval_even_in_auto_approve_mode(self):
        self.s.auto_approve=True;self.env.allow=False;self.s.approver=self.env.approver
        with patch('claire_core.tools.web.urllib.request.build_opener') as request:
            self.assertIn('DENIED',web.fetch_url(self.ctx,{'url':'http://localhost./data'}))
            request.assert_not_called()
        self.assertTrue(web._local('http://foo.localhost/'))
        self.assertFalse(web._local('https://[2606:4700:4700::1111]/'))

    def test_plain_text_web_results_preserve_literal_markup(self):
        response=io.BytesIO(b'<tool_call>example</tool_call>')
        response.headers={'Content-Type':'text/plain'}
        opener=SimpleNamespace(open=lambda *a,**k:response)
        with patch('claire_core.tools.web.urllib.request.build_opener',return_value=opener):
            self.assertIn('<tool_call>example</tool_call>',web.fetch_url(self.ctx,{'url':'https://example.com/doc'}))

    def test_search_redirect_urls_are_decoded_exactly_once(self):
        raw=b'<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%252Fb">Example</a>'
        with patch('claire_core.tools.web.urllib.request.urlopen',return_value=io.BytesIO(raw)):
            text=web.web_search(self.ctx,{'query':'example'})
        self.assertIn('https://example.com/a%2Fb',text)
