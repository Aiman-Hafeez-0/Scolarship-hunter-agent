"""Show deadlines in Pakistan time (PKT, UTC+5), with the page's own time zone in brackets.

A deadline is saved as a date plus the time and zone exactly as the page wrote them
(timezone_note, e.g. "23:59 CET"). We convert to PKT only when both the time and the zone are
clear. Ambiguous zones (PST can be Pakistan or US Pacific, IST can be India or Ireland) are
never guessed: the note is shown as written instead.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

PKT = timezone(timedelta(hours=5), "PKT")

# Zone names we are sure about -> hours from UTC. Matched as whole words, ignoring case.
ZONES: dict[str, float] = {
    "pakistan standard time": 5, "pakistan time": 5, "pkt": 5,
    "central european summer time": 2, "central european time": 1, "cest": 2, "cet": 1,
    "eastern european summer time": 3, "eastern european time": 2, "eest": 3, "eet": 2,
    "western european summer time": 1, "west": 1, "wet": 0,
    "british summer time": 1, "bst": 1,
    "gulf standard time": 4, "gst": 4,
    "moscow time": 3, "msk": 3, "trt": 3,
    "japan standard time": 9, "jst": 9, "korea standard time": 9, "kst": 9,
    "singapore time": 8, "sgt": 8, "hkt": 8,
    "aest": 10, "aedt": 11, "nzst": 12, "nzdt": 13,
    "est": -5, "edt": -4, "pdt": -7, "mst": -7,
    "utc": 0, "gmt": 0, "coordinated universal time": 0,
}  # fmt: skip

_OFFSET = re.compile(r"\b(utc|gmt)\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
_NAMED = re.compile(
    r"\b(" + "|".join(re.escape(z) for z in sorted(ZONES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_HH_MM = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(a\.?m\.?|p\.?m\.?)?", re.IGNORECASE)
_H_AMPM = re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)", re.IGNORECASE)
_HHMM_HRS = re.compile(r"\b(\d{2})(\d{2})\s*hrs\b", re.IGNORECASE)
_NOON = re.compile(r"\bnoon\b", re.IGNORECASE)


@dataclass(frozen=True)
class ShownDeadline:
    """A deadline ready to show: its date in PKT and the text to print."""

    pkt_date: date
    text: str


def format_day(day: date) -> str:
    """date(2026, 11, 27) -> "Fri 27 Nov 2026"."""
    return f"{day:%a} {day.day} {day:%b %Y}"


def _hour(hour: int, ampm: str | None) -> int:
    if ampm is None:
        return hour
    pm = ampm.lower().startswith("p")
    return (hour % 12) + (12 if pm else 0)


def parse_time(note: str) -> time | None:
    """The time in a note ("23:59 CET" -> 23:59, "5 pm" -> 17:00), or None."""
    hour: int | None = None
    minute = 0
    if match := _HH_MM.search(note):
        hour, minute = _hour(int(match.group(1)), match.group(3)), int(match.group(2))
    elif match := _H_AMPM.search(note):
        hour = _hour(int(match.group(1)), match.group(2))
    elif match := _HHMM_HRS.search(note):
        hour, minute = int(match.group(1)), int(match.group(2))
    elif _NOON.search(note):
        hour = 12
    if hour is None or hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_zone(note: str) -> tuple[str, timedelta] | None:
    """(zone as written, offset from UTC) for a zone we are sure about, else None."""
    if match := _OFFSET.search(note):
        sign = 1 if match.group(2) == "+" else -1
        offset = timedelta(hours=int(match.group(3)), minutes=int(match.group(4) or 0))
        return match.group(0), sign * offset
    if match := _NAMED.search(note):
        return match.group(1), timedelta(hours=ZONES[match.group(1).lower()])
    return None


def show_deadline(day: date, note: str | None) -> ShownDeadline:
    """The deadline in PKT with the original zone in brackets, as far as the note allows.

    "23:59 CET" on 14 Jan  -> "Fri 15 Jan 2027, 03:59 PKT (23:59 CET, Thu 14 Jan 2027)"
    "16:00 PKT" on 27 Nov  -> "Fri 27 Nov 2026, 16:00 PKT"
    no note                -> "Fri 27 Nov 2026 (no time given)"
    """
    note = " ".join((note or "").split())
    if not note:
        return ShownDeadline(day, f"{format_day(day)} (no time given)")
    at, zone = parse_time(note), parse_zone(note)
    if at is None or zone is None:
        return ShownDeadline(day, f"{format_day(day)} (page says: {note})")

    zone_text, offset = zone
    original = datetime.combine(day, at, tzinfo=timezone(offset))
    pkt = original.astimezone(PKT)
    shown = f"{format_day(pkt.date())}, {pkt:%H:%M} PKT"
    if offset == timedelta(hours=5):
        return ShownDeadline(pkt.date(), shown)
    bracket = f"{at:%H:%M} {zone_text}"
    if pkt.date() != day:
        bracket += f", {format_day(day)}"
    return ShownDeadline(pkt.date(), f"{shown} ({bracket})")


def days_left_text(days: int) -> str:
    if days == 0:
        return "closes today"
    if days == 1:
        return "1 day left"
    return f"{days} days left"
