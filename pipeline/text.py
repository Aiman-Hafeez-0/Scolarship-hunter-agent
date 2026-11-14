"""Turn downloaded HTML or PDF into clean plain text, and fingerprint it."""

import hashlib
import io
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

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
    """Extract the readable text of a web page (tables included).

    trafilatura is good at dropping menus and footers, but it is built for articles and
    sometimes throws away lists of announcements, which is exactly where deadlines appear.
    So we also strip all tags, and if trafilatura kept less than half of that, we use
    the stripped text instead.
    """
    try:
        extracted = trafilatura.extract(html, include_tables=True, favor_recall=True)
    except Exception:  # trafilatura can choke on odd markup; the fallback still works
        extracted = None
    smart = normalize(extracted or "")
    fallback = normalize(_strip_tags(html))
    if len(smart) < len(fallback) / 2:
        return fallback
    return smart


def extract_links(html: str, base_url: str) -> list[str]:
    """Every link (<a href>) and image (<img src>) URL on the page.

    URLs are made absolute, "#fragments" are removed, and the list is sorted with no
    duplicates. mailto:, javascript: and other non-web links are skipped.
    """
    collector = _LinkCollector()
    collector.feed(html)
    collector.close()
    links: set[str] = set()
    for raw in collector.values:
        url, _fragment = urldefrag(urljoin(base_url, raw.strip()))
        if url.startswith(("http://", "https://")):
            links.add(url)
    return sorted(links)


def append_links(text: str, links: list[str]) -> str:
    """Add the URLs at the end of the text, so a renamed file changes the content hash."""
    if not links:
        return text
    return text + "\n\nLinks:\n" + "\n".join(links)


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


class _LinkCollector(HTMLParser):
    """Collect href of <a> tags and src of <img> tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted = {"a": "href", "img": "src"}.get(tag)
        for name, value in attrs:
            if name == wanted and value and value.strip():
                self.values.append(value)


def _strip_tags(html: str) -> str:
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)
