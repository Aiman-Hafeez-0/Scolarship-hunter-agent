"""Download every source politely and turn it into plain text.

Polite means: robots.txt is checked for every URL (redirect targets too), each domain is
hit at most once every `per_domain_delay_s` seconds, and failed requests are retried only
when retrying can help (HTTP 429, 5xx, network errors).

Sources with render_js: true are opened in a headless browser (pipeline/browser.py) with
the same rules. Without a browser (Playwright not installed) they are skipped.
"""

import asyncio
from urllib.parse import urljoin

import httpx

from pipeline.browser import (
    Browser,
    BrowserError,
    BrowserFactory,
    RenderedPage,
    no_browser,
    playwright_browser,
)
from pipeline.config import Settings
from pipeline.models import FetchResult, FetchStatus, Source
from pipeline.ratelimit import DomainRateLimiter, domain_of
from pipeline.robots import RobotsChecker, make_client
from pipeline.text import (
    append_links,
    content_hash,
    extract_links,
    html_to_text,
    looks_like_pdf,
    normalize,
    pdf_to_text,
)

MIN_TEXT_CHARS = 50
MAX_REDIRECTS = 5
SKIPPED_JS_NOTE = "needs a browser (render_js)"


def _should_retry(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


async def _get_with_retries(
    url: str, client: httpx.AsyncClient, limiter: DomainRateLimiter, settings: Settings
) -> httpx.Response:
    """GET one URL. Retries on 429/5xx/network errors with exponential backoff."""
    for attempt in range(settings.retries + 1):
        last_attempt = attempt == settings.retries
        await limiter.wait(domain_of(url))
        try:
            response = await client.get(url)
        except httpx.TransportError:
            if last_attempt:
                raise
        else:
            if last_attempt or not _should_retry(response.status_code):
                return response
        await asyncio.sleep(settings.retry_backoff_s * 2**attempt)
    raise AssertionError("unreachable")  # the loop always returns or raises


async def _render_with_retries(
    url: str, browser: Browser, limiter: DomainRateLimiter, settings: Settings
) -> RenderedPage:
    """Open one page in the browser, with the same waits and retries as a plain GET."""
    for attempt in range(settings.retries + 1):
        last_attempt = attempt == settings.retries
        await limiter.wait(domain_of(url))
        try:
            page = await browser.render(url)
        except BrowserError:
            if last_attempt:
                raise
        else:
            if last_attempt or not _should_retry(page.status):
                return page
        await asyncio.sleep(settings.retry_backoff_s * 2**attempt)
    raise AssertionError("unreachable")  # the loop always returns or raises


async def fetch_source(
    source: Source,
    client: httpx.AsyncClient,
    limiter: DomainRateLimiter,
    robots: RobotsChecker,
    settings: Settings,
    browser: Browser | None = None,
) -> FetchResult:
    """Fetch one source. Never raises: any problem becomes a "failed" result."""
    try:
        if source.render_js:
            return await _fetch_rendered(source, browser, limiter, robots, settings)
        return await _fetch(source, client, limiter, robots, settings)
    except Exception as exc:  # a broken page must never crash the whole run
        return FetchResult(source, FetchStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


def _from_html(
    source: Source, status: int, content_type: str | None, raw: str, text: str, base_url: str,
    links: bool, hint: str,
) -> FetchResult:  # fmt: skip
    """The result for a downloaded page: failed when it has too little text, else ok."""
    # Check the length before adding links: a page with only links is still "empty".
    if len(text) < MIN_TEXT_CHARS:
        return FetchResult(source, FetchStatus.FAILED, http_status=status,
                           content_type=content_type,
                           error=f"only {len(text)} characters of text; {hint}")  # fmt: skip
    if links:
        text = append_links(text, extract_links(raw, base_url))
    return FetchResult(source, FetchStatus.OK, http_status=status, content_type=content_type,
                       text=text, content_hash=content_hash(text))  # fmt: skip


async def _fetch_rendered(
    source: Source,
    browser: Browser | None,
    limiter: DomainRateLimiter,
    robots: RobotsChecker,
    settings: Settings,
) -> FetchResult:
    """A render_js source: robots.txt, the rate limit, then the page in the browser."""
    if browser is None:
        return FetchResult(source, FetchStatus.SKIPPED_JS, error=SKIPPED_JS_NOTE)
    if not await robots.allowed(source.url):
        return FetchResult(source, FetchStatus.BLOCKED_ROBOTS, error=f"robots.txt: {source.url}")
    page = await _render_with_retries(source.url, browser, limiter, settings)
    if page.status >= 400 or page.status == 0:
        return FetchResult(source, FetchStatus.FAILED, http_status=page.status or None,
                           content_type=page.content_type,
                           error=f"HTTP {page.status}" if page.status else "no answer")  # fmt: skip
    text = normalize(html_to_text(page.html))
    return _from_html(source, page.status, page.content_type, page.html, text, page.url,
                      source.include_links, "even in a browser")  # fmt: skip


async def _fetch(
    source: Source,
    client: httpx.AsyncClient,
    limiter: DomainRateLimiter,
    robots: RobotsChecker,
    settings: Settings,
) -> FetchResult:
    url = source.url
    for _ in range(MAX_REDIRECTS + 1):
        if not await robots.allowed(url):
            return FetchResult(source, FetchStatus.BLOCKED_ROBOTS, error=f"robots.txt: {url}")
        response = await _get_with_retries(url, client, limiter, settings)
        if not (response.is_redirect and "location" in response.headers):
            break
        url = urljoin(url, response.headers["location"])
    else:
        return FetchResult(source, FetchStatus.FAILED, error="too many redirects")

    content_type = response.headers.get("content-type")
    if response.status_code >= 400:
        return FetchResult(
            source,
            FetchStatus.FAILED,
            http_status=response.status_code,
            content_type=content_type,
            error=f"HTTP {response.status_code}",
        )

    is_pdf = looks_like_pdf(content_type, response.content)
    text = normalize(pdf_to_text(response.content) if is_pdf else html_to_text(response.text))
    raw = "" if is_pdf else response.text
    return _from_html(source, response.status_code, content_type, raw, text, url,
                      source.include_links and not is_pdf, "page may need a browser")  # fmt: skip


async def fetch_all(
    sources: list[Source],
    settings: Settings,
    browser_factory: BrowserFactory = playwright_browser,
) -> list[FetchResult]:
    """Fetch all sources, at most `max_concurrency` at a time. Results keep the input order.

    A browser is started only when at least one source has render_js: true.
    """
    if not settings.contact_email:
        raise ValueError("Set CONTACT_EMAIL in .env: it goes in the User-Agent we send.")

    limiter = DomainRateLimiter(settings.per_domain_delay_s)
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    factory = browser_factory if any(s.render_js for s in sources) else no_browser

    async with make_client(settings) as client:
        robots = RobotsChecker(client, limiter)
        async with factory(settings, robots.allowed) as browser:

            async def run_one(source: Source) -> FetchResult:
                async with semaphore:
                    return await fetch_source(source, client, limiter, robots, settings,
                                              browser)  # fmt: skip

            return list(await asyncio.gather(*(run_one(source) for source in sources)))
