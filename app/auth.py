"""Login for the dashboard: password check, signed cookies, CSRF tokens and a rate limit.

- The password is never stored, only its argon2 hash (DASHBOARD_PASSWORD_HASH).
- After login the browser gets a session cookie signed with SESSION_SECRET. Anyone can read
  a signed cookie, but nobody can change or forge it without the secret. It expires after
  7 days (the signature itself carries the time, so an old cookie stops working too).
- CSRF: every POST form carries a random token that must equal the one in the (signed) CSRF
  cookie. Another website can make your browser send a form, but it cannot read the token.
- At most 5 failed logins per 15 minutes from one IP address (kept in memory, so restarting
  the server clears it).
"""

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from pipeline.config import Settings

SESSION_COOKIE = "sh_session"
CSRF_COOKIE = "sh_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE_S = 7 * 24 * 3600
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


def make_session(settings: Settings, email: str) -> str:
    return _serializer(settings, "session").dumps({"email": email})


def read_session(settings: Settings, cookie: str | None) -> str | None:
    """The logged-in email, or None if the cookie is missing, forged or older than 7 days."""
    if not cookie or setup_problems(settings):
        return None
    try:
        data = _serializer(settings, "session").loads(cookie, max_age=SESSION_MAX_AGE_S)
    except BadSignature:  # also covers SignatureExpired
        return None
    email = data.get("email") if isinstance(data, dict) else None
    if not isinstance(email, str):
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


@dataclass
class LoginRateLimiter:
    """Allows `max_failures` failed logins per `window_s` seconds for each key (IP address)."""

    max_failures: int = 5
    window_s: float = 15 * 60
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, deque[float]] = field(default_factory=dict)

    def _recent(self, key: str) -> deque[float]:
        times = self._failures.setdefault(key, deque())
        cutoff = self.clock() - self.window_s
        while times and times[0] <= cutoff:
            times.popleft()
        return times

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def record_failure(self, key: str) -> None:
        self._recent(key).append(self.clock())

    def retry_after_s(self, key: str) -> int:
        """Seconds until the oldest failure leaves the window (0 when not blocked)."""
        times = self._recent(key)
        if len(times) < self.max_failures:
            return 0
        return max(1, int(times[0] + self.window_s - self.clock()) + 1)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
