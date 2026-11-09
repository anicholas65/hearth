"""Command palette: turn short phrases into actions.

  add milk to list / add 2 gal milk to groceries / buy eggs / add caulk to supplies
  log filter replaced 12/1 / done gutters yesterday / did smoke test on dec 3
  new task clean dryer vent every 6 months
  go budget / open forecast / settings
Anything else falls back to search.
"""
import re
from datetime import date, timedelta

from . import plan, shopping
from .db import get_db

PAGES = {"today": "/", "home": "/", "tasks": "/tasks", "plan": "/tasks", "schedule": "/schedule",
         "calendar": "/schedule", "shopping": "/shopping", "list": "/shopping", "pantry": "/pantry",
         "inventory": "/items", "stuff": "/items", "items": "/items", "documents": "/documents", "docs": "/documents",
         "photos": "/gallery", "contractors": "/contractors", "vehicles": "/vehicles", "hobbies": "/projects",
         "projects": "/projects", "budget": "/budget", "money": "/budget", "forecast": "/budget?view=forecast",
         "settings": "/settings", "capture": "/capture"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}
STOP = {"the", "a", "an", "my", "log", "logged", "done", "did", "finished", "replaced", "cleaned", "changed",
        "checked", "tested", "on", "at", "today", "yesterday", "task", "mark", "complete", "completed"}


def parse_date(text: str, today: date | None = None) -> tuple[date | None, str]:
    """Find a date in free text; returns (date, text without it). Future dates roll back a year."""
    today = today or date.today()
    t = text
    if re.search(r"\byesterday\b", t, re.I):
        return today - timedelta(days=1), re.sub(r"\byesterday\b", "", t, flags=re.I)
    if re.search(r"\btoday\b", t, re.I):
        return today, re.sub(r"\btoday\b", "", t, flags=re.I)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        mo, dy, yr = int(m[1]), int(m[2]), m[3]
        y = (int(yr) + 2000 if yr and len(yr) == 2 else int(yr)) if yr else today.year
        try:
            d = date(y, mo, dy)
            if not yr and d > today:
                d = date(y - 1, mo, dy)
            return d, t[:m.start()] + t[m.end():]
        except ValueError:
            pass
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", t, re.I)
    if m:
        try:
            d = date(today.year, MONTHS[m[1].lower()[:3]], int(m[2]))
            if d > today:
                d = date(today.year - 1, d.month, d.day)
            return d, t[:m.start()] + t[m.end():]
        except ValueError:
            pass
    return None, t


def match_task(words: str):
    toks = {w for w in re.findall(r"[a-z0-9]+", words.lower()) if w not in STOP and len(w) > 1}
    toks |= {w[:-1] for w in toks if w.endswith("s")}
    if not toks:
        return None
    best, score = None, 0
    with get_db() as db:
        for t in db.execute("SELECT id, name, interval_days FROM tasks WHERE completed_at IS NULL"):
            name = set(re.findall(r"[a-z0-9]+", t["name"].lower()))
            name |= {w[:-1] for w in name if w.endswith("s")}
            s = len(toks & name)
            if s > score:
                best, score = dict(t), s
    return best


def interpret(text: str, today: date | None = None) -> dict:
    """Return an action dict: {kind, label, ...}. Pure: does not change data."""
    q = " ".join(text.strip().split())
    low = q.lower()
    if not q:
        return {"kind": "none", "label": ""}
    m = re.match(r"^(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:the\s+)?(?:my\s+)?(list|shopping(?: list)?|groceries|grocery list|supplies|supply list)$", q, re.I) \
        or re.match(r"^(?:buy|need|get)\s+(.+?)()$", q, re.I)
    if m:
        lst = "supplies" if m.group(2) and m.group(2).lower().startswith("suppl") else "groceries"
        items = [shopping.parse_quick(p)[0] for p in re.split(r",|\band\b", m.group(1)) if p.strip()]
        return {"kind": "shop", "list": lst, "text": m.group(1),
                "label": f"Add {', '.join(items)} to {shopping.LISTS[lst]}"}
    m = re.match(r"^(?:new|add|create)\s+task\s+(.+?)(?:\s+every\s+(\d+)?\s*(day|week|month|year)s?)?$", q, re.I)
    if m:
        n = int(m[2] or 1)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[m[3].lower()] * n if m[3] else 0
        return {"kind": "task", "name": m[1].strip(), "interval": days,
                "label": f"New task “{m[1].strip()}” · {plan.repeat_label(days)}"}
    if re.match(r"^(log|done|did|finished|mark|completed?)\b", low):
        d, rest = parse_date(q, today)
        t = match_task(rest)
        if t:
            when = d or (today or date.today())
            return {"kind": "done", "task_id": t["id"], "date": when.isoformat(),
                    "label": f"Mark “{t['name']}” done · {when.strftime('%b')} {when.day}"}
        return {"kind": "search", "q": rest.strip(), "label": f"No task matches “{rest.strip()}”: search instead"}
    m = re.match(r"^(?:go(?: to)?|open|show)\s+(.+)$", low)
    page = (m[1] if m else low).strip()
    if page in PAGES:
        return {"kind": "go", "href": PAGES[page], "label": f"Go to {page.title()}"}
    return {"kind": "search", "q": q, "label": f"Search for “{q}”"}


def run(text: str) -> dict:
    """Perform the interpreted action. Returns {ok, message, href}."""
    a = interpret(text)
    if a["kind"] == "shop":
        from .main import shopping_add_text
        n = shopping_add_text(a["list"], a["text"])
        return {"ok": True, "message": a["label"].replace("Add", "Added", 1) if n else "Already on the list",
                "href": f"/shopping?list={a['list']}"}
    if a["kind"] == "task":
        from .main import create_task_simple
        tid = create_task_simple(a["name"], a["interval"])
        return {"ok": True, "message": f"Added task “{a['name']}”", "href": f"/tasks/{tid}"}
    if a["kind"] == "done":
        from .main import complete_task
        complete_task(a["task_id"], a["date"])
        return {"ok": True, "message": a["label"].replace("Mark", "Marked", 1), "href": f"/tasks/{a['task_id']}"}
    if a["kind"] == "go":
        return {"ok": True, "message": "", "href": a["href"]}
    from urllib.parse import quote_plus
    return {"ok": True, "message": "", "href": f"/search?q={quote_plus(a.get('q', text))}"}
