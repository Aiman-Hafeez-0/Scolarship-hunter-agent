"""Prompt templates for the LLM, kept as Markdown files next to this module."""

from datetime import date
from pathlib import Path

from pipeline.models import Source

PROMPTS_DIR = Path(__file__).resolve().parent
EXTRACT_VERSION = "extract_v4"


def extract_system_prompt(source: Source, today: date | None = None) -> str:
    """The extraction rules with this source's hints filled in."""
    template = (PROMPTS_DIR / f"{EXTRACT_VERSION}.md").read_text(encoding="utf-8")
    values = {
        "source_name": source.name,
        "opportunity_type": source.opportunity_type.value,
        "location": source.location.value,
        "today": (today or date.today()).isoformat(),
    }
    for name, value in values.items():
        template = template.replace("{{" + name + "}}", value)
    return template


def extract_user_message(page_text: str, feedback: str | None = None) -> str:
    """The page text, plus the validation error if the previous answer was invalid."""
    message = f"<page>\n{page_text}\n</page>"
    if feedback:
        message += (
            "\n\nYour previous answer did not match the schema. Error:\n"
            f"{feedback}\nAnswer again with valid JSON that follows every rule."
        )
    return message
