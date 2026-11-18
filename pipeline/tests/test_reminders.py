"""Tests for deadline reminders (fake store and fake mailer: no database, nothing sent)."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from pipeline.notify.mailer import MailError
from pipeline.notify.reminders import due_threshold, pkt_today, render_reminder, send_reminders
from pipeline.notify.store import DeadlineRow, ItemRow, ProfileRow

# 10:00 UTC = 15:00 PKT on Tue 17 Nov 2026.
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 17)
PROFILE = ProfileRow("p1", "Aisha Khan", "aisha@example.com")


class FakeMailer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail = fail

    def send(self, to: str, subject: str, text: str, html: str) -> str:
        if self.fail:
            raise MailError(f"Could not send email to {to}: HTTP 500")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return f"msg-{len(self.sent)}"


@dataclass
class FakeStore:
    """Behaves like the database: one notification per (profile, deadline, kind)."""

    profiles: list[ProfileRow]
    items: list[ItemRow]
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock: datetime = NOW  # when claims are made

    def load_profiles(self) -> list[ProfileRow]:
        return self.profiles

    def load_items(self, profile_id: str) -> list[ItemRow]:
        return self.items

    def claim_reminder(
        self, profile_id: str, scholarship_id: str, deadline_id: str, kind: str
    ) -> str | None:
        key = (profile_id, deadline_id, kind)
        if any(r["key"] == key for r in self.rows.values()):
            return None
        nid = f"n{len(self.rows) + 1}"
        self.rows[nid] = {"key": key, "status": "pending", "at": self.clock}
        return nid

    def mark_sent(self, notification_id: str, message_id: str) -> None:
        self.rows[notification_id]["status"] = "sent"

    def release(self, notification_id: str) -> None:
        del self.rows[notification_id]

    def delete_stale_pending(self, before: datetime) -> int:
        stale = [n for n, r in self.rows.items() if r["status"] == "pending" and r["at"] < before]
        for nid in stale:
            del self.rows[nid]
        return len(stale)

    def kinds(self) -> list[str]:
        return sorted(r["key"][2] for r in self.rows.values() if r["status"] == "sent")


def item(days: int | None = 10, *, status: str = "maybe", note: str | None = None,
         **overrides: Any) -> ItemRow:  # fmt: skip
    """An item with one deadline `days` after TODAY."""
    deadlines = [] if days is None else [
        DeadlineRow("Application deadline", TODAY + timedelta(days=days), note, id="d1")
    ]  # fmt: skip
    fields: dict[str, Any] = {
        "id": "s1", "name": "Alpha Scholarship", "opportunity_type": "scholarship",
        "location": "abroad", "source_url": "https://example.org/alpha", "deadlines": deadlines,
        "match_status": status,
        "reasons": [{"rule": "english", "outcome": "unknown", "reason": "IELTS 6.5 needed",
                     "action": "take IELTS or TOEFL"},
                    {"rule": "nationality", "outcome": "pass", "reason": "open to Pakistan"}],
    }  # fmt: skip
    fields.update(overrides)
    return ItemRow(**fields)


def run(store: FakeStore, now: datetime = NOW) -> FakeMailer:
    mailer = FakeMailer()
    send_reminders(store, mailer, now)
    return mailer


# ---------- Which threshold ----------


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [(31, None), (30, 30), (29, 30), (15, 30), (14, 14), (8, 14), (7, 7), (3, 7), (2, 2),
     (0, 2), (-1, None)],
)  # fmt: skip
def test_due_threshold(days_left: int, expected: int | None) -> None:
    assert due_threshold(days_left, [30, 14, 7, 2]) == expected


@pytest.mark.parametrize(("days", "kinds"), [(31, []), (30, ["reminder_30d"]),
                                             (14, ["reminder_14d"]), (2, ["reminder_2d"])])
def test_sends_when_a_threshold_is_reached(days: int, kinds: list[str]) -> None:
    store = FakeStore([PROFILE], [item(days)])
    mailer = run(store)
    assert store.kinds() == kinds
    assert len(mailer.sent) == len(kinds)


def test_missed_days_send_only_the_smallest_threshold() -> None:
    store = FakeStore([PROFILE], [item(5)])  # 30, 14 and 7 were all reached; nothing was sent
    mailer = run(store)
    assert store.kinds() == ["reminder_7d"]
    [email] = mailer.sent
    assert email["subject"] == "Reminder: Alpha Scholarship - 5 days left"


def test_bigger_thresholds_are_not_sent_later() -> None:
    store = FakeStore([PROFILE], [item(5)])
    run(store)
    mailer = run(store, NOW + timedelta(days=1))  # 4 days left: 7 is still the smallest reached
    assert mailer.sent == []
    assert store.kinds() == ["reminder_7d"]


def test_follows_each_threshold_day_by_day() -> None:
    store = FakeStore([PROFILE], [item(31)])
    for day in range(32):
        run(store, NOW + timedelta(days=day))
    assert store.kinds() == ["reminder_14d", "reminder_2d", "reminder_30d", "reminder_7d"]


def test_profile_reminder_days_are_used() -> None:
    profile = ProfileRow("p1", "A", "a@example.com", reminder_days=[10, 3])
    store = FakeStore([profile], [item(9), item(11, id="s2", deadlines=[
        DeadlineRow("Apply", TODAY + timedelta(days=11), id="d2")])])  # fmt: skip
    run(store)
    assert store.kinds() == ["reminder_10d"]  # the default days would have sent reminder_14d


# ---------- Pakistan time ----------


def test_today_changes_at_midnight_in_pakistan() -> None:
    assert pkt_today(datetime(2026, 11, 17, 18, 59, tzinfo=UTC)) == date(2026, 11, 17)
    assert pkt_today(datetime(2026, 11, 17, 19, 0, tzinfo=UTC)) == date(2026, 11, 18)


def test_midnight_boundary_in_pkt() -> None:
    """Deadline 25 Nov: 8 days left until 23:59 PKT on the 17th, 7 days from 00:00 PKT."""
    store = FakeStore([PROFILE], [item(8)])
    before = run(store, datetime(2026, 11, 17, 18, 59, tzinfo=UTC))
    assert store.kinds() == ["reminder_14d"]
    after = run(store, datetime(2026, 11, 17, 19, 0, tzinfo=UTC))
    assert store.kinds() == ["reminder_14d", "reminder_7d"]
    assert "8 days left" in before.sent[0]["subject"]
    assert "7 days left" in after.sent[0]["subject"]


def test_days_left_uses_the_deadline_in_pakistan_time() -> None:
    """23:00 CET on 23 Nov is 03:00 PKT on 24 Nov: 7 days left, not 6."""
    store = FakeStore([PROFILE], [item(6, note="23:00 CET")])
    mailer = run(store)
    assert store.kinds() == ["reminder_7d"]
    assert "7 days left" in mailer.sent[0]["subject"]


# ---------- Who and what gets reminders ----------


def test_second_run_sends_nothing() -> None:
    store = FakeStore([PROFILE], [item(5)])
    assert len(run(store).sent) == 1
    assert run(store).sent == []
    assert len(store.rows) == 1


def test_paused_profiles_get_nothing() -> None:
    paused = ProfileRow("p2", "B", "b@example.com", emails_paused=True)
    store = FakeStore([paused], [item(5)])
    assert run(store).sent == []
    assert store.rows == {}


def test_only_eligible_or_maybe_future_dated_deadlines() -> None:
    items = [
        item(5, id="a", status="not_eligible"),
        item(-1, id="b"),
        item(None, id="c"),
        item(5, id="d", deadlines=[DeadlineRow("Apply", None, id="d4")]),
        item(5, id="e", status="eligible", deadlines=[
            DeadlineRow("Apply", TODAY + timedelta(days=5), id="d5")]),
    ]  # fmt: skip
    store = FakeStore([PROFILE], items)
    run(store)
    assert [r["key"] for r in store.rows.values()] == [("p1", "d5", "reminder_7d")]


def test_entry_test_registration_deadlines_count() -> None:
    test = item(2, opportunity_type="entry_test", location="pakistan", name="HAT",
                deadlines=[DeadlineRow("Registration deadline", TODAY + timedelta(days=2),
                                       id="d9")])  # fmt: skip
    store = FakeStore([PROFILE], [test])
    mailer = run(store)
    assert store.kinds() == ["reminder_2d"]
    assert "Registration deadline" in mailer.sent[0]["text"]


@pytest.mark.parametrize("status", ["submitted", "skipped"])
def test_submitted_or_skipped_applications_stop_reminders(status: str) -> None:
    store = FakeStore([PROFILE], [item(5, application_status=status)])
    assert run(store).sent == []


def test_failed_send_is_retried_next_run() -> None:
    store = FakeStore([PROFILE], [item(5)])
    results = send_reminders(store, FakeMailer(fail=True), NOW)
    assert results[0].error is not None
    assert store.rows == {}  # the claim was released
    assert len(run(store).sent) == 1


def test_reminder_left_pending_by_a_crash_is_retried_after_an_hour() -> None:
    store = FakeStore([PROFILE], [item(5)])
    store.claim_reminder("p1", "s1", "d1", "reminder_7d")  # claimed at NOW, then the run died

    assert run(store, NOW + timedelta(minutes=59)).sent == []  # may still be sending
    mailer = run(store, NOW + timedelta(hours=1, minutes=1))
    assert len(mailer.sent) == 1
    assert [r["status"] for r in store.rows.values()] == ["sent"]


def test_sent_reminders_are_never_cleared() -> None:
    store = FakeStore([PROFILE], [item(5)])
    run(store)
    assert run(store, NOW + timedelta(hours=5)).sent == []
    assert len(store.rows) == 1


# ---------- Email content ----------


def test_email_has_deadline_in_pkt_and_original_zone_actions_and_link() -> None:
    store = FakeStore([PROFILE], [item(6, note="23:00 CET")])
    [email] = run(store).sent
    assert email["to"] == "aisha@example.com"
    text = email["text"]
    assert "Alpha Scholarship (Scholarship)" in text
    assert "Application deadline: Tue 24 Nov 2026, 03:00 PKT (23:00 CET, Mon 23 Nov 2026)" in text
    assert "7 days left." in text
    assert "- take IELTS or TOEFL" in text
    assert "open to Pakistan" not in text  # passed checks are not listed
    assert "Source: https://example.org/alpha" in text
    assert 'href="https://example.org/alpha"' in email["html"]


def test_email_without_open_questions_and_html_is_escaped() -> None:
    reminder_item = item(0, status="eligible", reasons=[], name="A <b> & C")
    store = FakeStore([PROFILE], [reminder_item])
    [email] = run(store).sent
    assert email["subject"] == "Reminder: A <b> & C - closes today"
    assert "Nothing missing that we know of" in email["text"]
    assert "A &lt;b&gt; &amp; C" in email["html"]


def test_render_marks_estimated_deadlines() -> None:
    from pipeline.notify.reminders import Reminder

    deadline = DeadlineRow("Apply", TODAY + timedelta(days=7), is_estimated=True, id="d1")
    _, text, _ = render_reminder(Reminder(PROFILE, item(7), deadline, 7, 7))
    assert "(no time given) (estimated)" in text
