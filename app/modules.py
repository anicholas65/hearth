"""Vehicles and Projects & Hobbies (scaffold modules), as an APIRouter using the shared page helpers."""
import json
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Request

from .db import get_db

router = APIRouter()


def _ui():
    from . import main  # late import: main owns render/back/toast helpers
    return main


# ---------------- vehicles ----------------

def _log(v) -> list[dict]:
    try:
        return json.loads(v["odometer_log"] or "[]")
    except ValueError:
        return []


def vehicle_summary(db, v) -> dict:
    tasks = db.execute("SELECT * FROM tasks WHERE vehicle_id=? AND completed_at IS NULL ORDER BY next_due", (v["id"],)).fetchall()
    services = db.execute(
        "SELECT l.done_on AS d, t.name AS title, l.cost, l.odometer, c.name AS contractor, t.id AS task_id "
        "FROM task_logs l JOIN tasks t ON t.id=l.task_id LEFT JOIN contractors c ON c.id=l.contractor_id "
        "WHERE t.vehicle_id=? ORDER BY l.done_on DESC", (v["id"],)).fetchall()
    expenses = db.execute("SELECT entry_date AS d, note AS title, amount AS cost, category FROM fund_entries "
                          "WHERE vehicle_id=? AND kind='expense' ORDER BY entry_date DESC", (v["id"],)).fetchall()
    log = _log(v)
    readings = sorted([(r["date"], int(r["miles"])) for r in log if r.get("miles") is not None] +
                      [(s["d"], s["odometer"]) for s in services if s["odometer"]])
    start = v["purchase_odometer"] if v["purchase_odometer"] is not None else (readings[0][1] if readings else None)
    odo = v["odometer"] or (readings[-1][1] if readings else None)
    spent = sum(s["cost"] or 0 for s in services) + sum(e["cost"] or 0 for e in expenses)
    miles = (odo - start) if odo is not None and start is not None and odo > start else None
    due = []
    today = date.today().isoformat()
    for t in tasks:
        by_miles = (t["next_due_miles"] - odo) if t["next_due_miles"] and odo is not None else None
        days = (date.fromisoformat(t["next_due"]) - date.today()).days
        due.append({**dict(t), "miles_left": by_miles, "days_left": days,
                    "due_now": t["next_due"] <= today or (by_miles is not None and by_miles <= 0)})
    return dict(odometer=odo, miles=miles, spent=spent, cpm=(spent / miles) if miles else None,
                due=due, services=services, expenses=expenses, log=sorted(log, key=lambda r: r["date"], reverse=True))


@router.get("/vehicles")
def vehicles_page(request: Request):
    m = _ui()
    with get_db() as db:
        vs = db.execute("SELECT * FROM vehicles ORDER BY name").fetchall()
        cards = [(v, vehicle_summary(db, v)) for v in vs]
    return m.render(request, "vehicles.html", cards=cards)


@router.post("/vehicles")
async def vehicle_save(request: Request):
    m = _ui()
    f = await request.form()
    vid = m.num(f.get("id"), int)
    vals = (m.blank(f.get("name")), m.blank(f.get("make")), m.blank(f.get("model")), m.num(f.get("year"), int),
            m.blank(f.get("vin")), m.blank(f.get("plate")), m.num(f.get("odometer"), int),
            m.blank(f.get("purchase_date")), m.num(f.get("purchase_odometer"), int), m.num(f.get("price")),
            m.blank(f.get("notes")))
    if not vals[0]:
        raise HTTPException(400, "Give the vehicle a name.")
    with get_db() as db:
        if vid:
            db.execute("UPDATE vehicles SET name=?, make=?, model=?, year=?, vin=?, plate=?, odometer=?, purchase_date=?, "
                       "purchase_odometer=?, price=?, notes=? WHERE id=?", (*vals, vid))
        else:
            vid = db.execute("INSERT INTO vehicles (name, make, model, year, vin, plate, odometer, purchase_date, "
                             "purchase_odometer, price, notes, odometer_log) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (*vals, json.dumps([{"date": date.today().isoformat(), "miles": vals[6]}] if vals[6] else []))).lastrowid
    return m.back(m.toast_url(f"/vehicles/{vid}", "Vehicle saved"))


@router.get("/vehicles/{vid}")
def vehicle_detail(request: Request, vid: int):
    m = _ui()
    with get_db() as db:
        v = db.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
        if not v:
            raise HTTPException(404)
        summ = vehicle_summary(db, v)
        contractors = db.execute("SELECT id, name FROM contractors ORDER BY name").fetchall()
        photos = db.execute("SELECT id FROM photos WHERE vehicle_id=? ORDER BY is_cover DESC, id", (vid,)).fetchall()
    return m.render(request, "vehicle.html", v=v, s=summ, contractors=contractors, photos=photos)


@router.post("/vehicles/{vid}/odometer")
def vehicle_odometer(vid: int, miles: int = Form(...), on: str = Form(None)):
    m = _ui()
    d = m.blank(on) or date.today().isoformat()
    with get_db() as db:
        v = db.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
        if not v:
            raise HTTPException(404)
        log = _log(v) + [{"date": d, "miles": miles}]
        db.execute("UPDATE vehicles SET odometer=MAX(COALESCE(odometer,0), ?), odometer_log=? WHERE id=?",
                   (miles, json.dumps(sorted(log, key=lambda r: r["date"])), vid))
        # mileage-based maintenance that just became due shows up today
        n = db.execute("UPDATE tasks SET next_due=? WHERE vehicle_id=? AND next_due_miles IS NOT NULL "
                       "AND next_due_miles<=? AND next_due>? AND completed_at IS NULL",
                       (date.today().isoformat(), vid, miles, date.today().isoformat())).rowcount
    msg = f"Odometer {miles:,} mi" + (f" · {n} service{'s' if n != 1 else ''} now due" if n else "")
    return m.back(m.toast_url(f"/vehicles/{vid}", msg))


@router.post("/vehicles/{vid}/maintenance")
def vehicle_maintenance(vid: int, name: str = Form(...), every_miles: str = Form(None), every_months: str = Form(None)):
    m = _ui()
    miles, months = m.num(every_miles, int), m.num(every_months, int)
    if not (miles or months):
        raise HTTPException(400, "Set a mileage or time interval.")
    with get_db() as db:
        v = db.execute("SELECT odometer FROM vehicles WHERE id=?", (vid,)).fetchone()
        days = months * 30 if months else 3650  # mileage-only: date far out, miles drive it
        due = date.fromordinal(date.today().toordinal() + days).isoformat()
        db.execute("INSERT INTO tasks (name, interval_days, next_due, vehicle_id, interval_miles, next_due_miles, kind) "
                   "VALUES (?,?,?,?,?,?, 'chore')", (name.strip(), days, due, vid, miles,
                                                     (v["odometer"] or 0) + miles if miles else None))
    return m.back(m.toast_url(f"/vehicles/{vid}", f"Added “{name.strip()}”"))


@router.post("/vehicles/{vid}/delete")
def vehicle_delete(vid: int):
    m = _ui()
    with get_db() as db:
        db.execute("DELETE FROM vehicles WHERE id=?", (vid,))
    return m.back(m.toast_url("/vehicles", "Vehicle removed"))


# ---------------- projects & hobbies ----------------

def _materials(p) -> list[dict]:
    try:
        return json.loads(p["materials"] or "[]")
    except ValueError:
        return []


def project_summary(db, p) -> dict:
    spent = db.execute("SELECT COALESCE(SUM(amount),0) FROM fund_entries WHERE project_id=? AND kind='expense'",
                       (p["id"],)).fetchone()[0]
    mats = _materials(p)
    planned = sum((x.get("cost") or 0) * (x.get("qty") or 1) for x in mats)
    return dict(spent=spent, planned=planned, materials=mats,
                pct=(spent / p["budget"] * 100) if p["budget"] else None)


@router.get("/projects")
def projects_page(request: Request, kind: str = ""):
    m = _ui()
    with get_db() as db:
        ps = db.execute("SELECT * FROM projects" + (" WHERE kind=?" if kind else "") +
                        " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, name",
                        (kind,) if kind else ()).fetchall()
        cards = [(p, project_summary(db, p),
                  db.execute("SELECT id FROM photos WHERE project_id=? ORDER BY is_cover DESC, id LIMIT 1", (p["id"],)).fetchone())
                 for p in ps]
    return m.render(request, "projects.html", cards=cards, kind=kind)


@router.post("/projects")
async def project_save(request: Request):
    m = _ui()
    f = await request.form()
    pid = m.num(f.get("id"), int)
    kind = f.get("kind") if f.get("kind") in ("project", "hobby") else "project"
    status = f.get("status") if f.get("status") in ("planned", "active", "paused", "done") else "planned"
    vals = (m.blank(f.get("name")), kind, status, m.num(f.get("budget")), m.blank(f.get("notes")),
            m.blank(f.get("started_on")), m.blank(f.get("finished_on")))
    if not vals[0]:
        raise HTTPException(400, "Give it a name.")
    with get_db() as db:
        if pid:
            db.execute("UPDATE projects SET name=?, kind=?, status=?, budget=?, notes=?, started_on=?, finished_on=? "
                       "WHERE id=?", (*vals, pid))
        else:
            pid = db.execute("INSERT INTO projects (name, kind, status, budget, notes, started_on, finished_on, materials) "
                             "VALUES (?,?,?,?,?,?,?, '[]')", vals).lastrowid
    return m.back(m.toast_url(f"/projects/{pid}", "Saved"))


@router.get("/projects/{pid}")
def project_detail(request: Request, pid: int):
    m = _ui()
    with get_db() as db:
        p = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        s = project_summary(db, p)
        expenses = db.execute("SELECT * FROM fund_entries WHERE project_id=? ORDER BY entry_date DESC", (pid,)).fetchall()
        photos = db.execute("SELECT id FROM photos WHERE project_id=? ORDER BY is_cover DESC, id", (pid,)).fetchall()
        stock = db.execute("SELECT * FROM pantry_items WHERE project_id=? ORDER BY name", (pid,)).fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        names = {r["id"]: r["name"] for r in items}
    for mt in s["materials"]:
        mt["item_name"] = names.get(mt.get("item_id"))
    return m.render(request, "project.html", p=p, s=s, expenses=expenses, photos=photos, stock=stock, items=items)


@router.post("/projects/{pid}/materials")
def project_material(pid: int, name: str = Form(...), qty: str = Form(None), cost: str = Form(None),
                     item_id: str = Form(None), remove: str = Form(None)):
    m = _ui()
    with get_db() as db:
        p = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        mats = _materials(p)
        if remove is not None:
            idx = int(remove)
            mats = [x for n, x in enumerate(mats) if n != idx]
        else:
            mats.append({"name": name.strip(), "qty": m.num(qty) or 1, "cost": m.num(cost), "item_id": m.num(item_id, int)})
        db.execute("UPDATE projects SET materials=? WHERE id=?", (json.dumps(mats), pid))
    return m.back(m.toast_url(f"/projects/{pid}#materials", "Materials updated"))


@router.post("/projects/{pid}/stock")
def project_stock(pid: int, name: str = Form(...), qty: str = Form(None), unit: str = Form(None), min_qty: str = Form(None)):
    """Consumables (filament, film…) live in the pantry table, tied to the hobby."""
    m = _ui()
    from .shopping import pantry_restock_check
    with get_db() as db:
        sid = db.execute("INSERT INTO pantry_items (name, qty, unit, min_qty, category, project_id) VALUES (?,?,?,?,?,?)",
                         (name.strip(), m.num(qty), m.blank(unit), m.num(min_qty), "Consumables", pid)).lastrowid
        pantry_restock_check(db, sid)
    return m.back(m.toast_url(f"/projects/{pid}#stock", f"Tracking {name.strip()}"))


@router.post("/projects/{pid}/delete")
def project_delete(pid: int):
    m = _ui()
    with get_db() as db:
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
    return m.back(m.toast_url("/projects", "Removed"))
