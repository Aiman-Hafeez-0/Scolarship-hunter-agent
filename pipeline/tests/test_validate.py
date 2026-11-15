"""Tests for pipeline.nodes.validate: quote check, date check and past dates."""

from datetime import date
from typing import Any

import pytest

from pipeline.nodes.validate import (
    date_in_quote,
    is_closed,
    quote_score,
    status_after_dates,
    validate_scholarship,
)
from pipeline.schemas import Coverage, DeadlineItem, Requirement, ScholarshipExtraction

TODAY = date(2026, 11, 15)
PAGE = """
Erasmus Mundus Joint Master in Example Studies

The application deadline is   15 January 2027 at 23:59 CET.
Scholarship holders receive a monthly allowance of EUR 1,400.
Applicants must hold a bachelor degree with a GPA of at least 3.0.
"""


def item(**fields: Any) -> ScholarshipExtraction:
    return ScholarshipExtraction(
        name="EMJM Example", opportunity_type="scholarship", location="abroad", **fields
    )


def deadline(evidence: str, day: date | None, label: str = "Application deadline") -> DeadlineItem:
    return DeadlineItem(label=label, deadline_date=day, evidence=evidence)


# ---------- Quote check ----------


def test_exact_quote_scores_100_even_with_different_spacing() -> None:
    assert quote_score("The application deadline is 15 January\n2027", PAGE) == 100


def test_nearly_exact_quote_passes() -> None:
    # One typo fixed by the model: still clearly the same sentence.
    assert quote_score("Scholarship holders recieve a monthly allowance of EUR 1,400.", PAGE) >= 90


def test_invented_quote_fails() -> None:
    assert quote_score("Applicants must be under 30 years old.", PAGE) < 90
    assert quote_score("", PAGE) == 0
    assert quote_score(None, PAGE) == 0


def test_failed_quote_drops_the_fact_and_flags_review() -> None:
    good = deadline("The application deadline is 15 January 2027", date(2027, 1, 15))
    invented = deadline("Second round closes on 1 March 2027", date(2027, 3, 1), "Round 2")
    real_req = Requirement(kind="cgpa_min", value="3.0", description="GPA 3.0",
                           evidence="with a GPA of at least 3.0")  # fmt: skip
    fake_req = Requirement(kind="age_max", value="30", description="Under 30",
                           evidence="Applicants must be under 30 years old.")  # fmt: skip

    result = validate_scholarship(
        item(deadlines=[invented, good], requirements=[fake_req, real_req]), PAGE, TODAY
    )

    assert [d.label for d in result.item.deadlines] == ["Application deadline"]
    assert [r.kind for r in result.item.requirements] == ["cgpa_min"]
    assert result.needs_review
    assert any("deadlines[0] (Round 2) quote not found" in note for note in result.notes)
    # Evidence is saved with its score, indexed by the fact's position after dropping.
    assert [(e.field_path, e.score) for e in result.evidence] == [
        ("deadlines[0]", 100),
        ("requirements[0]", 100),
    ]


def test_single_facts_without_a_real_quote_fall_back() -> None:
    result = validate_scholarship(
        item(
            coverage=Coverage(monthly_stipend="1400", evidence="a monthly allowance of EUR 1,400"),
            application_fee="EUR 50",
            application_fee_evidence="The application fee is EUR 50.",
            open_to_pakistan="yes",  # no quote at all
        ),
        PAGE,
        TODAY,
    )

    assert result.item.coverage is not None  # its quote is on the page
    assert result.item.application_fee is None
    assert result.item.open_to_pakistan == "unclear"
    assert result.needs_review
    assert [e.field_path for e in result.evidence] == ["coverage"]


def test_clean_item_needs_no_review() -> None:
    result = validate_scholarship(
        item(deadlines=[deadline("deadline is 15 January 2027", date(2027, 1, 15))]), PAGE, TODAY
    )
    assert not result.needs_review and result.notes == []


# ---------- Date check ----------


@pytest.mark.parametrize(
    "quote",
    [
        "The application deadline is 15 January 2027.",  # English
        "Deadline: January 15th, 2027",
        "Scadenza per le candidature: 15 gennaio 2027, ore 12:00",  # Italian
        "Başvurular 15 Ocak 2027 tarihinde sona erer.",  # Turkish
        "SON BAŞVURU: 15 OCAK 2027",
        "Deadline: 15.01.2027",  # numbers
        "Deadline: 2027-01-15",
        "Deadline: 01/15/2027",
    ],
)
def test_date_found_in_quote(quote: str) -> None:
    assert date_in_quote(date(2027, 1, 15), quote)


@pytest.mark.parametrize(
    ("day", "quote"),
    [
        (date(2027, 2, 28), "Başvurular 28 Şubat 2027 tarihine kadar açıktır."),
        (date(2027, 5, 3), "Son başvuru tarihi: 3 Mayıs 2027"),
        (date(2027, 8, 31), "31 Ağustos 2027'ye kadar"),
        (date(2027, 3, 1), "Le domande vanno presentate entro il 1° marzo 2027"),
        (date(2027, 12, 6), "entro il 6 dicembre 2027"),
    ],
)
def test_date_with_accents_and_turkish_letters(day: date, quote: str) -> None:
    assert date_in_quote(day, quote)


@pytest.mark.parametrize(
    "quote",
    [
        "The application deadline is 15 February 2027.",  # wrong month
        "The application deadline is 16 January 2027.",  # wrong day
        "Scadenza: 15 febbraio 2027",
        "Son başvuru: 15 Şubat 2027",
        "Deadline: 15.02.2027",
        "Applications close in January 2027.",  # no day at all
        "Applications close on 2015-01-20",  # 15 only inside the year
    ],
)
def test_date_not_in_quote(quote: str) -> None:
    assert not date_in_quote(date(2027, 1, 15), quote)


def test_unverified_date_is_removed_but_deadline_kept() -> None:
    page = "Applications close on 15 January 2027."
    wrong = deadline("Applications close on 15 January 2027.", date(2027, 1, 25))

    result = validate_scholarship(item(deadlines=[wrong]), page, TODAY)

    [kept] = result.item.deadlines
    assert kept.deadline_date is None
    assert result.needs_review
    assert "date 2027-01-25 not written in its quote" in result.notes[0]


# ---------- Past dates ----------


def test_deadline_before_today_is_closed() -> None:
    assert is_closed(deadline("x", date(2026, 11, 14)), TODAY)
    assert not is_closed(deadline("x", TODAY), TODAY)  # the last day is still open
    assert not is_closed(deadline("x", None), TODAY)


def test_all_deadlines_passed_means_closed() -> None:
    past = [deadline("a", date(2026, 3, 1)), deadline("b", date(2026, 10, 1))]
    assert status_after_dates(item(status="open", deadlines=past), TODAY) == "closed"


def test_one_future_deadline_keeps_status() -> None:
    mixed = [deadline("a", date(2026, 3, 1)), deadline("b", date(2027, 1, 15))]
    assert status_after_dates(item(status="open", deadlines=mixed), TODAY) == "open"


def test_announced_new_cycle_is_not_closed() -> None:
    past = [deadline("a", date(2026, 3, 1))]
    assert status_after_dates(item(status="upcoming", deadlines=past), TODAY) == "upcoming"


def test_no_dated_deadlines_keeps_status() -> None:
    assert status_after_dates(item(status="open"), TODAY) == "open"
    undated = [deadline("a", None)]
    assert status_after_dates(item(status="unknown", deadlines=undated), TODAY) == "unknown"


def test_validate_sets_closed_status() -> None:
    page = "Applications closed on 1 March 2026."
    old = deadline("Applications closed on 1 March 2026.", date(2026, 3, 1))
    result = validate_scholarship(item(status="open", deadlines=[old]), page, TODAY)
    assert result.item.status == "closed"
