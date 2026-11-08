import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panel.admin import initialize
from panel.app import create_app

spec = importlib.util.spec_from_file_location('helper', Path(__file__).parents[1] / 'deploy/helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        initialize(self.state, 'admin', 'correct-horse-battery')
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-secret', 'DATABASE': str(self.state / 'panel.db')})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self):
        response = self.client.post('/api/login', json={'username': 'admin', 'password': 'correct-horse-battery'})
        self.assertEqual(response.status_code, 200)
        return {'X-CSRF-Token': response.json['csrf']}

    def test_all_api_reads_require_auth(self):
        for url in ('metrics', 'sites', 'services', 'audit', 'session', 'logs/nginx'):
            self.assertEqual(self.client.get('/api/' + url).status_code, 401)

    def test_csrf_and_logout(self):
        headers = self.login()
        with patch('panel.app.subprocess.run') as run:
            self.assertEqual(self.client.post('/api/services/nginx/stop').status_code, 403)
            run.assert_not_called()
        self.assertEqual(self.client.post('/api/logout', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 401)

    def test_login_origin_and_bad_input(self):
        response = self.client.post('/api/login', json={}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/login', json=[]).status_code, 400)
        self.assertEqual(self.client.post('/api/login', json={'username': []}).status_code, 400)

    def test_persistent_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post('/api/login', json={}).status_code, 401)
        # A new client cannot reset the stored per-source limit.
        fresh = self.app.test_client()
        self.assertEqual(fresh.post('/api/login', json={}).status_code, 429)
        with sqlite3.connect(self.state / 'panel.db') as conn:
            conn.execute('UPDATE attempts SET ts=0')
        self.login()

    def test_password_reset_revokes_sessions(self):
        self.login()
        initialize(self.state, 'admin', 'another-valid-password')
        self.assertEqual(self.client.get('/api/session').status_code, 401)

    def test_expired_session(self):
        self.login()
        self.app.config['PERMANENT_SESSION_LIFETIME'] = -1
        self.assertEqual(self.client.get('/api/session').status_code, 401)

    def test_metrics_and_headers(self):
        self.login()
        response = self.client.get('/api/metrics')
        self.assertEqual(response.status_code, 200)
        self.assertIn('total', response.json['memory'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])

    def test_helper_failure_is_audited(self):
        headers = self.login()
        result = subprocess.CompletedProcess([], 1, '{"error":"nginx config rejected"}', '')
        with patch('panel.app.subprocess.run', return_value=result):
            response = self.client.post('/api/sites', json={'domain': 'example.com', 'kind': 'static'}, headers=headers)
        self.assertEqual(response.status_code, 400)
        entries = self.client.get('/api/audit').json['entries']
        self.assertEqual(entries[0]['ok'], 0)
        self.assertEqual(entries[0]['action'], 'create_site')

    def test_helper_timeout(self):
        self.login()
        with patch('panel.app.subprocess.run', side_effect=subprocess.TimeoutExpired('helper', 45)):
            self.assertEqual(self.client.get('/api/services').status_code, 400)

    def test_command_is_fixed_and_json_is_not_shell(self):
        headers = self.login()
        result = subprocess.CompletedProcess([], 0, '{"ok":true}', '')
        with patch('panel.app.subprocess.run', return_value=result) as run:
            self.client.post('/api/services/nginx/restart', json={}, headers=headers)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['sudo', '-n', '/usr/local/libexec/ubuntu-node-helper'])
            self.assertNotIn('shell', kwargs)
            self.assertEqual(json.loads(kwargs['input'])['operation'], 'restart')


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conf = self.root / 'conf'
        self.www = self.root / 'www'
        self.conf.mkdir()
        self.patches = [patch.object(helper, 'CONF', self.conf), patch.object(helper, 'WWW', self.www)]
        for item in self.patches:
            item.start()
        self.runner = patch.object(helper, 'run', return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.run = self.runner.start()

    def tearDown(self):
        self.runner.stop()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def create(self, **kwargs):
        return helper.dispatch({'action': 'create_site', 'domain': 'example.com', 'kind': 'static', **kwargs})

    def test_domain_injection_rejected(self):
        for domain in ('../passwd', 'example.com; return 200;', '*.example.com', 'test\n.com', '-bad.com', '', None, ['a']):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.create(domain=domain)
        self.run.assert_not_called()

    def test_service_allowlist(self):
        for service in ('nginx;id', '../ssh', '--help', None):
            with self.assertRaises(ValueError):
                helper.dispatch({'action': 'service', 'service': service, 'operation': 'restart'})
        with self.assertRaises(ValueError):
            helper.dispatch({'action': 'service', 'service': 'nginx', 'operation': 'enable'})
        self.run.assert_not_called()

    def test_static_create_list_delete_preserves_content(self):
        self.create()
        self.assertTrue((self.www / 'example.com/index.html').exists())
        self.assertEqual(helper.dispatch({'action': 'sites'})['sites'][0]['domain'], 'example.com')
        helper.dispatch({'action': 'delete_site', 'domain': 'example.com'})
        self.assertFalse(helper.conf_path('example.com').exists())
        self.assertTrue((self.www / 'example.com/index.html').exists())

    def test_proxy_and_port_validation(self):
        for port in (0, 80, 8888, 65536, '3000;id', True, None):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.create(kind='proxy', port=port)
        self.create(kind='proxy', port=3000)
        content = helper.conf_path('example.com').read_text()
        self.assertIn('proxy_pass http://127.0.0.1:3000;', content)
        self.assertFalse(self.www.exists())

    def test_duplicate_site(self):
        self.create()
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.create()

    def test_external_duplicate_site(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, 'server_name example.com www.example.com;', '')
        with self.assertRaisesRegex(ValueError, '同名'):
            self.create()

    def test_symlink_rejected(self):
        helper.conf_path('example.com').symlink_to(self.root / 'unrelated')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.create()

    def test_config_validation_rollback_on_create(self):
        def failure(args, check=True):
            if '-t' in args:
                raise ValueError('syntax error')
            return subprocess.CompletedProcess(args, 0, '', '')
        self.run.side_effect = failure
        with self.assertRaisesRegex(ValueError, 'syntax'):
            self.create()
        self.assertFalse(helper.conf_path('example.com').exists())

    def test_reload_failure_rollback_on_delete(self):
        self.create()
        path = helper.conf_path('example.com')
        original = path.read_text()
        def failure(args, check=True):
            if 'reload' in args:
                raise ValueError('reload failed')
            return subprocess.CompletedProcess(args, 0, '', '')
        self.run.side_effect = failure
        with self.assertRaisesRegex(ValueError, 'reload'):
            helper.dispatch({'action': 'delete_site', 'domain': 'example.com'})
        self.assertEqual(path.read_text(), original)

    def test_does_not_delete_unmanaged_config(self):
        path = helper.conf_path('example.com')
        path.write_text('# manually managed\n')
        with self.assertRaises(ValueError):
            helper.dispatch({'action': 'delete_site', 'domain': 'example.com'})
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
