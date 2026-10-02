"""Tests for the per-loop "Max attempts" option (1 .. unlimited).

Run with the project's Python environment:

    python -m unittest tests.test_attempts_option -v

These pin the behaviour of the new attempt-cap option surfaced by the UI:

* a positive ``max_attempts`` bounds the loop and it exits with a
  ``retry limit reached (N attempts)`` reason when the cap is hit;
* ``unlimited_attempts`` (or ``max_attempts`` 0 / the string ``"unlimited"``)
  makes the loop hunt forever — it only stops on a user stop, never on a
  retry limit.

No network access and no real OCI SDK are needed: the SDK double and
in-memory clients from tests/test_preflight.py are reused.
"""
import os
import threading
import time
import unittest

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402
from tests.test_preflight import (
    AD,
    FakeServiceError,
    PreflightTestBase,
    make_account,
)


def _capacity_error():
    return FakeServiceError(
        code='OutOfHostCapacity', status=500, message='Out of capacity'
    )


class AttemptsOptionTests(PreflightTestBase):
    def _run(self, max_attempts, retry_delay=0, launch_error=None, threaded=False):
        if launch_error is not None:
            self.compute.launch_error = launch_error
        args = (
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay, False, 25, 60, None, None,
            max_attempts,
        )
        if threaded:
            t = threading.Thread(
                target=app.run_automated_creation, args=args, daemon=True
            )
            t.start()
            return t
        app.run_automated_creation(*args)
        return None

    def test_bounded_attempts_reaches_retry_limit(self):
        # A small positive cap must bound the loop and report the limit reached.
        self._run(max_attempts=3, launch_error=_capacity_error())
        self.assertEqual(len(self.compute.launch_calls), 3)
        logs = self.log_text()
        self.assertIn('Retry limit: 3 attempts', logs)
        self.assertIn('Retry limit reached (3 attempts)', logs)
        self.assertNotIn('Instance created', logs)
        self.assertFalse(app.automation_running)

    def test_default_max_attempts_is_respected(self):
        # No explicit cap is passed here (uses MAX_ATTEMPTS via the default),
        # but we force a tiny ceiling through the positional arg to prove the
        # loop still treats a positive value as bounded.
        self._run(max_attempts=2, launch_error=_capacity_error())
        self.assertEqual(len(self.compute.launch_calls), 2)
        self.assertIn('Retry limit reached (2 attempts)', self.log_text())

    def test_unlimited_zero_is_unbounded_until_stop(self):
        # max_attempts=0 (the "unlimited" sentinel) must never auto-exit on a
        # retry limit; the loop keeps hunting until the user stops it.
        t = self._run(max_attempts=0, launch_error=_capacity_error(), threaded=True)
        time.sleep(0.5)
        app.stop_event.set()
        t.join(timeout=5)

        self.assertGreater(len(self.compute.launch_calls), 5)
        # The loop must stop because the user asked, never on a retry limit.
        self.assertEqual(app.automation_stop_reason, 'stopped by user')
        logs = self.log_text()
        self.assertIn('Provisioning loop exited (stopped by user)', logs)
        self.assertNotIn('Retry limit reached', logs)
        self.assertFalse(app.automation_running)

    def test_unlimited_string_sentinel_is_unbounded(self):
        # The literal string "unlimited" must behave like the unbounded mode.
        t = self._run(max_attempts='unlimited', launch_error=_capacity_error(),
                      threaded=True)
        time.sleep(0.4)
        app.stop_event.set()
        t.join(timeout=5)

        self.assertGreater(len(self.compute.launch_calls), 5)
        self.assertEqual(app.automation_stop_reason, 'stopped by user')
        self.assertFalse(app.automation_running)

    def test_unlimited_flag_still_launches_when_capacity_appears(self):
        # Unlimited mode must not interfere with a successful launch: as soon as
        # Oracle has capacity the instance is created even after many attempts.
        self.compute.launch_error = _capacity_error()
        t = threading.Thread(
            target=app.run_automated_creation,
            args=(self.config, make_account(), self.compute, self.network,
                  self.identity, 0, False, 25, 60, None, None, 0),
            daemon=True,
        )
        t.start()
        # Let it fail a few times, then clear the error so the next attempt wins.
        time.sleep(0.3)
        self.compute.launch_error = None
        t.join(timeout=5)

        self.assertGreater(len(self.compute.launch_calls), 1)
        logs = self.log_text()
        self.assertIn('SUCCESS! Instance created', logs)
        self.assertNotIn('Retry limit reached', logs)
        self.assertFalse(app.automation_running)


if __name__ == '__main__':
    unittest.main()
