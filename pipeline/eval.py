"""Measure extraction quality on the golden test set (calls the real LLM API).

    uv run python -m pipeline.eval                    Gemini, with Groq as fallback
    uv run python -m pipeline.eval --provider groq    only Groq (or: gemini, auto)

For every pipeline/tests/golden/<key>.txt with a <key>.expected.json (written by hand),
run the real extraction + validation and compare with the expected answer:

- deadline date: scored per page. Each required expected date is correct if some extracted
  deadline (after validation) has that date. Extracted dates that are neither required nor
  optional in the expected file are invented and count against the score:
      accuracy = correct / (required + invented)
- funding_type, open_to_pakistan, application fee: scored per expected item. Extracted
  items are paired with expected items by name similarity. A field set to null in the
  expected file is too ambiguous to score and is skipped (application_fee null means
  "the page states no fee" and IS scored).

Prints accuracy per field and every mismatch, and appends one row to results.csv
(its `provider` column is the --provider option; `model` lists the models that answered).
Not part of pytest or CI, because it costs API tokens.
"""

import argparse
import csv
import io
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz

from pipeline.config import Settings
from pipeline.llm.provider import PROVIDER_CHOICES, ProviderChoice, build_provider
from pipeline.models import Source
from pipeline.nodes.extract import ExtractResult, extract_page
from pipeline.nodes.load_sources import load_sources
from pipeline.nodes.validate import ValidatedScholarship, simplify, validate_extraction
from pipeline.prompts import EXTRACT_VERSION

GOLDEN_DIR = Path(__file__).resolve().parent / "tests" / "golden"
RESULTS_CSV = GOLDEN_DIR / "results.csv"
FIELDS = ("funding_type", "open_to_pakistan", "application_fee")
CSV_HEADER = [
    "date", "provider", "model", "prompt_version", "pages",
    "deadline_correct", "deadline_total", "deadline_accuracy",
    "funding_type_accuracy", "open_to_pakistan_accuracy", "fee_accuracy",
    "tokens_in", "tokens_out",
]  # fmt: skip


# ---------- The expected answers ----------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldenDeadline(_Strict):
    label: str
    date: date
    quote: str
    optional: bool = False  # opening dates, results, semester starts: neither needed nor wrong


class GoldenItem(_Strict):
    name: str
    funding_type: Literal["full", "partial", "tuition_only", "unknown"] | None
    open_to_pakistan: Literal["yes", "no", "unclear"] | None
    application_fee: str | None
    deadlines: list[GoldenDeadline] = field(default_factory=list)


class Golden(_Strict):
    source: str
    notes: str = ""
    items: list[GoldenItem]


@dataclass
class GoldenPage:
    key: str
    text: str
    expected: Golden


def load_golden(golden_dir: Path = GOLDEN_DIR) -> list[GoldenPage]:
    """Every page that has both <key>.txt and <key>.expected.json, sorted by key."""
    pages: list[GoldenPage] = []
    for expected_path in sorted(golden_dir.glob("*.expected.json")):
        key = expected_path.name.removesuffix(".expected.json")
        expected = Golden.model_validate_json(expected_path.read_text(encoding="utf-8"))
        if expected.source != key:
            raise ValueError(f"{expected_path.name}: source is '{expected.source}'")
        text = (golden_dir / f"{key}.txt").read_text(encoding="utf-8")
        pages.append(GoldenPage(key, text, expected))
    return pages


# ---------- Scoring ----------


@dataclass
class Score:
    correct: int = 0
    total: int = 0

    def add(self, ok: bool) -> None:
        self.correct += ok
        self.total += 1

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.total if self.total else None


@dataclass
class PageResult:
    key: str
    deadlines: Score = field(default_factory=Score)
    fields: dict[str, Score] = field(default_factory=lambda: {f: Score() for f in FIELDS})
    mismatches: list[str] = field(default_factory=list)


def normalize_fee(fee: str | None) -> str | None:
    """Compare fees by their numbers: "PKR 5,000" == "Rs. 5000"; "free" == "0"."""
    if fee is None or not fee.strip():
        return None
    text = simplify(fee)
    if re.search(r"\bfree\b|\bno (application )?fee\b|free of charge", text):
        return "0"
    numbers = sorted({n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,.]*", text)})
    if numbers and all(float(n) == 0 for n in numbers):
        return "0"
    return " ".join(numbers) or text


def pair_items(
    expected: list[GoldenItem], extracted: list[ValidatedScholarship]
) -> list[tuple[GoldenItem, ValidatedScholarship | None]]:
    """Pair each expected item with the most similar extracted name (each used once)."""
    candidates = sorted(
        ((fuzz.token_set_ratio(simplify(e.name), simplify(x.item.name)), i, j)
         for i, e in enumerate(expected) for j, x in enumerate(extracted)),
        reverse=True,
    )  # fmt: skip
    chosen: dict[int, int] = {}
    used: set[int] = set()
    for _, i, j in candidates:
        if i not in chosen and j not in used:
            chosen[i] = j
            used.add(j)
    return [(e, extracted[chosen[i]] if i in chosen else None) for i, e in enumerate(expected)]


def score_page(page: GoldenPage, extracted: list[ValidatedScholarship]) -> PageResult:
    result = PageResult(page.key)

    # Deadlines, per page.
    required = {d.date: d for item in page.expected.items for d in item.deadlines
                if not d.optional}  # fmt: skip
    optional = {d.date for item in page.expected.items for d in item.deadlines if d.optional}
    got = {d.deadline_date: (v.item.name, d.label) for v in extracted for d in v.item.deadlines
           if d.deadline_date is not None}  # fmt: skip
    for day, expected in sorted(required.items()):
        result.deadlines.add(day in got)
        if day not in got:
            result.mismatches.append(f"deadline missed: {day} {expected.label!r}")
    for day, (name, label) in sorted(got.items()):
        if day not in required and day not in optional:
            result.deadlines.add(False)
            result.mismatches.append(f"deadline invented: {day} {label!r} (item {name!r})")

    # Other fields, per expected item.
    for expected_item, match in pair_items(page.expected.items, extracted):
        for name in FIELDS:
            want = getattr(expected_item, name)
            if name != "application_fee" and want is None:
                continue  # too ambiguous to score
            if match is None:
                result.fields[name].add(False)
                result.mismatches.append(f"{name}: item {expected_item.name!r} not extracted")
                continue
            have = getattr(match.item, name)
            if name == "application_fee":
                ok = normalize_fee(want) == normalize_fee(have)
            else:
                ok = want == have
            result.fields[name].add(ok)
            if not ok:
                result.mismatches.append(
                    f"{name}: {expected_item.name!r} expected {want!r}, got {have!r}"
                    f" (from {match.item.name!r})"
                )
    return result


def _sum(scores: list[Score]) -> Score:
    return Score(sum(s.correct for s in scores), sum(s.total for s in scores))


def deadline_total(results: list[PageResult]) -> Score:
    return _sum([r.deadlines for r in results])


def field_total(results: list[PageResult], name: str) -> Score:
    return _sum([r.fields[name] for r in results])


# ---------- Running ----------


@dataclass
class EvalRun:
    pages: list[PageResult]
    extractions: dict[str, ExtractResult]
    review_notes: dict[str, list[str]]

    @property
    def models(self) -> str:
        found = sorted({r.model for r in self.extractions.values() if r.model})
        return ";".join(found) or "none"


def run_eval(
    pages: list[GoldenPage],
    sources: dict[str, Source],
    extract: Callable[[str, Source], ExtractResult],
    today: date,
) -> EvalRun:
    results: list[PageResult] = []
    extractions: dict[str, ExtractResult] = {}
    notes: dict[str, list[str]] = {}
    for page in pages:
        print(f"extracting {page.key} ...", file=sys.stderr, flush=True)
        extraction = extract(page.text, sources[page.key])
        validated = validate_extraction(extraction.extraction, page.text, today)
        page_result = score_page(page, validated)
        page_result.mismatches.extend(f"extraction error: {e}" for e in extraction.errors)
        results.append(page_result)
        extractions[page.key] = extraction
        notes[page.key] = [n for v in validated for n in v.notes]
    return EvalRun(results, extractions, notes)


def _score_line(label: str, score: Score) -> str:
    percent = "n/a" if score.accuracy is None else f"{score.accuracy:.1%}"
    return f"  {label:<17} {score.correct:>3}/{score.total:<3}  {percent}"


def format_report(run: EvalRun) -> str:
    lines = ["Accuracy per field", "", _score_line("deadline date", deadline_total(run.pages))]
    lines += [_score_line(name, field_total(run.pages, name)) for name in FIELDS]
    tokens_in = sum(r.tokens_in for r in run.extractions.values())
    tokens_out = sum(r.tokens_out for r in run.extractions.values())
    lines += ["", f"Model: {run.models}   prompt: {EXTRACT_VERSION}   "
              f"tokens: {tokens_in:,} in, {tokens_out:,} out", "", "Mismatches"]  # fmt: skip
    any_mismatch = False
    for page in run.pages:
        if not page.mismatches:
            continue
        any_mismatch = True
        model = run.extractions[page.key].model or "none"
        lines.append(f"\n  [{page.key}] (model {model})")
        lines.extend(f"    - {m}" for m in page.mismatches)
        lines.extend(f"      validation: {n}" for n in run.review_notes[page.key])
    if not any_mismatch:
        lines.append("  none")
    return "\n".join(lines)


def csv_row(run: EvalRun, today: date, provider: ProviderChoice = "auto") -> list[str]:
    deadline = deadline_total(run.pages)

    def acc(score: Score) -> str:
        return "" if score.accuracy is None else f"{score.accuracy:.4f}"

    fields = [acc(field_total(run.pages, name)) for name in FIELDS]
    return [
        today.isoformat(), provider, run.models, EXTRACT_VERSION, str(len(run.pages)),
        str(deadline.correct), str(deadline.total), acc(deadline), *fields,
        str(sum(r.tokens_in for r in run.extractions.values())),
        str(sum(r.tokens_out for r in run.extractions.values())),
    ]  # fmt: skip


def append_results(row: list[str], path: Path = RESULTS_CSV) -> None:
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.eval")
    parser.add_argument("--provider", choices=PROVIDER_CHOICES, default="auto",
                        help="LLM to use (auto = Gemini, then Groq if Gemini fails)")
    args = parser.parse_args(argv)
    choice: ProviderChoice = args.provider

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    pages = load_golden()
    sources = {s.key: s for s in load_sources(settings.sources_file, include_inactive=True)}
    provider = build_provider(settings, choice)
    today = date.today()

    run = run_eval(pages, sources, lambda text, src: extract_page(text, src, provider), today)
    print(format_report(run))
    append_results(csv_row(run, today, choice))
    print(f"\nAppended to {RESULTS_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
