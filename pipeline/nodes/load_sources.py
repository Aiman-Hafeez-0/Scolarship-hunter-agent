"""Read pipeline/sources.yaml and turn it into a list of checked Source objects."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from pipeline.models import Source


class SourcesError(ValueError):
    """Raised when sources.yaml is missing, malformed, or has a bad or duplicate entry."""


def load_sources(path: Path, include_inactive: bool = False) -> list[Source]:
    """Load and validate every source in the YAML file.

    Every entry is validated, even inactive ones, so mistakes show up early.
    Inactive sources are left out of the result unless include_inactive is True.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourcesError(f"Could not read {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise SourcesError(f"{path} must have a top-level 'sources:' list")

    sources: list[Source] = []
    seen_keys: set[str] = set()
    seen_urls: set[str] = set()

    for index, entry in enumerate(data["sources"]):
        label = entry.get("key", f"#{index + 1}") if isinstance(entry, dict) else f"#{index + 1}"
        try:
            source = Source.model_validate(entry)
        except ValidationError as exc:
            raise SourcesError(f"Invalid source {label} in {path}:\n{exc}") from exc

        if source.key in seen_keys:
            raise SourcesError(f"Duplicate source key '{source.key}' in {path}")
        if source.url in seen_urls:
            raise SourcesError(
                f"Duplicate source url '{source.url}' (key '{source.key}') in {path}"
            )
        seen_keys.add(source.key)
        seen_urls.add(source.url)
        sources.append(source)

    if include_inactive:
        return sources
    return [source for source in sources if source.active]
