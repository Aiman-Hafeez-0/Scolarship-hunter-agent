"""Tests for pipeline.db against a real Postgres (TEST_DATABASE_URL; skipped if not set)."""

import gzip
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
import respx

from pipeline import db
from pipeline.config import Settings
from pipeline.graph import SourceOutcome, run_sources
from pipeline.llm.provider import ProviderError
from pipeline.migrate import apply_migrations
from pipeline.models import Change, FetchStatus
from pipeline.nodes.detect_change import detect_changes
from pipeline.nodes.fetch import fetch_all
from pipeline.nodes.validate import ValidatedScholarship, validate_scholarship
from pipeline.profile import Profile
from pipeline.schemas import DeadlineItem, PageExtraction, Requirement, ScholarshipExtraction
from pipeline.tests.conftest import FakeProvider, make_result, make_source

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


def _age_run(conn: Conn, run_id: object, hours: float) -> None:
    conn.execute("update runs set started_at = now() - %s where id = %s",
                 (timedelta(hours=hours), run_id))  # fmt: skip


def test_stale_running_runs_are_marked_failed(conn: Conn) -> None:
    # Create every run before ageing any of them: start_run() itself calls fail_stale_runs(),
    # so if "stuck" were already 3 h old when the next run starts, start_run() would mark it
    # failed and the fail_stale_runs() call below would find nothing left to do.
    stuck = db.start_run(conn, "weekly")
    recent = db.start_run(conn, "weekly")
    done = db.start_run(conn, "fetch")
    db.finish_run(conn, done, [])
    _age_run(conn, stuck, 3)
    _age_run(conn, recent, 1)
    _age_run(conn, done, 5)

    assert db.fail_stale_runs(conn) == 1

    rows = {row[0]: row[1] for row in _rows(conn, "select id, status::text from runs")}
    assert rows[stuck] == "failed"
    assert rows[recent] == "running"
    assert rows[done] == "success"
    row = conn.execute("select error, finished_at from runs where id = %s", (stuck,)).fetchone()
    assert row is not None
    assert row[0] == "stopped before finishing"
    assert row[1] is not None


def test_start_run_cleans_up_stale_runs(conn: Conn) -> None:
    stuck = db.start_run(conn, "weekly")
    _age_run(conn, stuck, 2.5)
    db.start_run(conn, "weekly")
    row = conn.execute("select status::text from runs where id = %s", (stuck,)).fetchone()
    assert row == ("failed",)


def test_run_error_hides_password(conn: Conn) -> None:
    run_id = db.start_run(conn)
    db.finish_run(conn, run_id, [], error="OperationalError: postgresql://u:s3cret@host/db")
    row = conn.execute("select error from runs where id = %s", (run_id,)).fetchone()
    assert row == ("OperationalError: postgresql://u:***@host/db",)


# ---------- run_status (no database needed) ----------


def test_run_status() -> None:
    ok = make_result("a")
    failed = make_result("b", status=FetchStatus.FAILED)
    skipped = make_result("c", status=FetchStatus.SKIPPED_JS)

    assert db.run_status([ok, skipped]) == "success"
    assert db.run_status([ok, failed]) == "partial"
    assert db.run_status([failed, failed]) == "failed"
    assert db.run_status([]) == "success"


# ---------- latest_snapshot ----------


def test_latest_snapshot(conn: Conn) -> None:
    _sync_example(conn, "a", "b")
    assert db.latest_snapshot(conn, "a") is None

    db.save_result(conn, make_result("a", "Text v1", change=Change.NEW))
    db.save_result(conn, make_result("a", "Text v2 with ü", change=Change.CHANGED))

    snapshot = db.latest_snapshot(conn, "a")
    assert snapshot is not None
    assert snapshot.text == "Text v2 with ü"
    assert db.latest_snapshot(conn, "b") is None


# ---------- save_extraction ----------

PAGE = "Applications close on 15 January 2027. Applicants need a CGPA of 3.0 or higher."


def _validated(
    status: str = "open", deadline_day: int = 15, cgpa: str = "3.0"
) -> ValidatedScholarship:
    item = ScholarshipExtraction(
        name="MS Admission",
        opportunity_type="admission",
        location="pakistan",
        status=status,
        deadlines=[DeadlineItem(label="Application deadline",
                                deadline_date=date(2027, 1, deadline_day),
                                evidence="Applications close on 15 January 2027.")],
        requirements=[Requirement(kind="cgpa_min", value=cgpa, description="Minimum CGPA",
                                  evidence="Applicants need a CGPA of 3.0 or higher.")],
        required_documents=["Transcript", "CNIC"],
    )  # fmt: skip
    return validate_scholarship(item, PAGE, date(2026, 11, 15))


def _rows(conn: Conn, query: str) -> list[tuple[object, ...]]:
    return conn.execute(query).fetchall()


def test_save_extraction_writes_all_tables(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_result(conn, make_result("a", PAGE, change=Change.NEW))

    assert db.save_extraction(conn, "a", [_validated()], "extract_v2+fake") == 1

    row = conn.execute(
        "select name, status::text, extraction_version, needs_review, snapshot_id is not null"
        " from scholarships"
    ).fetchone()
    assert row == ("MS Admission", "open", "extract_v2+fake", False, True)
    assert _rows(conn, "select label, deadline_date, evidence_id is not null from deadlines") == [
        ("Application deadline", date(2027, 1, 15), True)
    ]
    assert _rows(conn, "select kind, value from requirements") == [("cgpa_min", "3.0")]
    documents = _rows(conn, "select name from required_documents order by name")
    assert documents == [("CNIC",), ("Transcript",)]
    assert sorted(_rows(conn, "select field_path, match_score from evidence")) == [
        ("deadlines[0]", 100),
        ("requirements[0]", 100),
    ]
    assert _rows(conn, "select * from changes") == []  # first time: nothing to compare with


def test_second_save_replaces_details_and_records_changes(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_extraction(conn, "a", [_validated()], "v1")

    db.save_extraction(conn, "a", [_validated(status="closed", cgpa="3.5")], "v2")

    assert _rows(conn, "select count(*) from scholarships") == [(1,)]  # upserted, not added
    assert _rows(conn, "select value from requirements") == [("3.5",)]  # replaced
    assert _rows(conn, "select count(*) from evidence") == [(2,)]
    changes = _rows(conn, "select field_path, old_value, new_value from changes order by 1")
    assert changes == [
        ("requirements", [{"kind": "cgpa_min", "value": "3.0", "description": "Minimum CGPA"}],
                         [{"kind": "cgpa_min", "value": "3.5", "description": "Minimum CGPA"}]),
        ("status", "open", "closed"),
    ]  # fmt: skip


def test_unchanged_extraction_records_no_changes(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_extraction(conn, "a", [_validated()], "v1")
    db.save_extraction(conn, "a", [_validated()], "v1")
    assert _rows(conn, "select count(*) from changes") == [(0,)]


def test_deadlines_keep_their_id_while_label_and_date_stay(conn: Conn) -> None:
    """Reminders are recorded per deadline id, so re-extracting must not change it."""
    _sync_example(conn, "a")
    db.save_extraction(conn, "a", [_validated()], "v1")
    [(first_id,)] = _rows(conn, "select id from deadlines")

    db.save_extraction(conn, "a", [_validated(cgpa="3.5")], "v2")  # same deadline
    assert _rows(conn, "select id, evidence_id is not null from deadlines") == [(first_id, True)]

    # The date changed (validation empties it: 20 January is not in the quote): a new row.
    db.save_extraction(conn, "a", [_validated(deadline_day=20)], "v3")
    [(new_id, day)] = _rows(conn, "select id, deadline_date from deadlines")
    assert new_id != first_id and day is None


def test_review_notes_are_saved(conn: Conn) -> None:
    _sync_example(conn, "a")
    unverified = _validated(deadline_day=25)  # 25 January is not in the quote

    db.save_extraction(conn, "a", [unverified], "v1")

    row = conn.execute("select needs_review, review_notes from scholarships").fetchone()
    assert row is not None
    assert row[0] is True
    assert "not written in its quote" in str(row[1])
    assert _rows(conn, "select deadline_date from deadlines") == [(None,)]


def test_store_saves_snapshot_and_extraction_in_one_transaction(conn: Conn) -> None:
    _sync_example(conn, "a")
    store = db.PostgresStore(conn)
    result = make_result("a", PAGE, change=Change.NEW)
    broken = _validated()
    broken.item = broken.item.model_copy(update={"name": None})  # violates "name not null"

    with pytest.raises(psycopg.errors.NotNullViolation):
        store.save(result, [broken], "v1", mark_extracted=True)

    assert _snapshot_count(conn, "a") == 0  # the snapshot was rolled back too
    assert db.last_hashes(conn) == {}
    assert db.last_extracted_hashes(conn) == {}

    assert store.save(result, [_validated()], "v1", mark_extracted=False) == 1
    assert _snapshot_count(conn, "a") == 1
    assert db.last_hashes(conn) == {"a": result.content_hash}  # it was fetched...
    assert db.last_extracted_hashes(conn) == {}  # ...but not fully extracted: retried next run

    assert store.save(result, [_validated()], "v1", mark_extracted=True) == 1
    assert db.last_extracted_hashes(conn) == {"a": result.content_hash}
    assert _snapshot_count(conn, "a") == 1  # same text: no second snapshot


# ---------- weekly vs fetch: last_extracted_hash ----------

WEEKLY_URL = "https://example.edu.pk/a"
WEEKLY_HTML = f"<html><body><p>{PAGE}</p></body></html>"


def _page_extraction() -> PageExtraction:
    return PageExtraction(scholarships=[_validated().item])


async def _fetch_command(conn: Conn, settings: Settings) -> None:
    """What `pipeline.run fetch` does: fetch, compare with last_hash, save. No LLM."""
    fetched = await fetch_all([make_source(key="a", url=WEEKLY_URL)], settings)
    for result in detect_changes(fetched, db.last_hashes(conn)):
        db.save_result(conn, result)


async def _weekly(conn: Conn, settings: Settings, provider: FakeProvider) -> SourceOutcome:
    """What `pipeline.run weekly` does for one source (compares with last_extracted_hash)."""
    [outcome] = await run_sources(
        [make_source(key="a", url=WEEKLY_URL)], settings, provider, db.PostgresStore(conn),
        db.last_extracted_hashes(conn), today=date(2026, 11, 15),
    )  # fmt: skip
    return outcome


@pytest.fixture
def weekly_site(mock_http: respx.MockRouter) -> respx.MockRouter:
    mock_http.get("https://example.edu.pk/robots.txt").respond(404)
    mock_http.get(WEEKLY_URL).respond(200, html=WEEKLY_HTML)
    return mock_http


async def test_page_fetched_but_never_extracted_is_extracted_by_weekly(
    conn: Conn, settings: Settings, weekly_site: respx.MockRouter
) -> None:
    _sync_example(conn, "a")
    await _fetch_command(conn, settings)
    assert "a" in db.last_hashes(conn)  # fetch remembered the page...
    assert db.last_extracted_hashes(conn) == {}  # ...but never extracted it

    provider = FakeProvider(_page_extraction())
    outcome = await _weekly(conn, settings, provider)

    assert len(provider.calls) == 1  # sent to the LLM even though last_hash matches
    assert outcome.saved == 1
    assert db.last_extracted_hashes(conn) == db.last_hashes(conn)
    assert _snapshot_count(conn, "a") == 1  # fetch already stored this text


async def test_extracted_unchanged_page_is_skipped(
    conn: Conn, settings: Settings, weekly_site: respx.MockRouter
) -> None:
    _sync_example(conn, "a")
    await _weekly(conn, settings, FakeProvider(_page_extraction()))
    await _fetch_command(conn, settings)  # a fetch in between changes nothing for weekly

    provider = FakeProvider()  # would fail if it were called
    outcome = await _weekly(conn, settings, provider)

    assert provider.calls == []
    assert outcome.fetched.change is Change.UNCHANGED
    assert outcome.extracted is None


async def test_failed_extraction_is_retried_next_run(
    conn: Conn, settings: Settings, weekly_site: respx.MockRouter
) -> None:
    _sync_example(conn, "a")
    first = await _weekly(conn, settings, FakeProvider(ProviderError("quota")))
    assert first.extracted is not None and first.extracted.errors
    assert db.last_extracted_hashes(conn) == {}  # failure: the old value (none) is kept

    provider = FakeProvider(_page_extraction())
    second = await _weekly(conn, settings, provider)

    assert len(provider.calls) == 1  # same page, but tried again
    assert second.saved == 1
    assert db.last_extracted_hashes(conn) == {"a": second.fetched.content_hash}


def test_weekly_run_totals_are_recorded(conn: Conn) -> None:
    run_id = db.start_run(conn, "weekly")
    totals = db.RunTotals(tokens_in=1000, tokens_out=200, items_saved=3, items_needing_review=1)
    db.finish_run(conn, run_id, [make_result("a", change=Change.NEW)], totals=totals)
    row = conn.execute(
        "select llm_tokens_in, llm_tokens_out, items_saved, items_needing_review, sources_changed"
        " from runs where id = %s",
        (run_id,),
    ).fetchone()
    assert row == (1000, 200, 3, 1, 1)


# ---------- changed_fields (no database needed) ----------


def test_changed_fields() -> None:
    old = {"status": "open", "intake": "Fall 2027", "campus": None}
    new = {"status": "closed", "intake": "Fall 2027", "campus": "Islamabad"}
    assert db.changed_fields(old, new) == [
        ("campus", None, "Islamabad"),
        ("status", "open", "closed"),
    ]


def test_comparable_ignores_quotes() -> None:
    first = _validated().item
    reworded = first.model_copy(
        update={"deadlines": [first.deadlines[0].model_copy(update={"evidence": "other words"})]}
    )
    assert db.comparable(first) == db.comparable(reworded)
    assert "name" not in db.comparable(first)


# ---------- profiles and matches ----------


def _profile(**fields: Any) -> Profile:
    data: dict[str, Any] = {"full_name": "Test Student", "email": "s@example.com",
                            "date_of_birth": date(2000, 1, 1), "cgpa": 3.25}  # fmt: skip
    data.update(fields)
    return Profile.model_validate(data)


def test_upsert_profile_by_email(conn: Conn) -> None:
    first = db.upsert_profile(conn, _profile())
    second = db.upsert_profile(conn, _profile(full_name="New Name", cgpa=3.5))

    assert first == second  # same email: updated, not added
    [(profile_id, loaded)] = db.load_profiles(conn)
    assert profile_id == str(first)
    assert (loaded.full_name, loaded.cgpa) == ("New Name", 3.5)
    assert loaded.reminder_days == [30, 14, 7, 2]


def test_load_open_scholarships_and_save_matches(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_extraction(conn, "a", [_validated()], "v1")  # status open, CGPA requirement 3.0
    closed = _validated(status="closed")
    closed.item = closed.item.model_copy(update={"name": "Old Call"})
    db.save_extraction(conn, "a", [closed], "v1")
    profile_id = str(db.upsert_profile(conn, _profile()))

    [facts] = db.load_open_scholarships(conn)
    assert facts.name == "MS Admission"
    assert facts.location in ("abroad", "pakistan")
    assert facts.deadlines == [date(2027, 1, 15)]
    assert [(r.kind, r.value) for r in facts.requirements] == [("cgpa_min", "3.0")]

    row = db.MatchRow(profile_id, facts.id or "", "maybe", 60, [{"rule": "cgpa"}])
    db.save_matches(conn, [row])
    db.save_matches(conn, [db.MatchRow(profile_id, facts.id or "", "eligible", 70, [])])
    assert _rows(conn, "select status::text, score, reasons from matches") == [
        ("eligible", 70, [])  # upserted
    ]


def test_matches_for_closed_scholarships_are_removed(conn: Conn) -> None:
    _sync_example(conn, "a")
    db.save_extraction(conn, "a", [_validated()], "v1")
    profile_id = str(db.upsert_profile(conn, _profile()))
    [facts] = db.load_open_scholarships(conn)
    db.save_matches(conn, [db.MatchRow(profile_id, facts.id or "", "maybe", 50, [])])

    db.save_extraction(conn, "a", [_validated(status="closed")], "v2")  # the call closed
    db.save_matches(conn, [])

    assert _rows(conn, "select count(*) from matches") == [(0,)]
