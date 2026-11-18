"""Check every extracted fact against the page before we trust it.

Three checks, run on every extraction:

1. Quote check: each evidence quote must really be on the page. After squeezing spaces, the
   rapidfuzz partial_ratio between quote and page must be at least QUOTE_MIN_SCORE (0-100).
   A fact whose quote fails (or that has no quote) is dropped, and the item needs review.
2. Date check: the day and the month of a deadline_date must both be written in that
   deadline's quote (month as an English, Italian or Turkish name, or as a number in a date
   like 15.01.2027). If not, the date is set to null and the item needs review.
3. Status from dates:
   - an item whose intake season has started (Fall 2026 starts in September 2026, Spring 2027
     in February 2027) is closed, unless a deadline is still ahead (then it needs review);
   - an item with no dated deadline is "unknown" (never "open" or "upcoming"), so it can
     never count as "action needed"; a "closed" from the page is kept;
   - if every dated deadline has passed and no new cycle is announced (LLM status not
     "upcoming"), status = closed.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from pipeline.schemas import DeadlineItem, PageExtraction, Requirement, ScholarshipExtraction

QUOTE_MIN_SCORE = 90

# Month names after simplify() (lowercase, no accents, Turkish dotless i -> i).
MONTH_NAMES: dict[int, tuple[str, ...]] = {
    1: ("january", "jan", "gennaio", "gen", "ocak"),
    2: ("february", "feb", "febbraio", "subat"),
    3: ("march", "mar", "marzo", "mart"),
    4: ("april", "apr", "aprile", "nisan"),
    5: ("may", "maggio", "mag", "mayis"),
    6: ("june", "jun", "giugno", "giu", "haziran"),
    7: ("july", "jul", "luglio", "lug", "temmuz"),
    8: ("august", "aug", "agosto", "ago", "agustos"),
    9: ("september", "sept", "sep", "settembre", "set", "eylul"),
    10: ("october", "oct", "ottobre", "ott", "ekim"),
    11: ("november", "nov", "novembre", "kasim"),
    12: ("december", "dec", "dicembre", "dic", "aralik"),
}
# The month each intake season starts.
SEASON_START_MONTH = {"fall": 9, "autumn": 9, "spring": 2}
INTAKE_SEASON = re.compile(r"\b(fall|autumn|spring)\b\W{0,3}(20\d\d)\b", re.IGNORECASE)
# A numeric date such as 15.01.2027, 15/1/27, 01/15/2027 or 2027-01-15.
NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})(?!\d)")


@dataclass
class Evidence:
    """One quote that backs a kept fact, with how well it matched the page."""

    field_path: str  # e.g. "deadlines[0]", "coverage", "open_to_pakistan"
    quote: str
    score: int


@dataclass
class ValidatedScholarship:
    item: ScholarshipExtraction  # only facts that passed the checks
    evidence: list[Evidence] = field(default_factory=list)
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)  # why it needs review

    def flag(self, note: str) -> None:
        self.needs_review = True
        self.notes.append(note)


# ---------- Quote check ----------


def squeeze_spaces(text: str) -> str:
    return " ".join(text.split())


def quote_score(quote: str | None, page_text: str) -> int:
    """0-100: how well the quote matches some part of the page (0 if there is no quote)."""
    if not quote or not quote.strip():
        return 0
    needle, haystack = squeeze_spaces(quote), squeeze_spaces(page_text)
    if needle in haystack:  # the common case, and much faster than fuzzy matching
        return 100
    return round(fuzz.partial_ratio(needle, haystack))


# ---------- Date check ----------


def simplify(text: str) -> str:
    """Lowercase, Turkish ı -> i, accents removed: "Şubat" -> "subat", "MAYIS" -> "mayis"."""
    text = text.replace("ı", "i").replace("İ", "i").casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def date_in_quote(day_month: date, quote: str) -> bool:
    """True if the date's day number and its month (by name or number) are in the quote."""
    text = simplify(quote)
    day, month = day_month.day, day_month.month

    day_found = re.search(rf"(?<!\d)0?{day}(?!\d)", text) is not None
    names = "|".join(MONTH_NAMES[month])
    month_found = re.search(rf"\b(?:{names})\b", text) is not None
    if not month_found:
        dates = NUMERIC_DATE.finditer(text)
        month_found = any(_month_in_numeric_date(m, day, month) for m in dates)
    return day_found and month_found


def _month_in_numeric_date(match: re.Match[str], day: int, month: int) -> bool:
    first, second, third = match.groups()
    if len(first) == 4:  # 2027-01-15
        return int(second) == month and int(third) == day
    # 15.01.2027 (day first) or 01/15/2027 (month first)
    return (int(first), int(second)) in ((day, month), (month, day))


# ---------- Running all checks ----------


def validate_extraction(
    extraction: PageExtraction, page_text: str, today: date, location: str | None = None
) -> list[ValidatedScholarship]:
    """Check every item's quotes and dates.

    location is the SOURCE's location (from sources.yaml, never from the LLM). For a source
    in Pakistan, open_to_pakistan is set to "yes": studying in Pakistan is open to Pakistanis.
    """
    results = [validate_scholarship(item, page_text, today) for item in extraction.scholarships]
    if location == "pakistan":
        for result in results:
            result.item = result.item.model_copy(update={"open_to_pakistan": "yes"})
    return results


def validate_scholarship(
    item: ScholarshipExtraction, page_text: str, today: date
) -> ValidatedScholarship:
    result = ValidatedScholarship(item=item)
    deadlines = _check_deadlines(item.deadlines, page_text, result)
    requirements = _check_requirements(item.requirements, page_text, result)
    updates: dict[str, object] = {"deadlines": deadlines, "requirements": requirements}

    # Single facts: if the quote fails, fall back to "no value".
    coverage = item.coverage
    if coverage is not None and not _keep("coverage", coverage.evidence, page_text, result):
        updates["coverage"] = None
    if item.application_fee is not None and not _keep(
        "application_fee", item.application_fee_evidence, page_text, result
    ):
        updates |= {"application_fee": None, "application_fee_evidence": None}
    if item.open_to_pakistan != "unclear" and not _keep(
        "open_to_pakistan", item.open_to_pakistan_evidence, page_text, result
    ):
        updates |= {"open_to_pakistan": "unclear", "open_to_pakistan_evidence": None}
    if item.moi_accepted != "unclear" and not _keep(
        "moi_accepted", item.moi_evidence, page_text, result
    ):
        updates |= {"moi_accepted": "unclear", "moi_evidence": None}
    if item.intake is not None and not _keep("intake", item.intake_evidence, page_text, result):
        updates |= {"intake": None, "intake_evidence": None}
    if item.degree_levels and not _keep(
        "degree_levels", item.degree_levels_evidence, page_text, result
    ):
        updates |= {"degree_levels": [], "degree_levels_evidence": None}

    cleaned = item.model_copy(update=updates)
    started = intake_start(cleaned.intake)
    ahead = [d.deadline_date for d in cleaned.deadlines
             if d.deadline_date is not None and d.deadline_date >= today]  # fmt: skip
    if started is not None and started <= today and ahead:
        result.flag(f"intake {cleaned.intake!r} has started, but deadline {min(ahead)} is "
                    "still ahead; status not changed")  # fmt: skip
    status = status_after_dates(cleaned, today)
    if status != cleaned.status:
        cleaned = cleaned.model_copy(update={"status": status})
    result.item = cleaned
    return result


def _check_quote(
    field_path: str, quote: str | None, page_text: str, result: ValidatedScholarship
) -> int | None:
    """The quote's score if it passes; otherwise flag the item and return None."""
    score = quote_score(quote, page_text)
    if score >= QUOTE_MIN_SCORE:
        return score
    reason = "has no quote" if not quote else f"quote not found on page (score {score})"
    result.flag(f"{field_path} {reason}; value dropped")
    return None


def _keep(field_path: str, quote: str | None, page_text: str, result: ValidatedScholarship) -> bool:
    """Check a single fact's quote; record the evidence if it passes."""
    score = _check_quote(field_path, quote, page_text, result)
    if score is None or quote is None:
        return False
    result.evidence.append(Evidence(field_path, quote, score))
    return True


def _check_deadlines(
    deadlines: list[DeadlineItem], page_text: str, result: ValidatedScholarship
) -> list[DeadlineItem]:
    kept: list[DeadlineItem] = []
    for index, deadline in enumerate(deadlines):
        score = _check_quote(f"deadlines[{index}] ({deadline.label})", deadline.evidence,
                             page_text, result)  # fmt: skip
        if score is None:
            continue
        if deadline.deadline_date and not date_in_quote(deadline.deadline_date, deadline.evidence):
            result.flag(
                f"deadlines[{index}] ({deadline.label}) date {deadline.deadline_date} "
                "not written in its quote; date removed"
            )
            deadline = deadline.model_copy(update={"deadline_date": None})
        result.evidence.append(Evidence(f"deadlines[{len(kept)}]", deadline.evidence, score))
        kept.append(deadline)
    return kept


def _check_requirements(
    requirements: list[Requirement], page_text: str, result: ValidatedScholarship
) -> list[Requirement]:
    kept: list[Requirement] = []
    for index, requirement in enumerate(requirements):
        score = _check_quote(f"requirements[{index}] ({requirement.kind})", requirement.evidence,
                             page_text, result)  # fmt: skip
        if score is None:
            continue
        result.evidence.append(Evidence(f"requirements[{len(kept)}]", requirement.evidence, score))
        kept.append(requirement)
    return kept


# ---------- Past dates ----------


def is_closed(deadline: DeadlineItem, today: date) -> bool:
    """A deadline before today is closed. One without a date is not known to be closed."""
    return deadline.deadline_date is not None and deadline.deadline_date < today


def intake_start(intake: str | None) -> date | None:
    """When the intake begins: "Fall 2026" -> 2026-09-01, "Spring 2027" -> 2027-02-01.

    If several seasons are named ("Fall 2026 and Spring 2027"), the latest one counts.
    None if the intake names no season and year.
    """
    if not intake:
        return None
    starts = [date(int(year), SEASON_START_MONTH[season.lower()], 1)
              for season, year in INTAKE_SEASON.findall(intake)]  # fmt: skip
    return max(starts) if starts else None


def status_after_dates(item: ScholarshipExtraction, today: date) -> str:
    """The status after looking at the intake and the dated deadlines.

    - intake season already started and no deadline still ahead: "closed";
    - no dated deadline: "unknown" (a "closed" written on the page is kept);
    - every dated deadline passed and no new cycle announced: "closed". A new cycle is
      announced when the page says the next round is coming (LLM status "upcoming").
    Otherwise the LLM's status is kept.
    """
    dated = [d for d in item.deadlines if d.deadline_date is not None]
    started = intake_start(item.intake)
    if started is not None and started <= today and all(is_closed(d, today) for d in dated):
        return "closed"
    if not dated:
        return "closed" if item.status == "closed" else "unknown"
    if item.status == "upcoming":
        return item.status
    if all(is_closed(d, today) for d in dated):
        return "closed"
    return item.status
