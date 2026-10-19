"""Todo and note business logic. Every query is scoped by user_id (DESIGN.md §3.5-3.7, §4.3).

Reminders follow the iPhone model: a task with a due time is reminded at that time
(`due_alert_sent`), and `remind_at` is an optional earlier alert ("提前提醒",
`reminder_sent`). Urgent tasks keep being reminded after the due time (`next_nag_at`,
`nag_count`) until they are done or the reminders are stopped.
"""

import datetime

from easylife import calendar_ics, db, lists, recurrence

STATUSES = ("open", "doing", "done", "archived")
TITLE_MAX = 200
DETAILS_MAX = 20000
NOTE_MAX = 10000
TAG_MAX = 30
TAGS_MAX = 10
IMAGES_MAX = 9
IMAGE_MAX_BYTES = 5 * 1024 * 1024
STALE_SECONDS = 600         # alerts this far in the past when a time is set are not mailed
URGENT_INTERVAL = 600       # seconds between reminders of an urgent task after its due time
URGENT_MAX = 36             # at most this many repeated reminders (6 hours at 10 minutes)
LIMIT_DEFAULT, LIMIT_MAX = 100, 500
ORDERS = {
    "due_at": "due_at IS NULL, due_at ASC, id DESC",
    "priority": "priority DESC, due_at IS NULL, due_at ASC, id DESC",
    "created_at": "created_at DESC, id DESC",
    "updated_at": "updated_at DESC, id DESC",
}
DEFAULT_ORDER = "priority DESC, due_at IS NULL, due_at ASC, id DESC"
EDITABLE = ("title", "details", "status", "priority", "due_at", "remind_at", "list_id",
            "repeat", "urgent", "flagged", "parent_id", "tags")
# Fields a client may echo back from a GET; ignored rather than rejected.
READ_ONLY = ("id", "user_id", "reminder_sent", "created_at", "updated_at",
             "notes", "notes_count", "ics_url", "google_calendar_url", "warnings",
             "due_alert_sent", "nag_count", "next_nag_at", "subtasks", "subtask_total",
             "subtask_done", "image_count", "images", "parent", "repeated")

TODO_COLUMNS = ("id, title, details, status, priority, due_at, remind_at, reminder_sent, "
                "list_id, created_at, updated_at, due_alert_sent, repeat, urgent, flagged, "
                "parent_id, nag_count, next_nag_at")
COUNT_COLUMNS = (
    "(SELECT COUNT(*) FROM todo_notes n WHERE n.todo_id = todos.id) AS notes_count, "
    "(SELECT COUNT(*) FROM todos c WHERE c.parent_id = todos.id AND c.status != 'archived') "
    "AS subtask_total, "
    "(SELECT COUNT(*) FROM todos c WHERE c.parent_id = todos.id AND c.status = 'done') "
    "AS subtask_done, "
    "(SELECT COUNT(*) FROM todo_images i WHERE i.todo_id = todos.id) AS image_count")
BOOLEANS = ("urgent", "flagged")
IMAGE_TYPES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


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


def _flag(name, value):
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return value
    raise ValidationError("%s 必须是 true 或 false" % name)


def clean_tag(value):
    if not isinstance(value, str):
        raise ValidationError("标签必须是文字")
    tag = " ".join(value.strip().lstrip("#＃").split())
    if not tag:
        return None
    if len(tag) > TAG_MAX or "," in tag or "，" in tag:
        raise ValidationError("标签不能超过 %d 个字，也不能包含逗号" % TAG_MAX)
    return tag


def _tags(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("tags 必须是列表")
    tags, seen = [], set()
    for item in value:
        tag = clean_tag(item)
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    if len(tags) > TAGS_MAX:
        raise ValidationError("每个任务最多 %d 个标签" % TAGS_MAX)
    return tags


def _parent_id(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 63:
        raise ValidationError("parent_id 必须是任务编号")
    return value


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
    if "repeat" in data:
        try:
            fields["repeat"] = recurrence.normalize(data["repeat"])
        except recurrence.RuleError as exc:
            raise ValidationError(str(exc))
    for name in BOOLEANS:
        if name in data:
            fields[name] = _flag(name, data[name])
    if "parent_id" in data:
        fields["parent_id"] = _parent_id(data["parent_id"])
    if "tags" in data:
        fields["tags"] = _tags(data["tags"])
    return fields


def _warnings(todo):
    if todo["due_at"] and todo["remind_at"] and todo["remind_at"] > todo["due_at"]:
        return ["remind_at 晚于 due_at"]
    return []


def alert_pending(at):
    """1 when an alert at `at` should still be mailed, else 0 (None / long past)."""
    return 0 if at and at > db.now_utc(-STALE_SECONDS) else 1


def _check_parent(conn, user_id, parent_id, todo_id=None):
    """The parent row for a new parent_id; subtasks are one level deep, like Reminders."""
    if parent_id is None:
        return None
    if parent_id == todo_id:
        raise ValidationError("任务不能成为自己的子任务")
    parent = _fetch(conn, user_id, parent_id)
    if parent is None:
        raise ValidationError("上级任务不存在")
    if parent["parent_id"] is not None:
        raise ValidationError("子任务下面不能再添加子任务")
    if todo_id is not None and conn.execute(
            "SELECT 1 FROM todos WHERE parent_id = ? AND user_id = ? LIMIT 1",
            (todo_id, user_id)).fetchone():
        raise ValidationError("已有子任务的任务不能再成为子任务")
    return parent


# ---------------------------------------------------------------- todos

def _to_dict(row):
    todo = {k: row[k] for k in row.keys()}
    for name in BOOLEANS:
        if name in todo:
            todo[name] = bool(todo[name])
    return todo


def tags_of(conn, ids):
    """{todo_id: [tag, ...]} in the order the tags were added."""
    found = {}
    ids = list(ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute("SELECT todo_id, tag FROM todo_tags WHERE todo_id IN (%s) ORDER BY rowid"
                            % ",".join("?" * len(chunk)), chunk).fetchall()
        for row in rows:
            found.setdefault(row["todo_id"], []).append(row["tag"])
    return found


def _set_tags(conn, todo_id, tags):
    conn.execute("DELETE FROM todo_tags WHERE todo_id = ?", (todo_id,))
    conn.executemany("INSERT INTO todo_tags (todo_id, tag) VALUES (?, ?)",
                     [(todo_id, tag) for tag in tags])


def _detail(conn, row, user_id, base_url=""):
    todo = _to_dict(row)
    todo["notes"] = list_notes(conn, user_id, todo["id"])
    todo["tags"] = tags_of(conn, [todo["id"]]).get(todo["id"], [])
    todo["ics_url"] = "/api/todos/%d/ics" % todo["id"]
    todo["google_calendar_url"] = calendar_ics.google_calendar_url(todo, base_url=base_url)
    children = conn.execute(
        "SELECT " + TODO_COLUMNS + ", " + COUNT_COLUMNS + " FROM todos WHERE parent_id = ? "
        "AND user_id = ? ORDER BY status = 'archived', status = 'done', created_at, id",
        (todo["id"], user_id)).fetchall()
    child_tags = tags_of(conn, [c["id"] for c in children])
    todo["subtasks"] = [_summary(c, base_url, child_tags) for c in children]
    todo["subtask_total"] = sum(1 for c in children if c["status"] != "archived")
    todo["subtask_done"] = sum(1 for c in children if c["status"] == "done")
    todo["parent"] = None
    if todo["parent_id"] is not None:
        parent = _fetch(conn, user_id, todo["parent_id"])
        if parent is not None:
            todo["parent"] = {"id": parent["id"], "title": parent["title"],
                              "status": parent["status"]}
    todo["images"] = list_images(conn, user_id, todo["id"])
    todo["image_count"] = len(todo["images"])
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
    if params.get("flagged") in ("1", "true"):
        where.append("flagged = 1")
    if params.get("tag"):
        tag = clean_tag(params["tag"])
        where.append("id IN (SELECT todo_id FROM todo_tags WHERE tag = ?)")
        args.append(tag)

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
        "SELECT " + TODO_COLUMNS + ", " + COUNT_COLUMNS + " "
        "FROM todos WHERE " + where_sql + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    tags = tags_of(conn, [r["id"] for r in rows])
    return {"items": [_summary(r, base_url, tags) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def _summary(row, base_url="", tags=None):
    """List/v1 item: the stored columns plus the export links."""
    todo = _to_dict(row)
    todo["tags"] = (tags or {}).get(todo["id"], [])
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


def _remind_without_due(fields, current_due):
    """Old clients set only remind_at: that time becomes the due time (alerted there)."""
    if fields.get("remind_at") and not fields.get("due_at", current_due):
        fields["due_at"], fields["remind_at"] = fields["remind_at"], None


def create_todo(conn, user_id, data, base_url=""):
    fields = _clean(data, partial=False, conn=conn, user_id=user_id)
    _remind_without_due(fields, None)
    if fields.get("repeat") and not fields.get("due_at"):
        raise ValidationError("设置重复前请先设置时间")
    now = db.now_utc()
    with db.tx(conn):
        parent = _check_parent(conn, user_id, fields.get("parent_id"))
        if parent is not None:
            fields["list_id"] = parent["list_id"]
        cur = conn.execute(
            "INSERT INTO todos (user_id, title, details, status, priority, due_at, remind_at, "
            "reminder_sent, list_id, created_at, updated_at, due_alert_sent, repeat, urgent, "
            "flagged, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, fields["title"], fields.get("details"), fields.get("status", "open"),
             fields.get("priority", 2), fields.get("due_at"), fields.get("remind_at"),
             fields.get("list_id"), now, now, alert_pending(fields.get("due_at")),
             fields.get("repeat"), fields.get("urgent", 0), fields.get("flagged", 0),
             fields.get("parent_id")),
        )
        if fields.get("tags"):
            _set_tags(conn, cur.lastrowid, fields["tags"])
    todo = get_todo(conn, user_id, cur.lastrowid, base_url)
    warnings = _warnings(todo)
    if warnings:
        todo["warnings"] = warnings
    return todo


def _advance_repeat(conn, user_id, todo_id, merged, fields):
    """Completing a repeating task moves it to its next date instead (like Reminders).

    Returns {'from': old due, 'to': new due}, or None when the rule has ended."""
    try:
        result = recurrence.next_due(merged["due_at"], merged["repeat"])
    except recurrence.RuleError:
        return None
    if result is None:
        return None
    new_due, new_rule = result
    fields.update(status="open", due_at=new_due, repeat=new_rule, due_alert_sent=0,
                  reminder_sent=0, nag_count=0, next_nag_at=None)
    if merged["remind_at"]:
        fmt = db.TIME_FORMAT
        shift = (datetime.datetime.strptime(new_due, fmt)
                 - datetime.datetime.strptime(merged["due_at"], fmt))
        fields["remind_at"] = (datetime.datetime.strptime(merged["remind_at"], fmt)
                               + shift).strftime(fmt)
    conn.execute("INSERT INTO todo_notes (todo_id, content, created_at) VALUES (?, ?, ?)",
                 (todo_id, "✓ 完成了一次（原定 %s），下次：%s" % (
                     recurrence.local_label(merged["due_at"]), recurrence.local_label(new_due)),
                  db.now_utc()))
    return {"from": merged["due_at"], "to": new_due}


def update_todo(conn, user_id, todo_id, data, base_url=""):
    """Partial update. Returns the updated todo, or None when not found for this user."""
    fields = _clean(data, partial=True, conn=conn, user_id=user_id)
    repeated = None
    with db.tx(conn):
        current = _fetch(conn, user_id, todo_id)
        if current is None:
            return None
        _remind_without_due(fields, current["due_at"])
        if "due_at" in fields and fields["due_at"] is None:
            fields["repeat"] = None   # a task without a date cannot repeat
        tags = fields.pop("tags", None)
        if "parent_id" in fields and fields["parent_id"] != current["parent_id"]:
            parent = _check_parent(conn, user_id, fields["parent_id"], todo_id)
            if parent is not None:
                fields["list_id"] = parent["list_id"]
        elif current["parent_id"] is not None and "list_id" in fields:
            fields.pop("list_id")     # a subtask stays in its parent's list
        merged = dict(current)
        merged.update(fields)
        if merged["repeat"] and not merged["due_at"]:
            raise ValidationError("设置重复前请先设置时间")
        if "due_at" in fields and fields["due_at"] != current["due_at"]:
            fields.update(due_alert_sent=alert_pending(fields["due_at"]), nag_count=0,
                          next_nag_at=None)
        if "remind_at" in fields and fields["remind_at"] != current["remind_at"]:
            fields["reminder_sent"] = 0
        if "urgent" in fields and fields["urgent"] != current["urgent"]:
            fields["nag_count"] = 0
            fields["next_nag_at"] = None
            sent = fields.get("due_alert_sent", current["due_alert_sent"])
            if (fields["urgent"] and sent and merged["status"] in ("open", "doing")
                    and merged["due_at"] and merged["due_at"] <= db.now_utc()):
                fields["next_nag_at"] = db.now_utc()   # already overdue: start right away
        if merged["status"] in ("done", "archived"):
            fields["next_nag_at"] = None
        if (fields.get("status") == "done" and current["status"] != "done"
                and merged["repeat"] and merged["due_at"]):
            repeated = _advance_repeat(conn, user_id, todo_id, merged, fields)
        if fields or tags is not None:
            fields["updated_at"] = db.now_utc()
            assignments = ", ".join("%s = ?" % k for k in fields)  # keys are whitelisted
            conn.execute("UPDATE todos SET " + assignments + " WHERE id = ? AND user_id = ?",
                         list(fields.values()) + [todo_id, user_id])
            if "list_id" in fields and fields["list_id"] != current["list_id"]:
                conn.execute("UPDATE todos SET list_id = ?, updated_at = ? "
                             "WHERE parent_id = ? AND user_id = ?",
                             (fields["list_id"], fields["updated_at"], todo_id, user_id))
            if tags is not None:
                _set_tags(conn, todo_id, tags)
    todo = get_todo(conn, user_id, todo_id, base_url)
    warnings = _warnings(todo)
    if warnings:
        todo["warnings"] = warnings
    if repeated:
        todo["repeated"] = repeated
    return todo


def stop_alerts(conn, user_id, todo_id, base_url=""):
    """Stop the repeated reminders of an urgent task until its date changes."""
    with db.tx(conn):
        cur = conn.execute("UPDATE todos SET next_nag_at = NULL WHERE id = ? AND user_id = ?",
                           (todo_id, user_id))
    return get_todo(conn, user_id, todo_id, base_url) if cur.rowcount == 1 else None


def delete_todo(conn, user_id, todo_id):
    """Delete a todo and its subtasks (notes, tags and images cascade).

    Returns True if a row belonging to user_id was removed."""
    with db.tx(conn):
        if _fetch(conn, user_id, todo_id) is None:
            return False
        # Subtasks first: the foreign key would otherwise detach them (ON DELETE SET NULL).
        conn.execute("DELETE FROM todos WHERE parent_id = ? AND user_id = ?", (todo_id, user_id))
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


# ---------------------------------------------------------------- images

def image_type(data):
    """The MIME type of a JPEG / PNG / GIF / WebP image, else None (SVG is never accepted)."""
    for magic, mime in IMAGE_TYPES:
        if data.startswith(magic):
            return mime
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def list_images(conn, user_id, todo_id):
    rows = conn.execute(
        "SELECT id, mime, size, created_at FROM todo_images WHERE todo_id = ? AND user_id = ? "
        "ORDER BY id", (todo_id, user_id)).fetchall()
    return [dict(_to_dict(r), url="api/images/%d" % r["id"]) for r in rows]


def add_image(conn, user_id, todo_id, data):
    """Store one image. Returns its summary, or None when the todo is not the user's."""
    if not data:
        raise ValidationError("图片是空的")
    if len(data) > IMAGE_MAX_BYTES:
        raise ValidationError("图片不能超过 %d MB" % (IMAGE_MAX_BYTES // 1024 // 1024))
    mime = image_type(data)
    if mime is None:
        raise ValidationError("只支持 JPEG、PNG、GIF、WebP 图片")
    with db.tx(conn):
        if _fetch(conn, user_id, todo_id) is None:
            return None
        count = conn.execute("SELECT COUNT(*) FROM todo_images WHERE todo_id = ?",
                             (todo_id,)).fetchone()[0]
        if count >= IMAGES_MAX:
            raise ValidationError("每个任务最多 %d 张图片" % IMAGES_MAX)
        now = db.now_utc()
        cur = conn.execute(
            "INSERT INTO todo_images (todo_id, user_id, mime, size, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (todo_id, user_id, mime, len(data), data, now))
        conn.execute("UPDATE todos SET updated_at = ? WHERE id = ?", (now, todo_id))
    return {"id": cur.lastrowid, "mime": mime, "size": len(data), "created_at": now,
            "url": "api/images/%d" % cur.lastrowid}


def get_image(conn, user_id, image_id):
    """(mime, bytes) of one of the user's images, or None."""
    row = conn.execute("SELECT mime, data FROM todo_images WHERE id = ? AND user_id = ?",
                       (image_id, user_id)).fetchone()
    return (row["mime"], bytes(row["data"])) if row else None


def delete_image(conn, user_id, image_id):
    with db.tx(conn):
        row = conn.execute("SELECT todo_id FROM todo_images WHERE id = ? AND user_id = ?",
                           (image_id, user_id)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM todo_images WHERE id = ?", (image_id,))
        conn.execute("UPDATE todos SET updated_at = ? WHERE id = ?", (db.now_utc(), row["todo_id"]))
    return True
