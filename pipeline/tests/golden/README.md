# Golden test set

15 real pages (`<key>.txt`, copied from Supabase with `uv run python -m pipeline.golden KEY ...`)
and the answers for each (`<key>.expected.json`), written by reading the page — never by the LLM.

`uv run python -m pipeline.eval` runs the real extraction + validation on every page, prints
accuracy per field and every mismatch, and appends a row to `results.csv`. It costs API tokens,
so it is not part of pytest or CI. (pytest does check that every expected quote is on its page
and every expected date is written in its quote.)

## How the answers are written

- **Deadlines**: required = the last day to apply / register / pay, and test dates.
  `"optional": true` = opening dates, interviews, results, semester starts: extracting them is
  neither needed nor wrong. Dates without a year are not listed. Dates already past are still
  correct if the page shows them.
- **Deadline score** is per page: `correct / (required + invented)`, where invented = an extracted
  date the page's answers do not list at all.
- **funding_type** (as read from the page): `full` = tuition and living costs; `tuition_only` =
  only tuition (any %); `partial` = some costs, but not all (e.g. only a stipend); `unknown` =
  the page does not say. `null` = too ambiguous to score.
- **open_to_pakistan** follows prompt rule 8: `yes` only if Pakistan is named or all countries
  are eligible. `null` = too ambiguous to score.
- **application_fee**: the cost to apply, compared by its numbers; `"free"` if the page says so;
  `null` if the page states no fee (this is scored).
