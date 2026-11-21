"""Tests for app.merge: duplicates are shown once, with every source."""

from datetime import date

import pytest

from app.merge import merge_deadlines, merge_rows, similar_names, sources_of
from app.queries import UpcomingDeadline
from app.tests.conftest import row

DAY = date(2027, 2, 1)


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("HEC Entry Test", "HEC Entry Test Calendar", True),  # one name inside the other
        ("Stipendium Hungaricum", "Stipendium Hungaricum Scholarship", True),
        ("NUST MS Admissions 2027", "NUST MS Admission 2027", True),  # almost the same
        ("NUST MS Computer Science", "NUST MS Electrical Engineering", False),
        ("HEC", "HEC NAT", False),  # one word is too little to call them the same
        ("DAAD EPOS", "Erasmus Mundus", False),
        ("", "HEC", False),
    ],
)
def test_similar_names(a: str, b: str, same: bool) -> None:
    assert similar_names(a, b) is same
    assert similar_names(b, a) is same


def test_duplicates_are_merged_with_both_sources() -> None:
    rows = [
        row(id="a", name="HEC Entry Test", opportunity_type="entry_test", next_deadline=DAY,
            score=None, source_name="HEC tests page", source_url="https://hec.gov.pk/tests"),
        row(id="b", name="HEC Entry Test Calendar", opportunity_type="entry_test",
            next_deadline=DAY, score=None, source_name="HEC calendar",
            source_url="https://hec.gov.pk/calendar"),
        row(id="c", name="Stipendium Hungaricum"),
    ]  # fmt: skip

    merged = merge_rows(rows)

    assert [r.name for r in merged] == ["HEC Entry Test", "Stipendium Hungaricum"]
    hec = merged[0]
    assert [(s.scholarship_id, s.name, s.url) for s in hec.sources] == [
        ("a", "HEC tests page", "https://hec.gov.pk/tests"),
        ("b", "HEC calendar", "https://hec.gov.pk/calendar"),
    ]
    assert len(merged[1].sources) == 1  # not merged: just its own source


@pytest.mark.parametrize(
    "other",
    [
        {"next_deadline": date(2027, 3, 1)},  # another deadline
        {"opportunity_type": "admission"},  # another type
        {"next_deadline": None},  # no date: never merged
    ],
)
def test_not_merged_when_type_or_deadline_differ(other: dict[str, object]) -> None:
    first = row(id="a", name="HEC Entry Test", opportunity_type="entry_test", next_deadline=DAY)
    values: dict[str, object] = {"id": "b", "name": "HEC Entry Test Calendar",
                                 "opportunity_type": "entry_test", "next_deadline": DAY}
    second = row(**(values | other))
    assert len(merge_rows([first, second])) == 2


def test_two_undated_items_are_not_merged() -> None:
    rows = [row(id="a", name="HEC Entry Test", next_deadline=None),
            row(id="b", name="HEC Entry Test Calendar", next_deadline=None)]  # fmt: skip
    assert len(merge_rows(rows)) == 2


def test_the_best_row_is_kept() -> None:
    rows = [row(id="a", name="Grant Programme Long Name", next_deadline=DAY, score=40),
            row(id="b", name="Grant Programme", next_deadline=DAY, score=90)]  # fmt: skip
    [kept] = merge_rows(rows)
    assert (kept.id, kept.score) == ("b", 90)
    assert [s.scholarship_id for s in kept.sources] == ["b", "a"]


def test_sources_of_finds_the_group() -> None:
    rows = [row(id="a", name="HEC Entry Test", next_deadline=DAY),
            row(id="b", name="HEC Entry Test Calendar", next_deadline=DAY),
            row(id="c", name="Other")]  # fmt: skip
    assert [s.scholarship_id for s in sources_of(rows, "b")] == ["a", "b"]
    assert [s.scholarship_id for s in sources_of(rows, "c")] == ["c"]
    assert sources_of(rows, "zzz") == []


def test_source_name_falls_back_to_the_host() -> None:
    [merged] = merge_rows([row(provider=None, source_name="", source_url="https://x.edu/p")])
    assert merged.sources[0].name == "x.edu"


def test_duplicate_deadlines_are_shown_once() -> None:
    deadlines = [
        UpcomingDeadline("a", "HEC Entry Test", "Register", DAY, False, "entry_test"),
        UpcomingDeadline("b", "HEC Entry Test Calendar", "Register", DAY, False, "entry_test"),
        UpcomingDeadline("a", "HEC Entry Test", "Fee", DAY, False, "entry_test"),  # same item
        UpcomingDeadline("c", "HEC Entry Test", "Register", DAY, False, "admission"),
    ]
    merged = merge_deadlines(deadlines)
    assert [(d.scholarship_id, d.label, d.source_count) for d in merged] == [
        ("a", "Register", 2), ("a", "Fee", 1), ("c", "Register", 1)
    ]  # fmt: skip
    assert deadlines[0].source_count == 1  # the input is not changed
