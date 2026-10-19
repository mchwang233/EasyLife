"""iPhone-style reminders, urgent / repeating / flagged tasks, subtasks, tags and images."""

import datetime
import http.client
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app, db, recurrence, reminders, todos  # noqa: E402
from tests.test_api import Client  # noqa: E402
from tests.test_caldav import CalDavTestCase  # noqa: E402

SMTP = {"host": "smtp.example.com", "port": 587, "user": "u", "password": "p",
        "from": "EasyLife <no-reply@example.com>"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def iso(seconds):
    return db.now_utc(seconds).replace(" ", "T") + "Z"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, msg):
        self.calls.append(msg)
        return "sent"


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-features-")
        self.path = os.path.join(self.tmp, "features.db")
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                          ("alice@example.com", "x", db.now_utc()))
        self.user = 1
        recurrence.set_zone(datetime.timezone(datetime.timedelta(hours=8)))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def create(self, **data):
        data.setdefault("title", "任务")
        return todos.create_todo(self.conn, self.user, data)

    def update(self, todo_id, **data):
        return todos.update_todo(self.conn, self.user, todo_id, data)

    def row(self, todo_id):
        return self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()

    def run_round(self, now=None):
        sender = Recorder()
        sent = reminders.run_once(self.conn, sender=sender, smtp=SMTP, now=now)
        return sent, sender.calls


class ReminderModelTests(DbTestCase):
    def test_alert_at_due_time_and_early_alert(self):
        todo = self.create(due_at=iso(3600), remind_at=iso(-60))
        sent, mails = self.run_round()
        self.assertEqual(sent, [todo["id"]])
        self.assertIn("提前提醒（还有", mails[0]["Subject"])
        self.assertEqual(self.run_round(), ([], []))
        sent, mails = self.run_round(now=db.now_utc(3700))
        self.assertEqual(sent, [todo["id"]])
        self.assertIn("任务提醒：任务", mails[0]["Subject"])
        self.assertNotIn("紧急", mails[0]["Subject"])
        self.assertIn("时间：", mails[0].get_content())
        self.assertEqual(self.row(todo["id"])["due_alert_sent"], 1)
        self.assertEqual(self.run_round(now=db.now_utc(7200)), ([], []))

    def test_missed_early_alert_is_not_sent_after_the_due_time(self):
        todo = self.create(due_at=iso(-30), remind_at=iso(-90))
        sent, mails = self.run_round()
        self.assertEqual(len(mails), 1)
        self.assertNotIn("提前", mails[0]["Subject"])
        row = self.row(todo["id"])
        self.assertEqual((row["due_alert_sent"], row["reminder_sent"]), (1, 1))

    def test_times_set_in_the_past_are_not_mailed(self):
        todo = self.create(due_at=iso(-86400))
        self.assertEqual(self.row(todo["id"])["due_alert_sent"], 1)
        self.assertEqual(self.run_round(), ([], []))
        self.update(todo["id"], due_at=iso(-10))     # just now: still mailed
        self.assertEqual(self.run_round()[0], [todo["id"]])

    def test_urgent_task_is_reminded_again_until_done(self):
        todo = self.create(due_at=iso(-30), urgent=True)
        sent, mails = self.run_round()
        self.assertIn("【紧急】任务提醒", mails[0]["Subject"])
        self.assertIn("每 10 分钟", mails[0].get_content())
        row = self.row(todo["id"])
        self.assertIsNotNone(row["next_nag_at"])
        self.assertEqual(self.run_round(), ([], []))                    # not before the interval
        sent, mails = self.run_round(now=db.now_utc(todos.URGENT_INTERVAL + 5))
        self.assertIn("【紧急】第 2 次提醒", mails[0]["Subject"])
        self.assertEqual(self.row(todo["id"])["nag_count"], 1)
        self.update(todo["id"], status="done")
        self.assertIsNone(self.row(todo["id"])["next_nag_at"])
        self.assertEqual(self.run_round(now=db.now_utc(3 * todos.URGENT_INTERVAL)), ([], []))

    def test_urgent_reminders_can_be_stopped_and_are_capped(self):
        todo = self.create(due_at=iso(-30), urgent=True)
        self.run_round()
        self.assertIsNone(todos.stop_alerts(self.conn, self.user, todo["id"])["next_nag_at"])
        self.assertEqual(self.run_round(now=db.now_utc(3600)), ([], []))
        other = self.create(due_at=iso(-30), urgent=True)
        self.run_round()
        self.conn.execute("UPDATE todos SET nag_count = ?, next_nag_at = ? WHERE id = ?",
                          (todos.URGENT_MAX - 1, db.now_utc(-1), other["id"]))
        self.assertEqual(self.run_round()[0], [other["id"]])
        self.assertIsNone(self.row(other["id"])["next_nag_at"])

    def test_turning_on_urgent_for_an_overdue_task_starts_reminding(self):
        todo = self.create(due_at=iso(-30))
        self.run_round()
        self.update(todo["id"], urgent=True)
        sent, mails = self.run_round()
        self.assertEqual(sent, [todo["id"]])
        self.assertIn("【紧急】", mails[0]["Subject"])

    def test_remind_only_clients_get_a_due_time(self):
        todo = self.create(remind_at=iso(600))
        self.assertIsNone(todo["remind_at"])
        self.assertEqual(todo["due_at"], db.now_utc(600))

    def test_migration_moves_old_reminders_without_double_mails(self):
        now = db.now_utc()
        rows = [  # (due_at, remind_at, reminder_sent)
            (db.now_utc(86400), db.now_utc(3600), 0),        # early alert stays
            (db.now_utc(86400), db.now_utc(90000), 0),       # pending, after the due time -> its time
            (db.now_utc(-3600), db.now_utc(-3600), 1),       # sent, at the due time
            (None, db.now_utc(7200), 0),                     # no due time -> becomes the due time
            (db.now_utc(-3600), db.now_utc(86400), 0),       # pending reminder after a passed due
        ]
        ids = []
        for due, remind, sent in rows:
            ids.append(self.conn.execute(
                "INSERT INTO todos (user_id, title, status, priority, due_at, remind_at, "
                "reminder_sent, created_at, updated_at) VALUES (1, 't', 'open', 2, ?, ?, ?, ?, ?)",
                (due, remind, sent, now, now)).lastrowid)
        db.migrate_due_alerts(self.conn)
        get = lambda i: tuple(self.row(i)[k] for k in ("due_at", "remind_at", "due_alert_sent",  # noqa: E731
                                                       "reminder_sent"))
        self.assertEqual(get(ids[0]), (rows[0][0], rows[0][1], 0, 0))
        self.assertEqual(get(ids[1]), (rows[1][1], None, 0, 0))
        self.assertEqual(get(ids[2]), (rows[2][0], None, 1, 0))
        self.assertEqual(get(ids[3]), (rows[3][1], None, 0, 0))
        self.assertEqual(get(ids[4]), (rows[4][1], None, 0, 0))


class RepeatTests(DbTestCase):
    def test_completing_a_repeating_task_moves_it_to_the_next_date(self):
        due = db.now_utc(-3600)
        todo = self.create(due_at=due, remind_at=db.now_utc(-4500), repeat="FREQ=DAILY")
        done = self.update(todo["id"], status="done")
        self.assertEqual(done["status"], "open")
        self.assertGreater(done["due_at"], db.now_utc())
        self.assertEqual(done["repeated"]["from"], due)
        fmt = db.TIME_FORMAT
        gap = (datetime.datetime.strptime(done["due_at"], fmt)
               - datetime.datetime.strptime(done["remind_at"], fmt))
        self.assertEqual(gap, datetime.timedelta(minutes=15))     # the early alert moves along
        self.assertEqual(done["due_alert_sent"], 0)
        self.assertIn("完成了一次", done["notes"][-1]["content"])

    def test_next_dates(self):
        nd = recurrence.next_due
        old = "2026-01-30 01:00:00"           # Friday 09:00 (+08:00)
        self.assertEqual(nd(old, "FREQ=DAILY", now=old), ("2026-01-31 01:00:00", "FREQ=DAILY"))
        workdays = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        self.assertEqual(nd(old, workdays, now=old)[0], "2026-02-02 01:00:00")   # Monday
        self.assertEqual(nd(old, "FREQ=WEEKLY;INTERVAL=2", now=old)[0], "2026-02-13 01:00:00")
        self.assertEqual(nd("2026-01-31 01:00:00", "FREQ=MONTHLY", now=old)[0],
                         "2026-02-28 01:00:00")
        self.assertEqual(nd(old, "FREQ=YEARLY", now=old)[0], "2027-01-30 01:00:00")
        self.assertEqual(nd(old, "FREQ=DAILY;COUNT=3", now=old), ("2026-01-31 01:00:00",
                                                                  "FREQ=DAILY;COUNT=2"))
        self.assertIsNone(nd(old, "FREQ=DAILY;COUNT=1", now=old))
        self.assertIsNone(nd(old, "FREQ=DAILY;UNTIL=20260130", now=old))
        # Overdue: the next upcoming date, not one that has already passed.
        self.assertEqual(nd(old, "FREQ=DAILY", now="2026-02-05 00:00:00")[0], "2026-02-05 01:00:00")

    def test_ended_rule_completes_normally(self):
        todo = self.create(due_at=iso(-60), repeat="FREQ=DAILY;COUNT=1")
        self.assertEqual(self.update(todo["id"], status="done")["status"], "done")

    def test_repeat_needs_a_date_and_valid_rule(self):
        with self.assertRaises(todos.ValidationError):
            self.create(repeat="FREQ=DAILY")
        with self.assertRaises(todos.ValidationError):
            self.create(due_at=iso(60), repeat="FREQ=HOURLY")
        todo = self.create(due_at=iso(60), repeat="freq=weekly")
        self.assertEqual(todo["repeat"], "FREQ=WEEKLY")
        self.assertIsNone(self.update(todo["id"], due_at=None)["repeat"])


class SubtaskTagFlagTests(DbTestCase):
    def test_subtasks(self):
        parent = self.create(title="搬家")
        child = self.create(title="打包", parent_id=parent["id"])
        self.create(title="退租", parent_id=parent["id"], status="done")
        detail = todos.get_todo(self.conn, self.user, parent["id"])
        self.assertEqual([c["title"] for c in detail["subtasks"]], ["打包", "退租"])
        self.assertEqual((detail["subtask_total"], detail["subtask_done"]), (2, 1))
        self.assertEqual(todos.get_todo(self.conn, self.user, child["id"])["parent"]["title"], "搬家")
        listed = {t["id"]: t for t in todos.list_todos(self.conn, self.user)["items"]}
        self.assertEqual(listed[parent["id"]]["subtask_total"], 2)
        with self.assertRaises(todos.ValidationError):
            self.create(title="孙任务", parent_id=child["id"])
        with self.assertRaises(todos.ValidationError):
            self.update(parent["id"], parent_id=self.create()["id"])
        with self.assertRaises(todos.ValidationError):
            self.update(child["id"], parent_id=child["id"])

    def test_subtasks_follow_the_parent_list_and_are_deleted_with_it(self):
        from easylife import lists
        work = lists.create_list(self.conn, self.user, "工作")
        parent = self.create(title="项目")
        child = self.create(title="步骤", parent_id=parent["id"])
        self.update(parent["id"], list_id=work["id"])
        self.assertEqual(self.row(child["id"])["list_id"], work["id"])
        self.update(child["id"], list_id=lists.ensure_default(self.conn, self.user)["id"])
        self.assertEqual(self.row(child["id"])["list_id"], work["id"])   # stays with its parent
        self.assertTrue(todos.delete_todo(self.conn, self.user, parent["id"]))
        self.assertIsNone(self.row(child["id"]))

    def test_tags_and_flags(self):
        todo = self.create(tags=["#工作", " 工作 ", "家  里", "Work", "work"], flagged=True)
        self.assertEqual(todo["tags"], ["工作", "家 里", "Work"])
        self.assertIs(todo["flagged"], True)
        self.create(title="别的")
        self.assertEqual([t["id"] for t in todos.list_todos(self.conn, self.user, {"tag": "工作"})["items"]],
                         [todo["id"]])
        self.assertEqual(len(todos.list_todos(self.conn, self.user, {"flagged": "1"})["items"]), 1)
        self.assertEqual(self.update(todo["id"], tags=[])["tags"], [])
        with self.assertRaises(todos.ValidationError):
            self.create(tags=["标签%d" % i for i in range(todos.TAGS_MAX + 1)])
        with self.assertRaises(todos.ValidationError):
            self.create(tags=["a,b"])


class ImageApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-images-")
        cfg = app.load_config({"EASYLIFE_PORT": "0", "EASYLIFE_DB": os.path.join(self.tmp, "i.db"),
                               "EASYLIFE_SECRET": "test-secret-0123456789abcdef",
                               "EASYLIFE_BASE_PATH": "/todos"})
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.alice = Client(self.port)
        self.alice.request("POST", "/todos/api/register", {"email": "a@example.com",
                                                            "password": "correct-horse-battery"})
        self.todo = self.alice.request("POST", "/todos/api/todos", {"title": "带图"})[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, client, data, todo_id=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "image/jpeg", "Cookie": client.cookie or ""}
        if csrf and client.csrf:
            headers["X-CSRF-Token"] = client.csrf
        conn.request("POST", "/todos/api/todos/%d/images" % (todo_id or self.todo["id"]),
                     body=data, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_upload_view_and_delete(self):
        status, _ = self.upload(self.alice, PNG)
        self.assertEqual(status, 201)
        detail = self.alice.request("GET", "/todos/api/todos/%d" % self.todo["id"])[1]
        self.assertEqual(detail["image_count"], 1)
        image = detail["images"][0]
        self.assertEqual(image["mime"], "image/png")     # detected from the bytes, not the header
        resp, raw = self.alice.request("GET", "/todos/" + image["url"])
        self.assertEqual((resp.status, resp.getheader("Content-Type"), raw), (200, "image/png", PNG))
        self.assertIn("private", resp.getheader("Cache-Control"))
        listed = self.alice.request("GET", "/todos/api/todos")[1]["items"][0]
        self.assertEqual(listed["image_count"], 1)
        self.assertEqual(self.alice.request("DELETE", "/todos/" + image["url"])[0].status, 200)
        self.assertEqual(self.alice.request("GET", "/todos/" + image["url"])[0].status, 404)

    def test_rejects_other_users_bad_types_and_missing_csrf(self):
        self.assertEqual(self.upload(self.alice, b"<svg xmlns='http://www.w3.org/2000/svg'/>")[0], 400)
        self.assertEqual(self.upload(self.alice, JPEG, csrf=False)[0], 403)
        self.assertEqual(self.upload(self.alice, JPEG)[0], 201)
        image_url = "/todos/api/images/1"
        bob = Client(self.port)
        bob.request("POST", "/todos/api/register", {"email": "b@example.com",
                                                     "password": "correct-horse-battery"})
        self.assertEqual(bob.request("GET", image_url)[0].status, 404)
        self.assertEqual(bob.request("DELETE", image_url)[0].status, 404)
        self.assertEqual(self.upload(bob, JPEG)[0], 404)
        self.assertEqual(Client(self.port).request("GET", image_url)[0].status, 401)

    def test_limits(self):
        for _ in range(todos.IMAGES_MAX - 1):
            self.assertEqual(self.upload(self.alice, JPEG)[0], 201)
        self.assertEqual(self.upload(self.alice, JPEG)[0], 201)
        self.assertEqual(self.upload(self.alice, JPEG)[0], 400)
        other = self.alice.request("POST", "/todos/api/todos", {"title": "大图"})[1]
        # Refused from the declared size alone, before the body is read.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/todos/api/todos/%d/images" % other["id"])
        for name, value in (("Content-Type", "image/jpeg"), ("Cookie", self.alice.cookie),
                            ("X-CSRF-Token", self.alice.csrf),
                            ("Content-Length", str(todos.IMAGE_MAX_BYTES + 1))):
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 413)

    def test_deleting_the_task_deletes_its_images(self):
        self.upload(self.alice, JPEG)
        self.alice.request("DELETE", "/todos/api/todos/%d" % self.todo["id"])
        conn = sqlite3.connect(os.path.join(self.tmp, "i.db"))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM todo_images").fetchone()[0], 0)
        finally:
            conn.close()


IOS_ABSOLUTE_ALARM = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Apple Inc.//iOS 26.6//EN\r
BEGIN:VTODO\r
UID:FC014D72-DC72-4206-8BBF-EC068E56499B\r
DTSTAMP:20261018T154753Z\r
DTSTART;TZID=Asia/Shanghai:20261020T090000\r
DUE;TZID=Asia/Shanghai:20261020T090000\r
SUMMARY:交房租\r
STATUS:NEEDS-ACTION\r
RRULE:FREQ=MONTHLY\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
DESCRIPTION:Reminder\r
TRIGGER;VALUE=DATE-TIME:20261020T010000Z\r
UID:BA1DC2B1-FC38-4D52-92E6-3B65B1D83A83\r
END:VALARM\r
END:VTODO\r
END:VCALENDAR\r
"""


class CalDavFeatureTests(CalDavTestCase):
    def todos(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM todos ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_phone_alarm_at_due_time_and_repeat_rule(self):
        href = self.cal + "FC014D72-DC72-4206-8BBF-EC068E56499B.ics"
        resp, _ = self.dav("PUT", href, IOS_ABSOLUTE_ALARM, {"Content-Type": "text/calendar"})
        self.assertEqual(resp.status, 201)
        row = self.todos()[0]
        self.assertEqual(row["due_at"], "2026-10-20 01:00:00")
        self.assertIsNone(row["remind_at"])           # the alarm at the due time is implied
        self.assertEqual(row["repeat"], "FREQ=MONTHLY")
        self.assertEqual(self.dav("GET", href)[1].decode(), IOS_ABSOLUTE_ALARM)
        self.web.request("PUT", "/todos/api/todos/%d" % row["id"], {"repeat": "FREQ=WEEKLY"})
        ics = self.dav("GET", href)[1].decode()
        self.assertIn("RRULE:FREQ=WEEKLY", ics)
        self.assertNotIn("FREQ=MONTHLY", ics)

    def test_web_tasks_carry_due_alarm_repeat_and_parent(self):
        _, parent = self.web.request("POST", "/todos/api/todos", {
            "title": "周会", "due_at": "2026-10-20T02:00:00Z", "remind_at": "2026-10-20T01:45:00Z",
            "repeat": "FREQ=WEEKLY"})
        _, child = self.web.request("POST", "/todos/api/todos", {"title": "准备材料",
                                                                 "parent_id": parent["id"]})
        parent_ics = self.dav("GET", self.cal + "easylife-%d.ics" % parent["id"])[1].decode()
        self.assertIn("RRULE:FREQ=WEEKLY", parent_ics)
        self.assertIn("TRIGGER;RELATED=START:PT0S", parent_ics)
        self.assertIn("TRIGGER;RELATED=START:-PT15M", parent_ics)
        child_ics = self.dav("GET", self.cal + "easylife-%d.ics" % child["id"])[1].decode()
        self.assertIn("RELATED-TO;RELTYPE=PARENT:easylife-task-%d@easylife" % parent["id"], child_ics)

    def test_phone_subtask_link_and_edits_without_it_keep_the_parent(self):
        _, parent = self.web.request("POST", "/todos/api/todos", {"title": "旅行"})
        body = IOS_ABSOLUTE_ALARM.replace("RRULE:FREQ=MONTHLY\r\n",
                                          "RELATED-TO;RELTYPE=PARENT:easylife-task-%d@easylife\r\n"
                                          % parent["id"])
        href = self.cal + "child.ics"
        self.assertEqual(self.dav("PUT", href, body, {"Content-Type": "text/calendar"})[0].status, 201)
        child = self.todos()[-1]
        self.assertEqual(child["parent_id"], parent["id"])
        edited = IOS_ABSOLUTE_ALARM.replace("RRULE:FREQ=MONTHLY\r\n", "").replace("交房租", "订机票")
        self.assertEqual(self.dav("PUT", href, edited, {"Content-Type": "text/calendar"})[0].status, 204)
        child = self.todos()[-1]
        self.assertEqual((child["title"], child["parent_id"]), ("订机票", parent["id"]))


if __name__ == "__main__":
    unittest.main()
