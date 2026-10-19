#!/usr/bin/env bash
# =============================================================================
#  EasyLife 部署脚本（幂等 / 可反复执行）
#
#  两种模式：
#   --mode path（默认，推荐）：挂到 mch.wang 现有站点的 /todos 路径下
#       sudo bash deploy/deploy.sh                      # 等价于 --mode path
#     1. 用 nginx -T（兜底 grep）找到 server_name 含 mch.wang 的 server 块（80/443 都处理）；
#        找不到就退出并提示改用 --mode subdomain；已有 location /todos 则拒绝覆盖
#     2. ss 检查端口；装用户/代码/env（EASYLIFE_BASE_PATH=/todos、PUBLIC_URL=https://mch.wang/todos）/单元
#     3. 渲染 nginx-easylife-location.conf -> /etc/nginx/easylife/todos-location.conf
#     4. 先备份带时间戳的副本，再在每个匹配的 server 块里插入一行 include（已插入则跳过，幂等）
#     5. nginx -t 通过才 reload；失败逐字节恢复备份并以非 0 退出
#
#   --mode subdomain：原有的独立子域方案（下面 1-8 步）
#     sudo bash deploy/deploy.sh --mode subdomain --domain easylife.mch.wang \
#          --certbot --certbot-email you@example.com
#
#  子域模式会依次：
#     1. 检查端口是否已被占用（被占用则退出并提示改端口）
#     2. 检查 python3 版本与必需的标准库模块
#     3. 创建系统用户 easylife 与目录 /opt/easylife、/var/lib/easylife、/etc/easylife
#     4. 复制代码到 /opt/easylife
#     5. 首次生成 /etc/easylife/easylife.env（已存在则保持不动，绝不覆盖密钥）
#     6. 仅当 /etc/nginx/conf.d/easylife.conf 不存在时写入（绝不覆盖既有 nginx 配置），
#        `nginx -t` 通过才 reload；失败自动删除刚写入的新文件并退出（零影响回滚）
#     7. 可选 certbot --nginx -d <域名>（只针对新域名）
#     8. systemctl enable --now easylife
#
#  其它用法：
#     sudo bash deploy/deploy.sh --port 8090 --no-nginx      # 只装服务
#     sudo bash deploy/deploy.sh --rollback                  # 撤销服务配置（单元/include/片段；不还原旧代码）
#     sudo bash deploy/deploy.sh --rollback --restore-db     # 撤销服务配置 + 显式把数据库换回部署前快照
#     sudo bash deploy/deploy.sh --no-backup                 # 跳过部署前的数据库自动备份（默认会先备份）
#     sudo bash deploy/deploy.sh --help
#
#  升级时的数据安全（幂等，详见 docs/DEPLOY.md §11）：
#     * $ENV_FILE 已存在时**逐字节保留**，只把缺失的新配置项（EASYLIFE_PUBLIC_URL、
#       EASYLIFE_REQUIRE_EMAIL_VERIFICATION、EASYLIFE_ADMIN_USER_IDS）追加进去，绝不改写/覆盖已有取值；
#     * 数据库（$DATA_DIR 下的 SQLite，含网页端保存的邮件设置）从不由本脚本写入或删除；
#     * 任何改动之前先调用 deploy/backup.sh 做一份一致性快照，备份失败即中止部署；
#     * --rollback 只撤销「服务配置」（nginx include/片段、systemd 单元、停服），**不会把代码
#       回退到旧版本**；数据与密钥保留。数据库默认也不动，只有显式 --rollback --restore-db
#       才会把它换成部署前的快照。
#
#  测试用：下列路径可用环境变量覆盖（生产环境不要设置）：
#     EASYLIFE_APP_DIR EASYLIFE_DATA_DIR EASYLIFE_CONF_DIR EASYLIFE_UNIT_FILE
#     EASYLIFE_NGINX_DIR EASYLIFE_BACKUP_DIR EASYLIFE_DB_BACKUP_DIR
# =============================================================================
set -Eeuo pipefail

PROG="$(basename "$0")"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SELF_DIR/.." && pwd)"

# ---------------------------------------------------------------- 可配置项
SERVICE_USER="easylife"
SERVICE_NAME="easylife"
APP_DIR="${EASYLIFE_APP_DIR:-/opt/easylife}"
DATA_DIR="${EASYLIFE_DATA_DIR:-/var/lib/easylife}"
CONF_DIR="${EASYLIFE_CONF_DIR:-/etc/easylife}"
ENV_FILE="$CONF_DIR/easylife.env"
UNIT_FILE="${EASYLIFE_UNIT_FILE:-/etc/systemd/system/easylife.service}"
NGINX_DIR="${EASYLIFE_NGINX_DIR:-/etc/nginx}"
MODE="path"             # path（默认）= mch.wang/todos；subdomain = 独立子域 server 块
MODE_FROM_ARG=""
DOMAIN=""               # 子域模式默认 easylife.mch.wang；路径模式默认 = SITE_DOMAIN
SITE_DOMAIN="mch.wang"  # 路径模式：要挂载到的现有站点的 server_name
PORT="8086"
LISTEN_WAIT="${EASYLIFE_LISTEN_WAIT:-15}"   # 启动/重启后等待端口开始监听的秒数
case "$LISTEN_WAIT" in ''|*[!0-9]*|0) LISTEN_WAIT=15 ;; esac
BASE_URL=""
BASE_PATH=""            # EASYLIFE_BASE_PATH；空 = 根路径（子域方案），路径方案填 /todos
# ---- 路径模式（--mode path）专用 ----
PATH_SNIPPET_DIR="$NGINX_DIR/easylife"                  # 不在 conf.d / sites-enabled 下，不会被 http 上下文误 include
PATH_SNIPPET="$PATH_SNIPPET_DIR/todos-location.conf"
INCLUDE_MARKER="# managed-by-easylife-deploy"          # include 行的尾注释，回滚只认这个标记
BACKUP_DIR="${EASYLIFE_BACKUP_DIR:-/var/backups/easylife/nginx}"   # 备份放在 nginx 目录之外，避免 sites-enabled/* 把副本也加载进去
PATH_MANIFEST="$CONF_DIR/nginx-path.manifest"           # 每行：<被修改的文件>\t<部署前的备份>
PATH_TARGETS=()        # 预检得到的 "文件:插入行号:缩进" 列表
PATH_CHANGED=()        # 本次运行改动过的文件（失败时据此恢复）
PATH_CHANGED_BAK=()    # 与 PATH_CHANGED 一一对应的备份
SNIPPET_PREV=""        # 片段文件改动前的内容副本（空 = 本次新建）
SNIPPET_TOUCHED=0
SMTP_HOST_ARG=""; SMTP_PORT_ARG=""; SMTP_USER_ARG=""; SMTP_FROM_ARG=""
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt/live}"   # 证书目录（与 certbot 默认一致）
RUN_NGINX=1
RUN_CERTBOT=0
CERTBOT_EMAIL=""
DO_ROLLBACK=0
DO_BACKUP=1            # 升级/部署前自动用 deploy/backup.sh 备份数据库（--no-backup 关闭）
DO_RESTORE_DB=0        # --rollback --restore-db：用升级前的备份恢复数据库（默认只打印恢复方法）
DB_BACKUP_DIR="${EASYLIFE_DB_BACKUP_DIR:-}"   # 数据库备份目录；空 = 用 backup.sh 的默认值
DB_BACKUP_PATH=""      # 本次部署前的数据库备份文件（backup_database 填充，供回滚提示用）
DB_BACKUP_STATE="$CONF_DIR/db-backup.last"    # 记录 "<数据库路径>\t<升级前备份>"，供 --restore-db 使用
PY_BIN="/usr/bin/python3"
NGINX_TARGET=""
CREATED_NGINX=0   # 1 = 本次运行新建了 nginx 配置文件（回滚时删除它）
CREATED_NGINX_LINK=""  # 本次运行新建的 sites-enabled 软链接（已存在的不算）
SUBDOMAIN_MANIFEST="$CONF_DIR/nginx-subdomain.manifest"  # 子域模式：本脚本新建的 nginx 文件，每行一个
PORT_FROM_ARG=""       # 命令行显式指定的端口（优先于已存在的环境文件）
BASE_URL_FROM_ARG=""   # 命令行显式指定的 BASE_URL
BASE_PATH_FROM_ARG=""  # 命令行显式指定的 EASYLIFE_BASE_PATH
SVC_SNAPSHOT_TAKEN=0   # 1 = 已在写 env 之前记录了服务现状（nginx -t 失败时据此回滚服务）
ENV_SNAPSHOT=""        # 部署前 env 文件的逐字节副本（空 = 部署前不存在）
OLD_PORT=""            # 部署前 env 里的 EASYLIFE_PORT
SVC_WAS_ACTIVE=0       # 部署前服务是否在运行

# ---------------------------------------------------------------- 日志
log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
info() { printf '\033[36m[i]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<USAGE
$PROG —— EasyLife 部署脚本（幂等，需 root）

用法：
  sudo bash $PROG [选项]

选项：
  --mode path|subdomain   部署模式，默认 path：挂到 https://mch.wang/todos/（只在现有 server 块里加一行 include）；
                          subdomain：原有的独立子域 server 块方案（easylife.mch.wang）
  --site-domain <域名>    路径模式要挂载的现有站点 server_name，默认 mch.wang
  --domain <域名>         子域模式的对外域名，默认 easylife.mch.wang（写入 server_name 与 BASE_URL）
  --port <端口>           应用监听端口，默认 8086；被占用时请改这里（会同步改 env 与 nginx 片段）
  --base-url <URL>        覆盖对外地址 PUBLIC_URL（路径模式默认 https://mch.wang/todos；子域模式 https://<域名>）
  --base-path <路径>      EASYLIFE_BASE_PATH；路径模式固定 /todos，子域模式默认空（根路径）
  --nginx-conf <路径>     子域模式：指定要新增的 nginx 配置文件（默认自动探测 /etc/nginx/conf.d/easylife.conf）
  --certbot               子域模式：安装 nginx 配置后执行 certbot --nginx -d <域名>（仅针对新域名）
  --certbot-email <邮箱>  certbot 注册邮箱（配合 --certbot，需要 --agree-tos）
  --smtp-host <主机>      可选：写入 SMTP_HOST（如 smtp.qq.com）
  --smtp-port <端口>      可选：写入 SMTP_PORT（465=SSL，587=STARTTLS）
  --smtp-user <邮箱>      可选：写入 SMTP_USER
  --smtp-from <发件人>    可选：写入 SMTP_FROM
                          授权码不走命令行：从环境变量 EASYLIFE_SMTP_PASS 读取，未设置且在终端里则交互输入
  --no-nginx              只部署 systemd 服务，不碰 nginx
  --no-backup             跳过「部署前自动备份数据库」（默认会先用 deploy/backup.sh 备份，失败则中止）
  --rollback              回滚服务配置：停服 + 删单元 + 删除 include 行并恢复备份 + 删除本脚本新增的 nginx
                          配置（只动带 "${INCLUDE_MARKER}" 标记的文件）。
                          **不会还原旧代码**（$APP_DIR 保持当前版本）；数据与密钥保留；数据库默认不动。
  --restore-db            只能配合 --rollback：显式把数据库恢复成部署前的快照（默认只打印恢复方法，
                          绝不自动动数据）
  -h, --help              显示本帮助

示例：
  sudo bash $PROG                                  # 路径模式，https://mch.wang/todos/
  sudo bash $PROG --port 8090                      # 8086 被占用时
  sudo bash $PROG --mode subdomain --domain easylife.mch.wang --certbot --certbot-email you@example.com
USAGE
}

# ERR 陷阱：任何未捕获的失败都先把「本次新建的」nginx 配置删掉（既有站点零影响）
on_error() {
    local line="$1" cmd="$2"
    # set -E 会把 ERR 陷阱带进 $(...) 子 shell；那里的失败（如 lsof 无输出）只影响取值，
    # 不能据此删配置/恢复备份——只在主 shell 里处理
    [ "${BASHPID:-$$}" = "$$" ] || return 0
    warn "第 ${line} 行执行失败：${cmd}"
    if [ "$CREATED_NGINX" = "1" ] && [ -n "$NGINX_TARGET" ] && [ -e "$NGINX_TARGET" ]; then
        rm -f "$NGINX_TARGET" ${CREATED_NGINX_LINK:+"$CREATED_NGINX_LINK"} 2>/dev/null || true
        CREATED_NGINX=0; CREATED_NGINX_LINK=""
        warn "已自动回滚：删除本次新建的 $NGINX_TARGET（既有站点配置未被改动）"
    fi
    path_restore_changes
}

# ---------------------------------------------------------------- 辅助函数

# env_get KEY DEFAULT —— 从环境文件读一个值（不存在时用默认值）。用于保证幂等：
# 已存在的环境文件里的 EASYLIFE_PORT / BASE_URL 优先。
env_get() {
    local key="$1" default="$2" value=""
    if [ -f "$ENV_FILE" ]; then
        value="$(awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/,""); print; exit}' "$ENV_FILE" 2>/dev/null || true)"
    fi
    printf '%s' "${value:-$default}"
}

# 监听某端口的进程信息（无输出 = 空闲）。优先 ss，其次 netstat，再 lsof。
listener_on_port() {
    local port="$1" out=""
    if command -v ss >/dev/null 2>&1; then
        out="$(ss -lntp 2>/dev/null | awk -v p=":${port}\$" 'NR>1 && $4 ~ p {print $4" "$6; exit}' || true)"
    fi
    if [ -z "$out" ] && command -v netstat >/dev/null 2>&1; then
        out="$(netstat -lntp 2>/dev/null | awk -v p=":${port}\$" 'NR>2 && $4 ~ p {print $4" "$7; exit}' || true)"
    fi
    if [ -z "$out" ] && command -v lsof >/dev/null 2>&1; then
        out="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" pid="$2; exit}' || true)"
    fi
    printf '%s' "$out"
}

# 该域名是否已出现在其它 nginx 配置里（用于避免 server_name 冲突 / 判断"新域名"）
domain_used_elsewhere() {
    local domain="$1"
    [ -d "$NGINX_DIR" ] || return 1
    grep -RIs --exclude="$(basename "${NGINX_TARGET:-easylife.conf}")" \
         -e "server_name[^;]*[[:space:]]${domain}[[:space:];]" "$NGINX_DIR" 2>/dev/null | head -5
}

# ---------------------------------------------------------------- 步骤 0：前置检查

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "请以 root 运行（例如：sudo bash $PROG --domain <域名>）。当前 UID=$(id -u)。"
    fi
}

check_python() {
    local candidate version major minor
    # EASYLIFE_PYTHON 可指定解释器（例如系统 python3 太旧时用 /opt/easylife-python/bin/python3）
    for candidate in ${EASYLIFE_PYTHON:-} python3 /usr/bin/python3 /usr/local/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY_BIN="$(command -v "$candidate")"
            break
        fi
    done
    [ -n "$PY_BIN" ] && [ -x "$PY_BIN" ] || die "未找到 python3；请先安装：apt install python3（或 dnf install python3）。"

    version="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    major="${version%%.*}"
    minor="${version##*.}"
    if [ "$major" -ne 3 ] || [ "$minor" -lt 8 ]; then
        die "python3 版本过低：$version（需要 >= 3.8）。$PY_BIN"
    fi
    log "python3 版本检查通过：$version（$PY_BIN）"

    if ! "$PY_BIN" -c 'import sqlite3, ssl, smtplib, http.server, secrets' 2>/dev/null; then
        die "$PY_BIN 缺少必需的标准库模块（sqlite3/ssl/smtplib 等）。
    常见原因：只装了 python3-minimal。请安装：apt install python3 python3-sqlite3（或 dnf install python3。）"
    fi
    log "python3 标准库模块检查通过（sqlite3 / ssl / smtplib / http.server / secrets）"
}

check_port() {
    local occupied owned=0
    occupied="$(listener_on_port "$PORT")"
    if [ -z "$occupied" ]; then
        if ! command -v ss >/dev/null 2>&1 && ! command -v netstat >/dev/null 2>&1 \
           && ! command -v lsof >/dev/null 2>&1; then
            warn "未找到 ss/netstat/lsof，无法检查端口占用；请自行确认 ${PORT} 端口空闲（ss -lntp | grep :$PORT）"
            return 0
        fi
        log "端口检查通过：127.0.0.1:$PORT 未被占用"
        return 0
    fi

    # 端口被自己（已有 easylife 服务）占用属于正常情况：本脚本要幂等重跑。
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then owned=1; fi
    case "$occupied" in
        *easylife*) owned=1 ;;
    esac
    if [ "$owned" -eq 1 ]; then
        info "端口 $PORT 已被本服务（$SERVICE_NAME）占用（$occupied），按幂等重跑处理，继续。"
        return 0
    fi

    die "端口 ${PORT} 已被占用：${occupied}
    为避免影响正在运行的服务，脚本不擅自处理。请改用其它端口后重试：
       1) 挑一个空闲端口（例如 8090），确认它没被监听：ss -lntp | grep :8090
       2) 重新执行： sudo bash $PROG --port 8090 --domain <域名>
          （脚本会把 EASYLIFE_PORT=8090 写进 $ENV_FILE，并同步改 nginx 的 proxy_pass）
       3) 若环境文件已存在，请手工把 EASYLIFE_PORT 改成 8090 并重启服务。"
}

# ---------------------------------------------------------------- 步骤 1：用户与目录

create_user_and_dirs() {
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        info "系统用户 $SERVICE_USER 已存在，跳过创建（幂等）。"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" \
            || die "创建系统用户 $SERVICE_USER 失败。"
        log "已创建系统用户 $SERVICE_USER（无登录 shell）。"
    fi

    install -d -m 0755 -o root -g root "$APP_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
    install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
    log "目录就绪：$APP_DIR（代码，只读）、$DATA_DIR（数据，$SERVICE_USER 可写）、$CONF_DIR（配置）"
}

# ---------------------------------------------------------------- 步骤 2：代码

install_code() {
    local pkg_src="$SRC_DIR/easylife"
    [ -d "$pkg_src" ] || die "找不到源码目录 $pkg_src（请在仓库内运行 deploy/deploy.sh）。"

    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '__pycache__/' "$pkg_src/" "$APP_DIR/easylife/"
    else
        rm -rf "${APP_DIR:?}/.easylife.new"
        mkdir -p "$APP_DIR/.easylife.new"
        cp -a "$pkg_src/." "$APP_DIR/.easylife.new/"
        rm -rf "$APP_DIR/easylife"
        mv "$APP_DIR/.easylife.new" "$APP_DIR/easylife"
    fi

    # 文档与部署脚本一并放入安装目录，便于服务器上查阅/回滚（DESIGN.md §8）
    if [ -d "$SRC_DIR/docs" ]; then
        install -d -m 0755 "$APP_DIR/docs"
        cp -a "$SRC_DIR/docs/." "$APP_DIR/docs/"
    fi
    install -d -m 0755 "$APP_DIR/deploy"
    cp -a "$SELF_DIR/." "$APP_DIR/deploy/"

    find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chown -R root:"$SERVICE_USER" "$APP_DIR"
    chmod -R go-w "$APP_DIR"
    chmod 0755 "$APP_DIR"
    log "代码已同步到 $APP_DIR（包目录 $APP_DIR/easylife）"
}

# ---------------------------------------------------------------- 步骤 3：环境文件

gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 48 | tr -d '\n=+/'
    else
        "$PY_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))'
    fi
}

install_env_file() {
    if [ -f "$ENV_FILE" ]; then
        info "$ENV_FILE 已存在，保持不动（不覆盖既有密钥，幂等）。"
        # 以现有配置为准，保证后续步骤（nginx/证书）与实际端口、域名一致
        if [ -z "$PORT_FROM_ARG" ]; then PORT="$(env_get EASYLIFE_PORT "$PORT")"; fi
        if [ -z "$BASE_URL_FROM_ARG" ]; then
            BASE_URL="$(env_get BASE_URL "$BASE_URL")"
            BASE_URL="$(env_get PUBLIC_URL "$BASE_URL")"
        fi
        if [ -z "$BASE_PATH_FROM_ARG" ]; then BASE_PATH="$(env_get EASYLIFE_BASE_PATH "$BASE_PATH")"; fi
        if grep -q 'EASYLIFE_SECRET=<REPLACE_WITH_SECRET_AT_LEAST_16_CHARS>\|EASYLIFE_SECRET=$\|^EASYLIFE_SECRET=$' "$ENV_FILE"; then
            warn "警告：$ENV_FILE 里的 EASYLIFE_SECRET 看起来还是占位符/空值，应用会拒绝启动！"
            warn "     请执行： python3 -c 'import secrets;print(secrets.token_urlsafe(48))' 并把结果填进去。"
        fi
        chown root:"$SERVICE_USER" "$ENV_FILE" 2>/dev/null || true
        chmod 0640 "$ENV_FILE"
        apply_env_overrides
        return 0
    fi

    local template="$SELF_DIR/easylife.env.example"
    [ -f "$template" ] || die "找不到环境变量模板 $template。"

    local secret
    secret="$(gen_secret)"
    [ ${#secret} -ge 16 ] || die "生成 EASYLIFE_SECRET 失败（长度不足）。"

    umask 0077
    sed -e "s|^EASYLIFE_SECRET=.*|EASYLIFE_SECRET=${secret}|" \
        -e "s|^EASYLIFE_PORT=.*|EASYLIFE_PORT=${PORT}|" \
        -e "s|^BASE_URL=.*|BASE_URL=${BASE_URL}|" \
        -e "s|^PUBLIC_URL=.*|PUBLIC_URL=${BASE_URL}|" \
        -e "s|^EASYLIFE_BASE_PATH=.*|EASYLIFE_BASE_PATH=${BASE_PATH}|" \
        "$template" > "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE" 2>/dev/null || warn "无法设置 $ENV_FILE 的属主（请确认以 root 运行）。"
    chmod 0640 "$ENV_FILE"

    # 模板里未替换的占位符提醒（SMTP 等），随机密钥已自动生成，不打印密钥本身
    log "已生成环境文件 $ENV_FILE（权限 0640 root:$SERVICE_USER，已自动生成 48 字节随机 EASYLIFE_SECRET）"
    apply_env_overrides
    if grep -q '^SMTP_USER=<REPLACE' "$ENV_FILE"; then
        # 模板里的 SMTP_HOST=smtp.qq.com 搭配占位账号只会让每次发信都失败；清空 = 日志模式
        set_env_key SMTP_HOST ""
        warn "SMTP 还是占位符：目前处于「只记日志不发信」模式，请按 docs/DEPLOY.md §7 填好 SMTP_* 后再重启服务"
        warn "（或重跑本脚本并带上 --smtp-host/--smtp-user，授权码用环境变量 EASYLIFE_SMTP_PASS 传入）。"
    fi
}

# set_env_key KEY VALUE —— 在环境文件里改写（或追加）一行 KEY=VALUE；值相同则不动。
# 用 awk 而不是 sed，避免值里的 | & / 等字符被当成正则/替换语法。
set_env_key() {
    local key="$1" value="$2" tmp
    if [ -f "$ENV_FILE" ] && grep -qxF -- "${key}=${value}" "$ENV_FILE"; then
        return 0
    fi
    tmp="$(mktemp "$CONF_DIR/.easylife.env.XXXXXX")"
    awk -v k="$key" -v v="$value" '
        BEGIN { done = 0 }
        index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }
    ' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE"      # 写回原文件（保留属主与 0640 权限）
    rm -f "$tmp"
}

# ensure_env_key KEY VALUE —— **只在缺失时**补新配置项（升级时的 env 保留策略）：
#   * 键已存在且非空 -> 一个字节都不改，只提示；
#   * 键存在但值为空（模板里的 `KEY=`）-> 原地补上值；
#   * 键不存在 -> 逐字节保留原内容，在文件末尾追加一行 KEY=VALUE。
# 绝不删除、绝不改写已有取值（EASYLIFE_SECRET、SMTP_*、自定义行都原样保留）。
ensure_env_key() {
    local key="$1" value="$2" cur="" tmp
    if [ -f "$ENV_FILE" ] && awk -F= -v k="$key" '$1 == k { found = 1 } END { exit !found }' "$ENV_FILE"; then
        cur="$(env_get "$key" "")"
        if [ -n "$cur" ]; then
            info "环境文件已有 $key，保持原值不动（升级不覆盖既有配置）"
            return 0
        fi
        if [ -z "$value" ]; then
            info "环境文件已有空的 $key=（即默认值）；保持不动"
            return 0
        fi
        set_env_key "$key" "$value"
        log "已补全 $ENV_FILE 里空的 $key=$value（原为空占位符）"
        return 0
    fi
    tmp="$(mktemp "$CONF_DIR/.easylife.env.XXXXXX")"
    cat -- "$ENV_FILE" > "$tmp"                        # 逐字节保留原有内容
    if [ -s "$tmp" ] && [ -n "$(tail -c 1 -- "$tmp")" ]; then
        printf '\n' >> "$tmp"                          # 原文件末尾没有换行时先补齐
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"                           # 写回原文件（保留属主与 0640 权限）
    rm -f "$tmp"
    log "已在 $ENV_FILE 追加新配置项 $key=$value（原有内容逐字节未改动）"
}

# 命令行显式给出的值、路径模式必需的值、可选 SMTP 设置写进环境文件（不碰 EASYLIFE_SECRET）。
apply_env_overrides() {
    if [ -n "$PORT_FROM_ARG" ]; then set_env_key EASYLIFE_PORT "$PORT"; fi
    if [ "$MODE" = "path" ] || [ -n "$BASE_PATH_FROM_ARG" ]; then
        set_env_key EASYLIFE_BASE_PATH "$BASE_PATH"
    fi
    if [ "$MODE" = "path" ] || [ -n "$BASE_URL_FROM_ARG" ]; then
        set_env_key PUBLIC_URL "$BASE_URL"
    fi

    local changed_smtp=0 pass="${EASYLIFE_SMTP_PASS:-}"
    if [ -n "$SMTP_HOST_ARG" ]; then set_env_key SMTP_HOST "$SMTP_HOST_ARG"; changed_smtp=1; fi
    if [ -n "$SMTP_PORT_ARG" ]; then set_env_key SMTP_PORT "$SMTP_PORT_ARG"; changed_smtp=1; fi
    if [ -n "$SMTP_USER_ARG" ]; then set_env_key SMTP_USER "$SMTP_USER_ARG"; changed_smtp=1; fi
    if [ -n "$SMTP_FROM_ARG" ]; then set_env_key SMTP_FROM "$SMTP_FROM_ARG"; changed_smtp=1; fi
    if [ "$changed_smtp" -eq 1 ]; then
        if [ -z "$pass" ] && [ -t 0 ] && grep -q '^SMTP_PASS=<REPLACE' "$ENV_FILE"; then
            read -r -s -p "请输入 SMTP 授权码（不回显，不会写进日志）：" pass </dev/tty || pass=""
            printf '\n' >&2
        fi
        if [ -n "$pass" ]; then
            set_env_key SMTP_PASS "$pass"
            log "SMTP 已写入 $ENV_FILE（授权码未打印）"
        elif grep -q '^SMTP_PASS=<REPLACE' "$ENV_FILE"; then
            warn "SMTP_PASS 仍是占位符：请设置环境变量 EASYLIFE_SMTP_PASS 后重跑，或手工编辑 $ENV_FILE"
        fi
    fi

    # ---- 新功能需要的配置项：只补「缺失」的，已有取值一律不动（升级时 env 不被覆盖）----
    # EASYLIFE_PUBLIC_URL：认证邮件/重置链接的公开地址（空则回退到 PUBLIC_URL）。
    ensure_env_key EASYLIFE_PUBLIC_URL "$BASE_URL"
    # EASYLIFE_REQUIRE_EMAIL_VERIFICATION：默认 true（配置 SMTP 时要求验证邮箱后才能登录）。
    ensure_env_key EASYLIFE_REQUIRE_EMAIL_VERIFICATION "true"
    # EASYLIFE_ADMIN_USER_IDS：管理员稳定 ID 名单；默认空 = 无人有权限（网页邮件设置入口隐藏）。
    # 只初始化为空行（便于运维填写），绝不覆盖已有名单，也绝不写入任何密码/邮箱匹配名单。
    ensure_env_key EASYLIFE_ADMIN_USER_IDS ""
}

# 写 env 之前调用：记下 env 原文、旧端口、服务是否在运行，供 nginx -t 失败时回滚服务。
snapshot_service_state() {
    ENV_SNAPSHOT=""
    OLD_PORT="$PORT"
    if [ -f "$ENV_FILE" ]; then
        ENV_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/easylife-env-prev.XXXXXX")"   # mktemp 默认 0600（含密钥）
        cat -- "$ENV_FILE" > "$ENV_SNAPSHOT"
        OLD_PORT="$(env_get EASYLIFE_PORT 8086)"
    fi
    SVC_WAS_ACTIVE=0
    systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null && SVC_WAS_ACTIVE=1
    SVC_SNAPSHOT_TAKEN=1
}

# 部署成功后丢弃快照
drop_service_snapshot() {
    if [ -n "$ENV_SNAPSHOT" ]; then rm -f -- "$ENV_SNAPSHOT"; fi
    ENV_SNAPSHOT=""; SVC_SNAPSHOT_TAKEN=0
}

# nginx -t 失败时调用：env 文件逐字节恢复；原来在运行的服务按旧 env 重启并确认旧端口在监听，
# 原来没在运行的停用。之后由调用方 die。
restore_service_state() {
    [ "$SVC_SNAPSHOT_TAKEN" = "1" ] || return 0
    SVC_SNAPSHOT_TAKEN=0
    if [ -n "$ENV_SNAPSHOT" ] && [ -f "$ENV_SNAPSHOT" ]; then
        cat -- "$ENV_SNAPSHOT" > "$ENV_FILE"
        rm -f -- "$ENV_SNAPSHOT"
        warn "已逐字节恢复 $ENV_FILE"
    else
        rm -f -- "$ENV_FILE"
        warn "已删除本次新建的 $ENV_FILE"
    fi
    ENV_SNAPSHOT=""
    systemctl daemon-reload 2>/dev/null || true
    if [ "$SVC_WAS_ACTIVE" = "1" ]; then
        if systemctl restart "$SERVICE_NAME"; then
            local i
            for i in $(seq 1 "$LISTEN_WAIT"); do
                [ -n "$(listener_on_port "$OLD_PORT")" ] && break
                sleep 1
            done
            if [ -n "$(listener_on_port "$OLD_PORT")" ]; then
                warn "服务已按原配置重启，127.0.0.1:${OLD_PORT} 重新在监听"
            else
                warn "服务已按原配置重启，但未确认 127.0.0.1:${OLD_PORT} 在监听，请检查：journalctl -u $SERVICE_NAME -n 50 --no-pager"
            fi
        else
            warn "按原配置重启 $SERVICE_NAME 失败，请检查：journalctl -u $SERVICE_NAME -n 50 --no-pager"
        fi
    else
        systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
        warn "部署前服务未运行，已停止并禁用 $SERVICE_NAME"
    fi
}

# ---------------------------------------------------------------- 步骤 3.5：数据库备份（升级前）
#
# 本脚本从不写数据库：升级只替换 $APP_DIR/easylife 下的代码，$DATA_DIR 里的 SQLite 原样保留。
# 但 schema 迁移在应用启动时执行，失败就可能需要一个可恢复的快照，所以任何实际改动之前
# 先复用 deploy/backup.sh 做一份一致性快照（WAL 安全、带 integrity_check）。备份失败即中止部署。

# 环境文件里的 EASYLIFE_DB，其次 $DATA_DIR/easylife.db
db_path_from_env() {
    env_get EASYLIFE_DB "$DATA_DIR/easylife.db"
}

# 备份目录里最新的一份备份（文件名带时间戳）
db_backup_newest() {
    local dir="${1:-${DB_BACKUP_DIR:-/var/backups/easylife}}"
    ls -1t "$dir"/easylife-*.db "$dir"/easylife-*.db.gz 2>/dev/null | head -n 1 || true
}

# --rollback 用：本次/最近一次部署前的数据库备份（记录文件优先）
db_backup_last_path() {
    local line=""
    if [ -f "$DB_BACKUP_STATE" ]; then
        line="$(tail -n 1 -- "$DB_BACKUP_STATE" 2>/dev/null || true)"
        line="${line#*$'\t'}"
        [ -n "$line" ] && [ -f "$line" ] && { printf '%s' "$line"; return 0; }
    fi
    db_backup_newest
}

backup_database() {
    DB_BACKUP_PATH=""
    if [ "$DO_BACKUP" -eq 0 ]; then
        warn "按 --no-backup 跳过部署前的数据库备份（请自行确认服务器上已有可恢复的备份）。"
        return 0
    fi
    local db
    db="$(db_path_from_env)"
    if [ ! -f "$db" ]; then
        info "数据库还不存在（首次部署）：$db —— 无需备份"
        return 0
    fi
    local script="$SELF_DIR/backup.sh" args out runner
    [ -f "$script" ] || die "找不到备份脚本 $script；升级前必须能备份数据库（确认不需要可用 --no-backup 跳过）。"
    args=(--db "$db")
    [ -n "$DB_BACKUP_DIR" ] && args+=(--dir "$DB_BACKUP_DIR")
    # 把本脚本已校验过的解释器（PY_BIN，>=3.8）传给 backup.sh：
    # 系统自带的 python3 可能太旧（例如 CentOS 7 的 3.6），其 sqlite3 没有在线备份 API。
    runner=(bash "$script")
    if [ -n "${PY_BIN:-}" ]; then
        runner=(env "EASYLIFE_PYTHON=$PY_BIN" bash "$script")
    fi
    if ! out="$( "${runner[@]}" "${args[@]}" 2>&1)"; then
        printf '%s\n' "$out" >&2
        die "部署前数据库备份失败，已中止部署（数据库、$ENV_FILE 与 nginx 均未改动）。
    请先手工备份后重试： sudo bash $APP_DIR/deploy/backup.sh --db $db
    确认不需要备份时： sudo bash $APP_DIR/deploy/deploy.sh --no-backup"
    fi
    printf '%s\n' "$out"
    DB_BACKUP_PATH="$(db_backup_newest "${DB_BACKUP_DIR:-/var/backups/easylife}")"
    if [ -n "$DB_BACKUP_PATH" ]; then
        printf '%s\t%s\n' "$db" "$DB_BACKUP_PATH" > "$DB_BACKUP_STATE"
        chmod 0600 "$DB_BACKUP_STATE" 2>/dev/null || true
        log "升级前的数据库备份：$DB_BACKUP_PATH（数据库本身未被改动）"
        info "如需连数据一起回到升级前： sudo bash $APP_DIR/deploy/deploy.sh --rollback --restore-db"
    else
        warn "未能定位刚生成的备份文件（目录 ${DB_BACKUP_DIR:-/var/backups/easylife}）：--rollback --restore-db 将不可用。"
    fi
}

# --rollback --restore-db：把数据库换回升级前的一致性快照（默认不做，避免丢掉部署后新写入的数据）。
restore_database_from_backup() {
    local db="" bak="" keep
    if [ -f "$DB_BACKUP_STATE" ]; then
        local line; line="$(tail -n 1 -- "$DB_BACKUP_STATE" 2>/dev/null || true)"
        db="${line%%$'\t'*}"
        [ "$db" = "$line" ] && db=""
    fi
    [ -n "$db" ] || db="$(db_path_from_env)"
    bak="$(db_backup_last_path)"
    if [ -z "$bak" ] || [ ! -f "$bak" ]; then
        die "找不到部署前的数据库备份（记录文件 $DB_BACKUP_STATE / 目录 ${DB_BACKUP_DIR:-/var/backups/easylife}）。
    手工恢复：systemctl stop $SERVICE_NAME 之后，把备份覆盖回 $db（见 docs/DEPLOY.md §9）。"
    fi
    log "用部署前的快照恢复数据库：$bak -> $db"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$db")"
    if [ -f "$db" ]; then
        keep="$db.before-restore.$(date +%Y%m%d-%H%M%S)"
        cp -p -- "$db" "$keep" && info "恢复前先保留了当前数据库：$keep"
    fi
    case "$bak" in
        *.gz)
            if command -v gzip >/dev/null 2>&1; then
                gzip -dc -- "$bak" > "$db"
            else
                "$PY_BIN" -c 'import gzip, shutil, sys; shutil.copyfileobj(gzip.open(sys.argv[1], "rb"), open(sys.argv[2], "wb"))' "$bak" "$db"
            fi ;;
        *) cp -p -- "$bak" "$db" ;;
    esac
    chown "$SERVICE_USER":"$SERVICE_USER" "$db" 2>/dev/null || true
    chmod 0600 "$db" 2>/dev/null || true
    log "数据库已恢复为部署前的快照，重新部署即可再走一次迁移： sudo bash $APP_DIR/deploy/deploy.sh"
}

# 失败提示里统一带上「怎么回到部署前」的路径（有数据库快照时连数据一起回）。
rollback_hint() {
    local extra=""
    if [ -n "${DB_BACKUP_PATH:-}" ] || [ -n "$(db_backup_last_path)" ]; then
        extra="
           连数据库一起回滚： sudo bash $APP_DIR/deploy/deploy.sh --rollback --restore-db"
    fi
    printf '回滚： sudo bash %s/deploy/deploy.sh --rollback%s' "$APP_DIR" "$extra"
}

# ---------------------------------------------------------------- 步骤 4：systemd

install_unit() {
    local template="$SELF_DIR/easylife.service"
    [ -f "$template" ] || die "找不到 $template。"

    umask 0022
    # ExecStart 使用本机检测到的 python3 路径；User/WorkingDirectory/EnvironmentFile 等保持不变
    sed -e "s|^ExecStart=/usr/bin/python3 |ExecStart=${PY_BIN} |" "$template" > "$UNIT_FILE"
    chmod 0644 "$UNIT_FILE"
    log "已安装 systemd 单元 $UNIT_FILE（ExecStart=${PY_BIN} -m easylife.app）"
}

# ---------------------------------------------------------------- 步骤 5：nginx

nginx_has_cert() {
    [ -f "$LETSENCRYPT_DIR/$DOMAIN/fullchain.pem" ]
}

detect_nginx_target() {
    [ -n "$NGINX_TARGET" ] && return 0      # 已由 --nginx-conf 明确指定，保持不动
    if [ -d "$NGINX_DIR/conf.d" ]; then
        NGINX_TARGET="$NGINX_DIR/conf.d/easylife.conf"
    elif [ -d "$NGINX_DIR/sites-available" ]; then
        NGINX_TARGET="$NGINX_DIR/sites-available/easylife.conf"
    else
        NGINX_TARGET=""
    fi
}

# 渲染 nginx 配置：替换 server_name / 后端端口 / 证书路径，
# 并按证书是否存在，只保留 HTTP 引导块或 TLS 块之一。
render_nginx_conf() {
    local template="$SELF_DIR/nginx-easylife.conf" keep_block="$1"
    [ -f "$template" ] || die "找不到 nginx 配置模板 $template。"

    sed -e "s|^\( *\)server_name easylife\.mch\.wang;|\1server_name ${DOMAIN};|" \
        -e "s|/etc/letsencrypt/live/easylife\.mch\.wang/|${LETSENCRYPT_DIR}/${DOMAIN}/|g" \
        -e "s|127\.0\.0\.1:8086|127.0.0.1:${PORT}|g" \
        "$template" |
    awk -v keep="$keep_block" '
        $0 == "# >>> EASYLIFE_BLOCK: HTTP" { inblock = "HTTP"; next }
        $0 == "# >>> EASYLIFE_BLOCK: SSL"  { inblock = "SSL";  next }
        $0 == "# <<< EASYLIFE_BLOCK: END"  { inblock = "";     next }
        { if (inblock == "" || inblock == keep) print }
    '
}

nginx_test() {
    nginx -t 2>&1
}

reload_nginx() {
    if systemctl is-active --quiet nginx 2>/dev/null; then
        systemctl reload nginx
    elif systemctl is-active --quiet nginx.service 2>/dev/null; then
        systemctl reload nginx.service
    else
        nginx -s reload
    fi
}

install_nginx() {
    if [ "$RUN_NGINX" -eq 0 ]; then
        info "按 --no-nginx 跳过 nginx 配置。"
        return 0
    fi
    if ! command -v nginx >/dev/null 2>&1; then
        warn "本机未安装 nginx，跳过反向代理配置（应用仍只监听 127.0.0.1:${PORT}，不会暴露到公网）。"
        warn "安装 nginx 后手工执行："
        warn "  sudo cp $SELF_DIR/nginx-easylife.conf /etc/nginx/conf.d/easylife.conf"
        warn "  sudo nginx -t && sudo systemctl reload nginx"
        return 0
    fi

    detect_nginx_target
    [ -n "$NGINX_TARGET" ] || { warn "找不到 /etc/nginx/conf.d 或 sites-available，跳过 nginx 配置。"; return 0; }

    # ---- 绝不覆盖已有配置（哪怕是我们上次装的那份，可能已被 certbot 改写）----
    if [ -e "$NGINX_TARGET" ]; then
        info "$NGINX_TARGET 已存在，拒绝覆盖（幂等）。如需更新，请先手工备份并删除该文件后重跑本脚本。"
        if [ "$PORT" = "$OLD_PORT" ]; then
            if ! nginx_test; then
                restore_service_state
                die "nginx -t 失败：未执行 reload，$ENV_FILE 与服务已恢复为部署前状态。请检查上面的报错后重试。"
            fi
            info "nginx 配置语法检查（nginx -t）通过，跳过 reload。"
            return 0
        fi

        # ---- 端口变了：只改本脚本创建的 conf 里的 proxy_pass 端口 ----
        if ! { [ -f "$SUBDOMAIN_MANIFEST" ] && grep -Fxq -- "$NGINX_TARGET" "$SUBDOMAIN_MANIFEST"; }; then
            restore_service_state
            die "$NGINX_TARGET 不是本脚本创建的，拒绝把端口从 ${OLD_PORT} 改成 ${PORT}；$ENV_FILE 与服务已恢复。
    如确需换端口，请手工把该文件里的 proxy_pass http://127.0.0.1:${OLD_PORT} 改成 ${PORT}，
    nginx -t 通过后 reload，再用 --port ${PORT} 重跑本脚本。"
        fi
        if ! grep -q "^[[:space:]]*proxy_pass[[:space:]]*http://127\.0\.0\.1:${OLD_PORT}\([/;]\)" "$NGINX_TARGET"; then
            restore_service_state
            die "$NGINX_TARGET 里找不到 proxy_pass http://127.0.0.1:${OLD_PORT}，无法自动换端口；$ENV_FILE 与服务已恢复。请手工修改。"
        fi
        local ts bak
        ts="$(date +%Y%m%d-%H%M%S)"
        bak="$(backup_file "$NGINX_TARGET" "$ts")"
        log "已备份 $NGINX_TARGET -> $bak"
        # 只改未注释的 proxy_pass 行
        sed "s|^\([[:space:]]*proxy_pass[[:space:]]*http://127\.0\.0\.1:\)${OLD_PORT}\([/;]\)|\1${PORT}\2|" "$bak" > "$NGINX_TARGET"
        if ! nginx_test; then
            cat -- "$bak" > "$NGINX_TARGET"          # 逐字节恢复（保留原 inode / 权限）
            restore_service_state
            die "nginx -t 失败：已把 $NGINX_TARGET 逐字节恢复（备份 $bak），未执行 reload，
    $ENV_FILE 与服务已恢复为部署前状态（127.0.0.1:${OLD_PORT}）。请检查上面的报错后重试。"
        fi
        log "nginx -t 通过（proxy_pass 端口 ${OLD_PORT} -> ${PORT}）"
        if reload_nginx; then
            log "nginx 已 reload（只改了 $NGINX_TARGET 的 proxy_pass 端口）"
        else
            warn "nginx reload 失败，请手工执行： sudo systemctl reload nginx"
        fi
        return 0
    fi

    if [ -n "$(domain_used_elsewhere "$DOMAIN" || true)" ]; then
        warn "域名 $DOMAIN 已出现在其它 nginx 配置中："
        printf '%s\n' "$(domain_used_elsewhere "$DOMAIN" || true)" | sed 's/^/      /' >&2
        warn "本脚本仍会新增 easylife.conf（不会改动上面那些文件），但请确认这是你要的子域；"
        warn "如果 mch.wang 已被占用，请改用子域 easylife.mch.wang 或路径方案（见 docs/DEPLOY.md §6）。"
    fi

    local keep="HTTP" reason="尚无证书，先安装 HTTP 引导块（80 端口）"
    if nginx_has_cert; then
        keep="SSL"; reason="已存在证书 $LETSENCRYPT_DIR/$DOMAIN/fullchain.pem，安装 TLS 块（443 端口）"
    fi
    log "写入 $NGINX_TARGET（$reason）"

    render_nginx_conf "$keep" > "$NGINX_TARGET"
    chmod 0644 "$NGINX_TARGET"
    CREATED_NGINX=1
    CREATED_NGINX_LINK=""
    local link="$NGINX_DIR/sites-enabled/easylife.conf"
    if [ "$NGINX_TARGET" = "$NGINX_DIR/sites-available/easylife.conf" ] && [ -d "$NGINX_DIR/sites-enabled" ] \
       && [ ! -e "$link" ] && [ ! -L "$link" ]; then
        ln -s "$NGINX_TARGET" "$link"
        CREATED_NGINX_LINK="$link"
    fi

    # ---- 语法检查；失败则删除刚写入的新文件（自动回滚），绝不 reload ----
    if ! nginx_test; then
        rm -f "$NGINX_TARGET" ${CREATED_NGINX_LINK:+"$CREATED_NGINX_LINK"} 2>/dev/null || true
        CREATED_NGINX=0; CREATED_NGINX_LINK=""
        restore_service_state
        die "nginx -t 失败：已自动回滚（删除本次新建的 $NGINX_TARGET），未执行 reload，
    任何既有站点配置都未被改动。请检查上面的报错后重试。"
    fi
    log "nginx -t 通过"

    # 记录归属：--rollback 只删除这里登记过的（本脚本新建的）文件
    printf '%s\n' "$NGINX_TARGET" ${CREATED_NGINX_LINK:+"$CREATED_NGINX_LINK"} >> "$SUBDOMAIN_MANIFEST"
    chmod 0600 "$SUBDOMAIN_MANIFEST" 2>/dev/null || true

    if reload_nginx; then
        log "nginx 已 reload（既有站点配置未做任何修改）"
    else
        warn "nginx reload 失败，请手工执行： sudo systemctl reload nginx"
    fi
}

# ---------------------------------------------------------------- 步骤 5'：nginx（路径模式）
#
# 只做两件事：新增片段文件 $PATH_SNIPPET；在 server_name 含 $SITE_DOMAIN 的现有 server 块里
# 各插入一行 "include $PATH_SNIPPET; $INCLUDE_MARKER"。改动前先备份，nginx -t 失败逐字节恢复。

# nginx 实际加载的配置文件（nginx -T 输出里的 "# configuration file X:" 行，解析成真实路径）；
# nginx -T 拿不到时兜底 grep 整个 nginx 目录。片段目录自身排除在外。
nginx_config_files() {
    local out=""
    out="$(nginx -T 2>/dev/null | sed -n 's/^# configuration file \(.*\):$/\1/p' || true)"
    if [ -z "$out" ]; then
        out="$(grep -RIls -e 'server_name' "$NGINX_DIR" 2>/dev/null || true)"
    fi
    printf '%s\n' "$out" | while IFS= read -r f; do
        [ -n "$f" ] && [ -f "$f" ] || continue
        f="$(readlink -f -- "$f")"
        case "$f" in "$PATH_SNIPPET_DIR"/*) continue ;; esac
        printf '%s\n' "$f"
    done | awk '!seen[$0]++'
}

# scan_server_blocks FILE —— 解析 FILE 里 server_name 含 $SITE_DOMAIN 的 server 块，每块输出一行
# （\037 分隔：它不是空白字符，read 时空字段不会被合并）：
#   <server 起始行号> <起始行结束时是否正好位于 server 内层(1/0)> <是否已有本脚本的 include(1/0)>
#   <冲突的 /todos location 所在行号，0=无> <冲突的 location 原文> <插入 include 用的缩进> <listen 列表>
#   <块内 include 的路径（\036 分隔，不含本脚本的片段）>
# 按字符跟踪 { } ; 的嵌套，注释（# 到行尾）不参与解析。
scan_server_blocks() {
    awk -v domain="$SITE_DOMAIN" -v snippet="$PATH_SNIPPET" -v prefix="$BASE_PATH" '
        function words(s, arr) { gsub(/^[ \t\r]+|[ \t\r]+$/, "", s); return split(s, arr, /[ \t\r]+/) }
        function unq(s) { gsub(/^["\047]|["\047]$/, "", s); return s }
        function enclosing_server(   d) { for (d = depth; d >= 1; d--) if (srv_at[d]) return srv_at[d]; return 0 }
        {
            raw = $0; code = raw; sub(/#.*/, "", code)
            opened = 0
            n = length(code)
            for (i = 1; i <= n; i++) {
                c = substr(code, i, 1)
                if (c == "{") {
                    nw = words(stmt, w); name = (nw > 0) ? w[1] : ""
                    depth++; srv_at[depth] = 0
                    if (name == "server") {
                        ns++; srv_at[depth] = ns; sdepth[ns] = depth; sline[ns] = NR
                        match(raw, /^[ \t]*/); sindent[ns] = substr(raw, 1, RLENGTH) "    "
                        opened = ns
                    } else if (name == "location" && (s = enclosing_server()) && !conflict[s]) {
                        # 跨行写法（"location\n    /todos/ {"）：stmt 已把换行合并成空格，行号取 location 所在行
                        norm = stmt; gsub(/[ \t\r]+/, " ", norm); gsub(/^ | $/, "", norm)
                        if (nw >= 3 && (w[2] == "~" || w[2] == "~*")) {
                            if (index(w[3], prefix) > 0) { conflict[s] = stmt_line; ctext[s] = norm " {" }
                        } else {
                            p = unq((nw >= 3) ? w[3] : w[2])
                            if (index(p, prefix) == 1) { conflict[s] = stmt_line; ctext[s] = norm " {" }
                        }
                    }
                    stmt = ""; stmt_line = 0
                } else if (c == "}") {
                    if (depth > 0) { srv_at[depth] = 0; depth-- }
                    stmt = ""; stmt_line = 0
                } else if (c == ";") {
                    nw = words(stmt, w)
                    if (nw > 0 && depth > 0 && srv_at[depth]) {
                        s = srv_at[depth]
                        if (w[1] == "server_name") { for (k = 2; k <= nw; k++) if (unq(w[k]) == domain) match_[s] = 1 }
                        if (w[1] == "listen") { listens[s] = listens[s] (listens[s] == "" ? "" : ",") w[2] }
                        if (w[1] == "include" && unq(w[2]) == snippet) has_inc[s] = 1
                    }
                    # server 块内（任意层级）的 include：交给 path_preflight 去被引入的文件里查 /todos
                    if (nw >= 2 && w[1] == "include" && (s = enclosing_server()) && unq(w[2]) != snippet)
                        incs[s] = incs[s] (incs[s] == "" ? "" : "\036") unq(w[2])
                    stmt = ""; stmt_line = 0
                } else {
                    if (stmt_line == 0 && c !~ /[ \t\r]/) stmt_line = NR
                    stmt = stmt c
                }
            }
            stmt = stmt " "
            if (opened) ok[opened] = (depth == sdepth[opened]) ? 1 : 0
        }
        END {
            for (s = 1; s <= ns; s++) if (match_[s]) {
                t = ctext[s]; gsub(/^[ \t]+/, "", t)
                printf "%d\037%d\037%d\037%d\037%s\037%s\037%s\037%s\n", sline[s], ok[s] + 0, has_inc[s] + 0, conflict[s] + 0, t, sindent[s], listens[s], incs[s]
            }
        }
    ' "$1"
}

# location_conflicts FILE —— FILE（被 include 进 server 块的片段）里与 $BASE_PATH 冲突的 location，
# 每个输出一行 "<行号>\037<规范化后的 location 语句>"。判定规则与 scan_server_blocks 一致：前缀 / = / ^~
# 以 $BASE_PATH 开头，或正则 location（~ / ~*）里出现 $BASE_PATH。
# 先去掉 # 注释，再把换行与连续空白合并成一个空格、按 ; { } 切分语句，所以 "location\n    /todos/ {"
# 这类跨行写法同样能识别；行号取 location 关键字所在行。
location_conflicts() {
    awk -v prefix="$BASE_PATH" '
        {
            code = $0; sub(/#.*/, "", code)
            n = length(code)
            for (i = 1; i <= n; i++) {
                c = substr(code, i, 1)
                if (c == "{" || c == ";" || c == "}") {
                    if (c == "{") {
                        seg = stmt; gsub(/[ \t\r]+/, " ", seg); gsub(/^ | $/, "", seg)
                        nw = split(seg, w, / /)
                        if (nw >= 2 && w[1] == "location") {
                            hit = 0
                            if (nw >= 3 && (w[2] == "~" || w[2] == "~*")) { if (index(w[3], prefix) > 0) hit = 1 }
                            else { p = (nw >= 3) ? w[3] : w[2]; gsub(/^["\047]|["\047]$/, "", p); if (index(p, prefix) == 1) hit = 1 }
                            if (hit) printf "%d\037%s {\n", sline, seg
                        }
                    }
                    stmt = ""; sline = 0
                } else {
                    if (sline == 0 && c !~ /[ \t\r]/) sline = NR
                    stmt = stmt c
                }
            }
            stmt = stmt " "
        }
    ' "$1"
}

# include_paths FILE —— FILE 里 include 指令的路径（去掉注释与引号），每行一个
include_paths() {
    awk '
        {
            code = $0; sub(/#.*/, "", code)
            n = split(code, parts, /[;{}]/)
            for (i = 1; i <= n; i++) {
                s = parts[i]; gsub(/^[ \t]+|[ \t]+$/, "", s)
                if (s ~ /^include[ \t]+/) { sub(/^include[ \t]+/, "", s); gsub(/^["\047]|["\047]$/, "", s); print s }
            }
        }
    ' "$1"
}

# included_conflicts PATTERN DEPTH —— 展开 include 路径（相对路径以 $NGINX_DIR 为基准，支持通配符），
# 在被引入的文件里找 /todos 冲突，并递归处理其中的 include（最多 5 层）。
# 输出 "<文件>:<行号>: <原文>"；本脚本自己的片段 $PATH_SNIPPET 不算。
included_conflicts() {
    local pat="$1" depth="${2:-1}" f real snippet_real line text sub
    [ "$depth" -le 5 ] || return 0
    case "$pat" in /*) ;; *) pat="$NGINX_DIR/$pat" ;; esac
    snippet_real="$(readlink -f -- "$PATH_SNIPPET" 2>/dev/null || printf '%s' "$PATH_SNIPPET")"
    while IFS= read -r f; do
        [ -n "$f" ] && [ -f "$f" ] || continue
        real="$(readlink -f -- "$f")"
        [ "$real" = "$snippet_real" ] && continue
        while IFS=$'\037' read -r line text; do
            [ -n "$line" ] && printf '%s:%s: %s\n' "$f" "$line" "$text"
        done < <(location_conflicts "$f")
        while IFS= read -r sub; do
            [ -n "$sub" ] && included_conflicts "$sub" $((depth + 1))
        done < <(include_paths "$f")
    done < <(compgen -G "$pat" 2>/dev/null || true)
}

# 预检（任何改动之前执行）：找不到目标 server 块 / 已有 /todos location / 无法安全插入时直接退出。
path_preflight() {
    command -v nginx >/dev/null 2>&1 \
        || die "本机未安装 nginx，无法使用路径模式。可加 --no-nginx 只装服务，或改用 --mode subdomain。"

    local files f line ok inc conflict ctext indent listens incs found=0 conflicts="" unsafe="" all_listen=""
    local inc_pat inc_hits hit inc_list=()
    files="$(nginx_config_files)"
    PATH_TARGETS=()
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        grep -qF -- "$SITE_DOMAIN" "$f" 2>/dev/null || continue
        while IFS=$'\037' read -r line ok inc conflict ctext indent listens incs; do
            [ -n "$line" ] || continue
            found=$((found + 1))
            all_listen="${all_listen} ${listens:-80}"
            # server 块经 include 引入的文件里已有 /todos，同样算冲突
            if [ -n "$incs" ]; then
                IFS=$'\036' read -r -a inc_list <<< "$incs"
                for inc_pat in "${inc_list[@]}"; do
                    [ -n "$inc_pat" ] || continue
                    inc_hits="$(included_conflicts "$inc_pat" 1)"
                    [ -n "$inc_hits" ] || continue
                    while IFS= read -r hit; do
                        conflicts="${conflicts}
      ${hit}（经 ${f}:${line} 的 include ${inc_pat} 引入）"
                    done <<< "$inc_hits"
                    [ "$conflict" != "0" ] || conflict="included"
                done
            fi
            if [ "$conflict" != "0" ]; then
                [ "$conflict" = "included" ] || conflicts="${conflicts}
      ${f}:${conflict}: ${ctext}"
                continue
            fi
            if [ "$inc" = "1" ]; then
                info "已插入过（幂等跳过）：${f}:${line} 的 server 块（listen ${listens:-80}）"
                continue
            fi
            if [ "$ok" != "1" ]; then
                unsafe="${unsafe}
      ${f}:${line}"
                continue
            fi
            PATH_TARGETS+=("${f}"$'\t'"${line}"$'\t'"${indent}")
            info "目标 server 块：${f}:${line}（server_name 含 ${SITE_DOMAIN}，listen ${listens:-80}）"
        done < <(scan_server_blocks "$f")
    done <<< "$files"

    if [ "$found" -eq 0 ]; then
        die "在 nginx 配置中找不到 server_name 含 ${SITE_DOMAIN} 的 server 块（已检查 nginx -T 列出的文件 / ${NGINX_DIR}）。
    未做任何改动。可选：
       1) 改用子域模式：sudo bash $PROG --mode subdomain --domain easylife.${SITE_DOMAIN}
       2) 手动处理：把 $SELF_DIR/nginx-easylife-location.conf 的两个 location 粘进目标站点的 server 块，
          然后 sudo bash $PROG --no-nginx，再 nginx -t && systemctl reload nginx"
    fi
    if [ -n "$conflicts" ]; then
        die "现有站点里已经存在 ${BASE_PATH} 的 location，拒绝覆盖（未做任何改动）：${conflicts}
    请先确认这些 location 的用途：若要让 EasyLife 接管，请手工删除/改名后重跑；
    否则改用子域模式：sudo bash $PROG --mode subdomain --domain easylife.${SITE_DOMAIN}"
    fi
    if [ -n "$unsafe" ]; then
        die "以下 server 块的写法（如 'server { ... }' 写在同一行）无法安全地自动插入 include，未做任何改动：${unsafe}
    请手工在这些 server 块内加入一行： include ${PATH_SNIPPET};"
    fi
    case " $all_listen " in
        *443*) ;;
        *) warn "匹配到的 server 块里没有 443 监听：https://${SITE_DOMAIN}${BASE_PATH}/ 可能不可用（只加了 HTTP）。" ;;
    esac
    log "路径模式预检通过：共 ${found} 个 ${SITE_DOMAIN} server 块，其中 ${#PATH_TARGETS[@]} 个需要插入 include"
}

render_location_snippet() {
    local template="$SELF_DIR/nginx-easylife-location.conf"
    [ -f "$template" ] || die "找不到 nginx 片段模板 $template。"
    sed -e "s|127\.0\.0\.1:8086|127.0.0.1:${PORT}|g" \
        -e "/^[[:space:]]*#/!s|/todos|${BASE_PATH}|g" \
        "$template"
}

# insert_line_after FILE N TEXT —— 用 head/tail 拼接，FILE 其余字节原样保留（包括末尾有无换行）
insert_line_after() {
    local f="$1" n="$2" text="$3" tmp
    tmp="$(mktemp "${TMPDIR:-/tmp}/easylife-nginx.XXXXXX")"
    { head -n "$n" -- "$f"; printf '%s\n' "$text"; tail -n "+$((n + 1))" -- "$f"; } > "$tmp"
    cat "$tmp" > "$f"          # 覆盖内容、保留 inode/权限/属主（sites-enabled 软链接也不受影响）
    rm -f "$tmp"
}

# remove_include_lines FILE —— 只删带本脚本标记的 include 行（GNU sed 保留末尾无换行的情况）
remove_include_lines() {
    sed "/^[[:space:]]*include[[:space:]].*${INCLUDE_MARKER}\$/d" "$1"
}

backup_file() {
    local f="$1" ts="$2" dest
    install -d -m 0700 "$BACKUP_DIR"
    dest="$BACKUP_DIR/$(printf '%s' "${f#/}" | tr '/' '_').${ts}.bak"
    [ -e "$dest" ] && dest="${dest%.bak}.$$.bak"
    cp -p -- "$f" "$dest"
    printf '%s' "$dest"
}

# 失败时调用：把本次改过的文件逐字节恢复成备份，片段文件恢复/删除。
path_restore_changes() {
    local i
    for ((i = 0; i < ${#PATH_CHANGED[@]}; i++)); do
        cat -- "${PATH_CHANGED_BAK[$i]}" > "${PATH_CHANGED[$i]}" \
            && warn "已从备份恢复：${PATH_CHANGED[$i]}（备份 ${PATH_CHANGED_BAK[$i]}）"
    done
    PATH_CHANGED=(); PATH_CHANGED_BAK=()
    if [ "$SNIPPET_TOUCHED" = "1" ]; then
        if [ -n "$SNIPPET_PREV" ] && [ -f "$SNIPPET_PREV" ]; then
            cat -- "$SNIPPET_PREV" > "$PATH_SNIPPET"
            rm -f -- "$SNIPPET_PREV"
        else
            rm -f -- "$PATH_SNIPPET"
            rmdir -- "$PATH_SNIPPET_DIR" 2>/dev/null || true
        fi
        SNIPPET_TOUCHED=0; SNIPPET_PREV=""
    fi
}

install_nginx_path() {
    if [ "$RUN_NGINX" -eq 0 ]; then
        info "按 --no-nginx 跳过 nginx 配置。手工接入：在 ${SITE_DOMAIN} 的 server 块里 include $SELF_DIR/nginx-easylife-location.conf 的内容。"
        return 0
    fi

    # ---- 片段文件：内容不变就不动 ----
    local rendered
    rendered="$(mktemp "${TMPDIR:-/tmp}/easylife-snippet.XXXXXX")"
    render_location_snippet > "$rendered"
    if [ -f "$PATH_SNIPPET" ] && cmp -s "$rendered" "$PATH_SNIPPET"; then
        rm -f "$rendered"
    else
        if [ -f "$PATH_SNIPPET" ]; then
            SNIPPET_PREV="$(mktemp "${TMPDIR:-/tmp}/easylife-snippet-prev.XXXXXX")"
            cat "$PATH_SNIPPET" > "$SNIPPET_PREV"
        fi
        SNIPPET_TOUCHED=1
        install -d -m 0755 "$PATH_SNIPPET_DIR"
        cat "$rendered" > "$PATH_SNIPPET"
        chmod 0644 "$PATH_SNIPPET"
        rm -f "$rendered"
        log "已写入 nginx 片段 $PATH_SNIPPET（后端 127.0.0.1:${PORT}）"
    fi

    # ---- 插入 include：同一文件从下往上插，行号不会错位；每个文件只备份一次 ----
    local ts entry f line indent i bak
    ts="$(date +%Y%m%d-%H%M%S)"
    local sorted
    sorted="$(for ((i = 0; i < ${#PATH_TARGETS[@]}; i++)); do printf '%s\n' "${PATH_TARGETS[$i]}"; done \
              | sort -t $'\t' -k1,1 -k2,2nr)"
    while IFS=$'\t' read -r f line indent; do
        [ -n "$f" ] || continue
        bak=""
        for ((i = 0; i < ${#PATH_CHANGED[@]}; i++)); do
            [ "${PATH_CHANGED[$i]}" = "$f" ] && bak="${PATH_CHANGED_BAK[$i]}"
        done
        if [ -z "$bak" ]; then
            bak="$(backup_file "$f" "$ts")"
            PATH_CHANGED+=("$f"); PATH_CHANGED_BAK+=("$bak")
            log "已备份 $f -> $bak"
        fi
        insert_line_after "$f" "$line" "${indent}include ${PATH_SNIPPET}; ${INCLUDE_MARKER}"
        log "已在 ${f}:${line} 的 server 块内插入 include"
    done <<< "$sorted"

    if [ "${#PATH_CHANGED[@]}" -eq 0 ] && [ "$SNIPPET_TOUCHED" = "0" ]; then
        info "nginx 已是目标状态（include 已存在、片段未变），跳过 nginx -t / reload（幂等）。"
        return 0
    fi

    # ---- nginx -t 通过才 reload；失败逐字节恢复备份，非 0 退出 ----
    local out
    if ! out="$(nginx_test)"; then
        printf '%s\n' "$out" >&2
        path_restore_changes
        restore_service_state
        die "nginx -t 失败：已从备份恢复全部改动（含 $ENV_FILE 与服务状态），未执行 reload，现有站点保持原样。"
    fi
    log "nginx -t 通过"

    for ((i = 0; i < ${#PATH_CHANGED[@]}; i++)); do
        printf '%s\t%s\n' "${PATH_CHANGED[$i]}" "${PATH_CHANGED_BAK[$i]}" >> "$PATH_MANIFEST"
    done
    chmod 0600 "$PATH_MANIFEST" 2>/dev/null || true
    PATH_CHANGED=(); PATH_CHANGED_BAK=()
    [ -n "$SNIPPET_PREV" ] && rm -f -- "$SNIPPET_PREV"
    SNIPPET_TOUCHED=0; SNIPPET_PREV=""

    if reload_nginx; then
        log "nginx 已 reload：https://${SITE_DOMAIN}${BASE_PATH}/ 生效（站点其余配置未改动）"
    else
        warn "nginx reload 失败，请手工执行： sudo systemctl reload nginx"
    fi
}

# 回滚路径模式：删 include 行（与部署前备份一致时直接用备份覆盖）、删片段；nginx -t 失败则撤销回滚。
path_rollback() {
    local files="" f
    if [ -f "$PATH_MANIFEST" ]; then
        files="$(cut -f1 "$PATH_MANIFEST")"
    fi
    if [ -d "$NGINX_DIR" ]; then
        files="${files}
$(grep -RlsF -- "$INCLUDE_MARKER" "$NGINX_DIR" 2>/dev/null || true)"
    fi
    files="$(printf '%s\n' "$files" | while IFS= read -r f; do
                 [ -n "$f" ] && [ -f "$f" ] || continue
                 f="$(readlink -f -- "$f")"
                 case "$f" in "$PATH_SNIPPET_DIR"/*) continue ;; esac
                 printf '%s\n' "$f"
             done | awk '!seen[$0]++')"

    local undo_files=() undo_copies=() tmp bak copy changed=0
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        grep -qF -- "$INCLUDE_MARKER" "$f" || continue
        copy="$(mktemp "${TMPDIR:-/tmp}/easylife-rb.XXXXXX")"; cat "$f" > "$copy"
        undo_files+=("$f"); undo_copies+=("$copy")
        tmp="$(mktemp "${TMPDIR:-/tmp}/easylife-rb.XXXXXX")"
        remove_include_lines "$f" > "$tmp"
        bak=""
        [ -f "$PATH_MANIFEST" ] && bak="$(awk -F'\t' -v f="$f" '$1 == f {print $2; exit}' "$PATH_MANIFEST")"
        if [ -n "$bak" ] && [ -f "$bak" ] && cmp -s "$tmp" "$bak"; then
            cat "$bak" > "$f"
            log "已删除 include 行并恢复备份：$f（与部署前 $bak 一致）"
        else
            cat "$tmp" > "$f"
            log "已删除 $f 中的 include 行"
            [ -n "$bak" ] && warn "  $f 在部署后另有改动，为保留这些改动只删除了 include 行；部署前的备份：$bak"
        fi
        rm -f "$tmp"
        changed=1
    done <<< "$files"

    local snip_copy=""
    if [ -f "$PATH_SNIPPET" ]; then
        snip_copy="$(mktemp "${TMPDIR:-/tmp}/easylife-rb.XXXXXX")"; cat "$PATH_SNIPPET" > "$snip_copy"
        rm -f "$PATH_SNIPPET"
        rmdir "$PATH_SNIPPET_DIR" 2>/dev/null || true
        log "已删除 nginx 片段 $PATH_SNIPPET"
        changed=1
    fi

    if [ "$changed" -eq 0 ]; then
        info "未发现路径模式的 include 行或片段，跳过。"
        return 0
    fi

    local i
    if command -v nginx >/dev/null 2>&1; then
        if ! nginx_test >&2; then
            for ((i = 0; i < ${#undo_files[@]}; i++)); do cat "${undo_copies[$i]}" > "${undo_files[$i]}"; done
            if [ -n "$snip_copy" ]; then install -d -m 0755 "$PATH_SNIPPET_DIR"; cat "$snip_copy" > "$PATH_SNIPPET"; fi
            die "回滚后 nginx -t 失败：已撤销本次回滚（配置恢复为回滚前状态），服务未停止。请检查上面的报错。"
        fi
        reload_nginx && log "nginx 已 reload（${SITE_DOMAIN} 其余配置不变）" || warn "请手工 reload nginx"
    fi
    for ((i = 0; i < ${#undo_copies[@]}; i++)); do rm -f "${undo_copies[$i]}"; done
    [ -n "$snip_copy" ] && rm -f "$snip_copy"
    if [ -f "$PATH_MANIFEST" ]; then
        mv -f "$PATH_MANIFEST" "$PATH_MANIFEST.rolled-back.$(date +%Y%m%d-%H%M%S)"
    fi
    return 0
}

# ---------------------------------------------------------------- 步骤 6：certbot

run_certbot() {
    [ "$RUN_CERTBOT" -eq 1 ] || return 0
    if [ "$RUN_NGINX" -eq 0 ]; then
        warn "指定了 --certbot 但带上了 --no-nginx，跳过证书申请。"
        return 0
    fi
    if ! command -v certbot >/dev/null 2>&1; then
        warn "未找到 certbot，跳过证书申请。安装后可执行："
        warn "  sudo certbot --nginx -d $DOMAIN --redirect"
        return 0
    fi
    if ! [ -e "$NGINX_TARGET" ]; then
        warn "nginx 配置文件不存在，跳过 certbot（证书申请依赖 nginx 配置）。"
        return 0
    fi

    # 只针对"新域名"：既有配置里没有该域名、且没有现成证书时才申请
    local used
    used="$(domain_used_elsewhere "$DOMAIN" || true)"
    if [ -n "$used" ]; then
        warn "域名 $DOMAIN 已存在于其它 nginx 配置（可能是既有站点），为免冲突，跳过 certbot："
        printf '%s\n' "$used" | sed 's/^/      /' >&2
        return 0
    fi
    if nginx_has_cert; then
        info "已存在证书 $LETSENCRYPT_DIR/$DOMAIN/，跳过申请（续期交给 certbot.timer）。"
        return 0
    fi
    if [ -z "$CERTBOT_EMAIL" ]; then
        warn "未提供 --certbot-email，跳过 certbot（--agree-tos 需要邮箱）。"
        warn "手工执行： sudo certbot --nginx -d $DOMAIN --redirect"
        return 0
    fi

    log "申请证书：certbot --nginx -d $DOMAIN（仅针对该新域名）"
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect -m "$CERTBOT_EMAIL"; then
        if nginx_test; then
            reload_nginx && log "证书就绪，nginx 已 reload（HTTPS 生效，80 自动跳转 443）" \
                || warn "nginx reload 失败，请手工执行 systemctl reload nginx"
        else
            warn "certbot 之后 nginx -t 未通过，请手工检查（未做任何删除，避免影响证书）。"
        fi
    else
        warn "certbot 申请失败（常见原因：DNS 未解析到本机、80 端口不可达、域名已有其它站点）。"
        warn "站点目前仍是 HTTP 引导配置；修好 DNS/防火墙后重跑： sudo certbot --nginx -d $DOMAIN --redirect"
    fi
}

# ---------------------------------------------------------------- 步骤 7：服务

enable_service() {
    local was_active=0
    systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null && was_active=1
    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME" || die "systemctl enable --now $SERVICE_NAME 失败，请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"

    # 已在运行时 enable --now 不会重启：显式 restart，才能加载新代码 / 新 env（例如 --port 换了端口）
    if [ "$was_active" -eq 1 ]; then
        systemctl restart "$SERVICE_NAME" \
            || die "systemctl restart $SERVICE_NAME 失败，请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
        log "服务已在运行，已 restart 以加载新的代码与 $ENV_FILE"
    fi

    local i
    for i in 1 2 3 4 5; do
        systemctl is-active --quiet "$SERVICE_NAME" && break
        sleep 1
    done
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl status "$SERVICE_NAME" --no-pager -l 2>&1 | tail -n 25 >&2 || true
        die "服务未能保持运行（也可能是数据库 schema 迁移失败）。常见原因：EASYLIFE_SECRET 未设置或短于 16 字符、
    $DATA_DIR 不可写、端口被占用、迁移里的 SQL 报错。排查： journalctl -u $SERVICE_NAME -n 50 --no-pager
    $(rollback_hint)"
    fi

    # 确认新端口真的在监听，之后才写 nginx 片段 / reload（否则 nginx 会指向没人监听的端口）
    if command -v ss >/dev/null 2>&1 || command -v netstat >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1; then
        for i in $(seq 1 "$LISTEN_WAIT"); do
            [ -n "$(listener_on_port "$PORT")" ] && break
            sleep 1
        done
        [ -n "$(listener_on_port "$PORT")" ] \
            || die "服务已运行，但 ${LISTEN_WAIT} 秒内没有在 127.0.0.1:${PORT} 上监听（nginx 未做任何改动）。
    请检查 $ENV_FILE 里的 EASYLIFE_PORT，并查看： journalctl -u $SERVICE_NAME -n 50 --no-pager"
        log "已确认 127.0.0.1:${PORT} 在监听"
    else
        warn "未找到 ss/netstat/lsof，无法确认 ${PORT} 端口已监听"
    fi
    log "服务已启动并设为开机自启：systemctl enable --now $SERVICE_NAME"
}

# 自检：EASYLIFE_ADMIN_USER_IDS 只允许指向 $DATA_DIR 数据库里**现有**的账号。
# 不存在的 ID 会被应用 fail-closed 禁用（不授予任何权限），这里把它报告成「配置错误」，
# 并给出修正命令；只预警、不阻断部署（保证升级/重跑在运维修正前仍然可用）。
verify_admin_ids() {
    local ids db out line ok_ids="" invalid_ids="" missing_ids=""
    ids="$(env_get EASYLIFE_ADMIN_USER_IDS "")"
    [ -n "$ids" ] || return 0
    db="$(db_path_from_env)"
    if [ ! -f "$db" ]; then
        info "管理员配置检查：数据库尚不存在（$db），跳过（首次启动后会自动创建）。"
        return 0
    fi
    out="$("$PY_BIN" - "$db" "$ids" <<'PY' 2>&1
import re
import sqlite3
import sys

db_path, raw = sys.argv[1], sys.argv[2]
items = [item for item in re.split(r"[,\s]+", raw.strip()) if item]
valid = []
invalid = []
for item in items:
    if re.fullmatch(r"0*[0-9]{1,19}", item) and int(item) > 0 and int(item) <= 2 ** 63 - 1:
        valid.append(int(item))
    else:
        # 不原样回显可疑值：截断并去掉控制字符后只用于提示
        invalid.append(re.sub(r"[^0-9A-Za-z,._+-]", "?", item)[:24])
valid = sorted(set(valid))
missing = []
try:
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
except Exception:
    print("error\tdatabase unreadable")
    raise SystemExit(1)
try:
    for user_id in valid:
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            missing.append(user_id)
finally:
    conn.close()
print("ok\t" + ",".join(str(i) for i in valid if i not in missing))
print("invalid\t" + " ".join(invalid))
print("missing\t" + ",".join(str(i) for i in missing))
PY
)" || {
        warn "管理员配置检查失败：无法只读打开 $db；请手工核对 EASYLIFE_ADMIN_USER_IDS 后重启服务。"
        return 0
    }
    while IFS=$'\t' read -r line_tag line_val; do
        case "$line_tag" in
            ok) ok_ids="$line_val" ;;
            invalid) invalid_ids="$line_val" ;;
            missing) missing_ids="$line_val" ;;
        esac
    done <<< "$out"

    if [ -n "$missing_ids" ]; then
        warn "配置错误：EASYLIFE_ADMIN_USER_IDS=$ids 里的用户 ID 在 $db 中不存在：${missing_ids}"
        warn "  这些 ID 不会获得管理员权限（应用已禁用它们）。修正后重启： systemctl restart $SERVICE_NAME"
        warn "  查询现有账号： sudo sqlite3 $db \"SELECT id, email FROM users;\"（无 sqlite3 时用 python3）"
    fi
    if [ -n "$invalid_ids" ]; then
        warn "配置错误：EASYLIFE_ADMIN_USER_IDS 含非正整数条目（已被忽略）：${invalid_ids}"
    fi
    if [ -z "$missing_ids" ] && [ -z "$invalid_ids" ]; then
        log "管理员配置检查通过：EASYLIFE_ADMIN_USER_IDS=${ok_ids} 均存在（is_admin 只对这些账号生效）"
    fi
}

verify() {
    log "---- 验证 ----"
    systemctl is-active --quiet "$SERVICE_NAME" \
        && log "systemctl is-active $SERVICE_NAME : active" \
        || warn "systemctl is-active $SERVICE_NAME : 未运行"

    local listener
    listener="$(listener_on_port "$PORT")"
    if [ -n "$listener" ]; then
        case "$listener" in
            127.0.0.1:"$PORT"*) log "端口监听正常：$listener（仅回环地址）" ;;
            *) warn "端口 $PORT 监听在非回环地址：$listener —— 请把 $ENV_FILE 里的 EASYLIFE_HOST 改回 127.0.0.1 并重启服务！" ;;
        esac
    else
        warn "未能确认 $PORT 端口监听状态（缺少 ss/netstat/lsof？），请手工执行 ss -lntp | grep :$PORT"
    fi

    if command -v curl >/dev/null 2>&1; then
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/api/me" || true)"
        if [ "$code" = "401" ]; then
            log "HTTP 自检通过：GET http://127.0.0.1:${PORT}/api/me -> 401（未登录，符合预期）"
        else
            warn "HTTP 自检返回 ${code:-无响应}（预期 401），请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
        fi
    fi

    if [ "$RUN_NGINX" -eq 1 ] && command -v nginx >/dev/null 2>&1 && [ -n "$NGINX_TARGET" ]; then
        if nginx_test >/dev/null 2>&1; then log "nginx -t 通过"; else warn "nginx -t 未通过"; fi
    fi

    # 管理员名单自检（仅当 EASYLIFE_ADMIN_USER_IDS 非空）：只预警不阻断
    verify_admin_ids
}

# ---------------------------------------------------------------- 回滚

do_rollback() {
    warn "回滚模式：撤销本次部署新增/修改的**服务配置**：删除 include 行并恢复备份、删除「本脚本新增的」nginx 配置、停止并禁用服务、删除 systemd 单元。"
    warn "注意：回滚**不会把代码恢复到旧版本**——$APP_DIR 保持当前版本（这里没有旧代码快照）。"
    warn "数据目录 $DATA_DIR 与配置 $ENV_FILE **保留不动**（包含你的密钥、待办数据与网页邮件设置）。"
    warn "数据库默认也不动：只有显式加上 --restore-db，才会把它换成部署前的一致性快照。"
    if [ "$(id -u)" -ne 0 ]; then die "回滚需要 root 权限。"; fi

    # 先撤 nginx（失败会撤销回滚并退出，此时服务仍在运行，状态一致）；只动带标记的文件
    path_rollback

    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE" && log "已删除 $UNIT_FILE" || true
    systemctl daemon-reload 2>/dev/null || true

    # 子域模式：只删除 manifest 里登记的（本脚本新建的）文件；部署前就存在的 easylife.conf / 软链接一律不动
    local f removed=0
    if [ -f "$SUBDOMAIN_MANIFEST" ]; then
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            if [ -e "$f" ] || [ -L "$f" ]; then
                rm -f -- "$f"
                log "已删除本脚本新增的 nginx 配置 $f"
                removed=1
            fi
        done < <(awk '!seen[$0]++' "$SUBDOMAIN_MANIFEST")
        mv -f "$SUBDOMAIN_MANIFEST" "$SUBDOMAIN_MANIFEST.rolled-back.$(date +%Y%m%d-%H%M%S)"
    fi
    if [ "$removed" -eq 1 ]; then
        if command -v nginx >/dev/null 2>&1 && nginx_test; then
            reload_nginx && log "nginx 已 reload（既有站点不受影响）" || warn "请手工 reload nginx"
        else
            warn "nginx -t 未通过，请手工检查（本脚本只删除了自己新建的 easylife.conf）"
        fi
    else
        detect_nginx_target
        if [ -n "$NGINX_TARGET" ] && [ -e "$NGINX_TARGET" ]; then
            info "$NGINX_TARGET 不是本脚本创建的（manifest 中无记录），保持不动。"
        else
            info "未发现子域模式的 easylife.conf，跳过。"
        fi
    fi

    if [ "$DO_RESTORE_DB" -eq 1 ]; then
        restore_database_from_backup
    else
        local bak
        bak="$(db_backup_last_path)"
        if [ -n "$bak" ]; then
            info "升级前的数据库快照：$bak（当前数据库未被本脚本改动）"
            info "若要连数据一起回到升级前： sudo bash $APP_DIR/deploy/deploy.sh --rollback --restore-db"
        fi
    fi

    log "回滚完成。会保留：代码 $APP_DIR（当前版本，未回退）、数据 $DATA_DIR、配置 $ENV_FILE。"
    log "如需彻底清理： sudo rm -rf $APP_DIR $DATA_DIR $CONF_DIR && sudo userdel $SERVICE_USER"
}

# ---------------------------------------------------------------- main

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --mode) MODE="${2:?}"; MODE_FROM_ARG="$MODE"; shift 2 ;;
            --site-domain) SITE_DOMAIN="${2:?}"; shift 2 ;;
            --smtp-host) SMTP_HOST_ARG="${2:?}"; shift 2 ;;
            --smtp-port) SMTP_PORT_ARG="${2:?}"; shift 2 ;;
            --smtp-user) SMTP_USER_ARG="${2:?}"; shift 2 ;;
            --smtp-from) SMTP_FROM_ARG="${2:?}"; shift 2 ;;
            --port) PORT="${2:?}"; PORT_FROM_ARG="$PORT"; shift 2 ;;
            --domain) DOMAIN="${2:?}"; shift 2 ;;
            --base-url) BASE_URL="${2:?}"; BASE_URL_FROM_ARG="$BASE_URL"; shift 2 ;;
            --base-path) BASE_PATH="${2:?}"; BASE_PATH_FROM_ARG="$BASE_PATH"; shift 2 ;;
            --nginx-conf) NGINX_TARGET="${2:?}"; shift 2 ;;
            --certbot) RUN_CERTBOT=1; shift ;;
            --certbot-email) CERTBOT_EMAIL="${2:?}"; shift 2 ;;
            --no-nginx) RUN_NGINX=0; shift ;;
            --no-backup) DO_BACKUP=0; shift ;;
            --restore-db) DO_RESTORE_DB=1; shift ;;
            --rollback) DO_ROLLBACK=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "未知参数：$1（用 --help 查看用法）" ;;
        esac
    done

    if [ "$DO_RESTORE_DB" -eq 1 ] && [ "$DO_ROLLBACK" -eq 0 ]; then
        die "--restore-db 只能配合 --rollback 使用（它会把数据库换成部署前的快照）。"
    fi

    case "$PORT" in
        ''|*[!0-9]*) die "--port 必须是数字：$PORT" ;;
    esac
    [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] \
        || die "--port 必须 >= 1024（无需特权的端口）：$PORT"

    # 兼容旧命令行：没写 --mode 但给了子域专用参数（--domain/--certbot/--nginx-conf）时按子域模式处理
    if [ -z "$MODE_FROM_ARG" ] && { [ -n "$DOMAIN" ] || [ "$RUN_CERTBOT" -eq 1 ] || [ -n "$NGINX_TARGET" ]; }; then
        MODE="subdomain"
        info "检测到子域参数（--domain/--certbot/--nginx-conf），按 --mode subdomain 处理。"
    fi
    case "$MODE" in
        path)
            case "$SITE_DOMAIN" in ''|*[!A-Za-z0-9.-]*) die "--site-domain 不合法：$SITE_DOMAIN" ;; esac
            DOMAIN="${DOMAIN:-$SITE_DOMAIN}"
            [ -n "$BASE_PATH_FROM_ARG" ] || BASE_PATH="/todos"
            [ -n "$BASE_URL_FROM_ARG" ] || BASE_URL="https://${SITE_DOMAIN}${BASE_PATH%/}"
            if [ "$RUN_CERTBOT" -eq 1 ]; then
                warn "路径模式沿用 ${SITE_DOMAIN} 现有证书，忽略 --certbot。"
                RUN_CERTBOT=0
            fi
            ;;
        subdomain)
            DOMAIN="${DOMAIN:-easylife.mch.wang}"
            ;;
        *) die "--mode 只能是 path 或 subdomain：$MODE" ;;
    esac
    case "$SMTP_PORT_ARG" in ''|[0-9]*) ;; *) die "--smtp-port 必须是数字：$SMTP_PORT_ARG" ;; esac

    if [ "$RUN_CERTBOT" -eq 1 ] && [ -z "$BASE_URL" ]; then
        BASE_URL="https://$DOMAIN"
    fi
    if [ -z "$BASE_URL" ]; then
        BASE_URL="https://$DOMAIN"
    fi
    BASE_URL_FROM_ARG="${BASE_URL_FROM_ARG:-}"
    case "$BASE_PATH" in
        ''|/) BASE_PATH="" ;;
        /*) BASE_PATH="${BASE_PATH%/}" ;;
        *) die "--base-path 必须以 / 开头，例如 /todos：$BASE_PATH" ;;
    esac
    case "$BASE_PATH" in
        *[!A-Za-z0-9._~/-]*) die "--base-path 含非法字符：$BASE_PATH" ;;
    esac
    if [ "$MODE" = "path" ] && [ -z "$BASE_PATH" ]; then
        die "路径模式需要非空的 --base-path（默认 /todos）；要部署在根路径请用 --mode subdomain。"
    fi

    require_root
    [ "$DO_ROLLBACK" -eq 1 ] && { do_rollback; exit 0; }

    trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
    trap 'drop_service_snapshot' EXIT     # 无论成败都不在 TMPDIR 留下含密钥的 env 副本

    # 重跑时以已有环境文件里的端口为准（否则会拿默认 8086 去做占用检查）
    if [ -z "$PORT_FROM_ARG" ] && [ -f "$ENV_FILE" ]; then PORT="$(env_get EASYLIFE_PORT "$PORT")"; fi

    if [ "$MODE" = "path" ]; then
        deploy_path_mode
        return 0
    fi

    info "EasyLife 部署开始（子域模式）：域名=$DOMAIN 后端=127.0.0.1:$PORT PUBLIC_URL=$BASE_URL BASE_PATH=${BASE_PATH:-/} 用户=$SERVICE_USER"
    check_port           # 1) 端口占用检查
    check_python         # 2) python3 版本检查
    create_user_and_dirs # 3) 系统用户与目录
    install_code         # 4) 复制代码（只换代码，不动 $DATA_DIR 里的数据库）
    backup_database      # 4') 升级前自动备份数据库（deploy/backup.sh；失败即中止）
    snapshot_service_state
    install_env_file     # 5) 环境文件（已存在则逐字节保留，只追加缺失的新配置项）
    install_unit         # 5) systemd 单元
    install_nginx        # 6) 仅当不存在时写入 + nginx -t 通过才 reload（失败自动回滚）
    run_certbot          # 7) 可选：certbot --nginx -d <域名>
    enable_service       # 8) systemctl enable --now
    verify

    log "---- 完成 ----"
    info "首次部署后请手工完成："
    info "  1) DNS：$DOMAIN 的 A 记录指向本机公网 IP（若还没做，certbot 会失败）"
    info "  2) 编辑 $ENV_FILE 填好 SMTP_*（QQ/163 用授权码），然后 systemctl restart $SERVICE_NAME"
    info "  3) 管理员：把现有账号 ID 填进 $ENV_FILE 的 EASYLIFE_ADMIN_USER_IDS（默认空=无人有权限），再 systemctl restart $SERVICE_NAME"
    info "  4) 备份：sudo bash $APP_DIR/deploy/backup.sh   （建议加进 crontab，见 docs/DEPLOY.md §9）"
    info "  5) 立刻更换服务器 root 密码并改用 SSH 密钥登录（见 docs/DEPLOY.md §2）"
}

deploy_path_mode() {
    info "EasyLife 部署开始（路径模式）：https://${SITE_DOMAIN}${BASE_PATH}/ -> 127.0.0.1:$PORT 用户=$SERVICE_USER"
    check_port               # 1) ss 检查端口占用（被占用请 --port 换端口）
    if [ "$RUN_NGINX" -eq 1 ]; then
        path_preflight       # 2) 找 ${SITE_DOMAIN} 的 server 块 / 拒绝已有 /todos —— 在任何改动之前
    fi
    check_python             # 3) python3 版本检查
    create_user_and_dirs     # 4) 系统用户与目录
    install_code             # 5) 复制代码（只换代码，不动 $DATA_DIR 里的数据库）
    backup_database          # 5') 升级前自动备份数据库（deploy/backup.sh；失败即中止）
    snapshot_service_state   #    记下旧 env / 旧端口 / 服务是否在运行（nginx -t 失败时回滚服务）
    install_env_file        # 6) 环境文件：已存在则逐字节保留，只追加缺失的新配置项 / 端口 / 可选 SMTP
    install_unit             # 7) systemd 单元
    enable_service           # 8) 先起后端（应用启动时做 schema 迁移），再接 nginx，避免短暂 502
    install_nginx_path       # 9) 备份 -> 插 include -> nginx -t 通过才 reload（失败恢复备份）
    verify

    log "---- 完成 ----"
    info "访问地址：https://${SITE_DOMAIN}${BASE_PATH}/ （http://${SITE_DOMAIN}${BASE_PATH}/ 按站点原有规则处理）"
    info "  - 改动的 nginx 文件与备份记录在 $PATH_MANIFEST；回滚： sudo bash $APP_DIR/deploy/deploy.sh --rollback"
    info "  - SMTP：重跑时加 --smtp-host/--smtp-user（授权码用环境变量 EASYLIFE_SMTP_PASS），或编辑 $ENV_FILE 后 systemctl restart $SERVICE_NAME"
    info "  - 管理员：把现有账号 ID 填进 $ENV_FILE 的 EASYLIFE_ADMIN_USER_IDS（默认空=无人有权限），再 systemctl restart $SERVICE_NAME"
    info "  - 备份：sudo bash $APP_DIR/deploy/backup.sh   （建议加进 crontab，见 docs/DEPLOY.md §9）"
    info "  - 立刻更换服务器 root 密码并改用 SSH 密钥登录（见 docs/DEPLOY.md §2）"
}

# 只有直接执行时才跑 main（便于 `source deploy.sh` 做单元测试）
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
