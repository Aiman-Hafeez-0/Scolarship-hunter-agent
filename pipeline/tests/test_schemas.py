"""Tests for pipeline.schemas."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from pipeline.schemas import PageExtraction, ScholarshipExtraction, llm_json_schema

MINIMAL = {"name": "KAUST Fellowship", "opportunity_type": "scholarship", "location": "abroad"}


def test_minimal_scholarship_gets_safe_defaults() -> None:
    item = ScholarshipExtraction.model_validate(MINIMAL)

    assert item.funding_type == "unknown"
    assert item.open_to_pakistan == "unclear"
    assert item.moi_accepted == "unclear"
    assert item.status == "unknown"
    assert item.deadlines == [] and item.requirements == [] and item.coverage is None


def test_full_page_parses_from_json() -> None:
    page = PageExtraction.model_validate_json(
        json.dumps(
            {
                "scholarships": [
                    {
                        **MINIMAL,
                        "degree_levels": ["master", "phd"],
                        "funding_type": "full",
                        "coverage": {"monthly_stipend": "2,500", "stipend_currency": "USD"},
                        "deadlines": [
                            {
                                "label": "Fall 2027 application deadline",
                                "deadline_date": "2027-01-15",
                                "evidence": "Applications close on January 15, 2027.",
                            }
                        ],
                        "requirements": [
                            {
                                "kind": "english_test",
                                "value": "IELTS 6.5",
                                "description": "Minimum IELTS score",
                                "evidence": "IELTS 6.5 or above is required.",
                            }
                        ],
                    }
                ]
            }
        )
    )
    [item] = page.scholarships
    assert item.deadlines[0].deadline_date == date(2027, 1, 15)
    assert item.deadlines[0].is_estimated is False
    assert item.coverage is not None and item.coverage.stipend_currency == "USD"


def test_empty_page_is_valid() -> None:
    assert PageExtraction.model_validate_json('{"scholarships": []}').scholarships == []


@pytest.mark.parametrize(
    "bad",
    [
        {**MINIMAL, "location": "mars"},
        {**MINIMAL, "funding_type": "lots"},
        {**MINIMAL, "degree_levels": ["kindergarten"]},
        {**MINIMAL, "deadlines": [{"label": "x", "deadline_date": "15 Jan"}]},  # no evidence
        {**MINIMAL, "made_up_field": 1},
    ],
    ids=["location", "funding", "degree", "deadline-missing-evidence", "unknown-field"],
)
def test_bad_values_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScholarshipExtraction.model_validate(bad)


def test_llm_schema_is_self_contained() -> None:
    schema = llm_json_schema()
    text = json.dumps(schema)

    assert "$ref" not in text and "$defs" not in text
    item_schema = schema["properties"]["scholarships"]["items"]
    assert "deadlines" in item_schema["properties"]
    assert "evidence" in item_schema["properties"]["deadlines"]["items"]["properties"]
