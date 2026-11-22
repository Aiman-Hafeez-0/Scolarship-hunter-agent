# Prompt review: last weekly run vs. the golden set (November 2026)

The weekly run of **21 Nov 2026** (17 sources checked, prompt `extract_v3`, Groq
`openai/gpt-oss-120b` and Gemini) compared with the hand-written answers in
`pipeline/tests/golden/`. Read from Supabase, nothing changed. No eval was run (it costs
tokens); the suggestions below are for `extract_v5`, after you have run the eval on `extract_v4`.

None of these suggestions loosens validation: every fact still needs an exact quote, and the
quote and date checks stay as they are.

## What was missed, and why

| Page | Golden answer | Last weekly run | Why |
| --- | --- | --- | --- |
| `turkiye-burslari` | 1 item "Türkiye Scholarships", fee **free**, no dates | **No item from v3.** The only saved item is left over from `extract_v1` (fee empty). | A home page full of menus and no dates. Rule 6 says "no information → empty list", and the model chose that. Nothing tells it how to read "free of charge". |
| `maeci-italy` | 1 item "MAECI grants", no dates, status closed | Found ("MAECI grants for the 2025-2026 academic year", closed). | The weekly run is right. The miss is in the eval run (Groq returned nothing): a **closed** call with no dates looks like "nothing to apply for". |
| `unipd-maeci` | Funding **partial** (only a monthly 900 euros) | v3 item has funding **unknown** (plus a stale v2 item). | The prompt never says what `full`, `partial`, `tuition_only` mean. |
| `polimi-merit` | "Invest your Talent in Italy", open to Pakistan **no** | Gemini: right ("no"). Groq: "unclear", and a different name ("… programme scholarships"). | The page gives a closed list of eligible countries without Pakistan; rule 10 only mentions a page that "excludes" Pakistan. |
| `hec-hat-calendar-pdf` | Every row: registration date **and** test date | Rows split over 6 items; block 1 session 5's test date (15 Nov) missing; one item shifted a column ("Test Date 04.12" is really the roll number slip date, "Result 13.12" is the test date). | Fixed in `extract_v4` (rule 11: two separate deadlines per row, read column by column). |

## Suggested prompt changes (for extract_v5)

**1. Home pages of a named programme (Türkiye).** Add to rule 6:

> The home page of one named scholarship programme (its name is the site title or repeats in
> the headings, e.g. "Türkiye Scholarships") is one entry for that programme, even with no
> dates and even when most of the text is menus. Never return an empty list for such a page.

**2. Closed calls are still entries (MAECI).** Add to rule 9:

> A call the page says is closed is still an entry: set `status` to "closed" and add no
> deadlines. Rule 4 (leave out past deadlines) is about dates only, never about the entry.

**3. Funding words (Padua; also the 64% funding score).** New rule:

> `funding_type`: "full" only if the page says both tuition and living costs are paid;
> "tuition_only" if only tuition is waived (any percentage); "partial" if some costs are paid
> but not all, for example only a monthly stipend; "unknown" if the page does not say. Put the
> sentence that shows it in `coverage.evidence`.

**4. Application fee (Türkiye; also the 75% fee score).** New rule:

> `application_fee`: the cost to apply, exactly as written (e.g. "EUR 30"). Write "free" when
> the page says applying costs nothing ("free of charge", "no application fee"). A fee paid
> only by winners or after admission is not an application fee. Copy the sentence into
> `application_fee_evidence`.

**5. A list of eligible countries (Invest your Talent).** Add to rule 10:

> If the page gives a closed list of eligible countries and Pakistan is not in it,
> `open_to_pakistan` is "no"; copy the list as evidence.

**6. Names exactly as written (Invest your Talent; fewer duplicates).** Add to rule 6:

> Use the opportunity's name exactly as the page writes it ("Invest your Talent in Italy"),
> without adding words such as "programme", "scholarships" or the year.

## Two findings outside the prompt

- **Stale items stay forever.** When a page is extracted again and an item is no longer
  returned (or comes back under another name), the old row stays in `scholarships`. The
  Türkiye item is from `extract_v1`; Padua, Polimi and HEC also keep v2 items, and Gemini and
  Groq runs left the same programme under two names. The dashboard now shows near-duplicates
  once, but the digest and matching still see them. Worth a fix: mark the items of a source
  that the newest extraction did not return as replaced.
- **Shifted table columns pass validation.** "Test Date 2026-12-04" passed because 04.12.2026
  really is in the row's quote. A stricter check (not a looser one) could compare a table date
  with its column: for rows under a known header, the n-th date must belong to the n-th
  column. Rule 11 in `extract_v4` should prevent most of it; check the eval first.
