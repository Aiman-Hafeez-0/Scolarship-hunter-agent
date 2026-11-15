"""Tests for pipeline.nodes.match (pure part) and the match output of pipeline.run."""

from datetime import date

from pipeline.matching.rules import RequirementFact, ScholarshipFacts
from pipeline.nodes.match import match_all
from pipeline.profile import Profile
from pipeline.run import format_matches

TODAY = date(2026, 11, 15)
ME = Profile(full_name="Test Student", email="s@example.com", date_of_birth=date(2000, 1, 1),
             cgpa=3.4, target_fields=["Artificial Intelligence"], moi_letter=True)  # fmt: skip


def scholarships() -> list[ScholarshipFacts]:
    return [
        ScholarshipFacts(
            name="AI Fellowship", id="1", open_to_pakistan="yes", degree_levels=["master"],
            fields_of_study=["Artificial Intelligence"], funding="full",
            application_fee="free", deadlines=[date(2026, 11, 25)],
        ),
        ScholarshipFacts(
            name="Med Grant", id="2", open_to_pakistan="yes", degree_levels=["master"],
            fields_of_study=["Medicine"], funding="full", deadlines=[date(2027, 3, 1)],
        ),
        ScholarshipFacts(
            name="Unclear Programme", id="3", degree_levels=["master"],
            fields_of_study=["All fields"], deadlines=[date(2027, 1, 10)],
            requirements=[RequirementFact("english_test", "IELTS 6.5", "IELTS 6.5")],
        ),
        ScholarshipFacts(name="Old Call", id="4", status="closed"),
    ]  # fmt: skip


def test_match_all_skips_closed_and_sorts_by_score() -> None:
    matches = match_all([("p1", ME)], scholarships(), TODAY)

    assert [(m.scholarship.name, m.status, m.score) for m in matches] == [
        ("AI Fellowship", "eligible", 100),  # 40 + 25 + 20 + 15 (+5 free, capped)
        ("Unclear Programme", "maybe", 52),  # 24 + 12.5 + 8 (56 days away) + 7.5
        ("Med Grant", "not_eligible", 29),  # field fails: 0 + 25 + 4 + 0
    ]
    english = next(r for r in matches[1].reasons if r.rule == "english")
    assert english.outcome == "unknown" and english.action == "take IELTS or TOEFL"


def test_format_matches() -> None:
    text = format_matches(match_all([("p1", ME)], scholarships(), TODAY), top=1)
    lines = text.splitlines()

    assert lines[0] == "Matches for Test Student: 3 (1 eligible, 1 maybe, 1 not_eligible)"
    assert lines[3].split()[:3] == ["100", "eligible", "scholarship"]
    assert "1. AI Fellowship  (score 100, eligible)" in text
    assert "Unclear Programme  (score" not in text  # only the top 1 gets reasons
    assert "pass     open_deadline    next deadline 2026-11-25 (in 10 days)" in text


def test_format_no_matches_explains_next_step() -> None:
    assert "fill in profile.yaml" in format_matches([])
