"""管理员邮件设置的真实浏览器验证（手机 + 桌面），非隐藏交付证据。

运行（仓库根目录）：python3 tests/browser_mail_ui.py

- 启动真实的 EasyLife 服务：临时 SQLite、随机本地端口、EASYLIFE_BASE_PATH=/todos、
  EASYLIFE_ADMIN_USER_IDS=1（管理员）与第二个普通账号。
- 真实本地 mini SMTP（127.0.0.1，含 AUTH PLAIN）验证「SMTP 服务器接受」；静默 TCP
  服务验证 15s 超时；已关闭端口验证连接失败；真实 HTTP 循环验证限流 429。
- Firefox 140 headless + Marionette；<450px 使用等尺寸 iframe 夹具（本地测试代理仅
  剥离 X-Frame-Options，产品安全头不变）。
输出：docs/verification/mobile-mail/mail-ui/（截图 + 结果 JSON + 控制台日志）。
不连接生产服务，不发外部邮件，不部署；证据不保存授权码明文。
"""

import base64
import hashlib
import http.server
import json
import logging
import pathlib
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from easylife import app, auth, db  # noqa: E402

ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
OUT = ROOT / "docs/verification/mobile-mail/mail-ui"
RACE_MODE = "--races" in sys.argv
if RACE_MODE:
    OUT = OUT / "race-fix/browser"
if "--output" in sys.argv:
    OUT = pathlib.Path(sys.argv[sys.argv.index("--output") + 1]).resolve()
TEST_PASSWORD = "local-test-auth-code"

MEASURE = r"""
const rect = s => { const n = document.querySelector(s); if (!n) return null;
 const r = n.getBoundingClientRect(); return Object.fromEntries(
 ['top','left','width','height','bottom'].map(k => [k, Math.round(r[k]*100)/100])); };
const visible = n => !!(n.getBoundingClientRect().width && n.getBoundingClientRect().height);
const card = document.querySelector('#mail-settings');
return {
 innerWidth, innerHeight, clientWidth: document.documentElement.clientWidth,
 scrollWidth: document.documentElement.scrollWidth,
 mailSectionVisible: !!(card && !card.hidden),
 formHidden: document.querySelector('#mail-form').hidden,
 mailForm: rect('#mail-form'), mailCard: rect('#mail-settings'),
 mailSave: rect('#mail-save'), mailTest: rect('#mail-test'), mailStatus: rect('#mail-status'),
 formGridColumns: getComputedStyle(document.querySelector('#mail-form .grid')).gridTemplateColumns,
 statusState: document.querySelector('#mail-status').dataset.state,
 statusText: document.querySelector('#mail-status').textContent,
 source: document.querySelector('#mail-source').textContent,
 passwordState: document.querySelector('#mail-password-state').textContent,
 passwordFilled: document.querySelector('#mail-password').value !== '',
 passwordLength: document.querySelector('#mail-password').value.length,
 hostValue: document.querySelector('#mail-host').value,
 portValue: document.querySelector('#mail-port').value,
 enabledChecked: document.querySelector('#mail-enabled').checked,
 shortTargets: [...document.querySelectorAll('#mail-settings button, #mail-settings a[href], #mail-settings input')]
  .filter(visible).filter(n => { const r = n.getBoundingClientRect();
    return r.width < 43.9 || r.height < 43.9; })
  .map(n => ({ id: n.id || n.className, width: Math.round(n.getBoundingClientRect().width * 10) / 10,
               height: Math.round(n.getBoundingClientRect().height * 10) / 10 })),
 calendarVisible: !document.querySelector('#settings').hidden,
 mailNavVisible: !document.querySelector('#nav-mail-settings').hidden,
};
"""


class Marionette:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.sock.settimeout(45)
        self.buf, self.next_id = b"", 0
        self.recv()

    def recv(self):
        while b":" not in self.buf:
            self.buf += self.sock.recv(65536)
        idx = self.buf.index(b":")
        size = int(self.buf[:idx])
        while len(self.buf) < idx + 1 + size:
            self.buf += self.sock.recv(65536)
        data, self.buf = self.buf[idx + 1:idx + 1 + size], self.buf[idx + 1 + size:]
        return json.loads(data)

    def command(self, name, params=None):
        self.next_id += 1
        data = json.dumps([0, self.next_id, name, params or {}]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        while True:
            msg = self.recv()
            if msg[0] == 1 and msg[1] == self.next_id:
                if msg[2]:
                    raise RuntimeError("%s: %s" % (name, msg[2]))
                result = msg[3]
                return result['value'] if isinstance(result, dict) and list(result) == ['value'] else result

    def js(self, script, *args):
        result = self.command("WebDriver:ExecuteScript", {"script": script, "args": list(args)})
        return result['value'] if isinstance(result, dict) and list(result) == ['value'] else result

    def async_js(self, script):
        result = self.command("WebDriver:ExecuteAsyncScript", {"script": script, "args": []})
        return result['value'] if isinstance(result, dict) and list(result) == ['value'] else result

    def wait(self, expression, tries=150):
        for _ in range(tries):
            if self.js("return !!(" + expression + ");"):
                return
            time.sleep(.1)
        raise AssertionError("Browser wait timed out: " + expression)

    def click(self, selector):
        node = self.command("WebDriver:FindElement", {"using": "css selector", "value": selector})
        self.command("WebDriver:ElementClick", {"id": node[ELEMENT]})

    def key(self, value):
        self.command("WebDriver:PerformActions", {"actions": [{"type": "key", "id": "keyboard",
                     "actions": [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}]}]})

    def frame(self, element):
        self.command("WebDriver:SwitchToFrame", {"id": element})

    def top(self):
        self.command("WebDriver:SwitchToFrame", {"id": None})


class MiniSMTPServer(threading.Thread):
    """本机 ESMTP（含 AUTH PLAIN），记录收到的邮件与登录账号，供真实投递路径使用。"""

    daemon = True

    def __init__(self):
        super().__init__()
        self._messages, self._auths = [], []
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def received(self):
        with self._lock:
            return list(self._messages)

    def auths(self):
        with self._lock:
            return list(self._auths)

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self.sock.close()

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
                        with self._lock:
                            self._messages.append("\n".join(data))
                        data = []
                        stream.write(b"250 OK queued\r\n")
                    else:
                        data.append(line[1:] if line.startswith("..") else line)
                else:
                    verb = line.split(" ", 1)[0].upper()
                    if verb == "EHLO":
                        stream.write(b"250-mini\r\n250-SIZE 10485760\r\n250 AUTH PLAIN LOGIN\r\n")
                    elif verb == "AUTH":
                        parts = line.split(" ")
                        blob = parts[2] if len(parts) > 2 else ""
                        try:
                            fields = base64.b64decode(blob + "===").decode("utf-8", "replace").split("\0")
                        except Exception:
                            fields = []
                        if len(fields) >= 3 and fields[1] and fields[2]:
                            with self._lock:
                                self._auths.append((fields[1], fields[2]))
                            stream.write(b"235 Authentication successful\r\n")
                        else:
                            stream.write(b"535 Authentication failed\r\n")
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


class SilentTCPServer(threading.Thread):
    """接受连接但不发 greeting：真实 smtplib 15 秒超时路径。"""

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._held = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._held.append(conn)   # hold the socket open, never greet

    def close(self):
        self.sock.close()
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass


def make_proxy(target):
    """本地测试代理：转发到 EasyLife，仅剥离 X-Frame-Options 供 375 夹具使用。"""

    class Proxy(http.server.BaseHTTPRequestHandler):
        def forward(self):
            if self.path.startswith("/__viewport"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                w, h = int(query["w"][0]), int(query["h"][0])
                page = query.get("target", ["/todos/#/todos"])[0]
                data = ('<!doctype html><html><head><style>html,body{margin:0;}'
                        'iframe{display:block;border:0;width:%dpx;height:%dpx;}</style></head>'
                        '<body><iframe title="Real CSS viewport fixture" src="%s"></iframe></body></html>'
                        % (w, h, page)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or None
            req = urllib.request.Request(target + self.path, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ("host", "connection", "accept-encoding"):
                    req.add_header(k, v)
            try:
                res = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as exc:
                res = exc
            with res:
                data = res.read()
                self.send_response(res.code)
                for k, v in res.headers.items():
                    if k.lower() not in ("x-frame-options", "connection", "transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = forward

        def log_message(self, *_):
            pass

    return Proxy


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    import builtins

    console = (OUT / "mail-ui-console.log").open("w", buffering=1)
    builtin_print = builtins.print

    def print(*args, **kwargs):  # noqa: A001 - tee every line into the evidence directory
        kwargs.pop("file", None)
        text = " ".join(str(arg) for arg in args)
        builtin_print(text, flush=True)
        console.write(text + "\n")
    results = {"date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "command": shlex.join(["python3"] + sys.argv),
               "checks": [], "measurements": [], "api_probes": [], "exit_code": 1}
    results["static_sha256"] = {
        name: hashlib.sha256((ROOT / "easylife/static" / name).read_bytes()).hexdigest()
        for name in ("index.html", "style.css", "app.js")}
    results["backend_sha256"] = {
        name: hashlib.sha256((ROOT / "easylife" / name).read_bytes()).hexdigest()
        for name in ("app.py", "mail_settings.py", "auth_mail.py", "reminders.py")}
    results["test_sha256"] = {
        name: hashlib.sha256((ROOT / "tests" / name).read_bytes()).hexdigest()
        for name in ("browser_mail_ui.py", "mail_ui_race_cases.py") if (ROOT / "tests" / name).exists()}
    logging.disable(logging.CRITICAL)
    checks = results["checks"]

    def check(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": str(detail)[:300]})
        print(("PASS " if passed else "FAIL ") + name + (("  [%s]" % detail) if detail and not passed else ""),
              flush=True)
        assert passed, name

    with tempfile.TemporaryDirectory(prefix="easylife-mail-ui-") as scratch:
        cfg = app.load_config({
            "EASYLIFE_HOST": "127.0.0.1", "EASYLIFE_PORT": "0",
            "EASYLIFE_BASE_PATH": "/todos", "EASYLIFE_DB": scratch + "/mail.db",
            "EASYLIFE_SECRET": "local-mail-ui-verification-secret-0001",
            "EASYLIFE_ADMIN_USER_IDS": "1",
        })
        db.init_db(cfg["db"])
        conn = db.connect(cfg["db"])
        admin, _, _ = auth.register(conn, "admin@example.com", "admin-password-2026",
                                    ip="127.0.0.1", limiter=auth.LoginRateLimiter())
        server = app.make_server(cfg)
        member, _, _ = auth.register(conn, "member@example.com", "member-password-2026",
                                     ip="127.0.0.1", limiter=auth.LoginRateLimiter())
        if RACE_MODE:
            # The account-switch scenario signs in after SMTP is enabled. Seed an
            # already verified fixture member; retain the production verification gate.
            with db.tx(conn):
                conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now_utc(), member["id"]))
        admin_token, _ = auth.create_session(conn, admin["id"], ttl=7200)
        member_token, _ = auth.create_session(conn, member["id"], ttl=7200)
        conn.close()
        check("服务器冻结的管理员仅为配置的账号", server.admin_user_ids == frozenset({admin["id"]}))
        check("普通账号不是管理员", member["id"] not in server.admin_user_ids)

        smtp, silent = MiniSMTPServer(), SilentTCPServer()
        smtp.start()
        silent.start()
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()

        base = "http://127.0.0.1:%d" % server.server_address[1]
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_proxy(base))
        proxy_base = "http://127.0.0.1:%d" % proxy.server_address[1]
        for srv in (server, proxy):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        admin_csrf = auth.csrf_token(cfg["secret"], admin_token)

        def request(method, path, payload=None, cookie=admin_token, csrf=admin_csrf):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            req.add_header("Cookie", "easylife_session=" + cookie)
            if method in ("POST", "PUT", "DELETE"):
                req.add_header("Content-Type", "application/json")
                if csrf:
                    req.add_header("X-CSRF-Token", csrf)
            try:
                with urllib.request.urlopen(req, timeout=45) as res:
                    return res.status, json.loads(res.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode()
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {"raw": body[:200]}
                return exc.code, parsed

        def wait_smtp(count, tries=200):
            for _ in range(tries):
                if len(smtp.received()) >= count:
                    return True
                time.sleep(.1)
            return False

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            marionette_port = sock.getsockname()[1]
        profile = pathlib.Path(scratch) / "profile"
        profile.mkdir()
        (profile / "user.js").write_text('user_pref("marionette.port", %d);\n' % marionette_port)
        with open(pathlib.Path(scratch) / "firefox.log", "w") as log:
            proc = subprocess.Popen(["firefox", "--headless", "--no-remote", "--marionette",
                                     "-profile", str(profile), "about:blank"], stdout=log, stderr=log)
            try:
                s = None
                for _ in range(120):
                    try:
                        s = Marionette(marionette_port)
                        break
                    except OSError:
                        time.sleep(.2)
                if s is None:
                    raise RuntimeError("Firefox Marionette did not start")
                caps = s.command("WebDriver:NewSession", {"capabilities": {}})
                browser = caps.get("capabilities", caps)
                results["browser"] = {key: browser.get(key) for key in
                                      ("browserName", "browserVersion", "platformName", "moz:headless")}
                frame = None

                def nav_guard(token):
                    # Marionette 的 Navigate 可能在旧文档上就返回（实测 7ms），因此每次
                    # 导航后用 performance navigation entry 确认「当前文档就是刚导航的
                    # 这一份」，避免在旧文档上误判等待条件成立。
                    return ("(String((performance.getEntriesByType('navigation')[0]||{}).name||'')"
                            ".indexOf(%s)>=0)" % json.dumps(token))

                def wait_doc(token, expression, tries=150):
                    s.wait(nav_guard(token) + " && (" + expression + ")", tries=tries)

                nav_stamp = [0]

                def viewport(w, h, target="#/settings/mail", wait_mail=True):
                    nonlocal frame
                    s.top()
                    # 唯一查询参数确保每次都是完整加载（真实会话/刷新语义），而不是仅 hash 变化。
                    nav_stamp[0] += 1
                    token = "ui=%d.%d" % (int(time.time() * 1000), nav_stamp[0])
                    stamp = token.split("=", 1)[1]
                    url = "/todos/?%s%s" % (token, target)
                    if w < 450:
                        s.command("WebDriver:SetWindowRect", {"width": max(w, 900), "height": h + 86})
                        # '#' 必须编码：否则 hash 会变成夹具 URL 的 fragment，target 丢掉路由。
                        s.command("WebDriver:Navigate", {"url": proxy_base + "/__viewport?w=%d&h=%d&stamp=%s&target=%s"
                                                         % (w, h, stamp, urllib.parse.quote(url, safe="/?=&"))})
                        s.wait(nav_guard("stamp=" + stamp) + " && !!document.querySelector('iframe')")
                        frame = s.command("WebDriver:FindElement", {"using": "css selector", "value": "iframe"})
                        s.frame(frame)
                        wait_doc(token, "!!document.querySelector('#mail-settings')")
                    else:
                        frame = None
                        s.command("WebDriver:SetWindowRect", {"width": w, "height": h + 86})
                        s.command("WebDriver:Navigate", {"url": base + url})
                        wait_doc(token, "!!document.querySelector('#mail-settings')")
                        actual = s.js("return [innerWidth,innerHeight,outerWidth,outerHeight]")
                        s.command("WebDriver:SetWindowRect", {"width": w + actual[2] - actual[0],
                                                              "height": h + actual[3] - actual[1]})
                    if wait_mail:
                        wait_doc(token, "!document.querySelector('#view-settings').hidden && "
                                        "!document.querySelector('#mail-settings').hidden && "
                                        "document.querySelector('#mail-status').dataset.state === 'saved'")
                    else:
                        wait_doc(token, "!document.querySelector('#view-settings').hidden")
                    s.js("document.querySelector('#theme-select').value='light';"
                         "document.querySelector('#theme-select').dispatchEvent(new Event('change'));"
                         "window.scrollTo(0,0);")
                    inner = s.js("return [innerWidth,innerHeight]")
                    assert inner == [w, h], (w, h, inner)

                def capture(name, w, h):
                    time.sleep(.3)
                    data = s.js(MEASURE)
                    feed = s.js("const n=document.querySelector('#feed-url'); const v=n.value;"
                                "if(!document.querySelector('#settings').hidden){n.value='已隐藏专属订阅地址';} return v;")
                    s.js("document.querySelector('#mail-settings').scrollIntoView({block:'start'});")
                    time.sleep(.15)
                    filename = name + "-%dx%d.png" % (w, h)
                    if frame is not None:
                        s.top()
                        shot = s.command("WebDriver:TakeScreenshot", {"id": frame[ELEMENT], "full": False})
                        s.frame(frame)
                    else:
                        shot = s.command("WebDriver:TakeScreenshot", {"full": False})
                    png = base64.b64decode(shot.get("value", "") if isinstance(shot, dict) else shot)
                    (OUT / filename).write_bytes(png)
                    data.update(name=name, screenshot=filename,
                                method="iframe CSS viewport; local X-Frame-Options test proxy" if frame is not None
                                else "real headless Firefox window",
                                png_dimensions=list(struct.unpack(">II", png[16:24])))
                    assert data["png_dimensions"] == [w, h], data["png_dimensions"]
                    s.js("const n=document.querySelector('#feed-url'); n.value=arguments[0];", feed)
                    results["measurements"].append(data)
                    print("shot " + filename, "state=" + str(data["statusState"]), flush=True)

                def layout(name, w):
                    m = s.js(MEASURE)
                    check(name + ": 无页面横向溢出", m["scrollWidth"] <= m["clientWidth"],
                          "%s>%s" % (m["scrollWidth"], m["clientWidth"]))
                    check(name + ": 邮件表单控件 >=44px", not m["shortTargets"], m["shortTargets"])
                    check(name + ": 邮件卡片可见", m["mailSectionVisible"])
                    if w <= 560:
                        check(name + ": 手机单列（表单网格单列）", " " not in m["formGridColumns"].strip(),
                              m["formGridColumns"])
                    else:
                        check(name + ": 桌面表单宽度合理（<=640px）",
                              280 <= (m["mailForm"] or {}).get("width", 0) <= 641, m["mailForm"])
                    return m

                def set_values(host, port, user, from_addr, password=None, enabled=True):
                    # 注意：ExecuteScript 的参数只出现在脚本顶层的 arguments；不能包进
                    # 无参 IIFE（否则 arguments 为空，会把 "undefined" 写进输入框）。
                    s.js(
                        "const set=(id,v)=>{const n=document.getElementById(id);n.value=v;"
                        "n.dispatchEvent(new Event('input',{bubbles:true}));};"
                        "const en=document.getElementById('mail-enabled');en.checked=arguments[0];"
                        "en.dispatchEvent(new Event('change',{bubbles:true}));"
                        "set('mail-host',arguments[1]);set('mail-port',arguments[2]);"
                        "set('mail-user',arguments[3]);set('mail-from',arguments[4]);"
                        "if(arguments[5]!==null){set('mail-password',arguments[5]);}",
                        enabled, host, str(port), user, from_addr, password)

                record_fetches = ("window.__calls=[];"
                                  "window.__origFetch=window.__origFetch||window.fetch;"
                                  "window.fetch=function(u,o){window.__calls.push({u:String(u),"
                                  "m:(o&&o.method)||'GET',b:o&&o.body?o.body:null});"
                                  "return window.__origFetch(u,o);};")

                if RACE_MODE:
                    from mail_ui_race_cases import run as run_race_cases
                    run_race_cases(s=s, viewport=viewport, capture=capture, layout=layout,
                                   set_values=set_values, request=request, wait_smtp=wait_smtp,
                                   check=check, results=results, base=base, admin_token=admin_token,
                                   member_token=member_token, smtp=smtp, db_path=cfg["db"])
                    results["exit_code"] = 0
                    print("EXIT_CODE=0", flush=True)
                    return 0

                # ---------------------------------------------- 匿名（必须在任何 AddCookie 之前：
                # 实测 Firefox 在 DeleteAllCookies 后导航会恢复刚删除的 cookie，所以要
                # 用尚未写入任何会话 cookie 的浏览器验证真匿名。）
                s.command("WebDriver:SetWindowRect", {"width": 900, "height": 900})
                s.command("WebDriver:Navigate", {"url": base + "/todos/?anon=0#/settings/mail"})
                wait_doc("anon=0", "!document.querySelector('#view-auth').hidden && "
                                   "document.querySelector('#view-settings').hidden")
                anon = None
                try:
                    anon = s.async_js("const done=arguments[arguments.length-1];"
                                      "fetch('./api/admin/mail-settings',{credentials:'same-origin'})"
                                      ".then(r=>done(r.status)).catch(()=>done(-1));")
                except Exception as exc:  # noqa: BLE001
                    results["api_probes"].append({"probe": "anonymous GET (browser)",
                                                  "error": type(exc).__name__})
                results["api_probes"].append({"probe": "anonymous GET mail-settings", "status": anon})
                check("匿名会话读取管理接口返回 401", anon == 401, anon)
                check("匿名访问 #/settings/mail 只显示登录页",
                      s.js("return !document.querySelector('#view-auth').hidden && "
                           "document.querySelector('#mail-settings').hidden"))

                # ---------------------------------------------- 管理员桌面流程（1440×814，真实窗口）
                s.command("WebDriver:Navigate", {"url": base + "/todos/"})
                s.command("WebDriver:AddCookie", {"cookie": {"name": "easylife_session", "value": admin_token,
                                                            "path": "/todos", "domain": "127.0.0.1"}})
                viewport(1440, 814)
                check("管理员可见邮件设置入口与卡片（真实登录态）",
                      s.js("return !document.querySelector('#mail-settings').hidden && "
                           "!document.querySelector('#nav-mail-settings').hidden"))
                check("初始来源=内置默认值、授权码未设置",
                      s.js("return document.querySelector('#mail-source').textContent==='内置默认值' && "
                           "document.querySelector('#mail-password-state').textContent==='未设置'"))
                check("初始表单默认值（587/no-reply 且未启用）",
                      s.js("return document.querySelector('#mail-port').value==='587' && "
                           "document.querySelector('#mail-from').value==='no-reply@localhost' && "
                           "!document.querySelector('#mail-enabled').checked"))
                check("密码输入框初始为空（不回填）",
                      s.js("return document.querySelector('#mail-password').value===''"))
                check("日历卡片仍在同一设置页且入口可用",
                      s.js("return !document.querySelector('#settings').hidden && "
                           "document.querySelector('#feed-url').value.length>10"))
                capture("mail-desktop-empty", 1440, 814)

                s.js(record_fetches)

                # 未保存 → 测试被阻止（不发任何请求）
                set_values("127.0.0.1", smtp.port, "admin@example.com", "EasyLife <admin@example.com>",
                           password=TEST_PASSWORD)
                check("修改后进入未保存状态",
                      s.js("return document.querySelector('#mail-status').dataset.state==='unsaved'"))
                s.click("#mail-test")
                time.sleep(.3)
                check("未保存时测试被阻止且未发请求",
                      s.js("return document.querySelector('#mail-status').dataset.state==='unsaved' && "
                           "document.querySelector('#mail-status').textContent.indexOf('请先')>=0 && "
                           "!window.__calls.some(c=>c.u.indexOf('mail-settings/test')>=0)"))
                capture("mail-unsaved", 1440, 814)

                # 服务商示例：只填充，不保存
                s.click("#mail-preset-qq")
                check("预设仅填入 QQ 主机与端口且未保存",
                      s.js("return document.querySelector('#mail-host').value==='smtp.qq.com' && "
                           "document.querySelector('#mail-port').value==='465' && "
                           "document.querySelector('#mail-status').dataset.state==='unsaved'"))
                check("预设未写入数据库（GET 仍为默认来源）",
                      request("GET", "/api/admin/mail-settings")[1]["source"] != "database")
                s.click("#mail-reload")
                try:
                    s.wait("document.querySelector('#mail-host').value===''")
                except AssertionError:
                    print("DEBUG host=", s.js("return document.querySelector('#mail-host').value"), flush=True)
                    print("DEBUG status=", s.js("return document.querySelector('#mail-status').dataset.state + '|' + "
                                                "document.querySelector('#mail-status').textContent"), flush=True)
                    print("DEBUG calls=", s.js("return JSON.stringify(window.__calls||null)"), flush=True)
                    raise
                check("重新读取丢弃未保存的示例值",
                      s.js("return document.querySelector('#mail-status').dataset.state==='saved'"))

                # 保存（真实 PUT，事务落库）
                set_values("127.0.0.1", smtp.port, "admin@example.com", "EasyLife <admin@example.com>",
                           password=TEST_PASSWORD)
                s.click("#mail-save")
                s.wait("document.querySelector('#mail-status').dataset.state==='saved'")
                check("保存成功且来源变为已保存配置",
                      s.js("return document.querySelector('#mail-status').textContent.indexOf('保存成功')>=0 && "
                           "document.querySelector('#mail-source').textContent==='已保存的站点配置'"))
                check("保存成功后授权码输入被清空（密码不留痕）",
                      s.js("return document.querySelector('#mail-password').value==='' && "
                           "!document.querySelector('#mail-clear-password').checked"))
                check("保存后授权码状态=已设置",
                      s.js("return document.querySelector('#mail-password-state').textContent.indexOf('已设置')>=0"))
                status, saved = request("GET", "/api/admin/mail-settings")
                saved_keys = set(saved.get("mail", {}))
                check("GET 脱敏字段与保存一致且不含 password",
                      status == 200 and saved["mail"]["host"] == "127.0.0.1"
                      and saved["mail"]["port"] == smtp.port and saved["mail"]["user"] == "admin@example.com"
                      and saved["mail"]["password_set"] is True
                      and saved_keys == {"enabled", "host", "port", "user", "from", "password_set"}
                      and '"password"' not in json.dumps(saved, ensure_ascii=False),
                      "%s %s" % (status, sorted(saved_keys)))
                conn = db.connect(cfg["db"])
                row = conn.execute("SELECT value, updated_at FROM site_settings WHERE key='mail.smtp'").fetchone()
                conn.close()
                parsed = json.loads(row["value"])
                check("SQLite site_settings 行已真实写入",
                      parsed["host"] == "127.0.0.1" and parsed["port"] == smtp.port
                      and parsed.get("password") == TEST_PASSWORD)
                results["db_row"] = {"key": "mail.smtp", "host": parsed["host"], "port": parsed["port"],
                                     "user": parsed["user"], "from": parsed["from"],
                                     "password_len": len(parsed.get("password", "")),
                                     "updated_at": row["updated_at"]}
                check("URL 与浏览器存储不含授权码",
                      s.js("return location.hash.indexOf(arguments[0])<0 && "
                           "JSON.stringify(Object.entries(localStorage)).indexOf(arguments[0])<0 && "
                           "JSON.stringify(Object.entries(sessionStorage)).indexOf(arguments[0])<0",
                           TEST_PASSWORD))
                calls = s.js("return window.__calls.map(c=>({u:c.u,m:c.m,b:c.b}))")
                put_calls = [c for c in calls if c["m"] == "PUT"]
                check("PUT 请求体包含全部窗口字段与新授权码",
                      put_calls and json.loads(put_calls[-1]["b"])["host"] == "127.0.0.1"
                      and json.loads(put_calls[-1]["b"])["password"] == TEST_PASSWORD)

                results["desktop_fetch_calls"] = [
                    {"url": c["u"], "method": c["m"],
                     "body": (json.loads(c["b"]) if c["b"] else None)} for c in calls]
                for entry in results["desktop_fetch_calls"]:
                    if isinstance(entry["body"], dict) and "password" in entry["body"]:
                        entry["body"]["password"] = "<redacted-len-%d>" % len(entry["body"]["password"])
                capture("mail-saved", 1440, 814)

                # 离开页面（pagehide：关闭 / 刷新 / 跳转）也清空授权码输入，且不改变已保存配置
                s.js("const n=document.querySelector('#mail-password');n.value='leftover-secret';"
                     "n.dispatchEvent(new Event('input',{bubbles:true}));"
                     "window.dispatchEvent(new Event('pagehide'));")
                check("离开页面（pagehide）清空授权码输入",
                      s.js("return document.querySelector('#mail-password').value==='' && "
                           "!document.querySelector('#mail-clear-password').checked && "
                           "document.querySelector('#mail-status').dataset.state==='saved'"))

                # 整页刷新恢复
                s.command("WebDriver:Navigate", {"url": base + "/todos/?reload=1#/settings/mail"})
                wait_doc("reload=1", "!document.querySelector('#view-settings').hidden && "
                                     "!document.querySelector('#mail-settings').hidden && "
                                     "document.querySelector('#mail-status').dataset.state==='saved'")
                s.js(record_fetches)
                check("刷新后从数据库恢复保存值",
                      s.js("return document.querySelector('#mail-host').value==='127.0.0.1' && "
                           "document.querySelector('#mail-port').value==='%d' && "
                           "document.querySelector('#mail-user').value==='admin@example.com' && "
                           "document.querySelector('#mail-enabled').checked" % smtp.port))
                check("刷新后密码输入仍为空（不回填）",
                      s.js("return document.querySelector('#mail-password').value==='' && "
                           "document.querySelector('#mail-password-state').textContent.indexOf('已设置')>=0"))

                # 真实测试发信：mini SMTP 接受；界面只声明服务器接受
                before = len(smtp.received())
                s.click("#mail-test")
                s.wait("document.querySelector('#mail-status').dataset.state==='accepted'", tries=300)
                check("mini SMTP 实际收到测试邮件（默认收件人=管理员）",
                      wait_smtp(before + 1) and "To: admin@example.com" in smtp.received()[-1])
                check("测试成功只声明 SMTP 接受、不承诺送达",
                      s.js("return document.querySelector('#mail-status').textContent.indexOf('SMTP')>=0 && "
                           "document.querySelector('#mail-status').textContent.indexOf('不代表')>=0"))
                check("mini SMTP 收到保存的账号与授权码（值未写入证据）",
                      smtp.auths() and smtp.auths()[-1][0] == "admin@example.com"
                      and smtp.auths()[-1][1] == TEST_PASSWORD)
                capture("mail-test-accepted", 1440, 814)

                # 显式收件地址：请求体只能有 to
                before = len(smtp.received())
                s.js("const n=document.querySelector('#mail-test-to');n.value='qa@example.com';"
                     "n.dispatchEvent(new Event('input',{bubbles:true}));")
                s.click("#mail-test")
                check("mini SMTP 收到显式收件地址",
                      wait_smtp(before + 1) and "To: qa@example.com" in smtp.received()[-1])
                time.sleep(.3)
                calls = s.js("return window.__calls.map(c=>({u:c.u,m:c.m,b:c.b}))")
                test_calls = [json.loads(c["b"]) for c in calls if c["u"].endswith("mail-settings/test")]
                check("测试请求体只有 to 字段（无临时 SMTP 覆盖）",
                      test_calls and set(test_calls[-1]) == {"to"} and test_calls[-1]["to"] == "qa@example.com")
                check("默认收件人请求体为空对象（服务端使用管理员邮箱）",
                      test_calls[0] == {} and "To: admin@example.com" in smtp.received()[0])

                # 失败（连接拒绝）→ 502 文案
                status, _ = request("PUT", "/api/admin/mail-settings", {"host": "127.0.0.1", "port": closed_port})
                check("API 保存失效端口配置成功", status == 200)
                viewport(1440, 814)
                s.click("#mail-test")
                s.wait("document.querySelector('#mail-status').dataset.state==='failed'", tries=300)
                check("连接失败显示失败状态而非成功",
                      s.js("return document.querySelector('#mail-status').textContent.indexOf('测试失败')>=0"))
                capture("mail-test-failed", 1440, 814)

                # 超时（静默 TCP，真实 15s smtplib 超时）→ 502
                status, _ = request("PUT", "/api/admin/mail-settings", {"host": "127.0.0.1", "port": silent.port})
                check("API 保存静默端口配置成功", status == 200)
                viewport(1440, 814)
                started = time.time()
                s.click("#mail-test")
                s.wait("document.querySelector('#mail-status').dataset.state==='failed'", tries=400)
                results["timeout_seconds"] = round(time.time() - started, 1)
                check("超时同样显示失败状态",
                      s.js("return document.querySelector('#mail-status').textContent.indexOf('测试失败')>=0"))
                capture("mail-test-timeout", 1440, 814)

                # 恢复可用配置后重新可用
                status, _ = request("PUT", "/api/admin/mail-settings",
                                    {"host": "127.0.0.1", "port": smtp.port, "enabled": True})
                check("API 恢复可用配置成功", status == 200)
                viewport(1440, 814)
                s.click("#mail-test")
                s.wait("document.querySelector('#mail-status').dataset.state==='accepted'", tries=300)
                check("恢复配置后测试重新成功", True)
                layout("邮件设置 1440x814", 1440)

                # 深色主题截图
                s.js("document.querySelector('#theme-select').value='dark';"
                     "document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                capture("mail-desktop-dark", 1440, 814)
                s.js("document.querySelector('#theme-select').value='light';"
                     "document.querySelector('#theme-select').dispatchEvent(new Event('change'));")

                # 键盘焦点反馈（1280 真实窗口）
                viewport(1280, 800)
                s.js("document.querySelector('#mail-host').focus()")
                for _ in range(14):
                    s.key("\ue004")
                    if s.js("return document.activeElement.id") == "mail-save":
                        break
                check("键盘可达保存按钮且焦点轮廓可见",
                      s.js("const el=document.activeElement; return el.id==='mail-save' && "
                           "el.matches(':focus-visible') && getComputedStyle(el).outlineWidth!=='0px'"),
                      s.js("return document.activeElement.id + '|' + document.activeElement.matches(':focus-visible')"))
                layout("邮件设置 1280x800", 1280)
                capture("mail-desktop-1280", 1280, 800)

                # 其余宽度：768 平板 + 430/390/375/320 手机（<450 用等尺寸 iframe 夹具）
                viewport(768, 1024)
                layout("邮件设置 768x1024", 768)
                capture("mail-tablet-768", 768, 1024)
                for w, h in [(430, 932), (390, 844), (375, 812), (320, 568)]:
                    viewport(w, h)
                    layout("邮件设置 %dx%d" % (w, h), w)
                    if w in (320, 390, 430):
                        capture("mail-mobile-%d" % w, w, h)
                    if w == 375:
                        capture("mail-mobile-375-light", w, h)
                        s.js("document.querySelector('#theme-select').value='dark';"
                             "document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                        capture("mail-mobile-375-dark", w, h)
                        s.js("document.querySelector('#theme-select').value='light';"
                             "document.querySelector('#theme-select').dispatchEvent(new Event('change'));")

                # ---------------------------------------------- 普通用户：无入口、后端拒绝
                s.top()
                s.command("WebDriver:DeleteAllCookies")
                s.command("WebDriver:AddCookie", {"cookie": {"name": "easylife_session", "value": member_token,
                                                            "path": "/todos", "domain": "127.0.0.1"}})
                viewport(1440, 814, wait_mail=False)
                check("普通用户设置页不渲染邮件入口",
                      s.js("return document.querySelector('#mail-settings').hidden && "
                           "document.querySelector('#nav-mail-settings').hidden && "
                           "document.querySelector('#mail-status').hidden"))
                check("普通用户日历订阅仍可用（保留日历功能）",
                      s.js("return !document.querySelector('#settings').hidden && "
                           "document.querySelector('#feed-url').value.length>10"))
                member_get = s.async_js("const done=arguments[arguments.length-1];"
                                        "fetch('./api/admin/mail-settings',{credentials:'same-origin'})"
                                        ".then(r=>done(r.status)).catch(()=>done(-1));")
                member_put = s.async_js("const done=arguments[arguments.length-1];"
                                        "fetch('./api/admin/mail-settings',{method:'PUT',credentials:'same-origin',"
                                        "headers:{'Content-Type':'application/json'},"
                                        "body:JSON.stringify({host:'evil.example'})})"
                                        ".then(r=>done(r.status)).catch(()=>done(-1));")
                member_test = s.async_js("const done=arguments[arguments.length-1];"
                                         "fetch('./api/admin/mail-settings/test',{method:'POST',credentials:'same-origin',"
                                         "headers:{'Content-Type':'application/json'},body:'{}'})"
                                         ".then(r=>done(r.status)).catch(()=>done(-1));")
                results["api_probes"] += [
                    {"probe": "member GET mail-settings (browser)", "status": member_get},
                    {"probe": "member PUT mail-settings (browser)", "status": member_put},
                    {"probe": "member POST mail-settings/test (browser)", "status": member_test}]
                check("普通用户读取返回 403", member_get == 403, member_get)
                check("普通用户写入返回 403", member_put == 403, member_put)
                check("普通用户测试发信返回 403", member_test == 403, member_test)
                status, _ = request("PUT", "/api/admin/mail-settings", {"host": "evil.example"},
                                    cookie=member_token, csrf=auth.csrf_token(cfg["secret"], member_token))
                check("普通用户经真实 API 写入仍被拒绝", status == 403, status)
                check("越权写入未落地", request("GET", "/api/admin/mail-settings")[1]["mail"]["host"] == "127.0.0.1")
                check("普通用户会话与日历订阅正常",
                      request("GET", "/api/me", cookie=member_token)[1]["user"]["email"] == "member@example.com")
                capture("mail-member-1440", 1440, 814)

                viewport(375, 812, wait_mail=False)
                check("普通用户在 375 也没有邮件卡片",
                      s.js("return document.querySelector('#mail-settings').hidden && "
                           "document.querySelector('#nav-mail-settings').hidden"))
                capture("mail-member-375", 375, 812)

                # ---------------------------------------------- 限流 429（最后执行）
                s.top()
                s.command("WebDriver:DeleteAllCookies")
                s.command("WebDriver:AddCookie", {"cookie": {"name": "easylife_session", "value": admin_token,
                                                            "path": "/todos", "domain": "127.0.0.1"}})
                viewport(1440, 814)
                pumped = []
                for _ in range(8):
                    status, _ = request("POST", "/api/admin/mail-settings/test", {"to": "rate@example.com"})
                    pumped.append(status)
                    if status == 429:
                        break
                results["rate_limit_pump"] = pumped
                check("真实 API 循环达到限流 429", 429 in pumped, pumped)
                s.click("#mail-test")
                s.wait("document.querySelector('#mail-status').dataset.state==='rate-limited'", tries=300)
                check("限流显示专用状态与冷却按钮",
                      s.js("return document.querySelector('#mail-status').textContent.indexOf('频繁')>=0 && "
                           "document.querySelector('#mail-test').disabled && "
                           "document.querySelector('#mail-test').textContent.indexOf('限流中')>=0"))
                capture("mail-rate-limited", 1440, 814)

                results["smtp_summary"] = {"messages_recorded": len(smtp.received()),
                                           "auth_usernames": sorted({user for user, _ in smtp.auths()})}
                results["exit_code"] = 0
                print("EXIT_CODE=0", flush=True)
            finally:
                proc.terminate()
                proc.wait(timeout=15)
                for srv in (server, proxy):
                    srv.shutdown()
                    srv.server_close()
                smtp.close()
                silent.close()
                passed = sum(1 for item in checks if item["passed"])
                results["summary"] = {
                    "checks_passed": passed, "checks_total": len(checks),
                    "measurements": len(results["measurements"]),
                    "screenshots": sorted(p.name for p in OUT.glob("mail-*.png")),
                    "exit_code": results["exit_code"],
                }
                (OUT / "mail-ui-results.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2) + "\n")
                print("SUMMARY %d/%d checks passed, %d measurements, %d checks total"
                      % (passed, len(checks), len(results["measurements"]), len(checks)), flush=True)
                console.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
