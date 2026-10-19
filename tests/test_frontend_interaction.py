"""前端交互回归测试：列表 → 详情 → 删除确认 → 回到列表。

用户反馈的缺陷：从列表进入详情并确认删除后，页面仍停留在显示已删除任务的
详情页——旧的 deleteTodo 只刷新了隐藏的列表（因为进入详情时 hash 没变，
`location.hash === "#/todos"` 成立），没有真正切换视图；同时详情状态残留
导致“保存 / 新增笔记”按钮静默失效。

这里用两种互补的方式验证：
1. `FrontendRoutingSourceTests`（不依赖 Node）对 app.js 源码做结构性断言；
2. `FrontendInteractionRegressionTests` 用一份最小 DOM 桩真正加载并驱动
   真实的 static/app.js（tests/frontend_interaction.js），模拟真实点击流。
"""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "easylife", "static")
APP_JS = os.path.join(STATIC_DIR, "app.js")
HARNESS = os.path.join(ROOT, "tests", "frontend_interaction.js")
NODE = shutil.which("node")

STALE_MESSAGE = 'showToast("任务不存在或已被删除，请返回列表重新打开", "error")'


def read_app_js():
    with open(APP_JS, encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name):
    """返回 `function name(...) { ... }` 的函数体（按大括号配平截取）。"""
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    raise AssertionError("unbalanced braces in " + name)


class FrontendRoutingSourceTests(unittest.TestCase):
    """源码级断言：不需要 Node 也能守住这次修复。"""

    def setUp(self):
        self.js = read_app_js()

    def test_list_title_routes_through_the_hash(self):
        """点击列表标题必须改写 hash，否则详情/列表会共用 #/todos。"""
        self.assertIn('go("#/todos/" + todo.id)', self.js)
        self.assertNotIn('title.addEventListener("click", function () { openDetail(todo.id); });', self.js)

    def test_delete_always_clears_detail_and_returns_to_list(self):
        body = function_body(self.js, "deleteTodo")
        self.assertIn("state.current = null;", body)
        self.assertIn('showView("list")', body)
        self.assertIn('go("#/todos")', body)
        # 旧实现只在 hash 未指向列表时才切换视图，正是它导致页面停在详情页。
        self.assertNotIn('if (location.hash === "#/todos")', body)

    def test_delete_returning_404_still_returns_to_list(self):
        """任务在别处被删除（DELETE -> 404）时也不能停在死详情页。"""
        body = function_body(self.js, "deleteTodo")
        self.assertRegex(body, r"error\.status === 404[\s\S]{0,400}?state\.current = null;",
                         "404 分支应清空详情状态")
        self.assertRegex(body, r"error\.status === 404[\s\S]{0,400}?go\(\"#/todos\"\)",
                         "404 分支应切回列表")

    def test_detail_handlers_warn_instead_of_failing_silently(self):
        """`state.current` 为空时必须有提示，不能悄悄 return。"""
        # submitDetail / addNote / downloadIcs 各一处，详情页删除按钮的兜底再来一处。
        self.assertGreaterEqual(self.js.count(STALE_MESSAGE), 3)
        for name in ("submitDetail", "addNote", "downloadIcs"):
            body = function_body(self.js, name)
            self.assertRegex(body, r"if \(!todo\) \{\s*showToast\([\s\S]{0,80}?return;",
                             name + " 在 state.current 为空时应给出提示")


class AdminMailFrontendSourceTests(unittest.TestCase):
    """管理员邮件设置的前端源码约束（不需要 Node，也不打开发请求）。"""

    def setUp(self):
        self.js = read_app_js()
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
            self.html = handle.read()

    def test_entry_is_gated_on_server_side_is_admin(self):
        self.assertIn("state.user.is_admin === true", self.js)
        self.assertRegex(self.html, r'id="mail-settings"[^>]* hidden')
        self.assertRegex(self.html, r'id="nav-mail-settings"[^>]* hidden')

    def test_password_is_never_refilled_or_persisted(self):
        # 密码输入框只在读配置时被赋值为空，服务端响应的任何字段都不会写进去。
        self.assertIn('$("#mail-password").value = "";', self.js)
        self.assertNotIn("sessionStorage", self.js)
        for value in re.findall(r'localStorage\.setItem\(([^;]+);', self.js):
            # Only the theme and the selected task list (an id) are kept in the browser.
            self.assertRegex(value, r'^"easylife-(theme", value\)|list", String\(state\.listFilter\)\))$')
        self.assertIn('autocomplete="new-password"', self.html)

    def test_test_endpoint_only_sends_recipient(self):
        body = function_body(self.js, "testMailSettings")
        self.assertIn('"./api/admin/mail-settings/test"', body)
        self.assertIn("{ to: to }", body)
        for field in ("enabled:", "host:", "port:", "user:", "from:", "password:"):
            self.assertNotIn(field, body)
        self.assertIn("请先", body)

    def test_presets_only_fill_the_form(self):
        preset = function_body(self.js, "applyMailPreset")
        self.assertIn('$("#mail-host").value = host;', preset)
        self.assertIn('$("#mail-port").value = String(port);', preset)
        self.assertNotIn("api(", preset)
        self.assertNotIn("setItem", preset)


@unittest.skipUnless(NODE, "需要 node 才能运行前端交互回归验证")
class FrontendInteractionRegressionTests(unittest.TestCase):
    """真正加载 static/app.js 并模拟“列表 → 详情 → 确认删除 → 返回列表”。"""

    maxDiff = None

    def test_list_detail_delete_returns_to_list(self):
        self.assertTrue(os.path.isfile(HARNESS), HARNESS)
        proc = subprocess.run([NODE, HARNESS], cwd=ROOT, capture_output=True, text=True, timeout=120)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("ALL CHECKS PASSED", output)
        for name in ("确认删除后回到列表视图", "进入详情后 hash 变为 #/todos/1",
                     "新增笔记后时间线出现条目", "详情保存已发出 PUT 请求",
                     "删除返回 404 时也回到列表视图", "删除返回 404 时给出提示",
                     "迟到的详情成功响应不覆盖忘记密码页", "迟到的详情404响应不覆盖重置密码页",
                     "所有 API 请求都使用相对路径"):
            self.assertIn("ok   " + name, output)


    def test_email_flows_at_todos_prefix(self):
        self.run_scenario("email-auth", "/todos/")

    def test_no_smtp_registration_keeps_session(self):
        self.run_scenario("no-smtp", "/todos/")

    def test_theme_filters_keyboard_and_calendar(self):
        for base_path in ("/", "/todos/"):
            with self.subTest(base_path=base_path):
                self.run_scenario("ui", base_path)

    def test_admin_mail_settings_flow(self):
        for base_path in ("/", "/todos/"):
            with self.subTest(base_path=base_path):
                self.run_scenario("admin-mail", base_path)

    def test_admin_mail_delays_and_lost_responses(self):
        for base_path in ("/", "/todos/"):
            with self.subTest(base_path=base_path):
                self.run_scenario("admin-mail-races", base_path)

    def test_original_delete_flow_at_todos_prefix(self):
        self.run_scenario("regression", "/todos/")

    def test_javascript_syntax(self):
        proc = subprocess.run([NODE, "--check", APP_JS], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def run_scenario(self, scenario, base_path):
        proc = subprocess.run([NODE, HARNESS, scenario, base_path], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALL CHECKS PASSED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
