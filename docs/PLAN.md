# Hearth prototype upgrade — plan

Stack stays: FastAPI + Jinja + SQLite, no SPA. Every phase is committed separately; the existing pages keep working throughout.

## Data safety (applies to every phase)
- A versioned migration runner (`app/migrations.py`) with **up and down** steps, recorded in `schema_version`.
- Before applying any pending migration, the DB is copied with the SQLite backup API to `$DATA_DIR/backups/hearth-<timestamp>-pre-v<N>.db`.
- Migrations only **add** tables/columns/triggers/indexes. One exception is unavoidable: `shopping_items` has a `CHECK (list IN ('groceries','supplies'))` that blocks custom lists; it is rebuilt by copy (same rows, verified by count) — still reversible.
- `HEARTH_DEMO=1` uses a separate directory (`$DATA_DIR/demo/`) and is visibly labelled; real data is never touched.

## Phase 1 — API contract (`/api/v1`)
- Sync identity on every synced table: `uuid`, `created_at`, `updated_at` (ISO-8601 UTC), filled by triggers so existing web code needs no changes; existing rows backfilled.
- Change feed: triggers append to `changelog(seq, type, uuid, op)`; deletes (web or API, incl. cascades) leave a `tombstones` row. `GET /changes?since=<seq>` returns `{changes:[{type,record}], deleted:[{type,id}], cursor, hasMore}`.
- Soft delete as seen by clients: `DELETE` (or PUT with `deleted_at`) removes the row and keeps a tombstone; `GET` of a deleted id returns `{id, deleted_at}`; a newer PUT resurrects it.
- `PUT /<type>/<id>`: idempotent upsert, client-generated UUIDs, last-write-wins on `updated_at`, response is always the server copy. Foreign keys travel as UUIDs.
- Types: tasks, completions, items, locations (nested), documents, photos, contractors, lists, list-items, pantry, ledger, funds, vehicles, projects.
- Files: `POST /files` → `{id,url}`; documents/photos reference `file_id`.
- Device tokens (hashed), one-time 10-minute pair codes, `POST /pair`, `/notify-config`, `/health`, OpenAPI at `/api/v1/openapi.json` + `docs/openapi.json`.

## Phase 2 — Tailscale
Unraid 7 native toggle + `docker-compose.tailscale.yml` sidecar; Tailscale Serve for HTTPS; trust `Tailscale-User-*` headers only on loopback connections (and drop `--proxy-headers`); household members; Settings → Remote access (status, URL, HTTPS, pairing QR, devices). `docs/TAILSCALE.md`, no Funnel.

## Phase 3 — PWA
Manifest, service worker (app shell + recently viewed pages for offline), install prompt, offline shopping list with a replay queue, ntfy push subscription details.

## Phase 4 — UX + motion
Nav Today · Plan · Stuff · Money; Stuff filter chips; Today = actions only; progress ring; staggered seeding + Rebalance; freshness bars for chores vs hard dates for safety tasks; command palette; swipe done/snooze; View Transitions, spring check-off, day-cleared celebration, count-ups, skeletons; dark default; reduced motion respected.

## Phase 5 — Fast capture
Capture hub: barcode (BarcodeDetector → ZXing fallback; Open Food Facts + pluggable lookup), label photo (model/serial), receipt photo (total/date/store) via pluggable OCR (Tesseract.js now; server hook for an LLM later); manual lookup + attach PDF to an item.

## Phase 6 — Money
Per-item sinking funds (purchase date + lifespan + replacement cost), roll-up in Money; cost-of-ownership timeline per item; warranty alerts (exists, 30 days); insurance claim packet PDF by room or whole house.

## Phase 7 — Pantry & shopping
Pantry with min quantity → auto-add to shopping; best-before + "use soon"; aisle order per store.

## Phase 8 — Scaffolds
Vehicles (odometer log, mileage-or-date maintenance, service history, cost per mile); Projects & Hobbies (materials linked to inventory, budget vs actual, photos, status; consumables stock).

## Done
Docs (ARCHITECTURE, PLAN, TAILSCALE, openapi.json, PROTOTYPE_REPORT with screenshots), Docker + Tailscale compose, tests (auth, pairing, changes feed, idempotent upserts, header trust, staggering), demo mode.
