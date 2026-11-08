#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "安装失败（行 %s）。修复原因后可重新运行；不会自动删除站点数据。\n" "$LINENO" >&2' ERR
[[ $EUID -eq 0 ]] || { echo '请使用 sudo bash install.sh'; exit 1; }
source /etc/os-release
[[ ${ID:-} == ubuntu && ( ${VERSION_ID:-} == 22.04 || ${VERSION_ID:-} == 24.04 ) ]] || {
  echo '本版本仅面向 Ubuntu 22.04 / 24.04 LTS。'; exit 1;
}
runtime=supervisor
if [[ -d /run/systemd/system && $(cat /proc/1/comm) == systemd ]]; then
  runtime=systemd
fi
printf '运行方式：%s\n' "$runtime"
if [[ $runtime == supervisor && ! -f /etc/ubuntu-node/supervisord.conf && -f /run/nginx.pid ]]; then
  echo '检测到已有独立 Nginx。请先自行停止它，再安装容器版，避免接管已有进程。'; exit 1
fi
password_stdin=0
case "${1:-}" in
  '') ;;
  --password-stdin) password_stdin=1 ;;
  *) echo '用法: sudo bash install.sh [--password-stdin]'; exit 1 ;;
esac
[[ $# -le 1 ]] || exit 1
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in panel/app.py panel/admin.py panel/wsgi.py panel/templates/index.html panel/static/app.js deploy/helper.py deploy/ubuntu-node.service deploy/supervisord.conf; do
  [[ -f "$src/$file" ]] || { echo "缺少源文件 $file，请克隆完整仓库。"; exit 1; }
done
mkdir -p /run/lock
exec 9>/run/lock/ubuntu-node-install.lock
flock -n 9 || { echo '另一个安装正在运行'; exit 1; }
# Check input before changing packages or service files.
initial_password=''
if [[ ! -f /var/lib/ubuntu-node/panel.db ]]; then
  if [[ $password_stdin == 1 ]]; then
    IFS= read -r initial_password
  else
    read -r -s -p '设置 admin 密码（至少 12 位）: ' initial_password </dev/tty
    printf '\n'
    read -r -s -p '再次输入密码: ' repeat_password </dev/tty
    printf '\n'
    [[ $initial_password == "$repeat_password" ]] || { echo '两次密码不一致'; exit 1; }
    unset repeat_password
  fi
  [[ ${#initial_password} -ge 12 ]] || { echo '密码至少需要 12 个字符'; exit 1; }
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
packages=(python3 python3-flask python3-psutil gunicorn sudo nginx ca-certificates)
[[ $runtime != supervisor ]] || packages+=(supervisor)
apt-get install -y "${packages[@]}"
if ! id ubuntu-node >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/ubuntu-node --shell /usr/sbin/nologin ubuntu-node
fi
# Refuse symlink destinations before installing root-owned code.
for path in /opt/ubuntu-node /opt/ubuntu-node/panel /var/lib/ubuntu-node /var/www/ubuntu-node /usr/local/libexec/ubuntu-node-helper; do
  [[ ! -L "$path" ]] || { echo "拒绝符号链接目标: $path"; exit 1; }
done
if [[ $runtime == systemd ]]; then
  systemctl stop ubuntu-node.service 2>/dev/null || true
elif [[ -f /etc/ubuntu-node/supervisord.conf ]]; then
  /usr/bin/supervisorctl -c /etc/ubuntu-node/supervisord.conf stop panel nginx || true
elif [[ -f /run/nginx.pid ]]; then
  # A fresh apt install may have started nginx via a SysV package hook.
  /usr/sbin/nginx -s quit
  for _ in {1..50}; do
    [[ -f /run/nginx.pid ]] || break
    sleep 0.1
  done
fi
install -d -o root -g root -m 755 /opt/ubuntu-node /usr/local/libexec /var/www/ubuntu-node
# Copy only application source, never the checkout's .git, tests or local state.
install -d -m 755 /opt/ubuntu-node/panel /opt/ubuntu-node/panel/templates /opt/ubuntu-node/panel/static
install -m 644 "$src"/panel/*.py /opt/ubuntu-node/panel/
install -m 644 "$src"/panel/templates/*.html /opt/ubuntu-node/panel/templates/
install -m 644 "$src"/panel/static/*.css "$src"/panel/static/*.js /opt/ubuntu-node/panel/static/
chown -R root:root /opt/ubuntu-node
find /opt/ubuntu-node -type d -exec chmod 755 {} +
find /opt/ubuntu-node -type f -exec chmod 644 {} +
install -o root -g root -m 755 "$src/deploy/helper.py" /usr/local/libexec/ubuntu-node-helper
sudoers_tmp=$(mktemp)
trap 'rm -f "$sudoers_tmp"' EXIT
printf 'ubuntu-node ALL=(root) NOPASSWD: /usr/local/libexec/ubuntu-node-helper ""\n' > "$sudoers_tmp"
visudo -cf "$sudoers_tmp"
install -o root -g root -m 440 "$sudoers_tmp" /etc/sudoers.d/ubuntu-node
install -d -o ubuntu-node -g ubuntu-node -m 700 /var/lib/ubuntu-node
if [[ ! -f /var/lib/ubuntu-node/panel.db ]]; then
  cd /opt/ubuntu-node
  printf '%s\n' "$initial_password" | runuser -u ubuntu-node -- /usr/bin/python3 -m panel.admin --password-stdin
fi
unset initial_password
install -d -o root -g root -m 755 /etc/ubuntu-node
printf '%s\n' "$runtime" > /etc/ubuntu-node/runtime
chmod 644 /etc/ubuntu-node/runtime
/usr/sbin/nginx -t
if [[ $runtime == systemd ]]; then
  install -m 644 "$src/deploy/ubuntu-node.service" /etc/systemd/system/ubuntu-node.service
  systemctl daemon-reload
  systemctl enable --now nginx.service ubuntu-node.service
  systemctl is-active --quiet ubuntu-node.service
else
  install -o root -g root -m 644 "$src/deploy/supervisord.conf" /etc/ubuntu-node/supervisord.conf
  if /usr/bin/supervisorctl -c /etc/ubuntu-node/supervisord.conf pid >/dev/null 2>&1; then
    /usr/bin/supervisorctl -c /etc/ubuntu-node/supervisord.conf reread
    /usr/bin/supervisorctl -c /etc/ubuntu-node/supervisord.conf update
    /usr/bin/supervisorctl -c /etc/ubuntu-node/supervisord.conf start all
  else
    /usr/bin/supervisord -c /etc/ubuntu-node/supervisord.conf
  fi
fi
# Wait for HTTP readiness rather than equating a spawned process with a working panel.
/usr/bin/python3 - <<'PYTHON'
import time
import urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8888/', timeout=2) as response:
            if response.status == 200:
                break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('面板未能启动。systemd 环境请查看 journalctl -u ubuntu-node；容器请查看 /var/log/ubuntu-node-panel-error.log。')
PYTHON
printf '\n安装完成。面板仅监听 127.0.0.1:8888，管理员：admin（升级时保留原账号）。\n'
printf '在你的电脑运行：ssh -L 8888:127.0.0.1:8888 用户名@服务器IP\n'
printf '然后打开：http://127.0.0.1:8888\n'
printf '静态站点位于 /var/www/ubuntu-node/，网站 HTTP 端口为 80。\n'
if [[ $runtime == supervisor ]]; then
  printf '\n容器模式：使用 Supervisor 管理面板和 Nginx，其他系统服务不支持操作。\n'
  printf '工作区没有 SSH 时，可使用平台的 8888 端口转发（保持私有访问）。\n'
  printf '容器重启后：sudo /usr/bin/supervisord -c /etc/ubuntu-node/supervisord.conf\n'
fi
