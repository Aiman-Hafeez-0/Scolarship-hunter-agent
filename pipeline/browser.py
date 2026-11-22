"""Open pages that need JavaScript (sources with render_js: true) in a headless browser.

Some official pages are empty until JavaScript runs (COMSATS admissions, the DAAD database).
For those, fetch.py asks a Browser instead of plain HTTP. The rules stay the same:
  - robots.txt is checked (by fetch.py) for the page before it is opened, and here for
    every page, redirect and data request (xhr / fetch) the page makes; a disallowed one
    is cancelled;
  - each page view waits for the per-domain rate limit (fetch.py), like a normal fetch;
  - images, fonts, video and style sheets are never downloaded: we only need the text,
    so the browser asks the website for as little as possible;
  - our User-Agent (with the contact address) is sent, and a page view times out after
    request_timeout_s.

Playwright and its Chromium are optional. The weekly GitHub workflow installs them (the
"browser" dependency group). Anywhere they are missing, playwright_browser yields None and
render_js sources are skipped with the note "needs a browser (render_js)", as before.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pipeline.config import Settings

logger = logging.getLogger(__name__)

# Never downloaded: not needed for the text.
BLOCKED_TYPES = frozenset({"image", "media", "font", "stylesheet"})
# Checked against robots.txt before the browser may send them.
CHECKED_TYPES = frozenset({"document", "xhr", "fetch"})
# After the page has loaded, wait at most this long for its data requests to finish.
SETTLE_MS = 10_000

Allowed = Callable[[str], Awaitable[bool]]  # robots.txt: may we fetch this URL?


@dataclass
class RenderedPage:
    """A page after its JavaScript ran."""

    status: int  # HTTP status of the page itself (0 if the browser got none)
    url: str  # the address after any redirects
    html: str  # the HTML the browser shows (what JavaScript built)
    content_type: str | None


class BrowserError(Exception):
    """The browser could not load the page (network error, timeout). Worth a retry."""


class Browser(Protocol):
    async def render(self, url: str) -> RenderedPage: ...


BrowserFactory = Callable[[Settings, Allowed], AbstractAsyncContextManager[Browser | None]]


def route_action(resource_type: str) -> Literal["block", "check", "allow"]:
    """What to do with a request the page wants to make."""
    if resource_type in BLOCKED_TYPES:
        return "block"
    return "check" if resource_type in CHECKED_TYPES else "allow"


class PlaywrightBrowser:
    """A Browser on top of Playwright's headless Chromium (one fresh context per page)."""

    def __init__(self, browser: Any, settings: Settings, allowed: Allowed) -> None:
        self._browser = browser
        self._settings = settings
        self._allowed = allowed

    async def _handle(self, route: Any) -> None:
        action = route_action(route.request.resource_type)
        if action == "block" or (action == "check" and not await self._allowed(route.request.url)):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def render(self, url: str) -> RenderedPage:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        context = await self._browser.new_context(user_agent=self._settings.user_agent)
        try:
            page = await context.new_page()
            page.set_default_timeout(self._settings.request_timeout_s * 1000)
            await page.route("**/*", self._handle)
            try:
                response = await page.goto(url, wait_until="load")
            except PlaywrightError as exc:
                raise BrowserError(str(exc).splitlines()[0]) from exc
            # Give the page's data requests a moment to finish. Some pages never go quiet:
            # then we use what has loaded.
            with suppress(PlaywrightTimeout):
                await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            return RenderedPage(
                status=response.status if response else 0,
                url=page.url,
                html=await page.content(),
                content_type=response.headers.get("content-type") if response else None,
            )
        finally:
            await context.close()


@asynccontextmanager
async def playwright_browser(settings: Settings, allowed: Allowed) -> AsyncIterator[Browser | None]:
    """A headless Chromium for this run, or None when Playwright or Chromium is missing."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        yield None
        return
    manager = async_playwright()
    playwright = await manager.start()
    try:
        chromium = await playwright.chromium.launch(headless=True)
    except Exception as exc:  # e.g. Chromium was never downloaded
        logger.warning("No browser, so render_js sources are skipped: %s",
                       str(exc).strip().splitlines()[0])  # fmt: skip
        await playwright.stop()
        yield None
        return
    try:
        yield PlaywrightBrowser(chromium, settings, allowed)
    finally:
        await chromium.close()
        await playwright.stop()


@asynccontextmanager
async def no_browser(settings: Settings, allowed: Allowed) -> AsyncIterator[Browser | None]:
    """No browser at all: render_js sources are skipped (used when none are fetched)."""
    yield None
