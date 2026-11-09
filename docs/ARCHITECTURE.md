# Hearth — architecture (as of v2.1.0, before the prototype upgrade)

## Stack
- **Server:** Python 3.13, FastAPI 0.162 + Uvicorn, server-rendered Jinja2 templates (no SPA, no build step).
- **DB:** SQLite (`$DATA_DIR/hearth.db`, `/data` in the container → Unraid `/mnt/user/appdata/hearth`). Raw `sqlite3`, no ORM. Uploads in `$DATA_DIR/uploads/` (UUID file names; photos re-encoded to JPEG + 480px thumb by Pillow).
- **Front end:** one token-based stylesheet (`app/static/style.css`), inline SVG icon sprite, theme-aware SVG illustrations, small vanilla JS in `base.html` (dialogs, shortcuts, toasts). Charts are server-rendered SVG.
- **Packaging:** `Dockerfile` (python:3.13-slim, runs as 99:100), GitHub Actions builds `ghcr.io/anicholas65/hearth` (amd64+arm64) on push to `main`; Unraid template in `unraid/hearth.xml`.

## Modules (`app/`)
| file | role |
|---|---|
| `main.py` | all HTTP routes (74) and template rendering, auth middleware, error pages |
| `db.py` | schema (`CREATE TABLE IF NOT EXISTS`), additive column migrations, settings key/value |
| `home.py` | dashboard: Up-next timeline, attention counts, setup steps, insights |
| `plan.py` | recurrence presets/labels, item & expense categories |
| `finance.py` | ledger (fund entries + task-log costs), budget/balance maths, replacement forecast |
| `shopping.py` | quick-add parsing, aisle categorisation |
| `search.py` | universal LIKE search across tables |
| `notify.py` | Apprise digest loop (tasks, appointments, warranties) |
| `gcal.py` | one-way Google Calendar sync via service account |
| `backup.py` | backup zip (sqlite backup API), CSV export, item CSV import |

## Data model (12 tables, integer primary keys)
`rooms`, `items` (room_id, category, warranty, lifespan, replacement cost), `tasks` (interval_days, next_due, priority, completed_at, gcal_event_id), `task_logs` (completions: date, cost, contractor), `contractors`, `appointments`, `documents`, `photos` (item/room, cover), `budgets` (per year), `fund_entries` (allocation/expense, category, item, receipt document), `shopping_items` (list ∈ groceries/supplies, status needed/checked/bought), `settings`.
Migrations: `db.MIGRATIONS` = list of additive `ALTER TABLE ADD COLUMN`, applied idempotently at startup. No down-migrations, no automatic backup before migrating.

## Auth
Optional HTTP Basic auth for every page (`HEARTH_USERNAME`/`HEARTH_PASSWORD`), `/api/health` exempt. No users, no tokens, no API.

## Routes (pages)
Home `/` · Plan `/tasks`, `/schedule` · Organize `/items`, `/shopping`, `/documents`, `/gallery`, `/contractors` · Finances `/budget`, `/budget?view=forecast` · `/search` · `/settings`. JSON only at `/api/health`.

## Reminders & integrations
- **Apprise** (`APPRISE_URLS`, e.g. `ntfy://`, Discord, email): hourly check, daily digest of due/overdue tasks, today/tomorrow appointments, warranties ending within 30 days.
- **Google Calendar:** service-account key in `/data`, calendar id/time zone in settings; tasks → all-day events, appointments/reminders → timed events; background sync on change.

## Known issues going into this upgrade
- Seeding 12 maintenance tasks gives them all the same due date (`today + min(interval, 30)`).
- Uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'`: any client can spoof its address via `X-Forwarded-For`. Harmless today (nothing trusts the client address) but must change before trusting Tailscale identity headers.
- No API, IDs or timestamps for sync; deletes are hard deletes.
