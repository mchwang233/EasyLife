/* EasyLife 前端：原生 JavaScript，无第三方库、无内联脚本。
 *
 * 安全约定：所有用户内容一律用 textContent 写入（从不拼接 HTML 字符串），
 * 外部链接先校验协议再赋值给 href。
 */
(function () {
  "use strict";

  var STATUS_LABELS = { open: "待办", doing: "进行中", done: "已完成", archived: "已归档" };

  var state = {
    user: null,
    csrf: null,
    feedUrl: "",
    /* CalDAV 同步：服务端公开信息；明文密码只在生成后显示一次，不进 state。 */
    dav: null,
    filter: "all",
    /* 标签筛选（本地）："" 表示不过滤。 */
    tag: "",
    /* 标签输入框：{ "new-tags": [...], "d-tags": [...] } */
    tags: {},
    lightbox: null,
    authMode: "login",
    authToken: "",
    view: "",
    ready: false,
    pendingEmail: "",
    offerResend: false,
    resendUntil: {},
    resendBusy: false,
    resendTimer: null,
    verification: null,
    listRequest: 0,
    /* 任务列表（项目）：listFilter 为 "all" 或列表 id。 */
    lists: [],
    listFilter: "all",
    listsRequest: 0,
    listEditing: null,
    composerListFor: null,
    confirmFocus: null,
    todos: [],
    current: null,
    confirmResolve: null,
    toastTimer: null,
    /* 管理员邮件设置：只保存最近一次服务端返回的公开快照与派生状态，密码永不入 state。 */
    mail: null,
    mailBaseline: "",
    mailDirty: false,
    mailGeneration: 0,
    mailEditVersion: 0,
    mailPasswordVersion: 0,
    mailClearVersion: 0,
    mailLoadRequest: null,
    mailSaveRequest: null,
    mailTestRequest: null,
    mailUnknownSave: null,
    mailResult: null,
    mailTestBusy: false,
    mailTestUntil: 0,
    mailTestTimer: null
  };

  // ------------------------------------------------------------ 小工具

  function $(selector) { return document.querySelector(selector); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function pad2(value) { return (value < 10 ? "0" : "") + value; }

  /* 本地时间 -> 服务端要求的 UTC "YYYY-MM-DD HH:MM:SS" */
  function fromLocalInput(value) {
    if (!value) { return null; }
    var date = new Date(value);
    if (isNaN(date.getTime())) { return null; }
    return date.getUTCFullYear() + "-" + pad2(date.getUTCMonth() + 1) + "-" + pad2(date.getUTCDate()) +
      " " + pad2(date.getUTCHours()) + ":" + pad2(date.getUTCMinutes()) + ":" + pad2(date.getUTCSeconds());
  }

  /* UTC "YYYY-MM-DD HH:MM:SS" -> 本地 datetime-local 输入值 */
  function toLocalInput(value) {
    if (!value || typeof value !== "string") { return ""; }
    var parts = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(value);
    if (!parts) { return ""; }
    var date = new Date(Date.UTC(+parts[1], +parts[2] - 1, +parts[3],
      +parts[4], +parts[5], +(parts[6] || 0)));
    if (isNaN(date.getTime())) { return ""; }
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) +
      "T" + pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function formatTime(value) {
    var local = toLocalInput(value);
    return local ? local.replace("T", " ") : "";
  }

  /* 只接受 https 绝对地址，避免 javascript:/data: 之类的伪协议。 */
  function safeLink(url) {
    return (typeof url === "string" && /^https:\/\/[^\s]+$/i.test(url)) ? url : "";
  }

  function setText(node, text) {
    if (node) { node.textContent = text === undefined || text === null ? "" : String(text); }
  }

  // ------------------------------------------------------------ 外观与表单

  function initTheme() {
    var theme = "system";
    try { theme = localStorage.getItem("easylife-theme") || "system"; } catch (ignore) { /* 隐私模式仍可切换 */ }
    if (["system", "light", "dark"].indexOf(theme) < 0) { theme = "system"; }
    applyTheme(theme);
    $("#theme-select").addEventListener("change", function () { setTheme($("#theme-select").value); });
    // 原生下拉在顶栏里很突兀：换成一个循环切换的小按钮，下拉保留为数据源。
    var toggle = $("#theme-toggle");
    if (toggle) {
      $("#theme-select").hidden = true;
      toggle.hidden = false;
      toggle.addEventListener("click", function () {
        var order = ["system", "light", "dark"];
        setTheme(order[(order.indexOf($("#theme-select").value) + 1) % order.length]);
      });
    }
    window.addEventListener("storage", function (event) {
      if (event.key === "easylife-theme") { applyTheme(event.newValue || "system"); }
    });
  }

  function applyTheme(theme) {
    if (["light", "dark"].indexOf(theme) < 0) { theme = "system"; }
    // system 不固定颜色，CSS prefers-color-scheme 会实时跟随系统。
    if (theme === "system") { document.documentElement.removeAttribute("data-theme"); }
    else { document.documentElement.setAttribute("data-theme", theme); }
    $("#theme-select").value = theme;
    var toggle = $("#theme-toggle");
    if (toggle) {
      var names = { system: "跟随系统", light: "浅色", dark: "深色" };
      // 图标和名称分开放：手机上只显示图标，顶栏不会被挤到换行。
      setText($("#theme-toggle-icon"), { system: "◐", light: "☀", dark: "☾" }[theme]);
      setText($("#theme-toggle-name"), names[theme]);
      toggle.setAttribute("aria-label", "外观主题：" + names[theme] + "，点击切换");
    }
  }

  function setTheme(value) {
    applyTheme(value);
    try { localStorage.setItem("easylife-theme", value); } catch (ignore) { /* 仅在当前页面生效 */ }
  }

  // ------------------------------------------------------------ 选择器
  //
  // 原生 select / datetime-local 跟随浏览器语言（mm/dd/yyyy、AM/PM）且无法统一样式，
  // 这里用分段按钮和日历面板替代显示；原生控件隐藏后仍是唯一数据源，
  // 其余代码照常读写 .value，改值后调用 refreshPickers() 即可同步显示。

  var pickers = [];
  var WEEKDAYS = "日一二三四五六";
  var openPicker = null;

  function labelText(id) {
    var label = $("label[for='" + id + "']");
    return label ? label.textContent.trim() : "";
  }

  function enhanceSelect(holder) {
    var select = $("#" + holder.dataset.for);
    if (!select) { return; }
    select.hidden = true;
    holder.setAttribute("role", "group");
    holder.setAttribute("aria-label", labelText(select.id));
    var buttons = [];
    var built = "";
    // Options can change (a custom repeat rule from the phone): rebuild when they do.
    function build() {
      var options = select.querySelectorAll("option");
      var key = Array.prototype.map.call(options, function (o) { return o.getAttribute("value"); }).join("|");
      if (key === built) { return; }
      built = key;
      holder.replaceChildren();
      buttons = [];
      Array.prototype.forEach.call(options, function (option) {
        var value = option.getAttribute("value");
        var button = el("button", "seg-btn", option.textContent.trim());
        button.type = "button";
        button.dataset.value = value;
        button.addEventListener("click", function () {
          select.value = value;
          ensureDueFor(select);
          refreshPickers();
        });
        holder.appendChild(button);
        buttons.push(button);
      });
    }
    build();
    pickers.push({ refresh: function () {
      build();
      buttons.forEach(function (button) {
        var active = button.dataset.value === String(select.value);
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    } });
  }

  function parseLocal(value) {
    var parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
    return parts ? new Date(+parts[1], +parts[2] - 1, +parts[3], +parts[4], +parts[5]) : null;
  }

  function formatLocal(date) {
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) +
      "T" + pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  /* 「今天 18:00 / 明天 09:30 / 10月21日 周三 18:00」，列表和选择器共用。 */
  function friendlyDate(date) {
    var clock = pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    var start = new Date(); start.setHours(0, 0, 0, 0);
    var day = new Date(date.getTime()); day.setHours(0, 0, 0, 0);
    var diff = Math.round((day - start) / 86400000);
    var names = { "-1": "昨天", 0: "今天", 1: "明天", 2: "后天" };
    if (names[diff] !== undefined) { return names[diff] + " " + clock; }
    var label = (date.getMonth() + 1) + "月" + date.getDate() + "日";
    if (date.getFullYear() !== start.getFullYear()) { label = date.getFullYear() + "年" + label; }
    else { label += " 周" + WEEKDAYS.charAt(date.getDay()); }
    return label + " " + clock;
  }

  function enhanceDateTime(holder) {
    var input = $("#" + holder.dataset.for);
    if (!input) { return; }
    input.hidden = true;
    var name = labelText(input.id);
    var picker = { input: input, holder: holder, name: name, month: null };
    var trigger = el("button", "dt-trigger");
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    var icon = el("span", "dt-icon");
    icon.setAttribute("aria-hidden", "true");
    var text = el("span", "dt-text");
    trigger.appendChild(icon);
    trigger.appendChild(text);
    var clear = el("button", "dt-clear", "×");
    clear.type = "button";
    clear.setAttribute("aria-label", "清除" + name);
    trigger.addEventListener("click", function () {
      if (openPicker === picker) { closePicker(true); } else { showPicker(picker); }
    });
    clear.addEventListener("click", function () {
      input.value = "";
      refreshPickers();
      trigger.focus();
    });
    holder.appendChild(trigger);
    holder.appendChild(clear);
    picker.trigger = trigger;
    picker.refresh = function () {
      var date = parseLocal(input.value);
      setText(text, date ? friendlyDate(date) : "未设置");
      trigger.classList.toggle("is-empty", !date);
      trigger.setAttribute("aria-label", name + "：" + (date ? friendlyDate(date) : "未设置") + "，点击选择");
      clear.hidden = !date;
      if (openPicker === picker) { renderPanel(picker); }
    };
    pickers.push(picker);
  }

  function refreshPickers() {
    pickers.forEach(function (picker) { picker.refresh(); });
    syncTimeOptions("new");
    syncTimeOptions("d");
  }

  /* 「提前提醒」「重复」都要有截止时间：清除截止时间时一起清掉；勾选「紧急」时显示说明。 */
  function syncTimeOptions(prefix) {
    var due = $("#" + prefix + "-due");
    if (due && !due.value) {
      $("#" + prefix + "-early").value = "";
      $("#" + prefix + "-repeat").value = "";
    }
    var hint = $("#" + prefix + "-urgent-hint");
    if (hint) { hint.hidden = !$("#" + prefix + "-urgent").checked; }
  }

  /* 像 iPhone 一样：还没设截止时间就选「提前提醒」或「重复」时，自动设为今天下一个整点
     （晚上 11 点以后设为明天 09:00），之后仍可再改。 */
  function ensureDueFor(select) {
    var match = /^(new|d)-(early|repeat)$/.exec(select.id);
    if (!match || !select.value) { return; }
    var due = $("#" + match[1] + "-due");
    if (due.value) { return; }
    var when = new Date();
    if (when.getHours() >= 23) { when.setDate(when.getDate() + 1); when.setHours(9, 0, 0, 0); }
    else { when.setHours(when.getHours() + 1, 0, 0, 0); }
    due.value = formatLocal(when);
    showToast("已把截止时间设为" + friendlyDate(when) + "，可以再改", "ok",
      match[1] === "new" ? "#create-feedback" : "#detail-feedback");
  }

  function pickerPanel() {
    var panel = $("#dt-panel");
    if (!panel) {
      panel = el("div", "dt-panel");
      panel.id = "dt-panel";
      panel.setAttribute("role", "dialog");
      panel.hidden = true;
      panel.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { event.stopPropagation(); closePicker(true); }
      });
    }
    return panel;
  }

  function showPicker(picker) {
    closePicker(false);
    var date = parseLocal(picker.input.value) || new Date();
    picker.month = new Date(date.getFullYear(), date.getMonth(), 1);
    openPicker = picker;
    var panel = pickerPanel();
    panel.remove();
    picker.holder.appendChild(panel);
    panel.setAttribute("aria-label", "选择" + picker.name);
    panel.hidden = false;
    picker.trigger.setAttribute("aria-expanded", "true");
    document.body.classList.add("has-sheet");
    renderPanel(picker);
    var focus = panel.querySelector(".is-selected") || panel.querySelector(".is-today") || panel.querySelector("button");
    if (focus) { focus.focus({ preventScroll: true }); }
    // 桌面端面板挂在输入框下方，靠近页面底部时滚动到完整可见；手机端是底部弹层，无需滚动。
    if (window.innerWidth > 560 && panel.scrollIntoView) { panel.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }

  function closePicker(restoreFocus) {
    var picker = openPicker;
    if (!picker) { return; }
    openPicker = null;
    pickerPanel().hidden = true;
    picker.trigger.setAttribute("aria-expanded", "false");
    document.body.classList.remove("has-sheet");
    if (restoreFocus) { picker.trigger.focus(); }
  }

  function setPicked(picker, date, focusSelector) {
    picker.input.value = formatLocal(date);
    picker.input.removeAttribute("aria-invalid");
    refreshPickers();
    // 面板重绘后原按钮已移除，把焦点放回新的选中项，键盘用户不会丢位置。
    var panel = pickerPanel();
    var target = openPicker === picker ? panel.querySelector(focusSelector || ".dt-day.is-selected") || panel.querySelector("button") : null;
    if (target) { target.focus(); }
  }

  function chip(label, className, onClick) {
    var button = el("button", className, label);
    button.type = "button";
    button.addEventListener("click", onClick);
    return button;
  }

  function renderPanel(picker) {
    var panel = pickerPanel();
    panel.replaceChildren();
    var current = parseLocal(picker.input.value);
    var hours = current ? current.getHours() : 9;
    var minutes = current ? current.getMinutes() : 0;
    function onDay(day) {
      return function () {
        var h = hours;
        var now = new Date();
        // 还没选过时间时，「今天」默认下一个整点，避免一选就是过去的 09:00。
        if (!current && sameDay(day, now)) { h = Math.min(now.getHours() + 1, 23); }
        setPicked(picker, new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, current ? minutes : 0), ".dt-day.is-selected");
      };
    }

    // 快捷：常用日期。
    var quick = el("div", "dt-quick");
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var nextMonday = new Date(today.getTime());
    nextMonday.setDate(today.getDate() + ((8 - today.getDay()) % 7 || 7));
    [["今天", 0], ["明天", 1], ["后天", 2]].forEach(function (item) {
      var day = new Date(today.getTime()); day.setDate(today.getDate() + item[1]);
      quick.appendChild(chip(item[0], "dt-chip", onDay(day)));
    });
    quick.appendChild(chip("下周一", "dt-chip", onDay(nextMonday)));
    panel.appendChild(quick);

    // 月历
    var head = el("div", "dt-head");
    var month = picker.month;
    head.appendChild(chip("‹", "dt-nav", function () {
      picker.month = new Date(month.getFullYear(), month.getMonth() - 1, 1); renderPanel(picker);
    }));
    head.appendChild(el("span", "dt-month", month.getFullYear() + "年" + (month.getMonth() + 1) + "月"));
    head.appendChild(chip("›", "dt-nav", function () {
      picker.month = new Date(month.getFullYear(), month.getMonth() + 1, 1); renderPanel(picker);
    }));
    head.childNodes[0].setAttribute("aria-label", "上个月");
    head.childNodes[2].setAttribute("aria-label", "下个月");
    panel.appendChild(head);

    var grid = el("div", "dt-grid");
    "一二三四五六日".split("").forEach(function (name) { grid.appendChild(el("span", "dt-weekday", name)); });
    var offset = (month.getDay() + 6) % 7;
    var cursor = new Date(month.getFullYear(), month.getMonth(), 1 - offset);
    for (var index = 0; index < 42; index += 1) {
      var day = new Date(cursor.getTime());
      var classes = "dt-day" + (day.getMonth() !== month.getMonth() ? " is-outside" : "") +
        (sameDay(day, today) ? " is-today" : "") + (current && sameDay(day, current) ? " is-selected" : "") +
        (day < today ? " is-past" : "");
      var button = chip(String(day.getDate()), classes, onDay(day));
      button.setAttribute("aria-label", (day.getMonth() + 1) + "月" + day.getDate() + "日 周" + WEEKDAYS.charAt(day.getDay()));
      grid.appendChild(button);
      cursor.setDate(cursor.getDate() + 1);
    }
    panel.appendChild(grid);

    // 时间：常用时刻 + 小时 / 分钟两列
    var time = el("div", "dt-time");
    var presets = el("div", "dt-presets");
    ["09:00", "12:00", "14:00", "18:00", "21:00"].forEach(function (value) {
      var active = current && pad2(hours) + ":" + pad2(minutes) === value;
      presets.appendChild(chip(value, "dt-chip" + (active ? " is-selected" : ""), function () {
        var base = current || new Date(today.getTime());
        setPicked(picker, new Date(base.getFullYear(), base.getMonth(), base.getDate(), +value.slice(0, 2), +value.slice(3)));
      }));
    });
    time.appendChild(presets);
    // 滚轮：小时 / 分钟两列，滚动吸附到中间一行即选中（也可点选或用方向键）。
    function applyTime(h, m, unit) {
      var base = current || new Date(today.getTime());
      setPicked(picker, new Date(base.getFullYear(), base.getMonth(), base.getDate(), h, m), ".dt-wheel[data-unit='" + unit + "']");
    }
    var wheels = el("div", "dt-wheels");
    wheels.appendChild(wheel("h", 24, 1, hours, function (h) { applyTime(h, minutes, "h"); }, "小时"));
    wheels.appendChild(el("span", "dt-colon", ":"));
    wheels.appendChild(wheel("m", 60, 5, Math.min(55, Math.round(minutes / 5) * 5), function (m) { applyTime(hours, m, "m"); }, "分钟"));
    var band = el("span", "dt-band");
    band.setAttribute("aria-hidden", "true");
    wheels.appendChild(band);
    time.appendChild(wheels);
    panel.appendChild(time);

    var foot = el("div", "dt-foot");
    foot.appendChild(chip("清除", "btn ghost", function () {
      picker.input.value = ""; refreshPickers(); closePicker(true);
    }));
    foot.appendChild(chip("完成", "btn primary", function () { closePicker(true); }));
    panel.appendChild(foot);

    // 面板挂到页面后再把滚轮定位到当前值（此前没有布局尺寸）。
    Array.prototype.forEach.call(panel.querySelectorAll(".dt-wheel"), function (list) {
      if (list.syncScroll) { list.syncScroll(); }
    });
  }

  var WHEEL_ITEM = 36;

  /* 一列滚轮。只在用户真正滚动 / 点按 / 按键后才写回数值，打开面板不会凭空设定时间。 */
  function wheel(unit, count, step, selected, apply, label) {
    var list = el("div", "dt-wheel");
    list.dataset.unit = unit;
    list.tabIndex = 0;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", label);
    var values = [];
    for (var value = 0; value < count; value += step) { values.push(value); }
    var items = values.map(function (item) {
      var option = el("div", "dt-wheel-item", pad2(item));
      option.setAttribute("role", "option");
      option.addEventListener("click", function () { armed = true; scrollToIndex(values.indexOf(item), true); });
      list.appendChild(option);
      return option;
    });
    var index = Math.max(0, values.indexOf(selected));
    var armed = false;
    var timer = null;
    function mark(position) {
      items.forEach(function (option, at) {
        option.classList.toggle("is-selected", at === position);
        option.setAttribute("aria-selected", String(at === position));
      });
      list.setAttribute("aria-valuetext", pad2(values[position]));
    }
    function scrollToIndex(position, smooth) {
      position = Math.max(0, Math.min(values.length - 1, position));
      if (list.scrollTo) { list.scrollTo({ top: position * WHEEL_ITEM, behavior: smooth ? "smooth" : "auto" }); }
      else { list.scrollTop = position * WHEEL_ITEM; }
      mark(position);
    }
    function commit() {
      var position = Math.max(0, Math.min(values.length - 1, Math.round(list.scrollTop / WHEEL_ITEM)));
      mark(position);
      if (armed && position !== index) { index = position; apply(values[position]); }
    }
    ["wheel", "touchstart", "pointerdown"].forEach(function (type) {
      list.addEventListener(type, function () { armed = true; }, { passive: true });
    });
    list.addEventListener("scroll", function () {
      mark(Math.round(list.scrollTop / WHEEL_ITEM));
      clearTimeout(timer);
      timer = setTimeout(commit, 140);
    });
    list.addEventListener("keydown", function (event) {
      var delta = { ArrowUp: -1, ArrowDown: 1, PageUp: -4, PageDown: 4 }[event.key];
      if (!delta) { return; }
      event.preventDefault();
      armed = true;
      scrollToIndex(Math.round(list.scrollTop / WHEEL_ITEM) + delta, false);
      commit();
    });
    list.syncScroll = function () { scrollToIndex(index, false); };
    mark(index);
    return list;
  }

  function initPickers() {
    Array.prototype.forEach.call(document.querySelectorAll(".seg"), enhanceSelect);
    Array.prototype.forEach.call(document.querySelectorAll(".dt"), enhanceDateTime);
    document.addEventListener("click", function (event) {
      if (!openPicker || !event.target.closest) { return; }
      if (!event.target.closest("#dt-panel") && !event.target.closest(".dt-trigger") && event.target.isConnected) { closePicker(false); }
    });
    refreshPickers();
  }

  function setBusy(button, busy, label) {
    if (busy) { button.dataset.idleLabel = button.textContent; }
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    button.classList.toggle("is-loading", busy);
    setText(button, busy ? label || "正在提交…" : button.dataset.idleLabel);
  }

  function formMessage(id, message, kind, field) {
    var node = $(id);
    setText(node, message);
    node.hidden = !message;
    node.className = "form-message" + (kind ? " " + kind : "");
    if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
  }

  function validEmail(value) { return value.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value); }
  function validPassword(value) { var length = Array.from(value).length; return length >= 8 && length <= 128; }

  function concealPasswords() {
    document.querySelectorAll(".password-toggle").forEach(function (button) {
      $("#" + button.dataset.password).type = "password";
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", "显示" + $("label[for='" + button.dataset.password + "']").textContent);
      setText(button, "显示");
    });
  }

  function updateAccount() {
    $("#account-nav").hidden = !state.user;
    $("#workspace-nav").hidden = !state.user;
    if (!state.user) { closeAccount(false); }
    $("#verification-banner").hidden = !state.user || state.user.email_verified !== false;
    setText($("#me-email"), state.user ? state.user.name || state.user.username || state.user.email : "");
    setText($("#set-email"), state.user ? state.user.email : "");
    $("#feed-url").value = state.feedUrl;
    renderDav(false);
    // 邮件设置入口只依据服务端下发的 is_admin；客户端字段不能提权。
    var admin = !!state.user && state.user.is_admin === true;
    var mailLink = $("#nav-mail-settings");
    if (mailLink) {
      mailLink.hidden = !admin;
      if (admin) {
        var badge = mailLink.querySelector(".badge");
        setText(mailLink, badge ? "邮件设置 " : "邮件设置");
        if (badge) { mailLink.appendChild(badge); }
        mailLink.setAttribute("aria-controls", "mail-settings");
      }
    }
    if (admin) {
      $("#mail-settings").hidden = false;
    } else {
      hideMailSettings();
    }
  }

  // ------------------------------------------------------------ 提示条

  function showToast(message, kind, target) {
    var toast = $("#toast");
    if (!toast) { return; }
    // 行内反馈不遮挡任务、表单或软键盘；路由切换时归位到公共反馈区。
    toast.remove();
    ($(target || "#page-feedback") || $("#page-feedback")).appendChild(toast);
    setText(toast, message);
    toast.className = "toast" + (kind ? " " + kind : "");
    toast.hidden = false;
    if (state.toastTimer) { clearTimeout(state.toastTimer); }
    state.toastTimer = setTimeout(function () { toast.hidden = true; }, 6000);
  }

  function hideToast() {
    var toast = $("#toast");
    if (toast) { toast.hidden = true; }
  }

  // ------------------------------------------------------------ 确认框

  function confirmDialog(message, confirmLabel) {
    return new Promise(function (resolve) {
      state.confirmFocus = document.activeElement;
      state.confirmResolve = resolve;
      setText($("#confirm-text"), message);
      setText($("#confirm-ok"), confirmLabel || "确认");
      $("#confirm").hidden = false;
      document.body.classList.add("has-modal");
      $("#app").inert = true;
      $(".site-header").inert = true;
      $("#verification-banner").inert = true;
      $("#confirm-cancel").focus();
    });
  }

  function closeConfirm(answer) {
    var resolve = state.confirmResolve;
    state.confirmResolve = null;
    $("#confirm").hidden = true;
    document.body.classList.remove("has-modal");
    $("#app").inert = false;
    $(".site-header").inert = false;
    $("#verification-banner").inert = false;
    if (state.confirmFocus && state.confirmFocus.isConnected) { state.confirmFocus.focus(); }
    state.confirmFocus = null;
    if (resolve) { resolve(answer); }
  }

  // ------------------------------------------------------------ API
  //
  // 所有请求都用 ./api/xxx 形式的相对路径：页面在 /todos/ 下时会解析成 /todos/api/...，
  // 部署在根路径时就是 /api/...，因此无需关心 EASYLIFE_BASE_PATH。

  function ApiError(status, code, message, retryAfter) {
    this.status = status;
    this.code = code;
    this.message = message;
    this.retryAfter = retryAfter || 60;
  }

  function errorBody(response) {
    return response.json().then(function (data) {
      if (data && data.error) {
        return { code: data.error.code || "error", message: data.error.message || "" };
      }
      return { code: "error", message: "" };
    }).catch(function () {
      return { code: "error", message: "" };
    });
  }

  function api(method, path, payload, request) {
    var options = { method: method, credentials: "same-origin", cache: "no-store", headers: {} };
    if (typeof Blob !== "undefined" && payload instanceof Blob) {
      options.headers["Content-Type"] = payload.type || "application/octet-stream";
      options.body = payload;                     // 图片上传：原样发送文件内容
    } else if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    if (state.csrf && method !== "GET" && method !== "HEAD") {
      options.headers["X-CSRF-Token"] = state.csrf;
    }
    var expired = false;
    var timer;
    var controller = request && typeof AbortController !== "undefined" ? new AbortController() : null;
    if (controller) { options.signal = controller.signal; }
    var pending = fetch(path, options).then(function (response) {
      if (response.status === 401 && path !== "./api/login") {
        return errorBody(response).then(function (info) {
          var message = info.message || "登录状态已失效，请重新登录";
          // A late mail response must not sign out a newer session/page, including after timeout.
          if (!expired && (!request || request.current())) { handleUnauthorized(message); }
          throw new ApiError(401, info.code, message);
        });
      }
      if (!response.ok) {
        return errorBody(response).then(function (info) {
          throw new ApiError(response.status, info.code,
            info.message || "请求失败（HTTP " + response.status + "）", Number(response.headers.get("Retry-After")));
        });
      }
      var type = response.headers.get("Content-Type") || "";
      if (type.indexOf("application/json") === 0) { return response.json(); }
      return response.text();
    }, function () {
      throw new ApiError(0, "network", "网络请求失败，请检查连接后重试");
    });
    if (!request) { return pending; }
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () {
        expired = true;
        reject(new ApiError(0, "timeout", "等待响应超时"));
        if (controller) { controller.abort(); }
      }, 30000);
    });
    return Promise.race([pending, timeout]).then(function (data) {
      clearTimeout(timer);
      return data;
    }, function (error) {
      clearTimeout(timer);
      throw error;
    });
  }

  function fail(error) {
    if (error && error.status === 401) { return; }
    showToast((error && error.message) || "操作失败", "error");
  }

  function handleUnauthorized(message) {
    clearSession();
    showToast(message || "登录状态已失效，请重新登录", "error");
    go("#/login");
  }

  function clearSession() {
    state.user = null;
    state.csrf = null;
    state.feedUrl = "";
    state.dav = null;
    state.todos = [];
    state.current = null;
    state.lists = [];
    state.listsRequest += 1;
    setText($("#me-email"), "");
    setText($("#set-email"), "");
    $("#feed-url").value = "";
    state.listRequest += 1;
    clearMailSecrets();
    state.mailTestUntil = 0;
    updateAccount();
  }

  // ------------------------------------------------------------ 视图切换

  function showView(name) {
    closeLightbox();
    var previous = state.view;
    if (state.view === "reset" && name !== "reset") {
      $("#recovery-password").value = "";
      $("#recovery-confirm").value = "";
    }
    state.view = name;
    closeAccount(false);
    closePicker(false);
    ["auth", "check-email", "verify", "forgot", "reset", "list", "detail", "settings"].forEach(function (view) {
      $("#view-" + view).hidden = name !== view;
    });
    var toast = $("#toast");
    toast.remove();
    $("#page-feedback").appendChild(toast);
    $("#btn-settings").setAttribute("aria-expanded", String(name === "settings"));
    $("#nav-settings").classList.toggle("is-active", name === "settings");
    if (name === "list" || name === "detail") { $("#nav-todos").setAttribute("aria-current", "page"); }
    else { $("#nav-todos").removeAttribute("aria-current"); }
    concealPasswords();
    updateAccount();
    window.scrollTo(0, 0);
    var heading = $("#view-" + name).querySelector("h1");
    if (heading) { heading.focus(); }
    updateResendButtons();
    // 进入设置页读取当前保存的邮件配置；离开设置页立即清空授权码输入。
    if (name === "settings" && previous !== "settings") { loadMailSettings(); }
    else if (previous === "settings" && name !== "settings") { clearMailSecrets(); renderDav(false); }
  }

  function setAuthMode(mode) {
    var register = mode === "register";
    state.authMode = register ? "register" : "login";
    ["login", "register"].forEach(function (name) {
      var active = state.authMode === name;
      var tab = $("#tab-" + name);
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.setAttribute("tabindex", active ? "0" : "-1");
    });
    $("#auth-panel").setAttribute("aria-labelledby", "tab-" + state.authMode);
    $("#auth-forgot").hidden = register;
    $("#auth-resend").hidden = register || !state.offerResend;
    setText($("#auth-title"), register ? "从 EasyLife 开始" : "欢迎回到 EasyLife");
    setText($("#auth-submit"), register ? "创建账号" : "登录");
    setText($("#auth-hint"), register ? "密码为 8–128 个字符。建议混合字母、数字和符号。" : "密码为 8–128 个字符。");
    $("#auth-password").setAttribute("autocomplete", register ? "new-password" : "current-password");
  }

  function showAuth(mode) {
    setAuthMode(mode === "register" ? "register" : "login");
    formMessage("#auth-error", "");
    showView("auth");
    $("#auth-email").focus();
  }

  /* 沿用 hash 路由，邮件链接无需服务器增加路径。 */
  function go(hash) {
    if (location.hash === hash) { applyHash(); }
    else { location.hash = hash; }
  }

  function applyHash() {
    if (!state.ready) { return; }
    var hash = location.hash || "";
    var emailLink = /^#\/(verify|reset)(?:\?(.*))?$/.exec(hash);
    state.authToken = emailLink ? new URLSearchParams(emailLink[2] || "").get("token") || "" : "";
    if (emailLink) {
      var mode = emailLink[1];
      showView(mode);
      if (mode === "verify") { submitVerification(); }
      else {
        $("#reset-submit").disabled = !state.authToken;
        $("#reset-request-link").hidden = !!state.authToken;
        formMessage("#reset-message", state.authToken ? "" : "重置链接缺少令牌，请重新获取链接。", "error");
        $("#recovery-password").focus();
      }
      return;
    }
    if (hash === "#/forgot-password") {
      showView("forgot");
      formMessage("#forgot-message", "");
      $("#recovery-email").focus();
      return;
    }
    if (hash === "#/check-email") {
      setText($("#check-email-address"), state.pendingEmail || (state.user && state.user.email) || "你的邮箱");
      $("#check-email-continue").href = state.user ? "#/todos" : "#/login";
      setText($("#check-email-continue"), state.user ? "继续使用 EasyLife" : "返回登录");
      showView("check-email");
      return;
    }
    if (!state.user) { showAuth(hash === "#/register" ? "register" : "login"); return; }
    var settingsMatch = /^#\/settings(\/mail)?$/.exec(hash);
    if (settingsMatch) {
      showView("settings");
      var mailSection = settingsMatch[1] === "/mail";
      updateSettingsSections(mailSection);
      if (mailSection && !$("#mail-settings").hidden) {
        $("#mail-settings").scrollIntoView();
        $("#mail-settings-heading").focus();
      }
      return;
    }
    var match = /^#\/todos\/(\d{1,20})$/.exec(hash);
    if (match) { openDetail(Number(match[1])); return; }
    if (hash !== "#/todos") { go("#/todos"); return; }
    state.current = null;
    showView("list");
    loadTodos();
  }

  // ------------------------------------------------------------ 会话

  function refreshSession() {
    return fetch("./api/me", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status !== 200) { return false; }
        return response.json().then(function (data) {
          state.user = data.user;
          state.csrf = data.csrf_token;
          state.feedUrl = data.calendar_feed_url || "";
          state.dav = data.dav || null;
          updateAccount();
          return true;
        });
      })
      .catch(function () { return false; });
  }

  // ------------------------------------------------------------ 登录 / 注册 / 登出

  function submitAuth(event) {
    event.preventDefault();
    var button = $("#auth-submit");
    if (button.disabled) { return; }
    var email = $("#auth-email").value.trim();
    var password = $("#auth-password").value;
    formMessage("#auth-error", "");
    if (!validEmail(email)) { formMessage("#auth-error", "请输入有效的邮箱地址", "error", $("#auth-email")); return; }
    if (!validPassword(password)) { formMessage("#auth-error", "密码长度需为 8–128 个字符", "error", $("#auth-password")); return; }
    var register = state.authMode === "register";
    setBusy(button, true, register ? "正在创建账号…" : "正在登录…");
    api("POST", register ? "./api/register" : "./api/login", { email: email, password: password })
      .then(function (data) {
        $("#auth-password").value = "";
        if (register) {
          state.pendingEmail = email;
          startResendCooldown(email, 60);
          if (data.verification_required) { clearSession(); }
          else { state.user = data.user; state.csrf = data.csrf_token; }
          formMessage("#check-email-message", data.message || "请点击邮件中的链接完成验证。如果未收到，可在倒计时结束后重发。");
          // 无 SMTP 时后端允许继续使用；保留该会话，同时展示验证提醒。
          go("#/check-email");
          if (state.user) { return refreshSession(); }
          return;
        }
        state.user = data.user;
        state.csrf = data.csrf_token;
        updateAccount();
        showToast("登录成功", "ok");
        return refreshSession().then(function () { go("#/todos"); });
      })
      .catch(function (error) {
        if (error.code === "email_not_verified") {
          state.pendingEmail = email;
          state.offerResend = true;
          $("#auth-resend").hidden = false;
          updateResendButtons();
        }
        formMessage("#auth-error", error.message || "登录失败，请重试", "error");
        showToast(error.message || "操作失败", "error");
      })
      .then(function () { setBusy(button, false); });
  }

  function resendEmail() {
    if (state.user) { return state.user.email; }
    return state.view === "auth" ? $("#auth-email").value.trim() : state.pendingEmail;
  }

  function startResendCooldown(email, seconds) {
    state.resendUntil[email.toLowerCase()] = Date.now() + Math.max(1, seconds) * 1000;
    updateResendButtons();
  }

  function updateResendButtons() {
    clearTimeout(state.resendTimer);
    var email = resendEmail() || "";
    var seconds = Math.max(0, Math.ceil(((state.resendUntil[email.toLowerCase()] || 0) - Date.now()) / 1000));
    document.querySelectorAll("[data-resend]").forEach(function (button) {
      button.disabled = state.resendBusy || seconds > 0;
      button.setAttribute("aria-busy", String(state.resendBusy));
      button.classList.toggle("is-loading", state.resendBusy);
      var compact = button.id === "banner-resend";
      setText(button, state.resendBusy ? "正在发送…" : seconds ? (compact ? seconds + " 秒后重发" : "重新发送（" + seconds + " 秒）") : compact ? "重新发送" : "重新发送验证邮件");
    });
    if (seconds > 0) { state.resendTimer = setTimeout(updateResendButtons, 1000); }
  }

  function resendVerification() {
    var email = resendEmail() || "";
    if (state.resendBusy || (state.resendUntil[email.toLowerCase()] || 0) > Date.now()) { return; }
    if (!validEmail(email)) {
      showToast("请先填写有效的邮箱地址", "error");
      go("#/login");
      $("#auth-resend").hidden = false;
      return;
    }
    state.pendingEmail = email;
    state.resendBusy = true;
    updateResendButtons();
    api("POST", "./api/resend-verification", { email: email })
      .then(function (data) {
        startResendCooldown(email, 60);
        formMessage("#check-email-message", data.message, "ok");
        showToast(data.message, "ok");
      }).catch(function (error) {
        if (error.code === "rate_limited") { startResendCooldown(email, error.retryAfter); }
        formMessage("#check-email-message", error.message, "error");
        fail(error);
      }).then(function () { state.resendBusy = false; updateResendButtons(); });
  }

  function renderVerification(result) {
    if (state.view !== "verify" || result.token !== state.authToken) { return; }
    var pending = result.status === "pending";
    var success = result.status === "success";
    setText($("#verify-title"), pending ? "正在验证邮箱" : success ? "邮箱验证成功" : result.code === "invalid_token" ? "验证链接无效或已过期" : "暂时无法验证");
    setText($("#verify-symbol"), pending ? "…" : success ? "✓" : "!");
    formMessage("#verify-message", result.message, pending ? "" : success ? "ok" : "error");
    $("#verify-panel").setAttribute("aria-busy", String(pending));
    $("#verify-retry").hidden = pending || success || result.code === "invalid_token";
    $("#verify-continue").hidden = !success;
    $("#verify-continue").href = state.user ? "#/todos" : "#/login";
    setText($("#verify-continue"), state.user ? "返回任务列表" : "返回登录");
    $("#verify-resend-link").hidden = pending || success;
    $("#verify-resend-link").href = state.user ? "#/todos" : "#/login";
    setText($("#verify-resend-link"), state.user ? "返回任务列表并重新发送" : "返回登录并重新发送验证邮件");
  }

  function submitVerification(retry) {
    var token = state.authToken;
    if (state.verification && state.verification.token === token && !retry) {
      renderVerification(state.verification);
      return;
    }
    var result = { token: token, status: "pending", message: "正在确认你的邮箱，请稍候…" };
    state.verification = result;
    if (!token) {
      result.status = "error"; result.code = "invalid_token";
      result.message = "验证链接缺少令牌，请重新发送验证邮件。";
      renderVerification(result);
      state.offerResend = true;
      $("#auth-resend").hidden = false;
      return;
    }
    renderVerification(result);
    // 自动 POST 验证；加载页面的 GET 本身不消费一次性令牌。
    api("POST", "./api/verify-email", { token: token }).then(function (data) {
      result.status = "success"; result.message = data.message;
      // 令牌可能属于另一个邮箱，重新读取当前用户，不能直接假定当前账号已验证。
      if (state.user) { return refreshSession(); }
    }).catch(function (error) {
      result.status = "error"; result.code = error.code; result.message = error.message;
      state.offerResend = true;
      $("#auth-resend").hidden = false;
      if (error.code === "invalid_token") { result.message = "链接无效、已使用或已过期。请重新发送验证邮件后再试。"; }
    }).then(function () { renderVerification(result); });
  }

  function submitForgot(event) {
    event.preventDefault();
    var button = $("#forgot-submit");
    if (button.disabled) { return; }
    var email = $("#recovery-email").value.trim();
    if (!validEmail(email)) { formMessage("#forgot-message", "请输入有效的邮箱地址", "error", $("#recovery-email")); return; }
    formMessage("#forgot-message", "");
    setBusy(button, true, "正在发送…");
    api("POST", "./api/forgot-password", { email: email }).then(function () {
      var message = "如果该邮箱已注册，我们已发送重置链接";
      formMessage("#forgot-message", message, "ok");
      showToast(message, "ok");
    }).catch(function (error) { formMessage("#forgot-message", error.message, "error"); fail(error); })
      .then(function () { setBusy(button, false); });
  }

  function submitReset(event) {
    event.preventDefault();
    var button = $("#reset-submit");
    if (button.disabled) { return; }
    var password = $("#recovery-password").value;
    if (!validPassword(password)) { formMessage("#reset-message", "密码长度需为 8–128 个字符", "error", $("#recovery-password")); return; }
    if (password !== $("#recovery-confirm").value) { formMessage("#reset-message", "两次输入的密码不一致", "error", $("#recovery-confirm")); return; }
    formMessage("#reset-message", "");
    setBusy(button, true, "正在重置…");
    api("POST", "./api/reset-password", { token: state.authToken, password: password }).then(function (data) {
      clearSession();
      state.authToken = "";
      $("#auth-password").value = "";
      $("#recovery-password").value = "";
      $("#recovery-confirm").value = "";
      go("#/login");
      showToast(data.message, "ok");
    }).catch(function (error) {
      formMessage("#reset-message", error.code === "invalid_token" ? "重置链接无效、已使用或已过期，请重新获取。" : error.message, "error");
      $("#reset-request-link").hidden = error.code !== "invalid_token";
      fail(error);
    }).then(function () { setBusy(button, false); });
  }

  function logout() {
    // Invalidate mail work immediately, even while the logout request is in flight.
    clearMailSecrets();
    api("POST", "./api/logout", {}).then(function () {
      clearSession();
      hideToast();
      go("#/login");
      showToast("已登出", "ok");
    }).catch(function (error) {
      fail(error);
    });
  }

  // ------------------------------------------------------------ Markdown
  //
  // 详情与笔记支持常用 Markdown：标题、粗体 / 斜体 / 删除线、行内代码与代码块、
  // 列表与待办清单、引用、分隔线、链接。只用 createElement + textContent 构建节点，
  // 从不拼接 HTML；链接仅接受 https 绝对地址（safeLink），并在新标签页打开。

  var INLINE = new RegExp([
    "(`+)([^`]+?)\\1",                          // 1,2 行内代码
    "\\*\\*([^*]+?)\\*\\*",                      // 3 粗体
    "~~([^~]+?)~~",                              // 4 删除线
    "\\*([^*\\s][^*]*?)\\*",                     // 5 斜体
    "\\[([^\\]]+)\\]\\((\\S+?)\\)",              // 6,7 链接
    "(https:\\/\\/[^\\s<>()\\u3000-\\u303f\\uff01-\\uff0f]+[^\\s<>().,;:!?\\u3000-\\u303f\\uff01-\\uff0f])"  // 8 裸链接
  ].join("|"));

  function textNode(text) {
    return document.createTextNode ? document.createTextNode(text) : el("span", "", text);
  }

  function appendInline(parent, text) {
    var rest = text;
    var match;
    while (rest && (match = INLINE.exec(rest))) {
      if (match.index) { parent.appendChild(textNode(rest.slice(0, match.index))); }
      if (match[2] !== undefined) { parent.appendChild(el("code", "", match[2])); }
      else if (match[3] !== undefined) { appendInline(parent.appendChild(el("strong")), match[3]); }
      else if (match[4] !== undefined) { appendInline(parent.appendChild(el("del")), match[4]); }
      else if (match[5] !== undefined) { appendInline(parent.appendChild(el("em")), match[5]); }
      else {
        var label = match[6] !== undefined ? match[6] : match[8];
        var url = safeLink(match[6] !== undefined ? match[7] : match[8]);
        if (url) {
          var link = el("a", "", label);
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          parent.appendChild(link);
        } else {
          parent.appendChild(textNode(match[0]));
        }
      }
      rest = rest.slice(match.index + match[0].length);
    }
    if (rest) { parent.appendChild(textNode(rest)); }
    return parent;
  }

  function renderMarkdown(text, className, onTask) {
    var root = el("div", "md" + (className ? " " + className : ""));
    var lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    var paragraph = null;
    var list = null;
    var index = 0;
    function endBlocks() { paragraph = null; list = null; }
    while (index < lines.length) {
      var line = lines[index];
      var fence = /^\s*```/.test(line);
      var heading = /^(#{1,3})\s+(.*)$/.exec(line);
      var item = /^\s*(?:([-*+])|(\d{1,3})[.)])\s+(?:\[([ xX])\]\s+)?(.*)$/.exec(line);
      if (fence) {
        endBlocks();
        var code = [];
        index += 1;
        while (index < lines.length && !/^\s*```/.test(lines[index])) { code.push(lines[index]); index += 1; }
        root.appendChild(el("pre")).appendChild(el("code", "", code.join("\n")));
      } else if (!line.trim()) {
        endBlocks();
      } else if (heading) {
        endBlocks();
        appendInline(root.appendChild(el("h" + (heading[1].length + 2), "md-heading")), heading[2]);
      } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        endBlocks();
        root.appendChild(el("hr"));
      } else if (/^\s*>/.test(line)) {
        endBlocks();
        var quote = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) { quote.push(lines[index].replace(/^\s*>\s?/, "")); index += 1; }
        var block = renderMarkdown(quote.join("\n"));
        var blockquote = el("blockquote");
        Array.prototype.slice.call(block.childNodes).forEach(function (node) { blockquote.appendChild(node); });
        root.appendChild(blockquote);
        continue;
      } else if (item) {
        paragraph = null;
        var tag = item[2] ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) { list = root.appendChild(el(tag)); }
        var li = list.appendChild(el("li"));
        if (item[3] !== undefined) {
          var done = item[3] !== " ";
          li.className = "md-task" + (done ? " is-checked" : "");
          var box;
          if (onTask) {
            // 详情预览里的清单可以直接勾选：按行号改回原文并保存。
            box = el("button", "md-box");
            box.type = "button";
            box.setAttribute("aria-pressed", String(done));
            box.setAttribute("aria-label", (done ? "取消完成：" : "标记完成：") + plainSnippet(item[4]));
            box.addEventListener("click", (function (line) {
              return function (event) { event.stopPropagation(); onTask(line); };
            })(index));
          } else {
            box = el("span", "md-box");
            box.setAttribute("aria-hidden", "true");
          }
          li.appendChild(box);
        }
        appendInline(li.appendChild(el("span")), item[4]);
      } else {
        list = null;
        if (paragraph) { paragraph.appendChild(el("br")); }
        else { paragraph = root.appendChild(el("p")); }
        appendInline(paragraph, line.trim());
      }
      index += 1;
    }
    return root;
  }

  /* 列表摘要只要一行纯文本：去掉 Markdown 标记。 */
  function plainSnippet(text) {
    var line = String(text || "").split("\n").filter(function (value) {
      return value.trim() && !/^\s*```/.test(value);
    })[0] || "";
    return line.replace(/^\s*(#{1,3}|>|[-*+]|\d{1,3}[.)])\s+/, "").replace(/^\[[ xX]\]\s+/, "")
      .replace(/\[([^\]]+)\]\(\S+?\)/g, "$1").replace(/(\*\*|~~|`|\*)/g, "").trim();
  }

  /* 文本框随内容长高，不再出现小框里滚动和右下角拖拽角。 */
  function autoGrow(area) {
    if (!area || !area.style || !area.scrollHeight) { return; }
    area.style.height = "auto";
    area.style.height = (area.scrollHeight + 2) + "px";
  }

  /* 「编辑 / 预览」切换：任务详情（#d-details）与首页新建的详情（#new-details）共用。 */
  function setMdMode(id, preview) {
    var area = $("#" + id);
    var view = $("#" + id + "-preview");
    if (preview) {
      view.replaceChildren();
      var onTask = id === "d-details" ? toggleChecklist : function (line) { toggleDraftChecklist(id, line); };
      var rendered = renderMarkdown(area.value, "", onTask);
      Array.prototype.slice.call(rendered.childNodes).forEach(function (node) { view.appendChild(node); });
      if (!area.value.trim()) { view.appendChild(el("p", "md-empty", "还没有内容，点击这里填写。")); }
    }
    view.hidden = !preview;
    area.hidden = preview;
    $("#" + id + "-toolbar").hidden = preview;
    $("#" + id + "-edit").setAttribute("aria-pressed", String(!preview));
    $("#" + id + "-view").setAttribute("aria-pressed", String(preview));
    if (!preview) { autoGrow(area); }
  }

  function setDetailsMode(preview) { setMdMode("d-details", preview); }

  /* 新建任务尚未保存：勾选只改草稿文字。 */
  function toggleDraftChecklist(id, line) {
    var area = $("#" + id);
    var lines = area.value.replace(/\r\n?/g, "\n").split("\n");
    if (lines[line] === undefined) { return; }
    lines[line] = lines[line].replace(/\[([ xX])\]/, function (match, mark) { return mark === " " ? "[x]" : "[ ]"; });
    area.value = lines.join("\n");
    setMdMode(id, true);
    var focus = $("#" + id + "-preview").querySelectorAll(".md-box")[taskOrdinal(area.value, line)];
    if (focus) { focus.focus(); }
  }

  function toggleChecklist(line) {
    var todo = state.current;
    var area = $("#d-details");
    var previous = area.value;
    var lines = previous.replace(/\r\n?/g, "\n").split("\n");
    if (!todo || lines[line] === undefined) { return; }
    lines[line] = lines[line].replace(/\[([ xX])\]/, function (match, mark) { return mark === " " ? "[x]" : "[ ]"; });
    area.value = lines.join("\n");
    setDetailsMode(true);
    var focus = $("#d-details-preview").querySelectorAll(".md-box")[taskOrdinal(area.value, line)];
    if (focus) { focus.focus(); }
    api("PUT", "./api/todos/" + todo.id, { details: area.value }).then(function (updated) {
      if (state.current && state.current.id === updated.id) { state.current.details = updated.details; }
    }).catch(function (error) {
      // 保存失败时恢复原文，避免界面与服务器不一致。
      if (state.current && state.current.id === todo.id && area.value !== previous) {
        area.value = previous;
        setDetailsMode(true);
      }
      fail(error);
    });
  }

  function taskOrdinal(text, line) {
    return text.split("\n").slice(0, line).filter(function (value) {
      return /^\s*(?:[-*+]|\d{1,3}[.)])\s+\[[ xX]\]/.test(value);
    }).length;
  }

  // 编辑工具栏：选中文字后加粗 / 斜体等；不选文字时插入示例并选中，方便直接改。
  var MD_ACTIONS = [
    { label: "B", title: "粗体 (Ctrl+B)", wrap: ["**", "**"], key: "b", className: "is-bold" },
    { label: "I", title: "斜体 (Ctrl+I)", wrap: ["*", "*"], key: "i", className: "is-italic" },
    { label: "S", title: "删除线", wrap: ["~~", "~~"], className: "is-strike" },
    { label: "H", title: "标题", prefix: "## " },
    { label: "•", title: "列表", prefix: "- " },
    { label: "1.", title: "编号列表", prefix: "1. " },
    { label: "☑", title: "待办清单", prefix: "- [ ] " },
    { label: "❝", title: "引用", prefix: "> " },
    { label: "</>", title: "代码", wrap: ["`", "`"] },
    { label: "链", title: "链接", link: true }
  ];

  function applyMarkdown(area, action) {
    var value = area.value;
    var start = area.selectionStart || 0;
    var end = area.selectionEnd || start;
    var selected = value.slice(start, end);
    var before = value.slice(0, start);
    var after = value.slice(end);
    var selectFrom;
    var selectTo;
    if (action.wrap || action.link) {
      var text = selected || (action.link ? "链接文字" : "文字");
      var open = action.link ? "[" : action.wrap[0];
      var close = action.link ? "](" + "https:" + "//)" : action.wrap[1];
      area.value = before + open + text + close + after;
      selectFrom = start + open.length;
      selectTo = selectFrom + text.length;
      if (action.link && selected) { selectFrom = selectTo = start + open.length + text.length + close.length - 1; }
    } else {
      var lineStart = value.lastIndexOf("\n", start - 1) + 1;
      var lineEnd = value.indexOf("\n", end);
      if (lineEnd < 0) { lineEnd = value.length; }
      var block = value.slice(lineStart, lineEnd).split("\n");
      var marker = /^(#{1,3} |> |[-*+] \[[ xX]\] |[-*+] |\d{1,3}[.)] )/;
      var has = block.every(function (line) { return line.indexOf(action.prefix) === 0 || (action.prefix === "1. " && /^\d{1,3}[.)] /.test(line)); });
      var changed = block.map(function (line, at) {
        var bare = line.replace(marker, "");
        if (has) { return bare; }
        return (action.prefix === "1. " ? (at + 1) + ". " : action.prefix) + bare;
      }).join("\n");
      area.value = value.slice(0, lineStart) + changed + value.slice(lineEnd);
      selectFrom = selectTo = lineStart + changed.length;
    }
    area.focus();
    if (area.setSelectionRange) { area.setSelectionRange(selectFrom, selectTo); }
    autoGrow(area);
  }

  function initToolbars() {
    Array.prototype.forEach.call(document.querySelectorAll(".md-toolbar"), function (bar) {
      var area = $("#" + bar.dataset.for);
      if (!area) { return; }
      bar.setAttribute("role", "toolbar");
      bar.setAttribute("aria-label", "格式");
      MD_ACTIONS.forEach(function (action) {
        var button = el("button", "md-tool" + (action.className ? " " + action.className : ""), action.label);
        button.type = "button";
        button.title = action.title;
        button.setAttribute("aria-label", action.title);
        // 按下时不抢走文本框焦点，保留选区。
        button.addEventListener("mousedown", function (event) { event.preventDefault(); });
        button.addEventListener("click", function () { applyMarkdown(area, action); });
        bar.appendChild(button);
      });
      area.addEventListener("keydown", function (event) {
        if (!(event.ctrlKey || event.metaKey) || event.altKey) { return; }
        var action = MD_ACTIONS.filter(function (item) { return item.key === String(event.key).toLowerCase(); })[0];
        if (action) { event.preventDefault(); applyMarkdown(area, action); }
      });
    });
  }

  // ------------------------------------------------------------ 任务列表（项目）

  /* 与「提醒事项」的列表颜色一致，同步到手机后颜色相同。 */
  var LIST_COLORS = ["#2F6FEB", "#FF3B30", "#FF9500", "#FFCC00", "#34C759", "#5AC8FA", "#AF52DE", "#A2845E"];

  function defaultList() {
    return state.lists.filter(function (item) { return item.is_default; })[0] || null;
  }

  /* 任务的 list_id 为 null 表示默认列表。 */
  function listOf(todo) {
    var id = todo && todo.list_id;
    return state.lists.filter(function (item) { return id ? item.id === id : item.is_default; })[0] || null;
  }

  function currentList() {
    return state.lists.filter(function (item) { return item.id === state.listFilter; })[0] || null;
  }

  function saveListFilter() {
    try { localStorage.setItem("easylife-list", String(state.listFilter)); } catch (ignore) { /* 仅本次有效 */ }
  }

  function loadLists() {
    if (!state.user) { return Promise.resolve(); }
    var request = ++state.listsRequest;
    return api("GET", "./api/lists").then(function (data) {
      if (request !== state.listsRequest || !state.user) { return; }
      state.lists = data.items || [];
      // 列表可能已在手机上删除：回到「全部」，避免用失效的列表去筛选。
      if (state.listFilter !== "all" && !currentList()) { state.listFilter = "all"; saveListFilter(); }
      renderLists();
    }, function () { /* 列表加载失败时仍显示任务，由任务请求报告错误 */ });
  }

  function listDot(item) {
    var dot = el("span", "list-dot");
    dot.setAttribute("aria-hidden", "true");
    dot.style.setProperty("--list-color", item.color);
    return dot;
  }

  function fillListSelect(select, value) {
    select.replaceChildren();
    state.lists.forEach(function (item) {
      var option = el("option", "", item.name);
      option.value = String(item.id);
      select.appendChild(option);
    });
    if (value) { select.value = String(value); }
    renderListPicker(select);
  }

  /* 列表选择用一排带颜色圆点的按钮代替原生下拉框；select 仍保存取值。 */
  function renderListPicker(select) {
    var holder = document.querySelector("[data-list-for='" + select.id + "']");
    if (!holder) { return; }
    select.hidden = true;
    holder.setAttribute("role", "group");
    holder.setAttribute("aria-label", labelText(select.id));
    holder.replaceChildren();
    state.lists.forEach(function (item) {
      var button = el("button", "seg-btn list-seg-btn");
      button.type = "button";
      button.dataset.value = String(item.id);
      button.appendChild(listDot(item));
      button.appendChild(el("span", "", item.name));
      var active = String(item.id) === select.value;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.addEventListener("click", function () {
        select.value = button.dataset.value;
        Array.prototype.forEach.call(holder.children, function (node) {
          var on = node === button;
          node.classList.toggle("is-active", on);
          node.setAttribute("aria-pressed", String(on));
        });
      });
      holder.appendChild(button);
    });
  }

  /* 新建任务默认放进正在查看的列表；切换列表后才重置，不覆盖用户刚选的值。 */
  function syncComposerList() {
    var target = currentList() || defaultList();
    if (target) { $("#new-list").value = String(target.id); renderListPicker($("#new-list")); }
    state.composerListFor = state.listFilter;
  }

  function renderLists() {
    var tabs = $("#list-tabs");
    tabs.replaceChildren();
    var multiple = state.lists.length > 1;
    var all = el("button", "chip list-chip", "全部");
    all.type = "button";
    all.dataset.list = "all";
    tabs.appendChild(all);
    state.lists.forEach(function (item) {
      var chip = el("button", "chip list-chip");
      chip.type = "button";
      chip.dataset.list = String(item.id);
      chip.appendChild(listDot(item));
      chip.appendChild(el("span", "", item.name));
      if (item.open_count) { chip.appendChild(el("span", "list-count-badge", item.open_count)); }
      chip.setAttribute("aria-label", item.name + (item.open_count ? "，" + item.open_count + " 件未完成" : ""));
      tabs.appendChild(chip);
    });
    Array.prototype.forEach.call(tabs.children, function (chip) {
      var active = chip.dataset.list === String(state.listFilter);
      chip.classList.toggle("is-active", active);
      chip.setAttribute("aria-pressed", String(active));
    });
    // 只有一个列表时不打扰：不显示「全部」与列表选择。
    tabs.hidden = !multiple;
    var activeChip = tabs.querySelector(".is-active");
    if (multiple && activeChip && activeChip.scrollIntoView) { activeChip.scrollIntoView({ block: "nearest", inline: "nearest" }); }
    $("#btn-list-edit").hidden = !currentList();
    var composerValue = $("#new-list").value;
    fillListSelect($("#new-list"), composerValue);
    if (!$("#new-list").value || state.composerListFor !== state.listFilter) { syncComposerList(); }
    fillListSelect($("#d-list"), state.current ? (listOf(state.current) || {}).id : null);
    Array.prototype.forEach.call(document.querySelectorAll(".list-field"), function (node) { node.hidden = !multiple; });
    // 子任务跟着上级任务所在的列表，不能单独改。
    if (state.current && state.current.parent_id) { $("#d-list-field").hidden = true; }
    var item = currentList();
    setText($("#list-heading"), item ? item.name : "我的任务");
  }

  function selectList(value) {
    state.listFilter = value === "all" ? "all" : Number(value);
    saveListFilter();
    closeListForm();
    renderLists();
    syncComposerList();
    return loadTodos();
  }

  function renderListColors(selected) {
    var holder = $("#list-colors");
    holder.replaceChildren();
    LIST_COLORS.forEach(function (color, index) {
      var swatch = el("button", "list-swatch");
      swatch.type = "button";
      swatch.dataset.color = color;
      swatch.setAttribute("role", "radio");
      swatch.setAttribute("aria-label", "颜色 " + (index + 1));
      swatch.setAttribute("aria-checked", String(color === selected));
      swatch.style.setProperty("--list-color", color);
      holder.appendChild(swatch);
    });
  }

  function openListForm(item) {
    state.listEditing = item ? item.id : null;
    setText($("#list-form-title"), item ? "编辑列表" : "新建列表");
    $("#list-name").value = item ? item.name : "";
    var used = state.lists.map(function (entry) { return entry.color; });
    var fresh = LIST_COLORS.filter(function (color) { return used.indexOf(color) < 0; })[0] || LIST_COLORS[0];
    renderListColors(item ? item.color : fresh);
    $("#list-delete").hidden = !item || item.is_default;
    $("#list-form").hidden = false;
    $("#btn-list-new").setAttribute("aria-expanded", "true");
    $("#list-name").focus();
  }

  function closeListForm() {
    $("#list-form").hidden = true;
    $("#btn-list-new").setAttribute("aria-expanded", "false");
    state.listEditing = null;
  }

  function submitList(event) {
    event.preventDefault();
    var name = $("#list-name").value.trim();
    if (!name) {
      showToast("请输入列表名称", "error", "#list-feedback");
      $("#list-name").focus();
      return;
    }
    var checked = $("#list-colors").querySelector("[aria-checked='true']");
    var payload = { name: name, color: checked ? checked.dataset.color : LIST_COLORS[0] };
    var editing = state.listEditing;
    var button = $("#list-save");
    if (button.disabled) { return; }
    setBusy(button, true, "保存中…");
    (editing ? api("PUT", "./api/lists/" + editing, payload) : api("POST", "./api/lists", payload))
      .then(function (item) {
        closeListForm();
        showToast(editing ? "列表已更新" : "已新建列表「" + item.name + "」", "ok", "#list-feedback");
        if (!editing) { state.listFilter = item.id; saveListFilter(); }
        return loadTodos();
      })
      .catch(function (error) {
        if (error.status !== 401) { showToast(error.message || "保存失败，请重试", "error", "#list-feedback"); }
      })
      .then(function () { setBusy(button, false); });
  }

  function deleteList() {
    var item = state.lists.filter(function (entry) { return entry.id === state.listEditing; })[0];
    var fallback = defaultList();
    if (!item || !fallback) { return; }
    confirmDialog("确定删除列表「" + item.name + "」吗？其中的任务会移到「" + fallback.name + "」，不会被删除。", "删除")
      .then(function (confirmed) {
        if (!confirmed) { return null; }
        return api("DELETE", "./api/lists/" + item.id).then(function (data) {
          closeListForm();
          showToast("列表已删除" + (data.moved ? "，" + data.moved + " 件任务已移到「" + fallback.name + "」" : ""), "ok", "#list-feedback");
          state.listFilter = "all";
          saveListFilter();
          return loadTodos();
        }).catch(function (error) {
          if (error.status !== 401) { showToast(error.message || "删除失败，请重试", "error", "#list-feedback"); }
        });
      });
  }

  // ------------------------------------------------------------ 任务列表

  function loadTodos() {
    if (!state.user) { return Promise.resolve(); }
    var request = ++state.listRequest;
    var filter = state.filter;
    // 日期与旗标在本地筛选：需要全部未归档任务。
    var dateFilter = filter === "today" || filter === "upcoming" || filter === "flagged";
    var items = [];
    var query = new URLSearchParams();
    if (filter !== "all" && !dateFilter) { query.set("status", filter); }
    query.set("limit", "200");
    var listFilter;
    $("#todo-list").setAttribute("aria-busy", "true");
    $("#list-loading").hidden = false;
    $("#list-error").hidden = true;
    $("#list-empty").hidden = true;
    $("#todo-list").hidden = true;
    $("#list-count").hidden = true;
    function page() {
      if (listFilter === undefined) {
        // 先刷新列表：手机上删掉的列表不能再拿来筛选。
        return loadLists().then(function () {
          listFilter = state.listFilter;
          if (listFilter !== "all") { query.set("list", String(listFilter)); }
          return page();
        });
      }
      query.set("offset", String(items.length));
      return api("GET", "./api/todos" + "?" + query.toString()).then(function (data) {
        if (request !== state.listRequest || !state.user) { return; }
        items = items.concat(data.items || []);
        // 日期筛选需要所有页，不能仅过滤服务端默认返回的前 50 条。
        if (items.length < data.total && data.items.length) { return page(); }
        state.todos = items;
        renderTodos();
      });
    }
    return page().catch(function (error) {
      if (request !== state.listRequest || !state.user) { return; }
      $("#list-error").hidden = false;
      setText($("#list-error-message"), (error.message || "任务加载失败") + "。请重新加载，已保存的任务不会丢失。");
    }).then(function () {
      if (request !== state.listRequest) { return; }
      $("#todo-list").setAttribute("aria-busy", "false");
      $("#list-loading").hidden = true;
    });
  }

  function localDate(date) { return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()); }

  function renderTodos() {
    var today = localDate(new Date());
    var weekEnd = new Date();
    weekEnd.setDate(weekEnd.getDate() + 7);
    var end = localDate(weekEnd);
    var items = state.todos.filter(function (todo) {
      if (state.tag && (todo.tags || []).indexOf(state.tag) < 0) { return false; }
      if (state.filter === "flagged") { return !!todo.flagged && todo.status !== "archived"; }
      if (state.filter !== "today" && state.filter !== "upcoming") { return true; }
      if (todo.status === "done" || todo.status === "archived" || !todo.due_at) { return false; }
      var due = toLocalInput(todo.due_at).slice(0, 10);
      return state.filter === "today" ? due === today : due > today && due <= end;
    });
    var list = $("#todo-list");
    list.hidden = false;
    $("#list-count").hidden = false;
    list.replaceChildren();
    // 子任务排在上级任务下面；上级任务不在当前筛选结果里时单独显示。
    var shown = {};
    items.forEach(function (todo) { shown[todo.id] = true; });
    var children = {};
    items.forEach(function (todo) {
      if (todo.parent_id && shown[todo.parent_id]) { (children[todo.parent_id] = children[todo.parent_id] || []).push(todo); }
    });
    var top = items.filter(function (todo) { return !(todo.parent_id && shown[todo.parent_id]); });
    function append(todo) {
      list.appendChild(todoItem(todo, false));
      (children[todo.id] || []).forEach(function (child) { list.appendChild(todoItem(child, true)); });
    }
    // 「全部」里未完成在前；已完成的沉到底部并加一个分组标题，不再和待办混排。
    var open = top.filter(function (todo) { return todo.status !== "done"; });
    var done = top.filter(function (todo) { return todo.status === "done"; });
    var grouped = state.filter === "all" && open.length && done.length;
    (grouped ? open : top).forEach(append);
    if (grouped) {
      var group = el("li", "todo-group", "已完成 · " + done.length);
      group.setAttribute("aria-hidden", "true");
      list.appendChild(group);
      done.forEach(append);
    }
    $("#list-empty").hidden = items.length > 0;
    $("#empty-all").hidden = state.filter === "all" && !state.tag;
    $("#tag-filter").hidden = !state.tag;
    setText($("#tag-filter-name"), state.tag ? "#" + state.tag : "");
    var hints = { today: "今天要提醒的未完成任务（按本地时间）。", upcoming: "明天起未来 7 天的未完成任务（按本地时间）。",
      flagged: "标了旗标的任务。" };
    setText($("#filter-hint"), hints[state.filter] || (state.filter === "all" ? "所有未归档任务，按优先级与时间排列。" : "查看「" + STATUS_LABELS[state.filter] + "」任务。"));
    setText($("#empty-title"), state.filter === "all" && !state.tag ? "给今天留一点期待" : state.filter === "done" ? "每一次完成，都值得记录" : "这里暂时没有任务");
    setText($("#empty-message"), state.filter === "all" && !state.tag ? "还没有任务。从一件小事开始吧。" : "当前筛选下没有任务，可以看看全部任务，或添加新计划。");
    setText($("#list-count"), items.length ? "共 " + items.length + " 件任务 · 已完成 " + items.filter(function (todo) { return todo.status === "done"; }).length + " 件" : "");
  }

  function selectTag(tag) {
    state.tag = tag || "";
    renderTodos();
    if (state.tag) { $("#tag-filter-clear").focus(); }
  }

  /* 「每周」「工作日」……；手机上设置的其他规则显示为「自定义重复」。 */
  function repeatLabel(rule) {
    if (!rule) { return ""; }
    var match = Array.prototype.filter.call($("#d-repeat").querySelectorAll("option"), function (option) {
      return option.getAttribute("value") === rule && !option.dataset.custom;
    })[0];
    return match ? match.textContent.trim() : "自定义重复";
  }

  function todoItem(todo, nested) {
    var item = el("li", "todo" + (todo.status === "done" ? " is-done" : "") +
      (todo.status === "archived" ? " is-archived" : "") + (todo.priority === 3 ? " is-urgent" : "") +
      (nested ? " is-subtask" : "") + " prio-" + todo.priority);

    var check = el("input", "check");
    check.type = "checkbox";
    check.id = "todo-check-" + todo.id;
    check.checked = todo.status === "done";
    check.setAttribute("aria-label", (check.checked ? "恢复为待办：" : "标记完成：") + todo.title);
    check.addEventListener("change", function () { check.disabled = true; toggleDone(todo, check.checked); });
    item.appendChild(check);

    var main = el("div", "todo-main");
    var title = el("button", "todo-title");
    if (todo.flagged) {
      var flag = el("span", "flag-mark", "⚑");
      flag.setAttribute("aria-label", "已标旗标");
      title.appendChild(flag);
    }
    title.appendChild(el("span", "todo-title-text", todo.title));
    title.setAttribute("title", todo.title);
    title.type = "button";
    title.addEventListener("click", function () { go("#/todos/" + todo.id); });
    main.appendChild(title);

    // 只显示有信息量的标记：默认的「待办 / 中优先级」不再占位。
    var meta = el("div", "todo-meta");
    var pending = todo.status !== "done" && todo.status !== "archived";
    var owner = listOf(todo);
    if (owner && state.listFilter === "all" && state.lists.length > 1 && !nested) {
      var tag = el("span", "meta-item list-tag");
      tag.appendChild(listDot(owner));
      tag.appendChild(el("span", "", owner.name));
      meta.appendChild(tag);
    }
    if (todo.parent_id && !nested) {
      var parent = state.todos.filter(function (other) { return other.id === todo.parent_id; })[0];
      if (parent) { meta.appendChild(el("span", "meta-item", "↳ " + parent.title)); }
    }
    if (todo.urgent && pending) { meta.appendChild(el("span", "tag urgent-tag", "紧急")); }
    if (todo.status === "doing" || todo.status === "archived") {
      meta.appendChild(el("span", "tag status-" + todo.status, STATUS_LABELS[todo.status]));
    }
    if (pending && todo.priority !== 2 && todo.priority !== undefined) {
      meta.appendChild(el("span", "tag priority-" + todo.priority, priorityLabel(todo.priority) + "优先级"));
    }
    if (todo.due_at) {
      var overdue = pending && new Date(toLocalInput(todo.due_at)) < new Date();
      var dueToday = toLocalInput(todo.due_at).slice(0, 10) === localDate(new Date());
      meta.appendChild(el("span", "meta-item " + (!pending ? "" : overdue ? "due-overdue" : dueToday ? "due-today" : "due-future"),
        (overdue ? "已逾期 · " : "") + friendlyTime(todo.due_at)));
    }
    if (todo.repeat) { meta.appendChild(el("span", "meta-item", "↻ " + repeatLabel(todo.repeat))); }
    if (todo.subtask_total) {
      meta.appendChild(el("span", "meta-item" + (todo.subtask_done === todo.subtask_total ? " checklist-done" : ""),
        "子任务 " + todo.subtask_done + "/" + todo.subtask_total));
    }
    var tasks = String(todo.details || "").match(/^\s*(?:[-*+]|\d{1,3}[.)])\s+\[[ xX]\]/gm) || [];
    if (tasks.length) {
      var checked = tasks.filter(function (value) { return /\[[xX]\]$/.test(value); }).length;
      meta.appendChild(el("span", "meta-item" + (checked === tasks.length ? " checklist-done" : ""), "清单 " + checked + "/" + tasks.length));
    }
    if (todo.image_count) { meta.appendChild(el("span", "meta-item", "图片 " + todo.image_count)); }
    if (todo.notes_count) { meta.appendChild(el("span", "meta-item", todo.notes_count + " 条笔记")); }
    (todo.tags || []).forEach(function (name) {
      var chipButton = el("button", "tag-chip tag-link", "#" + name);
      chipButton.type = "button";
      chipButton.setAttribute("aria-label", "只看标签「" + name + "」");
      chipButton.addEventListener("click", function () { selectTag(name); });
      meta.appendChild(chipButton);
    });
    var snippet = plainSnippet(todo.details);
    if (snippet) { main.appendChild(el("p", "todo-snippet", snippet)); }
    if (meta.childNodes.length) { main.appendChild(meta); }
    item.appendChild(main);

    var actions = el("div", "todo-actions");
    var remove = el("button", "icon-button danger", "删除");
    remove.type = "button";
    remove.setAttribute("aria-label", "删除任务：" + todo.title);
    remove.addEventListener("click", function () { deleteTodo(todo.id, todo.title, todo.subtask_total); });
    actions.appendChild(remove);
    item.appendChild(actions);

    return item;
  }

  function friendlyTime(value) {
    var date = parseLocal(toLocalInput(value));
    return date ? friendlyDate(date) : "";
  }

  function priorityLabel(priority) {
    return { 3: "高", 2: "中", 1: "低", 0: "最低" }[priority] || "中";
  }

  function doneMessage(updated, checked) {
    if (checked && updated.repeated) { return "已完成一次，下次：" + friendlyTime(updated.repeated.to); }
    return checked ? "已标记为完成" : "已恢复为待办";
  }

  function toggleDone(todo, checked) {
    api("PUT", "./api/todos/" + todo.id, { status: checked ? "done" : "open" })
      .then(function (updated) {
        if (state.current && state.current.id === updated.id) {
          state.current = updated;
          renderDetail(updated);
        }
        showToast(doneMessage(updated, checked), "ok", "#list-feedback");
        return loadTodos();
      })
      .catch(function (error) {
        fail(error);
        return loadTodos();
      }).then(function () {
        if (state.view !== "list") { return; }
        var checkbox = $("#todo-check-" + todo.id);
        if (checkbox) { checkbox.disabled = false; checkbox.focus(); }
        else { $("#list-heading").focus(); }
      });
  }

  function deleteTodo(id, title, subtasks) {
    var extra = subtasks ? "以及它的 " + subtasks + " 个子任务" : "";
    confirmDialog('确定删除任务「' + title + '」' + extra + '吗？删除后无法恢复。', "删除")
      .then(function (confirmed) {
        if (!confirmed) { return null; }
        return api("DELETE", "./api/todos/" + id).then(function () {
          /* 无论从列表还是详情页删除，都要清掉详情状态并切回列表视图，
             否则删除后会停留在详情页显示已删除的任务。 */
          state.current = null;
          showToast("任务已删除", "ok");
          showView("list");
          go("#/todos");
        }).catch(function (error) {
          if (error && error.status === 404) {
            /* 任务已被删除（例如在另一个标签页里删过）：同样切回列表，
               不能把用户留在一个已经不存在、保存/加笔记都会失败的死详情页。 */
            state.current = null;
            showView("list");
            go("#/todos");
            showToast("任务已被删除，已返回列表", "warn");
            return;
          }
          fail(error);
        });
      });
  }

  // ------------------------------------------------------------ 提醒、重复与标记

  function utcDate(value) {
    var parts = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(value || "");
    return parts ? new Date(Date.UTC(+parts[1], +parts[2] - 1, +parts[3], +parts[4], +parts[5], +(parts[6] || 0))) : null;
  }

  function utcString(date) {
    return date.getUTCFullYear() + "-" + pad2(date.getUTCMonth() + 1) + "-" + pad2(date.getUTCDate()) +
      " " + pad2(date.getUTCHours()) + ":" + pad2(date.getUTCMinutes()) + ":" + pad2(date.getUTCSeconds());
  }

  /* 提前提醒的分钟数（字符串，"" 表示不提前）。 */
  function earlyMinutes(todo) {
    var due = utcDate(todo.due_at);
    var remind = utcDate(todo.remind_at);
    if (!due || !remind) { return ""; }
    var minutes = Math.round((due - remind) / 60000);
    return minutes > 0 ? String(minutes) : "";
  }

  function durationLabel(minutes) {
    if (minutes % 10080 === 0) { return minutes / 10080 + " 周"; }
    if (minutes % 1440 === 0) { return minutes / 1440 + " 天"; }
    if (minutes % 60 === 0) { return minutes / 60 + " 小时"; }
    return minutes + " 分钟";
  }

  /* 设定选择框的值；值不在选项里时（手机上设置的）临时加一个选项。 */
  function setChoice(select, value, customLabel) {
    Array.prototype.forEach.call(select.querySelectorAll("option"), function (option) {
      if (option.dataset.custom) { option.remove(); }
    });
    var known = Array.prototype.some.call(select.querySelectorAll("option"), function (option) {
      return option.getAttribute("value") === value;
    });
    if (!known) {
      var option = el("option", "", customLabel || value);
      option.setAttribute("value", value);
      option.dataset.custom = "1";
      select.appendChild(option);
    }
    select.value = value;
  }

  /* 表单里的截止时间、提前提醒、重复、旗标、紧急、标签 -> 接口字段。 */
  function timeFields(prefix) {
    var dueLocal = $("#" + prefix + "-due").value;
    var due = fromLocalInput(dueLocal);
    var early = Number($("#" + prefix + "-early").value);
    var remind = null;
    if (due && early > 0) { remind = utcString(new Date(utcDate(due).getTime() - early * 60000)); }
    return {
      due_at: due,
      remind_at: remind,
      repeat: due ? ($("#" + prefix + "-repeat").value || null) : null,
      flagged: !!$("#" + prefix + "-flagged").checked,
      urgent: !!$("#" + prefix + "-urgent").checked,
      tags: readTags(prefix + "-tags")
    };
  }

  function alertSummary(todo) {
    var pending = todo.status !== "done" && todo.status !== "archived";
    if (!todo.due_at) { return "提醒：未设置"; }
    if (!pending) { return "提醒：" + friendlyTime(todo.due_at); }
    if (todo.remind_at && !todo.reminder_sent) { return "下次提醒：" + friendlyTime(todo.remind_at) + "（提前提醒）"; }
    if (!todo.due_alert_sent) { return "下次提醒：" + friendlyTime(todo.due_at); }
    return "提醒：已发送";
  }

  function renderAlertState(todo) {
    var nagging = !!(todo.urgent && todo.next_nag_at && todo.status !== "done" && todo.status !== "archived");
    $("#d-nag").hidden = !nagging;
    if (nagging) {
      setText($("#d-nag-text"), "紧急提醒进行中：已额外提醒 " + (todo.nag_count || 0) + " 次，下一次 " + friendlyTime(todo.next_nag_at) + "。");
    }
  }

  function stopAlerts() {
    var todo = state.current;
    if (!todo) { return; }
    var button = $("#btn-stop-alerts");
    setBusy(button, true, "正在停止…");
    api("POST", "./api/todos/" + todo.id + "/stop-alerts", {}).then(function (updated) {
      if (!state.current || state.current.id !== updated.id) { return; }
      state.current.next_nag_at = updated.next_nag_at;
      renderAlertState(updated);
      showToast("已停止这次的紧急提醒", "ok", "#detail-feedback");
    }).catch(fail).then(function () { setBusy(button, false); });
  }

  // ------------------------------------------------------------ 标签输入

  function initTags() {
    Array.prototype.forEach.call(document.querySelectorAll(".tags-field"), function (holder) {
      var key = holder.dataset.tags;
      var input = $("#" + key + "-entry");
      state.tags[key] = [];
      input.addEventListener("keydown", function (event) {
        if (event.isComposing) { return; }        // 中文输入法正在选字
        if (event.key === "Enter" || event.key === "," || event.key === "，") {
          event.preventDefault();
          commitTag(key);
        } else if (event.key === "Backspace" && !input.value && state.tags[key].length) {
          setTags(key, state.tags[key].slice(0, -1));
        }
      });
      input.addEventListener("blur", function () { commitTag(key); });
    });
  }

  function cleanTag(value) {
    return String(value || "").replace(/^[#＃]+/, "").replace(/\s+/g, " ").trim().slice(0, 30);
  }

  function commitTag(key) {
    var input = $("#" + key + "-entry");
    var parts = String(input.value || "").split(/[,，]/).map(cleanTag).filter(Boolean);
    input.value = "";
    if (!parts.length) { return; }
    var tags = state.tags[key].slice();
    parts.forEach(function (tag) {
      var exists = tags.some(function (item) { return item.toLowerCase() === tag.toLowerCase(); });
      if (!exists && tags.length < 10) { tags.push(tag); }
    });
    setTags(key, tags);
  }

  function readTags(key) {
    commitTag(key);
    return (state.tags[key] || []).slice();
  }

  function setTags(key, tags) {
    state.tags[key] = tags.slice();
    var list = $("#" + key + "-list");
    list.replaceChildren();
    tags.forEach(function (tag, index) {
      var item = el("span", "tag-chip");
      item.appendChild(el("span", "", "#" + tag));
      var remove = el("button", "tag-remove", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "删除标签 " + tag);
      remove.addEventListener("click", function () {
        var next = state.tags[key].slice();
        next.splice(index, 1);
        setTags(key, next);
        $("#" + key + "-entry").focus();
      });
      item.appendChild(remove);
      list.appendChild(item);
    });
  }

  // ------------------------------------------------------------ 子任务与图片

  /* 只刷新详情页的子任务、图片和提醒状态，不覆盖正在编辑但未保存的字段。 */
  function refreshDetailParts() {
    var current = state.current;
    if (!current) { return Promise.resolve(); }
    return api("GET", "./api/todos/" + current.id).then(function (todo) {
      if (!state.current || state.current.id !== todo.id) { return; }
      ["subtasks", "subtask_total", "subtask_done", "images", "image_count", "next_nag_at", "nag_count"].forEach(function (key) {
        state.current[key] = todo[key];
      });
      renderSubtasks(todo);
      renderImages(todo);
      renderAlertState(todo);
    }).catch(fail);
  }

  function renderSubtasks(todo) {
    // 子任务只有一层，和「提醒事项」一样。
    $("#subtasks").hidden = !!todo.parent_id;
    var list = $("#subtask-list");
    list.replaceChildren();
    (todo.subtasks || []).forEach(function (child) {
      var item = el("li", "subtask" + (child.status === "done" ? " is-done" : ""));
      var check = el("input", "check");
      check.type = "checkbox";
      check.id = "subtask-check-" + child.id;
      check.checked = child.status === "done";
      check.setAttribute("aria-label", (check.checked ? "恢复为待办：" : "标记完成：") + child.title);
      check.addEventListener("change", function () {
        check.disabled = true;
        api("PUT", "./api/todos/" + child.id, { status: check.checked ? "done" : "open" })
          .then(function (updated) {
            if (updated.repeated) { showToast(doneMessage(updated, true), "ok", "#subtask-feedback"); }
            return refreshDetailParts();
          })
          .catch(function (error) { fail(error); return refreshDetailParts(); });
      });
      item.appendChild(check);
      var title = el("button", "subtask-title", child.title);
      title.type = "button";
      title.addEventListener("click", function () { go("#/todos/" + child.id); });
      item.appendChild(title);
      if (child.due_at) { item.appendChild(el("span", "meta-item", friendlyTime(child.due_at))); }
      list.appendChild(item);
    });
    setText($("#subtask-count"), todo.subtask_total ? todo.subtask_done + "/" + todo.subtask_total : "");
  }

  function addSubtask(event) {
    event.preventDefault();
    var parent = state.current;
    var title = $("#subtask-title").value.trim();
    if (!parent || !title) { $("#subtask-title").focus(); return; }
    var button = $("#subtask-submit");
    if (button.disabled) { return; }
    setBusy(button, true, "添加中…");
    api("POST", "./api/todos", { title: title, parent_id: parent.id }).then(function () {
      $("#subtask-title").value = "";
      $("#subtask-title").focus();
      return refreshDetailParts();
    }).catch(function (error) {
      if (error.status !== 401) { showToast(error.message || "添加失败，请重试", "error", "#subtask-feedback"); }
    }).then(function () { setBusy(button, false); });
  }

  var IMAGES_MAX = 9;

  function renderImages(todo) {
    var images = todo.images || [];
    var grid = $("#image-grid");
    grid.replaceChildren();
    images.forEach(function (image, index) {
      var item = el("li", "image-item");
      var open = el("button", "thumb");
      open.type = "button";
      open.setAttribute("aria-label", "查看第 " + (index + 1) + " 张图片");
      var img = el("img");
      img.setAttribute("src", "./" + image.url);
      img.setAttribute("alt", "");
      img.setAttribute("loading", "lazy");
      open.appendChild(img);
      open.addEventListener("click", function () { openLightbox(index); });
      item.appendChild(open);
      var remove = el("button", "thumb-delete", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "删除第 " + (index + 1) + " 张图片");
      remove.addEventListener("click", function () { deleteImage(image); });
      item.appendChild(remove);
      grid.appendChild(item);
    });
    setText($("#image-count"), images.length ? images.length + "/" + IMAGES_MAX : "");
    $("#btn-add-image").hidden = images.length >= IMAGES_MAX;
  }

  function deleteImage(image) {
    confirmDialog("删除这张图片吗？删除后无法恢复。", "删除").then(function (confirmed) {
      if (!confirmed) { return null; }
      return api("DELETE", "./" + image.url).then(function () {
        showToast("图片已删除", "ok", "#image-feedback");
        return refreshDetailParts();
      }).catch(fail);
    });
  }

  /* 手机照片常有好几 MB：缩到长边 2048 像素、转成 JPEG 再上传，顺便去掉位置等 EXIF 信息。 */
  function compressImage(file) {
    var LIMIT = 2048;
    if (file.type === "image/gif" || typeof createImageBitmap !== "function") { return Promise.resolve(file); }
    return createImageBitmap(file).then(function (bitmap) {
      var scale = Math.min(1, LIMIT / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      var context = canvas.getContext("2d");
      context.fillStyle = "#fff";            // 透明 PNG 转 JPEG 时用白底
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      if (bitmap.close) { bitmap.close(); }
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) { resolve(blob && blob.size < file.size ? blob : file); }, "image/jpeg", 0.85);
      });
    }).catch(function () { return file; });   // 浏览器解不开（如部分 HEIC）就原样上传，由服务器判断
  }

  function uploadImages() {
    var input = $("#image-input");
    var files = Array.prototype.slice.call(input.files || []);
    input.value = "";
    var todo = state.current;
    if (!todo || !files.length) { return; }
    var room = IMAGES_MAX - (todo.images || []).length;
    if (files.length > room) {
      showToast("每个任务最多 " + IMAGES_MAX + " 张图片，这次只上传前 " + room + " 张", "warn", "#image-feedback");
      files = files.slice(0, room);
    }
    var button = $("#btn-add-image");
    setBusy(button, true, "正在上传…");
    var uploaded = 0;
    files.reduce(function (chain, file) {
      return chain.then(function () {
        return compressImage(file).then(function (blob) {
          return api("POST", "./api/todos/" + todo.id + "/images", blob);
        }).then(function () { uploaded += 1; });
      });
    }, Promise.resolve()).then(function () {
      showToast("已添加 " + uploaded + " 张图片", "ok", "#image-feedback");
    }).catch(function (error) {
      if (error.status !== 401) {
        showToast((uploaded ? "已上传 " + uploaded + " 张；" : "") + (error.message || "图片上传失败"), "error", "#image-feedback");
      }
    }).then(function () {
      setBusy(button, false);
      return refreshDetailParts();
    });
  }

  function openLightbox(index) {
    var images = (state.current && state.current.images) || [];
    if (!images.length) { return; }
    index = (index + images.length) % images.length;
    if (!state.lightbox) { state.lightbox = { focus: document.activeElement }; }
    state.lightbox.index = index;
    // 图片节点按需创建：页面里不放没有地址的 <img>。
    var frame = $("#lightbox-frame");
    frame.replaceChildren();
    var img = el("img");
    img.id = "lightbox-image";
    img.setAttribute("src", "./" + images[index].url);
    img.setAttribute("alt", "第 " + (index + 1) + " 张图片");
    frame.appendChild(img);
    setText($("#lightbox-index"), (index + 1) + " / " + images.length);
    $("#lightbox-prev").hidden = $("#lightbox-next").hidden = images.length < 2;
    $("#lightbox").hidden = false;
    document.body.classList.add("has-modal");
    $("#lightbox-close").focus();
  }

  function closeLightbox() {
    if (!state.lightbox) { return; }
    var focus = state.lightbox.focus;
    state.lightbox = null;
    $("#lightbox").hidden = true;
    $("#lightbox-frame").replaceChildren();
    document.body.classList.remove("has-modal");
    if (focus && focus.isConnected) { focus.focus(); }
  }

  // ------------------------------------------------------------ 新建任务

  function setCreateOptions(open) {
    $("#create-options").hidden = !open;
    $("#create-options-toggle").setAttribute("aria-expanded", String(open));
    setText($("#create-options-toggle"), open ? "收起选项 −" : "更多选项 ＋");
  }

  function focusComposer() {
    $("#new-title").focus();
  }

  function submitCreate(event) {
    event.preventDefault();
    var title = $("#new-title").value.trim();
    if (!title) {
      showToast("请输入任务标题", "error");
      $("#new-title").focus();
      return;
    }
    var button = $("#create-submit");
    if (button.disabled) { return; }
    setBusy(button, true, "添加中…");
    var payload = timeFields("new");
    payload.title = title;
    payload.details = $("#new-details").value || null;
    payload.priority = Number($("#new-priority").value);
    payload.list_id = Number($("#new-list").value) || null;
    api("POST", "./api/todos", payload).then(function (todo) {
      $("#create-form").reset();
      $("#new-priority").value = "2";
      $("#new-early").value = "";
      $("#new-repeat").value = "";
      $("#new-flagged").checked = false;
      $("#new-urgent").checked = false;
      setTags("new-tags", []);
      syncComposerList();
      setMdMode("new-details", false);
      refreshPickers();
      setCreateOptions(false);
      var warnings = todo.warnings && todo.warnings.length ? "（" + todo.warnings.join("；") + "）" : "";
      showToast("已添加任务" + warnings, warnings ? "warn" : "ok", "#create-feedback");
      if (state.view === "list") { $("#new-title").focus(); }
      return loadTodos();
    }).catch(function (error) {
      if (error.status !== 401) { showToast(error.message || "添加失败，请重试", "error", "#create-feedback"); }
    }).then(function () { setBusy(button, false); });
  }

  // ------------------------------------------------------------ 详情 / 编辑

  function openDetail(id) {
    var hash = "#/todos/" + id;
    // 从邮件或收藏的链接直接打开详情时，列表还没加载过；加载后 renderLists 会补上「所属列表」。
    loadLists();
    return api("GET", "./api/todos/" + id).then(function (todo) {
      // 用户已切换到其他视图时，迟到的详情响应不能抢回页面与焦点。
      if (!state.user || location.hash !== hash) { return; }
      state.current = todo;
      renderDetail(todo);
      showView("detail");
    }).catch(function (error) {
      if (!state.user || location.hash !== hash) { return; }
      if (error && error.status === 401) { return; }
      if (error && error.status === 404) {
        showToast("任务不存在或已被删除", "error");
        go("#/todos");
        return;
      }
      fail(error);
    });
  }

  function renderDetail(todo) {
    $("#d-title").value = todo.title || "";
    $("#d-details").value = todo.details || "";
    setDetailsMode(!!(todo.details || "").trim());
    $("#d-status").value = todo.status || "open";
    $("#d-priority").value = String(todo.priority === undefined ? 2 : todo.priority);
    fillListSelect($("#d-list"), (listOf(todo) || {}).id);
    $("#d-list-field").hidden = state.lists.length < 2 || !!todo.parent_id;
    $("#d-due").value = toLocalInput(todo.due_at);
    var early = earlyMinutes(todo);
    setChoice($("#d-early"), early, early ? "提前 " + durationLabel(Number(early)) : "");
    setChoice($("#d-repeat"), todo.repeat || "", "自定义重复");
    $("#d-flagged").checked = !!todo.flagged;
    $("#d-urgent").checked = !!todo.urgent;
    setTags("d-tags", todo.tags || []);
    var parentLink = $("#d-parent");
    parentLink.hidden = !todo.parent;
    if (todo.parent) {
      parentLink.href = "#/todos/" + todo.parent.id;
      setText(parentLink, "↑ 上级任务：" + todo.parent.title);
    }
    renderSubtasks(todo);
    renderImages(todo);
    renderAlertState(todo);

    var meta = [];
    meta.push("创建于 " + formatTime(todo.created_at));
    meta.push("最后更新 " + formatTime(todo.updated_at));
    meta.push(alertSummary(todo));
    if (todo.warnings && todo.warnings.length) { meta.push(todo.warnings.join("；")); }
    setText($("#d-meta"), meta.join(" · "));

    var link = $("#link-google");
    var url = safeLink(todo.google_calendar_url);
    if (url) {
      link.href = url;
      link.hidden = false;
    } else {
      link.removeAttribute("href");
      link.hidden = true;
    }

    renderNotes(todo.notes || []);
    refreshPickers();
  }

  function renderNotes(notes) {
    var list = $("#note-list");
    list.replaceChildren();
    notes.forEach(function (note) {
      var item = el("li", "note");
      item.appendChild(el("time", "note-time", formatTime(note.created_at)));
      item.appendChild(renderMarkdown(note.content, "note-body"));
      list.appendChild(item);
    });
    $("#note-empty").hidden = notes.length > 0;
  }

  function submitDetail(event) {
    event.preventDefault();
    var todo = state.current;
    if (!todo) {
      showToast("任务不存在或已被删除，请返回列表重新打开", "error");
      return;
    }
    var title = $("#d-title").value.trim();
    if (!title) {
      showToast("标题不能为空", "error");
      $("#d-title").focus();
      return;
    }
    var button = $("#d-save");
    if (button.disabled) { return; }
    setBusy(button, true, "正在保存…");
    var changes = timeFields("d");
    changes.title = title;
    changes.details = $("#d-details").value || null;
    changes.status = $("#d-status").value;
    changes.priority = Number($("#d-priority").value);
    // 列表还没加载时选择框是空的：这时不改所属列表，否则任务会被移回默认列表。
    if (Number($("#d-list").value)) { changes.list_id = Number($("#d-list").value); }
    api("PUT", "./api/todos/" + todo.id, changes).then(function (updated) {
      state.current = updated;
      renderDetail(updated);
      var warnings = updated.warnings && updated.warnings.length ? "（" + updated.warnings.join("；") + "）" : "";
      var repeated = updated.repeated ? "，已完成一次，下次：" + friendlyTime(updated.repeated.to) : "";
      showToast("已保存" + repeated + warnings, warnings ? "warn" : "ok", "#detail-feedback");
      return loadTodos();
    }).catch(function (error) {
      if (error.status !== 401) { showToast(error.message || "保存失败，请重试", "error", "#detail-feedback"); }
    }).then(function () { setBusy(button, false); });
  }

  function addNote(event) {
    event.preventDefault();
    var todo = state.current;
    if (!todo) {
      showToast("任务不存在或已被删除，请返回列表重新打开", "error");
      return;
    }
    var content = $("#note-content").value;
    if (!content.trim()) {
      showToast("笔记内容不能为空", "error");
      $("#note-content").focus();
      return;
    }
    var button = $("#note-submit");
    if (button.disabled) { return; }
    setBusy(button, true, "正在添加…");
    api("POST", "./api/todos/" + todo.id + "/notes", { content: content })
      .then(function (note) {
        // 不重载整张详情表单，保留尚未保存的编辑与当前滚动位置。
        if (!state.current || state.current.id !== todo.id || state.view !== "detail") { return; }
        state.current.notes = (state.current.notes || []).concat([note]);
        renderNotes(state.current.notes);
        $("#note-content").value = "";
        autoGrow($("#note-content"));
        showToast("已添加笔记", "ok", "#note-feedback");
        $("#note-content").focus();
      })
      .catch(function (error) {
        if (error.status !== 401) { showToast(error.message || "笔记添加失败，请重试", "error", "#note-feedback"); }
      })
      .then(function () { setBusy(button, false); });
  }

  function downloadIcs() {
    var todo = state.current;
    if (!todo) {
      showToast("任务不存在或已被删除，请返回列表重新打开", "error");
      return;
    }
    var button = $("#btn-download-ics");
    button.disabled = true;
    fetch("./api/todos/" + todo.id + "/ics", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 401) {
          handleUnauthorized("登录状态已失效，请重新登录");
          return null;
        }
        if (!response.ok) {
          return errorBody(response).then(function (info) {
            throw new ApiError(response.status, info.code,
              info.message || "无法生成日历文件（请先设置截止时间）");
          });
        }
        return response.text();
      })
      .then(function (text) {
        if (text === null) { return; }
        var blob = new Blob([text], { type: "text/calendar;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var link = el("a");
        link.href = url;
        link.download = "easylife-" + todo.id + ".ics";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
        showToast("已开始下载 .ics 文件", "ok", "#detail-feedback");
      })
      .catch(function (error) { fail(error); })
      .then(function () { button.disabled = false; });
  }

  // ------------------------------------------------------------ 设置

  function toggleSettings() {
    go("#/settings");
  }

  function closeAccount(restoreFocus) {
    $("#account-panel").hidden = true;
    $("#btn-account").setAttribute("aria-expanded", "false");
    if (restoreFocus) { $("#btn-account").focus(); }
  }

  function copyFeedUrl() {
    if (!state.feedUrl) {
      showToast("订阅地址尚未生成", "error");
      return;
    }
    var done = function (ok) {
      showToast(ok ? "订阅地址已复制" : "复制失败，请手动选择并复制", ok ? "ok" : "error", "#settings-feedback");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(state.feedUrl).then(function () { done(true); },
        function () { done(fallbackCopy()); });
      return;
    }
    done(fallbackCopy());
  }

  function fallbackCopy() {
    var input = $("#feed-url");
    input.focus();
    input.select();
    try {
      return document.execCommand("copy");
    } catch (error) {
      return false;
    }
  }

  function resetFeedUrl() {
    confirmDialog("重置后旧订阅地址会立即失效，需要在日历客户端重新添加。确定重置吗？", "重置")
      .then(function (confirmed) {
        if (!confirmed) { return null; }
        return api("POST", "./api/calendar/token/reset", {}).then(function (data) {
          state.feedUrl = data.calendar_feed_url || "";
          $("#feed-url").value = state.feedUrl;
          showToast("订阅地址已重置，请重新订阅", "ok", "#settings-feedback");
        }).catch(function (error) { fail(error); });
      });
  }

  // ------------------------------------------------------------ 提醒事项同步（CalDAV）

  function renderDav(password) {
    var dav = state.dav;
    $("#dav-url").value = dav ? dav.server_url : "";
    $("#dav-user").value = dav ? dav.username : "";
    $("#dav-password").value = password || "";
    $("#dav-password-box").hidden = !password;
    var enabled = !!dav && dav.enabled;
    $("#btn-dav-revoke").hidden = !enabled;
    setText($("#btn-dav-generate"), enabled ? "重新生成密码" : "生成同步密码");
    var text = "尚未启用：点「生成同步密码」后按下面的步骤添加账户。";
    if (enabled) {
      text = "已启用" + (dav.last_used_at ? "，最近同步 " + formatTime(dav.last_used_at) : "，还没有设备连接过") + "。";
    }
    setText($("#dav-state"), dav ? text : "");
  }

  function copyField(selector, label) {
    var input = $(selector);
    var value = input.value;
    if (!value) { return; }
    var done = function (ok) {
      showToast(ok ? label + "已复制" : "复制失败，请手动选择并复制", ok ? "ok" : "error", "#dav-feedback");
    };
    var manual = function () {
      input.focus();
      input.select();
      try { return document.execCommand("copy"); } catch (error) { return false; }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(function () { done(true); }, function () { done(manual()); });
      return;
    }
    done(manual());
  }

  function generateDavPassword() {
    var again = !!state.dav && state.dav.enabled;
    var ask = again ? confirmDialog("重新生成后旧密码立即失效，已添加的设备需要改成新密码。确定吗？", "重新生成")
      : Promise.resolve(true);
    ask.then(function (confirmed) {
      if (!confirmed) { return null; }
      var button = $("#btn-dav-generate");
      setBusy(button, true, "正在生成…");
      return api("POST", "./api/dav/password", {}).then(function (data) {
        var password = data.password;
        delete data.password;
        state.dav = data;
        setBusy(button, false);
        renderDav(password);
        showToast("同步密码已生成，只显示这一次", "ok", "#dav-feedback");
      }).catch(function (error) {
        setBusy(button, false);
        renderDav(false);
        fail(error);
      });
    });
  }

  function revokeDav() {
    confirmDialog("停用后所有设备都会停止同步（手机上的 EasyLife 列表会显示连接失败）。确定停用吗？", "停用")
      .then(function (confirmed) {
        if (!confirmed) { return null; }
        return api("DELETE", "./api/dav/password").then(function (data) {
          state.dav = data;
          renderDav(false);
          showToast("已停用提醒事项同步", "ok", "#dav-feedback");
        }).catch(function (error) { fail(error); });
      });
  }

  // ------------------------------------------------------------ 管理员邮件设置
  //
  // 入口与请求都只在 state.user.is_admin === true 时发生。授权码永不回填：服务端
  // 只返回 password_set 布尔值，输入框仅在提交时携带新值，成功保存 / 离开设置页 /
  // 登出时清空。测试邮件只使用已保存的配置，有未保存修改时不会调用测试接口。

  var MAIL_SOURCES = { database: "已保存的站点配置", environment: "服务器环境变量", defaults: "内置默认值" };
  var MAIL_PASSWORD_SOURCES = { database: "已保存的授权码", environment: "服务器环境变量", none: "未设置" };

  function mailSourceLabel(value) { return MAIL_SOURCES[value] || value || "未知"; }
  function mailPasswordSourceLabel(value) { return MAIL_PASSWORD_SOURCES[value] || value || "未知"; }

  function setMailStatus(kind, message, tone) {
    state.mailResult = { kind: kind, message: message, tone: tone };
    renderMailStatus();
  }

  function renderMailStatus() {
    var result = state.mailResult || { kind: "idle", message: "", tone: "" };
    var values = mailFormValues();
    if (state.mail) {
      state.mailDirty = mailSnapshot(values) !== state.mailBaseline || values.password !== "" || values.clear_password;
    }
    // Even before the first GET returns, edits are drafts and must survive that GET.
    if (!state.mail && state.mailLoadRequest) { state.mailDirty = state.mailEditVersion !== state.mailLoadRequest.edit; }
    var busy = state.mailSaveRequest || state.mailLoadRequest;
    var newerDraft = state.mailDirty && (!busy || state.mailEditVersion !== busy.edit);
    var node = $("#mail-status");
    node.setAttribute("aria-live", "polite");
    node.setAttribute("aria-atomic", "true");
    node.dataset.state = newerDraft ? "unsaved" : result.kind;
    node.className = "form-message mail-status" + (newerDraft ? " warn" : result.tone ? " " + result.tone : "");
    setText($("#mail-draft-status"), state.mailDirty
      ? "有未保存的修改，请先保存；测试不会使用表单草稿。 " : "");
    $("#mail-result").dataset.state = result.kind;
    setText($("#mail-result"), result.message);
    node.hidden = !state.mailDirty && !result.message;
    setText($("#mail-reload"), state.mailUnknownSave ? "重新读取核对（保留草稿）" : "放弃修改并重新读取");
  }

  // Contexts contain only public snapshots/versions, never the submitted password.
  function mailRequestContext() {
    return { generation: state.mailGeneration, user: state.user.id, csrf: state.csrf,
      edit: state.mailEditVersion, passwordEdit: state.mailPasswordVersion,
      clearEdit: state.mailClearVersion, snapshot: mailSnapshot(mailFormValues()) };
  }

  function mailContextCurrent(context) {
    return state.mailGeneration === context.generation && state.view === "settings" &&
      !!state.user && state.user.is_admin === true && state.user.id === context.user && state.csrf === context.csrf;
  }

  function mailApi(method, path, payload, context) {
    return api(method, path, payload, { current: function () { return mailContextCurrent(context); } });
  }

  function canUseMailSettings() {
    return state.view === "settings" && !!state.user && state.user.is_admin === true;
  }

  function updateMailButtons() {
    var saving = !!state.mailSaveRequest;
    var loading = !!state.mailLoadRequest;
    // 状态提示位于表单外，表单忙碌时仍可播报进行中与结果文案。
    var form = $("#mail-form");
    if (form) { form.setAttribute("aria-busy", String(saving || loading || state.mailTestBusy)); }
    $("#mail-save").disabled = !state.mail || saving || loading || state.mailTestBusy;
    $("#mail-reload").disabled = saving || loading || state.mailTestBusy;
    updateMailTestButton();
  }

  function mailFormValues() {
    return {
      enabled: $("#mail-enabled").checked,
      host: $("#mail-host").value.trim(),
      port: $("#mail-port").value.trim(),
      user: $("#mail-user").value.trim(),
      from: $("#mail-from").value.trim(),
      password: $("#mail-password").value,
      clear_password: $("#mail-clear-password").checked
    };
  }

  /* 只比较会影响保存结果的字段；测试收件地址不属于已保存配置。 */
  function mailSnapshot(values) {
    return JSON.stringify([values.enabled, values.host, values.port, values.user, values.from]);
  }

  function mailSavedSummary() {
    var saved = state.mail;
    if (!saved) { return "尚未读取保存中的配置。"; }
    return "已生效的保存配置：来源 " + mailSourceLabel(saved.source) +
      (saved.updated_at
        ? "，最近保存于 " + saved.updated_at + (saved.updated_by ? "（" + saved.updated_by + "）" : "")
        : "，尚未在页面保存过") + "。";
  }

  function renderMailMeta(data) {
    setText($("#mail-source"), mailSourceLabel(data.source));
    setText($("#mail-password-source"), mailPasswordSourceLabel(data.password_source));
    setText($("#mail-password-state"), data.mail.password_set ? "已设置（不回显）" : "未设置");
    setText($("#mail-updated"), data.updated_at
      ? data.updated_at + (data.updated_by ? " · " + data.updated_by : "")
      : "从未在页面保存过");
  }

  function rememberMailSettings(data) {
    state.mail = data;
    state.mailBaseline = mailSnapshot({ enabled: !!data.mail.enabled, host: data.mail.host || "",
      port: data.mail.port ? String(data.mail.port) : "", user: data.mail.user || "", from: data.mail.from || "" });
    renderMailMeta(data);
  }

  function fillMailForm(data) {
    rememberMailSettings(data);
    $("#mail-enabled").checked = !!data.mail.enabled;
    $("#mail-host").value = data.mail.host || "";
    $("#mail-port").value = data.mail.port ? String(data.mail.port) : "";
    $("#mail-user").value = data.mail.user || "";
    $("#mail-from").value = data.mail.from || "";
    $("#mail-password").value = "";
    $("#mail-clear-password").checked = false;
    renderMailStatus();
  }

  function clearMailSecrets() {
    state.mailGeneration += 1;
    state.mailEditVersion += 1;
    state.mailPasswordVersion += 1;
    state.mailClearVersion += 1;
    state.mail = null;
    state.mailBaseline = "";
    state.mailDirty = false;
    state.mailUnknownSave = null;
    state.mailLoadRequest = null;
    state.mailSaveRequest = null;
    state.mailTestRequest = null;
    state.mailTestBusy = false;
    $("#mail-password").value = "";
    $("#mail-clear-password").checked = false;
    $("#mail-enabled").checked = false;
    ["host", "port", "user", "from", "test-to"].forEach(function (field) { $("#mail-" + field).value = ""; });
    ["source", "password-source", "password-state", "updated"].forEach(function (field) { setText($("#mail-" + field), "—"); });
    ["save", "test"].forEach(function (field) {
      var button = $("#mail-" + field);
      if (button.getAttribute("aria-busy") === "true") { setBusy(button, false); }
    });
    clearTimeout(state.mailTestTimer);
    setMailStatus("idle", "");
    updateMailButtons();
  }

  function hideMailSettings() {
    clearMailSecrets();
    $("#mail-settings").hidden = true;
    $("#nav-mail-settings").hidden = true;
  }

  function refreshMailDirty(event) {
    state.mailEditVersion += 1;
    if (!state.mail) { state.mailDirty = true; }
    if (event && event.target.id === "mail-password") { state.mailPasswordVersion += 1; }
    if (event && event.target.id === "mail-clear-password") { state.mailClearVersion += 1; }
    renderMailStatus();
  }

  function applyMailPreset(host, port, label) {
    if (!state.user || state.user.is_admin !== true || $("#mail-settings").hidden) { return; }
    // 示例只做显式快捷填充：写入表单并标记未保存，不自动保存、不覆盖已保存配置。
    $("#mail-host").value = host;
    $("#mail-port").value = String(port);
    refreshMailDirty();
    // Keep any in-flight/unknown result visible; preset feedback is separate draft feedback.
    setText($("#mail-draft-status"), "已填入「" + label + "」示例（" + host + ":" + port +
      (state.mailDirty ? "）——尚未保存，不会自动覆盖已保存的配置。 " : "），与已保存的配置相同。 "));
    $("#mail-user").focus();
  }

  function mailAccessDenied() {
    clearMailSecrets();
    $("#nav-mail-settings").hidden = true;
    $("#mail-form").hidden = true;
    setMailStatus("failed", "服务器拒绝了访问：当前账号没有管理员权限，无法读取或修改全站邮件设置。", "error");
  }

  function loadMailSettings(reconcile, restoreSession) {
    if (!state.user || state.user.is_admin !== true) { hideMailSettings(); return Promise.resolve(); }
    if (!canUseMailSettings() || state.mailLoadRequest || state.mailTestBusy ||
        (state.mailSaveRequest && !reconcile)) { return Promise.resolve(); }
    var context = mailRequestContext();
    var unknown = state.mailUnknownSave;
    state.mailLoadRequest = context;
    $("#mail-settings").hidden = false;
    $("#nav-mail-settings").hidden = false;
    $("#mail-form").hidden = false;
    setMailStatus(unknown ? "unknown" : "loading", unknown
      ? "上次保存结果尚未确认，正在重新读取服务器状态核对；保留当前草稿。" : "正在读取保存中的配置…", unknown ? "warn" : "");
    updateMailButtons();
    // A page restored from the back/forward cache has cleared its fields on pagehide.
    // Refresh its identity first; edits during either read belong to the same context.
    var pending = restoreSession ? mailApi("GET", "./api/me", undefined, context)
      .then(function (session) {
        if (!mailContextCurrent(context) || state.mailLoadRequest !== context) { return; }
        state.user = session.user;
        state.csrf = session.csrf_token;
        state.feedUrl = session.calendar_feed_url || "";
        state.dav = session.dav || null;
        updateAccount();
        if (state.mailLoadRequest !== context || !canUseMailSettings()) { return; }
        context.user = state.user.id;
        context.csrf = state.csrf;
        return mailApi("GET", "./api/admin/mail-settings", undefined, context);
      }) : mailApi("GET", "./api/admin/mail-settings", undefined, context);
    return pending.then(function (data) {
      if (!mailContextCurrent(context) || state.mailLoadRequest !== context) { return; }
      if (!unknown && state.mailEditVersion === context.edit) { fillMailForm(data); }
      else { rememberMailSettings(data); }
      if (unknown) {
        setMailStatus("unknown", "上次保存结果尚未确认。已重新读取服务器当前配置：公开字段" +
          (state.mailBaseline === unknown.snapshot ? "与提交值一致" : "与提交值不同（空值回退也会造成差异）") +
          "。这只能核对当前公开字段，不能证明上次请求的结果。" +
          (unknown.secretChanged ? "授权码不可回读，无法确认本次授权码是否更新或清除。" : "") +
          "当前草稿已保留；核对后可显式再次保存以确认。", "warn");
      } else { setMailStatus("saved", "已重新读取服务器当前配置。" + mailSavedSummary(), "ok"); }
    }).catch(function (error) {
      if (!mailContextCurrent(context) || state.mailLoadRequest !== context) { return; }
      if (error.status === 401) { return; }
      if (error.status === 403) { mailAccessDenied(); return; }
      if (unknown) {
        setMailStatus("unknown", "上次保存结果尚未确认，核对读取也失败了。草稿已保留，请恢复网络后点击「重新读取核对（保留草稿）」。", "warn");
        return;
      }
      setMailStatus("failed", "读取配置失败：" + (error.message || "网络错误") +
        " 可点击「放弃修改并重新读取」重试。", "error");
    }).then(function () {
      if (!mailContextCurrent(context) || state.mailLoadRequest !== context) { return; }
      state.mailLoadRequest = null;
      renderMailStatus();
      updateMailButtons();
    });
  }

  function saveMailSettings(event) {
    event.preventDefault();
    var button = $("#mail-save");
    if (button.disabled || !canUseMailSettings() || $("#mail-form").hidden ||
        state.mailSaveRequest || state.mailLoadRequest || state.mailTestBusy) { return; }
    var values = mailFormValues();
    var payload = { enabled: values.enabled, host: values.host, user: values.user, from: values.from };
    if (values.port !== "") {
      if (!/^\d{1,5}$/.test(values.port) || Number(values.port) < 1 || Number(values.port) > 65535) {
        $("#mail-port").setAttribute("aria-invalid", "true");
        setMailStatus("failed", "端口必须是 1–65535 的整数。", "error");
        $("#mail-port").focus();
        return;
      }
      payload.port = Number(values.port);
    }
    if (values.clear_password && values.password) {
      setMailStatus("failed", "「清除已保存的授权码」不能与新授权码同时提交：请只选择其一。", "error");
      $("#mail-password").focus();
      return;
    }
    if (values.password) { payload.password = values.password; }
    if (values.clear_password) { payload.clear_password = true; }
    var context = mailRequestContext();
    context.secretChanged = !!values.password || values.clear_password;
    state.mailSaveRequest = context;
    setBusy(button, true, "正在保存设置…");
    setMailStatus("saving", "正在保存邮件设置，请稍候…");
    updateMailButtons();
    mailApi("PUT", "./api/admin/mail-settings", payload, context).then(function (data) {
      if (!mailContextCurrent(context) || state.mailSaveRequest !== context) { return; }
      state.mailUnknownSave = null;
      if (state.mailEditVersion === context.edit) { fillMailForm(data); }
      else {
        rememberMailSettings(data);
        // Clear only the submitted secret intent, never a newer input/clear operation.
        if (state.mailPasswordVersion === context.passwordEdit) { $("#mail-password").value = ""; }
        if (state.mailClearVersion === context.clearEdit) { $("#mail-clear-password").checked = false; }
      }
      setMailStatus("saved", "保存成功，本次提交的配置已生效。已确认提交的授权码输入已清空；提交后新输入的内容仍保留为草稿。", "ok");
    }).catch(function (error) {
      if (!mailContextCurrent(context) || state.mailSaveRequest !== context) { return; }
      if (error.status === 401) { return; }
      if (error.status === 403) { mailAccessDenied(); return; }
      if (!error.status || error.status >= 500) {
        state.mailUnknownSave = context;
        setMailStatus("unknown", "保存结果尚未确认：" + (error.message || "响应未能读取") +
          "。服务器可能已经保存，正在重新读取核对；草稿保留。", "warn");
        return loadMailSettings(true);
      }
      setMailStatus(error.status === 429 ? "rate-limited" : "failed",
        "保存失败，服务器拒绝本次保存：" + (error.message || "请求无效") + "。草稿已保留。", "error");
    }).then(function () {
      if (!mailContextCurrent(context) || state.mailSaveRequest !== context) { return; }
      state.mailSaveRequest = null;
      setBusy(button, false);
      renderMailStatus();
      updateMailButtons();
    });
    // The request body has been serialized; do not retain extra plaintext copies in closures.
    values.password = "";
    delete payload.password;
  }

  function updateMailTestButton() {
    clearTimeout(state.mailTestTimer);
    var button = $("#mail-test");
    if (state.mailTestBusy) { return; }
    var remaining = Math.max(0, Math.ceil((state.mailTestUntil - Date.now()) / 1000));
    if (remaining > 0) {
      button.disabled = true;
      setText(button, "限流中：" + (remaining >= 60 ? Math.ceil(remaining / 60) + " 分钟" : remaining + " 秒") + "后可重试");
      if (canUseMailSettings()) { state.mailTestTimer = setTimeout(updateMailTestButton, 1000); }
      return;
    }
    button.disabled = !!state.mailSaveRequest || !!state.mailLoadRequest;
    setText(button, "发送测试邮件");
  }

  function startMailTestCooldown(seconds) {
    state.mailTestUntil = Date.now() + Math.max(1, Number(seconds) || 60) * 1000;
    updateMailTestButton();
  }

  function testMailSettings() {
    var button = $("#mail-test");
    if (button.disabled || state.mailTestBusy || state.mailSaveRequest || state.mailLoadRequest ||
        !canUseMailSettings() || $("#mail-form").hidden) { return; }
    renderMailStatus();
    if (state.mailUnknownSave) {
      setMailStatus("unknown", "上次保存结果尚未确认，请先重新读取核对当前服务器配置，并显式保存确认后再测试。授权码不可回读，不能用已设置状态确认本次更新。", "warn");
      $("#mail-reload").focus();
      return;
    }
    if (state.mailDirty || $("#mail-password").value || $("#mail-clear-password").checked) {
      // Do not erase an earlier request result just because testing a draft was blocked.
      setText($("#mail-draft-status"), "有未保存的修改：测试邮件只使用已保存的配置，请先点击「保存设置」。 ");
      $("#mail-save").focus();
      return;
    }
    if (!state.mail) {
      setMailStatus("failed", "尚未读取保存中的配置，请先重新读取。", "error");
      return;
    }
    var to = $("#mail-test-to").value.trim();
    if (to && !validEmail(to)) {
      $("#mail-test-to").setAttribute("aria-invalid", "true");
      setMailStatus("failed", "测试收件地址格式不正确。", "error");
      $("#mail-test-to").focus();
      return;
    }
    var context = mailRequestContext();
    context.to = to || state.user.email;
    context.saved = "发起时读取的已保存配置 " + state.mail.mail.host + ":" + state.mail.mail.port +
      "（最近保存：" + (state.mail.updated_at || "环境配置") + "），收件地址 " + context.to + "。";
    state.mailTestRequest = context;
    state.mailTestBusy = true;
    setBusy(button, true, "正在发送测试邮件…");
    setMailStatus("testing", "正在使用已保存的配置发送测试邮件，请稍候…" + context.saved);
    updateMailButtons();
    // 只传收件地址；不向测试接口发送任何临时 SMTP 覆盖字段。
    mailApi("POST", "./api/admin/mail-settings/test", to ? { to: to } : {}, context).then(function (data) {
      if (!mailContextCurrent(context) || state.mailTestRequest !== context) { return; }
      setMailStatus("accepted", "SMTP 服务器已接受测试邮件。" +
        "（耗时 " + data.latency_ms + " ms；仅代表 SMTP 服务器接受，不代表已送达收件箱）" + context.saved, "ok");
    }).catch(function (error) {
      if (!mailContextCurrent(context) || state.mailTestRequest !== context) { return; }
      if (error.status === 401) { return; }
      if (error.status === 403) { mailAccessDenied(); return; }
      if (!error.status || error.status >= 500 && error.status !== 502) {
        setMailStatus("unknown", context.saved + "测试结果尚未确认：" + (error.message || "响应未能读取") +
          "。SMTP 服务器可能已接受邮件，请检查收件箱（含垃圾箱）或由管理员核对服务端记录。不会自动重发；再次手动测试可能产生重复邮件。", "warn");
        return;
      }
      if (error.status === 429) {
        startMailTestCooldown(error.retryAfter);
        setMailStatus("rate-limited", context.saved + "请求过于频繁：" + (error.message || "请稍后再试") +
          "（测试发信限速：同一管理员 10 分钟内最多 5 次）", "error");
        return;
      }
      if (error.status === 409) {
        setMailStatus("failed", context.saved + (error.message || "邮件服务未配置或已停用，请先保存有效配置后再测试。"), "error");
        return;
      }
      setMailStatus("failed", "服务器报告测试失败：" + (error.message || "请求失败") +
        "。未获得投递成功确认；重试前请检查收件箱并核对已保存配置。" + context.saved, "error");
    }).then(function () {
      if (!mailContextCurrent(context) || state.mailTestRequest !== context) { return; }
      state.mailTestRequest = null;
      state.mailTestBusy = false;
      setBusy(button, false);
      renderMailStatus();
      updateMailButtons();
    });
  }

  function updateSettingsSections(mailSection) {
    var calendar = $("#nav-calendar-settings");
    var mail = $("#nav-mail-settings");
    var denied = mailSection && (!state.user || state.user.is_admin !== true);
    var feedback = $("#settings-feedback");
    if (feedback) {
      // showView 已把 toast 归位；拒绝提示留在设置页，不显示管理员表单。
      feedback.replaceChildren();
      if (denied) {
        var message = el("p", "form-message error");
        message.setAttribute("role", "status");
        message.setAttribute("aria-live", "polite");
        message.setAttribute("aria-atomic", "true");
        feedback.appendChild(message);
        setText(message, "无权限：当前账号没有管理员权限，无法访问邮件设置。");
      }
    }
    if (mailSection && !denied) {
      calendar.classList.remove("is-active");
      calendar.removeAttribute("aria-current");
      mail.classList.add("is-active");
      mail.setAttribute("aria-current", "page");
    } else {
      mail.classList.remove("is-active");
      mail.removeAttribute("aria-current");
      calendar.classList.add("is-active");
      calendar.setAttribute("aria-current", "page");
    }
  }

  // ------------------------------------------------------------ 事件绑定

  function bindEvents() {
    $("#tab-login").addEventListener("click", function () { hideToast(); go("#/login"); });
    $("#tab-register").addEventListener("click", function () { hideToast(); go("#/register"); });
    $("#auth-form").addEventListener("submit", submitAuth);
    document.querySelectorAll("[data-resend]").forEach(function (button) { button.addEventListener("click", resendVerification); });
    $("#auth-email").addEventListener("input", updateResendButtons);
    $("#verify-retry").addEventListener("click", function () { submitVerification(true); });
    $("#verify-resend-link").addEventListener("click", function () {
      // 无邮箱的失效链接也允许用户回到登录页填写地址后重发。
      state.offerResend = true;
    });
    $("#empty-create").addEventListener("click", focusComposer);
    $("#btn-new").addEventListener("click", focusComposer);
    $("#create-options-toggle").addEventListener("click", function () { setCreateOptions($("#create-options").hidden); });
    $("#list-retry").addEventListener("click", function () {
      loadTodos().then(function () { if ($("#list-error").hidden && state.view === "list") { $("#list-heading").focus(); } });
    });
    $("#empty-all").addEventListener("click", function () { selectFilter("all"); });
    document.querySelectorAll(".password-toggle").forEach(function (button) {
      button.addEventListener("click", function () {
        var input = $("#" + button.dataset.password);
        var visible = input.type === "password";
        input.type = visible ? "text" : "password";
        button.setAttribute("aria-pressed", String(visible));
        button.setAttribute("aria-label", (visible ? "隐藏" : "显示") + $("label[for='" + input.id + "']").textContent);
        setText(button, visible ? "隐藏" : "显示");
      });
    });
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("keydown", function (event) {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(event.key) < 0) { return; }
        event.preventDefault();
        var register = event.key === "End" || (event.key !== "Home" && state.authMode === "login");
        go(register ? "#/register" : "#/login");
        $(register ? "#tab-register" : "#tab-login").focus();
      });
    });
    document.querySelectorAll("input").forEach(function (input) {
      input.addEventListener("input", function () { input.removeAttribute("aria-invalid"); });
    });
    $("#auth-forgot").addEventListener("click", function () {
      $("#recovery-email").value = $("#auth-email").value;
      go("#/forgot-password");
    });
    $("#forgot-form").addEventListener("submit", submitForgot);
    $("#reset-form").addEventListener("submit", submitReset);
    $("#btn-logout").addEventListener("click", logout);
    $("#btn-settings").addEventListener("click", toggleSettings);
    $("#nav-settings").addEventListener("click", toggleSettings);
    $("#btn-account").addEventListener("click", function () {
      var open = $("#account-panel").hidden;
      $("#account-panel").hidden = !open;
      $("#btn-account").setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", function (event) {
      if (!event.target.closest("#account-nav")) { closeAccount(false); }
    });
    $("#account-nav").addEventListener("focusout", function (event) {
      if (!event.relatedTarget || !event.relatedTarget.closest("#account-nav")) { closeAccount(false); }
    });
    $("#btn-copy-feed").addEventListener("click", copyFeedUrl);
    $("#btn-reset-feed").addEventListener("click", resetFeedUrl);
    $("#btn-dav-generate").addEventListener("click", generateDavPassword);
    $("#btn-dav-revoke").addEventListener("click", revokeDav);
    $("#btn-copy-dav-url").addEventListener("click", function () { copyField("#dav-url", "服务器地址"); });
    $("#btn-copy-dav-user").addEventListener("click", function () { copyField("#dav-user", "用户名"); });
    $("#btn-copy-dav-password").addEventListener("click", function () { copyField("#dav-password", "密码"); });
    $("#mail-form").addEventListener("submit", saveMailSettings);
    // 逐控件绑定，保持与既有交互检查相同的可测性；测试收件地址不影响「已保存」判断。
    ["mail-enabled", "mail-host", "mail-port", "mail-user", "mail-from",
     "mail-password", "mail-clear-password"].forEach(function (id) {
      var node = $("#" + id);
      node.addEventListener("input", refreshMailDirty);
      node.addEventListener("change", refreshMailDirty);
    });
    $("#mail-test").addEventListener("click", testMailSettings);
    $("#mail-reload").addEventListener("click", function () { loadMailSettings(); });
    // 关闭 / 刷新 / 跳转离开页面时清空授权码输入框，浏览器会话恢复也不会残留明文。
    window.addEventListener("pagehide", clearMailSecrets);
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && canUseMailSettings()) { loadMailSettings(false, true); }
    });
    $("#mail-preset-qq").addEventListener("click", function () { applyMailPreset("smtp.qq.com", 465, "QQ 邮箱"); });
    $("#mail-preset-exmail").addEventListener("click", function () { applyMailPreset("smtp.exmail.qq.com", 465, "腾讯企业邮"); });
    $("#create-form").addEventListener("submit", submitCreate);

    $("#list-tabs").addEventListener("click", function (event) {
      var chip = event.target.closest ? event.target.closest(".chip") : null;
      if (chip) { selectList(chip.dataset.list); }
    });
    $("#btn-list-new").addEventListener("click", function () {
      if ($("#list-form").hidden || state.listEditing) { openListForm(null); } else { closeListForm(); }
    });
    $("#btn-list-edit").addEventListener("click", function () { openListForm(currentList()); });
    $("#list-form").addEventListener("submit", submitList);
    $("#list-cancel").addEventListener("click", closeListForm);
    $("#list-delete").addEventListener("click", deleteList);
    $("#list-colors").addEventListener("click", function (event) {
      var swatch = event.target.closest ? event.target.closest(".list-swatch") : null;
      if (!swatch) { return; }
      Array.prototype.forEach.call($("#list-colors").children, function (node) {
        node.setAttribute("aria-checked", String(node === swatch));
      });
    });

    $("#filters").addEventListener("click", function (event) {
      var chip = event.target.closest ? event.target.closest(".chip") : null;
      if (!chip) { return; }
      selectFilter(chip.dataset.status || "all");
    });

    $("#btn-back").addEventListener("click", function () { go("#/todos"); });
    $("#detail-form").addEventListener("submit", submitDetail);
    ["d-details", "new-details"].forEach(function (id) {
      $("#" + id + "-edit").addEventListener("click", function () { setMdMode(id, false); $("#" + id).focus(); });
      $("#" + id + "-view").addEventListener("click", function () { setMdMode(id, true); });
      $("#" + id + "-preview").addEventListener("click", function (event) {
        // 点链接照常打开；点其他位置进入编辑。
        if (event.target.closest && event.target.closest("a")) { return; }
        setMdMode(id, false);
        $("#" + id).focus();
      });
    });
    document.querySelectorAll("textarea").forEach(function (area) {
      area.addEventListener("input", function () { autoGrow(area); });
    });
    $("#btn-detail-delete").addEventListener("click", function () {
      if (state.current) {
        deleteTodo(state.current.id, state.current.title, state.current.subtask_total);
      } else {
        showToast("任务不存在或已被删除，请返回列表重新打开", "error");
      }
    });
    $("#btn-download-ics").addEventListener("click", downloadIcs);
    $("#note-form").addEventListener("submit", addNote);
    $("#subtask-form").addEventListener("submit", addSubtask);
    $("#btn-add-image").addEventListener("click", function () { $("#image-input").click(); });
    $("#image-input").addEventListener("change", uploadImages);
    $("#btn-stop-alerts").addEventListener("click", stopAlerts);
    ["new-urgent", "d-urgent"].forEach(function (id) {
      $("#" + id).addEventListener("change", refreshPickers);
    });
    $("#tag-filter-clear").addEventListener("click", function () { selectTag(""); $("#list-heading").focus(); });
    $("#lightbox-close").addEventListener("click", closeLightbox);
    $("#lightbox-prev").addEventListener("click", function () { openLightbox(state.lightbox.index - 1); });
    $("#lightbox-next").addEventListener("click", function () { openLightbox(state.lightbox.index + 1); });
    $("#lightbox").addEventListener("click", function (event) {
      if (event.target === $("#lightbox")) { closeLightbox(); }
    });

    $("#confirm-cancel").addEventListener("click", function () { closeConfirm(false); });
    $("#confirm-ok").addEventListener("click", function () { closeConfirm(true); });
    $("#confirm").addEventListener("click", function (event) {
      if (event.target === $("#confirm")) { closeConfirm(false); }
    });
    document.addEventListener("keydown", function (event) {
      if (state.lightbox) {
        if (event.key === "Escape") { closeLightbox(); }
        if (event.key === "ArrowLeft") { openLightbox(state.lightbox.index - 1); }
        if (event.key === "ArrowRight") { openLightbox(state.lightbox.index + 1); }
        return;
      }
      if (event.key === "Escape" && openPicker) { closePicker(true); return; }
      if (event.key === "Escape" && !$("#account-panel").hidden) { closeAccount(true); }
      if ($("#confirm").hidden) { return; }
      if (event.key === "Escape") { closeConfirm(false); }
      if (event.key === "Tab") {
        event.preventDefault();
        (document.activeElement === $("#confirm-cancel") ? $("#confirm-ok") : $("#confirm-cancel")).focus();
      }
    });

    window.addEventListener("hashchange", applyHash);
  }

  function selectFilter(status) {
    state.filter = status;
    Array.prototype.forEach.call($("#filters").querySelectorAll(".chip"), function (node) {
      var active = node.dataset.status === status;
      node.classList.toggle("is-active", active);
      node.setAttribute("aria-pressed", String(active));
    });
    return loadTodos();
  }

  // ------------------------------------------------------------ 启动

  function boot() {
    initTheme();
    try {
      var savedList = localStorage.getItem("easylife-list");
      if (savedList && savedList !== "all" && /^\d+$/.test(savedList)) { state.listFilter = Number(savedList); }
    } catch (ignore) { /* 默认显示全部 */ }
    initPickers();
    initTags();
    initToolbars();
    bindEvents();
    setText($("#today-date"), new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" }));
    // 先取会话 CSRF，再自动验证邮件；匿名的 /me 401 不会覆盖邮件路由。
    refreshSession().then(function () { state.ready = true; applyHash(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
