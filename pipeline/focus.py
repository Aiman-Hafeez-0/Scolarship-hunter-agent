"""Make a shorter "focused" copy of a page to send to the LLM (free tiers limit tokens).

Keep every line that looks relevant (deadlines, admissions, scholarships, tests, fees,
degree words, month names, years 2026-2028, dates), plus CONTEXT lines before and after it.
Drop menus (runs of short lines without numbers) and footer lines (copyright, cookies, ...).
Blocks that are not next to each other are separated by a blank line.

Lines are copied unchanged, so quotes the LLM copies are still found on the full page:
the quote check in validate.py always uses the FULL page text, never this focused copy.
"""

import re

from pipeline.nodes.validate import MONTH_NAMES, NUMERIC_DATE, simplify

CONTEXT = 2  # lines kept before and after each relevant line
SHORT_LINE_WORDS = 4  # a line with fewer words than this is "short"

# Matched on simplify(line): lowercase, no accents. Words in English, Italian and Turkish.
_WORDS = (
    r"deadline|last date|closing date|due date|\bappl(?:y|ie|ica)|admission|intake|enrol"
    r"|\bspring\b|\bfall\b|\bautumn\b|semester|scholarship|fellowship|stipend|tuition"
    r"|\bfees?\b|eligib|requirement|ielts|toefl|\btest|\bmaster|\bph\.? ?d\b|doctora"
    r"|\bgraduate|undergraduate|bachelor|\bcgpa\b|\bgpa\b|\bgrant|funding|bursar"
    r"|scadenz|bando|borse? di studio|ammission|iscrizion|\bburs|basvuru|kontenjan"
)
_FULL_MONTHS = sorted({n for names in MONTH_NAMES.values() for n in names if len(n) > 3})
_SHORT_MONTHS = sorted({n for names in MONTH_NAMES.values() for n in names if len(n) <= 3})
RELEVANT = re.compile(
    rf"{_WORDS}"
    rf"|\b(?:{'|'.join(_FULL_MONTHS)})\b"
    # "may", "jan", "set" ... are ordinary words too: count them only next to a number.
    rf"|\d\s*(?:st|nd|rd|th)?\s*(?:{'|'.join(_SHORT_MONTHS)})\b"
    rf"|\b(?:{'|'.join(_SHORT_MONTHS)})\.?\s*\d"
    r"|\b202[678]\b"
)
# Matched on the original line, case-sensitive: short acronyms that are also plain words.
ACRONYMS = re.compile(r"\b(?:MS|MSc|MPhil|BS|BSc|PhD|MOI|HAT|GAT|NTS|GRE|GMAT|SAT)\b")
FOOTER = re.compile(
    r"©|copyright|all rights reserved|privacy policy|cookie|terms of use|terms and conditions"
    r"|follow us|subscribe to our newsletter",
    re.IGNORECASE,
)


def is_relevant(line: str) -> bool:
    return bool(ACRONYMS.search(line) or RELEVANT.search(simplify(line))
                or NUMERIC_DATE.search(line))  # fmt: skip


def _is_short(line: str) -> bool:
    return len(line.split()) < SHORT_LINE_WORDS and not any(ch.isdigit() for ch in line)


def focus_text(text: str) -> str:
    """The relevant lines of the page with their context; "" if nothing looks relevant."""
    lines: list[str] = []
    blank_before: list[bool] = []  # was there a blank line before this line on the page?
    previous_blank = False
    for raw in text.splitlines():
        if raw.strip():
            lines.append(raw.strip())
            blank_before.append(previous_blank)
        previous_blank = not raw.strip()
    count = len(lines)

    def short(i: int) -> bool:
        return 0 <= i < count and _is_short(lines[i])

    # A menu is a run of short lines: the line and both its neighbours are short.
    dropped = [
        bool(FOOTER.search(line)) or (short(i) and short(i - 1) and short(i + 1))
        for i, line in enumerate(lines)
    ]
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if not dropped[i] and is_relevant(line):
            keep.update(j for j in range(i - CONTEXT, i + CONTEXT + 1)
                        if 0 <= j < count and not dropped[j])  # fmt: skip

    out: list[str] = []
    previous = -2
    for i in sorted(keep):
        if out and (i != previous + 1 or blank_before[i]):
            out.append("")  # a gap, or a paragraph break on the page (helps split at headings)
        out.append(lines[i])
        previous = i
    return "\n".join(out)
