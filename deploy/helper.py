#!/usr/bin/python3 -I
"""Root-owned, fixed-argument sudo entrypoint. Never invokes a shell."""
import fcntl
import hashlib
import ipaddress
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


def runtime():
    path = Path('/etc/ubuntu-node/runtime')
    return path.read_text().strip() if path.exists() else 'systemd'


def supervisor(operation, check=True):
    return run(['/usr/bin/supervisorctl', '-c', '/etc/ubuntu-node/supervisord.conf',
                operation, 'nginx'], check=check)


def reload_nginx():
    if runtime() == 'supervisor':
        run(['/usr/sbin/nginx', '-s', 'reload'])
    else:
        run(['/usr/bin/systemctl', 'reload', 'nginx.service'])


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
        reload_nginx()
        # Reload acknowledgment is asynchronous; new workers take over shortly.
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, previous)
        # Recover the prior running configuration if the first reload partially applied.
        try:
            run(['/usr/sbin/nginx', '-t'])
            reload_nginx()
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


FIREWALL_STATE = Path('/etc/ubuntu-node/firewall.json')
FIREWALL_CHAIN = 'UNODE_INPUT'


def firewall_saved():
    raw = FIREWALL_STATE.read_text() if FIREWALL_STATE.exists() else '[]'
    rules = json.loads(raw)
    return rules, hashlib.sha256(raw.encode()).hexdigest()


def firewall_probe():
    try:
        result = run(['/usr/sbin/iptables', '-w', '3', '-S', 'INPUT'], check=False)
        if result.returncode:
            return False, '当前环境无 iptables 管理权限（通常缺少 CAP_NET_ADMIN）。', ''
        return True, '', result.stdout
    except OSError:
        return False, 'iptables 未安装，请更新并重新运行安装脚本。', ''


def protected_ports():
    ports = {22, 8888}
    # Also preserve custom SSH listeners, including systemd socket activation.
    result = run(['/usr/bin/ss', '-H', '-lntp'])
    for line in result.stdout.splitlines():
        if 'sshd' in line or '"systemd"' in line:
            fields = line.split()
            if len(fields) >= 4:
                value = fields[3].rsplit(':', 1)[-1]
                if value.isdigit():
                    ports.add(int(value))
    return sorted(ports)


def firewall_rules(value, ports):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError('最多允许 32 条规则')
    rules = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('规则格式错误')
        protocol, verdict, port = item.get('protocol'), item.get('verdict'), item.get('port')
        if protocol not in ('tcp', 'udp') or verdict not in ('ACCEPT', 'DROP'):
            raise ValueError('只允许 TCP/UDP 和允许/阻止操作')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('端口必须为 1–65535 的整数')
        if protocol == 'tcp' and verdict == 'DROP' and port in ports:
            raise ValueError('禁止阻止 SSH 或面板保护端口：' + str(port))
        source = item.get('source', '0.0.0.0/0')
        try:
            source = str(ipaddress.IPv4Network(source, strict=False)) if isinstance(source, str) else None
        except ValueError:
            source = None
        if source is None:
            raise ValueError('来源必须是 IPv4 地址或 CIDR 网段')
        note = item.get('note', '')
        if not isinstance(note, str) or len(note) > 80 or any(ord(c) < 32 for c in note):
            raise ValueError('备注最多 80 字且不能包含控制字符')
        rules.append({'protocol': protocol, 'verdict': verdict, 'port': port, 'source': source, 'note': note})
    return rules


def firewall_script(rules, ports):
    lines = ['*filter', '-F ' + FIREWALL_CHAIN,
             f'-A {FIREWALL_CHAIN} -i lo -j RETURN',
             f'-A {FIREWALL_CHAIN} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN']
    for port in ports:
        lines.append(f'-A {FIREWALL_CHAIN} -p tcp --dport {port} -j RETURN')
    for rule in rules:
        lines.append(f'-A {FIREWALL_CHAIN} -p {rule["protocol"]} -s {rule["source"]} --dport {rule["port"]} -j {rule["verdict"]}')
    lines.extend([f'-A {FIREWALL_CHAIN} -m comment --comment ubuntu-node-managed -j RETURN', 'COMMIT', ''])
    return '\n'.join(lines)


def firewall_apply(rules, ports):
    # Only our own chain is flushed. Existing INPUT policy and third-party chains stay intact.
    existing = run(['/usr/sbin/iptables', '-w', '3', '-S', FIREWALL_CHAIN], check=False)
    created = existing.returncode != 0
    if created:
        run(['/usr/sbin/iptables', '-w', '3', '-N', FIREWALL_CHAIN])
    elif 'ubuntu-node-managed' not in existing.stdout:
        raise ValueError('同名链不属于本面板，拒绝覆盖')
    script = firewall_script(rules, ports)
    result = subprocess.run(['/usr/sbin/iptables-restore', '--wait', '3', '--noflush'],
                            input=script, text=True, capture_output=True, timeout=15,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'})
    if result.returncode:
        if created:
            run(['/usr/sbin/iptables', '-w', '3', '-X', FIREWALL_CHAIN], check=False)
        raise ValueError('规则应用失败：' + result.stderr[-2000:])
    if run(['/usr/sbin/iptables', '-w', '3', '-C', 'INPUT', '-j', FIREWALL_CHAIN], check=False).returncode:
        run(['/usr/sbin/iptables', '-w', '3', '-I', 'INPUT', '1', '-j', FIREWALL_CHAIN])


def firewall_dispatch(data):
    saved, version = firewall_saved()
    available, reason, system_rules = firewall_probe()
    if data['action'] == 'firewall':
        ports = protected_ports() if available else [22, 8888]
        current = run(['/usr/sbin/iptables', '-w', '3', '-S', FIREWALL_CHAIN], check=False).stdout if available else ''
        return {'available': available, 'reason': reason, 'rules': saved, 'version': version,
                'protected_ports': ports, 'system_rules': system_rules[-24000:], 'active_rules': current[-24000:]}
    # With no saved rules, boot restoration does not require network capabilities.
    if data['action'] == 'firewall_restore' and not FIREWALL_STATE.exists():
        return {'ok': True}
    if not available:
        raise ValueError(reason)
    ports = protected_ports()
    if data['action'] == 'firewall_save':
        if data.get('version') != version:
            raise ValueError('规则已被其他会话修改，请刷新后重试')
        updated = firewall_rules(data.get('rules'), ports)
    else:
        # A newly detected SSH port supersedes an older deny rule at boot.
        updated = firewall_rules([r for r in saved if not
                                  (r.get('protocol') == 'tcp' and r.get('verdict') == 'DROP' and r.get('port') in ports)], ports)
    try:
        firewall_apply(updated, ports)
        if data['action'] == 'firewall_save':
            write_atomic(FIREWALL_STATE, json.dumps(updated, ensure_ascii=False))
    except Exception as error:
        try:
            firewall_apply(saved, ports)
        except Exception as rollback_error:
            raise ValueError(f'{error}；回滚失败，请通过服务器控制台检查：{rollback_error}') from error
        raise
    return {'ok': True}


def dispatch(data):
    if not isinstance(data, dict):
        raise ValueError('请求必须为对象')
    action = data.get('action')
    if action in ('firewall', 'firewall_save', 'firewall_restore'):
        return firewall_dispatch(data)
    if action == 'services':
        if runtime() == 'supervisor':
            result = supervisor('status', check=False)
            fields = result.stdout.split()
            state = fields[1] if len(fields) > 1 and fields[0] == 'nginx' else 'UNKNOWN'
            active = 'active' if state == 'RUNNING' else 'inactive' if state == 'STOPPED' else 'failed'
            return {'runtime': 'supervisor', 'services': [
                {'name': name, 'load': 'loaded' if name == 'nginx' else 'unsupported',
                 'active': active if name == 'nginx' else 'unknown', 'sub': state if name == 'nginx' else '',
                 'enabled': 'Supervisor' if name == 'nginx' else '容器模式不支持'} for name in SERVICES]}
        services = []
        for name in SERVICES:
            output = run(['/usr/bin/systemctl', 'show', name + '.service',
                          '--property=LoadState,ActiveState,SubState,UnitFileState'], check=False)
            props = dict(line.split('=', 1) for line in output.stdout.splitlines() if '=' in line)
            services.append({'name': name, 'load': props.get('LoadState', 'unknown'),
                             'active': props.get('ActiveState', 'unknown'),
                             'sub': props.get('SubState', ''), 'enabled': props.get('UnitFileState', '')})
        return {'runtime': 'systemd', 'services': services}
    if action == 'logs':
        name = service_name(data.get('service'))
        if runtime() == 'supervisor':
            if name != 'nginx.service':
                raise ValueError('容器模式只支持 Nginx 服务')
            path = Path('/var/log/nginx/error.log')
            if not path.exists():
                return {'text': '暂无 Nginx 错误日志'}
            output = run(['/usr/bin/tail', '-n', '100', str(path)])
            return {'text': output.stdout[-60000:]}
        output = run(['/usr/bin/journalctl', '--unit=' + name, '--lines=100', '--no-pager', '--output=short-iso'])
        return {'text': output.stdout[-60000:]}
    if action == 'service':
        name = service_name(data.get('service'))
        operation = data.get('operation')
        if operation not in OPS:
            raise ValueError('不允许的服务操作')
        if runtime() == 'supervisor':
            if name != 'nginx.service':
                raise ValueError('容器模式只支持 Nginx 服务')
            supervisor(operation)
        else:
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
