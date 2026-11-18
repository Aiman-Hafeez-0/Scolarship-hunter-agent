"""Tests for the digest's Postgres queries (TEST_DATABASE_URL; skipped if not set)."""

from collections.abc import Iterator
from datetime import date, datetime

import psycopg
import pytest

from pipeline import db
from pipeline.migrate import apply_migrations
from pipeline.notify.store import PostgresDigestStore, PostgresNotifyStore

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def conn(test_conn: Conn) -> Iterator[Conn]:
    apply_migrations(test_conn)
    yield test_conn


def _one(conn: Conn, query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _seed(conn: Conn) -> tuple[str, str]:
    """A profile, a source and one open scholarship with a deadline, a match and a change."""
    profile = _one(conn, "insert into profiles (full_name, email) values ('A', 'a@x.org')"
                         " returning id")  # fmt: skip
    conn.execute("insert into profiles (full_name, email, emails_paused)"
                 " values ('B', 'b@x.org', true)")  # fmt: skip
    source = _one(conn, "insert into sources (key, url, name, opportunity_type, location)"
                        " values ('k', 'https://x.org/p', 'X', 'scholarship', 'abroad')"
                        " returning id")  # fmt: skip
    sch = _one(conn, "insert into scholarships (source_id, name, opportunity_type, location,"
                     " status) values (%s, 'Alpha', 'scholarship', 'abroad', 'open')"
                     " returning id", source)  # fmt: skip
    conn.execute("insert into scholarships (source_id, name, opportunity_type, location, status)"
                 " values (%s, 'Gone', 'scholarship', 'abroad', 'closed')", (source,))  # fmt: skip
    conn.execute("insert into deadlines (scholarship_id, label, deadline_date, timezone_note)"
                 " values (%s, 'Apply', '2027-01-15', '23:59 CET')", (sch,))  # fmt: skip
    conn.execute(
        "insert into matches (profile_id, scholarship_id, status, score, reasons)"
        """ values (%s, %s, 'maybe', 50, '[{"outcome": "unknown"}]')""",
        (profile, sch),
    )
    conn.execute("insert into changes (scholarship_id, field_path, old_value, new_value)"
                 """ values (%s, 'intake', null, '"Fall 2027"')""", (sch,))  # fmt: skip
    conn.execute("insert into runs (kind, status, finished_at, sources_total)"
                 " values ('weekly', 'success', now(), 17)")  # fmt: skip
    return str(profile), str(sch)


def test_loads_profiles_items_changes_and_run(conn: Conn) -> None:
    profile_id, sch_id = _seed(conn)
    store = PostgresDigestStore(conn)

    assert [(p.email, p.emails_paused) for p in store.load_profiles()] == [
        ("a@x.org", False), ("b@x.org", True)]  # fmt: skip
    [item] = store.load_items(profile_id)  # the closed one is left out
    assert (item.name, item.match_status, item.source_url) == ("Alpha", "maybe", "https://x.org/p")
    assert item.first_matched_at is not None
    assert [(d.deadline_date, d.timezone_note) for d in item.deadlines] == [
        (date(2027, 1, 15), "23:59 CET")]  # fmt: skip
    [ch] = store.load_unnotified_changes()
    assert (ch.scholarship_name, ch.old_value, ch.new_value) == ("Alpha", None, "Fall 2027")
    run = store.last_run()
    assert run is not None and run.sources_total == 17
    assert store.last_digest_at(profile_id) is None


def test_record_digest_logs_email_and_marks_changes(conn: Conn) -> None:
    profile_id, _ = _seed(conn)
    store = PostgresDigestStore(conn)
    [ch] = store.load_unnotified_changes()

    store.record_digest(profile_id, "<id@gmail.com>", [ch.id])

    assert store.load_unnotified_changes() == []
    assert store.last_digest_at(profile_id) is not None
    row = conn.execute("select kind, channel, status, provider_message_id from notifications"
                       ).fetchone()  # fmt: skip
    assert row == ("digest", "email", "sent", "<id@gmail.com>")


def test_matching_again_keeps_first_matched_at(conn: Conn) -> None:
    profile_id, sch_id = _seed(conn)
    before = _one(conn, "select first_matched_at from matches")
    db.save_matches(conn, [db.MatchRow(profile_id, sch_id, "eligible", 70, [])])
    assert _one(conn, "select status::text from matches") == "eligible"
    assert _one(conn, "select first_matched_at from matches") == before


# ---------- Reminders and admin alerts ----------


def test_profile_reminder_days_and_deadline_ids_are_loaded(conn: Conn) -> None:
    profile_id, _ = _seed(conn)
    store = PostgresNotifyStore(conn)
    assert store.load_profiles()[0].reminder_days == [30, 14, 7, 2]
    [item] = store.load_items(profile_id)
    assert item.deadlines[0].id == str(_one(conn, "select id from deadlines"))


def test_a_reminder_can_be_claimed_only_once(conn: Conn) -> None:
    profile_id, sch_id = _seed(conn)
    deadline_id = str(_one(conn, "select id from deadlines"))
    store = PostgresNotifyStore(conn)

    claim = store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d")
    assert claim is not None
    assert store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d") is None
    assert store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_2d") is not None

    store.mark_sent(claim, "msg-1")
    row = conn.execute("select status, provider_message_id from notifications where id = %s",
                       (claim,)).fetchone()  # fmt: skip
    assert row == ("sent", "msg-1")


def test_released_reminder_can_be_claimed_again(conn: Conn) -> None:
    profile_id, sch_id = _seed(conn)
    deadline_id = str(_one(conn, "select id from deadlines"))
    store = PostgresNotifyStore(conn)
    claim = store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d")
    assert claim is not None
    store.release(claim)
    assert store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d") is not None


def test_admin_alert_is_claimed_once_until_resolved(conn: Conn) -> None:
    store = PostgresNotifyStore(conn)
    assert store.claim_alert("no_weekly_run") is not None
    assert store.claim_alert("no_weekly_run") is None
    assert store.open_alerts() == ["no_weekly_run"]

    store.resolve_alert("no_weekly_run")
    assert store.open_alerts() == []
    assert store.claim_alert("no_weekly_run") is not None  # re-armed
    assert _one(conn, "select count(*) from notifications where kind = 'admin'") == 2


def test_admin_check_queries(conn: Conn) -> None:
    _seed(conn)  # one successful weekly run, finished now
    conn.execute("insert into runs (kind, status, finished_at, llm_tokens_in, llm_tokens_out,"
                 " error) values ('fetch', 'failed', now() + interval '1 minute', 70, 30,"
                 " 'boom')")  # fmt: skip
    conn.execute("insert into runs (kind, status) values ('weekly', 'running')")
    conn.execute("insert into runs (kind, status, started_at, finished_at, llm_tokens_in)"
                 " values ('weekly', 'failed', now() - interval '60 days',"
                 " now() - interval '60 days', 5000)")  # fmt: skip
    conn.execute("update sources set consecutive_failures = 2, last_error = 'HTTP 503'")
    store = PostgresNotifyStore(conn)

    run = store.last_finished_run()
    assert run is not None and (run.kind, run.status, run.error) == ("fetch", "failed", "boom")
    [source] = store.failing_sources(2)
    assert (source.key, source.failures, source.last_error) == ("k", 2, "HTTP 503")
    assert store.failing_sources(3) == []
    assert store.last_good_weekly_at() is not None
    start = _one(conn, "select now() - interval '1 day'")
    assert isinstance(start, datetime)
    assert store.tokens_since(start) == 100  # the 60-day-old run is not counted


def test_delete_stale_pending_keeps_sent_and_recent_rows(conn: Conn) -> None:
    profile_id, sch_id = _seed(conn)
    deadline_id = str(_one(conn, "select id from deadlines"))
    store = PostgresNotifyStore(conn)
    old = store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d")
    sent = store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_2d")
    assert old is not None and sent is not None
    store.mark_sent(sent, "msg-1")
    store.claim_alert("no_weekly_run")  # pending, but recent
    conn.execute("update notifications set sent_at = now() - interval '2 hours'"
                 " where id in (%s, %s)", (old, sent))  # fmt: skip

    cutoff = _one(conn, "select now() - interval '1 hour'")
    assert isinstance(cutoff, datetime)
    assert store.delete_stale_pending(cutoff) == 1
    rows = conn.execute("select kind, status from notifications order by kind").fetchall()
    assert rows == [("admin", "pending"), ("reminder_2d", "sent")]
    assert store.claim_reminder(profile_id, sch_id, deadline_id, "reminder_7d") is not None
