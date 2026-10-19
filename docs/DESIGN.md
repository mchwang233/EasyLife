# EasyLife 技术设计文档

EasyLife 是一个极简的个人任务（Todo）与提醒服务：后端仅使用 **Python 3 标准库**（`http.server` / `ThreadingHTTPServer`、`sqlite3`、`hashlib.pbkdf2_hmac`、`secrets`、`smtplib`、`email`、`threading`、`zoneinfo`），前端为**原生 HTML/CSS/JS**（不依赖任何 CDN 或第三方库）。执行环境无网络、不能安装依赖，因此全部功能必须零第三方包实现。

- 默认监听：`127.0.0.1:8086`（仅回环地址，外部访问经由 nginx 反向代理）
- 数据存储：单个 SQLite 文件（默认 `./easylife.db`）
- 认证：Cookie 会话（HttpOnly + SameSite=Lax）
- 提醒：后台线程每分钟轮询 + SMTP 发信（未配置 SMTP 时只写日志）

---

## 1. 目录结构

```
<仓库根目录>/                    # 部署时应用目录放到 /opt/easylife，见 8.1
├── easylife/                   # 应用包（Python 模块 + 静态前端）
│   ├── app.py                  # 入口：配置加载、HTTP 服务器、路由分发、静态文件
│   ├── db.py                   # SQLite 连接/初始化(schema)/迁移/行工厂/事务辅助
│   ├── auth.py                 # 注册、登录、登出、会话校验、密码哈希、限速、CSRF
│   ├── todos.py                # Todo 与笔记(notes)的业务逻辑：CRUD、字段校验、排序过滤
│   ├── reminders.py            # 提醒后台线程：扫描 remind_at、SMTP 发信、标记已发送
│   ├── calendar_ics.py         # 单条 .ics 生成、日历订阅 feed、Google Calendar 链接
│   └── static/
│       ├── index.html          # 单页应用外壳（登录/注册 + 列表 + 详情抽屉 + 设置面板）
│       ├── app.js              # 原生 JS：fetch 调用 API、渲染、事件绑定、CSRF 头
│       ├── style.css           # 原生 CSS（无框架、无 CDN、移动端自适应）
│       └── favicon.svg         # 站点图标（缺失时返回 404，不影响功能）
├── tests/                      # 全部测试；`python3 -m unittest discover -s tests`
│   ├── test_api.py             # 注册/登录/会话/限速/CSRF/CRUD/笔记/数据隔离/静态路径
│   ├── test_reminders_ics.py   # 提醒线程、.ics 内容、订阅 token、Row 兼容、生产路径回归
│   ├── test_static.py          # GET / 返回 HTML、静态资源、无外链、textContent 渲染
│   ├── test_frontend_interaction.py  # 前端 hash 路由 / 删除流程回归（驱动 tests/frontend_interaction.js）
│   └── frontend_interaction.js # Node 下加载真实 app.js 的最小 DOM 桩测试
├── docs/
│   ├── DESIGN.md               # 本设计文档
│   └── DEPLOY.md               # 部署与运维手册
└── deploy/
    ├── easylife.service        # systemd 单元（独立用户/WorkingDirectory/EnvironmentFile）
    ├── easylife.env.example    # 环境变量样例（端口、DB、SECRET、BASE_URL、SMTP）
    ├── nginx-easylife.conf     # 仅新增的 nginx server 块（不改动已有站点）
    ├── deploy.sh               # 一键部署（幂等、失败回滚）
    └── backup.sh               # SQLite 备份（.backup + gzip + 清理旧档）
```

### 1.1 模块职责与依赖方向

| 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `app.py` | 读环境变量、建库、启动 `ThreadingHTTPServer`、路由表、Cookie 解析、统一 JSON 响应与错误处理、启动提醒线程 | `db` `auth` `todos` `reminders` `calendar_ics` |
| `db.py` | `connect()`（`check_same_thread=False` + `PRAGMA foreign_keys=ON`）、`init_db()`、`row_factory = sqlite3.Row`、`now_utc()`、`tx()` 上下文管理器 | `sqlite3` |
| `auth.py` | `hash_password` / `verify_password`、`create_session` / `get_session` / `delete_session`、`require_user`、`LoginRateLimiter`、`check_csrf` | `db` |
| `todos.py` | `list_todos` / `create_todo` / `get_todo` / `update_todo` / `delete_todo` / `list_notes` / `add_note`，全部带 `user_id` 过滤 | `db` |
| `reminders.py` | `reminder_loop(stop_event)` 后台守护线程；`run_once()`、`scan_due()`、`build_message()`、`send_message()`、`deliver()`、`mark_sent()`、`purge_expired()` | `db` `auth` `calendar_ics` `smtplib` `email` `threading` |
| `calendar_ics.py` | `todo_to_ics()`、`calendar_feed()`、`google_calendar_url()`、`task_link()`、订阅 token 的签发/校验/轮换、RFC 5545 行折叠与转义 | `db` `datetime` `secrets` `hmac` |

依赖方向单向：`app.py` → 业务模块 → `db.py`。业务模块之间基本不互相导入，仅 `reminders` 额外使用 `auth`（顺带清理过期会话）与 `calendar_ics`（邮件正文里的任务链接），因此不存在循环依赖，也便于单独测试与替换。

需要注意的是，日历相关工具函数接受**字典或 `sqlite3.Row`**：`scan_due()`、`todos.list_todos()` 直接返回 `Row`，而 `sqlite3.Row` 没有 `.get()`，所以 `calendar_ics` 内部统一走 `_field(todo, key, default)` 取值，`reminders.build_message()` 先把行 `dict(row)` 化（见 6.4）。

### 1.2 请求处理模型

- 使用 `ThreadingHTTPServer` 承载并发请求；每个请求在独立线程中处理。
- SQLite 连接为线程局部（每线程新建连接），通过 `threading.local` 缓存，避免跨线程复用连接。
- 开启 `PRAGMA journal_mode=WAL` 以支持「后台提醒线程写 + 请求线程写」并发；写操作包在短事务中，遇 `sqlite3.OperationalError: database is locked` 做有限次退避重试。
- 路由为显式白名单匹配：静态前缀 `/static/`、`/`（返回 `index.html`），其余 `/api/...` 交给注册表；未匹配返回 404 JSON，未知方法返回 405。
- 请求体上限 256 KiB（超过返回 413），JSON 解析失败返回 400，统一错误体 `{"error": {"code": ..., "message": ...}}`。

---

## 2. SQLite 数据表

数据库文件路径由 `EASYLIFE_DB` 指定（默认 `./easylife.db`）。所有时间戳以 **UTC ISO-8601 文本**（`YYYY-MM-DD HH:MM:SS`，由 `db.now_utc()` 生成）存储，比较使用字符串比较即可保证时序正确。`db.init_db()` 在启动时以 `CREATE TABLE IF NOT EXISTS` 幂等建表，并为每个表建立必要索引。

### 2.1 `users`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 用户主键 |
| `email` | TEXT | NOT NULL **UNIQUE** | 登录名，入库前 `strip().lower()` 归一化 |
| `password_hash` | TEXT | NOT NULL | 格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>` |
| `created_at` | TEXT | NOT NULL DEFAULT (UTC now) | 注册时间 |
| `calendar_token` | TEXT | UNIQUE，可空 | 日历订阅私有 token（`secrets.token_urlsafe(24)` 随机生成，首次 `GET /api/me` 时惰性创建；迁移 v2 新增，见 6.3） |
| `email_verified_at` | TEXT | 可空 | 迁移 v3 新增，非空表示邮箱已验证；API 返回布尔值 `email_verified` |

```sql
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
-- 迁移 v2（PRAGMA user_version = 2）：
ALTER TABLE users ADD COLUMN calendar_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_calendar_token ON users(calendar_token);
```

### 2.2 `sessions`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `token` | TEXT | PRIMARY KEY | `secrets.token_urlsafe(32)`，仅存明文随机串（Cookie 值与库中一致，便于服务端直接吊销） |
| `user_id` | INTEGER | NOT NULL REFERENCES users(id) ON DELETE CASCADE | 会话所属用户 |
| `expires_at` | TEXT | NOT NULL | 过期时刻；签发时 = now + 30 天 |

```sql
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);
```

### 2.3 `todos`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 任务主键 |
| `user_id` | INTEGER | NOT NULL REFERENCES users(id) ON DELETE CASCADE | 归属用户，所有查询必须带此条件 |
| `title` | TEXT | NOT NULL, 长度 1–200 | 标题 |
| `details` | TEXT | NULL, **长文本** | 详情/描述（可多行，长度上限 20 000 字符） |
| `status` | TEXT | NOT NULL DEFAULT `'open'`，CHECK IN (`open`,`doing`,`done`,`archived`) | 状态 |
| `priority` | INTEGER | NOT NULL DEFAULT 2，CHECK BETWEEN 0 AND 3 | 优先级：0=最低 … 3=最高 |
| `due_at` | TEXT | NULL | 截止时间（UTC） |
| `remind_at` | TEXT | NULL | 提醒时间（UTC），提醒线程据此扫描 |
| `reminder_sent` | INTEGER | NOT NULL DEFAULT 0，CHECK IN (0,1) | 是否已发送提醒 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 最后修改时间（任何更新都刷新） |

```sql
CREATE TABLE IF NOT EXISTS todos (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         TEXT    NOT NULL,
  details       TEXT,
  status        TEXT    NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','doing','done','archived')),
  priority      INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
  due_at        TEXT,
  remind_at     TEXT,
  reminder_sent INTEGER NOT NULL DEFAULT 0 CHECK (reminder_sent IN (0,1)),
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_user   ON todos(user_id, status, due_at);
CREATE INDEX IF NOT EXISTS idx_todos_remind ON todos(reminder_sent, remind_at);
```

### 2.4 `todo_notes`

用于记录任务详情补充与**进展日志**（每条 note 是一条带时间戳的进展记录）。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 笔记主键 |
| `todo_id` | INTEGER | NOT NULL REFERENCES todos(id) ON DELETE CASCADE | 所属任务 |
| `content` | TEXT | NOT NULL, 长度 1–10 000 | 进展/详情内容 |
| `created_at` | TEXT | NOT NULL | 记录时间 |

```sql
CREATE TABLE IF NOT EXISTS todo_notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  todo_id    INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
  content    TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_todo ON todo_notes(todo_id, created_at);
```

> 由于 `todo_notes.todo_id` 的级联查询需要校验归属，删除/新增笔记前先在 `todos` 上按 `id = ? AND user_id = ?` 校验任务存在，从而实现**按 user_id 的完整隔离**。

### 2.5 数据完整性

- `PRAGMA foreign_keys = ON`（每个连接都设置），删除用户即级联删除其会话、任务与笔记。
- 时间字段全部由服务端生成（客户端传入的 `due_at` / `remind_at` 需能被解析为 ISO-8601，否则 400）。
- 软删除策略：`DELETE /api/todos/{id}` 执行物理删除并级联删除笔记；如需保留历史可改为 `status='archived'`（见 3.6 备注）。

### 2.6 邮箱认证迁移与 `auth_tokens`（v3）

`db.init_db()` 按 `PRAGMA user_version` 在事务中升级。首次添加
`users.email_verified_at` 时回填当时全部老用户，保留任务、笔记、会话和日历订阅；
新注册用户该列为空。重复启动或重放 v3 不会把后续注册的未验证用户误标为已验证。

`auth_tokens` 包含 `id`、`user_id`（外键级联删除）、`purpose`（仅 `verify` / `reset`）、
`token_hash`（唯一的 SHA-256 十六进制摘要）、`expires_at`、`used_at`、`created_at`。
该表没有明文令牌列。令牌由 `secrets.token_urlsafe(32)` 产生，验证有效期 24 小时，
重置有效期 1 小时；创建和过期时间基于同一 UTC 时刻计算。同用户、同用途的新令牌会使旧令牌失效。

消费令牌及修改账户在同一 `BEGIN IMMEDIATE` 事务中完成，过期、已用、伪造、格式错误
和用途不符均拒绝，失败回滚不会消耗令牌。重置沿用 8–128 字符规则及 PBKDF2 哈希，
同时删除该用户全部会话并标记邮箱已验证；注册和登录创建会话时重新核对密码哈希，
避免并发重置后凭旧密码创建会话。

---

## 3. REST API 契约

约定：

- 前缀 `/api`，请求与响应均为 `application/json; charset=utf-8`（`GET /api/todos/{id}/ics` 与 `GET /api/calendar.ics` 除外，返回 `text/calendar`）。
- 认证通过 Cookie `easylife_session`（HttpOnly，SameSite=Lax）；另可对同一会话使用请求头 `X-Auth-Token: <token>`（便于脚本/测试）。
- 使用有效会话的写操作（POST/PUT/DELETE）必须带请求头 `X-CSRF-Token`，值取自 `GET /api/me` 或登录响应中的 `csrf_token`（与该会话的 HMAC 不一致则 403）。注册/登录沿用匿名入口，邮箱恢复接口的可选会话策略见 3.10。
- 时间字段为 UTC ISO-8601 字符串，形如 `2024-06-01 09:30:00`。
- 统一错误体：

```json
{ "error": { "code": "unauthorized", "message": "登录状态已失效，请重新登录" } }
```

**错误码表**

| HTTP | `code` | 触发条件 |
| --- | --- | --- |
| 400 | `bad_request` | JSON 解析失败、字段缺失/类型错误/超长 |
| 400 | `invalid_token` | 邮箱认证令牌无效、已使用、过期或用途不符 |
| 401 | `unauthorized` | 无会话、会话过期/已删除 |
| 403 | `forbidden` | CSRF 校验失败、访问他人资源、订阅 token 无效 |
| 403 | `email_not_verified` | 密码正确但尚未验证邮箱，且当前配置要求验证 |
| 404 | `not_found` | 路由或资源不存在（含他人资源，避免泄露存在性） |
| 405 | `method_not_allowed` | 路径存在但方法不支持（响应头带 `Allow`） |
| 409 | `conflict` | 邮箱已被注册 |
| 413 | `payload_too_large` | 请求体 > 256 KiB |
| 429 | `rate_limited` | 登录/注册过于频繁（响应头 `Retry-After`） |
| 500 | `internal_error` | 未捕获异常（日志记录堆栈，响应不含细节） |

### 3.1 `POST /api/register` — 注册

请求：

```json
{ "email": "alice@example.com", "password": "s3cret-password" }
```

校验：`email` 长度 ≤ 254，本地部分 ≤ 64、域名标签 ≤ 63，必须符合邮箱格式，
不允许空白、连续点或首尾点；入库前去除两端空白并转小写。`password` 长度 ≥ 8、≤ 128，
支持正常 Unicode 字符，拒绝无法编码为 UTF-8 的输入。

成功 `201 Created`。配置了 SMTP 且 `EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true`
（默认）时，先发送验证邮件，返回以下响应，不创建会话：

```json
{
  "user": { "id": 1, "email": "alice@example.com", "created_at": "2024-06-01 09:30:00", "email_verified": false },
  "verification_required": true,
  "message": "注册成功，请查看验证邮件并验证邮箱后登录。"
}
```

未配置 SMTP 或明确关闭验证要求时，下发会话 Cookie，保留原有自动登录流程：

```json
{
  "user": { "id": 1, "email": "alice@example.com", "created_at": "2024-06-01 09:30:00", "email_verified": false },
  "csrf_token": "kZ8...Q1"
}
```

失败：`409 conflict`（邮箱已存在）、`400 bad_request`（格式不合法）、`429 rate_limited`。

```bash
curl -i -X POST http://127.0.0.1:8086/api/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"s3cret-password"}'
```

### 3.2 `POST /api/login` — 登录

请求与 3.1 相同。成功 `200 OK`：

```json
{
  "user": { "id": 1, "email": "alice@example.com", "created_at": "2024-06-01 09:30:00", "email_verified": true },
  "csrf_token": "kZ8...Q1",
  "expires_at": "2024-07-01 09:30:00"
}
```

响应头：`Set-Cookie: easylife_session=<token>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`（当 `BASE_URL` 为 https 时附加 `Secure`；设置了 `EASYLIFE_BASE_PATH` 时 `Path` 为该前缀，如 `Path=/todos`，见 8.5）。

失败：`401 unauthorized`（邮箱或密码错误，**不区分哪种错误**）、`403 email_not_verified`
（密码正确、邮箱未验证且要求验证；前端显示提示和“重新发送验证邮件”）、
`429 rate_limited`（同一邮箱连续失败 ≥ 5 次 / 同一 IP 窗口内 ≥ 20 次，见第 5 节）。
登录及 `GET /api/me` 的 `user` 均包含布尔字段 `email_verified`。

### 3.3 `POST /api/logout` — 登出

请求：空体（需 CSRF 头）。服务端删除 `sessions` 行并下发过期 Cookie。

成功 `200 OK`：

```json
{ "ok": true }
```

失败：`401 unauthorized`（未登录）、`403 forbidden`（CSRF 失败）。

### 3.4 `GET /api/me` — 当前用户

成功 `200 OK`：

```json
{
  "user": { "id": 1, "email": "alice@example.com", "created_at": "2024-06-01 09:30:00", "email_verified": true },
  "csrf_token": "kZ8...Q1",
  "calendar_token": "8f3c...a2 (随机生成并存于 users.calendar_token，可通过 POST /api/calendar/token/reset 轮换)",
  "calendar_feed_url": "https://easylife.example.com/api/calendar.ics?token=8f3c...a2",
  "server_time": "2024-06-01 09:31:02"
}
```

前端在页面加载时调用本接口：未登录返回 `401`，前端切换到登录视图；成功后把 `csrf_token` 存入内存变量，用于后续写请求。

失败：`401 unauthorized`。

### 3.5 `GET /api/todos` — 任务列表 / `POST /api/todos` — 新建任务

`GET /api/todos` 支持的查询参数（均可选）：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `status` | `open` / `doing` / `done` / `archived` / `all` | 默认 `all`（不含 `archived`） |
| `q` | 关键字 | 对 `title` 与 `details` 做 `LIKE %q%`（转义 `%` `_`） |
| `due_before` / `due_after` | ISO-8601 | 按 `due_at` 过滤 |
| `limit` / `offset` | 整数 | 分页，`limit` 默认 100、上限 500 |
| `order` | `due_at` / `priority` / `created_at` / `updated_at` | 默认 `priority DESC, due_at ASC, id DESC` |

成功 `200 OK`：

```json
{
  "items": [
    {
      "id": 12,
      "title": "写项目周报",
      "details": "汇总本周进展与下周计划",
      "status": "open",
      "priority": 3,
      "due_at": "2024-06-03 10:00:00",
      "remind_at": "2024-06-03 09:00:00",
      "reminder_sent": 0,
      "created_at": "2024-06-01 09:33:10",
      "updated_at": "2024-06-01 09:33:10",
      "notes_count": 2
    }
  ],
  "total": 1,
  "limit": 100,
  "offset": 0
}
```

`POST /api/todos` 请求体（`title` 必填，其余可选）：

```json
{
  "title": "写项目周报",
  "details": "汇总本周进展与下周计划",
  "status": "open",
  "priority": 3,
  "due_at": "2024-06-03 10:00:00",
  "remind_at": "2024-06-03 09:00:00"
}
```

成功 `201 Created`：返回单个任务对象（同列表中元素，额外含 `notes: []` 与 `ics_url`、`google_calendar_url`）。

失败：`400 bad_request`（标题为空/超 200 字、`status` 非法、`priority` 非 0–3、时间不可解析、`remind_at` 晚于 `due_at` 时给出警告字段 `warnings`）、`401`、`403`、`413`。

### 3.6 `GET/PUT/DELETE /api/todos/{id}` — 单条任务

`GET /api/todos/{id}` → `200 OK`：

```json
{
  "id": 12,
  "title": "写项目周报",
  "details": "汇总本周进展与下周计划",
  "status": "doing",
  "priority": 3,
  "due_at": "2024-06-03 10:00:00",
  "remind_at": "2024-06-03 09:00:00",
  "reminder_sent": 0,
  "created_at": "2024-06-01 09:33:10",
  "updated_at": "2024-06-01 10:02:44",
  "notes": [
    { "id": 5, "todo_id": 12, "content": "已完成第 1 节", "created_at": "2024-06-01 10:01:00" }
  ],
  "ics_url": "/api/todos/12/ics",
  "google_calendar_url": "https://calendar.google.com/calendar/render?action=TEMPLATE&text=..."
}
```

`PUT /api/todos/{id}` 请求体（部分更新，只传需要改的字段；`null` 表示清空 `details`/`due_at`/`remind_at`）：

```json
{ "status": "done", "remind_at": null }
```

成功 `200 OK`（返回更新后的完整对象）。语义：`status` 从非 `done` 变为 `done` 时自动设置 `updated_at`；若 `remind_at` 被修改，则 `reminder_sent` 重置为 `0`；若 `remind_at` 设为 `null`，则不再提醒。

`DELETE /api/todos/{id}` → `200 OK`：

```json
{ "ok": true, "deleted_id": 12 }
```

备注：需要「软删除/归档」时，用 `PUT {"status":"archived"}` 代替，归档任务默认不出现在列表中。

失败：`400`（非法字段）、`401`、`403`（CSRF）、`404 not_found`（任务不存在**或不属于当前用户**）、路径 `{id}` 非整数时 `404`。

### 3.7 `POST/GET /api/todos/{id}/notes` — 任务笔记 / 进展日志

`POST` 请求体：

```json
{ "content": "已联系设计同学，等待切图" }
```

成功 `201 Created`：

```json
{ "id": 6, "todo_id": 12, "content": "已联系设计同学，等待切图", "created_at": "2024-06-01 10:05:00" }
```

`GET` → `200 OK`（按 `created_at ASC, id ASC` 排序）：

```json
{
  "items": [
    { "id": 5, "todo_id": 12, "content": "已完成第 1 节", "created_at": "2024-06-01 10:01:00" },
    { "id": 6, "todo_id": 12, "content": "已联系设计同学，等待切图", "created_at": "2024-06-01 10:05:00" }
  ],
  "total": 2
}
```

失败：`400`（内容为空或超 10 000 字）、`401`、`403`、`404`（任务不存在或非本人）。笔记本身不提供修改/删除接口；如需清理由删除任务级联处理。

### 3.8 `GET /api/todos/{id}/ics` — 单条任务日历导出

响应 `200 OK`，`Content-Type: text/calendar; charset=utf-8`，`Content-Disposition: attachment; filename="easylife-12.ics"`：

```text
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//EasyLife//Todo//CN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:easylife-todo-12@easylife
DTSTAMP:20240601T100544Z
DTSTART:20240603T100000Z
DTEND:20240603T103000Z
SUMMARY:写项目周报
DESCRIPTION:汇总本周进展与下周计划\n优先级 3\nhttps://easylife.example.com/todos/12
STATUS:CONFIRMED
PRIORITY:1
BEGIN:VALARM
TRIGGER:-PT30M
ACTION:DISPLAY
DESCRIPTION:写项目周报
END:VALARM
END:VEVENT
END:VCALENDAR
```

规则：`DTSTART` 取 `due_at`（无 `due_at` 时取 `remind_at`，都没有则返回 `400 bad_request`）；`DTEND = DTSTART + 30 分钟`；`VALARM` 默认提前 30 分钟，若设置了 `remind_at` 则按 `due_at - remind_at` 的差值生成 `TRIGGER`；文本中的 `\` `;` `,` 换行分别转义为 `\\` `\;` `\,` `\n`，行内容按 RFC 5545 折叠（每 75 字节插入 `CRLF + 空格`）；换行统一 `CRLF`。认证：需登录会话（或 `X-Auth-Token`）。

### 3.9 `GET /api/calendar.ics?token=` — 日历订阅源

- 用途：把某用户的全部未完成任务订阅进 Google Calendar / Apple 日历 / Thunderbird，客户端会周期性拉取。
- 认证：**不使用 Cookie**，而是 URL 中的私有 `token`。`token` 为每用户随机生成的 32 字符 URL-safe 串（`secrets.token_urlsafe(24)`），存于 `users.calendar_token`（唯一索引）；服务端按索引查找后再用 `hmac.compare_digest` 复核。
- 成功 `200 OK`：`Content-Type: text/calendar; charset=utf-8`，`Cache-Control: no-store`，包含该用户所有 `status IN ('open','doing')` 且带时间的任务（`due_at` 优先，否则 `remind_at`）：

```json
{
  "ok": true,
  "note": "返回 text/calendar，非 JSON；此处仅示意",
  "count": 3
}
```

```text
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//EasyLife//Calendar Feed//CN
CALSCALE:GREGORIAN
X-WR-CALNAME:EasyLife 任务
X-WR-TIMEZONE:UTC
BEGIN:VEVENT
UID:easylife-todo-12@easylife
DTSTAMP:20240601T100544Z
DTSTART:20240603T100000Z
DTEND:20240603T103000Z
SUMMARY:写项目周报
END:VEVENT
END:VCALENDAR
```

- 无/错 token → `403 forbidden`（`{"error":{"code":"forbidden","message":"订阅 token 无效"}}`）；`token` 缺失 → `400 bad_request`。
- 订阅 URL 由 `BASE_URL` 拼接：`{BASE_URL}/api/calendar.ics?token=...`。
- 轮换：`POST /api/calendar/token/reset`（需登录 + `X-CSRF-Token`）生成新 token 并立即使旧 URL 失效，返回 `{"calendar_token": "...", "calendar_feed_url": "..."}`。
- Google Calendar 添加链接（前端根据任务字段生成，无需服务端接口）：

```text
https://calendar.google.com/calendar/render?action=TEMPLATE
  &text=<URL 编码标题>
  &details=<URL 编码详情 + 任务链接>
  &dates=20240603T100000Z/20240603T103000Z
```

`dates` 使用 UTC 基本格式（`%Y%m%dT%H%M%SZ/...`，`-` 为结束分隔符），仅当任务有 `due_at` 时提供。

---

### 3.10 邮箱验证与密码恢复

| 接口 | JSON 请求体 | 成功行为 |
| --- | --- | --- |
| `POST /api/verify-email` | `token` | `200`，一次性消费验证令牌，标记邮箱已验证 |
| `POST /api/resend-verification` | 登录时可空，否则提供 `email` | `200`，仅向未验证账户发送邮件；会话存在时使用当前用户邮箱 |
| `POST /api/forgot-password` | `email` | 邮箱存在与否均为 `200` 和完全相同响应体；存在时尝试发送重置邮件 |
| `POST /api/reset-password` | `token`、`password` | `200`，更新密码、撤销全部旧会话并清除当前会话 Cookie，要求重新登录 |

找回密码成功响应固定为：

```json
{"ok": true, "message": "如果该邮箱已注册，我们已尝试发送重置密码邮件，请检查收件箱；60 秒后可重新发送。"}
```

SMTP 发送或邮件构造失败也保持相同响应，并在服务器记录错误。所有新接口复用现有
JSON 对象、256 KiB 请求体限制、会话和 CSRF 检查；携带有效会话的写请求必须提供
该会话的 `X-CSRF-Token`，匿名用户可以凭邮箱或一次性令牌操作。邮箱上限 254 字符，
新密码 8–128 字符，令牌输入上限 128 字符，实际令牌为 43 位 URL 安全字符。

注册验证邮件及重发共用同一邮箱的 60 秒间隔；找回密码独立按同一邮箱限制 60 秒，
超过返回 `429 rate_limited` 和 `Retry-After`。存在与不存在邮箱均在查询用户前计数；
另有每 IP 每小时 20 次邮件请求限制。限流器复用进程内计数，重启后清空。

认证邮件通过 `auth_mail.py` 调用 `reminders.normalize_config()` 和 `send_message()`，
共用 `SMTP_*`。`SMTP_HOST` 为空时允许登录、将认证邮件正文写入日志并发出 WARNING；
已配置但故障的 SMTP 不会绕过邮箱验证。邮件地址优先使用 `EASYLIFE_PUBLIC_URL`，
默认空时沿用 `PUBLIC_URL` / `BASE_URL`，结合 `EASYLIFE_BASE_PATH` 保留 `/todos` 前缀：
`https://mch.wang/todos/#/verify?token=...` 和 `https://mch.wang/todos/#/reset?token=...`。
认证公开地址或原有 `PUBLIC_URL` / `BASE_URL` 任一使用 HTTPS 时，会话 Cookie 均带
`Secure`，包括只设置 `EASYLIFE_PUBLIC_URL` 的情况。
前端打开链接只展示表单，由用户提交 POST 消费令牌；找回和重置切换时禁用隐藏字段，
避免残留输入触发浏览器校验。

## 4. 安全设计

### 4.1 会话与 Cookie

- 会话令牌由 `secrets.token_urlsafe(32)` 生成（约 256 bit 熵），仅在服务端保存明文于 `sessions.token`（PK），便于立即吊销。
- Cookie 属性：`Set-Cookie: easylife_session=<token>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`；当 `BASE_URL` 以 `https://` 开头时追加 `Secure`。`Path` 取 `EASYLIFE_BASE_PATH`（未设置时为 `/`），部署在 `mch.wang/todos` 时 Cookie 只随 `/todos/...` 请求发送，不会与同域其他服务的 Cookie 互相串扰。`HttpOnly` 阻断 `document.cookie` 读取以降低 XSS 窃取风险；`SameSite=Lax` 阻断跨站写请求携带 Cookie。
- 有效期 30 天；每次成功请求不做滑动续期以保持简单（可选策略：剩余 < 7 天时刷新 `expires_at`）。
- 服务端校验：`token` 存在、`expires_at > now`；过期会话由提醒线程定期 `DELETE FROM sessions WHERE expires_at <= now` 顺带清理（复用同一后台线程的清理分支）。
- 登出：删除 `sessions` 行并下发同名过期 Cookie（`Max-Age=0`）。
- 响应头统一附加：`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`、`Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'`（禁止内联脚本与外链，前端不使用任何 CDN）。

### 4.2 密码存储

- 使用 `hashlib.pbkdf2_hmac('sha256', password, salt, 260000)`；`salt = secrets.token_bytes(16)`，`dklen=32`。
- 存储格式：`pbkdf2_sha256$260000$<salt_b64url>$<hash_b64url>`（自描述，便于将来升级算法/轮数）。
- 校验用 `hmac.compare_digest` 做常量时间比较；登录失败与成功路径保持大致相同耗时（无效邮箱时也执行一次假哈希），避免用户枚举时序差异。
- 备选：`hashlib.scrypt(password, salt=salt, n=2**14, r=8, p=1)`，格式 `scrypt$16384$8$1$<salt>$<hash>`；切换时按前缀分派校验函数。
- 明文密码只在内存中短暂存在，禁止写日志；日志中对邮箱做掩码（`a***@example.com`）。

### 4.3 数据隔离（按 user_id）

- 所有 `todos` / `todo_notes` 访问都必须同时带 `user_id` 条件，典型形态：

```sql
-- 读取（列表）
SELECT * FROM todos WHERE user_id = ? AND status != 'archived' ORDER BY priority DESC, due_at ASC;
-- 读取/更新/删除（单条）
SELECT * FROM todos WHERE id = ? AND user_id = ?;
UPDATE todos SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?;
DELETE FROM todos          WHERE id = ? AND user_id = ?;   -- 级联删除其 todo_notes
-- 笔记（先校验任务归属，再操作）
INSERT INTO todo_notes (todo_id, content, created_at)
SELECT id, ?, ? FROM todos WHERE id = ? AND user_id = ?;
```

（`INSERT ... SELECT` 天然保证只有任务属于当前用户时才插入。）

- 越权访问统一返回 `404 not_found` 而非 `403`，避免泄露资源是否存在。
- 请求体的 `user_id` 字段一律忽略；用户身份只来自会话。
- 新增功能代码评审检查点：任何 SQL 是否包含 `user_id` 约束；所有写语句是否使用参数绑定（`?`），禁止字符串拼接 SQL。

### 4.4 CSRF 防护策略

采用「双重提交 + 自定义请求头」三层防护：

1. **SameSite=Lax Cookie**：跨站表单/`<img>` 触发的 POST 不会带上会话 Cookie，挡掉绝大多数传统 CSRF。
2. **会话绑定 CSRF Token（同步器模式）**：登录/注册时生成 `csrf_token = secrets.token_urlsafe(24)`，与 `sessions.token` 一起在服务端内存映射（或存 `sessions` 表的可选列；本设计用 `app.py` 内的 `lru_cache` 风格字典 + 会话令牌哈希为键，避免改动既定表结构），通过 `GET /api/me` 返回给前端，前端保存在 JS 内存变量中。
3. **自定义头校验**：所有 POST/PUT/DELETE 必须携带 `X-CSRF-Token`，服务端用 `hmac.compare_digest` 与会话对应的值比对，不符返回 `403 forbidden`；同时只接受 `Content-Type: application/json` 的写请求，浏览器跨站表单无法伪造该 Content-Type 而不触发预检。

例外与说明：`GET /api/calendar.ics?token=` 为无 Cookie 的只读订阅接口，不做 CSRF 校验（不使用会话凭据，无权修改数据）；`GET /api/todos/{id}/ics` 为只读，同样不校验 CSRF。

### 4.5 登录限速与会话安全附加项

- 内存限速器 `LoginRateLimiter`（`threading.Lock` + 字典，进程内）：

| 维度 | 阈值 | 窗口 | 超限行为 |
| --- | --- | --- | --- |
| 邮箱 | 5 次失败 | 15 分钟 | `429`，成功后清零 |
| 客户端 IP（`X-Real-IP`/`X-Forwarded-For` 首个值，回退 `client_address`） | 20 次失败 | 15 分钟 | `429` |
| 注册 | 每 IP 10 次 | 1 小时 | `429` |

- `429` 响应带 `Retry-After: <秒>`；窗口到期条目惰性清理，避免内存增长。
- 其他：请求体大小限制 256 KiB；URL 长度限制 2 KiB；`sqlite3` 全部使用参数绑定；异常统一捕获为 `500 internal_error`，细节仅写 stderr 日志；`EASYLIFE_SECRET` 缺失时**拒绝启动**（因为 CSRF token 派生依赖它）；生产环境必须由 nginx 终止 TLS，应用只监听 `127.0.0.1`。

---

## 5. 邮件提醒机制

### 5.1 后台线程

`app.py` 启动时创建**守护线程**：`threading.Thread(target=reminders.reminder_loop, args=(stop_event,), daemon=True).start()`。

```python
# reminders.py（伪代码）
def reminder_loop(stop_event, interval=60, *, db_path, smtp, sender=None, base_url=""):
    while not stop_event.wait(interval):          # 每 60 秒唤醒一次，可被优雅停止
        try:
            conn = db.get_conn(db_path)           # 线程私有连接
            run_once(conn, smtp=smtp, sender=sender, base_url=base_url)
            purge_expired(conn)                   # 顺带清理过期会话
        except Exception:
            log.exception("reminder loop iteration failed")   # 单次失败不影响下一轮

def run_once(conn, *, now=None, smtp=None, sender=None, base_url=""):
    sent = []
    for row in scan_due(conn, now=now):           # row 是 sqlite3.Row
        try:
            result = deliver(row, smtp=smtp, sender=sender, base_url=base_url)
        except Exception:
            log.exception("could not send todo=%s, will retry next round", row["id"])
            continue                              # 未标记：下一轮自动重试
        if result == "logged":                    # 未配置 SMTP：只记日志，保持待发送
            continue
        if result == "sent" and mark_sent(conn, row["id"]):
            sent.append(row["id"])
    return sent

def deliver(row, *, smtp=None, sender=None, base_url=""):
    cfg = smtp_config() if smtp is None else normalize_config(smtp)
    if not _valid_address(row["email"]):          # 非法收件人：跳过并记日志
        return None
    return (sender or send_message)(cfg, build_message(row, cfg, base_url))
```

- 唤醒间隔 60 秒（`stop_event.wait(60)`，比 `time.sleep` 更容易在 SIGTERM 时退出）；`app.py` 在 `finally` 中 `stop_event.set()` 后 `server.shutdown()`。
- 线程持自己的 SQLite 连接（每线程连接，`PRAGMA foreign_keys=ON`），与请求线程共享同一 DB 文件，靠 WAL 模式与短事务避免 `database is locked`。
- 只处理「自己的」待发提醒，不做分布式锁；若将来多进程部署，用 `UPDATE ... WHERE reminder_sent=0` 的原子性做抢占（见 5.3）。

### 5.2 扫描条件与发送流程

扫描 SQL（利用 `idx_todos_remind` 索引）：

```sql
SELECT t.id, t.user_id, t.title, t.details, t.due_at, t.remind_at, u.email
FROM todos t JOIN users u ON u.id = t.user_id
WHERE t.reminder_sent = 0
  AND t.remind_at IS NOT NULL
  AND t.remind_at <= ?                 -- 参数：db.now_utc()
  AND t.status IN ('open', 'doing')    -- 已完成/归档任务不再提醒
ORDER BY t.remind_at ASC
LIMIT 100;                             -- 单轮上限，防止积压时一口气发信
```

时区处理：`remind_at` 一律以 UTC 存储，扫描时用 `db.now_utc()` 比较字符串即可；前端输入本地时间时由 JS 转 UTC 后提交。

发送成功后（`AND reminder_sent = 0` 让重复扫描不会二次投递）：

```sql
UPDATE todos SET reminder_sent = 1, updated_at = ?
WHERE id = ? AND reminder_sent = 0;   -- rowcount == 1 才算本轮发送成功
```

- `reminders.mark_sent()` 在短事务里执行上面的语句并返回 `cursor.rowcount == 1`，只有真正抢到的调用才计入 `run_once()` 的返回值，因此「同一轮并发扫描 / 进程重启」都不会重复发信。
- 失败重试：SMTP 发送异常时不标记 `reminder_sent`（只记异常日志），下一轮（60 秒后）自动重试，不做失败次数上限，避免提醒被永久静默丢弃。
- 用户改期：`PUT /api/todos/{id}` 修改 `remind_at` 会把 `reminder_sent` 重置为 0（见 3.6），使新时间点仍能触发提醒。

### 5.3 多进程部署加固（可选）

```sql
UPDATE todos SET reminder_sent = 1, updated_at = ?
WHERE id = ? AND reminder_sent = 0;   -- changes()==1 才真正发送
```

5.2 已经是「先发后标 + `WHERE reminder_sent = 0`」，单进程下天然不重复；若将来多进程/多实例共用一份 DB，可把标记提前到发送之前（用 `rowcount == 1` 抢占），代价是发送失败需回滚标记（`UPDATE todos SET reminder_sent = 0 WHERE id = ?`）。当前实现选择 5.2 的顺序：失败也不会丢提醒。

### 5.4 SMTP 配置与发信实现

SMTP 参数**全部来自环境变量**：

| 环境变量 | 必填 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | 否 | SMTP 服务器主机名；**未设置时进入日志模式** |
| `SMTP_PORT` | 否 | 默认 `587`（STARTTLS）；若为 `465` 则使用 `SMTP_SSL` |
| `SMTP_USER` | 否 | 登录用户名，为空则匿名发信 |
| `SMTP_PASS` | 否 | 登录密码/应用专用密码（不写入日志） |
| `SMTP_FROM` | 否 | 发件人，形如 `EasyLife <no-reply@example.com>`；缺省用 `SMTP_USER` |

发信实现要点（仅标准库 `smtplib` + `email.message.EmailMessage`）：

```python
msg = EmailMessage()
msg["Subject"] = f"[EasyLife] 任务提醒：{title}"
msg["From"], msg["To"] = from_addr, to_addr
msg.set_content(f"{title}\n截止：{due_at or '未设置'}\n详情：{details or '-'}")
msg.add_alternative(html_body, subtype="html")   # 原生 HTML 邮件，无外链资源
with smtplib.SMTP(host, port, timeout=15) as s:  # 或 SMTP_SSL
    s.ehlo()
    if port != 465:
        s.starttls()
    if user:
        s.login(user, password)
    s.send_message(msg)
```

- 超时 15 秒；整个发信包在 `try/except` 中，异常只记日志，绝不中断后台线程。
- **未配置 SMTP 时（`SMTP_HOST` 为空）：只记录日志，不标记**，格式如
  `[reminder] (SMTP disabled) would send to a***@example.com todo=12 remind_at=2024-06-03 09:00:00`，
  `reminder_sent` 保持 `0`，该条提醒仍处于「待发送」状态，**配好 SMTP 后由下一轮扫描补发**（只发一次，发送成功才置 `1`）。
- 主题/正文中的用户内容做换行清理（去除 `\r`、折叠多余 `\n`）以避免邮件头注入；地址用 `email.utils.parseaddr` 校验，非法则跳过并记日志。

---

## 6. 日历集成（.ics 导出与订阅）

### 6.1 三种使用方式

| 方式 | 入口 | 认证 | 用途 |
| --- | --- | --- | --- |
| 单条任务下载 | `GET /api/todos/{id}/ics` | 会话 Cookie / `X-Auth-Token` | 「把这个任务加到我的日历」，一次性导入 |
| 日历订阅 | `GET /api/calendar.ics?token=<私有令牌>` | URL 私有 token（无 Cookie） | Google/Apple 日历周期性拉取全部任务 |
| 重置订阅 | `POST /api/calendar/token/reset` | 会话 Cookie + `X-CSRF-Token` | 轮换本人订阅 token，旧 URL 立即失效 |
| Google 快速添加 | 前端链接 `https://calendar.google.com/calendar/render?action=TEMPLATE&text=...&dates=...` | 无（由 Google 侧处理） | 桌面/移动端不下载文件的快捷方式 |

### 6.2 `calendar_ics.py` 接口

```python
def escape_text(s: str) -> str          # \ ; , 换行 转义
def fold_line(line: str) -> list[str]   # 每 75 字节折行，续行以空格开头
def todo_to_ics(todo: dict, *, base_url: str) -> str        # 单事件 VCALENDAR
def calendar_feed(todos: list[dict], *, name: str, base_url: str) -> str  # 多事件 VCALENDAR
def google_calendar_url(todo: dict, *, base_url: str) -> str
def get_or_create_token(conn, user_id: int) -> str      # 读取 users.calendar_token，空则随机生成并写入
def reset_token(conn, user_id: int) -> str              # 轮换：写入新的随机 token
def user_id_for_token(conn, token: str) -> int | None  # 按唯一索引查找 + compare_digest 复核
```

生成要点：

- `UID` 稳定：`easylife-todo-{id}@easylife`（订阅刷新时同一任务不会重复创建事件）。
- `DTSTAMP` 用当前 UTC；`DTSTART`/`DTEND` 用 UTC 基本格式（`YYYYMMDDTHHMMSSZ`，无时区名），保证无需 `VTIMEZONE` 组件即可被广泛解析。
- 单条导出包含 `SUMMARY`、`DESCRIPTION`（`details` + 优先级 + 任务链接）、`STATUS`（`done` → `COMPLETED`，否则 `CONFIRMED`）、`PRIORITY`（内部 3→`1`、2→`5`、1→`9`、0→`9`）、以及 `VALARM`（默认 `-PT30M` DISPLAY）。
- 订阅源包含 `X-WR-CALNAME: EasyLife 任务`、`X-WR-TIMEZONE: UTC`，`METHOD:PUBLISH`；只输出 `open/doing` 且有时间的任务；输出顺序按 `due_at ASC`。
- 行结束一律 `\r\n`；末尾行 `END:VCALENDAR\r\n`。

### 6.3 订阅 token 的签发、校验与轮换

- 随机存储而非派生：每个用户的 token 为 `secrets.token_urlsafe(24)`（约 192 bit 熵），存于 `users.calendar_token`（唯一索引），首次 `GET /api/me` 时惰性生成。token 与 `EASYLIFE_SECRET` **无关**，因此更换 `EASYLIFE_SECRET` **不会**使订阅 URL 失效（只会让已登录会话的 CSRF token 变化）。
- 校验：解析 URL 查询串（`urllib.parse.parse_qs`），按 `calendar_token` 索引查找用户并用 `hmac.compare_digest` 复核；命中即返回该用户的 feed，否则 `403 forbidden`；缺少 token 为 `400`。
- 轮换（按用户）：用户在 UI 的「日历订阅」面板点击「重置」，调用 `POST /api/calendar/token/reset`，旧 URL 立即失效，再复制新 URL。
- 轮换（全部用户，如怀疑批量泄露）：管理员执行 `UPDATE users SET calendar_token = NULL;`，所有旧 URL 立即失效，用户下次打开页面时自动生成新 token。
- 泄露缓解：订阅 URL 只出现在用户自己的「设置/订阅」区域；响应头 `Cache-Control: no-store`；nginx 访问日志对 `token` 参数做正则遮蔽（见 8.3），避免令牌落入日志。
- 前端交互：`GET /api/me` 返回 `calendar_feed_url`，页面显示一个只读输入框 + 「复制」按钮，以及「添加到 Google 日历」链接（每条任务详情抽屉中）。

### 6.4 字典与 `sqlite3.Row` 的兼容（P1 回归）

`db.connect()` 把 `row_factory` 设为 `sqlite3.Row`，所以 `scan_due()`（提醒线程）、`todos.list_todos()`/`get_todo()`（HTTP 层）拿到的都是 `Row`。`Row` 支持 `row["col"]` 但**没有 `.get()`**，一旦用 `.get()` 就会抛 `AttributeError: 'sqlite3.Row' object has no attribute 'get'`。

约定：

- `calendar_ics` 内部所有取字段都经 `_field(todo, key, default=None)`（`try: todo[key] except (KeyError, IndexError, TypeError)`），因此 `todo_to_ics()`、`calendar_feed()`、`google_calendar_url()`、`task_link()` 对字典和 `Row` 都成立；缺列（如只 `SELECT` 了几列）时回落到默认值而不是报错。
- `reminders.build_message(row, cfg, base_url)` 入口立即 `row = dict(row)`，再拼正文与 `任务链接：{BASE_URL}/todos/{id}`。

回归用例：`tests/test_reminders_ics.py::RowCompatibilityTests`（模块级：把 `Row` 传进各工具函数）以及 `ProductionReminderPathTests::test_daemon_thread_delivers_with_a_non_empty_base_url`（完整生产路径：非空 `BASE_URL` + 本地迷你 SMTP 服务器 + 真实 `smtplib` + 守护线程，断言邮件带任务链接、`reminder_sent = 1`、且只投递一次）。

---

## 7. 配置（全部来自环境变量）

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `EASYLIFE_PORT` | 否 | `8086` | 监听端口 |
| `EASYLIFE_HOST` | 否 | `127.0.0.1` | **默认仅绑定回环地址**，不暴露公网；只有在明确需要时才改（本设计不推荐） |
| `EASYLIFE_DB` | 否 | `./easylife.db`（相对 `WorkingDirectory`） | SQLite 文件路径；目录须存在且可写 |
| `EASYLIFE_SECRET` | **是** | 无 | 用于 CSRF token 派生的密钥（订阅 token 为随机存储，与其无关）；缺失或短于 16 字符时拒绝启动并打印醒目错误 |
| `EASYLIFE_BASE_PATH` | 否 | 空（根路径） | 路径前缀部署，如 `/todos`；规范化为「以 `/` 开头、不以 `/` 结尾」，非法字符拒绝启动。详见 8.5 |
| `PUBLIC_URL` | 否 | 同 `BASE_URL` | 对外可达地址（推荐的新名字，优先于 `BASE_URL`），可写 `https://mch.wang/todos` 或 `https://mch.wang`，程序保证结果末尾恰好带一次 `EASYLIFE_BASE_PATH` |
| `BASE_URL` | 否 | `http://127.0.0.1:8086` | 对外可达地址（旧名，仍兼容），用于拼接订阅 URL、任务链接、Google 链接；以 `https://` 开头时 Cookie 追加 `Secure` |
| `EASYLIFE_PUBLIC_URL` | 否 | 空 | 认证邮件公开地址，空时沿用 `PUBLIC_URL` / `BASE_URL`；自动补齐路径前缀，线上为 `https://mch.wang/todos` |
| `EASYLIFE_REQUIRE_EMAIL_VERIFICATION` | 否 | `true` | 配置 SMTP 时要求新用户验证邮箱后登录；未配置 SMTP 时允许登录并记录 WARNING 和认证邮件正文 |
| `SMTP_HOST` | 否 | 空（日志模式） | SMTP 主机 |
| `SMTP_PORT` | 否 | `587` | `465` 走 `SMTP_SSL`，其余走 `starttls()` |
| `SMTP_USER` | 否 | 空 | SMTP 用户名 |
| `SMTP_PASS` | 否 | 空 | SMTP 密码，不入日志 |
| `SMTP_FROM` | 否 | `SMTP_USER` | 发件人 |
| `EASYLIFE_REMIND_INTERVAL` | 否 | `60` | 提醒扫描间隔（秒），测试时可设 1 |
| `EASYLIFE_SESSION_TTL` | 否 | `2592000` | 会话有效期（秒，默认 30 天） |

加载方式（`app.py`）：

```python
cfg = {
    "host": os.environ.get("EASYLIFE_HOST", "127.0.0.1"),
    "port": int(os.environ.get("EASYLIFE_PORT", "8086")),
    "db": os.environ.get("EASYLIFE_DB", "./easylife.db"),
    "secret": os.environ.get("EASYLIFE_SECRET", ""),
    "base_path": normalize_base_path(os.environ.get("EASYLIFE_BASE_PATH", "")),   # "" 或 "/todos"
    # PUBLIC_URL 优先、BASE_URL 兼容；末尾补齐 base_path（已带则不重复）
    "base_url": public_base_url(os.environ.get("PUBLIC_URL") or os.environ.get("BASE_URL")
                                or f"http://127.0.0.1:{port}", base_path),
    "smtp": {k: os.environ.get(f"SMTP_{k}") for k in ("HOST", "PORT", "USER", "PASS", "FROM")},
}
assert len(cfg["secret"]) >= 16, "EASYLIFE_SECRET 必须设置且至少 16 字符"
```

- 启动日志打印**生效配置摘要**（`db` 路径、`host:port`、`base_url`、SMTP 是否启用），但永不打印 `EASYLIFE_SECRET`、`SMTP_PASS`。
- 不允许命令行参数覆盖（保持唯一配置来源），便于 systemd 通过 `EnvironmentFile` 管理。

---

## 8. 部署原则

核心原则：**最小侵入**。EasyLife 使用独立的 Linux 系统用户、独立的安装目录 `/opt/easylife`，由 systemd 以普通用户身份运行，只监听 `127.0.0.1`；对外访问**仅在 nginx 中新增一个 server 块（或一个 location）**，绝不修改任何既有站点配置文件。

### 8.1 独立用户与目录

```bash
# 1) 专用系统用户（无登录 shell、无 home）
sudo useradd --system --no-create-home --shell /usr/sbin/nologin easylife

# 2) 安装目录与数据目录
sudo mkdir -p /opt/easylife /var/lib/easylife /etc/easylife
sudo cp -r easylife/* /opt/easylife/          # app.py db.py ... static/ deploy/
sudo chown -R easylife:easylife /opt/easylife /var/lib/easylife
sudo chmod 750 /opt/easylife /var/lib/easylife
sudo chmod 640 /etc/easylife/easylife.env     # 含密钥，仅 root/服务可读
sudo chown root:easylife /etc/easylife/easylife.env
```

- 代码目录 `/opt/easylife` 与该用户专属数据目录 `/var/lib/easylife/easylife.db` 分离，升级时只替换代码，数据不动（`EASYLIFE_DB=/var/lib/easylife/easylife.db`）。
- 不使用 root、不使用端口 < 1024（8086 无需特权），不写入 `/opt/easylife` 以外的任何路径（日志走 stdout/stderr 交给 journald）。

### 8.2 systemd 服务（`deploy/easylife.service`）

```ini
[Unit]
Description=EasyLife todo & reminder service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=easylife
Group=easylife
WorkingDirectory=/opt/easylife
EnvironmentFile=/etc/easylife/easylife.env
ExecStart=/usr/bin/python3 /opt/easylife/app.py
Restart=on-failure
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=15
# 加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/easylife
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo cp deploy/easylife.service /etc/systemd/system/easylife.service
sudo cp deploy/easylife.env.example /etc/easylife/easylife.env   # 再填入 EASYLIFE_SECRET 等
sudo systemctl daemon-reload
sudo systemctl enable --now easylife
sudo systemctl status easylife
journalctl -u easylife -f
```

不引入 nginx 之外的反向代理、不注册 cron（提醒由应用内线程完成，避免额外的系统级改动）。

### 8.3 nginx：仅新增 server 块（`deploy/nginx-easylife.conf`）

实际模板是 `deploy/nginx-easylife.conf`（含 `EASYLIFE_BLOCK` 标记的 443 与 80 两个互斥 `server` 块，
由 `deploy/deploy.sh` 按证书是否存在只安装其中一个；也可手工 `cp`）。等价的最小形式：

```nginx
# /etc/nginx/conf.d/easylife.conf  —— 新增文件，不触碰任何已有站点配置
# map / log_format 属于 http 上下文：放在 conf.d 文件顶层（server 块之外）即可生效。
map $request_uri $easylife_log_uri {          # 订阅 URL 中的 token 不进访问日志
    default      $request_uri;
    "~*(token=)[^&]+" "$1REDACTED";
}
log_format easylife_redacted '$remote_addr [$time_local] '
                             '"$request_method $easylife_log_uri $server_protocol" $status';

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name easylife.example.com;          # 可配置：换成自己的域名（子域方案）

    ssl_certificate     /etc/letsencrypt/live/easylife.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/easylife.example.com/privkey.pem;

    access_log /var/log/nginx/easylife-access.log easylife_redacted;

    location / {
        proxy_pass http://127.0.0.1:8086;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        client_max_body_size 1m;
    }

    location = /api/calendar.ics {
        access_log /var/log/nginx/easylife-ics.log easylife_redacted;
        proxy_pass http://127.0.0.1:8086;
        proxy_set_header Host $host;
    }
}
# 若采用路径方案（mch.wang 已被现有站点占用），**不新增**上面的 server 块，只在那个已有
# server 块内追加 location ^~ /easylife/（proxy_pass 末尾带 "/" 以剥掉前缀），见
# docs/DEPLOY.md §6 方案 B；两种方案互不冲突，也绝不覆盖/改动既有站点配置。
```

部署步骤与校验：

```bash
sudo cp deploy/nginx-easylife.conf /etc/nginx/conf.d/easylife.conf
sudo nginx -t          # 语法检查；通过才继续
sudo systemctl reload nginx
curl -I https://easylife.example.com/          # 200，静态首页
curl -s https://easylife.example.com/api/me    # 401 unauthorized（未登录）
```

安全边界与约束：

1. **只新增文件**：`/etc/nginx/conf.d/easylife.conf`（或 `/etc/nginx/sites-available/easylife` + 软链）。不编辑、不删除、不重排其他 `server` 块，也不修改全局 `nginx.conf`（除极少数情况需在新文件内自带所需指令）。
2. 应用仍只绑 `127.0.0.1:8086`，即便 nginx 出错，服务也不会直接暴露到公网；用 `ss -lntp | grep 8086` 验证只监听 `127.0.0.1`。
3. 端口冲突检查：安装前 `ss -lntp | grep :8086`，若被占用则改 `EASYLIFE_PORT`（仅改环境文件，不动系统）。
4. 备份与回滚：升级前 `tar czf /var/backups/easylife-$(date +%F).tgz /opt/easylife /var/lib/easylife/easylife.db`；回滚只需恢复目录并 `systemctl restart easylife`，nginx 侧删除新增的单个文件即可，对既有站点零影响。
5. 数据备份：`sqlite3 /var/lib/easylife/easylife.db ".backup '/var/backups/easylife-$(date +%F).db'"`（WAL 下 `.backup` 安全）。

### 8.4 测试与验收（无第三方依赖）

- `python3 -m unittest discover -s tests -v`（无 pytest，全部用标准库 `unittest`）；测试使用临时目录中的 SQLite（`tempfile`）与 `EASYLIFE_DB` 环境变量隔离，除 `127.0.0.1` 上的本地 mini SMTP 外不触网。
- 前端回归：`node --check easylife/static/app.js`（语法）与 `node tests/frontend_interaction.js`（用最小 DOM 桩加载真实 `app.js`，断言「列表 → 详情 → 删除确认 → 回到列表」等交互）；`tests/test_frontend_interaction.py` 在 Node 缺失时自动跳过。
- 测试覆盖：注册/重复邮箱 409、登录失败限速 429、会话过期 401、CSRF 缺失 403、跨用户访问 404、`TODO` 增删改查、笔记追加与级联删除、`.ics` 内容含 `BEGIN:VEVENT`/`UID`/`VALARM`、订阅 token 校验失败 403 / 缺失 400、`POST /api/calendar/token/reset` 使旧 URL 立即失效、更换 `EASYLIFE_SECRET` 后旧订阅 URL 仍可用（token 为随机列，见 6.3）、`GET /` 返回 200 且为 HTML、静态资源无外链且用户内容只用 `textContent` 渲染、`sqlite3.Row` 与字典皆可用于日历工具、提醒线程在未配置 SMTP 时「仅记录日志且不标记 `reminder_sent`，配置后补发一次」、非空 `BASE_URL` + 真实 `smtplib` 的生产路径确实投递且只投递一次（见 6.4）。
- 回归测试（针对曾被发现的问题，见 §6.3 与 §5）：
  1. `scan_due()` 返回的 `sqlite3.Row` + 非空 `BASE_URL` 走**真实** `send_message` 也必须发出提醒并带上任务链接（历史缺陷：`task_link()` 调 `row.get()` 导致 `AttributeError`，一封也发不出）；`calendar_ics._field()` 与 `reminders.build_message()` 的 `dict(row)` 是两层防线。
  2. 无效/被篡改的订阅 token → `403`（不是 404）；缺 token → `400`；轮换后旧 token → `403`。
  3. 订阅 token 为随机存储列：**更换 `EASYLIFE_SECRET` 后旧订阅 URL 仍然可用**，只有 `POST /api/calendar/token/reset` 才会让其失效；v1 旧库启动时自动迁移出 `users.calendar_token`。
- 手工验收清单：`ls -l docs/DESIGN.md`；`python3 -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" easylife/*.py`；`nginx -t`；`ss -lntp | grep 8086` 显示 `127.0.0.1:8086`。

### 8.5 路径前缀部署（`EASYLIFE_BASE_PATH`，如 `https://mch.wang/todos/`）

`EASYLIFE_BASE_PATH` 默认为空，行为与根路径部署完全一致；设为 `/todos` 后：

1. **两种反向代理方式都兼容**：
   - nginx 剥掉前缀后转发（`location ^~ /todos/ { proxy_pass http://127.0.0.1:8086/; }`）：应用收到 `/api/me`，原样路由；
   - nginx 原样带前缀转发（`proxy_pass http://127.0.0.1:8086;` 末尾无 `/`）：应用收到 `/todos/api/me`，先剥掉 `/todos` 再路由。
   只在剥掉后是应用自己的路径（`/`、`/api/*`、`/static/*`、`/todos/{id}`）时才剥，避免把剥前缀代理转来的任务链接 `/todos/5` 误剥成 `/5`。
2. **`/todos`（无结尾斜杠）→ `301 Location: /todos/`**（保留查询串），保证页面里的相对路径解析到 `/todos/` 之下。
3. **Cookie**：`Path=/todos`（未设置时 `Path=/`），不会与 `mch.wang` 上其它服务冲突。
4. **绝对链接**：`PUBLIC_URL`（或旧名 `BASE_URL`）末尾自动补齐前缀，邮件「任务链接」、`.ics` 的 `DESCRIPTION`、订阅 URL、Google 日历链接都形如 `https://mch.wang/todos/...`。
   任务链接 `{PUBLIC_URL}/todos/{id}` 由服务端 `302` 到 `{BASE_PATH}/#/todos/{id}`，前端登录后直接打开该任务。
5. **前端**：`index.html` 用 `./static/...`，`app.js` 用 `./api/...` 相对路径，不写死以 `/` 开头的路径；同一份静态文件在根路径和 `/todos/` 下都可用，无需运行时注入。

测试：`tests/test_base_path.py`（两种代理方式下的注册/登录/增删改查、Cookie Path、`/todos` 跳转、ics/邮件链接前缀、未设置时的根路径行为）。
