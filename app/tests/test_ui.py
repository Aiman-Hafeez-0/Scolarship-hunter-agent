"""Tests for app.ui: the small helpers the templates use."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app import ui

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)  # 15:00 in Pakistan


def test_pkt_shows_pakistan_time() -> None:
    assert ui.pkt(datetime(2026, 11, 18, 3, 17, tzinfo=UTC)) == "18 Nov 2026, 08:17 PKT"
    assert ui.pkt(None) == "-"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=45), "45 minutes ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=1, hours=1), "yesterday"),
        (timedelta(days=3), "3 days ago"),
        (timedelta(days=65), "2 months ago"),
        (timedelta(days=800), "2 years ago"),
        (-timedelta(hours=5), "in 5 hours"),
        (-timedelta(days=1), "tomorrow"),
    ],
)
def test_relative_time(delta: timedelta, expected: str) -> None:
    assert ui.relative_time(NOW - delta, NOW) == expected


def test_days_are_counted_in_pakistan_time() -> None:
    # Both are about 25 hours ago; what counts is the calendar day in Pakistan.
    night = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)  # 19 Nov, 01:00 PKT
    late = datetime(2026, 11, 19, 21, 30, tzinfo=UTC)  # 20 Nov, 02:30 PKT
    assert ui.relative_time(night, late) == "yesterday"
    evening = datetime(2026, 11, 18, 18, 30, tzinfo=UTC)  # 18 Nov, 23:30 PKT
    after_midnight = datetime(2026, 11, 19, 19, 30, tzinfo=UTC)  # 20 Nov, 00:30 PKT
    assert ui.relative_time(evening, after_midnight) == "2 days ago"


def test_deadline_tone_red_7_days_amber_30() -> None:
    assert [ui.deadline_tone(d) for d in (0, 7, 8, 30, 31)] == [
        "red", "red", "amber", "amber", "gray"
    ]  # fmt: skip


def test_countdown_text() -> None:
    assert [ui.countdown_text(d) for d in (-1, 0, 1, 12)] == [
        "passed", "today", "1 day left", "12 days left"
    ]  # fmt: skip
    assert ui.days_left(date(2026, 11, 20), date(2026, 11, 19)) == 1


def test_funding_text_is_a_dash_for_tests_and_admissions() -> None:
    assert ui.funding_text("scholarship", "tuition_only") == "Tuition only"
    assert ui.funding_text("admission", "unknown") == "—"
    assert ui.funding_text("entry_test", None) == "—"


def test_quote_link_highlights_the_quote_on_the_page() -> None:
    url = "https://example.org/page#top"
    assert ui.quote_link(url, "Apply by 15 Jan-2027.") == (
        "https://example.org/page#:~:text=Apply%20by%2015%20Jan%2D2027."
    )
    long = "one two three four five six seven eight nine ten eleven twelve thirteen"
    assert ui.quote_link(url, long) == (
        "https://example.org/page#:~:text=one%20two%20three%20four%20five,"
        "nine%20ten%20eleven%20twelve%20thirteen"
    )
    assert ui.quote_link(url, "  ") == url


def test_initial() -> None:
    assert ui.initial("owner@example.com") == "O"
    assert ui.initial("_9lives@example.com") == "9"
    assert ui.initial("") == "?"


def test_field_errors_keep_the_first_error_per_field() -> None:
    errors = ["cgpa: too high", "cgpa: second one", "email: not valid", "something general"]
    assert ui.field_errors(errors) == {"cgpa": "too high", "email": "not valid"}


def test_asset_links_carry_a_fingerprint() -> None:
    link = ui.asset("app.css")
    assert link.startswith("/static/app.css?v=") and len(link) > len("/static/app.css?v=")
    assert ui.asset("missing.js") == "/static/missing.js"  # no file, no fingerprint


def test_built_css_is_committed_and_has_the_design_tokens() -> None:
    css = (ui.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "Manrope" in css  # the font
    assert ".text-title" in css and ".tabular-nums" in css  # type scale, aligned numbers
    assert ".dark" in css  # the dark mode toggle


def test_source_health() -> None:
    assert ui.source_health(False, 3, True, True) == "paused"  # paused wins
    assert ui.source_health(True, 2, True, True) == "failing"
    assert ui.source_health(True, 0, False, True) == "failing"  # blocked by robots.txt
    assert ui.source_health(True, 0, None, False) == "new"
    assert ui.source_health(True, 0, True, True) == "ok"


def test_duration() -> None:
    start = NOW
    assert ui.duration(start, start + timedelta(seconds=45)) == "45 s"
    assert ui.duration(start, start + timedelta(minutes=3, seconds=5)) == "3 min 05 s"
    assert ui.duration(start, start + timedelta(hours=1, minutes=2)) == "1 h 02 min"
    assert ui.duration(start, None) == "running"
    assert ui.duration(start, start - timedelta(seconds=5)) == "0 s"  # clock skew
