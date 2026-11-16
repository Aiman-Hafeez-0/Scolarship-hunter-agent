"""Smoke tests for pipeline.config."""

from pathlib import Path

import pytest

from pipeline.config import Settings, mask_secrets, strip_quotes


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


URL = "postgresql://postgres.abc:s3cret@aws-0.pooler.supabase.com:5432/postgres"


@pytest.mark.parametrize("line", [
    f"DATABASE_URL={URL}",
    f'DATABASE_URL="{URL}"',
    f"DATABASE_URL='{URL}'",
    f'DATABASE_URL = "{URL}"  ',
])  # fmt: skip
def test_env_file_with_or_without_quotes(
    line: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"{line}\nCONTACT_EMAIL='me@example.com'\n", encoding="utf-8")

    settings = Settings(_env_file=env)

    assert settings.database_url == URL
    assert settings.contact_email == "me@example.com"


def test_environment_variable_with_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f'"{URL}"')
    monkeypatch.setenv("GEMINI_API_KEY", " 'abc123' ")
    settings = Settings(_env_file=None)
    assert settings.database_url == URL
    assert settings.gemini_api_key == "abc123"


@pytest.mark.parametrize(("raw", "clean"), [
    ('"abc"', "abc"), ("'abc'", "abc"), ("  abc  ", "abc"), ('"abc', '"abc'),
    ("a'b", "a'b"), ('""', ""), ("", ""),
])  # fmt: skip
def test_strip_quotes(raw: str, clean: str) -> None:
    assert strip_quotes(raw) == clean


def test_mask_secrets_hides_passwords() -> None:
    assert mask_secrets(f"cannot connect to {URL}") == (
        "cannot connect to postgresql://postgres.abc:***@aws-0.pooler.supabase.com:5432/postgres"
    )
    assert mask_secrets("host=db user=me password=s3cret port=5432") == (
        "host=db user=me password=*** port=5432"
    )
    assert mask_secrets("no secrets here") == "no secrets here"


def test_secrets_are_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=URL, gemini_api_key="key-1",
                        groq_api_key="key-2")  # fmt: skip
    text = repr(settings)
    assert "s3cret" not in text and "key-1" not in text and "key-2" not in text
