"""Focused cases for browser_mail_ui.py --races; reuse its browser/server/SMTP driver.

The fetch gate waits for real HTTP completion before delaying/rejecting delivery to
app.js. No API response is fabricated. Only method/field names/counts are recorded;
passwords, request bodies and session cookies are never written to evidence.
"""

import json

from easylife import db


WIRE = r"""
window.__mailWire = {next:null, held:null, calls:[], completed:[]};
const originalFetch = window.fetch.bind(window);
window.fetch = function(url, options) {
  if (!String(url).includes('api/admin/mail-settings')) return originalFetch(url, options);
  const wire = window.__mailWire, method = (options && options.method) || 'GET';
  const body = options && options.body ? JSON.parse(options.body) : {};
  wire.calls.push({method, path:String(url), fields:Object.keys(body), to:body.to || null,
    csrf:!!(options && options.headers && options.headers['X-CSRF-Token'])});
  const gate = wire.next === method;
  if (gate) wire.next = null;
  return originalFetch(url, options).then(async response => {
    await response.clone().arrayBuffer(); // real server completed; never record the body
    wire.completed.push({method, status:response.status});
    if (!gate) return response;
    return new Promise((resolve,reject) => {
      wire.held = {
        release: () => { wire.held=null; resolve(response); },
        drop: () => { wire.held=null; reject(new TypeError('Controlled HTTP response loss')); }
      };
    });
  });
};
"""


def run(*, s, viewport, capture, layout, set_values, request, wait_smtp,
        check, results, base, admin_token, member_token, smtp, db_path):
    def js_bool(script, *args):
        return bool(s.js("return " + script, *args))

    def edit(field, value):
        s.js("const n=document.getElementById('mail-'+arguments[0]);"
             "if(typeof arguments[1]==='boolean') n.checked=arguments[1]; else n.value=arguments[1];"
             "n.dispatchEvent(new Event('input',{bubbles:true}));", field, value)

    def wait_result(kind):
        s.wait("document.querySelector('#mail-result').dataset.state===" + json.dumps(kind) +
               " && !document.querySelector('#mail-save').disabled && "
               "!document.querySelector('#mail-reload').disabled")

    def count(method):
        return s.js("return __mailWire.calls.filter(c=>c.method===arguments[0]).length", method)

    def hold(method):
        s.js("__mailWire.next=arguments[0]", method)

    def held():
        s.wait("!!__mailWire.held")

    def release():
        s.js("__mailWire.held.release()")

    def drop():
        s.js("__mailWire.held.drop()")

    def submit():
        s.click("#mail-save")

    def reload():
        s.click("#mail-reload")
        wait_result("saved")

    def stored():
        conn = db.connect(db_path)
        try:
            return json.loads(conn.execute("SELECT value FROM site_settings WHERE key='mail.smtp'").fetchone()[0])
        finally:
            conn.close()

    def switch_settings():
        s.click("#nav-calendar-settings")
        s.wait("location.hash==='#/settings'")
        s.click("#nav-mail-settings")
        s.wait("location.hash==='#/settings/mail'")

    # Fresh browser, no cookie: real anonymous protection.
    s.command("WebDriver:Navigate", {"url": base + "/todos/?race-anon=1#/settings/mail"})
    s.wait("String((performance.getEntriesByType('navigation')[0]||{}).name).includes('race-anon=1') && "
           "!!document.querySelector('#view-auth') && !document.querySelector('#view-auth').hidden")
    anon = s.async_js("const done=arguments[arguments.length-1];"
                      "fetch('./api/admin/mail-settings').then(r=>done(r.status));")
    check("本轮匿名 GET 仍为 401", anon == 401)
    s.command("WebDriver:AddCookie", {"cookie": {"name": "easylife_session", "value": admin_token,
                                                "path": "/todos", "domain": "127.0.0.1"}})
    results["response_fault_method"] = "Real HTTP completes, then fetch delivery is held/rejected; no fabricated responses"
    results["race_calls"] = []

    for label, width, height in (("desktop", 1440, 814), ("mobile", 375, 812)):
        viewport(width, height)
        s.js(WIRE)
        if label == "mobile":
            s.js("document.querySelector('#theme-select').value='dark';"
                 "document.querySelector('#theme-select').dispatchEvent(new Event('change'));")
        check(label + "：日历订阅入口仍可用",
              js_bool("!document.querySelector('#settings').hidden && document.querySelector('#feed-url').value.length>10"))
        layout(label + " 初始布局", width)
        secret1, secret2 = "browser-race-submitted", "browser-race-newer"
        set_values("127.0.0.1", smtp.port, "admin@example.com", "Saved <admin@example.com>", secret1)
        hold("PUT")
        submit()
        held()
        puts = count("PUT")
        s.js("document.querySelector('#mail-form').dispatchEvent(new Event('submit',{cancelable:true}));")
        check(label + "：保存中重复提交不新增PUT", count("PUT") == puts)
        edit("from", "New draft <draft@example.com>")
        edit("password", secret2)
        switch_settings()
        check(label + "：保存中切换日历邮件入口保留草稿及忙碌",
              js_bool("document.querySelector('#mail-password').value===arguments[0] && "
                      "document.querySelector('#mail-from').value==='New draft <draft@example.com>' && "
                      "document.querySelector('#mail-save').getAttribute('aria-busy')==='true'", secret2))
        check(label + "：页内切换没有新增PUT", count("PUT") == puts)
        release()
        wait_result("saved")
        check(label + "：迟到保存保留新的公开草稿和授权码",
              js_bool("document.querySelector('#mail-from').value==='New draft <draft@example.com>' && "
                      "document.querySelector('#mail-password').value===arguments[0]", secret2))
        check(label + "：数据库只有提交时快照",
              stored()["from"] == "Saved <admin@example.com>" and stored()["password"] == secret1)
        check(label + "：成功与未保存提示同时存在",
              js_bool("document.querySelector('#mail-status').dataset.state==='unsaved' && "
                      "document.querySelector('#mail-draft-status').textContent.includes('未保存') && "
                      "document.querySelector('#mail-result').textContent.includes('保存成功')"))
        capture("mail-race-save-" + label, width, height)
        tests_before = count("POST")
        s.click("#mail-test")
        check(label + "：新草稿阻止测试请求", count("POST") == tests_before)
        submit()
        wait_result("saved")
        check(label + "：再次显式保存才提交新授权码并清空输入",
              stored()["password"] == secret2 and js_bool("document.querySelector('#mail-password').value===''") )

        # SMTP receives the original recipient. Edit both the form and recipient while held.
        edit("test-to", label + "-original@example.com")
        hold("POST")
        smtp_before = len(smtp.received())
        s.click("#mail-test")
        held()
        check(label + "：延迟前真实SMTP已接受", wait_smtp(smtp_before + 1))
        tests_before = count("POST")
        s.js("document.querySelector('#mail-test').click()")
        edit("host", "later-draft.example.test")
        edit("test-to", "later@example.com")
        switch_settings()
        check(label + "：测试中切换入口保留未保存与忙碌",
              js_bool("document.querySelector('#mail-status').dataset.state==='unsaved' && "
                      "document.querySelector('#mail-test').disabled && "
                      "document.querySelector('#mail-result').dataset.state==='testing'"))
        release()
        wait_result("accepted")
        check(label + "：旧测试成功不会覆盖未保存",
              js_bool("document.querySelector('#mail-status').dataset.state==='unsaved' && "
                      "document.querySelector('#mail-draft-status').textContent.includes('未保存')"))
        check(label + "：测试结果绑定原收件地址与已保存主机",
              js_bool("document.querySelector('#mail-result').textContent.includes(arguments[0]) && "
                      "document.querySelector('#mail-result').textContent.includes('127.0.0.1') && "
                      "!document.querySelector('#mail-result').textContent.includes('later@example.com')",
                      label + "-original@example.com"))
        check(label + "：测试防重复且只传to",
              count("POST") == tests_before and js_bool("__mailWire.calls.filter(c=>c.method==='POST').every(c=>c.fields.join(',')==='to')"))
        capture("mail-race-test-" + label, width, height)
        reload()

        # PUT really commits. Drop its HTTP result, then also delay the automatic GET.
        edit("from", "Committed <committed@example.com>")
        edit("password", "browser-race-uncertain")
        hold("PUT")
        submit()
        held()
        check(label + "：丢响应前数据库已提交",
              stored()["from"] == "Committed <committed@example.com>" and stored()["password"] == "browser-race-uncertain")
        gets_before, puts_before = count("GET"), count("PUT")
        edit("from", "Latest <latest@example.com>")
        edit("password", "browser-race-latest")
        hold("GET")
        drop()
        held()
        check(label + "：丢PUT响应自动GET且结果未知",
              count("GET") == gets_before + 1 and js_bool("document.querySelector('#mail-result').textContent.includes('尚未确认')"))
        edit("user", "latest-user@example.com")
        switch_settings()
        check(label + "：核对中切换入口仍保留未知结果与新授权码",
              js_bool("document.querySelector('#mail-result').dataset.state==='unknown' && "
                      "document.querySelector('#mail-password').value==='browser-race-latest'"))
        release()
        wait_result("unknown")
        check(label + "：核对不覆盖后续草稿与新授权码",
              js_bool("document.querySelector('#mail-from').value==='Latest <latest@example.com>' && "
                      "document.querySelector('#mail-user').value==='latest-user@example.com' && "
                      "document.querySelector('#mail-password').value==='browser-race-latest'"))
        check(label + "：password_set不能确认本次授权码更新",
              js_bool("document.querySelector('#mail-password-state').textContent.includes('已设置') && "
                      "document.querySelector('#mail-result').textContent.includes('无法确认本次授权码')"))
        check(label + "：核对不自动重发PUT", count("PUT") == puts_before)
        capture("mail-race-reconcile-" + label, width, height)
        s.click("#mail-reload")
        wait_result("unknown")
        check(label + "：手动核对继续保留草稿",
              js_bool("document.querySelector('#mail-password').value==='browser-race-latest' && "
                      "document.querySelector('#mail-from').value==='Latest <latest@example.com>'"))
        submit()
        wait_result("saved")

        # SMTP receives exactly one message; lost HTTP result must stay unknown.
        hold("POST")
        smtp_before = len(smtp.received())
        s.click("#mail-test")
        held()
        check(label + "：丢测试响应前SMTP已接受", wait_smtp(smtp_before + 1))
        tests_before = count("POST")
        drop()
        wait_result("unknown")
        check(label + "：未知测试提示查收而不否认接受",
              js_bool("document.querySelector('#mail-result').textContent.includes('尚未确认') && "
                      "document.querySelector('#mail-result').textContent.includes('检查收件箱') && "
                      "!document.querySelector('#mail-result').textContent.includes('未被 SMTP 接受')"))
        check(label + "：未知测试未自动重发", count("POST") == tests_before and len(smtp.received()) == smtp_before + 1)
        layout(label + " 未知结果长文案", width)
        capture("mail-race-unknown-" + label, width, height)

        # Dispatch the browser lifecycle deterministically; this exercises real app + GET,
        # but does not claim Firefox chose to put a real navigation in its bfcache.
        edit("password", "browser-race-history")
        gets_before = count("GET")
        s.js("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));"
             "window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));")
        wait_result("saved")
        check(label + "：缓存恢复事件重新读取且秘密为空",
              count("GET") == gets_before + 1 and
              js_bool("document.querySelector('#mail-password').value==='' && "
                      "document.querySelector('#mail-host').value==='127.0.0.1'"))
        results["page_restore_method"] = "Controlled PageTransitionEvent persisted=true; real session/config GET; not a physical bfcache navigation"

        hold("GET")
        s.click("#mail-reload")
        held()
        edit("password", "browser-race-read-draft")
        switch_settings()
        release()
        wait_result("saved")
        check(label + "：读取中切换入口仍保留新草稿与未保存",
              js_bool("document.querySelector('#mail-password').value==='browser-race-read-draft' && "
                      "document.querySelector('#mail-status').dataset.state==='unsaved'"))
        reload()

        # A late read after leaving/returning cannot clear a new page's secret.
        hold("GET")
        s.click("#mail-reload")
        held()
        edit("password", "browser-race-leaving")
        s.js("location.hash='#/todos'")
        s.wait("!document.querySelector('#view-list').hidden")
        check(label + "：离开页面立即清空授权码", js_bool("document.querySelector('#mail-password').value===''") )
        s.js("location.hash='#/settings/mail'")
        wait_result("saved")
        edit("password", "browser-race-new-page")
        release()
        # A task turn + fetch to the real backend ensures the late callback has run.
        s.async_js("const done=arguments[arguments.length-1]; fetch('./api/me').then(()=>done(true));")
        check(label + "：旧页面读取不覆盖新页面授权码与未保存",
              js_bool("document.querySelector('#mail-password').value==='browser-race-new-page' && "
                      "document.querySelector('#mail-status').dataset.state==='unsaved'"))
        check(label + "：秘密未进入URL或浏览器存储",
              js_bool("![location.href,JSON.stringify(localStorage),JSON.stringify(sessionStorage)].some(v=>v.includes('browser-race-'))"))
        calls = s.js("return __mailWire.calls")
        check(label + "：写请求使用相对路径与CSRF",
              all(c["path"].startswith("./api/") and (c["method"] == "GET" or c["csrf"]) for c in calls))
        results["race_calls"].append({"viewport": [width, height], "calls": calls})

    # Same SPA, new login: don't let an old admin GET repopulate a member's hidden form.
    reload()
    hold("GET")
    s.click("#mail-reload")
    held()
    s.click("#btn-account")
    s.click("#btn-logout")
    s.wait("!document.querySelector('#view-auth').hidden")
    s.js("document.querySelector('#auth-email').value='member@example.com';"
         "document.querySelector('#auth-password').value='member-password-2026';")
    s.click("#auth-submit")
    s.wait("!document.querySelector('#view-list').hidden")
    s.js("location.hash='#/settings'")
    s.wait("!document.querySelector('#view-settings').hidden")
    release()
    s.async_js("const done=arguments[arguments.length-1]; fetch('./api/me').then(()=>done(true));")
    check("切换普通用户后迟到GET不恢复管理员配置",
          js_bool("document.querySelector('#mail-settings').hidden && document.querySelector('#nav-mail-settings').hidden && "
                  "document.querySelector('#mail-password').value==='' && document.querySelector('#mail-host').value===''"))
    check("切换普通用户后日历仍可用",
          js_bool("!document.querySelector('#settings').hidden && document.querySelector('#feed-url').value.length>10"))
    for method, suffix, payload in (("GET", "", None), ("PUT", "", {"host": "forbidden.test"}),
                                    ("POST", "/test", {"to": "member@example.com"})):
        # Member's real token + matching CSRF, so this is a permission rejection.
        status, me = request("GET", "/api/me", cookie=member_token)
        code, _ = request(method, "/api/admin/mail-settings" + suffix, payload,
                          cookie=member_token, csrf=me["csrf_token"])
        results["api_probes"].append({"method": method, "path": "mail-settings" + suffix, "status": code})
        check("本轮普通用户 " + method + suffix + " 越权返回403", status == 200 and code == 403)
    check("越权写入未落库", stored()["host"] != "forbidden.test")
    results["smtp_summary"] = {"messages_recorded": len(smtp.received()),
                               "auth_usernames": sorted({user for user, _ in smtp.auths()})}
