"""Field catalogue shared by search filters and formulas, plus user-defined custom fields.

Every kind of record exposes named fields (price, room, warranty, upkeep, cost_per_mile, …). Search uses
them as filters (`price>500 room:kitchen`), formulas use them as variables (`= price / life`).
Items, vehicles and projects can also have custom fields: plain inputs (number, money, text, date) or
formulas computed from other fields. Formulas may use other formulas; cycles are rejected at save time.
"""
import re
from datetime import date

from . import formula as F
from .db import get_db

# field: (label, type). Types: text, number, money, date, years, days, percent
# Each kind: label, icon, from-clause, id/title/sub/href SQL, text columns for full-text search, fields (key → sql)
KINDS: dict[str, dict] = {
    "items": dict(
        label="Inventory", icon="box", custom=True, frm="FROM items i LEFT JOIN rooms r ON r.id=i.room_id",
        id="i.id", title="i.name", href="'/items/' || i.id",
        sub="trim(COALESCE(i.brand,'') || ' ' || COALESCE(i.model,'') || "
            "CASE WHEN r.name IS NOT NULL THEN ' · ' || r.name ELSE '' END)",
        text=["i.name", "i.brand", "i.model", "i.serial", "i.notes", "r.name", "i.category", "i.barcode"],
        fields={
            "name": ("Name", "text", "i.name"), "room": ("Room", "text", "r.name"),
            "brand": ("Brand", "text", "i.brand"), "model": ("Model", "text", "i.model"),
            "serial": ("Serial", "text", "i.serial"), "category": ("Category", "text", "i.category"),
            "price": ("Price paid", "money", "i.price"), "replace_cost": ("Replacement cost", "money", "i.replace_cost"),
            "life": ("Lifespan (years)", "years", "i.life_years"), "purchased": ("Purchased", "date", "i.purchase_date"),
            "warranty": ("Warranty until", "date", "i.warranty_until"),
            "upkeep": ("Upkeep spent", "money",
                       "(SELECT COALESCE(SUM(l.cost),0) FROM task_logs l JOIN tasks t ON t.id=l.task_id "
                       "WHERE t.item_id=i.id) + (SELECT COALESCE(SUM(amount),0) FROM fund_entries "
                       "WHERE item_id=i.id AND kind='expense')"),
        },
        derived={"age": ("Age (years)", "years"), "total_cost": ("Price + upkeep", "money"),
                 "warranty_days": ("Warranty days left", "days"), "replace_year": ("Replace around", "number")}),
    "tasks": dict(
        label="Tasks", icon="wrench", frm="FROM tasks t LEFT JOIN items it ON it.id=t.item_id",
        id="t.id", title="t.name", href="'/tasks/' || t.id", sub="'Next due ' || t.next_due",
        text=["t.name", "t.notes", "it.name"],
        fields={"name": ("Name", "text", "t.name"), "due": ("Next due", "date", "t.next_due"),
                "every": ("Repeats every (days)", "days", "t.interval_days"), "kind": ("Kind", "text", "t.kind"),
                "item": ("Item", "text", "it.name"), "priority": ("Priority", "number", "t.priority"),
                "done": ("Completed", "date", "t.completed_at")}),
    "schedule": dict(
        label="Schedule", icon="calendar", frm="FROM appointments a", id="a.id", title="a.title",
        href="'/schedule/' || a.id",
        sub="replace(substr(a.starts_at,1,16),'T',' ') || COALESCE(' · ' || a.location, '')",
        text=["a.title", "a.notes", "a.location"],
        fields={"title": ("Title", "text", "a.title"), "date": ("Date", "date", "substr(a.starts_at,1,10)"),
                "location": ("Location", "text", "a.location"), "kind": ("Kind", "text", "a.kind")}),
    "documents": dict(
        label="Documents", icon="file", frm="FROM documents d LEFT JOIN items it ON it.id=d.item_id",
        id="d.id", title="d.title", href="'/documents/' || d.id || '/file'", sub="d.filename",
        text=["d.title", "d.filename", "it.name"],
        fields={"title": ("Title", "text", "d.title"), "uploaded": ("Uploaded", "date", "substr(d.uploaded_at,1,10)"),
                "item": ("Item", "text", "it.name")}),
    "contractors": dict(
        label="Contractors", icon="users", frm="FROM contractors c", id="c.id", title="c.name",
        href="'/contractors/' || c.id", sub="trim(COALESCE(c.trade,'') || ' ' || COALESCE(c.phone,''))",
        text=["c.name", "c.trade", "c.phone", "c.email", "c.notes"],
        fields={"name": ("Name", "text", "c.name"), "trade": ("Trade", "text", "c.trade"),
                "phone": ("Phone", "text", "c.phone"), "email": ("Email", "text", "c.email")}),
    "shopping": dict(
        label="Shopping", icon="cart", frm="FROM shopping_items s", where="s.status != 'bought'",
        id="s.id", title="s.name", href="'/shopping?list=' || s.list",
        sub="s.list || COALESCE(' · ' || s.qty, '')", text=["s.name", "s.note", "s.store", "s.category"],
        fields={"name": ("Name", "text", "s.name"), "list": ("List", "text", "s.list"),
                "store": ("Store", "text", "s.store"), "price": ("Price", "money", "s.price"),
                "category": ("Category", "text", "s.category")}),
    "pantry": dict(
        label="Pantry", icon="box", frm="FROM pantry_items p", id="p.id", title="p.name", href="'/pantry'",
        sub="trim(COALESCE(p.qty || ' ' || COALESCE(p.unit,''), '') || COALESCE(' · ' || p.category, ''))",
        text=["p.name", "p.category", "p.notes", "p.barcode"],
        fields={"name": ("Name", "text", "p.name"), "qty": ("Quantity", "number", "p.qty"),
                "min": ("Minimum", "number", "p.min_qty"), "category": ("Category", "text", "p.category"),
                "best_before": ("Best before", "date", "p.best_before")}),
    "vehicles": dict(
        label="Vehicles", icon="car", custom=True, frm="FROM vehicles v", id="v.id", title="v.name",
        href="'/vehicles/' || v.id",
        sub="trim(COALESCE(v.year || ' ', '') || COALESCE(v.make,'') || ' ' || COALESCE(v.model,''))",
        text=["v.name", "v.make", "v.model", "v.vin", "v.plate", "v.notes"],
        fields={"name": ("Name", "text", "v.name"), "make": ("Make", "text", "v.make"),
                "model": ("Model", "text", "v.model"), "year": ("Year", "number", "v.year"),
                "plate": ("Plate", "text", "v.plate"), "vin": ("VIN", "text", "v.vin"),
                "price": ("Price paid", "money", "v.price"), "purchased": ("Purchased", "date", "v.purchase_date")},
        derived={"odometer": ("Odometer", "number"), "miles": ("Miles driven", "number"),
                 "spent": ("Spent", "money"), "cost_per_mile": ("Cost per mile", "money"), "age": ("Age (years)", "years")}),
    "projects": dict(
        label="Projects", icon="star", custom=True, frm="FROM projects pr", id="pr.id", title="pr.name",
        href="'/projects/' || pr.id", sub="pr.status || ' · ' || pr.kind", text=["pr.name", "pr.notes", "pr.kind"],
        fields={"name": ("Name", "text", "pr.name"), "kind": ("Kind", "text", "pr.kind"),
                "status": ("Status", "text", "pr.status"), "budget": ("Budget", "money", "pr.budget"),
                "started": ("Started", "date", "pr.started_on"), "finished": ("Finished", "date", "pr.finished_on")},
        derived={"spent": ("Spent", "money"), "planned": ("Materials planned", "money"),
                 "remaining": ("Budget left", "money")}),
    "photos": dict(
        label="Photos", icon="camera", frm="FROM photos ph", id="ph.id",
        title="COALESCE(ph.caption, 'Photo ' || substr(ph.created_at,1,10))", href="'/gallery/' || ph.id",
        sub="substr(ph.created_at,1,10)", text=["ph.caption"],
        fields={"caption": ("Caption", "text", "ph.caption"), "date": ("Taken", "date", "substr(ph.created_at,1,10)")}),
    "budget": dict(
        label="Budget", icon="wallet", frm="FROM fund_entries fe", id="fe.id", title="COALESCE(fe.note, fe.kind)",
        href="'/budget?year=' || substr(fe.entry_date,1,4)",
        sub="fe.entry_date || ' · $' || printf('%.2f', fe.amount)", text=["fe.note", "fe.category"],
        fields={"note": ("Note", "text", "fe.note"), "amount": ("Amount", "money", "fe.amount"),
                "date": ("Date", "date", "fe.entry_date"), "kind": ("Kind", "text", "fe.kind"),
                "category": ("Category", "text", "fe.category")}),
}
CUSTOM_KINDS = [k for k, v in KINDS.items() if v.get("custom")]
INPUT_TYPES = {"number": "Number", "money": "Money", "text": "Text", "date": "Date"}
FORMATS = {"number": "Number", "money": "Money", "percent": "Percent", "days": "Days", "years": "Years",
           "date": "Date", "text": "Text"}
RESERVED = set(F.FUNCS) | {"if", "and", "or", "not", "true", "false", "in", "type", "sort", "id"}


def _years_since(d):
    try:
        return max((date.today() - date.fromisoformat(d[:10])).days / 365.25, 0) if d else None
    except ValueError:
        return None


def _derive(kind: str, db, row: dict) -> dict:
    """Values that need Python (re-using the same maths as the detail pages)."""
    if kind == "items":
        age = _years_since(row.get("purchased"))
        wd = None
        if row.get("warranty"):
            try:
                wd = (date.fromisoformat(row["warranty"][:10]) - date.today()).days
            except ValueError:
                pass
        ry = None
        if row.get("purchased") and row.get("life"):
            try:
                ry = int(row["purchased"][:4]) + int(row["life"])
            except ValueError:
                pass
        return {"age": age, "total_cost": (row.get("price") or 0) + (row.get("upkeep") or 0),
                "warranty_days": wd, "replace_year": ry}
    if kind == "vehicles":
        from .modules import vehicle_summary
        v = db.execute("SELECT * FROM vehicles WHERE id=?", (row["id"],)).fetchone()
        s = vehicle_summary(db, v)
        return {"odometer": s["odometer"], "miles": s["miles"], "spent": s["spent"], "cost_per_mile": s["cpm"],
                "age": _years_since(row.get("purchased"))}
    if kind == "projects":
        from .modules import project_summary
        p = db.execute("SELECT * FROM projects WHERE id=?", (row["id"],)).fetchone()
        s = project_summary(db, p)
        return {"spent": s["spent"], "planned": s["planned"],
                "remaining": (row["budget"] - s["spent"]) if row.get("budget") is not None else None}
    return {}


# ---------------- custom field definitions ----------------

def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    if s and s[0].isdigit():
        s = "f_" + s
    return s[:40]


def custom_fields(kind: str, db=None) -> list[dict]:
    if kind not in CUSTOM_KINDS:
        return []
    if db is None:
        with get_db() as db2:
            return custom_fields(kind, db2)
    return [dict(r) for r in db.execute("SELECT * FROM custom_fields WHERE entity=? ORDER BY position, id", (kind,))]


def catalog(kind: str, db=None) -> dict[str, dict]:
    """key → {label, type, source: builtin|custom|formula, formula?}"""
    k = KINDS[kind]
    out = {key: {"label": lbl, "type": t, "source": "builtin"} for key, (lbl, t, _) in k["fields"].items()}
    out.update({key: {"label": lbl, "type": t, "source": "builtin"} for key, (lbl, t) in k.get("derived", {}).items()})
    for f in custom_fields(kind, db):
        out[f["key"]] = {"label": f["label"], "type": f["format"] if f["kind"] == "formula" else f["kind"],
                         "source": "formula" if f["kind"] == "formula" else "custom", "formula": f["formula"],
                         "id": f["id"]}
    return out


def _order(fields: list[dict]) -> list[dict]:
    """Formula fields in dependency order. Raises FormulaError on a cycle."""
    by_key = {f["key"]: f for f in fields if f["kind"] == "formula"}
    deps = {}
    for k, f in by_key.items():
        try:
            deps[k] = F.names(f["formula"]) & set(by_key)
        except F.FormulaError:
            deps[k] = set()
    out, state = [], {}

    def visit(k, path):
        if state.get(k) == 2:
            return
        if state.get(k) == 1:
            raise F.FormulaError("Circular formula: " + " → ".join(path + [k]))
        state[k] = 1
        for d in sorted(deps[k]):
            visit(d, path + [k])
        state[k] = 2
        out.append(by_key[k])

    for k in by_key:
        visit(k, [])
    return out


def validate_formula(kind: str, src: str, key: str | None = None) -> None:
    F.parse(src)
    cat = catalog(kind)
    unknown = sorted(n for n in F.names(src) if n not in cat or n == key)
    if key and key in F.names(src):
        raise F.FormulaError("A formula can't use itself")
    if unknown:
        raise F.FormulaError(f"Unknown field “{unknown[0]}”. Click a field below to insert its name.")
    fields = custom_fields(kind)
    fields = [f for f in fields if f["key"] != key] + [{"key": key or "__new__", "kind": "formula", "formula": src}]
    _order(fields)


def add_field(kind: str, label: str, ftype: str, fmt: str = "number", src: str = "") -> dict:
    if kind not in CUSTOM_KINDS:
        raise ValueError("Custom fields work on inventory, vehicles and projects")
    label = (label or "").strip()
    key = slug(label)
    if not key:
        raise ValueError("Give the field a name")
    if key in RESERVED or key in catalog(kind):
        raise ValueError(f"“{key}” is already a field name; pick another")
    if ftype not in INPUT_TYPES and ftype != "formula":
        raise ValueError("Unknown field type")
    if ftype == "formula":
        validate_formula(kind, src, key)
        fmt = fmt if fmt in FORMATS else "number"
    else:
        fmt, src = ftype, None
    with get_db() as db:
        pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM custom_fields WHERE entity=?", (kind,)).fetchone()[0]
        fid = db.execute("INSERT INTO custom_fields (entity, key, label, kind, format, formula, position, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (kind, key, label, ftype, fmt, src, pos,
                                                      date.today().isoformat())).lastrowid
        return dict(db.execute("SELECT * FROM custom_fields WHERE id=?", (fid,)).fetchone())


def get_field(fid: int) -> dict | None:
    with get_db() as db:
        r = db.execute("SELECT * FROM custom_fields WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None


def update_field(fid: int, label: str, fmt: str | None = None, src: str | None = None) -> dict:
    f = get_field(fid)
    if not f:
        raise ValueError("No such field")
    label = (label or "").strip() or f["label"]
    if f["kind"] == "formula":
        validate_formula(f["entity"], src or "", f["key"])
        fmt = fmt if fmt in FORMATS else f["format"]
    else:
        src, fmt = None, f["format"]
    with get_db() as db:
        db.execute("UPDATE custom_fields SET label=?, format=?, formula=? WHERE id=?", (label, fmt, src, fid))
    return get_field(fid)


def delete_field(fid: int) -> None:
    f = get_field(fid)
    if not f:
        return
    users = [g["label"] for g in custom_fields(f["entity"])
             if g["kind"] == "formula" and g["id"] != fid and f["key"] in _safe_names(g["formula"])]
    if users:
        raise ValueError(f"“{users[0]}” uses this field; change or delete that formula first")
    with get_db() as db:
        db.execute("DELETE FROM custom_fields WHERE id=?", (fid,))


def move_field(fid: int, delta: int) -> None:
    f = get_field(fid)
    if not f:
        return
    fields = custom_fields(f["entity"])
    i = next(n for n, g in enumerate(fields) if g["id"] == fid)
    j = max(0, min(len(fields) - 1, i + delta))
    fields.insert(j, fields.pop(i))
    with get_db() as db:
        for n, g in enumerate(fields):
            db.execute("UPDATE custom_fields SET position=? WHERE id=?", (n + 1, g["id"]))


def _safe_names(src):
    try:
        return F.names(src)
    except F.FormulaError:
        return set()


def parse_input(ftype: str, raw: str | None):
    """Validate a typed value for storage (TEXT). Returns None for blank."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if ftype in ("number", "money"):
        n = parse_number(raw)
        if n is None:
            raise ValueError(f"“{raw}” isn't a number")
        return repr(n) if isinstance(n, float) else str(n)
    if ftype == "date":
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError(f"“{raw}” isn't a date (YYYY-MM-DD)") from None
    return raw[:2000]


def parse_number(raw: str):
    s = str(raw).strip().replace(",", "").replace("$", "").replace("%", "")
    mult = 1
    if s[-1:].lower() == "k":
        s, mult = s[:-1], 1000
    try:
        v = float(s) * mult
    except ValueError:
        return None
    return int(v) if v.is_integer() and "." not in s else v


def _typed(ftype, raw):
    if raw is None:
        return None
    if ftype in ("number", "money"):
        return parse_number(raw)
    return raw


def set_values(kind: str, record_id: int, form: dict) -> list[str]:
    """form: {field_key: raw}. Returns error messages (valid values are still saved)."""
    errors = []
    with get_db() as db:
        for f in custom_fields(kind, db):
            if f["kind"] == "formula" or f["key"] not in form:
                continue
            try:
                v = parse_input(f["kind"], form[f["key"]])
            except ValueError as e:
                errors.append(f"{f['label']}: {e}")
                continue
            if v is None:
                db.execute("DELETE FROM custom_values WHERE field_id=? AND record_id=?", (f["id"], record_id))
            else:
                db.execute("INSERT INTO custom_values (field_id, entity, record_id, value) VALUES (?,?,?,?) "
                           "ON CONFLICT(field_id, record_id) DO UPDATE SET value=excluded.value",
                           (f["id"], kind, record_id, v))
    return errors


# ---------------- loading records with every field computed ----------------

def load(kind: str, ids: list[int] | None = None, db=None, formulas: bool = True) -> list[dict]:
    """Records of one kind: {id, title, sub, href, values: {key: value}, errors: {key: msg}}."""
    if db is None:
        with get_db() as db2:
            return load(kind, ids, db2, formulas)
    k = KINDS[kind]
    cols = ", ".join(f"{sql} AS f_{key}" for key, (_, _, sql) in k["fields"].items())
    where = [k["where"]] if k.get("where") else []
    params: list = []
    if ids is not None:
        if not ids:
            return []
        where.append(f"{k['id']} IN ({','.join('?' * len(ids))})")
        params += list(ids)
    sql = (f"SELECT {k['id']} AS id, {k['title']} AS title, {k['sub']} AS sub, {k['href']} AS href, {cols} "
           f"{k['frm']}{' WHERE ' + ' AND '.join(where) if where else ''}")
    rows = []
    for r in db.execute(sql, params):
        vals = {key: r[f"f_{key}"] for key in k["fields"]}
        rows.append({"id": r["id"], "title": r["title"], "sub": r["sub"], "href": r["href"], "values": vals,
                     "errors": {}})
    if k.get("derived"):
        for row in rows:
            row["values"].update(_derive(kind, db, {"id": row["id"], **row["values"]}))
    if k.get("custom"):
        fields = custom_fields(kind, db)
        if fields:
            by_id = {f["id"]: f for f in fields}
            stored: dict[int, dict] = {}
            q = "SELECT field_id, record_id, value FROM custom_values WHERE entity=?"
            for v in db.execute(q, (kind,)):
                f = by_id.get(v["field_id"])
                if f:
                    stored.setdefault(v["record_id"], {})[f["key"]] = _typed(f["kind"], v["value"])
            plain = [f for f in fields if f["kind"] != "formula"]
            try:
                ordered = _order(fields) if formulas else []
            except F.FormulaError:
                ordered = []
            trees = {}
            for f in ordered:
                try:
                    trees[f["key"]] = F.parse(f["formula"])
                except F.FormulaError as e:
                    trees[f["key"]] = e
            for row in rows:
                vals = row["values"]
                for f in plain:
                    vals[f["key"]] = stored.get(row["id"], {}).get(f["key"])
                for f in ordered:
                    t = trees[f["key"]]
                    try:
                        if isinstance(t, F.FormulaError):
                            raise t
                        vals[f["key"]] = F.evaluate(t, vals)
                    except F.FormulaError as e:
                        vals[f["key"]] = None
                        row["errors"][f["key"]] = str(e)
                    except (ArithmeticError, ValueError, TypeError, OverflowError):
                        vals[f["key"]] = None
                        row["errors"][f["key"]] = "Can't calculate this"
    return rows


def record(kind: str, record_id: int) -> dict | None:
    rows = load(kind, [record_id])
    return rows[0] if rows else None


def fmt_value(v, ftype: str) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, date):
        v = v.isoformat()
    if ftype == "date" and isinstance(v, str):
        try:
            return date.fromisoformat(v[:10]).strftime("%b %-d, %Y")
        except ValueError:
            return v
    if isinstance(v, (int, float)):
        if ftype == "money":
            return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
        if ftype == "percent":
            return f"{v:,.1f}%"
        n = f"{v:,.0f}" if float(v).is_integer() or abs(v) >= 100 else f"{v:,.2f}"
        if ftype in ("number",) and isinstance(v, int) and 1900 <= v <= 2200:
            n = str(v)  # years read better without a comma
        if ftype == "days":
            return f"{n} day{'s' if abs(v) != 1 else ''}"
        if ftype == "years":
            return f"{v:,.1f} yr" if not float(v).is_integer() else f"{int(v)} yr"
        return n
    return str(v)


def detail_ctx(kind: str, record_id: int) -> dict:
    """Context for the 'Custom fields' card on a detail page."""
    fields = custom_fields(kind)
    rec = record(kind, record_id) if fields else None
    out = []
    for f in fields:
        v = rec["values"].get(f["key"]) if rec else None
        t = f["format"] if f["kind"] == "formula" else f["kind"]
        out.append({**f, "value": v, "display": fmt_value(v, t), "error": (rec or {}).get("errors", {}).get(f["key"]),
                    "raw": "" if v is None else (v if f["kind"] == "text" else str(v).rstrip("0").rstrip(".")
                                                  if isinstance(v, float) else str(v))})
    return {"kind": kind, "id": record_id, "fields": out, "label": KINDS[kind]["label"]}


def preview(kind: str, src: str, fmt: str, key: str | None = None, n: int = 4) -> dict:
    """Live preview for the formula editor: first n records' results, or the error."""
    try:
        validate_formula(kind, src, key)
    except F.FormulaError as e:
        return {"ok": False, "error": str(e)}
    rows = load(kind)
    tree = F.parse(src)
    out = []
    for r in rows[: max(n * 5, 20)]:
        try:
            v = F.evaluate(tree, r["values"])
            out.append({"title": r["title"], "display": fmt_value(v, fmt), "error": None})
        except F.FormulaError as e:
            out.append({"title": r["title"], "display": "—", "error": str(e)})
        except (ArithmeticError, ValueError, TypeError, OverflowError):
            out.append({"title": r["title"], "display": "—", "error": "Can't calculate this"})
    with_value = [o for o in out if o["display"] != "—" or o["error"]]
    return {"ok": True, "rows": (with_value or out)[:n], "count": len(rows)}
