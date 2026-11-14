"""Keep requests to the same website spaced out, so we never hammer a server."""

import asyncio
import time
from collections import defaultdict
from urllib.parse import urlsplit


def domain_of(url: str) -> str:
    """The host name of a URL, lowercased: "https://WWW.Example.com/a" -> "www.example.com"."""
    return (urlsplit(url).hostname or "").lower()


class DomainRateLimiter:
    """Allow at most one request per domain every `delay_s` seconds.

    Each domain has its own lock. Callers for the same domain queue up behind each other,
    while callers for different domains never wait for one another.
    """

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}

    async def wait(self, domain: str) -> None:
        """Return once it is polite to send the next request to `domain`."""
        async with self._locks[domain]:
            last = self._last_request.get(domain)
            if last is not None:
                remaining = self.delay_s - (time.monotonic() - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_request[domain] = time.monotonic()
