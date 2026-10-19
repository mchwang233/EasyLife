# EasyLife 部署指南（生产服务器）

把 EasyLife（任务 + 邮件提醒服务）部署到一台已经在跑 `mch.wang` 站点的 Linux 服务器上
（Ubuntu 20.04+ / Debian 11+ / CentOS 8+ 或同年代发行版）。两种方式：

| 模式 | 访问地址 | nginx 改动 | DNS |
| --- | --- | --- | --- |
| **路径模式（首选方案，默认）** `--mode path` | `https://mch.wang/todos/` | 在现有 `mch.wang` server 块（80/443）里各**插入一行 include**，先备份，可一键回滚 | 不需要改 |
| 子域模式（备选）`--mode subdomain` | `https://easylife.mch.wang/` | **新增**独立的 `conf.d/easylife.conf`，不碰现有站点 | 新增 A 记录 |

应用**只用 Python 标准库**，外部依赖仅系统的 `python3`、`nginx` 与 `systemd`；
始终以独立的 `easylife` 用户运行，只监听 `127.0.0.1:8086`。

> 本文命令请你在自己的电脑 / 服务器上执行。动手前先读第 0 节与第 2 节。

## 0. 快速开始（动手前必读）

### 0.1 部署命令与验证

在**你本地**的仓库根目录执行（第 4 节有详细说明）：

```bash
cd <仓库根目录>
ssh root@<服务器IP> 'echo ok'      # 先确认网络与登录可用
bash deploy/deploy-remote.sh <服务器IP> --check    # 可选：先只读预检（不改服务器任何东西）
bash deploy/deploy-remote.sh <服务器IP>            # 一键：打包 → scp → 远程部署 → 三条 curl 自检
```

> **服务器自带的 `python3` 低于 3.8 时**（例如 CentOS 7 的 3.6），先在服务器上装好新版 Python，
> 再用 `EASYLIFE_PYTHON` 指定它，例如：
> `EASYLIFE_PYTHON=/opt/easylife-python/bin/python3 bash deploy/deploy-remote.sh <服务器IP> -i ~/.ssh/id_ed25519`

也可以登录服务器后手工执行 `sudo bash deploy/deploy.sh --mode path`（步骤见第 5.1 节）。
`8086` 被占用时加 `--port 8090`；用私钥时加 `-i ~/.ssh/id_ed25519`。

**执行完后的验证方法**：

```bash
# 1) 命令行：下面三条应依次返回 200 / 301 / 401
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos/
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos/api/me

# 2) 服务器上确认后端只在回环监听、服务是 active
ssh root@<服务器IP> 'systemctl is-active easylife; systemctl is-enabled easylife; ss -lntp | grep 8086'

# 3) 浏览器打开 https://mch.wang/todos/  ← 注意结尾的斜杠
#    能看到 EasyLife 注册/登录页、能注册、能新建一条 Todo、刷新后数据还在 = 部署成功
```

> **请用带尾斜杠的 `https://mch.wang/todos/` 访问。** 不带斜杠的 `/todos` 会 301 跳到 `/todos/`，
> 这是刻意设计的：页面里用的是 `./static/...`、`./api/...` 相对路径，只有落在 `/todos/` 下才能解析正确。

### 0.2 常见故障排查（部署后打不开时按顺序做）

| 现象 | 排查与处理 |
| --- | --- |
| **502 Bad Gateway**（nginx 活着，后端没起来） | 在服务器上 `systemctl status easylife --no-pager`，再看 `journalctl -u easylife -n 50 --no-pager`。常见原因：`/etc/easylife/easylife.env` 里 `EASYLIFE_SECRET` 缺失或短于 16 字符、`/var/lib/easylife` 权限不对、8086 被别的进程占用。再确认 `ss -lntp \| grep 8086` 有 `127.0.0.1:8086`，且端口与片段里 `proxy_pass` 一致；改完 `sudo systemctl restart easylife` |
| **404 Not Found**（nginx 没把 `/todos/` 交给 EasyLife） | 检查那一行 include 是否真在**承载该域名的 443 server 块**里：`grep -rn 'managed-by-easylife-deploy' /etc/nginx`、`cat /etc/easylife/nginx-path.manifest`、`cat /etc/nginx/easylife/todos-location.conf`；用 `sudo nginx -T \| grep -n -B2 -A6 'server_name mch.wang'` 确认 https 的那个 server 块里有 include。若 80 端口的块只是 `return 301 https://...`，光在 80 里插 include 是不够的（脚本会 80/443 都插）。补上后 `sudo nginx -t && sudo systemctl reload nginx` |
| **页面样式/图标丢失、按钮点了没反应**（HTML 回来了但 CSS/JS 404） | 几乎都是访问路径不对：请用**带斜杠**的 `https://mch.wang/todos/`。再确认片段是 `location ^~ /todos/`、`proxy_pass http://127.0.0.1:8086/;` 末尾有 `/`，且 env 里 `EASYLIFE_BASE_PATH=/todos` |
| **其它站点（`https://mch.wang/` 首页等）异常** | 按设计不该发生：脚本只在 `mch.wang` 的 server 块里加一行 include。立刻 `sudo bash /opt/easylife/deploy/deploy.sh --rollback` 还原（见第 10 节），再排查 |
| 想彻底撤掉 | `sudo bash /opt/easylife/deploy/deploy.sh --rollback`，见第 10 节（保留代码/数据/密钥，只撤销 nginx 与 systemd 改动） |

### 0.3 前置条件

- **服务器**：Linux + systemd（Ubuntu 20.04+ / Debian 11+ / CentOS 8+ 或同年代发行版）；有 `root` 或 `sudo`；
  **`nginx` 已安装且 `mch.wang` 站点正在运行**（路径模式就是挂到这个既有站点上）；`python3 ≥ 3.8`
  且带 `sqlite3` / `ssl` / `smtplib` 标准库；`8086` 空闲（不空闲就 `--port 8090`）；云安全组放行 `22/80/443`。
- **本地**：`bash`、`tar`、`ssh`/`scp`、`curl`；能 SSH 登录服务器（**推荐用 SSH 密钥**，见第 2 节）；
  仓库根目录下存在 `easylife/`、`deploy/`、`docs/`。
- **不需要**新增 DNS 记录、**不需要**新证书：路径模式沿用 `mch.wang` 现有的域名与 HTTPS 证书，
  `https://mch.wang/todos/` 走的就是现有 443 server 块的证书（脚本不会加任何 `ssl_certificate` 行）。
- 想更稳妥可以先自己备份：`sudo tar czf /var/backups/nginx-$(date +%F).tgz /etc/nginx`
  （脚本自己也会逐个文件备份，见第 5.1 节）。

### 0.4 `deploy-remote.sh` 能力速览：`--check`、部署后自检、`SSHPASS`

| 能力 | 怎么用 | 行为 |
| --- | --- | --- |
| **只读预检 `--check`** | `bash deploy/deploy-remote.sh <服务器IP> --check` | 只做 4 项检查——SSH 连通、远端 `nginx -t`（不 reload）、`8086` 端口空闲、`mch.wang` 的 443 server 块存在——然后打印 `预检汇总：PASS n / FAIL m`。**不上传、不写任何远端文件、不 reload、不重启**；全 PASS 才退出 0。建议顺序：先 `--check`，再跑正式部署 |
| **部署后自检**（默认开启，4 项） | 正式部署命令里不用加任何参数 | 部署成功后**在服务器上**跑三条 `curl`：`/todos` → **301**、`/todos/` → **200**、未登录 `/todos/api/todos` → **401**；再加第 4 项**页面标识检查**：`/todos/` 页面必须包含新界面标识 `verification-banner`（可用 `EASYLIFE_UI_MARKER=<字符串>` 覆盖）。逐条打印 `[PASS]`/`[FAIL]`，末行 `自检汇总：PASS 4 / FAIL 0`。任一条失败即非 0 退出，并提示 `bash deploy/deploy-remote.sh <host> --rollback`。`--no-smoke` 可跳过 |
| **用 `SSHPASS` 传密码** | `export SSHPASS='<服务器密码占位符>'` 后再跑上面的命令 | 密码**只从环境变量读**；且**仅当本机装有 `sshpass`** 时脚本才用 `sshpass -e`（密码不出现在命令行、进程列表、脚本输出与 `--log` 日志里），没有 `sshpass` 就自动退回密钥或交互式输入。用完 `unset SSHPASS` |

```bash
# SSHPASS 用法（占位符示例；请勿把真实密码写进任何文件或命令历史）
export SSHPASS='<服务器密码占位符>'
bash deploy/deploy-remote.sh <服务器IP> --check     # 只读预检
bash deploy/deploy-remote.sh <服务器IP>             # 正式部署 + 自检（PASS 4 / FAIL 0）
unset SSHPASS
```

> 认证首选 **SSH 密钥**（`-i ~/.ssh/id_ed25519`，可完全非交互）；`SSHPASS` 只是过渡手段。
> **脚本、`--log` 日志、文档与仓库里都只写占位符，不写、不回显任何真实密码。**

## 1. 产物一览

| 文件 | 作用 |
| --- | --- |
| `deploy/deploy-remote.sh` | **在本地运行**（一条命令、可完全非交互）：打包 → `scp` 到服务器 `/opt/easylife-src` → `ssh` 执行 `deploy.sh --mode path` → **在服务器上跑三条 `curl` 自检**（301/200/401）并打印 PASS/FAIL 汇总；另有只读预检 `--check` 与 `--rollback`，见第 4 节 |
| `deploy/deploy.sh` | 幂等部署脚本（在服务器上以 **root** 运行），默认路径模式，见第 5 节 |
| `deploy/nginx-easylife-location.conf` | 路径模式的 nginx 片段：只有 `location = /todos`（301 到 `/todos/`）和 `location ^~ /todos/`（反代到 `127.0.0.1:8086/`） |
| `deploy/nginx-easylife.conf` | 子域模式的独立 `server` 块（含手工版路径片段的注释） |
| `deploy/easylife.service` | systemd 单元：`User=easylife`、`WorkingDirectory=/opt/easylife`、`EnvironmentFile=/etc/easylife/easylife.env`、仅监听 `127.0.0.1:8086`、`Restart=on-failure`，含 `NoNewPrivileges` / `ProtectSystem=strict` / `ReadWritePaths=/var/lib/easylife` 等加固项 |
| `deploy/easylife.env.example` | 环境变量模板（`EASYLIFE_BASE_PATH`、`PUBLIC_URL`、`SMTP_*`、`EASYLIFE_SECRET` 占位符），**不含任何真实密码** |
| `deploy/backup.sh` | 备份 SQLite 数据库（在线快照 + 完整性校验 + 轮转），见第 9 节 |

## 2. 安全须知（先读）

1. **只用 SSH 密钥登录，不要用密码登录。**
   - 本地生成密钥：`ssh-keygen -t ed25519 -C "easylife-deploy"`
   - 上传公钥：`ssh-copy-id -i ~/.ssh/id_ed25519.pub root@<服务器IP>`
   - 服务器 `/etc/ssh/sshd_config`：`PermitRootLogin prohibit-password`、`PasswordAuthentication no`，
     然后 `sshd -t && systemctl reload sshd`。
2. **不要把 root 密码写进任何文件、脚本、命令历史或本仓库。** 需要提权就用 SSH 密钥（或 `sudo`）。
   `deploy/` 下脚本全部以 root 运行，但**不接收也不存储任何密码**；`easylife.env` 里只有 `SMTP_PASS`
   一项凭据，它是**邮箱授权码**而非系统密码，且只存在于服务器 `/etc/easylife/easylife.env`（`640 root:easylife`）。
3. **如果 root 密码曾在任何对话/工单/截图中出现过，请在部署后第一时间更换**：用 SSH 密钥登录后
   执行 `passwd`（或由云厂商控制台重置），并确认旧密码不再可用于登录。本文档与仓库中不会、
   也不应出现该密码本身。
4. 公网访问一律经过 nginx（TLS 在 nginx 终止）；应用**只监听 `127.0.0.1:8086`**，nginx 挂掉也不会
   把后端直接暴露到公网。

## 3. DNS 解析配置（仅子域模式）

在 `mch.wang` 的 DNS 服务商处添加 A 记录：

| 主机记录 | 类型 | 记录值 | TTL |
| --- | --- | --- | --- |
| `easylife` | A | `<服务器公网 IPv4>` | 600 |

有 IPv6 可再加 `AAAA`。生效后验证（可能需等几分钟到 TTL）：

```bash
dig +short easylife.mch.wang        # 应返回服务器 IP
getent hosts easylife.mch.wang
```

> 以上只在**子域模式**需要。默认的路径模式（`https://mch.wang/todos/`）**不需要**新增 DNS 记录。

## 4. 首选方案：一条命令远程部署（`deploy/deploy-remote.sh`）

> 首选的是**路径模式**：把 EasyLife 挂到现有站点 `https://mch.wang/todos/` 下，不改 DNS、不申请新证书。
> 需要你本地能 SSH 到服务器（前置条件见第 0.3 节）。下面的命令在**本地**仓库根目录执行：

```bash
bash deploy/deploy-remote.sh <服务器IP>              # 默认 root@，路径模式
bash deploy/deploy-remote.sh root@<服务器IP> --port 8090                 # 8086 被占用时
bash deploy/deploy-remote.sh <服务器IP> -i ~/.ssh/id_ed25519             # 指定私钥
bash deploy/deploy-remote.sh <服务器IP> -- --smtp-host smtp.qq.com --smtp-user you@qq.com
bash deploy/deploy-remote.sh <服务器IP> --check            # 只读预检：SSH 连通 + nginx -t + 8086 空闲 + 443 server 块
bash deploy/deploy-remote.sh <服务器IP> --rollback         # 撤销本次部署做的 nginx/systemd 改动
bash deploy/deploy-remote.sh <服务器IP> --log /tmp/d.log   # 输出同时写日志（日志里不含任何密码）
```

它会：

1. 用 `tar` 只打包 `easylife/`、`deploy/`、`docs/`（不含 `.git`、测试、缓存、`*.db`、`*.env`）；
2. `scp` 上传到服务器 `/opt/easylife-src/easylife-src.tar.gz` 并解包；
3. `ssh` 执行 `bash deploy/deploy.sh --mode path [你给的参数]`（非 root 用户会自动加 `sudo`）；
4. **在服务器上**执行三条 `curl` 自检（不用本机 DNS/网络，服务器自己打自己）：
   `https://mch.wang/todos` 必须 **301**、`https://mch.wang/todos/` 必须 **200**、
   `https://mch.wang/todos/api/todos`（未登录）必须 **401**；每条打印 `[PASS]`/`[FAIL]`，
   最后一行是 `自检汇总：PASS n / FAIL m`。任一条失败就非 0 退出，并提示用
   `bash deploy/deploy-remote.sh <host> --rollback` 回到部署前状态。
   （服务器上打公网地址完全无响应时——DNS 未生效或云厂商不支持回流——会自动改用
   `curl --resolve mch.wang:443:127.0.0.1` 复测一次，仍走 nginx 与 TLS，只是绕过 DNS。）
   `--no-smoke` 可跳过这三条自检。

**认证**：默认优先 SSH 密钥（`ssh-agent`、`~/.ssh/id_*` 或 `-i`），可完全非交互；整个过程复用一条
SSH 连接（ControlMaster）。也可以 `export SSHPASS=<密码>`：**仅当本机装有 `sshpass`** 时脚本才走密码方式
（`sshpass -e`，从环境变量读，密码不出现在命令行/进程列表里）；没有 `sshpass` 就自动退回密钥或交互式输入。
**脚本、日志（`--log`）与文档里都不写、不回显任何明文密码**；用完请 `unset SSHPASS`，并尽快改用密钥登录（见第 2 节）。
`--log <文件>` 把本次输出追加写入日志文件，日志内容与终端一致、同样不含密码。

### 4.1 手工上传（等价步骤）

```bash
tar -czf /tmp/easylife-src.tar.gz easylife deploy docs
ssh root@<IP> 'mkdir -p /opt/easylife-src'
scp /tmp/easylife-src.tar.gz root@<IP>:/opt/easylife-src/
ssh root@<IP> 'cd /opt/easylife-src && tar -xzf easylife-src.tar.gz && bash deploy/deploy.sh --mode path'
```

校验完整性（可选）：本地 `sha256sum /tmp/easylife-src.tar.gz`，服务器上再算一次比对。

### 4.2 预检模式：`--check`（只读，不做任何修改）

```bash
bash deploy/deploy-remote.sh <服务器IP> --check
```

只做四件事，然后打印 `PASS`/`FAIL` 与 `预检汇总：PASS n / FAIL m`：

1. SSH 连通（不成功就只报这一条 FAIL，并提示 `-i` / `SSHPASS` 两条认证路径）；
2. 远端 `nginx -t`（只校验语法，**不 reload**）；
3. `8086` 端口空闲（被别的进程占用 → FAIL 并提示 `--port 8090`；已被本服务 `easylife` 占用则视为
   已部署状态，算 PASS；用 `--port` 时检查的是你将使用的那个端口）；
4. `mch.wang` 的 **443 server 块**存在（解析 `nginx -T` 输出，按花括号配对找 `server_name` 含
   `mch.wang` 且 `listen …443` 的块；找不到 → FAIL 并提示改用 `--mode subdomain`）。

**`--check` 全程不做任何修改**：不上传、不写任何远端文件、不 reload、不重启服务、不生成任何配置；
任一项 FAIL 就以非 0 退出，四项全 PASS 才退出 0（并提示去掉 `--check` 即可部署）。
**建议顺序**：`--check` → 通过后再跑不带 `--check` 的那条命令。

### 4.3 部署后自检与回滚

部署成功后会打印（输出示例）：

```text
[PASS] 自检 1/3：https://mch.wang/todos -> 301（期望 301）
[PASS] 自检 2/3：https://mch.wang/todos/ -> 200（期望 200）
[PASS] 自检 3/3：https://mch.wang/todos/api/todos -> 401（期望 401）
[i] 自检汇总：PASS 4 / FAIL 0
[+] 部署完成并通过自检：https://mch.wang/todos/
```

有失败时会打印每条 `[FAIL]`（含期望码与实际码）、`自检汇总`，并给出排查命令与
`bash deploy/deploy-remote.sh <host> --rollback`（等价于在服务器上 `sudo bash /opt/easylife/deploy/deploy.sh --rollback`，
见第 10 节；只撤销本次的 nginx/systemd 改动，数据与密钥保留）。

## 5. 备选（等价）方案：在服务器上手工执行 `deploy.sh --mode path`

以 root 在服务器上运行（脚本**幂等**，可反复执行）：

```bash
cd /opt/easylife-src
bash -n deploy/deploy.sh                          # 语法自检
sudo bash deploy/deploy.sh                        # 默认 --mode path：https://mch.wang/todos/
```

### 5.1 路径模式（默认）做了什么

1. **端口检查**：`ss -lntp` 看 `8086` 是否被占用；被别的进程占用就退出，提示用 `--port 8090` 换端口
   （端口会同步写进 env 的 `EASYLIFE_PORT` 和 nginx 片段的 `proxy_pass`）。已由 `easylife` 自己占用时按重跑继续。
2. **找站点（改动前预检）**：用 `nginx -T` 列出实际加载的配置文件（拿不到就 `grep` `/etc/nginx`），
   解析出 `server_name` 含 `mch.wang` 的 server 块，**80 和 443 都处理**（`api.mch.wang` 之类的其它域名不算）。
   - 找不到 → 退出，提示改用 `--mode subdomain` 或手工处理，**不做任何改动**；
   - 该 server 块里已经有 `location /todos…`（`= /todos`、`/todos/`、`^~ /todos` 或匹配 `/todos` 的正则）→
     **拒绝覆盖**并列出文件与行号，返回非 0，**不做任何改动**；别的站点（如 `blog.example.com`）里的 `/todos` 不受影响。
3. 检查 `python3`（≥3.8 及 `sqlite3`/`ssl`/`smtplib` 等标准库），创建系统用户 `easylife` 与
   `/opt/easylife`（代码，只读）、`/var/lib/easylife`（数据）、`/etc/easylife`（配置）。
4. 生成 / 更新 `/etc/easylife/easylife.env`（`640 root:easylife`）：首次自动生成随机 `EASYLIFE_SECRET`，
   **已存在则保留密钥**；每次都确保 `EASYLIFE_BASE_PATH=/todos`、`PUBLIC_URL=https://mch.wang/todos`。
   SMTP 可选（见下）。
5. 安装 systemd 单元并 `systemctl enable --now easylife`（先起后端，再接 nginx，避免 502）。
6. 渲染 `deploy/nginx-easylife-location.conf` 到 **`/etc/nginx/easylife/todos-location.conf`**
   （刻意不放 `conf.d/`、`sites-enabled/`：那里会被 http 上下文整体 include，`location` 放在那里 `nginx -t` 会失败）。
7. **先备份**每个要改的文件到 `/var/backups/easylife/nginx/<路径>.<时间戳>.bak`（放在 nginx 目录外，
   免得 `include sites-enabled/*` 把副本也加载进去），再在每个 `mch.wang` server 块的 `server {` 下一行插入：
   ```nginx
   include /etc/nginx/easylife/todos-location.conf; # managed-by-easylife-deploy
   ```
   已经插入过就跳过（**幂等**：重跑不会重复插入、不会产生新备份、不会无谓 reload）。只用 `head`/`tail` 拼接，
   文件其余字节保持不变；`sites-enabled` 的软链接保持软链接。
8. `nginx -t` **通过才** `systemctl reload nginx`；**失败就把备份逐字节写回、删除新片段、返回非 0**，
   不 reload，现有站点保持原样。改动清单记在 `/etc/easylife/nginx-path.manifest`，供回滚使用。

片段内容（`--port` 会替换 `8086`）：

```nginx
location = /todos {
    return 301 /todos/;
}
location ^~ /todos/ {
    proxy_pass            http://127.0.0.1:8086/;   # 末尾 "/"：剥掉 /todos 前缀再转发
    proxy_http_version    1.1;
    proxy_set_header      Host               $host;
    proxy_set_header      X-Real-IP          $remote_addr;
    proxy_set_header      X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_set_header      X-Forwarded-Proto  $scheme;
    proxy_set_header      X-Forwarded-Host   $host;
    proxy_set_header      X-Forwarded-Prefix /todos;
    proxy_set_header      Connection         "";
    client_max_body_size  1m;
    proxy_connect_timeout 5s;
    proxy_send_timeout    30s;
    proxy_read_timeout    30s;
}
```

> 用 `^~` 而不是普通前缀：命中 `/todos/` 后不再匹配正则 location，避免现有站点的
> `location ~* \.(js|css)$` 之类规则把 `/todos/static/app.js` 抢走。

### 5.2 常用参数

```bash
sudo bash deploy/deploy.sh --port 8090                      # 8086 被占用
sudo bash deploy/deploy.sh --site-domain example.com        # 挂到别的现有站点
EASYLIFE_SMTP_PASS='<邮箱授权码>' sudo -E bash deploy/deploy.sh \
     --smtp-host smtp.qq.com --smtp-port 465 --smtp-user you@qq.com   # 可选 SMTP
sudo bash deploy/deploy.sh --no-nginx                       # 只装服务，nginx 你自己接
sudo bash deploy/deploy.sh --rollback                       # 回滚，见第 10 节
sudo bash deploy/deploy.sh --help
```

SMTP 授权码**不走命令行参数**（会进 shell 历史和进程列表）：从环境变量 `EASYLIFE_SMTP_PASS` 读，
没设置且在终端里运行时会提示不回显输入。不配 SMTP 也能用：`SMTP_HOST` 为空时是「日志模式」。

### 5.3 子域模式（原方案，保留）

```bash
sudo bash deploy/deploy.sh --mode subdomain --domain easylife.mch.wang
sudo bash deploy/deploy.sh --mode subdomain --domain easylife.mch.wang \
     --certbot --certbot-email you@example.com          # 只为新域名申请证书
```

旧命令行（只写 `--domain` / `--certbot` / `--nginx-conf`、不写 `--mode`）仍按子域模式处理。步骤：

1. 端口检查、python3 检查、用户与目录、代码、env（`PUBLIC_URL=https://<域名>`、`EASYLIFE_BASE_PATH=` 为空）、systemd 单元（同上）；
2. **仅当 `/etc/nginx/conf.d/easylife.conf` 不存在时**才写入新的 server 块——**绝不覆盖已有站点配置**；
   写完 `nginx -t`，**通过才** reload，**失败则删除刚写入的新文件并退出**；
3. 可选 `certbot --nginx -d <域名>`（仅对新域名申请证书）；
4. `systemctl enable --now easylife`。

## 6. 两种 nginx 方案对比

> 前提：**`mch.wang` 已经有现有站点，绝不覆盖它。** 默认用路径模式；路径被占用或不想动现有站点时用子域模式。

### 6.1 路径模式（首选方案）：`https://mch.wang/todos/`

**为什么不影响已有服务**（六个要点）：

1. **只在 `mch.wang` 的 server 块里加 `location`**：改动就是 80/443 两个 server 块各插入一行
   `include /etc/nginx/easylife/todos-location.conf;`（片子里只有 `location = /todos` 与
   `location ^~ /todos/`），别处一个字节都不动；`api.mch.wang`、其它站点、`default_server` 一概不碰。
2. **先备份**：改前把每个要改的文件逐字节复制到 `/var/backups/easylife/nginx/<路径>.<时间戳>.bak`。
3. **`nginx -t` 通过才 `systemctl reload nginx`**：语法不过就不 reload，线上仍是老配置。
4. **失败自动回滚**：`nginx -t` 失败时把备份逐字节写回、删掉新片段、恢复 env 与服务状态，以非 0 退出。
5. **独立用户**：应用以专用非特权用户 `easylife` 运行，与既有站点不共用账户，也不使用 root。
6. **只监听 `127.0.0.1:8086`**：`ExecStartPre` 会断言 `EASYLIFE_HOST=127.0.0.1`，绑非回环地址直接拒绝启动；
   即便 nginx 出问题，后端也不会暴露到公网。

另外：

- 对现有站点的改动只有每个 `mch.wang` server 块里**一行带标记的 include**；改前有带时间戳的备份，
  `nginx -t` 失败自动恢复，`--rollback` 删除该行并恢复备份。
- 前端只用相对路径（`./static/...`、`./api/...`），在 `/todos/` 下解析为 `/todos/static/...`、`/todos/api/...`，
  不会打到现有站点的 `/static/`、`/api/`。
- 会话 Cookie 名 `easylife_session`、`Path=/todos`、`HttpOnly`，https 下带 `Secure`，与 `mch.wang` 其它服务互不影响。
- 邮件链接、`.ics`、日历订阅 URL、Google 日历链接都以 `PUBLIC_URL=https://mch.wang/todos` 为前缀。
- 如果 `mch.wang` 的 80 端口 server 块是 `return 301 https://...`，`http://mch.wang/todos/` 会照旧跳到 https。

**手工接入**（不想让脚本改文件时）：`sudo bash deploy/deploy.sh --no-nginx`，然后把
`deploy/nginx-easylife-location.conf` 复制为 `/etc/nginx/easylife/todos-location.conf`，在 `mch.wang` 的 server 块里加
`include /etc/nginx/easylife/todos-location.conf;`，最后 `sudo nginx -t && sudo systemctl reload nginx`。

### 6.2 子域模式：`easylife.mch.wang`

新增一个**完全独立**的 `server` 块，与现有站点互不干扰（`server_name` 精确匹配；即使 80 端口已被
现有站点设为 `default_server`，本块也不会抢走它的流量）。

```bash
# 1) DNS 加 easylife 的 A 记录（见第 3 节）
# 2) 推荐用脚本：sudo bash deploy/deploy.sh --mode subdomain --domain easylife.mch.wang
#    或手工安装（只需改一行 server_name）：
sudo cp /opt/easylife-src/deploy/nginx-easylife.conf /etc/nginx/conf.d/easylife.conf
sudo sed -i 's/^\(\s*server_name\s*\).*/\1easylife.mch.wang;/' /etc/nginx/conf.d/easylife.conf
sudo nginx -t && sudo systemctl reload nginx
```

环境变量：`EASYLIFE_BASE_PATH=`（空）、`PUBLIC_URL=https://easylife.mch.wang`（末尾不加 `/`）。

两种模式可以同时存在；无论哪种，应用始终只监听 `127.0.0.1:8086`。

## 7. SMTP 配置（QQ / 163 邮箱授权码）

编辑 `/etc/easylife/easylife.env`（模板见 `deploy/easylife.env.example`）：

```ini
EASYLIFE_BASE_PATH=/todos               # 子域模式留空
PUBLIC_URL=https://mch.wang/todos       # 子域模式为 https://easylife.mch.wang
EASYLIFE_SECRET=<用下方命令生成，至少 16 字符>
SMTP_HOST=smtp.qq.com        # 163 邮箱填 smtp.163.com
SMTP_PORT=465                # 465=SMTP_SSL；587=STARTTLS（应用自动选择）
SMTP_USER=you@qq.com
SMTP_PASS=<邮箱授权码，不是邮箱登录密码>
SMTP_FROM=EasyLife <you@qq.com>   # 可留空，默认用 SMTP_USER
```

**`SMTP_HOST` 留空 = 日志模式**：只把提醒写进日志、不真发信，便于先跑通再补配置。

邮箱验证和找回密码复用上述 SMTP 配置。在环境文件中追加以下两行（注释另起一行）：

```ini
EASYLIFE_PUBLIC_URL=https://mch.wang/todos
EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true
```

`EASYLIFE_PUBLIC_URL` 默认空，沿用已有 `PUBLIC_URL` / `BASE_URL` 并保留
`EASYLIFE_BASE_PATH`。验证和重置链接分别进入 `/#/verify?token=...` 和
`/#/reset?token=...`，路径部署下均包含 `/todos`。默认要求新用户验证后登录；
启动时自动迁移已有库，老用户自动标记为已验证，新用户不受这次回填影响。
设置 `EASYLIFE_REQUIRE_EMAIL_VERIFICATION=false` 可关闭登录验证要求，注册仍发送验证邮件。
`SMTP_HOST` 为空时也允许未验证用户登录，并输出 WARNING 与认证邮件正文到服务器日志；
日志包含可用链接，应按密钥保护，不能公开。SMTP 已配置但投递失败时继续要求验证，
服务器记录错误，用户可稍后重发。

新增 `POST /api/verify-email`（`token`）、`POST /api/resend-verification`（登录或 `email`）、
`POST /api/forgot-password`（`email`）、`POST /api/reset-password`（`token`、`password`）；
`GET /api/me` 的 `user.email_verified` 返回验证状态。未验证登录返回
`403 / email_not_verified`，登录页提供重发入口，忘记密码入口和邮件中的链接均可直接使用。
验证令牌有效期 24 小时、重置令牌 1 小时，只存 SHA-256，单次使用；同类新邮件会废弃旧链接。
重置成功后所有原会话失效。找回密码对邮箱存在、不存在以及投递失败均返回相同 200 文案。
同一邮箱同类邮件 60 秒内限一次，超过返回 429 和 `Retry-After`；计数复用现有进程内限流器，
应用重启会清空，另有每 IP 每小时 20 次的认证邮件请求限制。
这些接口共用 JSON / 请求体限制；携带有效登录会话时必须提供该会话的 `X-CSRF-Token`，
未登录时凭邮箱或一次性令牌完成恢复。

获取**授权码**（不是登录密码）：

* **QQ 邮箱**：网页版 → 设置 → 账号 → 开启「IMAP/SMTP 服务」→ 短信验证 → 得 16 位**授权码**。
* **163 邮箱**：设置 → 开启「POP3/SMTP/IMAP」中的 **SMTP 服务** → 新增**客户端授权密码**
  （只展示一次，请立即保存）。

写入并生效：

```bash
python3 -c 'import secrets;print("EASYLIFE_SECRET="+secrets.token_urlsafe(48))'
sudo chmod 640 /etc/easylife/easylife.env && sudo chown root:easylife /etc/easylife/easylife.env
sudo systemctl restart easylife
```

### 7.1 SMTP 配置示例（QQ 邮箱 / 腾讯企业邮）、重启与测试邮件

| 服务商 | `SMTP_HOST` | `SMTP_PORT`（两种都支持） | `SMTP_PASS` 填什么 |
| --- | --- | --- | --- |
| QQ 邮箱 | `smtp.qq.com` | `465`（SMTP_SSL）/ `587`（STARTTLS） | `<QQ 邮箱授权码占位符，16 位，不是邮箱登录密码>` |
| 腾讯企业邮 | `smtp.exmail.qq.com` | `465`（SMTP_SSL）/ `587`（STARTTLS） | `<企业邮客户端专用密码 / 授权码占位符>` |

```ini
# /etc/easylife/easylife.env —— 腾讯企业邮示例（换成 QQ 邮箱只改前两行）
SMTP_HOST=smtp.exmail.qq.com
SMTP_PORT=465
SMTP_USER=<你的完整邮箱地址占位符，如 you@example.com>
SMTP_PASS=<邮箱授权码占位符，不是邮箱登录密码>
SMTP_FROM=EasyLife <you@example.com>
EASYLIFE_PUBLIC_URL=https://mch.wang/todos
EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true
```

> 端口二选一：`465` 走隐式 TLS（SMTP_SSL），`587` 先连明文再升级 STARTTLS，应用自动选择，不用改代码。
> `SMTP_PASS` **只能填授权码 / 客户端专用密码**，且文档与仓库里**一律只写占位符**；
> 真实值只写在服务器的 `/etc/easylife/easylife.env`（`640 root:easylife`），不入库、不提交、不外传。

**改完必须重启服务**（`EnvironmentFile` 只在进程启动时读取，不重启不生效）：

```bash
sudo chmod 640 /etc/easylife/easylife.env && sudo chown root:easylife /etc/easylife/easylife.env
sudo systemctl restart easylife
systemctl is-active easylife             # -> active
journalctl -u easylife -n 20 --no-pager  # 启动行会打印 smtp=<host>:<port>
```

**怎样发一封测试邮件**（复用已有的「提醒发信」自检方式，不新增任何脚本）：

```bash
# 1) 新开一个终端盯发信日志
journalctl -u easylife -f | grep -i -E '\[reminder\]|认证邮件'

# 2) 触发一封真实提醒邮件：把某条待办的「提醒时间」设成 1 分钟后
#    （网页任务详情里改；想快点看到可把 EASYLIFE_REMIND_INTERVAL 临时设成 10 再 restart）
#    后台线程扫描到后打印 [reminder] sent to … subject=…，收到邮件即 SMTP 配置成功。
#    若打印 [reminder] (SMTP disabled) would send …，说明 SMTP_HOST 还是空的（日志模式）。

# 3) 顺带验证认证邮件通道：注册一个新账号，或在登录页点「忘记密码」提交自己的邮箱；
#    收到「验证邮箱 / 重置密码」邮件即说明邮箱验证与找回密码也走通了。
# 4) 没有 SMTP 时的回退（预期行为）：邮件不发出，正文与一次性链接写进服务器日志：
journalctl -u easylife -n 50 --no-pager | grep -A8 '认证邮件'
```

> `EnvironmentFile` 里**只能写 `KEY=VALUE`**，值不要加引号，**行尾不能写注释**（注释单独占一行）。
> 切勿把该文件提交进 git 或贴进聊天工具。

关于 `EASYLIFE_SECRET` 与日历订阅地址的关系（避免误解）：

* `EASYLIFE_SECRET` 只用于派生会话的 CSRF token；**换掉它并不会使日历订阅 URL 失效**。
* 订阅 token 是存在数据库 `users.calendar_token` 列里的**每用户独立随机值**。要轮换订阅地址，
  请在网页的「日历订阅」面板点「重置订阅地址」（或 `POST /api/calendar/token/reset`）；
  怀疑批量泄露时可执行 `sqlite3 /var/lib/easylife/easylife.db "UPDATE users SET calendar_token = NULL;"`，
  所有旧地址立即失效、用户下次打开页面自动生成新地址。

## 8. 验证

```bash
systemctl status easylife --no-pager
systemctl is-enabled easylife                  # -> enabled
journalctl -u easylife -n 50 --no-pager

ss -lntp | grep 8086                           # 关键：只应出现 127.0.0.1:8086，不应出现 0.0.0.0 / [::]

curl -fsS http://127.0.0.1:8086/ >/dev/null && echo "backend OK"      # 直连后端

# 路径模式（默认）
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos/          # -> 200
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos          # -> 301（跳到 /todos/）
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/todos/api/me    # 未登录 -> 401
curl -sS -o /dev/null -w '%{http_code}\n' http://mch.wang/todos/           # 200 或 301 到 https
curl -sS -o /dev/null -w '%{http_code}\n' https://mch.wang/              # 现有站点照旧
grep -rn 'managed-by-easylife-deploy' /etc/nginx                      # 每个 mch.wang server 块一行
cat /etc/easylife/nginx-path.manifest                                 # 改了哪些文件、备份在哪

# 子域模式
curl -fsS https://easylife.mch.wang/ | head -n 5
curl -sS https://easylife.mch.wang/api/me -o /dev/null -w '%{http_code}\n'   # 未登录 -> 401

sudo nginx -t

journalctl -u easylife -f | grep -i remind     # 把某待办提醒时间设成 1 分钟后，观察发送
```

## 9. 备份与定时任务

```bash
sudo bash /opt/easylife/deploy/backup.sh                  # 备份到 /var/backups/easylife
sudo bash /opt/easylife/deploy/backup.sh --keep 30 --gzip
```

加入 crontab（`crontab -e`，示例为每天 03:17）：

```cron
17 3 * * * /usr/bin/bash /opt/easylife/deploy/backup.sh --keep 30 >/dev/null 2>&1
```

备份用 Python 标准库 `sqlite3` 在线备份 API（WAL 下也安全、无需停服），完成后做
`PRAGMA integrity_check`，非 `ok` 即删除该文件并非 0 退出；文件权限 `600`。
恢复：`systemctl stop easylife` → 用备份覆盖 `/var/lib/easylife/easylife.db`
（同时删掉残留的 `-wal` / `-shm`）→ `systemctl start easylife`。

## 10. 回滚

### 10.1 一键回滚（只撤销本脚本做过的事）

```bash
sudo bash /opt/easylife/deploy/deploy.sh --rollback
```

按顺序：

1. **路径模式**：找出带 `# managed-by-easylife-deploy` 标记的 include 行（`nginx-path.manifest` 里记录的文件
   + 全目录 `grep` 兜底），删掉这些行；删完与部署前备份一致时**直接用备份覆盖**（恢复到部署前的逐字节状态），
   若该文件部署后另有人工改动，则只删 include 行、保留那些改动并打印备份位置。再删除
   `/etc/nginx/easylife/todos-location.conf`。**没有标记的文件、其它站点一律不碰。**
2. `nginx -t` 通过才 reload；**失败则撤销本次回滚**（配置恢复为回滚前状态、服务不停），返回非 0。
3. 停用服务：`systemctl disable --now easylife`，删除 systemd 单元。
4. **子域模式**：删除本脚本新增的 `/etc/nginx/conf.d/easylife.conf`（不存在则跳过）。

**保留**代码 `/opt/easylife`、数据 `/var/lib/easylife`、密钥 `/etc/easylife/easylife.env` 和 nginx 备份
`/var/backups/easylife/nginx/`。

### 10.2 手工回滚（等价步骤）

```bash
# 1) 路径模式：删掉 include 行（或用 /var/backups/easylife/nginx/ 下的备份覆盖原文件）
sudo grep -rln 'managed-by-easylife-deploy' /etc/nginx \
    | xargs -r sudo sed -i '/managed-by-easylife-deploy$/d'
sudo rm -rf /etc/nginx/easylife
#    子域模式：删掉新增的 server 块
sudo rm -f /etc/nginx/conf.d/easylife.conf /etc/nginx/sites-enabled/easylife.conf

# 2) 语法检查通过才 reload（失败就先排查，别 reload）
sudo nginx -t && sudo systemctl reload nginx

# 3) 停服并取消开机自启
sudo systemctl disable --now easylife
sudo rm -f /etc/systemd/system/easylife.service
sudo systemctl daemon-reload

# 4) 确认端口已释放、服务已消失
ss -lntp | grep 8086 || echo "8086 已释放"
```

> 数据与密钥默认保留；确认不需要时再手工删除 `/var/lib/easylife`、`/etc/easylife`、`/opt/easylife`、
> `/opt/easylife-src`。子域模式的证书需撤销时：`sudo certbot delete --cert-name easylife.mch.wang`。

## 11. 常见问题

| 现象 | 处理 |
| --- | --- |
| `端口 8086 已被占用` | 换端口重跑：`sudo bash deploy/deploy.sh --port 8090`（同步改 env 的 `EASYLIFE_PORT` 与 nginx 片段的 `proxy_pass`） |
| `找不到 server_name 含 mch.wang 的 server 块` | 现有站点不在本机 nginx 上或域名写法不同：用 `--site-domain` 指定，或改用 `--mode subdomain`，或按 §6.1 手工接入 |
| `已经存在 /todos 的 location，拒绝覆盖` | 现有站点已占用 `/todos`：确认用途后手工删除/改名再重跑，或改用子域模式 |
| `无法安全地自动插入 include` | server 块写在一行里（`server { ... }`）：按提示手工加一行 include |
| `nginx -t` 失败 | 脚本已把备份逐字节写回、删除新片段且**未** reload；按报错修正后重跑，现有站点不受影响 |
| 服务起不来 / `EASYLIFE_SECRET` 报错 | `systemctl status easylife --no-pager` 看状态，`journalctl -u easylife -n 50 --no-pager` 看日志；确认 `/etc/easylife/easylife.env` 中有 ≥16 字符的 `EASYLIFE_SECRET` |
| 502 Bad Gateway | 后端没在听：`systemctl status easylife --no-pager` + `journalctl -u easylife -n 50 --no-pager`；再 `ss -lntp \| grep 8086` 确认后端在听、端口和片段里 `proxy_pass` 一致。详见第 0.2 节 |
| 404 Not Found（`/todos/`） | include 没落在**承载该域名的 443 server 块**里：`grep -rn 'managed-by-easylife-deploy' /etc/nginx`，用 `sudo nginx -T \| grep -n -B2 -A6 'server_name mch.wang'` 核对 https 的 server 块；补上后 `sudo nginx -t && sudo systemctl reload nginx`。详见第 0.2 节 |
| `/todos/` 页面能开但样式/图标丢失、按钮无反应 | 用**带尾斜杠**的 `https://mch.wang/todos/` 访问；确认片段是 `location ^~ /todos/`、`proxy_pass` 末尾带 `/`；env 里 `EASYLIFE_BASE_PATH=/todos`。详见第 0.2 节 |
| 提醒没发出去 | 确认 `SMTP_HOST` 非空、`SMTP_PASS` 是**授权码**；`journalctl -u easylife -f` 观察发送结果 |
| 证书没签下来（子域模式） | 确认 DNS 已解析到本机、80/443 未被防火墙拦截；`sudo certbot --nginx -d <域名>` 单独重试 |
| 想换模式 | 先 `--rollback`，再用另一种 `--mode` 部署；env 里的密钥与数据都会保留 |
| 部署前想先确认一遍 | `bash deploy/deploy-remote.sh <host> --check`：只读预检（SSH、`nginx -t`、8086 空闲、443 server 块），打印 PASS/FAIL 汇总，全程不改任何东西 |
| 自检有 FAIL | 按输出的排查提示看 `systemctl status easylife` / `journalctl -u easylife -n 50` / `nginx -t`；要回到部署前状态用 `bash deploy/deploy-remote.sh <host> --rollback` |
| 设置了 `SSHPASS` 但没装 `sshpass` | 脚本会提示并自动退回密钥/交互式输入；密码不会被写入任何文件或日志 |

## 12. 界面改版（新界面说明）

本次界面重做后前端仍是原生 HTML / CSS / JS（无第三方依赖、无外链 CDN），重做与新增的部分如下：

| 区域 | 新界面行为 |
| --- | --- |
| 主题 | 浅色 / 深色主题可手动切换并写入浏览器本地存储（刷新后保持）；未手动选择时跟随系统 `prefers-color-scheme` |
| 登录 / 注册 | 卡片式表单；注册成功后进入「查收邮件」引导页，页面上有「重新发送验证邮件」按钮与 **60 秒倒计时** |
| 邮箱验证页 | `#/verify?token=...` 自动提交令牌，给出「成功 / 已过期 / 已使用 / 无效」的明确提示 |
| 找回密码页 | 登录页「忘记密码」入口 → `#/forgot` 提交邮箱（统一提示文案）→ 邮件里的 `#/reset?token=...` 设置新密码 |
| 未验证提示条 | 页面顶部的 `verification-banner` 提示条（**部署后自检的第 4 项就是用这个标识判断「新版已上线」**） |
| 任务视图 | 列表筛选（状态 / 关键字）、排序、分页；详情抽屉含「进展笔记」时间线；操作反馈用 toast 提示 |
| 移动端 | 375px 起自适应；无横向溢出，可点区域不小于 40px |

* 用户内容一律用 `textContent` 渲染（防 XSS）；静态资源全部用 `./static/...` 相对路径，
  因此在 `https://mch.wang/todos/` 子路径下也能正确加载（见第 6.1 节）。
* **部署后如何确认新版已生效**（与自检第 4 项同一判据）：

```bash
curl -sS https://mch.wang/todos/ | grep -c verification-banner     # >= 1 即新版页面
curl -sS https://mch.wang/todos/static/app.js | wc -c              # 43976（新版 app.js）
curl -sS https://mch.wang/todos/static/app.js | sha256sum          # 1a4f237e…522afc（与仓库文件一致）
```

## 13. 邮箱验证 / 找回密码（使用流程、有效期与限制）

### 13.1 使用流程（页面与接口一一对应）

| 步骤 | 页面上怎么做 | 后端接口 | 成功表现 |
| --- | --- | --- | --- |
| 注册 | 登录页「注册」→ 填邮箱 + 密码 | `POST /api/register` | `201`，进入「查收邮件」页，邮件含 `https://mch.wang/todos/#/verify?token=...` |
| 验证邮箱 | 点邮件里的按钮（打开 `#/verify?token=...`），或在引导页点「重新发送」 | `POST /api/verify-email`（`token`）/ `POST /api/resend-verification`（登录态或 `email`） | `200`，之后再登录即 `200`；验证状态可从 `GET /api/me` 的 `user.email_verified` 读出 |
| 找回密码 | 登录页「忘记密码」→ 填邮箱 → 点邮件里的链接设新密码 | `POST /api/forgot-password`（`email`）→ `POST /api/reset-password`（`token`、`password`） | 两次都 `200`；改完后用**新密码**登录成功、**旧密码 401** |
| 登出 | 右上角「退出」 | `POST /api/logout` | `200`，之后 `GET /api/me` → `401` |

* 链接前缀由 `EASYLIFE_PUBLIC_URL=https://mch.wang/todos` 决定（默认空时沿用 `PUBLIC_URL` / `BASE_URL` 并保留
  `EASYLIFE_BASE_PATH`）；**链接里一定带 `/todos`**，验证链接形如
  `https://mch.wang/todos/#/verify?token=...`，重置链接形如 `https://mch.wang/todos/#/reset?token=...`。
* 这些接口共用 JSON / 请求体限制；**携带有效登录会话时必须提供该会话的 `X-CSRF-Token`**，
  未登录时凭一次性令牌即可完成验证与重置。

### 13.2 链接有效期、单次使用与令牌存储

| 项目 | 取值 | 说明 |
| --- | --- | --- |
| 邮箱验证令牌 | **24 小时** | `auth.VERIFY_TOKEN_TTL = 24 * 3600` |
| 重置密码令牌 | **1 小时** | `auth.RESET_TOKEN_TTL = 3600` |
| 使用次数 | **一次性** | 用过即失效；同一个令牌再用返回 `400 invalid_token` |
| 存储方式 | **只存 SHA-256** | 数据库 `auth_tokens` 表只有 `token_hash` 列，**没有明文令牌** |
| 重新发送 | 作废旧链接 | 同一用户同类邮件的新令牌会废弃旧链接，只有最新一封可用 |
| 过期 / 伪造 / 用途混用 | 一律 `400 invalid_token` | 用验证令牌去重置（或反之）同样被拒 |

### 13.3 频率限制

| 限制 | 阈值 | 超限表现 |
| --- | --- | --- |
| 同一邮箱、同一类邮件 | 60 秒内 1 次（`MAIL_INTERVAL = 60`） | `429` + `Retry-After: 60`（页面按 60 秒倒计时） |
| 同一 IP 的认证邮件请求 | 每小时 20 次 | `429` |
| 实现方式 | 进程内滑动窗口限流器 | 计数在应用重启后清空（与既有登录限速同一套实现） |

### 13.4 防枚举（不泄露「邮箱是否注册」）

* `POST /api/forgot-password` 对**邮箱已注册 / 未注册 / 投递失败**三种情况返回**完全相同的 `200` 与同一段文案**
  （`RESET_MAIL_MESSAGE`，实测 body 逐字节相同，且**不下发任何 `Set-Cookie`**）。
* `POST /api/resend-verification` 同理，只回「如果该邮箱需要验证，我们已尝试发送验证邮件…」。
* 限流计数**先按提交的地址累计、再查库**，因此 `429` 也不会暴露该地址是否存在。

### 13.5 重置成功后：所有旧会话立即失效

`POST /api/reset-password` 成功后会**删除该用户的全部会话**（`auth.delete_session`）：旧密码登录 `401`、
新密码登录 `200`、浏览器里其它已登录标签页下一次请求即 `401`（需重新登录）。
（重置 → 旧密码 401 / 旧会话失效 / 新密码 200 / 复用同一 token → `400 invalid_token`。）

### 13.6 老用户自动视为已验证（升级不打扰既有用户）

应用启动时会自动迁移已有数据库（示例：线上本次 `user_version` 2 → 3，由**应用启动时**完成，
部署脚本不动数据库）：迁移会把**已有账号**回填为「已验证」，因此老用户升级后照常登录、不需要重新验证；
只有**迁移之后新注册**的账号受验证闸门约束。

### 13.7 开关 `EASYLIFE_REQUIRE_EMAIL_VERIFICATION` 与无 SMTP 时的日志回退

```ini
# /etc/easylife/easylife.env
EASYLIFE_PUBLIC_URL=https://mch.wang/todos
EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true    # 默认 true；false = 不要求先验证邮箱
```

| 配置 | 未验证邮箱的用户能否登录 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` 已配置（如 `smtp.exmail.qq.com`）+ 开关 `true` | **不能** | 登录返回 `403 / email_not_verified`，页面给「重发验证邮件」入口 |
| 开关 `false` | 能 | 关闭登录验证要求，但**注册仍会发送验证邮件** |
| **`SMTP_HOST` 留空（日志回退）** | 能 | 邮件发不出去，所以**自动不强制验证**：启动即打印 `WARNING … SMTP disabled: 认证邮件只写服务器日志，未验证邮箱不会阻止登录。`，并把**邮件正文（含一次性链接）写进服务器日志**，用户可从日志里取出链接完成验证/重置 |

* 日志里的链接等同密钥：`journalctl` 输出应按密钥保护，不要公开或贴进工单。
* **SMTP 已配置但投递失败**（例如授权码写错）时**仍然要求验证**（不会因为一次发信失败就放行），
  服务器记录错误，用户可在 60 秒后重发。
* 回退模式只是「先跑通」的权宜之计；正式对外请配好 SMTP（见第 7.1 节），否则用户注册后收不到验证邮件。
* 改完开关或 SMTP 后都要 `sudo systemctl restart easylife` 才生效。

## 14. 管理员邮件设置与上线确认

### 14.1 网页「邮件设置」显示条件：先把管理员 ID 写入 `EASYLIFE_ADMIN_USER_IDS` 才会显示（fail-closed）

网页上的「设置 → 邮件发送」入口（`#nav-mail-settings`）与 `#mail-settings` 面板**只在
`user.is_admin === true` 时显示**，而管理员名单的唯一来源是服务器 `/etc/easylife/easylife.env` 里的
`EASYLIFE_ADMIN_USER_IDS`：

- 取值是**已存在账号的 `users.id`**（正整数，逗号或空格分隔）；**默认留空 = 无人是管理员**。
- **fail-closed**：非法条目、`users` 表中不存在的 ID 一律**不授权**（只记 WARNING，名单在应用启动时冻结）；
  邮箱永远不参与提权（新注册账号不会自动获得权限）。
- 非管理员：入口不显示，前端也不会发起邮件设置请求；直接调接口是 `403 forbidden`（匿名 `401`）。
- **改完必须 `sudo systemctl restart easylife`** 才生效；管理员重新登录（或刷新页面）后入口才会出现。

```bash
# 1) 在服务器上查出要授权的用户 ID（没有 sqlite3 命令时用第二条）
sqlite3 /var/lib/easylife/easylife.db "SELECT id, email FROM users;"
python3 -c "import sqlite3;print(*sqlite3.connect('/var/lib/easylife/easylife.db').execute('SELECT id,email FROM users'))"
```

```ini
# 2) 编辑 /etc/easylife/easylife.env（示例值为占位符；多个 ID 用逗号分隔）
EASYLIFE_ADMIN_USER_IDS=<你的用户 ID 占位符，如 1>
```

```bash
# 3) 修改权限并重启，使名单生效
sudo chmod 640 /etc/easylife/easylife.env && sudo chown root:easylife /etc/easylife/easylife.env
sudo systemctl restart easylife
```

补充：网页表单保存的 SMTP 设置（含密码）存在数据库 `site_settings` 表，**优先于 env 里的 `SMTP_*`**，
且不会写回 env；授权码**永不回填**（只显示「已设置」状态），密码框留空 = 保留原值。更多说明见
`deploy/easylife.env.example` 的「管理员（网页邮件设置）」一段。

### 14.2 SMTP 配置与「改完必须重启」——对照 §7.1

服务商、端口与授权码的填法**以 §7.1 的对照表为准**；补充三点提醒：

- **QQ 邮箱**：`SMTP_HOST=smtp.qq.com`；**腾讯企业邮**：`SMTP_HOST=smtp.exmail.qq.com`。
  端口 `465`（SMTP_SSL，隐式 TLS）或 `587`（STARTTLS）任选一种，应用自动识别。
- `SMTP_PASS` 只能填**授权码 / 客户端专用密码**（不是邮箱登录密码）；本文档与仓库**一律只写占位符**，
  真实值只写在服务器的 `/etc/easylife/easylife.env`（`640 root:easylife`），不入库、不提交、不外传。
- **改完 env 必须重启**（`EnvironmentFile` 只在进程启动时读取）：`sudo systemctl restart easylife`，
  再用 `systemctl is-active easylife`、`journalctl -u easylife -n 20 --no-pager` 确认；验证发信可在网页
  「邮件发送」里点「测试发信」（使用已保存配置），或按 §7.1 的方式用提醒邮件自检。

```ini
# /etc/easylife/easylife.env —— 与 §7.1 示例同款（值均为占位符）
SMTP_HOST=smtp.exmail.qq.com
SMTP_PORT=465
SMTP_USER=<你的完整邮箱地址占位符>
SMTP_PASS=<邮箱授权码占位符，不是邮箱登录密码>
EASYLIFE_PUBLIC_URL=https://mch.wang/todos
EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true
```

### 14.3 「新版是否已上线」判定：curl + sha256 与仓库 `easylife/static` 对比

在**仓库根目录**执行；三个文件全部「线上 = 仓库」即新版已上线：

```bash
for f in index.html style.css app.js; do
  live=$(curl -fsS "https://mch.wang/todos/static/$f" | sha256sum | cut -d' ' -f1)
  repo=$(sha256sum "easylife/static/$f" | cut -d' ' -f1)
  if [ "$live" = "$repo" ]; then
    echo "OK   $f  线上=仓库（新版已上线）：$live"
  else
    echo "DIFF $f  线上=$live  仓库=$repo（新版未上线）"
  fi
done
```

- **判据**：三行全部 `OK … 线上=仓库（新版已上线）` ⇒ 已上线；出现任何一行 `DIFF` ⇒ 尚未上线。
