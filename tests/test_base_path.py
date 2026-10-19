"""EASYLIFE_BASE_PATH tests: deploying under https://mch.wang/todos/ (DESIGN.md §8.5).

nginx may forward either with the prefix stripped (proxy_pass .../;) or with the
prefix kept (proxy_pass ...;) — both must work against the same server.
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app, db, reminders  # noqa: E402

PASSWORD = "correct-horse-battery"
SECRET = "test-secret-0123456789abcdef"
PUBLIC = "https://mch.wang/todos"


def unfold(ics):
    """Undo RFC 5545 line folding so long URLs can be searched for."""
    return ics.replace("\r\n ", "").replace("\r\n\t", "")


class Client:
    """Keeps the session cookie / CSRF token and prepends a path prefix to every request.

    prefix="/todos" simulates nginx keeping the prefix, prefix="" simulates
    nginx stripping it before proxying.
    """

    def __init__(self, port, prefix):
        self.port = port
        self.prefix = prefix
        self.cookie = None
        self.csrf = None
        self.set_cookies = []

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp, raw

    def request(self, method, path, body=None):
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf and method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = self.csrf
        resp, raw = self.raw(method, self.prefix + path, body, headers)
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith(app.COOKIE_NAME + "="):
            self.set_cookies.append(set_cookie)
            value = set_cookie.split(";", 1)[0]
            self.cookie = None if value.endswith("=") else value
        ctype = resp.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and ctype.startswith("application/json") else raw
        if isinstance(payload, dict) and "csrf_token" in payload:
            self.csrf = payload["csrf_token"]
        return resp, payload


def cookie_attrs(set_cookie):
    attrs = {}
    for part in set_cookie.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attrs[name.lower()] = value
    return attrs


class ServerCase(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-basepath-")
        self.db_path = os.path.join(self.tmp, "test.db")
        env = {"EASYLIFE_PORT": "0", "EASYLIFE_DB": self.db_path, "EASYLIFE_SECRET": SECRET}
        env.update(self.ENV)
        self.server = app.make_server(app.load_config(env))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.stop_reminders()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, prefix):
        return Client(self.port, prefix)


class BasePathConfigTests(unittest.TestCase):
    def cfg(self, **env):
        env.setdefault("EASYLIFE_SECRET", SECRET)
        return app.load_config(env)

    def test_default_is_root_and_unchanged(self):
        cfg = self.cfg(EASYLIFE_PORT="8086")
        self.assertEqual(cfg["base_path"], "")
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:8086")
        self.assertEqual(self.cfg(BASE_URL="https://easylife.mch.wang/")["base_url"],
                         "https://easylife.mch.wang")

    def test_base_path_is_normalized(self):
        for raw, expected in [("", ""), ("/", ""), ("/todos", "/todos"), ("todos", "/todos"),
                              ("/todos/", "/todos"), (" /todos/ ", "/todos")]:
            self.assertEqual(self.cfg(EASYLIFE_BASE_PATH=raw)["base_path"], expected, raw)

    def test_public_url_gets_exactly_one_prefix(self):
        for public in ("https://mch.wang/todos", "https://mch.wang/todos/", "https://mch.wang"):
            cfg = self.cfg(EASYLIFE_BASE_PATH="/todos", PUBLIC_URL=public)
            self.assertEqual(cfg["base_url"], PUBLIC, public)
        # The legacy BASE_URL name still works; PUBLIC_URL wins when both are set.
        self.assertEqual(self.cfg(EASYLIFE_BASE_PATH="/todos",
                                  BASE_URL="https://mch.wang")["base_url"], PUBLIC)
        self.assertEqual(self.cfg(EASYLIFE_BASE_PATH="/todos", BASE_URL="https://old.example",
                                  PUBLIC_URL="https://mch.wang")["base_url"], PUBLIC)

    def test_invalid_base_path_refuses_to_start(self):
        for bad in ("/to dos", "/todos?x=1", "/todos#a", '/a"b'):
            with self.assertRaises(SystemExit, msg=bad):
                app.validate_config(self.cfg(EASYLIFE_BASE_PATH=bad))
        app.validate_config(self.cfg(EASYLIFE_BASE_PATH="/todos"))


class BasePathProxyTests(ServerCase):
    """BASE_PATH=/todos: the full flow behind both proxy styles."""

    ENV = {"EASYLIFE_BASE_PATH": "/todos", "PUBLIC_URL": PUBLIC}

    def run_flow(self, prefix):
        email = "user%s@example.com" % (prefix.strip("/") or "root")
        c = self.client(prefix)

        resp, body = c.request("POST", "/api/register", {"email": email, "password": PASSWORD})
        self.assertEqual(resp.status, 201, body)
        resp, me = c.request("GET", "/api/me")
        self.assertEqual(resp.status, 200, me)
        self.assertEqual(me["user"]["email"], email)
        self.assertTrue(me["calendar_feed_url"].startswith(PUBLIC + "/api/calendar.ics?token="),
                        me["calendar_feed_url"])

        resp, _ = c.request("POST", "/api/logout", {})
        self.assertEqual(resp.status, 200)
        self.assertEqual(c.request("GET", "/api/me")[0].status, 401)
        resp, body = c.request("POST", "/api/login", {"email": email, "password": PASSWORD})
        self.assertEqual(resp.status, 200, body)

        # Create / read / update / list / notes / delete.
        resp, todo = c.request("POST", "/api/todos", {"title": "周报", "details": "写周报",
                                                      "due_at": "2030-01-02 03:00:00"})
        self.assertEqual(resp.status, 201, todo)
        tid = todo["id"]
        self.assertIn(urllib.parse.quote(PUBLIC + "/todos/%d" % tid, safe=""),
                      todo["google_calendar_url"])
        resp, got = c.request("GET", "/api/todos/%d" % tid)
        self.assertEqual((resp.status, got["title"]), (200, "周报"))
        resp, updated = c.request("PUT", "/api/todos/%d" % tid, {"status": "doing"})
        self.assertEqual((resp.status, updated["status"]), (200, "doing"))
        resp, note = c.request("POST", "/api/todos/%d/notes" % tid, {"content": "进展"})
        self.assertEqual(resp.status, 201, note)
        resp, listing = c.request("GET", "/api/todos")
        self.assertEqual((resp.status, listing["total"]), (200, 1))
        self.assertEqual(listing["items"][0]["notes_count"], 1)

        resp, ics = c.request("GET", "/api/todos/%d/ics" % tid)
        self.assertEqual(resp.status, 200)
        self.assertIn(PUBLIC + "/todos/%d" % tid, unfold(ics.decode("utf-8")))

        resp, body = c.request("DELETE", "/api/todos/%d" % tid)
        self.assertEqual((resp.status, body["deleted_id"]), (200, tid))
        self.assertEqual(c.request("GET", "/api/todos/%d" % tid)[0].status, 404)

        # Every Set-Cookie (register, logout, login) is scoped to /todos.
        self.assertEqual(len(c.set_cookies), 3)
        for set_cookie in c.set_cookies:
            attrs = cookie_attrs(set_cookie)
            self.assertEqual(attrs.get("path"), "/todos", set_cookie)
            self.assertIn("httponly", attrs)
            self.assertIn("secure", attrs)  # PUBLIC_URL is https
        return c

    def test_flow_with_prefix_kept_by_proxy(self):
        self.run_flow("/todos")

    def test_flow_with_prefix_stripped_by_proxy(self):
        self.run_flow("")

    def test_session_works_across_both_proxy_styles(self):
        kept = self.run_flow("/todos")
        stripped = self.client("")
        stripped.cookie, stripped.csrf = kept.cookie, kept.csrf
        resp, me = stripped.request("GET", "/api/me")
        self.assertEqual(resp.status, 200, me)

    def test_bare_prefix_redirects_to_trailing_slash(self):
        c = self.client("")
        resp, _ = c.raw("GET", "/todos")
        self.assertEqual(resp.status, 301)
        self.assertEqual(resp.getheader("Location"), "/todos/")
        resp, _ = c.raw("GET", "/todos?from=mail")
        self.assertEqual((resp.status, resp.getheader("Location")), (301, "/todos/?from=mail"))
        resp, _ = c.raw("HEAD", "/todos")
        self.assertEqual((resp.status, resp.getheader("Location")), (301, "/todos/"))

    def test_index_and_assets_are_served_under_both_styles(self):
        c = self.client("")
        for prefix in ("/todos", ""):
            resp, body = c.raw("GET", prefix + "/")
            self.assertEqual(resp.status, 200, prefix)
            self.assertIn("text/html", resp.getheader("Content-Type"))
            self.assertIn(b'src="./static/app.js"', body)
            for asset, ctype in [("/static/app.js", "text/javascript"),
                                 ("/static/style.css", "text/css"),
                                 ("/static/favicon.svg", "image/svg+xml")]:
                resp, body = c.raw("GET", prefix + asset)
                self.assertEqual(resp.status, 200, prefix + asset)
                self.assertTrue(resp.getheader("Content-Type").startswith(ctype))
                self.assertTrue(body)
        for path in ("/todos/static/../db.py", "/todos/static/%2e%2e/app.py", "/todos/nope",
                     "/todosx/", "/todos/todos/static/app.js"):
            self.assertEqual(c.raw("GET", path)[0].status, 404, path)

    def test_task_links_redirect_to_the_detail_view(self):
        c = self.client("")
        # Prefix kept: /todos/todos/7 ; prefix stripped by nginx: /todos/7.
        for path in ("/todos/todos/7", "/todos/7"):
            resp, _ = c.raw("GET", path)
            self.assertEqual(resp.status, 302, path)
            self.assertEqual(resp.getheader("Location"), "/todos/#/todos/7", path)

    def test_calendar_feed_under_both_styles(self):
        c = self.run_flow("/todos")
        c.request("POST", "/api/todos", {"title": "开会", "due_at": "2030-01-02 03:00:00"})
        token = c.request("GET", "/api/me")[1]["calendar_token"]
        for prefix in ("/todos", ""):
            anon = self.client(prefix)
            resp, body = anon.request("GET", "/api/calendar.ics?token=" + token)
            self.assertEqual(resp.status, 200, prefix)
            text = unfold(body.decode("utf-8"))
            self.assertIn("BEGIN:VCALENDAR", text)
            # The feed deliberately carries no DESCRIPTION/links (privacy); the event is enough.
            self.assertIn("SUMMARY:开会", text)
        resp, reset = c.request("POST", "/api/calendar/token/reset", {})
        self.assertEqual(resp.status, 200)
        self.assertTrue(reset["calendar_feed_url"].startswith(PUBLIC + "/api/calendar.ics?token="))


class BasePathReminderTests(ServerCase):
    ENV = {"EASYLIFE_BASE_PATH": "/todos", "PUBLIC_URL": "https://mch.wang",
           "EASYLIFE_REMIND_INTERVAL": "1"}

    def test_reminder_mail_links_include_prefix(self):
        c = self.client("/todos")
        c.request("POST", "/api/register", {"email": "mail@example.com", "password": PASSWORD})
        resp, todo = c.request("POST", "/api/todos", {"title": "交房租",
                                                      "remind_at": db.now_utc(-60)})
        self.assertEqual(resp.status, 201, todo)

        sent = []
        done = threading.Event()

        def sender(cfg, msg):
            sent.append(msg)
            done.set()
            return "sent"

        # The real daemon thread, fed with the server's own config.
        self.server.start_reminders(sender=sender)
        self.assertTrue(done.wait(10), "reminder thread did not send a mail")
        body = sent[0].get_content()
        self.assertIn("任务链接：https://mch.wang/todos/todos/%d" % todo["id"], body)

        # The link really lands on the detail view.
        deadline = time.time() + 5
        while time.time() < deadline:
            conn = db.get_conn(self.db_path)
            if conn.execute("SELECT reminder_sent FROM todos WHERE id = ?",
                            (todo["id"],)).fetchone()[0] == 1:
                break
            time.sleep(0.05)
        path = urllib.parse.urlsplit(body.rsplit("任务链接：", 1)[1].strip()).path
        resp, _ = c.raw("GET", path)
        self.assertEqual((resp.status, resp.getheader("Location")),
                         (302, "/todos/#/todos/%d" % todo["id"]))

    def test_build_message_uses_configured_base_url(self):
        row = {"id": 3, "title": "t", "details": None, "due_at": None, "remind_at": None,
               "email": "a@example.com"}
        msg = reminders.build_message(row, {}, self.server.cfg["base_url"])
        self.assertIn("https://mch.wang/todos/todos/3", msg.get_content())


class RootPathUnchangedTests(ServerCase):
    """EASYLIFE_BASE_PATH unset: root behaviour exactly as before."""

    def test_cookie_path_is_root_and_no_prefix_handling(self):
        c = self.client("")
        resp, _ = c.request("POST", "/api/register", {"email": "r@example.com",
                                                      "password": PASSWORD})
        self.assertEqual(resp.status, 201)
        attrs = cookie_attrs(c.set_cookies[0])
        self.assertEqual(attrs.get("path"), "/")
        self.assertNotIn("secure", attrs)  # default base URL is plain http
        resp, me = c.request("GET", "/api/me")
        # EASYLIFE_PORT=0 in tests, so the default public URL is http://127.0.0.1:0.
        self.assertTrue(me["calendar_feed_url"].startswith(
            "http://127.0.0.1:0/api/calendar.ics?token="), me["calendar_feed_url"])
        self.assertEqual(c.raw("GET", "/")[0].status, 200)
        self.assertEqual(c.raw("GET", "/static/app.js")[0].status, 200)
        # Without a base path nothing is stripped and nothing redirects to "/todos/".
        self.assertEqual(c.raw("GET", "/todos")[0].status, 404)
        self.assertEqual(c.raw("GET", "/todos/api/me")[0].status, 404)
        resp, _ = c.raw("GET", "/todos/5")
        self.assertEqual((resp.status, resp.getheader("Location")), (302, "/#/todos/5"))


if __name__ == "__main__":
    unittest.main()
