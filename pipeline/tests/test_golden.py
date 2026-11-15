"""Tests for pipeline.golden, plus checks that the hand-written golden answers are sound."""

from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.eval import GOLDEN_DIR, GoldenPage, load_golden
from pipeline.golden import copy_snapshots, save_golden_text
from pipeline.nodes.load_sources import load_sources
from pipeline.nodes.validate import date_in_quote, quote_score

# ---------- the copy script ----------


def test_save_golden_text(tmp_path: Path) -> None:
    path = save_golden_text("kaust", "Deadline: 3 January 2027\nümlaut", tmp_path / "golden")
    assert path == tmp_path / "golden" / "kaust.txt"
    assert path.read_text(encoding="utf-8") == "Deadline: 3 January 2027\nümlaut"


def test_copy_snapshots_reports_missing(tmp_path: Path) -> None:
    texts = {"a": "page a", "b": None}
    written, missing = copy_snapshots(["a", "b"], texts.get, tmp_path)
    assert [p.name for p in written] == ["a.txt"]
    assert missing == ["b"]


# ---------- the golden set itself ----------

PAGES = load_golden()


def test_golden_set_is_complete() -> None:
    texts = {p.stem for p in GOLDEN_DIR.glob("*.txt")}
    assert texts == {page.key for page in PAGES}  # every page has its answers
    assert len(PAGES) == 15
    known = {s.key for s in load_sources(Settings(_env_file=None).sources_file, True)}
    assert {page.key for page in PAGES} <= known


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.key)
def test_every_expected_deadline_is_backed_by_its_page(page: GoldenPage) -> None:
    for item in page.expected.items:
        for deadline in item.deadlines:
            assert quote_score(deadline.quote, page.text) == 100, deadline.quote
            assert date_in_quote(deadline.date, deadline.quote), (deadline.date, deadline.quote)
