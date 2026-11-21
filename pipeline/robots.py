"""Check robots.txt before fetching a page (RFC 9309).

What we do with the robots.txt response:
- 2xx: obey the rules in the file.
- 4xx: the file is "unavailable", so everything is allowed.
- 5xx, network error or anything else: the site is "unreachable", so nothing is allowed
  for the rest of this run.
"""

import asyncio
from collections import defaultdict
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from pipeline.config import Settings
from pipeline.ratelimit import DomainRateLimiter, domain_of

BOT_TOKEN = "ScholarshipHunterBot"
MAX_ROBOTS_BYTES = 500 * 1024  # RFC 9309 asks crawlers to read at least 500 KiB.


def make_client(settings: Settings) -> httpx.AsyncClient:
    """The one HTTP client used for every request in a run (also by the dashboard)."""
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.request_timeout_s,
        follow_redirects=False,  # we follow redirects ourselves, checking robots.txt each hop
    )


class RobotsChecker:
    """Answer "may we fetch this URL?", downloading each site's robots.txt only once."""

    def __init__(self, client: httpx.AsyncClient, limiter: DomainRateLimiter) -> None:
        self._client = client
        self._limiter = limiter
        # Per site: a parsed rule set, or True/False for "allow everything"/"allow nothing".
        self._cache: dict[str, RobotFileParser | bool] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}".lower()
        async with self._locks[site]:  # two pages on one site -> robots.txt fetched once
            if site not in self._cache:
                self._cache[site] = await self._fetch_rules(site)
        rules = self._cache[site]
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(BOT_TOKEN, url)

    async def _fetch_rules(self, site: str) -> RobotFileParser | bool:
        robots_url = f"{site}/robots.txt"
        await self._limiter.wait(domain_of(robots_url))
        try:
            response = await self._client.get(robots_url, follow_redirects=True)
        except httpx.HTTPError:
            return False
        if 200 <= response.status_code < 300:
            parser = RobotFileParser(robots_url)
            body = response.content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
            parser.parse(body.splitlines())
            return parser
        return 400 <= response.status_code < 500
