"""Read and write pipeline data in Postgres (psycopg 3, synchronous).

Every function that writes uses conn.transaction(), so its changes are saved all together
or not at all. Sources are always looked up by their `key` from sources.yaml.
"""

import gzip
import uuid

import psycopg
from psycopg.rows import TupleRow

from pipeline.models import Change, FetchResult, FetchStatus, Source

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


def finish_run(
    conn: Conn, run_id: uuid.UUID, results: list[FetchResult], error: str | None = None
) -> None:
    """Record the end of a run with its counts and overall status."""
    status = "failed" if error else run_status(results)
    changed = sum(r.change in (Change.NEW, Change.CHANGED) for r in results)
    failed = sum(r.status is FetchStatus.FAILED for r in results)
    conn.execute(
        """
        update runs set finished_at = now(), status = %s::run_status, sources_total = %s,
                        sources_changed = %s, sources_failed = %s, error = %s
        where id = %s
        """,
        (status, len(results), changed, failed, error, run_id),
    )


def save_result(conn: Conn, result: FetchResult) -> None:
    """Store what one fetch found."""
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
                conn.execute(
                    "update sources set last_hash = %s where key = %s", (result.content_hash, key)
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
