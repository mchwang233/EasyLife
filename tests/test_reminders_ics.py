"""Tests for the reminder thread and the .ics export/subscription endpoints."""

import http.client
from email import message_from_string, policy
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app, calendar_ics, db, reminders  # noqa: E402

PASSWORD = "correct-horse-battery"
SMTP = {"host": "smtp.example.com", "port": 587, "user": "u", "password": "p",
        "from": "EasyLife <no-reply@example.com>"}


def make_db(tmp):
    """A fresh database with one user; returns (conn, user_id)."""
    path = os.path.join(tmp, "reminders.db")
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                 ("alice@example.com", "pbkdf2_sha256$1$x$y", db.now_utc()))
    user_id = conn.execute("SELECT id FROM users").fetchone()[0]
    return conn, user_id


def add_todo(conn, user_id, title="写项目周报", *, remind_at=None, status="open", due_at=None):
    cur = conn.execute(
        "INSERT INTO todos (user_id, title, details, status, priority, due_at, remind_at, "
        "reminder_sent, created_at, updated_at) VALUES (?, ?, ?, ?, 2, ?, ?, 0, ?, ?)",
        (user_id, title, "汇总本周进展", status, due_at, remind_at, db.now_utc(), db.now_utc()))
    return cur.lastrowid


def row_of(conn, todo_id):
    return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


class Recorder:
    """Injected replacement for reminders.send_message."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, cfg, msg):
        if self.fail:
            raise RuntimeError("smtp is down")
        self.calls.append(msg)
        return "sent"


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-remind-")
        self.conn, self.user_id = make_db(self.tmp)

    def tearDown(self):
        self.conn.close()
        db.close_thread_conns()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_due_reminder_is_sent_exactly_once(self):
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-120))
        sender = Recorder()
        sent = reminders.run_once(self.conn, sender=sender, smtp=SMTP)
        self.assertEqual(sent, [todo_id])
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0]["To"], "alice@example.com")
        self.assertIn("写项目周报", sender.calls[0]["Subject"])
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 1)

        # Second round (and a third) must not send again.
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [])
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [])
        self.assertEqual(len(sender.calls), 1)

    def test_reminder_with_base_url_includes_task_link(self):
        # 回归：扫描结果是 sqlite3.Row，base_url 非空时拼任务链接曾抛 AttributeError。
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-60))
        sender = Recorder()
        sent = reminders.run_once(self.conn, sender=sender, smtp=SMTP,
                                  base_url="https://easylife.example.com/")
        self.assertEqual(sent, [todo_id])
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("https://easylife.example.com/todos/%d" % todo_id,
                      sender.calls[0].get_content())
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 1)

    def test_reminder_thread_with_base_url_delivers(self):
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-60))
        self.conn.commit()
        sender = Recorder()
        stop = threading.Event()
        worker = threading.Thread(target=reminders.reminder_loop, args=(stop, 0.05), kwargs={
            "db_path": os.path.join(self.tmp, "reminders.db"), "smtp": SMTP,
            "sender": sender, "base_url": "https://easylife.example.com"})
        worker.start()
        try:
            deadline = time.time() + 5
            while not sender.calls and time.time() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            worker.join(5)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 1)

    def test_future_and_finished_todos_are_not_sent(self):
        future = add_todo(self.conn, self.user_id, remind_at=db.now_utc(3600))
        done = add_todo(self.conn, self.user_id, status="done", remind_at=db.now_utc(-60))
        archived = add_todo(self.conn, self.user_id, status="archived", remind_at=db.now_utc(-60))
        no_time = add_todo(self.conn, self.user_id)
        sender = Recorder()
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [])
        self.assertEqual(sender.calls, [])
        for todo_id in (future, done, archived, no_time):
            self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 0)

    def test_changing_remind_at_resets_the_flag(self):
        from easylife import todos
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-120),
                           due_at=db.now_utc(3600))
        sender = Recorder()
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [todo_id])
        todos.update_todo(self.conn, self.user_id, todo_id,
                          {"remind_at": db.now_utc(-60).replace(" ", "T") + "Z"})
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 0)
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [todo_id])
        self.assertEqual(len(sender.calls), 2)

    def test_missing_smtp_config_logs_and_does_not_crash(self):
        """No SMTP: log only, never mark -- and deliver once SMTP is configured."""
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-30))
        cfg = reminders.smtp_config({})           # no SMTP_* set at all
        self.assertIsNone(cfg["host"])
        for _ in range(3):                        # repeated rounds must not blow up
            with self.assertLogs("easylife.reminders", level="INFO") as logs:
                self.assertEqual(reminders.run_once(self.conn, smtp=cfg), [])
            self.assertTrue(any("SMTP disabled" in line for line in logs.output))
        # Not marked: the reminder is still pending, ready to be sent later.
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 0)

        # Configure SMTP afterwards: the pending reminder goes out, exactly once.
        sender = Recorder()
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [todo_id])
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("写项目周报", sender.calls[0]["Subject"])
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 1)
        self.assertEqual(reminders.run_once(self.conn, sender=sender, smtp=SMTP), [])
        self.assertEqual(len(sender.calls), 1)

    def test_send_failure_keeps_flag_and_retries(self):
        todo_id = add_todo(self.conn, self.user_id, remind_at=db.now_utc(-10))
        broken = Recorder(fail=True)
        with self.assertLogs("easylife.reminders", level="ERROR"):
            self.assertEqual(reminders.run_once(self.conn, sender=broken, smtp=SMTP), [])
        self.assertEqual(row_of(self.conn, todo_id)["reminder_sent"], 0)
        good = Recorder()
        self.assertEqual(reminders.run_once(self.conn, sender=good, smtp=SMTP), [todo_id])
        self.assertEqual(len(good.calls), 1)


class IcsFormatTests(unittest.TestCase):
    BASE = "https://easylife.example.com"

    def todo(self, **over):
        todo = {"id": 12, "title": "写项目周报", "details": "汇总本周进展\n下周计划",
                "status": "open", "priority": 3, "due_at": "2024-06-03 10:00:00",
                "remind_at": "2024-06-03 09:00:00"}
        todo.update(over)
        return todo

    def test_escape_and_fold(self):
        self.assertEqual(calendar_ics.escape_text("a;b,c\\d\r\ne\nf"),
                         "a\\;b\\,c\\\\d\\ne\\nf")
        long_title = "标题" * 40
        physical = calendar_ics.fold_line("SUMMARY:" + long_title)
        self.assertGreater(len(physical), 1)
        self.assertTrue(all(len(p.encode("utf-8")) <= 75 for p in physical))
        self.assertTrue(all(p.startswith(" ") for p in physical[1:]))

    def test_single_todo_ics_uses_crlf_and_required_fields(self):
        out = calendar_ics.todo_to_ics(self.todo(), base_url=self.BASE)
        self.assertTrue(out.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", out.replace("\r\n", ""))   # CRLF only
        for token in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "END:VEVENT",
                      "UID:easylife-todo-12@easylife", "DTSTAMP:", "DTSTART:20240603T100000Z",
                      "DTEND:20240603T103000Z", "SUMMARY:写项目周报", "STATUS:CONFIRMED",
                      "PRIORITY:1", "BEGIN:VALARM", "TRIGGER:-PT60M"):
            self.assertIn(token, out)
        for line in out.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)

    def test_dtstart_falls_back_to_remind_at_and_requires_a_time(self):
        out = calendar_ics.todo_to_ics(self.todo(due_at=None), base_url=self.BASE)
        self.assertIn("DTSTART:20240603T090000Z", out)
        self.assertIn("TRIGGER:-PT30M", out)   # no due_at -> default 30 minutes
        with self.assertRaises(calendar_ics.IcsError):
            calendar_ics.todo_to_ics(self.todo(due_at=None, remind_at=None))

    def test_feed_has_all_open_events_and_skips_others(self):
        rows = [self.todo(),
                self.todo(id=13, title="第二件事", status="doing", due_at="2024-06-04 08:00:00",
                          remind_at=None),
                self.todo(id=14, title="已完成", status="done"),
                self.todo(id=15, title="归档", status="archived"),
                self.todo(id=16, title="没有时间", due_at=None, remind_at=None)]
        out = calendar_ics.calendar_feed(rows, base_url=self.BASE)
        self.assertIn("BEGIN:VCALENDAR", out)
        self.assertIn("X-WR-CALNAME", out)
        self.assertEqual(out.count("BEGIN:VEVENT"), 2)
        self.assertNotIn("已完成", out)
        self.assertNotIn("没有时间", out)
        self.assertLess(out.index("UID:easylife-todo-12"), out.index("UID:easylife-todo-13"))

    def test_google_calendar_url(self):
        url = calendar_ics.google_calendar_url(self.todo(), base_url=self.BASE)
        self.assertTrue(url.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE"))
        self.assertIn("dates=20240603T100000Z%2F20240603T103000Z", url)
        self.assertIn("%E5%86%99%E9%A1%B9%E7%9B%AE%E5%91%A8%E6%8A%A5", url)  # 写项目周报
        self.assertIn("easylife.example.com%2Ftodos%2F12", url)
        self.assertNotIn("dates=", calendar_ics.google_calendar_url(
            self.todo(due_at=None, remind_at=None)))

    def test_fold_line_never_breaks_multibyte_characters(self):
        line = "DESCRIPTION:" + "中文测试" * 30
        for physical in calendar_ics.fold_line(line):
            physical.encode("utf-8").decode("utf-8")
        joined = "".join(p if i == 0 else p[1:]
                         for i, p in enumerate(calendar_ics.fold_line(line)))
        self.assertEqual(joined, line)


class FakeSMTP:
    """Stand-in for smtplib.SMTP/SSL that records calls instead of using the network."""

    instances = []
    messages = []
    offers_starttls = True

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("close",))
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def has_extn(self, name):
        return self.offers_starttls and name.lower() == "starttls"

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"]))
        FakeSMTP.messages.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


class SendPathTests(unittest.TestCase):
    """The default sender must speak SMTP_SSL on 465 and STARTTLS on 587.

    These tests patch `reminders.smtplib`, so no socket is ever opened; they
    guard the production path (not the injectable `sender`) that P1 broke.
    """

    def setUp(self):
        FakeSMTP.instances = []
        FakeSMTP.messages = []
        FakeSMTP.offers_starttls = True
        patch = mock.patch.object(reminders, "smtplib",
                                  types.SimpleNamespace(SMTP=FakeSMTP, SMTP_SSL=FakeSMTPSSL))
        patch.start()
        self.addCleanup(patch.stop)

    def message(self):
        """A message built from a sqlite3.Row (the shape scan_due() returns)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 AS id, 'alice@example.com' AS email, '周报' AS title, "
            "NULL AS details, NULL AS due_at, '2024-06-03 09:00:00' AS remind_at").fetchone()
        msg = reminders.build_message(row, {"from": "no-reply@example.com"},
                                      "https://easylife.example.com")
        conn.close()
        return msg

    def test_port_465_uses_smtp_ssl_without_starttls(self):
        cfg = reminders.smtp_config({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465",
                                     "SMTP_USER": "u@example.com", "SMTP_PASS": "pw"})
        self.assertEqual(reminders.send_message(cfg, self.message()), "sent")
        client, = FakeSMTP.instances
        self.assertIsInstance(client, FakeSMTPSSL)
        self.assertEqual((client.host, client.port), ("smtp.example.com", 465))
        self.assertEqual([c for c in client.calls if c[0] != "close"],
                         [("ehlo",), ("login", "u@example.com", "pw"),
                          ("send", "alice@example.com")])

    def test_port_587_upgrades_with_starttls(self):
        cfg = reminders.smtp_config({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                                     "SMTP_USER": "u@example.com", "SMTP_PASS": "pw"})
        self.assertEqual(reminders.send_message(cfg, self.message()), "sent")
        client, = FakeSMTP.instances
        self.assertNotIsInstance(client, FakeSMTPSSL)
        self.assertEqual([c[0] for c in client.calls],
                         ["ehlo", "starttls", "ehlo", "login", "send", "close"])

    def test_port_587_without_starttls_logs_a_warning_but_still_sends(self):
        FakeSMTP.offers_starttls = False
        cfg = reminders.smtp_config({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                                     "SMTP_USER": "u@example.com", "SMTP_PASS": "pw"})
        with self.assertLogs("easylife.reminders", level="WARNING") as logs:
            self.assertEqual(reminders.send_message(cfg, self.message()), "sent")
        self.assertTrue(any("STARTTLS" in line for line in logs.output))
        client, = FakeSMTP.instances
        self.assertNotIn("starttls", [c[0] for c in client.calls])
        self.assertIn("send", [c[0] for c in client.calls])

    def test_run_once_uses_the_real_sender_with_a_non_empty_base_url(self):
        """P1 回归：scan_due() 的 sqlite3.Row + 真实 send_message + 非空 BASE_URL。

        之前 `task_link()` 用 `sqlite3.Row.get()`，只要配了 `BASE_URL`，生产路径
        每封提醒都抛 AttributeError，一封也发不出去。
        """
        tmp = tempfile.mkdtemp(prefix="easylife-live-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "live.db")
        db.init_db(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                     ("alice@example.com", "pbkdf2_sha256$1$x$y", db.now_utc()))
        user_id = conn.execute("SELECT id FROM users").fetchone()[0]
        todo_id = add_todo(conn, user_id, remind_at=db.now_utc(-60), due_at=db.now_utc(3600))
        cfg = reminders.smtp_config({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                                     "SMTP_USER": "u@example.com", "SMTP_PASS": "pw",
                                     "SMTP_FROM": "EasyLife <no-reply@example.com>"})
        live = "https://live.example.com"

        self.assertEqual(reminders.run_once(conn, smtp=cfg, base_url=live), [todo_id])
        msg, = FakeSMTP.messages
        self.assertEqual(msg["To"], "alice@example.com")
        self.assertIn("https://live.example.com/todos/%d" % todo_id, msg.get_content())
        self.assertEqual(row_of(conn, todo_id)["reminder_sent"], 1)

        # 下一轮（同一个守护线程会在一分钟后执行）不得重复发送。
        self.assertEqual(reminders.run_once(conn, smtp=cfg, base_url=live), [])
        self.assertEqual(len(FakeSMTP.messages), 1)


def row_todo(**over):
    """A `sqlite3.Row` shaped like the ones `scan_due()` and raw SQL return."""
    values = {"id": 12, "title": "写项目周报", "details": "汇总本周进展", "status": "open",
              "priority": 3, "due_at": "2024-06-03 10:00:00", "remind_at": "2024-06-03 09:00:00",
              "email": "alice@example.com"}
    values.update(over)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT :id AS id, :title AS title, :details AS details, :status AS status, "
            ":priority AS priority, :due_at AS due_at, :remind_at AS remind_at, "
            ":email AS email", values).fetchone()
    finally:
        conn.close()


class RowCompatibilityTests(unittest.TestCase):
    """`sqlite3.Row` has no `.get()`, so every calendar_ics helper must cope.

    This is the same defect class as P1, checked at the module level: passing a
    raw row where a todo dict is expected must never raise AttributeError.
    """

    BASE = "https://easylife.example.com"

    def test_task_link_and_message_accept_rows(self):
        row = row_todo()
        self.assertEqual(calendar_ics.task_link(row, self.BASE + "/"),
                         self.BASE + "/todos/12")
        msg = reminders.build_message(row, {"from": "no-reply@example.com"}, self.BASE)
        self.assertEqual(msg["To"], "alice@example.com")
        self.assertIn(self.BASE + "/todos/12", msg.get_content())

    def unfold(self, text):
        """Logical lines: a folded line continues after CRLF + one space."""
        return text.replace(calendar_ics.CRLF + " ", "")

    def test_todo_to_ics_accepts_a_row(self):
        out = calendar_ics.todo_to_ics(row_todo(), base_url=self.BASE)
        self.assertTrue(out.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", out.replace("\r\n", ""))
        for token in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:easylife-todo-12@easylife",
                      "DTSTART:20240603T100000Z", "SUMMARY:写项目周报",
                      "PRIORITY:1", "TRIGGER:-PT60M", "END:VCALENDAR"):
            self.assertIn(token, out)
        self.assertIn(self.BASE + "/todos/12", self.unfold(out))

    def test_feed_and_google_url_accept_rows(self):
        rows = [row_todo(), row_todo(id=13, title="已完成", status="done"),
                row_todo(id=14, title="没时间", due_at=None, remind_at=None)]
        feed = calendar_ics.calendar_feed(rows, base_url=self.BASE)
        self.assertEqual(feed.count("BEGIN:VEVENT"), 1)
        self.assertIn("UID:easylife-todo-12@easylife", feed)
        url = calendar_ics.google_calendar_url(row_todo(), base_url=self.BASE)
        self.assertIn("dates=20240603T100000Z%2F20240603T103000Z", url)

    def test_rows_missing_columns_or_times_fall_back_instead_of_raising(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            sparse = conn.execute(
                "SELECT 7 AS id, '只有时间' AS title, '2024-06-03 10:00:00' AS due_at"
            ).fetchone()
        finally:
            conn.close()
        out = calendar_ics.todo_to_ics(sparse, base_url=self.BASE)
        self.assertIn("UID:easylife-todo-7@easylife", out)
        self.assertIn("PRIORITY:5", out)          # priority default 2 -> 5
        self.assertIn("STATUS:CONFIRMED", out)
        self.assertIn("TRIGGER:PT0M", out)        # no remind_at -> alert at the due time
        self.assertIn("DESCRIPTION:优先级 2", self.unfold(out))

        timed_out = row_todo(due_at=None, remind_at=None)
        with self.assertRaises(calendar_ics.IcsError):
            calendar_ics.todo_to_ics(timed_out)
        self.assertEqual(calendar_ics.calendar_feed([timed_out]).count("BEGIN:VEVENT"), 0)
        self.assertNotIn("dates=", calendar_ics.google_calendar_url(timed_out))


class Client:
    """Minimal HTTP client that keeps the session cookie and CSRF token."""

    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def request(self, method, path, body=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf and self.csrf and method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        headers_out = dict(resp.getheaders())
        conn.close()
        set_cookie = headers_out.get("Set-Cookie")
        if set_cookie and set_cookie.startswith(app.COOKIE_NAME + "="):
            self.cookie = set_cookie.split(";", 1)[0]
        ctype = headers_out.get("Content-Type", "")
        payload = json.loads(raw) if raw and ctype.startswith("application/json") else raw
        if isinstance(payload, dict) and "csrf_token" in payload:
            self.csrf = payload["csrf_token"]
        return resp, payload, headers_out

    def register(self, email):
        return self.request("POST", "/api/register", {"email": email, "password": PASSWORD})


class IcsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-ics-")
        cfg = app.load_config({"EASYLIFE_PORT": "0",
                               "EASYLIFE_DB": os.path.join(self.tmp, "test.db"),
                               "EASYLIFE_SECRET": "test-secret-0123456789abcdef",
                               "BASE_URL": "https://easylife.example.com"})
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        db.close_thread_conns()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def logged_in(self, email="alice@example.com"):
        c = Client(self.port)
        self.assertEqual(c.register(email)[0].status, 201)
        return c

    def new_todo(self, c, **fields):
        body = {"title": "写项目周报", "details": "汇总本周进展", "priority": 3,
                "due_at": "2024-06-03T10:00:00Z", "remind_at": "2024-06-03 09:00:00"}
        body.update(fields)
        resp, todo, _ = c.request("POST", "/api/todos", body)
        self.assertEqual(resp.status, 201)
        return todo

    def test_ics_download(self):
        c = self.logged_in()
        todo = self.new_todo(c)
        resp, body, headers = c.request("GET", "/api/todos/%d/ics" % todo["id"])
        self.assertEqual(resp.status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
        self.assertIn('attachment; filename="easylife-%d.ics"' % todo["id"],
                      headers["Content-Disposition"])
        text = body.decode("utf-8")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        for token in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "DTSTART:20240603T100000Z",
                      "UID:easylife-todo-%d@easylife" % todo["id"], "BEGIN:VALARM",
                      "TRIGGER:-PT60M"):
            self.assertIn(token, text)

    def test_ics_errors(self):
        c = self.logged_in()
        no_time = self.new_todo(c, title="没有时间", due_at=None, remind_at=None)
        resp, body, _ = c.request("GET", "/api/todos/%d/ics" % no_time["id"])
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertEqual(c.request("GET", "/api/todos/999999/ics")[0].status, 404)
        other = self.logged_in("bob@example.com")
        self.assertEqual(other.request("GET", "/api/todos/%d/ics" % no_time["id"])[0].status, 404)
        self.assertEqual(Client(self.port).request("GET", "/api/todos/1/ics")[0].status, 401)

    def test_google_calendar_url_is_in_todo_responses(self):
        c = self.logged_in()
        todo = self.new_todo(c)
        for payload in (todo, c.request("GET", "/api/todos/%d" % todo["id"])[1],
                        c.request("GET", "/api/todos")[1]["items"][0]):
            self.assertTrue(payload["google_calendar_url"].startswith(
                "https://calendar.google.com/calendar/render?action=TEMPLATE"))
            self.assertIn("dates=20240603T100000Z", payload["google_calendar_url"])
            self.assertEqual(payload["ics_url"], "/api/todos/%d/ics" % todo["id"])

    def test_feed_requires_a_valid_private_token(self):
        c = self.logged_in()
        todo = self.new_todo(c)
        me = c.request("GET", "/api/me")[1]
        token = me["calendar_token"]
        self.assertTrue(token and len(token) >= 20)
        self.assertEqual(me["calendar_feed_url"],
                         "https://easylife.example.com/api/calendar.ics?token=" + token)

        anon = Client(self.port)
        resp, body, headers = anon.request("GET", "/api/calendar.ics?token=" + token)
        self.assertEqual(resp.status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        text = body.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("BEGIN:VEVENT", text)
        self.assertIn("UID:easylife-todo-%d@easylife" % todo["id"], text)
        self.assertNotIn("\n", text.replace("\r\n", ""))

        self.assertEqual(anon.request("GET", "/api/calendar.ics?token=wrong-token")[0].status, 403)
        self.assertEqual(anon.request("GET", "/api/calendar.ics")[0].status, 400)
        # 令牌是随机的：直接把末位拼成 "x" 有 1/64 的概率与原令牌相同（测试会偶发失败），
        # 所以先确认末位不是 "x" 再替换。
        tampered = token[:-1] + ("x" if not token.endswith("x") else "y")
        self.assertNotEqual(tampered, token)
        self.assertEqual(anon.request("GET", "/api/calendar.ics?token=" + tampered)[0].status, 403)

    def test_feed_only_lists_this_users_open_todos(self):
        a, b = self.logged_in("a@example.com"), self.logged_in("b@example.com")
        self.new_todo(a, title="A 的任务")
        self.new_todo(a, title="已完成", status="done")
        self.new_todo(b, title="B 的任务")
        token = a.request("GET", "/api/me")[1]["calendar_token"]
        text = Client(self.port).request(
            "GET", "/api/calendar.ics?token=" + token)[1].decode("utf-8")
        self.assertIn("A 的任务", text)
        self.assertNotIn("B 的任务", text)
        self.assertNotIn("已完成", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), 1)

    def test_token_reset_invalidates_the_old_url(self):
        c = self.logged_in()
        old = c.request("GET", "/api/me")[1]["calendar_token"]
        resp, body, _ = c.request("POST", "/api/calendar/token/reset")
        self.assertEqual(resp.status, 200)
        new = body["calendar_token"]
        self.assertNotEqual(old, new)
        self.assertIn(new, body["calendar_feed_url"])
        anon = Client(self.port)
        self.assertEqual(anon.request("GET", "/api/calendar.ics?token=" + old)[0].status, 403)
        self.assertEqual(anon.request("GET", "/api/calendar.ics?token=" + new)[0].status, 200)
        self.assertEqual(c.request("POST", "/api/calendar/token/reset", csrf=False)[0].status, 403)


class SubscriptionTokenLifecycleTests(unittest.TestCase):
    """P3：订阅 token 存于 `users.calendar_token`（随机列），与 `EASYLIFE_SECRET` 解耦。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-feed-")
        self.db_path = os.path.join(self.tmp, "feed.db")
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(5)
        db.close_thread_conns()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_server(self, secret):
        """Boot a server against the shared database file, return its port."""
        cfg = app.load_config({"EASYLIFE_PORT": "0", "EASYLIFE_DB": self.db_path,
                               "EASYLIFE_SECRET": secret,
                               "BASE_URL": "https://easylife.example.com"})
        server = app.make_server(cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server.server_address[1]

    def test_changing_the_secret_does_not_invalidate_the_feed_url(self):
        c = Client(self.start_server("first-secret-0123456789abcdef"))
        self.assertEqual(c.register("alice@example.com")[0].status, 201)
        resp, todo, _ = c.request("POST", "/api/todos", {
            "title": "订阅里的任务", "due_at": "2024-06-03T10:00:00Z",
            "remind_at": "2024-06-03 09:00:00"})
        self.assertEqual(resp.status, 201)
        token = c.request("GET", "/api/me")[1]["calendar_token"]
        self.assertTrue(token)

        # 同一数据库、不同 EASYLIFE_SECRET 的新进程：旧订阅 URL 仍然可用。
        second = self.start_server("second-secret-0123456789abcdef")
        resp, body, _ = Client(second).request("GET", "/api/calendar.ics?token=" + token)
        self.assertEqual(resp.status, 200)
        self.assertIn("UID:easylife-todo-%d@easylife" % todo["id"], body.decode("utf-8"))

        # 但 token 只对本人有效：另一个用户拿不到别人的任务。
        other = Client(second)
        self.assertEqual(other.register("bob@example.com")[0].status, 201)
        other_token = other.request("GET", "/api/me")[1]["calendar_token"]
        self.assertNotEqual(other_token, token)
        text = Client(second).request(
            "GET", "/api/calendar.ics?token=" + other_token)[1].decode("utf-8")
        self.assertNotIn("订阅里的任务", text)


class MiniSMTPServer(threading.Thread):
    """A tiny ESMTP server on 127.0.0.1 that records the messages it receives.

    Lets the *production* delivery path (`reminders.send_message` -> `smtplib`)
    run for real without touching the network outside localhost. It advertises no
    STARTTLS, which is exactly the case that raised `AttributeError` in P1.
    """

    daemon = True

    def __init__(self):
        super().__init__()
        self._messages = []
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def received(self):
        with self._lock:
            return list(self._messages)

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return                      # socket closed: shut down
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self.sock.close()

    def _record(self, lines):
        with self._lock:
            self._messages.append("\n".join(lines))

    def _serve(self, conn):
        with conn, conn.makefile("rwb") as stream:
            stream.write(b"220 mini ESMTP\r\n")
            stream.flush()
            data, in_data = [], False
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if in_data:
                    if line == ".":
                        in_data = False
                        self._record(data)
                        data = []
                        stream.write(b"250 OK queued\r\n")
                    else:
                        # smtplib dots leading periods on the wire; undo that here.
                        data.append(line[1:] if line.startswith("..") else line)
                else:
                    verb = line.split(" ", 1)[0].upper()
                    if verb == "EHLO":
                        stream.write(b"250-mini\r\n250 SIZE 10485760\r\n")
                    elif verb in ("HELO", "MAIL", "RCPT", "RSET", "NOOP"):
                        stream.write(b"250 OK\r\n")
                    elif verb == "DATA":
                        in_data = True
                        stream.write(b"354 send data\r\n")
                    elif verb == "QUIT":
                        stream.write(b"221 bye\r\n")
                        stream.flush()
                        return
                    else:
                        stream.write(b"250 OK\r\n")
                stream.flush()


class ProductionReminderPathTests(unittest.TestCase):
    """P1 回归（完整生产路径）：非空 BASE_URL + 真实 smtplib + 后台守护线程。

    生产环境里 `BASE_URL` 总是非空，而 `scan_due()` 返回的是 `sqlite3.Row`；
    只要有人把 `Row` 交给 `.get()`，每封提醒都会在 `task_link()` 里抛
    `AttributeError`，一封也发不出去（`reminder_sent` 永远停在 0）。这条用例
    用一个本地迷你 SMTP 服务器把「HTTP 建任务 → 守护线程扫描 → smtplib 投递」
    整条链路跑通，断言邮件里带任务链接、标记被置 1、且只投递一次。
    """

    def setUp(self):
        self.smtp = MiniSMTPServer()
        self.smtp.start()
        self.addCleanup(self.smtp.close)
        self.tmp = tempfile.mkdtemp(prefix="easylife-prod-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = app.load_config({
            "EASYLIFE_PORT": "0",
            "EASYLIFE_DB": os.path.join(self.tmp, "prod.db"),
            "EASYLIFE_SECRET": "test-secret-0123456789abcdef",
            "BASE_URL": "https://live.example.com",     # 非空：P1 的触发条件
            "EASYLIFE_REMIND_INTERVAL": "1",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(self.smtp.port),
            "SMTP_FROM": "EasyLife <no-reply@live.example.com>",
        })
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.stop = self.server.start_reminders()
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.stop.set()
        self.server.stop_reminders()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(5)
        db.close_thread_conns()
        time.sleep(0.05)                # let the SMTP writer finish its last line

    def wait_for_mail(self, deadline=15):
        limit = time.time() + deadline
        while time.time() < limit:
            messages = self.smtp.received()[self.mail_offset:]
            if messages:
                return messages
            time.sleep(0.1)
        return self.smtp.received()[self.mail_offset:]

    def test_daemon_thread_delivers_with_a_non_empty_base_url(self):
        client = Client(self.port)
        self.assertEqual(client.register("live@example.com")[0].status, 201)
        # SMTP-enabled registration now requires mailbox verification. Keep the
        # production SMTP path for both messages, then count reminders separately.
        verification, = self.smtp.received()
        mail = message_from_string(verification, policy=policy.default)
        token = re.search(r"token=([A-Za-z0-9_-]+)", mail.get_content()).group(1)
        self.assertEqual(client.request("POST", "/api/verify-email", {"token": token})[0].status, 200)
        self.assertEqual(client.request("POST", "/api/login", {
            "email": "live@example.com", "password": PASSWORD})[0].status, 200)
        self.mail_offset = 1
        resp, todo, _ = client.request("POST", "/api/todos", {
            "title": "生产路径提醒", "details": "回归 P1",
            "due_at": db.now_utc(900), "remind_at": db.now_utc(-120)})
        self.assertEqual(resp.status, 201)

        messages = self.wait_for_mail()
        self.assertEqual(len(messages), 1,
                         "非空 BASE_URL 下应通过 smtplib 真发出一封提醒邮件")
        mail = messages[0]
        self.assertIn("To: live@example.com", mail)
        self.assertIn("https://live.example.com/todos/%d" % todo["id"], mail)
        self.assertEqual(client.request("GET", "/api/todos/%d" % todo["id"])[1]["reminder_sent"], 1)

        # 下一轮扫描（1 秒后）不得重复投递同一封提醒。
        time.sleep(2.2)
        self.assertEqual(len(self.smtp.received()[self.mail_offset:]), 1)


if __name__ == "__main__":
    unittest.main()
