"""Tests for pipeline.nodes.fetch. All HTTP is mocked with respx: no real network."""

import httpx
import respx
from fpdf import FPDF

from pipeline.config import Settings
from pipeline.models import FetchStatus
from pipeline.nodes.fetch import fetch_all
from pipeline.tests.conftest import make_source

ROBOTS = "https://example.edu.pk/robots.txt"
PAGE = "https://example.edu.pk/admissions"
PAGE_HTML = (
    "<html><body><h1>Admissions 2027</h1>"
    "<p>Applications for the MS programme are open. Deadline: 15 January 2027.</p>"
    "<p>Applicants need 16 years of education and a valid entry test score.</p>"
    "</body></html>"
)


def _allow_robots(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).respond(404)


async def test_html_ok(mock_http: respx.MockRouter, settings: Settings) -> None:
    _allow_robots(mock_http)
    route = mock_http.get(PAGE).respond(200, html=PAGE_HTML)

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.OK
    assert result.http_status == 200
    assert result.text is not None and "Deadline: 15 January 2027" in result.text
    assert result.content_hash is not None and len(result.content_hash) == 64
    assert result.change is None
    sent_ua = route.calls.last.request.headers["user-agent"]
    assert sent_ua == "ScholarshipHunterBot/0.1 (contact: test@example.com)"


async def test_500_then_200_succeeds(mock_http: respx.MockRouter, settings: Settings) -> None:
    _allow_robots(mock_http)
    route = mock_http.get(PAGE).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, html=PAGE_HTML)]
    )

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.OK
    assert route.call_count == 2


async def test_network_error_retried_then_fails(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    _allow_robots(mock_http)
    route = mock_http.get(PAGE).mock(side_effect=httpx.ConnectError("down"))

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.FAILED
    assert route.call_count == 3  # first try + 2 retries
    assert result.error is not None and "ConnectError" in result.error


async def test_404_fails_without_retry(mock_http: respx.MockRouter, settings: Settings) -> None:
    _allow_robots(mock_http)
    route = mock_http.get(PAGE).respond(404)

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.FAILED
    assert result.http_status == 404
    assert route.call_count == 1


async def test_pdf_is_read(mock_http: respx.MockRouter, settings: Settings) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "Scholarship call 2027. The application deadline is 1 March 2027.")
    _allow_robots(mock_http)
    url = "https://example.edu.pk/call.pdf"
    mock_http.get(url).respond(
        200, content=bytes(pdf.output()), headers={"content-type": "application/pdf"}
    )

    [result] = await fetch_all([make_source(url=url, is_pdf=True)], settings)

    assert result.status is FetchStatus.OK
    assert result.text is not None and "deadline is 1 March 2027" in result.text


async def test_broken_pdf_fails_without_crashing(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    _allow_robots(mock_http)
    mock_http.get(PAGE).respond(200, content=b"%PDF-1.7 this is not really a pdf")

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.FAILED
    assert result.error


async def test_render_js_is_skipped(mock_http: respx.MockRouter, settings: Settings) -> None:
    [result] = await fetch_all([make_source(render_js=True)], settings)

    assert result.status is FetchStatus.SKIPPED_JS
    assert not mock_http.calls  # nothing was downloaded, not even robots.txt


async def test_blocked_by_robots(mock_http: respx.MockRouter, settings: Settings) -> None:
    mock_http.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /admissions\n")
    page = mock_http.get(PAGE).respond(200, html=PAGE_HTML)

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.BLOCKED_ROBOTS
    assert not page.called


async def test_short_page_needs_browser(mock_http: respx.MockRouter, settings: Settings) -> None:
    _allow_robots(mock_http)
    mock_http.get(PAGE).respond(200, html='<html><body><div id="app"></div></body></html>')

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.FAILED
    assert result.error is not None and "may need a browser" in result.error


async def test_redirect_target_is_checked_against_robots(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    _allow_robots(mock_http)
    mock_http.get("https://other.edu.pk/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /\n"
    )
    mock_http.get(PAGE).respond(301, headers={"location": "https://other.edu.pk/new"})
    target = mock_http.get("https://other.edu.pk/new").respond(200, html=PAGE_HTML)

    [result] = await fetch_all([make_source()], settings)

    assert result.status is FetchStatus.BLOCKED_ROBOTS
    assert not target.called


async def test_one_bad_source_does_not_stop_others(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    _allow_robots(mock_http)
    mock_http.get(PAGE).respond(200, html=PAGE_HTML)
    mock_http.get("https://example.edu.pk/broken").respond(404)
    sources = [
        make_source(key="broken", url="https://example.edu.pk/broken"),
        make_source(),
    ]

    results = await fetch_all(sources, settings)

    assert [r.status for r in results] == [FetchStatus.FAILED, FetchStatus.OK]
