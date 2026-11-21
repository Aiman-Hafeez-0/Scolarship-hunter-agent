"""Tests for app.deadline_views: week groups and the month calendar."""

from datetime import date

from app.deadline_views import month_view, parse_month, week_groups
from app.queries import UpcomingDeadline

TODAY = date(2026, 11, 19)  # a Thursday


def _d(day: date, name: str = "X") -> UpcomingDeadline:
    return UpcomingDeadline("id-" + name, name, "Apply", day, False)


def test_week_groups_this_week_ends_on_sunday() -> None:
    groups = week_groups([_d(date(2026, 11, 19), "today"), _d(date(2026, 11, 22), "sunday"),
                          _d(date(2026, 11, 23), "monday"), _d(date(2026, 11, 29), "next-sun"),
                          _d(date(2026, 11, 30), "later")], TODAY)  # fmt: skip
    assert [g.title for g in groups] == ["This week", "Next week", "Later"]
    assert [[d.scholarship_name for d in g.deadlines] for g in groups] == [
        ["today", "sunday"], ["monday", "next-sun"], ["later"]
    ]  # fmt: skip
    assert groups[0].hint == "19 Nov - 22 Nov"
    assert groups[1].hint == "23 Nov - 29 Nov"


def test_week_groups_on_a_sunday() -> None:
    sunday = date(2026, 11, 22)
    groups = week_groups([_d(date(2026, 11, 23))], sunday)
    assert [len(g.deadlines) for g in groups] == [0, 1, 0]  # Monday is already next week


def test_parse_month() -> None:
    assert parse_month("2027-01", TODAY) == date(2027, 1, 1)
    assert parse_month("2026-02", TODAY) == date(2026, 11, 1)  # past: the current month
    assert parse_month("nonsense", TODAY) == date(2026, 11, 1)
    assert parse_month("2027-13", TODAY) == date(2026, 11, 1)
    assert parse_month(None, TODAY) == date(2026, 11, 1)


def test_month_view_is_a_monday_first_grid() -> None:
    view = month_view([_d(date(2026, 12, 1), "a"), _d(date(2026, 12, 1), "b"),
                       _d(date(2027, 1, 5), "next month")], date(2026, 12, 1), TODAY)  # fmt: skip
    assert view.title == "December 2026"
    assert all(len(week) == 7 for week in view.weeks)
    assert view.weeks[0][0].day == date(2026, 11, 30)  # a Monday, from the month before
    assert not view.weeks[0][0].in_month
    first = view.weeks[0][1]
    assert first.day == date(2026, 12, 1) and [d.scholarship_name for d in first.deadlines] == [
        "a", "b"
    ]  # fmt: skip
    assert [d.day for d in view.busy_days] == [date(2026, 12, 1)]  # January is not counted
    assert view.previous == date(2026, 11, 1) and view.next == date(2027, 1, 1)


def test_month_view_has_no_way_back_before_this_month() -> None:
    view = month_view([], date(2026, 11, 1), TODAY)
    assert view.previous is None
    assert any(day.is_today for week in view.weeks for day in week)
