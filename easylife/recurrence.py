"""Repeating tasks: iCalendar RRULE values and the next date of a repeating task.

A task's `repeat` column holds the RRULE value as written in iCalendar (for example
"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"), so rules set on the iPhone sync back unchanged.
EasyLife computes the next date for FREQ / INTERVAL / weekly BYDAY / UNTIL / COUNT;
other parts are kept but ignored when moving a task to its next date.
"""

import calendar
import datetime
import re

from easylife import db

FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
RULE_MAX = 300
_RULE_RE = re.compile(r"^[A-Z]+=[A-Z0-9,+\-:]+(;[A-Z]+=[A-Z0-9,+\-:]+)*$")
_MAX_STEPS = 5000

# Times are moved in this zone so "every day at 09:00" stays at 09:00 local time.
_zone = datetime.timezone(datetime.timedelta(hours=8))


class RuleError(ValueError):
    pass


def set_zone(tz):
    global _zone
    _zone = tz


def parse(rule):
    """'FREQ=WEEKLY;INTERVAL=2' -> {'FREQ': 'WEEKLY', 'INTERVAL': '2'}; RuleError if invalid."""
    if not isinstance(rule, str):
        raise RuleError("重复规则必须是字符串")
    text = rule.strip().upper()
    if text.startswith("RRULE:"):
        text = text[6:]
    if not text or len(text) > RULE_MAX or not _RULE_RE.match(text):
        raise RuleError("重复规则无效")
    parts = dict(item.split("=", 1) for item in text.split(";"))
    if parts.get("FREQ") not in FREQS:
        raise RuleError("只支持按天、周、月、年重复")
    if "INTERVAL" in parts and not re.fullmatch(r"[1-9]\d{0,2}", parts["INTERVAL"]):
        raise RuleError("重复间隔无效")
    if "COUNT" in parts and not re.fullmatch(r"[1-9]\d{0,3}", parts["COUNT"]):
        raise RuleError("重复次数无效")
    if "UNTIL" in parts and not re.fullmatch(r"\d{8}(T\d{6}Z?)?", parts["UNTIL"]):
        raise RuleError("重复结束日期无效")
    if "BYDAY" in parts and parts["FREQ"] == "WEEKLY":
        if any(day not in DAYS for day in parts["BYDAY"].split(",")):
            raise RuleError("重复的星期无效")
    return parts


def normalize(rule):
    """Canonical text of a valid rule, or None for empty / None."""
    if rule is None or (isinstance(rule, str) and not rule.strip()):
        return None
    parts = parse(rule)
    return ";".join("%s=%s" % item for item in parts.items())


def _until(value):
    """UNTIL as a UTC datetime (a date-only UNTIL includes that whole local day)."""
    if "T" in value:
        naive = datetime.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            return naive
        return naive.replace(tzinfo=_zone).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    day = datetime.datetime.strptime(value, "%Y%m%d").replace(hour=23, minute=59, second=59)
    return day.replace(tzinfo=_zone).astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _add_months(local, months, day):
    index = local.month - 1 + months
    year, month = local.year + index // 12, index % 12 + 1
    day = min(day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


def _step(local, parts, anchor_day):
    """The occurrence after `local` (an aware local datetime)."""
    freq = parts["FREQ"]
    interval = int(parts.get("INTERVAL", "1"))
    if freq == "DAILY":
        return local + datetime.timedelta(days=interval)
    if freq == "WEEKLY":
        days = [DAYS.index(d) for d in parts.get("BYDAY", "").split(",") if d in DAYS]
        if not days:
            return local + datetime.timedelta(weeks=interval)
        week_start = (local - datetime.timedelta(days=local.weekday())).date()
        candidate = local
        for _ in range(7 * interval + 7):
            candidate = candidate + datetime.timedelta(days=1)
            weeks = ((candidate.date() - datetime.timedelta(days=candidate.weekday())) - week_start).days // 7
            if candidate.weekday() in days and weeks % interval == 0:
                return candidate
        return local + datetime.timedelta(weeks=interval)
    if freq == "MONTHLY":
        return _add_months(local, interval, anchor_day)
    return _add_months(local, 12 * interval, anchor_day)


def next_due(due_at, rule, now=None):
    """Next date of a repeating task after it was completed.

    Returns (new_due_at, new_rule), or None when the rule has ended. The new date is
    the first occurrence after both the old date and `now`, so an overdue daily task
    moves to its next upcoming day instead of a day that has already passed.
    """
    parts = parse(rule)
    now = now or db.now_utc()
    start = datetime.datetime.strptime(due_at, db.TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
    local = start.astimezone(_zone)
    anchor_day = int(parts["BYMONTHDAY"]) if re.fullmatch(r"[1-9]|[12]\d|3[01]",
                                                          parts.get("BYMONTHDAY", "")) else local.day
    until = _until(parts["UNTIL"]) if "UNTIL" in parts else None
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    steps = 0
    while True:
        local = _step(local, parts, anchor_day)
        steps += 1
        utc = local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        if until is not None and utc > until:
            return None
        if count is not None and count - steps < 1:
            return None
        if utc.strftime(db.TIME_FORMAT) > now or steps >= _MAX_STEPS:
            break
    if count is not None:
        parts["COUNT"] = str(count - steps)
    return utc.strftime(db.TIME_FORMAT), ";".join("%s=%s" % item for item in parts.items())


def local_label(stored):
    """'2026-10-20 10:00:00' (UTC) -> '10月20日 18:00' in the local zone."""
    utc = datetime.datetime.strptime(stored, db.TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
    local = utc.astimezone(_zone)
    return "%d月%d日 %02d:%02d" % (local.month, local.day, local.hour, local.minute)
