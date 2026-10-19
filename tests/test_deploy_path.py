"""deploy/deploy.sh --mode path and deploy/deploy-remote.sh, run against fake system tools.

Fake nginx / systemctl / ss / ssh / scp / curl / id / install / chown are put first on
PATH; every path the script touches (/etc/nginx, /opt/easylife, /etc/easylife, ...) is
redirected into a temporary directory via the EASYLIFE_*_DIR overrides, so nothing on
this machine is modified and no root is needed.

升级预演（UpgradeRehearsalTest / BackupScriptTest）：用临时环境 + 命令桩验证
「旧版环境与旧库 -> 升级 -> 重复升级 -> 备份/恢复」，包括 SMTP_* / 会话密钥 /
数据库 / 网页邮件设置的保留、权限收紧、管理员名单自检，以及发布包的凭据排查。
"""

import gzip
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

from easylife import auth as easylife_auth
from easylife import db as easylife_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy", "deploy.sh")
DEPLOY_REMOTE = os.path.join(ROOT, "deploy", "deploy-remote.sh")
BACKUP = os.path.join(ROOT, "deploy", "backup.sh")
MARKER = "# managed-by-easylife-deploy"

# 升级预演里使用的合成占位符（不是任何真实密钥；证据里也会再脱敏）
FAKE_SMTP_CODE = "<FAKE_SMTP_AUTH_CODE_FOR_TESTS>"
OLD_SECRET = "old-secret-0123456789abcdef-keep-me"

FAKES = {
    # root for `id -u`, "user exists" for `id -u easylife`
    "id": """
        if [ "$#" -eq 1 ] && [ "$1" = "-u" ]; then echo 0; else echo 999; fi
    """,
    "chown": "exit 0",
    "useradd": "exit 0",
    # drop -o/-g (needs root) and hand the rest to the real install
    "install": """
        args=()
        while [ $# -gt 0 ]; do
            case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
        done
        exec /usr/bin/install "${args[@]}"
    """,
    "nginx": """
        echo "nginx $*" >> "$FAKE_STATE/calls"
        case "$1" in
          -t)
            if [ -e "$FAKE_STATE/nginx_t_fail" ]; then
                echo "nginx: [emerg] unexpected \\"}\\" (fake failure)" >&2; exit 1
            fi
            echo "nginx: configuration file test is successful" >&2; exit 0 ;;
          -T)
            [ -e "$FAKE_STATE/nginx_T_fail" ] && exit 1
            d="$EASYLIFE_NGINX_DIR"
            for f in "$d/nginx.conf" "$d"/conf.d/*.conf "$d"/sites-enabled/* "$d"/easylife/*.conf; do
                [ -f "$f" ] || continue
                echo "# configuration file $f:"; cat "$f"; echo
            done
            exit 0 ;;
          -s) exit 0 ;;
        esac
    """,
    "systemctl": """
        echo "systemctl $*" >> "$FAKE_STATE/calls"
        if [ "$1" = "is-active" ] || [ "$1" = "is-enabled" ]; then
            for a in "$@"; do last="$a"; done
            if [ "$last" = "nginx" ] || [ -e "$FAKE_STATE/$last.active" ]; then
                if [ "$1" = "is-active" ]; then echo active; else echo enabled; fi
                exit 0
            fi
            if [ "$1" = "is-active" ]; then echo inactive; else echo disabled; fi
            exit 3
        fi
        # a started/restarted easylife listens on EASYLIFE_PORT read from the env file *at that moment*
        listen() {
            port="$(sed -n 's/^EASYLIFE_PORT=//p' "$EASYLIFE_CONF_DIR/easylife.env" 2>/dev/null)"
            echo "LISTEN 0 128 127.0.0.1:${port:-8086} 0.0.0.0:* users:((\\"python3\\",pid=4242,fd=3))" \\
                > "$FAKE_STATE/$1.listen"
        }
        case "$1 $2" in
          "enable --now")
            [ -e "$FAKE_STATE/$3.active" ] || { touch "$FAKE_STATE/$3.active"; listen "$3"; } ;;
          "disable --now") rm -f "$FAKE_STATE/$3.active" "$FAKE_STATE/$3.listen" ;;
          "restart "*) touch "$FAKE_STATE/$2.active"; listen "$2" ;;
        esac
        exit 0
    """,
    "ss": """
        echo "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process"
        [ -f "$FAKE_STATE/ss_output" ] && cat "$FAKE_STATE/ss_output"
        cat "$FAKE_STATE"/*.listen 2>/dev/null
        exit 0
    """,
    "sudo": 'exec "$@"',
    "curl": """
        url=""
        for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
        resolve=0
        for a in "$@"; do [ "$a" = "--resolve" ] && resolve=1; done
        echo "curl $url resolve=$resolve" >> "$FAKE_STATE/calls"
        # 测试可以用 curl_code 文件强制某个状态码（例如统一 502）
        [ -f "$FAKE_STATE/curl_code" ] && { cat "$FAKE_STATE/curl_code"; exit 0; }
        # 没有 -o /dev/null 的调用 = 取页面正文（自检第 4 项：新界面标识）
        tonull=0
        for a in "$@"; do [ "$a" = "/dev/null" ] && tonull=1; done
        if [ "$tonull" = 0 ]; then
            printf '<!doctype html><html><body><div id="verification-banner" class="verification-banner"></div><section id="view-check-email"></section></body></html>'
            exit 0
        fi
        case "$url" in
          */api/me|*/api/todos) printf 401 ;;
          http://mch.wang/*) printf 301 ;;
          */todos) printf 301 ;;
          *) printf 200 ;;
        esac
    """,
    # 假的 ssh 不只是"成功退出"：它把远端命令在本机真跑一遍（假工具都在 PATH 里，
    # 路径都被 EASYLIFE_* 覆盖到临时目录），因此 --check / 部署 / 部署后自检都是端到端验证。
    "ssh": """
        echo "ssh $*" >> "$FAKE_STATE/calls"
        cmd=()
        want_val=0
        host_seen=0
        for a in "$@"; do
            if [ "$host_seen" -eq 1 ]; then cmd+=("$a"); continue; fi
            if [ "$want_val" -eq 1 ]; then want_val=0; continue; fi
            case "$a" in
              -O) exit 0 ;;                       # ControlMaster 的 "关闭主连接"：直接成功
              -o|-i|-p|-l|-F|-b|-c|-E|-e|-m|-J|-L|-R|-D|-W) want_val=1 ;;
              -*) ;;
              *) host_seen=1 ;;
            esac
        done
        [ "${#cmd[@]}" -gt 0 ] || exit 0
        bash -c "${cmd[*]}"
    """,
    "scp": """
        echo "scp $*" >> "$FAKE_STATE/calls"
        src=""; dst=""
        for a in "$@"; do
            case "$a" in -*) ;; *:*) dst="$a" ;; *) src="$a" ;; esac
        done
        [ -n "$src" ] && [ -f "$src" ] || exit 1
        cp "$src" "$FAKE_STATE/uploaded.tar.gz"
        [ -n "$dst" ] || exit 0
        target="${dst#*:}"                       # user@host:/path -> /path，模拟真的传到"服务器"上
        mkdir -p "$(dirname "$target")"
        cp "$src" "$target"
        exit 0
    """,
    "sshpass": """
        echo "sshpass $1 env_set=${SSHPASS:+yes}" >> "$FAKE_STATE/calls"
        shift
        exec "$@"
    """,
}

SITE_80 = """\
server {
    listen 80;
    server_name mch.wang www.mch.wang;
    return 301 https://$host$request_uri;
}
"""

SITE_443 = """\
server {
    listen 443 ssl http2;
    server_name mch.wang;
    ssl_certificate     /etc/letsencrypt/live/mch.wang/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mch.wang/privkey.pem;
    root /var/www/mch.wang;

    location / {
        try_files $uri $uri/ =404;
    }
    location ~* \\.(js|css|png)$ {
        expires 7d;
    }
%s}"""   # no trailing newline on purpose: bytes must survive insert + rollback

OTHER_SITE = """\
server {
    listen 80;
    server_name blog.example.com;
    location /todos/ {
        proxy_pass http://127.0.0.1:9000/;
    }
}
"""

API_SITE = """\
server {
    listen 80;
    server_name api.mch.wang;
    location / { proxy_pass http://127.0.0.1:7000; }
}
"""


def seed_old_db(path, version=2, todos=2, email="admin@example.com",
                password="correct horse battery staple"):
    """用仓库自己的 MIGRATIONS 造一个「旧版」数据库，写入一个用户和若干待办。

    version=2 模拟本功能上线前部署的库；部署脚本升级时不应写它（迁移由应用启动时执行）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statements in easylife_db.MIGRATIONS[:version]:
            for statement in statements:
                if callable(statement):
                    statement(conn)
                else:
                    conn.execute(statement)
        conn.execute("PRAGMA user_version = %d" % version)
        now = easylife_db.now_utc()
        conn.execute("INSERT INTO users (id, email, password_hash, created_at) VALUES (1, ?, ?, ?)",
                     (email, easylife_auth.hash_password(password), now))
        for index in range(todos):
            conn.execute("INSERT INTO todos (user_id, title, status, priority, created_at, updated_at) "
                         "VALUES (1, ?, 'open', 2, ?, ?)", ("旧任务 %d" % index, now, now))
        conn.commit()
    finally:
        conn.close()
    return path


MAIL_VALUE = ('{"enabled": true, "host": "smtp.saved.example", "port": 465, '
              '"user": "ops@example.com", "password": "<FAKE_WEB_MAIL_CODE>", "from": "ops@example.com"}')


def add_mail_settings(path, value=MAIL_VALUE):
    """模拟管理员在网页里保存过的邮件设置（site_settings 行；密码为合成占位符）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO site_settings (key, value, updated_at, updated_by) "
                     "VALUES ('mail.smtp', ?, ?, 1) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (value, easylife_db.now_utc()))
        conn.commit()
    finally:
        conn.close()


def release_violations(names):
    """发布包里不允许出现的运行时凭据 / 浏览器测试资料（与 deploy-remote.sh 的规则一致）。"""
    bad = []
    for name in names:
        base = os.path.basename(name)
        if name.startswith("docs/verification"):
            bad.append(name)
        elif name.endswith(".env") or name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
                                                     ".pem", ".key", ".har", ".log", ".parentlock",
                                                     ".bak", ".orig")):
            bad.append(name)
        elif "cookies" in name or "ffprofile" in name or base.startswith("id_"):
            bad.append(name)
    return bad


def snapshot(top):
    """{relative path: ('dir',) | ('link', target) | ('file', bytes, mode)} for a whole tree."""
    out = {}
    if not os.path.isdir(top):
        return out
    for base, dirs, files in os.walk(top):
        for name in dirs + files:
            p = os.path.join(base, name)
            rel = os.path.relpath(p, top)
            if os.path.islink(p):
                out[rel] = ("link", os.readlink(p))
            elif os.path.isdir(p):
                out[rel] = ("dir",)
            else:
                with open(p, "rb") as fh:
                    out[rel] = ("file", fh.read(), stat.S_IMODE(os.stat(p).st_mode))
    return out


class DeployHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="easylife-deploy-test.")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state")
        self.nginx = os.path.join(self.tmp, "etc", "nginx")
        self.conf_dir = os.path.join(self.tmp, "etc", "easylife")
        self.backup_dir = os.path.join(self.tmp, "backups")
        self.app_dir = os.path.join(self.tmp, "opt", "easylife")
        self.remote_dir = os.path.join(self.tmp, "opt", "easylife-src")
        self.unit = os.path.join(self.tmp, "systemd", "easylife.service")
        for d in (self.bin, self.state, os.path.dirname(self.unit)):
            os.makedirs(d)
        for name, body in FAKES.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/bash\n" + textwrap.dedent(body).strip() + "\n")
            os.chmod(path, 0o755)

    # ---- fixtures -------------------------------------------------------
    def write(self, rel, text, mode=0o644):
        path = os.path.join(self.nginx, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, mode)
        return path

    def make_sites(self, extra_443=""):
        self.write("nginx.conf", "events {}\nhttp {\n    include %s/conf.d/*.conf;\n"
                   "    include %s/sites-enabled/*;\n}\n" % (self.nginx, self.nginx))
        self.site = self.write("sites-available/mch.wang", SITE_80 + "\n" + SITE_443 % extra_443)
        os.makedirs(os.path.join(self.nginx, "sites-enabled"), exist_ok=True)
        os.symlink(self.site, os.path.join(self.nginx, "sites-enabled", "mch.wang"))
        self.write("sites-enabled/blog", OTHER_SITE)
        self.write("conf.d/api.conf", API_SITE)

    # ---- running --------------------------------------------------------
    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
            "FAKE_STATE": self.state,
            "TMPDIR": self.tmp,
            "EASYLIFE_APP_DIR": self.app_dir,
            "EASYLIFE_REMOTE_DIR": self.remote_dir,
            "EASYLIFE_DATA_DIR": os.path.join(self.tmp, "var", "lib", "easylife"),
            "EASYLIFE_CONF_DIR": self.conf_dir,
            "EASYLIFE_UNIT_FILE": self.unit,
            "EASYLIFE_NGINX_DIR": self.nginx,
            "EASYLIFE_BACKUP_DIR": self.backup_dir,
            "EASYLIFE_DB_BACKUP_DIR": os.path.join(self.tmp, "db-backups"),
            "LETSENCRYPT_DIR": os.path.join(self.tmp, "letsencrypt"),
        })
        env.pop("SSHPASS", None)
        env.pop("EASYLIFE_SMTP_PASS", None)
        env.update(extra)
        return env

    def run_script(self, script, *args, **env):
        proc = subprocess.run(["bash", script] + list(args), env=self.env(**env),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.last_out = proc.stdout.decode("utf-8", "replace")
        self.last_rc = proc.returncode
        return proc.returncode, self.last_out

    def deploy(self, *args, **env):
        return self.run_script(DEPLOY, *args, **env)

    def calls(self):
        path = os.path.join(self.state, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def reset_calls(self):
        path = os.path.join(self.state, "calls")
        if os.path.exists(path):
            os.remove(path)

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def env_file(self):
        with open(os.path.join(self.conf_dir, "easylife.env")) as fh:
            return fh.read().splitlines()

    def backups(self):
        return sorted(os.listdir(self.backup_dir)) if os.path.isdir(self.backup_dir) else []

    def reloads(self):
        return [c for c in self.calls() if c in ("systemctl reload nginx", "nginx -s reload")]


class PathModeTest(DeployHarness):
    def assert_ok(self, rc, out):
        self.assertEqual(rc, 0, out)

    def test_inserts_include_in_80_and_443_blocks_and_keeps_backup(self):
        self.make_sites()
        before = snapshot(self.nginx)
        original = self.read(self.site)

        rc, out = self.deploy()
        self.assert_ok(rc, out)

        text = self.read(self.site).decode()
        lines = text.split("\n")
        include = "    include %s/easylife/todos-location.conf; %s" % (self.nginx, MARKER)
        self.assertEqual(lines.count(include), 2, text)
        # each include sits directly after a "server {" line, i.e. inside the block
        for i, line in enumerate(lines):
            if line == include:
                self.assertEqual(lines[i - 1], "server {")
        # removing the two lines gives the original bytes back (nothing else touched)
        self.assertEqual("\n".join(l for l in lines if l != include).encode(), original)
        self.assertTrue(os.path.islink(os.path.join(self.nginx, "sites-enabled", "mch.wang")))

        # timestamped backup equal to the original file
        baks = self.backups()
        self.assertEqual(len(baks), 1, baks)
        self.assertRegex(baks[0], r"mch\.wang\.\d{8}-\d{6}\.bak$")
        self.assertEqual(self.read(os.path.join(self.backup_dir, baks[0])), original)

        # other server blocks / files are byte-identical
        after = snapshot(self.nginx)
        for rel in ("nginx.conf", "sites-enabled/blog", "conf.d/api.conf", "sites-enabled/mch.wang"):
            self.assertEqual(after[rel], before[rel], rel)

        snippet = self.read(os.path.join(self.nginx, "easylife", "todos-location.conf")).decode()
        self.assertIn("location = /todos {", snippet)
        self.assertIn("return 301 /todos/;", snippet)
        self.assertIn("location ^~ /todos/ {", snippet)
        self.assertIn("proxy_pass            http://127.0.0.1:8086/;", snippet)
        for header in ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "Host"):
            self.assertIn("proxy_set_header      %s" % header, snippet)
        self.assertIn("client_max_body_size", snippet)
        self.assertIn("proxy_read_timeout", snippet)

        env = self.env_file()
        self.assertIn("EASYLIFE_BASE_PATH=/todos", env)
        self.assertIn("PUBLIC_URL=https://mch.wang/todos", env)
        self.assertIn("EASYLIFE_HOST=127.0.0.1", env)
        self.assertIn("EASYLIFE_PORT=8086", env)
        self.assertIn("SMTP_HOST=", env)              # placeholder account -> log-only mode
        secret = [l for l in env if l.startswith("EASYLIFE_SECRET=")][0]
        self.assertGreaterEqual(len(secret), len("EASYLIFE_SECRET=") + 16)

        calls = self.calls()
        self.assertIn("nginx -t", calls)
        self.assertEqual(len(self.reloads()), 1, calls)
        self.assertIn("systemctl enable --now easylife", calls)
        with open(self.unit) as fh:
            self.assertIn("User=easylife", fh.read())

    def test_second_run_is_idempotent(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        first = snapshot(self.nginx)
        baks = self.backups()
        env_first = self.env_file()
        self.reset_calls()

        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.assertEqual(snapshot(self.nginx), first)
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)
        self.assertEqual(self.backups(), baks)
        self.assertEqual(self.reloads(), [])
        self.assertEqual(self.env_file(), env_first)     # secret not regenerated
        self.assertIn("幂等", out)

    def test_nginx_t_failure_restores_original_bytes(self):
        self.make_sites()
        before = snapshot(self.nginx)
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()

        rc, out = self.deploy()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("nginx -t 失败", out)
        self.assertEqual(snapshot(self.nginx), before)          # byte-for-byte, snippet removed
        self.assertFalse(os.path.exists(os.path.join(self.nginx, "easylife")))
        self.assertEqual(self.reloads(), [])

    def test_nginx_t_failure_on_rerun_restores_previous_snippet(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        before = snapshot(self.nginx)
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()

        rc, out = self.deploy("--port", "8090")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(snapshot(self.nginx), before)

    def test_nginx_t_failure_on_port_change_rolls_back_env_and_service(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        nginx_before = snapshot(self.nginx)
        env_path = os.path.join(self.conf_dir, "easylife.env")
        env_before = self.read(env_path)
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()
        self.reset_calls()

        rc, out = self.deploy("--port", "8090")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("nginx -t 失败", out)
        self.assertEqual(snapshot(self.nginx), nginx_before)
        self.assertEqual(self.read(env_path), env_before)
        ss = subprocess.run(["ss", "-lntp"], env=self.env(), stdout=subprocess.PIPE,
                            timeout=30).stdout.decode()
        self.assertIn("127.0.0.1:8086", ss)
        self.assertNotIn("127.0.0.1:8090", ss)
        calls = self.calls()
        self.assertEqual(calls.count("systemctl restart easylife"), 2, calls)   # to 8090, then back
        self.assertNotIn("systemctl disable --now easylife", calls)
        self.assertEqual(self.reloads(), [])
        self.assertFalse([f for f in os.listdir(self.tmp) if f.startswith("easylife-env-prev.")])

    def test_nginx_t_failure_on_first_deploy_stops_service_and_removes_env(self):
        self.make_sites()
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()
        rc, out = self.deploy()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("systemctl disable --now easylife", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.state, "easylife.active")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "easylife.listen")))
        self.assertFalse(os.path.exists(os.path.join(self.conf_dir, "easylife.env")))

    def test_refuses_multiline_todos_location_in_block_and_include(self):
        multiline = "    location\n        /todos/ {\n        return 404;\n    }\n"
        cases = [
            ("server block", multiline, None),
            ("included file", "    include snippets/multi.conf;\n",
             "# location /todos/ in a comment is ignored\nlocation\n  /todos/\n{\n    return 410;\n}\n"),
        ]
        for name, extra, inc_body in cases:
            with self.subTest(where=name):
                shutil.rmtree(self.nginx, True)
                shutil.rmtree(self.backup_dir, True)
                self.reset_calls()
                self.make_sites(extra_443=extra)
                if inc_body is not None:
                    self.write("snippets/multi.conf", inc_body)
                before = snapshot(self.nginx)

                rc, out = self.deploy()
                self.assertNotEqual(rc, 0, out)
                self.assertIn("拒绝覆盖", out)
                self.assertIn("location /todos/ {", out)
                self.assertEqual(snapshot(self.nginx), before)
                self.assertEqual(self.backups(), [])
                self.assertNotIn("systemctl enable --now easylife", self.calls())

    def test_refuses_when_site_already_has_todos_location(self):
        for loc in ("    location /todos/ {\n        proxy_pass http://127.0.0.1:3000/;\n    }\n",
                    "    location = /todos {\n        return 404;\n    }\n",
                    "    location ^~ /todos {\n        root /srv;\n    }\n"):
            with self.subTest(loc=loc.split("{")[0].strip()):
                shutil.rmtree(self.nginx, True)
                self.reset_calls()
                self.make_sites(extra_443=loc)
                before = snapshot(self.nginx)

                rc, out = self.deploy()
                self.assertNotEqual(rc, 0, out)
                self.assertIn("拒绝覆盖", out)
                self.assertEqual(snapshot(self.nginx), before)
                self.assertEqual(self.backups(), [])
                self.assertNotIn("systemctl enable --now easylife", self.calls())

    def test_other_site_with_todos_is_not_a_conflict(self):
        self.make_sites()       # blog.example.com has location /todos/ — a different server
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.assertNotIn(MARKER, self.read(os.path.join(self.nginx, "sites-enabled", "blog")).decode())

    def test_missing_site_block_suggests_subdomain_mode(self):
        self.write("nginx.conf", "events {}\nhttp {}\n")
        self.write("conf.d/api.conf", API_SITE)       # api.mch.wang must not count as mch.wang
        before = snapshot(self.nginx)

        rc, out = self.deploy()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("找不到 server_name 含 mch.wang", out)
        self.assertIn("--mode subdomain", out)
        self.assertEqual(snapshot(self.nginx), before)

    def test_falls_back_to_grep_when_nginx_T_fails(self):
        self.make_sites()
        open(os.path.join(self.state, "nginx_T_fail"), "w").close()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)

    def test_rollback_restores_pre_deploy_state(self):
        self.make_sites()
        before = snapshot(self.nginx)
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.assertNotEqual(snapshot(self.nginx), before)
        self.reset_calls()

        rc, out = self.deploy("--rollback")
        self.assert_ok(rc, out)
        self.assertEqual(snapshot(self.nginx), before)
        self.assertIn("systemctl disable --now easylife", self.calls())
        self.assertEqual(len(self.reloads()), 1)
        self.assertFalse(os.path.exists(self.unit))
        self.assertFalse(os.path.exists(os.path.join(self.state, "easylife.active")))
        self.assertFalse(os.path.exists(os.path.join(self.conf_dir, "nginx-path.manifest")))
        # data / secrets kept
        self.assertTrue(os.path.exists(os.path.join(self.conf_dir, "easylife.env")))

        # deploy again after rollback works and rollback again returns to the same state
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        rc, out = self.deploy("--rollback")
        self.assert_ok(rc, out)
        self.assertEqual(snapshot(self.nginx), before)

    def test_rollback_keeps_unrelated_later_edits(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        with open(self.site, "a") as fh:
            fh.write("\n# added by admin later\n")
        rc, out = self.deploy("--rollback")
        self.assert_ok(rc, out)
        text = self.read(self.site).decode()
        self.assertNotIn(MARKER, text)
        self.assertIn("# added by admin later", text)

    def test_rollback_reverts_itself_when_nginx_t_fails(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        deployed = snapshot(self.nginx)
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()
        self.reset_calls()

        rc, out = self.deploy("--rollback")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(snapshot(self.nginx), deployed)
        self.assertNotIn("systemctl disable --now easylife", self.calls())

    def test_busy_port_is_refused_then_port_option_updates_env_and_snippet(self):
        self.make_sites()
        with open(os.path.join(self.state, "ss_output"), "w") as fh:
            fh.write('LISTEN 0 128 127.0.0.1:8086 0.0.0.0:* users:(("java",pid=42,fd=7))\n')
        before = snapshot(self.nginx)

        rc, out = self.deploy()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("8086 已被占用", out)
        self.assertIn("--port", out)
        self.assertEqual(snapshot(self.nginx), before)

        rc, out = self.deploy("--port", "8090")
        self.assert_ok(rc, out)
        snippet = self.read(os.path.join(self.nginx, "easylife", "todos-location.conf")).decode()
        self.assertIn("http://127.0.0.1:8090/;", snippet)
        code = [l for l in snippet.splitlines() if not l.lstrip().startswith("#")]
        self.assertFalse([l for l in code if "8086" in l], code)
        self.assertIn("EASYLIFE_PORT=8090", self.env_file())

    def test_port_change_restarts_running_service_before_nginx_points_at_it(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.reset_calls()

        rc, out = self.deploy("--port", "8090")
        self.assert_ok(rc, out)
        calls = self.calls()
        self.assertIn("systemctl restart easylife", calls)
        # restart (and the ss check that follows it) happen before nginx is reloaded with the new port
        self.assertLess(calls.index("systemctl restart easylife"), calls.index("systemctl reload nginx"))
        with open(os.path.join(self.state, "easylife.listen")) as fh:
            self.assertIn("127.0.0.1:8090", fh.read())
        self.assertIn("已确认 127.0.0.1:8090 在监听", out)
        snippet = self.read(os.path.join(self.nginx, "easylife", "todos-location.conf")).decode()
        self.assertIn("http://127.0.0.1:8090/;", snippet)
        self.assertIn("EASYLIFE_PORT=8090", self.env_file())

    def test_port_not_listening_after_restart_leaves_nginx_untouched(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        before = snapshot(self.nginx)
        # a service that keeps listening on the old port no matter what
        sysctl = os.path.join(self.bin, "systemctl")
        with open(sysctl) as fh:
            body = fh.read().replace('"restart "*) touch "$FAKE_STATE/$2.active"; listen "$2" ;;',
                                     '"restart "*) ;;')
        with open(sysctl, "w") as fh:
            fh.write(body)
        self.reset_calls()

        rc, out = self.deploy("--port", "8090", EASYLIFE_LISTEN_WAIT="1")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("没有在 127.0.0.1:8090 上监听", out)
        self.assertEqual(snapshot(self.nginx), before)
        self.assertEqual(self.reloads(), [])

    def test_refuses_when_included_file_has_todos_location(self):
        cases = [
            # relative include (based on the nginx dir) with a wildcard
            ("    include snippets/legacy-*.conf;\n", "snippets/legacy-app.conf",
             "location /todos/ {\n    proxy_pass http://127.0.0.1:3000/;\n}\n"),
            # absolute, quoted include; regex location
            ('    include "%s/extra/apps.conf";\n', "extra/apps.conf",
             "location ~ ^/todos/(api|static) {\n    return 404;\n}\n"),
            # nested: server includes a file that includes the conflicting one
            ("    include snippets/outer.conf;\n", "snippets/outer.conf",
             "include snippets/inner.conf;\n"),
        ]
        for inc, rel, body in cases:
            with self.subTest(include=inc.strip()):
                shutil.rmtree(self.nginx, True)
                shutil.rmtree(self.backup_dir, True)
                self.reset_calls()
                self.make_sites(extra_443=inc.replace("%s", self.nginx))
                self.write(rel, body)
                if rel == "snippets/outer.conf":
                    self.write("snippets/inner.conf", "location = /todos { return 410; }\n")
                before = snapshot(self.nginx)

                rc, out = self.deploy()
                self.assertNotEqual(rc, 0, out)
                self.assertIn("拒绝覆盖", out)
                self.assertIn("include", out)
                self.assertEqual(snapshot(self.nginx), before)
                self.assertEqual(self.backups(), [])
                self.assertNotIn("systemctl enable --now easylife", self.calls())

    def test_included_file_without_todos_is_not_a_conflict(self):
        self.make_sites(extra_443="    include snippets/ssl-params.conf;\n")
        self.write("snippets/ssl-params.conf", "ssl_protocols TLSv1.2 TLSv1.3;\nlocation /todo-old/ { return 404; }\n")
        rc, out = self.deploy()
        self.assert_ok(rc, out)
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)
        # rerun: our own snippet (now included in the block) must not count as a conflict
        rc, out = self.deploy()
        self.assert_ok(rc, out)

    def test_optional_smtp_settings_without_password_on_command_line(self):
        self.make_sites()
        rc, out = self.deploy("--smtp-host", "smtp.qq.com", "--smtp-user", "me@qq.com",
                              EASYLIFE_SMTP_PASS="auth-code-123")
        self.assert_ok(rc, out)
        env = self.env_file()
        self.assertIn("SMTP_HOST=smtp.qq.com", env)
        self.assertIn("SMTP_USER=me@qq.com", env)
        self.assertIn("SMTP_PASS=auth-code-123", env)
        self.assertNotIn("auth-code-123", out)


class SubdomainModeTest(DeployHarness):
    def test_subdomain_mode_still_adds_its_own_server_block(self):
        self.make_sites()
        site_before = self.read(self.site)

        for args in (["--mode", "subdomain", "--domain", "easylife.mch.wang"],
                     ["--domain", "easylife.mch.wang"]):        # legacy command line
            with self.subTest(args=args):
                conf = os.path.join(self.nginx, "conf.d", "easylife.conf")
                if os.path.exists(conf):
                    os.remove(conf)
                env_path = os.path.join(self.conf_dir, "easylife.env")
                if os.path.exists(env_path):
                    os.remove(env_path)
                rc, out = self.deploy(*args)
                self.assertEqual(rc, 0, out)
                with open(conf) as fh:
                    text = fh.read()
                self.assertIn("server_name easylife.mch.wang;", text)
                self.assertEqual(self.read(self.site), site_before)
                self.assertFalse(os.path.exists(os.path.join(self.nginx, "easylife")))
                env = self.env_file()
                self.assertIn("PUBLIC_URL=https://easylife.mch.wang", env)
                self.assertIn("EASYLIFE_BASE_PATH=", env)

        rc, out = self.deploy("--rollback")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.nginx, "conf.d", "easylife.conf")))
        self.assertEqual(self.read(self.site), site_before)


    def test_rollback_keeps_pre_existing_easylife_conf(self):
        self.make_sites()
        # someone else's files that merely share the name — they existed before any deploy
        self.write("conf.d/easylife.conf", "server {\n    listen 80;\n    server_name old-easylife.example;\n}\n")
        self.write("sites-enabled/easylife.conf", "# unrelated hand-written site\n")
        before = snapshot(self.nginx)

        for args in (["--mode", "subdomain", "--domain", "easylife.mch.wang"], []):   # subdomain, then path
            with self.subTest(args=args):
                rc, out = self.deploy(*args)
                self.assertEqual(rc, 0, out)
                rc, out = self.deploy("--rollback")
                self.assertEqual(rc, 0, out)
                self.assertEqual(snapshot(self.nginx), before)
                self.assertIn("不是本脚本创建的", out)

    def test_rollback_removes_only_what_it_created(self):
        self.make_sites()
        before = snapshot(self.nginx)
        rc, out = self.deploy("--mode", "subdomain", "--domain", "easylife.mch.wang")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.conf_dir, "nginx-subdomain.manifest")) as fh:
            self.assertEqual(fh.read().splitlines(), [os.path.join(self.nginx, "conf.d", "easylife.conf")])
        rc, out = self.deploy("--rollback")
        self.assertEqual(rc, 0, out)
        self.assertEqual(snapshot(self.nginx), before)

    SUB = ("--mode", "subdomain", "--domain", "easylife.mch.wang")

    def listening(self):
        return subprocess.run(["ss", "-lntp"], env=self.env(), stdout=subprocess.PIPE,
                              timeout=30).stdout.decode()

    def test_port_change_with_failing_nginx_t_restores_env_and_service(self):
        self.make_sites()
        rc, out = self.deploy(*self.SUB)
        self.assertEqual(rc, 0, out)
        self.assertIn("127.0.0.1:8086", self.listening())
        nginx_before = snapshot(self.nginx)
        env_path = os.path.join(self.conf_dir, "easylife.env")
        env_before = self.read(env_path)
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()
        self.reset_calls()

        rc, out = self.deploy(*self.SUB, "--port", "8090")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("nginx -t 失败", out)
        self.assertEqual(snapshot(self.nginx), nginx_before)
        self.assertEqual(self.read(env_path), env_before)
        ss = self.listening()
        self.assertIn("127.0.0.1:8086", ss)
        self.assertNotIn("127.0.0.1:8090", ss)
        self.assertEqual(self.reloads(), [])
        self.assertNotIn("systemctl disable --now easylife", self.calls())
        self.assertFalse([f for f in os.listdir(self.tmp) if f.startswith("easylife-env-prev.")])

    def test_port_change_with_passing_nginx_t_updates_proxy_pass(self):
        self.make_sites()
        rc, out = self.deploy(*self.SUB)
        self.assertEqual(rc, 0, out)
        conf = os.path.join(self.nginx, "conf.d", "easylife.conf")
        conf_before = self.read(conf).decode()
        self.reset_calls()

        rc, out = self.deploy(*self.SUB, "--port", "8090")
        self.assertEqual(rc, 0, out)
        conf_after = self.read(conf).decode()
        self.assertNotIn("proxy_pass         http://127.0.0.1:8086", conf_after)
        self.assertIn("proxy_pass         http://127.0.0.1:8090;", conf_after)
        # only the (uncommented) proxy_pass lines change
        self.assertEqual(conf_after, conf_before.replace("proxy_pass         http://127.0.0.1:8086",
                                                         "proxy_pass         http://127.0.0.1:8090"))
        self.assertIn("EASYLIFE_PORT=8090", self.env_file())
        ss = self.listening()
        self.assertIn("127.0.0.1:8090", ss)
        self.assertNotIn("127.0.0.1:8086", ss)
        self.assertEqual(len(self.reloads()), 1, self.calls())
        baks = self.backups()
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(self.read(os.path.join(self.backup_dir, baks[0])).decode(), conf_before)

    def test_port_change_refused_for_conf_not_created_by_script(self):
        self.make_sites()
        rc, out = self.deploy(*self.SUB)
        self.assertEqual(rc, 0, out)
        os.remove(os.path.join(self.conf_dir, "nginx-subdomain.manifest"))   # now "someone else's"
        nginx_before = snapshot(self.nginx)
        env_path = os.path.join(self.conf_dir, "easylife.env")
        env_before = self.read(env_path)
        self.reset_calls()

        rc, out = self.deploy(*self.SUB, "--port", "8090")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("不是本脚本创建的", out)
        self.assertIn("proxy_pass", out)
        self.assertEqual(snapshot(self.nginx), nginx_before)
        self.assertEqual(self.read(env_path), env_before)
        ss = self.listening()
        self.assertIn("127.0.0.1:8086", ss)
        self.assertNotIn("127.0.0.1:8090", ss)
        self.assertEqual(self.reloads(), [])


class DeployRemoteTest(DeployHarness):
    """deploy/deploy-remote.sh：打包 -> 上传 -> 远程部署 -> 部署后自检 / --check / --rollback。

    假 ssh 会把「远端」命令在本机真跑一遍（假工具都在 PATH 里，所有目标路径都被
    EASYLIFE_* 重定向到临时目录），所以下面验证的是端到端行为，而不是「命令字符串长什么样」。
    """

    HOST = "203.0.113.7"

    def remote(self, *args, **env):
        return self.run_script(DEPLOY_REMOTE, self.HOST, *args, **env)

    def ssh_calls(self, needle=""):
        return [c for c in self.calls() if c.startswith("ssh ") and needle in c]

    def curl_calls(self):
        # 只看发往 mch.wang 的自检请求（deploy.sh 自己也会 curl 127.0.0.1 的后端做验证）
        return [c for c in self.calls() if c.startswith("curl ") and "mch.wang" in c]

    def force_curl_code(self, code):
        with open(os.path.join(self.state, "curl_code"), "w") as fh:
            fh.write("%s" % code)

    def tar_names(self):
        with tarfile.open(os.path.join(self.state, "uploaded.tar.gz")) as tar:
            return tar.getnames()

    def stage_source_tree(self):
        """复制一份源码树到临时目录，并植入「绝不该进发布包」的凭据/浏览器资料。"""
        src = os.path.join(self.tmp, "src")
        shutil.copytree(os.path.join(ROOT, "easylife"), os.path.join(src, "easylife"))
        shutil.copytree(os.path.join(ROOT, "deploy"), os.path.join(src, "deploy"))
        docs = os.path.join(src, "docs")
        os.makedirs(docs)
        for name in ("DEPLOY.md", "DESIGN.md"):
            shutil.copy2(os.path.join(ROOT, "docs", name), os.path.join(docs, name))
        plants = {
            "deploy/server-easylife.env": "SMTP_PASS=never-ship-this\n",
            "deploy/id_ed25519": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
            "deploy/notes.log": "deploy run log\n",
            "easylife/preview.db": "not-really-sqlite",
            "easylife/cookies.sqlite": "cookie-jar",
            "easylife/ffprofile-auth/cookies.sqlite": "cookie-jar",
            "docs/verification/mobile-mail/ui/browser.log": "browser log\n",
            "docs/verification/mobile-mail/ui/shot.png": b"\x89PNG fake",
            "docs/session.har": "{}",
        }
        for rel, data in plants.items():
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data.encode() if isinstance(data, str) else data)
        return src

    def test_release_package_excludes_runtime_credentials_and_browser_material(self):
        self.make_sites()
        src = self.stage_source_tree()

        rc, out = self.remote("--no-smoke", EASYLIFE_SRC_DIR=src,
                              EASYLIFE_APP_DIR=os.path.join(self.tmp, "staged-app"),
                              EASYLIFE_REMOTE_DIR=os.path.join(self.tmp, "staged-remote"))
        self.assertEqual(rc, 0, out)
        names = self.tar_names()

        # 植入的运行时凭据 / 浏览器资料 / 测试资料一个都不在包里
        self.assertEqual(release_violations(names), [])
        for bad in ("deploy/server-easylife.env", "deploy/id_ed25519", "deploy/notes.log",
                    "easylife/preview.db", "easylife/cookies.sqlite",
                    "easylife/ffprofile-auth/cookies.sqlite",
                    "docs/verification/mobile-mail/ui/browser.log",
                    "docs/verification/mobile-mail/ui/shot.png", "docs/session.har"):
            self.assertNotIn(bad, names)
        self.assertFalse([n for n in names if n.startswith("docs/verification")])
        # 首装/升级真正需要的文件仍然在（--exclude 没有误伤）
        for required in ("deploy/deploy.sh", "deploy/easylife.env.example", "deploy/easylife.service",
                         "deploy/backup.sh", "easylife/app.py", "easylife/static/app.js",
                         "docs/DEPLOY.md"):
            self.assertIn(required, names)
        self.assertIn("发布包核对通过", out)
        # 「远端」确实用这个包完成了部署
        self.assertIn("systemctl enable --now easylife", self.calls())

    def assert_secret_nowhere(self, secret):
        """密码不得出现在 stdout/stderr、工具调用日志、以及临时目录里的任何文件中。"""
        self.assertNotIn(secret, self.last_out)
        self.assertNotIn(secret, "\n".join(self.calls()))
        for base, _dirs, files in os.walk(self.tmp):
            for name in files:
                path = os.path.join(base, name)
                if os.path.islink(path):
                    continue
                with open(path, "rb") as fh:
                    self.assertNotIn(secret.encode(), fh.read(), path)

    # ------------------------------------------------------------ 打包 / 上传 / 部署
    def test_packs_uploads_runs_path_mode_and_selfchecks_on_server(self):
        self.make_sites()
        rc, out = self.remote()
        self.assertEqual(rc, 0, out)

        calls = self.calls()
        scp = [c for c in calls if c.startswith("scp ")]
        self.assertEqual(len(scp), 1, calls)
        self.assertTrue(scp[0].endswith("root@%s:%s/easylife-src.tar.gz" % (self.HOST, self.remote_dir)), scp)
        ssh = self.ssh_calls("deploy.sh")
        self.assertEqual(len(ssh), 1, calls)
        self.assertIn("cd %s" % self.remote_dir, ssh[0])
        self.assertIn("bash deploy/deploy.sh --mode path", ssh[0])
        self.assertFalse([c for c in calls if c.startswith("sshpass")])

        # 远端真的解包并跑了 deploy.sh：只加 include、服务 enable --now
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)
        self.assertIn("systemctl enable --now easylife", calls)

        names = self.tar_names()
        for want in ("deploy/deploy.sh", "deploy/deploy-remote.sh",
                     "deploy/nginx-easylife-location.conf", "easylife/app.py"):
            self.assertIn(want, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.startswith((".", "tests"))])
        self.assertIn("deploy/easylife.env.example", names)          # 模板必须在（首装要用）
        self.assertEqual(release_violations(names), [])               # 无运行时凭据/浏览器资料
        self.assertFalse([n for n in names if n.startswith("docs/verification")])
        self.assertIn("发布包核对通过", out)

        # 四条自检都在服务器上通过 ssh 执行：三条状态码 + 一条页面正文标识检查
        self.assertEqual([c.split()[1] for c in self.curl_calls()],
                         ["https://mch.wang/todos",
                          "https://mch.wang/todos/",
                          "https://mch.wang/todos/api/todos",
                          "https://mch.wang/todos/"], calls)
        self.assertTrue([c for c in calls if c.startswith("ssh ")
                         and "curl" in c and "https://mch.wang/todos/api/todos" in c], calls)
        for line in ("自检 1/4：https://mch.wang/todos -> 301（期望 301）",
                     "自检 2/4：https://mch.wang/todos/ -> 200（期望 200）",
                     "自检 3/4：https://mch.wang/todos/api/todos -> 401（期望 401）",
                     "自检 4/4：https://mch.wang/todos/ 页面包含新界面标识 'verification-banner'"):
            self.assertIn(line, out)
        self.assertIn("自检汇总：PASS 4 / FAIL 0", out)
        self.assertIn("部署完成并通过自检：https://mch.wang/todos/", out)
        self.assertNotIn("[FAIL]", out)

    def test_options_are_forwarded_to_deploy_sh_and_sshpass_reads_env_only(self):
        self.make_sites()
        secret = "not-a-real-pass-%d" % os.getpid()
        rc, out = self.run_script(DEPLOY_REMOTE, "admin@" + self.HOST, "--port", "8090",
                                  "--", "--smtp-host", "smtp.qq.com", "--smtp-user", "me@qq.com",
                                  SSHPASS=secret)
        self.assertEqual(rc, 0, out)
        calls = self.calls()
        self.assertTrue([c for c in calls if c == "sshpass -e env_set=yes"], calls)  # -e：只从环境变量读
        ssh = self.ssh_calls("deploy.sh")[0]
        self.assertIn("sudo bash deploy/deploy.sh --mode path --port 8090"
                      " --smtp-host smtp.qq.com --smtp-user me@qq.com", ssh)
        self.assertIn("EASYLIFE_PORT=8090", self.env_file())
        self.assert_secret_nowhere(secret)

    def test_sshpass_secret_not_in_stdout_log_file_or_any_file(self):
        self.make_sites()
        secret = "not-a-real-pass-%d" % os.getpid()
        log = os.path.join(self.tmp, "deploy.log")
        rc, out = self.remote("--log", log, SSHPASS=secret)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(log))
        with open(log) as fh:
            logged = fh.read()
        self.assertIn("[+]", logged)                     # 日志确实是本次运行的完整输出
        self.assertIn("部署完成并通过自检", logged)
        self.assertIn("sshpass -e", out)                  # 提示里只有变量名，没有密码本身
        self.assertNotIn(secret, logged)
        self.assert_secret_nowhere(secret)

    def test_sshpass_env_ignored_when_sshpass_is_missing(self):
        self.make_sites()
        secret = "not-a-real-pass-%d" % os.getpid()
        bin2 = os.path.join(self.tmp, "bin-nosshpass")
        os.makedirs(bin2, exist_ok=True)
        for name in os.listdir(self.bin):
            if name != "sshpass":
                os.symlink(os.path.join(self.bin, name), os.path.join(bin2, name))

        rc, out = self.remote(SSHPASS=secret,
                              PATH=bin2 + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
        self.assertEqual(rc, 0, out)
        self.assertIn("本机没有 sshpass", out)
        self.assertFalse([c for c in self.calls() if c.startswith("sshpass")])
        self.assertIn("bash deploy/deploy.sh --mode path", self.ssh_calls("deploy.sh")[0])
        self.assert_secret_nowhere(secret)

    # ------------------------------------------------------------ 部署后自检
    def test_selfcheck_failure_exits_non_zero_and_suggests_rollback(self):
        self.make_sites()
        self.force_curl_code(502)
        rc, out = self.remote()
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(out.count("冒烟检查失败"), 3, out)
        self.assertIn("页面标识检查失败", out)
        self.assertIn("自检汇总：PASS 0 / FAIL 4", out)
        self.assertIn("deploy-remote.sh root@%s --rollback" % self.HOST, out)
        self.assertIn("--rollback --restore-db", out)
        # 部署本身已经做了（自检失败只是提示回滚，不回滚由用户决定）
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)

    def test_selfcheck_falls_back_to_resolve_when_public_url_unreachable(self):
        self.make_sites()
        # 服务器上用公网地址自测拿不到响应（DNS 未生效 / 回流限制）：自动改用 --resolve 127.0.0.1
        with open(os.path.join(self.bin, "curl"), "w") as fh:
            fh.write(
                '#!/bin/bash\n'
                'url=""; for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done\n'
                'resolve=0; for a in "$@"; do [ "$a" = "--resolve" ] && resolve=1; done\n'
                'echo "curl $url resolve=$resolve" >> "$FAKE_STATE/calls"\n'
                'tonull=0; for a in "$@"; do [ "$a" = "/dev/null" ] && tonull=1; done\n'
                '[ "$resolve" = 1 ] || { printf 000; exit 0; }\n'
                '[ "$tonull" = 1 ] || { printf \'<div id="verification-banner"></div>\'; exit 0; }\n'
                'case "$url" in\n'
                '  */api/todos) printf 401 ;;\n'
                '  */todos) printf 301 ;;\n'
                '  *) printf 200 ;;\n'
                'esac\n')
        os.chmod(os.path.join(self.bin, "curl"), 0o755)

        rc, out = self.remote()
        self.assertEqual(rc, 0, out)
        self.assertIn("改用 --resolve mch.wang:443:127.0.0.1", out)
        self.assertIn("自检汇总：PASS 4 / FAIL 0", out)
        calls = self.curl_calls()
        self.assertEqual(calls[0], "curl https://mch.wang/todos resolve=0", calls)   # 先试公网
        self.assertTrue([c for c in calls if c.endswith("resolve=1")], calls)

    def test_no_smoke_skips_the_three_selfchecks(self):
        self.make_sites()
        rc, out = self.remote("--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("已跳过部署后自检", out)
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(self.read(self.site).decode().count(MARKER), 2)

    def test_rollback_option_undoes_the_remote_deploy(self):
        self.make_sites()
        before = snapshot(self.nginx)
        rc, out = self.remote()
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(snapshot(self.nginx), before)

        rc, out = self.remote("--rollback")
        self.assertEqual(rc, 0, out)
        self.assertIn("回滚完成", out)
        self.assertEqual(snapshot(self.nginx), before)          # 逐字节回到部署前
        self.assertFalse(os.path.exists(os.path.join(self.conf_dir, "nginx-path.manifest")))
        self.assertIn("%s/deploy/deploy.sh --rollback" % self.app_dir, "\n".join(self.ssh_calls()))

    # ------------------------------------------------------------ --check 预检（只读）
    def test_check_mode_passes_and_changes_nothing(self):
        self.make_sites()
        before = snapshot(self.nginx)

        rc, out = self.remote("--check")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("[PASS]"), 4, out)
        self.assertNotIn("[FAIL]", out)
        self.assertIn("预检汇总：PASS 4 / FAIL 0", out)
        self.assertIn("SSH 连通", out)
        self.assertIn("远端 nginx -t 通过", out)
        self.assertIn("端口 8086 空闲", out)
        self.assertIn("找到 mch.wang 的 443 server 块", out)

        # 全程不做任何修改：没打包、没上传、没跑 deploy.sh、没发任何 curl、远端文件逐字节不变
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith("scp ")], [])
        self.assertEqual([c for c in calls if "deploy.sh" in c], [])
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(snapshot(self.nginx), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "uploaded.tar.gz")))
        self.assertFalse(os.path.exists(self.app_dir))
        self.assertFalse(os.path.exists(self.conf_dir))
        self.assertFalse(os.path.exists(self.backup_dir))
        self.assertFalse(os.path.exists(self.unit))

    def test_check_mode_reports_busy_port(self):
        self.make_sites()
        before = snapshot(self.nginx)
        with open(os.path.join(self.state, "ss_output"), "w") as fh:
            fh.write('LISTEN 0 128 127.0.0.1:8086 0.0.0.0:* users:(("java",pid=42,fd=7))\n')

        rc, out = self.remote("--check")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("[FAIL]", out)
        self.assertIn("8086 已被占用", out)
        self.assertIn("--port 8090", out)
        self.assertIn("预检汇总：PASS 3 / FAIL 1", out)
        self.assertEqual(snapshot(self.nginx), before)

    def test_check_mode_accepts_port_used_by_own_service(self):
        self.make_sites()
        with open(os.path.join(self.state, "ss_output"), "w") as fh:
            fh.write('LISTEN 0 128 127.0.0.1:8086 0.0.0.0:* users:(("python3",pid=4242,fd=3))\n')
        open(os.path.join(self.state, "easylife.active"), "w").close()

        rc, out = self.remote("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("已被本服务 easylife 占用", out)
        self.assertIn("预检汇总：PASS 4 / FAIL 0", out)

        rc, out = self.remote("--check", "--port", "8090")       # 换端口后检查的是新端口
        self.assertEqual(rc, 0, out)
        self.assertIn("端口 8090 空闲", out)

    def test_check_mode_fails_without_443_server_block(self):
        self.make_sites()
        self.write("sites-available/mch.wang", SITE_80)          # 只剩 80 端口的块
        rc, out = self.remote("--check")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("没找到 server_name 含 mch.wang 且 listen 443 的 server 块", out)
        self.assertIn("预检汇总：PASS 3 / FAIL 1", out)

    def test_check_mode_fails_when_remote_nginx_t_fails(self):
        self.make_sites()
        open(os.path.join(self.state, "nginx_t_fail"), "w").close()
        rc, out = self.remote("--check")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("远端 nginx -t 失败", out)
        self.assertIn("预检汇总：PASS 3 / FAIL 1", out)

    def test_check_mode_reports_unreachable_host(self):
        with open(os.path.join(self.bin, "ssh"), "w") as fh:
            fh.write('#!/bin/bash\necho "ssh $*" >> "$FAKE_STATE/calls"\nexit 255\n')
        os.chmod(os.path.join(self.bin, "ssh"), 0o755)

        rc, out = self.remote("--check")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("SSH 连通失败", out)
        self.assertIn("sshpass", out)                 # 提示里给出 SSHPASS/sshpass 这条可选路径
        self.assertIn("预检汇总：PASS 0 / FAIL 1", out)

    # ------------------------------------------------------------ 选项/用法
    def test_help_keeps_original_options_and_documents_new_ones(self):
        rc, out = self.run_script(DEPLOY_REMOTE, "-h")
        self.assertEqual(rc, 0, out)
        for opt in ("--port", "--site-domain", "--ssh-port", "-i", "--no-smoke", "--check",
                    "--rollback", "--log", "--mode path", "--help"):
            self.assertIn(opt, out)

    def test_unknown_option_and_mutually_exclusive_modes_still_fail(self):
        rc, out = self.run_script(DEPLOY_REMOTE, self.HOST, "--nope")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("未知参数", out)
        rc, out = self.run_script(DEPLOY_REMOTE, self.HOST, "--check", "--rollback")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("不能同时使用", out)
        rc, out = self.run_script(DEPLOY_REMOTE)          # 没有 host
        self.assertEqual(rc, 2, out)


class UpgradeRehearsalTest(DeployHarness):
    """升级预演：旧库/旧环境 -> 升级 -> 重复升级 -> 备份/恢复（临时目录 + 命令桩）。

    不接触真实 systemd/nginx：systemctl/nginx/ss/id/useradd/chown/install 都是桩，
    所有目录都被 EASYLIFE_*_DIR 指到临时目录；数据库备份目录由 EASYLIFE_DB_BACKUP_DIR 指定。
    """

    def data_db(self):
        return os.path.join(self.tmp, "var", "lib", "easylife", "easylife.db")

    def env_path(self):
        return os.path.join(self.conf_dir, "easylife.env")

    def write_env(self, text, mode=0o644):
        os.makedirs(self.conf_dir, exist_ok=True)
        with open(self.env_path(), "w") as fh:
            fh.write(text)
        os.chmod(self.env_path(), mode)
        return self.env_path()

    def old_env_text(self, db_path):
        return (
            "# 旧版部署留下的环境文件（模拟升级现场：SMTP_*、密钥与自定义行都在）\n"
            "EASYLIFE_HOST=127.0.0.1\n"
            "EASYLIFE_PORT=8086\n"
            "EASYLIFE_DB=%s\n"
            "EASYLIFE_SECRET=%s\n"
            "EASYLIFE_BASE_PATH=/todos\n"
            "PUBLIC_URL=https://mch.wang/todos\n"
            "SMTP_HOST=smtp.qq.com\n"
            "SMTP_PORT=465\n"
            "SMTP_USER=old@example.com\n"
            "SMTP_PASS=%s\n"
            "SMTP_FROM=\n"
            "EASYLIFE_REMIND_INTERVAL=60\n"
            "EASYLIFE_SESSION_TTL=2592000\n"
            "EASYLIFE_CUSTOM_FLAG=keep-me\n" % (db_path, OLD_SECRET, FAKE_SMTP_CODE)
        )

    def db_backup_dir(self):
        return os.path.join(self.tmp, "db-backups")

    def read_mail_value(self, db_path):
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            row = conn.execute("SELECT value FROM site_settings WHERE key = 'mail.smtp'").fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    # ------------------------------------------------------------------ 新装
    def test_fresh_install_initializes_admin_key_empty_and_never_admin_emails(self):
        self.make_sites()
        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)
        env = self.env_file()
        self.assertEqual(env.count("EASYLIFE_ADMIN_USER_IDS="), 1, env)
        self.assertEqual([l for l in env if l.startswith("EASYLIFE_ADMIN_USER_IDS=")],
                         ["EASYLIFE_ADMIN_USER_IDS="])          # 默认空 = 无人有权限
        joined = "\n".join(env)
        self.assertNotIn("EASYLIFE_ADMIN_EMAILS", joined)        # 不得引入邮箱匹配机制
        self.assertIn("EASYLIFE_PUBLIC_URL=https://mch.wang/todos", env)
        self.assertIn("EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true", env)
        template = self.read(os.path.join(ROOT, "deploy", "easylife.env.example")).decode()
        self.assertIn("EASYLIFE_ADMIN_USER_IDS=", template)
        self.assertIn("SELECT id, email FROM users;", template)
        self.assertNotIn("EASYLIFE_ADMIN_EMAILS", template)

    # ------------------------------------------------------------ 旧环境升级
    def test_upgrade_keeps_secrets_smtp_db_and_appends_only_missing_keys(self):
        self.make_sites()
        db_path = self.data_db()
        seed_old_db(db_path, todos=3)
        db_before = self.read(db_path)
        old_env = self.old_env_text(db_path)
        self.write_env(old_env, mode=0o644)          # 旧文件本来还偏开放，部署应收紧到 0640

        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)

        after = self.read(self.env_path()).decode()
        self.assertTrue(after.startswith(old_env), after)        # 原有内容逐字节保留
        appended = [line for line in after[len(old_env):].splitlines() if line]
        self.assertEqual(appended, [                              # 只补缺失项，且只补一次
            "EASYLIFE_PUBLIC_URL=https://mch.wang/todos",
            "EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true",
            "EASYLIFE_ADMIN_USER_IDS=",
        ])
        self.assertIn("SMTP_HOST=smtp.qq.com", after)
        self.assertIn("SMTP_USER=old@example.com", after)
        self.assertIn("SMTP_PASS=%s" % FAKE_SMTP_CODE, after)
        self.assertIn("EASYLIFE_SECRET=%s" % OLD_SECRET, after)
        self.assertIn("EASYLIFE_CUSTOM_FLAG=keep-me", after)
        self.assertNotIn("EASYLIFE_ADMIN_EMAILS", after)
        self.assertEqual(self.read(db_path), db_before)          # 部署脚本从不写数据库
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path()).st_mode), 0o640)

    def test_upgrade_keeps_existing_admin_ids_and_repeated_runs_do_not_touch_anything(self):
        self.make_sites()
        db_path = self.data_db()
        seed_old_db(db_path, todos=1)
        easylife_db.init_db(db_path)                             # 应用已把旧库迁移到最新 schema
        add_mail_settings(db_path)
        self.write_env(self.old_env_text(db_path) +
                       "EASYLIFE_PUBLIC_URL=https://mch.wang/todos\n"
                       "EASYLIFE_REQUIRE_EMAIL_VERIFICATION=true\n"
                       "EASYLIFE_ADMIN_USER_IDS=1\n")

        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("管理员配置检查通过", out)                  # ID 1 是现有账号
        env_after_first = self.read(self.env_path())
        db_after_first = self.read(db_path)
        nginx_after_first = snapshot(self.nginx)

        rc, out = self.deploy()                                  # 第二次升级：幂等
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.env_path()), env_after_first)   # 不再追加、不覆盖
        self.assertEqual(self.read(db_path), db_after_first)            # 数据库逐字节不变
        self.assertEqual(snapshot(self.nginx), nginx_after_first)       # nginx 不再改动
        self.assertEqual(self.read_mail_value(db_path), MAIL_VALUE)     # 网页邮件设置完好
        self.assertIn("保持原值不动", out)

    # ---------------------------------------------------------------- 权限
    def test_upgrade_restricts_permissions_of_config_backups_and_state_files(self):
        self.make_sites()
        db_path = self.data_db()
        seed_old_db(db_path, todos=1)
        self.write_env(self.old_env_text(db_path), mode=0o600)

        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)

        self.assertEqual(stat.S_IMODE(os.stat(self.env_path()).st_mode), 0o640)
        state = os.path.join(self.conf_dir, "db-backup.last")
        self.assertTrue(os.path.exists(state))
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o600)
        manifest = os.path.join(self.conf_dir, "nginx-path.manifest")
        self.assertEqual(stat.S_IMODE(os.stat(manifest).st_mode), 0o600)
        backup_root = self.db_backup_dir()
        self.assertEqual(stat.S_IMODE(os.stat(backup_root).st_mode), 0o700)
        names = os.listdir(backup_root)
        self.assertEqual(len(names), 1, names)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(backup_root, names[0])).st_mode), 0o600)
        # 单元文件不是密件，保持 0644（服务本身仍以 easylife 用户运行、不写 /etc）
        self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
        with open(self.unit) as fh:
            unit = fh.read()
        self.assertIn("User=easylife", unit)
        self.assertIn("ReadWritePaths=/var/lib/easylife", unit)
        self.assertNotIn("User=root", unit)

    # ------------------------------------------------------------ 备份与恢复
    def test_plain_rollback_keeps_data_and_explicit_restore_db_brings_mail_settings_back(self):
        self.make_sites()
        db_path = self.data_db()
        seed_old_db(db_path, todos=2)
        easylife_db.init_db(db_path)
        add_mail_settings(db_path)
        self.write_env(self.old_env_text(db_path))

        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)
        code_app = self.read(os.path.join(self.app_dir, "easylife", "app.py"))

        # 部署后：网页保存了新配置、用户又加了一条待办
        changed_value = MAIL_VALUE.replace("smtp.saved.example", "smtp.changed.example")
        conn = sqlite3.connect(db_path)
        try:
            now = easylife_db.now_utc()
            conn.execute("UPDATE site_settings SET value = ? WHERE key = 'mail.smtp'", (changed_value,))
            conn.execute("INSERT INTO todos (user_id, title, status, priority, created_at, updated_at) "
                         "VALUES (1, '升级后新增', 'open', 2, ?, ?)", (now, now))
            conn.commit()
        finally:
            conn.close()
        changed_bytes = self.read(db_path)

        # 1) 普通 --rollback：只撤服务配置；数据（含网页邮件设置）与代码都不动
        rc, out = self.deploy("--rollback")
        self.assertEqual(rc, 0, out)
        self.assertIn("不会把代码恢复到旧版本", out)
        self.assertIn("--rollback --restore-db", out)
        self.assertEqual(self.read(db_path), changed_bytes)
        self.assertEqual(self.read(os.path.join(self.app_dir, "easylife", "app.py")), code_app)
        self.assertFalse(os.path.exists(self.unit))

        # 2) 显式 --rollback --restore-db：数据库换回部署前的一致性快照
        rc, out = self.deploy("--rollback", "--restore-db")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_mail_value(db_path), MAIL_VALUE)      # 邮件设置回到部署前
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            todos = conn.execute("SELECT count(*) FROM todos").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(todos, 2)                                       # 部署后新增的待办不再出现
        self.assertEqual(stat.S_IMODE(os.stat(db_path).st_mode), 0o600)
        copies = [f for f in os.listdir(os.path.dirname(db_path)) if ".before-restore." in f]
        self.assertEqual(len(copies), 1, copies)                         # 恢复前的库有留档
        self.assertEqual(self.read(os.path.join(self.app_dir, "easylife", "app.py")), code_app)

    def test_backup_script_snapshot_contains_web_mail_settings(self):
        db_path = self.data_db()
        seed_old_db(db_path, todos=1)
        easylife_db.init_db(db_path)
        add_mail_settings(db_path)
        out_dir = os.path.join(self.tmp, "manual-backups")

        rc, out = self.run_script(BACKUP, "--db", db_path, "--dir", out_dir,
                                  EASYLIFE_PYTHON=shutil.which("python3") or sys.executable)
        self.assertEqual(rc, 0, out)
        names = os.listdir(out_dir)
        self.assertEqual(len(names), 1, names)
        snapshot_path = os.path.join(out_dir, names[0])
        self.assertEqual(stat.S_IMODE(os.stat(snapshot_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(out_dir).st_mode), 0o700)
        conn = sqlite3.connect("file:%s?mode=ro" % snapshot_path, uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            value = conn.execute("SELECT value FROM site_settings WHERE key = 'mail.smtp'").fetchone()[0]
            self.assertEqual(value, MAIL_VALUE)
            self.assertEqual(conn.execute("SELECT count(*) FROM todos").fetchone()[0], 1)
        finally:
            conn.close()

    # ------------------------------------------------------------ 管理员自检
    def test_admin_selfcheck_reports_missing_and_invalid_ids_without_blocking_upgrade(self):
        self.make_sites()
        db_path = self.data_db()
        seed_old_db(db_path, todos=0)
        self.write_env(self.old_env_text(db_path) + "EASYLIFE_ADMIN_USER_IDS=1, 999, abc\n")

        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)                      # 只预警，不阻断升级
        self.assertIn("配置错误", out)
        self.assertIn("999", out)
        self.assertIn("abc", out)
        self.assertIn("不会获得管理员权限", out)

        fixed = self.read(self.env_path()).decode().replace("EASYLIFE_ADMIN_USER_IDS=1, 999, abc",
                                                            "EASYLIFE_ADMIN_USER_IDS=1")
        self.write_env(fixed)
        rc, out = self.deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("管理员配置检查通过", out)
        self.assertNotIn("配置错误", out)

    # ---------------------------------------------------------------- 语义
    def test_help_spells_out_rollback_and_restore_semantics(self):
        rc, out = self.run_script(DEPLOY, "--help")
        self.assertEqual(rc, 0, out)
        for opt in ("--port", "--mode path|subdomain", "--base-url", "--base-path", "--domain",
                    "--site-domain", "--nginx-conf", "--certbot", "--smtp-host", "--smtp-port",
                    "--smtp-user", "--smtp-from", "--no-nginx", "--no-backup", "--rollback",
                    "--restore-db"):
            self.assertIn(opt, out)
        self.assertIn("不会还原旧代码", out)
        self.assertIn("绝不自动动数据", out)


class BackupScriptTest(DeployHarness):
    def test_backup_output_and_intermediate_files_are_private_from_creation(self):
        # gzip 桩：在压缩前记录 .part 文件此刻的权限（旧实现是 0644，umask 0077 后是 0600）
        stub = os.path.join(self.bin, "gzip")
        with open(stub, "w") as fh:
            fh.write(textwrap.dedent("""\
                #!/bin/bash
                for a in "$@"; do
                    if [ -f "$a" ]; then stat -c '%a' "$a" >> "$FAKE_STATE/part-mode"; break; fi
                done
                exec /usr/bin/gzip "$@"
            """))
        os.chmod(stub, 0o755)

        db_path = os.path.join(self.tmp, "var", "lib", "easylife", "easylife.db")
        seed_old_db(db_path, todos=2)
        out_dir = os.path.join(self.tmp, "backup-out")

        rc, out = self.run_script(BACKUP, "--db", db_path, "--dir", out_dir, "--gzip",
                                  EASYLIFE_PYTHON=shutil.which("python3") or sys.executable)
        self.assertEqual(rc, 0, out)

        with open(os.path.join(self.state, "part-mode")) as fh:
            moments = fh.read().split()
        self.assertEqual(moments, ["600"])                        # 中途文件从创建起就是 0600
        self.assertEqual(stat.S_IMODE(os.stat(out_dir).st_mode), 0o700)
        names = os.listdir(out_dir)
        self.assertEqual(len(names), 1, names)
        self.assertTrue(names[0].endswith(".db.gz"), names)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(out_dir, names[0])).st_mode), 0o600)
        self.assertFalse([n for n in names if n.endswith(".part")])

        # 压缩包可解开、integrity_check=ok、数据完整
        restored = os.path.join(self.tmp, "restored.db")
        with gzip.open(os.path.join(out_dir, names[0]), "rb") as src, open(restored, "wb") as dst:
            shutil.copyfileobj(src, dst)
        conn = sqlite3.connect("file:%s?mode=ro" % restored, uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM todos").fetchone()[0], 2)
        finally:
            conn.close()


class ScriptSyntaxTest(unittest.TestCase):
    def test_bash_n(self):
        deploy_dir = os.path.join(ROOT, "deploy")
        for name in sorted(os.listdir(deploy_dir)):
            if name.endswith(".sh"):
                with self.subTest(script=name):
                    proc = subprocess.run(["bash", "-n", os.path.join(deploy_dir, name)],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    self.assertEqual(proc.returncode, 0, proc.stdout.decode())


if __name__ == "__main__":
    unittest.main()
