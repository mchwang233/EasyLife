"""End-to-end API tests against a real server on a temporary database."""

import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app  # noqa: E402

PASSWORD = "correct-horse-battery"


class Client:
    """Tiny HTTP client that keeps the session cookie and CSRF token."""

    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def request(self, method, path, body=None, headers=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if csrf and self.csrf and method in ("POST", "PUT", "DELETE"):
            hdrs["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith(app.COOKIE_NAME + "="):
            value = set_cookie.split(";", 1)[0]
            self.cookie = None if value.endswith("=") else value
        ctype = resp.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and ctype.startswith("application/json") else raw
        if isinstance(payload, dict) and "csrf_token" in payload:
            self.csrf = payload["csrf_token"]
        return resp, payload

    def register(self, email, password=PASSWORD):
        return self.request("POST", "/api/register", {"email": email, "password": password})

    def login(self, email, password=PASSWORD):
        return self.request("POST", "/api/login", {"email": email, "password": password})


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        cfg = app.load_config({
            "EASYLIFE_PORT": "0",
            "EASYLIFE_DB": self.db_path,
            "EASYLIFE_SECRET": "test-secret-0123456789abcdef",
        })
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        return Client(self.port)

    def logged_in(self, email):
        c = self.client()
        resp, _ = c.register(email)
        self.assertEqual(resp.status, 201)
        return c


class AuthTests(ApiTestCase):
    def test_register_sets_session_and_returns_user(self):
        c = self.client()
        resp, body = c.register("Alice@Example.com ")
        self.assertEqual(resp.status, 201)
        self.assertEqual(body["user"]["email"], "alice@example.com")
        self.assertTrue(body["csrf_token"])
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        resp, me = c.request("GET", "/api/me")
        self.assertEqual(resp.status, 200)
        self.assertEqual(me["user"]["email"], "alice@example.com")

    def test_duplicate_registration_is_409(self):
        self.client().register("bob@example.com")
        resp, body = self.client().register("BOB@example.com")
        self.assertEqual(resp.status, 409)
        self.assertEqual(body["error"]["code"], "conflict")

    def test_invalid_registration_is_400(self):
        resp, body = self.client().register("not-an-email")
        self.assertEqual(resp.status, 400)
        resp, body = self.client().register("short@example.com", "1234567")
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_login_wrong_password_and_unknown_email_are_401(self):
        self.client().register("carol@example.com")
        c = self.client()
        resp, body = c.login("carol@example.com", "wrong-password")
        self.assertEqual(resp.status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertIsNone(c.cookie)
        resp, unknown = c.login("nobody@example.com", "wrong-password")
        self.assertEqual(resp.status, 401)
        self.assertEqual(unknown["error"]["message"], body["error"]["message"])
        resp, body = c.login("carol@example.com")
        self.assertEqual(resp.status, 200)
        self.assertTrue(body["expires_at"])
        self.assertEqual(c.request("GET", "/api/todos")[0].status, 200)

    def test_password_not_stored_in_plaintext(self):
        self.client().register("dave@example.com")
        conn = sqlite3.connect(self.db_path)
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
        conn.close()
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$260000$"))

    def test_login_rate_limited_after_repeated_failures(self):
        self.client().register("eve@example.com")
        c = self.client()
        for _ in range(5):
            self.assertEqual(c.login("eve@example.com", "bad-password")[0].status, 401)
        resp, body = c.login("eve@example.com")
        self.assertEqual(resp.status, 429)
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertTrue(int(resp.getheader("Retry-After")) > 0)

    def test_logout_invalidates_session(self):
        c = self.logged_in("frank@example.com")
        resp, body = c.request("POST", "/api/logout")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertIsNone(c.cookie)
        self.assertEqual(c.request("GET", "/api/me")[0].status, 401)


class UnauthenticatedTests(ApiTestCase):
    def test_todos_require_login(self):
        c = self.client()
        for method, path, body in [
            ("GET", "/api/todos", None),
            ("POST", "/api/todos", {"title": "x"}),
            ("GET", "/api/todos/1", None),
            ("PUT", "/api/todos/1", {"title": "x"}),
            ("DELETE", "/api/todos/1", None),
            ("GET", "/api/todos/1/notes", None),
            ("POST", "/api/todos/1/notes", {"content": "x"}),
            ("GET", "/api/me", None),
        ]:
            resp, payload = c.request(method, path, body)
            self.assertEqual(resp.status, 401, (method, path))
            self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_forged_session_cookie_is_401(self):
        c = self.client()
        c.cookie = app.COOKIE_NAME + "=forged-token"
        self.assertEqual(c.request("GET", "/api/todos")[0].status, 401)


class TodoTests(ApiTestCase):
    def test_crud_flow(self):
        c = self.logged_in("alice@example.com")

        resp, todo = c.request("POST", "/api/todos", {
            "title": "  写项目周报 ", "details": "汇总本周进展\n下周计划", "priority": 3,
            "due_at": "2024-06-03T10:00:00Z", "remind_at": "2024-06-03 09:00:00",
        })
        self.assertEqual(resp.status, 201)
        self.assertEqual(todo["title"], "写项目周报")
        self.assertEqual(todo["status"], "open")
        self.assertEqual(todo["due_at"], "2024-06-03 10:00:00")
        self.assertEqual(todo["notes"], [])
        tid = todo["id"]

        c.request("POST", "/api/todos", {"title": "second"})
        resp, listing = c.request("GET", "/api/todos")
        self.assertEqual(resp.status, 200)
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["items"][0]["id"], tid)  # priority 3 sorts first
        self.assertEqual(c.request("GET", "/api/todos?q=%E5%91%A8%E6%8A%A5")[1]["total"], 1)

        resp, got = c.request("GET", "/api/todos/%d" % tid)
        self.assertEqual(resp.status, 200)
        self.assertEqual(got["details"], "汇总本周进展\n下周计划")

        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE todos SET reminder_sent = 1 WHERE id = ?", (tid,))
        conn.commit()
        conn.close()
        resp, updated = c.request("PUT", "/api/todos/%d" % tid,
                                  {"status": "done", "remind_at": "2024-06-03 08:00:00"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["title"], "写项目周报")
        self.assertEqual(updated["reminder_sent"], 0)
        resp, updated = c.request("PUT", "/api/todos/%d" % tid, {"details": None})
        self.assertIsNone(updated["details"])

        resp, body = c.request("DELETE", "/api/todos/%d" % tid)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, {"ok": True, "deleted_id": tid})
        self.assertEqual(c.request("GET", "/api/todos/%d" % tid)[0].status, 404)
        self.assertEqual(c.request("GET", "/api/todos")[1]["total"], 1)

    def test_validation_errors(self):
        c = self.logged_in("alice@example.com")
        for body in [{}, {"title": ""}, {"title": "x" * 201}, {"title": "x", "status": "bad"},
                     {"title": "x", "priority": 7}, {"title": "x", "due_at": "tomorrow"},
                     {"title": "x", "bogus": 1}]:
            resp, payload = c.request("POST", "/api/todos", body)
            self.assertEqual(resp.status, 400, body)
            self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertEqual(c.request("GET", "/api/todos/abc")[0].status, 404)

    def test_bad_json_is_400(self):
        c = self.logged_in("alice@example.com")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/todos", body=b"{not json",
                     headers={"Content-Type": "application/json", "Cookie": c.cookie,
                              "X-CSRF-Token": c.csrf})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_write_without_csrf_is_403(self):
        c = self.logged_in("alice@example.com")
        resp, body = c.request("POST", "/api/todos", {"title": "x"}, csrf=False)
        self.assertEqual(resp.status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        resp, _ = c.request("POST", "/api/todos", {"title": "x"},
                            headers={"X-CSRF-Token": "wrong"}, csrf=False)
        self.assertEqual(resp.status, 403)

    def test_notes(self):
        c = self.logged_in("alice@example.com")
        tid = c.request("POST", "/api/todos", {"title": "task"})[1]["id"]
        resp, note = c.request("POST", "/api/todos/%d/notes" % tid, {"content": "已完成第 1 节"})
        self.assertEqual(resp.status, 201)
        self.assertEqual(note["todo_id"], tid)
        self.assertEqual(note["content"], "已完成第 1 节")
        c.request("POST", "/api/todos/%d/notes" % tid, {"content": "等待切图"})

        resp, notes = c.request("GET", "/api/todos/%d/notes" % tid)
        self.assertEqual(resp.status, 200)
        self.assertEqual(notes["total"], 2)
        self.assertEqual([n["content"] for n in notes["items"]], ["已完成第 1 节", "等待切图"])
        self.assertEqual(len(c.request("GET", "/api/todos/%d" % tid)[1]["notes"]), 2)
        self.assertEqual(c.request("GET", "/api/todos")[1]["items"][0]["notes_count"], 2)

        self.assertEqual(c.request("POST", "/api/todos/%d/notes" % tid, {"content": " "})[0].status, 400)
        self.assertEqual(c.request("GET", "/api/todos/999/notes")[0].status, 404)

        c.request("DELETE", "/api/todos/%d" % tid)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM todo_notes").fetchone()[0], 0)
        conn.close()

    def test_cross_user_isolation(self):
        a = self.logged_in("a@example.com")
        b = self.logged_in("b@example.com")
        tid = a.request("POST", "/api/todos", {"title": "A's secret", "details": "private"})[1]["id"]
        a.request("POST", "/api/todos/%d/notes" % tid, {"content": "A's note"})

        listing = b.request("GET", "/api/todos")[1]
        self.assertEqual(listing["total"], 0)
        self.assertEqual(b.request("GET", "/api/todos?q=secret")[1]["items"], [])

        for method, path, body in [
            ("GET", "/api/todos/%d" % tid, None),
            ("PUT", "/api/todos/%d" % tid, {"title": "hijacked"}),
            ("DELETE", "/api/todos/%d" % tid, None),
            ("GET", "/api/todos/%d/notes" % tid, None),
            ("POST", "/api/todos/%d/notes" % tid, {"content": "B's note"}),
        ]:
            resp, payload = b.request(method, path, body)
            self.assertEqual(resp.status, 404, (method, path))
            self.assertEqual(payload["error"]["code"], "not_found")

        # user_id in the body is ignored; B's todo stays B's.
        resp, own = b.request("POST", "/api/todos", {"title": "B's", "user_id": 1})
        self.assertEqual(resp.status, 201)
        self.assertEqual(a.request("GET", "/api/todos")[1]["total"], 1)

        resp, todo = a.request("GET", "/api/todos/%d" % tid)
        self.assertEqual(resp.status, 200)
        self.assertEqual(todo["title"], "A's secret")
        self.assertEqual([n["content"] for n in todo["notes"]], ["A's note"])


class ServerTests(ApiTestCase):
    def test_security_headers_and_errors(self):
        resp, body = self.client().request("GET", "/api/nope")
        self.assertEqual(resp.status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        for name in app.SECURITY_HEADERS:
            self.assertEqual(resp.getheader(name), app.SECURITY_HEADERS[name])
        resp, body = self.client().request("PATCH", "/api/todos")
        self.assertEqual(resp.status, 405)
        self.assertIn("GET", resp.getheader("Allow"))

    def test_static_path_traversal_blocked(self):
        for path in ["/static/../db.py", "/static/%2e%2e/db.py", "/static/..%2fapp.py", "/etc/passwd"]:
            self.assertEqual(self.client().request("GET", path)[0].status, 404, path)

    def test_oversized_body_is_413(self):
        c = self.logged_in("alice@example.com")
        resp, body = c.request("POST", "/api/todos", {"title": "x", "details": "y" * (300 * 1024)})
        self.assertEqual(resp.status, 413)

    def test_config_defaults(self):
        cfg = app.load_config({})
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["port"], 8086)
        with self.assertRaises(SystemExit):
            app.validate_config(cfg)

    def test_schema_initialised_and_idempotent(self):
        from easylife import db
        db.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertTrue({"users", "sessions", "todos", "todo_notes"} <= tables)
        self.assertEqual(version, len(db.MIGRATIONS))


if __name__ == "__main__":
    unittest.main()
