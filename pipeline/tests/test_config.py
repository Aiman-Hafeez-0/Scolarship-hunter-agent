"""Smoke tests for pipeline.config."""

from pathlib import Path

import pytest

from pipeline.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.database_url is None
    assert settings.request_timeout_s == 30
    assert settings.per_domain_delay_s == 5
    assert settings.retries == 2
    assert settings.retry_backoff_s == 2
    assert settings.max_concurrency == 4
    assert settings.sources_file == Path("pipeline/sources.yaml")
    assert settings.data_dir == Path("data")


def test_user_agent_uses_contact_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "me@example.com")
    settings = Settings(_env_file=None)

    assert settings.user_agent == "ScholarshipHunterBot/0.1 (contact: me@example.com)"
