# Changelog

## 3.1.0
- **Sign-in**: per-user accounts with a sign-in page. The first visit creates the owner account; owners add,
  reset and remove household accounts in Settings → Accounts; everyone can change their own password and
  sign out. scrypt password hashes, hashed session tokens, sign-in throttling. `HEARTH_USERNAME`/`HEARTH_PASSWORD`
  now pre-create the owner account; `HEARTH_AUTH=off` disables sign-in. Migration v3 (reversible).
- Money is now called Budget in the menu.

## 3.0.0 (prototype)
- REST API v1 for the Android client: sync change feed, idempotent LWW upserts, soft deletes, files, device
  pairing and tokens; OpenAPI in `docs/openapi.json`. Reversible migrations with automatic backups.
- Tailscale remote access (native or sidecar), tailnet sign-in with loopback-only header trust, pairing QR.
- PWA: offline shell, offline shopping list, install prompt, ntfy push config.
- Today · Plan · Stuff · Money; staggered scheduling + Rebalance (fixes all seeded tasks due the same day);
  freshness bars for chores, hard dates for safety tasks; command palette; swipe; motion; dark default.
- Capture: barcode lookup, label/receipt reading (browser OCR or Claude), manual attach.
- Sinking funds, cost of ownership, insurance claim packet PDF; pantry with minimums and use-soon; aisle
  order per store; Vehicles; Projects & Hobbies; `HEARTH_DEMO=1` demo mode.

## 2.1.0
**Design**
- New design system: Hearth palette (Forest, Copper, Sage, Cream, Charcoal) as tokens, coordinated light/dark themes with WCAG AA contrast, consistent type scale, sentence-case labels, restrained cards and shadows.
- One Lucide-style icon set; new navigation icons; no emoji in the interface.
- New Hearth logo family (SVG + PNG, light/dark/mono, app + maskable icons, favicon, web manifest). Opt-in from Settings until approved.
- 18 theme-aware illustrations for the welcome banner, empty states and rooms.
- Dialogs: side panels on desktop, bottom sheets on phones; toasts confirm actions.

**Plan**
- Tasks: Today / Upcoming / Overdue / Completed / All filters, sorting, mobile-friendly task rows.
- New task panel with friendly repeat presets and custom intervals; one-time tasks; high priority.
- One-tap Done, then optional details (date, cost, who, notes, receipt) and undo.
- Task detail: facts, history, related item and documents, edit panel.

**Organize**
- Inventory: room cards with illustrations or cover photos, search, category and warranty filters, grid/list views.
- Guided item creation: name, room (or create one inline), photo; details expandable. Item categories.

**Finances**
- Budget metrics with explanations; Add funds / Record expense panels; expense categories (configurable); receipts on expenses; editable entries; ledger search, filters and sorting; spending by category; year-by-year totals; empty state instead of an empty chart.
- Replacement forecast: 1/3/5/10-year periods, chart with per-year items, suggested monthly savings that account for the fund balance, coverage and missing-data list, method explanation.

**Home**
- Welcome banner, quick actions (task, item, expense, photo), insights that distinguish zero from no data, compact onboarding.

**Other**
- Friendly error pages; reduced-motion and forced-colours support; visible focus; labelled icon buttons; larger touch targets on phones.
- Shopping trips logged to the budget are categorised (Groceries/Supplies).
- Additive DB migrations only; all existing routes and data preserved.

## 2.0.0
Four-destination navigation, action-first home, universal search, + New menu, capture-to-record, connected item and contractor pages, warranty alerts, backup/CSV.
