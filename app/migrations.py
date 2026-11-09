"""Versioned, reversible migrations for the sync/API upgrade.

* Each migration has up() and down(). Applied versions are recorded in `schema_version`.
* Before any pending migration runs, the database is copied (SQLite backup API) to
  $DATA_DIR/backups/hearth-<UTC timestamp>-pre-v<N>.db.
* Up-migrations only add tables, columns, indexes and triggers — except v2, which rebuilds
  `shopping_items` by copying every row (to lift a CHECK that blocked custom lists).

CLI:  python -m app.migrations status | up | down <target_version>
"""
import sqlite3
import sys
from datetime import datetime, timezone

from .db import data_dir

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
UUID = ("(lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
        "substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab', 1 + (abs(random()) % 4), 1) || "
        "substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))))")

# API type -> table. Every one gets uuid/created_at/updated_at, change-log + tombstone triggers.
SYNCED = {
    "tasks": "tasks", "completions": "task_logs", "items": "items", "locations": "rooms",
    "documents": "documents", "photos": "photos", "contractors": "contractors", "lists": "lists",
    "list-items": "shopping_items", "pantry": "pantry_items", "ledger": "fund_entries",
    "funds": "funds", "vehicles": "vehicles", "projects": "projects",
}
LEGACY = ["tasks", "task_logs", "items", "rooms", "documents", "photos", "contractors",
          "shopping_items", "fund_entries"]

NEW_TABLES = {
    "schema_version": "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    "changelog": "CREATE TABLE IF NOT EXISTS changelog (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, "
                 "uuid TEXT NOT NULL, op TEXT NOT NULL, at TEXT NOT NULL DEFAULT (" + NOW + "))",
    "tombstones": "CREATE TABLE IF NOT EXISTS tombstones (type TEXT NOT NULL, uuid TEXT NOT NULL, "
                  "deleted_at TEXT NOT NULL, PRIMARY KEY (type, uuid))",
    "sync_ctx": "CREATE TABLE IF NOT EXISTS sync_ctx (api INTEGER)",
    "files": "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, "
             "stored_name TEXT NOT NULL UNIQUE, filename TEXT, mime TEXT, size INTEGER, created_at TEXT NOT NULL)",
    "devices": "CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, "
               "name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, member_id INTEGER, created_at TEXT NOT NULL, "
               "last_seen_at TEXT, revoked_at TEXT)",
    "pair_codes": "CREATE TABLE IF NOT EXISTS pair_codes (code_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL, "
                  "used_at TEXT, created_at TEXT NOT NULL)",
    "members": "CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, "
               "name TEXT, role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL, last_seen_at TEXT)",
    "lists": "CREATE TABLE IF NOT EXISTS lists (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
             "kind TEXT NOT NULL DEFAULT 'custom', store TEXT, position INTEGER NOT NULL DEFAULT 0)",
    "pantry_items": "CREATE TABLE IF NOT EXISTS pantry_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, qty REAL, "
                    "unit TEXT, min_qty REAL, category TEXT, room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL, "
                    "best_before TEXT, barcode TEXT, notes TEXT, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL)",
    "funds": "CREATE TABLE IF NOT EXISTS funds (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
             "item_id INTEGER REFERENCES items(id) ON DELETE CASCADE, target_amount REAL, target_date TEXT, "
             "monthly_amount REAL, balance REAL, notes TEXT)",
    "vehicles": "CREATE TABLE IF NOT EXISTS vehicles (id INTEGER PRIMARY KEY, name TEXT NOT NULL, make TEXT, model TEXT, "
                "year INTEGER, vin TEXT, plate TEXT, odometer INTEGER, odometer_log TEXT, purchase_date TEXT, "
                "purchase_odometer INTEGER, price REAL, notes TEXT)",
    "projects": "CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "kind TEXT NOT NULL DEFAULT 'project', status TEXT NOT NULL DEFAULT 'planned', budget REAL, notes TEXT, "
                "materials TEXT, started_on TEXT, finished_on TEXT)",
    "store_aisles": "CREATE TABLE IF NOT EXISTS store_aisles (store TEXT NOT NULL, category TEXT NOT NULL, "
                    "position INTEGER NOT NULL, PRIMARY KEY (store, category))",
}

# (table, column, ddl) added in v1 besides uuid/created_at/updated_at
NEW_COLUMNS = [
    ("rooms", "parent_id", "INTEGER REFERENCES rooms(id) ON DELETE SET NULL"),
    ("tasks", "kind", "TEXT NOT NULL DEFAULT 'chore'"),
    ("tasks", "vehicle_id", "INTEGER REFERENCES vehicles(id) ON DELETE CASCADE"),
    ("tasks", "interval_miles", "INTEGER"),
    ("tasks", "next_due_miles", "INTEGER"),
    ("tasks", "snoozed_until", "TEXT"),
    ("task_logs", "odometer", "INTEGER"),
    ("photos", "project_id", "INTEGER REFERENCES projects(id) ON DELETE CASCADE"),
    ("photos", "vehicle_id", "INTEGER REFERENCES vehicles(id) ON DELETE CASCADE"),
    ("fund_entries", "vehicle_id", "INTEGER REFERENCES vehicles(id) ON DELETE SET NULL"),
    ("fund_entries", "project_id", "INTEGER REFERENCES projects(id) ON DELETE SET NULL"),
    ("items", "barcode", "TEXT"),
    ("shopping_items", "barcode", "TEXT"),
]

SAFETY_WORDS = ("smoke", "co detector", "carbon monoxide", "extinguisher", "gfci", "radon", "sump pump")


def _script(db, sql: str) -> None:
    """Run a multi-statement script inside the current transaction (executescript would COMMIT)."""
    buf = ""
    for part in sql.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \n\t;"):
                db.execute(buf)
            buf = ""


def _cols(db, table) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _sync_triggers(db, api_type: str, table: str) -> None:
    t = f"sync_{table}"
    _script(db, f"""
    DROP TRIGGER IF EXISTS {t}_ai; DROP TRIGGER IF EXISTS {t}_au; DROP TRIGGER IF EXISTS {t}_ad;
    CREATE TRIGGER {t}_ai AFTER INSERT ON {table} BEGIN
      UPDATE {table} SET uuid = COALESCE(NEW.uuid, {UUID}),
                         created_at = COALESCE(NEW.created_at, {NOW}),
                         updated_at = COALESCE(NEW.updated_at, {NOW}) WHERE rowid = NEW.rowid;
      DELETE FROM tombstones WHERE type = '{api_type}' AND uuid = (SELECT uuid FROM {table} WHERE rowid = NEW.rowid);
      INSERT INTO changelog (type, uuid, op) SELECT '{api_type}', uuid, 'upsert' FROM {table} WHERE rowid = NEW.rowid;
    END;
    CREATE TRIGGER {t}_au AFTER UPDATE ON {table} WHEN NEW.uuid IS NOT NULL BEGIN
      UPDATE {table} SET updated_at = {NOW}
        WHERE rowid = NEW.rowid AND NEW.updated_at IS OLD.updated_at AND NOT EXISTS (SELECT 1 FROM sync_ctx);
      INSERT INTO changelog (type, uuid, op) VALUES ('{api_type}', NEW.uuid, 'upsert');
    END;
    CREATE TRIGGER {t}_ad AFTER DELETE ON {table} WHEN OLD.uuid IS NOT NULL BEGIN
      INSERT OR REPLACE INTO tombstones (type, uuid, deleted_at) VALUES ('{api_type}', OLD.uuid, {NOW});
      INSERT INTO changelog (type, uuid, op) VALUES ('{api_type}', OLD.uuid, 'delete');
    END;
    """)


def _file_triggers(db) -> None:
    """Uploads made by the web UI get a `files` row so the API can reference them by id."""
    for table, name_col in (("documents", "filename"), ("photos", "stored_name")):
        _script(db, f"""
        DROP TRIGGER IF EXISTS files_from_{table};
        CREATE TRIGGER files_from_{table} AFTER INSERT ON {table} BEGIN
          INSERT INTO files (uuid, stored_name, filename, created_at)
            SELECT {UUID}, NEW.stored_name, NEW.{name_col}, {NOW}
            WHERE NOT EXISTS (SELECT 1 FROM files WHERE stored_name = NEW.stored_name);
        END;""")


# ---------------- v1: sync foundation + new modules ----------------

def v1_up(db):
    for ddl in NEW_TABLES.values():
        db.execute(ddl)
    for table, col, ddl in NEW_COLUMNS:
        if col not in _cols(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    for api_type, table in SYNCED.items():
        cols = _cols(db, table)
        for col in ("uuid", "created_at", "updated_at"):
            if col not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # backfill: created_at from the best existing date, else now
        created = {"task_logs": "done_on", "documents": "uploaded_at", "photos": "created_at",
                   "fund_entries": "entry_date", "shopping_items": "added_at"}.get(table)
        db.execute(f"UPDATE {table} SET uuid = {UUID} WHERE uuid IS NULL")
        src = f"COALESCE({created} || CASE WHEN length({created}) = 10 THEN 'T00:00:00.000Z' ELSE '' END, {NOW})" \
            if created and created != "created_at" else NOW
        db.execute(f"UPDATE {table} SET created_at = {src} WHERE created_at IS NULL")
        db.execute(f"UPDATE {table} SET updated_at = {NOW} WHERE updated_at IS NULL")
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_uuid ON {table}(uuid)")
        _sync_triggers(db, api_type, table)
        db.execute(f"INSERT INTO changelog (type, uuid, op) SELECT '{api_type}', uuid, 'upsert' FROM {table}")
    # files rows for existing uploads
    for table, name_col in (("documents", "filename"), ("photos", "stored_name")):
        db.execute(f"INSERT OR IGNORE INTO files (uuid, stored_name, filename, created_at) "
                   f"SELECT {UUID}, stored_name, {name_col}, COALESCE(created_at, {NOW}) FROM {table}")
    _file_triggers(db)
    # the two built-in shopping lists
    db.execute("INSERT OR IGNORE INTO lists (key, name, kind, position) VALUES "
               "('groceries', 'Groceries', 'groceries', 0), ('supplies', 'Supplies', 'supplies', 1)")
    # life-safety maintenance keeps hard due dates
    for w in SAFETY_WORDS:
        db.execute("UPDATE tasks SET kind = 'safety' WHERE lower(name) LIKE ?", (f"%{w}%",))
    db.execute("CREATE INDEX IF NOT EXISTS ix_changelog_type_uuid ON changelog(type, uuid)")


def v1_down(db):
    for table in SYNCED.values():
        t = f"sync_{table}"
        _script(db, f"DROP TRIGGER IF EXISTS {t}_ai; DROP TRIGGER IF EXISTS {t}_au; DROP TRIGGER IF EXISTS {t}_ad;")
    _script(db, "DROP TRIGGER IF EXISTS files_from_documents; DROP TRIGGER IF EXISTS files_from_photos;")
    for table in LEGACY:
        db.execute(f"DROP INDEX IF EXISTS ux_{table}_uuid")
        for col in ("uuid", "created_at", "updated_at"):
            if col in _cols(db, table) and not (table == "photos" and col == "created_at"):
                db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    for table, col, _ in reversed(NEW_COLUMNS):
        if col in _cols(db, table):
            try:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # SQLite can't drop FK columns; a NULL column is harmless to v2.1 code
    for name in ["store_aisles", "projects", "vehicles", "funds", "pantry_items", "lists", "members",
                 "pair_codes", "devices", "files", "sync_ctx", "tombstones", "changelog"]:
        db.execute(f"DROP TABLE IF EXISTS {name}")


# ---------------- v2: shopping_items without the two-list CHECK ----------------

SHOP_COLS = ("id, list, name, qty, category, store, note, price, item_id, status, added_at, checked_at, "
             "bought_at, barcode, uuid, created_at, updated_at")


def _rebuild_shopping(db, check: bool):
    lst = "list TEXT NOT NULL" + (" CHECK (list IN ('groceries', 'supplies'))" if check else "")
    before = db.execute("SELECT COUNT(*) FROM shopping_items").fetchone()[0]
    _script(db, f"""
    CREATE TABLE shopping_items_new (
        id INTEGER PRIMARY KEY, {lst}, name TEXT NOT NULL, qty TEXT, category TEXT, store TEXT, note TEXT,
        price REAL, item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'needed' CHECK (status IN ('needed', 'checked', 'bought')),
        added_at TEXT NOT NULL, checked_at TEXT, bought_at TEXT, barcode TEXT,
        uuid TEXT, created_at TEXT, updated_at TEXT);
    INSERT INTO shopping_items_new ({SHOP_COLS}) SELECT {SHOP_COLS} FROM shopping_items;
    DROP TABLE shopping_items;
    ALTER TABLE shopping_items_new RENAME TO shopping_items;
    CREATE INDEX IF NOT EXISTS shopping_status ON shopping_items(list, status);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_shopping_items_uuid ON shopping_items(uuid);
    """)
    after = db.execute("SELECT COUNT(*) FROM shopping_items").fetchone()[0]
    if before != after:
        raise RuntimeError(f"shopping_items rebuild lost rows ({before} -> {after})")
    _sync_triggers(db, "list-items", "shopping_items")


def v2_up(db):
    _rebuild_shopping(db, check=False)


def v2_down(db):
    bad = db.execute("SELECT COUNT(*) FROM shopping_items WHERE list NOT IN ('groceries','supplies')").fetchone()[0]
    if bad:
        raise RuntimeError(f"{bad} items are on custom lists; move or delete them before downgrading")
    _rebuild_shopping(db, check=True)


MIGRATIONS = [(1, v1_up, v1_down), (2, v2_up, v2_down)]
LATEST = MIGRATIONS[-1][0]


def current_version(db) -> int:
    db.execute(NEW_TABLES["schema_version"])
    return db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def backup(db_path, label: str):
    dest_dir = data_dir() / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"hearth-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{label}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def migrate_up(db_path) -> list[int]:
    con = sqlite3.connect(db_path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = OFF")  # table rebuilds; FKs are re-checked below
    applied = []
    try:
        cur = current_version(con)
        pending = [m for m in MIGRATIONS if m[0] > cur]
        if pending:
            has_data = any(con.execute(f"SELECT EXISTS (SELECT 1 FROM {t})").fetchone()[0]
                           for t in ("tasks", "items", "fund_entries", "shopping_items", "rooms")
                           if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone())
            if has_data:
                backup(db_path, f"pre-v{pending[0][0]}")
        for version, up, _ in pending:
            con.execute("BEGIN")
            try:
                up(con)
                con.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                            (version, datetime.now(timezone.utc).isoformat()))
                problems = con.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"foreign key check failed after v{version}: {problems[:3]}")
                con.execute("COMMIT")
                applied.append(version)
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()
    return applied


def migrate_down(db_path, target: int) -> list[int]:
    con = sqlite3.connect(db_path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = OFF")
    undone = []
    try:
        cur = current_version(con)
        if cur > target:
            backup(db_path, f"pre-down-v{target}")
        for version, _, down in reversed(MIGRATIONS):
            if target < version <= cur:
                con.execute("BEGIN")
                try:
                    down(con)
                    con.execute("DELETE FROM schema_version WHERE version = ?", (version,))
                    con.execute("COMMIT")
                    undone.append(version)
                except Exception:
                    con.execute("ROLLBACK")
                    raise
    finally:
        con.close()
    return undone


if __name__ == "__main__":
    path = data_dir() / "hearth.db"
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "up":
        print("applied:", migrate_up(path))
    elif cmd == "down":
        print("reverted:", migrate_down(path, int(sys.argv[2])))
    else:
        con = sqlite3.connect(path)
        print(f"schema version {current_version(con)} (latest {LATEST})")
