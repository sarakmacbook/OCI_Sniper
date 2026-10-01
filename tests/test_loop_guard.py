"""Tests for the single-loop guarantee of the provisioning loop.

Run with the project's Python environment:

    python -m unittest tests.test_loop_guard -v

These pin the bug where a second start request — typically a second browser
session pasting a *different* OCI key — crashed the endpoint with a 500
(``UnboundLocalError`` on ``automation_shape``) instead of refusing cleanly, and
silently wiped the running loop's Telegram live-log settings on the way out.
The guarantee under test:

* only one provisioning loop runs at a time, whatever OCI key asks for another;
* the refusal is written to the live log (UI terminal and Telegram), naming the
  loop that already holds the slot;
* a refused request cannot change the running loop's Telegram settings;
* Stop is confirmed in the live log with the loop's exit reason;
* a stale thread can never clear the state of a newer run.

No network access and no real OCI SDK are needed: the SDK double and in-memory
clients mirror ``tests/test_preflight.py``.
"""
import base64
import os
import threading
import time
import types
import unittest
from unittest import mock

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402
from tests.test_preflight import (  # noqa: E402
    AD, FakeBlock, FakeCompute, FakeIdentity, FakeNetwork, FakeServiceError,
    _resp, make_sdk,
)

AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'x:test-password').decode()}


class BlockingCompute(FakeCompute):
    """Keeps the loop alive inside ``launch_instance`` until released.

    A real hunt sits in exactly this state: the launch request is in flight and
    the single loop slot must stay locked while it retries. Set
    ``next_result = 'success'`` before releasing to let the loop win.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.next_result = 'capacity'

    def launch_instance(self, launch_instance_details=None):
        self.launch_calls.append(launch_instance_details)
        self.entered.set()
        self.release.wait(timeout=10)
        if self.next_result == 'success':
            return _resp(types.SimpleNamespace(id='ocid1.instance.new',
                                               display_name='new'))
        raise FakeServiceError(code='OutOfHostCapacity', status=500,
                               message='Out of capacity')


def loop_payload(user='ocid1.user.A', region='ap-kulai-1', fingerprint='aa:bb', **over):
    data = {
        'user': user,
        'tenancy': 'ocid1.tenancy.fake',
        'fingerprint': fingerprint,
        'region': region,
        'private_key': 'fake-key',
        'shape': 'VM.Standard.E2.1.Micro',
        'image_id': 'ocid1.image.apkulai',
        'subnet_id': 'ocid1.subnet.apkulai',
        'ssh_key': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB test',
        'boot_volume_gb': 50,
        'display_name': 'test-bot',
        'ad_preference': '',
        'retry_delay': 10,
    }
    data.update(over)
    return data


class LoopGuardTestBase(unittest.TestCase):
    def setUp(self):
        self._saved_oci = app.oci
        self.reset_loop_state()
        self.client = app.app.test_client()

        self.compute_a = BlockingCompute(
            shapes_offered=['VM.Standard.E2.1.Micro', 'VM.Standard.A1.Flex'],
            images={'ocid1.image.apkulai': 'AVAILABLE'},
        )
        self.compute_b = BlockingCompute(
            shapes_offered=['VM.Standard.E2.1.Micro', 'VM.Standard.A1.Flex'],
            images={'ocid1.image.apkulai': 'AVAILABLE'},
        )
        self.network = FakeNetwork(subnets={'ocid1.subnet.apkulai': 'AVAILABLE'})
        self.block = FakeBlock()
        self.identity = FakeIdentity([types.SimpleNamespace(name=AD)])
        self.sdk, self.clients = make_sdk(
            self.compute_a, self.network, self.block, self.identity
        )
        self.patches = [
            mock.patch.object(app, 'oci', self.sdk),
            mock.patch.object(app, 'get_oci', return_value=self.sdk),
            mock.patch.object(
                app, 'create_oci_client',
                side_effect=lambda cls, config: self.clients[cls],
            ),
        ]
        for patcher in self.patches:
            patcher.start()

        # Count how many loop threads a test manages to start.
        self.started_runs = []
        real_loop = app.run_automated_creation

        def counting_loop(*args, **kwargs):
            self.started_runs.append(kwargs.get('run_id'))
            return real_loop(*args, **kwargs)

        self._loop_patch = mock.patch.object(app, 'run_automated_creation', counting_loop)
        self._loop_patch.start()

        with app.logs_lock:
            self.log_mark = app.global_log_base + len(app.global_logs)

    def tearDown(self):
        self.release_loops()
        self.stop_running_loop()
        self._loop_patch.stop()
        for patcher in self.patches:
            patcher.stop()
        app.oci = self._saved_oci
        with app.tg_live_lock:
            app.tg_live_enabled = False
            app.tg_live_bot_token = None
            app.tg_live_chat_id = None
        self.reset_loop_state()

    # ---- helpers ---------------------------------------------------------
    def reset_loop_state(self):
        with app.automation_lock:
            app.automation_running = False
            app.automation_shape = None
            app.automation_run_id = 0
            app.automation_run_seq = 0
            app.automation_info = {}
            app.automation_stop_reason = None
            app.stop_event = threading.Event()

    def release_loops(self):
        """Unblock any launch sleeping in the fakes."""
        self.compute_a.release.set()
        self.compute_b.release.set()

    def stop_running_loop(self, timeout=5):
        with app.automation_lock:
            event = app.stop_event
        event.set()
        self.release_loops()
        self.wait_until(lambda: not app.automation_running, timeout=timeout)

    def wait_until(self, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def log_text(self):
        with app.logs_lock:
            start = max(0, self.log_mark - app.global_log_base)
            return '\n'.join(app.global_logs[start:])

    def start_loop(self, payload):
        return self.client.post('/api/auto-launch-loop', json=payload, headers=AUTH)

    def stop_loop_and_release(self):
        """Click Stop, then let the in-flight launch call return like OCI would."""
        body = self.client.post('/api/stop-loop', headers=AUTH).get_json()
        self.release_loops()
        return body

    def status(self):
        return self.client.get('/api/status', headers=AUTH).get_json()


class DuplicateLoopRefusedTests(LoopGuardTestBase):
    def test_duplicate_start_with_other_key_is_refused_not_500(self):
        first = self.start_loop(loop_payload())
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()['success'])
        self.assertTrue(self.compute_a.entered.wait(timeout=5))

        second = self.start_loop(loop_payload(
            user='ocid1.user.B', region='ap-singapore-1', fingerprint='cc:dd',
            display_name='second-bot',
        ))
        body = second.get_json()

        # The old code raised UnboundLocalError here and answered HTTP 500.
        self.assertEqual(second.status_code, 200)
        self.assertFalse(body['success'])
        self.assertTrue(body['running'])
        self.assertIn('already running in the background', body['error'])
        # The refusal names the loop that owns the slot, and the one refused.
        self.assertIn('ap-kulai-1', body['error'])
        self.assertIn('aa:bb', body['error'])
        self.assertIn('ap-singapore-1', body['error'])
        self.assertEqual(body['loop']['run_id'], 1)
        self.assertEqual(body['loop']['region'], 'ap-kulai-1')

        # No second loop thread ever started and the other account was never
        # touched: only the first loop is hunting.
        self.assertEqual(self.started_runs, [1])
        self.assertEqual(self.compute_b.launch_calls, [])
        self.assertTrue(app.automation_running)
        self.assertEqual(app.automation_run_id, 1)

    def test_duplicate_start_with_same_key_is_refused_too(self):
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))

        second = self.start_loop(loop_payload())
        body = second.get_json()
        self.assertFalse(body['success'])
        self.assertIn('already running in the background', body['error'])
        self.assertEqual(self.started_runs, [1])

    def test_refusal_is_written_to_the_live_log(self):
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        self.start_loop(loop_payload(user='ocid1.user.B', region='ap-singapore-1',
                                     fingerprint='cc:dd'))

        logs = self.log_text()
        self.assertIn('A provisioning loop is already running in the background', logs)
        self.assertIn('Refused this start request', logs)
        self.assertIn('even with a different OCI key', logs)

    def test_refused_start_cannot_silence_running_loop_telegram_live_log(self):
        first = self.start_loop(loop_payload(
            telegram_bot_token='tok-A', telegram_chat_id='chat-A',
            telegram_live_log=True,
        ))
        self.assertTrue(first.get_json()['success'])
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        self.assertTrue(app.tg_live_enabled)
        self.assertEqual(app.tg_live_bot_token, 'tok-A')

        # A second session with live logging switched off must not reconfigure
        # the running loop (this used to silently turn the live log off).
        self.start_loop(loop_payload(user='ocid1.user.B', region='ap-singapore-1',
                                     fingerprint='cc:dd', telegram_live_log=False))
        self.assertTrue(app.tg_live_enabled)
        self.assertEqual(app.tg_live_bot_token, 'tok-A')
        self.assertEqual(app.tg_live_chat_id, 'chat-A')

    def test_start_logs_the_loop_it_started(self):
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        logs = self.log_text()
        self.assertIn('Provisioning loop started — run #1: shape VM.Standard.E2.1.Micro', logs)
        self.assertIn('region ap-kulai-1', logs)
        self.assertIn('key aa:bb', logs)
        self.assertIn('Only this one loop can run until it exits', logs)


class StartLoopStatusTests(LoopGuardTestBase):
    def test_status_reports_the_running_loop_details(self):
        self.start_loop(loop_payload(display_name='status-bot'))
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        self.assertTrue(self.wait_until(lambda: self.status()['loop']['attempts'] >= 1))

        data = self.status()
        loop = data['loop']
        self.assertTrue(data['running'])
        self.assertEqual(data['shape'], 'VM.Standard.E2.1.Micro')
        self.assertEqual(loop['run_id'], 1)
        self.assertEqual(loop['region'], 'ap-kulai-1')
        self.assertEqual(loop['fingerprint'], 'aa:bb')
        self.assertEqual(loop['name'], 'status-bot')
        self.assertEqual(loop['current_ad'], AD)
        self.assertGreaterEqual(loop['attempts'], 1)
        self.assertIsNone(loop['finished_at'])
        # The private key never appears in status output.
        self.assertNotIn('private_key', loop)

    def test_status_without_a_loop_is_idle(self):
        data = self.status()
        self.assertFalse(data['running'])
        self.assertEqual(data['loop']['run_id'], 0)
        self.assertFalse(data['loop']['running'])


class StopLoopTests(LoopGuardTestBase):
    def test_stop_is_confirmed_in_the_live_log_with_an_exit_reason(self):
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))

        res = self.client.post('/api/stop-loop', headers=AUTH)
        body = res.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['running'])
        self.assertIn('Stop signal sent', body['message'])

        # The in-flight launch call returns, then the loop notices the stop.
        self.release_loops()
        self.assertTrue(self.wait_until(lambda: not app.automation_running))

        logs = self.log_text()
        self.assertIn('Stop requested for run #1', logs)
        self.assertIn('Provisioning loop exited (stopped by user).', logs)

        data = self.status()
        self.assertFalse(data['running'])
        self.assertEqual(data['loop']['stop_reason'], 'stopped by user')
        self.assertIsNotNone(data['loop']['finished_at'])

    def test_stop_without_a_loop_is_safe_and_logged(self):
        before = self.get_run_seq()
        res = self.client.post('/api/stop-loop', headers=AUTH)
        body = res.get_json()
        self.assertTrue(body['success'])
        self.assertFalse(body['running'])
        self.assertIn('No provisioning loop is running', body['message'])
        self.assertIn('nothing to stop', self.log_text())
        self.assertEqual(self.get_run_seq(), before)

    def get_run_seq(self):
        with app.automation_lock:
            return app.automation_run_seq

    def test_a_new_loop_can_start_after_the_previous_one_exits(self):
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        self.stop_loop_and_release()
        self.assertTrue(self.wait_until(lambda: not app.automation_running))

        # Second run uses the other OCI key and is accepted only now.
        second = self.start_loop(loop_payload(
            user='ocid1.user.B', region='ap-singapore-1', fingerprint='cc:dd',
        ))
        body = second.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['loop']['run_id'], 2)
        self.assertEqual(body['loop']['region'], 'ap-singapore-1')
        self.assertTrue(self.wait_until(lambda: app.automation_run_id == 2))
        self.assertEqual(self.started_runs, [1, 2])

    def test_loop_started_after_stop_uses_a_fresh_stop_event(self):
        # The stop event of the previous run must not leak into the new one,
        # otherwise the new loop would exit on its first check.
        self.start_loop(loop_payload())
        self.assertTrue(self.compute_a.entered.wait(timeout=5))
        old_event = app.stop_event
        self.stop_loop_and_release()
        self.assertTrue(self.wait_until(lambda: not app.automation_running))

        self.compute_b.release.set()
        self.start_loop(loop_payload(user='ocid1.user.B', region='ap-singapore-1',
                                     fingerprint='cc:dd'))
        self.assertIsNot(app.stop_event, old_event)
        self.assertTrue(self.wait_until(lambda: app.automation_running))
        # Give the new loop a moment to survive its first check.
        time.sleep(0.2)
        self.assertTrue(app.automation_running)


class LoopExitReasonTests(LoopGuardTestBase):
    def test_preflight_failure_releases_the_slot_and_logs_the_reason(self):
        self.compute_a.images = {}  # image OCID unknown in this region -> fatal
        res = self.start_loop(loop_payload())
        self.assertTrue(res.get_json()['success'])

        self.assertTrue(self.wait_until(lambda: not app.automation_running))
        logs = self.log_text()
        self.assertIn('Pre-flight check failed', logs)
        self.assertIn('Provisioning loop exited (pre-flight check failed).', logs)

        data = self.status()
        self.assertEqual(data['loop']['stop_reason'], 'pre-flight check failed')
        # The slot is free again, so another key can start.
        second = self.start_loop(loop_payload(user='ocid1.user.B',
                                              region='ap-singapore-1',
                                              fingerprint='cc:dd'))
        self.assertTrue(second.get_json()['success'])
        self.assertEqual(second.get_json()['loop']['run_id'], 2)

    def test_success_is_recorded_as_the_exit_reason(self):
        res = self.start_loop(loop_payload())
        self.assertTrue(res.get_json()['success'])
        self.assertTrue(self.compute_a.entered.wait(timeout=5))

        # Oracle has capacity on this attempt: the loop wins and exits itself.
        self.compute_a.next_result = 'success'
        self.compute_a.release.set()

        self.assertTrue(self.wait_until(lambda: not app.automation_running, timeout=8))
        logs = self.log_text()
        self.assertIn('SUCCESS! Instance created and running.', logs)
        self.assertIn('Provisioning loop exited (success', logs)
        data = self.status()
        self.assertFalse(data['running'])
        self.assertIn('success', data['loop']['stop_reason'])


class SingleSlotUnitTests(LoopGuardTestBase):
    def test_begin_reserves_and_second_begin_is_refused(self):
        run_id, event, busy = app._automation_begin('shape-A', 'region-A', 'aa:bb',
                                                    'user-A', 'name-A')
        self.assertEqual(run_id, 1)
        self.assertIsNotNone(event)
        self.assertIsNone(busy)
        self.assertTrue(app.automation_running)

        run_id_2, event_2, busy_2 = app._automation_begin('shape-B', 'region-B',
                                                          'cc:dd', 'user-B', 'name-B')
        self.assertIsNone(run_id_2)
        self.assertIsNone(event_2)
        self.assertEqual(busy_2['run_id'], 1)
        self.assertEqual(busy_2['region'], 'region-A')
        self.assertEqual(app.automation_run_id, 1)

    def test_stale_thread_cannot_clear_a_newer_run(self):
        first, _, _ = app._automation_begin('shape-A', 'region-A', 'aa:bb', 'u', 'n')
        self.assertTrue(app._automation_end(first, 'stopped by user'))
        second, _, _ = app._automation_begin('shape-B', 'region-B', 'cc:dd', 'u', 'n')
        self.assertEqual(second, 2)

        # A slow, stale thread from run #1 must not unlock run #2.
        self.assertFalse(app._automation_end(first, 'stale'))
        self.assertFalse(app._automation_finish(first, 'stale'))
        self.assertTrue(app.automation_running)
        self.assertEqual(app.automation_run_id, 2)

        self.assertTrue(app._automation_end(second, 'stopped by user'))
        self.assertFalse(app.automation_running)

    def test_begin_uses_a_fresh_stop_event_per_run(self):
        _, first_event, _ = app._automation_begin('s', 'r', 'f', 'u', 'n')
        first_event.set()
        app._automation_end(1, 'stopped by user')
        _, second_event, _ = app._automation_begin('s', 'r', 'f', 'u', 'n')
        self.assertIsNot(first_event, second_event)
        self.assertFalse(second_event.is_set())
        self.assertIs(app.stop_event, second_event)

    def test_crashing_job_cannot_leave_the_slot_locked(self):
        run_id, _, _ = app._automation_begin('shape-A', 'region-A', 'aa:bb', 'u', 'n')

        @app.hold_oci_slot
        def crashing_job(**kwargs):
            raise RuntimeError('unexpected boom')

        with self.assertRaises(RuntimeError):
            crashing_job(run_id=run_id)

        self.assertFalse(app.automation_running)
        self.assertIn('crashed with an unexpected error',
                      app.automation_snapshot()['stop_reason'])
        self.assertIn('Provisioning loop exited (crashed with an unexpected error).',
                      self.log_text())

    def test_finish_logs_the_exit_line_once(self):
        run_id, _, _ = app._automation_begin('shape-A', 'region-A', 'aa:bb', 'u', 'n')
        with app.logs_lock:
            mark = app.global_log_base + len(app.global_logs)
        self.assertTrue(app._automation_finish(run_id, 'stopped by user'))
        self.assertFalse(app._automation_finish(run_id, 'stopped by user'))
        with app.logs_lock:
            start = max(0, mark - app.global_log_base)
            lines = [l for l in app.global_logs[start:]
                     if 'Provisioning loop exited' in l]
        self.assertEqual(len(lines), 1)


if __name__ == '__main__':
    unittest.main()
