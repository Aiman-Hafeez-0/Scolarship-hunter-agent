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


# ---------- HEC test calendar: both deadlines of every November/December row ----------

# The rows of the HEC calendar PDF whose registration or test date falls in November or
# December 2026: (row as written, last date for registration, test date). Each row has two
# required deadlines that must be extracted separately (prompt rule 11).
HEC_LATE_ROWS = [
    ("5 Sunday 04.10.2026 Tuesday 20.10.2026 Friday 06.11.2026 Sunday 15.11.2026 Thursday "
     "24.12.2026", "2026-10-20", "2026-11-15"),
    ("4 Sunday 15.11.2026 Sunday 29.11.2026 Friday 11.12.2026 Sunday 20.12.2026 Thursday "
     "31.12.2026", "2026-11-29", "2026-12-20"),
    ("5 Sunday 08.11.2026 Sunday 22.11.2026 Friday 04.12.2026 Sunday 13.12.2026 Thursday "
     "31.12.2026", "2026-11-22", "2026-12-13"),
]  # fmt: skip


@pytest.mark.parametrize(("row", "registration", "test_day"), HEC_LATE_ROWS,
                         ids=["block1-s5", "block2-s4", "block3-s5"])  # fmt: skip
def test_hec_late_rows_have_both_deadlines(row: str, registration: str, test_day: str) -> None:
    [page] = [p for p in PAGES if p.key == "hec-hat-calendar-pdf"]
    assert row in page.text
    from_row = [d for item in page.expected.items for d in item.deadlines if d.quote == row]
    required = {(d.date.isoformat(), d.label.split(": ")[-1].lower())
                for d in from_row if not d.optional}  # fmt: skip
    assert required == {(registration, "last date for online registration"),
                        (test_day, "test date")}  # fmt: skip


def test_prompt_asks_for_both_deadlines_of_a_test_calendar_row() -> None:
    from pipeline.prompts import EXTRACT_VERSION, PROMPTS_DIR

    rule = (PROMPTS_DIR / f"{EXTRACT_VERSION}.md").read_text(encoding="utf-8")
    assert "every row has TWO deadlines" in rule
    assert '"Last Date for (Online) Registration"' in rule and '"Test Date"' in rule
    assert "Never merge them into one deadline" in rule
