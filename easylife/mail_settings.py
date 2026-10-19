"""Site SMTP settings: transactional overrides and one fresh resolver for all mail.

Passwords are stored in the service's private SQLite database. Only public_config
may cross the HTTP boundary; never log a settings dictionary or exception text.
Empty non-secret overrides fall back to the startup environment. An empty/null
password preserves it; clear_password removes only the database password.
"""

import email.policy
import email.utils
import json
import re

from easylife import auth, db

KEY = "mail.smtp"
DEFAULT_PORT = 587
DEFAULT_FROM = "no-reply@localhost"
FIELDS = frozenset(("enabled", "host", "port", "user", "password", "from", "clear_password"))


def _pick(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def normalize_config(cfg):
    """Keep the existing SMTP_*/uppercase/lowercase injection API compatible."""
    cfg = cfg or {}
    try:
        port = int(_pick(cfg, "SMTP_PORT", "port", "PORT") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    user = str(_pick(cfg, "SMTP_USER", "user", "USER")).strip()
    password = _pick(cfg, "SMTP_PASS", "password", "pass", "PASS")
    return {
        "host": (str(_pick(cfg, "SMTP_HOST", "host", "HOST")).strip() or None)
                if cfg.get("enabled") is not False else None,
        "port": port,
        "user": user,
        "password": "" if password is None else str(password),
        "from": str(_pick(cfg, "SMTP_FROM", "from_addr", "from", "FROM")).strip()
                or user or DEFAULT_FROM,
    }


def _read(conn):
    row = conn.execute(
        "SELECT s.value, s.updated_at, u.email AS updated_by FROM site_settings s "
        "LEFT JOIN users u ON u.id = s.updated_by WHERE s.key = ?", (KEY,)).fetchone()
    saved = json.loads(row["value"]) if row else {}
    if not isinstance(saved, dict):
        raise ValueError("Invalid stored mail settings")
    return row, saved


def resolve_mail_config(conn, cfg):
    """Resolve each call, without caches. Preserve the editable host when disabled.

    Transport normalization clears that host when enabled=False, so even callers
    passing this complete dictionary to send_message cannot accidentally send.
    """
    row, saved = _read(conn)
    raw_env = cfg.get("smtp") or {}
    env = normalize_config(raw_env)
    host = saved.get("host") or env["host"] or ""
    user = saved.get("user") or env["user"]
    password = saved.get("password") or env["password"]
    env_from = str(_pick(raw_env, "SMTP_FROM", "from_addr", "from", "FROM")).strip()
    return {
        "enabled": saved.get("enabled") is not False and bool(host),
        "host": host,
        "port": saved.get("port") or env["port"],
        "user": user,
        "password": password,
        "from": saved.get("from") or env_from or user or DEFAULT_FROM,
        "source": "database" if row else ("environment" if any(
            value not in (None, "") for value in raw_env.values()) else "defaults"),
        "password_source": ("database" if saved.get("password") else
                            "environment" if env["password"] else "none"),
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
    }


def public_config(effective):
    """Explicit allowlist: never serialize the internal dictionary itself."""
    return {
        "mail": {**{key: effective[key] for key in ("enabled", "host", "port", "user", "from")},
                 "password_set": bool(effective["password"])},
        **{key: effective[key] for key in ("source", "password_source", "updated_at", "updated_by")},
    }


def _string(value, field, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise auth.ValidationError("%s 必须是长度不超过 %d 的字符串" % (field, limit))
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise auth.ValidationError("%s 不允许换行或控制字符" % field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise auth.ValidationError("%s 包含无效字符" % field)
    return value


def _sender(value):
    if not value:
        return
    # parseaddr alone silently accepts trailing junk / multiple mailboxes on
    # Python 3.9. Check the complete header as well, including parser defects.
    try:
        header = email.policy.default.header_factory("From", value)
        _, addr = email.utils.parseaddr(value)
        valid = (not header.defects and len(header.addresses) == 1
                 and header.addresses[0].addr_spec == addr
                 and len(addr) <= 254
                 and re.fullmatch(r"[^\s<>@,;:]+@[^\s<>@,;:]+", addr))
    except Exception:
        # Python 3.9's header parser also raises AttributeError/TypeError on
        # malformed headers (e.g. '.:@'). All parsing failures are bad input.
        valid = False
    if not valid:
        raise auth.ValidationError("from 必须是一个有效的发件邮箱地址")


def validate_patch(body):
    if not isinstance(body, dict) or set(body) - FIELDS:
        raise auth.ValidationError("请求体必须是 JSON 对象，且只能包含邮件设置字段")
    result = dict(body)
    for field in ("enabled", "clear_password"):
        if field in result and type(result[field]) is not bool:
            raise auth.ValidationError("%s 必须是布尔值" % field)
    if "port" in result and (type(result["port"]) is not int or not 1 <= result["port"] <= 65535):
        raise auth.ValidationError("port 必须是 1–65535 的整数")
    for field, limit in (("host", 253), ("user", 254), ("from", 320), ("password", 512)):
        if field not in result or (field == "password" and result[field] is None):
            continue
        _string(result[field], field, limit)
    if "host" in result and any(c.isspace() for c in result["host"]):
        raise auth.ValidationError("host 不允许空白字符")
    if "from" in result:
        _sender(result["from"])
    if result.get("clear_password") and result.get("password"):
        raise auth.ValidationError("清除密码时不能同时设置新密码")
    return result


def save(conn, cfg, body, user_id):
    patch = validate_patch(body)
    # Merge under the same write lock: simultaneous partial updates cannot lose
    # another request's fields or resurrect its cleared password.
    with db.tx(conn):
        _, saved = _read(conn)
        for key, value in patch.items():
            if key in ("password", "clear_password"):
                continue
            if value == "":
                saved.pop(key, None)
            else:
                saved[key] = value
        if patch.get("clear_password"):
            saved.pop("password", None)
        elif patch.get("password"):
            saved["password"] = patch["password"]
        conn.execute(
            "INSERT INTO site_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (KEY, json.dumps(saved, ensure_ascii=True), db.now_utc(), user_id))
        result = public_config(resolve_mail_config(conn, cfg))
    return result


def test_recipient(body, default):
    if not isinstance(body, dict) or set(body) - {"to"}:
        raise auth.ValidationError("测试邮件只接受 to 字段，请先保存邮件设置")
    value = body.get("to", default)
    _string(value, "to", 254)
    return auth.normalize_email(value)
