import asyncio
import logging
import os
from datetime import date, timedelta

import apprise

from .db import get_db

log = logging.getLogger("hearth.notify")


def apprise_urls() -> list[str]:
    raw = os.environ.get("APPRISE_URLS", "")
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


def send(title: str, body: str) -> bool:
    urls = apprise_urls()
    if not urls:
        return False
    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    return bool(ap.notify(title=title, body=body))


def due_digest(today: date | None = None) -> tuple[list[int], str]:
    """Tasks due within NOTIFY_LEAD_DAYS that haven't been notified today."""
    today = today or date.today()
    lead = int(os.environ.get("NOTIFY_LEAD_DAYS", "3"))
    horizon = (today + timedelta(days=lead)).isoformat()
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, next_due FROM tasks WHERE next_due <= ? "
            "AND (last_notified IS NULL OR last_notified < ?) ORDER BY next_due",
            (horizon, today.isoformat()),
        ).fetchall()
    lines = []
    for r in rows:
        tag = "OVERDUE" if r["next_due"] < today.isoformat() else "due"
        lines.append(f"- {r['name']} ({tag} {r['next_due']})")
    return [r["id"] for r in rows], "\n".join(lines)


def run_check(today: date | None = None) -> int:
    today = today or date.today()
    ids, body = due_digest(today)
    if not ids or not send(f"Hearth: {len(ids)} home task(s) due", body):
        return 0
    with get_db() as db:
        db.executemany(
            "UPDATE tasks SET last_notified = ? WHERE id = ?",
            [(today.isoformat(), i) for i in ids],
        )
    return len(ids)


async def notify_loop() -> None:
    interval = float(os.environ.get("NOTIFY_CHECK_HOURS", "1")) * 3600
    while True:
        try:
            n = await asyncio.to_thread(run_check)
            if n:
                log.info("sent reminder for %d task(s)", n)
        except Exception:
            log.exception("notification check failed")
        await asyncio.sleep(interval)
