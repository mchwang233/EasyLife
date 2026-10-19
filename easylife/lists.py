"""Task lists ("projects"). Each user has one default list; todos with list_id NULL belong to it.

The default list row only carries its name / colour / order and is created on first use, so
existing todos never need to be moved into it. CalDAV exposes every list as a calendar
collection named by its slug (the default one is "tasks").
"""

import re

from easylife import db

DEFAULT_SLUG = "tasks"
DEFAULT_NAME = "EasyLife"
DEFAULT_COLOR = "#2F6FEB"
NAME_MAX = 50
MAX_LISTS = 50
SLUG_RE = re.compile(r"^[A-Za-z0-9._~@+=-]{1,100}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
COLUMNS = "id, slug, name, color, sort_order, is_default, created_at, updated_at"


class ListError(ValueError):
    pass


def clean_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ListError("列表名称不能为空")
    value = " ".join(value.split())
    if len(value) > NAME_MAX:
        raise ListError("列表名称不能超过 %d 个字符" % NAME_MAX)
    return value


def clean_color(value):
    """'#RRGGBB'; clients may send '#RRGGBBAA', the alpha is dropped."""
    if value is None:
        return None
    if not isinstance(value, str) or not COLOR_RE.match(value.strip()):
        raise ListError("颜色必须是 #RRGGBB 格式")
    return value.strip()[:7].upper()


def _dict(row):
    item = {k: row[k] for k in row.keys()}
    item["is_default"] = bool(item["is_default"])
    return item


def ensure_default(conn, user_id):
    row = conn.execute("SELECT " + COLUMNS + " FROM lists WHERE user_id = ? AND is_default = 1",
                       (user_id,)).fetchone()
    if row is None:
        now = db.now_utc()
        with db.tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO lists (user_id, slug, name, color, sort_order, is_default, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
                (user_id, DEFAULT_SLUG, DEFAULT_NAME, DEFAULT_COLOR, now, now))
        row = conn.execute("SELECT " + COLUMNS + " FROM lists WHERE user_id = ? AND is_default = 1",
                           (user_id,)).fetchone()
    return _dict(row)


def all_lists(conn, user_id, counts=False):
    """Every list of the user, default first, then by order and creation."""
    ensure_default(conn, user_id)
    rows = conn.execute("SELECT " + COLUMNS + " FROM lists WHERE user_id = ? "
                        "ORDER BY is_default DESC, sort_order, id", (user_id,)).fetchall()
    items = [_dict(r) for r in rows]
    if counts:
        tally = {r[0]: r[1] for r in conn.execute(
            "SELECT list_id, COUNT(*) FROM todos WHERE user_id = ? AND status NOT IN "
            "('done', 'archived') GROUP BY list_id", (user_id,))}
        for item in items:
            item["open_count"] = tally.get(None if item["is_default"] else item["id"], 0)
    return items


def get_list(conn, user_id, list_id):
    row = conn.execute("SELECT " + COLUMNS + " FROM lists WHERE id = ? AND user_id = ?",
                       (list_id, user_id)).fetchone()
    return _dict(row) if row else None


def by_slug(conn, user_id, slug):
    if slug == DEFAULT_SLUG:
        return ensure_default(conn, user_id)
    row = conn.execute("SELECT " + COLUMNS + " FROM lists WHERE user_id = ? AND slug = ?",
                       (user_id, slug)).fetchone()
    return _dict(row) if row else None


def stored_id(item):
    """The todos.list_id value for a list: NULL for the default list."""
    return None if item["is_default"] else item["id"]


def todo_list_id(conn, user_id, value):
    """Validate a list id sent by the web client and return the value to store."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ListError("list_id 必须是整数")
    item = get_list(conn, user_id, value)
    if item is None:
        raise ListError("列表不存在")
    return stored_id(item)


def create_list(conn, user_id, name, color=None, slug=None, order=None):
    name = clean_name(name)
    color = clean_color(color) or DEFAULT_COLOR
    if slug is not None and (not SLUG_RE.match(slug) or slug == DEFAULT_SLUG):
        raise ListError("列表地址无效")
    ensure_default(conn, user_id)
    now = db.now_utc()
    with db.tx(conn):
        count = conn.execute("SELECT COUNT(*) FROM lists WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= MAX_LISTS:
            raise ListError("最多只能有 %d 个列表" % MAX_LISTS)
        if slug is not None and conn.execute("SELECT 1 FROM lists WHERE user_id = ? AND slug = ?",
                                             (user_id, slug)).fetchone():
            raise ListError("列表已存在")
        if order is None:
            order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM lists "
                                 "WHERE user_id = ?", (user_id,)).fetchone()[0]
        # Web-created lists get a temporary slug, replaced by one derived from the id.
        cur = conn.execute(
            "INSERT INTO lists (user_id, slug, name, color, sort_order, is_default, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (user_id, slug or "new-" + now, name, color, order, now, now))
        list_id = cur.lastrowid
        if slug is None:
            conn.execute("UPDATE lists SET slug = ? WHERE id = ?", ("list-%d" % list_id, list_id))
    return get_list(conn, user_id, list_id)


def update_list(conn, user_id, list_id, name=None, color=None, order=None):
    fields = {}
    if name is not None:
        fields["name"] = clean_name(name)
    if color is not None:
        fields["color"] = clean_color(color)
    if order is not None:
        fields["sort_order"] = int(order)
    with db.tx(conn):
        if get_list(conn, user_id, list_id) is None:
            return None
        if fields:
            fields["updated_at"] = db.now_utc()
            assignments = ", ".join("%s = ?" % k for k in fields)  # keys are fixed above
            conn.execute("UPDATE lists SET " + assignments + " WHERE id = ? AND user_id = ?",
                         list(fields.values()) + [list_id, user_id])
    return get_list(conn, user_id, list_id)


def delete_list(conn, user_id, list_id, delete_todos=False):
    """Remove a non-default list. Its todos move to the default list, or are deleted
    (CalDAV clients delete a list together with its reminders). Returns the count moved/deleted,
    or None when the list does not exist."""
    with db.tx(conn):
        item = get_list(conn, user_id, list_id)
        if item is None:
            return None
        if item["is_default"]:
            raise ListError("默认列表不能删除")
        if delete_todos:
            cur = conn.execute("DELETE FROM todos WHERE user_id = ? AND list_id = ?",
                               (user_id, list_id))
        else:
            cur = conn.execute("UPDATE todos SET list_id = NULL, updated_at = ? "
                               "WHERE user_id = ? AND list_id = ?",
                               (db.now_utc(), user_id, list_id))
        conn.execute("DELETE FROM lists WHERE id = ? AND user_id = ?", (list_id, user_id))
    return cur.rowcount
