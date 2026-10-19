"""Background e-mail reminders (DESIGN.md §5). Standard library only.

The daemon thread started by `app.py` wakes up every `interval` seconds and mails the
alerts that have come due, one e-mail per todo and round (iPhone model, see todos.py):

* the early alert at `remind_at` (提前提醒) -> `reminder_sent = 1`
* the alert at the due time `due_at` -> `due_alert_sent = 1`; an urgent task then gets
  `next_nag_at`, and is reminded again every few minutes until it is done or stopped.

Sending is injectable (`sender(cfg, message)`) so tests never touch a network.
"""

import email.utils
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

import datetime

from easylife import auth, calendar_ics, db, mail_settings, recurrence, todos

log = logging.getLogger("easylife.reminders")

DEFAULT_INTERVAL = 60
DEFAULT_PORT = 587
SSL_PORT = 465
SMTP_TIMEOUT = 15
SCAN_LIMIT = 100          # at most this many reminders per round

# ---------------------------------------------------------------- config

# Preserve the public normalization helper used by existing callers/tests.
normalize_config = mail_settings.normalize_config


def smtp_config(env=None):
    """Read SMTP configuration from the environment (`SMTP_*` variables).

    `host` empty means "log-only" mode: reminders are only logged, the todo stays
    unmarked (`reminder_sent = 0`) and no network connection is attempted, so the
    mail goes out once SMTP is configured.
    """
    return normalize_config(os.environ if env is None else env)


def _valid_address(value):
    name, addr = email.utils.parseaddr(value or "")
    return bool(addr) and "@" in addr and " " not in addr and len(value) <= 254


def _clean_header(value, limit=200):
    """Collapse newlines so user content cannot inject extra mail headers."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


# ---------------------------------------------------------------- message

def _value(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _before(due_at, remind_at):
    """'15 分钟' / '1 小时' / '1 天' between an early alert and the due time."""
    fmt = db.TIME_FORMAT
    minutes = int(round((datetime.datetime.strptime(due_at, fmt)
                         - datetime.datetime.strptime(remind_at, fmt)).total_seconds() / 60))
    if minutes % 1440 == 0:
        return "%d 天" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%d 小时" % (minutes // 60)
    return "%d 分钟" % minutes


def build_message(row, cfg, base_url="", kind="due"):
    """Build the plain-text reminder e-mail for one todo row (dict or sqlite3.Row).

    kind: 'early' (提前提醒), 'due' (到点提醒) or 'urgent' (紧急任务到点后的重复提醒)."""
    row = dict(row)
    title = _clean_header(row["title"])
    due, remind = _value(row, "due_at"), _value(row, "remind_at")
    urgent = bool(_value(row, "urgent"))
    from_addr = cfg.get("from") or "no-reply@localhost"
    msg = EmailMessage()
    if kind == "early" and due and remind and remind < due:
        subject = "提前提醒（还有 %s）：%s" % (_before(due, remind), title)
    elif kind == "urgent":
        subject = "【紧急】第 %d 次提醒：%s" % ((_value(row, "nag_count") or 0) + 2, title)
    else:
        subject = ("【紧急】" if urgent else "") + "任务提醒：%s" % title
    msg["Subject"] = "[EasyLife] " + subject
    msg["From"] = from_addr
    msg["To"] = row["email"]
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain="easylife")
    body = ["%s" % (row["title"] or "")]
    if due:
        body.append("时间：%s" % recurrence.local_label(due))
    elif remind:
        body.append("提醒：%s" % recurrence.local_label(remind))
    if row.get("details"):
        body.append("详情：%s" % row["details"])
    if base_url:
        body.append("任务链接：%s" % calendar_ics.task_link(row, base_url))
    if urgent:
        body.append("")
        body.append("这是紧急任务：到时间后每 %d 分钟提醒一次，直到完成（最多 %d 次）。"
                    "打开任务，标记完成或点「停止提醒」即可停止。"
                    % (todos.URGENT_INTERVAL // 60, todos.URGENT_MAX))
    msg.set_content("\n".join(body))
    return msg


def send_message(cfg, msg):
    """Default sender: real SMTP delivery, or a log line when SMTP is unset.

    Returns 'sent' or 'logged'. Raises on a real SMTP failure so the caller can
    retry on the next scan instead of marking the todo as reminded.
    """
    cfg = normalize_config(cfg)      # accept SMTP_*/lower-case/upper-case configs
    if not cfg["host"]:
        log.info("[reminder] (SMTP disabled) would send to %s subject=%s",
                 auth.mask_email(msg["To"]), msg["Subject"])
        return "logged"
    port = int(cfg["port"] or DEFAULT_PORT)
    if port == SSL_PORT:
        client = smtplib.SMTP_SSL(cfg["host"], port, timeout=SMTP_TIMEOUT)
    else:
        client = smtplib.SMTP(cfg["host"], port, timeout=SMTP_TIMEOUT)
    with client:
        client.ehlo()
        if port != SSL_PORT:
            if client.has_extn("starttls"):
                client.starttls()
                client.ehlo()
            else:
                log.warning("[reminder] %s:%s does not offer STARTTLS, "
                            "sending without transport encryption", cfg["host"], port)
        if cfg["user"]:
            client.login(cfg["user"], cfg["password"])
        refused = client.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    log.info("[reminder] SMTP accepted to %s subject=%s", auth.mask_email(msg["To"]), msg["Subject"])
    return "sent"


# ---------------------------------------------------------------- scanning

SCAN_SQL = (
    "SELECT t.id, t.user_id, t.title, t.details, t.due_at, t.remind_at, t.reminder_sent, "
    "t.due_alert_sent, t.urgent, t.nag_count, t.next_nag_at, u.email "
    "FROM todos t JOIN users u ON u.id = t.user_id "
    "WHERE t.status IN ('open', 'doing') AND ("
    "(t.reminder_sent = 0 AND t.remind_at IS NOT NULL AND t.remind_at <= :now) OR "
    "(t.due_alert_sent = 0 AND t.due_at IS NOT NULL AND t.due_at <= :now) OR "
    "(t.urgent = 1 AND t.next_nag_at IS NOT NULL AND t.next_nag_at <= :now)) "
    "ORDER BY MIN(COALESCE(t.remind_at, '9999'), COALESCE(t.due_at, '9999'), "
    "COALESCE(t.next_nag_at, '9999')) ASC, t.id ASC LIMIT :limit"
)


def scan_due(conn, now=None, limit=SCAN_LIMIT):
    """Todos with an alert due right now (design §5.2)."""
    return conn.execute(SCAN_SQL, {"now": now or db.now_utc(), "limit": limit}).fetchall()


def alert_kind(row, now=None):
    """Which alert of `row` is due: 'due', 'early' or 'urgent'.

    When the due time has also passed (e.g. the server was down) only the alert at
    the due time is sent; the missed early alert is not mailed afterwards."""
    now = now or db.now_utc()
    if not row["due_alert_sent"] and row["due_at"] and row["due_at"] <= now:
        return "due"
    if not row["reminder_sent"] and row["remind_at"] and row["remind_at"] <= now:
        return "early"
    return "urgent"


def _later(seconds):
    return db.now_utc(seconds)


def mark_sent(conn, todo_id, kind="early", row=None):
    """Atomically record a sent alert (only if it is still the pending one)."""
    now = db.now_utc()
    with db.tx(conn):
        if kind == "due":
            nag = _later(todos.URGENT_INTERVAL) if row is not None and row["urgent"] else None
            cur = conn.execute(
                "UPDATE todos SET due_alert_sent = 1, nag_count = 0, next_nag_at = ?, "
                "reminder_sent = CASE WHEN remind_at IS NOT NULL AND remind_at <= ? THEN 1 "
                "ELSE reminder_sent END, updated_at = ? "
                "WHERE id = ? AND due_alert_sent = 0 AND due_at = ?",
                (nag, now, now, todo_id, row["due_at"]))
        elif kind == "urgent":
            count = row["nag_count"] + 1
            nag = _later(todos.URGENT_INTERVAL) if count < todos.URGENT_MAX else None
            cur = conn.execute(
                "UPDATE todos SET nag_count = ?, next_nag_at = ?, updated_at = ? "
                "WHERE id = ? AND next_nag_at = ?",
                (count, nag, now, todo_id, row["next_nag_at"]))
        else:
            cur = conn.execute(
                "UPDATE todos SET reminder_sent = 1, updated_at = ? WHERE id = ? AND reminder_sent = 0",
                (now, todo_id))
    return cur.rowcount == 1


def _drop(reason, row):
    log.warning("[reminder] %s for todo=%s, skipped", reason, row["id"])


def deliver(row, *, smtp=None, sender=None, base_url="", kind="due"):
    """Send one reminder. Returns 'sent'/'logged', or None when it was skipped."""
    cfg = smtp_config() if smtp is None else normalize_config(smtp)
    sender = sender or send_message
    if not _valid_address(row["email"]):
        _drop("invalid recipient", row)
        return None
    return sender(cfg, build_message(row, cfg, base_url, kind))


def run_once(conn, *, now=None, smtp=None, sender=None, limit=SCAN_LIMIT, base_url="", cfg=None):
    """One scan-and-send round. Returns the list of todo ids that were reminded.

    An alert is only marked after a mail really went out. Two cases keep it pending
    so the next round retries: a delivery error, and log-only mode (no SMTP
    configured) -- once SMTP is configured the pending reminders are sent.
    """
    if smtp is None:
        if cfg is None:
            cfg = {"smtp": {key: os.environ.get("SMTP_" + key)
                            for key in ("HOST", "PORT", "USER", "PASS", "FROM")}}
        smtp = mail_settings.resolve_mail_config(conn, cfg)
    sent = []
    for row in scan_due(conn, now=now, limit=limit):
        todo_id = row["id"]
        kind = alert_kind(row, now)
        try:
            result = deliver(row, smtp=smtp, sender=sender, base_url=base_url, kind=kind)
        except Exception as exc:
            log.error("[reminder] could not send todo=%s error=%s, will retry next round",
                      todo_id, type(exc).__name__)
            continue
        if result is None:                     # invalid address: drop it quietly
            continue
        if result != "sent":
            log.info("[reminder] not sent yet (SMTP disabled) todo=%s kind=%s; "
                     "it stays pending and will be sent once SMTP is configured", todo_id, kind)
            continue
        if mark_sent(conn, todo_id, kind, row):
            sent.append(todo_id)
            log.info("[reminder] %s todo=%s kind=%s", result, todo_id, kind)
    return sent


def purge_expired(conn):
    """Housekeeping on the same thread: drop expired sessions (design §4.1)."""
    try:
        auth.purge_expired_sessions(conn)
    except Exception:
        log.exception("[reminder] session purge failed")


# ---------------------------------------------------------------- thread

def reminder_loop(stop_event, interval=DEFAULT_INTERVAL, *, db_path=None, smtp=None,
                  sender=None, base_url="", cfg=None):
    """Daemon-thread body: scan every `interval` seconds until stop_event is set."""
    if not db_path:
        log.warning("[reminder] no database path given, reminder thread not started")
        return
    log.info("[reminder] thread started (interval=%ss)", interval)
    try:
        while not stop_event.wait(interval):
            try:
                conn = db.get_conn(db_path)
                run_once(conn, smtp=smtp, sender=sender, base_url=base_url, cfg=cfg)
                purge_expired(conn)
            except Exception as exc:
                log.error("[reminder] reminder loop iteration failed error=%s", type(exc).__name__)
    finally:
        db.close_thread_conns()
    log.info("[reminder] thread stopped")


def start_thread(cfg, *, sender=None):
    """Start the daemon reminder thread for this server config."""
    interval = int(cfg.get("remind_interval", DEFAULT_INTERVAL))
    stop_event = threading.Event()
    if interval <= 0:
        log.info("[reminder] disabled (EASYLIFE_REMIND_INTERVAL=%s)", interval)
        return stop_event
    # Site settings become visible on the next round, at most 60s between scans.
    interval = min(interval, DEFAULT_INTERVAL)
    thread = threading.Thread(target=reminder_loop, name="easylife-reminders", daemon=True,
                              kwargs={"stop_event": stop_event, "interval": interval,
                                      "db_path": cfg["db"], "cfg": cfg,
                                      "sender": sender, "base_url": cfg.get("base_url", "")})
    thread.start()
    return stop_event
