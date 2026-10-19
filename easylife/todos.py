"""Todo and note business logic. Every query is scoped by user_id (DESIGN.md §3.5-3.7, §4.3)."""

from easylife import calendar_ics, db, lists

STATUSES = ("open", "doing", "done", "archived")
TITLE_MAX = 200
DETAILS_MAX = 20000
NOTE_MAX = 10000
LIMIT_DEFAULT, LIMIT_MAX = 100, 500
ORDERS = {
    "due_at": "due_at IS NULL, due_at ASC, id DESC",
    "priority": "priority DESC, due_at IS NULL, due_at ASC, id DESC",
    "created_at": "created_at DESC, id DESC",
    "updated_at": "updated_at DESC, id DESC",
}
DEFAULT_ORDER = "priority DESC, due_at IS NULL, due_at ASC, id DESC"
EDITABLE = ("title", "details", "status", "priority", "due_at", "remind_at", "list_id")
# Fields a client may echo back from a GET; ignored rather than rejected.
READ_ONLY = ("id", "user_id", "reminder_sent", "created_at", "updated_at",
             "notes", "notes_count", "ics_url", "google_calendar_url", "warnings")

TODO_COLUMNS = ("id, title, details, status, priority, due_at, remind_at, reminder_sent, "
                "list_id, created_at, updated_at")


class ValidationError(Exception):
    pass


# ---------------------------------------------------------------- validation

def _title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("标题不能为空")
    value = value.strip()
    if len(value) > TITLE_MAX:
        raise ValidationError("标题不能超过 %d 个字符" % TITLE_MAX)
    return value


def _details(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("details 必须是字符串")
    if len(value) > DETAILS_MAX:
        raise ValidationError("详情不能超过 %d 个字符" % DETAILS_MAX)
    return value


def _status(value):
    if value not in STATUSES:
        raise ValidationError("status 必须是 %s 之一" % "/".join(STATUSES))
    return value


def _priority(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise ValidationError("priority 必须是 0-3 的整数")
    return value


def _time(name, value):
    if value is None:
        return None
    try:
        return db.parse_time(value)
    except ValueError:
        raise ValidationError("%s 不是有效的 ISO-8601 时间" % name)


def _clean(data, partial, conn=None, user_id=None):
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    unknown = [k for k in data if k not in EDITABLE and k not in READ_ONLY]
    if unknown:
        raise ValidationError("不支持的字段：%s" % ", ".join(sorted(unknown)))
    if not partial and "title" not in data:
        raise ValidationError("标题不能为空")
    fields = {}
    if "title" in data:
        fields["title"] = _title(data["title"])
    if "details" in data:
        fields["details"] = _details(data["details"])
    if "status" in data:
        fields["status"] = _status(data["status"])
    if "priority" in data:
        fields["priority"] = _priority(data["priority"])
    for name in ("due_at", "remind_at"):
        if name in data:
            fields[name] = _time(name, data[name])
    if "list_id" in data:
        try:
            fields["list_id"] = lists.todo_list_id(conn, user_id, data["list_id"])
        except lists.ListError as exc:
            raise ValidationError(str(exc))
    return fields


def _warnings(todo):
    if todo["due_at"] and todo["remind_at"] and todo["remind_at"] > todo["due_at"]:
        return ["remind_at 晚于 due_at"]
    return []


# ---------------------------------------------------------------- todos

def _to_dict(row):
    return {k: row[k] for k in row.keys()}


def _detail(conn, row, user_id, base_url=""):
    todo = _to_dict(row)
    todo["notes"] = list_notes(conn, user_id, todo["id"])
    todo["ics_url"] = "/api/todos/%d/ics" % todo["id"]
    todo["google_calendar_url"] = calendar_ics.google_calendar_url(todo, base_url=base_url)
    return todo


def list_todos(conn, user_id, params=None, base_url=""):
    """Filtered, paginated list for one user. `params` is a dict of query strings."""
    params = params or {}
    where, args = ["user_id = ?"], [user_id]

    status = params.get("status") or "all"
    if status == "all":
        where.append("status != 'archived'")
    else:
        where.append("status = ?")
        args.append(_status(status))

    if params.get("list"):
        try:
            item = lists.get_list(conn, user_id, int(params["list"]))
        except ValueError:
            raise ValidationError("list 必须是整数")
        if item is None:
            raise ValidationError("列表不存在")
        if item["is_default"]:
            where.append("list_id IS NULL")
        else:
            where.append("list_id = ?")
            args.append(item["id"])

    q = params.get("q")
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(title LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\')")
        args += ["%" + escaped + "%"] * 2
    if params.get("due_before"):
        where.append("due_at IS NOT NULL AND due_at < ?")
        args.append(_time("due_before", params["due_before"]))
    if params.get("due_after"):
        where.append("due_at IS NOT NULL AND due_at > ?")
        args.append(_time("due_after", params["due_after"]))

    try:
        limit = int(params.get("limit") or LIMIT_DEFAULT)
        offset = int(params.get("offset") or 0)
    except ValueError:
        raise ValidationError("limit/offset 必须是整数")
    if limit < 1 or offset < 0:
        raise ValidationError("limit/offset 超出范围")
    limit = min(limit, LIMIT_MAX)

    order = params.get("order")
    if order and order not in ORDERS:
        raise ValidationError("order 必须是 %s 之一" % "/".join(ORDERS))
    order_sql = ORDERS[order] if order else DEFAULT_ORDER

    where_sql = " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) FROM todos WHERE " + where_sql, args).fetchone()[0]
    rows = conn.execute(
        "SELECT " + TODO_COLUMNS + ", "
        "(SELECT COUNT(*) FROM todo_notes n WHERE n.todo_id = todos.id) AS notes_count "
        "FROM todos WHERE " + where_sql + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return {"items": [_summary(r, base_url) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def _summary(row, base_url=""):
    """List/v1 item: the stored columns plus the export links."""
    todo = _to_dict(row)
    todo["ics_url"] = "/api/todos/%d/ics" % todo["id"]
    todo["google_calendar_url"] = calendar_ics.google_calendar_url(todo, base_url=base_url)
    return todo


def _fetch(conn, user_id, todo_id):
    return conn.execute("SELECT " + TODO_COLUMNS + " FROM todos WHERE id = ? AND user_id = ?",
                        (todo_id, user_id)).fetchone()


def get_todo(conn, user_id, todo_id, base_url=""):
    """Full todo with notes, or None when missing or owned by someone else."""
    row = _fetch(conn, user_id, todo_id)
    return _detail(conn, row, user_id, base_url) if row else None


def create_todo(conn, user_id, data, base_url=""):
    fields = _clean(data, partial=False, conn=conn, user_id=user_id)
    now = db.now_utc()
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO todos (user_id, title, details, status, priority, due_at, remind_at, "
            "reminder_sent, list_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (user_id, fields["title"], fields.get("details"), fields.get("status", "open"),
             fields.get("priority", 2), fields.get("due_at"), fields.get("remind_at"),
             fields.get("list_id"), now, now),
        )
    todo = get_todo(conn, user_id, cur.lastrowid, base_url)
    warnings = _warnings(todo)
    if warnings:
        todo["warnings"] = warnings
    return todo


def update_todo(conn, user_id, todo_id, data, base_url=""):
    """Partial update. Returns the updated todo, or None when not found for this user."""
    fields = _clean(data, partial=True, conn=conn, user_id=user_id)
    with db.tx(conn):
        current = _fetch(conn, user_id, todo_id)
        if current is None:
            return None
        if fields:
            if "remind_at" in fields and fields["remind_at"] != current["remind_at"]:
                fields["reminder_sent"] = 0
            fields["updated_at"] = db.now_utc()
            assignments = ", ".join("%s = ?" % k for k in fields)  # keys are whitelisted
            conn.execute("UPDATE todos SET " + assignments + " WHERE id = ? AND user_id = ?",
                         list(fields.values()) + [todo_id, user_id])
    todo = get_todo(conn, user_id, todo_id, base_url)
    warnings = _warnings(todo)
    if warnings:
        todo["warnings"] = warnings
    return todo


def delete_todo(conn, user_id, todo_id):
    """Delete (notes cascade). Returns True if a row belonging to user_id was removed."""
    with db.tx(conn):
        cur = conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id))
    return cur.rowcount == 1


# ---------------------------------------------------------------- notes

def list_notes(conn, user_id, todo_id):
    rows = conn.execute(
        "SELECT n.id, n.todo_id, n.content, n.created_at "
        "FROM todo_notes n JOIN todos t ON t.id = n.todo_id "
        "WHERE n.todo_id = ? AND t.user_id = ? ORDER BY n.created_at ASC, n.id ASC",
        (todo_id, user_id),
    ).fetchall()
    return [_to_dict(r) for r in rows]


def get_notes(conn, user_id, todo_id):
    """Notes for a todo, or None when the todo is missing or owned by someone else."""
    if _fetch(conn, user_id, todo_id) is None:
        return None
    items = list_notes(conn, user_id, todo_id)
    return {"items": items, "total": len(items)}


def add_note(conn, user_id, todo_id, data):
    """Append a note. Returns the note, or None when the todo is not the user's."""
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("内容不能为空")
    if len(content) > NOTE_MAX:
        raise ValidationError("内容不能超过 %d 个字符" % NOTE_MAX)
    with db.tx(conn):
        # INSERT ... SELECT only inserts when the todo belongs to user_id.
        cur = conn.execute(
            "INSERT INTO todo_notes (todo_id, content, created_at) "
            "SELECT id, ?, ? FROM todos WHERE id = ? AND user_id = ?",
            (content, db.now_utc(), todo_id, user_id),
        )
    if cur.rowcount != 1:
        return None
    row = conn.execute(
        "SELECT n.id, n.todo_id, n.content, n.created_at FROM todo_notes n "
        "JOIN todos t ON t.id = n.todo_id WHERE n.id = ? AND t.user_id = ?",
        (cur.lastrowid, user_id),
    ).fetchone()
    return _to_dict(row)
