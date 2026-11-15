"""The owner's profile: read from profile.yaml (gitignored) and checked with Pydantic.

Copy profile.example.yaml to profile.yaml, fill it in, then run:

    uv run python -m pipeline.run profile
"""

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ProfileError(ValueError):
    """profile.yaml is missing or has a bad value."""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    nationality: str = "PK"
    date_of_birth: date | None = None
    degree: str | None = None  # the degree you have now, e.g. "BS Computer Science"
    cgpa: float | None = Field(default=None, ge=0)
    cgpa_scale: float = Field(default=4.0, gt=0)
    english_test: str | None = None  # e.g. "IELTS"; empty if you have not taken one
    english_score: str | None = None  # e.g. "6.5"
    moi_letter: bool = False  # you have a Medium of Instruction (English) letter
    work_experience_months: int = Field(default=0, ge=0)
    target_fields: list[str] = Field(default_factory=list)
    target_intake: str | None = None  # e.g. "Fall 2027"
    max_application_fee_eur: float | None = Field(default=None, ge=0)  # None = no cap
    prefer_no_fee: bool = True
    fully_funded_only: bool = True
    reminder_days: list[int] = Field(default_factory=lambda: [30, 14, 7, 2])

    @model_validator(mode="after")
    def cgpa_within_scale(self) -> "Profile":
        if self.cgpa is not None and self.cgpa > self.cgpa_scale:
            raise ValueError(f"cgpa {self.cgpa} is higher than cgpa_scale {self.cgpa_scale}")
        return self


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise ProfileError(
            f"{path} not found: copy profile.example.yaml to {path.name} and fill it in"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Profile.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ProfileError(f"Problem in {path}:\n{exc}") from exc
