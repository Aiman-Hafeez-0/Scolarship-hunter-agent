"""Rank matches with a 0-100 score.

    score = 40*field + 25*funding + 20*urgency + 15*confidence  (+5 if free to apply), max 100

- field: 1 for AI / machine learning / data science, 0.6 for general CS or "all fields", else 0
- funding: 1 full, 0.5 unknown, 0.4 partial or tuition only
- urgency: next deadline in 14 days or less 1, 30 or less 0.7, 60 or less 0.4, else 0.2
- confidence: 1 eligible, 0.5 maybe, 0 not eligible
"""

import re
from datetime import date

from pipeline.matching.rules import ALL_FIELDS, ScholarshipFacts, Status, is_free, next_deadline
from pipeline.nodes.validate import simplify

AI_FIELDS = re.compile(
    r"artificial intelligence|machine learning|data science|deep learning|\bai\b|\bml\b"
)
CS_FIELDS = re.compile(
    r"computer|computing|software|informatics|information technology|\bcs\b|\bict\b"
)
FUNDING = {"full": 1.0, "unknown": 0.5, "partial": 0.4, "tuition_only": 0.4}
CONFIDENCE = {"eligible": 1.0, "maybe": 0.5, "not_eligible": 0.0}


def field_factor(s: ScholarshipFacts) -> float:
    fields = [simplify(f) for f in s.fields_of_study]
    if any(AI_FIELDS.search(f) for f in fields):
        return 1.0
    if any(CS_FIELDS.search(f) or ALL_FIELDS.search(f) for f in fields):
        return 0.6
    return 0.0


def urgency_factor(s: ScholarshipFacts, today: date) -> float:
    upcoming = next_deadline(s, today)
    if upcoming is None:
        return 0.2
    days = (upcoming - today).days
    if days <= 14:
        return 1.0
    if days <= 30:
        return 0.7
    if days <= 60:
        return 0.4
    return 0.2


def match_score(s: ScholarshipFacts, status: Status, today: date) -> int:
    raw = (
        40 * field_factor(s)
        + 25 * FUNDING.get(s.funding, 0.5)
        + 20 * urgency_factor(s, today)
        + 15 * CONFIDENCE[status]
        + (5 if is_free(s.application_fee) else 0)
    )
    return min(100, round(raw))
