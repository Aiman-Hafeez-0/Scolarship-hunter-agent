"""Show duplicates once.

Two official pages sometimes describe the same thing, e.g. "HEC Entry Test" and "HEC Entry
Test Calendar". They are joined when they have
  - the same type (scholarship, admission or entry test),
  - a very similar name (one name's words all appear in the other, or the names are at
    least 88% alike), and
  - the same next deadline (a real date: items without a date are never joined, because
    we cannot tell them apart safely).
The joined item keeps the best match and lists every page it came from ("2 sources").
Nothing changes in the database; this only affects what the dashboard shows.
"""

import re
from dataclasses import replace
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from app.queries import ScholarshipRow, SourceLink, UpcomingDeadline

SIMILAR_RATIO = 0.88
MIN_SHARED_WORDS = 2  # "HEC" alone is too short to call two names the same


def _words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.lower())


def similar_names(a: str, b: str) -> bool:
    """True if two names very likely mean the same thing."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    sa, sb = set(wa), set(wb)
    if (sa <= sb or sb <= sa) and min(len(sa), len(sb)) >= MIN_SHARED_WORDS:
        return True
    return SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= SIMILAR_RATIO


def same_item(a: ScholarshipRow, b: ScholarshipRow) -> bool:
    return (
        a.opportunity_type == b.opportunity_type
        and a.next_deadline is not None
        and a.next_deadline == b.next_deadline
        and similar_names(a.name, b.name)
    )


def _source(row: ScholarshipRow) -> SourceLink:
    name = row.source_name or row.provider or urlsplit(row.source_url).hostname or row.source_url
    return SourceLink(row.id, name, row.source_url)


def _best_first(group: list[ScholarshipRow]) -> list[ScholarshipRow]:
    """Open before closed, matched before unmatched, higher score first, shorter name first."""
    return sorted(group, key=lambda r: (r.status == "closed", r.score is None,
                                        -(r.score or 0), len(r.name)))  # fmt: skip


def group_rows(rows: list[ScholarshipRow]) -> list[list[ScholarshipRow]]:
    """The rows in groups of duplicates (most groups have one row), best row first."""
    groups: list[list[ScholarshipRow]] = []
    for row in rows:
        for group in groups:
            if any(same_item(member, row) for member in group):
                group.append(row)
                break
        else:
            groups.append([row])
    return [_best_first(group) for group in groups]


def merge_rows(rows: list[ScholarshipRow]) -> list[ScholarshipRow]:
    """One row per item: the best row of each group, listing every source of the group."""
    return [replace(group[0], sources=[_source(r) for r in group]) for group in group_rows(rows)]


def sources_of(rows: list[ScholarshipRow], scholarship_id: str) -> list[SourceLink]:
    """Every source of the item that `scholarship_id` belongs to (one when not merged)."""
    for group in group_rows(rows):
        if any(r.id == scholarship_id for r in group):
            return [_source(r) for r in group]
    return []


def merge_deadlines(deadlines: list[UpcomingDeadline]) -> list[UpcomingDeadline]:
    """The same deadline of duplicate items, shown once (with source_count)."""
    kept: list[UpcomingDeadline] = []
    for d in deadlines:
        twin = next((k for k in kept if k.scholarship_id != d.scholarship_id
                     and k.opportunity_type == d.opportunity_type
                     and k.deadline_date == d.deadline_date
                     and similar_names(k.scholarship_name, d.scholarship_name)), None)  # fmt: skip
        if twin is None:
            kept.append(replace(d))
        else:
            twin.source_count += 1
    return kept
