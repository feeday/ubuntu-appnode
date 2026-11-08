# Ubuntu Node

为 **Ubuntu 22.04 / 24.04 LTS** 开发的轻量服务器管理面板，参考 AppNode 的集中式操作体验，独立实现，不是 AppNode 官方产品，也不包含或调用其安装器、二进制程序或授权服务。

首版 `v0.1.0` 管理**当前这台 Ubuntu 服务器**，不支持多节点。Python / Flask 后端，原生 HTML / CSS / JavaScript 界面，无 Node.js 构建依赖。安装使用 Ubuntu 系统软件包。

## 已实现

- 中文深色响应式界面，手机端抽屉导航。
- 管理员登录、密码哈希、8 小时会话、登录限速、CSRF 校验；本地重置密码后旧会话失效。
- CPU、内存、根分区磁盘、系统负载、运行时长和开机累计网络流量，每 5 秒刷新。
- systemd 服务状态、启动 / 停止 / 重启、最近 100 条 journal 日志。
- Nginx 静态站点、本机 HTTP 反向代理、站点列表与移除。
- Nginx 修改前检测同名配置，修改后 `nginx -t`，检查或重载失败时恢复配置。
- 登录和管理操作审计；页面显示最近 100 条，数据库保留完整记录。
- Ubuntu 安装脚本、systemd 服务、受限 root 助手和双版本 CI。

## 安装

自动识别运行环境：完整 Ubuntu 主机使用 systemd；没有 systemd 的容器 / 云工作区使用独立 Supervisor，管理面板和 Nginx。建议先在新建测试服务器验证。现有 Nginx 配置会保留，但安装会启动 Nginx；如其他 Web 服务占用 80 端口，请先处理冲突。

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/feeday/ubuntu-appnode.git
cd ubuntu-appnode
sudo bash install.sh
```

安装会提示设置 `admin` 密码，至少 12 个字符，无默认密码。首次安装需要可访问 Ubuntu APT 软件源。支持 Ubuntu 官方软件仓库提供依赖的架构；CI 验证目标为 x86_64，ARM64 尚未实机验证。

面板只监听 `127.0.0.1:8888`。**在你自己的电脑上**执行以下命令，将用户名和服务器 IP 换成实际值：

```bash
ssh -L 8888:127.0.0.1:8888 用户名@服务器IP
```

保持 SSH 窗口打开，在浏览器访问 **http://127.0.0.1:8888**。Windows PowerShell 自带 OpenSSH 的系统也可使用这条命令。无需开放公网 8888 端口。脚本不修改防火墙或 SSH 配置。

自动化安装可通过标准输入提供密码：`sudo bash install.sh --password-stdin`，仅首次安装读取一行密码。不要把真实密码写入仓库或命令历史。

## 容器 / 云工作区（没有 systemd）

使用相同安装命令，无需安装或强行启动 systemd。安装脚本自动选择 Supervisor，面板仍以普通用户运行，管理接口仍只监听 `127.0.0.1:8888`。

- 有 SSH：使用上面的 SSH 隧道。
- 云工作区没有 SSH：使用平台的 **8888 端口转发 / 预览**，设为私有访问。平台需要支持转发容器的本机监听地址；不要把密码登录页未经 HTTPS 直接公开到互联网。
- 容器模式支持监控、网站管理和 Nginx 启停 / 重启；SSH、Cron、MySQL 等系统服务显示“容器不支持”，不会尝试调用 systemctl。
- Nginx 日志显示最近 100 行错误日志，替代 journald。
- 无 systemd 的容器没有系统自启动承诺：工作区停止或重建会结束进程；持久化需要平台保存 `/etc/ubuntu-node`、`/opt/ubuntu-node`、`/var/lib/ubuntu-node`、Nginx 配置与网站目录。仅保存程序仓库不会保存账号和网站。
- 已有独立 Nginx 时，首次安装会提示先自行停止，避免接管你的已有进程。

查看状态：

```bash
sudo supervisorctl -c /etc/ubuntu-node/supervisord.conf status
sudo tail -n 100 /var/log/ubuntu-node-panel-error.log
```

容器重启后，如果 Supervisor 尚未运行：

```bash
sudo /usr/bin/supervisord -c /etc/ubuntu-node/supervisord.conf
```

如果 Supervisor 已运行但面板停止：

```bash
sudo supervisorctl -c /etc/ubuntu-node/supervisord.conf start all
```

## 网站和服务

在“网站管理”中填写域名，选择静态网站或反向代理：

| 类型 | 行为 |
| --- | --- |
| 静态网站 | 文件目录 `/var/www/ubuntu-node/域名`，自动生成欢迎页；使用 SSH / SFTP 上传你的网页 |
| 反向代理 | 代理到 `http://127.0.0.1:端口`，允许 1024–65535，禁止代理面板端口 8888 |
| 移除网站 | 只移除本面板生成的 Nginx 配置，保留网站目录和文件 |

将域名 DNS 解析到服务器，并在云安全组和主机防火墙按需放行 **80/TCP**。首版不自动申请 HTTPS 证书，不提供 PHP / 数据库安装器、文件编辑器、终端、WebSocket 专用代理模板或多节点代理。PHP-FPM / MySQL / Redis 等已安装服务可查看状态和管理，未安装时不会自动安装。

服务白名单：`nginx`、`ssh`、`cron`、`mysql`、`mariadb`、`redis-server`、`php8.1-fpm`、`php8.3-fpm`。停止 SSH 或 Web 服务可能影响连接，请确认操作对象。

站点配置保存在 `/etc/nginx/conf.d/ubuntu-node-域名.conf`。面板只管理带有自身元数据标记的文件。请勿手工去掉第一行标记；对于手工配置的同名站点，先自行迁移。通配符或正则 server_name 冲突仍需管理员排查。

## 升级、密码与日志

```bash
cd ubuntu-appnode
git pull --ff-only
sudo bash install.sh
```

升级保留账号、数据库和网站文件。升级会短暂重启面板；失败时不会自动卸载或删除数据。更新前建议备份以下配置和状态。

重置管理员密码（同时撤销旧登录会话）：

```bash
cd /opt/ubuntu-node
sudo -u ubuntu-node /usr/bin/python3 -m panel.admin --username admin
```

查看运行状态：

```bash
sudo systemctl status ubuntu-node nginx
sudo journalctl -u ubuntu-node -n 100 --no-pager
sudo nginx -t
```

| 路径 | 用途 |
| --- | --- |
| `/opt/ubuntu-node` | root 拥有的程序代码 |
| `/var/lib/ubuntu-node` | SQLite 账号 / 审计数据库、会话密钥；仅服务用户可读写 |
| `/usr/local/libexec/ubuntu-node-helper` | root 拥有的受限操作助手 |
| `/etc/sudoers.d/ubuntu-node` | 仅允许无参数执行该助手 |
| `/etc/systemd/system/ubuntu-node.service` | 面板服务 |
| `/etc/nginx/conf.d/ubuntu-node-*.conf` | 本面板创建的站点配置 |
| `/var/www/ubuntu-node` | 静态网站文件 |

备份时先停止面板 `sudo systemctl stop ubuntu-node`，用 root 备份上述状态、站点配置与网站目录，然后启动面板。备份包含密码哈希和会话密钥，请限制访问。

## 权限模型

Web 进程以 `ubuntu-node` 普通用户运行。助手通过固定路径与 JSON 标准输入通信，以 Python `-I` 隔离模式运行；只允许预设服务操作和验证后的站点配置，不提供任意命令、任意路径或原始 Nginx 配置入口。Nginx 写操作加锁，配置原子替换。程序、助手和 sudoers 必须保持 root 所有且不可由面板用户写入。

默认单管理员、单进程 Gunicorn，4 个线程。未启用代理头信任，登录限速按照实际连接 IP 每 15 分钟最多 10 次失败尝试；通过 SSH 隧道的访问共享本机来源配额。审计信息存储在服务用户可写的 SQLite 中，用于日常排查，不是防篡改审计系统。

默认 HTTP 只用于 SSH 隧道内部。若自行配置公网 HTTPS 反代，需同时配置可信来源、访问限制和服务环境 `UBUNTU_NODE_HTTPS=1`，不要直接把 Gunicorn 改为公网监听。本项目首版未提供这一配置向导。

## 验证

```bash
sudo apt-get install -y python3-flask python3-psutil
/usr/bin/python3 -m unittest discover -s tests -v
bash -n install.sh
```

GitHub Actions 在 Ubuntu 22.04 / 24.04 虚拟机和无 systemd 的 Docker 容器上执行安装测试，在虚拟机上执行回归测试，并在**临时 CI 虚拟机**实际安装，验证 systemd、权限边界、登录、静态站点、反向代理、站点移除与重复安装。`scripts/integration.py` 会安装系统包和服务，只用于可丢弃的测试机，不要直接在生产服务器运行。

界面使用真实 API 数据；系统操作单元测试使用模拟命令，不能代替真实 Ubuntu 安装验证。请查看仓库 Actions 的具体运行结果，不以语法检查代替功能验证。

## 停用与卸载

systemd 环境先停用面板：

```bash
sudo systemctl disable --now ubuntu-node
```

容器环境停用：`sudo supervisorctl -c /etc/ubuntu-node/supervisord.conf shutdown`（同时停止本面板托管的 Nginx）。

如需卸载，可手动移除 `/etc/systemd/system/ubuntu-node.service`、`/etc/sudoers.d/ubuntu-node`、`/usr/local/libexec/ubuntu-node-helper` 和 `/opt/ubuntu-node`，再执行 `sudo systemctl daemon-reload`。默认保留 Nginx、网站配置、网站文件、服务用户与数据库，以免影响已运行的网站。确认备份后再自行清理这些数据。

## License

本项目自行编写的代码使用 MIT License；AppNode 名称仅用于说明设计参考，与其无关联或授权关系。
