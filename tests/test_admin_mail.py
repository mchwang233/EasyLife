"""Admin SMTP contract: real HTTP/SQLite, simulated transports and loopback SMTP.

No external mailbox or credentials are used. Secrets below are random canaries;
assertions never include their value in a failure message or test artifact.
"""

import concurrent.futures
import http.client
import json
import logging
import os
import secrets
import smtplib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from easylife import app, auth, auth_mail, db, mail_settings, reminders, todos
from tests.test_api import Client, PASSWORD
from tests.test_reminders_ics import FakeSMTP, FakeSMTPSSL, MiniSMTPServer

SETTINGS = "/api/admin/mail-settings"
TEST = SETTINGS + "/test"
ADMIN = "admin@example.com"
MEMBER = "member@example.com"


class AdminMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="easylife-admin-mail-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mail.db")
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.accounts = []
        for address in (ADMIN, MEMBER):
            user, token, _ = auth.register(self.conn, address, PASSWORD,
                                           ip="127.0.0.1", limiter=auth.LoginRateLimiter())
            self.accounts.append((user, token))
        self.conn.execute("UPDATE users SET email_verified_at = ?", (db.now_utc(),))
        self.secret = secrets.token_urlsafe(24)
        self.old_password = secrets.token_urlsafe(32)
        self.new_password = secrets.token_urlsafe(32)
        self.env_password = secrets.token_urlsafe(32)
        self.env = {"EASYLIFE_PORT": "0", "EASYLIFE_DB": self.path,
                    "EASYLIFE_SECRET": self.secret, "EASYLIFE_ADMIN_USER_IDS": "1",
                    "EASYLIFE_BASE_PATH": "/todos", "EASYLIFE_REMIND_INTERVAL": "1"}
        self.server = None
        self.addCleanup(self.stop_server)
        self.start_server()

    def start_server(self):
        self.server = app.make_server(app.load_config(self.env))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin = self.client(0)
        self.member = self.client(1)

    def stop_server(self):
        if self.server is not None:
            self.server.stop_reminders()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.server = None
        db.close_thread_conns()

    def restart(self, **env):
        self.stop_server()
        self.env.update(env)
        self.start_server()

    def client(self, account=None):
        client = Client(self.server.server_address[1])
        if account is not None:
            _, token = self.accounts[account]
            client.cookie = app.COOKIE_NAME + "=" + token
            client.csrf = auth.csrf_token(self.secret, token)
        return client

    def put(self, data, status=200):
        response, result = self.admin.request("PUT", SETTINGS, data)
        self.assertEqual(response.status, status)
        self.no_secrets(json.dumps(result))
        return result

    def get(self):
        response, result = self.admin.request("GET", SETTINGS)
        self.assertEqual(response.status, 200)
        self.no_secrets(json.dumps(result))
        return result

    def effective(self):
        return mail_settings.resolve_mail_config(self.conn, self.server.cfg)

    def saved_row(self):
        row = self.conn.execute("SELECT * FROM site_settings WHERE key = ?", (mail_settings.KEY,)).fetchone()
        return dict(row) if row else None

    def no_secrets(self, text):
        for value in (self.secret, self.old_password, self.new_password, self.env_password):
            self.assertFalse(value in text, "secret canary was exposed")

    def smtp_settings(self, **changes):
        value = {"enabled": True, "host": "smtp.saved.example", "port": 465,
                 "user": "saved@example.com", "from": "EasyLife <saved@example.com>",
                 "password": self.old_password}
        value.update(changes)
        return value

    def pending_todo(self):
        return todos.create_todo(self.conn, 1, {"title": "邮件配置运行时测试",
                                                "remind_at": db.now_utc(-60)})["id"]

    def test_all_admin_endpoints_require_server_role_and_session_with_both_proxy_paths(self):
        for prefix in ("", "/todos"):
            for method, endpoint, body in (("GET", SETTINGS, None), ("PUT", SETTINGS, {}),
                                            ("POST", TEST, {})):
                for client, status in ((self.client(), 401), (self.member, 403)):
                    response, result = client.request(method, prefix + endpoint, body)
                    self.assertEqual(response.status, status)
                    self.assertEqual(set(result), {"error"})
        self.assertEqual(self.admin.request("GET", SETTINGS)[0].status, 200)
        self.assertTrue(self.admin.request("GET", "/todos/api/me")[1]["user"]["is_admin"])
        self.assertFalse(self.member.request("GET", "/api/me")[1]["user"]["is_admin"])
        self.assertTrue(self.client().login(ADMIN)[1]["user"]["is_admin"])
        for method, endpoint in (("DELETE", SETTINGS), ("GET", TEST), ("GET", SETTINGS + "/unknown")):
            self.assertEqual(self.client().request(method, endpoint)[0].status, 401)
            self.assertEqual(self.member.request(method, endpoint)[0].status, 403)

    def test_default_no_admin_and_legacy_email_setting_never_grants_permissions(self):
        self.restart(EASYLIFE_ADMIN_USER_IDS="", EASYLIFE_ADMIN_EMAILS=ADMIN)
        self.assertEqual(self.server.admin_user_ids, frozenset())
        self.assertFalse(self.client().login(ADMIN)[1]["user"]["is_admin"])
        for method, endpoint, body in (("GET", SETTINGS, None), ("PUT", SETTINGS, {}),
                                        ("POST", TEST, {})):
            self.assertEqual(self.admin.request(method, endpoint, body)[0].status, 403)

    def test_invalid_and_future_admin_ids_are_frozen_and_client_cannot_promote(self):
        with self.assertLogs("easylife", "WARNING") as logs:
            self.restart(EASYLIFE_ADMIN_USER_IDS="1, 001 3,0,-2,2.0,+2,abc,9223372036854775808",
                         EASYLIFE_ADMIN_EMAILS="future@example.com")
        self.assertIn("3 does not exist; disabled", "\n".join(logs.output))
        self.assertEqual(self.server.cfg["admin_user_ids"], (1, 3))
        self.assertEqual(self.server.admin_user_ids, frozenset({1}))
        future = self.client()
        with mock.patch.object(reminders, "send_message", return_value="logged"):
            response, result = future.request("POST", "/api/register", {
                "email": "future@example.com", "password": PASSWORD, "is_admin": True,
                "admin_user_ids": [3], "id": 1, "role": "admin"})
        self.assertEqual(response.status, 201)
        self.assertEqual(result["user"]["id"], 3)
        self.assertFalse(result["user"]["is_admin"])
        self.assertFalse(future.request("GET", "/api/me")[1]["user"]["is_admin"])
        self.assertEqual(future.request("GET", SETTINGS)[0].status, 403)
        self.assertFalse(self.client().login("future@example.com")[1]["user"]["is_admin"])
        self.server.cfg["admin_user_ids"] = (1, 2, 3)
        self.assertEqual(future.request("GET", SETTINGS)[0].status, 403)

    def test_deleted_admin_email_and_autoincrement_id_are_not_reused(self):
        self.conn.execute("DELETE FROM users WHERE id = 1")
        with mock.patch.object(reminders, "send_message", return_value="logged"):
            response, result = self.client().register(ADMIN)
        self.assertEqual(response.status, 201)
        self.assertGreater(result["user"]["id"], 2)
        self.assertFalse(result["user"]["is_admin"])
        self.assertEqual(self.admin.request("GET", SETTINGS)[0].status, 401)

    def test_writes_require_session_bound_csrf_for_cookies_and_header_tokens(self):
        for method, endpoint in (("PUT", SETTINGS), ("POST", TEST)):
            for token in (None, "wrong", self.member.csrf):
                headers = {"X-CSRF-Token": token} if token else {}
                response, result = self.admin.request(method, endpoint, {}, headers=headers, csrf=False)
                self.assertEqual(response.status, 403)
                self.assertEqual(result["error"]["code"], "forbidden")
            header_client = self.client()
            headers = {"X-Auth-Token": self.accounts[0][1]}
            self.assertEqual(header_client.request(method, endpoint, {}, headers=headers)[0].status, 403)
            headers["X-CSRF-Token"] = self.admin.csrf
            status = header_client.request(method, endpoint, {}, headers=headers)[0].status
            self.assertEqual(status, 200 if method == "PUT" else 409)

    def test_default_response_allowlist_and_env_only_response_are_redacted(self):
        result = self.get()
        self.assertEqual(result, {
            "mail": {"enabled": False, "host": "", "port": 587, "user": "",
                     "from": "no-reply@localhost", "password_set": False},
            "source": "defaults", "password_source": "none", "updated_at": None, "updated_by": None})
        self.restart(SMTP_HOST="smtp.env.example", SMTP_PORT="465", SMTP_USER="env@example.com",
                     SMTP_PASS=self.env_password, SMTP_FROM="Environment <env@example.com>")
        result = self.get()
        self.assertEqual(result["source"], "environment")
        self.assertEqual(result["password_source"], "environment")
        self.assertTrue(result["mail"]["password_set"])
        self.assertEqual(result["mail"]["host"], "smtp.env.example")
        self.assertIsNone(result["updated_at"])
        with mock.patch.object(reminders, "send_message", return_value="sent") as send:
            self.assertEqual(self.admin.request("POST", TEST, {})[0].status, 200)
        self.assertTrue(send.call_args[0][0]["password"] == self.env_password)

    def test_partial_update_password_preservation_replacement_and_explicit_clear(self):
        self.put(self.smtp_settings())
        for patch in ({"host": "smtp.changed.example"}, {"password": ""}, {"password": None}):
            result = self.put(patch)
            self.assertEqual(result["source"], "database")
            self.assertEqual(result["password_source"], "database")
            self.assertEqual(result["updated_by"], ADMIN)
            self.assertTrue(self.effective()["password"] == self.old_password)
        self.put({"password": self.new_password})
        self.assertTrue(self.effective()["password"] == self.new_password)
        self.put({"clear_password": True})
        for patch in ({"password": ""}, {"password": None}, {}):
            result = self.put(patch)
            self.assertFalse(result["mail"]["password_set"])
            self.assertEqual(result["password_source"], "none")
        self.assertNotIn("password", json.loads(self.saved_row()["value"]))

    def test_per_field_environment_fallback_disable_and_row_deletion(self):
        self.restart(SMTP_HOST="smtp.env.example", SMTP_PORT="587", SMTP_USER="env@example.com",
                     SMTP_PASS=self.env_password, SMTP_FROM="Environment <env@example.com>")
        self.put(self.smtp_settings())
        self.put({"host": "", "user": "", "from": "", "clear_password": True, "password": None})
        result = self.get()
        self.assertEqual(result["mail"]["host"], "smtp.env.example")
        self.assertEqual(result["mail"]["user"], "env@example.com")
        self.assertEqual(result["mail"]["from"], "Environment <env@example.com>")
        self.assertEqual(result["mail"]["port"], 465)
        self.assertEqual(result["password_source"], "environment")
        self.assertTrue(self.effective()["password"] == self.env_password)
        result = self.put({"enabled": False})
        self.assertFalse(result["mail"]["enabled"])
        self.assertEqual(result["mail"]["host"], "smtp.env.example")
        with mock.patch.object(reminders.smtplib, "SMTP") as smtp, \
                mock.patch.object(reminders.smtplib, "SMTP_SSL") as ssl:
            self.assertEqual(self.admin.request("POST", TEST, {})[0].status, 409)
            user = auth.user_by_email(self.conn, ADMIN)
            self.assertEqual(auth_mail.send(self.conn, user, "verify", self.server.cfg), "logged")
            todo_id = self.pending_todo()
            self.assertEqual(reminders.run_once(self.conn, cfg=self.server.cfg), [])
            smtp.assert_not_called()
            ssl.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0], 0)
        self.assertTrue(self.put({"enabled": True})["mail"]["enabled"])
        self.conn.execute("DELETE FROM site_settings WHERE key = ?", (mail_settings.KEY,))
        result = self.get()
        self.assertEqual(result["source"], "environment")
        self.assertEqual(result["mail"]["port"], 587)
        self.assertIsNone(result["updated_at"])

    def test_sender_default_follows_effective_user_and_explicit_env_sender_wins(self):
        self.restart(SMTP_USER="env@example.com")
        self.assertEqual(self.put({"user": "saved@example.com"})["mail"]["from"], "saved@example.com")
        self.assertEqual(self.put({"user": ""})["mail"]["from"], "env@example.com")
        self.restart(SMTP_FROM="Explicit <from@example.com>")
        self.assertEqual(self.put({"user": "saved@example.com"})["mail"]["from"], "Explicit <from@example.com>")

    def test_invalid_fields_never_change_old_configuration_or_reflect_secrets(self):
        self.put(self.smtp_settings())
        before = self.saved_row()
        invalid = [[], None, "bad", {"extra": self.new_password}, {"is_admin": True},
                   {"enabled": 1}, {"enabled": "true"}, {"clear_password": 0},
                   {"port": "465"}, {"port": True}, {"port": 465.0}, {"port": 0},
                   {"port": 65536}, {"port": None}, {"host": 1}, {"host": None},
                   {"host": "h" * 254}, {"host": " smtp.example.com"}, {"host": "a\tb"},
                   {"host": "a\u2003b"}, {"user": "u" * 255}, {"user": []},
                   {"from": "a" * 321}, {"from": "not-email"},
                   {"from": ".:@"}, {"from": " .>>]"},
                   {"from": "one@example.com,two@example.com"},
                   {"from": "one@example.com unwanted suffix"},
                   {"password": "p" * 513}, {"password": False}, {"password": []},
                   {"clear_password": True, "password": self.new_password}]
        for field in ("host", "user", "from", "password"):
            for value in ("x\r\nBcc: victim@example.com", "x\x00", "x\ud800"):
                invalid.append({field: value})
        for patch in invalid:
            # None must be encoded as a JSON null rather than Client's no-body default.
            if patch is None:
                self.raw_request("PUT", SETTINGS, b"null", 400)
            else:
                self.put(patch, 400)
            self.assertTrue(self.saved_row() == before, "invalid input changed saved configuration")

    def test_valid_length_and_port_boundaries(self):
        for port in (1, 65535, 465, 587):
            self.assertEqual(self.put({"port": port})["mail"]["port"], port)
        self.put({"host": "h" * 253, "user": "u" * 254, "password": "p" * 512,
                  "from": '"' + "名" * (320 - len('\"\" <sender@example.com>')) + '" <sender@example.com>'})
        self.assertEqual(len(self.effective()["password"]), 512)

    def raw_request(self, method, endpoint, payload, expected, content_type="application/json"):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Cookie": self.admin.cookie, "X-CSRF-Token": self.admin.csrf,
                   "Content-Type": content_type}
        client.request(method, endpoint, body=payload, headers=headers)
        response = client.getresponse()
        raw = response.read().decode()
        client.close()
        self.assertEqual(response.status, expected)
        self.no_secrets(raw)

    def test_json_and_body_limit_rejections_are_atomic(self):
        self.put(self.smtp_settings())
        before = self.saved_row()
        for method, endpoint in (("PUT", SETTINGS), ("POST", TEST)):
            self.raw_request(method, endpoint, b"{bad", 400)
            self.raw_request(method, endpoint, b"{}", 400, "text/plain")
            self.raw_request(method, endpoint, b" " * (app.MAX_BODY + 1), 413)
        self.assertTrue(self.saved_row() == before)

    def test_insert_update_and_commit_failure_roll_back_and_do_not_leak(self):
        # SQLite trigger errors may echo sensitive values; the HTTP log must not.
        for operation in ("INSERT", "UPDATE", "COMMIT"):
            if operation != "INSERT":
                self.put(self.smtp_settings())
            before = self.saved_row()
            if operation == "COMMIT":
                self.conn.execute("CREATE TABLE commit_guard (user_id INTEGER REFERENCES users(id) "
                                  "DEFERRABLE INITIALLY DEFERRED)")
                self.conn.execute("CREATE TRIGGER reject_save AFTER UPDATE ON site_settings "
                                  "BEGIN INSERT INTO commit_guard VALUES (99999); END")
            else:
                # Canary is URL-safe; trigger is local test-only fault injection.
                self.conn.execute("CREATE TRIGGER reject_save BEFORE %s ON site_settings "
                                  "BEGIN SELECT RAISE(ABORT, '%s'); END" % (operation, self.old_password))
            with self.assertLogs("easylife", "ERROR") as logs:
                self.put(self.smtp_settings(host="smtp.failed.example", password=self.new_password), 500)
            self.no_secrets("\n".join(logs.output))
            self.assertTrue(self.saved_row() == before, "failed transaction changed stored settings")
            self.conn.execute("DROP TRIGGER reject_save")
        self.put({"host": "smtp.recovered.example"})
        self.assertEqual(self.effective()["host"], "smtp.recovered.example")

    def test_commit_failure_also_rolls_back_same_connection_and_releases_transaction(self):
        self.put(self.smtp_settings())
        before = self.saved_row()
        self.conn.execute("CREATE TABLE commit_guard (user_id INTEGER REFERENCES users(id) "
                          "DEFERRABLE INITIALLY DEFERRED)")
        self.conn.execute("CREATE TRIGGER reject_commit AFTER UPDATE ON site_settings "
                          "BEGIN INSERT INTO commit_guard VALUES (99999); END")
        with self.assertRaises(sqlite3.IntegrityError):
            mail_settings.save(self.conn, self.server.cfg, {"host": "bad.example"}, 1)
        self.assertFalse(self.conn.in_transaction)
        self.assertTrue(self.saved_row() == before)

    def test_concurrent_partial_updates_preserve_both_fields(self):
        self.put(self.smtp_settings())
        barrier = threading.Barrier(2)

        def write(patch):
            client = self.client(0)
            barrier.wait(3)
            return client.request("PUT", SETTINGS, patch)[0].status

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ({"host": "concurrent.example"}, {"user": "other@example.com"})))
        self.assertEqual(results, [200, 200])
        self.assertEqual(self.get()["mail"]["host"], "concurrent.example")
        self.assertEqual(self.get()["mail"]["user"], "other@example.com")
        self.assertTrue(self.effective()["password"] == self.old_password)

    def test_test_endpoint_rejects_overrides_and_invalid_recipients_before_delivery(self):
        self.put(self.smtp_settings())
        before = self.saved_row()
        with mock.patch.object(reminders, "send_message") as send:
            for field in (*mail_settings.FIELDS, "smtp", "timeout", "is_admin"):
                self.assertEqual(self.admin.request("POST", TEST, {field: "override"})[0].status, 400)
            for value in (None, True, [], "", "not-an-email", "a@example.com,b@example.com",
                          "\r\na@example.com", "a@example.com\nBcc: b@example.com", "x" * 255):
                self.assertEqual(self.admin.request("POST", TEST, {"to": value})[0].status, 400)
            send.assert_not_called()
        self.assertTrue(self.saved_row() == before)

    def test_disabled_missing_host_and_logged_results_cannot_claim_success(self):
        with mock.patch.object(reminders, "send_message") as send:
            for patch in (None, {"enabled": True}, {"host": "smtp.saved.example", "enabled": False}):
                if patch is not None:
                    self.put(patch)
                response, result = self.admin.request("POST", TEST, {})
                self.assertEqual(response.status, 409)
                self.assertEqual(result["error"]["code"], "mail_disabled")
            send.assert_not_called()
        self.put({"enabled": True})
        with mock.patch.object(reminders, "send_message", return_value="logged"):
            response, result = self.admin.request("POST", TEST, {})
            self.assertEqual(response.status, 502)
            self.assertNotIn("sent", result)

    def test_smtp_failure_responses_and_all_senders_logs_hide_secrets(self):
        self.put(self.smtp_settings())
        todo_id = self.pending_todo()
        user = auth.user_by_email(self.conn, ADMIN)
        with mock.patch.object(reminders, "send_message", side_effect=smtplib.SMTPAuthenticationError(
                535, self.old_password.encode())), self.assertLogs("easylife", "ERROR") as logs:
            response, result = self.admin.request("POST", TEST, {})
            self.assertEqual(response.status, 502)
            self.assertEqual(result["error"]["code"], "mail_delivery_failed")
            self.no_secrets(json.dumps(result))
            self.assertEqual(auth_mail.send(self.conn, user, "reset", self.server.cfg), "failed")
            self.assertEqual(reminders.run_once(self.conn, cfg=self.server.cfg), [])
        self.no_secrets("\n".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0], 0)

    def test_rate_limit_counts_failures_across_sessions_and_recipients_and_expires(self):
        self.put(self.smtp_settings())
        second_session = self.client()
        self.assertEqual(second_session.login(ADMIN)[0].status, 200)
        with mock.patch.object(auth.time, "monotonic", return_value=1000) as clock, \
                mock.patch.object(reminders, "send_message", side_effect=TimeoutError(self.old_password)) as send:
            for index in range(5):
                self.assertEqual(self.admin.request("POST", TEST, {"to": "test%d@example.com" % index})[0].status, 502)
            clock.return_value = 1001
            response, result = second_session.request("POST", TEST, {})
            self.assertEqual(response.status, 429)
            self.assertEqual(result["error"]["code"], "rate_limited")
            self.assertEqual(response.getheader("Retry-After"), "599")
            self.assertEqual(send.call_count, 5)
            clock.return_value = 1600
            self.assertEqual(second_session.request("POST", TEST, {})[0].status, 502)
            self.assertEqual(send.call_count, 6)

    def test_465_and_587_share_sender_and_do_not_touch_reminders_or_auth_tokens(self):
        todo_id = self.pending_todo()
        token = auth.issue_token(self.conn, 1, "verify", auth.VERIFY_TOKEN_TTL)
        before = [dict(row) for row in self.conn.execute("SELECT * FROM auth_tokens")]
        for port in (465, 587):
            FakeSMTP.instances, FakeSMTP.messages, FakeSMTP.offers_starttls = [], [], True
            self.put(self.smtp_settings(port=port))
            with mock.patch.object(reminders.smtplib, "SMTP", FakeSMTP), \
                    mock.patch.object(reminders.smtplib, "SMTP_SSL", FakeSMTPSSL):
                response, result = self.admin.request("POST", TEST, {})
            self.assertEqual(response.status, 200)
            self.assertTrue(result["sent"])
            self.assertEqual(result["to"], ADMIN)
            self.assertIn("SMTP", result["message"])
            self.assertIn("不代表邮件已送达", result["message"])
            self.assertGreaterEqual(result["latency_ms"], 0)
            transport, = FakeSMTP.instances
            self.assertEqual(transport.host, "smtp.saved.example")
            self.assertEqual(transport.timeout, reminders.SMTP_TIMEOUT)
            self.assertEqual(isinstance(transport, FakeSMTPSSL), port == 465)
            self.assertEqual("starttls" in [call[0] for call in transport.calls], port == 587)
            login, = [call for call in transport.calls if call[0] == "login"]
            self.assertTrue(login[2] == self.old_password)
        self.assertTrue(before == [dict(row) for row in self.conn.execute("SELECT * FROM auth_tokens")])
        self.assertEqual(self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens WHERE token_hash = ?",
                                           (auth.hash_token(token),)).fetchone()[0])

    def test_real_loopback_smtp_accepts_saved_settings(self):
        smtp = MiniSMTPServer()
        smtp.start()
        self.addCleanup(smtp.close)
        self.put({"enabled": True, "host": "127.0.0.1", "port": smtp.port,
                  "from": "EasyLife <sender@example.com>"})
        response, result = self.admin.request("POST", TEST, {"to": "recipient@example.com"})
        self.assertEqual(response.status, 200)
        self.assertTrue(result["sent"])
        messages = smtp.received()
        self.assertEqual(len(messages), 1)
        self.assertIn("To: recipient@example.com", messages[0])
        self.assertIn("From: EasyLife <sender@example.com>", messages[0])
        self.no_secrets(messages[0])

    def test_recipient_refusal_from_shared_transport_is_not_a_success(self):
        self.put(self.smtp_settings())
        with mock.patch.object(reminders.smtplib, "SMTP_SSL", FakeSMTPSSL), \
                mock.patch.object(FakeSMTP, "send_message", return_value={
                    ADMIN: (550, self.old_password.encode())}), \
                self.assertLogs("easylife", "ERROR") as logs:
            response, result = self.admin.request("POST", TEST, {})
        self.assertEqual(response.status, 502)
        self.assertEqual(result["error"]["code"], "mail_delivery_failed")
        self.no_secrets(json.dumps(result) + "\n".join(logs.output))

    def test_run_once_without_explicit_injection_uses_same_resolver_defaults(self):
        self.put({"host": "saved.example", "user": "saved@example.com"})
        todo_id = self.pending_todo()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(reminders, "send_message", return_value="sent") as send:
            self.assertEqual(reminders.run_once(self.conn), [todo_id])
        self.assertEqual(send.call_args[0][0]["from"], "saved@example.com")
        self.assertEqual(send.call_args[0][0]["host"], "saved.example")

    def test_registration_uses_one_config_snapshot_even_when_save_overlaps(self):
        self.put(self.smtp_settings())
        register = auth.register

        def overlapping_save(*args, **kwargs):
            self.put({"enabled": False})
            return register(*args, **kwargs)

        with mock.patch.object(auth, "register", side_effect=overlapping_save), \
                mock.patch.object(reminders, "send_message", return_value="sent") as send:
            response, result = self.client().register("overlap@example.com")
        self.assertEqual(response.status, 201)
        self.assertTrue(result["verification_required"])
        self.assertEqual(send.call_args[0][0]["host"], "smtp.saved.example")
        self.assertEqual(self.client().login("overlap@example.com")[0].status, 200)

    def test_socket_timeout_is_bounded_and_does_not_report_success(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        self.addCleanup(listener.close)
        # The local port accepts TCP but never sends an SMTP greeting.
        self.put({"host": "127.0.0.1", "port": listener.getsockname()[1]})
        started = time.monotonic()
        with mock.patch.object(reminders, "SMTP_TIMEOUT", 0.15):
            response, result = self.admin.request("POST", TEST, {})
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(response.status, 502)
        self.assertEqual(result["error"]["code"], "mail_delivery_failed")

    def test_next_auth_requests_and_login_gate_use_saved_configuration(self):
        self.conn.execute("UPDATE users SET email_verified_at = NULL WHERE id = 2")
        self.assertEqual(self.client().login(MEMBER)[0].status, 200)
        self.put(self.smtp_settings())
        self.assertEqual(self.client().login(MEMBER)[0].status, 403)
        with mock.patch.object(reminders, "send_message", return_value="sent") as send:
            self.assertEqual(self.client().request("POST", "/api/resend-verification", {"email": MEMBER})[0].status, 200)
            cfg = send.call_args[0][0]
            self.assertEqual(cfg["host"], "smtp.saved.example")
            self.assertTrue(cfg["password"] == self.old_password)
            self.put({"host": "smtp.updated.example", "password": self.new_password})
            self.assertEqual(self.client().request("POST", "/api/forgot-password", {"email": MEMBER})[0].status, 200)
            cfg = send.call_args[0][0]
            self.assertEqual(cfg["host"], "smtp.updated.example")
            self.assertTrue(cfg["password"] == self.new_password)
            response, result = self.client().register("new@example.com")
            self.assertEqual(response.status, 201)
            self.assertTrue(result["verification_required"])
            self.assertFalse(result["user"]["is_admin"])
            self.assertEqual(send.call_args[0][0]["host"], "smtp.updated.example")
        self.put({"enabled": False})
        self.assertEqual(self.client().login(MEMBER)[0].status, 200)
        with mock.patch.object(reminders.smtplib, "SMTP_SSL") as transport, \
                self.assertLogs("easylife.auth_mail", "WARNING"):
            client = self.client()
            response, result = client.register("disabled@example.com")
            self.assertEqual(response.status, 201)
            self.assertIn("csrf_token", result)
            transport.assert_not_called()
        self.put({"enabled": True})
        self.assertEqual(self.client().login(MEMBER)[0].status, 403)

    def test_running_production_thread_uses_new_config_on_next_round(self):
        first_round, accepted = threading.Event(), threading.Event()
        seen = []

        def sender(cfg, message):
            seen.append(dict(cfg))
            if cfg["host"]:
                accepted.set()
                return "sent"
            first_round.set()
            return "logged"

        todo_id = self.pending_todo()
        self.server.start_reminders(sender=sender)
        self.assertTrue(first_round.wait(4), "running thread did not scan disabled settings")
        self.assertEqual(self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0], 0)
        self.put(self.smtp_settings())
        self.assertTrue(accepted.wait(4), "running thread did not pick up committed settings")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0]:
                break
            time.sleep(0.01)
        self.assertEqual(self.conn.execute("SELECT reminder_sent FROM todos WHERE id = ?", (todo_id,)).fetchone()[0], 1)
        self.assertEqual(seen[-1]["host"], "smtp.saved.example")
        self.assertTrue(seen[-1]["password"] == self.old_password)
        self.assertEqual([entry["host"] for entry in seen if entry["host"]], ["smtp.saved.example"])

    def test_saved_config_survives_fresh_process_restart_and_secret_rotation(self):
        before = self.put(self.smtp_settings())
        self.stop_server()
        # Compare inside a fresh interpreter. stdout contains no config/credentials.
        script = """
import json, sys
from easylife import app, db, mail_settings
cfg = app.load_config({'EASYLIFE_PORT': '0', 'EASYLIFE_DB': sys.argv[1],
                       'EASYLIFE_SECRET': 'rotated-process-test-secret', 'EASYLIFE_ADMIN_USER_IDS': '1'})
server = app.make_server(cfg)
conn = db.connect(cfg['db'])
saved = json.loads(conn.execute("SELECT value FROM site_settings WHERE key='mail.smtp'").fetchone()[0])
effective = mail_settings.resolve_mail_config(conn, cfg)
assert effective['password'] == saved['password']
assert effective['host'] == 'smtp.saved.example'
assert effective['enabled'] and server.admin_user_ids == frozenset({1})
assert conn.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
conn.close()
server.server_close()
print('fresh_process_persistence=PASS')
"""
        completed = subprocess.run([sys.executable, "-c", script, self.path], capture_output=True,
                                   text=True, timeout=10)
        self.no_secrets(completed.stdout + completed.stderr)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "fresh_process_persistence=PASS")
        self.start_server()
        self.assertEqual(self.get(), before)


class MailMigrationTests(unittest.TestCase):
    def test_versions_1_2_3_upgrade_idempotently_without_losing_existing_records(self):
        for version in (1, 2, 3):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "old.db")
                conn = db.connect(path)
                try:
                    for statements in db.MIGRATIONS[:version]:
                        for statement in statements:
                            statement(conn) if callable(statement) else conn.execute(statement)
                    conn.execute("PRAGMA user_version = %d" % version)
                    conn.execute("INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                                 (ADMIN, "legacy-password-hash", db.now_utc()))
                    conn.execute("INSERT INTO sessions VALUES ('fixture-session', 1, ?)", (db.now_utc(3600),))
                    # Raw SQL: the current todos module expects columns added by later migrations.
                    todo_id = conn.execute(
                        "INSERT INTO todos (user_id, title, details, created_at, updated_at) "
                        "VALUES (1, '保留旧任务', '原内容', ?, ?)", (db.now_utc(), db.now_utc())).lastrowid
                    todos.add_note(conn, 1, todo_id, {"content": "保留旧笔记"})
                    if version >= 2:
                        conn.execute("UPDATE users SET calendar_token = 'fixture-calendar'")
                    if version == 3:
                        auth.issue_token(conn, 1, "verify", auth.VERIFY_TOKEN_TTL)
                    tables = [row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'")]
                    before = {table: [dict(row) for row in conn.execute("SELECT * FROM " + table)] for table in tables}
                    db.init_db(path)
                    db.init_db(path)
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_settings").fetchone()[0], 0)
                    for table, records in before.items():
                        after = [dict(row) for row in conn.execute("SELECT * FROM " + table)]
                        self.assertEqual(len(after), len(records))
                        for old, new in zip(records, after):
                            self.assertEqual({key: new[key] for key in old}, old)
                    mail_settings.save(conn, {"smtp": {}}, {"host": "saved.example"}, 1)
                    saved = dict(conn.execute("SELECT * FROM site_settings").fetchone())
                    # Replaying CREATE IF NOT EXISTS preserves already-saved overrides.
                    conn.execute("PRAGMA user_version = 3")
                    db.init_db(path)
                    self.assertEqual(dict(conn.execute("SELECT * FROM site_settings").fetchone()), saved)
                finally:
                    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
