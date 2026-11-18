"""Tests for splitting long tables of dates (pipeline/tables.py)."""

from pathlib import Path

from pipeline.focus import focus_text
from pipeline.nodes.extract import llm_chunks
from pipeline.tables import date_count, find_tables, is_row, split_long_tables, table_chunks

HEADER = "Sr. No. Announcement Date Last Date for Online Registration Test Date"
GOLDEN = Path(__file__).resolve().parent / "golden"


def row(n: int, month: int = 1) -> str:
    return f"{n} Sunday 0{n % 9 + 1}.{month:02d}.2026 Monday 1{n % 9}.{month:02d}.2026"


def test_a_row_has_at_least_two_dates() -> None:
    assert is_row("1 Sunday 14.12.2025 Monday 29.12.2025")
    assert is_row("Round 1: 1 October 2026 to 15 November 2026")
    assert is_row("Opens March 3, 2027, closes April 9, 2027")
    assert not is_row("Applications close on 15 January 2027.")
    assert date_count("no dates here") == 0


def test_short_tables_are_left_alone() -> None:
    text = "\n".join([HEADER, *[row(n) for n in range(1, 16)]])  # exactly 15 rows
    assert split_long_tables(text) == (text, [])


def test_long_table_is_cut_at_blocks_with_the_header_in_every_piece() -> None:
    sizes = ((1, 5), (2, 4), (3, 5), (4, 5))  # (month, rows): four blocks, 19 rows
    blocks = [[row(n, m) for n in range(1, size + 1)] for m, size in sizes]
    rows = [r for block in blocks for r in block]
    text = "\n".join(["Test Calendar 2026", HEADER, *rows, "", "* Dates may change."])

    rest, pieces = split_long_tables(text)

    assert rest == "* Dates may change."  # the table (and its header) went into the pieces
    assert len(pieces) == 2
    first, second = (p.splitlines() for p in pieces)
    assert first[:2] == ["Test Calendar 2026", HEADER] and second[:2] == first[:2]
    assert first[2:] == rows[:14]  # blocks of 5, 4 and 5 rows; the next block would pass 15
    assert second[2:] == rows[14:]


def test_block_titles_stay_with_their_rows() -> None:
    hat = [row(n, 1) for n in range(1, 11)]
    usat = [row(n, 2) for n in range(1, 11)]
    text = "\n".join([HEADER, "HAT", *hat, "USAT", *usat])

    [table] = find_tables(text.splitlines())
    assert [(b.title, len(b.rows)) for b in table.blocks] == [("HAT", 10), ("USAT", 10)]
    pieces = table_chunks(table)
    assert [p.splitlines()[:2] for p in pieces] == [[HEADER, "HAT"], [HEADER, "USAT"]]


def test_a_block_bigger_than_a_piece_repeats_its_title() -> None:
    text = "\n".join([HEADER, "HAT", *[row(n) for n in range(1, 21)]])
    _, pieces = split_long_tables(text)
    assert [p.splitlines()[:2] for p in pieces] == [[HEADER, "HAT"], [HEADER, "HAT"]]
    assert [len(p.splitlines()) - 2 for p in pieces] == [15, 5]


def test_hec_calendar_pdf_is_sent_in_table_pieces() -> None:
    text = (GOLDEN / "hec-hat-calendar-pdf.txt").read_text(encoding="utf-8")
    chunks = llm_chunks(text)

    assert len(chunks) == 3  # the notes and titles, then two pieces of the 19-row table
    assert "Test Calendar : Year 2026" in chunks[0]
    header = text.splitlines()[0]
    assert all(c.startswith(header) for c in chunks[1:])
    table_rows = [line for c in chunks[1:] for line in c.splitlines()[1:]]
    assert len(table_rows) == 19
    assert all(line in text for line in table_rows)  # copied unchanged: quotes still match


def test_no_other_golden_page_has_a_long_table() -> None:
    for path in GOLDEN.glob("*.txt"):
        if path.stem != "hec-hat-calendar-pdf":
            rest, pieces = split_long_tables(focus_text(path.read_text(encoding="utf-8")))
            assert pieces == [], path.stem
