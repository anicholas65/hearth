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


def due_digest(today: date | None = None) -> tuple[list[int], list[int], str]:
    """Tasks due within NOTIFY_LEAD_DAYS and appointments today/tomorrow not yet notified today."""
    today = today or date.today()
    lead = int(os.environ.get("NOTIFY_LEAD_DAYS", "3"))
    horizon = (today + timedelta(days=lead)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, next_due FROM tasks WHERE next_due <= ? "
            "AND (last_notified IS NULL OR last_notified < ?) ORDER BY next_due",
            (horizon, today.isoformat()),
        ).fetchall()
        appts = db.execute(
            "SELECT id, kind, title, starts_at, all_day FROM appointments "
            "WHERE substr(starts_at,1,10) BETWEEN ? AND ? "
            "AND (last_notified IS NULL OR last_notified < ?) ORDER BY starts_at",
            (today.isoformat(), tomorrow, today.isoformat()),
        ).fetchall()
    lines = []
    for a in appts:
        when = "today" if a["starts_at"][:10] == today.isoformat() else "tomorrow"
        at = "" if a["all_day"] else f" at {a['starts_at'][11:16]}"
        icon = "🔔" if a["kind"] == "reminder" else "📅"
        lines.append(f"{icon} {a['title']} ({when}{at})")
    for r in rows:
        tag = "OVERDUE" if r["next_due"] < today.isoformat() else "due"
        lines.append(f"- {r['name']} ({tag} {r['next_due']})")
    return [r["id"] for r in rows], [a["id"] for a in appts], "\n".join(lines)


def run_check(today: date | None = None) -> int:
    today = today or date.today()
    tids, aids, body = due_digest(today)
    n = len(tids) + len(aids)
    if not n or not send(f"Hearth: {n} upcoming item(s)", body):
        return 0
    with get_db() as db:
        db.executemany("UPDATE tasks SET last_notified = ? WHERE id = ?",
                       [(today.isoformat(), i) for i in tids])
        db.executemany("UPDATE appointments SET last_notified = ? WHERE id = ?",
                       [(today.isoformat(), i) for i in aids])
    return n


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
