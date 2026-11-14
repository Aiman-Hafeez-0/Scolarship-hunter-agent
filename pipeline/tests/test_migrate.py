"""Tests for pipeline.migrate.

The database test needs a real Postgres in TEST_DATABASE_URL (CI provides one) and is skipped
otherwise. It works inside a throwaway schema, so it never touches existing tables.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from pipeline.migrate import MIGRATIONS_DIR, apply_migrations, migration_files

EXPECTED_TABLES = {
    "profiles",
    "sources",
    "snapshots",
    "scholarships",
    "evidence",
    "deadlines",
    "requirements",
    "required_documents",
    "matches",
    "changes",
    "notifications",
    "runs",
    "schema_migrations",
}


def test_migration_files_are_sorted(tmp_path: Path) -> None:
    for name in ["0002_b.sql", "0001_a.sql", "notes.txt", "0010_c.sql"]:
        (tmp_path / name).write_text("")

    assert [p.name for p in migration_files(tmp_path)] == ["0001_a.sql", "0002_b.sql", "0010_c.sql"]


def test_init_migration_exists() -> None:
    assert (MIGRATIONS_DIR / "0001_init.sql") in migration_files()


@pytest.fixture
def test_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A connection whose search_path points at a fresh schema, dropped afterwards."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")

    schema = f"migrate_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}") as conn:
            yield conn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


def test_migrate_twice_is_safe(test_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    first = apply_migrations(test_conn)
    second = apply_migrations(test_conn)

    assert "0001_init.sql" in first
    assert second == []

    rows = test_conn.execute(
        "select tablename, rowsecurity from pg_tables where schemaname = current_schema()"
    ).fetchall()
    rls_by_table = {str(name): bool(rls) for name, rls in rows}
    assert rls_by_table.keys() >= EXPECTED_TABLES
    assert all(rls_by_table[t] for t in EXPECTED_TABLES), "RLS must be on for every table"


def test_reminder_cannot_be_sent_twice(test_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    apply_migrations(test_conn)
    row = test_conn.execute(
        """
        with p as (insert into profiles (full_name, email) values ('A', 'a@example.com')
                   returning id),
             s as (insert into sources (key, url, name, opportunity_type, location)
                   values ('k', 'https://example.com', 'S', 'scholarship', 'abroad')
                   returning id),
             sch as (insert into scholarships (source_id, name, opportunity_type, location)
                     select id, 'X', 'scholarship', 'abroad' from s returning id),
             d as (insert into deadlines (scholarship_id, label, deadline_at)
                   select id, 'Apply', now() from sch returning id)
        select p.id, d.id from p, d
        """
    ).fetchone()
    assert row is not None
    insert = "insert into notifications (profile_id, deadline_id, kind) values (%s, %s, 'r7')"

    test_conn.execute(insert, row)
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_conn.execute(insert, row)
