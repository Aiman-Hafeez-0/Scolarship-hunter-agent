"""Tests for pipeline.text."""

from pipeline.text import content_hash, html_to_text, looks_like_pdf, normalize


def test_normalize() -> None:
    raw = "  Deadline:\t 15   January \r\n\n\n\n   Apply   now  \n\n"
    assert normalize(raw) == "Deadline: 15 January\n\nApply now"


def test_normalize_is_idempotent() -> None:
    text = normalize("a  b\n\n\n c ")
    assert normalize(text) == text


def test_hash_is_stable_and_ignores_spacing() -> None:
    assert content_hash("Deadline: 15 January") == content_hash("Deadline:   15 January\n\n")
    assert content_hash("Deadline: 15 January") == content_hash("Deadline: 15 January")
    assert content_hash("Deadline: 15 January") != content_hash("Deadline: 16 January")
    assert len(content_hash("x")) == 64


def test_html_to_text_drops_scripts_and_styles() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<script>var x = 1;</script><p>Apply by 15 January</p></body></html>"
    )
    text = html_to_text(html)
    assert "Apply by 15 January" in text
    assert "var x" not in text
    assert "color" not in text


def test_html_to_text_keeps_tables() -> None:
    rows = "".join(
        f"<tr><td>Programme {i}</td><td>Deadline {i} March 2027</td></tr>" for i in range(1, 8)
    )
    text = html_to_text(f"<html><body><table>{rows}</table></body></html>")
    assert "Deadline 3 March 2027" in text


def test_looks_like_pdf() -> None:
    assert looks_like_pdf("application/pdf", b"")
    assert looks_like_pdf("Application/PDF; charset=binary", b"")
    assert looks_like_pdf(None, b"%PDF-1.7 ...")
    assert not looks_like_pdf("text/html", b"<html>")
