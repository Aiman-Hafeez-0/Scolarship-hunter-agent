"""Tests for pipeline.nodes.detect_change."""

from pipeline.models import Change, FetchStatus
from pipeline.nodes.detect_change import detect_changes
from pipeline.tests.conftest import make_result
from pipeline.text import content_hash

OLD_TEXT = "Deadline: 15 January 2027. Apply online."
NEW_TEXT = "Deadline: 31 January 2027. Apply online."


def test_never_seen_is_new() -> None:
    [result] = detect_changes([make_result("a", OLD_TEXT)], {})
    assert result.change is Change.NEW


def test_saved_hash_none_counts_as_new() -> None:
    [result] = detect_changes([make_result("a", OLD_TEXT)], {"a": None})
    assert result.change is Change.NEW


def test_same_hash_is_unchanged() -> None:
    [result] = detect_changes([make_result("a", OLD_TEXT)], {"a": content_hash(OLD_TEXT)})
    assert result.change is Change.UNCHANGED


def test_different_hash_is_changed() -> None:
    [result] = detect_changes([make_result("a", NEW_TEXT)], {"a": content_hash(OLD_TEXT)})
    assert result.change is Change.CHANGED


def test_matches_by_source_key() -> None:
    results = [make_result("a", OLD_TEXT), make_result("b", OLD_TEXT)]
    a, b = detect_changes(results, {"b": content_hash(OLD_TEXT)})
    assert (a.change, b.change) == (Change.NEW, Change.UNCHANGED)


def test_non_ok_results_get_no_change() -> None:
    results = [
        make_result("f", status=FetchStatus.FAILED, error="HTTP 404"),
        make_result("r", status=FetchStatus.BLOCKED_ROBOTS),
        make_result("j", status=FetchStatus.SKIPPED_JS),
    ]
    assert [r.change for r in detect_changes(results, {})] == [None, None, None]


def test_input_is_not_modified() -> None:
    original = make_result("a", OLD_TEXT)
    detect_changes([original], {})
    assert original.change is None
