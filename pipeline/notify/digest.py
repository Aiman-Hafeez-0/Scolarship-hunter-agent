"""Build and send one weekly digest email per profile.

Sections, in order (an item appears in only one of the first three):
  1. Action needed   - eligible or maybe, a deadline in 14 days or less
  2. New matches     - eligible or maybe, first matched after the last digest
                       (Pakistan admissions and tests go to section 4 instead, marked NEW)
  3. Changed         - changes not yet notified: old value -> new value
  4. Pakistan admissions and tests - eligible or maybe, not already in section 1
  5. Needs review    - items whose extraction failed a check

If nothing is new (no action needed, no new matches, no changes) a short email is sent.
After a successful send, a notifications row is added and the changes are marked notified.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from pipeline.notify.deadlines import PKT, days_left_text, format_day, show_deadline
from pipeline.notify.mailer import MailError
from pipeline.notify.store import ChangeRow, DigestStore, ItemRow, ProfileRow, RunInfo

ACTION_DAYS = 14
OK_STATUSES = ("eligible", "maybe")
PAKISTAN_TYPES = ("admission", "entry_test")
MAX_VALUE_CHARS = 160
TYPE_LABELS = {"scholarship": "Scholarship", "admission": "Admission", "entry_test": "Entry test"}
STATUS_LABELS = {"eligible": "Eligible", "maybe": "Maybe", "not_eligible": "Not eligible"}
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str) -> str: ...


# ---------- What the templates show ----------


@dataclass
class ItemView:
    name: str
    type_label: str
    deadlines: list[str]  # e.g. "Application deadline: Fri 27 Nov 2026, 16:00 PKT - 11 days left"
    status_line: str  # e.g. "Maybe: take IELTS or TOEFL"
    link: str
    is_new: bool = False
    next_days: int | None = None  # days to the nearest future deadline
    notes: list[str] = field(default_factory=list)  # why it needs review, if it does


@dataclass
class ChangeGroup:
    name: str
    link: str
    lines: list[str]  # e.g. "deadline: 1 Dec -> 15 Dec"


@dataclass
class Digest:
    profile: ProfileRow
    subject: str
    week_of: str
    action: list[ItemView] = field(default_factory=list)
    new: list[ItemView] = field(default_factory=list)
    changed: list[ChangeGroup] = field(default_factory=list)
    pakistan: list[ItemView] = field(default_factory=list)
    review: list[ItemView] = field(default_factory=list)
    run_line: str = ""
    change_ids: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.action or self.new or self.changed)


# ---------- Building ----------


def main_reason(status: str | None, reasons: list[dict[str, Any]]) -> str:
    """The one thing to tell the reader: why not eligible, or what to do next."""
    if status is None:
        return "Not matched yet: run matching"
    label = STATUS_LABELS.get(status, status)
    fails = [r for r in reasons if r.get("outcome") == "fail"]
    unknowns = [r for r in reasons if r.get("outcome") == "unknown"]
    if fails:
        return f"{label}: {fails[0].get('reason', '')}"
    if not unknowns:
        return f"{label}: all checks passed"
    first = next((r for r in unknowns if r.get("action")), unknowns[0])
    text = str(first.get("action") or first.get("reason", ""))
    others = len(unknowns) - 1
    if others:
        text += f" (+{others} more to check)"
    return f"{label}: {text}"


def item_view(item: ItemRow, today: date, last_digest: datetime | None) -> ItemView:
    lines: list[str] = []
    next_days: int | None = None
    for d in item.deadlines:
        if d.deadline_date is None:
            continue
        shown = show_deadline(d.deadline_date, d.timezone_note)
        days = (shown.pkt_date - today).days
        if days < 0:
            continue  # only future deadlines
        next_days = days if next_days is None else min(next_days, days)
        estimated = " (estimated)" if d.is_estimated else ""
        lines.append(f"{d.label}: {shown.text}{estimated} - {days_left_text(days)}")
    if not lines:
        lines.append("No upcoming dated deadline on the page")
    return ItemView(
        name=item.name,
        type_label=TYPE_LABELS.get(item.opportunity_type, item.opportunity_type),
        deadlines=lines,
        status_line=main_reason(item.match_status, item.reasons),
        link=item.source_url,
        is_new=is_new(item, last_digest),
        next_days=next_days,
        notes=list(item.review_notes) if item.needs_review else [],
    )


def is_new(item: ItemRow, last_digest: datetime | None) -> bool:
    if item.match_status not in OK_STATUSES:
        return False
    if last_digest is None:
        return True  # no digest yet: every match is new
    return item.first_matched_at is not None and item.first_matched_at > last_digest


def is_pakistan_type(item: ItemRow) -> bool:
    return item.location == "pakistan" and item.opportunity_type in PAKISTAN_TYPES


def show_value(value: Any) -> str:
    """A changed value in a few words: lists joined, dicts as "kind: value", long text cut."""
    if value is None or value == [] or value == "":
        return "(none)"
    if isinstance(value, list):
        text = "; ".join(show_value(v) for v in value)
    elif isinstance(value, dict):
        main = value.get("label") or value.get("kind")
        rest = value.get("deadline_date") or value.get("value") or value.get("description")
        text = f"{main}: {rest}" if main and rest else json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 3] + "..."


def group_changes(changes: list[ChangeRow]) -> list[ChangeGroup]:
    groups: dict[str, ChangeGroup] = {}
    for c in changes:
        group = groups.setdefault(c.scholarship_name, ChangeGroup(c.scholarship_name,
                                                                  c.source_url, []))  # fmt: skip
        what = c.field_path.replace("_", " ")
        group.lines.append(f"{what}: {show_value(c.old_value)} -> {show_value(c.new_value)}")
    return list(groups.values())


def week_start(today: date) -> date:
    """The Monday of this week."""
    return today - timedelta(days=today.weekday())


def run_line(run: RunInfo | None) -> str:
    if run is None:
        return "No finished weekly run yet."
    at = run.finished_at.astimezone(PKT)
    return f"{run.sources_total} sources checked, last run {format_day(at.date())}, {at:%H:%M} PKT."


def _by_deadline(view: ItemView) -> tuple[int, str]:
    return (view.next_days if view.next_days is not None else 10_000, view.name)


def build_digest(
    profile: ProfileRow,
    items: list[ItemRow],
    changes: list[ChangeRow],
    last_digest: datetime | None,
    run: RunInfo | None,
    today: date,
) -> Digest:
    """Sort the items into the digest sections (pure: no database, no email)."""
    views = [(item, item_view(item, today, last_digest)) for item in items]
    ok = [(i, v) for i, v in views if i.match_status in OK_STATUSES]

    urgent = {i.id for i, v in ok if v.next_days is not None and v.next_days <= ACTION_DAYS}
    action = [v for i, v in ok if i.id in urgent]
    rest = [(i, v) for i, v in ok if i.id not in urgent]
    pakistan = [v for i, v in rest if is_pakistan_type(i)]
    new = [v for i, v in rest if v.is_new and not is_pakistan_type(i)]
    new_count = sum(v.is_new for _, v in ok)

    week = week_start(today)
    week_of = f"{week.day} {week:%b}"
    return Digest(
        profile=profile,
        subject=f"Scholarships: {new_count} new, {len(action)} closing soon (week of {week_of})",
        week_of=week_of,
        action=sorted(action, key=_by_deadline),
        new=sorted(new, key=_by_deadline),
        changed=group_changes(changes),
        pakistan=sorted(pakistan, key=_by_deadline),
        review=[v for i, v in views if i.needs_review],
        run_line=run_line(run),
        change_ids=[c.id for c in changes],
    )


# ---------- Rendering ----------

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(digest: Digest) -> tuple[str, str]:
    """(plain text, HTML) for one digest."""
    context = {"d": digest}
    text = _env.get_template("digest.txt").render(context)
    text = re.sub(r"\n{3,}", "\n\n", text)  # at most one blank line in a row
    html = _env.get_template("digest.html").render(context)
    return text, html


# ---------- Building for every profile, and sending ----------


def build_all(store: DigestStore, today: date) -> list[Digest]:
    """One digest per profile, skipping profiles with emails_paused."""
    changes = store.load_unnotified_changes()
    run = store.last_run()
    return [
        build_digest(p, store.load_items(p.id), changes, store.last_digest_at(p.id), run, today)
        for p in store.load_profiles()
        if not p.emails_paused
    ]


@dataclass
class SendResult:
    email: str
    subject: str
    error: str | None = None


def send_digests(store: DigestStore, mailer: Mailer, today: date | None = None) -> list[SendResult]:
    """Send every digest. Changes are marked notified only after their email was sent."""
    today = today or datetime.now(PKT).date()
    results: list[SendResult] = []
    for digest in build_all(store, today):
        text, html = render(digest)
        try:
            message_id = mailer.send(digest.profile.email, digest.subject, text, html)
        except MailError as exc:
            results.append(SendResult(digest.profile.email, digest.subject, str(exc)))
            continue
        store.record_digest(digest.profile.id, message_id, digest.change_ids)
        results.append(SendResult(digest.profile.email, digest.subject))
    return results
