"""API v1 contract tests: auth, pairing, change feed, idempotent LWW upserts, files, soft delete."""
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

U = lambda: str(uuid.uuid4())  # noqa: E731


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    from app.api import create_pair_code
    code, _ = create_pair_code()
    r = client.post("/api/v1/pair", json={"pairCode": code, "deviceName": "Pixel 9"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------- auth & pairing ----------

def test_health_is_public_everything_else_needs_token(client):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["version"] and h["server_time"].endswith("Z")
    for path in ("/api/v1/tasks", "/api/v1/changes", "/api/v1/notify-config", "/api/v1/me"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_pairing_one_time_and_expiry(client):
    from app.api import create_pair_code
    from app.db import get_db
    code, exp = create_pair_code()
    assert len(code) == 8 and exp.endswith("Z")
    r = client.post("/api/v1/pair", json={"pairCode": code.lower(), "deviceName": "Phone"})
    assert r.status_code == 200 and r.json()["token"].startswith("hth_")
    assert client.post("/api/v1/pair", json={"pairCode": code, "deviceName": "Again"}).status_code == 401  # used
    code2, _ = create_pair_code()
    with get_db() as db:
        db.execute("UPDATE pair_codes SET expires_at='2000-01-01T00:00:00.000Z'")
    assert client.post("/api/v1/pair", json={"pairCode": code2, "deviceName": "Late"}).status_code == 401
    with get_db() as db:  # tokens are only stored hashed
        row = db.execute("SELECT token_hash FROM devices").fetchone()
        assert r.json()["token"] not in row[0] and len(row[0]) == 64


def test_revoked_token_stops_working(client, auth):
    from app.api import list_devices, revoke_device
    assert client.get("/api/v1/me", headers=auth).json()["name"] == "Pixel 9"
    dev = list_devices()[0]
    assert dev["last_seen_at"]
    revoke_device(dev["id"])
    assert client.get("/api/v1/me", headers=auth).status_code == 401


def test_pairing_rate_limited(client):
    from app import api as api_mod
    api_mod._attempts.clear()
    codes = [client.post("/api/v1/pair", json={"pairCode": "WRONG123", "deviceName": "x"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    api_mod._attempts.clear()


# ---------- upserts ----------

def test_upsert_create_update_idempotent_and_lww(client, auth):
    tid = U()
    body = {"name": "Clean gutters", "interval_days": 180, "next_due": "2027-04-01",
            "updated_at": "2026-11-01T10:00:00Z"}
    r1 = client.put(f"/api/v1/tasks/{tid}", json=body, headers=auth).json()
    assert r1["id"] == tid and r1["updated_at"] == "2026-11-01T10:00:00.000Z" and r1["kind"] == "chore"
    r2 = client.put(f"/api/v1/tasks/{tid}", json=body, headers=auth).json()
    assert r2 == r1  # idempotent: identical server copy, timestamps unchanged
    stale = client.put(f"/api/v1/tasks/{tid}", json={**body, "name": "OLD", "updated_at": "2026-10-01T00:00:00Z"},
                       headers=auth).json()
    assert stale["name"] == "Clean gutters"  # older write loses; server copy returned
    newer = client.put(f"/api/v1/tasks/{tid}", json={"name": "Clean gutters + downspouts",
                                                      "updated_at": "2026-11-02T08:00:00+02:00"}, headers=auth).json()
    assert newer["name"] == "Clean gutters + downspouts" and newer["updated_at"] == "2026-11-02T06:00:00.000Z"
    assert newer["interval_days"] == 180  # omitted fields are kept
    # web edits bump updated_at automatically and are visible through the API
    from app.db import get_db
    with get_db() as db:
        db.execute("UPDATE tasks SET notes='from web' WHERE uuid=?", (tid,))
    web = client.get(f"/api/v1/tasks/{tid}", headers=auth).json()
    assert web["notes"] == "from web" and web["updated_at"] > newer["updated_at"]
    # and the web page shows API-created data
    assert "Clean gutters + downspouts" in client.get("/tasks?show=all").text


def test_upsert_validation(client, auth):
    assert client.put(f"/api/v1/tasks/{U()}", json={"interval_days": 7}, headers=auth).status_code == 422
    assert client.put("/api/v1/tasks/not-a-uuid", json={"name": "x"}, headers=auth).status_code == 422
    assert client.put(f"/api/v1/tasks/{U()}", json={"name": "x", "kind": "weird"}, headers=auth).status_code == 422
    assert client.put(f"/api/v1/tasks/{U()}", json={"name": "x", "item_id": U()}, headers=auth).status_code == 422
    assert client.put(f"/api/v1/tasks/{U()}", json={"name": "x", "updated_at": "yesterday"},
                      headers=auth).status_code == 422
    assert client.put(f"/api/v1/ledger/{U()}", json={"kind": "expense"}, headers=auth).status_code == 422


def test_foreign_keys_are_uuids_and_nested_locations(client, auth):
    garage, shelf, bin_, item = U(), U(), U(), U()
    client.put(f"/api/v1/locations/{garage}", json={"name": "Garage"}, headers=auth)
    client.put(f"/api/v1/locations/{shelf}", json={"name": "Shelf 2", "parent_id": garage}, headers=auth)
    client.put(f"/api/v1/locations/{bin_}", json={"name": "Bin A", "parent_id": shelf}, headers=auth)
    it = client.put(f"/api/v1/items/{item}", json={"name": "Drill", "location_id": bin_}, headers=auth).json()
    assert it["location_id"] == bin_
    assert client.get(f"/api/v1/locations/{bin_}", headers=auth).json()["parent_id"] == shelf
    tid = U()
    client.put(f"/api/v1/tasks/{tid}", json={"name": "Charge batteries", "interval_days": 30,
                                             "next_due": "2026-12-01", "item_id": item}, headers=auth)
    c = client.put(f"/api/v1/completions/{U()}", json={"task_id": tid, "done_on": "2026-12-03", "cost": 4.5},
                   headers=auth).json()
    assert c["task_id"] == tid
    assert client.get(f"/api/v1/tasks/{tid}", headers=auth).json()["next_due"] == "2027-01-02"  # rescheduled


def test_lists_and_list_items_including_custom_lists(client, auth):
    lists = client.get("/api/v1/lists", headers=auth).json()
    assert {l["name"] for l in lists} == {"Groceries", "Supplies"}
    groceries = next(l["id"] for l in lists if l["kind"] == "groceries")
    milk = client.put(f"/api/v1/list-items/{U()}", json={"list_id": groceries, "name": "Milk", "qty": "2 gal"},
                      headers=auth).json()
    assert milk["status"] == "needed" and milk["list_id"] == groceries
    assert "Milk" in client.get("/shopping").text  # visible on the web list
    hw = U()
    client.put(f"/api/v1/lists/{hw}", json={"name": "Hardware store", "store": "Lowe's"}, headers=auth)
    r = client.put(f"/api/v1/list-items/{U()}", json={"list_id": hw, "name": "Wood screws"}, headers=auth)
    assert r.status_code == 200 and r.json()["list_id"] == hw


# ---------- delete ----------

def test_soft_delete_tombstone_and_resurrect(client, auth):
    cid = U()
    client.put(f"/api/v1/contractors/{cid}", json={"name": "Pat", "updated_at": "2026-11-01T00:00:00Z"}, headers=auth)
    d = client.delete(f"/api/v1/contractors/{cid}", headers=auth).json()
    assert d["id"] == cid and d["deleted_at"]
    assert client.get(f"/api/v1/contractors/{cid}", headers=auth).json()["deleted_at"]
    assert "Pat" not in client.get("/contractors").text
    old = client.put(f"/api/v1/contractors/{cid}", json={"name": "Pat", "updated_at": "2026-11-02T00:00:00Z"},
                     headers=auth).json()
    assert old.get("deleted_at")  # older than the delete -> stays deleted
    back = client.put(f"/api/v1/contractors/{cid}", json={"name": "Pat", "updated_at": "2099-01-01T00:00:00Z"},
                      headers=auth).json()
    assert back["deleted_at"] is None and back["name"] == "Pat"
    # delete via PUT deleted_at
    gone = client.put(f"/api/v1/contractors/{cid}", json={"deleted_at": "2099-01-02T00:00:00Z",
                                                           "updated_at": "2099-01-02T00:00:00Z"}, headers=auth).json()
    assert gone["deleted_at"]
    listed = client.get("/api/v1/contractors?include_deleted=true", headers=auth).json()
    assert [x["id"] for x in listed if x.get("deleted_at")] == [cid]


# ---------- change feed ----------

def test_changes_feed_initial_incremental_deletes_and_paging(client, auth):
    # web-created data appears in the feed
    client.post("/rooms", data={"name": "Kitchen"})
    client.post("/items", data={"name": "Fridge", "room_id": "1"})
    first = client.get("/api/v1/changes?since=0", headers=auth).json()
    types = {(c["type"], c["record"].get("name")) for c in first["changes"]}
    assert ("locations", "Kitchen") in types and ("items", "Fridge") in types
    assert first["hasMore"] is False and int(first["cursor"]) > 0
    fridge = next(c["record"] for c in first["changes"] if c["record"].get("name") == "Fridge")
    kitchen = next(c["record"] for c in first["changes"] if c["record"].get("name") == "Kitchen")
    assert fridge["location_id"] == kitchen["id"]
    # nothing new -> empty, same cursor
    again = client.get(f"/api/v1/changes?since={first['cursor']}", headers=auth).json()
    assert again["changes"] == [] and again["deleted"] == [] and again["cursor"] == first["cursor"]
    # update + delete since cursor (web delete with cascade to its photos)
    client.put(f"/api/v1/items/{fridge['id']}", json={"brand": "Samsung"}, headers=auth)
    client.post("/rooms/1/delete")
    inc = client.get(f"/api/v1/changes?since={first['cursor']}", headers=auth).json()
    assert {"type": "locations", "id": kitchen["id"]} in inc["deleted"]
    rec = next(c["record"] for c in inc["changes"] if c["type"] == "items")
    assert rec["brand"] == "Samsung" and rec["location_id"] is None  # FK set null on delete
    # paging
    for n in range(7):
        client.put(f"/api/v1/contractors/{U()}", json={"name": f"C{n}"}, headers=auth)
    cur, seen, pages = inc["cursor"], [], 0
    while True:
        p = client.get(f"/api/v1/changes?since={cur}&limit=3", headers=auth).json()
        seen += [c["record"]["name"] for c in p["changes"]]
        cur, pages = p["cursor"], pages + 1
        if not p["hasMore"]:
            break
    assert sorted(seen) == [f"C{n}" for n in range(7)] and pages == 3


def test_files_documents_and_photos(client, auth):
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (640, 480), (90, 120, 100)).save(b, "JPEG")
    fid = U()
    up = client.post("/api/v1/files", files={"file": ("label.jpg", b.getvalue(), "image/jpeg")},
                     data={"id": fid}, headers=auth).json()
    assert up == {"id": fid, "url": f"/api/v1/files/{fid}"}
    assert client.post("/api/v1/files", files={"file": ("label.jpg", b"x", "image/jpeg")}, data={"id": fid},
                       headers=auth).json()["id"] == fid  # idempotent retry
    assert client.get(up["url"], headers=auth).content == b.getvalue()
    assert client.get(up["url"]).status_code == 401
    item = U()
    client.put(f"/api/v1/items/{item}", json={"name": "Washer"}, headers=auth)
    ph = client.put(f"/api/v1/photos/{U()}", json={"file_id": fid, "item_id": item, "caption": "Label"},
                    headers=auth).json()
    assert ph["file_id"] == fid and ph["url"].endswith(fid)
    assert "/photos/1/thumb" in client.get("/items").text  # thumbnail made for the web UI
    doc = client.put(f"/api/v1/documents/{U()}", json={"file_id": fid, "title": "Manual", "item_id": item},
                     headers=auth).json()
    assert doc["filename"] == "label.jpg"
    # web uploads get file ids too
    client.post("/documents", files={"file": ("r.pdf", b"%PDF", "application/pdf")}, data={"title": "Receipt"})
    docs = client.get("/api/v1/documents", headers=auth).json()
    web = next(d for d in docs if d["title"] == "Receipt")
    assert client.get(web["url"], headers=auth).content == b"%PDF"


def test_notify_config(client, auth, monkeypatch):
    monkeypatch.setenv("APPRISE_URLS", "ntfys://ntfy.example.com/hearth-house")
    assert client.get("/api/v1/notify-config", headers=auth).json() == {
        "ntfyServer": "https://ntfy.example.com", "ntfyTopic": "hearth-house"}
    monkeypatch.setenv("APPRISE_URLS", "ntfy://my-topic")
    assert client.get("/api/v1/notify-config", headers=auth).json()["ntfyTopic"] == "my-topic"


def test_all_resource_types_round_trip(client, auth):
    from app.sync import RESOURCES
    assert set(RESOURCES) == {"tasks", "completions", "items", "locations", "documents", "photos", "contractors",
                              "lists", "list-items", "pantry", "ledger", "funds", "vehicles", "projects"}
    v, p = U(), U()
    assert client.put(f"/api/v1/vehicles/{v}", json={"name": "Outback", "odometer": 61000,
                      "odometer_log": [{"date": "2026-11-01", "miles": 61000}]}, headers=auth).json()["odometer_log"][0]["miles"] == 61000
    assert client.put(f"/api/v1/projects/{p}", json={"name": "Deck", "budget": 1500,
                      "materials": [{"name": "Stain", "qty": 2}]}, headers=auth).json()["materials"][0]["name"] == "Stain"
    assert client.put(f"/api/v1/pantry/{U()}", json={"name": "Rice", "qty": 1, "min_qty": 2, "unit": "bag"},
                      headers=auth).status_code == 200
    assert client.put(f"/api/v1/funds/{U()}", json={"name": "Roof", "target_amount": 15000}, headers=auth).status_code == 200
    assert client.put(f"/api/v1/ledger/{U()}", json={"kind": "expense", "amount": 12.5, "vehicle_id": v,
                      "project_id": p}, headers=auth).status_code == 200
    for t in RESOURCES:
        assert client.get(f"/api/v1/{t}", headers=auth).status_code == 200, t


def test_committed_openapi_matches(client):
    spec = client.get("/api/v1/openapi.json").json()
    committed = Path(__file__).parent.parent / "docs" / "openapi.json"
    assert committed.exists(), "run: python -m app.export_openapi"
    assert json.loads(committed.read_text()) == spec, "docs/openapi.json is stale: python -m app.export_openapi"
