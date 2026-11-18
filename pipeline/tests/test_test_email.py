"""Tests for the `test-email` command (with a fake SMTP server: nothing is really sent)."""

from typing import Any

import pytest

from pipeline.config import Settings
from pipeline.notify.mailer import GmailMailer
from pipeline.run import SetupError, run_test_email
from pipeline.tests.test_mailer import FakeSMTP


def _settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, gmail_address="bot@gmail.com",
                    gmail_app_password="abcd efgh ijkl mnop", **fields)  # fmt: skip


def _mailer(fake: FakeSMTP) -> GmailMailer:
    connect: Any = fake
    return GmailMailer("bot@gmail.com", "abcd efgh ijkl mnop", connect=connect)


def test_sends_to_admin_and_prints_each_step() -> None:
    fake = FakeSMTP()
    lines: list[str] = []
    code = run_test_email(_settings(admin_email="me@example.com"), lines.append, _mailer(fake))

    assert code == 0
    [sent] = fake.sent
    assert sent["To"] == "me@example.com"
    assert lines[-1] == "OK: test email sent to me@example.com"
    assert any(line.startswith("-> STARTTLS") for line in lines)
    assert not any("abcdefghijklmnop" in line for line in lines)


def test_failure_is_printed_and_exit_code_1() -> None:
    lines: list[str] = []
    fake = FakeSMTP(fail_on="login")
    code = run_test_email(_settings(admin_email="me@example.com"), lines.append, _mailer(fake))

    assert code == 1
    assert lines[-1].startswith("FAILED:") and "failed at login" in lines[-1]
    assert not any("abcdefghijklmnop" in line for line in lines)


def test_missing_admin_email_is_a_setup_error() -> None:
    with pytest.raises(SetupError, match="ADMIN_EMAIL"):
        run_test_email(_settings(), print)
