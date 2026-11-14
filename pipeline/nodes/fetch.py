"""Download every source politely and turn it into plain text.

Polite means: robots.txt is checked for every URL (redirect targets too), each domain is
hit at most once every `per_domain_delay_s` seconds, and failed requests are retried only
when retrying can help (HTTP 429, 5xx, network errors).
"""

import asyncio
from urllib.parse import urljoin

import httpx

from pipeline.config import Settings
from pipeline.models import FetchResult, FetchStatus, Source
from pipeline.ratelimit import DomainRateLimiter, domain_of
from pipeline.robots import RobotsChecker
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


def make_client(settings: Settings) -> httpx.AsyncClient:
    """The one HTTP client used for every request in a run."""
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.request_timeout_s,
        follow_redirects=False,  # we follow redirects ourselves, checking robots.txt each hop
    )


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


async def fetch_source(
    source: Source,
    client: httpx.AsyncClient,
    limiter: DomainRateLimiter,
    robots: RobotsChecker,
    settings: Settings,
) -> FetchResult:
    """Fetch one source. Never raises: any problem becomes a "failed" result."""
    try:
        return await _fetch(source, client, limiter, robots, settings)
    except Exception as exc:  # a broken page must never crash the whole run
        return FetchResult(source, FetchStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


async def _fetch(
    source: Source,
    client: httpx.AsyncClient,
    limiter: DomainRateLimiter,
    robots: RobotsChecker,
    settings: Settings,
) -> FetchResult:
    if source.render_js:
        return FetchResult(source, FetchStatus.SKIPPED_JS, error="needs a browser (render_js)")

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

    # Check the length before adding links: a page with only links is still "empty".
    if len(text) < MIN_TEXT_CHARS:
        return FetchResult(
            source,
            FetchStatus.FAILED,
            http_status=response.status_code,
            content_type=content_type,
            error=f"only {len(text)} characters of text; page may need a browser",
        )

    if source.include_links and not is_pdf:
        text = append_links(text, extract_links(response.text, url))

    return FetchResult(
        source,
        FetchStatus.OK,
        http_status=response.status_code,
        content_type=content_type,
        text=text,
        content_hash=content_hash(text),
    )


async def fetch_all(sources: list[Source], settings: Settings) -> list[FetchResult]:
    """Fetch all sources, at most `max_concurrency` at a time. Results keep the input order."""
    if not settings.contact_email:
        raise ValueError("Set CONTACT_EMAIL in .env: it goes in the User-Agent we send.")

    limiter = DomainRateLimiter(settings.per_domain_delay_s)
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async with make_client(settings) as client:
        robots = RobotsChecker(client, limiter)

        async def run_one(source: Source) -> FetchResult:
            async with semaphore:
                return await fetch_source(source, client, limiter, robots, settings)

        return list(await asyncio.gather(*(run_one(source) for source in sources)))
