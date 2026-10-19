"""Background e-mail reminders (DESIGN.md §5). Standard library only.

The daemon thread started by `app.py` wakes up every `interval` seconds, finds
open todos whose `remind_at` has passed and that have not been reminded yet,
sends one e-mail per todo through `smtplib` and marks `reminder_sent = 1`.
Sending is injectable (`sender(cfg, message)`) so tests never touch a network.
"""

import email.utils
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

from easylife import auth, calendar_ics, db, mail_settings

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

def build_message(row, cfg, base_url=""):
    """Build the plain-text reminder e-mail for one todo row (dict or sqlite3.Row)."""
    row = dict(row)
    title = _clean_header(row["title"])
    from_addr = cfg.get("from") or "no-reply@localhost"
    msg = EmailMessage()
    msg["Subject"] = "[EasyLife] 任务提醒：%s" % title
    msg["From"] = from_addr
    msg["To"] = row["email"]
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain="easylife")
    body = ["%s" % (row["title"] or "")]
    for label, value in (("截止", row["due_at"]), ("提醒", row["remind_at"]),
                         ("详情", row["details"])):
        if value:
            body.append("%s：%s" % (label, value))
    if base_url:
        body.append("任务链接：%s" % calendar_ics.task_link(row, base_url))
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
    "SELECT t.id, t.user_id, t.title, t.details, t.due_at, t.remind_at, u.email "
    "FROM todos t JOIN users u ON u.id = t.user_id "
    "WHERE t.reminder_sent = 0 AND t.remind_at IS NOT NULL AND t.remind_at <= ? "
    "AND t.status IN ('open', 'doing') ORDER BY t.remind_at ASC, t.id ASC LIMIT ?"
)


def scan_due(conn, now=None, limit=SCAN_LIMIT):
    """Todos that are due for a reminder right now (design §5.2)."""
    return conn.execute(SCAN_SQL, (now or db.now_utc(), limit)).fetchall()


def mark_sent(conn, todo_id):
    """Atomically mark a reminder as sent (only if it was still pending)."""
    with db.tx(conn):
        cur = conn.execute(
            "UPDATE todos SET reminder_sent = 1, updated_at = ? WHERE id = ? AND reminder_sent = 0",
            (db.now_utc(), todo_id))
    return cur.rowcount == 1


def _drop(reason, row):
    log.warning("[reminder] %s for todo=%s, skipped", reason, row["id"])


def deliver(row, *, smtp=None, sender=None, base_url=""):
    """Send one reminder. Returns 'sent'/'logged', or None when it was skipped."""
    cfg = smtp_config() if smtp is None else normalize_config(smtp)
    sender = sender or send_message
    if not _valid_address(row["email"]):
        _drop("invalid recipient", row)
        return None
    return sender(cfg, build_message(row, cfg, base_url))


def run_once(conn, *, now=None, smtp=None, sender=None, limit=SCAN_LIMIT, base_url="", cfg=None):
    """One scan-and-send round. Returns the list of todo ids that were reminded.

    The flag is only set after a mail really went out. Two cases keep it at 0 so
    the next round retries: a delivery error, and log-only mode (no SMTP
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
        try:
            result = deliver(row, smtp=smtp, sender=sender, base_url=base_url)
        except Exception as exc:
            log.error("[reminder] could not send todo=%s error=%s, will retry next round",
                      todo_id, type(exc).__name__)
            continue
        if result is None:                     # invalid address: drop it quietly
            continue
        if result != "sent":
            log.info("[reminder] not sent yet (SMTP disabled) todo=%s remind_at=%s; "
                     "reminder_sent stays 0 and it will be sent once SMTP is configured",
                     todo_id, row["remind_at"])
            continue
        if mark_sent(conn, todo_id):
            sent.append(todo_id)
            log.info("[reminder] %s todo=%s remind_at=%s", result, todo_id, row["remind_at"])
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
