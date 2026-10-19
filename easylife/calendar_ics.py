"""RFC 5545 calendar generation and per-user subscription tokens (DESIGN.md §3.8-3.9, §6)."""

import datetime
import hmac
import secrets
import urllib.parse

from easylife import db

CRLF = "\r\n"
PRODID_TODO = "-//EasyLife//Todo//CN"
PRODID_FEED = "-//EasyLife//Calendar Feed//CN"
FEED_NAME = "EasyLife 任务"
EVENT_MINUTES = 30          # DTEND = DTSTART + 30min
ALARM_MINUTES = 30          # default VALARM trigger
TOKEN_BYTES = 24            # -> 32 URL-safe characters
PRIORITY_MAP = {3: 1, 2: 5, 1: 9, 0: 9}
FOLD_LIMIT = 75             # octets per physical line, excluding CRLF
FEED_STATUSES = ("open", "doing")


class IcsError(ValueError):
    """A todo that cannot be exported (no due_at / remind_at)."""


# ---------------------------------------------------------------- access

def _field(todo, key, default=None):
    """Read one field from a dict *or* a `sqlite3.Row`.

    `sqlite3.Row` supports `row[key]` but has **no** `.get()`, so everything in
    this module goes through this helper: a row straight from a SELECT must be
    usable anywhere a todo dict is (regression: `reminders.build_message` used to
    raise `AttributeError: 'sqlite3.Row' object has no attribute 'get'` through
    `task_link()` as soon as `BASE_URL` was configured).
    """
    try:
        value = todo[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


# ---------------------------------------------------------------- text

def escape_text(value):
    r"""Escape \ ; , and newlines as required by RFC 5545 (3.3.11)."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\n", "\\n")


def fold_line(line, limit=FOLD_LIMIT):
    """Split one content line into physical lines of at most `limit` octets.

    Continuation lines keep the single leading space RFC 5545 requires, and that
    space counts towards the octet budget. Multi-byte characters are never cut.
    """
    physical, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        budget = limit if not physical else limit - 1
        if size + width > budget and size > 1:
            physical.append(current)
            current, size = " ", 1
        current += char
        size += width
    physical.append(current)
    return physical


def fold_all(lines):
    """Fold every content line and terminate each physical line with CRLF."""
    out = []
    for line in lines:
        out.extend(fold_line(line))
    return CRLF.join(out) + CRLF


# ---------------------------------------------------------------- time

def _parse(stored):
    return datetime.datetime.strptime(stored, db.TIME_FORMAT)


def _add_minutes(stored, minutes):
    return (_parse(stored) + datetime.timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%SZ")


def basic_time(stored):
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> 'YYYYMMDDTHHMMSSZ'."""
    return _parse(stored).strftime("%Y%m%dT%H%M%SZ")


def dtuple(stored):
    """(start, end) in the basic UTC format used by ICS and Google links."""
    return basic_time(stored), _add_minutes(stored, EVENT_MINUTES)


def stamp():
    """DTSTAMP: current UTC instant."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def todo_time(todo):
    """DTSTART source: due_at wins, else remind_at, else raise IcsError."""
    for key in ("due_at", "remind_at"):
        if _field(todo, key):
            return _field(todo, key)
    raise IcsError("任务未设置 due_at 或 remind_at，无法生成日历事件")


def _alarm_minutes(todo):
    """Trigger offset in minutes before DTSTART: the early alert, else the due time itself
    (iPhone model); 30 minutes for a reminder time without a due time."""
    start = _field(todo, "due_at") or _field(todo, "remind_at")
    remind = _field(todo, "remind_at")
    if _field(todo, "due_at") and not remind:
        return 0
    if remind and start:
        delta = (_parse(start) - _parse(remind)).total_seconds() / 60.0
        if 0 < delta <= 7 * 24 * 60:
            return int(round(delta))
    return ALARM_MINUTES


def _trigger(todo):
    minutes = _alarm_minutes(todo)
    return "-PT%dM" % minutes if minutes > 0 else "PT0M"


# ---------------------------------------------------------------- events

def uid_for(todo_id):
    """Stable UID so refetching a feed updates rather than duplicates events."""
    return "easylife-todo-%s@easylife" % todo_id


def task_link(todo, base_url):
    # `_field`, not `.get`: dicts *and* sqlite3.Row rows are both accepted.
    return "%s/todos/%s" % ((base_url or "").rstrip("/"), _field(todo, "id"))


def _description(todo, base_url):
    parts = []
    if _field(todo, "details"):
        parts.append(_field(todo, "details"))
    parts.append("优先级 %s" % _field(todo, "priority", 2))
    if base_url:
        parts.append(task_link(todo, base_url))
    return escape_text("\n".join(parts))


def event_lines(todo, base_url="", *, with_alarm=True, with_details=True):
    """The BEGIN:VEVENT .. END:VEVENT lines for one todo (unescaped folding later)."""
    start = todo_time(todo)
    dtstart, dtend = dtuple(start)
    lines = [
        "BEGIN:VEVENT",
        "UID:" + uid_for(_field(todo, "id")),
        "DTSTAMP:" + stamp(),
        "DTSTART:" + dtstart,
        "DTEND:" + dtend,
        "SUMMARY:" + escape_text(_field(todo, "title") or ""),
    ]
    if with_details:
        lines.append("DESCRIPTION:" + _description(todo, base_url))
        lines.append("STATUS:" + ("COMPLETED" if _field(todo, "status") == "done" else "CONFIRMED"))
        lines.append("PRIORITY:%d" % PRIORITY_MAP.get(_field(todo, "priority", 2), 5))
    if _field(todo, "repeat") and _field(todo, "due_at"):
        lines.append("RRULE:" + _field(todo, "repeat"))
    if with_alarm:
        lines += [
            "BEGIN:VALARM",
            "TRIGGER:" + _trigger(todo),
            "ACTION:DISPLAY",
            "DESCRIPTION:" + escape_text(_field(todo, "title") or ""),
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def todo_to_ics(todo, *, base_url=""):
    """Single-event VCALENDAR for one todo (design §3.8)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID_TODO,
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    lines += event_lines(todo, base_url)
    lines.append("END:VCALENDAR")
    return fold_all(lines)


def calendar_feed(todos, *, name=FEED_NAME, base_url=""):
    """Multi-event subscription feed with only open/doing todos that have a time."""
    events = []
    for todo in sorted((t for t in todos if _field(t, "status") in FEED_STATUSES),
                       key=lambda t: (_field(t, "due_at") or _field(t, "remind_at") or "",
                                      _field(t, "id"))):
        try:
            events += event_lines(todo, base_url,
                                  with_alarm=bool(_field(todo, "remind_at") or _field(todo, "due_at")),
                                  with_details=False)
        except IcsError:
            continue  # tasks without a time simply do not appear in the feed
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID_FEED,
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:" + escape_text(name), "X-WR-TIMEZONE:UTC"]
    lines += events
    lines.append("END:VCALENDAR")
    return fold_all(lines)


def feed_headers(filename=None):
    """Response headers for text/calendar bodies."""
    headers = {"Cache-Control": "no-store"}
    if filename:
        headers["Content-Disposition"] = 'attachment; filename="%s"' % filename
    return headers


def google_calendar_url(todo, *, base_url=""):
    """'Add to Google Calendar' template link (design §3.9)."""
    params = {"action": "TEMPLATE", "text": _field(todo, "title") or ""}
    details = [_field(todo, "details") or ""]
    if base_url:
        details.append(task_link(todo, base_url))
    params["details"] = "\n".join(p for p in details if p)
    if _field(todo, "due_at"):
        start, end = dtuple(_field(todo, "due_at"))
        params["dates"] = "%s/%s" % (start, end)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------- tokens

def new_token():
    """A fresh per-user private subscription token (URL-safe, 32 chars)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def get_or_create_token(conn, user_id):
    """Return the user's subscription token, creating one on first use."""
    row = conn.execute("SELECT calendar_token FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    if not row["calendar_token"]:
        with db.tx(conn):
            conn.execute("UPDATE users SET calendar_token = ? WHERE id = ? AND "
                         "(calendar_token IS NULL OR calendar_token = '')",
                         (new_token(), user_id))
        row = conn.execute("SELECT calendar_token FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["calendar_token"]


def reset_token(conn, user_id):
    """Rotate the subscription token (invalidates previously shared URLs)."""
    token = new_token()
    with db.tx(conn):
        cur = conn.execute("UPDATE users SET calendar_token = ? WHERE id = ?", (token, user_id))
    return token if cur.rowcount == 1 else None


def user_id_for_token(conn, token):
    """Resolve a token to a user id, or None. Comparison is constant time."""
    if not isinstance(token, str) or not 1 <= len(token) <= 128:
        return None
    row = conn.execute("SELECT id, calendar_token FROM users WHERE calendar_token = ?",
                       (token,)).fetchone()
    if row is None:
        return None
    return row["id"] if hmac.compare_digest(row["calendar_token"], token) else None


def feed_url(base_url, token):
    return "%s/api/calendar.ics?token=%s" % ((base_url or "").rstrip("/"),
                                             urllib.parse.quote(token, safe=""))
