"""What the dashboard reads from the database, and the small pure helpers around it.

DashboardStore is the list of reads. PostgresDashboardStore is the real one; tests use a
fake with the same methods. Filtering and sorting happen in Python (filter_rows), because
the list is small (tens of items) and that keeps them easy to test.
"""

import math
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

from psycopg import errors

from app import auth
from pipeline.dbcore import PROFILE_COLUMNS, Conn, upsert_profile
from pipeline.models import Source
from pipeline.notify.store import PostgresNotifyStore
from pipeline.profile import Profile

OPPORTUNITY_TYPES = ("scholarship", "admission", "entry_test")
LOCATIONS = ("abroad", "pakistan")
MATCH_STATUSES = ("eligible", "maybe", "not_eligible")
NOT_MATCHED = "not_matched"
FUNDING_TYPES = ("full", "partial", "tuition_only", "unknown")
SORTS = ("deadline", "score")
NO_FUNDING_TYPES = ("admission", "entry_test")  # they have no funding: shown as "—"
PER_PAGE = 25

Sort = Literal["deadline", "score"]


# ---------- Rows ----------


@dataclass
class SourceLink:
    """One official page an item was read from (a merged item has several)."""

    scholarship_id: str
    name: str
    url: str


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
    source_name: str = ""
    # Every page this item was read from; more than one after app.merge joined duplicates.
    sources: list[SourceLink] = field(default_factory=list)


@dataclass
class UpcomingDeadline:
    scholarship_id: str
    scholarship_name: str
    label: str
    deadline_date: date
    is_estimated: bool
    opportunity_type: str = "scholarship"
    source_count: int = 1  # more than 1 after app.merge joined duplicates


@dataclass
class RecentChange:
    """One change seen on a page, for the "Recently changed" list."""

    scholarship_id: str
    scholarship_name: str
    field_path: str
    old_value: Any
    new_value: Any
    detected_at: datetime


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
    opportunity_type: str = "scholarship"
    location: str = ""
    country: str | None = None


@dataclass
class SourceRow:
    key: str
    name: str
    url: str
    opportunity_type: str
    location: str
    active: bool
    last_fetched_at: datetime | None
    last_http_status: int | None
    consecutive_failures: int
    last_error: str | None
    robots_allowed: bool | None  # None: never checked; False: robots.txt blocked us
    robots_checked_at: datetime | None
    note: str | None


@dataclass
class ProfileRecord:
    id: str
    profile: Profile
    emails_paused: bool


class ProfileEmailTaken(ValueError):
    """Another profile already uses this email."""


class DashboardStore(Protocol):
    def profile_id(self, email: str) -> str | None: ...
    def scholarship_rows(self, profile_id: str | None, today: date) -> list[ScholarshipRow]: ...
    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]: ...
    def recent_changes(self, limit: int) -> list[RecentChange]: ...
    def scholarship(
        self, profile_id: str | None, scholarship_id: str
    ) -> ScholarshipDetail | None: ...
    def runs(self, limit: int) -> list[RunRow]: ...
    def last_good_weekly_at(self) -> datetime | None: ...
    # Actions (they write)
    def verify(self, scholarship_id: str) -> datetime | None: ...
    def sources(self) -> list[SourceRow]: ...
    def set_source_active(self, key: str, active: bool) -> SourceRow | None: ...
    def add_source(self, source: Source) -> bool: ...
    def profile(self, profile_id: str) -> ProfileRecord | None: ...
    def save_profile(self, profile_id: str | None, profile: Profile, paused: bool) -> str: ...


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
    page: int = 1

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
            "page": str(self.page) if self.page > 1 else "",
        }
        return {key: value for key, value in values.items() if value}

    def link(self, **changes: Any) -> str:
        """The list page with some filters changed, e.g. f.link(location="pakistan").
        Any change other than the page goes back to page 1."""
        changed = replace(self, **({"page": 1} | changes))
        query = urlencode(changed.as_query())
        return "/scholarships" + (f"?{query}" if query else "")


def _choice(value: str | None, allowed: tuple[str, ...]) -> str | None:
    return value if value in allowed else None


def parse_filters(params: Mapping[str, str]) -> Filters:
    """Read the filters from URL query values. Unknown or broken values are ignored."""
    try:
        before = date.fromisoformat(params.get("deadline_before", ""))
    except ValueError:
        before = None
    sort = params.get("sort")
    page = params.get("page", "")
    return Filters(
        q=params.get("q", "").strip()[:100],
        opportunity_type=_choice(params.get("type"), OPPORTUNITY_TYPES),
        location=_choice(params.get("location"), LOCATIONS),
        match=_choice(params.get("match"), (*MATCH_STATUSES, NOT_MATCHED)),
        funding=_choice(params.get("funding"), FUNDING_TYPES),
        deadline_before=before,
        include_closed=params.get("include_closed") in ("1", "on", "true"),
        sort="score" if sort == "score" else "deadline",
        page=max(1, min(int(page), 10_000)) if page.isdigit() else 1,
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
    # Funding only means something for scholarships (admissions and tests show "—").
    if f.funding and (row.opportunity_type in NO_FUNDING_TYPES or row.funding != f.funding):
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


@dataclass
class FacetOption:
    value: str  # "" for "All"
    text: str
    count: int
    selected: bool


@dataclass
class Facet:
    """One group of filter chips, e.g. Type: All 12 · Scholarship 7 · Admission 3 ..."""

    name: str  # the URL parameter
    label: str
    options: list[FacetOption]


# (URL parameter, Filters field, label, values with their chip text)
FACETS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("type", "opportunity_type", "Type",
     [("scholarship", "Scholarship"), ("admission", "Admission"), ("entry_test", "Entry test")]),
    ("location", "location", "Location", [("abroad", "Abroad"), ("pakistan", "Pakistan")]),
    ("match", "match", "Match",
     [("eligible", "Eligible"), ("maybe", "Maybe"), ("not_eligible", "Not eligible"),
      ("not_matched", "Not matched")]),
    ("funding", "funding", "Funding",
     [("full", "Full"), ("partial", "Partial"), ("tuition_only", "Tuition only"),
      ("unknown", "Unknown")]),
]  # fmt: skip


def _facet_value(row: ScholarshipRow, attr: str) -> str | None:
    if attr == "match":
        return row.match_status or NOT_MATCHED
    if attr == "funding":
        return None if row.opportunity_type in NO_FUNDING_TYPES else row.funding
    return str(getattr(row, attr))


def facets(rows: list[ScholarshipRow], f: Filters) -> list[Facet]:
    """The filter chips with counts. Each group counts the rows that pass every OTHER filter,
    so a count says how many results you get when you click that chip."""
    result = []
    for name, attr, label, values in FACETS:
        blank: dict[str, Any] = {attr: None}
        others = replace(f, **blank)
        counts = Counter(_facet_value(row, attr) for row in rows if _matches(row, others))
        current = getattr(f, attr)
        total = sum(n for value, n in counts.items() if value is not None)
        options = [FacetOption("", "All", total, current is None)]
        options += [FacetOption(value, text, counts.get(value, 0), current == value)
                    for value, text in values]  # fmt: skip
        result.append(Facet(name, label, options))
    return result


@dataclass
class Page:
    """One page of a list: items, which page, how many pages."""

    items: list[ScholarshipRow]
    number: int
    pages: int
    total: int
    first: int  # position of the first item on this page (1-based; 0 when empty)
    last: int


def paginate(items: list[ScholarshipRow], number: int, per_page: int = PER_PAGE) -> Page:
    """Page `number` (1-based) of the items. A page past the end shows the last page."""
    pages = max(1, math.ceil(len(items) / per_page))
    number = min(max(1, number), pages)
    start = (number - 1) * per_page
    chunk = items[start : start + per_page]
    return Page(chunk, number, pages, len(items), start + 1 if chunk else 0, start + len(chunk))


def match_counts(rows: list[ScholarshipRow]) -> dict[str, int]:
    """How many open items have each match status (closed ones are left out)."""
    counts = Counter(row.match_status or NOT_MATCHED for row in rows if row.status != "closed")
    return {status: counts.get(status, 0) for status in (*MATCH_STATUSES, NOT_MATCHED)}


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
       m.status::text, m.score, src.name
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
                score=None if r[13] is None else int(str(r[13])), source_name=str(r[14]),
            )
            for r in rows
        ]  # fmt: skip

    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]:
        rows = self.conn.execute(
            """
            select s.id, s.name, d.label, d.deadline_date, d.is_estimated, s.opportunity_type
            from deadlines d join scholarships s on s.id = d.scholarship_id
            where d.deadline_date >= %s and s.status <> 'closed'
            order by d.deadline_date, s.name, d.label
            limit %s
            """,
            (today, limit),
        ).fetchall()
        return [UpcomingDeadline(str(r[0]), str(r[1]), str(r[2]), r[3], bool(r[4]), str(r[5]))
                for r in rows]  # fmt: skip

    def recent_changes(self, limit: int) -> list[RecentChange]:
        """The newest changes seen on any page, newest first."""
        rows = self.conn.execute(
            """
            select s.id, s.name, c.field_path, c.old_value, c.new_value, c.detected_at
            from changes c join scholarships s on s.id = c.scholarship_id
            order by c.detected_at desc, s.name, c.field_path
            limit %s
            """,
            (limit,),
        ).fetchall()
        return [RecentChange(str(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5]) for r in rows]

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
            opportunity_type=str(row["opportunity_type"]), location=str(row["location"]),
            country=row["country"],
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

    # ---------- Actions ----------

    def verify(self, scholarship_id: str) -> datetime | None:
        """Mark a scholarship as checked by you, now. None if there is no such scholarship."""
        try:
            sid = uuid.UUID(scholarship_id)
        except ValueError:
            return None
        found = self.conn.execute(
            "update scholarships set verified_at = now() where id = %s returning verified_at",
            (sid,),
        ).fetchone()
        return None if found is None else found[0]

    def sources(self) -> list[SourceRow]:
        rows = self.conn.execute(SOURCE_ROWS + " order by active desc, key").fetchall()
        return [_source_row(r) for r in rows]

    def set_source_active(self, key: str, active: bool) -> SourceRow | None:
        self.conn.execute("update sources set active = %s where key = %s", (active, key))
        found = self.conn.execute(SOURCE_ROWS + " where key = %s", (key,)).fetchone()
        return None if found is None else _source_row(found)

    def add_source(self, source: Source) -> bool:
        """Insert a new source (robots.txt was just checked). False if its key or url exists."""
        found = self.conn.execute(
            """
            insert into sources (key, url, name, program_key, country, type, opportunity_type,
                                 location, render_js, is_pdf, active, note, robots_allowed,
                                 robots_checked_at)
            values (%(key)s, %(url)s, %(name)s, %(program_key)s, %(country)s,
                    %(type)s::source_type, %(opportunity_type)s, %(location)s, %(render_js)s,
                    %(is_pdf)s, %(active)s, %(note)s, true, now())
            on conflict do nothing
            returning id
            """,
            source.model_dump(mode="json"),
        ).fetchone()
        return found is not None

    def profile(self, profile_id: str) -> ProfileRecord | None:
        found = self.conn.execute(
            f"select {', '.join(PROFILE_COLUMNS)}, emails_paused from profiles where id = %s",
            (uuid.UUID(profile_id),),
        ).fetchone()
        if found is None:
            return None
        data = dict(zip(PROFILE_COLUMNS, found[: len(PROFILE_COLUMNS)], strict=True))
        return ProfileRecord(profile_id, Profile.model_validate(data), bool(found[-1]))

    def save_profile(self, profile_id: str | None, profile: Profile, paused: bool) -> str:
        """Update the profile (or create it when there is none yet). Returns its id."""
        values = profile.model_dump()
        try:
            with self.conn.transaction():
                if profile_id is None:
                    profile_id = str(upsert_profile(self.conn, profile))
                else:
                    updates = ", ".join(f"{c} = %({c})s" for c in PROFILE_COLUMNS)
                    self.conn.execute(f"update profiles set {updates} where id = %(id)s",
                                      {**values, "id": uuid.UUID(profile_id)})  # fmt: skip
                self.conn.execute("update profiles set emails_paused = %s where id = %s",
                                  (paused, uuid.UUID(profile_id)))  # fmt: skip
        except errors.UniqueViolation as exc:
            raise ProfileEmailTaken(profile.email) from exc
        return profile_id


SOURCE_ROWS = """
select key, name, url, opportunity_type, location, active, last_fetched_at, last_http_status,
       consecutive_failures, last_error, robots_allowed, robots_checked_at, note
from sources
"""


def _source_row(r: Any) -> SourceRow:
    return SourceRow(
        key=str(r[0]), name=str(r[1]), url=str(r[2]), opportunity_type=str(r[3]),
        location=str(r[4]), active=bool(r[5]), last_fetched_at=r[6], last_http_status=r[7],
        consecutive_failures=int(r[8]), last_error=r[9], robots_allowed=r[10],
        robots_checked_at=r[11], note=r[12],
    )  # fmt: skip


class PostgresLoginLimiter:
    """The login rate limit (auth.LoginLimiter), with one row per failed login in the
    login_failures table, so every server instance sees the same count."""

    def __init__(
        self,
        conn: Conn,
        max_failures: int = auth.MAX_LOGIN_FAILURES,
        window_s: float = auth.LOGIN_WINDOW_S,
    ) -> None:
        self.conn = conn
        self.max_failures = max_failures
        self.window_s = float(window_s)

    def retry_after_s(self, key: str) -> int:
        # The max_failures-th newest failure in the window: once it is older than the window,
        # fewer than max_failures are left and the next try is allowed.
        row = self.conn.execute(
            """
            select extract(epoch from failed_at + make_interval(secs => %(window)s) - now())
            from login_failures
            where client_key = %(key)s and failed_at > now() - make_interval(secs => %(window)s)
            order by failed_at desc
            offset %(skip)s limit 1
            """,
            {"key": key, "window": self.window_s, "skip": self.max_failures - 1},
        ).fetchone()
        return 0 if row is None else max(1, math.ceil(float(row[0])))

    def blocked(self, key: str) -> bool:
        return self.retry_after_s(key) > 0

    def record_failure(self, key: str) -> None:
        with self.conn.transaction():
            self.conn.execute("insert into login_failures (client_key) values (%s)", (key,))
            # Housekeeping: rows outside the window no longer count for anyone.
            self.conn.execute(
                "delete from login_failures where failed_at <= now() - make_interval(secs => %s)",
                (self.window_s,),
            )

    def reset(self, key: str) -> None:
        self.conn.execute("delete from login_failures where client_key = %s", (key,))
