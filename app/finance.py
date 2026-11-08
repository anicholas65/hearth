"""Budget, ledger and replacement-forecast calculations (one place, used everywhere)."""
from datetime import date

from .db import get_db, get_setting
from .plan import DEFAULT_EXPENSE_CATEGORIES

MAINTENANCE = "Maintenance"
UNCATEGORIZED = "Uncategorized"


def expense_categories() -> list[str]:
    raw = get_setting("expense_categories")
    cats = [c.strip() for c in (raw or "").splitlines() if c.strip()]
    return cats or list(DEFAULT_EXPENSE_CATEGORIES)


def nice_ceiling(v: float) -> float:
    if v <= 0:
        return 100.0
    mag = 10 ** (len(str(int(v))) - 1)
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * mag >= v:
            return step * mag
    return 10 * mag


# Maintenance costs live on task_logs and are read straight into the ledger, never copied into
# fund_entries, so a completed task can't create a duplicate expense.
LEDGER_SQL = """
SELECT 'f' || f.id AS key, f.id, f.entry_date AS d, f.kind, f.amount, f.note, f.item_id, NULL AS task_id,
       f.category, f.document_id, i.name AS item_name
  FROM fund_entries f LEFT JOIN items i ON i.id = f.item_id
UNION ALL
SELECT 't' || l.id, l.id, l.done_on, 'expense', l.cost, 'Maintenance: ' || t.name, t.item_id, t.id,
       'Maintenance', NULL, i.name
  FROM task_logs l JOIN tasks t ON t.id = l.task_id LEFT JOIN items i ON i.id = t.item_id
 WHERE l.cost IS NOT NULL AND l.cost > 0
"""


def _sign(r) -> float:
    return r["amount"] if r["kind"] == "allocation" else -r["amount"]


def fund_balance() -> float:
    with get_db() as db:
        return sum(_sign(r) for r in db.execute(f"SELECT kind, amount FROM ({LEDGER_SQL})"))


def balance_data(year: int, q: str = "", kind: str = "", category: str = "", sort: str = "new") -> dict:
    y = str(year)
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM ({LEDGER_SQL}) ORDER BY d, key").fetchall()
        budget = db.execute("SELECT amount FROM budgets WHERE year=?", (year,)).fetchone()
        years = {r[0] for r in db.execute("SELECT year FROM budgets")}
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    carry = sum(_sign(r) for r in rows if r["d"][:4] < y)
    ledger, run = [], carry
    months = [{"m": m, "alloc": 0.0, "spent": 0.0} for m in range(1, 13)]
    by_cat: dict[str, float] = {}
    per_year: dict[str, dict] = {}
    for r in rows:
        yy = r["d"][:4]
        years.add(int(yy))
        py = per_year.setdefault(yy, {"alloc": 0.0, "spent": 0.0})
        py["alloc" if r["kind"] == "allocation" else "spent"] += r["amount"]
        if yy != y:
            continue
        run += _sign(r)
        ledger.append({**dict(r), "balance": run})
        months[int(r["d"][5:7]) - 1]["alloc" if r["kind"] == "allocation" else "spent"] += r["amount"]
        if r["kind"] == "expense":
            c = r["category"] or UNCATEGORIZED
            by_cat[c] = by_cat.get(c, 0) + r["amount"]
    allocated = sum(m["alloc"] for m in months)
    spent = sum(m["spent"] for m in months)
    fund_total = sum(_sign(r) for r in rows)
    budget_amt = budget["amount"] if budget else None
    peak = max([m["alloc"] for m in months] + [m["spent"] for m in months] + [0])
    axis_max = nice_ceiling(peak)
    years.add(date.today().year)
    # ledger filtering/sorting (display only; totals above always use everything)
    view = ledger
    if kind in ("allocation", "expense"):
        view = [e for e in view if e["kind"] == kind]
    if category:
        view = [e for e in view if (e["category"] or UNCATEGORIZED) == category]
    if q.strip():
        ql = q.strip().lower()
        view = [e for e in view if ql in (e["note"] or "").lower() or ql in (e["item_name"] or "").lower()]
    view = {"old": view, "amount": sorted(view, key=lambda e: -e["amount"])}.get(sort, list(reversed(view)))
    months_elapsed = 12 if year < date.today().year else max(1, date.today().month)
    return dict(
        year=year, years=sorted(years, reverse=True), budget=budget_amt,
        allocated=allocated, spent=spent, carry=carry, year_end=run, fund_total=fund_total,
        remaining=(budget_amt - spent) if budget_amt is not None else None,
        unallocated=(budget_amt - allocated) if budget_amt is not None else None,
        months=months, axis_max=axis_max, ticks=[axis_max * k / 4 for k in range(5)],
        has_activity=bool(ledger), ledger=view, ledger_total=len(ledger), items=items,
        by_cat=sorted(by_cat.items(), key=lambda kv: -kv[1]),
        per_year=sorted(per_year.items(), reverse=True)[:6],
        avg_monthly_alloc=allocated / months_elapsed if year <= date.today().year else 0,
        filt=dict(q=q, kind=kind, category=category, sort=sort))


def replacement_year(item) -> int | None:
    if not item["purchase_date"] or not item["life_years"]:
        return None
    return int(item["purchase_date"][:4]) + int(item["life_years"])


def forecast_data(period: int = 5) -> dict:
    """Estimated replacements = purchase year + expected life. Past-due items count in this year."""
    period = period if period in (1, 3, 5, 10) else 5
    this_year = date.today().year
    last = this_year + period - 1
    with get_db() as db:
        items = db.execute(
            "SELECT i.*, r.name AS room_name FROM items i LEFT JOIN rooms r ON r.id=i.room_id ORDER BY i.name"
        ).fetchall()
    years: dict[int, list] = {}
    missing, beyond = [], 0
    for i in items:
        y = replacement_year(i)
        if y is None:
            missing.append(i)
        elif max(y, this_year) <= last:
            years.setdefault(max(y, this_year), []).append((y, i))
        else:
            beyond += 1
    timeline = [(yr, years.get(yr, []), sum((i["replace_cost"] or 0) for _, i in years.get(yr, [])))
                for yr in range(this_year, last + 1)]
    total = sum(t for _, _, t in timeline)
    no_cost = sum(1 for _, rows, _ in timeline for _, i in rows if not i["replace_cost"])
    balance = fund_balance()
    shortfall = max(0.0, total - max(balance, 0))
    soon = [(y, i) for yr, rows, _ in timeline if yr <= this_year + 1 for y, i in rows]
    peak = max([t for _, _, t in timeline] + [0])
    axis_max = nice_ceiling(peak)
    return dict(
        period=period, this_year=this_year, last=last, timeline=timeline, total=total,
        monthly=shortfall / (period * 12), balance=balance, shortfall=shortfall,
        soon=soon, missing=missing, beyond=beyond, n_items=len(items), no_cost=no_cost,
        covered=len(items) - len(missing), axis_max=axis_max, ticks=[axis_max * k / 4 for k in range(5)])
