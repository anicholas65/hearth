# Hearth

Self-hosted home manager for Unraid: maintenance reminders, home inventory, photos, documents & manuals, contractors, and budgeting. FastAPI + SQLite, single container.

## What's new in 3.0 (prototype)
- **REST API** at `/api/v1` for the Android client: device pairing, sync change feed, idempotent upserts, files. Contract: `docs/openapi.json`.
- **Tailscale** remote access with HTTPS and tailnet sign-in (`docs/TAILSCALE.md`), installable **PWA** with an offline shopping list.
- **Today · Plan · Stuff · Budget**, staggered maintenance with Rebalance, freshness bars, command palette (`/`), swipe gestures.
- **Capture**: barcode lookup, label and receipt reading on the device (Tesseract.js) — no AI services.
- **Budget**: sinking funds, cost of ownership, insurance claim packet PDF. **Pantry**, **Vehicles**, **Projects & Hobbies**.
- `HEARTH_DEMO=1` runs on sample data in `/data/demo`. Details: `docs/PROTOTYPE_REPORT.md`.

## What's new in 2.1
A full visual and usability refresh: new design system and light/dark themes, a new logo (opt-in in Settings), illustrations, task filters and repeat presets, one-time tasks, guided item creation, inventory search and filters, expense categories and receipts, an editable ledger, and a replacement forecast with 1–10 year periods. See [CHANGELOG.md](CHANGELOG.md) and [docs/HEARTH-2.1.md](docs/HEARTH-2.1.md).

## What's new in 2.0
- **Simpler navigation**: four destinations (Home, Plan, Organize, Finances) with sub-tabs, plus a bottom tab bar on phones.
- **Action-first home**: what needs attention, one "Up next" timeline (tasks, appointments, expiring warranties), quick actions, a setup checklist instead of empty counters, and home insights.
- **Universal search** (press `/`) across items, tasks, schedule, documents, contractors, shopping, photos and budget notes.
- **One "+ New" menu** for every record type, led by camera capture.
- **Capture → record**: snap a photo, then turn it into a new item, a receipt, or attach it to something you own.
- **Connected item pages**: receipts, warranty status, maintenance, history and costs, contractors, bookings, photos and supplies in one place. Contractor pages show their work history.
- **Warranty-expiry alerts** in the daily digest.
- **Backup & CSV**: one-click backup zip, CSV exports, and inventory CSV import.
- **Hearth theme**: Pine, Copper, Sage and Cream, in light and dark mode.

## Features
- **Maintenance** – recurring tasks with due dates, one-click "done", cost/contractor logging, full history. Seed with 12 common home tasks.
- **Inventory** – rooms → items with brand/model/serial, purchase price, warranty, expected lifespan.
- **Documents** – upload receipts/manuals/policies, optionally linked to an item.
- **Photos** – capture from a phone camera (or webcam over HTTPS) anywhere in the app, caption it, and file it to an item or room later. Item galleries and room cover photos.
- **Contractors** – contact list with job count and total spend.
- **Budget → Balance** – yearly budget, money allocated to the home fund, money spent (manual expenses + maintenance costs), budget remaining and fund balance, monthly allocated-vs-spent chart, ledger with running balance.
- **Budget → Forecast** – replacement forecast by year from purchase date + lifespan, suggested monthly allocation.
- **Shopping list** – separate Groceries and Supplies lists. Quick-add parses quantities ("2 gal milk, bread, 6 eggs") and auto-sorts into aisles; tap to check off, "Done shopping" clears the cart and can log the total as a budget expense; one-tap "Buy again"; add parts straight from an inventory item; plain-text view for sharing.
- **Schedule** – timed appointments (contractor, item, location) and reminders, with per-entry alert times; upcoming items on the dashboard and in the daily digest.
- **Google Calendar sync** – tasks (all-day events that move when completed), appointments and reminders pushed to a Google calendar with alerts. One-way (Hearth → Google).
- **Notifications** – daily digest of due/overdue tasks via [Apprise](https://github.com/caronc/apprise/wiki) (ntfy, Discord, email, Pushover, Gotify…).

## Unraid
1. Docker → Add Container → Template: paste `https://raw.githubusercontent.com/anicholas65/hearth/main/unraid/hearth.xml`
   (or copy `unraid/hearth.xml` to `/boot/config/plugins/dockerMan/templates-user/my-hearth.xml`).
2. Set **Apprise URLs** (e.g. `ntfy://ntfy.sh/your-topic`) and apply.
3. Back up `/mnt/user/appdata/hearth` (DB + uploads).

The image is built to `ghcr.io/anicholas65/hearth` by GitHub Actions on push to `main`.

## Google Calendar setup
Uses a Google **service account** (no sign-in redirect, so it works on a LAN IP):
1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable the **Google Calendar API**.
2. IAM & Admin → Service Accounts → Create (no roles) → Keys → Add key → JSON.
3. Hearth → Settings → Google Calendar → **Upload JSON key** (stored as `/data/google-service-account.json`, mode 600).
4. In Google Calendar settings, share a calendar with the service account email (**Make changes to events**), copy its **Calendar ID** into Hearth, set your time zone, **Test connection**, then **Sync everything now**.

Optional env overrides: `GOOGLE_CALENDAR_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE`. Time zone defaults to `TZ` (Unraid sets this automatically).

## Env vars
| Var | Default | |
|---|---|---|
| `DATA_DIR` | `/data` | DB + uploads |
| `APPRISE_URLS` | – | comma-separated Apprise URLs |
| `NOTIFY_LEAD_DAYS` | `3` | days ahead to remind |
| `NOTIFY_CHECK_HOURS` | `1` | check frequency |
| `HEARTH_USERNAME` / `HEARTH_PASSWORD` | – | optional: creates this owner account on first start (otherwise Hearth asks you to create one) |
| `HEARTH_AUTH` | `on` | `off` disables sign-in (only on a network you fully trust) |
| `HEARTH_PUBLIC_URL` | – | address paired devices use (Tailscale HTTPS URL) |
| `OCR_URL` | – | optional self-hosted OCR service (POST image → `{"text"}`) |
| `HEARTH_DEMO` | `0` | `1` = sample data in `/data/demo` |
| `PORT` | `8080` | |

## Dev
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest -q
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Everyone signs in with their own account. The first visit asks you to create the owner account; owners add the rest of the household in **Settings → Accounts**. Passwords are stored as scrypt hashes; sessions last 90 days and end when the password changes. Tailscale users are signed in automatically, and the Android app uses its own device tokens.

## About
Hearth is developed by **Alan Nicholas**.
