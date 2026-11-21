"""Reading the dashboard's edit forms: the profile, and a new source.

Each parse_* function turns the submitted text into a checked model, or a list of
problems in plain words to show next to the form.
"""

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.models import Source
from pipeline.profile import Profile
from pipeline.ratelimit import DomainRateLimiter
from pipeline.robots import RobotsChecker, make_client

PROFILE_TEXT_FIELDS = [
    "full_name", "email", "nationality", "date_of_birth", "degree", "cgpa", "cgpa_scale",
    "english_test", "english_score", "work_experience_months", "target_intake",
    "max_application_fee_eur",
]  # fmt: skip
PROFILE_CHECKBOXES = ["moi_letter", "prefer_no_fee", "fully_funded_only"]
SOURCE_TEXT_FIELDS = ["key", "name", "url", "program_key", "country", "type",
                      "opportunity_type", "location", "note"]  # fmt: skip


def _problems(exc: ValidationError) -> list[str]:
    """Pydantic errors as short lines like 'cgpa: Input should be a valid number'."""
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "form"
        lines.append(f"{where}: {error['msg'].removeprefix('Value error, ')}")
    return lines


def _checked(form: Mapping[str, str], name: str) -> bool:
    return form.get(name) in ("1", "on", "true")


def _split_list(text: str) -> list[str]:
    """One item per line or comma: 'AI, Data Science' -> ['AI', 'Data Science']."""
    items = [part.strip() for line in text.splitlines() for part in line.split(",")]
    return [item for item in items if item]


def parse_reminder_days(text: str) -> tuple[list[int], list[str]]:
    """'30, 14, 7, 2' -> [30, 14, 7, 2]. Each must be a whole number of days from 1 to 365."""
    days: set[int] = set()
    for part in _split_list(text):
        if not part.isdigit() or not 1 <= int(part) <= 365:
            return [], [f"reminder_days: '{part}' is not a whole number from 1 to 365"]
        days.add(int(part))
    return sorted(days, reverse=True), []


def parse_profile_form(form: Mapping[str, str]) -> tuple[Profile | None, bool, list[str]]:
    """(profile, emails_paused, problems). The profile is None when there are problems."""
    data: dict[str, Any] = {}
    for name in PROFILE_TEXT_FIELDS:
        value = form.get(name, "").strip()
        if value:  # an empty box means "not known": the model's default (usually None)
            data[name] = value
    for name in PROFILE_CHECKBOXES:
        data[name] = _checked(form, name)
    data["target_fields"] = _split_list(form.get("target_fields", ""))
    days, problems = parse_reminder_days(form.get("reminder_days", ""))
    data["reminder_days"] = days
    paused = _checked(form, "emails_paused")
    try:
        profile = Profile.model_validate(data)
    except ValidationError as exc:
        return None, paused, problems + _problems(exc)
    return (None if problems else profile), paused, problems


def profile_form_values(profile: Profile | None, paused: bool) -> dict[str, Any]:
    """What to put in each box of the profile form."""
    if profile is None:
        return {"emails_paused": paused, "reminder_days": "30, 14, 7, 2",
                "prefer_no_fee": True, "fully_funded_only": True}  # fmt: skip
    values: dict[str, Any] = {
        name: "" if getattr(profile, name) is None else str(getattr(profile, name))
        for name in PROFILE_TEXT_FIELDS
    }
    values |= {name: getattr(profile, name) for name in PROFILE_CHECKBOXES}
    values["target_fields"] = "\n".join(profile.target_fields)
    values["reminder_days"] = ", ".join(str(d) for d in profile.reminder_days)
    values["emails_paused"] = paused
    return values


def parse_source_form(form: Mapping[str, str]) -> tuple[Source | None, list[str]]:
    """A new source from the form (the url must be https://; key and program_key are slugs)."""
    data: dict[str, Any] = {}
    for name in SOURCE_TEXT_FIELDS:
        value = form.get(name, "").strip()
        if value:
            data[name] = value
    data.setdefault("program_key", data.get("key", ""))
    data["is_pdf"] = _checked(form, "is_pdf")
    data["active"] = True
    try:
        return Source.model_validate(data), []
    except ValidationError as exc:
        return None, _problems(exc)


async def robots_allows(url: str, settings: Settings) -> bool:
    """True if the site's robots.txt lets our bot fetch this url (same rules as the pipeline:
    no robots.txt (4xx) allows everything; an unreachable site allows nothing)."""
    async with make_client(settings) as client:
        checker = RobotsChecker(client, DomainRateLimiter(settings.per_domain_delay_s))
        return await checker.allowed(url)


def source_yaml(source: Source) -> str:
    """The same source as a sources.yaml entry (optional: the database is the source of
    truth), without fields left at their defaults."""
    data = source.model_dump(mode="json", exclude_defaults=True)
    data = {"key": data.pop("key"), "name": data.pop("name"), "url": data.pop("url"), **data}
    text = yaml.safe_dump([data], sort_keys=False, allow_unicode=True)
    return "\n".join("  " + line for line in text.splitlines())
