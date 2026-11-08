import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import psutil
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.security import check_password_hash


def create_app(test_config=None):
    app = Flask(__name__)
    state = Path(os.environ.get('UBUNTU_NODE_STATE', '/var/lib/ubuntu-node'))
    app.config.update(
        DATABASE=str(state / 'panel.db'),
        SECRET_KEY=os.environ.get('UBUNTU_NODE_SECRET'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('UBUNTU_NODE_HTTPS', '0') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False, MAX_CONTENT_LENGTH=8192,
        HELPER='/usr/local/libexec/ubuntu-node-helper',
    )
    if test_config:
        app.config.update(test_config)
    if not app.config['SECRET_KEY']:
        app.config['SECRET_KEY'] = (state / 'secret').read_text().strip()

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=15)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        if 'db' in g:
            g.db.close()

    def audit(action, details, ok=True):
        db().execute('INSERT INTO audit(ts,username,ip,action,details,ok) VALUES(?,?,?,?,?,?)',
                     (time.time(), session.get('user', 'anonymous'), request.remote_addr,
                      action, details[:2000], int(ok)))
        db().commit()

    @app.before_request
    def protect():
        if not request.path.startswith('/api/'):
            return None
        if request.path == '/api/login' and request.method == 'POST':
            # Login has no session CSRF token yet. Reject browser cross-origin requests.
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='拒绝跨站请求'), 403
            if request.headers.get('Origin') and request.headers['Origin'] != request.host_url.rstrip('/'):
                return jsonify(error='登录来源不匹配'), 403
            return None
        user = db().execute('SELECT version FROM admin WHERE username=?',
                            (session.get('user', ''),)).fetchone()
        if not user or user['version'] != session.get('version'):
            session.clear()
            return jsonify(error='请先登录'), 401
        if request.method != 'GET':
            token = request.headers.get('X-CSRF-Token', '')
            if not token or not hmac.compare_digest(token, session.get('csrf', '')):
                return jsonify(error='请求校验失败，请刷新页面'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(400)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def bad_request(error):
        return jsonify(error='请求格式不正确或内容过大'), error.code

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.post('/api/login')
    def login():
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='请求格式错误'), 400
        username, password = data.get('username', ''), data.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str):
            return jsonify(error='请求格式错误'), 400
        now, ip = time.time(), request.remote_addr or 'local'
        # Persisted, per-source rate limit; X-Forwarded-For is intentionally ignored.
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM attempts WHERE ts < ?', (now - 900,))
        count = conn.execute('SELECT count(*) FROM attempts WHERE ip=?', (ip,)).fetchone()[0]
        if count >= 10:
            conn.commit()
            return jsonify(error='尝试次数过多，请 15 分钟后重试'), 429
        conn.execute('INSERT INTO attempts(ip,ts) VALUES(?,?)', (ip, now))
        conn.commit()
        row = conn.execute('SELECT * FROM admin WHERE username=?', (username,)).fetchone()
        if not row or not check_password_hash(row['password'], password):
            audit('login', '登录失败', False)
            return jsonify(error='用户名或密码错误'), 401
        conn.execute('DELETE FROM attempts WHERE ip=?', (ip,))
        conn.commit()
        session.clear()
        session.permanent = True
        session.update(user=username, version=row['version'], csrf=secrets.token_hex(32))
        audit('login', '登录成功')
        return jsonify(user=username, csrf=session['csrf'])

    @app.get('/api/session')
    def whoami():
        return jsonify(user=session['user'], csrf=session['csrf'])

    @app.post('/api/logout')
    def logout():
        audit('logout', '退出登录')
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/metrics')
    def metrics():
        memory, disk, net = psutil.virtual_memory(), psutil.disk_usage('/'), psutil.net_io_counters()
        return jsonify(host=os.uname().nodename, kernel=os.uname().release,
                       cpu=psutil.cpu_percent(interval=0.15), cores=psutil.cpu_count(),
                       memory={'total': memory.total, 'used': memory.used, 'percent': memory.percent},
                       disk={'total': disk.total, 'used': disk.used, 'percent': disk.percent},
                       uptime=int(time.time() - psutil.boot_time()), load=os.getloadavg(),
                       network={'sent': net.bytes_sent, 'received': net.bytes_recv}, timestamp=time.time())

    def helper(payload):
        try:
            result = subprocess.run(['sudo', '-n', app.config['HELPER']], input=json.dumps(payload),
                                    text=True, capture_output=True, timeout=45, check=False)
            if result.returncode:
                try:
                    message = json.loads(result.stdout).get('error', '系统操作失败')
                except (ValueError, AttributeError):
                    message = '受限助手不可用，请检查安装和服务日志'
                raise RuntimeError(message)
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('操作超时，请刷新检查实际状态') from error
        except (OSError, ValueError) as error:
            raise RuntimeError('系统助手返回异常') from error

    @app.get('/api/services')
    def services():
        return system_action({'action': 'services'}, False)

    @app.get('/api/sites')
    def sites():
        return system_action({'action': 'sites'}, False)

    @app.get('/api/logs/<service>')
    def logs(service):
        return system_action({'action': 'logs', 'service': service}, False)

    def system_action(payload, record=True):
        try:
            result = helper(payload)
            if record:
                audit(payload['action'], json.dumps(payload, ensure_ascii=False))
            return jsonify(result)
        except RuntimeError as error:
            if record:
                audit(payload['action'], str(error), False)
            return jsonify(error=str(error)), 400

    @app.post('/api/services/<service>/<operation>')
    def change_service(service, operation):
        return system_action({'action': 'service', 'service': service, 'operation': operation})

    @app.post('/api/sites')
    def create_site():
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='请求格式错误'), 400
        return system_action({'action': 'create_site', 'domain': data.get('domain'),
                              'kind': data.get('kind'), 'port': data.get('port')})

    @app.post('/api/sites/<domain>/delete')
    def delete_site(domain):
        return system_action({'action': 'delete_site', 'domain': domain})

    @app.get('/api/audit')
    def audit_log():
        rows = db().execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100').fetchall()
        return jsonify(entries=[dict(row) for row in rows])

    return app
