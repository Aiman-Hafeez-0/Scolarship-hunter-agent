"""The small database pieces shared by the pipeline and the dashboard.

This module only imports psycopg and the profile model. The dashboard (app/) imports it
instead of pipeline.db, which pulls in the matching and validation code (and rapidfuzz).
Keeping app/ away from those heavy libraries keeps the Vercel function small.
"""

import uuid

import psycopg
from psycopg.rows import TupleRow

from pipeline.profile import Profile

Conn = psycopg.Connection[TupleRow]

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
