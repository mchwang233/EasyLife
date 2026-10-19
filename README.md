# EasyLife

极简的个人待办与提醒服务：网页管理任务、到点发邮件提醒，并能和 iPhone / Mac 的「提醒事项」双向同步。

- 后端只用 **Python 3.8+ 标准库**，前端是原生 HTML / CSS / JS，没有任何第三方依赖或外链 CDN。
- 数据存在一个 SQLite 文件里。

## 功能

| 模块 | 功能 |
| --- | --- |
| 账户 | 邮箱注册 / 登录、邮箱验证、找回密码；密码加盐哈希，登录失败限速，写操作带 CSRF 校验 |
| 任务 | 标题、详情、状态、优先级、截止时间；筛选、排序、进展笔记 |
| 重复 | 每天 / 工作日 / 周末 / 每周 / 每两周 / 每月 / 每 3、6 个月 / 每年；完成后自动排到下一次 |
| 标记 | 旗标（可单独筛选）、紧急（到点后每 10 分钟再提醒一次，直到完成或停止）、标签（点标签筛选） |
| 子任务 | 一层子任务，各自可设时间和提醒；删除任务时一并删除 |
| 图片 | 每个任务最多 9 张，上传前在浏览器里压缩并去掉位置信息 |
| 列表 | 多个任务列表，可改名、改颜色；网页和「提醒事项」里的列表互相同步 |
| 提醒 | 像 iPhone 一样：到截止时间发邮件，可另加一次「提前提醒」；SMTP 未配置时只写日志；管理员可在网页上设置发信邮箱 |
| 同步 | CalDAV：在 iPhone / Mac「提醒事项」里添加账户后双向同步任务、列表、提醒和重复（旗标、紧急、标签、子任务层级、图片只在网页上） |
| 日历 | 单个任务导出 `.ics`、一键加到 Google 日历、私有日历订阅链接 |

## 本地运行

```bash
EASYLIFE_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
EASYLIFE_DB=./easylife.db \
EASYLIFE_PORT=8086 \
python3 -m easylife.app
```

浏览器打开 <http://127.0.0.1:8086/> 注册即可使用。

- `EASYLIFE_SECRET` 必填，至少 16 个字符。
- 想模拟子路径部署，再加 `EASYLIFE_BASE_PATH=/todos`，然后访问 `http://127.0.0.1:8086/todos/`。
- 全部配置项见 [`deploy/easylife.env.example`](deploy/easylife.env.example)。

## 测试

```bash
python3 -m unittest discover -s tests -t .   # 后端与部署脚本测试
node tests/frontend_interaction.js           # 不开浏览器的前端交互检查
```

`tests/browser_*.py` 会启动真实的 Firefox 做界面检查并截图，需要本机装有 Firefox，按需手动运行。

## 部署

```bash
bash deploy/deploy-remote.sh <服务器IP> -i ~/.ssh/id_ed25519
```

脚本会打包上传、在服务器上安装为 systemd 服务，并把 nginx 配置挂到现有站点的 `/todos/` 路径下，最后自动做 4 项自检。
详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 目录

```
easylife/          程序代码
  app.py           服务入口与路由
  auth.py          注册、登录、会话
  todos.py         任务与笔记
  lists.py         任务列表
  caldav.py        「提醒事项」同步（CalDAV）
  reminders.py     邮件提醒
  calendar_ics.py  日历导出与订阅
  db.py            数据库建表与升级
  static/          网页前端
tests/             测试
deploy/            部署、备份脚本与服务器配置模板
docs/              设计文档 DESIGN.md、部署指南 DEPLOY.md
```
