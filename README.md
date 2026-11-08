# Hearth

Self-hosted home manager for Unraid: maintenance reminders, home inventory, documents & manuals, contractors, and replacement budgeting. FastAPI + SQLite, single container.

## Features
- **Maintenance** – recurring tasks with due dates, one-click "done", cost/contractor logging, full history. Seed with 12 common home tasks.
- **Inventory** – rooms → items with brand/model/serial, purchase price, warranty, expected lifespan.
- **Documents** – upload receipts/manuals/policies, optionally linked to an item.
- **Contractors** – contact list with job count and total spend.
- **Budget** – replacement forecast by year from purchase date + lifespan, monthly set-aside, maintenance spend by year.
- **Notifications** – daily digest of due/overdue tasks via [Apprise](https://github.com/caronc/apprise/wiki) (ntfy, Discord, email, Pushover, Gotify…).

## Unraid
1. Docker → Add Container → Template: paste `https://raw.githubusercontent.com/anicholas65/hearth/main/unraid/hearth.xml`
   (or copy `unraid/hearth.xml` to `/boot/config/plugins/dockerMan/templates-user/my-hearth.xml`).
2. Set **Apprise URLs** (e.g. `ntfy://ntfy.sh/your-topic`) and apply.
3. Back up `/mnt/user/appdata/hearth` (DB + uploads).

The image is built to `ghcr.io/anicholas65/hearth` by GitHub Actions on push to `main`.

## Env vars
| Var | Default | |
|---|---|---|
| `DATA_DIR` | `/data` | DB + uploads |
| `APPRISE_URLS` | – | comma-separated Apprise URLs |
| `NOTIFY_LEAD_DAYS` | `3` | days ahead to remind |
| `NOTIFY_CHECK_HOURS` | `1` | check frequency |
| `PORT` | `8080` | |

## Dev
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest -q
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

No auth built in – keep it on your LAN or behind a reverse proxy with auth.
