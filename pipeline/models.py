"""Data models shared by the pipeline steps."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

# A "slug" is a short lowercase id such as "turkiye-burslari": letters, digits and single dashes.
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class SourceType(StrEnum):
    """How much we trust a page: "official" is the real owner, "lead" just points to one."""

    OFFICIAL = "official"
    LEAD = "lead"


class OpportunityType(StrEnum):
    SCHOLARSHIP = "scholarship"
    ADMISSION = "admission"
    ENTRY_TEST = "entry_test"


class Location(StrEnum):
    ABROAD = "abroad"
    PAKISTAN = "pakistan"


class Source(BaseModel):
    """One web page (or PDF) that we check for deadlines and requirements."""

    # extra="forbid" turns a typo like "activ: false" into an error instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Slug
    name: str
    url: str
    program_key: Slug
    country: str
    type: SourceType = SourceType.OFFICIAL
    opportunity_type: OpportunityType
    location: Location
    render_js: bool = False
    is_pdf: bool = False
    active: bool = True
    note: str | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("url must start with https://")
        return value


class FetchStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    BLOCKED_ROBOTS = "blocked_robots"
    SKIPPED_JS = "skipped_js"


class Change(StrEnum):
    """How a page compares with the last time we saw it (filled in after fetching)."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass
class FetchResult:
    """What happened when we tried to download one source."""

    source: Source
    status: FetchStatus
    http_status: int | None = None
    content_type: str | None = None
    text: str | None = None
    content_hash: str | None = None
    error: str | None = None
    change: Change | None = None
