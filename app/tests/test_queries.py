"""Tests for app.queries: the pure helpers, and the Postgres store (needs TEST_DATABASE_URL)."""

from datetime import date, timedelta

from psycopg.types.json import Jsonb

from app.queries import (
    Filters,
    PostgresDashboardStore,
    Quote,
    build_fields,
    deadline_tone,
    filter_rows,
    match_counts,
    parse_filters,
)
from app.tests.conftest import TODAY, Conn, row


def test_parse_filters_keeps_good_values_and_drops_bad_ones() -> None:
    f = parse_filters({"q": "  hungary ", "type": "scholarship", "location": "mars",
                       "match": "not_matched", "funding": "full",
                       "deadline_before": "2027-01-31", "include_closed": "1",
                       "sort": "score"})  # fmt: skip
    assert f == Filters(q="hungary", opportunity_type="scholarship", location=None,
                        match="not_matched", funding="full", deadline_before=date(2027, 1, 31),
                        include_closed=True, sort="score")  # fmt: skip
    assert parse_filters({"deadline_before": "31/01/2027", "sort": "drop table"}) == Filters()


def test_filters_round_trip_through_the_url() -> None:
    f = Filters(q="x", funding="full", deadline_before=date(2027, 1, 1), sort="score")
    assert parse_filters(f.as_query()) == f
    assert Filters().as_query() == {}


def test_deadline_before_includes_that_day_and_drops_undated() -> None:
    rows = [row(id="a", name="A", next_deadline=date(2027, 1, 1)),
            row(id="b", name="B", next_deadline=date(2027, 1, 2)),
            row(id="c", name="C", next_deadline=None)]  # fmt: skip
    kept = filter_rows(rows, Filters(deadline_before=date(2027, 1, 1)))
    assert [r.name for r in kept] == ["A"]


def test_match_counts_skip_closed() -> None:
    rows = [row(match_status="eligible"), row(match_status=None),
            row(match_status="eligible", status="closed")]  # fmt: skip
    assert match_counts(rows) == {"eligible": 1, "maybe": 0, "not_eligible": 0, "not_matched": 1}


def test_deadline_tone() -> None:
    assert [deadline_tone(d) for d in (0, 7, 8, 30, 31)] == [
        "red", "red", "orange", "orange", "gray"
    ]  # fmt: skip


def test_build_fields_attaches_quotes() -> None:
    fields = build_fields(
        {"funding": "full", "coverage": {"tuition": "full", "housing": None},
         "degree_levels": ["MS", "PhD"], "campus": None},
        {"coverage": [Quote("Tuition is fully covered.", 100)]},
    )  # fmt: skip
    by_label = {f.label: f for f in fields}
    assert by_label["Funding"].quotes[0].text == "Tuition is fully covered."
    assert by_label["Coverage"].value == "tuition: full"
    assert by_label["Degree levels"].value == "MS, PhD"
    assert by_label["Degree levels"].has_evidence and not by_label["Degree levels"].quotes
    assert by_label["Campus"].value == "" and not by_label["Campus"].has_evidence


# ---------- Postgres (skipped unless TEST_DATABASE_URL is set) ----------


def _one(conn: Conn, query: str, params: tuple[object, ...]) -> str:
    found = conn.execute(query, params).fetchone()
    assert found is not None
    return str(found[0])


def test_postgres_store_reads_everything(conn: Conn) -> None:
    source_id = _one(conn, "insert into sources (key, url, name, opportunity_type, location)"
                     " values ('sh', 'https://example.org/sh', 'Tempus', 'scholarship', 'abroad')"
                     " returning id", ())  # fmt: skip
    profile_id = _one(conn, "insert into profiles (full_name, email) values ('Owner',"
                      " 'owner@example.com') returning id", ())  # fmt: skip
    sid = _one(conn, """
        insert into scholarships (source_id, name, opportunity_type, location, funding,
                                  coverage, degree_levels, needs_review, review_notes)
        values (%s, 'Stipendium Hungaricum', 'scholarship', 'abroad', 'full',
                %s, '{MS}', true, '{"check the fee"}') returning id
        """, (source_id, Jsonb({"tuition": "full"})))  # fmt: skip
    _one(conn, "insert into evidence (scholarship_id, field_path, quote, match_score)"
         " values (%s, 'coverage', 'Full tuition waiver.', 100) returning id", (sid,))  # fmt: skip
    evidence_id = _one(conn, "insert into evidence (scholarship_id, field_path, quote,"
                       " match_score) values (%s, 'deadlines[0]', 'Apply by 15 January 2027.',"
                       " 97) returning id", (sid,))  # fmt: skip
    conn.execute("insert into deadlines (scholarship_id, label, deadline_date, evidence_id)"
                 " values (%s, 'Apply', %s, %s), (%s, 'Old round', %s, null)",
                 (sid, TODAY + timedelta(days=10), evidence_id, sid,
                  TODAY - timedelta(days=10)))  # fmt: skip
    conn.execute("insert into matches (profile_id, scholarship_id, status, score, reasons)"
                 " values (%s, %s, 'eligible', 85, %s)",
                 (profile_id, sid, Jsonb([{"rule": "nationality", "outcome": "pass",
                                           "reason": "open to Pakistan"}])))  # fmt: skip
    conn.execute("insert into changes (scholarship_id, field_path, old_value, new_value)"
                 " values (%s, 'status', %s, %s)", (sid, Jsonb("upcoming"), Jsonb("open")))
    conn.execute("insert into runs (kind, status, finished_at, sources_total)"
                 " values ('weekly', 'success', now(), 18)")
    store = PostgresDashboardStore(conn)

    assert store.profile_id("OWNER@example.com") == profile_id
    assert store.profile_id("nobody@example.com") == profile_id  # falls back to the first

    [listed] = store.scholarship_rows(profile_id, TODAY)
    assert (listed.name, listed.match_status, listed.score) == ("Stipendium Hungaricum",
                                                                "eligible", 85)  # fmt: skip
    assert listed.next_deadline == TODAY + timedelta(days=10)  # the past one is skipped
    assert listed.needs_review and not listed.verified
    assert store.scholarship_rows(None, TODAY)[0].match_status is None

    [upcoming] = store.upcoming_deadlines(TODAY, 5)
    assert upcoming.label == "Apply"

    detail = store.scholarship(profile_id, sid)
    assert detail is not None
    assert detail.review_notes == ["check the fee"]
    funding = next(f for f in detail.fields if f.label == "Funding")
    assert funding.quotes[0].text == "Full tuition waiver."
    first = next(d for d in detail.deadlines if d.label == "Apply")
    assert first.quote is not None and first.quote.text == "Apply by 15 January 2027."
    assert detail.match is not None and detail.match.score == 85
    assert detail.changes[0].new_value == "open"
    assert store.scholarship(profile_id, "not-a-uuid") is None

    [run] = store.runs(10)
    assert (run.kind, run.status, run.sources_total) == ("weekly", "success", 18)
    assert store.last_good_weekly_at() is not None


def test_postgres_store_actions(conn: Conn) -> None:
    from app.queries import ProfileEmailTaken
    from app.tests.conftest import profile
    from pipeline.models import Source

    store = PostgresDashboardStore(conn)
    new = Source(key="a-ms", name="A MS", url="https://www.example.edu/a", program_key="a-ms",
                 country="DE", opportunity_type="admission", location="abroad")  # fmt: skip

    assert store.add_source(new) is True
    assert store.add_source(new) is False  # same key and url
    [row_] = store.sources()
    assert (row_.key, row_.active, row_.robots_allowed) == ("a-ms", True, True)
    assert row_.robots_checked_at is not None
    toggled = store.set_source_active("a-ms", False)
    assert toggled is not None and toggled.active is False
    assert store.set_source_active("nope", True) is None

    sid = _one(conn, "insert into scholarships (source_id, name, opportunity_type, location)"
               " select id, 'A', 'admission', 'abroad' from sources returning id", ())  # fmt: skip
    assert store.verify(sid) is not None
    assert store.verify("11111111-1111-1111-1111-111111111111") is None
    assert store.verify("not-a-uuid") is None

    first_id = store.save_profile(None, profile(), paused=False)  # creates it
    record = store.profile(first_id)
    assert record is not None and record.profile == profile() and not record.emails_paused
    store.save_profile(first_id, profile(cgpa=3.6, reminder_days=[10]), paused=True)
    record = store.profile(first_id)
    assert record is not None and record.profile.cgpa == 3.6 and record.emails_paused
    assert record.profile.reminder_days == [10]

    other_id = store.save_profile(None, profile(email="other@example.com"), paused=False)
    try:
        store.save_profile(other_id, profile(), paused=False)  # first profile's email
    except ProfileEmailTaken:
        pass
    else:
        raise AssertionError("expected ProfileEmailTaken")
