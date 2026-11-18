"""Deadline reminders: one short email when a deadline gets close.

For every profile that is not paused, and every future deadline of an eligible or maybe item:
  days left = deadline date in Pakistan time - today's date in Pakistan time.
A reminder is due when days left has reached one of the profile's reminder_days
(default 30, 14, 7, 2). If several were reached (e.g. the job did not run for a week), only the
smallest one is sent: with 5 days left and nothing sent yet, you get the 7-day reminder only.

Each reminder kind ("reminder_7d") is sent at most once per profile and deadline. We first
insert a 'pending' notifications row; the database's unique index refuses a second one, and
then we skip quietly. Only after that insert works is the email sent.

If a run crashes between that insert and the email, the row stays 'pending'. Each run starts by
deleting rows still pending after an hour (admin alerts too), so those emails are tried again.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Protocol

from pipeline.notify.deadlines import PKT, days_left_text, show_deadline
from pipeline.notify.digest import OK_STATUSES, TYPE_LABELS, Mailer, SendResult
from pipeline.notify.mailer import MailError
from pipeline.notify.store import DeadlineRow, ItemRow, ProfileRow

STOP_STATUSES = ("submitted", "skipped")  # application statuses that end reminders
STALE_PENDING = timedelta(hours=1)  # a claim this old without an email: the run crashed


class ReminderStore(Protocol):
    def load_profiles(self) -> list[ProfileRow]: ...
    def load_items(self, profile_id: str) -> list[ItemRow]: ...
    def claim_reminder(
        self, profile_id: str, scholarship_id: str, deadline_id: str, kind: str
    ) -> str | None: ...
    def mark_sent(self, notification_id: str, message_id: str) -> None: ...
    def release(self, notification_id: str) -> None: ...
    def delete_stale_pending(self, before: datetime) -> int: ...


@dataclass
class Reminder:
    profile: ProfileRow
    item: ItemRow
    deadline: DeadlineRow
    days_left: int
    threshold: int  # which of reminder_days this is

    @property
    def kind(self) -> str:
        return f"reminder_{self.threshold}d"


def pkt_today(now: datetime) -> date:
    """Today's date in Pakistan. 18:59 UTC is still today there; 19:00 UTC is tomorrow."""
    return now.astimezone(PKT).date()


def due_threshold(days_left: int, reminder_days: list[int]) -> int | None:
    """The smallest reminder day that days_left has reached, or None.

    due_threshold(5, [30, 14, 7, 2]) -> 7;  due_threshold(31, [30, 14, 7, 2]) -> None
    """
    if days_left < 0:
        return None
    reached = [day for day in reminder_days if days_left <= day]
    return min(reached) if reached else None


def due_reminders(profile: ProfileRow, items: list[ItemRow], today: date) -> list[Reminder]:
    """Every reminder due for this profile today (before checking what was already sent)."""
    due: list[Reminder] = []
    for item in items:
        if item.match_status not in OK_STATUSES or item.application_status in STOP_STATUSES:
            continue
        for deadline in item.deadlines:
            if deadline.deadline_date is None or not deadline.id:
                continue
            pkt_date = show_deadline(deadline.deadline_date, deadline.timezone_note).pkt_date
            days_left = (pkt_date - today).days
            threshold = due_threshold(days_left, profile.reminder_days)
            if threshold is not None:
                due.append(Reminder(profile, item, deadline, days_left, threshold))
    return due


def to_do(item: ItemRow) -> list[str]:
    """What is still missing or unclear, from the match reasons that are not a clear pass."""
    lines = [str(r.get("action") or r.get("reason") or "")
             for r in item.reasons if r.get("outcome") in ("unknown", "fail")]  # fmt: skip
    return [line for line in lines if line]


def render_reminder(reminder: Reminder) -> tuple[str, str, str]:
    """(subject, plain text, HTML) for one reminder."""
    item, deadline = reminder.item, reminder.deadline
    assert deadline.deadline_date is not None
    shown = show_deadline(deadline.deadline_date, deadline.timezone_note).text
    if deadline.is_estimated:
        shown += " (estimated)"
    left = days_left_text(reminder.days_left)
    kind = TYPE_LABELS.get(item.opportunity_type, item.opportunity_type)
    todo = to_do(item) or ["Nothing missing that we know of: check the page and apply."]

    subject = f"Reminder: {item.name} - {left}"
    text = "\n".join([
        f"{item.name} ({kind})",
        f"{deadline.label}: {shown}",
        f"{left[0].upper()}{left[1:]}.",
        "",
        "To do:",
        *[f"- {line}" for line in todo],
        "",
        f"Source: {item.source_url}",
    ])  # fmt: skip
    todo_html = "".join(f"<li>{escape(line)}</li>" for line in todo)
    html = (
        f"<p><strong>{escape(item.name)}</strong> ({escape(kind)})<br>"
        f"{escape(deadline.label)}: {escape(shown)}<br>"
        f"<strong>{escape(left)}</strong></p>"
        f"<p>To do:</p><ul>{todo_html}</ul>"
        f'<p>Source: <a href="{escape(item.source_url)}">{escape(item.source_url)}</a></p>'
    )
    return subject, text, html


def send_reminders(
    store: ReminderStore, mailer: Mailer, now: datetime | None = None
) -> list[SendResult]:
    """Send every due reminder that was not sent before. Returns one result per email tried.

    First clears claims left 'pending' by a crashed run, so those reminders are retried.
    """
    now = now or datetime.now(UTC)
    store.delete_stale_pending(now - STALE_PENDING)
    today = pkt_today(now)
    results: list[SendResult] = []
    for profile in store.load_profiles():
        if profile.emails_paused:
            continue
        for reminder in due_reminders(profile, store.load_items(profile.id), today):
            claim = store.claim_reminder(
                profile.id, reminder.item.id, reminder.deadline.id, reminder.kind
            )
            if claim is None:
                continue  # already sent (or being sent by another run)
            subject, text, html = render_reminder(reminder)
            try:
                message_id = mailer.send(profile.email, subject, text, html)
            except MailError as exc:
                store.release(claim)  # so the next run tries again
                results.append(SendResult(profile.email, subject, str(exc)))
                continue
            store.mark_sent(claim, message_id)
            results.append(SendResult(profile.email, subject))
    return results
