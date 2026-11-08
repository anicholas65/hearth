"""Dashboard data: what needs attention, a unified 'Up next' timeline, setup checklist, insights."""
from datetime import date, timedelta

from . import gcal, notify, plan
from .db import get_db, get_setting


def _when_bucket(d: date, today: date) -> str:
    if d < today:
        return "Overdue"
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    if d <= today + timedelta(days=6):
        return "Next 7 days"
    return "Later"


def timeline(today: date | None = None, days: int = 14) -> list[dict]:
    """Merge overdue/due tasks, appointments and expiring warranties into one sorted list."""
    today = today or date.today()
    horizon = (today + timedelta(days=days)).isoformat()
    warn = (today + timedelta(days=45)).isoformat()
    out: list[dict] = []
    with get_db() as db:
        for t in db.execute(
                "SELECT t.*, i.name AS item_name FROM tasks t LEFT JOIN items i ON i.id=t.item_id "
                "WHERE next_due <= ? AND t.completed_at IS NULL ORDER BY next_due", (horizon,)):
            d = date.fromisoformat(t["next_due"])
            out.append(dict(kind="task", id=t["id"], title=t["name"], date=d, time=None,
                            sub=(t["item_name"] + " · " if t["item_name"] else "")
                            + plan.repeat_label(t["interval_days"]),
                            href=f"/tasks/{t['id']}", overdue=d < today))
        for a in db.execute(
                "SELECT a.*, c.name AS contractor_name FROM appointments a "
                "LEFT JOIN contractors c ON c.id=a.contractor_id "
                "WHERE substr(starts_at,1,10) BETWEEN ? AND ? ORDER BY starts_at",
                (today.isoformat(), horizon)):
            out.append(dict(kind=a["kind"], id=a["id"], title=a["title"],
                            date=date.fromisoformat(a["starts_at"][:10]),
                            time=None if a["all_day"] else a["starts_at"],
                            sub=" · ".join(x for x in (a["contractor_name"], a["location"]) if x),
                            href=f"/schedule/{a['id']}", overdue=False))
        for i in db.execute(
                "SELECT id, name, warranty_until FROM items WHERE warranty_until BETWEEN ? AND ?",
                (today.isoformat(), warn)):
            d = date.fromisoformat(i["warranty_until"])
            out.append(dict(kind="warranty", id=i["id"], title=f"{i['name']} warranty ends",
                            date=d, time=None, sub=f"{(d - today).days} days left · check for issues before it lapses",
                            href=f"/items/{i['id']}", overdue=False))
    out.sort(key=lambda e: (e["date"], e["time"] or "", e["kind"] != "task"))
    for e in out:
        e["bucket"] = _when_bucket(e["date"], today)
    return out


def attention(events: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    week = today + timedelta(days=7)
    return dict(
        overdue=sum(1 for e in events if e["kind"] == "task" and e["overdue"]),
        due_week=sum(1 for e in events if e["kind"] == "task" and not e["overdue"] and e["date"] <= week),
        today_events=sum(1 for e in events if e["kind"] in ("appointment", "reminder") and e["date"] == today),
        warranties=sum(1 for e in events if e["kind"] == "warranty"),
    )


def setup_steps() -> list[dict]:
    year = date.today().year
    with get_db() as db:
        n_items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        n_tasks = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        n_life = db.execute("SELECT COUNT(*) FROM items WHERE purchase_date IS NOT NULL "
                            "AND life_years IS NOT NULL").fetchone()[0]
        budget = db.execute("SELECT 1 FROM budgets WHERE year=?", (year,)).fetchone()
    return [
        dict(done=n_tasks > 0, title="Set up home maintenance", href="/tasks#new-task",
             hint="Start from 12 common tasks: filters, detectors, gutters…"),
        dict(done=n_items > 0, title="Add the things you own", href="/items#new-item",
             hint="Appliances and systems; snap a photo of each."),
        dict(done=n_life > 0, title="Forecast replacements", href="/items",
             hint="Add a purchase date and expected life to an item."),
        dict(done=bool(notify.apprise_urls()), title="Turn on reminders", href="/settings#notifications",
             hint="Get due tasks on your phone via ntfy, Discord or email."),
        dict(done=gcal.configured(), title="Connect Google Calendar", href="/settings#google",
             hint="Tasks and appointments show up in your calendar."),
        dict(done=bool(budget), title=f"Set a {year} budget", href="/budget#budget",
             hint="Track what you set aside and spend on the house."),
    ]


def show_setup(steps: list[dict]) -> bool:
    return get_setting("setup_dismissed") != "1" and not all(s["done"] for s in steps)


def insights(today: date | None = None) -> dict | None:
    """Only facts we can back up: counts and sums, never a % from too little data."""
    today = today or date.today()
    since = (today - timedelta(days=90)).isoformat()
    year = str(today.year)
    with get_db() as db:
        n_tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE completed_at IS NULL").fetchone()[0]
        overdue = db.execute("SELECT COUNT(*) FROM tasks WHERE next_due < ? AND completed_at IS NULL",
                             (today.isoformat(),)).fetchone()[0]
        done90 = db.execute("SELECT COUNT(*) FROM task_logs WHERE done_on >= ?", (since,)).fetchone()[0]
        any_logs = db.execute("SELECT COUNT(*) FROM task_logs").fetchone()[0]
        spend = db.execute(
            "SELECT COALESCE(SUM(cost),0) FROM task_logs WHERE substr(done_on,1,4)=?", (year,)).fetchone()[0]
        spend += db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM fund_entries WHERE kind='expense' "
            "AND substr(entry_date,1,4)=?", (year,)).fetchone()[0]
        any_money = db.execute("SELECT COUNT(*) FROM fund_entries").fetchone()[0] + any_logs
        warr = db.execute("SELECT COUNT(*) FROM items WHERE warranty_until BETWEEN ? AND ?",
                          (today.isoformat(), (today + timedelta(days=90)).isoformat())).fetchone()[0]
        any_warr = db.execute("SELECT COUNT(*) FROM items WHERE warranty_until IS NOT NULL").fetchone()[0]
        items = db.execute("SELECT purchase_date, life_years, replace_cost FROM items "
                           "WHERE purchase_date IS NOT NULL AND life_years IS NOT NULL").fetchall()
    repl = sum((i["replace_cost"] or 0) for i in items
               if int(i["purchase_date"][:4]) + int(i["life_years"]) <= today.year + 2)
    if not (n_tasks or any_logs or any_money or any_warr or items):
        return None
    # value None = no data to say anything; 0 = genuinely zero
    return dict(year=today.year,
                schedule=None if not n_tasks else ("No overdue tasks" if not overdue else
                                                   f"{overdue} overdue of {n_tasks}"),
                schedule_ok=n_tasks > 0 and not overdue,
                done90=done90 if any_logs else None,
                spend=spend if any_money else None,
                warranties=warr if any_warr else None,
                repl=repl if items else None)
