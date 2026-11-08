"""Run only on disposable Ubuntu CI VMs: installs packages and the system service."""
import http.server
import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import http.cookiejar

password = secrets.token_urlsafe(24)
subprocess.run(['sudo', 'bash', 'install.sh', '--password-stdin'], input=password + '\n', text=True, check=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf = ''


def api(path, payload=None, expected=200):
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
    request = urllib.request.Request('http://127.0.0.1:8888/api/' + path,
                                     data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as error:
        response = error
    assert response.code == expected, (path, response.code, response.read())
    return json.load(response)


for attempt in range(30):
    try:
        api('session', expected=401)
        break
    except urllib.error.URLError:
        time.sleep(1)
else:
    raise RuntimeError('Panel did not become ready')
csrf = api('login', {'username': 'admin', 'password': password})['csrf']
assert api('metrics')['memory']['total'] > 0
for _ in range(30):
    if any(s['name'] == 'nginx' and s['active'] == 'active' for s in api('services')['services']):
        break
    time.sleep(0.2)
else:
    raise AssertionError('Nginx did not enter running state')
api('services/nginx/stop', {})
assert any(s['name'] == 'nginx' and s['active'] == 'inactive' for s in api('services')['services'])
api('services/nginx/start', {})
api('services/nginx/restart', {})
assert 'text' in api('logs/nginx')
for file in ('/opt/ubuntu-node/panel/app.py', '/usr/local/libexec/ubuntu-node-helper'):
    assert subprocess.run(['sudo', '-u', 'ubuntu-node', 'test', '-w', file]).returncode != 0
assert subprocess.run(['sudo', '-u', 'ubuntu-node', 'sudo', '-n', '/usr/local/libexec/ubuntu-node-helper', 'bad-arg'], capture_output=True).returncode != 0
subprocess.run(['sudo', 'bash', 'install.sh'], check=True)  # Upgrade must preserve credentials.
for attempt in range(30):
    try:
        api('session')
        break
    except urllib.error.URLError:
        time.sleep(1)
else:
    raise RuntimeError('Panel did not recover after reinstall')
def wait_site(domain, expected):
    # nginx reload signals its master asynchronously; verify eventual HTTP behavior.
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(50):
        request = urllib.request.Request('http://127.0.0.1/', headers={'Host': domain})
        try:
            with direct.open(request, timeout=2) as response:
                last = response.read()
                if expected in last:
                    return
        except (urllib.error.URLError, OSError) as error:
            last = str(error)
        time.sleep(0.1)
    raise AssertionError((domain, 'HTTP content did not converge after reload', last))


api('sites', {'domain': 'node-test.example.com', 'kind': 'static'})
try:
    wait_site('node-test.example.com', b'node-test.example.com')
    subprocess.run(['sudo', 'tee', '/var/www/ubuntu-node/node-test.example.com/.env'],
                   input='test fixture, not a secret', text=True, stdout=subprocess.DEVNULL, check=True)
    hidden = urllib.request.Request('http://127.0.0.1/.env', headers={'Host': 'node-test.example.com'})
    try:
        urllib.request.urlopen(hidden)
        raise AssertionError('Hidden files must not be served')
    except urllib.error.HTTPError as error:
        assert error.code == 403, error.code
    api('sites', {'domain': 'node-test.example.com', 'kind': 'static'}, expected=400)
    assert any(s['domain'] == 'node-test.example.com' for s in api('sites')['sites'])
finally:
    api('sites/node-test.example.com/delete', {})
assert subprocess.run(['sudo', 'test', '-f', '/var/www/ubuntu-node/node-test.example.com/index.html']).returncode == 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'proxy integration passed')

    def log_message(self, *_args):
        pass


server = http.server.ThreadingHTTPServer(('127.0.0.1', 18080), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
api('sites', {'domain': 'proxy-test.example.com', 'kind': 'proxy', 'port': 18080})
try:
    wait_site('proxy-test.example.com', b'proxy integration passed')
finally:
    api('sites/proxy-test.example.com/delete', {})
    server.shutdown()
assert any(row['action'] == 'create_site' for row in api('audit')['entries'])
api('logout', {})
api('metrics', expected=401)
print('Ubuntu installation, upgrade, authentication, privilege boundary, static/proxy sites: PASS')
