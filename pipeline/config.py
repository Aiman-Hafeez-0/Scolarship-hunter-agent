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

    sources_file: Path = Path("pipeline/sources.yaml")
    data_dir: Path = Path("data")

    @property
    def user_agent(self) -> str:
        """The polite User-Agent sent with every request, so site owners can reach us."""
        return f"ScholarshipHunterBot/0.1 (contact: {self.contact_email})"
