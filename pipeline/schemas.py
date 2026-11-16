"""The shape of what the LLM must return for one page (Pydantic v2).

The same models are used twice: their JSON schema is sent to the LLM so it answers in
exactly this shape, and the answer is validated against them afterwards.
Every fact carries an `evidence` quote copied word for word from the page.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OpportunityType = Literal["scholarship", "admission", "entry_test"]
Location = Literal["abroad", "pakistan"]
YesNoUnclear = Literal["yes", "no", "unclear"]


class _Strict(BaseModel):
    # Unknown keys are an error, so a misspelled field is caught instead of silently lost.
    model_config = ConfigDict(extra="forbid")


class DeadlineItem(_Strict):
    label: str = Field(description='What the deadline is for, e.g. "Application deadline".')
    deadline_date: date | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD). Null if the page does not write the year.",
    )
    time_and_timezone: str | None = Field(
        default=None, description='Time and timezone exactly as written, e.g. "23:59 CET".'
    )
    is_estimated: bool = Field(
        default=False,
        description="True only if the page itself says expected, tentative or similar.",
    )
    evidence: str = Field(description="The exact sentence from the page, copied word for word.")


class Coverage(_Strict):
    tuition: str | None = None
    monthly_stipend: str | None = None
    stipend_currency: str | None = None
    housing: str | None = None
    flight: str | None = None
    health_insurance: str | None = None
    evidence: str | None = Field(default=None, description="Exact quote about what is covered.")


class Requirement(_Strict):
    kind: Literal[
        "age_max", "cgpa_min", "english_test", "nationality", "degree", "work_experience", "other"
    ]
    value: str | None = Field(
        default=None, description='Short machine value, e.g. "30" or "IELTS 6.5".'
    )
    description: str
    evidence: str


class ScholarshipExtraction(_Strict):
    name: str
    provider: str | None = None
    country: str | None = None
    opportunity_type: OpportunityType
    location: Location
    campus: str | None = None
    intake: str | None = Field(
        default=None, description='The intake exactly as written, e.g. "Spring 2027".'
    )
    intake_evidence: str | None = Field(
        default=None, description="Exact quote that names the intake."
    )
    entry_tests: list[str] = Field(default_factory=list)
    degree_levels: list[Literal["bachelor", "master", "phd", "research", "other"]] = Field(
        default_factory=list
    )
    degree_levels_evidence: str | None = Field(
        default=None,
        description="Exact quote (or the item's name as written) with the degree words.",
    )
    fields_of_study: list[str] = Field(default_factory=list)
    funding_type: Literal["full", "partial", "tuition_only", "unknown"] = "unknown"
    coverage: Coverage | None = None
    application_fee: str | None = None
    application_fee_evidence: str | None = None
    deadlines: list[DeadlineItem] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    open_to_pakistan: YesNoUnclear = "unclear"
    open_to_pakistan_evidence: str | None = None
    moi_accepted: YesNoUnclear = "unclear"
    moi_evidence: str | None = None
    application_url: str | None = None
    status: Literal["open", "upcoming", "closed", "unknown"] = "unknown"


class PageExtraction(_Strict):
    """Everything found on one page: several opportunities, one, or none."""

    scholarships: list[ScholarshipExtraction] = Field(default_factory=list)


def llm_json_schema() -> dict[str, Any]:
    """PageExtraction's JSON schema with every $ref replaced by its definition.

    Some LLM APIs handle "$ref" poorly, so we send one flat, self-contained schema.
    """
    schema = PageExtraction.model_json_schema()
    definitions: dict[str, Any] = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return inline(definitions[name])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    result: dict[str, Any] = inline(schema)
    return result
