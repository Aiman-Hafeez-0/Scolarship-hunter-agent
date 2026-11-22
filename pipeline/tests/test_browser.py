"""Tests for render_js sources: fetch.py with a fake browser (no Chromium needed)."""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
import respx

from pipeline.browser import (
    Allowed,
    Browser,
    BrowserError,
    RenderedPage,
    playwright_browser,
    route_action,
)
from pipeline.config import Settings
from pipeline.models import FetchStatus
from pipeline.nodes.fetch import fetch_all
from pipeline.ratelimit import DomainRateLimiter
from pipeline.tests.conftest import make_source

PAGE = "https://admissions.example.edu.pk/"
ROBOTS = "https://admissions.example.edu.pk/robots.txt"
APP_HTML = (
    "<html><body><div id='root'><h1>Admissions Fall 2027</h1>"
    "<p>Online applications for MS programmes close on 15 January 2027.</p>"
    "<p>Entry test: 2 February 2027. <a href='/fee'>Fee structure</a></p></div></body></html>"
)


@dataclass
class FakeBrowser:
    """Answers render() from a list of pages (or errors); records each URL asked for."""

    answers: list[RenderedPage | Exception] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def render(self, url: str) -> RenderedPage:
        self.calls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def page(status: int = 200, html: str = APP_HTML) -> RenderedPage:
    return RenderedPage(status, PAGE, html, "text/html; charset=utf-8")


def factory_for(browser: Browser | None, started: list[bool] | None = None):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory(settings: Settings, allowed: Allowed) -> AsyncIterator[Browser | None]:
        if started is not None:
            started.append(True)
        yield browser

    return factory


def js_source(**overrides: object):  # type: ignore[no-untyped-def]
    return make_source(url=PAGE, render_js=True, **overrides)


async def test_render_js_page_is_fetched_in_the_browser(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    mock_http.get(ROBOTS).respond(404)  # no robots.txt: allowed
    browser = FakeBrowser([page()])

    [result] = await fetch_all([js_source()], settings, browser_factory=factory_for(browser))

    assert result.status is FetchStatus.OK
    assert result.text is not None and "close on 15 January 2027" in result.text
    assert result.content_hash is not None and result.http_status == 200
    assert browser.calls == [PAGE]


async def test_links_are_added_when_asked(mock_http: respx.MockRouter, settings: Settings) -> None:
    mock_http.get(ROBOTS).respond(404)
    browser = FakeBrowser([page()])
    [result] = await fetch_all([js_source(include_links=True)], settings,
                               browser_factory=factory_for(browser))  # fmt: skip
    assert result.text is not None and "https://admissions.example.edu.pk/fee" in result.text


async def test_robots_txt_is_checked_before_the_browser_opens_the_page(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    mock_http.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /\n")
    browser = FakeBrowser([page()])

    [result] = await fetch_all([js_source()], settings, browser_factory=factory_for(browser))

    assert result.status is FetchStatus.BLOCKED_ROBOTS
    assert browser.calls == []  # the page was never opened


async def test_each_page_view_waits_for_the_rate_limit(
    mock_http: respx.MockRouter, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_http.get(ROBOTS).respond(404)
    waited: list[str] = []
    original = DomainRateLimiter.wait

    async def spy(self: DomainRateLimiter, domain: str) -> None:
        waited.append(domain)
        await original(self, domain)

    monkeypatch.setattr(DomainRateLimiter, "wait", spy)
    browser = FakeBrowser([page()])
    await fetch_all([js_source()], settings, browser_factory=factory_for(browser))

    # once for robots.txt, once for the page view itself
    assert waited.count("admissions.example.edu.pk") == 2


async def test_server_errors_and_browser_errors_are_retried(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    mock_http.get(ROBOTS).respond(404)
    browser = FakeBrowser([page(503), BrowserError("net::ERR_TIMED_OUT"), page()])

    [result] = await fetch_all([js_source()], settings, browser_factory=factory_for(browser))

    assert result.status is FetchStatus.OK
    assert len(browser.calls) == 3  # 1 try + 2 retries (settings.retries)


async def test_gives_up_after_the_retries(mock_http: respx.MockRouter, settings: Settings) -> None:
    mock_http.get(ROBOTS).respond(404)
    browser = FakeBrowser([BrowserError("down")] * 3)
    [result] = await fetch_all([js_source()], settings, browser_factory=factory_for(browser))
    assert result.status is FetchStatus.FAILED and "down" in (result.error or "")


async def test_a_404_or_an_empty_app_fails(mock_http: respx.MockRouter, settings: Settings) -> None:
    mock_http.get(ROBOTS).respond(404)
    browser = FakeBrowser([page(404), page(html="<div id='root'></div>")])
    sources = [js_source(key="a"), js_source(key="b")]

    missing, empty = await fetch_all(sources, settings, browser_factory=factory_for(browser))

    assert missing.status is FetchStatus.FAILED and missing.error == "HTTP 404"
    assert empty.status is FetchStatus.FAILED and "even in a browser" in (empty.error or "")


async def test_no_browser_when_no_source_needs_one(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    mock_http.get("https://example.edu.pk/robots.txt").respond(404)
    mock_http.get("https://example.edu.pk/admissions").respond(200, html=APP_HTML)
    started: list[bool] = []

    await fetch_all([make_source()], settings, browser_factory=factory_for(None, started))

    assert started == []  # Chromium is not started for plain pages


async def test_missing_browser_skips_with_the_usual_note(
    mock_http: respx.MockRouter, settings: Settings
) -> None:
    [result] = await fetch_all([js_source()], settings, browser_factory=factory_for(None))
    assert result.status is FetchStatus.SKIPPED_JS
    assert result.error == "needs a browser (render_js)"
    assert not mock_http.calls


async def test_playwright_browser_is_none_without_playwright(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)  # import fails

    async def allowed(url: str) -> bool:
        return True

    async with playwright_browser(settings, allowed) as browser:
        assert browser is None


@pytest.mark.parametrize(
    ("resource_type", "action"),
    [("image", "block"), ("font", "block"), ("media", "block"), ("stylesheet", "block"),
     ("document", "check"), ("xhr", "check"), ("fetch", "check"), ("script", "allow")],
)  # fmt: skip
def test_route_action(resource_type: str, action: str) -> None:
    assert route_action(resource_type) == action
