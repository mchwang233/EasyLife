#!/usr/bin/env node
/* 无浏览器前端交互回归验证。
 *
 * 用一份最小 DOM 桩把真实的 easylife/static/app.js 加载起来，然后模拟用户操作：
 *   列表 → 点击标题进入详情 → 点删除 → 确认 → 必须回到列表
 *   （之前只刷新了隐藏的列表，页面仍停在显示已删除任务的详情页）
 * 以及：
 *   → 详情状态被清空后，“保存 / 新增笔记”要给出提示而不是静默失败
 *   → 打开另一条任务时新增笔记必须真的写入时间线
 *
 * 只使用 Node 标准库，不需要网络、不需要 npm 安装任何东西。
 * 运行：node tests/frontend_interaction.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "easylife", "static", "index.html"), "utf8");
const APP_JS = fs.readFileSync(path.join(ROOT, "easylife", "static", "app.js"), "utf8");
const scenario = process.argv[2] || "regression";
const emailAuthScenario = scenario === "email-auth" || scenario === "no-smtp";
const uiScenario = scenario === "ui";
const mailRaceScenario = scenario === "admin-mail-races";
const adminMailScenario = scenario === "admin-mail" || mailRaceScenario;
const basePath = process.argv[3] || "/";
const browserBase = "http://127.0.0.1" + basePath;
const realDateNow = Date.now;
let clockOffset = 0;
Date.now = function () { return realDateNow() + clockOffset; };

/* ------------------------------------------------------------ 计时器
 * 包一层以便在结束前清掉全部挂起计时（toast 有 6 秒定时器），避免进程空转。 */
const realSetTimeout = global.setTimeout;
const realClearTimeout = global.clearTimeout;
const pendingTimers = new Set();
const scheduled = new Map();
global.setTimeout = function (fn, ms) {
  const extra = Array.prototype.slice.call(arguments, 2);
  const id = realSetTimeout(function () { pendingTimers.delete(id); fn.apply(null, extra); }, ms || 0);
  pendingTimers.add(id);
  scheduled.set(id, { fn: fn, due: Date.now() + (ms || 0) });
  return id;
};
global.clearTimeout = function (id) { pendingTimers.delete(id); scheduled.delete(id); realClearTimeout(id); };
function advanceTime(seconds) {
  clockOffset += seconds * 1000;
  Array.from(scheduled.entries()).forEach(function ([id, timer]) {
    if (timer.due <= Date.now()) { global.clearTimeout(id); timer.fn(); }
  });
}

function settle(rounds) {
  let promise = Promise.resolve();
  for (let i = 0; i < (rounds || 5); i += 1) {
    promise = promise.then(function () {
      return new Promise(function (resolve) { realSetTimeout(resolve, 0); });
    });
  }
  return promise;
}

/* ------------------------------------------------------------ 最小 DOM */

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.nodeType = 1;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.style = { setProperty: function () {}, removeProperty: function () {} };
    this.id = "";
    this.hidden = false;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.type = "";
    this.href = "";
    this.download = "";
    this._text = "";
    this._classes = [];
  }

  get textContent() { return this._text + this.childNodes.map(function (child) { return child.textContent; }).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get isConnected() { let node = this; while (node.parentNode) { node = node.parentNode; } return node === root; }

  get className() { return this._classes.join(" "); }
  set className(value) { this._classes = String(value === undefined || value === null ? "" : value).split(/\s+/).filter(Boolean); }

  get classList() {
    const self = this;
    return {
      add: function () { Array.prototype.forEach.call(arguments, function (name) { if (!self._classes.includes(name)) { self._classes.push(name); } }); },
      remove: function () { Array.prototype.forEach.call(arguments, function (name) { self._classes = self._classes.filter(function (item) { return item !== name; }); }); },
      contains: function (name) { return self._classes.includes(name); },
      toggle: function (name, force) {
        const on = force === undefined ? !self._classes.includes(name) : !!force;
        if (on) { if (!self._classes.includes(name)) { self._classes.push(name); } }
        else { self._classes = self._classes.filter(function (item) { return item !== name; }); }
        return on;
      }
    };
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") { this.className = value; }
    if (name === "id") { this.id = String(value); }
    if (name.startsWith("data-")) { this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = String(value); }
    if (["value", "type", "href"].includes(name)) { this[name] = String(value); }
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }

  get children() { return this.childNodes; }
  appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; }
  replaceChildren() { this.childNodes.forEach(function (child) { child.parentNode = null; }); this.childNodes = []; }
  remove() {
    if (this.parentNode) {
      this.parentNode.childNodes = this.parentNode.childNodes.filter((node) => node !== this);
      this.parentNode = null;
    }
  }

  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  removeEventListener() {}
  dispatch(type, extra) {
    if (type === "click" && this.disabled) { return; }
    const event = Object.assign({
      type: type,
      target: this,
      preventDefault: function () {},
      stopPropagation: function () {}
    }, extra || {});
    (this.listeners[type] || []).slice().forEach(function (handler) { handler(event); });
    return event;
  }
  click() { return this.dispatch("click"); }

  focus() { doc.activeElement = this; }
  scrollIntoView() {}
  select() {}
  reset() { this.querySelectorAll("input").concat(this.querySelectorAll("textarea")).forEach(function (child) { child.value = ""; }); }

  matches(selector) {
    const attribute = /^(\w+)?\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]$/.exec(selector);
    if (attribute) { return (!attribute[1] || this.tagName.toLowerCase() === attribute[1]) && this.getAttribute(attribute[2]) !== null && (attribute[3] === undefined || this.getAttribute(attribute[2]) === attribute[3]); }
    if (selector.charAt(0) === "#") { return this.id === selector.slice(1); }
    if (selector.charAt(0) === ".") { return this._classes.includes(selector.slice(1)); }
    return this.tagName.toLowerCase() === String(selector).toLowerCase();
  }
  querySelectorAll(selector) {
    const found = [];
    (function walk(node) {
      node.childNodes.forEach(function (child) {
        if (child.matches(selector)) { found.push(child); }
        walk(child);
      });
    })(this);
    return found;
  }
  querySelector(selector) { const all = this.querySelectorAll(selector); return all.length ? all[0] : null; }
  closest(selector) {
    let node = this;
    while (node) { if (node.matches && node.matches(selector)) { return node; } node = node.parentNode; }
    return null;
  }
}

/* 解析真实 HTML 的结构、属性和标签文本，缺失元素应直接导致失败，不能偷偷补 DOM。 */
const byId = new Map();
const root = new El("document");
const stack = [root];
const voidTags = new Set(["meta", "link", "input", "img", "br", "hr"]);
for (const part of HTML.match(/<!--[\s\S]*?-->|<![^>]*>|<[^>]+>|[^<]+/g)) {
  if (part.startsWith("<!")) { continue; }
  if (part.startsWith("</")) { stack.pop(); continue; }
  if (part.startsWith("<")) {
    const tag = /^<([\w-]+)/.exec(part)[1].toLowerCase();
    const node = new El(tag);
    for (const attr of part.slice(tag.length + 1).matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      node.setAttribute(attr[1], attr[2] || "");
      if (["hidden", "disabled", "required", "checked"].includes(attr[1])) { node[attr[1]] = true; }
    }
    stack[stack.length - 1].appendChild(node);
    if (node.id) { byId.set(node.id, node); }
    if (!voidTags.has(tag)) { stack.push(node); }
  } else {
    const node = new El("text"); node.nodeType = 3; node.textContent = part;
    stack[stack.length - 1].appendChild(node);
  }
}
const body = root.querySelector("body");
const documentListeners = {};
const doc = {
  readyState: "complete", baseURI: browserBase, body: body,
  documentElement: root.querySelector("html"), activeElement: body,
  createElement: function (tag) { return new El(tag); },
  querySelector: function (selector) { return root.querySelector(selector); },
  getElementById: function (id) { return doc.querySelector("#" + id); },
  querySelectorAll: function (selector) { return root.querySelectorAll(selector); },
  addEventListener: function (type, handler) { (documentListeners[type] = documentListeners[type] || []).push(handler); },
  execCommand: function () { return true; }
};

const windowListeners = {};
let currentHash = emailAuthScenario ? "#/verify?token=expired-start" : "";
const locationStub = {
  href: browserBase,
  protocol: "http:",
  pathname: basePath,
  get hash() { return currentHash; },
  set hash(value) {
    const next = String(value);
    if (next === currentHash) { return; }
    currentHash = next;
    (windowListeners.hashchange || []).slice().forEach(function (handler) { handler({ type: "hashchange" }); });
  }
};
const winStub = {
  addEventListener: function (type, handler) { (windowListeners[type] = windowListeners[type] || []).push(handler); },
  removeEventListener: function () {},
  scrollTo: function () {},
  location: locationStub
};

/* ------------------------------------------------------------ fetch 桩 */

const NOW = "2026-01-01 10:00:00";
let todos = [
  {
    id: 1, title: "写周报", details: "本周进展", status: "open", priority: 2,
    due_at: null, remind_at: null, reminder_sent: 0, notes_count: 0, notes: [],
    created_at: NOW, updated_at: NOW,
    ics_url: "/api/todos/1/ics",
    google_calendar_url: "https://calendar.google.com/calendar/render?action=TEMPLATE&text=%E5%86%99%E5%91%A8%E6%8A%A5"
  },
  {
    id: 2, title: "买牛奶", details: "", status: "open", priority: 1,
    due_at: null, remind_at: null, reminder_sent: 0, notes_count: 0, notes: [],
    created_at: NOW, updated_at: NOW,
    ics_url: "/api/todos/2/ics",
    google_calendar_url: ""
  }
];

const requests = [];
/* 设为 404 时，下一次 DELETE 会返回 404（模拟“任务已在另一个标签页被删除”）。 */
let deleteStatusOverride = null;
let deferDetail = false;
let finishDetail = null;
let detailStatusOverride = null;
let listFailure = false;
let deferList = false;
let finishList = null;
let createFailure = false;
/* 列表接口默认 404（老版本服务器）；置为 true 后返回两个列表。 */
let listsAvailable = false;

function jsonResponse(status, payload) {
  payload = JSON.parse(JSON.stringify(payload)); // freeze the response at request completion
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: { get: function (name) { return String(name).toLowerCase() === "content-type" ? "application/json; charset=utf-8" : String(name).toLowerCase() === "retry-after" ? "90" : null; } },
    // HTTP JSON creates a fresh object; sharing the server object's identity hid UI bugs.
    json: function () { return Promise.resolve(JSON.parse(JSON.stringify(payload))); },
    text: function () { return Promise.resolve(JSON.stringify(payload)); }
  };
}

function notFound() {
  return jsonResponse(404, { error: { code: "not_found", message: "任务不存在" } });
}

const absoluteRequests = [];
let emailVerified = false;
let emailLoggedIn = false;
let recoveryRateLimited = false;
let verificationNetworkFailure = false;
let loginUnauthorized = false;
let listPageSize = 200;
let feedResets = 0;

/* 管理员邮件设置（scenario admin-mail）：保存状态、限流、失败与网络错误开关。 */
let mailCfg = {
  mail: { enabled: false, host: "", port: 587, user: "", from: "no-reply@localhost", password_set: false },
  source: "defaults", password_source: "none", updated_at: null, updated_by: null
};
let mailSaved = null;
let mailTestBody = null;
let mailGetStatus = 0;
let mailPutStatus = 0;
let mailPutDefer = false;
let finishMailPut = null;
let mailTestStatus = 200;
let mailTestDefer = false;
let finishMailTest = null;
let mailTestNetworkFail = false;
let mailGetNetworkFail = false;
let mailHoldNext = null;
let mailBodyLossNext = null;
const mailHeld = [];
let mailAccepted = 0;
let mailSessionId = 1;

// Deliver/drop a completed response on demand; the PUT/SMTP effect already happened.
function mailReply(method, response) {
  if (mailBodyLossNext === method) {
    mailBodyLossNext = null;
    response.json = () => Promise.reject(new TypeError("response body truncated"));
  }
  if (mailHoldNext === method) {
    mailHoldNext = null;
    return new Promise((resolve, reject) => mailHeld.push({
      release: () => resolve(response),
      drop: () => reject(new TypeError("response lost"))
    }));
  }
  return Promise.resolve(response);
}

function fakeFetch(url, options) {
  const method = (options && options.method) || "GET";
  const body = options && options.body ? options.body : null;
  // 前端用 "./api/..." 相对路径；这里按页面位于 "/" 时浏览器的解析方式还原成 "/api/..."。
  if (String(url).charAt(0) === "/") { absoluteRequests.push(String(url)); }
  const absolute = new URL(url, browserBase);
  const resolved = "/" + absolute.pathname.slice(basePath.length) + absolute.search;
  requests.push({ raw: String(url), resolvedPath: absolute.pathname, method: method, url: resolved, body: body ? JSON.parse(body) : null,
    headers: (options && options.headers) || {} });
  const route = resolved.split("?")[0];
  let hit;

  if (emailAuthScenario) {
    if (route === "/api/me" && !emailLoggedIn) {
      return Promise.resolve(jsonResponse(401, { error: { code: "unauthorized", message: "请登录" } }));
    }
    if (route === "/api/register" && scenario === "no-smtp") {
      emailLoggedIn = true;
      return Promise.resolve(jsonResponse(201, { user: { id: 1, email: "user@example.com", email_verified: false }, csrf_token: "csrf-token-1" }));
    }
    if (route === "/api/register") {
      return Promise.resolve(jsonResponse(201, {
        user: { id: 1, email: "user@example.com", email_verified: false },
        verification_required: true, message: "注册成功，请验证邮箱后登录。"
      }));
    }
    if (route === "/api/login") {
      if (loginUnauthorized) { return Promise.resolve(jsonResponse(401, { error: { code: "unauthorized", message: "邮箱或密码错误" } })); }
      if (!emailVerified) {
        return Promise.resolve(jsonResponse(403, { error: { code: "email_not_verified", message: "邮箱尚未验证" } }));
      }
      emailLoggedIn = true;
      return Promise.resolve(jsonResponse(200, {
        user: { id: 1, email: "user@example.com", email_verified: true }, csrf_token: "csrf-token-1"
      }));
    }
    if (route === "/api/resend-verification" || route === "/api/forgot-password") {
      if (recoveryRateLimited) {
        return Promise.resolve(jsonResponse(429, { error: { code: "rate_limited", message: "请求过于频繁，请稍后再试" } }));
      }
      return Promise.resolve(jsonResponse(200, { ok: true, message: "如果邮箱符合条件，请检查收件箱。" }));
    }
    if (route === "/api/verify-email") {
      if (verificationNetworkFailure) { return Promise.reject(new Error("offline")); }
      if (JSON.parse(body).token !== "verification-link") {
        return Promise.resolve(jsonResponse(400, { error: { code: "invalid_token", message: "链接无效或已过期，请重新获取" } }));
      }
      emailVerified = true;
      return Promise.resolve(jsonResponse(200, { ok: true, message: "邮箱验证成功，请登录。" }));
    }
    if (route === "/api/reset-password") {
      if (JSON.parse(body).token !== "reset-link") { return Promise.resolve(jsonResponse(400, { error: { code: "invalid_token", message: "链接无效或已过期，请重新获取" } })); }
      emailLoggedIn = false;
      return Promise.resolve(jsonResponse(200, { ok: true, message: "密码已重置，请使用新密码重新登录。" }));
    }
  }

  if (route === "/api/admin/mail-settings" && method === "GET") {
    if (mailGetNetworkFail) { return Promise.reject(new TypeError("offline")); }
    if (mailGetStatus) {
      const status = mailGetStatus;
      return mailReply("GET", jsonResponse(status, {
        error: { code: status === 403 ? "forbidden" : status === 401 ? "unauthorized" : "error",
                 message: status === 403 ? "需要管理员权限" : "请登录" }
      }));
    }
    return mailReply("GET", jsonResponse(200, mailCfg));
  }
  if (route === "/api/admin/mail-settings" && method === "PUT") {
    if (mailPutStatus) {
      const status = mailPutStatus;
      mailPutStatus = 0;
      return Promise.resolve(jsonResponse(status, { error: { code: "bad_request", message: "请求无效" } }));
    }
    mailSaved = JSON.parse(body);
    const mail = mailCfg.mail;
    mail.enabled = !!mailSaved.enabled;
    if ("host" in mailSaved) { mail.host = mailSaved.host; }
    if ("port" in mailSaved) { mail.port = mailSaved.port; }
    if ("user" in mailSaved) { mail.user = mailSaved.user; }
    if ("from" in mailSaved) { mail.from = mailSaved.from; }
    if (mailSaved.password) { mail.password_set = true; mailCfg.password_source = "database"; }
    if (mailSaved.clear_password) { mail.password_set = false; mailCfg.password_source = "none"; }
    mail.enabled = mail.enabled && !!mail.host;
    mailCfg.source = "database";
    mailCfg.updated_at = "2026-01-01 10:05:00";
    mailCfg.updated_by = "user@example.com";
    const savedResponse = jsonResponse(200, mailCfg);
    if (mailPutDefer) { mailPutDefer = false; return new Promise(resolve => { finishMailPut = () => resolve(savedResponse); }); }
    return mailReply("PUT", savedResponse);
  }
  if (route === "/api/admin/mail-settings/test" && method === "POST") {
    mailTestBody = JSON.parse(body);
    if (mailTestNetworkFail) { return Promise.reject(new Error("offline")); }
    if (mailTestStatus !== 200) {
      const messages = {
        429: "测试发信过于频繁，请稍后再试", 409: "邮件服务未配置或已停用，请先保存有效配置",
        502: "邮件服务器连接、认证或投递失败，请检查已保存配置", 403: "需要管理员权限"
      };
      return Promise.resolve(jsonResponse(mailTestStatus, {
        error: { code: mailTestStatus === 429 ? "rate_limited" : "mail_delivery_failed",
                 message: messages[mailTestStatus] || "测试失败" }
      }));
    }
    const testResponse = jsonResponse(200, {
      ok: true, sent: true, to: mailTestBody.to || "user@example.com",
      message: "SMTP 服务器已接受测试邮件，请检查收件箱；这不代表邮件已送达。", latency_ms: 12
    });
    mailAccepted += 1;
    if (mailTestDefer) { mailTestDefer = false; return new Promise(resolve => { finishMailTest = () => resolve(testResponse); }); }
    return mailReply("POST", testResponse);
  }
  if (adminMailScenario && route === "/api/login") {
    return Promise.resolve(jsonResponse(200, { user: { id: mailSessionId, email: "user@example.com", is_admin: true }, csrf_token: "csrf-token-" + mailSessionId }));
  }
  if (route === "/api/me" && method === "GET") {
    return mailReply("ME", jsonResponse(200, {
      user: { id: mailSessionId, email: "user@example.com", email_verified: emailAuthScenario ? emailVerified : !uiScenario, is_admin: adminMailScenario },
      csrf_token: "csrf-token-" + mailSessionId,
      calendar_feed_url: "http://127.0.0.1:8080/api/calendar.ics?token=secret"
    }));
  }
  if (route === "/api/lists" && method === "GET") {
    if (!listsAvailable) { return Promise.resolve(notFound()); }
    return Promise.resolve(jsonResponse(200, { items: [
      { id: 1, slug: "tasks", name: "EasyLife", color: "#426557", sort_order: 0, is_default: true },
      { id: 2, slug: "home", name: "家里", color: "#c0392b", sort_order: 1, is_default: false }
    ] }));
  }
  if (route === "/api/todos" && method === "GET") {
    if (listFailure) { return Promise.reject(new Error("offline")); }
    const status = absolute.searchParams.get("status") || "all";
    const filtered = todos.filter(todo => status === "all" ? todo.status !== "archived" : todo.status === status);
    const offset = Number(absolute.searchParams.get("offset") || 0);
    const response = jsonResponse(200, { items: filtered.slice(offset, offset + listPageSize), total: filtered.length });
    if (deferList) { deferList = false; return new Promise(resolve => { finishList = () => resolve(response); }); }
    return Promise.resolve(response);
  }
  if (route === "/api/todos" && method === "POST") {
    if (createFailure) { return Promise.reject(new Error("offline")); }
    const todo = Object.assign({ id: 100, status: "open", notes: [], notes_count: 0, created_at: NOW, updated_at: NOW }, JSON.parse(body));
    todos.push(todo); return Promise.resolve(jsonResponse(201, todo));
  }
  if (route === "/api/resend-verification") { return Promise.resolve(jsonResponse(200, { ok: true, message: "请查收验证邮件" })); }
  if (route === "/api/logout") { emailLoggedIn = false; return Promise.resolve(jsonResponse(200, { ok: true })); }
  if (route === "/api/calendar/token/reset") {
    feedResets += 1;
    return Promise.resolve(jsonResponse(200, { calendar_feed_url: browserBase + "api/calendar.ics?token=new-secret" }));
  }
  if (/^\/api\/todos\/\d+\/ics$/.test(route)) {
    return Promise.resolve({ status: 200, ok: true, text: () => Promise.resolve("BEGIN:VCALENDAR\r\nEND:VCALENDAR") });
  }
  if ((hit = /^\/api\/todos\/(\d+)$/.exec(route))) {
    const wanted = todos.filter(function (todo) { return todo.id === Number(hit[1]); })[0];
    if (!wanted) { return Promise.resolve(notFound()); }
    if (method === "GET") {
      const response = detailStatusOverride === 404 ? notFound() : jsonResponse(200, wanted);
      detailStatusOverride = null;
      if (deferDetail) {
        deferDetail = false;
        return new Promise(resolve => { finishDetail = () => resolve(response); });
      }
      return Promise.resolve(response);
    }
    if (method === "DELETE") {
      if (deleteStatusOverride) {
        const status = deleteStatusOverride;
        deleteStatusOverride = null;
        return Promise.resolve(jsonResponse(status, { error: { code: "not_found", message: "任务不存在" } }));
      }
      todos = todos.filter(function (todo) { return todo.id !== wanted.id; });
      return Promise.resolve(jsonResponse(200, { ok: true, deleted: wanted.id }));
    }
    if (method === "PUT") {
      Object.assign(wanted, body ? JSON.parse(body) : {});
      return Promise.resolve(jsonResponse(200, wanted));
    }
  }
  if ((hit = /^\/api\/todos\/(\d+)\/notes$/.exec(route)) && method === "POST") {
    const wanted = todos.filter(function (todo) { return todo.id === Number(hit[1]); })[0];
    if (!wanted) { return Promise.resolve(notFound()); }
    const note = { id: wanted.notes.length + 1, content: JSON.parse(body).content, created_at: NOW };
    wanted.notes = wanted.notes.concat([note]);
    wanted.notes_count = wanted.notes.length;
    return Promise.resolve(jsonResponse(201, note));
  }
  return Promise.resolve(notFound());
}

/* ------------------------------------------------------------ 加载真实前端代码 */

global.document = doc;
global.window = winStub;
global.location = locationStub;
// Node 22 把 navigator 定义成只读 getter，用 defineProperty 覆盖。
Object.defineProperty(global, "navigator", { value: {}, writable: true, configurable: true });
global.fetch = fakeFetch;
global.Blob = function Blob(parts, opts) { this.parts = parts; this.type = (opts || {}).type; };
URL.createObjectURL = function () { return "blob:stub"; };
URL.revokeObjectURL = function () {};
const stored = new Map(uiScenario ? [["easylife-theme", "dark"]] : []);
let storageBlocked = false;
global.localStorage = {
  getItem: key => stored.get(key) || null,
  setItem: (key, value) => { if (storageBlocked) { throw new Error("storage blocked"); } stored.set(key, value); }
};

vm.runInThisContext(APP_JS, { filename: "easylife/static/app.js" });

/* ------------------------------------------------------------ 断言工具 */

const results = [];
function check(name, condition, detail) {
  results.push({ name: name, ok: !!condition, detail: detail === undefined ? "" : String(detail) });
}

function view(name) { return byId.get("view-" + name).hidden === false; }
function todoNodes() { return byId.get("todo-list").childNodes; }
function textOf(node) {
  if (!node) { return ""; }
  if (!node.childNodes || node.childNodes.length === 0) { return node.textContent || ""; }
  return node.childNodes.map(textOf).join(" ");
}
function find(node, predicate) {
  if (!node) { return null; }
  if (predicate(node)) { return node; }
  for (const child of node.childNodes || []) {
    const hit = find(child, predicate);
    if (hit) { return hit; }
  }
  return null;
}
function titleButton(index) {
  return find(todoNodes()[index], function (node) { return node.classList.contains("todo-title"); });
}

function countRequests(path) { return requests.filter(request => request.url.split("?")[0] === path).length; }
function fillAuth() { byId.get("auth-email").value = "user@example.com"; byId.get("auth-password").value = "correct-horse-battery"; }
/* UTC "YYYY-MM-DD HH:MM:SS" -> 本地 "YYYY-MM-DDTHH:MM"，与页面里的换算一致。 */
function toLocal(value) {
  const date = new Date(value.replace(" ", "T") + "Z");
  const pad = n => String(n).padStart(2, "0");
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

function clickFilter(status) {
  const chip = doc.querySelector('[data-status="' + status + '"]');
  byId.get("filters").dispatch("click", { target: chip });
}

async function emailAuthFlow() {
  await settle(8);
  check("未登录启动保留邮件验证路由", currentHash === "#/verify?token=expired-start" && view("verify"));
  check("验证链接自动POST且GET不消费令牌", requests.some(request => request.url === "/api/verify-email" && request.method === "POST") && !requests.some(request => request.url === "/api/verify-email" && request.method === "GET"));
  check("自动验证显示过期提示", byId.get("verify-title").textContent.includes("已过期"));
  locationStub.hash = "#/register";
  fillAuth(); byId.get("auth-form").dispatch("submit");
  check("注册提交时显示loading", byId.get("auth-submit").disabled && byId.get("auth-submit").textContent.includes("正在"));
  await settle(8);
  check("注册后展示查收邮件页", view("check-email") && currentHash === "#/check-email" && byId.get("check-email-address").textContent === "user@example.com");
  check("注册后重发倒计时60秒", byId.get("check-email-resend").disabled && byId.get("check-email-resend").textContent.includes("60 秒"));
  check("待验证注册不进入任务列表", countRequests("/api/todos") === 0);
  const beforeCooldown = countRequests("/api/resend-verification");
  byId.get("check-email-resend").dispatch("click"); await settle(3);
  check("倒计时内不重复发送", countRequests("/api/resend-verification") === beforeCooldown);
  advanceTime(59);
  check("倒计时到1秒仍禁止重发", byId.get("check-email-resend").disabled && byId.get("check-email-resend").textContent.includes("1 秒"));
  advanceTime(2);
  check("倒计时结束可重发", !byId.get("check-email-resend").disabled);
  byId.get("check-email-resend").dispatch("click"); await settle(6);
  check("查收页重发携带注册邮箱", requests.some(request => request.url === "/api/resend-verification" && request.body.email === "user@example.com"));
  if (scenario === "no-smtp") {
    check("无SMTP注册保留会话并提示未验证", !byId.get("verification-banner").hidden && !byId.get("account-nav").hidden && byId.get("check-email-continue").href === "#/todos");
    locationStub.hash = "#/todos"; await settle(8);
    check("无SMTP注册可以继续使用待办", view("list") && countRequests("/api/todos") > 0);
    check("邮箱认证交互完成", true); return;
  }
  locationStub.hash = "#/login";
  fillAuth(); byId.get("auth-form").dispatch("submit"); await settle(8);
  check("未验证登录提示并提供重新发送", byId.get("auth-error").textContent.includes("尚未验证") && !byId.get("auth-resend").hidden);
  advanceTime(61); recoveryRateLimited = true;
  byId.get("auth-resend").dispatch("click"); await settle(8);
  check("重发限流按Retry-After等待", byId.get("toast").textContent.includes("过于频繁") && byId.get("auth-resend").disabled && byId.get("auth-resend").textContent.includes("90 秒"));
  recoveryRateLimited = false; advanceTime(91);
  byId.get("auth-resend").dispatch("click"); await settle(8);
  check("重发限流结束可以重试", countRequests("/api/resend-verification") === 3);
  loginUnauthorized = true;
  fillAuth(); byId.get("auth-form").dispatch("submit"); await settle(8);
  check("错误密码在登录表单内显示错误", view("auth") && byId.get("auth-error").textContent.includes("密码错误") && !byId.get("auth-submit").disabled);
  loginUnauthorized = false;
  byId.get("auth-forgot").dispatch("click"); await settle(4);
  check("忘记密码独立视图只需邮箱", view("forgot") && byId.get("view-reset").hidden);
  byId.get("forgot-form").dispatch("submit");
  check("忘记密码提交时显示loading", byId.get("forgot-submit").disabled);
  await settle(8);
  const generic = "如果该邮箱已注册，我们已发送重置链接";
  check("忘记密码发送邮箱并显示统一提示", requests.some(request => request.url === "/api/forgot-password" && request.body.email === "user@example.com") && byId.get("forgot-message").textContent === generic);
  byId.get("recovery-email").value = "unknown@example.com";
  byId.get("forgot-form").dispatch("submit"); await settle(8);
  check("不存在邮箱与已注册邮箱提示一致", byId.get("forgot-message").textContent === generic);
  recoveryRateLimited = true; byId.get("forgot-form").dispatch("submit"); await settle(6);
  check("忘记密码限流错误可重试", byId.get("forgot-message").textContent.includes("过于频繁") && !byId.get("forgot-submit").disabled);
  recoveryRateLimited = false;
  locationStub.hash = "#/verify"; await settle(4);
  check("缺失验证令牌显示恢复入口", byId.get("verify-message").textContent.includes("缺少令牌") && !byId.get("verify-resend-link").hidden);
  verificationNetworkFailure = true;
  locationStub.hash = "#/verify?token=verification-link"; await settle(8);
  check("验证网络错误提供重试", !byId.get("verify-retry").hidden && byId.get("verify-message").textContent.includes("网络"));
  verificationNetworkFailure = false;
  byId.get("verify-retry").dispatch("click"); await settle(8);
  check("验证成功保留结果视图", view("verify") && byId.get("verify-title").textContent.includes("成功") && !byId.get("verify-continue").hidden);
  const verifiedCalls = countRequests("/api/verify-email");
  locationStub.hash = "#/login"; locationStub.hash = "#/verify?token=verification-link"; await settle(4);
  check("重复进入成功视图不消费同一令牌", countRequests("/api/verify-email") === verifiedCalls);
  locationStub.hash = "#/login";
  fillAuth(); byId.get("auth-form").dispatch("submit"); await settle(8);
  check("验证后登录进入任务列表", view("list") && currentHash === "#/todos");
  check("已验证用户不显示提示条", byId.get("verification-banner").hidden);
  locationStub.hash = "#/reset"; await settle(4);
  check("缺失重置令牌禁止提交并提供恢复", byId.get("reset-submit").disabled && !byId.get("reset-request-link").hidden);
  locationStub.hash = "#/reset?token=expired-link";
  byId.get("recovery-password").value = "new-correct-horse-battery";
  byId.get("recovery-confirm").value = "new-correct-horse-battery";
  byId.get("reset-form").dispatch("submit"); await settle(8);
  check("过期重置链接显示重新获取入口", view("reset") && byId.get("reset-message").textContent.includes("已过期") && !byId.get("reset-request-link").hidden);
  locationStub.hash = "#/reset?token=reset-link"; await settle(4);
  check("已登录仍可打开邮件重置路由", view("reset"));
  const beforeInvalid = countRequests("/api/reset-password");
  byId.get("recovery-password").value = "short";
  byId.get("reset-form").dispatch("submit"); await settle(4);
  check("短密码在客户端阻止提交", countRequests("/api/reset-password") === beforeInvalid && byId.get("reset-message").textContent.includes("8–128"));
  byId.get("recovery-password").value = "new-correct-horse-battery";
  byId.get("recovery-confirm").value = "different-password";
  byId.get("reset-form").dispatch("submit"); await settle(4);
  check("确认密码不一致阻止提交", countRequests("/api/reset-password") === beforeInvalid && byId.get("reset-message").textContent.includes("不一致"));
  byId.get("recovery-confirm").value = "new-correct-horse-battery";
  byId.get("reset-form").dispatch("submit"); await settle(8);
  check("重置提交新密码令牌及会话CSRF", requests.some(request => request.url === "/api/reset-password" && request.body.token === "reset-link" && request.body.password === "new-correct-horse-battery" && request.headers["X-CSRF-Token"] === "csrf-token-1"));
  check("重置成功清空密码并要求重新登录", currentHash === "#/login" && view("auth") && byId.get("recovery-password").value === "" && byId.get("recovery-confirm").value === "" && byId.get("account-nav").hidden);
  check("所有认证请求使用相对路径", absoluteRequests.length === 0 && requests.every(request => request.raw.startsWith("./api/") && request.resolvedPath.startsWith(basePath + "api/")));
  check("邮箱认证交互完成", true);
}

async function uiFlow() {
  await settle(8);
  const sections = doc.querySelector(".workspace-grid").childNodes.filter(node => node.tagName === "SECTION");
  check("快速添加位于列表上方且DOM顺序自洽", sections[0].classList.contains("composer") && sections[1].classList.contains("tasks-card"));
  check("可选字段默认折叠并关联开关", byId.get("create-options").hidden && byId.get("create-options-toggle").getAttribute("aria-expanded") === "false" && byId.get("create-options-toggle").getAttribute("aria-controls") === "create-options");
  byId.get("btn-new").dispatch("click");
  check("首屏新建入口聚焦标题", doc.activeElement === byId.get("new-title"));
  byId.get("create-options-toggle").dispatch("click");
  check("展开保留可选字段", !byId.get("create-options").hidden && ["new-details", "new-priority", "new-due", "new-early", "new-repeat", "new-flagged", "new-urgent", "new-tags-entry"].every(id => byId.get(id).closest("#create-options")) && byId.get("create-options-toggle").getAttribute("aria-expanded") === "true");
  check("未设截止时间也显示提前提醒和重复", !byId.get("new-time-options").hidden && byId.get("new-due").value === "");
  doc.querySelector('[data-for="new-repeat"]').childNodes.filter(b => b.dataset.value === "FREQ=DAILY")[0].dispatch("click");
  check("先选重复会自动设好截止时间", byId.get("new-repeat").value === "FREQ=DAILY" && /^\d{4}-\d{2}-\d{2}T\d{2}:00$/.test(byId.get("new-due").value) && byId.get("toast").textContent.includes("截止时间"));
  byId.get("new-due").value = ""; byId.get("new-due").dispatch("change");
  doc.querySelector('[data-for="new-due"]').childNodes.filter(b => b.classList.contains("dt-clear"))[0].dispatch("click");
  check("清除截止时间时一起清掉重复和提前提醒", byId.get("new-repeat").value === "" && byId.get("new-early").value === "");
  doc.querySelector('[data-for="new-due"]').childNodes.filter(b => b.classList.contains("dt-trigger"))[0].dispatch("click");
  doc.querySelector("#dt-panel").querySelectorAll("button").filter(b => b.textContent === "完成")[0].dispatch("click");
  check("没动默认 09:00 直接点完成也会设好截止时间", /T09:00$/.test(byId.get("new-due").value) && doc.querySelector("#dt-panel").hidden, byId.get("new-due").value);
  byId.get("new-due").value = "";
  byId.get("new-details").value = "折叠后保留的草稿";
  byId.get("create-options-toggle").dispatch("click");
  check("收起不丢失已填内容", byId.get("new-details").value === "折叠后保留的草稿");
  byId.get("btn-account").dispatch("click");
  check("账户开关可展开统一设置入口", !byId.get("account-panel").hidden && byId.get("btn-account").getAttribute("aria-expanded") === "true");
  documentListeners.keydown.forEach(fn => fn({ key: "Escape", preventDefault() {} }));
  check("账户Escape收起并恢复焦点", byId.get("account-panel").hidden && doc.activeElement === byId.get("btn-account"));
  deferList = true; clickFilter("open"); await settle(2);
  check("加载时明确反馈且隐藏旧筛选结果", !byId.get("list-loading").hidden && byId.get("todo-list").hidden && byId.get("todo-list").getAttribute("aria-busy") === "true" && byId.get("list-empty").hidden);
  finishList(); await settle(6);
  listFailure = true; clickFilter("all"); await settle(6);
  check("加载失败显示持久重试且不伪装为空列表", !byId.get("list-error").hidden && byId.get("list-error-message").textContent.includes("网络") && byId.get("list-empty").hidden);
  listFailure = false; byId.get("list-retry").dispatch("click"); await settle(6);
  check("重试恢复列表和焦点", byId.get("list-error").hidden && !byId.get("todo-list").hidden && doc.activeElement === byId.get("list-heading"));
  check("启动恢复手动主题", doc.documentElement.getAttribute("data-theme") === "dark");
  byId.get("theme-select").value = "light"; byId.get("theme-select").dispatch("change");
  check("手动主题持久化", doc.documentElement.getAttribute("data-theme") === "light" && stored.get("easylife-theme") === "light");
  byId.get("theme-select").value = "system"; byId.get("theme-select").dispatch("change");
  check("可恢复跟随系统主题", doc.documentElement.getAttribute("data-theme") === null);
  storageBlocked = true; byId.get("theme-select").value = "dark"; byId.get("theme-select").dispatch("change");
  check("存储不可用仍可切换主题", doc.documentElement.getAttribute("data-theme") === "dark");
  check("登录后顶部显示用户名和未验证提示", byId.get("me-email").textContent === "user@example.com" && !byId.get("verification-banner").hidden);
  byId.get("banner-resend").dispatch("click"); await settle(8);
  check("顶部重发携带CSRF", requests.some(request => request.url === "/api/resend-verification" && request.headers["X-CSRF-Token"] === "csrf-token-1"));
  byId.get("new-title").value = "<img src=x onerror=alert(1)>";
  byId.get("new-priority").value = "3"; byId.get("new-details").value = "提醒测试";
  byId.get("new-due").value = "2026-10-20T09:30"; byId.get("new-early").value = "30";
  createFailure = true; byId.get("create-form").dispatch("submit"); await settle(6);
  check("新建失败保留全部输入并提供行内错误", byId.get("new-title").value.includes("<img") && byId.get("new-priority").value === "3" && byId.get("new-due").value && byId.get("new-early").value === "30" && byId.get("toast").closest("#create-feedback") && !byId.get("create-submit").disabled);
  createFailure = false;
  byId.get("create-options-toggle").dispatch("click");
  const createsBefore = requests.filter(r => r.url === "/api/todos" && r.method === "POST").length;
  byId.get("create-form").dispatch("submit");
  byId.get("create-form").dispatch("submit");
  check("新建提交中防止重复请求", byId.get("create-submit").disabled && byId.get("create-submit").getAttribute("aria-busy") === "true" && requests.filter(r => r.url === "/api/todos" && r.method === "POST").length === createsBefore + 1);
  await settle(8);
  check("新建成功重置并折叠选项", byId.get("new-title").value === "" && byId.get("new-details").value === "" && byId.get("new-priority").value === "2" && byId.get("create-options").hidden && doc.activeElement === byId.get("new-title"));
  check("创建任务保留提醒和截止时间", requests.some(request => request.method === "POST" && request.url === "/api/todos" && request.body.due_at && request.body.remind_at && request.body.priority === 3));
  check("任务标题按纯文本渲染", textOf(byId.get("todo-list")).includes("<img src=x onerror=alert(1)>") && !byId.get("todo-list").querySelector("img"));
  const checkbox = doc.querySelector("#todo-check-1"); checkbox.checked = true; checkbox.dispatch("change"); await settle(8);
  check("完成任务仍调用PUT", todos[0].status === "done" && doc.querySelector("#todo-check-1").checked);
  check("勾选后恢复键盘焦点", doc.activeElement === doc.querySelector("#todo-check-1"));
  function utc(days, hour) { const value = new Date(); value.setDate(value.getDate() + days); value.setHours(hour, 0, 0, 0); return value.toISOString().slice(0, 19).replace("T", " "); }
  const template = Object.assign({}, todos[1]);
  todos = [
    Object.assign({}, template, { id: 11, title: "今天零点", due_at: utc(0, 0) }),
    Object.assign({}, template, { id: 12, title: "今天晚间", due_at: utc(0, 23), status: "doing" }),
    Object.assign({}, template, { id: 13, title: "七天内", due_at: utc(7, 23) }),
    Object.assign({}, template, { id: 14, title: "八天后", due_at: utc(8, 0) }),
    Object.assign({}, template, { id: 15, title: "已完成", due_at: utc(0, 10), status: "done" }),
    Object.assign({}, template, { id: 16, title: "昨日逾期", due_at: utc(-1, 23) }),
    Object.assign({}, template, { id: 17, title: "已归档", due_at: utc(0, 10), status: "archived" })
  ];
  listPageSize = 2;
  clickFilter("today"); await settle(12);
  check("今天筛选覆盖本地零点且排除已完成", todoNodes().length === 2 && textOf(byId.get("todo-list")).includes("今天零点") && !textOf(byId.get("todo-list")).includes("已完成"));
  clickFilter("upcoming"); await settle(12);
  check("即将到期跨分页且仅未来七天", todoNodes().length === 1 && textOf(byId.get("todo-list")).includes("七天内"));
  check("筛选按钮提供按下状态", doc.querySelector('[data-status="upcoming"]').getAttribute("aria-pressed") === "true");
  clickFilter("done"); await settle(8);
  check("已完成筛选保留", todoNodes().length === 1 && textOf(byId.get("todo-list")).includes("已完成"));
  clickFilter("archived"); await settle(8);
  check("归档筛选保留", todoNodes().length === 1 && textOf(byId.get("todo-list")).includes("已归档"));
  titleButton(0).dispatch("click"); await settle(8);
  byId.get("btn-download-ics").dispatch("click"); await settle(8);
  check("ICS下载入口可用", requests.some(request => request.url === "/api/todos/17/ics") && byId.get("toast").textContent.includes(".ics"));
  byId.get("btn-settings").dispatch("click"); await settle(8);
  check("详情页可打开日历设置", view("settings") && currentHash === "#/settings" && byId.get("feed-url").value);
  check("设置路由关闭账户浮层并聚焦页标题", byId.get("account-panel").hidden && doc.activeElement === byId.get("settings-heading"));
  check("普通用户不渲染管理员邮件入口", byId.get("nav-mail-settings").hidden && byId.get("mail-settings").hidden);
  byId.get("btn-copy-feed").dispatch("click");
  check("ICS订阅地址可复制", byId.get("toast").textContent.includes("已复制"));
  byId.get("btn-reset-feed").focus(); byId.get("btn-reset-feed").dispatch("click"); await settle(4);
  check("确认框聚焦取消且主体不可交互", doc.activeElement === byId.get("confirm-cancel") && byId.get("app").inert);
  documentListeners.keydown.forEach(fn => fn({ key: "Tab", preventDefault() {} }));
  check("确认框Tab焦点约束", doc.activeElement === byId.get("confirm-ok"));
  documentListeners.keydown.forEach(fn => fn({ key: "Escape", preventDefault() {} }));
  check("Escape取消并恢复焦点", byId.get("confirm").hidden && !byId.get("app").inert && doc.activeElement === byId.get("btn-reset-feed"));
  byId.get("btn-reset-feed").dispatch("click"); await settle(4); byId.get("confirm-ok").dispatch("click"); await settle(8);
  check("ICS订阅地址重置保留", feedResets === 1 && byId.get("feed-url").value.includes("new-secret"));
  locationStub.hash = "#/todos"; await settle(8);
  todos = []; clickFilter("all"); await settle(8);
  check("空列表展示引导文案", !byId.get("list-empty").hidden && byId.get("empty-title").textContent.includes("期待"));
  byId.get("empty-create").dispatch("click");
  check("空状态按钮聚焦新建标题", doc.activeElement === byId.get("new-title"));
  clickFilter("today"); await settle(6);
  check("筛选空状态提供返回全部入口", !byId.get("empty-all").hidden);
  byId.get("empty-all").dispatch("click"); await settle(6);
  check("返回全部重置筛选和按下状态", doc.querySelector('[data-status="all"]').getAttribute("aria-pressed") === "true" && byId.get("empty-all").hidden);

  /* 旗标、紧急、重复、标签、子任务、图片 */
  todos = [
    Object.assign({}, template, { id: 21, title: "搬家", flagged: true, urgent: true, due_at: utc(1, 9), repeat: "FREQ=WEEKLY",
      tags: ["家里"], subtask_total: 2, subtask_done: 1, image_count: 1,
      subtasks: [{ id: 22, title: "打包", status: "open", parent_id: 21 }, { id: 23, title: "退租", status: "done", parent_id: 21 }],
      images: [{ id: 5, url: "api/images/5", mime: "image/jpeg" }] }),
    Object.assign({}, template, { id: 22, title: "打包", parent_id: 21, tags: [] }),
    Object.assign({}, template, { id: 24, title: "买菜", tags: ["家里", "日常"] })
  ];
  clickFilter("all"); await settle(8);
  const featureText = textOf(byId.get("todo-list"));
  check("列表显示旗标、紧急、重复、子任务、图片和标签", ["⚑", "紧急", "↻ 每周", "子任务 1/2", "图片 1", "#家里"].every(text => featureText.includes(text)), featureText);
  check("子任务排在上级任务下面并缩进", textOf(todoNodes()[0]).includes("搬家") && textOf(todoNodes()[1]).includes("打包") && todoNodes()[1].classList.contains("is-subtask"));
  clickFilter("flagged"); await settle(8);
  check("旗标筛选只显示标了旗标的任务", todoNodes().length === 1 && textOf(byId.get("todo-list")).includes("搬家"));
  clickFilter("all"); await settle(8);
  byId.get("todo-list").querySelectorAll(".tag-link").filter(node => node.textContent === "#日常")[0].dispatch("click");
  check("点标签只看这个标签", todoNodes().length === 1 && textOf(byId.get("todo-list")).includes("买菜") && !byId.get("tag-filter").hidden);
  byId.get("tag-filter-clear").dispatch("click");
  check("可以清除标签筛选", todoNodes().length === 3 && byId.get("tag-filter").hidden);

  byId.get("new-title").value = "周会"; byId.get("new-due").value = "2026-10-20T10:00";
  byId.get("new-early").value = "15"; byId.get("new-repeat").value = "FREQ=WEEKLY";
  byId.get("new-flagged").checked = true; byId.get("new-urgent").checked = true;
  const tagEntry = byId.get("new-tags-entry");
  tagEntry.value = "#工作"; tagEntry.dispatch("keydown", { key: "Enter" });
  check("回车把文字变成标签", byId.get("new-tags-list").childNodes.length === 1 && tagEntry.value === "");
  tagEntry.value = "开会"; tagEntry.dispatch("keydown", { key: "Enter", isComposing: true });
  check("输入法选字时回车不加标签", byId.get("new-tags-list").childNodes.length === 1 && tagEntry.value === "开会");
  byId.get("create-form").dispatch("submit"); await settle(8);
  const createdTask = requests.filter(r => r.method === "POST" && r.url === "/api/todos").slice(-1)[0].body;
  const dueLocal = new Date("2026-10-20T10:00");
  const isoUtc = date => date.toISOString().slice(0, 19).replace("T", " ");
  check("新建任务发送提前提醒、重复、标记和标签",
    createdTask.due_at === isoUtc(dueLocal) && createdTask.remind_at === isoUtc(new Date(dueLocal.getTime() - 15 * 60000)) &&
    createdTask.repeat === "FREQ=WEEKLY" && createdTask.flagged === true && createdTask.urgent === true &&
    JSON.stringify(createdTask.tags) === JSON.stringify(["工作", "开会"]), JSON.stringify(createdTask));
  check("新建后清空标签、标记和提前提醒", byId.get("new-tags-list").childNodes.length === 0 && !byId.get("new-flagged").checked && byId.get("new-early").value === "" && byId.get("new-repeat").value === "");

  locationStub.hash = "#/todos/21"; await settle(10);
  check("详情显示子任务和图片", byId.get("subtask-list").childNodes.length === 2 && byId.get("image-grid").childNodes.length === 1 && byId.get("subtask-count").textContent === "1/2" && !byId.get("subtasks").hidden);
  check("详情回填旗标、紧急、重复和标签", byId.get("d-flagged").checked && byId.get("d-urgent").checked && byId.get("d-repeat").value === "FREQ=WEEKLY" && byId.get("d-tags-list").childNodes.length === 1);
  byId.get("subtask-title").value = "找搬家公司"; byId.get("subtask-form").dispatch("submit"); await settle(8);
  check("添加子任务带上上级任务", requests.some(r => r.method === "POST" && r.url === "/api/todos" && r.body.parent_id === 21 && r.body.title === "找搬家公司") && byId.get("subtask-title").value === "");
  byId.get("image-grid").querySelector(".thumb").dispatch("click");
  check("点缩略图打开大图", !byId.get("lightbox").hidden && byId.get("lightbox-frame").childNodes[0].getAttribute("src") === "./api/images/5");
  documentListeners.keydown.forEach(fn => fn({ key: "Escape", preventDefault() {} }));
  check("Escape 关闭大图", byId.get("lightbox").hidden);
  byId.get("d-early").value = "60"; byId.get("detail-form").dispatch("submit"); await settle(8);
  const savedTask = requests.filter(r => r.method === "PUT" && r.url === "/api/todos/21").slice(-1)[0].body;
  check("详情保存提前提醒和标签", savedTask.remind_at === isoUtc(new Date(new Date(toLocal(savedTask.due_at)).getTime() - 3600000)) && JSON.stringify(savedTask.tags) === JSON.stringify(["家里"]), JSON.stringify(savedTask));
  locationStub.hash = "#/todos"; await settle(8);
  byId.get("btn-logout").dispatch("click"); await settle(8);
  check("退出后账户导航隐藏", view("auth") && byId.get("account-nav").hidden);
  const toggle = doc.querySelector('[data-password="auth-password"]'); toggle.dispatch("click");
  check("密码显示按钮支持切换", byId.get("auth-password").type === "text" && toggle.getAttribute("aria-pressed") === "true");
  toggle.dispatch("click"); check("密码可恢复隐藏", byId.get("auth-password").type === "password");
  byId.get("tab-login").dispatch("keydown", { key: "ArrowRight" });
  check("登录注册标签可用键盘切换", currentHash === "#/register" && doc.activeElement === byId.get("tab-register"));
  check("所有请求保留部署前缀", requests.every(request => request.raw.startsWith("./api/") && request.resolvedPath.startsWith(basePath + "api/")));
}

async function adminMailFlow() {
  await settle(8);
  check("管理员会话进入任务列表", view("list") && byId.get("account-nav").hidden === false);

  /* 1. 设置页入口 + 初始读取（无保存行 → 默认来源、密码未设置） */
  locationStub.hash = "#/settings"; await settle(8);
  check("管理员设置页显示邮件配置入口", view("settings") && !byId.get("mail-settings").hidden && !byId.get("nav-mail-settings").hidden);
  check("读取使用相对路径的管理接口", requests.some(r => r.method === "GET" && r.url === "/api/admin/mail-settings" && r.raw === "./api/admin/mail-settings"));
  check("显示当前配置来源与密码状态", byId.get("mail-source").textContent === "内置默认值" && byId.get("mail-password-source").textContent === "未设置" && byId.get("mail-password-state").textContent === "未设置" && byId.get("mail-updated").textContent.includes("从未"));
  check("密码输入框不回填且标注new-password", byId.get("mail-password").value === "" && byId.get("mail-password").getAttribute("autocomplete") === "new-password");
  check("表单展示已保存配置", byId.get("mail-port").value === "587" && byId.get("mail-from").value === "no-reply@localhost" && !byId.get("mail-enabled").checked && byId.get("mail-host").value === "");
  check("日历订阅入口保留可用", byId.get("feed-url").value.includes("token=secret") && byId.get("nav-calendar-settings").getAttribute("aria-current") === "page");

  /* 2. 修改字段 → 未保存；测试被阻止且不发请求 */
  byId.get("mail-enabled").checked = true; byId.get("mail-enabled").dispatch("change");
  byId.get("mail-host").value = "smtp.example.test"; byId.get("mail-host").dispatch("input");
  byId.get("mail-port").value = "465"; byId.get("mail-port").dispatch("input");
  byId.get("mail-user").value = "admin@example.com"; byId.get("mail-user").dispatch("input");
  byId.get("mail-from").value = "EasyLife <admin@example.com>"; byId.get("mail-from").dispatch("input");
  byId.get("mail-password").value = "auth-code-1"; byId.get("mail-password").dispatch("input");
  check("修改后标记未保存", byId.get("mail-status").dataset.state === "unsaved" && byId.get("mail-status").textContent.includes("未保存"));
  byId.get("mail-test").dispatch("click"); await settle(4);
  check("未保存时测试被阻止且不发请求", countRequests("/api/admin/mail-settings/test") === 0 && byId.get("mail-status").textContent.includes("请先") && doc.activeElement === byId.get("mail-save"));

  /* 3. 保存：保存中状态、防重复、成功清空授权码 */
  mailPutDefer = true;
  byId.get("mail-form").dispatch("submit");
  check("保存中禁用按钮并显示状态", byId.get("mail-save").disabled && byId.get("mail-status").dataset.state === "saving");
  byId.get("mail-form").dispatch("submit"); await settle(2);
  check("保存中重复提交被忽略", countRequests("/api/admin/mail-settings") === 2);
  finishMailPut(); await settle(8);
  check("保存成功状态与来源更新", byId.get("mail-status").dataset.state === "saved" && byId.get("mail-status").textContent.includes("保存成功") && byId.get("mail-source").textContent === "已保存的站点配置");
  check("保存请求携带表单字段与新授权码", !!mailSaved && mailSaved.host === "smtp.example.test" && mailSaved.port === 465 && mailSaved.user === "admin@example.com" && mailSaved.from === "EasyLife <admin@example.com>" && mailSaved.enabled === true && mailSaved.password === "auth-code-1" && !("clear_password" in mailSaved));
  check("保存成功后清空授权码输入", byId.get("mail-password").value === "" && !byId.get("mail-clear-password").checked);
  check("授权码状态更新为已设置", byId.get("mail-password-state").textContent.includes("已设置"));
  check("授权码不进入网址与浏览器存储", currentHash.indexOf("auth-code") < 0 && Array.from(stored.keys()).every(key => key === "easylife-theme") && Array.from(stored.values()).every(value => value.indexOf("auth-code") < 0));

  /* 4. 离开设置页清空输入；重新进入从服务端恢复 */
  byId.get("mail-password").value = "leftover-secret"; byId.get("mail-password").dispatch("input");
  check("再次输入可标记未保存", byId.get("mail-status").dataset.state === "unsaved");
  locationStub.hash = "#/todos"; await settle(4);
  check("离开设置页清空授权码输入", byId.get("mail-password").value === "");
  locationStub.hash = "#/settings"; await settle(8);
  check("重新进入恢复已保存配置", byId.get("mail-host").value === "smtp.example.test" && byId.get("mail-port").value === "465" && byId.get("mail-enabled").checked && byId.get("mail-password").value === "" && byId.get("mail-status").dataset.state === "saved");

  /* 5. 服务商示例：显式填充、不自动保存、不覆盖已保存配置 */
  byId.get("mail-preset-qq").dispatch("click");
  check("示例只快捷填充主机与端口", byId.get("mail-host").value === "smtp.qq.com" && byId.get("mail-port").value === "465" && byId.get("mail-status").dataset.state === "unsaved" && byId.get("mail-status").textContent.includes("不会自动覆盖"));
  check("示例不覆盖已保存配置", mailCfg.mail.host === "smtp.example.test");
  byId.get("mail-preset-exmail").dispatch("click");
  check("第二个服务商示例可用", byId.get("mail-host").value === "smtp.exmail.qq.com");
  byId.get("mail-reload").dispatch("click"); await settle(8);
  check("放弃修改并重新读取恢复已保存值", byId.get("mail-host").value === "smtp.example.test" && byId.get("mail-port").value === "465");

  /* 6. 测试邮件：测试中状态、防重复、只传 to、接受语义 */
  mailTestDefer = true;
  byId.get("mail-test").dispatch("click");
  check("测试中状态与忙碌按钮", byId.get("mail-status").dataset.state === "testing" && byId.get("mail-test").disabled);
  byId.get("mail-test").dispatch("click"); await settle(2);
  check("测试中重复点击被忽略", countRequests("/api/admin/mail-settings/test") === 1);
  finishMailTest(); await settle(8);
  check("测试成功只声明SMTP接受", byId.get("mail-status").dataset.state === "accepted" && byId.get("mail-status").textContent.includes("SMTP 服务器已接受") && byId.get("mail-status").textContent.includes("不代表"));
  check("默认测试请求体为空对象", mailTestBody !== null && Object.keys(mailTestBody).length === 0);
  byId.get("mail-test-to").value = "qa@example.com"; byId.get("mail-test-to").dispatch("input"); await settle(2);
  check("收件地址不影响未保存判定", byId.get("mail-status").dataset.state === "accepted");
  byId.get("mail-test").dispatch("click"); await settle(8);
  check("显式收件地址只传to字段", mailTestBody && Object.keys(mailTestBody).join(",") === "to" && mailTestBody.to === "qa@example.com");

  /* 7. 失败、网络错误与停用（409） */
  mailTestStatus = 502; byId.get("mail-test").dispatch("click"); await settle(8);
  check("投递失败显示失败状态", byId.get("mail-status").dataset.state === "failed" && byId.get("mail-status").textContent.includes("邮件服务器连接"));
  mailTestStatus = 200; mailTestNetworkFail = true;
  byId.get("mail-test").dispatch("click"); await settle(8);
  check("网络错误显示结果未知", byId.get("mail-result").dataset.state === "unknown" && byId.get("mail-status").textContent.includes("网络请求失败") && byId.get("mail-status").textContent.includes("检查收件箱"));
  mailTestNetworkFail = false; mailTestStatus = 409;
  byId.get("mail-test").dispatch("click"); await settle(8);
  check("未配置或停用时提示先保存", byId.get("mail-status").dataset.state === "failed" && byId.get("mail-status").textContent.includes("停用"));
  mailTestStatus = 200;

  /* 8. 限流：专用状态 + 按钮冷却 */
  mailTestStatus = 429; byId.get("mail-test").dispatch("click"); await settle(8);
  check("限流显示专用状态并进入冷却", byId.get("mail-status").dataset.state === "rate-limited" && byId.get("mail-test").disabled && byId.get("mail-test").textContent.includes("限流中"));
  advanceTime(91);
  check("冷却结束可重新测试", !byId.get("mail-test").disabled && byId.get("mail-test").textContent === "发送测试邮件");
  mailTestStatus = 200;

  /* 9. 403 与 401 */
  mailGetStatus = 403;
  byId.get("mail-reload").dispatch("click"); await settle(8);
  check("403 显示权限提示并收起表单", byId.get("mail-status").dataset.state === "failed" && byId.get("mail-status").textContent.includes("管理员权限") && byId.get("mail-form").hidden);
  mailGetStatus = 401;
  byId.get("mail-reload").dispatch("click"); await settle(8);
  check("401 清理会话并回到登录", view("auth") && byId.get("account-nav").hidden && byId.get("mail-settings").hidden);
  check("所有邮件接口保留相对路径", absoluteRequests.length === 0 && requests.every(request => request.raw.startsWith("./api/")));
  check("管理员邮件设置交互完成", true);
}

async function adminMailRaceFlow() {
  const node = id => byId.get("mail-" + id);
  const status = () => node("status").textContent;
  const result = () => (node("result") || node("status")).textContent;
  const resultState = () => (node("result") || node("status")).dataset.state;
  const edit = (id, value) => {
    const n = node(id);
    if (typeof value === "boolean") { n.checked = value; } else { n.value = value; }
    n.dispatch("input");
  };
  const count = method => requests.filter(r => r.url === "/api/admin/mail-settings" && r.method === method).length;
  const tests = () => countRequests("/api/admin/mail-settings/test");
  const hold = method => { mailHoldNext = method; };
  const release = () => mailHeld.shift().release();
  const drop = () => mailHeld.shift().drop();
  const save = () => node("form").dispatch("submit");
  const reload = async () => { node("reload").dispatch("click"); await settle(8); };
  const leave = async () => { locationStub.hash = "#/todos"; await settle(4); };
  const enter = async () => { locationStub.hash = "#/settings"; await settle(8); };
  await settle(8); await enter();
  edit("enabled", true); edit("host", "saved.example.test"); edit("port", "465");
  edit("from", "sender@example.com"); edit("password", "race-initial-code");
  save(); await settle(8);

  // Late PUT must update the saved baseline, never replace a newer draft/secret.
  edit("host", "submitted.example.test"); edit("password", "race-submitted-code");
  hold("PUT"); save(); const putCount = count("PUT");
  save(); node("test").dispatch("click");
  check("保存期间重复提交和测试均不发请求", count("PUT") === putCount && tests() === 0);
  edit("host", "newer.example.test"); edit("password", "race-newer-code");
  release(); await settle(8);
  check("迟到保存保留后续主机草稿", node("host").value === "newer.example.test");
  check("迟到保存不清空后来输入的授权码", node("password").value === "race-newer-code");
  check("迟到保存后仍显示未保存", status().includes("未保存") && node("status").dataset.state === "unsaved");
  check("保存结果只确认提交时的版本", result().includes("保存成功") && mailCfg.mail.host === "submitted.example.test");
  const blockedTests = tests(); node("test").dispatch("click"); await settle(4);
  check("迟到成功后新草稿仍阻止测试", tests() === blockedTests);
  save(); await settle(8);
  check("新草稿成功保存后清空其授权码", node("password").value === "" && mailCfg.mail.host === "newer.example.test");

  edit("password", "race-submitted-again"); hold("PUT"); save();
  node("preset-exmail").dispatch("click"); release(); await settle(8);
  check("服务商预设计入编辑且迟到保存不覆盖", node("host").value === "smtp.exmail.qq.com" && status().includes("未保存"));
  check("仅公开字段变化时仍清理已确认提交的授权码", node("password").value === "");
  await reload();
  edit("host", "clear-submit.example.test"); hold("PUT"); save(); edit("clear-password", true);
  release(); await settle(8);
  check("保存途中勾选清除操作不会被旧响应撤销", node("clear-password").checked && status().includes("未保存"));
  await reload();

  // Reads also carry an edit version; a reload cannot discard edits made while waiting.
  hold("GET"); node("reload").dispatch("click"); const getCount = count("GET");
  node("reload").dispatch("click"); edit("user", "during-read@example.com"); edit("password", "race-read-code");
  release(); await settle(8);
  check("延迟读取保留读取期间的新草稿和授权码", node("user").value === "during-read@example.com" && node("password").value === "race-read-code");
  check("重新读取防重复且保留未保存提示", count("GET") === getCount && status().includes("未保存"));
  await reload();

  // Test receipt refers to the saved snapshot and original recipient, not the edited form.
  edit("test-to", "original@example.com"); hold("POST"); node("test").dispatch("click");
  const testCount = tests(); node("test").dispatch("click");
  edit("host", "during-test.example.test"); edit("test-to", "later@example.com");
  release(); await settle(8);
  check("旧测试成功不覆盖未保存提示", node("status").dataset.state === "unsaved" && status().includes("未保存"));
  check("测试结果绑定原收件人和发起时配置", result().includes("original@example.com") && result().includes("clear-submit.example.test") && !result().includes("later@example.com"));
  check("测试只提交to且防重复", tests() === testCount && Object.keys(mailTestBody).join() === "to" && mailTestBody.to === "original@example.com");
  check("接受结果不声称送达", resultState() === "accepted" && result().includes("不代表"));
  await reload();

  // The server committed the PUT, then its response vanished. Reconcile by GET only.
  edit("host", "committed.example.test"); edit("password", "race-unknown-code");
  hold("PUT"); save(); const uncertainPuts = count("PUT"); const beforeGet = count("GET");
  edit("host", "latest-draft.example.test"); edit("password", "race-latest-code");
  hold("GET"); drop(); await settle(8);
  check("PUT响应丢失显示未知并自动GET核对", result().includes("尚未确认") && count("GET") === beforeGet + 1);
  check("未知保存不声称失败或未生效", !result().includes("保存失败") && !result().includes("修改仍未保存"));
  edit("from", "later-draft@example.com");
  if (mailHeld.length) { release(); } await settle(8);
  check("核对GET保留其间最新草稿与密码", node("host").value === "latest-draft.example.test" && node("from").value === "later-draft@example.com" && node("password").value === "race-latest-code");
  check("核对响应只确认公开字段不推断授权码更新", resultState() === "unknown" && result().includes("公开字段") && result().includes("授权码") && result().includes("无法确认"));
  check("未知核对后仍有未保存提示且没有自动重试PUT", status().includes("未保存") && count("PUT") === uncertainPuts);
  const unknownTests = tests(); node("test").dispatch("click"); await settle(4);
  check("授权码更新未知时不能直接发测试", tests() === unknownTests);
  // Re-reading unknown outcomes preserves drafts; explicit re-save resolves uncertainty.
  await reload();
  check("未知状态手动核对也保留草稿", node("host").value === "latest-draft.example.test" && node("password").value === "race-latest-code" && resultState() === "unknown");
  save(); await settle(8);
  check("显式再保存成功解除未知并清理授权码", resultState() === "saved" && node("password").value === "");

  edit("host", "get-fails.example.test"); hold("PUT"); save(); mailGetNetworkFail = true; drop(); await settle(8);
  check("核对读取失败保留未知及手动读取入口", resultState() === "unknown" && result().includes("重新读取") && !node("reload").disabled);
  check("核对失败仍保留草稿", node("host").value === "get-fails.example.test");
  mailGetNetworkFail = false; edit("user", "latest-user@example.com"); await reload();
  check("手动核对恢复网络但不覆盖后续编辑", node("user").value === "latest-user@example.com");
  save(); await settle(8);

  // SMTP acceptance precedes response loss: never claim non-acceptance or auto-send again.
  const acceptedBefore = mailAccepted; hold("POST"); node("test").dispatch("click"); const lostTests = tests();
  drop(); await settle(8); advanceTime(31); await settle(4);
  check("测试响应丢失而SMTP已接受显示结果未知", mailAccepted === acceptedBefore + 1 && resultState() === "unknown" && result().includes("尚未确认"));
  check("未知测试提示检查收件箱不否认SMTP接受", result().includes("检查收件箱") && !result().includes("未被 SMTP 接受"));
  check("未知测试没有自动重发", tests() === lostTests);

  hold("POST"); node("test").dispatch("click"); const timeoutTests = tests();
  advanceTime(31); await settle(8);
  check("测试客户端超时显示未知且解除忙碌", resultState() === "unknown" && result().includes("超时") && !node("test").disabled);
  release(); await settle(8);
  check("测试超时后迟到成功不会改写未知或重发", resultState() === "unknown" && tests() === timeoutTests);
  mailBodyLossNext = "POST"; node("test").dispatch("click"); await settle(8);
  check("成功HTTP的测试响应体丢失仍显示未知", resultState() === "unknown" && result().includes("检查收件箱"));

  edit("password", "race-body-loss"); mailBodyLossNext = "PUT"; const bodyGets = count("GET"); save(); await settle(8);
  check("成功HTTP的保存响应体丢失触发GET核对", resultState() === "unknown" && count("GET") === bodyGets + 1 && node("password").value === "race-body-loss");
  save(); await settle(8);

  // Version checks must handle a retyped identical secret and undoing a submitted clear.
  edit("password", "race-identical-code"); hold("PUT"); save(); edit("password", ""); edit("password", "race-identical-code");
  release(); await settle(8);
  check("保存期间重新输入相同授权码也保留为新草稿", node("password").value === "race-identical-code" && status().includes("未保存"));
  await reload(); edit("clear-password", true); hold("PUT"); save(); edit("clear-password", false); edit("password", "race-after-clear");
  release(); await settle(8);
  check("旧清除响应保留后来输入的授权码", node("password").value === "race-after-clear" && !node("clear-password").checked && status().includes("未保存"));
  save(); await settle(8);

  // Body loss / client timeout are also ambiguous, and must recover their busy controls.
  edit("host", "timeout.example.test"); hold("PUT"); save(); advanceTime(31); await settle(8);
  check("保存客户端超时会GET核对并解除忙碌", resultState() === "unknown" && !node("save").disabled);
  release(); await settle(8);
  check("超时后的迟到成功不能改写未知结果", resultState() === "unknown");
  save(); await settle(8);

  // Both settings anchors share one page. Navigation must not abandon live requests/drafts.
  hold("GET"); node("reload").dispatch("click"); const anchorGets = count("GET");
  edit("user", "anchor-read@example.com"); edit("password", "race-anchor-read");
  locationStub.hash = "#/settings/mail"; await settle(4);
  check("设置页内切到邮件入口保留读取期间草稿", node("user").value === "anchor-read@example.com" && node("password").value === "race-anchor-read");
  check("设置页内切换不取消读取或增加GET", node("reload").disabled && count("GET") === anchorGets);
  release(); await settle(8);
  check("页内切换后延迟GET仍完成且保留草稿", resultState() === "saved" && status().includes("未保存") && node("password").value === "race-anchor-read" && !node("reload").disabled);
  await leave(); await enter();

  edit("password", "race-anchor-submit"); hold("PUT"); save(); const anchorPuts = count("PUT");
  node("preset-qq").dispatch("click"); edit("password", "race-anchor-new");
  locationStub.hash = "#/settings/mail"; await settle(4);
  byId.get("btn-settings").dispatch("click"); await settle(4);
  check("保存中邮件日历入口切换保留预设及新授权码", node("host").value === "smtp.qq.com" && node("password").value === "race-anchor-new");
  check("页内切换不解除保存忙碌或允许重复PUT", node("save").disabled && node("save").getAttribute("aria-busy") === "true" && resultState() === "saving" && count("PUT") === anchorPuts);
  save(); release(); await settle(8);
  check("页内切换后旧保存仅确认原提交", count("PUT") === anchorPuts && resultState() === "saved" && status().includes("未保存") && node("password").value === "race-anchor-new");
  await leave(); await enter();

  hold("POST"); node("test").dispatch("click"); const anchorTests = tests();
  edit("host", "anchor-test-draft.example.test");
  locationStub.hash = "#/settings/mail"; await settle(4);
  check("测试中页内导航保留忙碌与未保存提示", node("test").disabled && status().includes("未保存") && resultState() === "testing");
  release(); await settle(8);
  check("页内导航后旧测试成功仍与未保存并存", resultState() === "accepted" && status().includes("未保存") && node("host").value === "anchor-test-draft.example.test" && tests() === anchorTests);
  await leave(); await enter();

  hold("GET"); node("reload").dispatch("click"); edit("password", "race-history-secret");
  (windowListeners.pagehide || []).forEach(fn => fn({ type: "pagehide", persisted: true }));
  const historyGets = count("GET");
  (windowListeners.pageshow || []).forEach(fn => fn({ type: "pageshow", persisted: true }));
  await settle(8);
  check("缓存页恢复会重新读取配置且秘密不恢复", count("GET") === historyGets + 1 && node("password").value === "" && node("host").value === mailCfg.mail.host && !node("save").disabled);
  edit("password", "race-history-new"); release(); await settle(8);
  check("缓存页恢复后旧读取不覆盖新授权码", node("password").value === "race-history-new" && status().includes("未保存"));
  await leave(); await enter();

  // The session refresh is part of restoration: protect edits and newer logins here too.
  (windowListeners.pagehide || []).forEach(fn => fn({ type: "pagehide", persisted: true }));
  hold("ME"); (windowListeners.pageshow || []).forEach(fn => fn({ type: "pageshow", persisted: true }));
  edit("user", "restore-draft@example.com"); edit("password", "race-restore-draft");
  release(); await settle(8);
  check("恢复会话期间编辑也受同一版本保护", node("user").value === "restore-draft@example.com" && node("password").value === "race-restore-draft" && status().includes("未保存"));
  await reload();
  (windowListeners.pagehide || []).forEach(fn => fn({ type: "pagehide", persisted: true }));
  hold("ME"); (windowListeners.pageshow || []).forEach(fn => fn({ type: "pageshow", persisted: true }));
  byId.get("btn-logout").dispatch("click"); await settle(8);
  mailSessionId = 7; fillAuth(); byId.get("auth-form").dispatch("submit"); await settle(12); await enter();
  edit("password", "race-new-login"); const restoredGets = count("GET");
  release(); await settle(8);
  check("旧缓存页会话响应不重读配置或清理新会话草稿", view("settings") && node("password").value === "race-new-login" && count("GET") === restoredGets);
  save(); await settle(8);
  check("旧会话恢复响应不能替换当前CSRF", requests.filter(r => r.url === "/api/admin/mail-settings" && r.method === "PUT").slice(-1)[0].headers["X-CSRF-Token"] === "csrf-token-7");

  (windowListeners.pagehide || []).forEach(fn => fn({ type: "pagehide", persisted: true }));
  hold("ME"); (windowListeners.pageshow || []).forEach(fn => fn({ type: "pageshow", persisted: true }));
  edit("password", "race-restore-timeout"); const timeoutGets = count("GET");
  advanceTime(31); await settle(8);
  check("恢复会话读取超时会解除忙碌并保留草稿", resultState() === "failed" && result().includes("超时") && !node("reload").disabled && node("password").value === "race-restore-timeout");
  release(); await settle(8);
  check("恢复会话超时后旧响应不继续读取或替换失败提示", count("GET") === timeoutGets && resultState() === "failed" && node("password").value === "race-restore-timeout");
  await leave(); await enter();

  // Invalidate on departure, pagehide, logout, and account change (including old 401).
  edit("password", "race-leaving-code"); hold("PUT"); save(); await leave();
  check("离开页面立即清空授权码", node("password").value === "");
  const leftStatus = status(); release(); await settle(8);
  check("离开页面后旧保存不能恢复配置或结果", status() === leftStatus && node("password").value === "" && view("list"));
  await enter(); hold("POST"); node("test").dispatch("click"); await leave(); await enter();
  edit("password", "race-new-page-code"); const newPageStatus = status(); release(); await settle(8);
  check("重新进入后旧测试不能覆盖新页面状态", status() === newPageStatus && node("password").value === "race-new-page-code");
  await reload(); hold("GET"); node("reload").dispatch("click");
  (windowListeners.pagehide || []).forEach(fn => fn({ type: "pagehide" }));
  const hiddenStatus = status(); release(); await settle(8);
  check("pagehide使旧读取失效且秘密为空", status() === hiddenStatus && node("password").value === "");
  await leave(); await enter();
  mailGetStatus = 401; hold("GET"); node("reload").dispatch("click"); mailGetStatus = 0;
  byId.get("btn-logout").dispatch("click"); await settle(8);
  mailSessionId = 2; fillAuth(); byId.get("auth-form").dispatch("submit"); await settle(12); await enter();
  edit("password", "race-second-account"); release(); await settle(8);
  check("旧会话迟到401不登出新账号或清空其输入", view("settings") && node("password").value === "race-second-account" && !byId.get("account-nav").hidden);
  await reload(); hold("GET"); node("reload").dispatch("click");
  byId.get("btn-logout").dispatch("click"); await settle(8); const logoutStatus = status();
  if (mailHeld.length) { release(); } await settle(8);
  check("登出后旧GET不恢复管理配置及状态", view("auth") && node("settings").hidden && node("host").value === "" && status() === logoutStatus);
  check("竞态测试中秘密不入URL或浏览器持久存储", !currentHash.includes("race-") && Array.from(stored.keys()).every(k => k === "easylife-theme"));
  check("竞态请求始终使用相对路径且写请求有CSRF", requests.every(r => r.raw.startsWith("./api/")) && requests.filter(r => r.url.includes("mail-settings") && r.method !== "GET").every(r => !!r.headers["X-CSRF-Token"]));
}

/* ------------------------------------------------------------ 场景 */
(async function main() {
  try {
    if (emailAuthScenario) {
      await emailAuthFlow();
    } else if (uiScenario) {
      await uiFlow();
    } else if (mailRaceScenario) {
      await adminMailRaceFlow();
    } else if (adminMailScenario) {
      await adminMailFlow();
    } else {
    await settle(8);

    /* 1. 启动：已登录，停在列表视图 */
    check("启动后停在列表视图", view("list") && !view("detail") && !view("auth"), "hash=" + currentHash);
    check("列表渲染出两条任务", todoNodes().length === 2, todoNodes().length);

    /* 2. 从列表点击标题进入详情 */
    const reopen = titleButton(0);
    check("列表项里有可点击的标题按钮", !!reopen);
    reopen.dispatch("click");
    await settle(8);

    check("点击标题进入详情视图", view("detail") && !view("list"), "hash=" + currentHash);
    check("进入详情后 hash 变为 #/todos/1", currentHash === "#/todos/1", currentHash);
    check("详情标题是被点击的任务", byId.get("d-title").value === "写周报", byId.get("d-title").value);
    check("详情页显示 Google 日历链接", byId.get("link-google").hidden === false);

    /* 3. 在详情页点删除 → 出现确认框 */
    byId.get("btn-detail-delete").dispatch("click");
    await settle(4);
    check("删除前弹出确认框", byId.get("confirm").hidden === false);
    check("确认文案包含任务标题", byId.get("confirm-text").textContent.includes("写周报"),
      byId.get("confirm-text").textContent);

    /* 4. 确认删除 → 必须回到列表，而不是停在详情页 */
    byId.get("confirm-ok").dispatch("click");
    await settle(10);

    check("确认删除后确认框关闭", byId.get("confirm").hidden === true);
    check("确认删除后回到列表视图", view("list") && !view("detail"), "hash=" + currentHash);
    check("确认删除后 hash 为 #/todos", currentHash === "#/todos", currentHash);
    check("确认删除后列表只剩一条", todoNodes().length === 1, todoNodes().length);
    check("已删除任务不再出现在列表", !textOf(byId.get("todo-list")).includes("写周报"),
      textOf(byId.get("todo-list")));
    check("列表空状态未误显示", byId.get("list-empty").hidden === true);
    check("DELETE 请求已发出", requests.some(function (item) {
      return item.method === "DELETE" && item.url === "/api/todos/1";
    }));

    /* 5. 详情状态已清空：保存 / 新增笔记 必须给出提示，不能静默失败 */
    const before = requests.length;
    byId.get("detail-form").dispatch("submit");
    byId.get("note-form").dispatch("submit");
    await settle(4);
    check("清空后点保存会提示错误", byId.get("toast").textContent.includes("任务不存在"),
      byId.get("toast").textContent);
    check("清空后保存不会发出请求", requests.length === before, requests.length - before);

    /* 6. 正常打开另一条任务时，新增笔记要写入时间线 */
    titleButton(0).dispatch("click");
    await settle(8);
    check("再次进入详情", view("detail") && currentHash === "#/todos/2", "hash=" + currentHash);
    byId.get("d-title").value = "尚未保存的标题草稿";
    byId.get("note-content").value = "记得买两盒";
    byId.get("note-form").dispatch("submit");
    await settle(10);

    check("新增笔记后时间线出现条目", byId.get("note-list").childNodes.length === 1,
      byId.get("note-list").childNodes.length);
    check("笔记内容用 textContent 渲染", textOf(byId.get("note-list")).includes("记得买两盒"),
      textOf(byId.get("note-list")));
    check("新增笔记请求已发出", requests.some(function (item) {
      return item.method === "POST" && item.url === "/api/todos/2/notes";
    }));
    check("新增笔记后仍停留在详情", view("detail"), "hash=" + currentHash);
    check("笔记输入框已清空", byId.get("note-content").value === "", byId.get("note-content").value);
    check("新增笔记保留未保存编辑并聚焦输入框", byId.get("d-title").value === "尚未保存的标题草稿" && doc.activeElement === byId.get("note-content"));

    /* 7. 详情页保存（没有 401）应当成功并发请求 */
    byId.get("d-title").value = "买两盒牛奶";
    byId.get("detail-form").dispatch("submit");
    await settle(10);
    check("详情保存已发出 PUT 请求", requests.some(function (item) {
      return item.method === "PUT" && item.url === "/api/todos/2";
    }));
    check("详情保存后提示成功", byId.get("toast").textContent.includes("已保存"),
      byId.get("toast").textContent);
    const savedBody = requests.filter(r => r.method === "PUT" && r.url === "/api/todos/2").slice(-1)[0].body || {};
    check("列表未加载时保存不改所属列表", !("list_id" in savedBody), JSON.stringify(savedBody));

    /* 8. 任务已在别处被删除（DELETE 返回 404）：同样要回到列表，不能停在死详情页 */
    deleteStatusOverride = 404;
    byId.get("btn-detail-delete").dispatch("click");
    await settle(4);
    check("404 场景下弹出确认框", byId.get("confirm").hidden === false);
    byId.get("confirm-ok").dispatch("click");
    await settle(10);
    check("删除返回 404 时也回到列表视图", view("list") && !view("detail"), "hash=" + currentHash);
    check("删除返回 404 时 hash 回到 #/todos", currentHash === "#/todos", currentHash);
    check("删除返回 404 时给出提示", byId.get("toast").textContent.includes("已返回列表"),
      byId.get("toast").textContent);
    check("删除返回 404 后列表仍在渲染", todoNodes().length === 1, todoNodes().length);

    /* 9. 详情响应晚于用户切页时，不得覆盖列表或邮箱恢复视图。 */
    deferDetail = true;
    titleButton(0).dispatch("click");
    await settle(2);
    locationStub.hash = "#/forgot-password";
    await settle(2);
    finishDetail();
    await settle(6);
    check("迟到的详情成功响应不覆盖忘记密码页", view("forgot") && currentHash === "#/forgot-password");

    locationStub.hash = "#/todos";
    await settle(6);
    deferDetail = true;
    detailStatusOverride = 404;
    titleButton(0).dispatch("click");
    await settle(2);
    locationStub.hash = "#/reset?token=reset-link";
    await settle(2);
    finishDetail();
    await settle(6);
    check("迟到的详情404响应不覆盖重置密码页", view("reset") && currentHash === "#/reset?token=reset-link");

    /* 9b. 从提醒邮件的链接直接打开详情：要加载列表并显示「所属列表」 */
    listsAvailable = true;
    locationStub.hash = "#/forgot-password";
    await settle(4);
    const linked = todos.filter(todo => todo.status !== "archived")[0];
    locationStub.hash = "#/todos/" + linked.id;
    await settle(10);
    const listFields = Array.prototype.filter.call(doc.querySelectorAll(".list-field"), node => node.closest ? node.closest("#view-detail") : true);
    check("邮件链接打开详情后显示所属列表", view("detail") && listFields.length > 0 && listFields.every(node => node.hidden === false) && byId.get("d-list").value === "1",
      "value=" + byId.get("d-list").value + " fields=" + listFields.map(node => node.hidden).join(","));

    /* 10. 部署在 /todos/ 下也要能用：所有请求都必须是相对路径，不能以 "/" 开头 */
    check("所有 API 请求都使用相对路径", absoluteRequests.length === 0, absoluteRequests.join(", "));
    }
  } catch (error) {
    check("运行期间没有异常抛出", false, (error && error.stack) || String(error));
  }

  const failed = results.filter(function (item) { return !item.ok; });
  results.forEach(function (item) {
    process.stdout.write((item.ok ? "  ok   " : "  FAIL ") + item.name +
      (item.detail && !item.ok ? "  [" + item.detail + "]" : "") + "\n");
  });
  process.stdout.write("\n" + (results.length - failed.length) + "/" + results.length +
    " checks passed" + (failed.length ? "\nSOME CHECKS FAILED\n" : " — ALL CHECKS PASSED\n"));

  pendingTimers.forEach(function (id) { realClearTimeout(id); });
  pendingTimers.clear();
  process.exit(failed.length ? 1 : 0);
})();
