import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from claire_core import resources
from claire_core.resources import GIB, model_start_problem, task_budget
from claire_core.settings import Settings
from claire_core.hub import Hub
from tests.helpers import Env, done
from tests.test_service import ServiceCase


def memory(available):
    return {'total_bytes': 64*GIB, 'available_bytes': available*GIB if available is not None else None}


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                  100000.
Pages inactive:                              100000.
"""


class MemorySnapshotTests(unittest.TestCase):
    def snapshot(self, level):
        def output(cmd, **kwargs):
            if cmd[:2] == ["sysctl", "-n"] and cmd[2] == "hw.memsize":
                return str(64*GIB)
            if cmd[:2] == ["sysctl", "-n"] and cmd[2] == "kern.memorystatus_level":
                if isinstance(level, Exception):
                    raise level
                return str(level)
            return VM_STAT
        resources._cache = (0, None)
        self.addCleanup(setattr, resources, "_cache", (0, None))
        with patch.object(resources.sys, "platform", "darwin"), \
             patch.object(resources.subprocess, "check_output", side_effect=output):
            return resources.memory_snapshot()

    def test_macos_uses_the_kernel_free_memory_level(self):
        self.assertEqual(self.snapshot(50)["available_bytes"], 32*GIB)

    def test_falls_back_to_free_and_inactive_pages(self):
        pages = 200000*16384
        self.assertEqual(self.snapshot(OSError("no sysctl"))["available_bytes"], pages)
        self.assertEqual(self.snapshot(250)["available_bytes"], pages)   # nonsense level


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(auto_parallel=True, max_task_workers=4,
                                 task_ram_gb=2, ram_reserve_gb=4, model_runtime_gb=4)

    def test_concurrency_uses_ram_and_leaves_a_chat_slot(self):
        self.assertEqual(task_budget(self.settings, memory(24), 4)[0], 3)
        self.assertEqual(task_budget(self.settings, memory(24), 2)[0], 1)
        self.assertEqual(task_budget(self.settings, memory(9), 8)[0], 1)
        # Low on RAM, one task still runs: it uses the model already loaded.
        self.assertEqual(task_budget(self.settings, memory(4), 8), (1, "Low RAM: one task at a time"))
        self.assertEqual(task_budget(self.settings, memory(0), 8)[0], 1)

    def test_missing_telemetry_never_scales_up(self):
        self.assertEqual(task_budget(self.settings, memory(None), 8)[0], 1)

    def test_fixed_worker_setting_remains_available(self):
        self.settings.auto_parallel = False
        self.settings.task_workers = 2
        self.assertEqual(task_budget(self.settings, memory(0), 8)[0], 2)

    def test_model_start_requires_memory_headroom(self):
        with tempfile.NamedTemporaryFile() as f:
            spec = {'gguf': f.name, 'role': 'scout'}
            self.assertIsNone(model_start_problem(self.settings, spec, memory(24)))
            self.assertIn('Not starting scout', model_start_problem(self.settings, spec, memory(3)))
            self.assertIn('unavailable', model_start_problem(self.settings, spec, memory(None)))

    def test_parallel_tasks_do_not_exceed_the_limit_and_queue_resumes(self):
        env = Env(auto_parallel=True, max_task_workers=3)
        self.addCleanup(env.close)
        hub = Hub(env.core, start_brain=False)
        entered = []
        release = threading.Event()
        completed = threading.Event()
        def run(tid, text, turn):
            entered.append(tid)
            if len(entered) == 3: completed.set()
            release.wait(3)
        hub._run_turn = run
        hub.capacity = lambda: {'task_limit': 2}
        hub.start()
        self.addCleanup(hub.stop)
        self.addCleanup(release.set)
        for i in range(3): hub.spawn_task(str(i), 'test', env.work)
        deadline = time.monotonic()+2
        while len(entered)<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(len(entered),2)
        time.sleep(.05)
        self.assertEqual(len(entered),2)
        release.set()
        self.assertTrue(completed.wait(2))

    def test_low_memory_queue_can_resume(self):
        env = Env(auto_parallel=True, max_task_workers=2)
        self.addCleanup(env.close)
        hub = Hub(env.core, start_brain=False)
        allowance = [0]
        ran = threading.Event()
        hub.capacity = lambda: {'task_limit': allowance[0]}
        hub._run_turn = lambda *args: ran.set()
        hub.start()
        self.addCleanup(hub.stop)
        hub.spawn_task('test', 'test', env.work)
        self.assertFalse(ran.wait(.1))
        allowance[0] = 1
        with hub._admission: hub._admission.notify_all()
        self.assertTrue(ran.wait(2))


    def test_cancel_ram_blocked_turns_then_resume_same_task(self):
        env = Env([done('resumed')], auto_parallel=True, max_task_workers=1)
        self.addCleanup(env.close)
        hub = Hub(env.core, start_brain=False)
        allowance = [0]
        hub.capacity = lambda: {'task_limit': allowance[0]}
        hub.start()
        self.addCleanup(hub.stop)
        tid = hub.spawn_task('queued work', 'do not run this', env.work)['id']
        hub.post(tid, 'or this queued followup')
        time.sleep(.05)
        self.assertTrue(hub.cancel(tid))
        self.assertEqual(hub.store.thread(tid)['status'], 'cancelled')
        allowance[0] = 1
        with hub._admission: hub._admission.notify_all()
        time.sleep(.1)
        self.assertEqual(len(env.llm.calls), 0)
        self.assertIn('Cancelled before starting', env.core.vault.work.context())
        turn = hub.post(tid, 'resume only this')
        deadline = time.monotonic()+3
        while not any(e['type']=='turn_end' and e.get('turn')==turn for e in hub.store.events_after(0)) and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(hub.store.thread(tid)['status'], 'done')
        self.assertEqual(len(env.llm.calls), 1)
        self.assertIn('resume only this', str(env.llm.calls[0]['messages']))
        self.assertNotIn('do not run this', str(env.llm.calls[0]['messages']))
        self.assertFalse(hub.cancel('missing'))

    def test_cancellation_during_startup_is_not_cleared_by_session(self):
        env = Env([done('must not run')])
        self.addCleanup(env.close)
        hub = Hub(env.core, start_brain=True)
        self.addCleanup(hub.stop)
        tid = hub.spawn_task('startup race', 'do not run', env.work)['id']
        with patch('claire_core.hub.boot.ensure_brain', side_effect=lambda s: (hub.cancel(tid), None)[1]):
            hub._run_turn(tid, 'do not run', 1)
        self.assertEqual(hub.store.thread(tid)['status'], 'cancelled')
        self.assertEqual(len(env.llm.calls), 0)


class DashboardStatusTests(ServiceCase):
    def test_status_counts_every_task_and_excludes_home(self):
        self.boot()
        for i in range(70):
            tid = self.env.core.store.create_thread('task', str(i), self.env.work)
            self.env.core.store.update_thread(tid, status='done')
        for status in ['running','queued','waiting','error','stopped','cancelled']:
            tid = self.env.core.store.create_thread('task', status, self.env.work)
            self.env.core.store.update_thread(tid, status=status)
        data = self.client.get('/status')
        self.assertEqual(data['task_counts'],dict(total=76,active=3,completed=70,failed=2,cancelled=1))
        self.assertIn('memory',data['capacity'])
        self.assertTrue(data['context']['estimated'])

    def test_context_is_available_after_turn_and_reload(self):
        self.boot(home=[done('A short response.')])
        turn = self.client.post('/threads/home/messages',{'text':'hello'})['turn']
        self.wait_for(lambda e:e['type']=='turn_end' and e.get('turn')==turn)
        detail = self.client.get('/threads/home')['thread']['usage']
        self.assertGreater(detail['used'],0)
        self.assertGreater(detail['limit'],detail['used'])
        events = self.client.get('/threads/home/events')['events']
        last_usage = [e for e in events if e['type']=='usage'][-1]
        self.assertEqual(last_usage['used'],detail['used'])
