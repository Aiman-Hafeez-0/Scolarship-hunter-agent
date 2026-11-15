"""Read and write pipeline data in Postgres (psycopg 3, synchronous).

Every function that writes uses conn.transaction(), so its changes are saved all together
or not at all. Sources are always looked up by their `key` from sources.yaml.
"""

import gzip
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from pipeline.matching.rules import RequirementFact, ScholarshipFacts
from pipeline.models import Change, FetchResult, FetchStatus, Source
from pipeline.nodes.validate import ValidatedScholarship
from pipeline.profile import Profile
from pipeline.schemas import ScholarshipExtraction

Conn = psycopg.Connection[TupleRow]

UPSERT_SOURCE = """
insert into sources (key, url, name, program_key, country, type, opportunity_type,
                     location, render_js, is_pdf, active, note)
values (%(key)s, %(url)s, %(name)s, %(program_key)s, %(country)s, %(type)s::source_type,
        %(opportunity_type)s, %(location)s, %(render_js)s, %(is_pdf)s, %(active)s, %(note)s)
on conflict (key) do update set
    url = excluded.url,
    name = excluded.name,
    program_key = excluded.program_key,
    country = excluded.country,
    type = excluded.type,
    opportunity_type = excluded.opportunity_type,
    location = excluded.location,
    render_js = excluded.render_js,
    is_pdf = excluded.is_pdf,
    active = excluded.active,
    note = excluded.note
"""


def sync_sources(conn: Conn, sources: list[Source]) -> None:
    """Make the sources table match sources.yaml.

    Pass every source from the YAML, inactive ones included. Rows whose key is no longer
    in the YAML are kept (their history matters) but marked inactive.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT_SOURCE, [s.model_dump(mode="json") for s in sources])
        cur.execute(
            "update sources set active = false where active and not (key = any(%s))",
            ([s.key for s in sources],),
        )


def last_hashes(conn: Conn) -> dict[str, str]:
    """The fingerprint of the last saved snapshot for each source key."""
    rows = conn.execute("select key, last_hash from sources where last_hash is not null")
    return {str(key): str(last_hash) for key, last_hash in rows}


def start_run(conn: Conn, kind: str = "fetch") -> uuid.UUID:
    """Record that a run has started. Returns its id."""
    row = conn.execute("insert into runs (kind) values (%s) returning id", (kind,)).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def run_status(results: list[FetchResult]) -> str:
    """success = nothing failed, failed = every source failed, partial = in between."""
    failed = sum(r.status is FetchStatus.FAILED for r in results)
    if failed == 0:
        return "success"
    return "failed" if failed == len(results) else "partial"


@dataclass
class RunTotals:
    """Extra counts a weekly run records (the fetch-only run leaves them at 0)."""

    tokens_in: int = 0
    tokens_out: int = 0
    items_saved: int = 0
    items_needing_review: int = 0


def finish_run(
    conn: Conn,
    run_id: uuid.UUID,
    results: list[FetchResult],
    error: str | None = None,
    totals: RunTotals | None = None,
) -> None:
    """Record the end of a run with its counts and overall status."""
    status = "failed" if error else run_status(results)
    changed = sum(r.change in (Change.NEW, Change.CHANGED) for r in results)
    failed = sum(r.status is FetchStatus.FAILED for r in results)
    totals = totals or RunTotals()
    conn.execute(
        """
        update runs set finished_at = now(), status = %s::run_status, sources_total = %s,
                        sources_changed = %s, sources_failed = %s, error = %s,
                        llm_tokens_in = %s, llm_tokens_out = %s,
                        items_saved = %s, items_needing_review = %s
        where id = %s
        """,
        (status, len(results), changed, failed, error, totals.tokens_in, totals.tokens_out,
         totals.items_saved, totals.items_needing_review, run_id),
    )  # fmt: skip


def save_result(conn: Conn, result: FetchResult, remember_hash: bool = True) -> None:
    """Store what one fetch found.

    remember_hash=False saves the snapshot but not the page's fingerprint, so the page
    counts as changed next time and is extracted again (used when the LLM call failed).
    """
    key = result.source.key
    with conn.transaction():
        if result.status is FetchStatus.OK:
            if result.change in (Change.NEW, Change.CHANGED) and result.text is not None:
                text_gz = gzip.compress(result.text.encode("utf-8"), mtime=0)
                conn.execute(
                    """
                    insert into snapshots (source_id, http_status, content_type, content_hash,
                                           text_gz, char_count)
                    select id, %s, %s, %s, %s, %s from sources where key = %s
                    """,
                    (
                        result.http_status,
                        result.content_type,
                        result.content_hash,
                        text_gz,
                        len(result.text),
                        key,
                    ),
                )
                if remember_hash:
                    conn.execute(
                        "update sources set last_hash = %s where key = %s",
                        (result.content_hash, key),
                    )
            conn.execute(
                """
                update sources set last_fetched_at = now(), last_http_status = %s,
                                   consecutive_failures = 0, last_error = null
                where key = %s
                """,
                (result.http_status, key),
            )
        elif result.status is FetchStatus.FAILED:
            conn.execute(
                """
                update sources set consecutive_failures = consecutive_failures + 1,
                                   last_error = %s
                where key = %s
                """,
                (result.error, key),
            )
        elif result.status is FetchStatus.BLOCKED_ROBOTS:
            conn.execute(
                """
                update sources set robots_allowed = false, robots_checked_at = now()
                where key = %s
                """,
                (key,),
            )
        # skipped_js: nothing was fetched, so there is nothing to store.


@dataclass
class Snapshot:
    id: uuid.UUID
    fetched_at: datetime
    text: str


def latest_snapshot(conn: Conn, key: str) -> Snapshot | None:
    """The most recent saved page text for a source, or None if it was never saved."""
    row = conn.execute(
        """
        select snapshots.id, snapshots.fetched_at, snapshots.text_gz
        from snapshots join sources on sources.id = snapshots.source_id
        where sources.key = %s and snapshots.text_gz is not null
        order by snapshots.fetched_at desc
        limit 1
        """,
        (key,),
    ).fetchone()
    if row is None:
        return None
    snapshot_id, fetched_at, text_gz = row
    return Snapshot(
        id=uuid.UUID(str(snapshot_id)),
        fetched_at=fetched_at,
        text=gzip.decompress(text_gz).decode("utf-8"),
    )


# ---------- Saving extractions ----------

UPSERT_SCHOLARSHIP = """
insert into scholarships (
    source_id, snapshot_id, name, provider, opportunity_type, location, campus, intake,
    entry_tests, moi_accepted, country, degree_levels, fields_of_study, funding, coverage,
    application_fee, open_to_pakistan, application_url, status, needs_review, review_notes,
    extraction_version, extracted_at, last_extraction)
values (
    %(source_id)s, %(snapshot_id)s, %(name)s, %(provider)s, %(opportunity_type)s, %(location)s,
    %(campus)s, %(intake)s, %(entry_tests)s, %(moi_accepted)s, %(country)s, %(degree_levels)s,
    %(fields_of_study)s, %(funding)s::funding_type, %(coverage)s, %(application_fee)s,
    %(open_to_pakistan)s, %(application_url)s, %(status)s::sch_status, %(needs_review)s,
    %(review_notes)s, %(extraction_version)s, now(), %(last_extraction)s)
on conflict (source_id, name) do update set
    snapshot_id = excluded.snapshot_id,
    provider = excluded.provider,
    opportunity_type = excluded.opportunity_type,
    location = excluded.location,
    campus = excluded.campus,
    intake = excluded.intake,
    entry_tests = excluded.entry_tests,
    moi_accepted = excluded.moi_accepted,
    country = excluded.country,
    degree_levels = excluded.degree_levels,
    fields_of_study = excluded.fields_of_study,
    funding = excluded.funding,
    coverage = excluded.coverage,
    application_fee = excluded.application_fee,
    open_to_pakistan = excluded.open_to_pakistan,
    application_url = excluded.application_url,
    status = excluded.status,
    needs_review = excluded.needs_review,
    review_notes = excluded.review_notes,
    extraction_version = excluded.extraction_version,
    extracted_at = excluded.extracted_at,
    last_extraction = excluded.last_extraction
returning id
"""

SCHOLARSHIP_COLUMNS = {
    "name", "provider", "opportunity_type", "location", "campus", "intake", "entry_tests",
    "moi_accepted", "country", "degree_levels", "fields_of_study", "application_fee",
    "open_to_pakistan", "application_url", "status",
}  # fmt: skip

# Quotes are left out of the comparison: a reworded quote is not a changed fact.
QUOTE_FIELDS: Any = {
    "name": True,
    "application_fee_evidence": True,
    "open_to_pakistan_evidence": True,
    "moi_evidence": True,
    "coverage": {"evidence"},
    "deadlines": {"__all__": {"evidence"}},
    "requirements": {"__all__": {"evidence"}},
}


def comparable(item: ScholarshipExtraction) -> dict[str, Any]:
    """The facts of one extraction as plain JSON, without quotes. Compared between runs."""
    return item.model_dump(mode="json", exclude=QUOTE_FIELDS)


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """(field, old value, new value) for every field whose value differs."""
    names = sorted(old.keys() | new.keys())
    return [(name, old.get(name), new.get(name)) for name in names
            if old.get(name) != new.get(name)]  # fmt: skip


def save_extraction(
    conn: Conn, source_key: str, items: list[ValidatedScholarship], extraction_version: str
) -> int:
    """Save the validated scholarships of one source, all in one transaction.

    Each scholarship is upserted by (source, name); its deadlines, requirements, documents
    and evidence are replaced; and every field that differs from the previous extraction
    gets a row in `changes`. Returns how many scholarships were saved.
    """
    with conn.transaction():
        row = conn.execute(
            """
            select sources.id,
                   (select id from snapshots where source_id = sources.id
                    order by fetched_at desc limit 1)
            from sources where key = %s
            """,
            (source_key,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown source key '{source_key}'")
        source_id, snapshot_id = row
        for validated in items:
            _save_scholarship(conn, source_id, snapshot_id, validated, extraction_version)
    return len(items)


def _save_scholarship(
    conn: Conn,
    source_id: object,
    snapshot_id: object,
    validated: ValidatedScholarship,
    extraction_version: str,
) -> None:
    item = validated.item
    facts = comparable(item)
    previous = conn.execute(
        "select last_extraction from scholarships where source_id = %s and name = %s",
        (source_id, item.name),
    ).fetchone()

    coverage = item.coverage.model_dump(exclude={"evidence"}) if item.coverage else {}
    row = conn.execute(
        UPSERT_SCHOLARSHIP,
        {
            **item.model_dump(include=SCHOLARSHIP_COLUMNS),
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "funding": item.funding_type,
            "coverage": Jsonb(coverage),
            "needs_review": validated.needs_review,
            "review_notes": validated.notes,
            "extraction_version": extraction_version,
            "last_extraction": Jsonb(facts),
        },
    ).fetchone()
    assert row is not None
    scholarship_id = row[0]

    for table in ("deadlines", "requirements", "required_documents", "evidence"):
        conn.execute(f"delete from {table} where scholarship_id = %s", (scholarship_id,))

    evidence_ids: dict[str, object] = {}
    for evidence in validated.evidence:
        evidence_row = conn.execute(
            """
            insert into evidence (scholarship_id, field_path, quote, match_score, snapshot_id)
            values (%s, %s, %s, %s, %s) returning id
            """,
            (scholarship_id, evidence.field_path, evidence.quote, evidence.score, snapshot_id),
        ).fetchone()
        assert evidence_row is not None
        evidence_ids[evidence.field_path] = evidence_row[0]

    for index, deadline in enumerate(item.deadlines):
        conn.execute(
            """
            insert into deadlines (scholarship_id, label, deadline_date, timezone_note,
                                   is_estimated, evidence_id)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (scholarship_id, deadline.label, deadline.deadline_date, deadline.time_and_timezone,
             deadline.is_estimated, evidence_ids.get(f"deadlines[{index}]")),
        )  # fmt: skip
    for index, requirement in enumerate(item.requirements):
        conn.execute(
            """
            insert into requirements (scholarship_id, kind, value, description, evidence_id)
            values (%s, %s, %s, %s, %s)
            """,
            (scholarship_id, requirement.kind, requirement.value, requirement.description,
             evidence_ids.get(f"requirements[{index}]")),
        )  # fmt: skip
    for name in item.required_documents:
        conn.execute(
            "insert into required_documents (scholarship_id, name) values (%s, %s)",
            (scholarship_id, name),
        )

    if previous is not None and previous[0] is not None:
        for field_path, old, new in changed_fields(previous[0], facts):
            conn.execute(
                """
                insert into changes (scholarship_id, field_path, old_value, new_value)
                values (%s, %s, %s, %s)
                """,
                (scholarship_id, field_path, _json_or_null(old), _json_or_null(new)),
            )


def _json_or_null(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


class PostgresStore:
    """What the LangGraph pipeline uses to save. Safe to call from several threads."""

    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self._lock = threading.Lock()  # one connection: one transaction at a time

    def record_fetch(self, result: FetchResult) -> None:
        """A source we will not extract (failed, skipped or unchanged)."""
        with self._lock:
            save_result(self.conn, result)

    def save(
        self,
        result: FetchResult,
        items: list[ValidatedScholarship],
        extraction_version: str,
        remember_hash: bool,
    ) -> int:
        """Snapshot and extraction of one source, in a single transaction."""
        with self._lock, self.conn.transaction():
            save_result(self.conn, result, remember_hash=remember_hash)
            return save_extraction(self.conn, result.source.key, items, extraction_version)


# ---------- Profiles and matches ----------

PROFILE_COLUMNS = [
    "full_name", "email", "nationality", "date_of_birth", "degree", "cgpa", "cgpa_scale",
    "english_test", "english_score", "moi_letter", "work_experience_months", "target_fields",
    "target_intake", "max_application_fee_eur", "prefer_no_fee", "fully_funded_only",
    "reminder_days",
]  # fmt: skip


def upsert_profile(conn: Conn, profile: Profile) -> uuid.UUID:
    """Insert the profile, or update the one with the same email. Returns its id."""
    columns = ", ".join(PROFILE_COLUMNS)
    values = ", ".join(f"%({c})s" for c in PROFILE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in PROFILE_COLUMNS if c != "email")
    with conn.transaction():
        row = conn.execute(
            f"insert into profiles ({columns}) values ({values}) "
            f"on conflict (email) do update set {updates} returning id",
            profile.model_dump(),
        ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def load_profiles(conn: Conn) -> list[tuple[str, Profile]]:
    """(id, profile) for every profile."""
    cur = conn.execute(f"select id, {', '.join(PROFILE_COLUMNS)} from profiles order by email")
    result: list[tuple[str, Profile]] = []
    for row in cur.fetchall():
        data = dict(zip(PROFILE_COLUMNS, row[1:], strict=True))
        result.append((str(row[0]), Profile.model_validate(data)))
    return result


def load_open_scholarships(conn: Conn) -> list[ScholarshipFacts]:
    """Every scholarship that is not closed, with its requirements and dated deadlines."""
    rows = conn.execute(
        """
        select id, name, opportunity_type, status::text, degree_levels, fields_of_study,
               funding::text, open_to_pakistan, moi_accepted, application_fee
        from scholarships where status <> 'closed' order by name
        """
    ).fetchall()
    facts = {
        str(r[0]): ScholarshipFacts(
            id=str(r[0]), name=str(r[1]), opportunity_type=str(r[2]), status=str(r[3]),
            degree_levels=list(r[4]), fields_of_study=list(r[5]),
            funding=str(r[6]), open_to_pakistan=str(r[7]), moi_accepted=str(r[8]),
            application_fee=None if r[9] is None else str(r[9]),
        )
        for r in rows
    }  # fmt: skip
    ids = list(facts)
    for sid, kind, value, description in conn.execute(
        "select scholarship_id, kind, value, coalesce(description, '') from requirements"
        " where scholarship_id = any(%s)",
        (ids,),
    ):
        facts[str(sid)].requirements.append(
            RequirementFact(str(kind), None if value is None else str(value), str(description))
        )
    for sid, deadline_date in conn.execute(
        "select scholarship_id, deadline_date from deadlines"
        " where scholarship_id = any(%s) and deadline_date is not null",
        (ids,),
    ):
        assert isinstance(deadline_date, date)
        facts[str(sid)].deadlines.append(deadline_date)
    return list(facts.values())


@dataclass
class MatchRow:
    profile_id: str
    scholarship_id: str
    status: str
    score: int
    reasons: list[dict[str, Any]]


def save_matches(conn: Conn, rows: list[MatchRow]) -> None:
    """Upsert the matches; drop matches for scholarships that are now closed. One transaction."""
    with conn.transaction():
        conn.execute(
            "delete from matches where scholarship_id in"
            " (select id from scholarships where status = 'closed')"
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into matches (profile_id, scholarship_id, status, score, reasons)
                values (%s, %s, %s::match_status, %s, %s)
                on conflict (profile_id, scholarship_id) do update set
                    status = excluded.status, score = excluded.score,
                    reasons = excluded.reasons, computed_at = now()
                """,
                [(r.profile_id, r.scholarship_id, r.status, r.score, Jsonb(r.reasons))
                 for r in rows],
            )  # fmt: skip
