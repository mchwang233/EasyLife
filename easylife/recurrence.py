"""Repeating tasks: iCalendar RRULE values and the next date of a repeating task.

A task's `repeat` column holds the RRULE value as written in iCalendar (for example
"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"), so rules set on the iPhone sync back unchanged.
EasyLife computes the next date for the rules Reminders' 自定义 can make:
FREQ / INTERVAL / UNTIL / COUNT, BYDAY (weekly days, or "2MO" / "-1FR" in a month),
BYMONTHDAY, BYMONTH and BYSETPOS. Other parts are kept but ignored.
"""

import calendar
import datetime
import re

from easylife import db

FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
RULE_MAX = 300
_RULE_RE = re.compile(r"^[A-Z]+=[A-Z0-9,+\-:]+(;[A-Z]+=[A-Z0-9,+\-:]+)*$")
_BYDAY_RE = re.compile(r"^([+-]?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")
_MAX_STEPS = 5000
_MAX_EMPTY = 400          # periods in a row without a date (e.g. BYMONTHDAY=31 + BYMONTH=2)

# Times are moved in this zone so "every day at 09:00" stays at 09:00 local time.
_zone = datetime.timezone(datetime.timedelta(hours=8))


class RuleError(ValueError):
    pass


def set_zone(tz):
    global _zone
    _zone = tz


def _int_list(value, low, high, message):
    try:
        numbers = [int(item) for item in value.split(",")]
    except ValueError:
        raise RuleError(message)
    if not numbers or any(n == 0 or not low <= abs(n) <= high for n in numbers):
        raise RuleError(message)
    return numbers


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
    if "BYDAY" in parts:
        for token in parts["BYDAY"].split(","):
            match = _BYDAY_RE.match(token)
            if not match or (match.group(1) and parts["FREQ"] not in ("MONTHLY", "YEARLY")):
                raise RuleError("重复的星期无效")
    if "BYMONTHDAY" in parts:
        _int_list(parts["BYMONTHDAY"], 1, 31, "重复的日期无效")
    if "BYMONTH" in parts:
        if any(n < 0 for n in _int_list(parts["BYMONTH"], 1, 12, "重复的月份无效")):
            raise RuleError("重复的月份无效")
    if "BYSETPOS" in parts:
        _int_list(parts["BYSETPOS"], 1, 366, "重复的序号无效")
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


def _month_start(year, month, offset):
    index = month - 1 + offset
    return year + index // 12, index % 12 + 1


def _month_days(year, month, parts, anchor):
    """Dates in one month: BYMONTHDAY, else BYDAY (with ordinals), else the anchor day.

    Without BY rules the anchor day is clamped to the month (Jan 31 -> Feb 28), as before."""
    length = calendar.monthrange(year, month)[1]
    if "BYMONTHDAY" in parts:
        days = set()
        for n in (int(item) for item in parts["BYMONTHDAY"].split(",")):
            day = n if n > 0 else length + 1 + n
            if 1 <= day <= length:
                days.add(day)
        return [datetime.date(year, month, day) for day in sorted(days)]
    if "BYDAY" in parts:
        found = set()
        for token in parts["BYDAY"].split(","):
            ordinal, name = _BYDAY_RE.match(token).groups()
            matches = [day for day in range(1, length + 1)
                       if datetime.date(year, month, day).weekday() == DAYS.index(name)]
            if ordinal:
                index = int(ordinal)
                index = index - 1 if index > 0 else len(matches) + index
                matches = [matches[index]] if 0 <= index < len(matches) else []
            found.update(matches)
        return [datetime.date(year, month, day) for day in sorted(found)]
    return [datetime.date(year, month, min(anchor.day, length))]


def _set_pos(dates, parts):
    if "BYSETPOS" not in parts or not dates:
        return dates
    chosen = set()
    for n in (int(item) for item in parts["BYSETPOS"].split(",")):
        index = n - 1 if n > 0 else len(dates) + n
        if 0 <= index < len(dates):
            chosen.add(dates[index])
    return sorted(chosen)


def _period(anchor, parts, k):
    """Dates of the k-th period (each INTERVAL units apart) counted from the anchor's period."""
    freq = parts["FREQ"]
    step = k * int(parts.get("INTERVAL", "1"))
    if freq == "DAILY":
        return [anchor + datetime.timedelta(days=step)]
    if freq == "WEEKLY":
        monday = anchor - datetime.timedelta(days=anchor.weekday()) + datetime.timedelta(weeks=step)
        days = sorted({DAYS.index(d) for d in parts.get("BYDAY", "").split(",") if d in DAYS})
        days = days or [anchor.weekday()]
        return _set_pos([monday + datetime.timedelta(days=d) for d in days], parts)
    if freq == "MONTHLY":
        year, month = _month_start(anchor.year, anchor.month, step)
        return _set_pos(_month_days(year, month, parts, anchor), parts)
    year = anchor.year + step
    months = sorted({int(m) for m in parts["BYMONTH"].split(",")}) if "BYMONTH" in parts else [anchor.month]
    if "BYDAY" in parts and "BYMONTH" not in parts and "BYMONTHDAY" not in parts:
        months = list(range(1, 13))     # "the 2nd Monday" of the year's months
    dates = []
    for month in months:
        dates.extend(_month_days(year, month, parts, anchor))
    return _set_pos(dates, parts)


def _occurrences(local, parts):
    """Local datetimes after `local`, in order, keeping its time of day."""
    anchor, clock = local.date(), local.timetz()
    k, empty = 0, 0
    while empty < _MAX_EMPTY:
        dates = [d for d in _period(anchor, parts, k) if d > anchor]
        empty = 0 if dates else empty + 1
        for day in dates:
            yield datetime.datetime.combine(day, clock)
        k += 1


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
    until = _until(parts["UNTIL"]) if "UNTIL" in parts else None
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    steps = 0
    utc = None
    for occurrence in _occurrences(local, parts):
        steps += 1
        utc = occurrence.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        if until is not None and utc > until:
            return None
        if count is not None and count - steps < 1:
            return None
        if utc.strftime(db.TIME_FORMAT) > now or steps >= _MAX_STEPS:
            break
    if utc is None:
        return None
    if count is not None:
        parts["COUNT"] = str(count - steps)
    return utc.strftime(db.TIME_FORMAT), ";".join("%s=%s" % item for item in parts.items())


def local_label(stored):
    """'2026-10-20 10:00:00' (UTC) -> '10月20日 18:00' in the local zone."""
    utc = datetime.datetime.strptime(stored, db.TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
    local = utc.astimezone(_zone)
    return "%d月%d日 %02d:%02d" % (local.month, local.day, local.hour, local.minute)
