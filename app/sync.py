"""Sync engine behind /api/v1: resource registry, serialisation, LWW upserts, soft deletes, change feed.

Clients see UUID ids, ISO-8601 UTC timestamps and `deleted_at`. Internally rows keep their integer
keys (the web UI uses them); foreign keys are translated UUID <-> integer at the boundary.
Deletes remove the row and keep a tombstone (written by triggers, so web deletes and cascades are
reported too); a deleted record reads back as {id, deleted_at}. A PUT newer than the tombstone
recreates it.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .db import get_db
from .migrations import SYNCED

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def now_ts() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def out_ts(v) -> str | None:
    """Stored timestamps -> canonical UTC form (older web rows may be naive or date-only)."""
    if not v:
        return None
    try:
        return norm_ts(v if "T" in v else v + "T00:00:00")
    except ApiError:
        return v


def norm_ts(v) -> str | None:
    """Any ISO-8601 instant -> 'YYYY-MM-DDTHH:MM:SS.mmmZ' (UTC) so strings compare correctly."""
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not TS_RE.match(v):
        raise ApiError(422, f"timestamp must be ISO-8601 with time: {v!r}")
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class F:
    """One API field. kind: str|int|num|bool|date|json; fk: target API type; col: DB column."""
    kind: str = "str"
    required: bool = False
    fk: str | None = None
    col: str | None = None
    default: object = None
    choices: tuple | None = None
    readonly: bool = False


@dataclass
class Resource:
    type: str
    table: str
    fields: dict[str, F] = field(default_factory=dict)
    title: str = ""

    def col(self, name: str) -> str:
        return self.fields[name].col or name


def _today():
    return date.today().isoformat()


RESOURCES: dict[str, Resource] = {r.type: r for r in [
    Resource("tasks", "tasks", {
        "name": F(required=True), "interval_days": F("int", default=90), "next_due": F("date", default=_today),
        "kind": F(default="chore", choices=("chore", "safety")), "priority": F("int", default=0),
        "notes": F(), "completed_at": F("date"), "snoozed_until": F("date"),
        "item_id": F(fk="items"), "vehicle_id": F(fk="vehicles"),
        "interval_miles": F("int"), "next_due_miles": F("int")}, "Recurring or one-time task (interval_days 0 = one-time)"),
    Resource("completions", "task_logs", {
        "task_id": F(fk="tasks", required=True), "done_on": F("date", default=_today), "cost": F("num"),
        "contractor_id": F(fk="contractors"), "notes": F(), "odometer": F("int")}, "A task being done"),
    Resource("items", "items", {
        "name": F(required=True), "location_id": F(fk="locations", col="room_id"), "category": F(),
        "brand": F(), "model": F(), "serial": F(), "barcode": F(), "purchase_date": F("date"), "price": F("num"),
        "warranty_until": F("date"), "life_years": F("int"), "replace_cost": F("num"), "notes": F()}, "Inventory item"),
    Resource("locations", "rooms", {
        "name": F(required=True), "parent_id": F(fk="locations")}, "Room or nested location (Garage → Shelf 2 → Bin A)"),
    Resource("documents", "documents", {
        "title": F(required=True), "file_id": F(fk="files", col="stored_name", required=True),
        "filename": F(), "item_id": F(fk="items"), "uploaded_at": F(readonly=True)}, "Receipt, manual, policy…"),
    Resource("photos", "photos", {
        "file_id": F(fk="files", col="stored_name", required=True), "caption": F(),
        "item_id": F(fk="items"), "location_id": F(fk="locations", col="room_id"),
        "project_id": F(fk="projects"), "vehicle_id": F(fk="vehicles"), "is_cover": F("bool", default=False)}, "Photo"),
    Resource("contractors", "contractors", {
        "name": F(required=True), "trade": F(), "phone": F(), "email": F(), "notes": F()}, "Contractor"),
    Resource("lists", "lists", {
        "name": F(required=True), "kind": F(default="custom", choices=("groceries", "supplies", "custom")),
        "store": F(), "position": F("int", default=0)}, "Shopping list"),
    Resource("list-items", "shopping_items", {
        "list_id": F(fk="lists", col="list", required=True), "name": F(required=True), "qty": F(), "category": F(),
        "store": F(), "note": F(), "price": F("num"), "barcode": F(), "item_id": F(fk="items"),
        "status": F(default="needed", choices=("needed", "checked", "bought")),
        "added_at": F(default=lambda: now_ts()), "checked_at": F(), "bought_at": F()}, "Shopping list entry"),
    Resource("pantry", "pantry_items", {
        "name": F(required=True), "qty": F("num"), "unit": F(), "min_qty": F("num"), "category": F(),
        "location_id": F(fk="locations", col="room_id"), "best_before": F("date"), "barcode": F(), "notes": F(),
        "project_id": F(fk="projects")}, "Pantry / consumables stock"),
    Resource("ledger", "fund_entries", {
        "entry_date": F("date", default=_today), "kind": F(required=True, choices=("allocation", "expense")),
        "amount": F("num", required=True), "note": F(), "category": F(), "item_id": F(fk="items"),
        "document_id": F(fk="documents"), "vehicle_id": F(fk="vehicles"), "project_id": F(fk="projects")},
        "Home fund entry (allocation = money in, expense = money out)"),
    Resource("funds", "funds", {
        "name": F(required=True), "item_id": F(fk="items"), "target_amount": F("num"), "target_date": F("date"),
        "monthly_amount": F("num"), "balance": F("num"), "notes": F()}, "Sinking fund"),
    Resource("vehicles", "vehicles", {
        "name": F(required=True), "make": F(), "model": F(), "year": F("int"), "vin": F(), "plate": F(),
        "odometer": F("int"), "odometer_log": F("json"), "purchase_date": F("date"), "purchase_odometer": F("int"),
        "price": F("num"), "notes": F()}, "Vehicle"),
    Resource("projects", "projects", {
        "name": F(required=True), "kind": F(default="project", choices=("project", "hobby")),
        "status": F(default="planned", choices=("planned", "active", "paused", "done")), "budget": F("num"),
        "notes": F(), "materials": F("json"), "started_on": F("date"), "finished_on": F("date")}, "Project or hobby"),
]}
assert set(RESOURCES) == set(SYNCED), "registry and migrations disagree"


# ---------------- id translation ----------------

def _uuid_for(db, target: str, int_val):
    if int_val is None:
        return None
    if target == "files":
        r = db.execute("SELECT uuid FROM files WHERE stored_name=?", (int_val,)).fetchone()
    elif target == "lists":
        r = db.execute("SELECT uuid FROM lists WHERE key=?", (int_val,)).fetchone()
    else:
        r = db.execute(f"SELECT uuid FROM {RESOURCES[target].table} WHERE id=?", (int_val,)).fetchone()
    return r[0] if r else None


def _int_for(db, target: str, uuid_val):
    if uuid_val is None:
        return None
    if target == "files":
        r = db.execute("SELECT stored_name FROM files WHERE uuid=?", (uuid_val,)).fetchone()
    elif target == "lists":
        r = db.execute("SELECT key FROM lists WHERE uuid=?", (uuid_val,)).fetchone()
    else:
        r = db.execute(f"SELECT id FROM {RESOURCES[target].table} WHERE uuid=?", (uuid_val,)).fetchone()
    if not r:
        raise ApiError(422, f"unknown {target} id: {uuid_val}")
    return r[0]


# ---------------- read ----------------

def serialize(db, res: Resource, row) -> dict:
    out = {"id": row["uuid"], "created_at": out_ts(row["created_at"]), "updated_at": out_ts(row["updated_at"]),
           "deleted_at": None}
    for name, f in res.fields.items():
        v = row[res.col(name)]
        if f.fk:
            v = _uuid_for(db, f.fk, v)
        elif f.kind == "bool":
            v = bool(v) if v is not None else None
        elif f.kind == "json" and v is not None:
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[name] = v
    if res.type in ("documents", "photos") and out.get("file_id"):
        out["url"] = f"/api/v1/files/{out['file_id']}"
    return out


def get_record(db, type_: str, uuid: str) -> dict | None:
    res = RESOURCES[type_]
    row = db.execute(f"SELECT * FROM {res.table} WHERE uuid=?", (uuid,)).fetchone()
    if row:
        return serialize(db, res, row)
    t = db.execute("SELECT deleted_at FROM tombstones WHERE type=? AND uuid=?", (type_, uuid)).fetchone()
    return {"id": uuid, "deleted_at": t[0]} if t else None


def list_records(db, type_: str, updated_since: str | None, include_deleted: bool, limit: int, offset: int):
    res = RESOURCES[type_]
    q, args = f"SELECT * FROM {res.table}", []
    if updated_since:
        q += " WHERE updated_at > ?"
        args.append(norm_ts(updated_since))
    q += " ORDER BY updated_at, id LIMIT ? OFFSET ?"
    rows = [serialize(db, res, r) for r in db.execute(q, args + [limit, offset])]
    if include_deleted:
        tq = "SELECT uuid, deleted_at FROM tombstones WHERE type=?" + (" AND deleted_at > ?" if updated_since else "")
        targs = [type_] + ([norm_ts(updated_since)] if updated_since else [])
        rows += [{"id": u, "deleted_at": d} for u, d in db.execute(tq, targs)]
    return rows


# ---------------- write ----------------

def _coerce(name: str, f: F, v):
    if v is None:
        return None
    try:
        if f.kind == "int":
            return int(v)
        if f.kind == "num":
            return float(v)
        if f.kind == "bool":
            return 1 if v in (True, 1, "1", "true", "True") else 0
        if f.kind == "date":
            return date.fromisoformat(str(v)[:10]).isoformat()
        if f.kind == "json":
            return json.dumps(v)
    except (TypeError, ValueError):
        raise ApiError(422, f"{name}: expected {f.kind}, got {v!r}")
    if f.choices and v not in f.choices:
        raise ApiError(422, f"{name}: must be one of {', '.join(f.choices)}")
    return str(v)


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def upsert(type_: str, uuid: str, body: dict) -> tuple[dict, bool]:
    """Idempotent create-or-update with last-write-wins. Returns (server copy, applied?)."""
    if not UUID_RE.match(uuid):
        raise ApiError(422, "id must be a UUID")
    uuid = uuid.lower()
    res = RESOURCES[type_]
    body = dict(body or {})
    if body.get("id") and str(body["id"]).lower() != uuid:
        raise ApiError(422, "body id does not match URL id")
    incoming = norm_ts(body.get("updated_at"))
    wants_delete = body.get("deleted_at") not in (None, "")
    unknown = set(body) - set(res.fields) - {"id", "created_at", "updated_at", "deleted_at", "url"}
    if unknown:
        raise ApiError(422, f"unknown field(s): {', '.join(sorted(unknown))}")
    with get_db() as db:
        db.execute("INSERT INTO sync_ctx (api) VALUES (1)")
        try:
            row = db.execute(f"SELECT * FROM {res.table} WHERE uuid=?", (uuid,)).fetchone()
            tomb = db.execute("SELECT deleted_at FROM tombstones WHERE type=? AND uuid=?", (type_, uuid)).fetchone()
            current_ts = row["updated_at"] if row else (tomb[0] if tomb else None)
            if current_ts and incoming and incoming < current_ts:
                return get_record(db, type_, uuid), False  # stale write: server copy wins
            if wants_delete:
                if row:
                    db.execute(f"DELETE FROM {res.table} WHERE id=?", (row["id"],))
                    if incoming:
                        db.execute("UPDATE tombstones SET deleted_at=? WHERE type=? AND uuid=?", (incoming, type_, uuid))
                return get_record(db, type_, uuid) or {"id": uuid, "deleted_at": incoming or now_ts()}, bool(row)
            values = {}
            for name, f in res.fields.items():
                if f.readonly or name not in body:
                    continue
                v = body[name]
                if f.fk:
                    v = _int_for(db, f.fk, v.lower() if isinstance(v, str) else v)
                else:
                    v = _coerce(name, f, v)
                values[res.col(name)] = v
            if row:
                changed = {c: v for c, v in values.items() if row[c] != v}
                if not changed:
                    return serialize(db, res, row), False  # identical: no-op, timestamps untouched
                changed["updated_at"] = incoming or now_ts()
                sets = ", ".join(f"{c}=?" for c in changed)
                db.execute(f"UPDATE {res.table} SET {sets} WHERE id=?", [*changed.values(), row["id"]])
            else:
                for name, f in res.fields.items():
                    c = res.col(name)
                    if c in values and values[c] is not None:
                        continue
                    if f.required:
                        raise ApiError(422, f"{name} is required")
                    if f.default is not None and c not in values:
                        d = f.default() if callable(f.default) else f.default
                        values[c] = _coerce(name, f, d) if not f.fk else d
                _prepare_insert(db, res, values)
                values.update(uuid=uuid, created_at=norm_ts(body.get("created_at")) or incoming or now_ts(),
                              updated_at=incoming or now_ts())
                cols = ", ".join(values)
                db.execute(f"INSERT INTO {res.table} ({cols}) VALUES ({', '.join('?' * len(values))})",
                           list(values.values()))
            rec = get_record(db, type_, uuid)
            _after_write(db, type_, rec)
            return rec, True
        finally:
            db.execute("DELETE FROM sync_ctx")


def _prepare_insert(db, res: Resource, values: dict) -> None:
    """Fill legacy NOT NULL columns the API doesn't expose."""
    if res.table == "documents":
        f = db.execute("SELECT filename FROM files WHERE stored_name=?", (values["stored_name"],)).fetchone()
        values["filename"] = values.get("filename") or (f[0] if f and f[0] else values["stored_name"])
        values["uploaded_at"] = now_ts()[:19]
    elif res.table == "photos":
        from .media import ensure_thumb
        values["thumb_name"] = ensure_thumb(values["stored_name"])
    elif res.table == "lists":
        import uuid as _u
        values["key"] = "l-" + _u.uuid4().hex[:12]


def _after_write(db, type_: str, rec: dict) -> None:
    """Server-side rules that also apply to API writes (kept tiny and idempotent)."""
    if type_ == "completions" and rec.get("task_id"):
        t = db.execute("SELECT id, interval_days FROM tasks WHERE uuid=?", (rec["task_id"],)).fetchone()
        last = db.execute("SELECT MAX(done_on) FROM task_logs WHERE task_id=?", (t["id"],)).fetchone()[0]
        if t["interval_days"] > 0 and last:
            nxt = date.fromordinal(date.fromisoformat(last).toordinal() + t["interval_days"]).isoformat()
            db.execute("UPDATE tasks SET next_due=? WHERE id=? AND next_due IS NOT ?", (nxt, t["id"], nxt))
        elif last:
            db.execute("UPDATE tasks SET completed_at=? WHERE id=? AND completed_at IS NULL", (last, t["id"]))


def delete(type_: str, uuid: str) -> dict | None:
    res = RESOURCES[type_]
    with get_db() as db:
        row = db.execute(f"SELECT id FROM {res.table} WHERE uuid=?", (uuid.lower(),)).fetchone()
        if row:
            db.execute(f"DELETE FROM {res.table} WHERE id=?", (row[0],))
        return get_record(db, type_, uuid.lower())


# ---------------- change feed ----------------

def changes(since: int, limit: int) -> dict:
    limit = max(1, min(limit, 500))
    with get_db() as db:
        rows = db.execute(
            "SELECT type, uuid, MAX(seq) AS seq FROM changelog WHERE seq > ? GROUP BY type, uuid "
            "ORDER BY seq LIMIT ?", (since, limit)).fetchall()
        out, deleted = [], []
        for r in rows:
            if r["type"] not in RESOURCES:
                continue
            res = RESOURCES[r["type"]]
            row = db.execute(f"SELECT * FROM {res.table} WHERE uuid=?", (r["uuid"],)).fetchone()
            if row:
                out.append({"type": r["type"], "record": serialize(db, res, row)})
            else:
                deleted.append({"type": r["type"], "id": r["uuid"]})
        cursor = rows[-1]["seq"] if rows else since
        more = db.execute("SELECT EXISTS (SELECT 1 FROM changelog WHERE seq > ?)", (cursor,)).fetchone()[0]
    return {"changes": out, "deleted": deleted, "cursor": str(cursor), "hasMore": bool(more)}
