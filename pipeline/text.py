"""Turn downloaded HTML or PDF into clean plain text, and fingerprint it."""

import hashlib
import io
from html.parser import HTMLParser

import trafilatura
from pypdf import PdfReader


def normalize(text: str) -> str:
    """Collapse runs of spaces, strip each line, and keep at most one blank line in a row."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def content_hash(text: str) -> str:
    """SHA-256 of the normalized text. Same content -> same hash, even if spacing differs."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def html_to_text(html: str) -> str:
    """Extract the readable text of a web page (tables included)."""
    try:
        extracted = trafilatura.extract(html, include_tables=True, favor_recall=True)
    except Exception:  # trafilatura can choke on odd markup; the fallback still works
        extracted = None
    if extracted and extracted.strip():
        return normalize(extracted)
    return normalize(_strip_tags(html))


def pdf_to_text(data: bytes) -> str:
    """Extract the text of every page of a PDF."""
    reader = PdfReader(io.BytesIO(data))
    return normalize("\n\n".join(page.extract_text() or "" for page in reader.pages))


def looks_like_pdf(content_type: str | None, data: bytes) -> bool:
    """True if the server says it is a PDF, or the bytes start like a PDF file."""
    return "application/pdf" in (content_type or "").lower() or data.startswith(b"%PDF-")


# ---------- Fallback: plain tag stripping ----------

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dd", "dt",
}  # fmt: skip


class _TextCollector(HTMLParser):
    """Collect visible text, skipping scripts and styles, with line breaks at block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _strip_tags(html: str) -> str:
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)
