"""Tests for pipeline.robots (RFC 9309 behaviour)."""

import httpx
import respx

from pipeline.ratelimit import DomainRateLimiter
from pipeline.robots import RobotsChecker

ROBOTS = "https://example.edu.pk/robots.txt"


def _checker(client: httpx.AsyncClient) -> RobotsChecker:
    return RobotsChecker(client, DomainRateLimiter(0))


async def test_rules_are_obeyed(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).respond(
        200, text="User-agent: ScholarshipHunterBot\nDisallow: /private/\n"
    )
    async with httpx.AsyncClient() as client:
        robots = _checker(client)
        assert await robots.allowed("https://example.edu.pk/admissions")
        assert not await robots.allowed("https://example.edu.pk/private/page")


async def test_disallow_all_for_everyone(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /\n")
    async with httpx.AsyncClient() as client:
        assert not await _checker(client).allowed("https://example.edu.pk/admissions")


async def test_404_allows_everything(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).respond(404)
    async with httpx.AsyncClient() as client:
        assert await _checker(client).allowed("https://example.edu.pk/anything")


async def test_503_disallows_everything(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).respond(503)
    async with httpx.AsyncClient() as client:
        assert not await _checker(client).allowed("https://example.edu.pk/anything")


async def test_network_error_disallows(mock_http: respx.MockRouter) -> None:
    mock_http.get(ROBOTS).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        assert not await _checker(client).allowed("https://example.edu.pk/anything")


async def test_robots_fetched_once_per_site(mock_http: respx.MockRouter) -> None:
    route = mock_http.get(ROBOTS).respond(200, text="User-agent: *\nAllow: /\n")
    async with httpx.AsyncClient() as client:
        robots = _checker(client)
        assert await robots.allowed("https://example.edu.pk/a")
        assert await robots.allowed("https://example.edu.pk/b")
    assert route.call_count == 1
