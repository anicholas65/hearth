"""Backup (zip), CSV export and CSV import."""
import csv
import io
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .db import data_dir, get_db

EXPORTS = {
    "items": ("SELECT i.name, r.name AS room, i.brand, i.model, i.serial, i.purchase_date, i.price, "
              "i.warranty_until, i.life_years, i.replace_cost, i.notes FROM items i "
              "LEFT JOIN rooms r ON r.id=i.room_id ORDER BY r.name, i.name"),
    "tasks": ("SELECT t.name, t.interval_days, t.next_due, i.name AS item, t.notes FROM tasks t "
              "LEFT JOIN items i ON i.id=t.item_id ORDER BY t.next_due"),
    "history": ("SELECT l.done_on, t.name AS task, l.cost, c.name AS contractor, l.notes FROM task_logs l "
                "JOIN tasks t ON t.id=l.task_id LEFT JOIN contractors c ON c.id=l.contractor_id "
                "ORDER BY l.done_on"),
    "ledger": ("SELECT entry_date, kind, amount, note, i.name AS item FROM fund_entries f "
               "LEFT JOIN items i ON i.id=f.item_id ORDER BY entry_date"),
    "contractors": "SELECT name, trade, phone, email, notes FROM contractors ORDER BY name",
    "schedule": ("SELECT kind, title, starts_at, ends_at, all_day, location, notes FROM appointments "
                 "ORDER BY starts_at"),
}
ITEM_COLUMNS = ["name", "room", "brand", "model", "serial", "purchase_date", "price",
                "warranty_until", "life_years", "replace_cost", "notes"]
SECRET_FILES = {"google-service-account.json"}


def export_csv(kind: str) -> str:
    with get_db() as db:
        cur = db.execute(EXPORTS[kind])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([d[0] for d in cur.description])
        w.writerows([tuple(r) for r in cur.fetchall()])
    return buf.getvalue()


def backup_zip() -> tuple[Path, str]:
    """Consistent snapshot of the DB (sqlite backup API) + uploads. Secrets are excluded."""
    d = data_dir()
    name = f"hearth-backup-{datetime.now():%Y%m%d-%H%M}.zip"
    tmp = Path(tempfile.mkdtemp()) / name
    snap = tmp.with_suffix(".db")
    src = sqlite3.connect(d / "hearth.db")
    dst = sqlite3.connect(snap)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "hearth.db")
        for f in sorted((d / "uploads").glob("*")):
            if f.is_file():
                z.write(f, f"uploads/{f.name}", compress_type=zipfile.ZIP_STORED)
        for kind in EXPORTS:
            z.writestr(f"csv/{kind}.csv", export_csv(kind))
    snap.unlink()
    return tmp, name


def _num(v, cast=float):
    v = (v or "").strip().replace("$", "").replace(",", "")
    try:
        return cast(float(v)) if v else None
    except ValueError:
        return None


def import_items(data: bytes) -> tuple[int, int, list[str]]:
    """Import inventory items from CSV (same columns as the export). Returns (added, skipped, errors)."""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().lower().replace(" ", "_"): c for c in (reader.fieldnames or [])}
    if "name" not in cols:
        return 0, 0, ["CSV needs a 'name' column. Columns: " + ", ".join(ITEM_COLUMNS)]
    added = skipped = 0
    errors: list[str] = []
    with get_db() as db:
        existing = {r[0].lower() for r in db.execute("SELECT name FROM items")}
        for n, row in enumerate(reader, start=2):
            g = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""
            name = g("name")
            if not name:
                continue
            if name.lower() in existing:
                skipped += 1
                continue
            room_id = None
            if g("room"):
                db.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (g("room"),))
                room_id = db.execute("SELECT id FROM rooms WHERE name=?", (g("room"),)).fetchone()[0]
            try:
                db.execute(
                    "INSERT INTO items (name, room_id, brand, model, serial, purchase_date, price, "
                    "warranty_until, life_years, replace_cost, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name, room_id, g("brand") or None, g("model") or None, g("serial") or None,
                     g("purchase_date") or None, _num(g("price")), g("warranty_until") or None,
                     _num(g("life_years"), int), _num(g("replace_cost")), g("notes") or None))
                existing.add(name.lower())
                added += 1
            except sqlite3.Error as e:
                errors.append(f"Row {n}: {e}")
    return added, skipped, errors
