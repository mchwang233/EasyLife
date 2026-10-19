#!/usr/bin/env bash
# =============================================================================
#  EasyLife 数据备份脚本（备份 SQLite 数据库）
#
#  用法：
#     sudo bash deploy/backup.sh                          # 默认备份 /var/lib/easylife/easylife.db
#     bash deploy/backup.sh --db /path/to/easylife.db --dir ~/backups
#     bash deploy/backup.sh --keep 30 --gzip              # 保留最近 30 份并压缩
#
#  特点：
#     * 使用 Python 标准库 sqlite3 的在线备份 API（等价于 sqlite3 CLI 的 .backup），
#       在 WAL 模式下也是安全的、一致性快照，不需要停服；
#     * 备份完成后做 PRAGMA integrity_check，非 ok 则删除该文件并以非 0 退出；
#     * 先写临时文件再原子改名，避免出现"半截"的备份文件；
#     * 自动清理旧备份（默认保留最近 14 份，--keep 0 表示不清理）；
#     * 不写任何密码/密钥，数据库文件权限置为 600。
#     * 快照包含数据库里的全部数据：用户、待办、会话表、以及**网页端保存的 SMTP 设置
#       （site_settings，可能含密码）**；因此脚本一开始就 umask 0077——目录 0700、
#       备份文件与中途的 .part 临时文件都从创建那一刻起就是 0600，不会出现"先 0644 后 chmod"的窗口。
#
#  建议放进 crontab（见 docs/DEPLOY.md §9）：
#     17 3 * * * /usr/bin/bash /opt/easylife/deploy/backup.sh --keep 30 >/dev/null 2>&1
# =============================================================================
set -Eeuo pipefail

# 备份目录/备份文件/中途临时文件一律从创建起就是私有权限（数据库可能含网页端 SMTP 密码）。
umask 0077

PROG="$(basename "$0")"
DB_PATH="${EASYLIFE_DB:-/var/lib/easylife/easylife.db}"
BACKUP_DIR="/var/backups/easylife"
KEEP=14
USE_GZIP=0
QUIET=0

log()  { [ "$QUIET" -eq 1 ] || printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<USAGE
$PROG —— 备份 EasyLife 的 SQLite 数据库

用法： bash $PROG [选项]

快照含全部用户/待办/会话/网页邮件设置（site_settings，可能含 SMTP 密码），按密件处理：
输出目录 0700、备份文件 0600（从创建起即私有）。恢复默认手动进行；升级前的快照可用
deploy.sh --rollback --restore-db 显式换回。

选项：
  --db <路径>     数据库文件（默认 \$EASYLIFE_DB 或 /var/lib/easylife/easylife.db）
  --dir <目录>    备份输出目录（默认 /var/backups/easylife）
  --keep <数量>   保留最近 N 份备份，0 表示不清理（默认 14）
  --gzip          压缩备份（.db.gz；恢复时用 gunzip 解开即可）
  --quiet         安静模式，只输出错误
  -h, --help      显示本帮助
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --db) DB_PATH="${2:?--db 需要参数}"; shift 2 ;;
        --dir) BACKUP_DIR="${2:?--dir 需要参数}"; shift 2 ;;
        --keep) KEEP="${2:?--keep 需要参数}"; shift 2 ;;
        --gzip) USE_GZIP=1; shift ;;
        --quiet) QUIET=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 查看用法）" ;;
    esac
done

case "$KEEP" in ''|*[!0-9]*) die "--keep 必须是非负整数：$KEEP" ;; esac

# ---------------------------------------------------------------- 检查
[ -f "$DB_PATH" ] || die "找不到数据库文件：$DB_PATH（若应用还没跑过，先启动服务生成它，或用 --db 指定路径）"
[ -r "$DB_PATH" ] || die "没有读取权限：$DB_PATH（备份请以 root 运行：sudo bash $PROG）"

# 解释器优先用 EASYLIFE_PYTHON：deploy.sh 会把它已校验过的（>=3.8）解释器传进来。
# 系统自带的 python3 可能太旧（例如 CentOS 7 的 3.6），其 sqlite3 没有在线备份 API。
PY_BIN=""
for c in "${EASYLIFE_PYTHON:-}" python3 /usr/bin/python3 /usr/local/bin/python3; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1; then PY_BIN="$(command -v "$c")"; break; fi
done
[ -n "$PY_BIN" ] || die "未找到 python3（备份依赖标准库 sqlite3）。"
"$PY_BIN" -c 'import sqlite3' 2>/dev/null || die "$PY_BIN 缺少 sqlite3 模块。"

# sqlite3.Connection.backup 需要 Python 3.7+；旧解释器退回 sqlite3 命令行的 .backup（同样是安全快照）。
if "$PY_BIN" -c 'import sqlite3; sqlite3.Connection.backup' 2>/dev/null; then
    HAS_PY_BACKUP=1
else
    HAS_PY_BACKUP=0
fi

mkdir -p "$BACKUP_DIR" || die "无法创建备份目录：$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

# ---------------------------------------------------------------- 备份
STAMP="$(date +%Y%m%d-%H%M%S)"
[ "$USE_GZIP" -eq 1 ] && OUT="$BACKUP_DIR/easylife-$STAMP.db.gz" || OUT="$BACKUP_DIR/easylife-$STAMP.db"
TMP="$OUT.part"

cleanup() { rm -f "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

if [ "$HAS_PY_BACKUP" -eq 1 ]; then
snapshot="$("$PY_BIN" - "$DB_PATH" "$TMP" <<'PY' || die "SQLite 在线备份失败（原始文件未被改动）。"
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]

# 只读方式打开源库：备份过程绝不修改生产数据
src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
    dst.commit()
    row = dst.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        sys.stderr.write("integrity_check 未通过：%r\n" % (row,))
        raise SystemExit(2)
    count = dst.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
except Exception as exc:                        # noqa: BLE001 - 统一报错退出
    sys.stderr.write("备份失败：%s\n" % exc)
    raise SystemExit(1)
finally:
    dst.close()
    src.close()
print("tables=%s" % count)
PY
)"
else
    # 旧解释器（如 CentOS 7 的 Python 3.6）没有 sqlite3.Connection.backup：
    # 改用 sqlite3 命令行的 .backup —— 同样是 SQLite 官方的一致性在线快照。
    command -v sqlite3 >/dev/null 2>&1 || die "$PY_BIN 的 sqlite3 不支持在线备份 API（需要 Python >= 3.7），
    且找不到 sqlite3 命令行工具。请安装（yum install -y sqlite）或把 EASYLIFE_PYTHON 指向较新的解释器后重试。"
    rm -f "$TMP"
    if ! cli_err="$(sqlite3 "$DB_PATH" ".backup '$TMP'" 2>&1)"; then
        [ -n "$cli_err" ] && printf '%s\n' "$cli_err" >&2
        die "SQLite 在线备份失败（原始文件未被改动）。"
    fi
    [ -s "$TMP" ] || die "SQLite 在线备份失败（原始文件未被改动）。"
    if [ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;' 2>&1)" != "ok" ]; then
        die "备份文件 integrity_check 未通过，已丢弃：$TMP"
    fi
    snapshot="tables=$(sqlite3 "$TMP" "SELECT count(*) FROM sqlite_master WHERE type='table';")"
fi
[ "$QUIET" -eq 1 ] || log "SQLite 在线快照完成（$snapshot）"

if [ "$USE_GZIP" -eq 1 ]; then
    if command -v gzip >/dev/null 2>&1; then
        gzip -c "$TMP" > "$OUT.tmp" && mv -f "$OUT.tmp" "$OUT" && rm -f "$TMP"
    else
        "$PY_BIN" -c 'import gzip,shutil,sys; shutil.copyfileobj(open(sys.argv[1],"rb"), gzip.open(sys.argv[2],"wb"))' "$TMP" "$OUT.tmp" \
            && mv -f "$OUT.tmp" "$OUT" && rm -f "$TMP"
    fi
else
    mv -f "$TMP" "$OUT"
fi

chmod 600 "$OUT" 2>/dev/null || true
[ -s "$OUT" ] || die "备份文件为空：$OUT"
log "已备份：$OUT（$(du -h "$OUT" | cut -f1)）"

# ---------------------------------------------------------------- 清理旧备份
if [ "$KEEP" -gt 0 ]; then
    # 按文件名里的时间戳排序，保留最新 $KEEP 个，其余删除
    mapfile -t old < <(ls -1t "$BACKUP_DIR"/easylife-*.db "$BACKUP_DIR"/easylife-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" || true)
    for f in "${old[@]:-}"; do
        [ -n "$f" ] || continue
        rm -f "$f" && log "清理旧备份：$f"
    done
fi

log "完成。恢复方式见 docs/DEPLOY.md §9（systemctl stop easylife → 覆盖 easylife.db → 改属主 → 启动），"
log "     或用升级前快照显式恢复： sudo bash deploy/deploy.sh --rollback --restore-db（数据恢复只能显式进行）。"
