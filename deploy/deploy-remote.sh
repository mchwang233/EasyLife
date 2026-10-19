#!/usr/bin/env bash
# =============================================================================
#  EasyLife 远程部署（在你本机电脑上运行；一条命令、可完全非交互）
#
#  用法：
#     bash deploy/deploy-remote.sh <host> [选项] [-- <传给 deploy.sh 的额外参数>]
#  例如：
#     bash deploy/deploy-remote.sh <服务器IP>                  # 默认 root@，路径模式，一条命令搞定
#     bash deploy/deploy-remote.sh <服务器IP> --check          # 只预检，不做任何修改
#     bash deploy/deploy-remote.sh root@<服务器IP> --port 8090 # 8086 被占用时换端口
#     bash deploy/deploy-remote.sh <服务器IP> -i ~/.ssh/id_ed25519
#     bash deploy/deploy-remote.sh <服务器IP> --rollback       # 回滚服务器上本部署做过的改动
#     bash deploy/deploy-remote.sh <服务器IP> --log /tmp/easylife-deploy.log
#     bash deploy/deploy-remote.sh <服务器IP> -- --smtp-host smtp.qq.com --smtp-user you@qq.com
#
#  步骤：tar 打包（只含 easylife/ deploy/ docs/，并排除运行时凭据与浏览器测试资料）
#        -> scp 到 <host>:/opt/easylife-src
#        -> ssh 执行 deploy.sh --mode path（升级前自动备份数据库、已有 env 只追加缺失的新配置项）
#        -> 在服务器上跑四条自检（/todos 301、/todos/ 200、未登录 /todos/api/todos 401，
#           以及 /todos/ 页面包含新界面标识 "$UI_MARKER"）并打印 PASS/FAIL 汇总；
#           有失败会提示用 --rollback 撤销服务配置（它不会回退代码）；数据恢复只能显式
#           --rollback --restore-db（把数据库换成部署前快照，默认不动数据）。
#
#  认证（默认优先 SSH 密钥，非交互）：
#   1) 首选 SSH 密钥：ssh-agent / ~/.ssh/id_*，或用 -i 指定私钥 —— 可完全无人值守；
#   2) 可选密码：export SSHPASS=... **且** 本机已安装 sshpass 时，脚本用 `sshpass -e`
#      从环境变量读密码（-e：密码不出现在命令行、进程列表、本脚本输出与日志里）；
#      没有 sshpass 就自动退回密钥 / 交互式输入，**绝不会**把密码写进任何文件或日志。
#   本脚本与仓库**绝不**保存、回显或记录任何明文密码（--log 记录的是脚本输出，不含密码）。
#   整个过程复用一条 SSH 连接（ControlMaster），交互输入密码时只需输一次。
# =============================================================================
set -Eeuo pipefail

PROG="$(basename "$0")"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 源码树根目录（本地仓库）。EASYLIFE_SRC_DIR 只用于测试（假 ssh/临时源码树），生产不要设置。
SRC_DIR="${EASYLIFE_SRC_DIR:-$(cd "$SELF_DIR/.." && pwd)}"

REMOTE_DIR="${EASYLIFE_REMOTE_DIR:-/opt/easylife-src}"   # 服务器上的暂存目录（测试用环境变量覆盖）
REMOTE_APP_DIR="${EASYLIFE_APP_DIR:-/opt/easylife}"      # 服务器上的安装目录（--rollback 用它下面的 deploy.sh）
SITE_DOMAIN="mch.wang"
BASE_PATH="/todos"
PORT="8086"
CURL_TIMEOUT="${CURL_TIMEOUT:-15}"
SSH_PORT=""
IDENTITY=""
DO_SMOKE=1
DO_CHECK=0
DO_ROLLBACK=0
DO_RESTORE_DB=0
# /todos/ 页面里的「新界面标识」：默认找邮箱验证界面的横幅元素（id/class）。
# 可用 EASYLIFE_UI_MARKER=<字符串> 覆盖（例如 view-check-email）。
UI_MARKER="${EASYLIFE_UI_MARKER:-verification-banner}"
LOG_FILE="${EASYLIFE_REMOTE_LOG:-}"
DEPLOY_ARGS=()

# ---------------------------------------------------------------- --log
# 需要日志时，用 `bash "$0" "$@" | tee -a` 把脚本重新执行一遍：tee 会一直写到脚本结束，
# 日志一定完整（`exec > >(tee ...)` 有竞态，可能丢掉尾部）。EASYLIFE_REMOTE_LOG_TEE 防止递归。
log_scan=( "$@" )
i=0
while [ "$i" -lt "${#log_scan[@]}" ]; do
    case "${log_scan[$i]}" in
        --log) LOG_FILE="${log_scan[$((i + 1))]:-}" ;;
        --log=*) LOG_FILE="${log_scan[$i]#--log=}" ;;
    esac
    i=$((i + 1))
done
unset log_scan i
if [ -n "$LOG_FILE" ] && [ -z "${EASYLIFE_REMOTE_LOG_TEE:-}" ]; then
    : >>"$LOG_FILE" 2>/dev/null || { printf '[x] 无法写入日志文件：%s\n' "$LOG_FILE" >&2; exit 2; }
    export EASYLIFE_REMOTE_LOG_TEE=1
    bash "$0" "$@" 2>&1 | tee -a "$LOG_FILE"
    exit "${PIPESTATUS[0]}"
fi

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
info() { printf '\033[36m[i]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# PASS/FAIL 汇总用的计数器（--check 与部署后自检共用）
N_PASS=0
N_FAIL=0
ok()      { N_PASS=$((N_PASS + 1)); printf '\033[32m[PASS]\033[0m %s\n' "$*"; }
bad()     { N_FAIL=$((N_FAIL + 1)); printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; }
summary() { printf '\033[36m[i]\033[0m %s：PASS %d / FAIL %d\n' "$1" "$N_PASS" "$N_FAIL"; }

usage() {
    cat <<USAGE
$PROG —— 在本地把 EasyLife 部署到远程服务器的 https://${SITE_DOMAIN}${BASE_PATH}/

用法：
  bash $PROG <host> [选项] [-- <deploy.sh 额外参数>]

  <host>                  服务器地址，可写 user@host；不写用户时默认 root
选项：
  --port <端口>           后端端口（转给 deploy.sh --port），默认 8086；被占用时改用 8090
  --site-domain <域名>    现有站点域名，默认 mch.wang（同时用于自检 URL）
  --base-path <路径>      子路径前缀，默认 /todos
  --ssh-port <端口>       SSH 端口，默认 22
  -i <私钥文件>           指定 SSH 私钥
  --check                 **只预检，不做任何修改**：SSH 连通 + 远端 nginx -t + ${PORT} 端口空闲
                          + ${SITE_DOMAIN} 的 443 server 块存在，最后打印 PASS/FAIL 汇总
  --rollback              在服务器上执行 deploy.sh --rollback：撤销本部署的 nginx/systemd **服务配置**
                          （不会把代码回退到旧版本，保留代码/数据/密钥）
  --restore-db            配合 --rollback：显式让远程 deploy.sh 把数据库恢复成部署前的快照（默认不动数据）
  --no-smoke              跳过部署后自检（三条状态码 + /todos/ 页面新界面标识）
  --log <文件>            把本次输出同时追加写入日志文件（脚本输出里没有任何密码）
  -- <deploy.sh 参数>     原样转发给服务器上的 deploy.sh（如 --smtp-host、--mode subdomain）
  -h, --help              显示本帮助

  脚本固定用 \`--mode path\`（挂到 https://mch.wang/todos/）。要传给 deploy.sh 的其它参数
  放在 \`--\` 后面原样转发，例如：\`-- --smtp-host smtp.qq.com\`，
  或改用子域模式：\`-- --mode subdomain --domain easylife.mch.wang\`。

认证：默认优先 SSH 密钥（ssh-agent / ~/.ssh/id_* / -i），可完全非交互；
      也可 export SSHPASS=... 并在本机装有 sshpass 时自动用 \`sshpass -e\` 读环境变量
      （密码不出现在命令行，也不会写进脚本、日志或任何文件）。
USAGE
}

# ---------------------------------------------------------------- 参数
HOST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?}"; DEPLOY_ARGS+=(--port "$2"); shift 2 ;;
        --site-domain) SITE_DOMAIN="${2:?}"; DEPLOY_ARGS+=(--site-domain "$2"); shift 2 ;;
        --base-path) BASE_PATH="${2:?}"; DEPLOY_ARGS+=(--base-path "$2"); shift 2 ;;
        --ssh-port) SSH_PORT="${2:?}"; shift 2 ;;
        -i) IDENTITY="${2:?}"; shift 2 ;;
        --check) DO_CHECK=1; shift ;;
        --rollback) DO_ROLLBACK=1; shift ;;
        --restore-db) DO_RESTORE_DB=1; shift ;;
        --no-smoke) DO_SMOKE=0; shift ;;
        --log) LOG_FILE="${2:?}"; shift 2 ;;
        --log=*) LOG_FILE="${1#--log=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; DEPLOY_ARGS+=("$@"); break ;;
        -*) die "未知参数：$1（用 --help 查看用法）" ;;
        *) [ -z "$HOST" ] || die "只能指定一个 host：$1"; HOST="$1"; shift ;;
    esac
done
[ -n "$HOST" ] || { usage >&2; exit 2; }
case "$HOST" in *@*) ;; *) HOST="root@$HOST" ;; esac
case "$HOST" in *[!A-Za-z0-9@._:-]*) die "host 含非法字符：$HOST" ;; esac
REMOTE_USER="${HOST%%@*}"
case "$PORT" in ''|*[!0-9]*) die "--port 必须是数字：$PORT" ;; esac
case "$BASE_PATH" in ''|/) BASE_PATH="" ;; /*) BASE_PATH="${BASE_PATH%/}" ;; *) die "--base-path 必须 / 开头：$BASE_PATH" ;; esac
if [ "$DO_CHECK" -eq 1 ] && [ "$DO_ROLLBACK" -eq 1 ]; then
    die "--check 与 --rollback 不能同时使用。"
fi
if [ "$DO_RESTORE_DB" -eq 1 ] && [ "$DO_ROLLBACK" -eq 0 ]; then
    die "--restore-db 只能配合 --rollback 使用（它会把数据库换回部署前的快照）。"
fi

# ---------------------------------------------------------------- SSH 选项
WORK="$(mktemp -d "${TMPDIR:-/tmp}/easylife-remote.XXXXXX")"
CONTROL="$WORK/cm"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$CONTROL" -o ControlPersist=120
          -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new)
[ -n "$SSH_PORT" ] && SSH_OPTS+=(-o "Port=$SSH_PORT")
[ -n "$IDENTITY" ] && SSH_OPTS+=(-i "$IDENTITY")

WRAP=()
if [ -n "${SSHPASS:-}" ]; then
    if command -v sshpass >/dev/null 2>&1; then
        WRAP=(sshpass -e)          # -e：从环境变量 SSHPASS 读，密码不出现在命令行
        SSH_OPTS+=(-o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no)
        info "使用 sshpass -e（密码取自环境变量 SSHPASS，不落盘、不回显）"
    else
        warn "设置了 SSHPASS 但本机没有 sshpass，改为 SSH 密钥 / 交互式输入密码（密码不会写进任何文件）。"
    fi
fi

cleanup() {
    ssh "${SSH_OPTS[@]}" -O exit "$HOST" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

rssh() { ${WRAP[@]+"${WRAP[@]}"} ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
rscp() { ${WRAP[@]+"${WRAP[@]}"} scp "${SSH_OPTS[@]}" "$@"; }

SUDO=""
[ "$REMOTE_USER" = "root" ] || SUDO="sudo"

# 把参数安全地拼进远程命令行
quote_args() { local a out=""; for a in "$@"; do out+=" $(printf '%q' "$a")"; done; printf '%s' "$out"; }

# ---------------------------------------------------------------- 0) 预检（--check）
# 只读：不做任何上传、不写任何远端文件、不 reload 任何服务。
# has_443_site_block <域名>：stdin 读 `nginx -T` 输出，输出 1/0（是否存在该域名 + listen 443 的 server 块）
has_443_site_block() {
    awk -v domain="$1" '
        function trim(s) { gsub(/^[ \t\r]+|[ \t\r]+$/, "", s); return s }
        function diff(s,   t) { t = s; return gsub(/\{/, "", t) - gsub(/\}/, "", t) }
        function lastword(s,   n, w) { n = split(trim(s), w, /[ \t]+/); return (n ? w[n] : "") }
        function flush() {
            if (blk != "") {
                if (blk ~ ("server_name[ \t]+[^;]*[ \t]" domain "[ \t;]") \
                    || blk ~ ("server_name[ \t]+" domain "[ \t;]")) {
                    if (blk ~ /listen[ \t]+[0-9.:\[\]a-zA-Z]*443[ \t;]/) found = 1
                }
                blk = ""
            }
        }
        {
            line = $0
            code = line; sub(/#.*/, "", code)
            if (blk == "") {
                head = code; sub(/\{.*/, "", head); head = trim(head)
                if (head == "server" || (head == "" && carry == "server")) {
                    blk = line; depth = diff(code); carry = ""
                    opened = (code ~ /\{/) ? 1 : 0
                    if (opened && depth <= 0) flush()
                } else {
                    carry = (code !~ /\{/) ? lastword(code) : ""
                }
            } else {
                blk = blk "\n" line
                depth += diff(code)
                if (depth > 0) opened = 1
                if (opened && depth <= 0) flush()
            }
        }
        END { flush(); print (found ? 1 : 0) }
    '
}

do_check() {
    log "预检模式（--check）：只做只读检查，不会被上传、不会修改服务器上的任何文件"
    local out line svc dump

    # 1) SSH 连通
    if rssh "$SUDO true" >/dev/null 2>&1; then
        ok "SSH 连通：$HOST（复用连接，非交互）"
    else
        bad "SSH 连通失败：$HOST —— 检查网络/端口，用 -i 指定私钥，或 export SSHPASS 配合本机 sshpass"
        summary "预检汇总"
        return 1
    fi

    # 2) 远端 nginx -t（只读，不 reload）
    if out="$(rssh "$SUDO nginx -t 2>&1" 2>&1)"; then
        ok "远端 nginx -t 通过"
    else
        bad "远端 nginx -t 失败（未做任何改动）：$(printf '%s' "$out" | tr '\n' ' ' | tail -c 300)"
    fi

    # 3) 端口空闲（默认 8086；--port 时检查指定端口）
    out="$(rssh "$SUDO ss -lntp 2>/dev/null || $SUDO netstat -lntp 2>/dev/null || true" 2>/dev/null || true)"
    line="$(printf '%s\n' "$out" | awk -v p=":$PORT\$" 'NR > 1 && $4 ~ p { print $4; exit }')"
    if [ -z "$line" ]; then
        ok "端口 $PORT 空闲（$SITE_DOMAIN 站点可挂到 127.0.0.1:$PORT）"
    else
        svc="$(rssh "$SUDO systemctl is-active easylife 2>/dev/null || true" 2>/dev/null || true)"
        if [ "$svc" = "active" ]; then
            ok "端口 $PORT 已被本服务 easylife 占用（属于已部署状态，重跑是幂等的）"
        else
            bad "端口 $PORT 已被占用：$line（easylife 服务未运行）—— 换成空闲端口重跑，例如 --port 8090"
        fi
    fi

    # 4) mch.wang 的 443 server 块存在
    dump="$(rssh "$SUDO nginx -T 2>/dev/null || true" 2>/dev/null || true)"
    if [ "$(printf '%s\n' "$dump" | has_443_site_block "$SITE_DOMAIN")" = "1" ]; then
        ok "找到 $SITE_DOMAIN 的 443 server 块（https://${SITE_DOMAIN}${BASE_PATH}/ 走现有证书）"
    else
        bad "没找到 server_name 含 $SITE_DOMAIN 且 listen 443 的 server 块 —— 先确认该站点在跑；或改用 --mode subdomain（见 docs/DEPLOY.md 第 5.3 节）"
    fi

    summary "预检汇总"
    if [ "$N_FAIL" -ne 0 ]; then
        warn "预检有 $N_FAIL 项未通过；本次没有做任何改动。修好后重跑：bash $PROG $HOST --check"
        return 1
    fi
    log "预检全部通过。开始部署：bash $PROG $HOST"
    return 0
}

# ---------------------------------------------------------------- 0) 回滚（--rollback）
do_remote_rollback() {
    local restore=""
    # 注意：这里必须判数值，不能用 ${DO_RESTORE_DB:+ ...}——DO_RESTORE_DB 默认值是字符串 "0"，
    # 而 `:+` 只判断「是否非空」，会把 --restore-db 无条件带上（即使没写 --restore-db）。
    if [ "$DO_RESTORE_DB" -eq 1 ]; then
        restore=" --restore-db"
        log "在服务器上执行回滚（撤销本部署加过的 nginx / systemd 服务配置；不会把代码回退到旧版本；并显式把数据库恢复成部署前的快照）"
    else
        log "在服务器上执行回滚（只撤销本部署加过的 nginx / systemd 服务配置；不会把代码回退到旧版本；保留代码、数据与密钥）"
    fi
    local cmd
    cmd="if [ -f $REMOTE_APP_DIR/deploy/deploy.sh ]; then $SUDO bash $REMOTE_APP_DIR/deploy/deploy.sh --rollback$restore; \
elif [ -f $REMOTE_DIR/deploy/deploy.sh ]; then $SUDO bash $REMOTE_DIR/deploy/deploy.sh --rollback$restore; \
else echo '找不到 deploy.sh（$REMOTE_APP_DIR/deploy/deploy.sh 与 $REMOTE_DIR/deploy/deploy.sh 都不存在）' >&2; exit 3; fi"
    rssh "$cmd" || die "回滚失败，请看上面的输出（未做任何其它改动）。"
    if [ "$DO_RESTORE_DB" -eq 1 ]; then
        log "回滚完成：nginx 里带 managed-by-easylife-deploy 标记的 include 行已删除，服务已停用，数据库已恢复成部署前的快照。"
    else
        log "回滚完成：nginx 里带 managed-by-easylife-deploy 标记的 include 行已删除，服务已停用。"
        info "数据库未动（数据恢复只能显式进行：bash $PROG $HOST --rollback --restore-db）。"
    fi
    info "保留：代码 $REMOTE_APP_DIR（当前版本，未回退）、数据 /var/lib/easylife、密钥 /etc/easylife/easylife.env、nginx 备份。"
    info "重新部署：bash $PROG $HOST"
}

# ---------------------------------------------------------------- 远端 curl 自检
# remote_http_code <URL> [额外 curl 参数] -> 打印 3 位状态码（拿不到就是 000）
remote_http_code() {
    local url="$1" extra="${2:-}" out code
    out="$(rssh "$SUDO curl -sS -o /dev/null -w '%{http_code}' --max-time $CURL_TIMEOUT${extra:+ $extra} $url" 2>/dev/null || true)"
    code="$(printf '%s\n' "$out" | grep -oE '[0-9]{3}' | tail -n 1 || true)"
    printf '%s' "${code:-000}"
}

# 取页面正文并在**服务器上** grep 新界面标识（自检第 4 项）
remote_page_has_marker() {
    local url="$1" extra="${2:-}" marker_q
    marker_q="$(printf '%q' "$UI_MARKER")"
    rssh "$SUDO curl -sS --max-time $CURL_TIMEOUT${extra:+ $extra} $url | grep -qF -- $marker_q" >/dev/null 2>&1
}

# 部署后在**服务器上**执行三条 curl 自检并打印 PASS/FAIL 汇总。
# 先在服务器上打公网地址（真实路径）；若完全无响应（000，常见于 DNS/回流限制），
# 再退回 --resolve 到 127.0.0.1 复测一次（仍走 nginx 与 TLS，只是绕过 DNS）。
post_deploy_selfcheck() {
    [ "$DO_SMOKE" -eq 1 ] || { log "已跳过部署后自检（--no-smoke）。"; return 0; }

    local paths=( "$BASE_PATH" "$BASE_PATH/" "$BASE_PATH/api/todos" )
    local wants=( 301 200 401 )
    local desc=( "未登录访问 ${BASE_PATH}（不带斜杠）应 301 跳到 ${BASE_PATH}/"
                 "${BASE_PATH}/ 页面应 200"
                 "未登录访问 ${BASE_PATH}/api/todos 应 401" )
    local extra="" url code

    log "---- 部署后自检（三条 curl + 页面标识，都在服务器上执行）----"
    local n
    for n in 0 1 2; do
        url="https://${SITE_DOMAIN}${paths[$n]}"
        code="$(remote_http_code "$url" "$extra")"
        # 服务器上用公网地址完全无响应（DNS 未生效 / 回流限制）：退回 --resolve 127.0.0.1 复测，
        # 仍然经过 nginx 与 TLS，只是绕过 DNS；此后几条都用这个方式。
        if [ "$code" = "000" ] && [ -z "$extra" ]; then
            extra="--resolve ${SITE_DOMAIN}:443:127.0.0.1"
            info "服务器上用公网地址自测无响应（DNS 未生效或回流限制），改用 ${extra} 直连本机 nginx 复测"
            code="$(remote_http_code "$url" "$extra")"
        fi
        if [ "$code" = "${wants[$n]}" ]; then
            ok "自检 $((n + 1))/4：$url -> $code（期望 ${wants[$n]}）${extra:+ [本机解析复测]}"
        else
            bad "冒烟检查失败：$url -> $code（期望 ${wants[$n]}）—— ${desc[$n]}"
        fi
    done

    # 自检 4/4：/todos/ 页面必须含新界面标识（邮箱验证/找回密码界面）
    url="https://${SITE_DOMAIN}${BASE_PATH}/"
    if remote_page_has_marker "$url" "$extra"; then
        ok "自检 4/4：$url 页面包含新界面标识 '$UI_MARKER'${extra:+ [本机解析复测]}"
    else
        bad "页面标识检查失败：$url 页面里没有找到 '$UI_MARKER'（新界面没生效？静态文件是否更新、或标识需要用 EASYLIFE_UI_MARKER=<字符串> 覆盖）"
    fi
    summary "自检汇总"

    if [ "$N_FAIL" -ne 0 ]; then
        warn "自检有 $N_FAIL 项未通过。排查：ssh $HOST 'systemctl status easylife --no-pager; journalctl -u easylife -n 50 --no-pager; nginx -t'"
        die "已部署但自检未全部通过 —— 需要回到部署前状态时用 --rollback：
        bash $PROG $HOST --rollback                    （撤销 nginx/systemd 改动）
        bash $PROG $HOST --rollback --restore-db        （连数据库一起回到部署前的快照）
        （等价：ssh $HOST '$SUDO bash $REMOTE_APP_DIR/deploy/deploy.sh --rollback --restore-db'）"
    fi
    log "部署完成并通过自检：https://${SITE_DOMAIN}${BASE_PATH}/"
    info "浏览器打开（注意结尾斜杠）：https://${SITE_DOMAIN}${BASE_PATH}/"
    info "回滚：bash $PROG $HOST --rollback"
}

# ---------------------------------------------------------------- 入口
if [ "$DO_CHECK" -eq 1 ]; then
    do_check || exit 1
    exit 0
fi

if [ "$DO_ROLLBACK" -eq 1 ]; then
    do_remote_rollback
    exit 0
fi

# ---------------------------------------------------------------- 1) 打包
# 发布包只含 easylife/ deploy/ docs/，并强制排除：
#   * 运行时凭据：真实 env 文件（*.env，**不含** deploy/easylife.env.example 模板）、私钥/证书、
#     SQLite 数据库与 WAL/SHM、netrc/SSH 私钥等；
#   * 浏览器/测试资料：Firefox/Chromium profile、cookies、采集数据（*.har）、日志、截图，以及
#     docs/verification/（截图与浏览器日志等证据留在仓库供审阅，服务器不需要）。
# 打完包还会逐条核对文件名（check_release_archive），发现漏网直接终止上传。
RELEASE_EXCLUDES=(
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo'
    --exclude='*.env'                     # 真实环境文件；*.env.example 模板不受影响
    --exclude='*.pem' --exclude='*.key' --exclude='*.p12' --exclude='*.pfx'
    --exclude='id_rsa*' --exclude='id_ed25519*' --exclude='id_ecdsa*'
    --exclude='.netrc' --exclude='*.netrc'
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm'
    --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='*.sqlite-wal' --exclude='*.sqlite-shm'
    --exclude='*cookies*' --exclude='ffprofile*' --exclude='*.parentlock'
    --exclude='*.har' --exclude='*.webm'
    --exclude='*.log' --exclude='*~' --exclude='*.bak' --exclude='*.orig' --exclude='*.rej'
    --exclude='docs/verification'         # 浏览器截图/日志等测试资料（不进发布包）
)

# 发布包内不允许出现的文件名形态（与 RELEASE_EXCLUDES 对应的兜底核对）
RELEASE_FORBIDDEN_RE='(^|/)([^/]*\.env$|[^/]*\.(pem|key|p12|pfx|har|webm|log|bak|orig|rej)$|id_(rsa|ed25519|ecdsa)([^/]*)$|[^/]*cookies[^/]*$|[^/]*ffprofile[^/]*$|[^/]*\.parentlock$|[^/]*\.(db|sqlite|sqlite3)(-wal|-shm)?$|docs/verification.*$)'
# 这些文件必须在包里（防止 --exclude 误伤首装/升级所需文件）
RELEASE_REQUIRED=(
    deploy/deploy.sh deploy/deploy-remote.sh deploy/backup.sh
    deploy/easylife.env.example deploy/easylife.service
    deploy/nginx-easylife-location.conf deploy/nginx-easylife.conf
    easylife/app.py easylife/static/app.js
    docs/DEPLOY.md docs/DESIGN.md
)

check_release_archive() {
    local tarball="$1" names bad required
    names="$(tar -tzf "$tarball")" || die "无法读取发布包 $tarball。"
    [ -n "$names" ] || die "发布包为空：$tarball"
    bad="$(printf '%s\n' "$names" | grep -E "$RELEASE_FORBIDDEN_RE" || true)"
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" | sed 's/^/      /' >&2
        die "发布包核对失败：上面这些文件属于运行时凭据/浏览器测试资料，不应进入发布包（检查 --exclude 规则）。"
    fi
    for required in "${RELEASE_REQUIRED[@]}"; do
        printf '%s\n' "$names" | grep -Fxq -- "$required" \
            || die "发布包缺少必需文件：$required（--exclude 规则是否误伤？）"
    done
    info "发布包核对通过：$(printf '%s\n' "$names" | wc -l | tr -d ' ') 个文件；无运行时凭据 / 浏览器测试资料（docs/verification 未打包）"
}

TARBALL="$WORK/easylife-src.tar.gz"
for d in easylife deploy docs; do
    [ -d "$SRC_DIR/$d" ] || die "找不到 $SRC_DIR/$d（请在仓库里运行本脚本）"
done
tar -C "$SRC_DIR" "${RELEASE_EXCLUDES[@]}" -czf "$TARBALL" easylife deploy docs
check_release_archive "$TARBALL"
log "已打包：$(du -h "$TARBALL" | cut -f1)（easylife/ deploy/ docs/；运行时凭据与浏览器测试资料已排除）"

# ---------------------------------------------------------------- 2) 上传
log "上传到 $HOST:$REMOTE_DIR"
rssh "$SUDO mkdir -p $REMOTE_DIR && $SUDO chown \$(id -u) $REMOTE_DIR" \
    || die "无法在服务器上创建 $REMOTE_DIR（SSH 认证失败或权限不足）"
rscp "$TARBALL" "$HOST:$REMOTE_DIR/easylife-src.tar.gz" || die "scp 上传失败"

# ---------------------------------------------------------------- 3) 远程部署
REMOTE_CMD="set -e; cd $REMOTE_DIR && rm -rf easylife deploy docs && tar -xzf easylife-src.tar.gz \
&& ${SUDO:+$SUDO }${EASYLIFE_PYTHON:+env EASYLIFE_PYTHON=$EASYLIFE_PYTHON }bash deploy/deploy.sh --mode path$(quote_args "${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}")"
log "远程执行：deploy.sh --mode path${DEPLOY_ARGS[*]+ ${DEPLOY_ARGS[*]}}"
# 终端里用 -t：需要时（sudo 密码、SMTP 授权码）可以交互输入；
# 非交互（管道 / --log）就不加 -t，避免 ssh 申请不到终端而报错。
if [ -t 0 ] && [ -z "$LOG_FILE" ]; then
    rssh -t "$REMOTE_CMD" || die "远程 deploy.sh 失败（它已自动恢复 nginx 备份）；请查看上面的输出。"
else
    rssh "$REMOTE_CMD" || die "远程 deploy.sh 失败（它已自动恢复 nginx 备份）；请查看上面的输出。"
fi

# ---------------------------------------------------------------- 4) 部署后自检（三条 curl + PASS/FAIL 汇总）
post_deploy_selfcheck
