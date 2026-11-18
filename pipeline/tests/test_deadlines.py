"""Tests for showing deadlines in Pakistan time (PKT)."""

from datetime import date, time, timedelta

import pytest

from pipeline.notify.deadlines import (
    days_left_text,
    format_day,
    parse_time,
    parse_zone,
    show_deadline,
)


def test_format_day() -> None:
    assert format_day(date(2026, 11, 27)) == "Fri 27 Nov 2026"


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("23:59 CET", time(23, 59)),
        ("16:00 HRS (PAKISTAN STANDARD TIME)", time(16, 0)),
        ("5 pm GMT", time(17, 0)),
        ("11.30 a.m. PKT", time(11, 30)),
        ("12 am UTC", time(0, 0)),
        ("1700 hrs", time(17, 0)),
        ("12 noon (UTC)", time(12, 0)),
        ("midnight", None),
        ("CET", None),
    ],
)
def test_parse_time(note: str, expected: time | None) -> None:
    assert parse_time(note) == expected


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("23:59 CET", ("CET", timedelta(hours=1))),
        ("16:00 HRS (PAKISTAN STANDARD TIME)", ("PAKISTAN STANDARD TIME", timedelta(hours=5))),
        ("23:59 UTC+2", ("UTC+2", timedelta(hours=2))),
        ("10:00 GMT-5:30", ("GMT-5:30", -timedelta(hours=5, minutes=30))),
        ("14:00 CEST", ("CEST", timedelta(hours=2))),
        ("17:00 PST", None),  # Pakistan or US Pacific: never guessed
        ("17:00 IST", None),
        ("17:00", None),
    ],
)
def test_parse_zone(note: str, expected: tuple[str, timedelta] | None) -> None:
    assert parse_zone(note) == expected


def test_converts_to_pkt_and_shows_original_zone() -> None:
    shown = show_deadline(date(2027, 1, 14), "12:00 CET")
    assert shown.pkt_date == date(2027, 1, 14)
    assert shown.text == "Thu 14 Jan 2027, 16:00 PKT (12:00 CET)"


def test_conversion_that_moves_to_the_next_day_shows_original_date() -> None:
    shown = show_deadline(date(2027, 1, 14), "23:59 CET")
    assert shown.pkt_date == date(2027, 1, 15)
    assert shown.text == "Fri 15 Jan 2027, 03:59 PKT (23:59 CET, Thu 14 Jan 2027)"


def test_conversion_that_moves_to_the_previous_day() -> None:
    shown = show_deadline(date(2027, 3, 1), "02:00 UTC")
    assert shown.pkt_date == date(2027, 3, 1)
    shown = show_deadline(date(2027, 3, 1), "01:00 AEST")
    assert shown.pkt_date == date(2027, 2, 28)


def test_pakistan_time_needs_no_bracket() -> None:
    shown = show_deadline(date(2026, 11, 27), "16:00 HRS (PAKISTAN STANDARD TIME)")
    assert shown.text == "Fri 27 Nov 2026, 16:00 PKT"


def test_no_note_says_no_time_given() -> None:
    assert show_deadline(date(2026, 11, 27), None).text == "Fri 27 Nov 2026 (no time given)"
    assert show_deadline(date(2026, 11, 27), "  ").text == "Fri 27 Nov 2026 (no time given)"


def test_unclear_note_is_shown_as_written() -> None:
    shown = show_deadline(date(2026, 11, 27), "17:00 PST")
    assert shown.pkt_date == date(2026, 11, 27)
    assert shown.text == "Fri 27 Nov 2026 (page says: 17:00 PST)"


def test_days_left_text() -> None:
    assert days_left_text(0) == "closes today"
    assert days_left_text(1) == "1 day left"
    assert days_left_text(9) == "9 days left"
