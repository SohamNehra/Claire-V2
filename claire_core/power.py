"""Unload local models only when the entire workspace has been idle.

Work reservations cover queued turns, model calls, tools and approval waits.
Sleeping and starting work are serialized so no turn can use an unloading model.
The service and scheduler remain alive; a future schedule is not active work.
"""

from contextlib import contextmanager
import logging
import threading
import time

from . import boot


class ModelPower:
    def __init__(self, settings, clock=None, unload=None, on_sleep=None):
        self.settings = settings
        self.timeout = max(0, float(settings.model_idle_seconds))
        self._clock = clock or time.monotonic
        self._unload = unload or boot.unload_models
        self._on_sleep = on_sleep or (lambda: None)
        self._condition = threading.Condition()
        self._last_activity = self._clock()
        self._last_attempt = None
        self._active = 0
        self._state = 'awake'
        self._error = ''
        self._slept = False
        self._ready_roles = set()
        self._stop = threading.Event()
        self._thread = None

    def touch(self):
        """A real user interaction; passive status polls must not call this."""
        with self._condition:
            self._last_activity = self._clock()

    def begin(self):
        with self._condition:
            while self._state == 'sleeping':
                self._condition.wait()
            self._active += 1
            self._last_activity = self._clock()
            if self._state == 'asleep':
                self._state = 'waking'

    def end(self):
        with self._condition:
            if self._active <= 0:
                raise RuntimeError('unbalanced model work reservation')
            self._active -= 1
            # Start a fresh idle period after a long command/approval finishes.
            self._last_activity = self._clock()
            if not self._active and self._state == 'waking':
                self._state = 'asleep'

    @contextmanager
    def activity(self):
        self.begin()
        try:
            yield
        finally:
            self.end()

    def needs_wake(self, role):
        with self._condition:
            return (self._slept and role not in self._ready_roles
                    and boot.lifecycle_spec(self.settings, role) is not None)

    def model_ready(self, role):
        with self._condition:
            self._ready_roles.add(role)
            self._state = 'awake'
            self._error = ''

    def status(self):
        with self._condition:
            return {'enabled': bool(self.timeout), 'state': self._state,
                    'timeout_seconds': self.timeout, 'busy': self._active > 0,
                    'idle_seconds': max(0, self._clock() - self._last_activity),
                    'last_error': self._error}

    def sleep_if_idle(self):
        """One bounded maintenance pass, also usable with a test clock."""
        with self._condition:
            now = self._clock()
            if (not self.timeout or self._active or self._state in ('sleeping', 'asleep')
                    or now - self._last_activity < self.timeout
                    or (self._last_attempt is not None and now - self._last_attempt < 60)):
                return False
            self._state = 'sleeping'
            self._last_attempt = now
        problem = ''
        try:
            result = self._unload(self.settings)
            problem = '; '.join(result.get('errors') or [])
            self._on_sleep()
        except Exception as exc:
            problem = f'{type(exc).__name__}: {exc}'
        finally:
            with self._condition:
                # Even a partial unload requires lazy reloading of every role.
                self._slept = True
                self._ready_roles.clear()
                self._error = problem
                self._state = 'awake' if problem else 'asleep'
                self._condition.notify_all()
        if problem:
            logging.getLogger(__name__).warning("Idle model unload failed: %s", problem)
        return not problem

    def start(self):
        if self.timeout and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name='claire-model-sleep', daemon=True)
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(min(15, self.timeout)):
            self.sleep_if_idle()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
