"""CalDAV task sync: discovery, two-way VTODO mapping, preconditions and credentials."""

import base64
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import http.client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app, caldav  # noqa: E402
from tests.test_api import Client  # noqa: E402

D = "{DAV:}"
C = "{urn:ietf:params:xml:ns:caldav}"
CS = "{http://calendarserver.org/ns/}"

IOS_TODO = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Apple Inc.//iOS 18.0//EN\r
CALSCALE:GREGORIAN\r
BEGIN:VTIMEZONE\r
TZID:Asia/Shanghai\r
BEGIN:STANDARD\r
DTSTART:19910915T010000\r
TZOFFSETFROM:+0900\r
TZOFFSETTO:+0800\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VTODO\r
UID:6C3F2B10-1111-4A5B-9C2D-ABCDEF012345\r
DTSTAMP:20261018T020000Z\r
CREATED:20261018T020000Z\r
SUMMARY:买牛奶\\, 鸡蛋\r
DESCRIPTION:两盒\\n全脂\r
STATUS:NEEDS-ACTION\r
PRIORITY:1\r
DTSTART;TZID=Asia/Shanghai:20261020T180000\r
DUE;TZID=Asia/Shanghai:20261020T180000\r
RRULE:FREQ=WEEKLY\r
X-APPLE-SORT-ORDER:12345\r
BEGIN:VALARM\r
UID:ALARM-1\r
X-WR-ALARMUID:ALARM-1\r
ACTION:DISPLAY\r
DESCRIPTION:Reminder\r
TRIGGER:-PT15M\r
END:VALARM\r
END:VTODO\r
END:VCALENDAR\r
"""


MKCALENDAR = ('<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
              'xmlns:a="http://apple.com/ns/ical/"><d:set><d:prop><d:displayname>%s</d:displayname>'
              '<a:calendar-color>#FF2968FF</a:calendar-color><c:supported-calendar-component-set>'
              '<c:comp name="%s"/></c:supported-calendar-component-set></d:prop></d:set></c:mkcalendar>')


def propfind_body(*props):
    inner = "".join('<x:%s xmlns:x="%s"/>' % (name, ns) for ns, name in props)
    return '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>%s</d:prop></d:propfind>' % inner


class CalDavTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-dav-")
        self.db_path = os.path.join(self.tmp, "dav.db")
        cfg = app.load_config({
            "EASYLIFE_PORT": "0",
            "EASYLIFE_DB": self.db_path,
            "EASYLIFE_SECRET": "test-secret-0123456789abcdef",
            "EASYLIFE_BASE_PATH": "/todos",
            "PUBLIC_URL": "https://mch.example/todos",
        })
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.web = Client(self.port)
        resp, body = self.web.request("POST", "/todos/api/register",
                                      {"email": "alice@example.com", "password": "correct-horse-battery"})
        self.assertEqual(resp.status, 201)
        self.user_id = body["user"]["id"]
        resp, body = self.web.request("POST", "/todos/api/dav/password", {})
        self.assertEqual(resp.status, 200)
        self.password = body["password"]
        self.cal = "/todos/dav/calendars/%d/tasks/" % self.user_id

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dav(self, method, path, body=None, headers=None, password=None, user="alice@example.com"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if password is not False:
            token = "%s:%s" % (user, password or self.password)
            hdrs["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
        data = body.encode("utf-8") if isinstance(body, str) else body
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp, raw

    def multistatus(self, method, path, body, depth="1"):
        resp, raw = self.dav(method, path, body, {"Depth": depth, "Content-Type": "application/xml"})
        self.assertEqual(resp.status, 207, raw)
        return ET.fromstring(raw)

    def responses(self, tree):
        out = {}
        for response in tree.findall(D + "response"):
            props = {}
            for propstat in response.findall(D + "propstat"):
                if "200" in propstat.find(D + "status").text:
                    for prop in propstat.find(D + "prop"):
                        props[prop.tag] = prop
            out[response.find(D + "href").text] = props
        return out

    def ctag(self):
        tree = self.multistatus("PROPFIND", self.cal, propfind_body(("http://calendarserver.org/ns/", "getctag")), "0")
        return self.responses(tree)[self.cal][CS + "getctag"].text

    def row(self, todo_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        finally:
            conn.close()

    def only_todo(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM todos").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        return rows[0]


class DiscoveryTests(CalDavTestCase):
    def test_me_reports_sync_settings_without_the_password(self):
        resp, me = self.web.request("GET", "/todos/api/me")
        self.assertEqual(me["dav"]["server_url"], "https://mch.example/todos/dav/")
        self.assertEqual(me["dav"]["username"], "alice@example.com")
        self.assertTrue(me["dav"]["enabled"])
        self.assertNotIn("password", me["dav"])
        self.assertRegex(self.password, r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")

    def test_principal_home_and_task_list_discovery(self):
        tree = self.multistatus("PROPFIND", "/todos/dav/", propfind_body(("DAV:", "current-user-principal")), "0")
        principal = self.responses(tree)["/todos/dav/"][D + "current-user-principal"].find(D + "href").text
        self.assertEqual(principal, "/todos/dav/principals/%d/" % self.user_id)

        tree = self.multistatus("PROPFIND", principal, propfind_body(
            ("urn:ietf:params:xml:ns:caldav", "calendar-home-set"),
            ("urn:ietf:params:xml:ns:caldav", "calendar-user-address-set")), "0")
        props = self.responses(tree)[principal]
        home = props[C + "calendar-home-set"].find(D + "href").text
        self.assertEqual(props[C + "calendar-user-address-set"].find(D + "href").text,
                         "mailto:alice@example.com")

        tree = self.multistatus("PROPFIND", home, propfind_body(
            ("DAV:", "resourcetype"), ("DAV:", "displayname"), ("DAV:", "current-user-privilege-set"),
            ("urn:ietf:params:xml:ns:caldav", "supported-calendar-component-set"),
            ("http://apple.com/ns/ical/", "calendar-color")))
        listing = self.responses(tree)
        task_list = listing[self.cal]
        self.assertIsNotNone(task_list[D + "resourcetype"].find(C + "calendar"))
        self.assertEqual([c.get("name") for c in task_list[C + "supported-calendar-component-set"]], ["VTODO"])
        privileges = [p[0].tag for p in task_list[D + "current-user-privilege-set"]]
        self.assertIn(D + "write-content", privileges)
        self.assertEqual(task_list[D + "displayname"].text, "EasyLife")

    def test_root_answers_principal_properties_like_ios_expects(self):
        tree = self.multistatus("PROPFIND", "/todos/dav/", propfind_body(
            ("urn:ietf:params:xml:ns:caldav", "calendar-home-set"), ("DAV:", "principal-URL")), "0")
        props = self.responses(tree)["/todos/dav/"]
        self.assertEqual(props[C + "calendar-home-set"].find(D + "href").text,
                         "/todos/dav/calendars/%d/" % self.user_id)
        self.assertEqual(props[D + "principal-URL"].find(D + "href").text,
                         "/todos/dav/principals/%d/" % self.user_id)

    def test_unknown_properties_are_reported_missing(self):
        resp, raw = self.dav("PROPFIND", self.cal, propfind_body(("urn:x-unknown", "nothing")), {"Depth": "0"})
        self.assertEqual(resp.status, 207)
        self.assertIn(b"404 Not Found", raw)
        self.assertIn(b'xmlns:x="urn:x-unknown"', raw)

    def test_options_and_well_known(self):
        resp, _ = self.dav("OPTIONS", "/todos/dav/", password=False)
        self.assertEqual(resp.status, 200)
        self.assertIn("calendar-access", resp.getheader("DAV"))
        resp, _ = self.dav("PROPFIND", "/todos/.well-known/caldav", password=False)
        self.assertEqual(resp.status, 301)
        self.assertEqual(resp.getheader("Location"), "/todos/dav/")

    def test_prefix_stripping_proxy_paths_work_too(self):
        tree = self.multistatus("PROPFIND", "/dav/", propfind_body(("DAV:", "current-user-principal")), "0")
        self.assertIn("/todos/dav/", self.responses(tree))


class CredentialTests(CalDavTestCase):
    def test_missing_or_wrong_credentials_are_401(self):
        resp, _ = self.dav("PROPFIND", "/todos/dav/", password=False)
        self.assertEqual(resp.status, 401)
        self.assertIn("Basic", resp.getheader("WWW-Authenticate"))
        resp, _ = self.dav("PROPFIND", "/todos/dav/", password="abcd-efgh-jkmn-pqrs")
        self.assertEqual(resp.status, 401)
        # The account password is not a sync password.
        resp, _ = self.dav("PROPFIND", "/todos/dav/", password="correct-horse-battery")
        self.assertEqual(resp.status, 401)

    def test_regenerate_and_revoke_invalidate_the_old_password(self):
        old = self.password
        resp, body = self.web.request("POST", "/todos/api/dav/password", {})
        self.assertNotEqual(body["password"], old)
        resp, _ = self.dav("PROPFIND", "/todos/dav/", password=old, headers={"Depth": "0"})
        self.assertEqual(resp.status, 401)
        resp, body = self.web.request("DELETE", "/todos/api/dav/password")
        self.assertEqual(resp.status, 200)
        self.assertFalse(body["enabled"])
        resp, _ = self.dav("PROPFIND", "/todos/dav/", password=body.get("password") or old)
        self.assertEqual(resp.status, 401)

    def test_dav_failures_never_lock_web_login(self):
        for _ in range(12):
            self.dav("PROPFIND", "/todos/dav/", password="wrong-wrong-wrong")
        resp, _ = self.dav("PROPFIND", "/todos/dav/", headers={"Depth": "0"})
        self.assertEqual(resp.status, 429)
        resp, _ = Client(self.port).request("POST", "/todos/api/login", {
            "email": "alice@example.com", "password": "correct-horse-battery"})
        self.assertEqual(resp.status, 200)

    def test_other_users_tree_is_not_visible(self):
        other = Client(self.port)
        resp, body = other.request("POST", "/todos/api/register",
                                   {"email": "bob@example.com", "password": "correct-horse-battery"})
        bob = body["user"]["id"]
        other.request("POST", "/todos/api/todos", {"title": "bob 的秘密"})
        resp, _ = self.dav("PROPFIND", "/todos/dav/calendars/%d/tasks/" % bob, headers={"Depth": "1"})
        self.assertEqual(resp.status, 404)
        resp, raw = self.dav("GET", "/todos/dav/calendars/%d/tasks/easylife-1.ics" % bob)
        self.assertEqual(resp.status, 404)
        resp, raw = self.dav("GET", self.cal + "easylife-1.ics")
        self.assertEqual(resp.status, 404)


class SyncTests(CalDavTestCase):
    def test_web_todos_are_listed_as_vtodos(self):
        resp, todo = self.web.request("POST", "/todos/api/todos", {
            "title": "写周报, 发邮件", "details": "第一行\n- [ ] 第二行", "priority": 3,
            "due_at": "2026-10-20T10:00:00Z", "remind_at": "2026-10-20T09:30:00Z"})
        self.web.request("POST", "/todos/api/todos", {"title": "已归档", "status": "archived"})
        tree = self.multistatus("PROPFIND", self.cal, propfind_body(("DAV:", "getetag")))
        hrefs = [h for h in self.responses(tree) if h != self.cal]
        self.assertEqual(hrefs, [self.cal + "easylife-%d.ics" % todo["id"]])
        resp, raw = self.dav("GET", hrefs[0])
        self.assertEqual(resp.status, 200)
        ics = raw.decode()
        self.assertTrue(resp.getheader("ETag"))
        for line in ("BEGIN:VTODO", "SUMMARY:写周报\\, 发邮件", "DESCRIPTION:第一行\\n☐ 第二行",
                     "PRIORITY:1", "DUE;TZID=Asia/Shanghai:20261020T180000", "STATUS:NEEDS-ACTION",
                     "BEGIN:VTIMEZONE", "TZOFFSETTO:+0800",
                     "TRIGGER;RELATED=START:-PT30M",
                     "UID:easylife-task-%d@easylife" % todo["id"]):
            self.assertIn(line, ics)
        # Stable between reads, changed by a web edit.
        first = self.ctag()
        self.assertEqual(first, self.ctag())
        self.web.request("PUT", "/todos/api/todos/%d" % todo["id"], {"status": "done"})
        self.assertNotEqual(first, self.ctag())
        resp, raw = self.dav("GET", hrefs[0])
        self.assertIn("STATUS:COMPLETED", raw.decode())
        self.assertIn("PERCENT-COMPLETE:100", raw.decode())

    def test_client_created_reminder_round_trips(self):
        href = self.cal + "6C3F2B10-1111-4A5B-9C2D-ABCDEF012345.ics"
        resp, _ = self.dav("PUT", href, IOS_TODO, {"Content-Type": "text/calendar", "If-None-Match": "*"})
        self.assertEqual(resp.status, 201)
        row = self.only_todo()
        self.assertEqual(row["title"], "买牛奶, 鸡蛋")
        self.assertEqual(row["details"], "两盒\n全脂")
        self.assertEqual(row["priority"], 3)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["due_at"], "2026-10-20 10:00:00")    # 18:00 Asia/Shanghai
        self.assertEqual(row["remind_at"], "2026-10-20 09:45:00")  # -PT15M from DTSTART
        resp, raw = self.dav("GET", href)
        ics = raw.decode()
        for line in ("UID:6C3F2B10-1111-4A5B-9C2D-ABCDEF012345", "RRULE:FREQ=WEEKLY",
                     "X-APPLE-SORT-ORDER:12345", "TRIGGER:-PT15M", "X-WR-ALARMUID:ALARM-1",
                     "DUE;TZID=Asia/Shanghai:20261020T180000", "TZID:Asia/Shanghai"):
            self.assertIn(line, ics)
        # Unchanged objects are echoed back verbatim, so the client has nothing to "repair".
        self.assertEqual(ics, IOS_TODO)
        # The web app sees it and can edit it; a new reminder time replaces the client's alarm.
        resp, todo = self.web.request("PUT", "/todos/api/todos/%d" % row["id"],
                                      {"remind_at": "2026-10-20T08:00:00Z"})
        self.assertEqual(resp.status, 200)
        resp, raw = self.dav("GET", href)
        ics = raw.decode()
        self.assertIn("TRIGGER;RELATED=START:-PT2H", ics)
        self.assertNotIn("ALARM-1", ics)
        self.assertIn("DUE;TZID=Asia/Shanghai:20261020T180000", ics)  # untouched field kept
        self.assertIn("SUMMARY:买牛奶\\, 鸡蛋", ics)
        # iPhone model: one alarm at the due time plus the earlier one.
        self.assertEqual(ics.count("BEGIN:VALARM"), 2)
        self.assertIn("TRIGGER;RELATED=START:PT0S", ics)
        # Changing the due date replaces DTSTART and DUE together.
        self.web.request("PUT", "/todos/api/todos/%d" % row["id"], {"due_at": "2026-10-21T02:00:00Z"})
        resp, raw = self.dav("GET", href)
        ics = raw.decode()
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261021T100000", ics)
        self.assertIn("DUE;TZID=Asia/Shanghai:20261021T100000", ics)
        self.assertEqual(ics.count("BEGIN:VTIMEZONE"), 1)  # the client's own block is reused
        self.assertNotIn("TZID=Asia/Shanghai:20261020", ics)
        self.assertIn("RRULE:FREQ=WEEKLY", ics)
        self.assertLess(ics.index("DUE;"), ics.index("BEGIN:VALARM"))

    def test_client_updates_keep_easylife_only_values(self):
        resp, todo = self.web.request("POST", "/todos/api/todos", {"title": "整理房间", "status": "doing", "priority": 0})
        href = self.cal + "easylife-%d.ics" % todo["id"]
        resp, raw = self.dav("GET", href)
        etag = resp.getheader("ETag")
        ics = raw.decode()
        self.assertIn("STATUS:IN-PROCESS", ics)
        self.assertNotIn("PRIORITY", ics)  # 最低 <-> no priority
        # Reminders cannot express "doing" and writes NEEDS-ACTION back.
        edited = ics.replace("STATUS:IN-PROCESS", "STATUS:NEEDS-ACTION").replace("整理房间", "整理书房")
        resp, _ = self.dav("PUT", href, edited, {"If-Match": etag})
        self.assertEqual(resp.status, 204)
        row = self.row(todo["id"])
        self.assertEqual((row["title"], row["status"], row["priority"]), ("整理书房", "doing", 0))
        # The old ETag is now stale.
        resp, _ = self.dav("PUT", href, edited, {"If-Match": etag})
        self.assertEqual(resp.status, 412)
        resp, _ = self.dav("PUT", href, edited, {"If-None-Match": "*"})
        self.assertEqual(resp.status, 412)
        # Completing it in Reminders completes it here.
        resp, raw = self.dav("GET", href)
        done = raw.decode().replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED")  # echoed as written
        resp, _ = self.dav("PUT", href, done, {"If-Match": resp.getheader("ETag")})
        self.assertEqual(resp.status, 204)
        self.assertEqual(self.row(todo["id"])["status"], "done")

    def test_checklist_and_priority_round_trip_by_name(self):
        resp, todo = self.web.request("POST", "/todos/api/todos", {
            "title": "出差", "details": "- [ ] 订酒店\n- [x] 买票"})
        href = self.cal + "easylife-%d.ics" % todo["id"]
        resp, raw = self.dav("GET", href)
        ics = raw.decode()
        self.assertIn("DESCRIPTION:☐ 订酒店\\n☑ 买票", ics)
        self.assertIn("PRIORITY:5", ics)
        edited = ics.replace("☐ 订酒店", "☑ 订酒店").replace("PRIORITY:5", "PRIORITY:9")
        resp, _ = self.dav("PUT", href, edited)
        self.assertEqual(resp.status, 204)
        row = self.row(todo["id"])
        self.assertEqual(row["details"], "- [x] 订酒店\n- [x] 买票")
        self.assertEqual(row["priority"], 1)
        for ical, ours in ((1, 3), (5, 2), (9, 1), (0, 0)):
            resp, raw = self.dav("GET", href)
            body = re.sub(r"PRIORITY:\d\r\n", "", raw.decode())
            if ical:
                body = body.replace("STATUS:", "PRIORITY:%d\r\nSTATUS:" % ical, 1)
            self.dav("PUT", href, body)
            self.assertEqual(self.row(todo["id"])["priority"], ours)
        # Echoed verbatim afterwards: a second read gives the same ETag and body.
        first = self.dav("GET", href)
        second = self.dav("GET", href)
        self.assertEqual(first[0].getheader("ETag"), second[0].getheader("ETag"))
        self.assertEqual(first[1], body.encode())

    def test_client_delete_removes_the_todo(self):
        resp, todo = self.web.request("POST", "/todos/api/todos", {"title": "删掉我"})
        href = self.cal + "easylife-%d.ics" % todo["id"]
        resp, _ = self.dav("DELETE", href, headers={"If-Match": '"stale"'})
        self.assertEqual(resp.status, 412)
        resp, _ = self.dav("DELETE", href)
        self.assertEqual(resp.status, 204)
        self.assertIsNone(self.row(todo["id"]))
        resp, _ = self.dav("DELETE", self.cal)
        self.assertEqual(resp.status, 403)

    def test_reports(self):
        resp, todo = self.web.request("POST", "/todos/api/todos", {"title": "报告"})
        href = self.cal + "easylife-%d.ics" % todo["id"]
        multiget = ('<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
                    '<d:href>%s</d:href><d:href>%smissing.ics</d:href></c:calendar-multiget>' % (href, self.cal))
        tree = self.multistatus("REPORT", self.cal, multiget)
        found = self.responses(tree)
        self.assertIn("SUMMARY:报告", found[href][C + "calendar-data"].text)
        self.assertEqual(found[self.cal + "missing.ics"], {})
        query = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                 '<d:prop><d:getetag/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
                 '<c:comp-filter name="%s"/></c:comp-filter></c:filter></c:calendar-query>')
        self.assertIn(href, self.responses(self.multistatus("REPORT", self.cal, query % "VTODO")))
        self.assertEqual(self.responses(self.multistatus("REPORT", self.cal, query % "VEVENT")), {})

    def test_bad_writes_are_rejected(self):
        event = IOS_TODO.replace("VTODO", "VEVENT")
        resp, _ = self.dav("PUT", self.cal + "event.ics", event)
        self.assertEqual(resp.status, 403)
        resp, _ = self.dav("PUT", self.cal + "broken.ics", "not a calendar")
        self.assertEqual(resp.status, 400)
        resp, _ = self.dav("PUT", self.cal + "a.ics", IOS_TODO)
        self.assertEqual(resp.status, 201)
        resp, _ = self.dav("PUT", self.cal + "b.ics", IOS_TODO)  # same UID, other name
        self.assertEqual(resp.status, 409)
        resp, _ = self.dav("MKCALENDAR", self.cal)
        self.assertEqual(resp.status, 405)
        events = MKCALENDAR % ("日历", "VEVENT")
        resp, _ = self.dav("MKCALENDAR", "/todos/dav/calendars/%d/cal/" % self.user_id, events)
        self.assertEqual(resp.status, 403)
        resp, _ = self.dav("PROPFIND", self.cal, '<!DOCTYPE x [<!ENTITY a "b">]><x/>')
        self.assertEqual(resp.status, 400)

    def test_stale_alarm_from_client_is_not_mailed(self):
        old = IOS_TODO.replace("20261020T180000", "20200101T090000").replace("RRULE:FREQ=WEEKLY\r\n", "")
        resp, _ = self.dav("PUT", self.cal + "old.ics", old)
        self.assertEqual(resp.status, 201)
        self.assertEqual(self.only_todo()["reminder_sent"], 1)


class ListTests(CalDavTestCase):
    def setUp(self):
        super().setUp()
        self.work = "/todos/dav/calendars/%d/9D1F-WORK/" % self.user_id

    def web_lists(self):
        resp, body = self.web.request("GET", "/todos/api/lists")
        self.assertEqual(resp.status, 200)
        return body["items"]

    def make_work_list(self):
        resp, _ = self.dav("MKCALENDAR", self.work, MKCALENDAR % ("工作", "VTODO"))
        self.assertEqual(resp.status, 201)
        return [item for item in self.web_lists() if item["slug"] == "9D1F-WORK"][0]

    def test_client_creates_renames_and_lists_a_list(self):
        work = self.make_work_list()
        self.assertEqual((work["name"], work["color"], work["is_default"]), ("工作", "#FF2968", False))
        tree = self.multistatus("PROPFIND", "/todos/dav/calendars/%d/" % self.user_id, propfind_body(
            ("DAV:", "displayname"), ("http://apple.com/ns/ical/", "calendar-color")))
        listing = self.responses(tree)
        self.assertEqual(listing[self.cal][D + "displayname"].text, "EasyLife")
        self.assertEqual(listing[self.work][D + "displayname"].text, "工作")
        rename = ('<d:propertyupdate xmlns:d="DAV:" xmlns:a="http://apple.com/ns/ical/"><d:set><d:prop>'
                  '<d:displayname>项目</d:displayname><a:calendar-color>#34C759FF</a:calendar-color>'
                  '</d:prop></d:set></d:propertyupdate>')
        before = self.ctag()
        self.multistatus("PROPPATCH", self.cal, rename)
        self.assertNotEqual(self.ctag(), before)
        default = self.web_lists()[0]
        self.assertEqual((default["name"], default["color"], default["is_default"]),
                         ("项目", "#34C759", True))

    def test_todos_follow_their_list_both_ways(self):
        work = self.make_work_list()
        resp, _ = self.dav("PUT", self.work + "a.ics", IOS_TODO)
        self.assertEqual(resp.status, 201)
        todo = self.only_todo()
        self.assertEqual(todo["list_id"], work["id"])
        resp, body = self.web.request("GET", "/todos/api/todos?list=%d" % work["id"])
        self.assertEqual([t["id"] for t in body["items"]], [todo["id"]])
        resp, body = self.web.request("GET", "/todos/api/todos?list=%d" % self.web_lists()[0]["id"])
        self.assertEqual(body["items"], [])
        self.assertEqual(self.responses(self.multistatus("PROPFIND", self.cal, propfind_body(
            ("DAV:", "getetag")))).keys(), {self.cal})

        # Moved on the web: the todo leaves the work collection and shows up in the default one.
        resp, moved = self.web.request("PUT", "/todos/api/todos/%d" % todo["id"],
                                       {"list_id": self.web_lists()[0]["id"]})
        self.assertEqual((resp.status, moved["list_id"]), (200, None))
        resp, _ = self.dav("GET", self.work + "a.ics")
        self.assertEqual(resp.status, 404)
        resp, _ = self.dav("GET", self.cal + "a.ics")
        self.assertEqual(resp.status, 200)

    def test_client_moves_a_todo_by_writing_it_into_another_list(self):
        self.make_work_list()
        self.dav("PUT", self.cal + "a.ics", IOS_TODO)
        todo_id = self.only_todo()["id"]
        self.web.request("POST", "/todos/api/todos/%d/notes" % todo_id, {"content": "保留的笔记"})
        resp, _ = self.dav("PUT", self.work + "b.ics", IOS_TODO, {"If-None-Match": "*"})
        self.assertEqual(resp.status, 201)
        resp, _ = self.dav("DELETE", self.cal + "a.ics")
        self.assertEqual(resp.status, 404)
        todo = self.only_todo()
        self.assertEqual(todo["id"], todo_id)  # same todo, notes kept
        self.assertIsNotNone(todo["list_id"])
        resp, _ = self.dav("GET", self.work + "b.ics")
        self.assertEqual(resp.status, 200)

    def test_deleting_lists(self):
        resp, _ = self.dav("DELETE", self.cal)
        self.assertEqual(resp.status, 403)
        self.make_work_list()
        self.dav("PUT", self.work + "a.ics", IOS_TODO)
        resp, _ = self.dav("DELETE", self.work)
        self.assertEqual(resp.status, 204)
        self.assertEqual(len(self.web_lists()), 1)
        resp, body = self.web.request("GET", "/todos/api/todos")
        self.assertEqual(body["items"], [])

    def test_web_list_api(self):
        resp, home = self.web.request("POST", "/todos/api/lists", {"name": "  家  务 ", "color": "#ff9500"})
        self.assertEqual((resp.status, home["name"], home["color"]), (201, "家 务", "#FF9500"))
        self.assertEqual(home["slug"], "list-%d" % home["id"])
        resp, todo = self.web.request("POST", "/todos/api/todos", {"title": "拖地", "list_id": home["id"]})
        self.assertEqual(todo["list_id"], home["id"])
        counts = {item["name"]: item["open_count"] for item in self.web_lists()}
        self.assertEqual(counts, {"EasyLife": 0, "家 务": 1})
        resp, _ = self.web.request("PUT", "/todos/api/lists/%d" % home["id"], {"name": ""})
        self.assertEqual(resp.status, 400)
        resp, renamed = self.web.request("PUT", "/todos/api/lists/%d" % home["id"], {"name": "家务"})
        self.assertEqual(renamed["name"], "家务")
        tree = self.multistatus("PROPFIND", "/todos/dav/calendars/%d/list-%d/" % (self.user_id, home["id"]),
                                propfind_body(("DAV:", "displayname")), "1")
        self.assertEqual(len(self.responses(tree)), 2)  # the list and its one todo

        default_id = self.web_lists()[0]["id"]
        resp, _ = self.web.request("DELETE", "/todos/api/lists/%d" % default_id)
        self.assertEqual(resp.status, 400)
        resp, body = self.web.request("DELETE", "/todos/api/lists/%d" % home["id"])
        self.assertEqual((resp.status, body["moved"]), (200, 1))
        self.assertIsNone(self.row(todo["id"])["list_id"])  # moved to the default list

    def test_lists_of_other_users_are_off_limits(self):
        resp, home = self.web.request("POST", "/todos/api/lists", {"name": "私人"})
        other = Client(self.port)
        other.request("POST", "/todos/api/register", {"email": "bob@example.com", "password": "correct-horse-battery"})
        resp, _ = other.request("POST", "/todos/api/todos", {"title": "x", "list_id": home["id"]})
        self.assertEqual(resp.status, 400)
        for method, body in (("PUT", {"name": "偷改"}), ("DELETE", None)):
            resp, _ = other.request(method, "/todos/api/lists/%d" % home["id"], body)
            self.assertEqual(resp.status, 404)
        resp, _ = other.request("GET", "/todos/api/todos?list=%d" % home["id"])
        self.assertEqual(resp.status, 400)


class ParsingTests(unittest.TestCase):
    tz = caldav.default_zone({"timezone": "Asia/Shanghai"})

    def test_date_only_due_is_nine_local(self):
        self.assertEqual(caldav.parse_datetime("20261020", {"VALUE": "DATE"}, self.tz), "2026-10-20 01:00:00")

    def test_floating_and_utc_times(self):
        self.assertEqual(caldav.parse_datetime("20261020T120000", {}, self.tz), "2026-10-20 04:00:00")
        self.assertEqual(caldav.parse_datetime("20261020T120000Z", {}, self.tz), "2026-10-20 12:00:00")
        self.assertEqual(caldav.parse_datetime("20261020T120000", {"TZID": "America/New_York"}, self.tz),
                         "2026-10-20 16:00:00")

    def test_relative_alarm_round_trip(self):
        obj = {"id": 1, "uid": "u", "title": "t", "details": None, "status": "open", "priority": 2,
               "due_at": "2026-10-23 01:00:00", "remind_at": "2026-10-19 01:00:00",
               "created_at": "2026-10-18 00:00:00", "updated_at": "2026-10-18 00:00:00"}
        ics = caldav.to_ics(obj)
        self.assertIn("TRIGGER;RELATED=START:-P4D", ics)
        self.assertNotIn("Z\r\nEND:VALARM", ics)
        _, fields, _, _ = caldav.parse_vtodo(ics, self.tz)
        self.assertEqual((fields["due_at"], fields["remind_at"]), (obj["due_at"], obj["remind_at"]))
        self.assertEqual(caldav.format_duration(-(4 * 86400 + 7 * 3600 + 30 * 60)), "-P4DT7H30M")
        self.assertEqual(caldav.format_duration(0), "PT0S")
        obj["due_at"] = None
        self.assertIn("TRIGGER;VALUE=DATE-TIME:20261019T010000Z", caldav.to_ics(obj))

    def test_durations(self):
        self.assertEqual(caldav.parse_duration("-P1DT2H").total_seconds(), -26 * 3600)
        self.assertEqual(caldav.parse_duration("PT0S").total_seconds(), 0)
        with self.assertRaises(ValueError):
            caldav.parse_duration("P")

    def test_quoted_parameters(self):
        self.assertEqual(caldav.parse_line('X-A;P="a;b:c";Q=1:v:w'), ("X-A", {"P": "a;b:c", "Q": "1"}, "v:w"))

    def test_folded_output_stays_parseable(self):
        obj = {"id": 1, "uid": "u", "title": "很长的标题" * 40, "details": None, "status": "open",
               "priority": 2, "due_at": None, "remind_at": None, "created_at": "2026-10-18 00:00:00",
               "updated_at": "2026-10-18 00:00:00"}
        ics = caldav.to_ics(obj)
        self.assertTrue(all(len(line.encode()) <= 75 for line in ics.split("\r\n")))
        uid, fields, _, _ = caldav.parse_vtodo(ics, self.tz)
        self.assertEqual(fields["title"], ("很长的标题" * 40)[:200])
        self.assertIn("PRIORITY:5", ics)  # 中 <-> Reminders medium


if __name__ == "__main__":
    unittest.main()
