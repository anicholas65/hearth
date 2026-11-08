import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    brand TEXT, model TEXT, serial TEXT,
    purchase_date TEXT, price REAL,
    warranty_until TEXT,
    life_years INTEGER, replace_cost REAL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS contractors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, trade TEXT, phone TEXT, email TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    interval_days INTEGER NOT NULL,
    next_due TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    notes TEXT,
    last_notified TEXT
);
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    done_on TEXT NOT NULL,
    cost REAL,
    contractor_id INTEGER REFERENCES contractors(id) ON DELETE SET NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL UNIQUE,
    thumb_name TEXT NOT NULL,
    is_cover INTEGER NOT NULL DEFAULT 0,
    caption TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    year INTEGER PRIMARY KEY,
    amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_entries (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('allocation', 'expense')),
    amount REAL NOT NULL CHECK (amount > 0),
    note TEXT,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'appointment' CHECK (kind IN ('appointment', 'reminder')),
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,          -- local 'YYYY-MM-DDTHH:MM' (or 'YYYY-MM-DD' if all_day)
    ends_at TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    notes TEXT,
    contractor_id INTEGER REFERENCES contractors(id) ON DELETE SET NULL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    remind_minutes INTEGER,
    gcal_event_id TEXT,
    last_notified TEXT
);
CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY,
    list TEXT NOT NULL CHECK (list IN ('groceries', 'supplies')),
    name TEXT NOT NULL,
    qty TEXT,
    category TEXT,
    store TEXT,
    note TEXT,
    price REAL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'needed' CHECK (status IN ('needed', 'checked', 'bought')),
    added_at TEXT NOT NULL,
    checked_at TEXT,
    bought_at TEXT
);
CREATE INDEX IF NOT EXISTS shopping_status ON shopping_items(list, status);
"""

# columns added after first release: (table, column, ddl)
MIGRATIONS = [
    ("photos", "caption", "ALTER TABLE photos ADD COLUMN caption TEXT"),
    ("tasks", "gcal_event_id", "ALTER TABLE tasks ADD COLUMN gcal_event_id TEXT"),
]


def data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", "./data"))
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    return d


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / "hearth.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                db.execute(ddl)


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def set_setting(key: str, value: str | None) -> None:
    with get_db() as db:
        db.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
