# Dashboard design guide

Every dashboard page follows this guide. If a page needs something that is not here, add it
here first (as a component), then use it.

## Feel

Calm, trustworthy, modern. The dashboard shows facts that decide real applications, so it must
look precise and quiet: lots of white space, one accent color, and color used only when it
means something.

## Color

All colors are Tailwind's built-in palette. Light theme is the default; dark mode is a toggle
in the top bar, saved in `localStorage` (`theme` = `light` or `dark`).

| Role | Light | Dark | Used for |
| --- | --- | --- | --- |
| Page background | `slate-50` | `slate-950` | `<body>` |
| Surface | `white` | `slate-900` | cards, tables, sidebar, dialogs |
| Border | `slate-200` | `slate-800` | card edges, dividers |
| Text | `slate-900` | `slate-100` | body text, titles |
| Secondary text | `slate-600` | `slate-400` | helper text, labels, metadata |
| Accent | `teal-700` | `teal-400` | primary buttons, links, active nav, focus ring |

**One accent only.** Deep teal is the only "brand" color. Never use it to mean a status.

**Status colors carry meaning, and nothing else:**

| Color | Meaning | Example |
| --- | --- | --- |
| Red | a deadline in **7 days or less**; errors; danger buttons | "3 days left" |
| Amber | a deadline in **30 days or less**; needs review; warnings | "21 days left" |
| Green | **eligible**; success | "Eligible" |
| Blue | **maybe** eligible | "Maybe" |
| Gray | **closed**, not eligible, not matched, deadlines more than 30 days away | "Closed" |

Status chips use a pale background, a darker text of the same color and a thin border
(for example `bg-red-50 text-red-800 border-red-200`; dark: `bg-red-500/15 text-red-300
border-red-500/30`). A status is never shown by color alone: the chip always has words.

**Contrast (WCAG AA):** body text at least 4.5:1, large text and icons at least 3:1. That is
why small secondary text is `slate-600` (not `slate-400`) in the light theme.

## Type

Font: **Manrope** (Google Fonts, weights 400, 500, 600, 700), with the system sans-serif as a
fallback.

| Name | Class | Size / line height | Weight | Used for |
| --- | --- | --- | --- | --- |
| Page title | `text-title` | 24 / 32 px | 700 | one per page, in the top bar |
| Section title | `text-section` | 16 / 24 px | 600 | card headings |
| Body | `text-body` | 15 / 24 px | 400 | normal text |
| Small | `text-small` | 13 / 20 px | 500 | labels, helper text, chips, table headers |
| Stat | `text-stat` | 30 / 36 px | 700 | the number on a stat card |

**Numbers that line up** (dates, days left, scores, counts, tokens) use `tabular-nums`, so
columns of digits do not wobble.

## Space, shape, depth

- Spacing follows Tailwind's 4 px steps. Cards have `p-5` (`p-4` on phones); gaps between
  cards are `gap-6`.
- Corners: `rounded-xl` for cards and dialogs, `rounded-lg` for buttons and inputs,
  `rounded-full` for chips and the avatar.
- Depth: cards have a border and `shadow-xs`. Dialogs, the slide-over and toasts have
  `shadow-xl`. Nothing else casts a shadow.

## Layout

- **Desktop (1024 px and up):** a fixed left sidebar (256 px): logo, navigation with icons
  (Overview, Opportunities, Deadlines, Sources, Runs, Profile), the active page highlighted
  (accent text on a pale teal background), and the last run's status at the bottom.
- **Top bar:** breadcrumbs above the page title on the left; on the right the global search
  (press `/` to focus it), the theme toggle, **Run now**, and the user menu (initial avatar,
  Profile, Log out).
- **Phones and tablets (under 1024 px):** the sidebar is hidden. A top bar has a menu button
  that opens the same navigation as a drawer from the left. The search box moves to its own
  row under the top bar.
- Content is at most `max-w-7xl` wide and works from **360 px** wide: tables turn into
  stacked cards under 640 px, and nothing scrolls sideways except a wide table inside its card.

## Components

All live in `app/templates/components/`. Import what a page needs, for example
`{% from "components/badges.html" import countdown %}`.

| File | Macros | Notes |
| --- | --- | --- |
| `icons.html` | `icon(name, cls)` | Lucide icons (ISC license) as inline SVG, `aria-hidden`. Size with `size-4` / `size-5`. |
| `buttons.html` | `button`, `link_button` | Variants: `primary` (accent fill), `secondary` (white with border), `ghost` (no border), `danger` (red fill). Sizes `sm`, `md`. Optional icon. |
| `badges.html` | `badge`, `match_badge`, `run_badge`, `type_badge`, `review_badge`, `unchecked_badge`, `countdown` | `countdown` is the chip "1 day left", colored by the deadline rule above. Each badge has `data-badge` / `data-tone` so tests can check meaning, not CSS. |
| `cards.html` | `card` (with `caller`), `stat_card` | A card has an optional title, action link and body. |
| `tables.html` | `table`, `th` | A bordered card with a scrollable table; header in `text-small` uppercase. |
| `forms.html` | `text_field`, `select_field`, `checkbox`, `textarea`, `field_errors` | Label above, helper text below, error text in red below with `aria-invalid` and `aria-describedby`. |
| `empty.html` | `empty_state` | Icon, short title, one friendly sentence, optional action. Never just "No data". |
| `toast.html` | `toast` | Bottom-right, `role="status"`, closes by itself after 6 s (errors stay until closed). |
| `slide_over.html` | `slide_over` (with `caller`) | A `<dialog>` that slides in from the right, for side forms like "Add source". |
| `dialog.html` | `confirm_dialog` | A `<dialog>` that asks "Are you sure?" before an action (used by Run now). |
| `time.html` | `rel_time`, `exact_time` | "2 hours ago" with the exact PKT time on hover (`<time title=...>`). |

## Rules for every page

- **Time:** every date and time is shown in Pakistan time (PKT). Times in the past are shown
  as relative ("2 hours ago"), with the exact time ("18 Nov 2026, 08:17 PKT") on hover.
  Deadlines show the date plus a countdown chip.
- **Keyboard:** everything works with the keyboard. Focus is always visible: a 2 px accent
  ring with an offset (`focus-visible:ring-2`). `Esc` closes dialogs, the drawer and menus.
  A "Skip to content" link is the first thing on the page.
- **Empty states:** every list has a friendly empty state that says what will appear and, when
  possible, what to do.
- **Loading:** buttons that start slow work show a spinner and are disabled until done.
- **Errors:** say what happened and what to do, in plain words ("Wrong email or password").
- **No JavaScript framework.** Pages are server-rendered with Jinja2; HTMX swaps parts of a
  page; `app/static/app.js` is a small file of plain JavaScript (theme, drawer, dialogs,
  toasts, `/` shortcut). Every form still works without JavaScript.

## CSS build

Tailwind runs from its standalone CLI (no Node). The source is `app/assets/tailwind.css`; the
built, minified `app/static/app.css` is committed. Rebuild after changing any class in a
template or in `app.js`:

```
uv run python -m app.build_css
```
