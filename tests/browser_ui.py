"""Real Firefox UI evidence; stdlib Marionette/iframe scaffold from the prior round.

Run from the repository root: python3 tests/browser_ui.py --phase before|after
No production data or network services are used. Profiles/DB/cookies live in /tmp.
Only the local test proxy strips X-Frame-Options; the product keeps DENY.
"""
import argparse
import base64
import hashlib
import http.server
import json
import logging
import pathlib
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
from easylife import app, auth, db, todos  # noqa: E402

ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
MEASURE = r"""
const rect = s => { const n = document.querySelector(s); if (!n) return null;
 const r = n.getBoundingClientRect(); return Object.fromEntries(
 ['top','left','width','height','bottom'].map(k => [k, Math.round(r[k]*100)/100])); };
const visible = n => !!(n.getBoundingClientRect().width && n.getBoundingClientRect().height);
return {innerWidth, innerHeight, clientWidth: document.documentElement.clientWidth,
 scrollWidth: document.documentElement.scrollWidth, dpr: devicePixelRatio,
 theme: document.documentElement.getAttribute('data-theme') || 'system',
 siteHeader: rect('.site-header'), pageHeading: rect('.page-heading'),
 tasksCard: rect('.tasks-card'), composer: rect('.composer'), filters: rect('#filters'),
 firstItem: rect('#todo-list .todo'),
 workspaceOrder: [...document.querySelectorAll('.workspace-grid > section')].map(n => n.className),
 visibleCreateFields: [...document.querySelectorAll('#create-form input, #create-form textarea, #create-form select')].filter(visible).map(n=>n.id),
 shortTargets: [...document.querySelectorAll('button,a[href],input,textarea,select')].filter(visible)
 .filter(n => !n.classList.contains('skip-link')).filter(n => {const r=n.getBoundingClientRect(); return r.width<43.9||r.height<43.9;})
 .map(n=>({id:n.id||n.className,width:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height})),
 smallInputs: [...document.querySelectorAll('input,textarea,select')].filter(visible)
 .filter(n=>parseFloat(getComputedStyle(n).fontSize)<16).map(n=>n.id),
 todoCount: document.querySelectorAll('#todo-list .todo').length,
 bannerVisible: !document.querySelector('#verification-banner').hidden};
"""


class Marionette:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.sock.settimeout(30)
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

    def wait(self, expression):
        for _ in range(100):
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


def make_proxy(target):
    class Proxy(http.server.BaseHTTPRequestHandler):
        def forward(self):
            if self.path.startswith('/todos/__settle-pixel'):
                # CLI screenshots wait for load, but /api/me is asynchronous. A hidden
                # test-only image delays load without changing visible content/layout/logic.
                time.sleep(2)
                data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG1sAAAAASUVORK5CYII=')
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/__viewport"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                w, h = int(query['w'][0]), int(query['h'][0])
                data = ('<!doctype html><html><head><style>html,body{margin:0;}iframe{display:block;border:0;width:%dpx;height:%dpx;}</style></head>'
                        '<body><iframe title="Real CSS viewport fixture" src="/todos/#/todos"></iframe></body></html>' % (w, h)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or None
            req = urllib.request.Request(target + self.path, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ("host", "connection", "accept-encoding"):
                    req.add_header(k, v)
            try:
                res = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as exc:
                res = exc
            with res:
                data = res.read()
                if self.path.startswith('/todos/?screenshot=1'):
                    data = data.replace(b'</body>', b'<img hidden alt="" width="1" height="1" src="./__settle-pixel"></body>')
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--static-dir", help="Optional archived static files for a comparable before run")
    parser.add_argument("--cli-only", action='store_true', help="Retake just the CLI auth screenshot, keeping the full browser results")
    args = parser.parse_args()
    out = ROOT / "docs/verification/mobile-mail/ui"
    out.mkdir(exist_ok=True)
    results = {"date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "command": "python3 tests/browser_ui.py " + " ".join(sys.argv[1:]),
               "phase": args.phase, "measurements": [], "checks": [], "exit_code": 1}
    if args.static_dir:
        app.STATIC_DIR = str(pathlib.Path(args.static_dir).resolve())
    results['static_sha256'] = {name: hashlib.sha256((pathlib.Path(app.STATIC_DIR) / name).read_bytes()).hexdigest()
                                for name in ('index.html', 'style.css', 'app.js')}
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="easylife-ui-") as scratch:
        cfg = app.load_config({"EASYLIFE_HOST": "127.0.0.1", "EASYLIFE_PORT": "0",
                               "EASYLIFE_BASE_PATH": "/todos", "EASYLIFE_DB": scratch + "/ui.db",
                               "EASYLIFE_SECRET": "local-ui-verification-only-0001"})
        server = app.make_server(cfg)
        conn = db.connect(cfg['db'])
        user, _, _ = auth.register(conn, "preview@example.com", "preview-password-2026",
                                    ip="127.0.0.1", limiter=server.limiter)
        for title, details, priority, day, status in [
            ("为周末留出一点空闲", "把本周想做的事记下来，慢慢完成。", 3, 16, "open"),
            ("整理旅行清单", "护照、相机，还有一本喜欢的书。", 2, 18, "doing"),
            ("预约年度体检", "确认时间并添加提醒。", 1, 21, "open"),
            ("给绿植浇水", "阳台的绿意也需要一点照顾。", 0, 15, "done")]:
            due = "2026-10-%d 10:00:00" % day
            todos.create_todo(conn, user['id'], dict(title=title, details=details, priority=priority,
                                                   due_at=due, remind_at=due, status=status))
        todos.add_note(conn, user['id'], 1, {"content": "周五下班前收尾，周末留给自己。"})
        token, _ = auth.create_session(conn, user['id'], ttl=7200)
        conn.close()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_proxy(base))
        proxy_base = "http://127.0.0.1:%d" % proxy.server_address[1]
        for srv in (server, proxy):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        # Allocate a private Marionette port; no dependency on other browser sessions.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        profile = pathlib.Path(scratch) / "profile"
        profile.mkdir()
        (profile / "user.js").write_text('user_pref("marionette.port", %d);\n' % port)
        with open(pathlib.Path(scratch) / "firefox.log", "w") as log:
            proc = subprocess.Popen(["firefox", "--headless", "--no-remote", "--marionette", "-profile", str(profile), "about:blank"], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        s = Marionette(port)
                        break
                    except OSError:
                        time.sleep(.2)
                else:
                    raise RuntimeError("Firefox Marionette did not start")
                caps = s.command("WebDriver:NewSession", {"capabilities": {}})
                browser = caps.get('capabilities', caps)
                results['browser'] = {key: browser.get(key) for key in ('browserName', 'browserVersion', 'platformName', 'moz:headless')}
                s.command("WebDriver:Navigate", {"url": base + "/todos/"})
                s.command("WebDriver:AddCookie", {"cookie": {"name": "easylife_session", "value": token, "path": "/todos", "domain": "127.0.0.1"}})
                frame = None

                def viewport(w, h, logged_in=True):
                    nonlocal frame
                    s.command("WebDriver:SwitchToFrame", {"id": None})
                    s.command("WebDriver:SetWindowRect", {"width": max(w, 900), "height": h + 86})
                    if w < 450:
                        s.command("WebDriver:Navigate", {"url": proxy_base + "/__viewport?w=%d&h=%d" % (w, h)})
                        frame = s.command("WebDriver:FindElement", {"using": "css selector", "value": "iframe"})
                        s.command("WebDriver:SwitchToFrame", {"id": frame})
                    else:
                        frame = None
                        s.command("WebDriver:SetWindowRect", {"width": w, "height": h + 86})
                        s.command("WebDriver:Navigate", {"url": base + "/todos/?size=%d#/todos" % w})
                        actual = s.js("return [innerWidth,innerHeight,outerWidth,outerHeight]")
                        s.command("WebDriver:SetWindowRect", {"width": w + actual[2] - actual[0], "height": h + actual[3] - actual[1]})
                    s.wait("document.querySelectorAll('#todo-list .todo').length === 4" if logged_in else "!document.querySelector('#view-auth').hidden")
                    s.js("document.querySelector('#theme-select').value='light'; document.querySelector('#theme-select').dispatchEvent(new Event('change')); window.scrollTo(0,0);")
                    assert s.js("return [innerWidth,innerHeight]") == [w, h]

                def capture(name, w, h):
                    # Theme background transitions last 150ms; capture the settled frame.
                    time.sleep(.25)
                    data = s.js(MEASURE)
                    # Calendar subscription URLs are bearer credentials: redact before screenshot.
                    original_feed = s.js("const n=document.querySelector('#feed-url'); const value=n.value; n.value='已隐藏专属订阅地址 · 请在应用中复制'; return value;")
                    filename = args.phase + "-" + name + "-%dx%d.png" % (w, h)
                    data.update(name=name, screenshot=filename, method="iframe CSS viewport; local X-Frame-Options test proxy" if frame else "real headless Firefox window")
                    if frame:
                        s.command("WebDriver:SwitchToFrame", {"id": None})
                        shot = s.command("WebDriver:TakeScreenshot", {"id": frame[ELEMENT], "full": False})
                        s.command("WebDriver:SwitchToFrame", {"id": frame})
                    else:
                        shot = s.command("WebDriver:TakeScreenshot", {"full": False})
                    png = base64.b64decode(shot.get('value', '') if isinstance(shot, dict) else shot)
                    (out / filename).write_bytes(png)
                    data['png_dimensions'] = struct.unpack('>II', png[16:24])
                    assert data['png_dimensions'] == (w, h)
                    s.js("document.querySelector('#feed-url').value=arguments[0];", original_feed)
                    data['redaction'] = 'Calendar subscription value replaced for screenshots only; DOM measurements unchanged.'
                    results['measurements'].append(data)
                    print(filename, 'viewport', data['innerWidth'], data['innerHeight'], 'first', data['firstItem'], flush=True)

                def check(name, passed):
                    results['checks'].append({'name': name, 'passed': bool(passed)})
                    print(('PASS ' if passed else 'FAIL ') + name, flush=True)
                    assert passed, name

                def layout(name, w):
                    m = s.js(MEASURE)
                    check(name + ': no page overflow', m['scrollWidth'] <= m['clientWidth'])
                    check(name + ': visible targets >=44x44', not m['shortTargets'])
                    if w <= 560:
                        check(name + ': input fonts >=16px', not m['smallInputs'])
                    return m

                sizes = [(1280, 800), (1440, 814), (768, 1024), (375, 812)]
                if args.phase == 'after':
                    sizes += [(320, 568), (390, 844), (430, 932)]
                if args.cli_only:
                    sizes = []
                for w, h in sizes:
                    viewport(w, h)
                    capture('list-light', w, h)
                    if args.phase == 'after':
                        m = results['measurements'][-1]
                        layout('list %dx%d' % (w, h), w)
                        check('first full task within %dx%d' % (w, h), m['firstItem']['top'] >= 0 and m['firstItem']['bottom'] <= h)
                        if w <= 560:
                            check('compact header and collapsed composer %d' % w, m['siteHeader']['height'] <= 120 and m['visibleCreateFields'] == ['new-title'])
                    if w in (1440, 375):
                        s.js("document.querySelector('#theme-select').value='dark'; document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                        capture('list-dark', w, h)
                        s.js("document.querySelector('#theme-select').value='light'; document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                        s.click('.todo-title')
                        s.wait("!document.querySelector('#view-detail').hidden")
                        capture('detail', w, h)
                        if args.phase == 'before':
                            s.click('#btn-settings')
                            s.wait("!document.querySelector('#settings').hidden")
                        else:
                            s.js("location.hash='#/settings'")
                            s.wait("!document.querySelector('#view-settings').hidden")
                        capture('settings', w, h)
                    if args.phase == 'after':
                        s.js("location.hash='#/todos'")
                        s.wait("!document.querySelector('#view-list').hidden && document.querySelector('#todo-list').getAttribute('aria-busy')==='false'")
                        s.click('#btn-new')
                        s.click('#create-options-toggle')
                        m = layout('expanded composer %d' % w, w)
                        check('all five create fields available %d' % w, len(m['visibleCreateFields']) == 5)
                        if w in (375, 1440):
                            s.js("document.querySelector('.composer').scrollIntoView({block:'start'})")
                            capture('composer-expanded', w, h)
                        s.js("location.hash='#/todos/1'")
                        s.wait("!document.querySelector('#view-detail').hidden")
                        layout('detail %d' % w, w)
                        s.js("document.querySelector('#theme-select').value='dark'; document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                        if w in (375, 1440):
                            capture('detail-dark', w, h)
                        s.js("location.hash='#/settings'")
                        s.wait("!document.querySelector('#view-settings').hidden")
                        layout('settings %d' % w, w)
                        if w in (375, 1440):
                            capture('settings-dark', w, h)

                if args.phase == 'after' and not args.cli_only:
                    viewport(375, 812)
                    # Real keyboard events, real API writes, real local SQLite persistence.
                    s.js("document.querySelector('#btn-new').focus()")
                    s.key('\ue007')  # Enter
                    check('keyboard new entry focuses title', s.js("return document.activeElement.id==='new-title'"))
                    s.key('\ue004')  # Tab
                    check('collapsed fields skipped in Tab order', s.js("return document.activeElement.id==='create-options-toggle'"))
                    s.key('\ue007')
                    s.key('\ue004')
                    check('keyboard disclosure reaches details', s.js("return document.activeElement.id==='new-details'"))
                    s.js("document.querySelector('#new-title').value='长文本测试'+'abcdefghijklmnop'.repeat(11); document.querySelector('#new-details').value='保留详情与提醒'; document.querySelector('#new-priority').value='3'; document.querySelector('#new-due').value='2026-10-20T09:30'; document.querySelector('#new-remind').value='2026-10-20T09:00';")
                    s.click('#create-submit')
                    s.wait("!document.querySelector('#create-submit').disabled && document.querySelectorAll('#todo-list .todo').length===5")
                    check('real create resets and collapses', s.js("return document.querySelector('#create-options').hidden && !document.querySelector('#new-title').value"))
                    conn = db.connect(cfg['db'])
                    created = dict(conn.execute("SELECT * FROM todos WHERE id=5").fetchone())
                    check('all create fields persisted in SQLite', created['details'] == '保留详情与提醒' and created['priority'] == 3 and created['due_at'] and created['remind_at'])
                    conn.close()
                    s.js("window.scrollTo(0,0)")
                    capture('long-title', 375, 812)
                    layout('long task title', 375)
                    s.js("location.hash='#/todos/5'")
                    s.wait("!document.querySelector('#view-detail').hidden")
                    s.js("document.querySelector('#d-title').value='未保存的编辑'; document.querySelector('#note-content').value='笔记长文本 '+'longtext'.repeat(80);")
                    s.click('#note-submit')
                    s.wait("document.querySelectorAll('#note-list .note').length===1 && !document.querySelector('#note-submit').disabled")
                    check('note keeps unsaved edits and focus', s.js("return document.querySelector('#d-title').value==='未保存的编辑' && document.activeElement.id==='note-content'"))
                    layout('long note', 375)
                    s.click('#btn-detail-delete')
                    check('delete dialog focuses cancel and inerts background', s.js("return document.activeElement.id==='confirm-cancel' && document.querySelector('#app').inert"))
                    s.key('\ue004')
                    check('real Tab stays in confirm dialog', s.js("return document.activeElement.id==='confirm-ok'"))
                    s.key('\ue00c')  # Escape
                    check('Escape returns delete focus', s.js("return document.querySelector('#confirm').hidden && document.activeElement.id==='btn-detail-delete'"))
                    s.click('#btn-detail-delete')
                    capture('delete-confirm', 375, 812)
                    s.click('#confirm-ok')
                    s.wait("!document.querySelector('#view-list').hidden && document.querySelectorAll('#todo-list .todo').length===4")
                    check('real delete returns to list', True)
                    s.click('#todo-check-1')
                    s.wait("document.querySelector('#todo-check-1').checked && !document.querySelector('#todo-check-1').disabled")
                    check('completion feedback restores checkbox focus', s.js("return document.activeElement.id==='todo-check-1' && document.querySelector('#toast').textContent.includes('完成')"))
                    s.click('#todo-check-1')
                    s.wait("!document.querySelector('#todo-check-1').checked && !document.querySelector('#todo-check-1').disabled")
                    # Explicitly injected network failures verify UI recovery, not a real outage.
                    s.js("window.realFetch=window.fetch; window.fetch=function(url, opts){if(String(url).includes('api/todos?')) return new Promise((resolve,reject)=>{window.rejectList=reject}); return window.realFetch(url,opts);};")
                    s.click('[data-status="open"]')
                    s.wait("!!window.rejectList")
                    check('pending transport shows loading, not empty', s.js("return !document.querySelector('#list-loading').hidden && document.querySelector('#list-empty').hidden"))
                    capture('loading', 375, 812)
                    s.js("window.rejectList(new Error('injected offline')); window.fetch=window.realFetch;")
                    s.wait("!document.querySelector('#list-error').hidden")
                    capture('network-error', 375, 812)
                    s.click('#list-retry')
                    s.wait("document.querySelector('#list-error').hidden && !document.querySelector('#todo-list').hidden")
                    check('network retry recovers focus', s.js("return document.activeElement.id==='list-heading'"))
                    s.click('[data-status="archived"]')
                    s.wait("!document.querySelector('#list-empty').hidden")
                    capture('empty-filter', 375, 812)
                    s.click('#empty-all')
                    s.wait("document.querySelectorAll('#todo-list .todo').length===4")
                    check('empty filter returns to all tasks', s.js("return document.querySelector('[data-status=all]').getAttribute('aria-pressed')==='true'"))
                    s.js("window.scrollTo(0,0)")
                    s.click('#btn-account')
                    s.key('\ue00c')
                    check('account Escape restores trigger', s.js("return document.querySelector('#account-panel').hidden && document.activeElement.id==='btn-account'"))
                    s.js("document.querySelector('#theme-select').value='system'; document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
                    check('system theme follows current media query', s.js("return !document.documentElement.hasAttribute('data-theme') && getComputedStyle(document.body).backgroundColor === (matchMedia('(prefers-color-scheme: dark)').matches ? 'rgb(18, 27, 23)' : 'rgb(245, 246, 242)')"))
                    # Short visual viewport exercises scrolling, with no fixed composer/toast.
                    viewport(375, 420)
                    s.click('#btn-new')
                    s.click('#create-options-toggle')
                    s.js("document.querySelector('#new-remind').focus()")
                    layout('short viewport expanded input', 375)
                    check('short viewport focused input unobstructed', s.js("const n=document.activeElement,r=n.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)===n;"))
                    capture('short-input', 375, 420)
                    s.js("location.hash='#/settings'")
                    s.wait("!document.querySelector('#view-settings').hidden")
                    s.js("document.querySelector('#set-email').textContent='verylongaccount'.repeat(15)+'@example.com'")
                    layout('long account text in settings', 375)
                    s.click('#btn-reset-feed')
                    check('short viewport confirmation reachable', s.js("const r=document.querySelector('#confirm-ok').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight"))
                    s.key('\ue00c')
                    # Anonymous views across every target width, including invalid link recovery.
                    s.js("window.scrollTo(0,0)")
                    s.click('#btn-account')
                    s.click('#btn-logout')
                    s.wait("!document.querySelector('#view-auth').hidden")
                    for w, h in sizes:
                        viewport(w, h, logged_in=False)
                        for route, view in [('#/login', 'auth'), ('#/register', 'auth'), ('#/forgot-password', 'forgot'), ('#/reset', 'reset'), ('#/verify', 'verify'), ('#/check-email', 'check-email')]:
                            s.js("location.hash=arguments[0]", route)
                            s.wait("!document.querySelector('#view-" + view + "').hidden")
                            layout('%s %d' % (route, w), w)
                            if w in (375, 1440) and route in ('#/login', '#/forgot-password', '#/reset'):
                                capture(view + '-light', w, h)
                        s.js("document.querySelector('#theme-select').value='dark'; document.querySelector('#theme-select').dispatchEvent(new Event('change')); location.hash='#/login';")
                        s.wait("!document.querySelector('#view-auth').hidden")
                        if w in (375, 1440):
                            capture('auth-dark', w, h)

                # CLI gives a true 375x812 screenshot without the minimum-window constraint.
                cli_profile = pathlib.Path(scratch) / 'cli-profile'
                cli_profile.mkdir()
                cli_shot = out / (args.phase + '-auth-375x812-cli.png')
                cli_cmd = ['firefox', '--headless', '--no-remote', '-profile', str(cli_profile), '--screenshot', str(cli_shot), '--window-size=375,812', proxy_base + '/todos/?screenshot=1#/login']
                cli = subprocess.run(cli_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
                assert cli.returncode == 0 and cli_shot.exists()
                dims = struct.unpack('>II', cli_shot.read_bytes()[16:24])
                assert dims == (375, 812)
                results['cli_screenshot'] = {'command': 'firefox --headless --no-remote -profile <temporary> --screenshot <absolute-output> --window-size=375,812 <local-test-proxy>/todos/?screenshot=1#/login', 'exit_code': cli.returncode, 'png_dimensions': dims, 'filename': cli_shot.name, 'method': 'True CLI 375x812 viewport. Local test proxy adds a hidden 1px image that delays load by 2s so real /api/me and SPA rendering settle. No visible content or product source changes.'}
                results['exit_code'] = 0
                print('EXIT_CODE=0', flush=True)
            finally:
                proc.terminate()
                proc.wait(timeout=15)
                for srv in (server, proxy):
                    srv.shutdown()
                    srv.server_close()
                # Browser capabilities may include a temporary profile path, but never session data.
                result_name = args.phase + ('-cli-result.json' if args.cli_only else '-measurements.json')
                (out / result_name).write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
