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
    assert 'data-theme="hearth"' in client.get("/").text  # default theme


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


class FakeCal:
    def __init__(self):
        self.events, self.n, self.fail = {}, 0, False
    def insert(self, body):
        if self.fail:
            raise RuntimeError("Google Calendar 403: no access")
        self.n += 1; eid = f"ev{self.n}"; self.events[eid] = body; return eid
    def update(self, eid, body):
        if eid not in self.events:
            return None
        self.events[eid] = body; return eid
    def delete(self, eid):
        self.events.pop(eid, None)


@pytest.fixture
def fakecal(client, monkeypatch):
    from app import gcal
    cal = FakeCal()
    monkeypatch.setattr(gcal, "get_client", lambda: cal)
    monkeypatch.setattr(gcal, "configured", lambda: True)
    return cal


def test_gcal_task_events(client, fakecal):
    from app.db import set_setting
    set_setting("public_url", "http://hearth.lan")
    client.post("/items", data={"name": "Furnace"})
    client.post("/tasks", data={"name": "Change filter", "interval_days": 90, "next_due": "2026-12-01", "item_id": "1"})
    ev = fakecal.events["ev1"]
    assert ev["summary"] == "🔧 Change filter"
    assert ev["start"] == {"date": "2026-12-01"} and ev["end"] == {"date": "2026-12-02"}
    assert ev["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]
    assert "Item: Furnace" in ev["description"] and "http://hearth.lan/tasks/1" in ev["description"]
    client.post("/tasks/1/done", data={"done_on": "2026-12-03"})
    assert list(fakecal.events) == ["ev1"] and fakecal.events["ev1"]["start"]["date"] == "2027-03-03"
    assert "In Google Calendar" in client.get("/tasks/1").text
    fakecal.events.clear()  # deleted in Google -> recreated on next sync
    client.post("/tasks/1", data={"name": "Change filter", "interval_days": 90, "next_due": "2027-03-10"})
    assert fakecal.events["ev2"]["start"]["date"] == "2027-03-10"
    client.post("/tasks/1/delete")
    assert fakecal.events == {}


def test_gcal_appointments(client, fakecal):
    from app.db import set_setting
    set_setting("gcal_timezone", "America/Chicago")
    client.post("/contractors", data={"name": "Mike", "trade": "Plumber", "phone": "555-0142"})
    client.post("/schedule", data={"kind": "appointment", "title": "Fix sink", "date": "2026-12-05",
                                   "start_time": "10:00", "end_time": "", "contractor_id": "1",
                                   "location": "Home", "remind_minutes": "60"})
    ev = fakecal.events["ev1"]
    assert ev["summary"] == "Fix sink · Mike" and ev["location"] == "Home"
    assert ev["start"] == {"dateTime": "2026-12-05T10:00:00", "timeZone": "America/Chicago"}
    assert ev["end"]["dateTime"] == "2026-12-05T11:00:00"
    assert ev["reminders"]["overrides"][0]["minutes"] == 60 and "555-0142" in ev["description"]
    client.post("/schedule", data={"kind": "reminder", "title": "Take out bins", "date": "2026-12-06",
                                   "start_time": "19:30", "remind_minutes": "0"})
    r = fakecal.events["ev2"]
    assert r["summary"] == "🔔 Take out bins" and r["end"]["dateTime"] == "2026-12-06T19:45:00"
    client.post("/schedule", data={"kind": "appointment", "title": "Roof inspection", "date": "2026-12-08",
                                   "all_day": "1", "remind_minutes": ""})
    a = fakecal.events["ev3"]
    assert a["start"] == {"date": "2026-12-08"} and a["end"] == {"date": "2026-12-09"} and "reminders" not in a
    client.post("/schedule/1", data={"kind": "appointment", "title": "Fix sink", "date": "2026-12-05",
                                     "start_time": "13:00", "end_time": "14:30", "remind_minutes": "30"})
    assert fakecal.events["ev1"]["end"]["dateTime"] == "2026-12-05T14:30:00"
    page = client.get("/schedule").text
    assert "Fix sink" in page and "1:00 PM" in page and "Google" in page
    client.post("/schedule/2/delete")
    assert "ev2" not in fakecal.events
    assert client.post("/schedule", data={"title": "", "date": ""}).status_code == 400


def test_gcal_errors_recorded(client, fakecal):
    from app.db import get_setting
    fakecal.fail = True
    client.post("/tasks", data={"name": "X", "interval_days": 30})
    assert "403" in get_setting("gcal_last_error")
    fakecal.fail = False
    from app import gcal
    assert gcal.sync_all() == (1, 0) and get_setting("gcal_last_error") is None


def test_gcal_disabled_is_noop(client):
    client.post("/tasks", data={"name": "X", "interval_days": 30})
    from app.db import get_db
    with get_db() as db:
        assert db.execute("SELECT gcal_event_id FROM tasks").fetchone()[0] is None


def test_gcal_settings_and_key(client, tmp_path):
    import json
    r = client.post("/settings/google/key", files={"file": ("k.json", b'{"type":"user"}', "application/json")},
                    follow_redirects=False)
    assert "g=err" in r.headers["location"]
    key = json.dumps({"type": "service_account", "client_email": "hearth@proj.iam.gserviceaccount.com",
                      "private_key": "x"}).encode()
    client.post("/settings/google/key", files={"file": ("k.json", key, "application/json")})
    assert (tmp_path / "google-service-account.json").stat().st_mode & 0o777 == 0o600
    client.post("/settings/google", data={"calendar_id": "home@group.calendar.google.com",
                                          "timezone": "America/Denver", "task_remind": "540", "enabled": "1"})
    page = client.get("/settings").text
    assert "hearth@proj.iam.gserviceaccount.com" in page and "Connected" in page
    from app import gcal
    assert gcal.configured() and gcal.timezone() == "America/Denver" and gcal.task_remind_minutes() == 540
    client.post("/settings/google", data={"calendar_id": "home@group.calendar.google.com", "task_remind": "-5"})
    assert gcal.task_remind_minutes() == 0 and not gcal.enabled()


def test_calendar_http_handling():
    from app.gcal import Calendar
    class R:
        def __init__(self, code, body=None): self.status_code, self._b, self.text = code, body or {}, ""
        def json(self): return self._b
    class S:
        def __init__(self, r): self.r, self.calls = r, []
        def put(self, url, **kw): self.calls.append(url); return self.r
        delete = post = get = put
    c = Calendar.__new__(Calendar); c.base = "https://x/calendars/a%40b"
    c.s = S(R(404)); assert c.update("e1", {}) is None
    c.s = S(R(410)); c.delete("e1")
    c.s = S(R(403, {"error": {"message": "Forbidden"}}))
    with pytest.raises(RuntimeError, match="403: Forbidden"):
        c.insert({})


def test_notify_includes_appointments(client, monkeypatch):
    from app import notify
    client.post("/schedule", data={"kind": "appointment", "title": "Plumber", "date": "2026-06-02", "start_time": "10:00"})
    client.post("/schedule", data={"kind": "reminder", "title": "Far away", "date": "2026-06-20", "start_time": "10:00"})
    sent = []
    monkeypatch.setattr(notify, "send", lambda t, b: sent.append(b) or True)
    assert notify.run_check(date(2026, 6, 1)) == 1
    assert "📅 Plumber (tomorrow at 10:00)" in sent[0] and "Far away" not in sent[0]
    assert notify.run_check(date(2026, 6, 1)) == 0


def _shop(client):
    from app.db import get_db
    with get_db() as db:
        return {r["name"]: dict(r) for r in db.execute("SELECT * FROM shopping_items")}


def test_shopping_quick_add_and_categories(client):
    client.post("/shopping", data={"list": "groceries", "name": "2 gal milk, bread, 6 eggs, peanut butter"})
    s = _shop(client)
    assert s["milk"]["qty"] == "2 gal" and s["milk"]["category"] == "Dairy & Eggs"
    assert s["eggs"]["qty"] == "6" and s["peanut butter"]["category"] == "Pantry"
    client.post("/shopping", data={"list": "groceries", "name": "3 gal milk"})  # duplicate -> qty updated
    s = _shop(client)
    assert len(s) == 4 and s["milk"]["qty"] == "3 gal"
    html = client.get("/shopping").text
    import re
    heads = re.findall(r'class="dayhead">([^<]+?) <span', html)
    assert heads == ["Dairy &amp; Eggs", "Bakery", "Pantry"]  # store-layout order
    client.post("/shopping", data={"list": "supplies", "name": "AA batteries", "category": "Other"})
    assert _shop(client)["AA batteries"]["category"] == "Other"
    assert "AA batteries" not in client.get("/shopping?list=groceries").text
    assert client.post("/shopping", data={"list": "groceries", "name": "  "}).status_code == 400


def test_shopping_check_clear_and_budget(client):
    from app.main import balance_data
    client.post("/shopping", data={"list": "groceries", "name": "milk, bread"})
    r = client.post("/shopping/1/toggle", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 204 and _shop(client)["milk"]["status"] == "checked"
    assert "In the cart" in client.get("/shopping").text
    client.post("/shopping/2/toggle"); client.post("/shopping/2/toggle")  # check + uncheck
    assert _shop(client)["bread"]["status"] == "needed"
    client.post("/shopping/clear", data={"list": "groceries", "spent": "42.50", "store": "Aldi"})
    s = _shop(client)
    assert s["milk"]["status"] == "bought" and s["bread"]["status"] == "needed"
    led = balance_data(date.today().year)["ledger"]
    assert led[0]["amount"] == 42.5 and led[0]["note"] == "Groceries at Aldi (1 item)"
    page = client.get("/shopping").text
    assert "Buy again" in page and 'name="name" value="milk"' in page
    client.post("/shopping/again", data={"list": "groceries", "name": "milk"})
    client.post("/shopping/again", data={"list": "groceries", "name": "milk"})  # no dup
    from app.db import get_db
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM shopping_items WHERE name='milk' AND status='needed'").fetchone()[0] == 1
    client.post("/shopping/clear", data={"list": "groceries"})  # nothing checked, no expense
    assert len(balance_data(date.today().year)["ledger"]) == 1


def test_shopping_edit_delete_text_and_item_link(client):
    client.post("/items", data={"name": "Furnace", "model": "59SC5"})
    r = client.post("/shopping", data={"list": "supplies", "name": "20x25x1 filter", "qty": "2",
                                       "item_id": "1", "next": "/items/1"}, follow_redirects=False)
    assert r.headers["location"] == "/items/1"
    assert "for Furnace" in client.get("/shopping?list=supplies").text
    client.post("/shopping/1", data={"name": "20x25x4 filter", "qty": "1", "category": "Filters",
                                     "price": "38", "store": "Home Depot"})
    s = _shop(client)["20x25x4 filter"]
    assert (s["qty"], s["price"], s["store"]) == ("1", 38.0, "Home Depot")
    txt = client.get("/shopping/text?list=supplies").text
    assert "Supplies list" in txt and "[ ] 20x25x4 filter (1)" in txt and "Filters" in txt
    assert client.post("/shopping/1/delete", headers={"X-Requested-With": "fetch"}).status_code == 204
    assert _shop(client) == {}
    assert "items to get" not in client.get("/").text  # empty list -> no shopping tile


def test_quick_parse_units():
    from app.shopping import guess_category, parse_quick
    assert parse_quick("2x eggs") == ("eggs", "2")
    assert parse_quick("1.5 lb chicken") == ("chicken", "1.5 lb")
    assert parse_quick("20x25x1 furnace filter") == ("20x25x1 furnace filter", None)
    assert guess_category("water filter") == "Filters" and guess_category("ice cream") == "Frozen"
    assert guess_category("mystery thing") == "Other"


def test_about_and_credit(client):
    from app import __version__
    page = client.get("/settings").text
    assert "Developed by <b>Alan Nicholas</b>" in page and f"v{__version__}" in page
    assert "Developed by <a" in client.get("/").text
    assert client.get("/api/health").json()["version"] == __version__


# ---------------- v2: navigation, home, search, connections, backup ----------------

def test_navigation_sections(client):
    html = client.get("/tasks").text
    assert 'class="on">Plan' in html.replace("\n", "") or '>Plan<' in html
    assert 'href="/schedule"' in html and 'Schedule' in html  # sub-tab
    budget = client.get("/budget?view=forecast").text
    assert 'href="/budget?view=forecast" class="on"' in budget
    assert 'href="/budget" class=""' in budget
    org = client.get("/gallery").text
    assert 'href="/gallery" class="on"' in org and 'href="/contractors"' in org
    assert 'id="quick"' in html and 'name="next" value="photo"' in html  # quick add + capture


def test_home_attention_and_timeline(client):
    from app import home
    t = date.today()
    client.post("/tasks", data={"name": "clean polish wine fridge", "interval_days": 90,
                                "next_due": (t - timedelta(days=2)).isoformat()})
    client.post("/tasks", data={"name": "Gutters", "interval_days": 90, "next_due": (t + timedelta(days=3)).isoformat()})
    client.post("/schedule", data={"title": "brush stells", "date": t.isoformat(), "start_time": "15:00"})
    client.post("/items", data={"name": "TV", "warranty_until": (t + timedelta(days=20)).isoformat()})
    ev = home.timeline()
    assert [e["kind"] for e in ev] == ["task", "appointment", "task", "warranty"]
    assert home.attention(ev) == {"overdue": 1, "due_week": 1, "today_events": 1, "warranties": 1}
    html = client.get("/").text
    assert "Clean polish wine fridge" in html and "Brush stells" in html  # display capitalisation
    assert "Overdue" in html and "Up next" in html and "3:00 PM" in html
    from app.db import get_db
    with get_db() as db:  # stored exactly as typed
        assert db.execute("SELECT name FROM tasks WHERE id=1").fetchone()[0] == "clean polish wine fridge"


def test_home_empty_state_and_setup(client):
    html = client.get("/").text
    assert "All caught up" in html and "Get started" in html and "0 of 6 done" in html
    assert "Replacements due" not in html  # no empty forecast module
    client.post("/tasks/seed")
    assert "1 of 6 done" in client.get("/").text
    client.post("/setup/dismiss")
    assert "Get started" not in client.get("/").text


def test_universal_search(client):
    client.post("/rooms", data={"name": "Kitchen"})
    client.post("/items", data={"name": "Wine fridge", "brand": "Whynter", "model": "BWR-33", "room_id": "1"})
    client.post("/tasks", data={"name": "Clean wine fridge coils", "interval_days": 90})
    client.post("/contractors", data={"name": "Fridge Fixers", "trade": "Appliance repair"})
    client.post("/shopping", data={"list": "supplies", "name": "fridge filter"})
    html = client.get("/search?q=fridge").text
    for label in ("Inventory", "Tasks", "Contractors", "Shopping"):
        assert f"> {label}</h2>" in html
    assert "<mark>fridge</mark>" in html
    assert "Wine fridge" in client.get("/search?q=bwr-33").text  # model number
    assert "Wine fridge" in client.get("/search?q=kitchen").text  # room
    assert "Nothing matches" in client.get("/search?q=zzz").text
    assert client.get("/search?q=100%25_").status_code == 200  # LIKE wildcards escaped


def test_capture_to_item_and_receipt(client, tmp_path):
    r = client.post("/photos", data={"next": "photo"}, files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))],
                    follow_redirects=False)
    assert r.headers["location"] == "/gallery/1?new=1"
    assert "What is this?" in client.get("/gallery/1?new=1").text
    r = client.post("/gallery/1/to-item", data={"name": "Wine fridge"}, follow_redirects=False)
    assert r.headers["location"] == "/items/1?new=1"
    page = client.get("/items/1?new=1").text
    assert "Item created from your photo" in page and 'src="/photos/1"' in page
    client.post("/photos", data={"next": "photo"}, files=[("files", ("r.jpg", _jpeg((250, 250, 250)), "image/jpeg"))])
    r = client.post("/gallery/2/to-receipt", data={"title": "Receipt Best Buy", "item_id": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/items/1"
    assert "Receipt Best Buy" in client.get("/items/1").text
    assert client.get("/documents/1/file").content[:2] == b"\xff\xd8"
    assert client.get("/photos/2").status_code == 404  # moved, not duplicated


def test_item_page_connects_everything(client):
    client.post("/items", data={"name": "Furnace", "purchase_date": "2015-01-01", "life_years": "15",
                                "replace_cost": "5000", "warranty_until": "2000-01-01"})
    client.post("/contractors", data={"name": "Sarah Chen", "trade": "HVAC", "phone": "555-0110"})
    client.post("/tasks", data={"name": "Service furnace", "interval_days": 365, "item_id": "1"})
    client.post("/tasks/1/done", data={"done_on": "2026-01-10", "cost": "180", "contractor_id": "1"})
    client.post("/budget/entries", data={"kind": "expense", "amount": "75", "entry_date": "2026-02-01",
                                         "note": "Igniter", "item_id": "1"})
    client.post("/schedule", data={"title": "Annual tune-up", "date": "2099-10-01", "start_time": "09:00",
                                   "item_id": "1", "contractor_id": "1"})
    client.post("/shopping", data={"list": "supplies", "name": "16x25 filter", "item_id": "1"})
    page = client.get("/items/1").text
    for s in ("Service furnace", "Igniter", "$255", "Sarah Chen", "Annual tune-up", "16x25 filter",
              "Expired", "2030", "budget $5,000"):
        assert s in page, s
    c = client.get("/contractors/1").text
    assert "Service furnace" in c and "$180" in c and "Furnace" in c and "Annual tune-up" in c
    client.post("/contractors/1", data={"name": "Sarah Chen", "trade": "HVAC", "phone": "555-9999"})
    assert "555-9999" in client.get("/contractors/1").text
    assert 'value="1" selected' in client.get("/schedule?item=1").text


def test_warranty_alert_once(client, monkeypatch):
    from app import notify
    client.post("/items", data={"name": "TV", "warranty_until": "2026-06-20"})
    sent = []
    monkeypatch.setattr(notify, "send", lambda t, b: sent.append(b) or True)
    assert notify.run_check(date(2026, 6, 1)) == 1 and "TV warranty ends in 19 days" in sent[0]
    assert notify.run_check(date(2026, 6, 2)) == 0  # only once per expiry
    client.post("/items/1", data={"name": "TV", "warranty_until": "2026-06-25"})  # extended -> alerts again
    assert notify.run_check(date(2026, 6, 3)) == 1


def test_backup_export_import(client, tmp_path):
    import io, zipfile, json
    client.post("/items", data={"name": "Fridge", "price": "1200"})
    client.post("/photos", data={"item_id": "1"}, files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    (tmp_path / "google-service-account.json").write_text(json.dumps({"type": "service_account"}))
    r = client.get("/settings/backup.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "hearth.db" in names and "csv/items.csv" in names
    assert sum(n.startswith("uploads/") for n in names) == 2
    assert not any("google" in n for n in names)
    csv_text = client.get("/export/items.csv").text
    assert csv_text.splitlines()[0].startswith("name,room,brand") and "Fridge" in csv_text
    assert client.get("/export/nope.csv").status_code == 404
    data = ("Name,Room,Brand,Price,Purchase Date,Life Years\nWasher,Laundry,LG,$849.00,2020-03-14,11\n"
            "Fridge,Kitchen,,,,\n,,,,,\nDryer,Laundry,LG,1,200,,\n").encode()
    r = client.post("/settings/import/items", files={"file": ("i.csv", data, "text/csv")}, follow_redirects=False)
    assert "imp=ok" in r.headers["location"] and "Imported+2" in r.headers["location"] and "skipped+1" in r.headers["location"]
    from app.db import get_db
    with get_db() as db:
        w = db.execute("SELECT i.price, i.life_years, r.name FROM items i JOIN rooms r ON r.id=i.room_id "
                       "WHERE i.name='Washer'").fetchone()
        assert tuple(w) == (849.0, 11, "Laundry")
    bad = client.post("/settings/import/items", files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")},
                      follow_redirects=False)
    assert "imp=err" in bad.headers["location"]
