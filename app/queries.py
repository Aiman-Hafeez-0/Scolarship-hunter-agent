"""What the dashboard reads from the database, and the small pure helpers around it.

DashboardStore is the list of reads. PostgresDashboardStore is the real one; tests use a
fake with the same methods. Filtering and sorting happen in Python (filter_rows), because
the list is small (tens of items) and that keeps them easy to test.
"""

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from pipeline.db import Conn
from pipeline.notify.store import PostgresNotifyStore

OPPORTUNITY_TYPES = ("scholarship", "admission", "entry_test")
LOCATIONS = ("abroad", "pakistan")
MATCH_STATUSES = ("eligible", "maybe", "not_eligible")
NOT_MATCHED = "not_matched"
FUNDING_TYPES = ("full", "partial", "tuition_only", "unknown")
SORTS = ("deadline", "score")

Sort = Literal["deadline", "score"]


# ---------- Rows ----------


@dataclass
class ScholarshipRow:
    """One line of the scholarships table."""

    id: str
    name: str
    provider: str | None
    opportunity_type: str
    location: str
    country: str | None
    funding: str
    status: str
    needs_review: bool
    verified: bool
    source_url: str
    next_deadline: date | None  # the closest deadline that is today or later
    match_status: str | None  # None: not matched yet
    score: int | None


@dataclass
class UpcomingDeadline:
    scholarship_id: str
    scholarship_name: str
    label: str
    deadline_date: date
    is_estimated: bool


@dataclass
class RunRow:
    id: str
    kind: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    sources_total: int
    sources_changed: int
    sources_failed: int
    items_saved: int
    items_needing_review: int
    tokens_in: int
    tokens_out: int
    error: str | None


@dataclass
class Quote:
    text: str
    score: int | None  # how well the quote matched the page (0-100)


@dataclass
class FieldRow:
    label: str
    value: str  # "" when the page did not say
    quotes: list[Quote] = field(default_factory=list)
    has_evidence: bool = False  # False: this field is never backed by a quote


@dataclass
class DeadlineDetail:
    label: str
    deadline_date: date | None
    timezone_note: str | None
    is_estimated: bool
    quote: Quote | None


@dataclass
class RequirementDetail:
    kind: str
    value: str | None
    description: str | None
    quote: Quote | None


@dataclass
class MatchInfo:
    status: str
    score: int
    reasons: list[dict[str, Any]]
    computed_at: datetime


@dataclass
class ChangeEntry:
    field_path: str
    old_value: Any
    new_value: Any
    detected_at: datetime


@dataclass
class ScholarshipDetail:
    id: str
    name: str
    source_name: str
    source_url: str
    application_url: str | None
    status: str
    needs_review: bool
    review_notes: list[str]
    verified_at: datetime | None
    extracted_at: datetime
    extraction_version: str | None
    fields: list[FieldRow]
    deadlines: list[DeadlineDetail]
    requirements: list[RequirementDetail]
    documents: list[str]
    match: MatchInfo | None
    changes: list[ChangeEntry]


class DashboardStore(Protocol):
    def profile_id(self, email: str) -> str | None: ...
    def scholarship_rows(self, profile_id: str | None, today: date) -> list[ScholarshipRow]: ...
    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]: ...
    def scholarship(
        self, profile_id: str | None, scholarship_id: str
    ) -> ScholarshipDetail | None: ...
    def runs(self, limit: int) -> list[RunRow]: ...
    def last_good_weekly_at(self) -> datetime | None: ...


# ---------- Filters ----------


@dataclass
class Filters:
    q: str = ""
    opportunity_type: str | None = None
    location: str | None = None
    match: str | None = None  # one of MATCH_STATUSES, or NOT_MATCHED
    funding: str | None = None
    deadline_before: date | None = None  # next deadline on or before this day
    include_closed: bool = False
    sort: Sort = "deadline"

    def as_query(self) -> dict[str, str]:
        """The filters as URL query values (empty ones left out)."""
        values = {
            "q": self.q,
            "type": self.opportunity_type or "",
            "location": self.location or "",
            "match": self.match or "",
            "funding": self.funding or "",
            "deadline_before": self.deadline_before.isoformat() if self.deadline_before else "",
            "include_closed": "1" if self.include_closed else "",
            "sort": self.sort if self.sort != "deadline" else "",
        }
        return {key: value for key, value in values.items() if value}


def _choice(value: str | None, allowed: tuple[str, ...]) -> str | None:
    return value if value in allowed else None


def parse_filters(params: Mapping[str, str]) -> Filters:
    """Read the filters from URL query values. Unknown or broken values are ignored."""
    try:
        before = date.fromisoformat(params.get("deadline_before", ""))
    except ValueError:
        before = None
    sort = params.get("sort")
    return Filters(
        q=params.get("q", "").strip()[:100],
        opportunity_type=_choice(params.get("type"), OPPORTUNITY_TYPES),
        location=_choice(params.get("location"), LOCATIONS),
        match=_choice(params.get("match"), (*MATCH_STATUSES, NOT_MATCHED)),
        funding=_choice(params.get("funding"), FUNDING_TYPES),
        deadline_before=before,
        include_closed=params.get("include_closed") in ("1", "on", "true"),
        sort="score" if sort == "score" else "deadline",
    )


def _matches(row: ScholarshipRow, f: Filters) -> bool:
    if not f.include_closed and row.status == "closed":
        return False
    if f.opportunity_type and row.opportunity_type != f.opportunity_type:
        return False
    if f.location and row.location != f.location:
        return False
    if f.match and (row.match_status or NOT_MATCHED) != f.match:
        return False
    if f.funding and row.funding != f.funding:
        return False
    if f.deadline_before and (row.next_deadline is None or row.next_deadline > f.deadline_before):
        return False
    if f.q:
        text = " ".join(filter(None, [row.name, row.provider, row.country])).lower()
        if f.q.lower() not in text:
            return False
    return True


def filter_rows(rows: list[ScholarshipRow], f: Filters) -> list[ScholarshipRow]:
    """The rows that pass every filter, sorted by next deadline (soonest first, none last)
    or by score (highest first, unmatched last). Ties are sorted by name."""
    kept = [row for row in rows if _matches(row, f)]
    if f.sort == "score":
        return sorted(kept, key=lambda r: (r.score is None, -(r.score or 0), r.name.lower()))
    return sorted(
        kept, key=lambda r: (r.next_deadline is None, r.next_deadline or date.max, r.name.lower())
    )


def match_counts(rows: list[ScholarshipRow]) -> dict[str, int]:
    """How many open items have each match status (closed ones are left out)."""
    counts = Counter(row.match_status or NOT_MATCHED for row in rows if row.status != "closed")
    return {status: counts.get(status, 0) for status in (*MATCH_STATUSES, NOT_MATCHED)}


def days_left(deadline: date, today: date) -> int:
    return (deadline - today).days


def deadline_tone(days: int) -> Literal["red", "orange", "gray"]:
    """Red at 7 days or less, orange at 30 or less."""
    if days <= 7:
        return "red"
    return "orange" if days <= 30 else "gray"


# ---------- Detail fields ----------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _coverage_text(coverage: Mapping[str, Any]) -> str:
    parts = [f"{key.replace('_', ' ')}: {value}" for key, value in coverage.items() if value]
    return "; ".join(parts)


# (label, column, evidence field_path or None when that fact never carries a quote)
DETAIL_FIELDS: list[tuple[str, str, str | None]] = [
    ("Provider", "provider", None),
    ("Type", "opportunity_type", None),
    ("Location", "location", None),
    ("Country", "country", None),
    ("Campus", "campus", None),
    ("Status", "status", None),
    ("Intake", "intake", "intake"),
    ("Degree levels", "degree_levels", "degree_levels"),
    ("Fields of study", "fields_of_study", None),
    ("Funding", "funding", "coverage"),
    ("Coverage", "coverage", "coverage"),
    ("Application fee", "application_fee", "application_fee"),
    ("Open to Pakistanis", "open_to_pakistan", "open_to_pakistan"),
    ("English by MOI letter", "moi_accepted", "moi_accepted"),
    ("Entry tests", "entry_tests", None),
]


def build_fields(row: Mapping[str, Any], evidence: Mapping[str, list[Quote]]) -> list[FieldRow]:
    """Every field of a scholarship with the quotes that back it."""
    fields: list[FieldRow] = []
    for label, column, path in DETAIL_FIELDS:
        value = row.get(column)
        text = _coverage_text(value or {}) if column == "coverage" else _text(value)
        quotes = list(evidence.get(path, [])) if path else []
        fields.append(FieldRow(label, text, quotes, has_evidence=path is not None))
    return fields


# ---------- Postgres ----------

SCHOLARSHIP_ROWS = """
select s.id, s.name, s.provider, s.opportunity_type, s.location, s.country, s.funding::text,
       s.status::text, s.needs_review, s.verified_at is not null, src.url,
       (select min(d.deadline_date) from deadlines d
        where d.scholarship_id = s.id and d.deadline_date >= %(today)s),
       m.status::text, m.score
from scholarships s
join sources src on src.id = s.source_id
left join matches m on m.scholarship_id = s.id and m.profile_id = %(profile_id)s::uuid
order by s.name
"""

DETAIL_COLUMNS = [
    "id", "name", "provider", "opportunity_type", "location", "country", "campus", "status",
    "intake", "degree_levels", "fields_of_study", "funding", "coverage", "application_fee",
    "open_to_pakistan", "moi_accepted", "entry_tests", "application_url", "needs_review",
    "review_notes", "verified_at", "extracted_at", "extraction_version",
]  # fmt: skip


def _quote(text: Any, score: Any) -> Quote | None:
    return None if text is None else Quote(str(text), None if score is None else int(score))


class PostgresDashboardStore:
    """Reads for the dashboard. Reuses the notify store for profiles and run health."""

    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self.notify = PostgresNotifyStore(conn)

    def profile_id(self, email: str) -> str | None:
        """The profile with this email, else the first profile (a one-person dashboard)."""
        profiles = self.notify.load_profiles()
        for profile in profiles:
            if profile.email.lower() == email.lower():
                return profile.id
        return profiles[0].id if profiles else None

    def scholarship_rows(self, profile_id: str | None, today: date) -> list[ScholarshipRow]:
        rows = self.conn.execute(
            SCHOLARSHIP_ROWS, {"today": today, "profile_id": profile_id}
        ).fetchall()
        return [
            ScholarshipRow(
                id=str(r[0]), name=str(r[1]), provider=r[2], opportunity_type=str(r[3]),
                location=str(r[4]), country=r[5], funding=str(r[6]), status=str(r[7]),
                needs_review=bool(r[8]), verified=bool(r[9]), source_url=str(r[10]),
                next_deadline=r[11], match_status=r[12],
                score=None if r[13] is None else int(str(r[13])),
            )
            for r in rows
        ]  # fmt: skip

    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]:
        rows = self.conn.execute(
            """
            select s.id, s.name, d.label, d.deadline_date, d.is_estimated
            from deadlines d join scholarships s on s.id = d.scholarship_id
            where d.deadline_date >= %s and s.status <> 'closed'
            order by d.deadline_date, s.name, d.label
            limit %s
            """,
            (today, limit),
        ).fetchall()
        return [UpcomingDeadline(str(r[0]), str(r[1]), str(r[2]), r[3], bool(r[4])) for r in rows]

    def scholarship(self, profile_id: str | None, scholarship_id: str) -> ScholarshipDetail | None:
        try:
            sid = uuid.UUID(scholarship_id)
        except ValueError:
            return None
        cur = self.conn.execute(
            f"select {', '.join('s.' + c for c in DETAIL_COLUMNS)}, src.name, src.url"
            " from scholarships s join sources src on src.id = s.source_id where s.id = %s",
            (sid,),
        )
        found = cur.fetchone()
        if found is None:
            return None
        row = dict(zip(DETAIL_COLUMNS, found[: len(DETAIL_COLUMNS)], strict=True))
        source_name, source_url = found[len(DETAIL_COLUMNS):]

        evidence: dict[str, list[Quote]] = {}
        for path, text, score in self.conn.execute(
            "select field_path, quote, match_score from evidence where scholarship_id = %s"
            " order by field_path",
            (sid,),
        ):
            quote = _quote(text, score)
            if quote:
                evidence.setdefault(str(path), []).append(quote)

        deadlines = [
            DeadlineDetail(str(r[0]), r[1], r[2], bool(r[3]), _quote(r[4], r[5]))
            for r in self.conn.execute(
                """
                select d.label, d.deadline_date, d.timezone_note, d.is_estimated,
                       e.quote, e.match_score
                from deadlines d left join evidence e on e.id = d.evidence_id
                where d.scholarship_id = %s order by d.deadline_date nulls last, d.label
                """,
                (sid,),
            )
        ]
        requirements = [
            RequirementDetail(str(r[0]), r[1], r[2], _quote(r[3], r[4]))
            for r in self.conn.execute(
                """
                select r.kind, r.value, r.description, e.quote, e.match_score
                from requirements r left join evidence e on e.id = r.evidence_id
                where r.scholarship_id = %s order by r.kind
                """,
                (sid,),
            )
        ]
        documents = [
            str(r[0])
            for r in self.conn.execute(
                "select name from required_documents where scholarship_id = %s order by name",
                (sid,),
            )
        ]
        match_row = self.conn.execute(
            "select status::text, score, reasons, computed_at from matches"
            " where scholarship_id = %s and profile_id = %s::uuid",
            (sid, profile_id),
        ).fetchone()
        match = None
        if match_row is not None:
            match = MatchInfo(str(match_row[0]), int(str(match_row[1])),
                              list(match_row[2] or []), match_row[3])  # fmt: skip
        changes = [
            ChangeEntry(str(r[0]), r[1], r[2], r[3])
            for r in self.conn.execute(
                "select field_path, old_value, new_value, detected_at from changes"
                " where scholarship_id = %s order by detected_at desc, field_path limit 100",
                (sid,),
            )
        ]
        return ScholarshipDetail(
            id=str(row["id"]), name=str(row["name"]), source_name=str(source_name),
            source_url=str(source_url), application_url=row["application_url"],
            status=str(row["status"]), needs_review=bool(row["needs_review"]),
            review_notes=list(row["review_notes"] or []), verified_at=row["verified_at"],
            extracted_at=row["extracted_at"], extraction_version=row["extraction_version"],
            fields=build_fields(row, evidence), deadlines=deadlines, requirements=requirements,
            documents=documents, match=match, changes=changes,
        )  # fmt: skip

    def runs(self, limit: int) -> list[RunRow]:
        rows = self.conn.execute(
            """
            select id, kind, started_at, finished_at, status::text, sources_total,
                   sources_changed, sources_failed, items_saved, items_needing_review,
                   llm_tokens_in, llm_tokens_out, error
            from runs order by started_at desc limit %s
            """,
            (limit,),
        ).fetchall()
        return [
            RunRow(
                id=str(r[0]), kind=str(r[1]), started_at=r[2], finished_at=r[3],
                status=str(r[4]), sources_total=int(r[5]), sources_changed=int(r[6]),
                sources_failed=int(r[7]), items_saved=int(r[8]), items_needing_review=int(r[9]),
                tokens_in=int(r[10]), tokens_out=int(r[11]), error=r[12],
            )
            for r in rows
        ]  # fmt: skip

    def last_good_weekly_at(self) -> datetime | None:
        return self.notify.last_good_weekly_at()
