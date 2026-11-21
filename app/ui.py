"""Small helpers the page templates use: Pakistan times, "2 hours ago", countdowns, links.

They are pure functions (no database, no request), so they are easy to test. app/main.py
registers them as Jinja filters and globals.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pipeline.notify.deadlines import PKT

STATIC_DIR = Path(__file__).resolve().parent / "static"

Tone = Literal["red", "amber", "gray"]


def pkt(value: datetime | None) -> str:
    """A time shown in Pakistan time, e.g. '18 Nov 2026, 08:17 PKT'."""
    return value.astimezone(PKT).strftime("%d %b %Y, %H:%M PKT") if value else "-"


def day(value: date | None) -> str:
    """A date like '15 Jan 2027'."""
    return value.strftime("%d %b %Y") if value else "no date"


def label(value: str | None) -> str:
    """A database word as text: 'entry_test' -> 'entry test'."""
    return (value or "-").replace("_", " ")


def as_json(value: Any) -> str:
    return "-" if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def relative_time(value: datetime, now: datetime | None = None) -> str:
    """How long ago (or how soon) a time is, in words: 'just now', '2 hours ago', 'in 3 days'.

    Days are counted in Pakistan time, so 'yesterday' means the day before today in PKT.
    """
    now = now or datetime.now(UTC)
    seconds = int((now - value).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        text = _plural(seconds // 60, "minute")
    elif seconds < 86400:
        text = _plural(seconds // 3600, "hour")
    else:
        days = abs((now.astimezone(PKT).date() - value.astimezone(PKT).date()).days)
        if days == 1:
            return "tomorrow" if future else "yesterday"
        if days < 30:
            text = _plural(days, "day")
        elif days < 365:
            text = _plural(days // 30, "month")
        else:
            text = _plural(days // 365, "year")
    return f"in {text}" if future else f"{text} ago"


def days_left(deadline: date, today: date) -> int:
    return (deadline - today).days


def deadline_tone(days: int) -> Tone:
    """The countdown color: red at 7 days or less, amber at 30 or less, else gray."""
    if days <= 7:
        return "red"
    return "amber" if days <= 30 else "gray"


def countdown_text(days: int) -> str:
    """'today', '1 day left', '12 days left'; 'passed' for a day already gone."""
    if days < 0:
        return "passed"
    if days == 0:
        return "today"
    return f"{_plural(days, 'day')} left"


FIELD_ERROR = re.compile(r"^([a-z_]+): (.+)$")


def field_errors(errors: list[str]) -> dict[str, str]:
    """Form errors by field, to show under each input: 'cgpa: too high' -> {'cgpa': 'too high'}.

    Lines that do not start with a field name stay only in the list above the form.
    """
    found: dict[str, str] = {}
    for error in errors:
        match = FIELD_ERROR.match(error)
        if match:
            found.setdefault(match.group(1), match.group(2))
    return found


def initial(email: str) -> str:
    """The avatar letter: the first letter or digit of the email, upper case ('?' if none)."""
    return next((c.upper() for c in email if c.isalnum()), "?")


@cache
def _file_version(path: Path) -> str:
    """A short fingerprint of a file's content (empty if the file is missing)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        return ""


def asset(name: str) -> str:
    """The link to a file in app/static, with its fingerprint: '/static/app.css?v=3f2a...'.

    A new build of the file gets a new link, so browsers never keep an old copy.
    """
    version = _file_version(STATIC_DIR / name)
    return f"/static/{name}" + (f"?v={version}" if version else "")
