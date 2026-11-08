import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import notify
from .db import data_dir, get_db, init_db

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

DEFAULT_TASKS = [
    ("Replace HVAC air filter", 90),
    ("Test smoke & CO detectors", 30),
    ("Replace smoke detector batteries", 365),
    ("Clean gutters", 180),
    ("Flush water heater", 365),
    ("Clean dryer vent", 365),
    ("Clean refrigerator coils", 180),
    ("Service HVAC system", 365),
    ("Clean range hood filter", 90),
    ("Inspect roof & flashing", 365),
    ("Test GFCI outlets", 90),
    ("Check fire extinguisher pressure", 180),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = None
    if os.environ.get("NOTIFY_DISABLED") != "1":
        task = asyncio.create_task(notify.notify_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Hearth", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def blank(v: str | None):
    return v.strip() if v and v.strip() else None


def num(v: str | None, cast=float):
    v = blank(v)
    return cast(v) if v is not None else None


def render(request: Request, name: str, **ctx):
    ctx["today"] = date.today().isoformat()
    return templates.TemplateResponse(request, name, ctx)


def replacement_year(item) -> int | None:
    if not item["purchase_date"] or not item["life_years"]:
        return None
    return int(item["purchase_date"][:4]) + int(item["life_years"])


# ---------- dashboard ----------

@app.get("/")
def dashboard(request: Request):
    today = date.today()
    soon = (today + timedelta(days=14)).isoformat()
    warn = (today + timedelta(days=60)).isoformat()
    with get_db() as db:
        due = db.execute(
            "SELECT t.*, i.name AS item_name FROM tasks t LEFT JOIN items i ON i.id=t.item_id "
            "WHERE next_due <= ? ORDER BY next_due", (soon,)).fetchall()
        warranties = db.execute(
            "SELECT * FROM items WHERE warranty_until BETWEEN ? AND ? ORDER BY warranty_until",
            (today.isoformat(), warn)).fetchall()
        items = db.execute("SELECT * FROM items").fetchall()
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("items", "tasks", "documents", "contractors")}
        recent = db.execute(
            "SELECT l.*, t.name AS task_name FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "ORDER BY done_on DESC, l.id DESC LIMIT 5").fetchall()
    horizon = today.year + 2
    upcoming = sorted(
        [(replacement_year(i), i) for i in items
         if replacement_year(i) and replacement_year(i) <= horizon],
        key=lambda x: x[0])
    return render(request, "dashboard.html", due=due, warranties=warranties,
                  upcoming=upcoming, counts=counts, recent=recent)


# ---------- tasks ----------

@app.get("/tasks")
def tasks_page(request: Request):
    with get_db() as db:
        tasks = db.execute(
            "SELECT t.*, i.name AS item_name FROM tasks t LEFT JOIN items i ON i.id=t.item_id "
            "ORDER BY next_due").fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        contractors = db.execute("SELECT id, name FROM contractors ORDER BY name").fetchall()
    return render(request, "tasks.html", tasks=tasks, items=items, contractors=contractors)


@app.post("/tasks")
def task_create(name: str = Form(...), interval_days: int = Form(...),
                next_due: str = Form(None), item_id: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        db.execute(
            "INSERT INTO tasks (name, interval_days, next_due, item_id, notes) VALUES (?,?,?,?,?)",
            (name.strip(), interval_days, blank(next_due) or date.today().isoformat(),
             num(item_id, int), blank(notes)))
    return back("/tasks")


@app.post("/tasks/seed")
def task_seed():
    today = date.today()
    with get_db() as db:
        existing = {r[0] for r in db.execute("SELECT name FROM tasks")}
        db.executemany(
            "INSERT INTO tasks (name, interval_days, next_due) VALUES (?,?,?)",
            [(n, d, (today + timedelta(days=min(d, 30))).isoformat())
             for n, d in DEFAULT_TASKS if n not in existing])
    return back("/tasks")


@app.post("/tasks/{task_id}/done")
def task_done(task_id: int, done_on: str = Form(None), cost: str = Form(None),
              contractor_id: str = Form(None), notes: str = Form(None)):
    done = blank(done_on) or date.today().isoformat()
    with get_db() as db:
        t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404)
        db.execute(
            "INSERT INTO task_logs (task_id, done_on, cost, contractor_id, notes) VALUES (?,?,?,?,?)",
            (task_id, done, num(cost), num(contractor_id, int), blank(notes)))
        nxt = date.fromisoformat(done) + timedelta(days=t["interval_days"])
        db.execute("UPDATE tasks SET next_due=?, last_notified=NULL WHERE id=?",
                   (nxt.isoformat(), task_id))
    return back("/tasks")


@app.get("/tasks/{task_id}")
def task_detail(request: Request, task_id: int):
    with get_db() as db:
        t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404)
        logs = db.execute(
            "SELECT l.*, c.name AS contractor_name FROM task_logs l "
            "LEFT JOIN contractors c ON c.id=l.contractor_id WHERE task_id=? ORDER BY done_on DESC",
            (task_id,)).fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    return render(request, "task_detail.html", t=t, logs=logs, items=items)


@app.post("/tasks/{task_id}")
def task_update(task_id: int, name: str = Form(...), interval_days: int = Form(...),
                next_due: str = Form(...), item_id: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        db.execute(
            "UPDATE tasks SET name=?, interval_days=?, next_due=?, item_id=?, notes=? WHERE id=?",
            (name.strip(), interval_days, next_due, num(item_id, int), blank(notes), task_id))
    return back(f"/tasks/{task_id}")


@app.post("/tasks/{task_id}/delete")
def task_delete(task_id: int):
    with get_db() as db:
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return back("/tasks")


# ---------- rooms & items ----------

ITEM_FIELDS = ["name", "room_id", "brand", "model", "serial", "purchase_date", "price",
               "warranty_until", "life_years", "replace_cost", "notes"]


def item_values(form) -> list:
    return [form.get("name", "").strip(), num(form.get("room_id"), int),
            blank(form.get("brand")), blank(form.get("model")), blank(form.get("serial")),
            blank(form.get("purchase_date")), num(form.get("price")),
            blank(form.get("warranty_until")), num(form.get("life_years"), int),
            num(form.get("replace_cost")), blank(form.get("notes"))]


@app.get("/items")
def items_page(request: Request, room: int | None = None):
    with get_db() as db:
        rooms = db.execute(
            "SELECT r.*, COUNT(i.id) AS n, COALESCE(SUM(i.price),0) AS value FROM rooms r "
            "LEFT JOIN items i ON i.room_id=r.id GROUP BY r.id ORDER BY r.name").fetchall()
        q = ("SELECT i.*, r.name AS room_name FROM items i LEFT JOIN rooms r ON r.id=i.room_id")
        items = (db.execute(q + " WHERE i.room_id=? ORDER BY i.name", (room,)) if room
                 else db.execute(q + " ORDER BY r.name, i.name")).fetchall()
    total = sum(i["price"] or 0 for i in items)
    return render(request, "items.html", rooms=rooms, items=items, room=room, total=total)


@app.post("/rooms")
def room_create(name: str = Form(...)):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (name.strip(),))
    return back("/items")


@app.post("/rooms/{room_id}/delete")
def room_delete(room_id: int):
    with get_db() as db:
        db.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    return back("/items")


@app.post("/items")
async def item_create(request: Request):
    form = await request.form()
    if not form.get("name", "").strip():
        raise HTTPException(400, "name required")
    with get_db() as db:
        cur = db.execute(
            f"INSERT INTO items ({','.join(ITEM_FIELDS)}) VALUES ({','.join('?' * len(ITEM_FIELDS))})",
            item_values(form))
    return back(f"/items/{cur.lastrowid}")


@app.get("/items/{item_id}")
def item_detail(request: Request, item_id: int):
    with get_db() as db:
        item = db.execute(
            "SELECT i.*, r.name AS room_name FROM items i LEFT JOIN rooms r ON r.id=i.room_id "
            "WHERE i.id=?", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404)
        rooms = db.execute("SELECT * FROM rooms ORDER BY name").fetchall()
        docs = db.execute("SELECT * FROM documents WHERE item_id=? ORDER BY uploaded_at DESC",
                          (item_id,)).fetchall()
        tasks = db.execute("SELECT * FROM tasks WHERE item_id=? ORDER BY next_due",
                           (item_id,)).fetchall()
        history = db.execute(
            "SELECT l.*, t.name AS task_name FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "WHERE t.item_id=? ORDER BY done_on DESC", (item_id,)).fetchall()
    return render(request, "item_detail.html", item=item, rooms=rooms, docs=docs, tasks=tasks,
                  history=history, replace_year=replacement_year(item))


@app.post("/items/{item_id}")
async def item_update(request: Request, item_id: int):
    form = await request.form()
    with get_db() as db:
        db.execute(
            f"UPDATE items SET {','.join(f + '=?' for f in ITEM_FIELDS)} WHERE id=?",
            item_values(form) + [item_id])
    return back(f"/items/{item_id}")


@app.post("/items/{item_id}/delete")
def item_delete(item_id: int):
    with get_db() as db:
        db.execute("DELETE FROM items WHERE id=?", (item_id,))
    return back("/items")


# ---------- documents ----------

@app.get("/documents")
def documents_page(request: Request):
    with get_db() as db:
        docs = db.execute(
            "SELECT d.*, i.name AS item_name FROM documents d LEFT JOIN items i ON i.id=d.item_id "
            "ORDER BY uploaded_at DESC").fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    return render(request, "documents.html", docs=docs, items=items)


@app.post("/documents")
async def document_upload(file: UploadFile = File(...), title: str = Form(None),
                          item_id: str = Form(None), next: str = Form("/documents")):
    if not file.filename:
        raise HTTPException(400, "file required")
    stored = f"{uuid.uuid4().hex}{Path(file.filename).suffix.lower()[:10]}"
    dest = data_dir() / "uploads" / stored
    with dest.open("wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    with get_db() as db:
        db.execute(
            "INSERT INTO documents (title, filename, stored_name, item_id, uploaded_at) "
            "VALUES (?,?,?,?,?)",
            (blank(title) or file.filename, Path(file.filename).name, stored,
             num(item_id, int), datetime.now().isoformat(timespec="seconds")))
    return back(next if next.startswith("/") else "/documents")


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: int):
    with get_db() as db:
        d = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404)
    return FileResponse(data_dir() / "uploads" / d["stored_name"], filename=d["filename"],
                        content_disposition_type="inline")


@app.post("/documents/{doc_id}/delete")
def document_delete(doc_id: int):
    with get_db() as db:
        d = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if d:
            (data_dir() / "uploads" / d["stored_name"]).unlink(missing_ok=True)
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    return back("/documents")


# ---------- contractors ----------

@app.get("/contractors")
def contractors_page(request: Request):
    with get_db() as db:
        cs = db.execute(
            "SELECT c.*, COUNT(l.id) AS jobs, COALESCE(SUM(l.cost),0) AS spent, MAX(l.done_on) AS last "
            "FROM contractors c LEFT JOIN task_logs l ON l.contractor_id=c.id "
            "GROUP BY c.id ORDER BY c.trade, c.name").fetchall()
    return render(request, "contractors.html", contractors=cs)


@app.post("/contractors")
def contractor_create(name: str = Form(...), trade: str = Form(None), phone: str = Form(None),
                      email: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        db.execute("INSERT INTO contractors (name, trade, phone, email, notes) VALUES (?,?,?,?,?)",
                   (name.strip(), blank(trade), blank(phone), blank(email), blank(notes)))
    return back("/contractors")


@app.post("/contractors/{cid}/delete")
def contractor_delete(cid: int):
    with get_db() as db:
        db.execute("DELETE FROM contractors WHERE id=?", (cid,))
    return back("/contractors")


# ---------- budget ----------

@app.get("/budget")
def budget_page(request: Request):
    this_year = date.today().year
    with get_db() as db:
        items = db.execute(
            "SELECT i.*, r.name AS room_name FROM items i LEFT JOIN rooms r ON r.id=i.room_id"
        ).fetchall()
        spend = db.execute(
            "SELECT substr(done_on,1,4) AS yr, SUM(cost) AS total FROM task_logs "
            "WHERE cost IS NOT NULL GROUP BY yr ORDER BY yr DESC").fetchall()
    years: dict[int, list] = {}
    untracked = []
    for i in items:
        y = replacement_year(i)
        if y is None:
            untracked.append(i)
        else:
            years.setdefault(max(y, this_year), []).append((y, i))
    forecast = [(y, rows, sum((i["replace_cost"] or 0) for _, i in rows))
                for y, rows in sorted(years.items())]
    ten_year = sum(t for y, _, t in forecast if y < this_year + 10)
    return render(request, "budget.html", forecast=forecast, untracked=untracked,
                  spend=spend, ten_year=ten_year, monthly=ten_year / 120, this_year=this_year)


# ---------- settings ----------

@app.get("/settings")
def settings_page(request: Request, sent: str | None = None):
    return render(request, "settings.html", targets=len(notify.apprise_urls()), sent=sent,
                  lead=os.environ.get("NOTIFY_LEAD_DAYS", "3"), data=str(data_dir()))


@app.post("/settings/test")
def settings_test():
    ok = notify.send("Hearth test", "Notifications are working.")
    return back(f"/settings?sent={'ok' if ok else 'fail'}")


@app.get("/api/health")
def health():
    return {"status": "ok"}
