"""Tests for pipeline.matching.score."""

from datetime import date, timedelta
from typing import Any

import pytest

from pipeline.matching.rules import ScholarshipFacts
from pipeline.matching.score import field_factor, match_score, urgency_factor

TODAY = date(2026, 11, 15)


def facts(**fields: Any) -> ScholarshipFacts:
    return ScholarshipFacts(name="X", **fields)


def in_days(days: int) -> list[date]:
    return [TODAY + timedelta(days=days)]


def test_field_factor() -> None:
    assert field_factor(facts(fields_of_study=["Machine Learning"])) == 1.0
    assert field_factor(facts(fields_of_study=["MSc in AI"])) == 1.0
    assert field_factor(facts(fields_of_study=["Software Engineering"])) == 0.6
    assert field_factor(facts(fields_of_study=["All available disciplines"])) == 0.6
    assert field_factor(facts(fields_of_study=["Medicine"])) == 0.0
    assert field_factor(facts()) == 0.0


@pytest.mark.parametrize(
    ("deadlines", "expected"),
    [(in_days(14), 1.0), (in_days(30), 0.7), (in_days(60), 0.4), (in_days(61), 0.2),
     (in_days(-1), 0.2), ([], 0.2)],
)  # fmt: skip
def test_urgency_factor(deadlines: list[date], expected: float) -> None:
    assert urgency_factor(facts(deadlines=deadlines), TODAY) == expected


def test_score_formula() -> None:
    # 40*1 + 25*1 + 20*1 + 15*1 = 100, +5 free is capped at 100
    best = facts(fields_of_study=["Data Science"], funding="full", deadlines=in_days(10),
                 application_fee="free")  # fmt: skip
    assert match_score(best, "eligible", TODAY) == 100

    # 40*0.6 + 25*0.5 + 20*0.7 + 15*0.5 = 24 + 12.5 + 14 + 7.5 = 58
    middle = facts(fields_of_study=["Computer Science"], deadlines=in_days(25))
    assert match_score(middle, "maybe", TODAY) == 58

    # 40*0 + 25*0.4 + 20*0.2 + 15*0 = 14, +5 free = 19
    weak = facts(funding="partial", application_fee="No application fee")
    assert match_score(weak, "not_eligible", TODAY) == 19
