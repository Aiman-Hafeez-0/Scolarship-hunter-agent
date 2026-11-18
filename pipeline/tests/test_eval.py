"""Tests for pipeline.eval scoring (no API calls: a fake provider stands in for the LLM)."""

import csv
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.eval import (
    CSV_HEADER,
    Golden,
    GoldenPage,
    append_results,
    csv_row,
    format_report,
    normalize_fee,
    pair_items,
    run_eval,
    score_page,
)
from pipeline.nodes.extract import extract_page
from pipeline.nodes.validate import ValidatedScholarship
from pipeline.schemas import DeadlineItem, PageExtraction, ScholarshipExtraction
from pipeline.tests.conftest import FakeProvider, make_source

TODAY = date(2026, 11, 15)
TEXT = (
    "Round 1 application deadline 27 September 2026. Round 2 application deadline "
    "3 January 2027. Decisions released 19 November 2026. Applications are free of charge."
)
FETCHED = date(2026, 9, 1)  # before every date on the page: all of them still ahead
GOLDEN = Golden.model_validate({
    "source": "kaust",
    "fetched_on": FETCHED.isoformat(),
    "items": [{
        "name": "KAUST Graduate Admission",
        "funding_type": "unknown",
        "open_to_pakistan": None,  # not scored
        "application_fee": "free",
        "deadlines": [
            {"label": "Round 1", "date": "2026-09-27", "quote": "27 September 2026"},
            {"label": "Round 2", "date": "2027-01-03", "quote": "3 January 2027"},
            {"label": "Decisions", "date": "2026-11-19", "quote": "19 November 2026",
             "optional": True},
        ],
    }],
})  # fmt: skip
PAGE = GoldenPage("kaust", TEXT, GOLDEN)


def item(name: str = "KAUST Admissions 2026-27", **fields: Any) -> ScholarshipExtraction:
    return ScholarshipExtraction(name=name, opportunity_type="scholarship", location="abroad",
                                 **fields)  # fmt: skip


def deadline(day: date, label: str = "Deadline") -> DeadlineItem:
    return DeadlineItem(label=label, deadline_date=day, evidence=TEXT)


def test_perfect_page() -> None:
    extracted = [ValidatedScholarship(item(
        funding_type="unknown", open_to_pakistan="yes", application_fee="Free",
        deadlines=[deadline(date(2026, 9, 27)), deadline(date(2027, 1, 3)),
                   deadline(date(2026, 11, 19))],  # the optional date costs nothing
    ))]  # fmt: skip

    result = score_page(PAGE, extracted)

    assert (result.deadlines.correct, result.deadlines.total) == (2, 2)
    assert result.fields["funding_type"].accuracy == 1.0
    assert result.fields["open_to_pakistan"].total == 0  # null in golden: skipped
    assert result.fields["application_fee"].accuracy == 1.0
    assert result.mismatches == []


def test_missed_and_invented_deadlines() -> None:
    extracted = [ValidatedScholarship(item(
        deadlines=[deadline(date(2026, 9, 27)), deadline(date(2026, 12, 1), "Made up")],
    ))]  # fmt: skip

    result = score_page(PAGE, extracted)

    # 1 correct out of 2 required + 1 invented
    assert (result.deadlines.correct, result.deadlines.total) == (1, 3)
    assert "deadline missed: 2027-01-03 'Round 2'" in result.mismatches
    assert any(m.startswith("deadline invented: 2026-12-01 'Made up'") for m in result.mismatches)


def test_missing_item_counts_scored_fields_wrong() -> None:
    result = score_page(PAGE, [])
    assert result.fields["funding_type"].total == 1 and result.fields["funding_type"].correct == 0
    assert result.fields["application_fee"].correct == 0
    assert result.deadlines.total == 2


def test_items_are_paired_by_name() -> None:
    golden = Golden.model_validate({"source": "x", "fetched_on": "2026-11-14", "items": [
        {"name": "USAID Merit and Needs based Scholarship", "funding_type": "full",
         "open_to_pakistan": "yes", "application_fee": None},
        {"name": "Rizvee-Kidwai Scholarship", "funding_type": "tuition_only",
         "open_to_pakistan": "unclear", "application_fee": None},
    ]})  # fmt: skip
    extracted = [ValidatedScholarship(item("Rizvee Kidwai Scholarship for CS")),
                 ValidatedScholarship(item("USAID MNBSP Scholarship Program"))]  # fmt: skip

    pairs = pair_items(golden.items, extracted)

    assert [(e.name[:5], x and x.item.name[:5]) for e, x in pairs] == [
        ("USAID", "USAID"),
        ("Rizve", "Rizve"),
    ]


def test_normalize_fee() -> None:
    assert normalize_fee("PKR 5,000") == normalize_fee("Rs. 5000") == "5000"
    assert normalize_fee("free of charge") == normalize_fee("EUR 0") == "0"
    assert normalize_fee(None) is None and normalize_fee("  ") is None
    assert normalize_fee("€30") != normalize_fee("€50")


def test_run_eval_report_and_csv(tmp_path: Path) -> None:
    answer = PageExtraction(scholarships=[item(
        funding_type="partial", application_fee="free",
        deadlines=[DeadlineItem(label="Round 1", deadline_date=date(2026, 9, 27),
                                evidence="Round 1 application deadline 27 September 2026.")],
    )])  # fmt: skip
    provider = FakeProvider(answer)

    run = run_eval([PAGE], {"kaust": make_source(key="kaust", location="abroad")},
                   lambda text, src, day: extract_page(text, src, provider, day))  # fmt: skip
    report = format_report(run)

    assert "deadline date       1/2    50.0%" in report
    assert "funding_type        0/1    0.0%" in report
    assert "funding_type: 'KAUST Graduate Admission' expected 'unknown', got 'partial'" in report
    assert "deadline missed: 2027-01-03 'Round 2'" in report

    path = tmp_path / "results.csv"
    append_results(csv_row(run, TODAY), path)
    append_results(csv_row(run, TODAY, "groq"), path)
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == CSV_HEADER
    assert rows[1][:8] == ["2026-11-15", "auto", "fake-model", "extract_v3", "1", "1", "2",
                           "0.5000"]  # fmt: skip
    assert rows[2][1] == "groq"
    assert len(rows) == 3  # header written once
    assert provider.todays == [FETCHED]  # the model was given the page's fetch date


def test_deadline_passed_on_fetch_date_is_optional() -> None:
    late = GoldenPage("kaust", TEXT, GOLDEN.model_copy(update={"fetched_on": date(2026, 10, 1)}))

    without = score_page(late, [ValidatedScholarship(item(deadlines=[deadline(date(2027, 1, 3))]))])
    assert (without.deadlines.correct, without.deadlines.total) == (1, 1)  # Round 1 not needed

    with_it = score_page(late, [ValidatedScholarship(item(
        deadlines=[deadline(date(2026, 9, 27)), deadline(date(2027, 1, 3))]))])  # fmt: skip
    assert (with_it.deadlines.correct, with_it.deadlines.total) == (1, 1)  # and not invented


def test_open_to_pakistan_is_not_scored_for_pakistan_sources() -> None:
    golden = GOLDEN.model_copy(deep=True)
    golden.items[0].open_to_pakistan = "yes"
    page = GoldenPage("kaust", TEXT, golden)
    extracted = [ValidatedScholarship(item(open_to_pakistan="unclear"))]

    assert score_page(page, extracted, "abroad").fields["open_to_pakistan"].total == 1
    assert score_page(page, extracted, "pakistan").fields["open_to_pakistan"].total == 0


def test_csv_has_provider_column() -> None:
    assert CSV_HEADER[:3] == ["date", "provider", "model"]
