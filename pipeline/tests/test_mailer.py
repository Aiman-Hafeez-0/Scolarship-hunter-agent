"""Tests for the Gmail mailer, with a fake SMTP server (nothing is really sent)."""

import smtplib
import ssl
from email.message import EmailMessage
from types import TracebackType
from typing import Any

import pytest

from pipeline.config import Settings
from pipeline.notify.mailer import GmailMailer, MailError, build_message

PASSWORD = "abcd efgh ijkl mnop"


class FakeSMTP:
    """Records what the mailer does. fail_on names a step that raises instead."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        self.login_args: tuple[str, str] | None = None
        self.connected_to: tuple[str, int, float] | None = None

    def __call__(self, host: str, port: int, timeout: float) -> "FakeSMTP":
        self.connected_to = (host, port, timeout)
        return self

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.calls.append("quit")

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise smtplib.SMTPAuthenticationError(535, f"bad login {PASSWORD.replace(' ', '')}")

    def ehlo(self) -> None:
        self._step("ehlo")

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        self._step("starttls")

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)
        self._step("login")

    def send_message(self, message: EmailMessage) -> None:
        self._step("send")
        self.sent.append(message)


def _mailer(fake: FakeSMTP) -> GmailMailer:
    connect: Any = fake
    return GmailMailer("bot@gmail.com", PASSWORD, connect=connect)


def test_sends_with_starttls_and_login() -> None:
    fake = FakeSMTP()
    message_id = _mailer(fake).send("me@example.com", "Hello", "plain", "<p>html</p>")

    assert fake.connected_to == ("smtp.gmail.com", 587, 30.0)
    assert fake.calls == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert fake.login_args == ("bot@gmail.com", "abcdefghijklmnop")  # spaces removed
    [sent] = fake.sent
    assert sent["Message-ID"] == message_id
    assert sent["To"] == "me@example.com"


def test_message_has_plain_and_html_parts() -> None:
    message = build_message("bot@gmail.com", "me@example.com", "Hi", "plain text", "<b>html</b>")
    types = [part.get_content_type() for part in message.iter_parts()]
    assert types == ["text/plain", "text/html"]
    assert message["Subject"] == "Hi"


def test_error_never_contains_the_password() -> None:
    fake = FakeSMTP(fail_on="login")
    with pytest.raises(MailError) as info:
        _mailer(fake).send("me@example.com", "Hello", "plain", "<p>html</p>")
    assert "abcdefghijklmnop" not in str(info.value)
    assert "***" in str(info.value)
    assert fake.sent == []


def test_missing_credentials_are_a_setup_error() -> None:
    with pytest.raises(ValueError, match="GMAIL_APP_PASSWORD"):
        GmailMailer("bot@gmail.com", "")


def test_password_not_shown_when_settings_are_printed() -> None:
    settings = Settings(_env_file=None, gmail_address="bot@gmail.com", gmail_app_password="s3cret")
    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings)


def _refuse(host: str, port: int, timeout: float) -> Any:
    raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


def test_falls_back_to_port_465_with_ssl() -> None:
    ssl_fake = FakeSMTP()
    connect: Any = _refuse
    connect_ssl: Any = ssl_fake
    mailer = GmailMailer("bot@gmail.com", PASSWORD, connect=connect, connect_ssl=connect_ssl)
    lines: list[str] = []
    mailer.send("me@example.com", "Hello", "plain", "<p>html</p>", log=lines.append)

    assert ssl_fake.connected_to == ("smtp.gmail.com", 465, 30.0)
    assert ssl_fake.calls == ["ehlo", "login", "send", "quit"]  # already encrypted: no STARTTLS
    assert any("port 587 failed" in line for line in lines)
    assert len(ssl_fake.sent) == 1


def test_both_ports_failing_is_a_mail_error() -> None:
    connect: Any = _refuse
    mailer = GmailMailer("bot@gmail.com", PASSWORD, connect=connect, connect_ssl=connect)
    with pytest.raises(MailError, match="failed at connect"):
        mailer.send("me@example.com", "Hello", "plain", "<p>html</p>")


def test_log_shows_every_step_and_never_the_password() -> None:
    fake = FakeSMTP(fail_on="login")
    lines: list[str] = []
    with pytest.raises(MailError, match="failed at login"):
        _mailer(fake).send("me@example.com", "Hello", "plain", "<p>html</p>", log=lines.append)
    joined = "\n".join(lines)
    for step in ("connect", "EHLO", "STARTTLS", "EHLO again", "login"):
        assert step in joined
    assert "send to" not in joined
    assert "abcdefghijklmnop" not in joined


def test_all_whitespace_removed_from_password() -> None:
    fake = FakeSMTP()
    connect: Any = fake
    GmailMailer("bot@gmail.com", " abcd efgh ijkl	mnop ", connect=connect).send(
        "me@example.com", "Hello", "plain", "<p>html</p>"
    )
    assert fake.login_args == ("bot@gmail.com", "abcdefghijklmnop")
