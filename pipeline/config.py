"""App settings, loaded from environment variables and the .env file."""

import re
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# "postgresql://user:PASSWORD@host" and "password=PASSWORD" (the key=value form).
_URL_PASSWORD = re.compile(r"(\w[\w+.-]*://[^:/@\s]*:)([^@\s]*)(@)")
_KEY_PASSWORD = re.compile(r"(\bpassword\s*=\s*)('[^']*'|\S+)", re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """Hide passwords in connection strings, so errors can be printed safely.

    "postgresql://me:s3cret@db.example.com/x" -> "postgresql://me:***@db.example.com/x"
    """
    text = _URL_PASSWORD.sub(r"\1***\3", text)
    return _KEY_PASSWORD.sub(r"\1***", text)


def strip_quotes(value: str) -> str:
    """Remove spaces and one pair of matching quotes around a value: '"abc"' -> 'abc'.

    The .env reader already removes quotes, but values from other places (a GitHub secret
    pasted with quotes, a shell `set`) may still have them.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


class Settings(BaseSettings):
    """All tunable settings in one place. Values in .env override these defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # repr=False: secrets never show up when Settings is printed or logged.
    database_url: str | None = Field(default=None, repr=False)
    contact_email: str = ""

    request_timeout_s: float = 30.0
    per_domain_delay_s: float = 5.0
    retries: int = 2
    retry_backoff_s: float = 2.0
    max_concurrency: int = 4

    # LLM extraction. Model names were checked against the official docs on 2026-11-15:
    # gemini-3.8-flash is the newest stable (not preview) Flash model, with no shutdown date
    # announced; openai/gpt-oss-120b is a production Groq model with JSON-schema output.
    gemini_api_key: str | None = Field(default=None, repr=False)
    groq_api_key: str | None = Field(default=None, repr=False)
    gemini_model: str = "gemini-3.8-flash"
    groq_model: str = "openai/gpt-oss-120b"
    llm_timeout_s: float = 60.0  # one LLM call may take at most this long
    llm_min_interval_s: float = 5.0  # pause between LLM calls, to stay inside free-tier limits
    llm_max_tries: int = 2  # calls per page and provider (first try + 1 retry), then fall back
    llm_retry_backoff_s: float = 10.0  # wait before retrying a 5xx, or a 429 with no retry-after
    llm_max_retry_wait_s: float = 60.0  # a 429 asking us to wait longer: switch provider now

    sources_file: Path = Path("pipeline/sources.yaml")
    profile_file: Path = Path("profile.yaml")
    data_dir: Path = Path("data")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_quotes(cls, value: Any) -> Any:
        return strip_quotes(value) if isinstance(value, str) else value

    @property
    def user_agent(self) -> str:
        """The polite User-Agent sent with every request, so site owners can reach us."""
        return f"ScholarshipHunterBot/0.1 (contact: {self.contact_email})"
