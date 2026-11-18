"""Tests for the Resend mailer and the provider choice (respx fakes the API: nothing is sent)."""

import json

import httpx
import pytest
import respx

from pipeline.config import Settings
from pipeline.notify.mailer import (
    RESEND_FROM,
    RESEND_URL,
    GmailMailer,
    MailError,
    ResendMailer,
    build_mailer,
)

KEY = "re_test_secret_key"


def _settings(**fields: object) -> Settings:
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


@respx.mock
def test_success_sends_json_with_bearer_key() -> None:
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "abc-123"}))
    lines: list[str] = []
    email_id = ResendMailer(KEY).send("me@example.com", "Hi", "plain", "<p>html</p>", lines.append)

    assert email_id == "abc-123"
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "from": RESEND_FROM,
        "to": ["me@example.com"],
        "subject": "Hi",
        "html": "<p>html</p>",
        "text": "plain",
    }
    assert not any(KEY in line for line in lines)


@respx.mock
def test_403_shows_resend_message_and_hint() -> None:
    body = {"statusCode": 403, "name": "validation_error", "message": "You can only send testing"
            f" emails to your own email address. (key {KEY})"}  # fmt: skip
    respx.post(RESEND_URL).mock(return_value=httpx.Response(403, json=body))

    with pytest.raises(MailError) as info:
        ResendMailer(KEY).send("other@example.com", "Hi", "plain", "<p>html</p>")
    error = str(info.value)
    assert "HTTP 403" in error
    assert "You can only send testing emails" in error
    assert "can only send to the email you signed up with" in error
    assert KEY not in error


@respx.mock
def test_network_error_is_a_mail_error() -> None:
    respx.post(RESEND_URL).mock(side_effect=httpx.ConnectError("no network"))
    with pytest.raises(MailError, match="ConnectError"):
        ResendMailer(KEY).send("me@example.com", "Hi", "plain", "<p>html</p>")


def test_missing_key_is_a_setup_error() -> None:
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        ResendMailer("")
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        build_mailer(_settings())


def test_provider_choice() -> None:
    assert _settings().mail_provider == "resend"  # the default
    assert _settings(mail_provider="").mail_provider == "resend"  # blank GitHub secret
    assert isinstance(build_mailer(_settings(resend_api_key=KEY)), ResendMailer)
    gmail = _settings(mail_provider="Gmail", gmail_address="bot@gmail.com",
                      gmail_app_password="abcd efgh ijkl mnop")  # fmt: skip
    assert isinstance(build_mailer(gmail), GmailMailer)


def test_key_not_shown_when_settings_are_printed() -> None:
    settings = _settings(resend_api_key=KEY)
    assert KEY not in repr(settings)
    assert KEY not in str(settings)
