import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from claire_core import boot, splash
from claire_core.llm import LLM
from claire_core.settings import Settings
from tests.helpers import managed_server_spec


class SplashConfigurationTests(unittest.TestCase):
    def test_named_profiles_select_source_endpoint_and_model_together(self):
        # The uncensored 27B serves its native window (2026-11-17); the others 32K.
        for name, port, context in [('uncensored27',8080,262144),('standard27',8082,32768),('fast35',8083,32768)]:
            settings = Settings(worker_profile=name)
            self.assertEqual(settings.worker_profile, name)
            self.assertEqual(settings.brain_server, 'splash')
            self.assertEqual(settings.api_base, f'http://127.0.0.1:{port}/v1')
            spec = managed_server_spec(settings, 'brain')
            self.assertEqual(spec['kind'], 'splash')
            self.assertEqual(spec['model'], settings.model)
            self.assertEqual(spec['context'], context)
            self.assertNotIn('/private/tmp/', spec['root'])
            self.assertNotIn('/private/tmp/', spec['draft'])

    def test_invalid_profile_is_rejected_without_falling_back_to_another_model(self):
        with self.assertRaisesRegex(ValueError, 'Unknown worker profile'):
            Settings(worker_profile='does-not-exist')

    def test_remote_override_is_not_managed_as_a_local_splash_process(self):
        with patch.dict(os.environ, {'CLAIRE_API':'http://remote.example/v1'}):
            settings = Settings()
        self.assertIsNone(managed_server_spec(settings, 'brain'))

    def test_conversation_and_reading_run_on_the_worker_with_no_second_model(self):
        settings = Settings()
        llm = LLM(settings)
        for role in ['talk','scout','clerk']:
            self.assertNotIn(role, settings.roles)
            self.assertFalse(llm.has_own_model(role))
            self.assertEqual(llm.endpoint(role), llm.endpoint('brain'))
            with patch.object(boot,'server_spec',managed_server_spec):
                self.assertEqual(boot.lifecycle_spec(settings, role)['kind'], 'splash')

    def test_the_small_model_stays_configured_for_a_one_line_return(self):
        import config
        self.assertIn('Qwen_Qwen3.5-9B-Q4_K_M.gguf', config._SMALL['gguf'])
        settings = Settings()
        settings.roles['talk'] = dict(config._SMALL, api_base='http://127.0.0.1:8081/v1')
        spec = managed_server_spec(settings, 'talk')
        self.assertNotEqual(spec.get('kind'), 'splash')
        self.assertEqual(spec['args'], ['--spec-type', 'ngram-simple'])

    def test_splash_command_uses_full_verification_and_claire_idle_policy(self):
        s = managed_server_spec(Settings(), 'brain')
        command = splash.command(s)
        for key,value in [('--verify-rows','all'),('--idle-release','off'),
                          ('--max-context','262144')]:
            self.assertEqual(command[command.index(key)+1],value)
        self.assertIn('--language-only',command)
        self.assertEqual(command[0],os.path.join(s['root'],'python','bin','python3'))

    def test_prefix_cache_persists_under_the_state_directory_or_is_off(self):
        s = managed_server_spec(Settings(state_dir='/state'), 'brain')
        command = splash.command(s)
        self.assertEqual(command[command.index('--max-cache-disk')+1],'2G')
        self.assertIn('--persistent-cache',command)
        self.assertEqual(command[command.index('--cache-dir')+1],os.path.normpath(os.path.join('/state','cache','splash-8080')))
        off = splash.command(dict(s,prefix_cache='0'))
        self.assertEqual(off[off.index('--max-cache-disk')+1],'0')
        self.assertNotIn('--persistent-cache',off)
        self.assertNotIn('--cache-dir',off)

    def test_memory_estimate_does_not_count_the_draft_file_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            target, draft = Path(folder)/'target.gguf', Path(folder)/'draft'
            draft.mkdir()
            with target.open('wb') as f:f.truncate(16*splash.GIB)
            with (draft/'model.safetensors').open('wb') as f:f.truncate(4*splash.GIB)
            s = dict(path=str(target),draft=str(draft),model='m')
            settings = SimpleNamespace(auto_parallel=True,model_runtime_gb=4,ram_reserve_gb=4)
            total = 64*splash.GIB
            # 16*1.25 + 4 runtime = 24 GiB, plus the 6.4 GiB (10%) reserve.
            with patch.object(splash,'memory_snapshot',return_value={'total_bytes':total,'available_bytes':31*splash.GIB}):
                self.assertIsNone(splash._memory_problem(settings,s))
            with patch.object(splash,'memory_snapshot',return_value={'total_bytes':total,'available_bytes':30*splash.GIB}):
                self.assertIn('Not starting',splash._memory_problem(settings,s))


class SplashClientTests(unittest.TestCase):
    def test_metadata_uses_advertised_context_and_never_lmstudio_or_llama_props(self):
        llm = LLM(Settings(worker_profile='uncensored27',ctx_limit=0))
        calls = []
        def get(url):
            calls.append(url)
            if url.endswith('/v1/models'):
                return {'data':[{'id':llm.s.model,'owned_by':'splash',
                                 'context_length':32768,'max_model_len':32768,'vision':False}]}
            if url.endswith('/ready'):return {'status':'ready'}
            self.fail('Unexpected metadata endpoint: '+url)
        with patch('claire_core.llm._get_json',side_effect=get):
            info = llm.describe()
        self.assertEqual(info['ctx'],32768)
        self.assertEqual(info['backend'],'splash')
        self.assertTrue(info['live'])
        self.assertEqual(len(calls),2)

    def test_metadata_does_not_claim_a_different_loaded_model_is_ready(self):
        llm = LLM(Settings(worker_profile='uncensored27'))
        with patch('claire_core.llm._get_json',return_value={'data':[{'id':'wrong-model','owned_by':'splash'}]}):
            self.assertFalse(llm.describe()['live'])

    def test_reasoning_switch_is_explicit_and_fallback_roles_use_splash_mapping(self):
        llm = LLM(Settings(worker_profile='uncensored27',roles={}))
        for thinking,role,expected in [(False,'brain','none'),(True,'brain','medium'),(False,'clerk','none')]:
            captured = []
            def request(req,timeout):
                captured.append(json.loads(req.data))
                return io.BytesIO(b'data: [DONE]\n\n')
            with patch('claire_core.llm.urllib.request.urlopen',side_effect=request):
                llm.chat([{'role':'user','content':'hi'}],role=role,thinking=thinking)
            self.assertEqual(captured[0]['reasoning_effort'],expected)
            self.assertEqual(captured[0]['enable_thinking'],thinking)


class SplashOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = managed_server_spec(Settings(state_dir=self.temp.name), 'brain')
        splash._write_record(self.s,123,'original','instance-one')

    def test_pid_reuse_never_adopts_or_signals_the_replacement(self):
        with patch.object(splash,'_listening',return_value=True),patch.object(splash.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'left running'):
                splash.unload(self.s,{},lambda pid:'replacement')
        kill.assert_not_called()

    def test_http_instance_replacement_is_not_killed(self):
        with patch.object(splash,'_listening',return_value=True), \
             patch.object(splash,'_json',return_value={'instance':{'pid':123,'id':'other'}}), \
             patch.object(splash.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'HTTP instance changed'):
                splash.unload(self.s,{},lambda pid:'original')
        kill.assert_not_called()

    def test_shutdown_reaps_own_process_and_removes_ownership_record(self):
        child = Mock()
        processes = {123:child}
        fingerprint = Mock(side_effect=['original','original',''])
        with patch.object(splash,'_listening',return_value=False),patch.object(splash.os,'kill') as kill:
            splash.unload(self.s,processes,fingerprint)
        child.poll.assert_called_once()
        self.assertNotIn(123,processes)
        self.assertFalse(splash._record_path(self.s).exists())
        self.assertEqual(kill.call_args.args[0],123)

    def test_startup_failure_is_reported_and_does_not_leave_a_child_running(self):
        root = Path(self.temp.name)/'runtime'
        for name in ['python/bin/python3','engine/splash','install/launcher.py']:
            p = root/name;p.parent.mkdir(parents=True,exist_ok=True);p.touch()
        draft = Path(self.temp.name)/'draft';draft.mkdir();(draft/'config.json').write_text('{}')
        target = Path(self.temp.name)/'target.gguf';target.touch()
        s = dict(self.s,root=str(root),draft=str(draft),path=str(target))
        child = Mock(pid=456,returncode=1)
        child.poll.return_value = 1
        processes = {}
        with patch.object(splash,'_listening',return_value=False), \
             patch.object(splash.subprocess,'Popen',return_value=child):
            result = splash.ensure(SimpleNamespace(auto_parallel=False),s,processes,lambda pid:'')
        self.assertIn('exited with status 1',result)
        self.assertNotIn(456,processes)

    def test_aliases_unload_once_and_keep_unrelated_backends_separate(self):
        s = self.s
        settings = SimpleNamespace(config=None,roles={'alias':{}},)
        with patch.object(boot,'server_spec',return_value=s), \
             patch.object(splash,'unload') as unload:
            result = boot.unload_models(settings)
        self.assertEqual(result,{'unloaded':['brain'],'errors':[]})
        unload.assert_called_once()

    def test_unload_limited_to_splash_leaves_shared_servers_running(self):
        llama = dict(role='talk',host='127.0.0.1',port=8081,gguf='/models/small.gguf')
        settings = SimpleNamespace(config=None,roles={'talk':{}})
        specs = {'brain':self.s,'talk':llama}
        with patch.object(boot,'server_spec',side_effect=lambda settings,role:specs[role]), \
             patch.object(splash,'unload') as unload, \
             patch.object(boot,'_unload_llama') as unload_llama:
            result = boot.unload_models(settings,kinds=('splash',))
        self.assertEqual(result,{'unloaded':['brain'],'errors':[]})
        unload.assert_called_once()
        unload_llama.assert_not_called()


class SplashPrimingTests(unittest.TestCase):
    def test_priming_sends_the_messages_once_for_one_token_on_splash_only(self):
        llm = LLM(Settings(worker_profile='uncensored27'))
        messages = [{'role':'system','content':'s'},{'role':'user','content':'hi'}]
        with patch.object(llm,'chat') as chat:
            llm.prime(messages, role='talk')
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[0], messages)
        self.assertEqual(chat.call_args.kwargs['max_tokens'], 1)
        self.assertFalse(chat.call_args.kwargs['thinking'])
        llm.s.roles['talk'] = {'gguf':'/models/small.gguf','port':8081,'api_base':'http://127.0.0.1:8081/v1'}
        with patch.object(llm,'chat') as chat:
            self.assertEqual(llm.prime(messages, role='talk'), 0.0)
        chat.assert_not_called()

    def test_a_failed_priming_request_does_not_fail_the_turn(self):
        from claire_core.llm import ModelError
        llm = LLM(Settings(worker_profile='uncensored27'))
        with patch.object(llm,'chat',side_effect=ModelError('busy')):
            llm.prime([{'role':'user','content':'hi'}], role='talk')


class SplashReportingTests(unittest.TestCase):
    def test_unloaded_profile_reports_its_configured_context(self):
        llm = LLM(Settings(worker_profile='uncensored27'))
        with patch('claire_core.llm._get_json',side_effect=OSError('not running')):
            info = llm.describe('standard27')
        self.assertFalse(info['live'])
        self.assertEqual(info['ctx'],32768)

    def test_a_role_on_its_own_llama_server_is_not_labelled_splash(self):
        from claire_core.service import _Handler as Handler
        settings = Settings(worker_profile='uncensored27')
        settings.roles['talk'] = {'gguf':'/models/small.gguf','port':8081,
                                  'api_base':'http://127.0.0.1:8081/v1'}
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(hub=SimpleNamespace(core=SimpleNamespace(settings=settings)))
        self.assertEqual(handler._backend('talk'),'llama')
        self.assertEqual(handler._backend('standard27'),'splash')
        self.assertEqual(handler._backend('vision'),'lmstudio')
        self.assertEqual(handler._backend('brain'),'splash')


if __name__ == '__main__':
    unittest.main()
