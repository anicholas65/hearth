from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_pages_render(client):
    for p in ["/", "/tasks", "/items", "/documents", "/contractors", "/budget", "/budget?view=forecast",
              "/budget?year=2020", "/gallery", "/gallery?show=unfiled", "/settings"]:
        assert client.get(p).status_code == 200, p


def test_task_lifecycle(client):
    client.post("/tasks", data={"name": "Filter", "interval_days": 90, "next_due": "2020-01-01"})
    assert "badge bad" in client.get("/tasks").text
    client.post("/tasks/1/done", data={"done_on": "2026-01-01", "cost": "12.5"})
    from app.db import get_db
    with get_db() as db:
        assert db.execute("SELECT next_due FROM tasks").fetchone()[0] == "2026-04-01"
        assert db.execute("SELECT cost FROM task_logs").fetchone()[0] == 12.5
    assert "2026-01-01" in client.get("/tasks/1").text


def test_seed_is_idempotent(client):
    client.post("/tasks/seed"); client.post("/tasks/seed")
    from app.db import get_db
    from app.main import DEFAULT_TASKS
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == len(DEFAULT_TASKS)


def test_items_rooms_budget(client):
    client.post("/rooms", data={"name": "Kitchen"})
    r = client.post("/items", data={"name": "Fridge", "room_id": "1", "purchase_date": "2018-05-01",
                                    "life_years": "10", "replace_cost": "1800", "price": "1500"},
                    follow_redirects=False)
    assert r.headers["location"] == "/items/1"
    assert "2028" in client.get("/items/1").text
    b = client.get("/budget?view=forecast").text
    assert "Fridge" in b and "$1,800" in b
    assert "Fridge" in client.get("/items?room=1").text


def test_document_upload_download(client):
    client.post("/items", data={"name": "Dishwasher"})
    client.post("/documents", files={"file": ("manual.pdf", b"%PDF-1.4 hi", "application/pdf")},
                data={"item_id": "1", "title": "Manual"})
    assert "Manual" in client.get("/items/1").text
    assert client.get("/documents/1/file").content == b"%PDF-1.4 hi"
    client.post("/documents/1/delete")
    assert client.get("/documents/1/file").status_code == 404


def test_contractor_spend(client):
    client.post("/contractors", data={"name": "Bob", "trade": "Plumber"})
    client.post("/tasks", data={"name": "Water heater", "interval_days": 365})
    client.post("/tasks/1/done", data={"cost": "200", "contractor_id": "1"})
    assert "$200" in client.get("/contractors").text


def test_notify_digest(client, monkeypatch):
    from app import notify
    today = date(2026, 6, 1)
    client.post("/tasks", data={"name": "Late", "interval_days": 30, "next_due": "2026-05-01"})
    client.post("/tasks", data={"name": "Far", "interval_days": 30, "next_due": "2026-09-01"})
    sent = []
    monkeypatch.setattr(notify, "send", lambda t, b: sent.append(b) or True)
    assert notify.run_check(today) == 1
    assert "Late (OVERDUE" in sent[0] and "Far" not in sent[0]
    assert notify.run_check(today) == 0  # once per day
    assert notify.run_check(today + timedelta(days=1)) == 1


def test_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    monkeypatch.setenv("HEARTH_USERNAME", "alan")
    monkeypatch.setenv("HEARTH_PASSWORD", "s3cret")
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("alan", "nope")).status_code == 401
        assert c.get("/", auth=("alan", "s3cret")).status_code == 200
        assert c.get("/api/health").status_code == 200


def _jpeg(color=(200, 80, 40), size=(800, 600)):
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, "JPEG"); return b.getvalue()


def test_item_photos(client, tmp_path):
    client.post("/items", data={"name": "Fridge"})
    r = client.post("/photos", data={"item_id": "1", "next": "/items/1"},
                    files=[("files", ("a.jpg", _jpeg(), "image/jpeg")),
                           ("files", ("b.jpg", _jpeg((0, 0, 255)), "image/jpeg"))],
                    follow_redirects=False)
    assert r.headers["location"] == "/items/1"
    assert client.get("/photos/1/thumb").content[:2] == b"\xff\xd8"
    assert 'src="/photos/1/thumb"' in client.get("/items").text
    client.post("/photos/2/cover", data={"next": "/items/1"})
    assert 'src="/photos/2/thumb"' in client.get("/items").text
    bad = client.post("/photos", data={"item_id": "1"}, files=[("files", ("x.jpg", b"nope", "image/jpeg"))])
    assert bad.status_code == 400
    client.post("/items/1/delete")
    assert client.get("/photos/1").status_code == 404
    assert [p.name for p in (tmp_path / "uploads").iterdir()] == []


def test_room_photo(client):
    client.post("/rooms", data={"name": "Kitchen"})
    client.post("/photos", data={"room_id": "1"}, files=[("files", ("k.png", _jpeg(), "image/png"))])
    assert '/photos/1/thumb' in client.get("/items").text
    assert "Room photo" in client.get("/items?room=1").text


def test_theme_cookie(client):
    r = client.post("/theme", data={"theme": "ocean", "mode": "dark"},
                    headers={"referer": "http://evil.example/items?room=2"}, follow_redirects=False)
    assert r.headers["location"] == "/items?room=2"
    client.cookies.set("theme", "ocean"); client.cookies.set("mode", "dark")
    html = client.get("/").text
    assert 'data-theme="ocean"' in html and 'data-mode="dark"' in html
    client.cookies.set("theme", "bogus")
    assert 'data-theme="ember"' in client.get("/").text


def test_unfiled_capture_and_filing(client):
    client.post("/rooms", data={"name": "Garage"})
    client.post("/items", data={"name": "Furnace"})
    r = client.post("/photos", data={"caption": "Leak by door"},
                    files=[("files", ("cap.jpg", _jpeg(), "image/jpeg"))], follow_redirects=False)
    assert r.headers["location"] == "/gallery"
    g = client.get("/gallery?show=unfiled").text
    assert "Leak by door" in g and "Unfiled" in g
    assert "Leak by door" in client.get("/gallery/1").text
    client.post("/gallery/1", data={"caption": "Leak by door", "item_id": "1", "room_id": "1"})
    assert "Leak by door" not in client.get("/gallery?show=unfiled").text
    assert 'src="/photos/1/thumb"' in client.get("/items/1").text
    assert "/photos/1/thumb" in client.get("/items").text  # room cover


def test_budget_balance(client):
    from app.main import balance_data
    client.post("/budget/year", data={"year": "2025", "amount": "1000"})
    client.post("/budget/entries", data={"kind": "allocation", "amount": "500", "entry_date": "2025-03-01"})
    client.post("/budget/entries", data={"kind": "expense", "amount": "200", "entry_date": "2025-06-10", "note": "Gutter guards"})
    client.post("/budget/year", data={"year": "2026", "amount": "2000"})
    client.post("/budget/entries", data={"kind": "allocation", "amount": "1000", "entry_date": "2026-01-05"})
    client.post("/tasks", data={"name": "HVAC service", "interval_days": 365})
    client.post("/tasks/1/done", data={"done_on": "2026-04-02", "cost": "150"})
    b = balance_data(2026)
    assert (b["budget"], b["allocated"], b["spent"], b["carry"]) == (2000, 1000, 150, 300)
    assert b["remaining"] == 1850 and b["unallocated"] == 1000 and b["fund_total"] == 1150
    assert b["months"][0]["alloc"] == 1000 and b["months"][3]["spent"] == 150
    assert [e["balance"] for e in b["ledger"]] == [1150, 1300]  # newest first
    html = client.get("/budget?year=2026").text
    assert "$1,150" in html and "Maintenance: HVAC service" in html and "$1,850" in html
    assert client.post("/budget/entries", data={"kind": "allocation", "amount": "-5"}).status_code == 400
    client.post("/budget/entries/2/delete")
    assert balance_data(2026)["carry"] == 500
    client.post("/budget/year", data={"year": "2026", "amount": ""})
    assert balance_data(2026)["budget"] is None


def test_caption_migration(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    con = sqlite3.connect(tmp_path / "hearth.db")
    con.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY, item_id INTEGER, room_id INTEGER, "
                "stored_name TEXT NOT NULL UNIQUE, thumb_name TEXT NOT NULL, "
                "is_cover INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    con.commit(); con.close()
    from app.db import init_db, get_db
    init_db()
    with get_db() as db:
        assert "caption" in {r["name"] for r in db.execute("PRAGMA table_info(photos)")}
