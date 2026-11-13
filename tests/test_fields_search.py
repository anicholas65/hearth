"""Formula engine, custom fields, and full-text search with filters."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import formula as F


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:
        yield c


# ---------------- formula engine ----------------

@pytest.mark.parametrize("src,env,want", [
    ("= 1 + 2 * 3", {}, 7),
    ("(price + upkeep) / max(age, 1)", {"price": 1000, "upkeep": 200, "age": 0.5}, 1200),
    ("2 ^ 10", {}, 1024),
    ("price / 0", {"price": 5}, None),                       # divide by zero → blank
    ("price * 2", {"price": None}, None),                    # blank propagates
    ('if(x > 5, "big", "small")', {"x": 9}, "big"),
    ('IF(x = 5, 1, 0)', {"x": 5}, 1),                         # spreadsheet style
    ("x <> 3 and not (x < 0)", {"x": 4}, True),
    ("coalesce(a, b, 7)", {"a": None, "b": None}, 7),
    ("round(10 / 3, 2)", {}, 3.33),
    ("min(a, b)", {"a": None, "b": 4}, 4),
    ("warranty - date(\"2026-01-01\")", {"warranty": "2026-01-31"}, 30),
    ("year(add_years(purchased, 10))", {"purchased": "2019-06-15"}, 2029),
    ("days_until(d)", {"d": (date.today() + timedelta(days=10)).isoformat()}, 10),
    ("Price * 2", {"price": 3}, 6),                           # case-insensitive names
])
def test_formula_values(src, env, want):
    assert F.evaluate(src, env) == want


@pytest.mark.parametrize("src,msg", [
    ("__import__('os').system('id')", "Unknown function"),
    ("price.__class__", "isn't allowed"),
    ("[1, 2]", "isn't allowed"),
    ("lambda: 1", "Can't read|isn't allowed"),
    ("open('x')", "Unknown function"),
    ("9 ^ 999", "too large"),
    ("1 +", "Can't read"),
    ("x" + " + 1" * 300, "under|too long"),
    ("nope + 1", "Unknown field"),
    ('"a" * 2', "needs numbers"),
])
def test_formula_rejects(src, msg):
    import re
    with pytest.raises(F.FormulaError) as e:
        F.evaluate(src, {"price": 1, "x": 1})
    assert re.search(msg, str(e.value))


# ---------------- custom fields ----------------

def _item(client, **kw):
    data = {"name": "Fridge", "price": "1200", "purchase_date": "2020-01-01", "life_years": "12",
            "replace_cost": "1500", **kw}
    if "room" in data:
        data.update(room_id="__new", new_room=data.pop("room"))
    client.post("/items", data=data)


def test_custom_fields_and_formulas(client):
    from app import fields as FD
    _item(client)
    _item(client, name="Washer", price="800", purchase_date="", life_years="", replace_cost="")
    r = client.post("/fields/items", data={"label": "Energy kWh", "type": "number"}, follow_redirects=False)
    assert "Added" in r.headers["location"].replace("+", " ") or "Added" in r.headers["location"]
    client.post("/fields/items", data={"label": "Monthly energy cost", "type": "formula", "format": "money",
                                       "formula": "= energy_kwh / 12 * 0.15"})
    client.post("/fields/items", data={"label": "Cost per year", "type": "formula", "format": "money",
                                       "formula": "(price + upkeep) / max(age, 1)"})
    client.post("/fields/items", data={"label": "Notes 2", "type": "text"})
    # values: bad number reported, good one saved
    r = client.post("/fields/items/1/values", data={"energy_kwh": "480", "notes_2": "garage spare"},
                    follow_redirects=False)
    assert r.status_code == 303
    rec = FD.record("items", 1)["values"]
    assert rec["energy_kwh"] == 480 and rec["monthly_energy_cost"] == pytest.approx(6.0)
    assert rec["cost_per_year"] == pytest.approx(1200 / FD._years_since("2020-01-01"))
    r = client.post("/fields/items/1/values", data={"energy_kwh": "lots"}, follow_redirects=False)
    assert "isn" in r.headers["location"]
    page = client.get("/items/1").text
    assert "Monthly energy cost" in page and "$6.00" in page and "Edit values" in page
    assert FD.record("items", 2)["values"]["monthly_energy_cost"] is None  # blank input → blank result
    # validation: unknown names, self-reference, cycles, reserved names, delete guarded by dependents
    bad = lambda **d: client.post("/fields/items", data={"type": "formula", **d}, follow_redirects=False) \
        .headers["location"].replace("+", " ")
    assert "Unknown field" in bad(label="X", formula="nope * 2")
    assert "already a field" in bad(label="Price", formula="1")
    assert "already a field" in bad(label="max", formula="1")
    f = client.post("/fields/items", data={"label": "A", "type": "formula", "formula": "1"})
    client.post("/fields/items", data={"label": "B", "type": "formula", "formula": "a + 1"})
    a_id = next(x["id"] for x in FD.custom_fields("items") if x["key"] == "a")
    r = client.post(f"/fields/edit/{a_id}", data={"label": "A", "formula": "b + 1"}, follow_redirects=False)
    assert "Circular" in r.headers["location"].replace("+", " ")
    r = client.post(f"/fields/edit/{a_id}", data={"label": "A", "formula": "a + 1"}, follow_redirects=False)
    assert "itself" in r.headers["location"].replace("+", " ")
    r = client.post(f"/fields/delete/{a_id}", follow_redirects=False)
    assert "uses this field" in r.headers["location"].replace("+", " ")
    # deleting a record removes its values
    client.post("/items/1/delete")
    from app.db import get_db
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM custom_values WHERE record_id=1 AND entity='items'").fetchone()[0] == 0
    assert f.status_code == 200


def test_preview_and_pages(client):
    _item(client)
    j = client.get("/fields/items/preview", params={"formula": "price * 2", "format": "money"}).json()
    assert j["ok"] and j["rows"][0] == {"title": "Fridge", "display": "$2,400.00", "error": None}
    assert client.get("/fields/items/preview", params={"formula": "price +"}).json()["ok"] is False
    for p in ("/fields", "/fields/items", "/fields/vehicles", "/fields/projects"):
        assert client.get(p).status_code == 200, p
    assert client.get("/fields/tasks").status_code == 404
    client.post("/fields/items", data={"label": "Cost per year", "type": "formula", "format": "money",
                                       "formula": "(price + upkeep) / max(age, 1)"})
    assert ">Added<" in client.get("/fields/items").text  # example marked as added


def test_vehicle_and_project_formulas(client):
    from app import fields as FD
    client.post("/vehicles", data={"name": "Civic", "price": "20000", "purchase_odometer": "1000", "odometer": "11000"})
    client.post("/fields/vehicles", data={"label": "Per mile", "type": "formula", "format": "money",
                                          "formula": "price / miles"})
    assert FD.record("vehicles", 1)["values"]["per_mile"] == pytest.approx(2.0)
    assert "Per mile" in client.get("/vehicles/1").text
    client.post("/projects", data={"name": "Deck", "budget": "1000"})
    client.post("/fields/projects", data={"label": "Left pct", "type": "formula", "format": "percent",
                                          "formula": "remaining / budget * 100"})
    assert FD.record("projects", 1)["values"]["left_pct"] == pytest.approx(100.0)


# ---------------- search ----------------

def test_fulltext_and_filters(client):
    from app.search import search_full
    _item(client, name="Refrigerator", brand="Whirlpool", model="WRF-555", room="Kitchen",
          warranty_until="2026-03-01")
    _item(client, name="Wine fridge", brand="Bosch", price="400", warranty_until="2030-01-01")
    _item(client, name="Dishwasher", brand="Bosch", price="900")
    titles = lambda q: [r["title"] for g in search_full(q, 50)["groups"] for r in g["rows"]]
    assert "Refrigerator" in titles("refrig")                     # prefix
    assert titles("wrf-555") == ["Refrigerator"]                    # model with punctuation
    assert titles("frig") == ["Refrigerator"]                       # mid-word fallback
    assert "Dishwasher" not in titles("bosch -dishwasher")
    assert set(titles("dishwasher OR refrigerator")) == {"Dishwasher", "Refrigerator"}
    assert titles('"wine fridge"') == ["Wine fridge"]
    assert set(titles("price>500 in:items")) == {"Refrigerator", "Dishwasher"}
    assert titles("bosch price<=400") == ["Wine fridge"]
    assert titles("warranty<2027") == ["Refrigerator"]
    assert titles("warranty:2030") == ["Wine fridge"]
    assert titles("in:items sort:-price")[:1] == ["Refrigerator"]
    assert search_full("color:red")["errors"][0].startswith("Unknown field")
    assert "isn't a number" in search_full("price>lots")["errors"][0]
    assert search_full("in:spaceships")["errors"]
    # index refreshes after edits (trigger marks it dirty)
    client.post("/items/3", data={"name": "Dish drawer", "brand": "Fisher"})
    assert titles("drawer") == ["Dish drawer"] and titles("dishwasher") == []


def test_search_custom_fields_and_formulas(client):
    from app.search import search_full
    _item(client, name="Fridge")
    _item(client, name="Dryer", price="300")
    client.post("/fields/items", data={"label": "Location note", "type": "text"})
    client.post("/fields/items", data={"label": "Double", "type": "formula", "formula": "price * 2"})
    client.post("/fields/items/1/values", data={"location_note": "basement corner"})
    titles = lambda q: [r["title"] for g in search_full(q, 50)["groups"] for r in g["rows"]]
    assert titles("basement") == ["Fridge"]                       # text custom fields indexed
    assert titles("double>1000") == ["Fridge"]                    # formulas filterable
    res = search_full("double>1000")
    assert res["groups"][0]["rows"][0]["facts"] == ["Double: 2,400"]


def test_search_page(client):
    _item(client, name="Fridge", room="Kitchen")
    html = client.get("/search", params={"q": "room:kitchen price>100"}).text
    assert "Fridge" in html and "Price paid: $1,200.00" in html and "How to search" in html
    assert "Unknown field" in client.get("/search", params={"q": "zzz:1"}).text
    assert client.get("/palette", params={"q": "fridge"}).json()["results"][0]["title"] == "Fridge"
