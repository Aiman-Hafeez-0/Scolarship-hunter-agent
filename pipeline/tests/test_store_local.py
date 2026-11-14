"""Tests for pipeline.store_local."""

from datetime import UTC, datetime
from pathlib import Path

from pipeline.models import Change, FetchStatus
from pipeline.nodes.detect_change import detect_changes
from pipeline.store_local import LocalStore
from pipeline.tests.conftest import make_result
from pipeline.text import content_hash

RUN_1 = datetime(2026, 11, 1, 9, 30, 0, tzinfo=UTC)
RUN_2 = datetime(2026, 11, 8, 9, 30, 0, tzinfo=UTC)


def test_empty_store_has_no_hashes(tmp_path: Path) -> None:
    assert LocalStore(tmp_path).last_hashes() == {}


def test_round_trip(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)

    # Run 1: both pages are new, so both get a snapshot.
    first = detect_changes([make_result("a", "Text A v1"), make_result("b", "Text B")], {})
    written = store.save(first, now=RUN_1)

    assert sorted(p.relative_to(tmp_path).as_posix() for p in written) == [
        "snapshots/a/20261101T093000Z.txt",
        "snapshots/b/20261101T093000Z.txt",
    ]
    assert (tmp_path / "snapshots/a/20261101T093000Z.txt").read_text(encoding="utf-8") == (
        "Text A v1"
    )
    assert LocalStore(tmp_path).last_hashes() == {
        "a": content_hash("Text A v1"),
        "b": content_hash("Text B"),
    }

    # Run 2: "a" changed, "b" did not. Only "a" gets a new snapshot.
    second = detect_changes(
        [make_result("a", "Text A v2"), make_result("b", "Text B")], store.last_hashes()
    )
    assert [r.change for r in second] == [Change.CHANGED, Change.UNCHANGED]
    written = store.save(second, now=RUN_2)

    assert [p.relative_to(tmp_path).as_posix() for p in written] == [
        "snapshots/a/20261108T093000Z.txt"
    ]
    assert not (tmp_path / "snapshots/b/20261108T093000Z.txt").exists()
    assert store.last_hashes()["a"] == content_hash("Text A v2")


def test_failures_do_not_touch_saved_hash(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.save(detect_changes([make_result("a", "Text A")], {}), now=RUN_1)

    failed = make_result("a", status=FetchStatus.FAILED, error="HTTP 500")
    assert store.save(detect_changes([failed], store.last_hashes()), now=RUN_2) == []
    assert store.last_hashes() == {"a": content_hash("Text A")}
