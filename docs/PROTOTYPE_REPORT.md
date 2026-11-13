# Hearth 3.0 prototype — report

Built on the existing FastAPI + Jinja + SQLite app; every existing page still works and every phase was
committed separately (`git log --oneline 18b7cf1..`). **99 automated tests pass** (`.venv/bin/pytest`).
Screenshots below were taken with Playwright in demo mode (`HEARTH_DEMO=1`), dark theme (the new default).

## What works

| Area | Status |
|---|---|
| **API v1** (`/api/v1`, `docs/openapi.json`) | 14 resource types (tasks, completions, items, nested locations, documents, photos, contractors, lists, list-items, pantry, ledger, funds, vehicles, projects). UUID ids (client-generated), ISO-8601 UTC timestamps, `deleted_at`. `PUT` upsert is idempotent and last-write-wins by `updated_at`, always returning the server copy. `GET /changes?since&limit` → `{changes, deleted, cursor, hasMore}` includes web edits and cascaded deletes. `POST /files` → `{id,url}`. Bearer device tokens (SHA-256 at rest, revocable), one-time 10-minute pair codes, `/pair`, `/notify-config`, `/health`, `/me`. |
| **Data safety** | Versioned up/down migrations (`python -m app.migrations status|up|down N`), automatic SQLite backup to `/data/backups/` before migrating, row counts verified; tested up → down → up on a copy of real data. |
| **Tailscale** | Unraid 7 "Use Tailscale" + sidecar compose (`docker-compose.tailscale.yml`, Funnel disabled). Identity headers trusted only from loopback (stripped otherwise; `--proxy-headers` removed). Household members, pairing QR, device list/revoke, ntfy settings. `docs/TAILSCALE.md`. |
| **PWA** | Manifest, maskable icons, root-scoped service worker (app shell + 40 recent pages), offline page, install button. Shopping list works offline and replays in order on reconnect (verified in Chromium). |
| **UX** | Today · Plan · Stuff · Money; Stuff filter chips incl. Vehicles/Hobbies. Seeded tasks staggered (the all-same-day bug is fixed) plus **Rebalance schedule**. Chores use draining freshness bars and never go red; safety tasks keep hard dates. Command palette (`/`): "add milk to list", "log filter replaced 12/1", "new task … every 3 months", "go budget". Swipe right = done, left = snooze. View Transitions, spring check-off, one celebration per cleared day, count-ups, skeletons; all off under reduced motion. |
| **Capture** | Barcode (BarcodeDetector → ZXing) with Open Food Facts → UPCitemdb lookup; label (brand/model/serial) and receipt (total/date/store) reading via Tesseract.js in the browser (no AI services); manual finder + attach-PDF-by-URL. Verified live: OFF lookup, in-browser OCR of a receipt. |
| **Money** | Per-item sinking funds ("Set aside $38/mo — water heater due 2029") + custom funds, cost-of-ownership timeline, insurance claim packet PDF (house or room), warranty alerts at 30 days. |
| **Pantry** | Minimums auto-add to Groceries (web + API), restock on purchase, use-soon list, per-store aisle order, custom lists from the API shown on the web. |
| **Vehicles / Projects** | Odometer log, maintenance by miles or months (whichever first), service history, cost per mile; projects/hobbies with status, budget vs actual, materials linked to inventory, photos, consumables stock. |
| **Demo mode** | `HEARTH_DEMO=1` uses `/data/demo/` only (real `hearth.db` never opened), seeds a realistic household, shows a banner, and never sends notifications or calendar events. |

Required tests: auth, pairing (`tests/test_api.py`), change feed, idempotent upserts and LWW (`test_api.py`), header-trust rule (`tests/test_tailnet.py`), task staggering (`tests/test_plan.py`).

## Stubbed or partial
- **Tailscale** paths are tested with simulated loopback/LAN peers; not yet run on a real tailnet or Unraid's native toggle (the image runs as `nobody`; the sidecar is the fallback if the toggle needs root).
- **Push**: Hearth publishes to ntfy through Apprise and exposes `/notify-config`; there is no Web Push (no Firebase, by design). Phones need the ntfy app or web subscription.
- **Product lookup for goods** uses UPCitemdb's free trial endpoint (rate-limited); groceries via Open Food Facts are solid.
- **ZXing and Tesseract.js** load from jsDelivr on first use, so offline barcode-from-photo/OCR needs internet once (BarcodeDetector on Android Chrome needs nothing).
- API types stop at the 14 in the contract: appointments, budgets and settings are web-only for now.
- Aisle order is edited as a text list (no drag-and-drop yet).

## Known issues / limits
- Last-write-wins trusts client clocks; a device with a wrong clock can lose or win edits unexpectedly. The server clock is in `/health` so the app can warn.
- `changelog` grows without compaction (a few rows per edit). Fine for a household; add pruning later.
- A deleted record reads back as `{id, deleted_at}` only (tombstone, not the old content).
- Multi-member households share one view; members have no per-person permissions yet.
- No Docker build on agent-01 (no Docker installed); the image is built by GitHub Actions on push — **not pushed yet**.

## Next steps
1. Push → CI image → update on Unraid; enable Tailscale (Option A, else B) and pair the Android app.
2. (Removed in 3.2: Hearth has no AI integration; photos are read on-device.)
3. Android client against `docs/openapi.json` (pair → `/changes` loop → `PUT` writes with client UUIDs + `updated_at`).
4. Expose appointments in the API; changelog pruning; per-member assignment of tasks.
5. Self-host ZXing/Tesseract assets for fully offline capture.

## Screenshots

| | Desktop (1280) | Phone (390) |
|---|---|---|
| Today | ![](screenshots/desktop-today.jpg) | ![](screenshots/phone-today.jpg) |
| Tasks (freshness, safety, rebalance) | ![](screenshots/desktop-tasks.jpg) | ![](screenshots/phone-tasks.jpg) |
| Command palette | ![](screenshots/desktop-palette.jpg) | ![](screenshots/phone-palette.jpg) |
| Task detail | ![](screenshots/desktop-task.jpg) | ![](screenshots/phone-task.jpg) |
| Shopping | ![](screenshots/desktop-shopping.jpg) | ![](screenshots/phone-shopping.jpg) |
| Pantry | ![](screenshots/desktop-pantry.jpg) | ![](screenshots/phone-pantry.jpg) |
| Inventory | ![](screenshots/desktop-inventory.jpg) | ![](screenshots/phone-inventory.jpg) |
| Item (cost of ownership) | ![](screenshots/desktop-item.jpg) | ![](screenshots/phone-item.jpg) |
| Capture (receipt) | ![](screenshots/desktop-capture.jpg) | ![](screenshots/phone-capture.jpg) |
| Budget | ![](screenshots/desktop-budget.jpg) | ![](screenshots/phone-budget.jpg) |
| Sinking funds | ![](screenshots/desktop-funds.jpg) | ![](screenshots/phone-funds.jpg) |
| Forecast | ![](screenshots/desktop-forecast.jpg) | ![](screenshots/phone-forecast.jpg) |
| Vehicle | ![](screenshots/desktop-vehicle.jpg) | ![](screenshots/phone-vehicle.jpg) |
| Project | ![](screenshots/desktop-project.jpg) | ![](screenshots/phone-project.jpg) |
| Settings → Remote access | ![](screenshots/desktop-settings.jpg) | ![](screenshots/phone-settings.jpg) |
