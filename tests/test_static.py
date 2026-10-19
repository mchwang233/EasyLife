"""Static frontend tests: GET / serves index.html, assets are offline-only and XSS-safe."""

import http.client
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from easylife import app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "easylife", "static")


class StaticTestCase(unittest.TestCase):
    """A real server on a temporary database, so static routing is exercised for real."""

    BASE_PATH = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-static-")
        cfg = app.load_config({
            "EASYLIFE_PORT": "0",
            "EASYLIFE_BASE_PATH": self.BASE_PATH,
            "EASYLIFE_DB": os.path.join(self.tmp, "test.db"),
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

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, body


class IndexRouteTests(StaticTestCase):
    def test_get_root_returns_200_html(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"), headers)
        self.assertEqual(int(headers["content-length"]), len(body))
        text = body.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", text)
        self.assertIn('<html lang="zh-CN">', text)
        self.assertIn("</html>", text)
        # The shell pulls in the local app JS/CSS only.
        self.assertIn("/static/app.js", text)
        self.assertIn("/static/style.css", text)

    def test_static_assets_are_served(self):
        for path, prefix in [("/static/app.js", "text/javascript"),
                             ("/static/style.css", "text/css"),
                             ("/static/favicon.svg", "image/svg+xml")]:
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(prefix), (path, headers))
            self.assertTrue(body, path)

    def test_unknown_static_paths_are_404(self):
        for path in ["/static/", "/static/missing.js", "/static/app.js.map", "/index.html"]:
            self.assertEqual(self.get(path)[0], 404, path)


class PrefixedStaticRouteTests(StaticTestCase):
    BASE_PATH = "/todos"

    def test_relative_assets_and_mail_landing_pages_under_todos(self):
        status, headers, _ = self.get("/todos")
        self.assertEqual(status, 301)
        self.assertEqual(headers["location"], "/todos/")
        for route in ("#/login", "#/verify?token=example", "#/reset?token=example",
                      "#/forgot-password", "#/check-email"):
            # Fragments are handled by app.js; they are not sent to the server.
            page = "http://localhost/todos/" + route
            status, _, body = self.get(urlsplit(page).path)
            self.assertEqual(status, 200)
            parser = Markup()
            parser.feed(body.decode())
            resources = [attrs.get("src") or attrs.get("href")
                         for tag, attrs in parser.elements if tag in ("script", "link", "img")]
            self.assertTrue(resources)
            for resource in resources:
                self.assertTrue(resource.startswith("./static/"), resource)
                resolved = urlsplit(urljoin(page, resource)).path
                self.assertTrue(resolved.startswith("/todos/static/"), resolved)
                self.assertEqual(self.get(resolved)[0], 200, resolved)


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class FrontendSourceTests(unittest.TestCase):
    """Checks that hold on the files themselves (no server needed)."""

    FILES = ("index.html", "app.js", "style.css")

    def read(self, name):
        with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as fh:
            return fh.read()

    def test_required_files_exist(self):
        for name in self.FILES:
            self.assertTrue(os.path.isfile(os.path.join(STATIC_DIR, name)), name)

    def test_no_external_resources(self):
        """No CDN, no absolute external URL: the app must run offline."""
        external = re.compile(r"(?:https?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
        for name in self.FILES:
            text = self.read(name)
            self.assertEqual(external.findall(text), [], name)
            self.assertNotIn("cdn", text.lower(), name)
            self.assertNotIn("integrity=", text, name)

    def test_js_renders_user_content_with_text_content_only(self):
        js = self.read("app.js")
        for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write",
                          "eval("):
            self.assertNotIn(forbidden, js, forbidden)
        self.assertIn("textContent", js)
        self.assertIn("createElement", js)

    def test_markup_has_no_inline_handlers_or_scripts(self):
        """CSP forbids inline script/style, so the shell must not use them."""
        html = self.read("index.html")
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("onload=", html)

    def test_frontend_endpoints_exist_in_the_server_route_table(self):
        """Every /api path the front-end fetches must be a real server route."""
        js = self.read("app.js")

        def routed(path):
            return any(pattern.match(path) for pattern, _auth, _methods in app.ROUTES)

        # The front-end uses "./api/..." (relative, so it also works under /todos/).
        literals = set("/" + path for path in re.findall(r'"\./(api/[^"]*)"', js))
        self.assertTrue(literals, "app.js should call /api endpoints")
        for base in sorted(literals):
            path = base.rstrip("/") or base
            self.assertTrue(routed(path), base)

        # Composed paths: "./api/todos/" + id and ".../notes" / ".../ics".
        self.assertIn('"./api/todos/"', js)
        for suffix in ('"/notes"', '"/ics"'):
            self.assertIn(suffix, js)
        for path in ("/api/todos/1", "/api/todos/1/notes", "/api/todos/1/ics"):
            self.assertTrue(routed(path), path)

    def test_no_root_absolute_asset_or_api_paths(self):
        """Under EASYLIFE_BASE_PATH=/todos a "/static/..." or "/api/..." URL would hit
        the root site, so the shell and the script must only use relative URLs."""
        absolute = re.compile(r"""["'`(]/(?:api|static)\b""")
        for name in ("index.html", "app.js"):
            self.assertEqual(absolute.findall(self.read(name)), [], name)
        html = self.read("index.html")
        for asset in ("./static/favicon.svg", "./static/style.css", "./static/app.js"):
            self.assertIn('"%s"' % asset, html)

    def test_ui_covers_required_actions(self):
        html = self.read("index.html")
        js = self.read("app.js")
        for label in ("登录", "注册", "登出", "设置", "新建任务", "删除任务",
                      "下载到日历(.ics)", "Google 日历", "标题", "详情", "优先级",
                      "提醒时间", "提前提醒", "重复", "旗标", "紧急", "标签", "子任务", "图片", "笔记", "重置订阅地址"):
            self.assertIn(label, html, label)
        for call in ("/api/register", "/api/login", "/api/logout", "/api/me", "/api/todos",
                     "/notes", "/ics", "/api/calendar/token/reset"):
            self.assertIn(call, js, call)
        self.assertIn("X-CSRF-Token", js)
        self.assertIn("#/login", js)  # 401 responses land on the login view


    def test_email_views_and_accessible_form_fields(self):
        html = self.read("index.html")
        parser = Markup()
        parser.feed(html)
        ids = [attrs["id"] for _, attrs in parser.elements if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)), "HTML ids must be unique")
        for view in ("auth", "check-email", "verify", "forgot", "reset", "list", "detail"):
            self.assertIn("view-" + view, ids)
        for element in ("auth-forgot", "check-email-resend", "verify-message", "verify-retry",
                        "forgot-form", "recovery-email", "reset-form", "recovery-password",
                        "recovery-confirm", "password-requirements", "verification-banner",
                        "theme-select", "account-nav", "reset-request-link"):
            self.assertIn(element, ids)
        labels = {attrs.get("for") for tag, attrs in parser.elements if tag == "label"}
        for tag, attrs in parser.elements:
            if tag in ("input", "textarea", "select"):
                self.assertTrue(attrs.get("id") in labels or attrs.get("aria-label"), attrs)
        forgot = re.search(r'<form id="forgot-form"[\s\S]*?</form>', html).group()
        reset = re.search(r'<form id="reset-form"[\s\S]*?</form>', html).group()
        self.assertIn('type="email"', forgot)
        self.assertNotIn('type="password"', forgot)
        self.assertEqual(reset.count('type="password"'), 2)
        self.assertIn("8–128", reset)
        self.assertIn("请查收验证邮件", html)
        self.assertIn("忘记密码", html)
        self.assertIn('data-status="today"', html)
        self.assertIn('data-status="upcoming"', html)

    def test_mail_endpoint_contract_and_no_blocking_alert(self):
        js = self.read("app.js")
        for endpoint in ("forgot-password", "reset-password", "verify-email", "resend-verification"):
            path = "/api/" + endpoint
            self.assertIn('".' + path + '"', js)
            routes = [methods for pattern, _, methods in app.ROUTES if pattern.fullmatch(path)]
            self.assertEqual(len(routes), 1)
            self.assertIn("POST", routes[0])
        self.assertNotRegex(js, r"\balert\s*\(")
        for name in self.FILES:
            self.assertNotRegex(self.read(name), r"https?://", name)

    def test_theme_responsive_and_accessibility_rules(self):
        css = self.read("style.css")
        for variable in ("--primary", "--radius", "--shadow", "--space-4", "--font"):
            self.assertIn(variable + ":", css)
        for rule in ("prefers-color-scheme: dark", 'data-theme="dark"', 'data-theme="light"',
                     "max-width: 560px", "focus-visible", "prefers-reduced-motion", "forced-colors"):
            self.assertIn(rule, css)
        self.assertRegex(css, r'--font:\s*system-ui,\s*"PingFang SC"')
        self.assertIn("localStorage", self.read("app.js"))

    def test_theme_colors_meet_aa_contrast(self):
        css = self.read("style.css")

        def colors(block):
            return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6})", block))

        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        light = colors(re.search(r":root \{([^}]+)", css).group(1))
        dark = dict(light, **colors(re.search(r':root\[data-theme="dark"\] \{([^}]+)', css).group(1)))
        pairs = [(foreground, background, 4.5) for foreground in ("text", "muted")
                 for background in ("bg", "card", "surface")]
        pairs += [("on-primary", "primary", 4.5), ("on-primary", "primary-hover", 4.5),
                  ("primary", "primary-soft", 4.5), ("danger", "danger-bg", 4.5),
                  ("ok", "ok-bg", 4.5), ("warn", "warn-bg", 4.5),
                  ("input-line", "card", 3), ("focus", "bg", 3), ("focus", "card", 3)]
        for mode, palette in (("light", light), ("dark", dark)):
            for foreground, background, minimum in pairs:
                values = sorted((luminance(palette[foreground]), luminance(palette[background])))
                contrast = (values[1] + .05) / (values[0] + .05)
                self.assertGreaterEqual(contrast, minimum, (mode, foreground, background, contrast))


if __name__ == "__main__":
    unittest.main()
