# Hearth

Self-hosted home manager for Unraid: maintenance reminders, home inventory, photos, documents & manuals, contractors, and budgeting. FastAPI + SQLite, single container.

## Features
- **Maintenance** – recurring tasks with due dates, one-click "done", cost/contractor logging, full history. Seed with 12 common home tasks.
- **Inventory** – rooms → items with brand/model/serial, purchase price, warranty, expected lifespan.
- **Documents** – upload receipts/manuals/policies, optionally linked to an item.
- **Photos** – capture from a phone camera (or webcam over HTTPS) anywhere in the app, caption it, and file it to an item or room later. Item galleries and room cover photos.
- **Contractors** – contact list with job count and total spend.
- **Budget → Balance** – yearly budget, money allocated to the home fund, money spent (manual expenses + maintenance costs), budget remaining and fund balance, monthly allocated-vs-spent chart, ledger with running balance.
- **Budget → Forecast** – replacement forecast by year from purchase date + lifespan, suggested monthly allocation.
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
| `HEARTH_USERNAME` / `HEARTH_PASSWORD` | – | set both to enable basic auth |
| `PORT` | `8080` | |

## Dev
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest -q
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Auth is optional basic auth. Without it, keep Hearth on your LAN or behind a reverse proxy with auth.
