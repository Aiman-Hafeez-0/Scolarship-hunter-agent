"""Matching rules: pure functions, no LLM, no database.

Every rule takes (profile, scholarship, today) and returns a RuleResult: "pass", "fail" or
"unknown" with a one-line reason (and sometimes an action, e.g. "take IELTS or TOEFL").

A requirement that is not on the page passes ("no age limit on the page"): we can only check
what was extracted. One exception: for opportunities abroad, a missing English requirement is
"unknown", because almost every program abroad asks for English proof.
Admissions and entry tests skip the funding and fee rules.

Overall status: any fail = not_eligible; otherwise any unknown = maybe; all pass = eligible.
"""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

from rapidfuzz import fuzz

from pipeline.nodes.validate import simplify
from pipeline.profile import Profile

Outcome = Literal["pass", "fail", "unknown"]
Status = Literal["eligible", "maybe", "not_eligible"]

TARGET_LEVEL = "master"
CHECK_ENGLISH = "check the English requirement on the official page"
ENGLISH_TESTS = {"ielts": "IELTS", "toefl": "TOEFL", "pte": "PTE", "duolingo": "Duolingo"}
ABBREVIATIONS = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "cs": "computer science",
    "ds": "data science",
}
ALL_FIELDS = re.compile(
    r"\ball\b(?: \w+)? (?:fields|disciplines|subjects|programmes|programs|areas)\b"
    r"|\bany (?:field|discipline|subject)\b"
)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    outcome: Outcome
    reason: str
    action: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class RequirementFact:
    kind: str
    value: str | None
    description: str

    @property
    def text(self) -> str:
        return f"{self.value or ''} {self.description}".strip()


@dataclass
class ScholarshipFacts:
    """What matching needs to know about one saved scholarship."""

    name: str
    opportunity_type: str = "scholarship"
    status: str = "unknown"
    location: str = ""  # "abroad" or "pakistan"
    degree_levels: list[str] = field(default_factory=list)
    fields_of_study: list[str] = field(default_factory=list)
    funding: str = "unknown"
    open_to_pakistan: str = "unclear"
    moi_accepted: str = "unclear"
    application_fee: str | None = None
    requirements: list[RequirementFact] = field(default_factory=list)
    deadlines: list[date] = field(default_factory=list)  # dated deadlines only
    id: str | None = None

    def requirements_of(self, kind: str) -> list[RequirementFact]:
        return [r for r in self.requirements if r.kind == kind]


Rule = Callable[[Profile, ScholarshipFacts, date], RuleResult]


# ---------- Small helpers ----------


def next_deadline(s: ScholarshipFacts, today: date) -> date | None:
    """The earliest deadline that is today or later."""
    future = [d for d in s.deadlines if d >= today]
    return min(future) if future else None


def needs_action(s: ScholarshipFacts, today: date) -> bool:
    """True only for an item that is not closed and has a dated deadline today or later.

    An item without a dated deadline never needs action: we cannot say when to act.
    """
    return s.status != "closed" and next_deadline(s, today) is not None


def is_free(fee: str | None) -> bool:
    if not fee:
        return False
    text = simplify(fee)
    if re.search(r"\bfree\b|\bno (?:application )?fee\b|\bwaived\b", text):
        return True
    numbers = re.findall(r"\d+(?:[.,]\d+)?", text)
    return bool(numbers) and all(float(n.replace(",", ".")) == 0 for n in numbers)


def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def _first_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _expand(text: str) -> str:
    text = simplify(text)
    for short, long in ABBREVIATIONS.items():
        text = re.sub(rf"\b{short}\b", long, text)
    return text


def fields_overlap(target: str, field_name: str) -> bool:
    """True if a target field and a scholarship field are about the same subject."""
    t, f = _expand(target), _expand(field_name)
    if re.search(rf"\b{re.escape(t)}\b", f) or re.search(rf"\b{re.escape(f)}\b", t):
        return True
    return fuzz.token_sort_ratio(t, f) >= 85


def _short(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


# ---------- The rules ----------


def nationality_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "nationality"
    if p.nationality.strip().upper() not in ("PK", "PAKISTAN", "PAKISTANI"):
        return RuleResult(name, "unknown", "only eligibility for Pakistan is extracted")
    if s.open_to_pakistan == "yes":
        return RuleResult(name, "pass", "open to applicants from Pakistan")
    if s.open_to_pakistan == "no":
        return RuleResult(name, "fail", "not open to applicants from Pakistan")
    return RuleResult(name, "unknown", "the page does not say whether Pakistan is eligible")


def degree_level_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "degree_level"
    if not s.degree_levels:
        return RuleResult(name, "unknown", "degree level not stated")
    if TARGET_LEVEL in s.degree_levels:
        return RuleResult(name, "pass", f"offered for a {TARGET_LEVEL}'s degree")
    return RuleResult(name, "fail", f"only for: {', '.join(s.degree_levels)}")


def field_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "field"
    if not s.fields_of_study:
        return RuleResult(name, "unknown", "fields of study not listed")
    if any(ALL_FIELDS.search(simplify(f)) for f in s.fields_of_study):
        return RuleResult(name, "pass", "open to all fields")
    if not p.target_fields:
        return RuleResult(name, "unknown", "no target_fields in your profile",
                          action="add target_fields to profile.yaml")  # fmt: skip
    matching = [f for f in s.fields_of_study if any(fields_overlap(t, f) for t in p.target_fields)]
    if matching:
        return RuleResult(name, "pass", f"matches your fields: {_short(matching)}")
    return RuleResult(name, "fail", f"fields are {_short(s.fields_of_study)}; none match yours")


def funding_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "funding"
    if s.funding == "full":
        return RuleResult(name, "pass", "fully funded")
    if s.funding in ("partial", "tuition_only"):
        label = "partly funded" if s.funding == "partial" else "covers tuition only"
        if p.fully_funded_only:
            return RuleResult(name, "fail", f"{label}; you want fully funded only")
        return RuleResult(name, "pass", label)
    return RuleResult(name, "unknown", "funding not stated")


def fee_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    """Never fails: a fee is shown so you can decide."""
    name = "application_fee"
    if is_free(s.application_fee):
        return RuleResult(name, "pass", "free to apply")
    if not s.application_fee:
        return RuleResult(name, "unknown", "application fee not stated")
    reason = f"application fee: {s.application_fee}"
    if p.max_application_fee_eur is not None:
        reason += f" (your cap: EUR {p.max_application_fee_eur:g})"
    return RuleResult(name, "unknown", reason)


def age_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "age"
    requirements = s.requirements_of("age_max")
    if not requirements:
        return RuleResult(name, "pass", "no age limit on the page")
    limits = [_first_int(r.value or "") or _first_int(r.description) for r in requirements]
    known = [limit for limit in limits if limit is not None]
    if not known:
        return RuleResult(name, "unknown", f"age limit unclear: {requirements[0].text}")
    if p.date_of_birth is None:
        return RuleResult(name, "unknown", f"age limit {min(known)}; your date of birth is missing",
                          action="add date_of_birth to profile.yaml")  # fmt: skip
    on = next_deadline(s, today) or today
    age = age_on(p.date_of_birth, on)
    verdict: Outcome = "pass" if age <= min(known) else "fail"
    return RuleResult(name, verdict, f"you will be {age} on {on}; the limit is {min(known)}")


def cgpa_on_4(text: str) -> float | None:
    """A CGPA requirement on a 4.0 scale: "3.0", "3.5/5", "2.8 out of 4" -> float, else None."""
    t = simplify(text)
    scaled = re.search(r"(\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+(?:\.\d+)?)", t)
    if scaled:
        value, scale = float(scaled.group(1)), float(scaled.group(2))
        if 0 < scale <= 10 and value <= scale:
            return value / scale * 4
    for match in re.finditer(r"(?<![\d.])(\d(?:\.\d+)?)(?![\d.%])", t):
        if float(match.group(1)) <= 4:
            return float(match.group(1))
    return None


def cgpa_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "cgpa"
    requirements = s.requirements_of("cgpa_min")
    if not requirements:
        return RuleResult(name, "pass", "no CGPA minimum on the page")
    parsed = [cgpa_on_4(r.value or "") or cgpa_on_4(r.description) for r in requirements]
    needed = [n for n in parsed if n is not None]
    if not needed:
        text = requirements[0].text
        if re.search(r"%|percent|division|marks", simplify(text)):
            return RuleResult(name, "unknown", f"requirement is not a CGPA: {text}")
        return RuleResult(name, "unknown", f"CGPA requirement unclear: {text}")
    if p.cgpa is None:
        return RuleResult(name, "unknown", f"needs CGPA {min(needed):.2f}/4; yours is missing",
                          action="add cgpa to profile.yaml")  # fmt: skip
    yours = p.cgpa / p.cgpa_scale * 4
    need = min(needed)
    verdict: Outcome = "pass" if yours + 1e-9 >= need else "fail"
    sign = ">=" if verdict == "pass" else "<"
    return RuleResult(name, verdict, f"your CGPA {yours:.2f}/4 {sign} required {need:.2f}/4")


def _test_name(text: str) -> str | None:
    lowered = simplify(text)
    for key, label in ENGLISH_TESTS.items():
        if key in lowered:
            return label
    return None


def _test_scores(text: str) -> list[tuple[str, float]]:
    """(test, minimum score) pairs, e.g. "IELTS 6.5 or TOEFL iBT 90" -> both."""
    pattern = r"(ielts|toefl|pte|duolingo)\D{0,15}?(\d+(?:\.\d+)?)"
    return [(ENGLISH_TESTS[t], float(n)) for t, n in re.findall(pattern, simplify(text))]


def english_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "english"
    requirements = s.requirements_of("english_test")
    if s.moi_accepted == "yes" and p.moi_letter:
        return RuleResult(name, "pass", "an MOI letter is accepted and you have one")
    if not requirements:
        if s.location == "abroad":  # programs abroad nearly always need English proof
            return RuleResult(name, "unknown", "the page does not state an English requirement",
                              action=CHECK_ENGLISH)  # fmt: skip
        return RuleResult(name, "pass", "no English test requirement on the page")

    needed = [pair for r in requirements for pair in _test_scores(r.text)]
    wanted = " / ".join(f"{t} {n:g}" for t, n in needed) or requirements[0].text
    yours = _test_name(p.english_test or "")
    score = _first_float(p.english_score or "")
    if yours is None or score is None:
        return RuleResult(name, "unknown", f"requires {wanted}; you have no test score",
                          action="take IELTS or TOEFL")  # fmt: skip
    same_test = [n for t, n in needed if t == yours]
    if not same_test:
        return RuleResult(name, "unknown", f"requires {wanted}; you have {yours} {score:g}")
    if score >= min(same_test):
        return RuleResult(name, "pass", f"your {yours} {score:g} meets {yours} {min(same_test):g}")
    return RuleResult(name, "fail", f"your {yours} {score:g} is below {yours} {min(same_test):g}")


def _first_float(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def months_required(text: str) -> int | None:
    """ "2 years" -> 24, "18 months" -> 18; None if there is no number with a unit."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?)\b", simplify(text))
    if not match:
        return None
    number = float(match.group(1))
    return round(number * 12) if match.group(2).startswith("y") else round(number)


def work_experience_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "work_experience"
    requirements = s.requirements_of("work_experience")
    if not requirements:
        return RuleResult(name, "pass", "no work experience required")
    months = [m for r in requirements if (m := months_required(r.text)) is not None]
    if not months:
        return RuleResult(name, "unknown", f"requirement unclear: {requirements[0].text}")
    need = max(months)
    verdict: Outcome = "pass" if p.work_experience_months >= need else "fail"
    return RuleResult(name, verdict,
                      f"you have {p.work_experience_months} months; {need} required")  # fmt: skip


def open_deadline_rule(p: Profile, s: ScholarshipFacts, today: date) -> RuleResult:
    name = "open_deadline"
    if not s.deadlines:
        return RuleResult(name, "unknown", "no dated deadline on the page")
    upcoming = next_deadline(s, today)
    if upcoming is None:
        return RuleResult(name, "fail", f"all deadlines have passed (last: {max(s.deadlines)})")
    return RuleResult(name, "pass", f"next deadline {upcoming} (in {(upcoming - today).days} days)")


# ---------- Putting them together ----------

ALL_RULES: list[Rule] = [
    nationality_rule, degree_level_rule, field_rule, funding_rule, fee_rule, age_rule,
    cgpa_rule, english_rule, work_experience_rule, open_deadline_rule,
]  # fmt: skip
SCHOLARSHIP_ONLY: set[Rule] = {funding_rule, fee_rule}


def evaluate(p: Profile, s: ScholarshipFacts, today: date) -> list[RuleResult]:
    """Run every rule that applies to this kind of opportunity."""
    is_scholarship = s.opportunity_type == "scholarship"
    rules = [r for r in ALL_RULES if is_scholarship or r not in SCHOLARSHIP_ONLY]
    return [rule(p, s, today) for rule in rules]


def overall_status(results: list[RuleResult]) -> Status:
    outcomes = {r.outcome for r in results}
    if "fail" in outcomes:
        return "not_eligible"
    return "maybe" if "unknown" in outcomes else "eligible"
