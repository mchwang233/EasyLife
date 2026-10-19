"""HTTP entry point: config, routing, JSON handling, static files (DESIGN.md §1, §3, §4, §7).

Run with:  EASYLIFE_SECRET=... python3 -m easylife.app
"""

import http.cookies
import json
import logging
import os
import re
import signal
import threading
import time
import traceback
import urllib.parse
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from easylife import (auth, auth_mail, caldav, calendar_ics, db, lists, mail_settings, recurrence,
                      reminders, todos)

log = logging.getLogger("easylife")

MAX_BODY = 256 * 1024
MAX_URL = 2048
COOKIE_NAME = "easylife_session"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                "script-src 'self'; connect-src 'self'; base-uri 'none'; "
                                "form-action 'self'"),
}


# ---------------------------------------------------------------- config

def normalize_base_path(value):
    """'' / '/' -> '' (root); 'todos/' -> '/todos'. Never ends with '/'."""
    value = (value or "").strip().strip("/")
    return "/" + value if value else ""


def public_base_url(url, base_path):
    """Absolute public URL including the base path (DESIGN.md §7).

    PUBLIC_URL may be given with the prefix (https://mch.wang/todos) or without
    it (https://mch.wang); either way the result ends with exactly one prefix.
    """
    url = url.rstrip("/")
    if base_path and not urllib.parse.urlsplit(url).path.endswith(base_path):
        url += base_path
    return url


def load_config(env=None):
    """Read configuration from environment variables only (DESIGN.md §7)."""
    env = os.environ if env is None else env
    port = int(env.get("EASYLIFE_PORT", "8086"))
    base_path = normalize_base_path(env.get("EASYLIFE_BASE_PATH", ""))
    public_url = env.get("PUBLIC_URL") or env.get("BASE_URL") or "http://127.0.0.1:%d" % port
    auth_public_url = (env.get("EASYLIFE_PUBLIC_URL") or "").strip()
    require_verification = env.get("EASYLIFE_REQUIRE_EMAIL_VERIFICATION", "true").strip().lower()
    if require_verification not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise SystemExit("错误：EASYLIFE_REQUIRE_EMAIL_VERIFICATION 必须为 true 或 false。")
    return {
        "host": env.get("EASYLIFE_HOST", "127.0.0.1"),
        "port": port,
        "db": env.get("EASYLIFE_DB", "./easylife.db"),
        "secret": env.get("EASYLIFE_SECRET", ""),
        "base_path": base_path,
        # Every absolute link (feed URL, mail, Google Calendar) is built from this.
        "base_url": public_base_url(public_url, base_path),
        # Empty by default: existing PUBLIC_URL / BASE_URL remain the fallback.
        "public_url": public_base_url(auth_public_url, base_path) if auth_public_url else "",
        "require_email_verification": require_verification in ("true", "1", "yes", "on"),
        "session_ttl": int(env.get("EASYLIFE_SESSION_TTL", str(auth.DEFAULT_SESSION_TTL))),
        "remind_interval": int(env.get("EASYLIFE_REMIND_INTERVAL", "60")),
        # Urgent tasks: minutes between repeated reminders after the due time, and how many.
        "urgent_interval": int(env.get("EASYLIFE_URGENT_INTERVAL", "10")),
        "urgent_max": int(env.get("EASYLIFE_URGENT_MAX", str(todos.URGENT_MAX))),
        "admin_user_ids": parse_admin_user_ids(env.get("EASYLIFE_ADMIN_USER_IDS", "")),
        # Floating / date-only times from CalDAV clients are read in this zone.
        "timezone": env.get("EASYLIFE_TIMEZONE", caldav.DEFAULT_TZ),
        "dav_debug": env.get("EASYLIFE_DAV_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"),
        "smtp": {k: env.get("SMTP_" + k) for k in ("HOST", "PORT", "USER", "PASS", "FROM")},
    }


def parse_admin_user_ids(value):
    ids = set()
    for item in re.split(r"[,\s]+", value.strip()):
        if not item:
            continue
        digits = item.lstrip("0")
        if (not re.fullmatch(r"[0-9]+", item) or not digits or len(digits) > 19
                or int(digits) > 2 ** 63 - 1):
            # Do not reflect raw environment values (or embedded control text).
            log.warning("invalid configured admin user id; disabled")
            continue
        ids.add(int(digits))
    return tuple(sorted(ids))


def existing_admin_user_ids(conn, configured):
    valid = set()
    for user_id in configured:
        if conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
            valid.add(user_id)
        else:
            log.warning("configured admin user id %d does not exist; disabled", user_id)
    return frozenset(valid)


def validate_config(cfg):
    if len(cfg.get("secret") or "") < 16:
        raise SystemExit("错误：EASYLIFE_SECRET 必须设置且至少 16 个字符，拒绝启动。")
    if not re.match(r"^(/[A-Za-z0-9._~-]+)*$", cfg.get("base_path", "")):
        raise SystemExit("错误：EASYLIFE_BASE_PATH 只能是形如 /todos 的路径，拒绝启动。")
    public_url = cfg.get("public_url") or ""
    if public_url:
        parsed = urllib.parse.urlsplit(public_url)
        if (len(public_url) > MAX_URL or parsed.scheme not in ("http", "https")
                or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(c.isspace() for c in public_url)):
            raise SystemExit("错误：EASYLIFE_PUBLIC_URL 必须是无查询参数的完整 http(s) 地址。")


# ---------------------------------------------------------------- routing

class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def bad_request(message):
    return ApiError(400, "bad_request", message)


NOT_FOUND = ("not_found", "资源不存在")


_ERRORS = {
    auth.ValidationError: 400,
    auth.InvalidCredentials: 401,
    auth.EmailTaken: 409,
    auth.EmailNotVerified: 403,
    auth.InvalidToken: 400,
}

VERIFY_MAIL_MESSAGE = "如果该邮箱需要验证，我们已尝试发送验证邮件，请检查收件箱；60 秒后可重新发送。"
RESET_MAIL_MESSAGE = "如果该邮箱已注册，我们已尝试发送重置密码邮件，请检查收件箱；60 秒后可重新发送。"

def _wrap(fn):
    """Translate business-layer exceptions into API errors."""
    def wrapper(handler, **kwargs):
        try:
            return fn(handler, **kwargs)
        except auth.RateLimited as exc:
            raise ApiError(429, exc.code, str(exc), {"Retry-After": str(exc.retry_after)})
        except auth.AuthError as exc:
            raise ApiError(_ERRORS.get(type(exc), 400), exc.code, str(exc))
        except (todos.ValidationError, lists.ListError) as exc:
            raise bad_request(str(exc))
        except calendar_ics.IcsError as exc:
            raise bad_request(str(exc))
    return wrapper


def route(pattern, auth_required=True, raw_body=False, **methods):
    # None means optional session: anonymous recovery is allowed, while a live
    # session still requires the existing session-bound CSRF token on writes.
    wrapped = {m: _wrap(fn) for m, fn in methods.items()}
    for fn in wrapped.values():
        fn.raw_body = raw_body      # the body is passed as bytes (image uploads), not JSON
    return (re.compile("^" + pattern + "$"), auth_required, wrapped)


def _todo_id(match):
    value = int(match.group(1))
    if value > 2 ** 63 - 1:
        raise ApiError(404, *NOT_FOUND)
    return value


TASK_LINK = re.compile(r"^/todos/(\d{1,20})$")


def _is_app_path(path):
    return (path == "/" or path.startswith("/api/") or path.startswith("/static/")
            or caldav.is_dav_path(path) or bool(TASK_LINK.match(path)))


def _found(result):
    if result is None:
        raise ApiError(404, *NOT_FOUND)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "EasyLife"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------- plumbing

    @property
    def cfg(self):
        return self.server.cfg

    def conn(self):
        return db.get_conn(self.cfg["db"])

    def log_message(self, fmt, *args):
        # Never log query strings (they may carry tokens).
        log.info("%s %s", self.address_string(), (fmt % args).split("?", 1)[0])

    def client_ip(self):
        real = self.headers.get("X-Real-IP")
        if real:
            return real.strip()
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def send(self, status, body, content_type, headers=None):
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (headers or {}).items():
            if isinstance(value, list):
                for item in value:
                    self.send_header(name, item)
            else:
                self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload, headers=None):
        headers = dict(headers or {})
        headers.setdefault("Cache-Control", "no-store")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send(status, body, "application/json; charset=utf-8", headers)

    def send_error_json(self, err):
        self.send_json(err.status, {"error": {"code": err.code, "message": err.message}},
                       err.headers)

    def session_cookie(self, token, max_age):
        # Scoped to the base path so other services on the same host never see it.
        path = self.cfg.get("base_path") or "/"
        cookie = "%s=%s; HttpOnly; SameSite=Lax; Path=%s; Max-Age=%d" % (
            COOKIE_NAME, token, path, max_age)
        # EASYLIFE_PUBLIC_URL can be the only configured HTTPS address. Honor
        # both public settings so introducing it never drops the existing flag.
        if any((self.cfg.get(key) or "").lower().startswith("https://")
               for key in ("base_url", "public_url")):
            cookie += "; Secure"
        return cookie

    def session_token(self):
        header = self.headers.get("X-Auth-Token")
        if header:
            return header.strip()
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def read_bytes(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise bad_request("不支持分块传输，请提供 Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise bad_request("Content-Length 无效")
        if length < 0:
            raise bad_request("Content-Length 无效")
        if length > limit:
            raise ApiError(413, "payload_too_large", "文件过大（上限 %d MB）" % (limit // 1024 // 1024))
        return self.rfile.read(length) if length else b""

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise bad_request("不支持分块传输，请提供 Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise bad_request("Content-Length 无效")
        if length < 0:
            raise bad_request("Content-Length 无效")
        if length > MAX_BODY:
            raise ApiError(413, "payload_too_large", "请求体过大（上限 256 KiB）")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise bad_request("Content-Type 必须为 application/json")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise bad_request("JSON 解析失败")

    # -------------------------------------------------- dispatch

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    # WebDAV / CalDAV methods; only the /dav/ tree accepts them.
    do_OPTIONS = do_PROPFIND = do_PROPPATCH = do_REPORT = do_MKCALENDAR = do_MKCOL = do_PATCH

    def dispatch(self):
        self._body_read = False
        path = ""
        try:
            if len(self.path) > MAX_URL:
                raise ApiError(414, "uri_too_long", "URL 过长")
            parsed = urllib.parse.urlsplit(self.path)
            path = self.strip_base_path(parsed.path)
            if path is None:
                # "/todos" -> "/todos/" so relative asset/API URLs resolve under the prefix.
                self.redirect(self.cfg["base_path"] + "/" + ("?" + parsed.query if parsed.query else ""))
                return
            if caldav.is_dav_path(path):
                caldav.handle(self, path)
            elif path.startswith("/api/"):
                self.handle_api(path, parsed.query)
            elif self.command in ("GET", "HEAD"):
                self.handle_static(path)
            else:
                raise ApiError(404, *NOT_FOUND)
        except ApiError as err:
            self.close_if_body_unread()
            self.send_error_json(err)
        except Exception as exc:
            if path.startswith("/api/admin/"):
                # DB/SMTP exception messages can contain the entire secret value.
                log.error("unhandled admin request error=%s", type(exc).__name__)
            else:
                log.error("unhandled error on %s %s\n%s", self.command, self.path.split("?")[0],
                          traceback.format_exc())
            self.close_connection = True
            self.send_error_json(ApiError(500, "internal_error", "服务器内部错误"))
        finally:
            db.close_thread_conns()

    def strip_base_path(self, path):
        """Accept both proxy styles (DESIGN.md §8.5).

        Prefix kept by nginx ("/todos/api/me") -> "/api/me"; prefix already
        stripped ("/api/me") -> unchanged. Returns None for the bare prefix,
        which must be redirected to "<prefix>/".
        """
        base = self.cfg.get("base_path") or ""
        if not base:
            return path
        if path == base:
            return None
        if path.startswith(base + "/"):
            rest = path[len(base):]
            # "/todos/5" behind a stripping proxy is the task link itself, not "/5".
            if _is_app_path(rest):
                return rest
        return path

    def redirect(self, location, status=301):
        self.close_if_body_unread()
        self.send(status, b"", "text/plain; charset=utf-8", {"Location": location})

    def close_if_body_unread(self):
        # If we bail out before consuming the body, don't reuse the connection.
        if self.headers.get("Content-Length") not in (None, "0") and not self._body_read:
            self.close_connection = True

    def handle_api(self, path, query):
        # Guard the whole admin namespace before method/body dispatch, including
        # future routes. A route's auth_required flag cannot weaken this boundary.
        admin_session = None
        if path.startswith("/api/admin/"):
            admin_session = auth.get_session(self.conn(), self.session_token())
            self.require_admin(admin_session)
        for pattern, auth_required, methods in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            method = "GET" if self.command == "HEAD" else self.command
            handler = methods.get(method)
            if handler is None:
                raise ApiError(405, "method_not_allowed", "不支持该请求方法",
                               {"Allow": ", ".join(sorted(methods))})
            session = admin_session
            if auth_required is not False:
                if session is None:
                    session = auth.get_session(self.conn(), self.session_token())
                if session is None and auth_required:
                    raise ApiError(401, "unauthorized", "登录状态已失效，请重新登录")
            if session is not None and method in ("POST", "PUT", "DELETE") and not auth.check_csrf(
                    self.cfg["secret"], session["token"], self.headers.get("X-CSRF-Token")):
                raise ApiError(403, "forbidden", "CSRF 校验失败")
            body = None
            if method in ("POST", "PUT", "DELETE"):
                if getattr(handler, "raw_body", False):
                    body = self.read_bytes(todos.IMAGE_MAX_BYTES)
                else:
                    body = self.read_json()
                self._body_read = True
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            handler(self, match=match, session=session, body=body, params=params)
            return
        raise ApiError(404, *NOT_FOUND)

    def handle_static(self, path):
        task = TASK_LINK.match(path)
        if task:
            # Task links in mails / calendar events point here; the SPA uses hash routes.
            self.redirect("%s/#/todos/%s" % (self.cfg.get("base_path") or "", task.group(1)), 302)
            return
        if path == "/":
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        else:
            raise ApiError(404, *NOT_FOUND)
        rel = urllib.parse.unquote(rel)
        if not re.match(r"^[A-Za-z0-9_\-][A-Za-z0-9_.\-]*(/[A-Za-z0-9_\-][A-Za-z0-9_.\-]*)*$", rel):
            raise ApiError(404, *NOT_FOUND)
        root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        ext = os.path.splitext(full)[1].lower()
        if not full.startswith(root + os.sep) or ext not in STATIC_TYPES or not os.path.isfile(full):
            raise ApiError(404, *NOT_FOUND)
        with open(full, "rb") as fh:
            body = fh.read()
        self.send(200, body, STATIC_TYPES[ext], {"Cache-Control": "no-cache"})

    # -------------------------------------------------- API handlers

    def requires_verification(self, mail_config=None):
        effective = (mail_settings.resolve_mail_config(self.conn(), self.cfg)
                     if mail_config is None else mail_config)
        return (self.cfg.get("require_email_verification", True)
                and effective["enabled"])

    def with_admin(self, user):
        return {**user, "is_admin": user["id"] in self.server.admin_user_ids}

    def require_admin(self, session):
        if session is None:
            raise ApiError(401, "unauthorized", "登录状态已失效，请重新登录")
        if session["id"] not in self.server.admin_user_ids:
            raise ApiError(403, "forbidden", "需要管理员权限")

    def api_get_mail_settings(self, **_):
        self.send_json(200, mail_settings.public_config(
            mail_settings.resolve_mail_config(self.conn(), self.cfg)))

    def api_put_mail_settings(self, session, body, **_):
        result = mail_settings.save(self.conn(), self.cfg, body, session["id"])
        self.send_json(200, result)

    def api_test_mail_settings(self, session, body, **_):
        recipient = mail_settings.test_recipient(body, session["email"])
        self.server.limiter.hit_mail_test(session["id"])
        effective = mail_settings.resolve_mail_config(self.conn(), self.cfg)
        if not effective["enabled"]:
            raise ApiError(409, "mail_disabled", "邮件服务未配置或已停用，请先保存有效配置")
        started = time.monotonic()
        try:
            msg = EmailMessage()
            msg["Subject"] = "[EasyLife] 邮件配置测试"
            msg["From"] = effective["from"]
            msg["To"] = recipient
            msg.set_content("这是一封 EasyLife 全站邮件配置测试邮件。")
            result = reminders.send_message(effective, msg)
        except Exception as exc:
            log.error("mail settings test failed error=%s", type(exc).__name__)
            raise ApiError(502, "mail_delivery_failed", "邮件服务器连接、认证或投递失败，请检查已保存配置")
        if result != "sent":
            raise ApiError(502, "mail_delivery_failed", "邮件未被 SMTP 接受，请检查已保存配置")
        self.send_json(200, {
            "ok": True, "sent": True, "to": recipient,
            "message": "SMTP 服务器已接受测试邮件，请检查收件箱；这不代表邮件已送达。",
            "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
        })

    def login_response(self, status, user, token, expires_at):
        self.send_json(status, {
            "user": self.with_admin(user),
            "csrf_token": auth.csrf_token(self.cfg["secret"], token),
            "expires_at": expires_at,
        }, {"Set-Cookie": self.session_cookie(token, self.cfg["session_ttl"])})

    def api_register(self, body, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        effective = mail_settings.resolve_mail_config(self.conn(), self.cfg)
        user, token, expires_at = auth.register(
            self.conn(), body.get("email"), body.get("password"),
            ip=self.client_ip(), limiter=self.server.limiter, ttl=self.cfg["session_ttl"],
            require_verification=self.requires_verification(effective))
        log.info("registered %s", auth.mask_email(user["email"]))
        try:
            self.server.limiter.hit_mail("verify", user["email"], self.client_ip())
            delivery = auth_mail.send(self.conn(), user, "verify", self.cfg, mail_config=effective)
        except auth.RateLimited:
            # The account has already been created; do not return a misleading
            # registration failure if a preceding anonymous resend used the slot.
            delivery = "rate_limited"
        if token is None:
            message = ("注册成功，请查看验证邮件并验证邮箱后登录。"
                       if delivery == "sent" else "注册成功，验证邮件暂未发送，请 60 秒后重新发送。")
            self.send_json(201, {"user": self.with_admin(user), "verification_required": True,
                                 "message": message})
            return
        self.login_response(201, user, token, expires_at)

    def api_login(self, body, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        user, token, expires_at = auth.login(
            self.conn(), body.get("email"), body.get("password"),
            ip=self.client_ip(), limiter=self.server.limiter, ttl=self.cfg["session_ttl"],
            require_verification=self.requires_verification())
        self.login_response(200, user, token, expires_at)

    def api_verify_email(self, body, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        auth.verify_email(self.conn(), body.get("token"))
        self.send_json(200, {"ok": True, "message": "邮箱验证成功，请登录。"})

    def api_resend_verification(self, body, session, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        email = auth.normalize_email(session["email"] if session else body.get("email"))
        self.server.limiter.hit_mail("verify", email, self.client_ip())
        user = auth.user_by_email(self.conn(), email)
        if user is not None and not user["email_verified_at"]:
            auth_mail.send(self.conn(), user, "verify", self.cfg)
        self.send_json(200, {"ok": True, "message": VERIFY_MAIL_MESSAGE})

    def api_forgot_password(self, body, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        email = auth.normalize_email(body.get("email"))
        # Charge every submitted address before lookup so both 200 and 429 are
        # independent of whether there is an account (and of SMTP delivery).
        self.server.limiter.hit_mail("reset", email, self.client_ip())
        user = auth.user_by_email(self.conn(), email)
        if user is not None:
            auth_mail.send(self.conn(), user, "reset", self.cfg)
        self.send_json(200, {"ok": True, "message": RESET_MAIL_MESSAGE})

    def api_reset_password(self, body, **_):
        if not isinstance(body, dict):
            raise bad_request("请求体必须是 JSON 对象")
        auth.reset_password(self.conn(), body.get("token"), body.get("password"))
        self.send_json(200, {"ok": True, "message": "密码已重置，请使用新密码重新登录。"},
                       {"Set-Cookie": self.session_cookie("", 0)})

    def api_logout(self, session, **_):
        auth.delete_session(self.conn(), session["token"])
        self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", 0)})

    def api_me(self, session, **_):
        conn = self.conn()
        token = calendar_ics.get_or_create_token(conn, session["id"])
        self.send_json(200, {
            "user": self.with_admin(auth.public_user(session)),
            "csrf_token": auth.csrf_token(self.cfg["secret"], session["token"]),
            "calendar_token": token,
            "calendar_feed_url": calendar_ics.feed_url(self.cfg["base_url"], token),
            "dav": caldav.info(conn, session, self.cfg["base_url"]),
            "server_time": db.now_utc(),
        })

    def api_create_dav_password(self, session, **_):
        """New CalDAV sync password, shown once; replaces (and invalidates) the old one."""
        conn = self.conn()
        password = caldav.set_password(conn, session["id"])
        self.send_json(200, {**caldav.info(conn, session, self.cfg["base_url"]),
                             "password": password})

    def api_revoke_dav_password(self, session, **_):
        conn = self.conn()
        caldav.revoke(conn, session["id"])
        self.send_json(200, caldav.info(conn, session, self.cfg["base_url"]))

    def api_reset_calendar_token(self, session, **_):
        """Rotate the subscription token; old feed URLs stop working."""
        token = calendar_ics.reset_token(self.conn(), session["id"])
        if token is None:
            raise ApiError(404, *NOT_FOUND)
        self.send_json(200, {
            "calendar_token": token,
            "calendar_feed_url": calendar_ics.feed_url(self.cfg["base_url"], token),
        })

    def api_lists(self, session, **_):
        self.send_json(200, {"items": lists.all_lists(self.conn(), session["id"], counts=True)})

    def api_create_list(self, session, body, **_):
        body = body if isinstance(body, dict) else {}
        self.send_json(201, lists.create_list(self.conn(), session["id"], body.get("name"),
                                              body.get("color")))

    def api_update_list(self, session, match, body, **_):
        body = body if isinstance(body, dict) else {}
        self.send_json(200, _found(lists.update_list(self.conn(), session["id"], _todo_id(match),
                                                     body.get("name"), body.get("color"))))

    def api_delete_list(self, session, match, **_):
        moved = _found(lists.delete_list(self.conn(), session["id"], _todo_id(match)))
        self.send_json(200, {"ok": True, "moved": moved})

    def api_list_todos(self, session, params, **_):
        self.send_json(200, todos.list_todos(self.conn(), session["id"], params,
                                             self.cfg["base_url"]))

    def api_create_todo(self, session, body, **_):
        self.send_json(201, todos.create_todo(self.conn(), session["id"], body,
                                              self.cfg["base_url"]))

    def api_get_todo(self, session, match, **_):
        self.send_json(200, _found(todos.get_todo(self.conn(), session["id"], _todo_id(match),
                                                  self.cfg["base_url"])))

    def api_update_todo(self, session, match, body, **_):
        todo_id = _todo_id(match)
        self.send_json(200, _found(todos.update_todo(self.conn(), session["id"], todo_id, body,
                                                     self.cfg["base_url"])))

    def api_stop_alerts(self, session, match, **_):
        self.send_json(200, _found(todos.stop_alerts(self.conn(), session["id"], _todo_id(match),
                                                     self.cfg["base_url"])))

    def api_add_image(self, session, match, body, **_):
        image = todos.add_image(self.conn(), session["id"], _todo_id(match), body or b"")
        self.send_json(201, _found(image))

    def api_get_image(self, session, match, **_):
        mime, data = _found(todos.get_image(self.conn(), session["id"], _todo_id(match)))
        # Image ids are never reused, so the browser may keep its copy.
        self.send(200, data, mime, {"Cache-Control": "private, max-age=31536000, immutable",
                                    "Content-Disposition": "inline"})

    def api_delete_image(self, session, match, **_):
        if not todos.delete_image(self.conn(), session["id"], _todo_id(match)):
            raise ApiError(404, *NOT_FOUND)
        self.send_json(200, {"ok": True})

    def api_todo_ics(self, session, match, **_):
        """Single-todo .ics download (design §3.8)."""
        todo_id = _todo_id(match)
        todo = _found(todos.get_todo(self.conn(), session["id"], todo_id))
        body = calendar_ics.todo_to_ics(todo, base_url=self.cfg["base_url"]).encode("utf-8")
        headers = calendar_ics.feed_headers("easylife-%d.ics" % todo_id)
        headers["Cache-Control"] = "no-store"
        self.send(200, body, "text/calendar; charset=utf-8", headers)

    def api_calendar_feed(self, params, **_):
        """Public (token authenticated) subscription feed (design §3.9)."""
        token = (params.get("token") or "").strip()
        if not token:
            raise bad_request("缺少订阅 token")
        conn = self.conn()
        user_id = calendar_ics.user_id_for_token(conn, token)
        if user_id is None:
            log.warning("calendar feed: invalid token rejected")
            raise ApiError(403, "forbidden", "订阅 token 无效")
        rows = todos.list_todos(conn, user_id, {"status": "all", "limit": 500},
                                self.cfg["base_url"])["items"]
        body = calendar_ics.calendar_feed(rows, base_url=self.cfg["base_url"]).encode("utf-8")
        self.send(200, body, "text/calendar; charset=utf-8",
                  calendar_ics.feed_headers())

    def api_delete_todo(self, session, match, **_):
        todo_id = _todo_id(match)
        if not todos.delete_todo(self.conn(), session["id"], todo_id):
            raise ApiError(404, *NOT_FOUND)
        self.send_json(200, {"ok": True, "deleted_id": todo_id})

    def api_list_notes(self, session, match, **_):
        self.send_json(200, _found(todos.get_notes(self.conn(), session["id"], _todo_id(match))))

    def api_add_note(self, session, match, body, **_):
        note = todos.add_note(self.conn(), session["id"], _todo_id(match), body)
        self.send_json(201, _found(note))


ROUTES = [
    route("/api/register", auth_required=False, POST=Handler.api_register),
    route("/api/login", auth_required=False, POST=Handler.api_login),
    route("/api/verify-email", auth_required=None, POST=Handler.api_verify_email),
    route("/api/resend-verification", auth_required=None, POST=Handler.api_resend_verification),
    route("/api/forgot-password", auth_required=None, POST=Handler.api_forgot_password),
    route("/api/reset-password", auth_required=None, POST=Handler.api_reset_password),
    route("/api/logout", POST=Handler.api_logout),
    route("/api/me", GET=Handler.api_me),
    route("/api/admin/mail-settings", GET=Handler.api_get_mail_settings, PUT=Handler.api_put_mail_settings),
    route("/api/admin/mail-settings/test", POST=Handler.api_test_mail_settings),
    # Token-authenticated, therefore no session and no CSRF check (design §3.9).
    route(r"/api/calendar\.ics", auth_required=False, GET=Handler.api_calendar_feed),
    route("/api/calendar/token/reset", POST=Handler.api_reset_calendar_token),
    route("/api/dav/password", POST=Handler.api_create_dav_password,
          DELETE=Handler.api_revoke_dav_password),
    route("/api/lists", GET=Handler.api_lists, POST=Handler.api_create_list),
    route(r"/api/lists/(\d{1,20})", PUT=Handler.api_update_list, DELETE=Handler.api_delete_list),
    route("/api/todos", GET=Handler.api_list_todos, POST=Handler.api_create_todo),
    route(r"/api/todos/(\d{1,20})", GET=Handler.api_get_todo, PUT=Handler.api_update_todo,
          DELETE=Handler.api_delete_todo),
    route(r"/api/todos/(\d{1,20})/ics", GET=Handler.api_todo_ics),
    route(r"/api/todos/(\d{1,20})/notes", GET=Handler.api_list_notes, POST=Handler.api_add_note),
    route(r"/api/todos/(\d{1,20})/stop-alerts", POST=Handler.api_stop_alerts),
    route(r"/api/todos/(\d{1,20})/images", raw_body=True, POST=Handler.api_add_image),
    route(r"/api/images/(\d{1,20})", GET=Handler.api_get_image, DELETE=Handler.api_delete_image),
]

# ---------------------------------------------------------------- server

class EasyLifeServer(ThreadingHTTPServer):
    daemon_threads = True
    stop_event = None

    def __init__(self, cfg):
        validate_config(cfg)
        self.cfg = cfg
        recurrence.set_zone(caldav.default_zone(cfg))
        todos.URGENT_INTERVAL = max(1, cfg.get("urgent_interval", 10)) * 60
        todos.URGENT_MAX = max(1, cfg.get("urgent_max", todos.URGENT_MAX))
        self.limiter = auth.LoginRateLimiter()
        # Separate counters: a phone retrying an old sync password must not lock web login.
        self.dav_limiter = auth.LoginRateLimiter(email_limit=10, ip_limit=30)
        db.init_db(cfg["db"])
        conn = db.connect(cfg["db"])
        try:
            self.admin_user_ids = existing_admin_user_ids(conn, cfg.get("admin_user_ids", ()))
            self.smtp_enabled_at_start = mail_settings.resolve_mail_config(conn, cfg)["enabled"]
        finally:
            conn.close()
        if not self.smtp_enabled_at_start:
            log.warning("SMTP disabled: 认证邮件只写服务器日志，未验证邮箱不会阻止登录。")
        super().__init__((cfg["host"], cfg["port"]), Handler)

    def start_reminders(self, sender=None):
        """Start the daemon reminder thread (design §5.1). Returned by main()."""
        self.stop_event = reminders.start_thread(self.cfg, sender=sender)
        return self.stop_event

    def stop_reminders(self):
        if self.stop_event is not None:
            self.stop_event.set()


def make_server(cfg):
    return EasyLifeServer(cfg)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    server = make_server(cfg)
    log.info("EasyLife listening on http://%s:%d db=%s base_path=%s base_url=%s smtp=%s",
             cfg["host"], server.server_address[1], cfg["db"], cfg["base_path"] or "/",
             cfg["base_url"],
             "enabled" if server.smtp_enabled_at_start else "disabled (log only)")
    log.info("reminder interval=%ss, calendar subscription feed at %s/api/calendar.ics",
             cfg["remind_interval"], cfg["base_url"])
    server.start_reminders()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.stop_reminders()
        server.server_close()


if __name__ == "__main__":
    main()
