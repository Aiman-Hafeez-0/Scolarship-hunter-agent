"""Tests for pipeline.text."""

import trafilatura

from pipeline.text import (
    append_links,
    content_hash,
    extract_links,
    html_to_text,
    looks_like_pdf,
    normalize,
)


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


def _announcement_page() -> str:
    """Like turkiyeburslari.gov.tr: a list of announcement cards plus a short "About" article."""
    cards = "".join(
        f'<div class="card"><a href="/news/{i}">Announcement</a>'
        f'<a href="/news/{i}"><h4>Scholarship Programme {i}: applications open until'
        f" {i} March 2027</h4></a></div>"
        for i in range(1, 13)
    )
    return (
        "<html><head><title>Scholarships</title></head><body>"
        '<nav><a href="/">Home</a><a href="/about">About</a></nav>'
        f'<div class="announcements">{cards}</div>'
        "<main><article><h1>About us</h1>"
        "<p>We offer scholarships to international students in engineering, health sciences,"
        " humanities and social sciences at bachelor, master and doctoral levels.</p>"
        "</article></main></body></html>"
    )


def test_html_to_text_keeps_announcement_list() -> None:
    html = _announcement_page()
    # Precondition: trafilatura alone keeps only the short article and drops the cards.
    smart = trafilatura.extract(html, include_tables=True, favor_recall=True) or ""
    assert "Programme 7" not in smart

    text = html_to_text(html)

    assert "Scholarship Programme 7: applications open until 7 March 2027" in text
    assert "We offer scholarships" in text


def test_html_to_text_prefers_trafilatura_when_it_keeps_enough() -> None:
    article = "<p>" + "Applicants must hold a bachelor degree with good grades. " * 20 + "</p>"
    html = (
        "<html><body><nav><a href='/'>Home</a><a href='/x'>Menu item</a></nav>"
        f"<main><article><h1>Eligibility</h1>{article}</article></main>"
        "<footer>Copyright footer text</footer></body></html>"
    )
    text = html_to_text(html)
    assert "Applicants must hold" in text
    assert "Copyright footer text" not in text  # boilerplate dropped by trafilatura


def test_extract_links() -> None:
    html = (
        '<a href="/docs/Calendar%202026.pdf">PDF</a>'
        '<a href="https://other.org/b">B</a>'
        '<a href="/docs/Calendar%202026.pdf#page=2">same PDF again</a>'
        '<img src="images/cal.jpg">'
        '<a href="mailto:info@hec.gov.pk">mail</a><a href="javascript:void(0)">js</a>'
        '<a href="#top">top</a><a>no href</a>'
        '<script src="/app.js"></script><link href="/style.css" rel="stylesheet">'
    )
    assert extract_links(html, "https://hec.gov.pk/etc/Pages/Test.aspx") == [
        "https://hec.gov.pk/docs/Calendar%202026.pdf",
        "https://hec.gov.pk/etc/Pages/Test.aspx",  # "#top" points back at the page itself
        "https://hec.gov.pk/etc/Pages/images/cal.jpg",
        "https://other.org/b",
    ]


def test_append_links() -> None:
    assert append_links("Text", ["https://a.pk/1", "https://a.pk/2"]) == (
        "Text\n\nLinks:\nhttps://a.pk/1\nhttps://a.pk/2"
    )
    assert append_links("Text", []) == "Text"


def test_looks_like_pdf() -> None:
    assert looks_like_pdf("application/pdf", b"")
    assert looks_like_pdf("Application/PDF; charset=binary", b"")
    assert looks_like_pdf(None, b"%PDF-1.7 ...")
    assert not looks_like_pdf("text/html", b"<html>")
