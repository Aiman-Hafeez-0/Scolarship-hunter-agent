"""Tests for app.auth and app.hash_password."""

from unittest.mock import patch

from argon2 import PasswordHasher

from app import auth
from app.hash_password import run
from app.tests.conftest import EMAIL, PASSWORD, make_settings


def test_check_login() -> None:
    settings = make_settings()
    assert auth.check_login(settings, EMAIL, PASSWORD)
    assert auth.check_login(settings, " Owner@Example.com ", PASSWORD)  # case and spaces
    assert not auth.check_login(settings, EMAIL, "wrong")
    assert not auth.check_login(settings, "other@example.com", PASSWORD)


def test_broken_hash_never_logs_in() -> None:
    assert not auth.check_login(make_settings(dashboard_password_hash="not-a-hash"), EMAIL, "x")


def test_setup_problems() -> None:
    assert auth.setup_problems(make_settings()) == []
    problems = auth.setup_problems(
        make_settings(dashboard_email="", dashboard_password_hash=None, session_secret="short")
    )
    assert len(problems) == 3
    assert not auth.check_login(make_settings(session_secret=None), EMAIL, PASSWORD)


def test_session_round_trip_and_tampering() -> None:
    settings = make_settings()
    cookie = auth.make_session(settings, EMAIL)
    assert auth.read_session(settings, cookie) == EMAIL
    assert auth.read_session(settings, cookie[:-2] + "xx") is None
    assert auth.read_session(make_settings(session_secret="y" * 40), cookie) is None
    assert auth.read_session(make_settings(dashboard_email="new@example.com"), cookie) is None
    assert auth.read_session(settings, None) is None


def test_session_expires_after_7_days() -> None:
    settings = make_settings()
    with patch("itsdangerous.timed.time.time", return_value=1_000_000):
        cookie = auth.make_session(settings, EMAIL)
    with patch("itsdangerous.timed.time.time", return_value=1_000_000 + 7 * 24 * 3600 - 10):
        assert auth.read_session(settings, cookie) == EMAIL
    with patch("itsdangerous.timed.time.time", return_value=1_000_000 + 7 * 24 * 3600 + 10):
        assert auth.read_session(settings, cookie) is None


def test_csrf_cookie_cannot_be_used_as_session() -> None:
    settings = make_settings()
    signed = auth.sign_csrf(settings, "token")
    assert auth.read_csrf(settings, signed) == "token"
    assert auth.read_session(settings, signed) is None  # different purpose, different salt
    assert not auth.csrf_ok(None, "token")
    assert not auth.csrf_ok("token", "")
    assert auth.csrf_ok("token", "token")


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = auth.LoginRateLimiter(clock=lambda: now[0])
    for _ in range(4):
        limiter.record_failure("1.2.3.4")
    assert not limiter.blocked("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4")
    assert not limiter.blocked("5.6.7.8")  # another address is not affected
    assert 0 < limiter.retry_after_s("1.2.3.4") <= 15 * 60 + 1

    now[0] = 15 * 60 + 1  # the oldest failures leave the 15-minute window
    assert not limiter.blocked("1.2.3.4")


def test_is_local() -> None:
    assert auth.is_local("localhost") and auth.is_local("127.0.0.1")
    assert not auth.is_local("dashboard.example.org")


def test_hash_password_command_prints_a_working_hash() -> None:
    answers = iter(["a long password!", "a long password!"])
    output, code = run(ask=lambda _prompt: next(answers))

    assert code == 0
    line = next(x for x in output.splitlines() if x.startswith("DASHBOARD_PASSWORD_HASH="))
    hashed = line.split("=", 1)[1].strip("'")
    assert PasswordHasher().verify(hashed, "a long password!")
    secret = next(x for x in output.splitlines() if x.startswith("SESSION_SECRET="))
    assert len(secret.split("=", 1)[1]) >= auth.MIN_SECRET_LENGTH
    assert "a long password!" not in output


def test_hash_password_command_refuses_short_or_different_passwords() -> None:
    assert run(ask=lambda _prompt: "short")[1] == 1
    answers = iter(["a long password!", "another password"])
    output, code = run(ask=lambda _prompt: next(answers))
    assert code == 1 and "different" in output
