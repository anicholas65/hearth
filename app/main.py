import asyncio
import base64
import os
import secrets
import io
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError

from . import DEVELOPER, DEVELOPER_HANDLE, REPO_URL, __version__, gcal, notify, shopping
from .db import data_dir, get_db, get_setting, init_db, set_setting

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
def _nicedate(v: str) -> str:
    d = date.fromisoformat(v[:10])
    t = date.today()
    if d == t:
        return "Today"
    if d == t + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a, %b ") + str(d.day) + (f", {d.year}" if d.year != t.year else "")


def _nicetime(v: str | None) -> str:
    if not v or len(v) < 16:
        return ""
    h, m = int(v[11:13]), v[14:16]
    return f"{(h % 12) or 12}:{m} {'AM' if h < 12 else 'PM'}"


templates.env.filters["nicedate"] = _nicedate
templates.env.filters["nicetime"] = _nicetime
templates.env.filters["n"] = lambda v: "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))

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
        if gcal.configured():
            asyncio.create_task(asyncio.to_thread(gcal.sync_all))
    yield
    if task:
        task.cancel()


app = FastAPI(title="Hearth", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    user, pw = os.environ.get("HEARTH_USERNAME"), os.environ.get("HEARTH_PASSWORD")
    if not (user and pw) or request.url.path == "/api/health":
        return await call_next(request)
    try:
        scheme, _, cred = request.headers.get("authorization", "").partition(" ")
        u, _, p = base64.b64decode(cred).decode().partition(":")
        ok = scheme.lower() == "basic" and secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)
    except Exception:
        ok = False
    if ok:
        return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Hearth"'})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def blank(v: str | None):
    return v.strip() if v and v.strip() else None


def num(v: str | None, cast=float):
    v = blank(v)
    return cast(v) if v is not None else None


THEMES = {  # key: (label, hue, saturation)
    "ember": ("Ember", 18, 62), "forest": ("Forest", 152, 42), "ocean": ("Ocean", 205, 68),
    "plum": ("Plum", 275, 42), "rose": ("Rose", 345, 58), "slate": ("Slate", 215, 18),
}
MODES = ("auto", "light", "dark")


def greeting() -> str:
    h = datetime.now().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"


def render(request: Request, name: str, **ctx):
    ctx["today"] = date.today().isoformat()
    t = request.cookies.get("theme")
    m = request.cookies.get("mode")
    ctx["theme"] = t if t in THEMES else "ember"
    ctx["mode"] = m if m in MODES else "auto"
    ctx["themes"] = THEMES
    ctx["about"] = {"version": __version__, "developer": DEVELOPER,
                     "handle": DEVELOPER_HANDLE, "repo": REPO_URL}
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
            "SELECT i.*, (SELECT id FROM photos p WHERE p.item_id=i.id ORDER BY is_cover DESC, id LIMIT 1) AS cover "
            "FROM items i WHERE warranty_until BETWEEN ? AND ? ORDER BY warranty_until",
            (today.isoformat(), warn)).fetchall()
        items = db.execute("SELECT * FROM items").fetchall()
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("items", "tasks", "documents", "contractors")}
        counts["shopping"] = db.execute(
            "SELECT COUNT(*) FROM shopping_items WHERE status='needed'").fetchone()[0]
        recent = db.execute(
            "SELECT l.*, t.name AS task_name FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "ORDER BY done_on DESC, l.id DESC LIMIT 5").fetchall()
        appts = db.execute(
            "SELECT a.*, c.name AS contractor_name FROM appointments a "
            "LEFT JOIN contractors c ON c.id=a.contractor_id "
            "WHERE substr(starts_at,1,10) BETWEEN ? AND ? ORDER BY starts_at LIMIT 6",
            (today.isoformat(), soon)).fetchall()
    horizon = today.year + 2
    upcoming = sorted(
        [(replacement_year(i), i) for i in items
         if replacement_year(i) and replacement_year(i) <= horizon],
        key=lambda x: x[0])
    overdue = sum(1 for t in due if t["next_due"] < today.isoformat())
    return render(request, "dashboard.html", due=due, warranties=warranties,
                  upcoming=upcoming, counts=counts, recent=recent, overdue=overdue,
                  greeting=greeting(), appts=appts)


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
def task_create(bg: BackgroundTasks, name: str = Form(...), interval_days: int = Form(...),
                next_due: str = Form(None), item_id: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO tasks (name, interval_days, next_due, item_id, notes) VALUES (?,?,?,?,?)",
            (name.strip(), interval_days, blank(next_due) or date.today().isoformat(),
             num(item_id, int), blank(notes)))
    bg.add_task(gcal.sync_task, cur.lastrowid)
    return back("/tasks")


@app.post("/tasks/seed")
def task_seed(bg: BackgroundTasks):
    today = date.today()
    with get_db() as db:
        existing = {r[0] for r in db.execute("SELECT name FROM tasks")}
        db.executemany(
            "INSERT INTO tasks (name, interval_days, next_due) VALUES (?,?,?)",
            [(n, d, (today + timedelta(days=min(d, 30))).isoformat())
             for n, d in DEFAULT_TASKS if n not in existing])
    bg.add_task(gcal.sync_all)
    return back("/tasks")


@app.post("/tasks/{task_id}/done")
def task_done(bg: BackgroundTasks, task_id: int, done_on: str = Form(None), cost: str = Form(None),
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
    bg.add_task(gcal.sync_task, task_id)
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
def task_update(bg: BackgroundTasks, task_id: int, name: str = Form(...), interval_days: int = Form(...),
                next_due: str = Form(...), item_id: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        db.execute(
            "UPDATE tasks SET name=?, interval_days=?, next_due=?, item_id=?, notes=? WHERE id=?",
            (name.strip(), interval_days, next_due, num(item_id, int), blank(notes), task_id))
    bg.add_task(gcal.sync_task, task_id)
    return back(f"/tasks/{task_id}")


@app.post("/tasks/{task_id}/delete")
def task_delete(bg: BackgroundTasks, task_id: int):
    with get_db() as db:
        r = db.execute("SELECT gcal_event_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    if r and r[0]:
        bg.add_task(gcal.delete_event, r[0])
    return back("/tasks")


# ---------- schedule (appointments & reminders) ----------

def appt_values(form) -> tuple:
    kind = form.get("kind") if form.get("kind") in ("appointment", "reminder") else "appointment"
    all_day = 1 if form.get("all_day") else 0
    d = blank(form.get("date"))
    if not d or not blank(form.get("title")):
        raise HTTPException(400, "title and date required")
    st, et = blank(form.get("start_time")), blank(form.get("end_time"))
    if all_day:
        starts, ends = d, None
    else:
        starts = f"{d}T{st or '09:00'}"
        ends = f"{d}T{et}" if et and kind == "appointment" else None
    rm = blank(form.get("remind_minutes"))
    return (kind, form["title"].strip(), starts, ends, all_day, blank(form.get("location")),
            blank(form.get("notes")), num(form.get("contractor_id"), int),
            num(form.get("item_id"), int), int(rm) if rm is not None else None)


APPT_COLS = ("kind, title, starts_at, ends_at, all_day, location, notes, contractor_id, "
             "item_id, remind_minutes")


@app.get("/schedule")
def schedule_page(request: Request, past: int = 0):
    today = date.today().isoformat()
    op, order = ("<", "DESC") if past else (">=", "ASC")
    with get_db() as db:
        appts = db.execute(
            "SELECT a.*, c.name AS contractor_name, c.phone AS contractor_phone, i.name AS item_name "
            "FROM appointments a LEFT JOIN contractors c ON c.id=a.contractor_id "
            f"LEFT JOIN items i ON i.id=a.item_id WHERE substr(starts_at,1,10) {op} ? "
            f"ORDER BY starts_at {order} LIMIT 200", (today,)).fetchall()
        contractors = db.execute("SELECT id, name, trade FROM contractors ORDER BY name").fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    return render(request, "schedule.html", appts=appts, contractors=contractors, items=items,
                  past=past, gcal_on=gcal.configured() and gcal.enabled())


@app.post("/schedule")
async def schedule_create(request: Request, bg: BackgroundTasks):
    v = appt_values(await request.form())
    with get_db() as db:
        cur = db.execute(f"INSERT INTO appointments ({APPT_COLS}) VALUES ({','.join('?' * 10)})", v)
    bg.add_task(gcal.sync_appointment, cur.lastrowid)
    return back("/schedule")


@app.get("/schedule/{appt_id}")
def schedule_detail(request: Request, appt_id: int):
    with get_db() as db:
        a = db.execute(
            "SELECT a.*, c.name AS contractor_name FROM appointments a "
            "LEFT JOIN contractors c ON c.id=a.contractor_id WHERE a.id=?", (appt_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        contractors = db.execute("SELECT id, name, trade FROM contractors ORDER BY name").fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    return render(request, "appointment.html", a=a, contractors=contractors, items=items,
                  gcal_on=gcal.configured() and gcal.enabled())


@app.post("/schedule/{appt_id}")
async def schedule_update(request: Request, bg: BackgroundTasks, appt_id: int):
    v = appt_values(await request.form())
    sets = ", ".join(f"{c.strip()}=?" for c in APPT_COLS.split(","))
    with get_db() as db:
        db.execute(f"UPDATE appointments SET {sets}, last_notified=NULL WHERE id=?", v + (appt_id,))
    bg.add_task(gcal.sync_appointment, appt_id)
    return back(f"/schedule/{appt_id}")


@app.post("/schedule/{appt_id}/delete")
def schedule_delete(bg: BackgroundTasks, appt_id: int):
    with get_db() as db:
        r = db.execute("SELECT gcal_event_id FROM appointments WHERE id=?", (appt_id,)).fetchone()
        db.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
    if r and r[0]:
        bg.add_task(gcal.delete_event, r[0])
    return back("/schedule")


# ---------- shopping list ----------

def list_key(v: str | None) -> str:
    return v if v in shopping.LISTS else "groceries"


def now_s() -> str:
    return datetime.now().isoformat(timespec="seconds")


@app.get("/shopping")
def shopping_page(request: Request, list: str = "groceries"):
    lk = list_key(list)
    with get_db() as db:
        rows = db.execute(
            "SELECT s.*, i.name AS item_name FROM shopping_items s LEFT JOIN items i ON i.id=s.item_id "
            "WHERE list=? AND status IN ('needed','checked') ORDER BY s.added_at, s.id", (lk,)).fetchall()
        counts = dict(db.execute(
            "SELECT list, COUNT(*) FROM shopping_items WHERE status='needed' GROUP BY list").fetchall())
        on_list = {r["name"].lower() for r in rows}
        frequent = [r for r in db.execute(
            "SELECT name, COUNT(*) AS n, MAX(bought_at) AS last FROM shopping_items "
            "WHERE list=? AND status='bought' GROUP BY lower(name) ORDER BY n DESC, last DESC LIMIT 24",
            (lk,)).fetchall() if r["name"].lower() not in on_list][:16]
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        stores = [r[0] for r in db.execute(
            "SELECT DISTINCT store FROM shopping_items WHERE store IS NOT NULL ORDER BY store")]
    needed = [r for r in rows if r["status"] == "needed"]
    checked = [r for r in rows if r["status"] == "checked"]
    order = {c: n for n, c in enumerate(shopping.CATEGORY_NAMES)}
    groups: dict[str, list] = {}
    for r in sorted(needed, key=lambda r: (order.get(r["category"] or "Other", 99), r["name"].lower())):
        groups.setdefault(r["category"] or "Other", []).append(r)
    est = sum(r["price"] or 0 for r in needed)
    return render(request, "shopping.html", lst=lk, lists=shopping.LISTS, groups=groups,
                  checked=checked, counts=counts, frequent=frequent, items=items, stores=stores,
                  categories=shopping.CATEGORY_NAMES, est=est, n_needed=len(needed))


def _done(request: Request, url: str):
    if request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=204)
    return back(url)


@app.post("/shopping")
async def shopping_add(request: Request):
    f = await request.form()
    lk = list_key(f.get("list"))
    raw = blank(f.get("name"))
    if not raw:
        raise HTTPException(400, "name required")
    nxt = f.get("next") or f"/shopping?list={lk}"
    added = 0
    with get_db() as db:
        # one item per line or comma, so a whole list can be pasted at once
        for part in [p for chunk in raw.splitlines() for p in chunk.split(",")]:
            if not part.strip():
                continue
            name, qty = shopping.parse_quick(part)
            qty = blank(f.get("qty")) or qty
            cat = blank(f.get("category")) or shopping.guess_category(name, lk)
            dup = db.execute("SELECT id FROM shopping_items WHERE list=? AND status='needed' "
                             "AND lower(name)=lower(?)", (lk, name)).fetchone()
            if dup:  # already on the list: just refresh qty/note
                db.execute("UPDATE shopping_items SET qty=COALESCE(?, qty), note=COALESCE(?, note) "
                           "WHERE id=?", (qty, blank(f.get("note")), dup[0]))
                continue
            db.execute(
                "INSERT INTO shopping_items (list, name, qty, category, store, note, price, item_id, added_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (lk, name, qty, cat, blank(f.get("store")), blank(f.get("note")), num(f.get("price")),
                 num(f.get("item_id"), int), now_s()))
            added += 1
    return back(nxt if nxt.startswith("/") else f"/shopping?list={lk}")


@app.post("/shopping/again")
def shopping_again(list: str = Form(...), name: str = Form(...)):
    lk = list_key(list)
    with get_db() as db:
        last = db.execute("SELECT * FROM shopping_items WHERE list=? AND lower(name)=lower(?) "
                          "ORDER BY bought_at DESC, id DESC LIMIT 1", (lk, name)).fetchone()
        on = db.execute("SELECT 1 FROM shopping_items WHERE list=? AND status='needed' "
                        "AND lower(name)=lower(?)", (lk, name)).fetchone()
        if not on:
            db.execute(
                "INSERT INTO shopping_items (list, name, qty, category, store, price, item_id, added_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (lk, last["name"] if last else name, last["qty"] if last else None,
                 (last["category"] if last else None) or shopping.guess_category(name, lk),
                 last["store"] if last else None, last["price"] if last else None,
                 last["item_id"] if last else None, now_s()))
    return back(f"/shopping?list={lk}")


@app.post("/shopping/{sid:int}/toggle")
def shopping_toggle(request: Request, sid: int):
    with get_db() as db:
        r = db.execute("SELECT list, status FROM shopping_items WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404)
        new = "needed" if r["status"] == "checked" else "checked"
        db.execute("UPDATE shopping_items SET status=?, checked_at=? WHERE id=?",
                   (new, now_s() if new == "checked" else None, sid))
    return _done(request, f"/shopping?list={r['list']}")


@app.post("/shopping/{sid:int}")
def shopping_edit(sid: int, name: str = Form(...), qty: str = Form(None), category: str = Form(None),
                  store: str = Form(None), note: str = Form(None), price: str = Form(None),
                  item_id: str = Form(None)):
    with get_db() as db:
        r = db.execute("SELECT list FROM shopping_items WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404)
        db.execute("UPDATE shopping_items SET name=?, qty=?, category=?, store=?, note=?, price=?, "
                   "item_id=? WHERE id=?",
                   (name.strip(), blank(qty), blank(category) or shopping.guess_category(name),
                    blank(store), blank(note), num(price), num(item_id, int), sid))
    return back(f"/shopping?list={r['list']}")


@app.post("/shopping/{sid:int}/delete")
def shopping_delete(request: Request, sid: int):
    with get_db() as db:
        r = db.execute("SELECT list FROM shopping_items WHERE id=?", (sid,)).fetchone()
        db.execute("DELETE FROM shopping_items WHERE id=?", (sid,))
    return _done(request, f"/shopping?list={r['list'] if r else 'groceries'}")


@app.post("/shopping/clear")
def shopping_clear(list: str = Form(...), spent: str = Form(None), store: str = Form(None)):
    lk = list_key(list)
    ts = now_s()
    with get_db() as db:
        n = db.execute("UPDATE shopping_items SET status='bought', bought_at=? "
                       "WHERE list=? AND status='checked'", (ts, lk)).rowcount
        amt = num(spent)
        if amt and amt > 0:
            where = f" at {blank(store)}" if blank(store) else ""
            db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note) VALUES (?,?,?,?)",
                       (ts[:10], "expense", amt,
                        f"{shopping.LISTS[lk]}{where} ({n} item{'s' if n != 1 else ''})"))
    return back(f"/shopping?list={lk}")


@app.get("/shopping/text")
def shopping_text(list: str = "groceries"):
    lk = list_key(list)
    with get_db() as db:
        rows = db.execute("SELECT * FROM shopping_items WHERE list=? AND status='needed'", (lk,)).fetchall()
    order = {c: n for n, c in enumerate(shopping.CATEGORY_NAMES)}
    out, cat = [f"{shopping.LISTS[lk]} list"], None
    for r in sorted(rows, key=lambda r: (order.get(r["category"] or "Other", 99), r["name"].lower())):
        if r["category"] != cat:
            cat = r["category"]
            out.append(f"\n{cat or 'Other'}")
        out.append(f"[ ] {r['name']}" + (f" ({r['qty']})" if r["qty"] else "")
                   + (f" - {r['note']}" if r["note"] else ""))
    return Response("\n".join(out) + "\n", media_type="text/plain; charset=utf-8")


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
            "SELECT r.*, COUNT(i.id) AS n, COALESCE(SUM(i.price),0) AS value, "
            "(SELECT id FROM photos p WHERE p.room_id=r.id ORDER BY is_cover DESC, id LIMIT 1) AS cover "
            "FROM rooms r LEFT JOIN items i ON i.room_id=r.id GROUP BY r.id ORDER BY r.name").fetchall()
        q = ("SELECT i.*, r.name AS room_name, "
             "(SELECT id FROM photos p WHERE p.item_id=i.id ORDER BY is_cover DESC, id LIMIT 1) AS cover "
             "FROM items i LEFT JOIN rooms r ON r.id=i.room_id")
        items = (db.execute(q + " WHERE i.room_id=? ORDER BY i.name", (room,)) if room
                 else db.execute(q + " ORDER BY r.name, i.name")).fetchall()
    total = sum(i["price"] or 0 for i in items)
    current = next((r for r in rooms if r["id"] == room), None)
    return render(request, "items.html", rooms=rooms, items=items, room=room, total=total,
                  current=current)


@app.post("/rooms")
def room_create(name: str = Form(...)):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (name.strip(),))
    return back("/items")


@app.post("/rooms/{room_id}/delete")
def room_delete(room_id: int):
    with get_db() as db:
        purge_photos(db, "room_id", room_id)
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
        photos = db.execute("SELECT * FROM photos WHERE item_id=? ORDER BY is_cover DESC, id",
                            (item_id,)).fetchall()
        history = db.execute(
            "SELECT l.*, t.name AS task_name FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "WHERE t.item_id=? ORDER BY done_on DESC", (item_id,)).fetchall()
    return render(request, "item_detail.html", item=item, rooms=rooms, docs=docs, tasks=tasks,
                  history=history, photos=photos,
                  replace_year=replacement_year(item))


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
        purge_photos(db, "item_id", item_id)
        db.execute("DELETE FROM items WHERE id=?", (item_id,))
    return back("/items")


# ---------- photos ----------

def save_image(data: bytes) -> tuple[str, str]:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Unsupported image (use JPEG, PNG, WebP or GIF)")
    base = uuid.uuid4().hex
    up = data_dir() / "uploads"
    full = img.copy()
    full.thumbnail((2000, 2000))
    full.save(up / f"{base}.jpg", "JPEG", quality=85, optimize=True)
    thumb = ImageOps.fit(img, (480, 360), Image.LANCZOS)
    thumb.save(up / f"{base}_t.jpg", "JPEG", quality=80, optimize=True)
    return f"{base}.jpg", f"{base}_t.jpg"


def purge_photos(db, col: str, ref: int) -> None:
    for p in db.execute(f"SELECT * FROM photos WHERE {col}=?", (ref,)).fetchall():
        for n in (p["stored_name"], p["thumb_name"]):
            (data_dir() / "uploads" / n).unlink(missing_ok=True)
    db.execute(f"DELETE FROM photos WHERE {col}=?", (ref,))


@app.post("/photos")
async def photo_upload(files: list[UploadFile] = File(...), item_id: str = Form(None),
                       room_id: str = Form(None), caption: str = Form(None),
                       next: str = Form("/gallery")):
    iid, rid = num(item_id, int), num(room_id, int)
    saved = []
    for f in files:
        if f.filename:
            saved.append(save_image(await f.read()))
    with get_db() as db:
        if rid and not iid and saved:
            purge_photos(db, "room_id", rid)  # rooms keep a single cover photo
        db.executemany(
            "INSERT INTO photos (item_id, room_id, stored_name, thumb_name, caption, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(iid, rid, a, b, blank(caption), datetime.now().isoformat(timespec="seconds"))
             for a, b in saved])
    return back(next if next.startswith("/") else "/gallery")


def photo_path(photo_id: int, thumb: bool) -> Path:
    with get_db() as db:
        p = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not p:
        raise HTTPException(404)
    return data_dir() / "uploads" / (p["thumb_name"] if thumb else p["stored_name"])


@app.get("/photos/{photo_id}")
def photo_full(photo_id: int):
    return FileResponse(photo_path(photo_id, False), headers={"Cache-Control": "max-age=604800"})


@app.get("/photos/{photo_id}/thumb")
def photo_thumb(photo_id: int):
    return FileResponse(photo_path(photo_id, True), headers={"Cache-Control": "max-age=604800"})


@app.post("/photos/{photo_id}/delete")
def photo_delete(photo_id: int, next: str = Form("/items")):
    with get_db() as db:
        p = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if p:
            for n in (p["stored_name"], p["thumb_name"]):
                (data_dir() / "uploads" / n).unlink(missing_ok=True)
            db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    return back(next if next.startswith("/") else "/items")


@app.post("/photos/{photo_id}/cover")
def photo_cover(photo_id: int, next: str = Form("/items")):
    with get_db() as db:
        p = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if p:
            col, ref = ("item_id", p["item_id"]) if p["item_id"] else ("room_id", p["room_id"])
            db.execute(f"UPDATE photos SET is_cover=0 WHERE {col}=?", (ref,))
            db.execute("UPDATE photos SET is_cover=1 WHERE id=?", (photo_id,))
    return back(next if next.startswith("/") else "/items")


@app.get("/gallery")
def gallery_page(request: Request, show: str = "all"):
    where = {"unfiled": "WHERE p.item_id IS NULL AND p.room_id IS NULL",
             "items": "WHERE p.item_id IS NOT NULL", "rooms": "WHERE p.room_id IS NOT NULL"}.get(show, "")
    with get_db() as db:
        photos = db.execute(
            "SELECT p.*, i.name AS item_name, r.name AS room_name FROM photos p "
            "LEFT JOIN items i ON i.id=p.item_id LEFT JOIN rooms r ON r.id=p.room_id "
            f"{where} ORDER BY p.created_at DESC, p.id DESC").fetchall()
        counts = db.execute(
            "SELECT COUNT(*) AS all_, SUM(item_id IS NULL AND room_id IS NULL) AS unfiled, "
            "SUM(item_id IS NOT NULL) AS items, SUM(room_id IS NOT NULL) AS rooms FROM photos").fetchone()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        rooms = db.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
    return render(request, "gallery.html", photos=photos, show=show, counts=counts,
                  items=items, rooms=rooms)


@app.get("/gallery/{photo_id}")
def gallery_photo(request: Request, photo_id: int):
    with get_db() as db:
        p = db.execute(
            "SELECT p.*, i.name AS item_name, r.name AS room_name FROM photos p "
            "LEFT JOIN items i ON i.id=p.item_id LEFT JOIN rooms r ON r.id=p.room_id WHERE p.id=?",
            (photo_id,)).fetchone()
        if not p:
            raise HTTPException(404)
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        rooms = db.execute("SELECT id, name FROM rooms ORDER BY name").fetchall()
        ids = [r[0] for r in db.execute("SELECT id FROM photos ORDER BY created_at DESC, id DESC")]
    pos = ids.index(photo_id)
    prev_id = ids[pos - 1] if pos > 0 else None
    next_id = ids[pos + 1] if pos + 1 < len(ids) else None
    return render(request, "photo.html", photo=p, items=items, rooms=rooms,
                  prev_id=prev_id, next_id=next_id)


@app.post("/gallery/{photo_id}")
def gallery_update(photo_id: int, caption: str = Form(None), item_id: str = Form(None),
                   room_id: str = Form(None)):
    with get_db() as db:
        rid = num(room_id, int)
        db.execute("UPDATE photos SET caption=?, item_id=?, room_id=?, is_cover=0 WHERE id=?",
                   (blank(caption), num(item_id, int), rid, photo_id))
        if rid:  # filing to a room makes it that room's cover
            db.execute("UPDATE photos SET is_cover=0 WHERE room_id=?", (rid,))
            db.execute("UPDATE photos SET is_cover=1 WHERE id=?", (photo_id,))
    return back(f"/gallery/{photo_id}")


# ---------- theme ----------

@app.post("/theme")
def set_theme(request: Request, theme: str = Form(None), mode: str = Form(None)):
    ref = urlsplit(request.headers.get("referer") or "/")
    resp = back((ref.path or "/") + (f"?{ref.query}" if ref.query else ""))
    year = 60 * 60 * 24 * 365
    if theme in THEMES:
        resp.set_cookie("theme", theme, max_age=year, samesite="lax")
    if mode in MODES:
        resp.set_cookie("mode", mode, max_age=year, samesite="lax")
    return resp


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

def forecast_data():
    this_year = date.today().year
    with get_db() as db:
        items = db.execute(
            "SELECT i.*, r.name AS room_name FROM items i LEFT JOIN rooms r ON r.id=i.room_id"
        ).fetchall()
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
    return dict(forecast=forecast, untracked=untracked, ten_year=ten_year,
                monthly=ten_year / 120, this_year=this_year)


def nice_ceiling(v: float) -> float:
    if v <= 0:
        return 100.0
    mag = 10 ** (len(str(int(v))) - 1)
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * mag >= v:
            return step * mag
    return 10 * mag


LEDGER_SQL = """
SELECT 'f' || id AS key, id, entry_date AS d, kind, amount, note, item_id, NULL AS task_id
  FROM fund_entries
UNION ALL
SELECT 't' || l.id, l.id, l.done_on, 'expense', l.cost, 'Maintenance: ' || t.name, t.item_id, t.id
  FROM task_logs l JOIN tasks t ON t.id = l.task_id WHERE l.cost IS NOT NULL AND l.cost > 0
"""


def balance_data(year: int):
    y = str(year)
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM ({LEDGER_SQL}) ORDER BY d, key").fetchall()
        budget = db.execute("SELECT amount FROM budgets WHERE year=?", (year,)).fetchone()
        years = {r[0] for r in db.execute("SELECT year FROM budgets")}
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
    sign = lambda r: r["amount"] if r["kind"] == "allocation" else -r["amount"]
    carry = sum(sign(r) for r in rows if r["d"][:4] < y)
    ledger, run = [], carry
    months = [{"m": m, "alloc": 0.0, "spent": 0.0} for m in range(1, 13)]
    for r in rows:
        years.add(int(r["d"][:4]))
        if r["d"][:4] != y:
            continue
        run += sign(r)
        ledger.append({**dict(r), "balance": run})
        months[int(r["d"][5:7]) - 1]["alloc" if r["kind"] == "allocation" else "spent"] += r["amount"]
    allocated = sum(m["alloc"] for m in months)
    spent = sum(m["spent"] for m in months)
    fund_total = sum(sign(r) for r in rows)
    budget_amt = budget["amount"] if budget else None
    peak = max([m["alloc"] for m in months] + [m["spent"] for m in months] + [0])
    axis_max = nice_ceiling(peak)
    years.add(date.today().year)
    return dict(
        year=year, years=sorted(years, reverse=True), budget=budget_amt,
        allocated=allocated, spent=spent, carry=carry, year_end=run, fund_total=fund_total,
        remaining=(budget_amt - spent) if budget_amt is not None else None,
        unallocated=(budget_amt - allocated) if budget_amt is not None else None,
        months=months, axis_max=axis_max, ticks=[axis_max * k / 4 for k in range(5)],
        ledger=list(reversed(ledger)), items=items)


@app.get("/budget")
def budget_page(request: Request, view: str = "balance", year: int | None = None):
    if view == "forecast":
        return render(request, "budget.html", view=view, **forecast_data())
    return render(request, "budget.html", view="balance",
                  **balance_data(year or date.today().year))


@app.post("/budget/year")
def budget_set(year: int = Form(...), amount: str = Form(None)):
    with get_db() as db:
        if num(amount) is None:
            db.execute("DELETE FROM budgets WHERE year=?", (year,))
        else:
            db.execute("INSERT INTO budgets (year, amount) VALUES (?,?) "
                       "ON CONFLICT(year) DO UPDATE SET amount=excluded.amount", (year, num(amount)))
    return back(f"/budget?year={year}")


@app.post("/budget/entries")
def budget_entry(kind: str = Form(...), amount: float = Form(...), entry_date: str = Form(None),
                 note: str = Form(None), item_id: str = Form(None)):
    if kind not in ("allocation", "expense") or amount <= 0:
        raise HTTPException(400, "kind must be allocation/expense and amount > 0")
    d = blank(entry_date) or date.today().isoformat()
    with get_db() as db:
        db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, item_id) "
                   "VALUES (?,?,?,?,?)", (d, kind, amount, blank(note), num(item_id, int)))
    return back(f"/budget?year={d[:4]}")


@app.post("/budget/entries/{entry_id}/delete")
def budget_entry_delete(entry_id: int):
    with get_db() as db:
        r = db.execute("SELECT entry_date FROM fund_entries WHERE id=?", (entry_id,)).fetchone()
        db.execute("DELETE FROM fund_entries WHERE id=?", (entry_id,))
    return back(f"/budget?year={r[0][:4]}" if r else "/budget")


# ---------- settings ----------

@app.get("/settings")
def settings_page(request: Request, sent: str | None = None, g: str | None = None,
                  gmsg: str | None = None):
    google = dict(
        configured=gcal.configured(), key=gcal.key_path().is_file(),
        email=gcal.service_account_email(), calendar_id=gcal.calendar_id() or "",
        calendar_env=bool(os.environ.get("GOOGLE_CALENDAR_ID")),
        timezone=gcal.timezone(), task_remind=gcal.task_remind_minutes(),
        enabled=gcal.enabled(), public_url=get_setting("public_url") or "",
        last_sync=get_setting("gcal_last_sync"), last_error=get_setting("gcal_last_error"))
    return render(request, "settings.html", targets=len(notify.apprise_urls()), sent=sent,
                  lead=os.environ.get("NOTIFY_LEAD_DAYS", "3"), data=str(data_dir()),
                  google=google, g=g, gmsg=gmsg)


@app.post("/settings/test")
def settings_test():
    ok = notify.send("Hearth test", "Notifications are working.")
    return back(f"/settings?sent={'ok' if ok else 'fail'}")


def gback(status: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote_plus
    return back(f"/settings?g={status}&gmsg={quote_plus(msg)}#google")


@app.post("/settings/google")
def settings_google(calendar_id: str = Form(None), timezone: str = Form(None),
                    task_remind: str = Form(None), public_url: str = Form(None),
                    enabled: str = Form(None)):
    set_setting("gcal_calendar_id", blank(calendar_id))
    set_setting("gcal_timezone", blank(timezone))
    rm = num(task_remind, int) if blank(task_remind) else 900
    set_setting("gcal_task_remind", str(min(max(rm, 0), 40320)))  # Google's allowed range
    set_setting("public_url", blank(public_url))
    set_setting("gcal_enabled", "1" if enabled else "0")
    return gback("ok", "Saved")


@app.post("/settings/google/key")
async def settings_google_key(file: UploadFile = File(...)):
    try:
        email = gcal.save_key(await file.read())
    except ValueError as e:
        return gback("err", str(e))
    return gback("ok", f"Key saved. Share your calendar with {email}")


@app.post("/settings/google/key/delete")
def settings_google_key_delete():
    p = data_dir() / "google-service-account.json"
    p.unlink(missing_ok=True)
    return gback("ok", "Key removed")


@app.post("/settings/google/test")
def settings_google_test():
    try:
        name = gcal.test_connection()
    except Exception as e:
        return gback("err", str(e))
    return gback("ok", f"Connected to calendar \u201c{name}\u201d")


@app.post("/settings/google/sync")
def settings_google_sync():
    if not gcal.configured():
        return gback("err", "Not configured yet")
    ok, failed = gcal.sync_all()
    if failed:
        return gback("err", f"Synced {ok}, {failed} failed: {get_setting('gcal_last_error')}")
    return gback("ok", f"Synced {ok} events to Google Calendar")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}
