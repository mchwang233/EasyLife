"""邮箱认证回归：临时 SQLite / HTTP 服务，SMTP 发信函数使用桩替换。"""

import concurrent.futures
import datetime
import hashlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from easylife import app, auth, db, reminders
from tests.test_api import Client, PASSWORD

PUBLIC = "https://mch.wang/todos"
EMAIL = "alice@example.com"
NEW_PASSWORD = "new-correct-horse-battery"
ENDPOINTS = ("verify-email", "resend-verification", "forgot-password", "reset-password")


class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="easylife-email-auth-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "auth.db")
        self.messages = []
        self.real_sender = reminders.send_message
        self.sender = mock.patch.object(reminders, "send_message", side_effect=self.record_mail).start()
        self.addCleanup(mock.patch.stopall)
        cfg = app.load_config({
            "EASYLIFE_PORT": "0", "EASYLIFE_DB": self.path,
            "EASYLIFE_SECRET": "email-auth-test-secret-0123456789",
            "EASYLIFE_BASE_PATH": "/todos", "PUBLIC_URL": "https://legacy.example/todos",
            "EASYLIFE_PUBLIC_URL": PUBLIC, "SMTP_HOST": "smtp.example.com",
            "SMTP_USER": "sender@example.com", "SMTP_FROM": "EasyLife <sender@example.com>",
        })
        self.server = app.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.conn.close()
        db.close_thread_conns()

    def record_mail(self, cfg, message):
        self.messages.append(message)
        self.assertEqual(cfg["host"], reminders.normalize_config(self.server.cfg["smtp"])["host"])
        return "sent" if cfg["host"] else "logged"

    def client(self):
        return Client(self.port)

    def post(self, endpoint, payload, client=None, **kwargs):
        return (client or self.client()).request("POST", "/todos/api/" + endpoint, payload, **kwargs)

    def register(self, email=EMAIL):
        client = self.client()
        response, body = self.post("register", {"email": email, "password": PASSWORD}, client)
        self.assertEqual(response.status, 201, body)
        return client, body

    def mail_token(self, index=-1):
        content = self.messages[index].get_content()
        link = re.search(r"https?://\S+", content).group(0)
        fragment = urllib.parse.urlsplit(link).fragment
        return urllib.parse.parse_qs(fragment.split("?", 1)[1])["token"][0]

    def verified_client(self):
        client, _ = self.register()
        response, body = self.post("verify-email", {"token": self.mail_token()})
        self.assertEqual(response.status, 200, body)
        self.assertEqual(client.login(EMAIL)[0].status, 200)
        return client

    def reset_token(self):
        response, body = self.post("forgot-password", {"email": EMAIL})
        self.assertEqual(response.status, 200, body)
        return self.mail_token()

    def test_registration_blocks_login_until_verified_and_me_exposes_status(self):
        client, body = self.register()
        self.assertFalse(body["user"]["email_verified"])
        self.assertTrue(body["verification_required"])
        self.assertIsNone(client.cookie)
        self.assertNotIn("csrf_token", body)
        self.assertEqual(client.request("GET", "/api/me")[0].status, 401)
        response, body = client.login(EMAIL)
        self.assertEqual(response.status, 403)
        self.assertEqual(body["error"]["code"], "email_not_verified")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(client.login(EMAIL, "wrong-password")[0].status, 401)
        self.assertEqual(self.post("verify-email", {"token": self.mail_token()})[0].status, 200)
        response, body = client.login(EMAIL)
        self.assertEqual(response.status, 200)
        self.assertTrue(body["user"]["email_verified"])
        self.assertIn("Path=/todos", response.getheader("Set-Cookie"))
        self.assertTrue(client.request("GET", "/todos/api/me")[1]["user"]["email_verified"])

    def test_verification_token_expires_in_24_hours_and_is_single_use(self):
        self.register()
        token = self.mail_token()
        row = self.conn.execute("SELECT * FROM auth_tokens").fetchone()
        self.assertEqual(self.conn.execute("SELECT strftime('%s', expires_at) - "
                                          "strftime('%s', created_at) FROM auth_tokens").fetchone()[0],
                         24 * 3600)
        self.assertEqual(row["purpose"], "verify")
        self.assertEqual(self.post("verify-email", {"token": token})[0].status, 200)
        response, body = self.post("verify-email", {"token": token})
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"]["code"], "invalid_token")
        self.assertIsNotNone(self.conn.execute("SELECT used_at FROM auth_tokens").fetchone()[0])

    def test_tokens_are_rejected_at_exact_expiration_without_being_consumed(self):
        self.register()
        tokens = {"verify": self.mail_token(), "reset": self.reset_token()}
        for purpose, token in tokens.items():
            with self.subTest(purpose=purpose):
                expiry = self.conn.execute("SELECT expires_at FROM auth_tokens WHERE purpose = ?",
                                           (purpose,)).fetchone()[0]
                endpoint = "verify-email" if purpose == "verify" else "reset-password"
                with mock.patch.object(db, "now_utc", return_value=expiry):
                    response, body = self.post(endpoint, {"token": token, "password": NEW_PASSWORD})
                self.assertEqual(response.status, 400)
                self.assertEqual(body["error"]["code"], "invalid_token")
                self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens WHERE purpose = ?",
                                                    (purpose,)).fetchone()[0])
        user = self.conn.execute("SELECT * FROM users WHERE email = ?", (EMAIL,)).fetchone()
        self.assertIsNone(user["email_verified_at"])
        self.assertTrue(auth.verify_password(PASSWORD, user["password_hash"]))

    def test_well_formed_forged_tokens_are_rejected_without_burning_valid_links(self):
        self.register()
        verify, reset = self.mail_token(), self.reset_token()
        forged = "A" * 43
        self.assertNotIn(forged, (verify, reset))
        for endpoint in ("verify-email", "reset-password"):
            response, body = self.post(endpoint, {"token": forged, "password": NEW_PASSWORD})
            self.assertEqual(response.status, 400)
            self.assertEqual(body["error"]["code"], "invalid_token")
        self.assertEqual(self.post("verify-email", {"token": verify})[0].status, 200)
        self.assertEqual(self.post("reset-password", {"token": reset, "password": NEW_PASSWORD})[0].status, 200)

    def test_expired_forged_missing_and_oversized_verification_tokens_rejected(self):
        self.register()
        token = self.mail_token()
        self.conn.execute("UPDATE auth_tokens SET expires_at = ?", (db.now_utc(-1),))
        for invalid in (token, "forged-token", None, "", "x" * 129, [], {"token": token}):
            with self.subTest(token_type=type(invalid).__name__):
                response, body = self.post("verify-email", {"token": invalid})
                self.assertEqual(response.status, 400)
                self.assertEqual(body["error"]["code"], "invalid_token")
        self.assertIsNone(self.conn.execute("SELECT email_verified_at FROM users").fetchone()[0])

    def test_malformed_unicode_tokens_return_400_without_changing_account(self):
        self.register()
        for endpoint in ("verify-email", "reset-password"):
            for token in ("\ud800" * 43, "\udfff", "\x00" * 43, "中文" * 22):
                with self.subTest(endpoint=endpoint, token=ascii(token)):
                    response, body = self.post(endpoint, {"token": token, "password": NEW_PASSWORD})
                    self.assertEqual(response.status, 400)
                    self.assertEqual(body["error"]["code"], "invalid_token")
        self.assertIsNone(auth.user_by_email(self.conn, EMAIL)["email_verified_at"])
        self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens").fetchone()[0])

    def test_token_lifetime_is_exact_even_when_clock_crosses_a_second(self):
        self.register()
        user = auth.user_by_email(self.conn, EMAIL)
        start = datetime.datetime(2030, 1, 1, 23, 59, 59)
        for purpose, ttl in (("verify", auth.VERIFY_TOKEN_TTL), ("reset", auth.RESET_TOKEN_TTL)):
            reads = []

            def advancing_clock(offset_seconds=0):
                instant = start + datetime.timedelta(seconds=len(reads) + offset_seconds)
                reads.append(instant)
                return instant.strftime(db.TIME_FORMAT)

            with mock.patch.object(db, "now_utc", side_effect=advancing_clock):
                token = auth.issue_token(self.conn, user["id"], purpose, ttl)
            lifetime = self.conn.execute(
                "SELECT strftime('%s', expires_at) - strftime('%s', created_at) "
                "FROM auth_tokens WHERE token_hash = ?", (auth.hash_token(token),)).fetchone()[0]
            self.assertEqual(lifetime, ttl, purpose)

    def test_only_sha256_token_hashes_are_stored_and_never_returned_by_api(self):
        _, body = self.register()
        verify = self.mail_token()
        reset = self.reset_token()
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(auth_tokens)")}
        self.assertEqual(columns, {"id", "user_id", "purpose", "token_hash", "expires_at", "used_at", "created_at"})
        stored = {row[0] for row in self.conn.execute("SELECT token_hash FROM auth_tokens")}
        self.assertEqual(stored, {hashlib.sha256(value.encode()).hexdigest() for value in (verify, reset)})
        dump = "\n".join(self.conn.iterdump())
        for token in (verify, reset):
            self.assertNotIn(token, dump)
            self.assertNotIn(token, json.dumps(body))

    def test_verification_and_reset_links_use_public_url_with_todos_prefix(self):
        self.register()
        self.reset_token()
        for message, route in zip(self.messages, ("verify", "reset")):
            content = message.get_content()
            self.assertIn(PUBLIC + "/#/" + route + "?token=", content)
            self.assertNotIn("legacy.example", content)
            self.assertNotIn("/todos/todos/", content)
            self.assertEqual(message["To"], EMAIL)
            self.assertEqual(message["From"], "EasyLife <sender@example.com>")

    def test_empty_public_url_keeps_legacy_url_fallback_and_todos_prefix(self):
        for setting in ("PUBLIC_URL", "BASE_URL"):
            cfg = app.load_config({setting: "https://mch.wang", "EASYLIFE_BASE_PATH": "/todos"})
            self.server.cfg.update({key: cfg[key] for key in ("public_url", "base_url")})
            email = setting.lower() + "@example.com"
            self.register(email)
            self.assertIn(PUBLIC + "/#/verify?token=", self.messages[-1].get_content())
            self.assertEqual(self.post("forgot-password", {"email": email})[0].status, 200)
            self.assertIn(PUBLIC + "/#/reset?token=", self.messages[-1].get_content())

    def test_forgot_password_same_status_and_body_for_existing_and_unknown_addresses(self):
        self.register()
        before = len(self.messages)
        existing = self.post("forgot-password", {"email": EMAIL})
        unknown = self.post("forgot-password", {"email": "nobody@example.com"})
        self.assertEqual(existing[0].status, 200)
        self.assertEqual(unknown[0].status, existing[0].status)
        self.assertEqual(unknown[1], existing[1])
        self.assertEqual(len(self.messages), before + 1)
        self.assertEqual(existing[0].getheader("Cache-Control"), "no-store")

    def test_forgot_password_smtp_failure_still_returns_identical_200(self):
        self.register()
        self.sender.side_effect = OSError("SMTP offline")
        with self.assertLogs("easylife.auth_mail", "ERROR"):
            existing = self.post("forgot-password", {"email": EMAIL})
        unknown = self.post("forgot-password", {"email": "nobody@example.com"})
        self.assertEqual((existing[0].status, existing[1]), (unknown[0].status, unknown[1]))
        self.assertEqual(existing[0].status, 200)

    def test_forgot_password_mail_header_failure_still_returns_identical_200(self):
        self.register()
        self.server.cfg["smtp"]["FROM"] = "invalid\nmail-header"
        before = len(self.messages)
        with self.assertLogs("easylife.auth_mail", "ERROR"):
            existing = self.post("forgot-password", {"email": EMAIL})
        unknown = self.post("forgot-password", {"email": "nobody@example.com"})
        self.assertEqual((existing[0].status, existing[1]), (unknown[0].status, unknown[1]))
        self.assertEqual(existing[0].status, 200)
        self.assertEqual(len(self.messages), before)

    def test_reset_invalidates_old_password_and_all_sessions_but_other_user_survives(self):
        first = self.verified_client()
        second = self.client()
        self.assertEqual(second.login(EMAIL)[0].status, 200)
        other, _ = self.register("other@example.com")
        self.assertEqual(self.post("verify-email", {"token": self.mail_token()})[0].status, 200)
        self.assertEqual(other.login("other@example.com")[0].status, 200)
        old_cookie, old_csrf = first.cookie, first.csrf
        token = self.reset_token()
        response, _ = self.post("reset-password", {"token": token, "password": NEW_PASSWORD}, first)
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        first.cookie, first.csrf = old_cookie, old_csrf
        for client in (first, second):
            self.assertEqual(client.request("GET", "/api/me")[0].status, 401)
            self.assertEqual(client.request("POST", "/api/todos", {"title": "stale"})[0].status, 401)
        self.assertEqual(other.request("GET", "/api/me")[0].status, 200)
        self.assertEqual(self.client().login(EMAIL, PASSWORD)[0].status, 401)
        self.assertEqual(self.client().login(EMAIL, NEW_PASSWORD)[0].status, 200)
        row = self.conn.execute("SELECT password_hash FROM users WHERE email = ?", (EMAIL,)).fetchone()
        self.assertTrue(row[0].startswith("pbkdf2_sha256$260000$"))
        self.assertTrue(auth.verify_password(NEW_PASSWORD, row[0]))
        self.assertEqual(self.post("reset-password", {"token": token, "password": PASSWORD})[0].status, 400)

    def test_reset_token_expires_after_one_hour(self):
        self.register()
        token = self.reset_token()
        ttl = self.conn.execute("SELECT strftime('%s', expires_at) - strftime('%s', created_at) "
                                "FROM auth_tokens WHERE purpose = 'reset'").fetchone()[0]
        self.assertEqual(ttl, 3600)
        self.conn.execute("UPDATE auth_tokens SET expires_at = ? WHERE purpose = 'reset'", (db.now_utc(-1),))
        for invalid in (token, "forged-reset", None, "", "x" * 129):
            response, body = self.post("reset-password", {"token": invalid, "password": NEW_PASSWORD})
            self.assertEqual(response.status, 400)
            self.assertEqual(body["error"]["code"], "invalid_token")

    def test_reset_invalidates_sessions_supplied_through_x_auth_token(self):
        client = self.verified_client()
        headers = {"X-Auth-Token": client.cookie.split("=", 1)[1], "X-CSRF-Token": client.csrf}
        self.assertEqual(self.client().request("GET", "/todos/api/me", headers=headers)[0].status, 200)
        token = self.reset_token()
        self.assertEqual(self.post("reset-password", {"token": token, "password": NEW_PASSWORD})[0].status, 200)
        self.assertEqual(self.client().request("GET", "/todos/api/me", headers=headers)[0].status, 401)
        response, _ = self.client().request("POST", "/todos/api/todos", {"title": "stale"}, headers=headers)
        self.assertEqual(response.status, 401)

    def test_reset_with_expired_session_still_allows_token_based_recovery(self):
        client = self.verified_client()
        token = self.reset_token()
        self.conn.execute("UPDATE sessions SET expires_at = ?", (db.now_utc(-1),))
        response, _ = self.post("reset-password", {"token": token, "password": NEW_PASSWORD}, client)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.client().login(EMAIL, NEW_PASSWORD)[0].status, 200)

    def test_tokens_cannot_be_used_for_the_wrong_purpose(self):
        self.register()
        verify = self.mail_token()
        reset = self.reset_token()
        self.assertEqual(self.post("reset-password", {"token": verify, "password": NEW_PASSWORD})[0].status, 400)
        self.assertEqual(self.post("verify-email", {"token": reset})[0].status, 400)
        self.assertEqual(self.post("verify-email", {"token": verify})[0].status, 200)
        self.assertEqual(self.post("reset-password", {"token": reset, "password": NEW_PASSWORD})[0].status, 200)

    def test_invalid_new_password_does_not_consume_token(self):
        self.register()
        token = self.reset_token()
        for password in (None, "1234567", "x" * 129, []):
            self.assertEqual(self.post("reset-password", {"token": token, "password": password})[0].status, 400)
        self.assertEqual(self.post("reset-password", {"token": token, "password": NEW_PASSWORD})[0].status, 200)
        # A reset also proves mailbox ownership for an initially unverified user.
        self.assertTrue(self.client().login(EMAIL, NEW_PASSWORD)[1]["user"]["email_verified"])

    def test_invalid_unicode_password_is_rejected_but_valid_unicode_still_works(self):
        invalid = "password-\ud800"
        response, body = self.post("register", {"email": EMAIL, "password": invalid})
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertIsNone(auth.user_by_email(self.conn, EMAIL))
        self.register()
        token = self.reset_token()
        response, body = self.post("reset-password", {"token": token, "password": invalid})
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens WHERE purpose = 'reset'").fetchone()[0])
        password = "新的密码仍支持汉字和🙂"
        self.assertEqual(self.post("reset-password", {"token": token, "password": password})[0].status, 200)
        self.assertEqual(self.client().login(EMAIL, password)[0].status, 200)

    def test_resend_rate_limit_includes_registration_and_normalizes_email(self):
        with mock.patch.object(auth.time, "monotonic", return_value=1000) as clock:
            self.register()
            old = self.mail_token()
            response, _ = self.post("resend-verification", {"email": " ALICE@Example.com "})
            self.assertEqual(response.status, 429)
            self.assertEqual(response.getheader("Retry-After"), "60")
            clock.return_value = 1059.5
            response, _ = self.post("resend-verification", {"email": EMAIL})
            self.assertEqual(response.status, 429)
            self.assertEqual(response.getheader("Retry-After"), "1")
            clock.return_value = 1060
            self.assertEqual(self.post("resend-verification", {"email": EMAIL})[0].status, 200)
            new = self.mail_token()
            self.assertNotEqual(new, old)
            self.assertEqual(self.post("verify-email", {"token": old})[0].status, 400)
            self.assertEqual(self.post("verify-email", {"token": new})[0].status, 200)

    def test_forgot_password_rate_limit_same_for_existing_and_unknown(self):
        self.register()
        with mock.patch.object(auth.time, "monotonic", return_value=1000) as clock:
            for email in (EMAIL, "missing@example.com"):
                self.assertEqual(self.post("forgot-password", {"email": email})[0].status, 200)
            first = self.post("forgot-password", {"email": EMAIL.upper()})
            second = self.post("forgot-password", {"email": "missing@example.com"})
            self.assertEqual((first[0].status, first[1]), (second[0].status, second[1]))
            self.assertEqual(first[0].status, 429)
            self.assertEqual(first[0].getheader("Retry-After"), "60")
            clock.return_value = 1060
            for email in (EMAIL, "missing@example.com"):
                self.assertEqual(self.post("forgot-password", {"email": email})[0].status, 200)

    def test_resend_accepts_session_without_email_and_enforces_csrf(self):
        self.server.cfg["require_email_verification"] = False
        with mock.patch.object(auth.time, "monotonic", return_value=1000) as clock:
            client, _ = self.register()
            clock.return_value = 1060
            self.assertEqual(self.post("resend-verification", {}, client, csrf=False)[0].status, 403)
            self.assertEqual(self.post("resend-verification", {}, client)[0].status, 200)
            self.assertEqual(self.messages[-1]["To"], EMAIL)

    def test_new_endpoints_require_session_csrf_and_allow_anonymous_recovery(self):
        client = self.verified_client()
        token = self.reset_token()
        for endpoint in ENDPOINTS:
            payload = {"email": EMAIL, "token": token, "password": NEW_PASSWORD}
            for headers in ({}, {"X-CSRF-Token": "forged"}):
                response, body = self.post(endpoint, payload, client, csrf=False, headers=headers)
                self.assertEqual(response.status, 403, endpoint)
                self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(self.post("reset-password", {"token": token, "password": NEW_PASSWORD})[0].status, 200)

    def test_new_endpoints_reuse_body_limits_and_json_validation(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.post(endpoint, ["not", "object"])[0].status, 400)
                self.assertEqual(self.post(endpoint, {"padding": "x" * app.MAX_BODY})[0].status, 413)
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                connection.request("POST", "/api/" + endpoint, b"{}", {"Content-Type": "text/plain"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                connection.close()
                self.assertEqual(self.client().request("GET", "/api/" + endpoint)[0].status, 405)

    def test_email_validation_rejects_bad_addresses_and_does_not_send(self):
        for email in (None, "invalid", "a@@b.com", "a@b", "a b@example.com", "a\r\nBcc:x@y.com",
                      ".a@example.com", "a..b@example.com", "a.@example.com", "a@-example.com",
                      "a" * 65 + "@example.com", "x" * 255, []):
            for endpoint in ("register", "forgot-password", "resend-verification"):
                with self.subTest(email=email, endpoint=endpoint):
                    # Separate IPs avoid the existing registration cap masking validation.
                    response, _ = self.post(endpoint, {"email": email, "password": PASSWORD},
                                            headers={"X-Real-IP": str(email)[:1] + str(len(str(email)))})
                    self.assertEqual(response.status, 400)
        self.assertEqual(self.messages, [])

    def test_no_smtp_logs_mail_warning_and_does_not_block_login(self):
        self.server.cfg["smtp"] = {"HOST": "   "}
        with self.assertLogs("easylife.auth_mail", "WARNING") as logs:
            client, body = self.register()
        self.assertFalse(body["user"]["email_verified"])
        self.assertIsNotNone(client.cookie)
        self.assertIn("SMTP disabled", "\n".join(logs.output))
        self.assertIn(self.mail_token(), "\n".join(logs.output))
        self.assertEqual(self.client().login(EMAIL)[0].status, 200)

    def test_no_smtp_logs_verification_and_reset_with_the_real_shared_sender(self):
        self.server.cfg["smtp"] = {"HOST": ""}
        self.sender.side_effect = self.real_sender
        with mock.patch.object(reminders.smtplib, "SMTP") as smtp, \
                mock.patch.object(reminders.smtplib, "SMTP_SSL") as smtp_ssl, \
                mock.patch.object(auth.time, "monotonic", return_value=1000) as clock, \
                self.assertLogs("easylife.auth_mail", "WARNING") as logs:
            client, body = self.register()
            self.assertFalse(body["user"]["email_verified"])
            self.assertEqual(self.post("forgot-password", {"email": EMAIL})[0].status, 200)
            clock.return_value = 1060
            self.assertEqual(self.post("resend-verification", {}, client)[0].status, 200)
            self.assertEqual(self.client().login(EMAIL)[0].status, 200)
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
        self.assertEqual(len(logs.records), 3)
        self.assertTrue(all(record.levelname == "WARNING" for record in logs.records))
        self.assertIn(PUBLIC + "/#/verify?token=", "\n".join(logs.output))
        self.assertIn(PUBLIC + "/#/reset?token=", "\n".join(logs.output))

    def test_https_in_either_public_url_keeps_session_cookies_secure(self):
        self.server.cfg["require_email_verification"] = False
        local_url = "http://127.0.0.1:8086/todos"
        for index, (base_url, public_url) in enumerate(((local_url, PUBLIC), (PUBLIC, local_url))):
            with self.subTest(base_url=base_url, public_url=public_url):
                self.server.cfg.update(base_url=base_url, public_url=public_url)
                client = self.client()
                email = "cookie%d@example.com" % index
                for endpoint, payload in (("register", {"email": email, "password": PASSWORD}),
                                          ("login", {"email": email, "password": PASSWORD}),
                                          ("logout", {})):
                    response, body = self.post(endpoint, payload, client)
                    self.assertEqual(response.status, 201 if endpoint == "register" else 200, body)
                    cookie = response.getheader("Set-Cookie")
                    self.assertIn("; Secure", cookie, endpoint)
                    self.assertIn("HttpOnly; SameSite=Lax; Path=/todos", cookie)

    def test_configured_smtp_failure_does_not_bypass_verification(self):
        self.sender.side_effect = OSError("SMTP unavailable")
        with self.assertLogs("easylife.auth_mail", "ERROR"):
            client, body = self.register()
        self.assertTrue(body["verification_required"])
        self.assertIn("暂未发送", body["message"])
        self.assertEqual(client.login(EMAIL)[0].status, 403)

    def test_explicit_verification_opt_out_still_sends_registration_mail(self):
        self.server.cfg["require_email_verification"] = False
        client, body = self.register()
        self.assertIsNotNone(client.cookie)
        self.assertFalse(body["user"]["email_verified"])
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.client().login(EMAIL)[0].status, 200)

    def test_reset_transaction_failure_keeps_token_password_and_sessions_intact(self):
        client = self.verified_client()
        token = self.reset_token()
        self.conn.execute("CREATE TRIGGER reject_reset BEFORE UPDATE OF password_hash ON users "
                          "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            auth.reset_password(self.conn, token, NEW_PASSWORD)
        self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens WHERE purpose = 'reset'").fetchone()[0])
        self.assertEqual(client.request("GET", "/api/me")[0].status, 200)
        self.assertEqual(self.client().login(EMAIL)[0].status, 200)
        self.conn.execute("DROP TRIGGER reject_reset")
        self.assertEqual(self.post("reset-password", {"token": token, "password": NEW_PASSWORD})[0].status, 200)

    def test_verify_transaction_failure_keeps_token_usable(self):
        self.register()
        token = self.mail_token()
        self.conn.execute("CREATE TRIGGER reject_verify BEFORE UPDATE OF email_verified_at ON users "
                          "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            auth.verify_email(self.conn, token)
        self.assertIsNone(self.conn.execute("SELECT used_at FROM auth_tokens").fetchone()[0])
        self.conn.execute("DROP TRIGGER reject_verify")
        self.assertEqual(self.post("verify-email", {"token": token})[0].status, 200)

    def test_concurrent_token_redemption_only_one_succeeds(self):
        self.register()
        for purpose, token in (("verify", self.mail_token()), ("reset", self.reset_token())):
            barrier = threading.Barrier(2)

            def redeem():
                conn = db.connect(self.path)
                try:
                    barrier.wait(timeout=5)
                    if purpose == "verify":
                        auth.verify_email(conn, token)
                    else:
                        auth.reset_password(conn, token, NEW_PASSWORD)
                    return "ok"
                except auth.InvalidToken:
                    return "invalid"
                finally:
                    conn.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(redeem) for _ in range(2)]
                self.assertCountEqual([f.result(timeout=10) for f in futures], ["ok", "invalid"])

    def test_login_racing_reset_cannot_create_a_session_with_old_password(self):
        self.verified_client()
        token = self.reset_token()
        verify_password = auth.verify_password

        def reset_after_password_check(password, stored):
            result = verify_password(password, stored)
            auth.reset_password(self.conn, token, NEW_PASSWORD)
            return result

        with mock.patch.object(auth, "verify_password", side_effect=reset_after_password_check):
            self.assertEqual(self.client().login(EMAIL)[0].status, 401)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_registration_racing_reset_cannot_create_a_session_with_old_password(self):
        self.server.cfg["require_email_verification"] = False
        create_session = auth.create_session

        def reset_before_session(conn, user_id, *args, **kwargs):
            token = auth.issue_token(self.conn, user_id, "reset", auth.RESET_TOKEN_TTL)
            auth.reset_password(self.conn, token, NEW_PASSWORD)
            return create_session(conn, user_id, *args, **kwargs)

        with mock.patch.object(auth, "create_session", side_effect=reset_before_session):
            response, _ = self.post("register", {"email": EMAIL, "password": PASSWORD})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.client().login(EMAIL, PASSWORD)[0].status, 401)
        self.assertEqual(self.client().login(EMAIL, NEW_PASSWORD)[0].status, 200)


class EmailAuthConfigAndMigrationTests(unittest.TestCase):
    def test_new_config_defaults_and_public_url_normalization(self):
        cfg = app.load_config({})
        self.assertEqual(cfg["public_url"], "")
        self.assertTrue(cfg["require_email_verification"])
        for value in (PUBLIC, PUBLIC + "/", "https://mch.wang"):
            self.assertEqual(app.load_config({"EASYLIFE_PUBLIC_URL": value,
                                             "EASYLIFE_BASE_PATH": "/todos"})["public_url"], PUBLIC)
        self.assertFalse(app.load_config({"EASYLIFE_REQUIRE_EMAIL_VERIFICATION": "false"})["require_email_verification"])
        with self.assertRaises(SystemExit):
            app.load_config({"EASYLIFE_REQUIRE_EMAIL_VERIFICATION": "typo"})

    def test_public_url_rejects_query_fragment_and_non_http_schemes(self):
        for value in ("javascript:alert(1)", "//mch.wang/todos", PUBLIC + "?x=1", PUBLIC + "#a",
                      "https://a:b@mch.wang/todos", "https://mch.wang/to dos"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                app.validate_config(app.load_config({"EASYLIFE_SECRET": "x" * 32,
                                                     "EASYLIFE_PUBLIC_URL": value}))

    def test_old_database_migration_grandfathers_users_and_preserves_data_and_sessions(self):
        for old_version in (0, 1, 2):
            with self.subTest(version=old_version), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "legacy.db")
                conn = db.connect(path)
                for statements in db.MIGRATIONS[:max(1, old_version)]:
                    for statement in statements:
                        statement(conn) if callable(statement) else conn.execute(statement)
                conn.execute("PRAGMA user_version = %d" % old_version)
                conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                             (EMAIL, auth.hash_password(PASSWORD), db.now_utc()))
                conn.execute("INSERT INTO sessions VALUES ('old-session', 1, ?)", (db.now_utc(3600),))
                conn.execute("INSERT INTO todos (user_id, title, created_at, updated_at) VALUES (1, 'keep me', ?, ?)",
                             (db.now_utc(), db.now_utc()))
                conn.close()
                db.init_db(path)
                db.init_db(path)
                conn = db.connect(path)
                try:
                    self.assertTrue(auth.public_user(auth.user_by_id(conn, 1))["email_verified"])
                    self.assertIsNotNone(auth.get_session(conn, "old-session"))
                    self.assertEqual(conn.execute("SELECT title FROM todos").fetchone()[0], "keep me")
                    user, _, _ = auth.login(conn, EMAIL, PASSWORD, ip="test",
                                           limiter=auth.LoginRateLimiter(), require_verification=True)
                    self.assertTrue(user["email_verified"])
                    conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('new@example.com', 'hash', ?)",
                                 (db.now_utc(),))
                    # Replay even with a stale version: never grandfather a new user.
                    conn.execute("PRAGMA user_version = 2")
                    db.init_db(path)
                    self.assertIsNone(auth.user_by_email(conn, "new@example.com")["email_verified_at"])
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
                finally:
                    conn.close()


@unittest.skipUnless(shutil.which("node"), "需要 node 才能运行前端交互测试")
class EmailAuthFrontendTests(unittest.TestCase):
    def test_browser_email_auth_flows(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run([shutil.which("node"), "tests/frontend_interaction.js", "email-auth"],
                                cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("邮箱认证交互完成", result.stdout)
        self.assertIn("ALL CHECKS PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
