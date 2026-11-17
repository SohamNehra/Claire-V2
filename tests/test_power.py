import io
import json
import os
import signal
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claire_core import boot
from claire_core.hub import Hub
from claire_core.llm import LLM, ModelUnreachable
from claire_core.power import ModelPower
from tests.helpers import Env, done
from tests.test_service import ServiceCase


class IdlePowerTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.]
        self.unload = Mock(return_value={'unloaded':['brain'], 'errors':[]})
        self.settings = SimpleNamespace(model_idle_seconds=1800, roles={}, config=None)
        self.invalidate = Mock()
        self.power = ModelPower(self.settings, clock=lambda:self.now[0],
                                unload=self.unload, on_sleep=self.invalidate)

    def test_exact_threshold_sleeps_once_and_user_activity_does_not_load(self):
        self.now[0] = 1799
        self.assertFalse(self.power.sleep_if_idle())
        self.now[0] = 1800
        self.assertTrue(self.power.sleep_if_idle())
        self.assertEqual(self.power.status()['state'], 'asleep')
        self.invalidate.assert_called_once()
        self.now[0] += 9000
        self.assertFalse(self.power.sleep_if_idle())
        self.power.touch()
        self.assertEqual(self.power.status()['idle_seconds'], 0)
        self.assertEqual(self.power.status()['state'], 'asleep')
        self.unload.assert_called_once()

    def test_model_and_tool_work_cannot_sleep_and_get_a_fresh_idle_period(self):
        with self.power.activity():
            self.now[0] = 10000
            self.assertFalse(self.power.sleep_if_idle())
            self.assertTrue(self.power.status()['busy'])
        self.now[0] += 1799
        self.assertFalse(self.power.sleep_if_idle())
        self.now[0] += 1
        self.assertTrue(self.power.sleep_if_idle())

    def test_interaction_resets_timer_but_reading_status_does_not(self):
        self.now[0] = 1700
        self.power.touch()
        self.now[0] = 3400
        self.power.status()
        self.assertFalse(self.power.sleep_if_idle())
        self.now[0] = 3500
        self.assertTrue(self.power.sleep_if_idle())

    def test_work_waits_for_unload_and_then_wakes_without_a_race(self):
        entered, release, working = threading.Event(), threading.Event(), threading.Event()
        def unload(settings):
            entered.set()
            release.wait(3)
            return {'errors':[]}
        self.power._unload = unload
        self.now[0] = 1800
        sleeper = threading.Thread(target=self.power.sleep_if_idle)
        sleeper.start()
        self.assertTrue(entered.wait(1))
        def run():
            with self.power.activity():
                working.set()
                self.power.model_ready('brain')
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertFalse(working.wait(.05))
        finally:
            release.set()
            sleeper.join(2)
            worker.join(2)
        self.assertTrue(working.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.power.status()['state'], 'awake')
        self.assertFalse(self.power.status()['busy'])

    def test_partial_failure_is_visible_and_retry_is_bounded(self):
        self.unload.side_effect = [{'errors':['brain: failed']}, {'errors':[]}]
        self.now[0] = 1800
        with self.assertLogs('claire_core.power', level='WARNING'):
            self.assertFalse(self.power.sleep_if_idle())
        self.assertIn('brain: failed', self.power.status()['last_error'])
        self.now[0] += 59
        self.assertFalse(self.power.sleep_if_idle())
        self.now[0] += 1
        self.assertTrue(self.power.sleep_if_idle())
        self.assertEqual(self.power.status()['last_error'], '')

    def test_disabling_idle_sleep_keeps_models_loaded(self):
        self.power.timeout = 0
        self.now[0] = 10000
        self.assertFalse(self.power.sleep_if_idle())
        self.unload.assert_not_called()


class ModelUnloadTests(unittest.TestCase):
    def setUp(self):
        self.studio = dict(role='brain', kind='lmstudio', url='http://127.0.0.1:1234', model='worker')
        self.small = dict(role='talk', gguf='/models/small.gguf', host='127.0.0.1', port=8081, context=32768, slots=4)
        self.settings = SimpleNamespace(roles={'talk':{}, 'scout':{}, 'clerk':{}, 'remote':{}}, config=None)

    def test_role_aliases_unload_once_and_external_endpoints_are_untouched(self):
        specs = {'brain':self.studio, 'talk':self.small, 'scout':dict(self.small,role='scout'),
                 'clerk':dict(self.small,role='clerk'), 'remote':None}
        with patch.object(boot,'server_spec', side_effect=lambda s,r:specs[r]), \
             patch.object(boot,'_unload_lmstudio') as lm, patch.object(boot,'_unload_llama') as llama:
            result = boot.unload_models(self.settings)
        self.assertEqual(result, {'unloaded':['brain','talk'], 'errors':[]})
        lm.assert_called_once_with(self.studio)
        llama.assert_called_once_with(self.small, None)

    def test_remote_lmstudio_is_never_unloaded(self):
        with patch.object(boot,'server_spec', return_value=dict(self.studio,url='http://other-host:1234')), \
             patch.object(boot,'_unload_lmstudio') as lm:
            self.assertEqual(boot.unload_models(self.settings), {'unloaded':[], 'errors':[]})
            self.assertIsNone(boot.lifecycle_spec(self.settings,'brain'))
        lm.assert_not_called()

    def test_lmstudio_uses_instance_ids_and_checks_unload_completed(self):
        loaded = {'loaded_instances':[{'id':'worker-instance'}]}
        captured=[]
        def request(req,timeout):
            captured.append((req.full_url,json.loads(req.data)))
            return io.BytesIO(b'{"instance_id":"worker-instance"}')
        with patch.object(boot,'_lmstudio_model',side_effect=[loaded,{'loaded_instances':[]}]), \
             patch.object(boot.urllib.request,'urlopen',side_effect=request):
            boot._unload_lmstudio(self.studio)
        self.assertEqual(captured,[('http://127.0.0.1:1234/api/v1/models/unload',{'instance_id':'worker-instance'})])
        with patch.object(boot,'_lmstudio_model',return_value=loaded), \
             patch.object(boot.urllib.request,'urlopen',return_value=io.BytesIO(b'{}')):
            with self.assertRaisesRegex(RuntimeError,'verify'):
                boot._unload_lmstudio(self.studio)

    def test_llama_only_matches_the_full_configured_command(self):
        command = boot.server_command(self.small,None)
        good = ' '.join(command)
        listing = f'12 {good}\n13 {good} --extra-flag\n14 {good.replace("8081","8082")}\n15 python3 other.py\n'
        with patch.object(boot.subprocess,'run',return_value=SimpleNamespace(stdout=listing,returncode=0)), \
             patch.object(boot,'_process_fingerprint',return_value='Thu Nov 12 16:00:00 2026 '+good):
            self.assertEqual(boot._matching_servers(self.small,None),[(12,'Thu Nov 12 16:00:00 2026 '+good)])

    def test_pid_reuse_during_discovery_never_adopts_the_replacement(self):
        good=' '.join(boot.server_command(self.small,None))
        listing=f'12 {good}\n'
        with patch.object(boot.subprocess,'run',return_value=SimpleNamespace(stdout=listing,returncode=0)), \
             patch.object(boot,'_process_fingerprint',return_value='Thu Nov 12 16:00:00 2026 python3 unrelated.py'):
            self.assertEqual(boot._matching_servers(self.small,None),[])

    def test_pid_reuse_never_signals_the_replacement(self):
        with patch.object(boot,'_matching_servers',return_value=[(123,'original')]), \
             patch.object(boot,'_process_fingerprint',return_value='replacement'), \
             patch.object(boot.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'changed'):
                boot._unload_llama(self.small,None)
        kill.assert_not_called()

    def test_unknown_listener_is_reported_and_not_killed(self):
        with patch.object(boot,'_matching_servers',return_value=[]), \
             patch.object(boot,'_healthy',return_value=True), patch.object(boot.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'left running'):
                boot._unload_llama(self.small,None)
        kill.assert_not_called()

    def test_matching_llama_process_exits_and_is_reaped(self):
        process=Mock()
        with patch.object(boot,'_matching_servers',return_value=[(123,'original')]), \
             patch.object(boot,'_process_fingerprint',side_effect=['original','']), \
             patch.dict(boot._processes,{123:process}), patch.object(boot.os,'kill') as kill:
            boot._unload_llama(self.small,None)
            self.assertNotIn(123,boot._processes)
        kill.assert_called_once_with(123,signal.SIGTERM)
        process.poll.assert_called_once()


class SleepWakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env=Env()
        self.addCleanup(self.env.close)
        self.now=[0.]
        self.env.core.power._clock=lambda:self.now[0]
        self.env.core.power._last_activity=0.
        self.env.core.power._unload=Mock(return_value={'errors':[]})
        self.hub=Hub(self.env.core,start_brain=False)
        self.addCleanup(self.hub.stop)

    def wait_idle(self):
        deadline=time.monotonic()+3
        while self.env.core.power.status()['busy'] and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertFalse(self.env.core.power.status()['busy'])

    def test_queued_and_cancelled_work_reserves_until_consumed(self):
        self.hub.post('home','must not run')
        self.now[0]=3600
        self.assertFalse(self.env.core.power.sleep_if_idle())
        self.hub.cancel('home')
        self.hub.start()
        self.wait_idle()
        self.assertEqual(self.env.llm.calls,[])
        self.assertFalse(self.env.core.power.sleep_if_idle())
        self.now[0]+=1800
        self.assertTrue(self.env.core.power.sleep_if_idle())

    def test_pending_approval_prevents_sleep_and_keeps_model_available(self):
        os.mkdir(os.path.join(self.env.work,'build'))
        self.env.llm.replies=['<run_command><command>rm -rf build</command></run_command>', done('Left it alone.')]
        self.hub.start()
        tid=self.hub.spawn_task('Cleanup','Clean the build folder.',self.env.work)['id']
        deadline=time.monotonic()+3
        while not self.hub._waiting and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.hub._waiting)
        self.now[0]=10000
        self.assertFalse(self.env.core.power.sleep_if_idle())
        for aid in list(self.hub._waiting): self.hub.resolve_approval(aid,False)
        self.wait_idle()
        self.assertTrue(os.path.isdir(os.path.join(self.env.work,'build')))
        self.assertEqual(self.env.core.store.thread(tid)['status'],'done')

    def test_long_running_tool_prevents_sleep(self):
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def pending_process(*args):
            entered.set()
            release.wait(3)
        self.hub._run_turn=pending_process
        self.hub.start()
        self.hub.post('home','Wait for this process.')
        self.assertTrue(entered.wait(1))
        self.now[0]=10000
        self.assertFalse(self.env.core.power.sleep_if_idle())
        release.set()
        self.wait_idle()
        self.assertFalse(self.env.core.power.sleep_if_idle())

    def test_future_schedule_allows_sleep_due_job_loads_before_model_call(self):
        made=self.hub.scheduler.create('Brief','Produce a brief.','0 9 * * *',timezone='UTC',workdir=self.env.work,active=True)
        due=self.env.core.store.schedule(made['id'])['next_run']
        self.now[0]=1800
        self.assertTrue(self.env.core.power.sleep_if_idle())
        self.assertEqual(self.env.core.power.status()['state'],'asleep')
        self.hub._start_brain=True
        loaded=[]
        def reply(messages):
            self.assertTrue(loaded, 'generation started before loading')
            return done('Brief completed.')
        self.env.llm.replies=[reply]
        # Dispatch while asleep, as the scheduler does without a browser open.
        self.assertEqual(len(self.hub.scheduler.tick(now=due)),1)
        self.hub.scheduler.stop()  # deterministic clock; tick above is the due run
        with patch('claire_core.hub.boot.ensure_brain',side_effect=lambda s:loaded.append('brain')):
            self.hub.start()
            self.wait_idle()
        self.assertTrue(loaded)
        self.assertEqual(self.env.core.power.status()['state'],'awake')
        run=self.env.core.store.threads(kind='task')[0]
        self.assertEqual((run['status'],run['summary']),('done','Brief completed.'))
        self.assertEqual(self.env.core.store.load_messages('home'),[])  # nothing relayed to a hidden chat

    def test_direct_llm_call_wakes_after_sleep_and_releases_on_failure(self):
        llm=LLM(self.env.core.settings)
        power=self.env.core.power
        llm.power=power
        self.now[0]=1800
        power.sleep_if_idle()
        with patch.object(boot,'lifecycle_spec',return_value={'managed':True}), \
             patch.object(boot,'ensure_role',return_value='load failed'), \
             patch.object(llm,'_chat') as chat:
            with self.assertRaisesRegex(ModelUnreachable,'load failed'):
                llm.chat([{'role':'user','content':'hello'}],role='clerk')
            chat.assert_not_called()
        self.assertFalse(power.status()['busy'])
        with patch.object(boot,'lifecycle_spec',return_value={'managed':True}), \
             patch.object(boot,'ensure_role',return_value=None) as ensure, \
             patch.object(llm,'_chat',return_value='answer'):
            self.assertEqual(llm.chat([],role='clerk'),'answer')
            self.assertEqual(llm.chat([],role='clerk'),'answer')
            ensure.assert_called_once()
        self.assertEqual(power.status()['state'],'awake')

    def test_external_role_does_not_gain_a_health_check_requirement_after_sleep(self):
        llm=LLM(self.env.core.settings)
        llm.power=self.env.core.power
        self.now[0]=1800
        self.env.core.power.sleep_if_idle()
        with patch.object(boot,'lifecycle_spec',return_value=None), \
             patch.object(boot,'ensure_role') as ensure, patch.object(llm,'_chat',return_value='answer'):
            self.assertEqual(llm.chat([],role='external'),'answer')
        ensure.assert_not_called()


class ActivityServiceTests(ServiceCase):
    def test_status_polls_do_not_keep_models_awake_but_user_activity_does(self):
        self.boot()
        now=[0.]
        power=self.env.core.power
        power._clock=lambda:now[0]
        power._last_activity=0.
        power._unload=Mock(return_value={'errors':[]})
        now[0]=1700
        self.assertIn('power',self.client.get('/status'))
        self.assertEqual(power.status()['idle_seconds'],1700)
        self.client.post('/activity')
        self.assertEqual(power.status()['idle_seconds'],0)
        now[0]=3500
        self.assertTrue(power.sleep_if_idle())
        self.client.post('/activity')
        self.assertEqual(self.client.get('/status')['power']['state'],'asleep')
