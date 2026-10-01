"""End-to-end tests for demo mode (the in-memory OCI double behind the UI).

Run with the project's Python environment:

    python -m unittest tests.test_demo_mode -v

``DEMO_MODE=1`` makes ``app.py`` talk to ``demo_sdk`` instead of Oracle Cloud,
which is how the Arena preview lets the UI be clicked through without
credentials. These tests pin that walkthrough: scans and quota work from fake
credentials, the loop hunts through ``OutOfHostCapacity`` and then wins, a
second start is refused while it hunts, Stop is confirmed in the live log, and
every line is marked ``[demo]``.
"""
import base64
import os
import time
import unittest

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402
import demo_sdk  # noqa: E402

AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'x:test-password').decode()}

CRED = {
    'user': 'ocid1.user.demo',
    'tenancy': 'ocid1.tenancy.demo',
    'fingerprint': 'aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99',
    'region': 'ap-kulai-1',
    'private_key': 'demo-private-key',
}

ACCOUNT = {
    'shape': 'VM.Standard.E2.1.Micro',
    'image_id': 'ocid1.image.oc1.ap-kulai-1.demo0',
    'subnet_id': 'ocid1.subnet.oc1.ap-kulai-1.public',
    'ssh_key': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB demo@example.com',
    'boot_volume_gb': 50,
    'display_name': 'demo-bot',
    'ad_preference': '',
    'retry_delay': 10,
}


class DemoModeTestBase(unittest.TestCase):
    def setUp(self):
        # install() rebinds module globals: remember the originals.
        self._saved = (app.oci, app.get_oci, app.create_oci_client, app.DEMO_MODE)
        demo_sdk.reset(capacity_after_attempts=1)
        demo_sdk.install(app)
        # The quota screen caches for USAGE_CACHE_SECONDS; drop it so each test
        # starts from the reset demo cloud.
        with app.usage_cache_lock:
            app.usage_cache.clear()

        with app.automation_lock:
            app.automation_running = False
            app.automation_shape = None
            app.automation_run_id = 0
            app.automation_run_seq = 0
            app.automation_info = {}
            app.automation_stop_reason = None
            app.stop_event.set()

        self.client = app.app.test_client()
        with app.logs_lock:
            self.log_mark = app.global_log_base + len(app.global_logs)

    def tearDown(self):
        with app.automation_lock:
            event = app.stop_event
        event.set()
        self.wait_until(lambda: not app.automation_running, timeout=5)
        (app.oci, app.get_oci, app.create_oci_client,
         app.DEMO_MODE) = self._saved
        with app.usage_cache_lock:
            app.usage_cache.clear()
        demo_sdk.reset()
        with app.tg_live_lock:
            app.tg_live_enabled = False

    # ---- helpers ---------------------------------------------------------
    def wait_until(self, predicate, timeout=6):
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

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers=AUTH).get_json()


class DemoScansTests(DemoModeTestBase):
    def test_images_and_subnets_come_from_the_demo_cloud(self):
        images = self.post('/api/list-images', CRED)
        self.assertTrue(images['success'])
        self.assertTrue(any('24.04' in i['name'] for i in images['images']))

        subnets = self.post('/api/list-subnets', CRED)
        self.assertTrue(subnets['success'])
        self.assertEqual([s['name'] for s in subnets['subnets']],
                         ['public-subnet', 'private-subnet'])
        self.assertTrue(subnets['subnets'][0]['public'])

    def test_quota_and_boot_volume_inventory_start_empty_and_work(self):
        usage = self.post('/api/free-tier-status', CRED)['usage']
        self.assertEqual(usage['storage']['used_gb'], 0)
        self.assertEqual(usage['micro']['used'], 0)
        self.assertEqual(usage['arm']['used_ocpus'], 0)

        listing = self.post('/api/boot-volumes/list', CRED)
        self.assertTrue(listing['success'])
        self.assertEqual(listing['boot_volumes'], [])

    def test_demo_config_rejects_a_half_filled_form(self):
        bad = dict(CRED, user='', private_key='')
        result = self.post('/api/list-images', bad)
        self.assertFalse(result['success'])
        self.assertIn('Demo mode: config is missing', result['error'])


class DemoLoopTests(DemoModeTestBase):
    def start_loop(self, **overrides):
        payload = dict(CRED, **dict(ACCOUNT, **overrides))
        return self.post('/api/auto-launch-loop', payload)

    def test_loop_hunts_through_capacity_and_then_wins(self):
        # Capacity opens on attempt 3, so the loop must survive two refusals.
        # Driven directly with no delay (the HTTP path clamps retry_delay to
        # 10s), against the same in-memory demo cloud.
        demo_sdk.reset(capacity_after_attempts=3)
        import threading

        config = {'user': CRED['user'], 'tenancy': CRED['tenancy'],
                  'fingerprint': CRED['fingerprint'], 'region': CRED['region'],
                  'key_content': CRED['private_key']}
        compute = app.create_oci_client(app.oci.core.ComputeClient, config)
        network = app.create_oci_client(app.oci.core.VirtualNetworkClient, config)
        identity = app.create_oci_client(app.oci.identity.IdentityClient, config)
        app.run_automated_creation(
            config, dict(ACCOUNT), compute, network, identity,
            retry_delay=0, max_attempts=5, stop_evt=threading.Event(),
        )

        logs = self.log_text()
        self.assertIn('SUCCESS! Instance created and running.', logs)
        self.assertIn('Out of host capacity', logs)
        self.assertIn('Provisioning loop exited (success', logs)

        usage = self.post('/api/free-tier-status', CRED)['usage']
        self.assertEqual(usage['micro']['used'], 1)
        self.assertEqual(usage['storage']['used_gb'], 50)
        listing = self.post('/api/boot-volumes/list', CRED)
        self.assertEqual(len(listing['boot_volumes']), 1)

    def test_success_boots_the_demo_instance_on_the_first_attempt(self):
        res = self.start_loop()
        self.assertTrue(res['success'])
        self.assertEqual(res['loop']['run_id'], 1)
        self.assertTrue(self.wait_until(lambda: not app.automation_running))
        self.assertIn('SUCCESS! Instance created and running.', self.log_text())

    def test_second_start_is_refused_and_stop_is_logged(self):
        # Capacity never opens: the loop keeps hunting like a real sniper.
        demo_sdk.reset(capacity_after_attempts=99)
        self.assertTrue(self.start_loop()['success'])
        self.assertTrue(self.wait_until(lambda: app.automation_running))

        refused = self.post('/api/auto-launch-loop',
                            dict(CRED, **dict(ACCOUNT, region='ap-singapore-1',
                                              fingerprint='cc:dd', user='ocid1.user.other')))
        self.assertFalse(refused['success'])
        self.assertIn('already running in the background', refused['error'])
        self.assertEqual(refused['loop']['region'], 'ap-kulai-1')

        stop = self.post('/api/stop-loop', {})
        self.assertTrue(stop['success'])
        self.assertTrue(self.wait_until(lambda: not app.automation_running))

        logs = self.log_text()
        self.assertIn('Refused this start request', logs)
        self.assertIn('Provisioning loop exited (stopped by user).', logs)
        # Demo runs are unmistakable: every line carries [demo].
        self.assertIn('[demo]', logs)

    def test_firewall_reads_work_and_writes_say_not_simulated(self):
        subnet_id = 'ocid1.subnet.oc1.ap-kulai-1.public'
        scan = self.post('/api/scan-security-rules', dict(CRED, subnet_id=subnet_id))
        self.assertTrue(scan['success'])
        self.assertEqual(scan['rules'], [])

        # A write must refuse loudly instead of pretending to have changed OCI.
        opened = self.post('/api/open-firewall',
                           dict(CRED, subnet_id=subnet_id, ports='22'))
        self.assertFalse(opened['success'])
        self.assertIn('Demo mode', opened['error'])


if __name__ == '__main__':
    unittest.main()
