"""Vehicles (mileage-or-date maintenance, odometer, cost per mile) and Projects & Hobbies."""
from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:
        yield c


def q(sql):
    from app.db import get_db
    with get_db() as db:
        return [dict(r) for r in db.execute(sql)]


def test_vehicle_mileage_or_date_maintenance_and_cost_per_mile(client):
    r = client.post("/vehicles", data={"name": "Outback", "year": "2019", "make": "Subaru", "odometer": "60000"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/vehicles/1")
    client.post("/vehicles/1/maintenance", data={"name": "Oil change", "every_miles": "5000", "every_months": "6"})
    t = q("SELECT interval_days, interval_miles, next_due_miles, vehicle_id FROM tasks")[0]
    assert t == {"interval_days": 180, "interval_miles": 5000, "next_due_miles": 65000, "vehicle_id": 1}
    client.post("/vehicles/1/odometer", data={"miles": "64000"})
    assert q("SELECT next_due FROM tasks")[0]["next_due"] > date.today().isoformat()  # not yet
    r = client.post("/vehicles/1/odometer", data={"miles": "65200"}, follow_redirects=False)
    assert "now+due" in r.headers["location"] or "now%20due" in r.headers["location"]
    assert q("SELECT next_due FROM tasks")[0]["next_due"] == date.today().isoformat()  # miles hit first
    client.post("/tasks/1/done", data={"cost": "79", "odometer": "65200", "next": "/vehicles/1"})
    t = q("SELECT next_due_miles, next_due FROM tasks")[0]
    assert t["next_due_miles"] == 70200
    client.post("/budget/entries", data={"kind": "expense", "amount": "121", "vehicle_id": "1", "note": "Tires"})
    from app.db import get_db
    from app.modules import vehicle_summary
    with get_db() as db:
        s = vehicle_summary(db, db.execute("SELECT * FROM vehicles").fetchone())
    assert s["miles"] == 5200 and s["spent"] == 200 and round(s["cpm"], 4) == round(200 / 5200, 4)
    page = client.get("/vehicles/1").text
    assert "Oil change" in page and "65,200 mi" in page and "Tires" in page
    assert "Outback" in client.get("/vehicles").text


def test_projects_budget_materials_stock_photos(client):
    client.post("/items", data={"name": "Circular saw"})
    client.post("/projects", data={"name": "Back deck", "budget": "1500", "status": "active"})
    client.post("/projects/1/materials", data={"name": "Deck boards", "qty": "20", "cost": "18.5"})
    client.post("/projects/1/materials", data={"name": "Saw", "item_id": "1"})
    client.post("/budget/entries", data={"kind": "expense", "amount": "420", "project_id": "1", "note": "Lumber"})
    client.post("/projects/1/stock", data={"name": "Deck stain", "qty": "0", "min_qty": "1", "unit": "gal"})
    from app.db import get_db
    from app.modules import project_summary
    with get_db() as db:
        s = project_summary(db, db.execute("SELECT * FROM projects").fetchone())
    assert s["spent"] == 420 and s["planned"] == 370 and round(s["pct"]) == 28
    assert q("SELECT name FROM shopping_items") == [{"name": "Deck stain"}]  # consumable below min
    page = client.get("/projects/1").text
    assert "Deck boards" in page and "Circular saw" in page and "Lumber" in page and "Deck stain" in page
    client.post("/projects/1/materials", data={"name": "x", "remove": "0"})
    with get_db() as db:
        assert len(project_summary(db, db.execute("SELECT * FROM projects").fetchone())["materials"]) == 1
    client.post("/projects", data={"name": "Film photography", "kind": "hobby"})
    assert "Film photography" in client.get("/projects?kind=hobby").text
    assert "Back deck" not in client.get("/projects?kind=hobby").text


def test_stuff_chips_include_vehicles_and_hobbies(client):
    html = client.get("/items").text
    assert 'href="/vehicles"' in html and 'href="/projects"' in html and ">Stuff<" in html


def test_demo_mode_is_isolated(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:  # real data first
        c.post("/items", data={"name": "My real fridge"})
    monkeypatch.setenv("HEARTH_DEMO", "1")
    monkeypatch.setenv("APPRISE_URLS", "json://localhost:1/never")
    with TestClient(app) as c:
        html = c.get("/").text
        assert "Demo mode" in html
        inv = c.get("/items").text
        assert "Refrigerator" in inv and "My real fridge" not in inv
        from app import gcal, notify
        assert notify.send("t", "b") is False and gcal.enabled() is False
    assert (tmp_path / "demo" / "hearth.db").exists() and (tmp_path / "demo" / "DEMO_DATA_ONLY").exists()
    real = sqlite3.connect(tmp_path / "hearth.db").execute("SELECT name FROM items").fetchall()
    assert real == [("My real fridge",)]  # untouched
    monkeypatch.delenv("HEARTH_DEMO")
    with TestClient(app) as c:
        assert "Demo mode" not in c.get("/").text and "My real fridge" in c.get("/items").text
