import asyncio
import base64
import os
import secrets
import shutil
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

from . import DEVELOPER, REPO_URL, __version__, backup, finance, gcal, home, notify, plan, shopping
from . import search as search_mod
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
def _cap(v):
    """Display titles with a leading capital without changing what was stored."""
    return v[:1].upper() + v[1:] if isinstance(v, str) and v[:1].islower() else v


templates.env.filters["cap"] = _cap


def _hl(v, q):
    """Escape, then wrap case-insensitive matches of q in <mark>."""
    import re
    from markupsafe import Markup, escape
    text = str(escape(v or ""))
    if q:
        text = re.sub(re.escape(str(escape(q))), lambda m: f"<mark>{m.group(0)}</mark>", text, flags=re.I)
    return Markup(text)


templates.env.filters["hl"] = _hl
templates.env.filters["repeat"] = lambda d: plan.repeat_label(d)


def _due_label(v: str) -> str:
    d = (date.fromisoformat(v[:10]) - date.today()).days
    if d < -1:
        return f"Overdue by {-d} days"
    return {-1: "Overdue by 1 day", 0: "Due today", 1: "Due tomorrow"}.get(d, f"Due in {d} days" if d <= 13 else
                                                                           f"Due {_nicedate(v)}")


templates.env.filters["duein"] = _due_label
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


from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def friendly_errors(request: Request, exc: StarletteHTTPException):
    """HTML pages get a readable error page; API/fetch callers keep JSON."""
    wants_html = "text/html" in request.headers.get("accept", "") and \
        request.headers.get("x-requested-with") != "fetch" and not request.url.path.startswith("/api")
    if not wants_html:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    msg = {404: "We couldn't find that page. It may have been deleted."}.get(exc.status_code, str(exc.detail))
    resp = render(request, "error.html", code=exc.status_code, message=msg,
                  back_href=request.headers.get("referer") or "/")
    resp.status_code = exc.status_code
    return resp


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def blank(v: str | None):
    return v.strip() if v and v.strip() else None


def num(v: str | None, cast=float):
    v = blank(v)
    return cast(v) if v is not None else None


def _hsl_swatch(h, s):
    return [f"hsl({h} {s}% 42%)", f"hsl({h + 28} {s}% 55%)", f"hsl({h} {s}% 90%)"]


THEMES = {  # key: (label, swatch colours)
    "hearth": ("Hearth", ["#365D4B", "#C68556", "#A8B9A2", "#F5F1E8"]),
    "ember": ("Ember", _hsl_swatch(18, 62)), "forest": ("Forest", _hsl_swatch(152, 42)),
    "ocean": ("Ocean", _hsl_swatch(205, 68)), "plum": ("Plum", _hsl_swatch(275, 42)),
    "rose": ("Rose", _hsl_swatch(345, 58)), "slate": ("Slate", _hsl_swatch(215, 18)),
}
SECTIONS = {  # primary nav: key -> (label, icon, [(path, label, icon)])
    "home": ("Home", "home", []),
    "plan": ("Plan", "calendar-check", [("/tasks", "Tasks", "wrench"), ("/schedule", "Schedule", "calendar")]),
    "organize": ("Organize", "box", [("/items", "Inventory", "box"), ("/shopping", "Shopping", "cart"),
                                     ("/documents", "Documents", "file"), ("/gallery", "Photos", "images"),
                                     ("/contractors", "Contractors", "users")]),
    "finances": ("Finances", "wallet", [("/budget", "Budget", "wallet"),
                                        ("/budget?view=forecast", "Forecast", "chart")]),
}


def section_for(path: str) -> str | None:
    if path == "/":
        return "home"
    for key, (_, _, pages) in SECTIONS.items():
        if any(path.startswith(p.split("?")[0]) for p, _, _ in pages):
            return key
    return None
MODES = ("auto", "light", "dark")


def greeting() -> str:
    h = datetime.now().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"


def render(request: Request, name: str, **ctx):
    ctx["today"] = date.today().isoformat()
    t = request.cookies.get("theme")
    m = request.cookies.get("mode")
    ctx["theme"] = t if t in THEMES else "hearth"
    ctx["mode"] = m if m in MODES else "auto"
    ctx["themes"] = THEMES
    ctx["about"] = {"version": __version__, "developer": DEVELOPER,
                     "repo": REPO_URL}
    ctx["sections"] = SECTIONS
    v2 = get_setting("brand_v2") == "1"
    ctx["brand"] = {"v2": v2, "icon": "/static/brand/hearth-icon.svg" if v2 else "/static/icon.png",
                    "favicon": "/static/favicon.svg" if v2 else "/static/icon.png"}
    ctx["msg"] = request.query_params.get("msg")
    ctx["section"] = section_for(request.url.path)
    path, q = request.url.path, request.url.query
    full = path + (f"?{q}" if q else "")
    pages = SECTIONS[ctx["section"]][2] if ctx["section"] else []
    ctx["sub_active"] = next((p for p, _, _ in pages if "?" in p and full.startswith(p)), None) or \
        next((p for p, _, _ in pages if "?" not in p and path.startswith(p)), None)
    with get_db() as db:
        ctx["nav_counts"] = {
            "shopping": db.execute("SELECT COUNT(*) FROM shopping_items WHERE status='needed'").fetchone()[0],
            "overdue": db.execute("SELECT COUNT(*) FROM tasks WHERE next_due < ? AND completed_at IS NULL",
                                  (ctx["today"],)).fetchone()[0]}
    return templates.TemplateResponse(request, name, ctx)


replacement_year = finance.replacement_year


# ---------- dashboard ----------

@app.get("/")
def dashboard(request: Request):
    events = home.timeline()
    steps = home.setup_steps()
    with get_db() as db:
        recent = db.execute(
            "SELECT l.*, t.name AS task_name FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "ORDER BY done_on DESC, l.id DESC LIMIT 4").fetchall()
        shop = db.execute("SELECT name, list FROM shopping_items WHERE status='needed' "
                          "ORDER BY added_at DESC LIMIT 6").fetchall()
        n_items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    buckets: dict[str, list] = {}
    for e in events:
        buckets.setdefault(e["bucket"], []).append(e)
    return render(request, "dashboard.html", greeting=greeting(), buckets=buckets,
                  att=home.attention(events), steps=steps, show_setup=home.show_setup(steps),
                  steps_done=sum(s["done"] for s in steps), insights=home.insights(),
                  recent=recent, shop=shop, n_items=n_items)


@app.post("/setup/dismiss")
def setup_dismiss():
    set_setting("setup_dismissed", "1")
    return back("/")


@app.get("/search")
def search_page(request: Request, q: str = ""):
    groups = search_mod.search(q)
    return render(request, "search.html", q=q, groups=groups,
                  total=sum(len(g["rows"]) for g in groups))


# ---------- tasks ----------

TASK_FILTERS = [("today", "Today"), ("upcoming", "Upcoming"), ("overdue", "Overdue"),
                ("completed", "Completed"), ("all", "All tasks")]
TASK_SORTS = [("due", "Due date"), ("priority", "Priority"), ("room", "Room"), ("name", "Name")]


def toast_url(path: str, msg: str, act: str | None = None, act_href: str | None = None) -> str:
    from urllib.parse import urlencode
    q = {"msg": msg}
    if act and act_href:
        q.update(act=act, act_href=act_href)
    sep = "&" if "?" in path else "?"
    frag = ""
    if "#" in path:
        path, frag = path.split("#", 1)
        frag = "#" + frag
    return f"{path}{sep}{urlencode(q)}{frag}"


def safe_next(v: str | None, default: str) -> str:
    return v if v and v.startswith("/") and not v.startswith("//") else default


@app.get("/tasks")
def tasks_page(request: Request, show: str = "upcoming", sort: str = "due"):
    today = date.today()
    t_iso, week = today.isoformat(), (today + timedelta(days=30)).isoformat()
    with get_db() as db:
        active = db.execute(
            "SELECT t.*, i.name AS item_name, r.name AS room_name FROM tasks t "
            "LEFT JOIN items i ON i.id=t.item_id LEFT JOIN rooms r ON r.id=i.room_id "
            "WHERE t.completed_at IS NULL").fetchall()
        logs = db.execute(
            "SELECT l.*, t.name AS task_name, t.interval_days, c.name AS contractor_name, i.name AS item_name "
            "FROM task_logs l JOIN tasks t ON t.id=l.task_id LEFT JOIN contractors c ON c.id=l.contractor_id "
            "LEFT JOIN items i ON i.id=t.item_id ORDER BY l.done_on DESC, l.id DESC LIMIT 100").fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        n_all = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    groups = {
        "today": [t for t in active if t["next_due"] == t_iso],
        "overdue": [t for t in active if t["next_due"] < t_iso],
        "upcoming": [t for t in active if t["next_due"] <= week],  # overdue first, then next 30 days
        "all": list(active),
    }
    show = show if show in dict(TASK_FILTERS) else "upcoming"
    keyf = {"due": lambda t: (t["next_due"], -t["priority"]),
            "priority": lambda t: (-t["priority"], t["next_due"]),
            "room": lambda t: ((t["room_name"] or "~").lower(), t["next_due"]),
            "name": lambda t: t["name"].lower()}[sort if sort in dict(TASK_SORTS) else "due"]
    rows = sorted(groups.get(show, []), key=keyf)
    counts = {k: len(v) for k, v in groups.items()}
    counts["completed"] = sum(1 for l in logs if l["done_on"] >= (today - timedelta(days=90)).isoformat())
    return render(request, "tasks.html", rows=rows, logs=logs, show=show, sort=sort, filters=TASK_FILTERS,
                  sorts=TASK_SORTS, counts=counts, items=items, presets=plan.PRESETS, n_all=n_all,
                  today_d=today)


def _task_fields(form, fallback_interval: int | None = None):
    legacy = form.get("interval_days")  # older forms/clients post the raw day count
    interval = plan.parse_repeat(form.get("repeat"), form.get("repeat_custom"),
                                 int(legacy) if legacy not in (None, "") else fallback_interval)
    return dict(name=(form.get("name") or "").strip(), interval_days=interval,
                next_due=blank(form.get("next_due")) or date.today().isoformat(),
                item_id=num(form.get("item_id"), int), notes=blank(form.get("notes")),
                priority=1 if form.get("priority") else 0)


@app.post("/tasks")
async def task_create(request: Request, bg: BackgroundTasks):
    form = await request.form()
    f = _task_fields(form)
    if not f["name"]:
        raise HTTPException(400, "Give the task a name.")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO tasks (name, interval_days, next_due, item_id, notes, priority) VALUES (?,?,?,?,?,?)",
            (f["name"], f["interval_days"], f["next_due"], f["item_id"], f["notes"], f["priority"]))
    bg.add_task(gcal.sync_task, cur.lastrowid)
    nxt = safe_next(form.get("next"), "/tasks?show=all")
    return back(toast_url(nxt, f"Added “{f['name']}”"))


@app.post("/tasks/seed")
def task_seed(bg: BackgroundTasks):
    today = date.today()
    with get_db() as db:
        existing = {r[0] for r in db.execute("SELECT name FROM tasks")}
        new = [(n, d, (today + timedelta(days=min(d, 30))).isoformat())
               for n, d in DEFAULT_TASKS if n not in existing]
        db.executemany("INSERT INTO tasks (name, interval_days, next_due) VALUES (?,?,?)", new)
    bg.add_task(gcal.sync_all)
    return back(toast_url("/tasks?show=all", f"Added {len(new)} common home tasks"))


@app.post("/tasks/{task_id}/done")
def task_done(bg: BackgroundTasks, task_id: int, done_on: str = Form(None), cost: str = Form(None),
              contractor_id: str = Form(None), notes: str = Form(None), next: str = Form(None)):
    done = blank(done_on) or date.today().isoformat()
    with get_db() as db:
        t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise HTTPException(404)
        cur = db.execute(
            "INSERT INTO task_logs (task_id, done_on, cost, contractor_id, notes) VALUES (?,?,?,?,?)",
            (task_id, done, num(cost), num(contractor_id, int), blank(notes)))
        log_id = cur.lastrowid
        if t["interval_days"] > 0:
            nxt = date.fromisoformat(done) + timedelta(days=t["interval_days"])
            db.execute("UPDATE tasks SET next_due=?, last_notified=NULL WHERE id=?", (nxt.isoformat(), task_id))
            bg.add_task(gcal.sync_task, task_id)
        else:  # one-time task: complete it and take it off the calendar
            db.execute("UPDATE tasks SET completed_at=?, gcal_event_id=NULL WHERE id=?", (done, task_id))
            bg.add_task(gcal.delete_event, t["gcal_event_id"])
    return back(toast_url(safe_next(next, "/tasks"), f"Done: {t['name']}",
                          "Add details", f"/tasks/{task_id}?log={log_id}#log-details"))


@app.post("/tasks/{task_id}/reopen")
def task_reopen(bg: BackgroundTasks, task_id: int):
    with get_db() as db:
        db.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (task_id,))
    bg.add_task(gcal.sync_task, task_id)
    return back(toast_url(f"/tasks/{task_id}", "Task reopened"))


@app.post("/logs/{log_id}")
async def log_update(request: Request, bg: BackgroundTasks, log_id: int):
    """Add details after a quick 'Done': date, cost, who, notes, receipt."""
    form = await request.form()
    with get_db() as db:
        lg = db.execute("SELECT l.*, t.interval_days, t.item_id, t.name AS task_name, t.completed_at "
                        "FROM task_logs l JOIN tasks t ON t.id=l.task_id WHERE l.id=?", (log_id,)).fetchone()
        if not lg:
            raise HTTPException(404)
        done = blank(form.get("done_on")) or lg["done_on"]
        db.execute("UPDATE task_logs SET done_on=?, cost=?, contractor_id=?, notes=? WHERE id=?",
                   (done, num(form.get("cost")), num(form.get("contractor_id"), int),
                    blank(form.get("notes")), log_id))
        latest = db.execute("SELECT id FROM task_logs WHERE task_id=? ORDER BY done_on DESC, id DESC LIMIT 1",
                            (lg["task_id"],)).fetchone()[0]
        if done != lg["done_on"] and latest == log_id and lg["interval_days"] > 0:
            nxt = date.fromisoformat(done) + timedelta(days=lg["interval_days"])
            db.execute("UPDATE tasks SET next_due=? WHERE id=?", (nxt.isoformat(), lg["task_id"]))
            bg.add_task(gcal.sync_task, lg["task_id"])
        receipt = form.get("receipt")
        if receipt is not None and getattr(receipt, "filename", ""):
            stored = f"{uuid.uuid4().hex}{Path(receipt.filename).suffix.lower()[:10]}"
            with (data_dir() / "uploads" / stored).open("wb") as fh:
                fh.write(await receipt.read())
            db.execute("INSERT INTO documents (title, filename, stored_name, item_id, uploaded_at) VALUES (?,?,?,?,?)",
                       (f"Receipt: {lg['task_name']} ({done})", Path(receipt.filename).name, stored,
                        lg["item_id"], datetime.now().isoformat(timespec="seconds")))
    return back(toast_url(safe_next(form.get("next"), f"/tasks/{lg['task_id']}"), "Details saved"))


@app.post("/logs/{log_id}/delete")
def log_delete(bg: BackgroundTasks, log_id: int):
    """Remove a completion (e.g. tapped Done by mistake). Restores the previous due date."""
    with get_db() as db:
        lg = db.execute("SELECT l.*, t.interval_days, t.next_due FROM task_logs l JOIN tasks t ON t.id=l.task_id "
                        "WHERE l.id=?", (log_id,)).fetchone()
        if not lg:
            raise HTTPException(404)
        db.execute("DELETE FROM task_logs WHERE id=?", (log_id,))
        if lg["interval_days"] > 0 and lg["next_due"] == (
                date.fromisoformat(lg["done_on"]) + timedelta(days=lg["interval_days"])).isoformat():
            prev = db.execute("SELECT done_on FROM task_logs WHERE task_id=? ORDER BY done_on DESC LIMIT 1",
                              (lg["task_id"],)).fetchone()
            restored = (date.fromisoformat(prev[0]) + timedelta(days=lg["interval_days"])).isoformat() \
                if prev else lg["done_on"]
            db.execute("UPDATE tasks SET next_due=? WHERE id=?", (restored, lg["task_id"]))
        db.execute("UPDATE tasks SET completed_at=NULL WHERE id=? AND interval_days=0", (lg["task_id"],))
    bg.add_task(gcal.sync_task, lg["task_id"])
    return back(toast_url(f"/tasks/{lg['task_id']}", "Completion removed"))


@app.get("/tasks/{task_id}")
def task_detail(request: Request, task_id: int, log: int | None = None):
    with get_db() as db:
        t = db.execute("SELECT t.*, i.name AS item_name, i.room_id, r.name AS room_name FROM tasks t "
                       "LEFT JOIN items i ON i.id=t.item_id LEFT JOIN rooms r ON r.id=i.room_id WHERE t.id=?",
                       (task_id,)).fetchone()
        if not t:
            raise HTTPException(404)
        logs = db.execute(
            "SELECT l.*, c.name AS contractor_name FROM task_logs l "
            "LEFT JOIN contractors c ON c.id=l.contractor_id WHERE task_id=? ORDER BY done_on DESC, l.id DESC",
            (task_id,)).fetchall()
        items = db.execute("SELECT id, name FROM items ORDER BY name").fetchall()
        contractors = db.execute("SELECT id, name, trade FROM contractors ORDER BY name").fetchall()
        docs = db.execute("SELECT * FROM documents WHERE item_id=? ORDER BY uploaded_at DESC LIMIT 6",
                          (t["item_id"],)).fetchall() if t["item_id"] else []
    edit_log = next((l for l in logs if l["id"] == log), None)
    return render(request, "task_detail.html", t=t, logs=logs, items=items, contractors=contractors,
                  docs=docs, edit_log=edit_log, presets=plan.PRESETS, preset_days=plan.PRESET_DAYS,
                  spent=sum(l["cost"] or 0 for l in logs))


@app.post("/tasks/{task_id}")
async def task_update(request: Request, bg: BackgroundTasks, task_id: int):
    form = await request.form()
    with get_db() as db:
        cur = db.execute("SELECT interval_days FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not cur:
            raise HTTPException(404)
        f = _task_fields(form, cur[0])
        db.execute("UPDATE tasks SET name=?, interval_days=?, next_due=?, item_id=?, notes=?, priority=? WHERE id=?",
                   (f["name"], f["interval_days"], f["next_due"], f["item_id"], f["notes"], f["priority"], task_id))
    bg.add_task(gcal.sync_task, task_id)
    return back(toast_url(f"/tasks/{task_id}", "Saved"))


@app.post("/tasks/{task_id}/delete")
def task_delete(bg: BackgroundTasks, task_id: int):
    with get_db() as db:
        r = db.execute("SELECT gcal_event_id, name FROM tasks WHERE id=?", (task_id,)).fetchone()
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    if r and r[0]:
        bg.add_task(gcal.delete_event, r[0])
    return back(toast_url("/tasks", f"Deleted “{r[1]}”" if r else "Deleted"))


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
def schedule_page(request: Request, past: int = 0, kind: str = "appointment", item: int | None = None,
                  contractor: int | None = None):
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
                  past=past, gcal_on=gcal.configured() and gcal.enabled(),
                  new_kind="reminder" if kind == "reminder" else "appointment",
                  new_item=item, new_contractor=contractor)


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
            db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, category) VALUES (?,?,?,?,?)",
                       (ts[:10], "expense", amt,
                        f"{shopping.LISTS[lk]}{where} ({n} item{'s' if n != 1 else ''})",
                        "Supplies" if lk == "supplies" else "Groceries"))
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
               "warranty_until", "life_years", "replace_cost", "notes", "category"]
ROOM_ART = [("kitchen", "kitchen"), ("pantry", "kitchen"), ("dining", "kitchen"), ("living", "living"),
            ("family", "living"), ("lounge", "living"), ("den", "living"), ("office", "living"),
            ("bed", "bedroom"), ("nursery", "bedroom"), ("guest", "bedroom"), ("garage", "garage"),
            ("shed", "garage"), ("workshop", "garage"), ("bath", "bathroom"), ("toilet", "bathroom"),
            ("powder", "bathroom"), ("laundry", "utility"), ("utility", "utility"), ("basement", "utility"),
            ("mechanical", "utility"), ("attic", "utility"), ("outdoor", "home"), ("yard", "home")]


def room_art(name: str | None) -> str:
    n = (name or "").lower()
    return next((art for key, art in ROOM_ART if key in n), "home")


templates.env.filters["room_art"] = room_art


def resolve_room(db, form) -> int | None:
    """Room select supports '__new' + a typed name, so rooms can be created inline."""
    rid = form.get("room_id")
    if rid == "__new":
        name = blank(form.get("new_room"))
        if not name:
            return None
        db.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (name,))
        return db.execute("SELECT id FROM rooms WHERE name=?", (name,)).fetchone()[0]
    return num(rid, int)


def item_values(form, room_id=None) -> list:
    return [form.get("name", "").strip(), room_id if room_id is not None else num(form.get("room_id"), int),
            blank(form.get("brand")), blank(form.get("model")), blank(form.get("serial")),
            blank(form.get("purchase_date")), num(form.get("price")),
            blank(form.get("warranty_until")), num(form.get("life_years"), int),
            num(form.get("replace_cost")), blank(form.get("notes")), blank(form.get("category"))]


WARRANTY_FILTERS = [("active", "Under warranty"), ("expiring", "Expiring ≤ 60 days"),
                    ("expired", "Expired"), ("none", "No warranty info")]


@app.get("/items")
def items_page(request: Request, room: int | None = None, q: str = "", category: str = "",
               warranty: str = "", view: str = "grid"):
    today = date.today()
    soon = (today + timedelta(days=60)).isoformat()
    where, args = [], []
    if room:
        where.append("i.room_id=?"); args.append(room)
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(i.name LIKE ? OR i.brand LIKE ? OR i.model LIKE ? OR i.serial LIKE ? OR i.notes LIKE ?)")
        args += [like] * 5
    if category == "__none":
        where.append("i.category IS NULL")
    elif category:
        where.append("i.category=?")
        args.append(category)
    if warranty == "active":
        where.append("i.warranty_until > ?"); args.append(soon)
    elif warranty == "expiring":
        where.append("i.warranty_until BETWEEN ? AND ?"); args += [today.isoformat(), soon]
    elif warranty == "expired":
        where.append("i.warranty_until < ?"); args.append(today.isoformat())
    elif warranty == "none":
        where.append("i.warranty_until IS NULL")
    with get_db() as db:
        rooms = db.execute(
            "SELECT r.*, COUNT(i.id) AS n, COALESCE(SUM(i.price),0) AS value, "
            "(SELECT id FROM photos p WHERE p.room_id=r.id ORDER BY is_cover DESC, id LIMIT 1) AS cover "
            "FROM rooms r LEFT JOIN items i ON i.room_id=r.id GROUP BY r.id ORDER BY r.name").fetchall()
        items = db.execute(
            "SELECT i.*, r.name AS room_name, "
            "(SELECT id FROM photos p WHERE p.item_id=i.id ORDER BY is_cover DESC, id LIMIT 1) AS cover "
            "FROM items i LEFT JOIN rooms r ON r.id=i.room_id"
            + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.name, i.name", args).fetchall()
        n_all = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        unassigned = db.execute("SELECT COUNT(*) FROM items WHERE room_id IS NULL").fetchone()[0]
        cats = [r[0] for r in db.execute("SELECT DISTINCT category FROM items WHERE category IS NOT NULL")]
    current = next((r for r in rooms if r["id"] == room), None)
    filtered = bool(q.strip() or category or warranty)
    return render(request, "items.html", rooms=rooms, items=items, room=room, current=current,
                  total=sum(i["price"] or 0 for i in items), q=q, category=category, warranty=warranty,
                  view="list" if view == "list" else "grid", n_all=n_all, unassigned=unassigned,
                  categories=sorted(set(plan.ITEM_CATEGORIES) | set(cats)), filtered=filtered,
                  warranty_filters=WARRANTY_FILTERS, soon=soon)


@app.post("/rooms")
def room_create(name: str = Form(...)):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (name.strip(),))
        rid = db.execute("SELECT id FROM rooms WHERE name=?", (name.strip(),)).fetchone()[0]
    return back(toast_url(f"/items?room={rid}", f"Added room “{name.strip()}”"))


@app.post("/rooms/{room_id}")
def room_rename(room_id: int, name: str = Form(...)):
    with get_db() as db:
        db.execute("UPDATE rooms SET name=? WHERE id=?", (name.strip(), room_id))
    return back(toast_url(f"/items?room={room_id}", "Room renamed"))


@app.post("/rooms/{room_id}/delete")
def room_delete(room_id: int):
    with get_db() as db:
        purge_photos(db, "room_id", room_id)
        db.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    return back(toast_url("/items", "Room deleted. Its items were kept."))


@app.post("/items")
async def item_create(request: Request):
    form = await request.form()
    if not form.get("name", "").strip():
        raise HTTPException(400, "Give the item a name.")
    with get_db() as db:
        rid = resolve_room(db, form)
        cur = db.execute(
            f"INSERT INTO items ({','.join(ITEM_FIELDS)}) VALUES ({','.join('?' * len(ITEM_FIELDS))})",
            item_values(form, rid))
        iid = cur.lastrowid
        photo = form.get("photo")
        if photo is not None and getattr(photo, "filename", ""):
            a, b = save_image(await photo.read())
            db.execute("INSERT INTO photos (item_id, stored_name, thumb_name, is_cover, created_at) VALUES (?,?,?,1,?)",
                       (iid, a, b, datetime.now().isoformat(timespec="seconds")))
    return back(toast_url(f"/items/{iid}", f"Added “{form['name'].strip()}”"))


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
            "SELECT l.done_on AS d, t.name AS title, l.cost, c.id AS contractor_id, c.name AS contractor, "
            "'task' AS src, t.id AS ref FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "LEFT JOIN contractors c ON c.id=l.contractor_id WHERE t.item_id=? "
            "UNION ALL SELECT entry_date, COALESCE(note,'Expense'), amount, NULL, NULL, 'expense', id "
            "FROM fund_entries WHERE item_id=? AND kind='expense' "
            "UNION ALL SELECT substr(a.starts_at,1,10), a.title, NULL, c.id, c.name, 'appt', a.id "
            "FROM appointments a LEFT JOIN contractors c ON c.id=a.contractor_id "
            "WHERE a.item_id=? AND substr(a.starts_at,1,10) < ? "
            "ORDER BY 1 DESC", (item_id, item_id, item_id, date.today().isoformat())).fetchall()
        upcoming = db.execute(
            "SELECT a.*, c.name AS contractor_name FROM appointments a "
            "LEFT JOIN contractors c ON c.id=a.contractor_id WHERE a.item_id=? "
            "AND substr(a.starts_at,1,10) >= ? ORDER BY starts_at", (item_id, date.today().isoformat())).fetchall()
        contractors = db.execute(
            "SELECT c.*, COUNT(*) AS jobs, MAX(d) AS last FROM ("
            "  SELECT l.contractor_id AS cid, l.done_on AS d FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "  WHERE t.item_id=? AND l.contractor_id IS NOT NULL "
            "  UNION ALL SELECT contractor_id, substr(starts_at,1,10) FROM appointments "
            "  WHERE item_id=? AND contractor_id IS NOT NULL) x JOIN contractors c ON c.id=x.cid "
            "GROUP BY c.id ORDER BY last DESC", (item_id, item_id)).fetchall()
        supplies = db.execute("SELECT * FROM shopping_items WHERE item_id=? AND status!='bought' "
                              "ORDER BY added_at", (item_id,)).fetchall()
        all_contractors = db.execute("SELECT id, name, trade FROM contractors ORDER BY name").fetchall()
    spent = sum(h["cost"] or 0 for h in history)
    w_days = (date.fromisoformat(item["warranty_until"]) - date.today()).days \
        if item["warranty_until"] else None
    return render(request, "item_detail.html", item=item, rooms=rooms, docs=docs, tasks=tasks,
                  categories=plan.ITEM_CATEGORIES, presets=plan.PRESETS,
                  history=history, photos=photos, upcoming=upcoming, contractors=contractors,
                  supplies=supplies, spent=spent, w_days=w_days, all_contractors=all_contractors,
                  replace_year=replacement_year(item))


@app.post("/items/{item_id}")
async def item_update(request: Request, item_id: int):
    form = await request.form()
    with get_db() as db:
        rid = resolve_room(db, form)
        db.execute(
            f"UPDATE items SET {','.join(f + '=?' for f in ITEM_FIELDS)} WHERE id=?",
            item_values(form, rid) + [item_id])
    return back(toast_url(f"/items/{item_id}", "Saved"))


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
        first = None
        for a, b in saved:
            cur = db.execute(
                "INSERT INTO photos (item_id, room_id, stored_name, thumb_name, caption, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (iid, rid, a, b, blank(caption), datetime.now().isoformat(timespec="seconds")))
            first = first or cur.lastrowid
    if next == "photo" and first:
        return back(f"/gallery/{first}?new=1")
    return back(next if next.startswith("/") else "/gallery")


@app.post("/gallery/{photo_id}/to-item")
def photo_to_item(photo_id: int, name: str = Form(...), room_id: str = Form(None)):
    """Camera-first: turn a captured photo into a new inventory item."""
    with get_db() as db:
        p = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not p:
            raise HTTPException(404)
        cur = db.execute("INSERT INTO items (name, room_id) VALUES (?,?)",
                         (name.strip(), num(room_id, int)))
        db.execute("UPDATE photos SET item_id=?, room_id=NULL, is_cover=1 WHERE id=?",
                   (cur.lastrowid, photo_id))
    return back(f"/items/{cur.lastrowid}?new=1")


@app.post("/gallery/{photo_id}/to-receipt")
def photo_to_receipt(photo_id: int, title: str = Form(None), item_id: str = Form(None)):
    """Camera-first: file a captured photo as a receipt/document (optionally on an item)."""
    with get_db() as db:
        p = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not p:
            raise HTTPException(404)
        up = data_dir() / "uploads"
        stored = f"{uuid.uuid4().hex}.jpg"
        shutil.copyfile(up / p["stored_name"], up / stored)
        iid = num(item_id, int)
        db.execute("INSERT INTO documents (title, filename, stored_name, item_id, uploaded_at) "
                   "VALUES (?,?,?,?,?)",
                   (blank(title) or f"Receipt {date.today().isoformat()}", f"receipt-{photo_id}.jpg",
                    stored, iid, datetime.now().isoformat(timespec="seconds")))
        for n in (p["stored_name"], p["thumb_name"]):
            (up / n).unlink(missing_ok=True)
        db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    return back(f"/items/{iid}" if iid else "/documents")


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


@app.get("/contractors/{cid}")
def contractor_detail(request: Request, cid: int):
    with get_db() as db:
        c = db.execute("SELECT * FROM contractors WHERE id=?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        jobs = db.execute(
            "SELECT l.done_on AS d, t.name AS title, l.cost, l.notes, i.id AS item_id, i.name AS item_name, "
            "t.id AS task_id FROM task_logs l JOIN tasks t ON t.id=l.task_id "
            "LEFT JOIN items i ON i.id=t.item_id WHERE l.contractor_id=? ORDER BY l.done_on DESC",
            (cid,)).fetchall()
        appts = db.execute(
            "SELECT a.*, i.name AS item_name FROM appointments a LEFT JOIN items i ON i.id=a.item_id "
            "WHERE contractor_id=? ORDER BY starts_at DESC", (cid,)).fetchall()
    today = date.today().isoformat()
    return render(request, "contractor.html", c=c, jobs=jobs,
                  upcoming=[a for a in appts if a["starts_at"][:10] >= today][::-1],
                  past=[a for a in appts if a["starts_at"][:10] < today],
                  spent=sum(j["cost"] or 0 for j in jobs),
                  items={(j["item_id"], j["item_name"]) for j in jobs if j["item_id"]}
                  | {(a["item_id"], a["item_name"]) for a in appts if a["item_id"]})


@app.post("/contractors/{cid}")
def contractor_update(cid: int, name: str = Form(...), trade: str = Form(None), phone: str = Form(None),
                      email: str = Form(None), notes: str = Form(None)):
    with get_db() as db:
        db.execute("UPDATE contractors SET name=?, trade=?, phone=?, email=?, notes=? WHERE id=?",
                   (name.strip(), blank(trade), blank(phone), blank(email), blank(notes), cid))
    return back(f"/contractors/{cid}")


@app.post("/contractors/{cid}/delete")
def contractor_delete(cid: int):
    with get_db() as db:
        db.execute("DELETE FROM contractors WHERE id=?", (cid,))
    return back("/contractors")


# ---------- budget ----------

balance_data = finance.balance_data      # kept importable from main (tests, older callers)
forecast_data = finance.forecast_data


@app.get("/budget")
def budget_page(request: Request, view: str = "balance", year: int | None = None, period: int = 5,
                q: str = "", kind: str = "", category: str = "", sort: str = "new", edit: int | None = None):
    if view == "forecast":
        return render(request, "forecast.html", **finance.forecast_data(period))
    data = finance.balance_data(year or date.today().year, q, kind, category, sort)
    fc = finance.forecast_data(5)
    edit_entry = None
    if edit:
        with get_db() as db:
            edit_entry = db.execute("SELECT * FROM fund_entries WHERE id=?", (edit,)).fetchone()
    return render(request, "budget.html", view="balance", categories=finance.expense_categories(),
                  fc_monthly=fc["monthly"], fc_total=fc["total"], edit_entry=edit_entry, **data)


@app.post("/budget/year")
def budget_set(year: int = Form(...), amount: str = Form(None)):
    with get_db() as db:
        if num(amount) is None:
            db.execute("DELETE FROM budgets WHERE year=?", (year,))
        else:
            db.execute("INSERT INTO budgets (year, amount) VALUES (?,?) "
                       "ON CONFLICT(year) DO UPDATE SET amount=excluded.amount", (year, num(amount)))
    return back(toast_url(f"/budget?year={year}", f"{year} budget saved"))


async def _save_receipt(upload, title: str, item_id: int | None) -> int | None:
    if upload is None or not getattr(upload, "filename", ""):
        return None
    stored = f"{uuid.uuid4().hex}{Path(upload.filename).suffix.lower()[:10]}"
    with (data_dir() / "uploads" / stored).open("wb") as fh:
        fh.write(await upload.read())
    with get_db() as db:
        return db.execute("INSERT INTO documents (title, filename, stored_name, item_id, uploaded_at) "
                          "VALUES (?,?,?,?,?)", (title, Path(upload.filename).name, stored, item_id,
                                                 datetime.now().isoformat(timespec="seconds"))).lastrowid


def _entry_fields(form):
    kind = form.get("kind")
    try:
        amount = float(form.get("amount") or 0)
    except ValueError:
        amount = 0
    if kind not in ("allocation", "expense") or amount <= 0:
        raise HTTPException(400, "Enter an amount greater than zero.")
    return dict(kind=kind, amount=amount, d=blank(form.get("entry_date")) or date.today().isoformat(),
                note=blank(form.get("note")), item_id=num(form.get("item_id"), int),
                category=blank(form.get("category")) if kind == "expense" else None)


@app.post("/budget/entries")
async def budget_entry(request: Request):
    form = await request.form()
    f = _entry_fields(form)
    doc = await _save_receipt(form.get("receipt"), f"Receipt: {f['note'] or f['category'] or 'expense'} ({f['d']})",
                              f["item_id"])
    with get_db() as db:
        db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, item_id, category, document_id) "
                   "VALUES (?,?,?,?,?,?,?)", (f["d"], f["kind"], f["amount"], f["note"], f["item_id"],
                                              f["category"], doc))
    label = "Funds added" if f["kind"] == "allocation" else "Expense recorded"
    return back(toast_url(safe_next(form.get("next"), f"/budget?year={f['d'][:4]}"), f"{label}: ${f['amount']:,.2f}"))


@app.post("/budget/entries/{entry_id}")
async def budget_entry_update(request: Request, entry_id: int):
    form = await request.form()
    f = _entry_fields(form)
    doc = await _save_receipt(form.get("receipt"), f"Receipt: {f['note'] or 'expense'} ({f['d']})", f["item_id"])
    with get_db() as db:
        db.execute("UPDATE fund_entries SET entry_date=?, kind=?, amount=?, note=?, item_id=?, category=?, "
                   "document_id=COALESCE(?, document_id) WHERE id=?",
                   (f["d"], f["kind"], f["amount"], f["note"], f["item_id"], f["category"], doc, entry_id))
    return back(toast_url(f"/budget?year={f['d'][:4]}", "Entry updated"))


@app.post("/budget/entries/{entry_id}/delete")
def budget_entry_delete(entry_id: int):
    with get_db() as db:
        r = db.execute("SELECT entry_date FROM fund_entries WHERE id=?", (entry_id,)).fetchone()
        db.execute("DELETE FROM fund_entries WHERE id=?", (entry_id,))
    return back(toast_url(f"/budget?year={r[0][:4]}" if r else "/budget", "Entry deleted"))


@app.post("/settings/brand")
def settings_brand(use: str = Form("0")):
    set_setting("brand_v2", "1" if use == "1" else "0")
    return back(toast_url("/settings#brand", "New Hearth logo turned on" if use == "1" else "Original logo restored"))


@app.post("/settings/categories")
def settings_categories(categories: str = Form("")):
    cats = [c.strip() for c in categories.splitlines() if c.strip()]
    set_setting("expense_categories", "\n".join(dict.fromkeys(cats)) or None)
    return back(toast_url("/settings#finances", "Expense categories saved"))


# ---------- settings ----------

@app.get("/settings")
def settings_page(request: Request, sent: str | None = None, g: str | None = None,
                  gmsg: str | None = None, imp: str | None = None, imsg: str | None = None):
    google = dict(
        configured=gcal.configured(), key=gcal.key_path().is_file(),
        email=gcal.service_account_email(), calendar_id=gcal.calendar_id() or "",
        calendar_env=bool(os.environ.get("GOOGLE_CALENDAR_ID")),
        timezone=gcal.timezone(), task_remind=gcal.task_remind_minutes(),
        enabled=gcal.enabled(), public_url=get_setting("public_url") or "",
        last_sync=get_setting("gcal_last_sync"), last_error=get_setting("gcal_last_error"))
    return render(request, "settings.html", targets=len(notify.apprise_urls()), sent=sent,
                  lead=os.environ.get("NOTIFY_LEAD_DAYS", "3"), data=str(data_dir()),
                  google=google, g=g, gmsg=gmsg, imp=imp, imsg=imsg,
                  expense_categories=finance.expense_categories())


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


@app.get("/settings/backup.zip")
def settings_backup(bg: BackgroundTasks):
    path, name = backup.backup_zip()
    bg.add_task(shutil.rmtree, path.parent, True)
    return FileResponse(path, filename=name, media_type="application/zip")


@app.get("/export/{kind}.csv")
def export_csv(kind: str):
    if kind not in backup.EXPORTS:
        raise HTTPException(404)
    return Response(backup.export_csv(kind), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="hearth-{kind}.csv"'})


@app.post("/settings/import/items")
async def settings_import(file: UploadFile = File(...)):
    from urllib.parse import quote_plus
    added, skipped, errors = backup.import_items(await file.read())
    msg = f"Imported {added} item{'s' if added != 1 else ''}"
    if skipped:
        msg += f", skipped {skipped} already in inventory"
    if errors:
        msg += ". " + "; ".join(errors[:3])
    return back(f"/settings?imp={'err' if errors and not added else 'ok'}&imsg={quote_plus(msg)}#backup")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}
