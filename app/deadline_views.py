"""The two views of the Deadlines page: by week (This week, Next week, Later) and a month
calendar. Pure functions: deadlines in, groups out (weeks start on Monday)."""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.queries import UpcomingDeadline


@dataclass
class WeekGroup:
    key: str  # "this-week", "next-week", "later"
    title: str
    hint: str  # e.g. "17 - 23 Nov"
    deadlines: list[UpcomingDeadline]


def _span(start: date, end: date) -> str:
    return f"{start.day} {start:%b} - {end.day} {end:%b}"


def week_groups(deadlines: list[UpcomingDeadline], today: date) -> list[WeekGroup]:
    """Deadlines (sorted by date) in three groups. "This week" runs from today to Sunday."""
    sunday = today + timedelta(days=6 - today.weekday())
    next_sunday = sunday + timedelta(days=7)
    groups = [
        WeekGroup("this-week", "This week", _span(today, sunday), []),
        WeekGroup("next-week", "Next week", _span(sunday + timedelta(days=1), next_sunday), []),
        WeekGroup("later", "Later", f"from {next_sunday + timedelta(days=1):%d %b}", []),
    ]
    for d in deadlines:
        if d.deadline_date <= sunday:
            groups[0].deadlines.append(d)
        elif d.deadline_date <= next_sunday:
            groups[1].deadlines.append(d)
        else:
            groups[2].deadlines.append(d)
    return groups


@dataclass
class CalendarDay:
    day: date
    in_month: bool
    is_today: bool
    deadlines: list[UpcomingDeadline] = field(default_factory=list)


@dataclass
class MonthView:
    title: str  # "December 2026"
    month: date  # the 1st of the month
    previous: date | None  # None: we do not show months before the current one
    next: date
    weeks: list[list[CalendarDay]]

    @property
    def busy_days(self) -> list[CalendarDay]:
        """The days of this month that have a deadline (for the phone list)."""
        return [d for week in self.weeks for d in week if d.in_month and d.deadlines]


def parse_month(text: str | None, today: date) -> date:
    """'2026-12' -> 1 Dec 2026. Missing, broken or past months give the current month."""
    current = today.replace(day=1)
    try:
        year, month = (int(part) for part in (text or "").split("-"))
        chosen = date(year, month, 1)
    except ValueError:
        return current
    return max(chosen, current)


def _add_months(first: date, months: int) -> date:
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_view(deadlines: list[UpcomingDeadline], month: date, today: date) -> MonthView:
    """A Monday-first grid of the month, each day with its deadlines."""
    by_day: dict[date, list[UpcomingDeadline]] = {}
    for d in deadlines:
        by_day.setdefault(d.deadline_date, []).append(d)
    weeks = [
        [CalendarDay(day, day.month == month.month, day == today, by_day.get(day, []))
         for day in week]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
    ]  # fmt: skip
    previous = _add_months(month, -1)
    return MonthView(
        title=f"{calendar.month_name[month.month]} {month.year}",
        month=month,
        previous=previous if previous >= today.replace(day=1) else None,
        next=_add_months(month, 1),
        weeks=weeks,
    )
