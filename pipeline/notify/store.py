"""Everything the digest, reminders and admin alerts read from and write to Postgres.

PostgresDigestStore and PostgresNotifyStore are the real ones; tests use fakes with the same
methods (DigestStore, and ReminderStore / AlertStore in reminders.py and alerts.py).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from psycopg import errors

from pipeline.db import Conn

DEFAULT_REMINDER_DAYS = [30, 14, 7, 2]


@dataclass
class ProfileRow:
    id: str
    full_name: str
    email: str
    emails_paused: bool = False
    reminder_days: list[int] = field(default_factory=lambda: list(DEFAULT_REMINDER_DAYS))


@dataclass
class DeadlineRow:
    label: str
    deadline_date: date | None
    timezone_note: str | None = None
    is_estimated: bool = False
    id: str = ""


@dataclass
class ItemRow:
    """One scholarship that is not closed, with this profile's match (if any)."""

    id: str
    name: str
    opportunity_type: str
    location: str
    source_url: str
    needs_review: bool = False
    review_notes: list[str] = field(default_factory=list)
    deadlines: list[DeadlineRow] = field(default_factory=list)
    match_status: str | None = None  # None: not matched yet
    reasons: list[dict[str, Any]] = field(default_factory=list)
    first_matched_at: datetime | None = None
    # "submitted" or "skipped" stops reminders. Always None until application tracking exists.
    application_status: str | None = None


@dataclass
class ChangeRow:
    id: str
    scholarship_name: str
    source_url: str
    field_path: str
    old_value: Any
    new_value: Any


@dataclass
class RunInfo:
    sources_total: int
    finished_at: datetime


class DigestStore(Protocol):
    def load_profiles(self) -> list[ProfileRow]: ...
    def load_items(self, profile_id: str) -> list[ItemRow]: ...
    def load_unnotified_changes(self) -> list[ChangeRow]: ...
    def last_digest_at(self, profile_id: str) -> datetime | None: ...
    def last_run(self) -> RunInfo | None: ...
    def record_digest(self, profile_id: str, message_id: str, change_ids: list[str]) -> None: ...


class PostgresDigestStore:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def load_profiles(self) -> list[ProfileRow]:
        rows = self.conn.execute(
            "select id, full_name, email, emails_paused, reminder_days from profiles"
            " order by email"
        ).fetchall()
        return [ProfileRow(str(r[0]), str(r[1]), str(r[2]), bool(r[3]), list(r[4] or []))
                for r in rows]  # fmt: skip

    def load_items(self, profile_id: str) -> list[ItemRow]:
        rows = self.conn.execute(
            """
            select s.id, s.name, s.opportunity_type, s.location, src.url, s.needs_review,
                   s.review_notes, m.status::text, m.reasons, m.first_matched_at
            from scholarships s
            join sources src on src.id = s.source_id
            left join matches m on m.scholarship_id = s.id and m.profile_id = %s
            where s.status <> 'closed'
            order by s.name
            """,
            (profile_id,),
        ).fetchall()
        items = {
            str(r[0]): ItemRow(
                id=str(r[0]), name=str(r[1]), opportunity_type=str(r[2]), location=str(r[3]),
                source_url=str(r[4]), needs_review=bool(r[5]), review_notes=list(r[6]),
                match_status=None if r[7] is None else str(r[7]),
                reasons=list(r[8] or []), first_matched_at=r[9],
            )
            for r in rows
        }  # fmt: skip
        for sid, label, day, note, estimated, deadline_id in self.conn.execute(
            """
            select scholarship_id, label, deadline_date, timezone_note, is_estimated, id
            from deadlines where scholarship_id = any(%s)
            order by deadline_date nulls last, label
            """,
            (list(items),),
        ):
            items[str(sid)].deadlines.append(
                DeadlineRow(str(label), day, note, bool(estimated), str(deadline_id))
            )
        return list(items.values())

    def load_unnotified_changes(self) -> list[ChangeRow]:
        rows = self.conn.execute(
            """
            select c.id, s.name, src.url, c.field_path, c.old_value, c.new_value
            from changes c
            join scholarships s on s.id = c.scholarship_id
            join sources src on src.id = s.source_id
            where not c.notified
            order by s.name, c.detected_at, c.field_path
            """
        ).fetchall()
        return [ChangeRow(str(r[0]), str(r[1]), str(r[2]), str(r[3]), r[4], r[5]) for r in rows]

    def last_digest_at(self, profile_id: str) -> datetime | None:
        row = self.conn.execute(
            """
            select max(sent_at) from notifications
            where profile_id = %s and kind = 'digest' and status = 'sent'
            """,
            (profile_id,),
        ).fetchone()
        return None if row is None else row[0]

    def last_run(self) -> RunInfo | None:
        """The latest weekly run that finished (fully or partly) successfully."""
        row = self.conn.execute(
            """
            select sources_total, finished_at from runs
            where kind = 'weekly' and status in ('success', 'partial')
              and finished_at is not null
            order by finished_at desc limit 1
            """
        ).fetchone()
        return None if row is None else RunInfo(int(str(row[0])), row[1])

    def record_digest(self, profile_id: str, message_id: str, change_ids: list[str]) -> None:
        """After a successful send: log the email and mark its changes as notified."""
        with self.conn.transaction():
            self.conn.execute(
                """
                insert into notifications (profile_id, kind, channel, status,
                                           provider_message_id)
                values (%s, 'digest', 'email', 'sent', %s)
                """,
                (profile_id, message_id),
            )
            self.conn.execute(
                "update changes set notified = true where id = any(%s)",
                ([uuid.UUID(i) for i in change_ids],),
            )


# ---------- Reminders and admin alerts ----------


@dataclass
class FinishedRun:
    kind: str
    status: str
    finished_at: datetime
    error: str | None


@dataclass
class FailingSource:
    key: str
    url: str
    failures: int
    last_error: str | None


class PostgresNotifyStore(PostgresDigestStore):
    """Adds what reminders and admin alerts need to the digest store."""

    def _claim(self, query: str, params: tuple[object, ...]) -> str | None:
        """Insert a 'pending' notification. None if the unique index says it already exists."""
        try:
            with self.conn.transaction():  # a savepoint: a duplicate rolls back just this insert
                row = self.conn.execute(query, params).fetchone()
        except errors.UniqueViolation:
            return None
        assert row is not None
        return str(row[0])

    def claim_reminder(
        self, profile_id: str, scholarship_id: str, deadline_id: str, kind: str
    ) -> str | None:
        return self._claim(
            """
            insert into notifications (profile_id, scholarship_id, deadline_id, kind, status)
            values (%s, %s, %s, %s, 'pending') returning id
            """,
            (profile_id, scholarship_id, deadline_id, kind),
        )

    def claim_alert(self, key: str) -> str | None:
        return self._claim(
            """
            insert into notifications (kind, alert_key, status)
            values ('admin', %s, 'pending') returning id
            """,
            (key,),
        )

    def mark_sent(self, notification_id: str, message_id: str) -> None:
        self.conn.execute(
            "update notifications set status = 'sent', sent_at = now(),"
            " provider_message_id = %s where id = %s",
            (message_id, notification_id),
        )

    def delete_stale_pending(self, before: datetime) -> int:
        """Delete 'pending' rows claimed before this time: their run crashed mid-send."""
        cur = self.conn.execute(
            "delete from notifications where status = 'pending' and sent_at < %s", (before,)
        )
        return cur.rowcount

    def release(self, notification_id: str) -> None:
        """Sending failed: delete the claim so the next run tries again."""
        self.conn.execute("delete from notifications where id = %s", (notification_id,))

    def open_alerts(self) -> list[str]:
        rows = self.conn.execute(
            "select alert_key from notifications"
            " where kind = 'admin' and resolved_at is null order by alert_key"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def resolve_alert(self, key: str) -> None:
        self.conn.execute(
            "update notifications set resolved_at = now()"
            " where kind = 'admin' and alert_key = %s and resolved_at is null",
            (key,),
        )

    def last_finished_run(self) -> FinishedRun | None:
        """The most recent run of any kind that is no longer running."""
        row = self.conn.execute(
            """
            select kind, status::text, finished_at, error from runs
            where status <> 'running' and finished_at is not null
            order by finished_at desc limit 1
            """
        ).fetchone()
        return None if row is None else FinishedRun(str(row[0]), str(row[1]), row[2], row[3])

    def failing_sources(self, min_failures: int) -> list[FailingSource]:
        rows = self.conn.execute(
            """
            select key, url, consecutive_failures, last_error from sources
            where active and consecutive_failures >= %s order by key
            """,
            (min_failures,),
        ).fetchall()
        return [FailingSource(str(r[0]), str(r[1]), int(str(r[2])), r[3]) for r in rows]

    def last_good_weekly_at(self) -> datetime | None:
        """When the latest weekly run that worked (success or partial) finished."""
        row = self.conn.execute(
            "select max(finished_at) from runs"
            " where kind = 'weekly' and status in ('success', 'partial')"
        ).fetchone()
        return None if row is None else row[0]

    def tokens_since(self, start: datetime) -> int:
        row = self.conn.execute(
            "select coalesce(sum(llm_tokens_in + llm_tokens_out), 0) from runs"
            " where started_at >= %s",
            (start,),
        ).fetchone()
        return 0 if row is None else int(str(row[0]))
