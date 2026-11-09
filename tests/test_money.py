"""Sinking funds, cost of ownership, insurance packet, warranty alerts."""
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


def test_item_sinking_funds_and_custom(client):
    from app.finance import sinking_funds
    today = date(2026, 1, 15)
    client.post("/items", data={"name": "Water heater", "purchase_date": "2017-01-10", "life_years": "12",
                                "replace_cost": "1368"})   # due 2029-01 -> 36 months
    client.post("/items", data={"name": "Old fridge", "purchase_date": "2010-05-01", "life_years": "10",
                                "replace_cost": "1500"})   # past life
    client.post("/items", data={"name": "Sofa"})            # no data -> not funded
    f = sinking_funds(today)
    wh = next(r for r in f["rows"] if r["name"] == "Water heater")
    assert wh["months"] == 36 and wh["monthly"] == 38 and wh["label"] == "Set aside $38/mo — water heater due 2029"
    old = next(r for r in f["rows"] if r["name"] == "Old fridge")
    assert old["overdue"] and old["monthly"] == 0 and f["due_now"] == 1500
    client.post("/funds", data={"name": "Water heater fund", "item_id": "1", "balance": "468"})
    wh = next(r for r in sinking_funds(today)["rows"] if r["name"] == "Water heater")
    assert wh["saved"] == 468 and wh["monthly"] == 25  # (1368-468)/36
    client.post("/funds", data={"name": "Roof", "target_amount": "12000", "target_date": "2031-01-15", "balance": "0"})
    roof = next(r for r in sinking_funds(today)["rows"] if r["name"] == "Roof")
    assert roof["monthly"] == 200 and roof["kind"] == "custom"
    page = client.get("/funds").text
    assert "Set aside" in page and "Roof" in page and "Insurance packet" in page
    assert ">Funds<" in client.get("/budget").text or "Funds" in client.get("/budget").text


def test_cost_of_ownership(client):
    from app.finance import cost_of_ownership
    client.post("/items", data={"name": "Furnace", "price": "3800", "purchase_date": "2012-10-10",
                                "life_years": "18", "replace_cost": "5200"})
    client.post("/tasks", data={"name": "Service furnace", "repeat": "365", "item_id": "1"})
    client.post("/tasks/1/done", data={"done_on": "2025-10-01", "cost": "180"})
    client.post("/budget/entries", data={"kind": "expense", "amount": "75", "entry_date": "2026-02-01",
                                         "note": "Igniter", "item_id": "1"})
    c = cost_of_ownership(1, date(2026, 10, 10))
    assert [e["label"] for e in c["events"]] == ["Purchase", "Service furnace", "Igniter"]
    assert c["spent"] == 4055 and c["maint"] == 255 and round(c["per_year"]) == 290
    assert c["projected"]["d"].startswith("2030-10") and c["projected"]["total"] == 9255
    assert "Cost of ownership" in client.get("/items/1").text


def test_insurance_packet_pdf(client):
    import io
    from PIL import Image
    client.post("/rooms", data={"name": "Kitchen"})
    b = io.BytesIO(); Image.new("RGB", (800, 600), (40, 80, 60)).save(b, "JPEG")
    client.post("/items", data={"name": "Refrigerator", "room_id": "1", "brand": "Samsung", "model": "RF28",
                                "serial": "SN123", "price": "2199", "purchase_date": "2019-04-12"},
                files={"photo": ("f.jpg", b.getvalue(), "image/jpeg")})
    client.post("/documents", data={"title": "Fridge receipt", "item_id": "1"},
                files={"file": ("r.pdf", b"%PDF", "application/pdf")})
    client.post("/items", data={"name": "Bike"})
    from app.reports import packet_data
    d = packet_data(None)
    assert [g["room"] for g in d["groups"]] == ["Kitchen", "No room"] and d["value"] == 2199
    assert d["groups"][0]["items"][0]["docs"] == ["Fridge receipt"]
    r = client.get("/reports/insurance.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-"
    assert len(r.content) > 5000  # includes the photo
    room = client.get("/reports/insurance.pdf?room=1")
    assert room.status_code == 200 and "room-1" in room.headers["content-disposition"]
    assert [g["room"] for g in packet_data(1)["groups"]] == ["Kitchen"]


def test_warranty_alert_30_days_via_existing_channels(client, monkeypatch):
    from datetime import timedelta
    from app import notify
    t = date.today()
    client.post("/items", data={"name": "TV", "warranty_until": (t + timedelta(days=29)).isoformat()})
    client.post("/items", data={"name": "Laptop", "warranty_until": (t + timedelta(days=45)).isoformat()})
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body: sent.append(body) or True)
    notify.run_check(t)
    assert "TV warranty ends in 29 days" in sent[0] and "Laptop" not in sent[0]
