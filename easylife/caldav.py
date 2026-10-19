"""CalDAV task sync (RFC 4791, VTODO only) for iOS / macOS Reminders and other task clients.

Every task list of the user is a calendar collection, served under <base>/dav/:

    /dav/                                   root (discovery)
    /dav/principals/<user id>/              principal
    /dav/calendars/<user id>/               calendar home
    /dav/calendars/<user id>/tasks/         the default list (todos with list_id NULL)
    /dav/calendars/<user id>/<slug>/        another list (see easylife.lists)
    /dav/calendars/<user id>/<slug>/<name>  one non-archived todo as a VTODO

Clients may create (MKCALENDAR), rename / recolour (PROPPATCH) and delete lists, and move a
todo between lists by writing it into another collection.

Clients authenticate with HTTP Basic: the account e-mail plus an app-specific password
generated in the settings page (only its SHA-256 is stored). Properties EasyLife does not
model (RRULE, X-APPLE-*, RELATED-TO, ...) and the client's own VALARM blocks are kept in
`dav_objects` and written back, so a round trip through EasyLife does not lose them.
"""

import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.parse
import xml.etree.ElementTree as ET

from easylife import auth, calendar_ics, db, lists, todos

try:
    import zoneinfo
except ImportError:  # Python 3.8
    zoneinfo = None

log = logging.getLogger("easylife.caldav")

NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:caldav"
NS_CS = "http://calendarserver.org/ns/"
NS_A = "http://apple.com/ns/ical/"
PREFIXES = {NS_D: "d", NS_C: "c", NS_CS: "cs", NS_A: "a"}

DAV_HEADER = "1, 3, calendar-access"
ALLOW = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, REPORT, MKCALENDAR"
PRODID = "-//EasyLife//CalDAV//CN"
MAX_BODY = 256 * 1024
EXTRA_MAX = 16000
DEFAULT_TZ = "Asia/Shanghai"
ALL_DAY_HOUR = 9            # a date-only DUE becomes 09:00 local time
STALE_ALARM_SECONDS = 600   # alarms already this far in the past are not mailed again
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
NAME_RE = re.compile(r"^[^/\x00-\x1f\x7f]{1,200}$")
DEFAULT_NAME_RE = re.compile(r"^easylife-(\d{1,19})\.ics$")
DEFAULT_UID_RE = re.compile(r"^easylife-task-(\d{1,19})@easylife$")

# Properties EasyLife writes itself; everything else in a client's VTODO is preserved.
MANAGED = {"UID", "DTSTAMP", "CREATED", "LAST-MODIFIED", "SUMMARY", "DESCRIPTION", "STATUS",
           "PRIORITY", "DTSTART", "DUE", "DURATION", "COMPLETED", "PERCENT-COMPLETE", "SEQUENCE"}
# Matched by name: 高/中/低 <-> Reminders high (1) / medium (5) / low (9); 最低 <-> none (0).
PRIORITY_OUT = {3: 1, 2: 5, 1: 9, 0: 0}
# Fields compared with the snapshot taken when a client last wrote the object.
SNAPSHOT_FIELDS = ("title", "details", "status", "priority", "due_at", "remind_at")
CHECK_OUT = re.compile(r"^(\s*)[-*+] \[([ xX])\] ?", re.M)
CHECK_IN = re.compile(r"^(\s*)([\u2610\u2611\u2612]) ?", re.M)
STATUS_OUT = {"open": "NEEDS-ACTION", "doing": "IN-PROCESS", "done": "COMPLETED"}


class DavError(Exception):
    def __init__(self, status, message="", headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


# ---------------------------------------------------------------- credentials

def new_password():
    """16 unambiguous characters in four groups (~79 bits), easy to type on a phone."""
    chars = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(16))
    return "-".join(chars[i:i + 4] for i in range(0, 16, 4))


def _hash(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def set_password(conn, user_id):
    """Create or replace the user's sync password; returns the plain text once."""
    password = new_password()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO dav_credentials (user_id, password_hash, created_at, last_used_at) "
            "VALUES (?, ?, ?, NULL) ON CONFLICT(user_id) DO UPDATE SET "
            "password_hash = excluded.password_hash, created_at = excluded.created_at, "
            "last_used_at = NULL", (user_id, _hash(password), db.now_utc()))
    return password


def revoke(conn, user_id):
    with db.tx(conn):
        conn.execute("DELETE FROM dav_credentials WHERE user_id = ?", (user_id,))


def info(conn, user, base_url):
    row = conn.execute("SELECT created_at, last_used_at FROM dav_credentials WHERE user_id = ?",
                       (user["id"],)).fetchone()
    return {
        "enabled": row is not None,
        "server_url": (base_url or "").rstrip("/") + "/dav/",
        "username": user["email"],
        "created_at": row["created_at"] if row else None,
        "last_used_at": row["last_used_at"] if row else None,
    }


def authenticate(handler):
    """The user row for valid Basic credentials, else raise 401 / 429."""
    header = handler.headers.get("Authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value.strip():
        raise _unauthorized()
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise _unauthorized()
    login, _, password = decoded.partition(":")
    try:
        login = auth.normalize_email(login)
    except auth.ValidationError:
        raise _unauthorized()
    limiter = handler.server.dav_limiter
    ip = handler.client_ip()
    try:
        limiter.check_login(login, ip)
    except auth.RateLimited as exc:
        raise DavError(429, "尝试次数过多，请稍后再试", {"Retry-After": str(exc.retry_after)})
    conn = handler.conn()
    row = conn.execute(
        "SELECT u.id, u.email, c.password_hash, c.last_used_at FROM users u "
        "JOIN dav_credentials c ON c.user_id = u.id WHERE u.email = ?", (login,)).fetchone()
    if row is None or not password or not hmac.compare_digest(row["password_hash"], _hash(password)):
        limiter.record_failure(login, ip)
        log.warning("caldav: rejected credentials for %s", auth.mask_email(login))
        raise _unauthorized()
    limiter.record_success(login)
    if not row["last_used_at"] or row["last_used_at"] < db.now_utc(-300):
        with db.tx(conn):
            conn.execute("UPDATE dav_credentials SET last_used_at = ? WHERE user_id = ?",
                         (db.now_utc(), row["id"]))
    return row


def _unauthorized():
    return DavError(401, "需要 EasyLife 账号邮箱和同步专用密码",
                    {"WWW-Authenticate": 'Basic realm="EasyLife", charset="UTF-8"'})


# ---------------------------------------------------------------- iCalendar parsing

def unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def parse_line(line):
    """'NAME;P=V;Q="a;b":value' -> (NAME, {P: V, Q: 'a;b'}, value)."""
    i, n = 0, len(line)
    while i < n and line[i] not in ";:":
        i += 1
    name = line[:i].upper()
    params = {}
    while i < n and line[i] == ";":
        i += 1
        start = i
        while i < n and line[i] not in "=;:":
            i += 1
        key = line[start:i].upper()
        value = ""
        if i < n and line[i] == "=":
            i += 1
            if i < n and line[i] == '"':
                end = line.find('"', i + 1)
                end = n if end < 0 else end
                value = line[i + 1:end]
                i = end + 1
            else:
                start = i
                while i < n and line[i] not in ";:":
                    i += 1
                value = line[start:i]
        params[key] = value
    if i >= n or line[i] != ":" or not name:
        raise ValueError("bad content line")
    return name, params, line[i + 1:]


def parse_components(text):
    """A tree of {'name', 'props': [(name, params, value, raw)], 'children'}."""
    root = {"name": "", "props": [], "children": []}
    stack = [root]
    for raw in unfold(text):
        if not raw.strip():
            continue
        name, params, value = parse_line(raw)
        if name == "BEGIN":
            comp = {"name": value.strip().upper(), "props": [], "children": [], "raw": [raw]}
            stack[-1]["children"].append(comp)
            stack.append(comp)
            continue
        if len(stack) > 1:
            for comp in stack[1:]:
                comp["raw"].append(raw)
        if name == "END":
            if len(stack) == 1 or stack[-1]["name"] != value.strip().upper():
                raise ValueError("unbalanced END")
            stack.pop()
            continue
        stack[-1]["props"].append((name, params, value, raw))
    if len(stack) != 1:
        raise ValueError("unterminated component")
    return root


def unescape_text(value):
    out, i = [], 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _first(comp, name):
    for prop in comp["props"]:
        if prop[0] == name:
            return prop
    return None


def _zone(name):
    if zoneinfo is not None and name:
        try:
            return zoneinfo.ZoneInfo(name.strip().lstrip("/"))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return None


def default_zone(cfg):
    zone = _zone((cfg or {}).get("timezone") or DEFAULT_TZ)
    return zone or datetime.timezone(datetime.timedelta(hours=8))


def parse_datetime(value, params, tz):
    """An iCalendar DATE / DATE-TIME -> stored UTC 'YYYY-MM-DD HH:MM:SS'."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.datetime.strptime(value[:8], "%Y%m%d")
        local = day.replace(hour=ALL_DAY_HOUR, tzinfo=tz)
    else:
        match = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value.upper())
        if not match:
            raise ValueError("bad date-time")
        naive = datetime.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
        if match.group(2):
            local = naive.replace(tzinfo=datetime.timezone.utc)
        else:
            local = naive.replace(tzinfo=_zone(params.get("TZID")) or tz)
    utc = local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return utc.strftime(db.TIME_FORMAT)


def parse_duration(value):
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
                         value.strip().upper())
    if not match or value.strip().upper() in ("P", "+P", "-P", "PT"):
        raise ValueError("bad duration")
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups()[1:])
    delta = datetime.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if match.group(1) == "-" else delta


def _alarm_time(alarm, todo, tz):
    """Absolute UTC time of one VALARM, or None (location alarms, unparseable triggers)."""
    if any(p[0] in ("X-APPLE-PROXIMITY", "X-APPLE-STRUCTURED-LOCATION") for p in alarm["props"]):
        return None
    trigger = _first(alarm, "TRIGGER")
    if trigger is None:
        return None
    _, params, value, _ = trigger
    try:
        if params.get("VALUE", "").upper() == "DATE-TIME" or re.match(r"^\d{8}T", value.strip()):
            at = parse_datetime(value, params, tz)
        else:
            base_prop = _first(todo, "DUE") if params.get("RELATED", "").upper() == "END" else (
                _first(todo, "DTSTART") or _first(todo, "DUE"))
            if base_prop is None:
                return None
            base = parse_datetime(base_prop[2], base_prop[1], tz)
            at = (datetime.datetime.strptime(base, db.TIME_FORMAT)
                  + parse_duration(value)).strftime(db.TIME_FORMAT)
    except ValueError:
        return None
    return at if at >= "2000-01-01" else None  # Apple uses 1976 placeholders


def parse_vtodo(text, tz):
    """Body of a PUT -> (uid, fields, extra_lines, alarm_blocks)."""
    try:
        tree = parse_components(text)
    except ValueError:
        raise DavError(400, "无法解析 iCalendar 数据")
    calendars = [c for c in tree["children"] if c["name"] == "VCALENDAR"]
    if len(calendars) != 1:
        raise DavError(400, "需要一个 VCALENDAR")
    items = [c for c in calendars[0]["children"] if c["name"] not in ("VTIMEZONE",)]
    vtodos = [c for c in items if c["name"] == "VTODO"]
    if len(vtodos) != 1 or len(items) != 1:
        raise DavError(403, "这个列表只接受提醒事项（VTODO）")
    todo = vtodos[0]
    uid = _first(todo, "UID")
    if uid is None or not uid[2].strip() or len(uid[2]) > 255:
        raise DavError(400, "缺少 UID")

    def text_of(name):
        prop = _first(todo, name)
        return unescape_text(prop[2]) if prop else ""

    fields = {}
    title = text_of("SUMMARY").strip() or "无标题"
    fields["title"] = title[:todos.TITLE_MAX]
    details = text_of("DESCRIPTION")
    details = markdown_checklist(details)
    fields["details"] = details[:todos.DETAILS_MAX] if details.strip() else None
    status = text_of("STATUS").strip().upper()
    if status == "COMPLETED" or _first(todo, "COMPLETED") is not None:
        fields["status"] = "done"
    elif status == "IN-PROCESS":
        fields["status"] = "doing"
    elif status == "CANCELLED":
        fields["status"] = "archived"
    else:
        fields["status"] = "open"
    try:
        priority = int(text_of("PRIORITY").strip() or 0)
    except ValueError:
        priority = 0
    fields["ical_priority"] = priority
    fields["priority"] = 0 if priority <= 0 else 3 if priority < 5 else 2 if priority == 5 else 1
    due = _first(todo, "DUE")
    try:
        fields["due_at"] = parse_datetime(due[2], due[1], tz) if due else None
    except ValueError:
        raise DavError(400, "DUE 不是有效的时间")
    alarms = [c for c in todo["children"] if c["name"] == "VALARM"]
    times = [t for t in (_alarm_time(a, todo, tz) for a in alarms) if t]
    fields["remind_at"] = min(times) if times else None
    extra = [raw for name, _, _, raw in todo["props"] if name not in MANAGED]
    for child in todo["children"]:
        if child["name"] != "VALARM":
            extra.extend(child["raw"])
    extra_text = "\n".join(extra)
    if len(extra_text) > EXTRA_MAX:
        extra_text = ""
    alarm_text = "\n".join("\n".join(a["raw"]) for a in alarms)
    if len(alarm_text) > EXTRA_MAX:
        alarm_text = ""
    return uid[2].strip(), fields, extra_text or None, alarm_text or None


# ---------------------------------------------------------------- iCalendar output

def default_name(todo_id):
    return "easylife-%d.ics" % todo_id


def default_uid(todo_id):
    return "easylife-task-%d@easylife" % todo_id


OBJECT_COLUMNS = ("t.id, t.title, t.details, t.status, t.priority, t.due_at, t.remind_at, t.list_id, "
                  "t.created_at, t.updated_at, o.name AS dav_name, o.uid AS dav_uid, "
                  "o.extra AS dav_extra, o.alarms AS dav_alarms, o.alarms_at AS dav_alarms_at, "
                  "o.raw AS dav_raw, o.snapshot AS dav_snapshot")


def reminders_checklist(text):
    """Markdown task items -> ☐ / ☑ lines, which read naturally in Reminders' notes."""
    return CHECK_OUT.sub(lambda m: m.group(1) + ("\u2610 " if m.group(2) == " " else "\u2611 "), text)


def markdown_checklist(text):
    return CHECK_IN.sub(lambda m: m.group(1) + ("- [ ] " if m.group(2) == "\u2610" else "- [x] "), text)


def _object(row):
    obj = {k: row[k] for k in row.keys()}
    obj["name"] = obj["dav_name"] or default_name(obj["id"])
    obj["uid"] = obj["dav_uid"] or default_uid(obj["id"])
    obj["ics"] = to_ics(obj)
    obj["etag"] = '"%s"' % hashlib.sha256(obj["ics"].encode("utf-8")).hexdigest()[:32]
    return obj


def list_objects(conn, user_id, item):
    stored = lists.stored_id(item)
    rows = conn.execute(
        "SELECT " + OBJECT_COLUMNS + " FROM todos t LEFT JOIN dav_objects o ON o.todo_id = t.id "
        "WHERE t.user_id = ? AND t.status != 'archived' AND t.list_id IS ? ORDER BY t.id",
        (user_id, stored)).fetchall()
    return [_object(r) for r in rows]


def in_list(obj, item):
    return obj is not None and obj["list_id"] == lists.stored_id(item)


def find_by_id(conn, user_id, todo_id):
    row = conn.execute(
        "SELECT " + OBJECT_COLUMNS + " FROM todos t LEFT JOIN dav_objects o ON o.todo_id = t.id "
        "WHERE t.id = ? AND t.user_id = ?", (todo_id, user_id)).fetchone()
    return _object(row) if row else None


def find_object(conn, user_id, name, include_archived=False):
    archived = "" if include_archived else " AND t.status != 'archived'"
    row = conn.execute(
        "SELECT " + OBJECT_COLUMNS + " FROM dav_objects o JOIN todos t ON t.id = o.todo_id "
        "WHERE o.user_id = ? AND o.name = ?" + archived, (user_id, name)).fetchone()
    if row is None:
        match = DEFAULT_NAME_RE.match(name)
        if match:
            row = conn.execute(
                "SELECT " + OBJECT_COLUMNS + " FROM todos t LEFT JOIN dav_objects o "
                "ON o.todo_id = t.id WHERE t.id = ? AND t.user_id = ? AND o.todo_id IS NULL"
                + archived, (int(match.group(1)), user_id)).fetchone()
    return _object(row) if row else None


def _uid_taken(conn, user_id, uid):
    """The todo id already using `uid` for this user, else None."""
    row = conn.execute("SELECT todo_id FROM dav_objects WHERE user_id = ? AND uid = ?",
                       (user_id, uid)).fetchone()
    if row:
        return row["todo_id"]
    match = DEFAULT_UID_RE.match(uid)
    if match:
        row = conn.execute(
            "SELECT t.id FROM todos t LEFT JOIN dav_objects o ON o.todo_id = t.id "
            "WHERE t.id = ? AND t.user_id = ? AND o.todo_id IS NULL",
            (int(match.group(1)), user_id)).fetchone()
        if row:
            return row["id"]
    return None


# Zone that DUE / DTSTART are written in (EASYLIFE_TIMEZONE; set per request in handle()).
# Times written as UTC make Reminders show the reminder in "GMT".
_output_zone = None


def output_zone():
    global _output_zone
    if _output_zone is None:
        _output_zone = default_zone({})
    return _output_zone


def _zone_key(tz):
    """The TZID for `tz` when a fixed-offset VTIMEZONE describes it, else None (use UTC)."""
    key = getattr(tz, "key", None)
    if not key:
        return None
    year = datetime.datetime.now().year
    offsets = {datetime.datetime(year, m, 1, tzinfo=tz).utcoffset() for m in (1, 4, 7, 10)}
    return key if len(offsets) == 1 else None


def vtimezone_lines(tz):
    key = _zone_key(tz)
    offset = datetime.datetime(2000, 1, 1, tzinfo=tz).utcoffset()
    minutes = int(offset.total_seconds() // 60)
    text = "%s%02d%02d" % ("-" if minutes < 0 else "+", abs(minutes) // 60, abs(minutes) % 60)
    name = datetime.datetime(2000, 1, 1, tzinfo=tz).tzname() or text
    return ["BEGIN:VTIMEZONE", "TZID:" + key, "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:" + text, "TZOFFSETTO:" + text, "TZNAME:" + name, "END:STANDARD",
            "END:VTIMEZONE"]


def local_time(prop, stored):
    """'DUE' + stored UTC -> 'DUE;TZID=Asia/Shanghai:20261020T180000' (or UTC with Z)."""
    tz = output_zone()
    key = _zone_key(tz)
    if not key:
        return prop + ":" + calendar_ics.basic_time(stored)
    utc = datetime.datetime.strptime(stored, db.TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
    return "%s;TZID=%s:%s" % (prop, key, utc.astimezone(tz).strftime("%Y%m%dT%H%M%S"))


def format_duration(seconds):
    """-370800 -> '-P4DT7H'; 0 -> 'PT0S'."""
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    time_part = "".join("%d%s" % (v, u) for v, u in ((hours, "H"), (minutes, "M"), (secs, "S")) if v)
    if not days and not time_part:
        return "PT0S"
    return sign + "P" + ("%dD" % days if days else "") + ("T" + time_part if time_part else "")


def _trigger_line(obj):
    """Relative to DTSTART (= DUE) when there is a due date: an absolute UTC alarm that
    differs from the due time is shown by Reminders with a "GMT" label."""
    if obj["due_at"]:
        parse = lambda v: datetime.datetime.strptime(v, db.TIME_FORMAT)  # noqa: E731
        delta = (parse(obj["remind_at"]) - parse(obj["due_at"])).total_seconds()
        return "TRIGGER;RELATED=START:" + format_duration(delta)
    return "TRIGGER;VALUE=DATE-TIME:" + calendar_ics.basic_time(obj["remind_at"])


def _alarm_lines(obj):
    esc = calendar_ics.escape_text
    return ["BEGIN:VALARM",
            "UID:easylife-alarm-%d" % obj["id"],
            "X-WR-ALARMUID:easylife-alarm-%d" % obj["id"],
            "ACTION:DISPLAY",
            "DESCRIPTION:" + esc(obj["title"]),
            _trigger_line(obj),
            "END:VALARM"]


def _field_lines(obj, field):
    """Content lines EasyLife writes for one changed field."""
    b = calendar_ics.basic_time
    esc = calendar_ics.escape_text
    if field == "title":
        return ["SUMMARY:" + esc(obj["title"])]
    if field == "details":
        return ["DESCRIPTION:" + esc(reminders_checklist(obj["details"]))] if obj["details"] else []
    if field == "status":
        lines = ["STATUS:" + STATUS_OUT.get(obj["status"], "CANCELLED")]
        if obj["status"] == "done":
            lines += ["COMPLETED:" + b(obj["updated_at"]), "PERCENT-COMPLETE:100"]
        return lines
    if field == "priority":
        priority = PRIORITY_OUT.get(obj["priority"], 0)
        return ["PRIORITY:%d" % priority] if priority else []
    if field == "due_at":
        # Reminders writes DTSTART = DUE; keeping both equal also satisfies RRULE.
        return [local_time("DTSTART", obj["due_at"]), local_time("DUE", obj["due_at"])] if obj["due_at"] else []
    return []


FIELD_PROPS = {"title": {"SUMMARY"}, "details": {"DESCRIPTION"},
               "status": {"STATUS", "COMPLETED", "PERCENT-COMPLETE"}, "priority": {"PRIORITY"},
               "due_at": {"DTSTART", "DUE", "DURATION"}}


def _patched_raw(obj):
    """The client's own iCalendar text, with only the fields changed in EasyLife replaced.

    Echoing the client's representation back (TZID, all-day dates, VTIMEZONE, Apple
    properties) is what stops Reminders from "repairing" and re-uploading the object.
    """
    try:
        snapshot = json.loads(obj["dav_snapshot"] or "{}")
        lines = [line for line in unfold(obj["dav_raw"]) if line.strip()]
    except ValueError:
        return None
    changed = [f for f in SNAPSHOT_FIELDS if obj[f] != snapshot.get(f)]
    if not changed:
        return calendar_ics.fold_all(lines)
    try:
        begin = lines.index("BEGIN:VTODO")
        end = len(lines) - 1 - lines[::-1].index("END:VTODO")
    except ValueError:
        return None
    drop = {"DTSTAMP", "LAST-MODIFIED", "SEQUENCE"}
    for field in changed:
        drop |= FIELD_PROPS.get(field, set())
    alarms_changed = "remind_at" in changed or ("due_at" in changed and obj["remind_at"])
    body, depth, skipping = [], 0, False
    for line in lines[begin + 1:end]:
        name, _, value = parse_line(line)
        if name == "BEGIN":
            depth += 1
            if depth == 1 and alarms_changed and value.strip().upper() == "VALARM":
                skipping = True
            if not skipping:
                body.append(line)
            continue
        if name == "END":
            if not skipping:
                body.append(line)
            depth -= 1
            if depth == 0:
                skipping = False
            continue
        if skipping or (depth == 0 and name in drop):
            continue
        body.append(line)
    b = calendar_ics.basic_time
    head = ["DTSTAMP:" + b(obj["updated_at"]), "LAST-MODIFIED:" + b(obj["updated_at"])]
    for field in changed:
        head += _field_lines(obj, field)
    # Properties first, then sub-components, as RFC 5545 expects.
    first_sub = next((i for i, line in enumerate(body) if line.upper().startswith("BEGIN:")), len(body))
    tail = _alarm_lines(obj) if alarms_changed and obj["remind_at"] else []
    new_body = body[:first_sub] + head + body[first_sub:] + tail
    prefix = lines[:begin + 1]
    key = _zone_key(output_zone())
    if "due_at" in changed and obj["due_at"] and key and ("TZID:" + key) not in lines:
        prefix = lines[:begin] + vtimezone_lines(output_zone()) + ["BEGIN:VTODO"]
    return calendar_ics.fold_all(prefix + new_body + lines[end:])


def to_ics(obj):
    if obj.get("dav_raw"):
        patched = _patched_raw(obj)
        if patched:
            return patched
    b = calendar_ics.basic_time
    esc = calendar_ics.escape_text
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:" + PRODID, "CALSCALE:GREGORIAN",
             "BEGIN:VTODO",
             "UID:" + obj["uid"],
             "DTSTAMP:" + b(obj["updated_at"]),
             "CREATED:" + b(obj["created_at"]),
             "LAST-MODIFIED:" + b(obj["updated_at"]),
             "SUMMARY:" + esc(obj["title"])]
    if obj["details"]:
        lines.append("DESCRIPTION:" + esc(reminders_checklist(obj["details"])))
    lines.append("STATUS:" + STATUS_OUT.get(obj["status"], "CANCELLED"))
    priority = PRIORITY_OUT.get(obj["priority"], 0)
    if priority:
        lines.append("PRIORITY:%d" % priority)
    extra = (obj.get("dav_extra") or "").split("\n") if obj.get("dav_extra") else []
    if obj["due_at"]:
        lines += [local_time("DTSTART", obj["due_at"]), local_time("DUE", obj["due_at"])]
        if _zone_key(output_zone()):
            begin = lines.index("BEGIN:VTODO")
            lines[begin:begin] = vtimezone_lines(output_zone())
    if obj["status"] == "done":
        lines += ["COMPLETED:" + b(obj["updated_at"]), "PERCENT-COMPLETE:100"]
    lines += extra
    if obj["remind_at"]:
        if obj.get("dav_alarms") and obj.get("dav_alarms_at") == obj["remind_at"]:
            lines += obj["dav_alarms"].split("\n")
        else:
            lines += _alarm_lines(obj)
    lines += ["END:VTODO", "END:VCALENDAR"]
    return calendar_ics.fold_all(lines)


# ---------------------------------------------------------------- writes

def _check_preconditions(headers, current):
    if_match = headers.get("If-Match")
    if_none = headers.get("If-None-Match")
    if current is None:
        if if_match is not None:
            raise DavError(412, "资源不存在")
        return
    if if_none is not None and (if_none.strip() == "*" or current["etag"] in if_none):
        raise DavError(412, "资源已存在")
    if if_match is not None and if_match.strip() != "*" and current["etag"] not in if_match:
        raise DavError(412, "资源已在别处修改，请刷新后重试")


def put_object(conn, user_id, item, name, body, headers, tz):
    """Create or update one todo of list `item` from a VTODO. Returns 201 or 204.

    A todo that already exists in another list (same resource name or same UID) is moved:
    that is how clients move a reminder between lists."""
    uid, fields, extra, alarms = parse_vtodo(body, tz)
    ical_priority = fields.pop("ical_priority")
    list_id = lists.stored_id(item)
    with db.tx(conn):
        current = find_object(conn, user_id, name, include_archived=True)
        # The client only sees what is in this collection: a todo elsewhere is "new" here.
        _check_preconditions(headers, current if in_list(current, item) else None)
        owner = _uid_taken(conn, user_id, uid)
        if owner is not None and (current is None or owner != current["id"]):
            moved = find_by_id(conn, user_id, owner) if current is None else None
            if moved is None or in_list(moved, item):
                raise DavError(409, "UID 已被另一条任务使用")
            current = moved
        now = db.now_utc()
        if current is None:
            remind = fields["remind_at"]
            sent = 1 if remind and remind < db.now_utc(-STALE_ALARM_SECONDS) else 0
            cur = conn.execute(
                "INSERT INTO todos (user_id, title, details, status, priority, due_at, remind_at, "
                "reminder_sent, list_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, fields["title"], fields["details"], fields["status"], fields["priority"],
                 fields["due_at"], remind, sent, list_id, now, now))
            todo_id, status = cur.lastrowid, 201
        else:
            todo_id = current["id"]
            status = 204 if in_list(current, item) else 201
            fields["list_id"] = list_id
            # Values the client cannot express keep their EasyLife meaning.
            if ical_priority == PRIORITY_OUT.get(current["priority"], 0):
                fields["priority"] = current["priority"]
            if fields["status"] == "open" and current["status"] == "doing":
                fields["status"] = "doing"
            if fields["remind_at"] != current["remind_at"]:
                remind = fields["remind_at"]
                fields["reminder_sent"] = 1 if remind and remind < db.now_utc(-STALE_ALARM_SECONDS) else 0
            fields["updated_at"] = now
            assignments = ", ".join("%s = ?" % k for k in fields)  # keys are fixed above
            conn.execute("UPDATE todos SET " + assignments + " WHERE id = ? AND user_id = ?",
                         list(fields.values()) + [todo_id, user_id])
        stored = conn.execute("SELECT " + ", ".join(SNAPSHOT_FIELDS) + " FROM todos WHERE id = ?",
                              (todo_id,)).fetchone()
        snapshot = json.dumps({k: stored[k] for k in SNAPSHOT_FIELDS}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO dav_objects (todo_id, user_id, name, uid, extra, alarms, alarms_at, raw, "
            "snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(todo_id) DO UPDATE SET "
            "name = excluded.name, uid = excluded.uid, extra = excluded.extra, "
            "alarms = excluded.alarms, alarms_at = excluded.alarms_at, raw = excluded.raw, "
            "snapshot = excluded.snapshot",
            (todo_id, user_id, name, uid, extra, alarms, fields["remind_at"] if alarms else None,
             body, snapshot))
    return status


def delete_object(conn, user_id, item, name, headers):
    with db.tx(conn):
        current = find_object(conn, user_id, name)
        if not in_list(current, item):
            raise DavError(404, "资源不存在")
        _check_preconditions(headers, current)
        conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (current["id"], user_id))


# ---------------------------------------------------------------- XML

def _tag(ns, name, inner=None):
    prefix = PREFIXES.get(ns)
    if prefix:
        open_tag, close = "%s:%s" % (prefix, name), "%s:%s" % (prefix, name)
    else:
        open_tag = 'x:%s xmlns:x="%s"' % (name, _xml(ns))
        close = "x:" + name
    if inner is None or inner == "":
        return "<%s/>" % open_tag
    return "<%s>%s</%s>" % (open_tag, inner, close)


def _xml(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _href(path):
    return "<d:href>%s</d:href>" % _xml(path)


def multistatus(responses):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus xmlns:d="DAV:" '
            'xmlns:c="%s" xmlns:cs="%s" xmlns:a="%s">%s</d:multistatus>'
            % (NS_C, NS_CS, NS_A, "".join(responses))).encode("utf-8")


def prop_response(href, props, requested):
    """One <d:response>: found properties under 200, the rest under 404."""
    found, missing = [], []
    for key in requested:
        value = props.get(key)
        if value is None:
            missing.append(_tag(*key))
        else:
            found.append(_tag(key[0], key[1], value))
    parts = ["<d:response>", _href(href)]
    if found:
        parts.append("<d:propstat><d:prop>%s</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
                     "</d:propstat>" % "".join(found))
    if missing:
        parts.append("<d:propstat><d:prop>%s</d:prop><d:status>HTTP/1.1 404 Not Found</d:status>"
                     "</d:propstat>" % "".join(missing))
    parts.append("</d:response>")
    return "".join(parts)


def parse_xml(body):
    if not body.strip():
        return None
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise DavError(400, "不支持 DTD")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise DavError(400, "XML 解析失败")


def _split(tag):
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


ALLPROP = [(NS_D, "resourcetype"), (NS_D, "displayname"), (NS_D, "getetag"),
           (NS_D, "getcontenttype"), (NS_D, "getcontentlength"), (NS_D, "getlastmodified"),
           (NS_CS, "getctag")]


def requested_props(root):
    """Requested property keys, or None for allprop."""
    if root is None:
        return None
    prop = root.find("{DAV:}prop")
    if prop is None:
        return None
    return [_split(child.tag) for child in prop]


# ---------------------------------------------------------------- request handling

class Context:
    def __init__(self, handler, user):
        self.handler = handler
        self.user = user
        self.conn = handler.conn()
        self.base = handler.cfg.get("base_path") or ""
        self.uid = user["id"]
        self.tz = default_zone(handler.cfg)
        self._lists = None
        self._objects = {}

    def path(self, rel):
        return self.base + rel

    @property
    def principal(self):
        return self.path("/dav/principals/%d/" % self.uid)

    @property
    def home(self):
        return self.path("/dav/calendars/%d/" % self.uid)

    def lists(self):
        if self._lists is None:
            self._lists = lists.all_lists(self.conn, self.uid)
        return self._lists

    def list_of(self, obj):
        """The list a todo belongs to (the default one if its list vanished meanwhile)."""
        for item in self.lists():
            if item["id"] == obj["list_id"] or (obj["list_id"] is None and item["is_default"]):
                return item
        return self.lists()[0]

    def calendar_href(self, item):
        return self.home + urllib.parse.quote(item["slug"], safe="@=+-._~") + "/"

    def objects(self, item):
        if item["id"] not in self._objects:
            self._objects[item["id"]] = list_objects(self.conn, self.uid, item)
        return self._objects[item["id"]]

    def object_href(self, obj):
        return (self.calendar_href(self.list_of(obj))
                + urllib.parse.quote(obj["name"], safe="@=+-._~"))

    def ctag(self, item):
        digest = hashlib.sha256()
        digest.update(("%s %s %s\n" % (item["name"], item["color"], item["sort_order"]))
                      .encode("utf-8"))
        for obj in self.objects(item):
            digest.update(("%s %s\n" % (obj["name"], obj["etag"])).encode("utf-8"))
        return '"%s"' % digest.hexdigest()[:32]

    def find(self, item, name):
        obj = find_object(self.conn, self.uid, name)
        return obj if in_list(obj, item) else None


def resolve(ctx, path, allow_missing=False):
    """Internal path -> (kind, target).

    kind is 'root' | 'principals' | 'principal' | 'home' (target None), 'calendar' (target: the
    list), 'object' (target: (list, name)) or, with allow_missing, 'new-calendar' (target: slug).
    """
    parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
    if not parts or parts[0] != "dav":
        raise DavError(404, "资源不存在")
    parts = parts[1:]
    own = str(ctx.uid)
    if not parts:
        return "root", None
    if parts == ["principals"]:
        return "principals", None
    if len(parts) == 2 and parts[0] == "principals" and parts[1] == own:
        return "principal", None
    if len(parts) == 2 and parts[0] == "calendars" and parts[1] == own:
        return "home", None
    if len(parts) in (3, 4) and parts[:2] == ["calendars", own] and lists.SLUG_RE.match(parts[2]):
        item = lists.by_slug(ctx.conn, ctx.uid, parts[2])
        if len(parts) == 3:
            if item is not None:
                return "calendar", item
            if allow_missing:
                return "new-calendar", parts[2]
        elif item is not None and NAME_RE.match(parts[3]):
            return "object", (item, parts[3])
    raise DavError(404, "资源不存在")


def _privileges(write):
    names = ["read", "read-current-user-privilege-set"]
    if write:
        names += ["write", "write-properties", "write-content", "bind", "unbind", "all"]
    return "".join("<d:privilege><d:%s/></d:privilege>" % n for n in names)


def props_for(ctx, kind, target=None):
    d, c, cs, a = NS_D, NS_C, NS_CS, NS_A
    props = {
        (d, "current-user-principal"): _href(ctx.principal),
        (d, "principal-collection-set"): _href(ctx.path("/dav/principals/")),
        (d, "current-user-privilege-set"): _privileges(kind in ("home", "calendar", "object")),
    }
    principal = {
        (d, "principal-URL"): _href(ctx.principal),
        (c, "calendar-home-set"): _href(ctx.home),
        (c, "calendar-user-address-set"): _href("mailto:" + ctx.user["email"]),
        (c, "calendar-user-type"): "INDIVIDUAL",
    }
    if kind == "root":
        # iOS Reminders keeps using the URL typed into "server" as the principal and asks
        # it for the calendar home, so the root answers the principal properties too.
        props.update(principal)
        props[(d, "resourcetype")] = "<d:collection/>"
        props[(d, "displayname")] = "EasyLife"
    elif kind == "principals":
        props[(d, "resourcetype")] = "<d:collection/>"
    elif kind == "principal":
        props.update(principal)
        props.update({
            (d, "resourcetype"): "<d:principal/><d:collection/>",
            (d, "displayname"): _xml(ctx.user["email"]),
        })
    elif kind == "home":
        props.update({
            (d, "resourcetype"): "<d:collection/>",
            (d, "displayname"): "EasyLife",
            (d, "owner"): _href(ctx.principal),
        })
    elif kind == "calendar":
        item = target
        ctag = ctx.ctag(item)
        props.update({
            (d, "resourcetype"): "<d:collection/><c:calendar/>",
            (d, "displayname"): _xml(item["name"]),
            (d, "owner"): _href(ctx.principal),
            (d, "getetag"): _xml(ctag),
            (cs, "getctag"): _xml(ctag),
            (d, "supported-report-set"): "".join(
                "<d:supported-report><d:report><c:%s/></d:report></d:supported-report>" % r
                for r in ("calendar-query", "calendar-multiget")),
            (c, "supported-calendar-component-set"): '<c:comp name="VTODO"/>',
            (c, "supported-calendar-data"):
                '<c:calendar-data content-type="text/calendar" version="2.0"/>',
            (c, "calendar-description"): "EasyLife 任务",
            (c, "max-resource-size"): str(MAX_BODY),
            (a, "calendar-color"): _xml(item["color"] + "FF"),
            (a, "calendar-order"): str(item["sort_order"] + 1),
        })
    elif kind == "object":
        obj = target
        modified = datetime.datetime.strptime(obj["updated_at"], db.TIME_FORMAT).replace(
            tzinfo=datetime.timezone.utc)
        props.update({
            (d, "resourcetype"): "",
            (d, "getetag"): _xml(obj["etag"]),
            (d, "getcontenttype"): "text/calendar; charset=utf-8; component=VTODO",
            (d, "getcontentlength"): str(len(obj["ics"].encode("utf-8"))),
            (d, "getlastmodified"): email.utils.format_datetime(modified, usegmt=True),
            (d, "owner"): _href(ctx.principal),
            (c, "calendar-data"): _xml(obj["ics"]),
        })
    return props


def _children(ctx, kind, target):
    if kind == "root":
        return [(ctx.path("/dav/principals/"), "principals", None),
                (ctx.home, "home", None)]
    if kind == "principals":
        return [(ctx.principal, "principal", None)]
    if kind == "home":
        return [(ctx.calendar_href(item), "calendar", item) for item in ctx.lists()]
    if kind == "calendar":
        return [(ctx.object_href(o), "object", o) for o in ctx.objects(target)]
    return []


def _target_object(ctx, kind, target):
    """For an object path, swap (list, name) for the todo itself (404 when missing)."""
    if kind != "object":
        return target
    obj = ctx.find(*target)
    if obj is None:
        raise DavError(404, "资源不存在")
    return obj


def propfind(ctx, path, body):
    kind, target = resolve(ctx, path)
    target = _target_object(ctx, kind, target)
    requested = requested_props(parse_xml(body))
    depth = (ctx.handler.headers.get("Depth") or "1").strip().lower()
    targets = [(_self_href(ctx, kind, target), kind, target)]
    if depth != "0":
        targets += _children(ctx, kind, target)
    responses = []
    for href, target_kind, item in targets:
        props = props_for(ctx, target_kind, item)
        keys = requested
        if keys is None:
            keys = [k for k in ALLPROP + [(NS_D, "current-user-principal")] if k in props]
        responses.append(prop_response(href, props, keys))
    return multistatus(responses)


def _self_href(ctx, kind, target):
    if kind == "calendar":
        return ctx.calendar_href(target)
    if kind == "object":
        return ctx.object_href(target)
    return {
        "root": ctx.path("/dav/"),
        "principals": ctx.path("/dav/principals/"),
        "principal": ctx.principal,
        "home": ctx.home,
    }[kind]


def _href_target(ctx, href):
    """A multiget href -> (list, name), or None when it is not one of the user's objects."""
    path = urllib.parse.urlsplit(href.strip()).path
    if ctx.base and path.startswith(ctx.base + "/"):
        path = path[len(ctx.base):]
    try:
        kind, target = resolve(ctx, path)
    except DavError:
        return None
    return target if kind == "object" else None


def report(ctx, path, body):
    kind, target = resolve(ctx, path)
    root = parse_xml(body)
    if root is None:
        raise DavError(400, "缺少 REPORT 请求体")
    report_name = _split(root.tag)
    keys = requested_props(root) or [(NS_D, "getetag"), (NS_C, "calendar-data")]
    if report_name == (NS_C, "calendar-multiget"):
        responses = []
        for href in root.findall("{DAV:}href"):
            found = _href_target(ctx, href.text or "")
            obj = ctx.find(*found) if found else None
            if obj is None:
                responses.append("<d:response>%s<d:status>HTTP/1.1 404 Not Found</d:status>"
                                 "</d:response>" % _href(href.text or ""))
            else:
                responses.append(prop_response(ctx.object_href(obj),
                                               props_for(ctx, "object", obj), keys))
        return multistatus(responses)
    if report_name == (NS_C, "calendar-query"):
        if kind == "object":
            obj = ctx.find(*target)
            objects = [obj] if obj else []
        elif kind == "calendar":
            objects = ctx.objects(target)
        else:
            objects = []
        if not _wants_vtodo(root):
            objects = []
        return multistatus([prop_response(ctx.object_href(o), props_for(ctx, "object", o), keys)
                            for o in objects])
    raise DavError(403, "不支持该 REPORT")


def _wants_vtodo(root):
    """False when the calendar-query filter asks for a component other than VTODO."""
    outer = root.find("{%s}filter/{%s}comp-filter" % (NS_C, NS_C))
    if outer is None:
        return True
    inner = outer.find("{%s}comp-filter" % NS_C)
    return inner is None or (inner.get("name") or "").upper() == "VTODO"


def _list_props(root, wrapper):
    """{displayname, color, order} values under <d:set>/<d:prop> (or the MKCALENDAR <d:set>)."""
    values = {}
    if root is None:
        return values
    for block in root.iter("{DAV:}" + wrapper):
        for prop in block.iter("{DAV:}prop"):
            for child in prop:
                key = _split(child.tag)
                text = (child.text or "").strip()
                if key == (NS_D, "displayname") and text:
                    values["name"] = text[:lists.NAME_MAX]
                elif key == (NS_A, "calendar-color") and lists.COLOR_RE.match(text):
                    values["color"] = text
                elif key == (NS_A, "calendar-order") and text.lstrip("-").isdigit():
                    values["order"] = max(0, int(text) - 1)
    return values


def proppatch(ctx, path, body):
    """Lists take a new name / colour / order; everything else is accepted and ignored,
    so clients do not show errors."""
    kind, target = resolve(ctx, path)
    target = _target_object(ctx, kind, target)
    root = parse_xml(body)
    keys = []
    if root is not None:
        for prop in root.iter("{DAV:}prop"):
            keys.extend(_split(child.tag) for child in prop)
    if kind == "calendar":
        values = _list_props(root, "set")
        if values:
            try:
                target = lists.update_list(ctx.conn, ctx.uid, target["id"], **values) or target
            except lists.ListError as exc:
                raise DavError(400, str(exc))
    href = _self_href(ctx, kind, target)
    found = "".join(_tag(*k) for k in keys)
    return multistatus(["<d:response>%s<d:propstat><d:prop>%s</d:prop><d:status>HTTP/1.1 200 OK"
                        "</d:status></d:propstat></d:response>" % (_href(href), found)])


def mkcalendar(ctx, path, body):
    kind, slug = resolve(ctx, path, allow_missing=True)
    if kind != "new-calendar":
        raise DavError(405, "该位置已存在")
    root = parse_xml(body)
    if root is not None:
        comps = [comp.get("name", "").upper()
                 for block in root.iter("{%s}supported-calendar-component-set" % NS_C)
                 for comp in block]
        if comps and "VTODO" not in comps:
            raise DavError(403, "EasyLife 只同步任务（提醒事项），不能新建日历")
    values = _list_props(root, "set")
    try:
        lists.create_list(ctx.conn, ctx.uid, values.get("name") or "新列表", values.get("color"),
                          slug=slug, order=values.get("order"))
    except lists.ListError as exc:
        raise DavError(403, str(exc))


def read_body(handler):
    if handler.headers.get("Transfer-Encoding"):
        raise DavError(411, "请提供 Content-Length")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise DavError(400, "Content-Length 无效")
    if length < 0:
        raise DavError(400, "Content-Length 无效")
    if length > MAX_BODY:
        raise DavError(413, "请求体过大")
    body = handler.rfile.read(length) if length else b""
    handler._body_read = True
    return body


def is_dav_path(path):
    return path == "/dav" or path.startswith("/dav/") or path.startswith("/.well-known/caldav")


def _debug_wrap(handler, path, body_holder):
    """EASYLIFE_DAV_DEBUG=1: log each exchange (never the Authorization header)."""
    send = handler.send

    def logged(status, body, content_type, headers=None):
        log.info("dav-debug %s %s depth=%s ua=%s\n>>> %s\n<<< %s %s", handler.command, path,
                 handler.headers.get("Depth"), handler.headers.get("User-Agent"),
                 body_holder[0][:6000].decode("utf-8", "replace"), status,
                 body[:6000].decode("utf-8", "replace"))
        send(status, body, content_type, headers)
    handler.send = logged


def handle(handler, path):
    """Serve one CalDAV request. `path` already has the base path stripped."""
    method = handler.command
    global _output_zone
    _output_zone = default_zone(handler.cfg)
    holder = [b""]
    if handler.cfg.get("dav_debug"):
        _debug_wrap(handler, path, holder)
    headers = {"DAV": DAV_HEADER, "Allow": ALLOW, "Cache-Control": "no-store"}
    try:
        if path.startswith("/.well-known/caldav"):
            handler.close_if_body_unread()
            handler.send(301, b"", "text/plain; charset=utf-8",
                         {"Location": (handler.cfg.get("base_path") or "") + "/dav/"})
            return
        if method == "OPTIONS":
            handler.close_if_body_unread()
            handler.send(200, b"", "text/plain; charset=utf-8", headers)
            return
        user = authenticate(handler)
        ctx = Context(handler, user)
        body = (read_body(handler) if method in ("PUT", "PROPFIND", "PROPPATCH", "REPORT",
                                                 "MKCALENDAR", "MKCOL") else b"")
        holder[0] = body
        if method == "PROPFIND":
            handler.send(207, propfind(ctx, path, body), "application/xml; charset=utf-8", headers)
        elif method == "REPORT":
            handler.send(207, report(ctx, path, body), "application/xml; charset=utf-8", headers)
        elif method == "PROPPATCH":
            handler.send(207, proppatch(ctx, path, body), "application/xml; charset=utf-8", headers)
        elif method in ("GET", "HEAD"):
            kind, target = resolve(ctx, path)
            if kind != "object":
                handler.send(200, b"EasyLife CalDAV\n", "text/plain; charset=utf-8", headers)
                return
            obj = _target_object(ctx, kind, target)
            headers["ETag"] = obj["etag"]
            handler.send(200, obj["ics"].encode("utf-8"), "text/calendar; charset=utf-8", headers)
        elif method == "PUT":
            kind, target = resolve(ctx, path)
            if kind != "object":
                raise DavError(405, "不能写入集合")
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                raise DavError(400, "需要 UTF-8 编码")
            status = put_object(ctx.conn, ctx.uid, target[0], target[1], text, handler.headers,
                                ctx.tz)
            # No ETag: EasyLife normalises the data, so the client must fetch it again.
            handler.send(status, b"", "text/plain; charset=utf-8", headers)
        elif method == "DELETE":
            kind, target = resolve(ctx, path)
            if kind == "calendar":
                if target["is_default"]:
                    raise DavError(403, "默认列表不能删除")
                # Like Reminders itself: deleting a list deletes its reminders.
                lists.delete_list(ctx.conn, ctx.uid, target["id"], delete_todos=True)
            elif kind == "object":
                delete_object(ctx.conn, ctx.uid, target[0], target[1], handler.headers)
            else:
                raise DavError(403, "不能删除这个资源")
            handler.send(204, b"", "text/plain; charset=utf-8", headers)
        elif method in ("MKCALENDAR", "MKCOL"):
            mkcalendar(ctx, path, body)
            handler.send(201, b"", "text/plain; charset=utf-8", headers)
        else:
            raise DavError(405, "不支持该请求方法")
    except DavError as err:
        handler.close_if_body_unread()
        headers.update(err.headers)
        handler.send(err.status, (err.message + "\n").encode("utf-8"),
                     "text/plain; charset=utf-8", headers)
