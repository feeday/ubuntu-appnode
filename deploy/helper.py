#!/usr/bin/python3 -I
"""Root-owned, fixed-argument sudo entrypoint. Never invokes a shell."""
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONF = Path('/etc/nginx/conf.d')
WWW = Path('/var/www/ubuntu-node')
LOCK = '/run/lock/ubuntu-node.lock'
SERVICES = ('nginx', 'ssh', 'cron', 'mysql', 'mariadb', 'redis-server', 'php8.1-fpm', 'php8.3-fpm')
OPS = ('start', 'stop', 'restart')
DOMAIN = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def run(args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    if check and result.returncode:
        raise ValueError((result.stderr or result.stdout or '系统命令失败')[-4000:])
    return result


def domain_name(value):
    if not isinstance(value, str) or not DOMAIN.fullmatch(value):
        raise ValueError('请输入小写 ASCII 域名，例如 example.com；国际域名请使用 Punycode')
    return value


def service_name(value):
    if value not in SERVICES:
        raise ValueError('服务不在允许列表中')
    return value + '.service'


def conf_path(domain):
    path = CONF / ('ubuntu-node-' + domain + '.conf')
    if path.is_symlink():
        raise ValueError('拒绝操作符号链接配置')
    return path


def write_atomic(path, content):
    # All paths are beneath root-owned directories. File creation is exclusive.
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_config(path, content):
    previous = path.read_text() if path.exists() else None
    if content is None:
        path.unlink()
    else:
        write_atomic(path, content)
    try:
        run(['/usr/sbin/nginx', '-t'])
        run(['/usr/bin/systemctl', 'reload', 'nginx.service'])
        # Reload acknowledgment is asynchronous; new workers take over shortly.
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, previous)
        # Recover the prior running configuration if the first reload partially applied.
        try:
            run(['/usr/sbin/nginx', '-t'])
            run(['/usr/bin/systemctl', 'reload', 'nginx.service'])
        except Exception:
            pass
        raise


def metadata(path):
    if path.is_symlink():
        raise ValueError('拒绝符号链接配置')
    first = path.read_text().splitlines()[0]
    if not first.startswith('# ubuntu-node '):
        raise ValueError('配置不是本面板创建的')
    return json.loads(first[len('# ubuntu-node '):])


def dispatch(data):
    if not isinstance(data, dict):
        raise ValueError('请求必须为对象')
    action = data.get('action')
    if action == 'services':
        services = []
        for name in SERVICES:
            output = run(['/usr/bin/systemctl', 'show', name + '.service',
                          '--property=LoadState,ActiveState,SubState,UnitFileState'], check=False)
            props = dict(line.split('=', 1) for line in output.stdout.splitlines() if '=' in line)
            services.append({'name': name, 'load': props.get('LoadState', 'unknown'),
                             'active': props.get('ActiveState', 'unknown'),
                             'sub': props.get('SubState', ''), 'enabled': props.get('UnitFileState', '')})
        return {'services': services}
    if action == 'logs':
        name = service_name(data.get('service'))
        output = run(['/usr/bin/journalctl', '--unit=' + name, '--lines=100', '--no-pager', '--output=short-iso'])
        return {'text': output.stdout[-60000:]}
    if action == 'service':
        name = service_name(data.get('service'))
        operation = data.get('operation')
        if operation not in OPS:
            raise ValueError('不允许的服务操作')
        run(['/usr/bin/systemctl', operation, name])
        return {'ok': True}
    if action == 'sites':
        entries = []
        for path in sorted(CONF.glob('ubuntu-node-*.conf')):
            entries.append(metadata(path))
        return {'sites': entries}
    if action in ('create_site', 'delete_site'):
        domain = domain_name(data.get('domain'))
        path = conf_path(domain)
        if action == 'delete_site':
            if not path.exists():
                raise ValueError('站点不存在')
            metadata(path)
            apply_config(path, None)
            return {'ok': True, 'message': '站点配置已移除，网站文件保留'}
        if path.exists():
            raise ValueError('站点已存在')
        kind = data.get('kind')
        info = {'domain': domain, 'kind': kind, 'root': str(WWW / domain)}
        root = WWW / domain
        if kind == 'static':
            if root.is_symlink() or WWW.is_symlink():
                raise ValueError('拒绝符号链接网站目录')
            location = (f'root {root};\n    index index.html;\n'
                        r'    location ~ /\.(?!well-known/) { deny all; }' + '\n'
                        '    location / { try_files $uri $uri/ =404; }')
        elif kind == 'proxy':
            port = data.get('port')
            if type(port) is not int or not 1024 <= port <= 65535 or port == 8888:
                raise ValueError('代理端口必须为 1024–65535 且不能为面板端口 8888')
            info['port'] = port
            location = f'''location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
    }}'''
        else:
            raise ValueError('站点类型必须为 static 或 proxy')
        # Check for externally managed conflicting names before installing our own file.
        current = run(['/usr/sbin/nginx', '-T'])
        names = re.findall(r'\bserver_name\s+([^;]+);', current.stdout)
        if any(domain in value.split() for value in names):
            raise ValueError('Nginx 中已有同名站点，请先处理已有配置')
        if kind == 'static':
            root.mkdir(parents=True, exist_ok=True, mode=0o755)
            index = root / 'index.html'
            if not index.exists() and not index.is_symlink():
                with index.open('x') as output:
                    output.write(f'<!doctype html><meta charset="utf-8"><title>{domain}</title><h1>{domain}</h1><p>Ubuntu Node · 站点已就绪</p>')
                index.chmod(0o644)
        content = '# ubuntu-node ' + json.dumps(info) + '\n' + f'''server {{
    listen 80;
    server_name {domain};
    access_log /var/log/nginx/ubuntu-node-{domain}.access.log;
    error_log /var/log/nginx/ubuntu-node-{domain}.error.log;
    client_max_body_size 16m;
    {location}
}}
'''
        apply_config(path, content)
        return {'ok': True, 'site': info}
    raise ValueError('未知操作')


def main():
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise ValueError('助手仅供 root 以无参数方式调用')
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            raise ValueError('请求过大')
        os.umask(0o022)
        data = json.loads(raw)
        # Serialize both reads and writes so readers never see an unvalidated config.
        with open(LOCK, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = dispatch(data)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
