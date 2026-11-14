"""Tests for pipeline.ratelimit."""

import asyncio
import time

from pipeline.ratelimit import DomainRateLimiter, domain_of

DELAY = 0.2


async def _timed(*domains: str) -> float:
    limiter = DomainRateLimiter(DELAY)
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait(d) for d in domains))
    return time.monotonic() - start


async def test_same_domain_waits() -> None:
    assert await _timed("a.edu.pk", "a.edu.pk") >= DELAY * 0.9


async def test_different_domains_do_not_wait() -> None:
    assert await _timed("a.edu.pk", "b.edu.pk", "c.edu.pk") < DELAY / 2


async def test_first_request_does_not_wait() -> None:
    assert await _timed("a.edu.pk") < DELAY / 2


def test_domain_of() -> None:
    assert domain_of("https://WWW.Example.com:443/a?b=1") == "www.example.com"
