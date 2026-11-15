"""Run LLM extraction on one page's text.

- Long pages (over MAX_CHARS) are split at headings, and the parts are extracted one by one.
  Results that share a scholarship name are merged into one entry.
- If the model's JSON does not match the schema, that part is asked once more with the
  validation error (extract_retry). If that fails too, the result is marked needs_review.
  extract_page does both passes; the LangGraph pipeline calls them as separate steps.
- Like fetching, this never raises: problems end up in `errors` with needs_review=True.
"""

from dataclasses import dataclass, field
from typing import Any

from pipeline.llm.provider import InvalidOutput, LLMProvider, ProviderError
from pipeline.models import Source
from pipeline.schemas import PageExtraction, ScholarshipExtraction

MAX_CHARS = 80_000
MAX_HEADING_CHARS = 80
_UNKNOWN_VALUES = (None, "unknown", "unclear")


@dataclass
class ExtractResult:
    source: Source
    extraction: PageExtraction
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    needs_review: bool = False
    errors: list[str] = field(default_factory=list)
    # Parts whose JSON was invalid on the first try: part index -> validation error.
    invalid_parts: dict[int, str] = field(default_factory=dict)
    found: list[ScholarshipExtraction] = field(default_factory=list)  # before merging


def extract_page(page_text: str, source: Source, provider: LLMProvider) -> ExtractResult:
    """Extract every opportunity on the page, retrying invalid JSON once."""
    result = extract_first(page_text, source, provider)
    if result.invalid_parts:
        extract_retry(page_text, result, provider)
    return result


def extract_first(page_text: str, source: Source, provider: LLMProvider) -> ExtractResult:
    """First pass over every part of the page. Invalid answers go to `invalid_parts`."""
    result = ExtractResult(source=source, extraction=PageExtraction())
    chunks = split_text(page_text)
    for index, chunk in enumerate(chunks):
        _extract_part(chunk, index, len(chunks), provider, result, feedback=None)
    result.extraction = PageExtraction(scholarships=merge_by_name(result.found))
    return result


def extract_retry(page_text: str, result: ExtractResult, provider: LLMProvider) -> None:
    """Ask again for each invalid part, sending the validation error as feedback."""
    chunks = split_text(page_text)
    invalid, result.invalid_parts = result.invalid_parts, {}
    for index, error in invalid.items():
        _extract_part(chunks[index], index, len(chunks), provider, result, feedback=error)
    result.extraction = PageExtraction(scholarships=merge_by_name(result.found))


def _extract_part(
    chunk: str,
    index: int,
    total: int,
    provider: LLMProvider,
    result: ExtractResult,
    feedback: str | None,
) -> None:
    label = f"part {index + 1}/{total}: " if total > 1 else ""
    try:
        page, tokens_in, tokens_out = provider.extract(chunk, result.source, feedback=feedback)
    except InvalidOutput as exc:
        result.tokens_in += exc.tokens_in
        result.tokens_out += exc.tokens_out
        if feedback is None:
            result.invalid_parts[index] = str(exc)
        else:
            _fail(result, f"{label}invalid JSON twice: {_first_line(exc)}")
        return
    except ProviderError as exc:
        _fail(result, f"{label}LLM call failed: {_first_line(exc)}")
        return
    except Exception as exc:  # one broken page must never crash the run
        _fail(result, f"{label}{type(exc).__name__}: {_first_line(exc)}")
        return
    result.tokens_in += tokens_in
    result.tokens_out += tokens_out
    result.model = provider.last_model
    result.found.extend(page.scholarships)


def _fail(result: ExtractResult, error: str) -> None:
    result.needs_review = True
    result.errors.append(error)


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


# ---------- Splitting long pages ----------


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split text into parts of at most max_chars, cutting at headings where possible."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for section in _sections(text):
        for piece in _hard_split(section, max_chars):
            if current and len(current) + 1 + len(piece) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _looks_like_heading(line: str, previous_blank: bool) -> bool:
    """A Markdown heading, or a short line after a blank line that is not a sentence."""
    line = line.strip()
    if line.startswith("#"):
        return True
    return previous_blank and 0 < len(line) <= MAX_HEADING_CHARS and line[-1] not in ".,;:!?)"


def _sections(text: str) -> list[str]:
    """Cut the text into sections, each starting at a heading."""
    sections: list[list[str]] = [[]]
    previous_blank = True
    for line in text.splitlines():
        if sections[-1] and _looks_like_heading(line, previous_blank):
            sections.append([])
        sections[-1].append(line)
        previous_blank = not line.strip()
    return ["\n".join(lines) for lines in sections if lines]


def _hard_split(section: str, max_chars: int) -> list[str]:
    """Split one oversized section at line breaks (or mid-line, if one line is huge)."""
    if len(section) <= max_chars:
        return [section]
    pieces: list[str] = []
    current = ""
    for line in section.splitlines():
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if current and len(current) + 1 + len(line) > max_chars:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces


# ---------- Merging results from several parts ----------


def merge_by_name(items: list[ScholarshipExtraction]) -> list[ScholarshipExtraction]:
    """Combine entries with the same name (ignoring case and spacing), keeping first order.

    For each field the first known value wins; lists are combined without duplicates.
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = " ".join(item.name.casefold().split())
        data = item.model_dump()
        if key not in merged:
            merged[key] = data
            continue
        base = merged[key]
        for name, value in data.items():
            current = base[name]
            if isinstance(current, list):
                base[name] = current + [v for v in value if v not in current]
            elif current in _UNKNOWN_VALUES and value not in _UNKNOWN_VALUES:
                base[name] = value
    return [ScholarshipExtraction.model_validate(data) for data in merged.values()]
