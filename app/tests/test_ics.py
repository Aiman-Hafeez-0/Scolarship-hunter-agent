"""Tests for app.ics: the "Add to calendar" file."""

from datetime import UTC, date, datetime

from app import ics

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def _event(**overrides: object) -> str:
    values: dict[str, object] = {
        "uid": "abc-0-2027-01-15@scholarship-hunter", "name": "Stipendium Hungaricum",
        "label": "Application deadline", "day": date(2027, 1, 15),
        "url": "https://apply.example.org", "now": NOW,
    }  # fmt: skip
    values.update(overrides)
    return ics.deadline_event(**values)  # type: ignore[arg-type]


def test_event_is_an_all_day_deadline_with_reminders() -> None:
    text = _event()
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20270115" in lines
    assert "DTEND;VALUE=DATE:20270116" in lines  # all-day: ends the next day
    assert "DTSTAMP:20261119T100000Z" in lines
    assert "SUMMARY:Application deadline: Stipendium Hungaricum" in lines
    assert "URL:https://apply.example.org" in lines
    assert "TRIGGER:-P7D" in lines and "TRIGGER:-P1D" in lines
    assert text.count("BEGIN:VEVENT") == 1


def test_text_is_escaped() -> None:
    assert ics.escape("a, b; c\\d\ne") == "a\\, b\\; c\\\\d\\ne"
    text = _event(name="MS, PhD; Fall")
    assert "SUMMARY:Application deadline: MS\\, PhD\\; Fall" in text


def test_long_lines_are_folded_at_75_bytes() -> None:
    text = _event(name="Ü" * 100)  # 2 bytes each: folding must not split one
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    unfolded = text.replace("\r\n ", "")
    assert "Ü" * 100 in unfolded


def test_filename() -> None:
    assert ics.filename("Stipendium Hungaricum!", date(2027, 1, 15)) == (
        "stipendium-hungaricum-2027-01-15.ics"
    )
    assert ics.filename("???", date(2027, 1, 15)) == "deadline-2027-01-15.ics"
