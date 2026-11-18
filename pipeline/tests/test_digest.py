"""Tests for building and sending the weekly digest (fake store, fake SMTP: no database)."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from pipeline.notify.digest import (
    build_all,
    build_digest,
    main_reason,
    render,
    send_digests,
    show_value,
)
from pipeline.notify.mailer import GmailMailer
from pipeline.notify.store import ChangeRow, DeadlineRow, ItemRow, ProfileRow, RunInfo
from pipeline.tests.test_mailer import FakeSMTP

TODAY = date(2026, 11, 18)  # a Wednesday; the week starts Monday 16 Nov
LAST_DIGEST = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
RUN = RunInfo(17, datetime(2026, 11, 16, 15, 46, tzinfo=UTC))
PROFILE = ProfileRow("p1", "Aisha Khan", "aisha@example.com")
MAYBE_IELTS = [
    {"rule": "nationality", "outcome": "pass", "reason": "open to applicants from Pakistan"},
    {"rule": "english", "outcome": "unknown", "reason": "requires IELTS 6.5; you have no test",
     "action": "take IELTS or TOEFL"},
    {"rule": "funding", "outcome": "unknown", "reason": "funding not stated"},
]  # fmt: skip


def item(name: str, *, days: int | None = None, status: str | None = "maybe",
         new: bool = False, **overrides: Any) -> ItemRow:  # fmt: skip
    """An item with one deadline `days` from TODAY (or none)."""
    deadlines = [] if days is None else [
        DeadlineRow("Application deadline", date.fromordinal(TODAY.toordinal() + days))
    ]  # fmt: skip
    fields: dict[str, Any] = {
        "id": name, "name": name, "opportunity_type": "scholarship", "location": "abroad",
        "source_url": f"https://example.org/{name}", "deadlines": deadlines,
        "match_status": status, "reasons": MAYBE_IELTS if status == "maybe" else [],
        "first_matched_at": datetime(2026, 11, 16, tzinfo=UTC) if new
                            else datetime(2026, 10, 1, tzinfo=UTC),
    }  # fmt: skip
    fields.update(overrides)
    return ItemRow(**fields)


def change(name: str = "Alpha", cid: str = "c1") -> ChangeRow:
    return ChangeRow(cid, name, f"https://example.org/{name}", "application_fee", "EUR 30", "free")


def names(views: list[Any]) -> list[str]:
    return [v.name for v in views]


# ---------- Section rules ----------


def test_action_needed_is_eligible_or_maybe_with_deadline_in_14_days() -> None:
    items = [
        item("Soon", days=3),
        item("Edge", days=14, status="eligible"),
        item("Later", days=15),
        item("Ruled out", days=2, status="not_eligible"),
        item("Unmatched", days=2, status=None),
        item("Passed", days=-1),
        item("Today", days=0),
    ]
    digest = build_digest(PROFILE, items, [], LAST_DIGEST, RUN, TODAY)
    assert names(digest.action) == ["Today", "Soon", "Edge"]  # nearest deadline first


def test_new_matches_are_first_matched_after_the_last_digest() -> None:
    items = [
        item("Fresh", days=40, new=True),
        item("Old", days=40),
        item("Fresh but urgent", days=5, new=True),  # shown in Action needed only
        item("Fresh but ruled out", days=40, new=True, status="not_eligible"),
    ]
    digest = build_digest(PROFILE, items, [], LAST_DIGEST, RUN, TODAY)
    assert names(digest.new) == ["Fresh"]
    assert names(digest.action) == ["Fresh but urgent"]
    assert digest.subject == "Scholarships: 2 new, 1 closing soon (week of 16 Nov)"


def test_first_digest_treats_every_match_as_new() -> None:
    digest = build_digest(PROFILE, [item("A", days=40), item("B")], [], None, RUN, TODAY)
    assert names(digest.new) == ["A", "B"]


def test_pakistan_admissions_and_tests_have_their_own_section() -> None:
    pk: dict[str, Any] = {"location": "pakistan", "opportunity_type": "admission"}
    items = [
        item("NUST", days=30, new=True, **pk),
        item("NTS test", days=60, location="pakistan", opportunity_type="entry_test"),
        item("FAST", days=5, **pk),  # urgent: Action needed instead
        item("HEC scholarship", days=30, location="pakistan", new=True),  # a scholarship
    ]
    digest = build_digest(PROFILE, items, [], LAST_DIGEST, RUN, TODAY)
    assert names(digest.pakistan) == ["NUST", "NTS test"]
    assert digest.pakistan[0].is_new and not digest.pakistan[1].is_new
    assert names(digest.new) == ["HEC scholarship"]
    assert names(digest.action) == ["FAST"]


def test_changes_are_grouped_and_show_old_and_new_value() -> None:
    changes = [
        change("Alpha", "c1"),
        ChangeRow("c2", "Alpha", "https://example.org/Alpha", "intake", None, "Fall 2027"),
    ]
    digest = build_digest(PROFILE, [], changes, LAST_DIGEST, RUN, TODAY)
    [group] = digest.changed
    assert group.lines == ["application fee: EUR 30 -> free", "intake: (none) -> Fall 2027"]
    assert digest.change_ids == ["c1", "c2"]


def test_needs_review_lists_items_with_their_notes() -> None:
    items = [item("Odd", days=40, needs_review=True, review_notes=["deadline quote not found"]),
             item("Fine", days=40)]  # fmt: skip
    digest = build_digest(PROFILE, items, [], LAST_DIGEST, RUN, TODAY)
    assert names(digest.review) == ["Odd"]
    assert digest.review[0].notes == ["deadline quote not found"]


def test_item_shows_future_deadlines_in_pkt_with_days_left() -> None:
    row = item("Alpha", deadlines=[
        DeadlineRow("Round 1", date(2026, 11, 1)),  # passed: hidden
        DeadlineRow("Round 2", date(2026, 11, 25), "23:59 CET", is_estimated=True),
    ])  # fmt: skip
    [view] = build_digest(PROFILE, [row], [], None, RUN, TODAY).action
    assert view.deadlines == [
        "Round 2: Thu 26 Nov 2026, 03:59 PKT (23:59 CET, Wed 25 Nov 2026) (estimated) - 8 days left"
    ]
    assert view.type_label == "Scholarship"
    assert view.status_line == "Maybe: take IELTS or TOEFL (+1 more to check)"
    assert view.link == "https://example.org/Alpha"


def test_main_reason() -> None:
    assert main_reason("eligible", []) == "Eligible: all checks passed"
    fail = [{"outcome": "fail", "reason": "only for: bachelor"}]
    assert main_reason("not_eligible", fail) == "Not eligible: only for: bachelor"
    unknown = [{"outcome": "unknown", "reason": "funding not stated"}]
    assert main_reason("maybe", unknown) == "Maybe: funding not stated"
    assert main_reason(None, []) == "Not matched yet: run matching"


def test_show_value() -> None:
    assert show_value(None) == "(none)"
    assert show_value(["AI", "Data Science"]) == "AI; Data Science"
    assert show_value([{"kind": "cgpa_min", "value": "3.0"}]) == "cgpa_min: 3.0"
    assert show_value("x" * 500).endswith("...")


# ---------- Rendering ----------


def test_full_digest_renders_every_section_in_order() -> None:
    items = [
        item("Urgent One", days=3),
        item("Fresh One", days=40, new=True),
        item("NUST MS", days=30, location="pakistan", opportunity_type="admission"),
        item("Odd One", days=50, needs_review=True, review_notes=["check the fee"]),
    ]
    digest = build_digest(PROFILE, items, [change("Changed One")], LAST_DIGEST, RUN, TODAY)
    text, html = render(digest)

    titles = ["ACTION NEEDED", "NEW MATCHES", "CHANGED", "PAKISTAN ADMISSIONS", "NEEDS REVIEW"]
    positions = [text.index(t) for t in titles]
    assert positions == sorted(positions)
    assert "take IELTS or TOEFL" in text
    assert "https://example.org/Urgent One" in text
    assert "17 sources checked, last run Mon 16 Nov 2026, 20:46 PKT." in text
    assert "No changes this week" not in text
    for t in ["Action needed", "New matches", "Changed (1)", "Pakistan admissions", "Needs review"]:
        assert t in html
    assert 'href="https://example.org/Fresh One"' in html


def test_html_escapes_page_text() -> None:
    digest = build_digest(PROFILE, [item("<script>x</script>", days=3)], [], None, RUN, TODAY)
    _, html = render(digest)
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


def test_empty_week_sends_a_short_email() -> None:
    digest = build_digest(PROFILE, [item("Old", days=40)], [], LAST_DIGEST, RUN, TODAY)
    assert digest.is_empty
    text, html = render(digest)
    assert "No changes this week. 17 sources checked, last run Mon 16 Nov 2026, 20:46 PKT." in text
    assert "ACTION NEEDED" not in text
    assert "No changes this week." in html
    assert digest.subject == "Scholarships: 0 new, 0 closing soon (week of 16 Nov)"


def test_empty_week_without_any_run() -> None:
    digest = build_digest(PROFILE, [], [], LAST_DIGEST, None, TODAY)
    text, _ = render(digest)
    assert "No changes this week. No finished weekly run yet." in text


# ---------- Sending ----------


@dataclass
class FakeStore:
    profiles: list[ProfileRow]
    items: list[ItemRow] = field(default_factory=list)
    changes: list[ChangeRow] = field(default_factory=list)
    recorded: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def load_profiles(self) -> list[ProfileRow]:
        return self.profiles

    def load_items(self, profile_id: str) -> list[ItemRow]:
        return self.items

    def load_unnotified_changes(self) -> list[ChangeRow]:
        return [c for c in self.changes if c.id not in self.notified]

    def last_digest_at(self, profile_id: str) -> datetime | None:
        return LAST_DIGEST

    def last_run(self) -> RunInfo | None:
        return RUN

    def record_digest(self, profile_id: str, message_id: str, change_ids: list[str]) -> None:
        self.recorded.append((profile_id, message_id, change_ids))

    @property
    def notified(self) -> set[str]:
        return {cid for _, _, ids in self.recorded for cid in ids}


def _mailer(fake: FakeSMTP) -> GmailMailer:
    connect: Any = fake
    return GmailMailer("bot@gmail.com", "app password", connect=connect)


def test_paused_profile_is_skipped() -> None:
    store = FakeStore([PROFILE, ProfileRow("p2", "Paused", "paused@example.com", True)])
    assert [d.profile.email for d in build_all(store, TODAY)] == ["aisha@example.com"]

    fake = FakeSMTP()
    results = send_digests(store, _mailer(fake), TODAY)
    assert [r.email for r in results] == ["aisha@example.com"]
    assert [m["To"] for m in fake.sent] == ["aisha@example.com"]


def test_changes_marked_notified_after_successful_send() -> None:
    store = FakeStore([PROFILE], changes=[change("Alpha", "c1"), change("Beta", "c2")])
    fake = FakeSMTP()
    [result] = send_digests(store, _mailer(fake), TODAY)

    assert result.error is None
    [sent] = fake.sent
    [(profile_id, message_id, change_ids)] = store.recorded
    assert (profile_id, change_ids) == ("p1", ["c1", "c2"])
    assert message_id == sent["Message-ID"]
    assert store.load_unnotified_changes() == []


def test_failed_send_marks_nothing_notified() -> None:
    store = FakeStore([PROFILE], changes=[change("Alpha", "c1")])
    [result] = send_digests(store, _mailer(FakeSMTP(fail_on="send")), TODAY)

    assert result.error is not None
    assert store.recorded == []
    assert [c.id for c in store.load_unnotified_changes()] == ["c1"]


def test_empty_week_is_still_sent_and_recorded() -> None:
    store = FakeStore([PROFILE], items=[item("Old", days=40)])
    fake = FakeSMTP()
    send_digests(store, _mailer(fake), TODAY)
    [sent] = fake.sent
    assert "No changes this week" in sent.get_body(("plain",)).get_content()  # type: ignore[union-attr]
    assert store.recorded[0][2] == []
