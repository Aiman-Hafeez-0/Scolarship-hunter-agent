"""An .ics calendar file for one deadline ("Add to calendar" on the detail page).

The format is iCalendar (RFC 5545), which Google Calendar, Outlook and Apple Calendar all
open. The deadline is an all-day event with two reminders: 7 days and 1 day before.
"""

import re
from datetime import UTC, date, datetime, timedelta

PRODUCT = "-//Scholarship Hunter//Dashboard//EN"
MAX_LINE_OCTETS = 75


def escape(text: str) -> str:
    """Text as iCalendar wants it: backslash, semicolon, comma and newlines escaped."""
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def fold(line: str) -> str:
    """Split a long line into pieces of at most 75 bytes; each next piece starts with a space.
    Never splits inside a multi-byte character (e.g. "ü")."""
    pieces: list[str] = []
    current = ""
    for char in line:
        limit = MAX_LINE_OCTETS - (1 if pieces else 0)  # the leading space counts too
        if len((current + char).encode("utf-8")) > limit:
            pieces.append(current)
            current = char
        else:
            current += char
    pieces.append(current)
    return "\r\n ".join(pieces)


def filename(name: str, day: date) -> str:
    """A safe file name, e.g. 'stipendium-hungaricum-2027-01-15.ics'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-") or "deadline"
    return f"{slug}-{day.isoformat()}.ics"


def deadline_event(
    *, uid: str, name: str, label: str, day: date, url: str, now: datetime | None = None
) -> str:
    """The whole .ics file for one all-day deadline."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    description = f"{label} for {name}.\nOfficial page: {url}\nAdded from Scholarship Hunter."
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODUCT}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{escape(uid)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{escape(f'{label}: {name}')}",
        f"DESCRIPTION:{escape(description)}",
        f"URL:{url}",
        "TRANSP:TRANSPARENT",
    ]
    for days_before in (7, 1):
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape(f'{label} in {days_before} day(s): {name}')}",
            f"TRIGGER:-P{days_before}D",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
