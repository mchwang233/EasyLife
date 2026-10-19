"""Registration, login, sessions, password hashing, rate limiting, CSRF (DESIGN.md §3.1-3.4, §4)."""

import base64
import datetime
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import threading
import time

from easylife import db

PBKDF2_ITERATIONS = 260000
SALT_BYTES = 16
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
DEFAULT_SESSION_TTL = 30 * 24 * 3600
VERIFY_TOKEN_TTL = 24 * 3600
RESET_TOKEN_TTL = 3600
MAIL_INTERVAL = 60          # one verification / reset mail per address per minute
MAIL_TEST_WINDOW = 600
MAX_TOKEN_LENGTH = 128
# secrets.token_urlsafe(32): 256 random bits encoded without padding.
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
# Pragmatic shape check: something@domain.tld, no whitespace, a single "@".
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")


class AuthError(Exception):
    """Base class; `code` maps onto the API error codes."""
    code = "bad_request"


class ValidationError(AuthError):
    code = "bad_request"


class EmailTaken(AuthError):
    code = "conflict"


class InvalidCredentials(AuthError):
    code = "unauthorized"


class EmailNotVerified(AuthError):
    code = "email_not_verified"


class InvalidToken(AuthError):
    code = "invalid_token"


class RateLimited(AuthError):
    code = "rate_limited"

    def __init__(self, retry_after):
        super().__init__("请求过于频繁，请稍后再试")
        self.retry_after = max(1, math.ceil(retry_after))


# ---------------------------------------------------------------- passwords

def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, _b64(salt), _b64(digest))


def verify_password(password, stored):
    """Constant-time check against a self-describing hash (pbkdf2_sha256 or scrypt)."""
    try:
        parts = stored.split("$")
        if parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            salt, expected = _unb64(parts[2]), _unb64(parts[3])
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                         int(parts[1]), dklen=len(expected))
        elif parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, expected = _unb64(parts[4]), _unb64(parts[5])
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                    dklen=len(expected))
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email is unknown so both login paths cost the same.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- validation

def mask_email(email):
    local, _, domain = (email or "").partition("@")
    return (local[:1] + "***@" + domain) if domain else "***"


def normalize_email(email):
    if not isinstance(email, str) or len(email) > 254:
        raise ValidationError("请输入有效的邮箱地址")
    email = email.strip().lower()
    local, at, domain = email.partition("@")
    if not at or not local or not domain or len(email) > 254 or any(c.isspace() for c in email):
        raise ValidationError("请输入有效的邮箱地址")
    if (len(local) > 64 or not EMAIL_RE.fullmatch(email)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or any(len(label) > 63 for label in domain.split("."))):
        raise ValidationError("请输入有效的邮箱地址")
    return email


def validate_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValidationError("密码长度需为 8-128 个字符")
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        # JSON may contain lone surrogate escapes, which cannot be hashed as UTF-8.
        raise ValidationError("密码包含无效字符")
    return password


def public_user(row):
    user = {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}
    if "email_verified_at" in row.keys():
        user["email_verified"] = bool(row["email_verified_at"])
    return user


# ---------------------------------------------------------------- rate limit

class LoginRateLimiter:
    """In-process sliding-window counters (DESIGN.md §4.5)."""

    def __init__(self, email_limit=5, ip_limit=20, window=15 * 60,
                 register_limit=10, register_window=3600):
        self.email_limit = email_limit
        self.ip_limit = ip_limit
        self.window = window
        self.register_limit = register_limit
        self.register_window = register_window
        self._lock = threading.Lock()
        self._hits = {}  # key -> list of monotonic timestamps

    def _recent(self, key, window, now):
        hits = [t for t in self._hits.get(key, ()) if now - t < window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def _check(self, key, limit, window, now):
        hits = self._recent(key, window, now)
        if len(hits) >= limit:
            raise RateLimited(window - (now - hits[0]))

    def _window(self, kind):
        return {"reg": self.register_window, "mail": MAIL_INTERVAL,
                "mail-ip": self.register_window, "mail-test": MAIL_TEST_WINDOW}.get(kind, self.window)

    def _sweep(self, now):
        # Lazily drop expired keys so memory does not grow without bound.
        if len(self._hits) > 1000:
            for key in list(self._hits):
                self._recent(key, self._window(key[0]), now)

    def check_login(self, email, ip):
        now = time.monotonic()
        with self._lock:
            self._check(("email", email), self.email_limit, self.window, now)
            self._check(("ip", ip), self.ip_limit, self.window, now)

    def record_failure(self, email, ip):
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._hits.setdefault(("email", email), []).append(now)
            self._hits.setdefault(("ip", ip), []).append(now)

    def record_success(self, email):
        with self._lock:
            self._hits.pop(("email", email), None)

    def hit_register(self, ip):
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._check(("reg", ip), self.register_limit, self.register_window, now)
            self._hits.setdefault(("reg", ip), []).append(now)

    def hit_mail(self, purpose, email, ip, ip_limit=20):
        """Allow one `purpose` mail per address per MAIL_INTERVAL seconds.

        Counted on the submitted address whether or not an account exists, so a
        429 never reveals which addresses are registered. A per-IP cap stops one
        client from spraying mails at many addresses.
        """
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._check(("mail", purpose, email), 1, MAIL_INTERVAL, now)
            self._check(("mail-ip", ip), ip_limit, self.register_window, now)
            self._hits.setdefault(("mail", purpose, email), []).append(now)
            self._hits.setdefault(("mail-ip", ip), []).append(now)

    def hit_mail_test(self, user_id):
        """Five attempts per administrator per ten minutes, across sessions/IPs."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._check(("mail-test", user_id), 5, MAIL_TEST_WINDOW, now)
            self._hits.setdefault(("mail-test", user_id), []).append(now)


# ---------------------------------------------------------------- sessions

def create_session(conn, user_id, ttl=DEFAULT_SESSION_TTL, *, password_hash=None):
    token = secrets.token_urlsafe(32)
    expires_at = db.now_utc(ttl)
    with db.tx(conn):
        # A concurrent password reset may finish after login checked the old
        # password, or registration inserted the account. Recheck under the same
        # write lock used to insert the session.
        if password_hash is not None:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or row["password_hash"] != password_hash:
                raise InvalidCredentials("邮箱或密码错误")
        conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (token, user_id, expires_at))
    return token, expires_at


def get_session(conn, token):
    """Return the user row for a live session token, or None."""
    if not token or not isinstance(token, str) or len(token) > 128:
        return None
    return conn.execute(
        "SELECT u.id, u.email, u.created_at, u.email_verified_at, s.token, s.expires_at "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, db.now_utc()),
    ).fetchone()


def delete_session(conn, token):
    with db.tx(conn):
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_user_sessions(conn, user_id):
    with db.tx(conn):
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions(conn):
    with db.tx(conn):
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (db.now_utc(),))
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (db.now_utc(-7 * 86400),))


# ---------------------------------------------------------------- one-time tokens

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn, user_id, purpose, ttl):
    """Create a one-time token; only its SHA-256 is stored. Returns the plain token.

    Older unused tokens of the same purpose are retired, so only the newest mail works.
    """
    token = secrets.token_urlsafe(32)
    with db.tx(conn):
        # Take one timestamp after acquiring the lock: crossing a second (or
        # waiting for another writer) must not extend the token's stated TTL.
        now = db.now_utc()
        expires_at = (datetime.datetime.strptime(now, db.TIME_FORMAT)
                      + datetime.timedelta(seconds=ttl)).strftime(db.TIME_FORMAT)
        conn.execute("UPDATE auth_tokens SET used_at = ? "
                     "WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
                     (now, user_id, purpose))
        conn.execute("INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (user_id, purpose, hash_token(token), expires_at, now))
    return token


def consume_token(conn, token, purpose):
    """Atomically mark a live token as used and return its user id.

    Unknown, forged, expired, already-used or wrong-purpose tokens all raise
    the same InvalidToken.
    """
    with db.tx(conn):
        return _consume_token(conn, token, purpose)


def _consume_token(conn, token, purpose):
    """Consume inside the caller's transaction, including its account changes."""
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_LENGTH:
        raise InvalidToken("链接无效或已过期，请重新获取")
    token = token.strip()
    if not TOKEN_RE.fullmatch(token):
        raise InvalidToken("链接无效或已过期，请重新获取")
    token_hash = hash_token(token)
    now = db.now_utc()
    row = conn.execute(
        "SELECT id, user_id FROM auth_tokens WHERE token_hash = ? AND purpose = ? "
        "AND used_at IS NULL AND expires_at > ?", (token_hash, purpose, now)).fetchone()
    if row is None:
        raise InvalidToken("链接无效或已过期，请重新获取")
    conn.execute("UPDATE auth_tokens SET used_at = ? WHERE id = ?", (now, row["id"]))
    return row["user_id"]


def user_by_email(conn, email):
    return conn.execute("SELECT id, email, created_at, email_verified_at FROM users "
                        "WHERE email = ?", (email,)).fetchone()


def user_by_id(conn, user_id):
    return conn.execute("SELECT id, email, created_at, email_verified_at FROM users "
                        "WHERE id = ?", (user_id,)).fetchone()


def verify_email(conn, token):
    """Consume a verify token and mark the address verified. Returns the user row."""
    with db.tx(conn):
        user_id = _consume_token(conn, token, "verify")
        conn.execute("UPDATE users SET email_verified_at = ? "
                     "WHERE id = ? AND email_verified_at IS NULL", (db.now_utc(), user_id))
    return user_by_id(conn, user_id)


def reset_password(conn, token, password):
    """Set a new password from a reset token and end every existing session.

    The password is validated before the token is consumed, so a rejected
    password does not burn the link. Proving control of the mailbox also
    counts as verifying the address.
    """
    validate_password(password)
    password_hash = hash_password(password)
    now = db.now_utc()
    with db.tx(conn):
        user_id = _consume_token(conn, token, "reset")
        conn.execute("UPDATE users SET password_hash = ?, "
                     "email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
                     (password_hash, now, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE auth_tokens SET used_at = ? "
                     "WHERE user_id = ? AND purpose = 'reset' AND used_at IS NULL",
                     (now, user_id))
    return user_by_id(conn, user_id)


def csrf_token(secret, session_token):
    """CSRF token bound to the session; derived so no extra storage is needed."""
    mac = hmac.new(secret.encode("utf-8"), b"csrf:" + session_token.encode("utf-8"), hashlib.sha256)
    return _b64(mac.digest())[:32]


def check_csrf(secret, session_token, supplied):
    if not supplied or not isinstance(supplied, str):
        return False
    return hmac.compare_digest(csrf_token(secret, session_token).encode(), supplied.encode())


# ---------------------------------------------------------------- flows

def register(conn, email, password, *, ip, limiter, ttl=DEFAULT_SESSION_TTL,
             require_verification=False):
    """Create a user and log them in. Returns (user_dict, token, expires_at).

    With `require_verification` the account must confirm its address first, so
    no session is created and token / expires_at are None.
    """
    limiter.hit_register(ip)
    email = normalize_email(email)
    validate_password(password)
    password_hash = hash_password(password)
    try:
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, password_hash, db.now_utc()),
            )
    except sqlite3.IntegrityError:
        raise EmailTaken("该邮箱已被注册")
    row = user_by_id(conn, cur.lastrowid)
    if require_verification:
        return public_user(row), None, None
    token, expires_at = create_session(conn, row["id"], ttl, password_hash=password_hash)
    return public_user(row), token, expires_at


def login(conn, email, password, *, ip, limiter, ttl=DEFAULT_SESSION_TTL,
          require_verification=False):
    """Check credentials. Returns (user_dict, token, expires_at).

    With `require_verification`, a correct password for an unverified address
    raises EmailNotVerified (checked only after the password, so it reveals
    nothing to someone who does not know it).
    """
    if (not isinstance(email, str) or not isinstance(password, str)
            or len(email) > 254 or len(password) > 128):
        raise ValidationError("请输入邮箱和密码")
    email = email.strip().lower()
    limiter.check_login(email, ip)
    row = conn.execute("SELECT id, email, password_hash, created_at, email_verified_at "
                       "FROM users WHERE email = ?", (email,)).fetchone()
    ok = verify_password(password[:1024], row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        limiter.record_failure(email, ip)
        raise InvalidCredentials("邮箱或密码错误")
    limiter.record_success(email)
    if require_verification and not row["email_verified_at"]:
        raise EmailNotVerified("邮箱尚未验证，请先点击验证邮件中的链接（可重新发送验证邮件）")
    token, expires_at = create_session(conn, row["id"], ttl, password_hash=row["password_hash"])
    return public_user(row), token, expires_at
