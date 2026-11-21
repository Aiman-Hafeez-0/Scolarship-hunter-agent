"""Login for the dashboard: password check, signed cookies, CSRF tokens and a rate limit.

- The password is never stored, only its argon2 hash (DASHBOARD_PASSWORD_HASH).
- After login the browser gets a session cookie signed with SESSION_SECRET. Anyone can read
  a signed cookie, but nobody can change or forge it without the secret. With "Keep me
  signed in" it lasts 7 days; without it, the browser forgets it when it closes and the
  server refuses it after 12 hours anyway (the signature itself carries the time, so an old
  cookie stops working too).
- CSRF: every POST form carries a random token that must equal the one in the (signed) CSRF
  cookie. Another website can make your browser send a form, but it cannot read the token.
- At most 5 failed logins per 15 minutes from one IP address. The failures are kept in the
  database (see PostgresLoginLimiter in app/queries.py), because on Vercel each request may
  run on a different server instance and instances share no memory.
"""

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from pipeline.config import Settings

SESSION_COOKIE = "sh_session"
CSRF_COOKIE = "sh_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE_S = 7 * 24 * 3600
SHORT_SESSION_MAX_AGE_S = 12 * 3600  # "Keep me signed in" not ticked
MIN_SECRET_LENGTH = 32
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """The argon2 hash to put in DASHBOARD_PASSWORD_HASH."""
    return _hasher.hash(password)


def setup_problems(settings: Settings) -> list[str]:
    """What is missing before anyone can log in (empty list: login is ready)."""
    problems = []
    if not settings.dashboard_email:
        problems.append("DASHBOARD_EMAIL is not set")
    if not settings.dashboard_password_hash:
        problems.append("DASHBOARD_PASSWORD_HASH is not set")
    if len(settings.session_secret or "") < MIN_SECRET_LENGTH:
        problems.append(f"SESSION_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters")
    return problems


def check_login(settings: Settings, email: str, password: str) -> bool:
    """True if the email and password are right. The password hash is always checked, even
    for a wrong email, so the answer takes the same time either way."""
    if setup_problems(settings):
        return False
    assert settings.dashboard_password_hash is not None
    password_ok: bool
    try:
        password_ok = _hasher.verify(settings.dashboard_password_hash, password)
    except (VerificationError, InvalidHashError):
        password_ok = False
    email_ok = hmac.compare_digest(
        email.strip().lower().encode(), settings.dashboard_email.strip().lower().encode()
    )
    return password_ok and email_ok


def _serializer(settings: Settings, purpose: str) -> URLSafeTimedSerializer:
    assert settings.session_secret is not None
    return URLSafeTimedSerializer(settings.session_secret, salt=f"scholarship-hunter-{purpose}")


def make_session(settings: Settings, email: str, remember: bool = True) -> str:
    return _serializer(settings, "session").dumps({"email": email, "remember": remember})


def read_session(
    settings: Settings, cookie: str | None, now: datetime | None = None
) -> str | None:
    """The logged-in email, or None if the cookie is missing, forged or too old: older than
    7 days, or than 12 hours when "Keep me signed in" was not ticked."""
    if not cookie or setup_problems(settings):
        return None
    try:
        data, signed_at = _serializer(settings, "session").loads(
            cookie, max_age=SESSION_MAX_AGE_S, return_timestamp=True
        )
    except BadSignature:  # also covers SignatureExpired
        return None
    email = data.get("email") if isinstance(data, dict) else None
    if not isinstance(email, str):
        return None
    age = (now or datetime.now(UTC)) - signed_at
    if not data.get("remember", True) and age > timedelta(seconds=SHORT_SESSION_MAX_AGE_S):
        return None
    same = hmac.compare_digest(email.lower().encode(), settings.dashboard_email.lower().encode())
    return email if same else None  # a changed DASHBOARD_EMAIL logs the old one out


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def sign_csrf(settings: Settings, token: str) -> str:
    return _serializer(settings, "csrf").dumps(token)


def read_csrf(settings: Settings, cookie: str | None) -> str | None:
    """The CSRF token stored in the cookie, or None if missing or tampered with."""
    if not cookie or not settings.session_secret:
        return None
    try:
        token = _serializer(settings, "csrf").loads(cookie, max_age=SESSION_MAX_AGE_S)
    except BadSignature:
        return None
    return token if isinstance(token, str) else None


def csrf_ok(expected: str | None, sent: str | None) -> bool:
    return bool(expected and sent) and hmac.compare_digest(str(expected), str(sent))


def is_local(hostname: str | None) -> bool:
    """On localhost the cookie may travel over plain http; everywhere else only over https."""
    return (hostname or "") in LOCAL_HOSTS


MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_S = 15 * 60


class LoginLimiter(Protocol):
    """Counts failed logins per key (the client's IP address) inside a moving time window."""

    def blocked(self, key: str) -> bool:
        """True after MAX_LOGIN_FAILURES failures in the last LOGIN_WINDOW_S seconds."""
        ...

    def retry_after_s(self, key: str) -> int:
        """Seconds until one more try is allowed (0 when not blocked)."""
        ...

    def record_failure(self, key: str) -> None: ...

    def reset(self, key: str) -> None:
        """Forget this key's failures (after a successful login)."""
        ...
