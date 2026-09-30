"""Tests for the 24/7 keep-alive (anti-sleep) feature.

Run with the project's Python environment:

    python -m unittest tests.test_keepalive -v

No network access is needed: outbound pings are stubbed at the
``app._keepalive_ping_target`` boundary or with a mocked ``requests.get``.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402


def reset_keepalive(enabled=True, urls=(), interval=240):
    """Put the keep-alive module state into a known shape for one test."""
    app._keepalive_stop_thread()
    with app._keepalive_lock:
        app._keepalive_cfg.clear()
        app._keepalive_cfg.update({'enabled': enabled, 'interval': interval, 'urls': list(urls)})
        app._keepalive_stats.clear()
        app._keepalive_stats.update({
            'started_at': None,
            'last_attempt': None,
            'last_success': None,
            'last_status_code': None,
            'last_error': None,
            'consecutive_failures': 0,
            'total_pings': 0,
            'total_failures': 0,
            'last_fail_log': 0.0,
        })


class KeepAliveParsingTests(unittest.TestCase):
    def test_parse_urls_accepts_http_and_https(self):
        urls = app._parse_keepalive_urls('https://a.example/healthz, http://b.example/ping')
        self.assertEqual(urls, ['https://a.example/healthz', 'http://b.example/ping'])

    def test_parse_urls_rejects_bad_schemes_and_garbage(self):
        self.assertEqual(app._parse_keepalive_urls('ftp://x, not-a-url, https://ok.example'), ['https://ok.example'])

    def test_parse_urls_deduplicates_and_caps_count(self):
        raw = ','.join('https://h%d.example' % i for i in range(10))
        raw = raw + ',https://h0.example'
        urls = app._parse_keepalive_urls(raw)
        self.assertEqual(len(urls), app.KEEPALIVE_MAX_URLS)
        self.assertEqual(len(urls), len(set(urls)))

    def test_default_urls_from_env_list(self):
        with mock.patch.dict(os.environ, {'KEEPALIVE_URLS': 'https://pinger.example/uuid', 'RAILWAY_PUBLIC_DOMAIN': ''}):
            self.assertEqual(app._default_keepalive_urls(), ['https://pinger.example/uuid'])

    def test_default_urls_from_railway_domain(self):
        env = {'KEEPALIVE_URLS': '', 'RAILWAY_PUBLIC_DOMAIN': 'myapp.up.railway.app'}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(app._default_keepalive_urls(), ['https://myapp.up.railway.app/healthz'])

    def test_default_urls_empty_without_any_hint(self):
        env = {'KEEPALIVE_URLS': '', 'RAILWAY_PUBLIC_DOMAIN': '', 'RAILWAY_STATIC_URL': ''}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(app._default_keepalive_urls(), [])

    def test_log_url_strips_query_token(self):
        redacted = app._keepalive_url_for_log('https://hc-ping.com/secret-uuid?query=1')
        self.assertEqual(redacted, 'https://hc-ping.com/secret-uuid')


class KeepAlivePingTests(unittest.TestCase):
    def setUp(self):
        reset_keepalive(urls=['https://self.example/healthz'])

    def tearDown(self):
        reset_keepalive(enabled=False)

    def test_ping_success_updates_stats(self):
        with mock.patch.object(app, '_keepalive_ping_target', return_value=(True, 200, None)):
            app.keepalive_ping_once()
        status = app.keepalive_status()
        self.assertEqual(status['total_pings'], 1)
        self.assertEqual(status['total_failures'], 0)
        self.assertEqual(status['consecutive_failures'], 0)
        self.assertEqual(status['last_status_code'], 200)
        self.assertIsNotNone(status['last_success_ago'])

    def test_ping_failure_updates_stats(self):
        with mock.patch.object(app, '_keepalive_ping_target', return_value=(False, None, 'connection refused')):
            app.keepalive_ping_once()
            app.keepalive_ping_once()
        status = app.keepalive_status()
        self.assertEqual(status['total_pings'], 2)
        self.assertEqual(status['consecutive_failures'], 2)
        self.assertEqual(status['total_failures'], 2)
        self.assertEqual(status['last_error'], 'connection refused')
        self.assertIsNone(status['last_success_ago'])

    def test_recovery_is_recorded(self):
        fail = (False, None, 'down')
        ok = (True, 200, None)
        with mock.patch.object(app, '_keepalive_ping_target', side_effect=[fail, fail, fail, ok]):
            for _ in range(4):
                app.keepalive_ping_once()
        status = app.keepalive_status()
        self.assertEqual(status['consecutive_failures'], 0)
        self.assertEqual(status['total_failures'], 3)
        self.assertEqual(status['total_pings'], 4)

    def test_ping_without_targets_is_a_noop(self):
        reset_keepalive(urls=[])
        with mock.patch.object(app, '_keepalive_ping_target') as ping:
            app.keepalive_ping_once()
        ping.assert_not_called()

    def test_any_http_response_counts_as_sent(self):
        # 401/404 from a misconfigured target still resets the host's idle
        # timer, so it is recorded as "sent", not as a failure.
        with mock.patch.object(app, '_keepalive_ping_target', return_value=(True, 404, None)):
            app.keepalive_ping_once()
        status = app.keepalive_status()
        self.assertEqual(status['last_status_code'], 404)
        self.assertEqual(status['consecutive_failures'], 0)


class KeepAliveApplyTests(unittest.TestCase):
    def tearDown(self):
        reset_keepalive(enabled=False)

    def test_apply_clamps_interval(self):
        ok, _ = app.keepalive_apply(interval=5)
        self.assertTrue(ok)
        self.assertEqual(app.keepalive_status()['interval_seconds'], app.KEEPALIVE_MIN_INTERVAL)
        ok, _ = app.keepalive_apply(interval=100000)
        self.assertEqual(app.keepalive_status()['interval_seconds'], app.KEEPALIVE_MAX_INTERVAL)

    def test_apply_rejects_non_integer_interval(self):
        ok, error = app.keepalive_apply(interval='soon')
        self.assertFalse(ok)
        self.assertIn('integer', error)

    def test_apply_rejects_urls_without_any_valid_entry(self):
        ok, error = app.keepalive_apply(urls=['nope', 'ftp://x'])
        self.assertFalse(ok)
        self.assertIn('No valid http(s) URLs', error)

    def test_apply_accepts_string_url_list(self):
        ok, _ = app.keepalive_apply(urls='https://a.example, https://b.example')
        self.assertTrue(ok)
        self.assertEqual(app.keepalive_status()['target_count'], 2)

    def test_disable_stops_thread(self):
        ok, _ = app.keepalive_apply(enabled=True, urls=['https://self.example/healthz'], interval=30)
        self.assertTrue(ok)
        self.assertTrue(app.keepalive_status()['running'])
        ok, _ = app.keepalive_apply(enabled=False)
        self.assertTrue(ok)
        status = app.keepalive_status()
        self.assertFalse(status['running'])
        self.assertFalse(status['enabled'])

    def test_background_loop_pings_repeatedly(self):
        # Minimum interval is 30s for real use, but the loop reads whatever
        # is in the config, so a short interval lets us observe the cycle.
        reset_keepalive(enabled=True, urls=['https://self.example/healthz'], interval=1)
        with mock.patch.object(app, '_keepalive_ping_target', return_value=(True, 200, None)) as ping:
            app._keepalive_start_thread()
            try:
                self.assertTrue(app.keepalive_status()['running'])
            finally:
                app._keepalive_stop_thread()
        self.assertGreaterEqual(ping.call_count, 1)
        self.assertFalse(app.keepalive_status()['running'])


class KeepAliveApiTests(unittest.TestCase):
    def setUp(self):
        reset_keepalive(urls=['https://self.example/healthz'])
        self.client = app.app.test_client()
        self.auth = ('operator', 'test-password')

    def tearDown(self):
        reset_keepalive(enabled=False)

    def test_healthz_includes_keepalive_and_needs_no_auth(self):
        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertIn('keepalive', body)
        # The public endpoint must not leak target URLs.
        self.assertNotIn('targets', body['keepalive'])

    def test_get_requires_auth(self):
        res = self.client.get('/api/keepalive')
        self.assertEqual(res.status_code, 401)

    def test_get_returns_status_with_auth(self):
        res = self.client.get('/api/keepalive', auth=self.auth)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body['success'])
        self.assertIn('interval_seconds', body['keepalive'])

    def test_post_updates_settings(self):
        res = self.client.post(
            '/api/keepalive',
            json={'enabled': False, 'interval_seconds': 60, 'urls': ['https://pinger.example/x']},
            auth=self.auth,
        )
        self.assertEqual(res.status_code, 200)
        ka = res.get_json()['keepalive']
        self.assertFalse(ka['enabled'])
        self.assertEqual(ka['interval_seconds'], 60)
        self.assertEqual(ka['targets'], ['https://pinger.example/x'])

    def test_post_rejects_invalid_urls(self):
        res = self.client.post('/api/keepalive', json={'urls': ['definitely not a url']}, auth=self.auth)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body['success'])
        self.assertIn('No valid http(s) URLs', body['error'])

    def test_status_includes_keepalive(self):
        res = self.client.get('/api/status', auth=self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIn('keepalive', res.get_json())


if __name__ == '__main__':
    unittest.main()
