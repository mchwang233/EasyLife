"""SQLite connection handling, schema creation and migrations (DESIGN.md §1.2, §2)."""

import contextlib
import datetime
import sqlite3
import threading
import time

# Each migration is a list of statements; index + 1 is the schema version it
# produces. Applied migrations are tracked in PRAGMA user_version, so adding a
# new entry at the end upgrades existing databases on the next start.
MIGRATIONS = [
    [
        """CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          email         TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at    TEXT NOT NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)",
        """CREATE TABLE IF NOT EXISTS sessions (
          token      TEXT PRIMARY KEY,
          user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at)",
        """CREATE TABLE IF NOT EXISTS todos (
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
        )""",
        "CREATE INDEX IF NOT EXISTS idx_todos_user   ON todos(user_id, status, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_todos_remind ON todos(reminder_sent, remind_at)",
        """CREATE TABLE IF NOT EXISTS todo_notes (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          todo_id    INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
          content    TEXT    NOT NULL,
          created_at TEXT    NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notes_todo ON todo_notes(todo_id, created_at)",
    ],
    # v2: per-user private calendar subscription token (random, rotatable).
    [
        lambda conn: add_column(conn, "users", "calendar_token", "TEXT"),
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_calendar_token ON users(calendar_token)",
    ],
    # v3: e-mail verification + one-time tokens for verify / password reset.
    # Every step is idempotent (column added only if missing, IF NOT EXISTS), so it
    # is safe even on a database whose user_version is out of step with its schema.
    [
        # Users that existed before this migration are trusted as verified, so the
        # rollout never locks existing accounts out. Replaying the migration must
        # not verify accounts registered after the column was introduced.
        lambda conn: migrate_email_verification(conn),
        # Only the SHA-256 of a token is stored; the plain token exists only in the mail.
        """CREATE TABLE IF NOT EXISTS auth_tokens (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          purpose    TEXT    NOT NULL CHECK (purpose IN ('verify','reset')),
          token_hash TEXT    NOT NULL UNIQUE,
          expires_at TEXT    NOT NULL,
          used_at    TEXT,
          created_at TEXT    NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id, purpose)",
        "CREATE INDEX IF NOT EXISTS idx_auth_tokens_exp  ON auth_tokens(expires_at)",
    ],
    # v4: optional site-wide mail overrides; no row means the legacy environment.
    [
        """CREATE TABLE IF NOT EXISTS site_settings (
          key        TEXT PRIMARY KEY,
          value      TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        )""",
    ],
    # v5: CalDAV task sync. One app-specific password per user (SHA-256 only), and
    # the client-side identity / unmodelled iCalendar data of todos synced by a client.
    [
        """CREATE TABLE IF NOT EXISTS dav_credentials (
          user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          password_hash TEXT NOT NULL,
          created_at    TEXT NOT NULL,
          last_used_at  TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS dav_objects (
          todo_id   INTEGER PRIMARY KEY REFERENCES todos(id) ON DELETE CASCADE,
          user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name      TEXT NOT NULL,
          uid       TEXT NOT NULL,
          extra     TEXT,
          alarms    TEXT,
          alarms_at TEXT,
          UNIQUE (user_id, name),
          UNIQUE (user_id, uid)
        )""",
    ],
    # v6: the client's last iCalendar text and the todo fields it produced, so unchanged
    # objects are echoed back verbatim and edited ones only get the changed fields replaced.
    [
        lambda conn: add_column(conn, "dav_objects", "raw", "TEXT"),
        lambda conn: add_column(conn, "dav_objects", "snapshot", "TEXT"),
    ],
    # v7: task lists. todos.list_id NULL means the user's default list, so existing todos
    # stay where they are; the default list's row (name / colour) is created on first use.
    [
        """CREATE TABLE IF NOT EXISTS lists (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          slug       TEXT    NOT NULL,
          name       TEXT    NOT NULL,
          color      TEXT    NOT NULL,
          sort_order INTEGER NOT NULL DEFAULT 0,
          is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
          created_at TEXT    NOT NULL,
          updated_at TEXT    NOT NULL,
          UNIQUE (user_id, slug)
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lists_default ON lists(user_id) WHERE is_default = 1",
        lambda conn: add_column(conn, "todos", "list_id",
                                "INTEGER REFERENCES lists(id) ON DELETE SET NULL"),
        "CREATE INDEX IF NOT EXISTS idx_todos_list ON todos(user_id, list_id)",
    ],
    # v8: iPhone-style reminders (an alert at the due time, remind_at is the optional
    # earlier alert), repeating / urgent / flagged tasks, subtasks, tags and images.
    [
        lambda conn: add_column(conn, "todos", "due_alert_sent",
                                "INTEGER NOT NULL DEFAULT 0 CHECK (due_alert_sent IN (0,1))"),
        lambda conn: add_column(conn, "todos", "repeat", "TEXT"),
        lambda conn: add_column(conn, "todos", "urgent",
                                "INTEGER NOT NULL DEFAULT 0 CHECK (urgent IN (0,1))"),
        lambda conn: add_column(conn, "todos", "flagged",
                                "INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0,1))"),
        # SET NULL: deleting a parent on the phone (where subtasks look like ordinary
        # reminders) must not silently delete its subtasks.
        lambda conn: add_column(conn, "todos", "parent_id",
                                "INTEGER REFERENCES todos(id) ON DELETE SET NULL"),
        lambda conn: add_column(conn, "todos", "nag_count", "INTEGER NOT NULL DEFAULT 0"),
        lambda conn: add_column(conn, "todos", "next_nag_at", "TEXT"),
        "CREATE INDEX IF NOT EXISTS idx_todos_parent ON todos(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_todos_due_alert ON todos(due_alert_sent, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_todos_nag ON todos(next_nag_at)",
        """CREATE TABLE IF NOT EXISTS todo_tags (
          todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
          tag     TEXT    NOT NULL,
          PRIMARY KEY (todo_id, tag)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_todo_tags_tag ON todo_tags(tag)",
        """CREATE TABLE IF NOT EXISTS todo_images (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          todo_id    INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
          user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          mime       TEXT    NOT NULL,
          size       INTEGER NOT NULL,
          data       BLOB    NOT NULL,
          created_at TEXT    NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_todo_images_todo ON todo_images(todo_id)",
        lambda conn: migrate_due_alerts(conn),
    ],
]

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOCK_RETRIES = 5

_local = threading.local()


def now_utc(offset_seconds=0):
    """Current UTC time as 'YYYY-MM-DD HH:MM:SS' (sortable as a string)."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_seconds)
    return t.strftime(TIME_FORMAT)


def parse_time(value):
    """Parse an ISO-8601 string into the canonical UTC storage format.

    Naive values are taken as UTC; offsets (including 'Z') are converted.
    Raises ValueError when the value cannot be parsed.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("invalid time")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).strftime(TIME_FORMAT)


def connect(path):
    """Open a new connection with the pragmas every connection needs."""
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn(path):
    """Return this thread's cached connection to `path`, creating it if needed."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(path)
    if conn is None:
        conn = conns[path] = connect(path)
    return conn


def close_thread_conns():
    """Close every connection cached by the current thread."""
    conns = getattr(_local, "conns", None) or {}
    for conn in conns.values():
        conn.close()
    conns.clear()


@contextlib.contextmanager
def tx(conn):
    """Run a short write transaction, retrying a few times if the DB is locked."""
    for attempt in range(LOCK_RETRIES):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == LOCK_RETRIES - 1:
                raise
            time.sleep(0.05 * (2 ** attempt))
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # COMMIT can fail too (disk errors / deferred constraints). Do not leave
        # a failed save visible on this connection or hold its write lock.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def add_column(conn, table, column, decl):
    """ALTER TABLE ... ADD COLUMN, skipped when the column already exists."""
    names = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in names:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        return True
    return False


def migrate_email_verification(conn):
    if add_column(conn, "users", "email_verified_at", "TEXT"):
        conn.execute("UPDATE users SET email_verified_at = ?", (now_utc(),))


def migrate_due_alerts(conn):
    """Move existing reminders to the iPhone model without mailing anything twice.

    A reminder without a due time becomes the due time; a reminder at or after the due
    time is replaced by the alert at the due time; alerts that already passed or were
    already sent are marked as sent.
    """
    now = now_utc()
    conn.execute("UPDATE todos SET due_at = remind_at, due_alert_sent = reminder_sent, "
                 "remind_at = NULL, reminder_sent = 0 "
                 "WHERE due_at IS NULL AND remind_at IS NOT NULL")
    conn.execute("UPDATE todos SET due_alert_sent = 1 WHERE due_at IS NOT NULL AND "
                 "(due_at <= ? OR (reminder_sent = 1 AND remind_at >= due_at))", (now,))
    conn.execute("UPDATE todos SET remind_at = NULL, reminder_sent = 0 "
                 "WHERE remind_at IS NOT NULL AND due_at IS NOT NULL AND remind_at >= due_at")


def init_db(path):
    """Create tables and apply pending migrations. Safe to call on every start."""
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, statements in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            with tx(conn):
                # Another process may have migrated while this one waited for
                # the write lock. Check again before applying any statements.
                if number <= conn.execute("PRAGMA user_version").fetchone()[0]:
                    continue
                for statement in statements:
                    if callable(statement):
                        statement(conn)
                    else:
                        conn.execute(statement)
                conn.execute("PRAGMA user_version = %d" % number)
    finally:
        conn.close()
