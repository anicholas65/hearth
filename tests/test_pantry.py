"""Pantry minimums -> shopping, use-soon, restock on purchase, per-store aisle order, custom lists."""
from datetime import date, timedelta
import uuid

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


def test_below_minimum_auto_adds_once(client):
    client.post("/pantry", data={"name": "Coffee beans", "qty": "2", "min_qty": "1", "unit": "bag"})
    assert q("SELECT * FROM shopping_items") == []
    client.post("/pantry/1/adjust", data={"delta": "-1"})
    client.post("/pantry/1/adjust", data={"delta": "-1"})
    client.post("/pantry/1/adjust", data={"delta": "-1"})  # floors at 0, still only one list entry
    s = q("SELECT name, qty, note, list FROM shopping_items")
    assert s == [{"name": "Coffee beans", "qty": "1 bag", "note": "low in pantry", "list": "groceries"}]
    assert q("SELECT qty FROM pantry_items") == [{"qty": 0.0}]
    page = client.get("/pantry").text
    assert "Running low" in page and "Coffee beans" in page


def test_buying_restocks_pantry(client):
    client.post("/pantry", data={"name": "Rice", "qty": "0", "min_qty": "1"})
    assert q("SELECT name FROM shopping_items") == [{"name": "Rice"}]
    client.post("/shopping/1/toggle", data={"to": "checked"})
    client.post("/shopping/clear", data={"list": "groceries"})
    assert q("SELECT qty FROM pantry_items") == [{"qty": 1.0}]


def test_use_soon_list(client):
    t = date.today()
    client.post("/pantry", data={"name": "Milk", "qty": "1", "best_before": (t + timedelta(days=2)).isoformat()})
    client.post("/pantry", data={"name": "Yogurt", "qty": "1", "best_before": (t - timedelta(days=1)).isoformat()})
    client.post("/pantry", data={"name": "Honey", "qty": "1", "best_before": (t + timedelta(days=200)).isoformat()})
    page = client.get("/pantry").text
    soon = page.split("Use soon")[1].split("</section>")[0]
    assert "Milk" in soon and "Yogurt" in soon and "Expired" in soon and "Honey" not in soon


def test_api_pantry_write_triggers_restock(client):
    from app.api import create_pair_code
    code, _ = create_pair_code()
    tok = client.post("/api/v1/pair", json={"pairCode": code, "deviceName": "t"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    client.put(f"/api/v1/pantry/{uuid.uuid4()}", json={"name": "Filament PLA", "qty": 0, "min_qty": 2,
                                                       "unit": "spool", "category": "Consumables"}, headers=h)
    assert q("SELECT name, qty FROM shopping_items") == [{"name": "Filament PLA", "qty": "2 spool"}]


def test_store_aisle_order(client):
    client.post("/shopping", data={"list": "groceries", "name": "milk, apples, bread"})
    import re
    heads = lambda html: re.findall(r'class="dayhead">([^<]+?) <span', html)  # noqa: E731
    assert heads(client.get("/shopping").text) == ["Produce", "Dairy &amp; Eggs", "Bakery"]
    client.post("/stores/aisles", data={"store": "Aldi", "order": "Bakery\nDairy & Eggs\nProduce"})
    assert heads(client.get("/shopping?store=Aldi").text) == ["Bakery", "Dairy &amp; Eggs", "Produce"]
    assert "Aldi" in client.get("/shopping").text  # offered in the store picker


def test_custom_api_list_shows_on_web(client):
    from app.api import create_pair_code
    code, _ = create_pair_code()
    h = {"Authorization": "Bearer " + client.post("/api/v1/pair", json={"pairCode": code, "deviceName": "t"}).json()["token"]}
    lid = str(uuid.uuid4())
    client.put(f"/api/v1/lists/{lid}", json={"name": "Hardware store"}, headers=h)
    client.put(f"/api/v1/list-items/{uuid.uuid4()}", json={"list_id": lid, "name": "Wood screws"}, headers=h)
    page = client.get("/shopping").text
    assert "Hardware store" in page
    key = q("SELECT key FROM lists WHERE name='Hardware store'")[0]["key"]
    assert "Wood screws" in client.get(f"/shopping?list={key}").text
