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
    for p in ["/", "/tasks", "/items", "/documents", "/contractors", "/budget", "/settings"]:
        assert client.get(p).status_code == 200, p


def test_task_lifecycle(client):
    client.post("/tasks", data={"name": "Filter", "interval_days": 90, "next_due": "2020-01-01"})
    assert "overdue" in client.get("/tasks").text
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
    b = client.get("/budget").text
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
