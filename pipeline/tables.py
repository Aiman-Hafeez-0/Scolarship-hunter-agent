"""Split long tables of dates into smaller pieces for the LLM.

A model given a long table of bare dates (like the HEC test calendar: 19 rows of five dates)
tends to skip it. A piece of about 15 rows, with the table's header repeated at the top, is
small enough to read row by row.

- A table row is a line with at least two dates ("14.12.2025", "15 January 2027").
- A table is a run of rows. A single short line between rows is a block title (e.g. "HAT")
  and starts a new block; so does a row numbered "1" after other rows.
- The header is the (at most two) lines right before the first row, e.g. the column names.
  If later blocks have titles, the short line right above the first row is the first
  block's title instead.
- Only tables with more than TABLE_CHUNK_ROWS rows are split. Each piece gets the header,
  then whole blocks (each with its title) until it holds about TABLE_CHUNK_ROWS rows. A block
  bigger than that is cut, and its title is repeated in every piece.

Lines are copied unchanged, so quotes from a piece are still found on the full page.
"""

import re
from dataclasses import dataclass, field

from pipeline.nodes.validate import MONTH_NAMES, NUMERIC_DATE, simplify

TABLE_CHUNK_ROWS = 15
MIN_ROW_DATES = 2
MAX_TITLE_CHARS = 80
HEADER_LINES = 2

_MONTHS = sorted({n for names in MONTH_NAMES.values() for n in names}, key=len, reverse=True)
_WORD_DATE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{'|'.join(_MONTHS)})\.?,?\s+\d{{4}}\b"
    rf"|\b(?:{'|'.join(_MONTHS)})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
)
_FIRST_ROW = re.compile(r"^\s*1[.)]?\s")


def date_count(line: str) -> int:
    return len(NUMERIC_DATE.findall(line)) + len(_WORD_DATE.findall(simplify(line)))


def is_row(line: str) -> bool:
    return date_count(line) >= MIN_ROW_DATES


@dataclass
class Block:
    title: str | None = None
    rows: list[str] = field(default_factory=list)


@dataclass
class Table:
    start: int  # index of the first header line (or first row, without a header)
    end: int  # index after the last row
    header: list[str]
    blocks: list[Block]

    @property
    def row_count(self) -> int:
        return sum(len(b.rows) for b in self.blocks)


def find_tables(lines: list[str]) -> list[Table]:
    tables: list[Table] = []
    i = 0
    while i < len(lines):
        if not is_row(lines[i]):
            i += 1
            continue
        first = i
        blocks = [Block()]
        while i < len(lines):
            line = lines[i]
            if is_row(line):
                if blocks[-1].rows and _FIRST_ROW.match(line):
                    blocks.append(Block())
                blocks[-1].rows.append(line)
                i += 1
            elif _is_title(line) and i + 1 < len(lines) and is_row(lines[i + 1]):
                blocks.append(Block(title=line.strip()))
                i += 1
            else:
                break
        header: list[str] = []
        start = first
        while start > 0 and len(header) < HEADER_LINES:
            above = lines[start - 1]
            if not above.strip() or is_row(above) or any(start - 1 < t.end for t in tables):
                break
            header.insert(0, above)
            start -= 1
        # Later blocks have titles: the short line right above the first row is a title too,
        # and belongs to the first block only (not to the header repeated in every piece).
        titled = any(b.title for b in blocks[1:])
        if titled and len(header) > 1 and _is_title(header[-1]) and blocks[0].title is None:
            blocks[0].title = header.pop().strip()
        tables.append(Table(start, i, header, [b for b in blocks if b.rows]))
    return tables


def _is_title(line: str) -> bool:
    text = line.strip()
    return 0 < len(text) <= MAX_TITLE_CHARS


def table_chunks(table: Table, max_rows: int = TABLE_CHUNK_ROWS) -> list[str]:
    """The table in pieces of at most max_rows rows, each starting with the header."""
    pieces: list[list[str]] = []
    current: list[str] = []
    rows_in_current = 0
    for block in table.blocks:
        if rows_in_current and rows_in_current + len(block.rows) > max_rows:
            pieces.append(current)
            current, rows_in_current = [], 0
        for start in range(0, len(block.rows), max_rows):
            part = block.rows[start : start + max_rows]
            if rows_in_current and rows_in_current + len(part) > max_rows:
                pieces.append(current)
                current, rows_in_current = [], 0
            current += ([block.title] if block.title else []) + part
            rows_in_current += len(part)
    if current:
        pieces.append(current)
    return ["\n".join([*table.header, *piece]) for piece in pieces]


def split_long_tables(text: str, max_rows: int = TABLE_CHUNK_ROWS) -> tuple[str, list[str]]:
    """(the text without its long tables, the pieces of those tables)."""
    lines = text.splitlines()
    long_tables = [t for t in find_tables(lines) if t.row_count > max_rows]
    if not long_tables:
        return text, []
    removed = {i for t in long_tables for i in range(t.start, t.end)}
    rest = "\n".join(line for i, line in enumerate(lines) if i not in removed)
    pieces = [piece for t in long_tables for piece in table_chunks(t, max_rows)]
    return rest.strip("\n"), pieces
