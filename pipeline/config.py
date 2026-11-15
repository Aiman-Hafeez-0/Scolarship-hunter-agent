"""App settings, loaded from environment variables and the .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunable settings in one place. Values in .env override these defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str | None = None
    contact_email: str = ""

    request_timeout_s: float = 30.0
    per_domain_delay_s: float = 5.0
    retries: int = 2
    retry_backoff_s: float = 2.0
    max_concurrency: int = 4

    # LLM extraction. Model names were checked against the official docs on 2026-11-14:
    # gemini-3.8-flash is the current stable Flash model; openai/gpt-oss-120b is a production
    # Groq model that supports JSON-schema output.
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    groq_model: str = "openai/gpt-oss-120b"
    llm_min_interval_s: float = 5.0  # pause between LLM calls, to stay inside free-tier limits
    llm_retries: int = 2  # extra tries after an HTTP 429 or 5xx
    llm_retry_backoff_s: float = 15.0  # first wait after a 429; doubles each retry

    sources_file: Path = Path("pipeline/sources.yaml")
    profile_file: Path = Path("profile.yaml")
    data_dir: Path = Path("data")

    @property
    def user_agent(self) -> str:
        """The polite User-Agent sent with every request, so site owners can reach us."""
        return f"ScholarshipHunterBot/0.1 (contact: {self.contact_email})"
