"""Local root/admin CLI. No web password reset endpoint."""
import argparse
import getpass
import os
import secrets
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admin(username TEXT PRIMARY KEY, password TEXT NOT NULL, version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_ip ON attempts(ip,ts);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL NOT NULL, username TEXT,
 ip TEXT, action TEXT, details TEXT, ok INTEGER);
'''


def initialize(state, username, password):
    if not username or len(username) > 64:
        raise ValueError('用户名长度须为 1–64 个字符')
    if len(password) < 12:
        raise ValueError('密码至少需要 12 个字符')
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret = state / 'secret'
    if not secret.exists():
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(secrets.token_hex(32))
    with sqlite3.connect(state / 'panel.db') as conn:
        conn.executescript(SCHEMA)
        conn.execute('DELETE FROM admin')
        conn.execute('INSERT INTO admin VALUES(?,?,?)',
                     (username, generate_password_hash(password, method='pbkdf2:sha256:600000'), secrets.token_hex(16)))
    os.chmod(state / 'panel.db', 0o600)


def main():
    parser = argparse.ArgumentParser(description='创建或重置唯一管理员，旧会话立即失效')
    parser.add_argument('--state', default='/var/lib/ubuntu-node')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--password-stdin', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.password_stdin:
        password = input()
    else:
        password = getpass.getpass('管理员密码（至少 12 位）: ')
        if password != getpass.getpass('再次输入密码: '):
            parser.error('两次密码不一致')
    try:
        initialize(args.state, args.username, password)
    except ValueError as error:
        parser.error(str(error))
    print('管理员已保存，旧登录会话已失效。')


if __name__ == '__main__':
    main()
