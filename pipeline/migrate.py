"""Apply the SQL files in db/migrations/ to the database, in order.

Run it with:  uv run python -m pipeline.migrate

Each file runs inside its own transaction: either the whole file is applied or none of it is.
Applied file names are recorded in the schema_migrations table, so running this again only
applies files that are new. That makes it safe to run as often as you like.
"""

import sys
from pathlib import Path

import psycopg

from pipeline.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

# The bookkeeping table. RLS is on (with no policies) for the same reason as in 0001_init.sql:
# it hides the table from Supabase's public Data API.
CREATE_TRACKING_TABLE = b"""
create table if not exists schema_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;
"""


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return the .sql files sorted by name (0001_..., 0002_..., ...)."""
    return sorted(migrations_dir.glob("*.sql"))


def apply_migrations(
    conn: psycopg.Connection[tuple[object, ...]], migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every migration not yet recorded. Returns the names of the files applied now."""
    with conn.transaction():
        conn.execute(CREATE_TRACKING_TABLE)
        rows = conn.execute("select filename from schema_migrations").fetchall()
    already_applied = {str(row[0]) for row in rows}

    applied_now: list[str] = []
    for path in migration_files(migrations_dir):
        if path.name in already_applied:
            continue
        with conn.transaction():
            # Read as bytes: psycopg then sends the file as-is, with no parameter parsing.
            conn.execute(path.read_bytes())
            conn.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
        applied_now.append(path.name)
    return applied_now


def main() -> int:
    database_url = Settings().database_url
    if not database_url:
        print("DATABASE_URL is not set. Add it to your .env file.", file=sys.stderr)
        return 1

    # autocommit=True means nothing is wrapped implicitly; conn.transaction() controls it all.
    with psycopg.connect(database_url, autocommit=True) as conn:
        applied = apply_migrations(conn)

    if applied:
        for name in applied:
            print(f"Applied {name}")
    else:
        print("Database is already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
