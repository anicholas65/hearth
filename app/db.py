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
"""


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
