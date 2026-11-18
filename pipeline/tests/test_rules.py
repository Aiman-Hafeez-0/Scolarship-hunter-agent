"""Tests for pipeline.matching.rules: every rule's pass, fail and unknown cases."""

from datetime import date
from typing import Any

import pytest

from pipeline.matching.rules import (
    RequirementFact,
    RuleResult,
    ScholarshipFacts,
    age_rule,
    cgpa_on_4,
    cgpa_rule,
    degree_level_rule,
    english_rule,
    evaluate,
    fee_rule,
    field_rule,
    fields_overlap,
    funding_rule,
    is_free,
    months_required,
    nationality_rule,
    open_deadline_rule,
    overall_status,
    work_experience_rule,
)
from pipeline.profile import Profile

TODAY = date(2026, 11, 15)


def profile(**fields: Any) -> Profile:
    data: dict[str, Any] = {
        "full_name": "Test Student",
        "email": "student@example.com",
        "date_of_birth": date(2000, 6, 1),
        "cgpa": 3.2,
        "cgpa_scale": 4.0,
        "target_fields": ["Artificial Intelligence", "Data Science"],
    }
    data.update(fields)
    return Profile.model_validate(data)


def facts(**fields: Any) -> ScholarshipFacts:
    return ScholarshipFacts(name="Test Scholarship", **fields)


def req(kind: str, value: str | None, description: str = "") -> RequirementFact:
    return RequirementFact(kind, value, description or (value or ""))


def outcome(result: RuleResult) -> str:
    return result.outcome


# ---------- nationality ----------


def test_nationality() -> None:
    p = profile()
    assert outcome(nationality_rule(p, facts(open_to_pakistan="yes"), TODAY)) == "pass"
    assert outcome(nationality_rule(p, facts(open_to_pakistan="no"), TODAY)) == "fail"
    assert outcome(nationality_rule(p, facts(open_to_pakistan="unclear"), TODAY)) == "unknown"
    other = profile(nationality="IN")
    assert outcome(nationality_rule(other, facts(open_to_pakistan="yes"), TODAY)) == "unknown"


def test_nationality_passes_for_items_in_pakistan() -> None:
    p = profile(nationality="PK")
    result = nationality_rule(p, facts(location="pakistan"), TODAY)
    assert (result.outcome, result.reason) == ("pass", "Pakistani university or scholarship")
    # An explicit "not open to Pakistan" still wins.
    closed_to_pk = facts(location="pakistan", open_to_pakistan="no")
    assert outcome(nationality_rule(p, closed_to_pk, TODAY)) == "fail"
    assert outcome(nationality_rule(p, facts(location="abroad"), TODAY)) == "unknown"


# ---------- degree level ----------


def test_degree_level() -> None:
    p = profile()
    assert outcome(degree_level_rule(p, facts(degree_levels=["master", "phd"]), TODAY)) == "pass"
    failed = degree_level_rule(p, facts(degree_levels=["bachelor", "phd"]), TODAY)
    assert failed.outcome == "fail" and failed.reason == "only for: bachelor, phd"
    assert outcome(degree_level_rule(p, facts(), TODAY)) == "unknown"


# ---------- field ----------


def test_field_overlap_passes() -> None:
    result = field_rule(profile(), facts(fields_of_study=["Computer Science and AI"]), TODAY)
    assert result.outcome == "pass"
    assert "Computer Science and AI" in result.reason


def test_field_all_fields_passes() -> None:
    for text in ["All available disciplines", "All fields of study", "any discipline"]:
        assert outcome(field_rule(profile(), facts(fields_of_study=[text]), TODAY)) == "pass"


def test_field_no_overlap_fails() -> None:
    result = field_rule(profile(), facts(fields_of_study=["Medicine", "Dentistry"]), TODAY)
    assert result.outcome == "fail"


def test_field_unknown() -> None:
    assert outcome(field_rule(profile(), facts(), TODAY)) == "unknown"
    no_targets = field_rule(profile(target_fields=[]), facts(fields_of_study=["Law"]), TODAY)
    assert no_targets.outcome == "unknown" and no_targets.action


def test_fields_overlap() -> None:
    assert fields_overlap("AI", "Artificial Intelligence")
    assert fields_overlap("Data Science", "MSc Data Science and Analytics")
    assert not fields_overlap("AI", "Maintenance Engineering")  # "ai" inside a word
    assert not fields_overlap("Data Science", "Computer Science")


# ---------- funding ----------


def test_funding() -> None:
    strict, relaxed = profile(fully_funded_only=True), profile(fully_funded_only=False)
    assert outcome(funding_rule(strict, facts(funding="full"), TODAY)) == "pass"
    assert outcome(funding_rule(strict, facts(funding="partial"), TODAY)) == "fail"
    assert outcome(funding_rule(strict, facts(funding="tuition_only"), TODAY)) == "fail"
    assert outcome(funding_rule(relaxed, facts(funding="partial"), TODAY)) == "pass"
    assert outcome(funding_rule(strict, facts(funding="unknown"), TODAY)) == "unknown"


# ---------- application fee ----------


def test_fee_never_fails() -> None:
    p = profile(max_application_fee_eur=50)
    assert outcome(fee_rule(p, facts(application_fee="free of charge"), TODAY)) == "pass"
    paid = fee_rule(p, facts(application_fee="EUR 100"), TODAY)
    assert paid.outcome == "unknown"
    assert paid.reason == "application fee: EUR 100 (your cap: EUR 50)"
    assert outcome(fee_rule(p, facts(), TODAY)) == "unknown"


def test_is_free() -> None:
    assert is_free("Free") and is_free("No application fee") and is_free("EUR 0")
    assert not is_free("PKR 2,500") and not is_free(None)


# ---------- age ----------


def test_age() -> None:
    p = profile(date_of_birth=date(1998, 3, 1))  # 28 now, 29 from 1 March 2027
    limit = [req("age_max", "28", "not over 28 years of age")]
    soon = facts(requirements=limit, deadlines=[date(2027, 1, 15)])
    later = facts(requirements=limit, deadlines=[date(2027, 3, 15)])

    assert outcome(age_rule(p, soon, TODAY)) == "pass"
    failed = age_rule(p, later, TODAY)  # checked on the deadline date, not today
    assert failed.outcome == "fail" and "you will be 29 on 2027-03-15" in failed.reason
    assert outcome(age_rule(p, facts(), TODAY)) == "pass"  # no limit on the page


def test_age_unknown() -> None:
    limit = facts(requirements=[req("age_max", "30")])
    missing_dob = age_rule(profile(date_of_birth=None), limit, TODAY)
    assert missing_dob.outcome == "unknown" and missing_dob.action
    unclear = facts(requirements=[req("age_max", None, "young applicants")])
    assert outcome(age_rule(profile(), unclear, TODAY)) == "unknown"


# ---------- cgpa ----------


def test_cgpa_converted_to_4_scale() -> None:
    p = profile(cgpa=4.0, cgpa_scale=5.0)  # = 3.2 on a 4.0 scale
    assert outcome(cgpa_rule(p, facts(requirements=[req("cgpa_min", "3.0")]), TODAY)) == "pass"
    failed = cgpa_rule(p, facts(requirements=[req("cgpa_min", "3.5")]), TODAY)
    assert failed.outcome == "fail" and failed.reason == "your CGPA 3.20/4 < required 3.50/4"
    assert outcome(cgpa_rule(p, facts(), TODAY)) == "pass"  # no minimum on the page


def test_cgpa_unknown() -> None:
    percent = facts(requirements=[req("cgpa_min", "60%", "Minimum 60% marks")])
    division = facts(requirements=[req("cgpa_min", None, "First division")])
    assert outcome(cgpa_rule(profile(), percent, TODAY)) == "unknown"
    assert outcome(cgpa_rule(profile(), division, TODAY)) == "unknown"
    no_cgpa = cgpa_rule(profile(cgpa=None), facts(requirements=[req("cgpa_min", "3.0")]), TODAY)
    assert no_cgpa.outcome == "unknown" and no_cgpa.action


def test_cgpa_on_4() -> None:
    assert cgpa_on_4("3.0") == 3.0
    assert cgpa_on_4("3.5/5") == pytest.approx(2.8)
    assert cgpa_on_4("2.8 out of 4") == 2.8
    assert cgpa_on_4("Minimum 60% or CGPA ≥ 2.00") == 2.0  # the % part is skipped
    assert cgpa_on_4("70%") is None
    assert cgpa_on_4("First division") is None


# ---------- english ----------


def test_english_pass() -> None:
    ielts = facts(requirements=[req("english_test", "IELTS 6.5")])
    assert outcome(english_rule(profile(english_test="IELTS", english_score="7.0"), ielts,
                                TODAY)) == "pass"  # fmt: skip
    moi = facts(requirements=[req("english_test", "IELTS 6.5")], moi_accepted="yes")
    assert outcome(english_rule(profile(moi_letter=True), moi, TODAY)) == "pass"
    assert outcome(english_rule(profile(), facts(), TODAY)) == "pass"  # no requirement


def test_english_missing_requirement_abroad_is_unknown() -> None:
    abroad = facts(location="abroad")
    result = english_rule(profile(), abroad, TODAY)
    assert result.outcome == "unknown"
    assert result.action == "check the English requirement on the official page"
    # Even with a test score: the minimum is not known.
    scored = english_rule(profile(english_test="IELTS", english_score="7"), abroad, TODAY)
    assert scored.outcome == "unknown"
    # MOI accepted and you have a letter: that is enough.
    moi = facts(location="abroad", moi_accepted="yes")
    assert outcome(english_rule(profile(moi_letter=True), moi, TODAY)) == "pass"
    # In Pakistan, no requirement on the page still passes.
    assert outcome(english_rule(profile(), facts(location="pakistan"), TODAY)) == "pass"


def test_other_missing_requirements_still_pass_abroad() -> None:
    abroad = facts(location="abroad")
    assert outcome(age_rule(profile(), abroad, TODAY)) == "pass"
    assert outcome(cgpa_rule(profile(), abroad, TODAY)) == "pass"
    assert outcome(work_experience_rule(profile(), abroad, TODAY)) == "pass"


def test_english_fail() -> None:
    either = facts(requirements=[req("english_test", "IELTS 6.5 or TOEFL iBT 90")])
    result = english_rule(profile(english_test="TOEFL iBT", english_score="85"), either, TODAY)
    assert result.outcome == "fail" and result.reason == "your TOEFL 85 is below TOEFL 90"


def test_english_unknown() -> None:
    ielts = facts(requirements=[req("english_test", "IELTS 6.5")])
    no_test = english_rule(profile(moi_letter=True), ielts, TODAY)  # MOI not accepted here
    assert no_test.outcome == "unknown" and no_test.action == "take IELTS or TOEFL"
    other_test = english_rule(profile(english_test="PTE", english_score="65"), ielts, TODAY)
    assert other_test.outcome == "unknown"
    cefr = facts(requirements=[req("english_test", "B2", "English language at B2 level")])
    assert outcome(english_rule(profile(english_test="IELTS", english_score="7"), cefr,
                                TODAY)) == "unknown"  # fmt: skip


# ---------- work experience ----------


def test_work_experience() -> None:
    two_years = facts(requirements=[req("work_experience", "2 years")])
    assert outcome(work_experience_rule(profile(work_experience_months=30), two_years,
                                        TODAY)) == "pass"  # fmt: skip
    assert outcome(work_experience_rule(profile(work_experience_months=6), two_years,
                                        TODAY)) == "fail"  # fmt: skip
    vague = facts(requirements=[req("work_experience", None, "relevant experience")])
    assert outcome(work_experience_rule(profile(), vague, TODAY)) == "unknown"
    assert outcome(work_experience_rule(profile(), facts(), TODAY)) == "pass"


def test_months_required() -> None:
    assert months_required("2 years of SDLC experience") == 24
    assert months_required("18 months") == 18
    assert months_required("experience preferred") is None


# ---------- open deadline ----------


def test_open_deadline() -> None:
    p = profile()
    upcoming = open_deadline_rule(p, facts(deadlines=[date(2026, 1, 15), TODAY]), TODAY)
    assert upcoming.outcome == "pass" and upcoming.reason == "next deadline 2026-11-15 (in 0 days)"
    assert outcome(open_deadline_rule(p, facts(deadlines=[date(2026, 1, 15)]), TODAY)) == "fail"
    assert outcome(open_deadline_rule(p, facts(), TODAY)) == "unknown"


# ---------- evaluate and status ----------


def test_admissions_skip_funding_and_fee() -> None:
    names = {r.rule for r in evaluate(profile(), facts(opportunity_type="admission"), TODAY)}
    assert "funding" not in names and "application_fee" not in names
    names = {r.rule for r in evaluate(profile(), facts(), TODAY)}
    assert {"funding", "application_fee"} <= names
    assert len(names) == 10


def test_overall_status() -> None:
    ok, maybe, bad = (RuleResult("x", "pass", ""), RuleResult("y", "unknown", ""),
                      RuleResult("z", "fail", ""))  # fmt: skip
    assert overall_status([ok, ok]) == "eligible"
    assert overall_status([ok, maybe]) == "maybe"
    assert overall_status([ok, maybe, bad]) == "not_eligible"
