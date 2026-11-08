# Hearth 2.1 — UI/UX modernization

## 1. Audit (v2.0.0 baseline)
- **Stack:** FastAPI + Jinja2 server-rendered pages, SQLite (12 tables, additive migrations in `db.py`), Pillow, Apprise, google-auth. No front-end build or framework; one stylesheet; inline SVG icon sprite. 66 routes, 34 tests. Deployed as one Docker image (GHCR) on Unraid; data in `/data`.
- **Connections already in place:** items ↔ tasks/logs ↔ contractors ↔ appointments ↔ documents/photos ↔ shopping ↔ ledger. Maintenance costs are read from `task_logs` straight into the ledger (no copies).

## 2. Problems found (prioritised)
1. **Plan:** wide desktop table, form always visible, recurrence typed as raw day counts, no one-time tasks / filters / priority, completion details only before marking done.
2. **Organize:** 11-field item form shown at once; no inventory search/filter/categories/list view; rooms couldn't be created from the item form.
3. **Finances:** always-visible ledger form; no categories, ledger search/filter/edit or receipt on expenses; chart drew an arbitrary $0–$100 scale with no data; forecast fixed at 10 years and ignored the fund balance and missing data.
4. **Home:** "% of tasks on schedule" misleading with little data; zero vs unknown not distinguished; checklist shown in full to established users.
5. **Visual system:** ~520 lines of layered overrides; uppercase micro-labels; mixed emphasis; input borders below 3:1 contrast.
6. **Brand:** single PNG; no SVG logo, favicon set or app manifest; no illustrations.
7. **Accessibility:** no reduced-motion support, some icon buttons unlabeled, small touch targets on phones, raw JSON error pages, dialogs didn't focus the first field.

## 3. Design system
- **Palette tokens** (`:root` in `static/style.css`): Charcoal `#252B29`, Forest `#36594B`, Sage `#90A795`, Cream `#F5F1E8`, Copper `#BE855B`. Semantic tokens: `--bg --surface --surface-2 --fg --muted --line --line-strong --field --accent --accent-soft --accent-2 --copper-text --bad/--warn/--ok (+ -soft)`. Dark mode is its own set of values, not an inversion.
- **Contrast (WCAG AA, computed):** body text 12.8–14.9:1; muted text ≥ 5.2:1; accent text ≥ 6.4:1; white on Forest 7.8:1; status text on tints ≥ 5.3:1; copper text uses `--copper-text` (5.1:1+) because raw Copper is decorative only; form field borders 3.3–3.9:1 (UI 3:1).
- **Type:** Inter/DM Sans if installed, else the system UI font. Scale: display 30, h1 26, section 17, card 15.5, body 15.5, small 13.5, xs 12; tabular numbers for money. Sentence case throughout (no uppercase labels).
- **Shape/space:** radii 8/12/16, spacing 4–32, restrained shadow, 1px borders, 40px minimum touch targets (coarse pointers).
- **Components:** cards, buttons/icon buttons, inputs, segmented controls, filter pills, tabs, badges/chips, list rows, task rows, facts strip, meters, tooltips (`.tip-i`), dialogs (centred sheet, right-hand side panel, bottom sheet on phones), toasts, empty states with illustrations, responsive tables (`.rtable` stacks into cards < 640px), charts.
- **Icons:** one Lucide-style stroke set (2px, 18px default). Navigation: house, calendar-check, package, wallet; sub-pages: wrench, calendar, cart, file-text, images, users, wallet, chart; settings gear. No emoji in the UI.
- **Brand assets** (`static/brand/`): primary horizontal logo (light, dark, mono), icon-only mark (light, dark, mono), app icon, maskable PWA icon, PNGs (512, 192, 180, 32), favicon SVG, `manifest.webmanifest`. Kept opt-in until approved: **Settings → Hearth logo → Use the new logo**.
- **Illustrations** (`templates/illus/*.svg`, inline, theme-aware, `aria-hidden`): home, calendar, maintenance, done, kitchen, living, bedroom, garage, bathroom, utility, budget, expense, savings, appliance, documents, photos, people, shopping. Rooms without a cover photo get the illustration matching their name.

## 4. Phases delivered
1. **Foundation:** token stylesheet rewrite, light/dark Hearth themes (legacy colour themes kept), type scale, icon set, dialog/panel/toast/tooltip components, navigation polish, brand assets and manifest.
2. **Usability:** New task panel with repeat presets (Daily…Yearly, custom; existing intervals preserved exactly), one-time tasks, priority, filters (Today/Upcoming/Overdue/Completed/All) and sorting, one-tap done with "Add details" afterwards (date, cost, who, notes, receipt) and undo; guided item creation (name, room, photo; details expandable; inline new room); Add funds / Record expense panels; illustrated empty states; responsive tables.
3. **Dashboard:** welcome banner, Up next prominent, quick actions (Add task, Add item, Record expense, Capture photo), honest insights, compact onboarding; room cards; inventory search/filter/grid/list; budget metrics with tooltips, category breakdown, year-by-year; forecast periods (1/3/5/10 yrs).
4. **Integration:** expense ↔ item ↔ receipt documents; task completion receipts filed to the item; forecast uses current fund balance and suggests monthly savings; budget page shows forecast vs actual monthly allocation.
5. **Advanced (evaluated, not built):** see §7.

## 5. Data & compatibility
- Additive migrations only: `tasks.priority`, `tasks.completed_at`, `items.category`, `fund_entries.category`, `fund_entries.document_id`. Existing rows remain valid (NULL = uncategorized / not completed).
- Recurrence: `interval_days` keeps its meaning; `0` now means "does not repeat" (no existing task has 0, as creation previously required ≥ 1).
- All routes kept; new ones added (`/logs/{id}`, `/tasks/{id}/reopen`, `/rooms/{id}`, `/budget/entries/{id}`, `/settings/brand`, `/settings/categories`). Old form fields (e.g. `interval_days`) still accepted.
- Maintenance expenses still come only from `task_logs`; editing a completion changes the amount and never adds a second expense.

## 6. Verification
- 44 automated tests (pytest), including: every link and `#anchor` reachable from every page resolves; recurrence presets/custom/legacy; one-time lifecycle; completion details, receipt filing, undo restoring the previous due date; no duplicate maintenance expense; guided item create with inline room + photo; inventory filters; ledger categories/filters/sort/edit; forecast periods, coverage and savings maths; insights never show a % from insufficient data; friendly HTML errors and JSON for API; brand toggle; migrations from older schemas.
- Visual QA with headless Chromium in light and dark, at 360, 390, 768 and 1280px; no horizontal overflow on any page; dialogs open/close with keyboard, focus the first field and return focus; no console errors.
- Not covered: real Google Calendar account (stubbed client), screen-reader run-through with NVDA/VoiceOver, Safari/iOS-specific rendering.

## 7. Recommendations for later releases
- Self-host Inter (≈100 KB woff2 subset) for identical typography on every device.
- Photo-assisted entry: OCR on receipts/labels (Tesseract sidecar) to prefill brand, model, serial, price, date; optional AI identification behind a setting.
- Predictive maintenance suggestions from item category (e.g. a new water heater suggests "Flush yearly").
- Replacement cost hints by category; inflation adjustment in the forecast.
- Dashboard customization (hide/reorder cards); printable annual home report (PDF).
- Two-way calendar sync; per-user accounts if more than one household member uses it.
- Contractors and Documents creation could also move into panels for full consistency.
