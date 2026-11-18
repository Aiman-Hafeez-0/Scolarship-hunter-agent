"""Tests for pipeline.nodes.validate: quote check, date check and past dates."""

from datetime import date
from typing import Any

import pytest

from pipeline.nodes.validate import (
    date_in_quote,
    intake_start,
    is_closed,
    quote_score,
    status_after_dates,
    validate_extraction,
    validate_scholarship,
)
from pipeline.schemas import (
    Coverage,
    DeadlineItem,
    PageExtraction,
    Requirement,
    ScholarshipExtraction,
)

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


def test_no_dated_deadline_means_unknown() -> None:
    # Without a dated deadline we cannot say when to act: never "open" or "upcoming".
    assert status_after_dates(item(status="open"), TODAY) == "unknown"
    assert status_after_dates(item(status="upcoming"), TODAY) == "unknown"
    undated = [deadline("a", None)]
    assert status_after_dates(item(status="open", deadlines=undated), TODAY) == "unknown"
    assert status_after_dates(item(status="closed"), TODAY) == "closed"  # the page said so


def test_validate_sets_closed_status() -> None:
    page = "Applications closed on 1 March 2026."
    old = deadline("Applications closed on 1 March 2026.", date(2026, 3, 1))
    result = validate_scholarship(item(status="open", deadlines=[old]), page, TODAY)
    assert result.item.status == "closed"


# ---------- Intake and degree evidence ----------

INTAKE_PAGE = "MS Admissions Spring 2027 are open.\nFall 2026 intake: classes began in September."


def test_intake_and_degree_levels_need_evidence() -> None:
    good = item(intake="Spring 2027", intake_evidence="MS Admissions Spring 2027 are open.",
                degree_levels=["master"], degree_levels_evidence="MS Admissions Spring 2027")
    result = validate_scholarship(good, INTAKE_PAGE, TODAY)
    assert (result.item.intake, result.item.degree_levels) == ("Spring 2027", ["master"])
    assert {e.field_path for e in result.evidence} == {"intake", "degree_levels"}
    assert not result.needs_review

    missing = item(intake="Spring 2027", degree_levels=["phd"],
                   degree_levels_evidence="PhD programmes for all")  # not on the page
    result = validate_scholarship(missing, INTAKE_PAGE, TODAY)
    assert (result.item.intake, result.item.degree_levels) == (None, [])
    assert result.needs_review
    assert any(n.startswith("intake has no quote") for n in result.notes)


@pytest.mark.parametrize(
    ("intake", "start"),
    [
        ("Fall 2026", date(2026, 9, 1)),
        ("Autumn 2026", date(2026, 9, 1)),
        ("Spring 2027", date(2027, 2, 1)),
        ("spring-2026", date(2026, 2, 1)),
        ("Fall 2026 and Spring 2027", date(2027, 2, 1)),  # the latest season counts
        ("2026-27", None),
        (None, None),
    ],
)
def test_intake_start(intake: str | None, start: date | None) -> None:
    assert intake_start(intake) == start


def test_started_intake_is_closed() -> None:
    fall = item(status="open", intake="Fall 2026",
                intake_evidence="Fall 2026 intake: classes began in September.")
    assert validate_scholarship(fall, INTAKE_PAGE, TODAY).item.status == "closed"

    spring = item(status="open", intake="Spring 2027",
                  intake_evidence="MS Admissions Spring 2027 are open.",
                  deadlines=[deadline("MS Admissions Spring 2027 are open.", None)])
    assert validate_scholarship(spring, INTAKE_PAGE, TODAY).item.status == "unknown"  # no date


def test_started_intake_with_deadline_ahead_needs_review() -> None:
    page = INTAKE_PAGE + "\nApply by 15 January 2027."
    odd = item(status="open", intake="Fall 2026",
               intake_evidence="Fall 2026 intake: classes began in September.",
               deadlines=[deadline("Apply by 15 January 2027.", date(2027, 1, 15))])
    result = validate_scholarship(odd, page, TODAY)
    assert result.item.status == "open"
    assert result.needs_review
    assert any("has started, but deadline 2027-01-15 is still ahead" in n for n in result.notes)


# ---------- open_to_pakistan from the source's location ----------


def test_sources_in_pakistan_are_open_to_pakistan_without_a_quote() -> None:
    extraction = PageExtraction(scholarships=[item(open_to_pakistan="unclear")])
    [result] = validate_extraction(extraction, PAGE, TODAY, location="pakistan")
    assert result.item.open_to_pakistan == "yes"
    assert result.notes == []


def test_abroad_sources_still_need_a_quote_for_yes() -> None:
    claimed = item(open_to_pakistan="yes", open_to_pakistan_evidence="Open to Pakistan.")
    extraction = PageExtraction(scholarships=[claimed])
    [result] = validate_extraction(extraction, PAGE, TODAY, location="abroad")
    assert result.item.open_to_pakistan == "unclear"  # the quote is not on the page
