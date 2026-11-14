"""Tests for pipeline.db against a real Postgres (TEST_DATABASE_URL; skipped if not set)."""

import gzip
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from pipeline import db
from pipeline.migrate import apply_migrations
from pipeline.models import Change, FetchStatus
from pipeline.tests.conftest import make_result, make_source

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def conn(test_conn: Conn) -> Iterator[Conn]:
    """A throwaway schema with all migrations applied."""
    apply_migrations(test_conn)
    yield test_conn


def _source_row(conn: Conn, key: str) -> dict[str, Any]:
    cur = conn.execute("select * from sources where key = %s", (key,))
    row = cur.fetchone()
    assert row is not None
    assert cur.description is not None
    return {col.name: value for col, value in zip(cur.description, row, strict=True)}


def _snapshot_count(conn: Conn, key: str) -> int:
    row = conn.execute(
        "select count(*) from snapshots join sources on sources.id = source_id where key = %s",
        (key,),
    ).fetchone()
    assert row is not None
    return int(str(row[0]))


def _sync_example(conn: Conn, *keys: str) -> None:
    db.sync_sources(conn, [make_source(key=k, url=f"https://example.edu.pk/{k}") for k in keys])


# ---------- sync_sources ----------


def test_sync_inserts_updates_and_deactivates(conn: Conn) -> None:
    _sync_example(conn, "a", "b")
    assert _source_row(conn, "a")["active"] is True

    renamed = make_source(key="a", url="https://example.edu.pk/a", name="New name", type="lead")
    db.sync_sources(conn, [renamed])

    row_a = _source_row(conn, "a")
    assert (row_a["name"], row_a["type"], row_a["active"]) == ("New name", "lead", True)
    assert _source_row(conn, "b")["active"] is False  # gone from the YAML -> inactive


def test_sync_keeps_inactive_flag_from_yaml(conn: Conn) -> None:
    db.sync_sources(conn, [make_source(key="a", active=False, note="Phase 2")])
    row = _source_row(conn, "a")
    assert (row["active"], row["note"]) == (False, "Phase 2")


# ---------- save_result and last_hashes ----------


def test_new_page_saves_snapshot_and_hash(conn: Conn) -> None:
    _sync_example(conn, "a")
    assert db.last_hashes(conn) == {}

    result = make_result("a", "Deadline: 15 January 2027.", change=Change.NEW)
    db.save_result(conn, result)

    assert db.last_hashes(conn) == {"a": result.content_hash}
    snap = conn.execute(
        "select text_gz, char_count, content_hash, http_status from snapshots"
    ).fetchone()
    assert snap is not None
    assert isinstance(snap[0], bytes)
    assert gzip.decompress(snap[0]).decode("utf-8") == "Deadline: 15 January 2027."
    assert snap[1:] == (len("Deadline: 15 January 2027."), result.content_hash, 200)

    row = _source_row(conn, "a")
    assert row["last_fetched_at"] is not None
    assert row["last_http_status"] == 200


def test_unchanged_page_adds_no_snapshot_but_resets_failures(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_result(conn, make_result("a", "Text", change=Change.NEW))
    db.save_result(conn, make_result("a", status=FetchStatus.FAILED, error="HTTP 500"))

    db.save_result(conn, make_result("a", "Text", change=Change.UNCHANGED))

    assert _snapshot_count(conn, "a") == 1
    row = _source_row(conn, "a")
    assert (row["consecutive_failures"], row["last_error"]) == (0, None)


def test_changed_page_adds_snapshot(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_result(conn, make_result("a", "Text v1", change=Change.NEW))
    changed = make_result("a", "Text v2", change=Change.CHANGED)
    db.save_result(conn, changed)

    assert _snapshot_count(conn, "a") == 2
    assert db.last_hashes(conn) == {"a": changed.content_hash}


def test_failures_are_counted(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_result(conn, make_result("a", status=FetchStatus.FAILED, error="HTTP 500"))
    db.save_result(conn, make_result("a", status=FetchStatus.FAILED, error="ConnectError"))

    row = _source_row(conn, "a")
    assert (row["consecutive_failures"], row["last_error"]) == (2, "ConnectError")


def test_blocked_by_robots_is_recorded(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_result(conn, make_result("a", status=FetchStatus.BLOCKED_ROBOTS))

    row = _source_row(conn, "a")
    assert row["robots_allowed"] is False
    assert row["robots_checked_at"] is not None


# ---------- runs ----------


def test_run_is_recorded(conn: Conn) -> None:
    _sync_example(conn, "a", "b", "c")
    run_id = db.start_run(conn, "fetch")
    results = [
        make_result("a", change=Change.NEW),
        make_result("b", change=Change.UNCHANGED),
        make_result("c", status=FetchStatus.FAILED, error="HTTP 404"),
    ]

    db.finish_run(conn, run_id, results)

    row = conn.execute(
        "select status::text, sources_total, sources_changed, sources_failed, finished_at"
        " from runs where id = %s",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert row[:4] == ("partial", 3, 1, 1)
    assert row[4] is not None


def test_run_with_error_is_failed(conn: Conn) -> None:
    run_id = db.start_run(conn)
    db.finish_run(conn, run_id, [], error="KeyboardInterrupt")
    row = conn.execute("select status::text, error from runs where id = %s", (run_id,)).fetchone()
    assert row == ("failed", "KeyboardInterrupt")


# ---------- run_status (no database needed) ----------


def test_run_status() -> None:
    ok = make_result("a")
    failed = make_result("b", status=FetchStatus.FAILED)
    skipped = make_result("c", status=FetchStatus.SKIPPED_JS)

    assert db.run_status([ok, skipped]) == "success"
    assert db.run_status([ok, failed]) == "partial"
    assert db.run_status([failed, failed]) == "failed"
    assert db.run_status([]) == "success"
