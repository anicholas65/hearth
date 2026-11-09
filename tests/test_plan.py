"""Task staggering, rebalance, snooze, freshness and the command palette."""
from collections import Counter
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


def tasks():
    from app.db import get_db
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY id")]


def test_seeded_tasks_are_staggered_within_their_intervals(client):
    client.post("/tasks/seed")
    ts, today = tasks(), date.today()
    assert len(ts) == 12
    per_day = Counter(t["next_due"] for t in ts)
    assert max(per_day.values()) == 1, per_day  # the old bug: all 12 on one day
    for t in ts:
        d = (date.fromisoformat(t["next_due"]) - today).days
        assert 1 <= d <= t["interval_days"], (t["name"], d)
    assert {t["name"] for t in ts if t["kind"] == "safety"} == {
        "Test smoke & CO detectors", "Replace smoke detector batteries", "Test GFCI outlets",
        "Check fire extinguisher pressure"}


def test_stagger_prefers_quiet_days():
    from app.plan import stagger_date
    today = date(2026, 1, 1)
    busy = [(today + timedelta(days=k)).isoformat() for k in range(1, 8)] * 3
    d = date.fromisoformat(stagger_date(30, busy, today))
    assert (d - today).days >= 9  # skips the crowded first week and its neighbour
    assert stagger_date(1, [], today) == "2026-01-02"
    assert stagger_date(0, [], today) == "2026-01-02"


def test_new_recurring_task_without_date_is_staggered(client):
    t = date.today()
    for n in range(4):
        client.post("/tasks", data={"name": f"Fixed {n}", "repeat": "30", "next_due": (t + timedelta(days=1)).isoformat()})
    client.post("/tasks", data={"name": "Auto", "repeat": "30", "next_due": ""})
    auto = next(x for x in tasks() if x["name"] == "Auto")
    assert auto["next_due"] not in {(t + timedelta(days=k)).isoformat() for k in (1, 2)}


def test_rebalance_spreads_chores_and_keeps_safety(client):
    pile = (date.today() + timedelta(days=5)).isoformat()
    for n in range(6):
        client.post("/tasks", data={"name": f"Chore {n}", "repeat": "90", "next_due": pile})
    client.post("/tasks", data={"name": "Smoke alarm test", "repeat": "30", "next_due": pile, "kind": "safety"})
    assert "Rebalance schedule" in client.get("/tasks").text
    client.post("/tasks/rebalance")
    ts = tasks()
    chores = [t for t in ts if t["kind"] == "chore"]
    assert len({t["next_due"] for t in chores}) == 6
    assert next(t for t in ts if t["kind"] == "safety")["next_due"] == pile
    assert "Rebalance schedule" not in client.get("/tasks").text


def test_snooze_chore_vs_safety(client):
    today = date.today()
    client.post("/tasks", data={"name": "Dust", "repeat": "30", "next_due": (today - timedelta(days=2)).isoformat()})
    client.post("/tasks", data={"name": "Smoke test", "repeat": "30", "next_due": today.isoformat(), "kind": "safety"})
    assert client.post("/tasks/1/snooze", headers={"X-Requested-With": "fetch"}).status_code == 204
    assert client.post("/tasks/2/snooze", data={"days": "10"}, follow_redirects=False).status_code == 303
    ts = tasks()
    assert ts[0]["next_due"] == (today + timedelta(days=3)).isoformat()
    assert ts[1]["next_due"] == (today + timedelta(days=1)).isoformat()  # safety: max 1 day


def test_chores_show_freshness_not_red(client):
    from app.plan import freshness
    assert freshness("2026-01-31", 30, date(2026, 1, 1)) == 1.0
    assert freshness("2026-01-16", 30, date(2026, 1, 1)) == 0.5
    assert freshness("2025-12-01", 30, date(2026, 1, 1)) == 0.0
    client.post("/tasks", data={"name": "Vacuum", "repeat": "7", "next_due": "2020-01-01"})
    html = client.get("/tasks").text
    assert 'class="fresh"' in html and "Ready" in html and "Overdue by" not in html
    assert client.get("/").text.count("Safety overdue") == 0


def test_day_cleared_flag(client):
    client.post("/tasks", data={"name": "Only one", "repeat": "7", "next_due": date.today().isoformat()})
    r = client.post("/tasks/1/done", data={"next": "/"}, follow_redirects=False)
    assert "cleared=1" in r.headers["location"]


def test_palette_interpret():
    from app.palette import interpret, parse_date
    today = date(2026, 11, 9)
    assert interpret("add milk to list")["kind"] == "shop"
    a = interpret("add caulk and wood screws to supplies")
    assert a["list"] == "supplies" and "wood screws" in a["label"]
    assert interpret("buy 2 gal milk")["label"] == "Add milk to Groceries"
    t = interpret("new task clean dryer vent every 6 months")
    assert (t["name"], t["interval"]) == ("clean dryer vent", 180)
    assert interpret("go budget") == {"kind": "go", "href": "/budget", "label": "Go to Budget"}
    assert interpret("forecast")["href"] == "/budget?view=forecast"
    assert interpret("whirlpool manual")["kind"] == "search"
    assert parse_date("log filter replaced 12/1", today)[0] == date(2025, 12, 1)  # future -> last year
    assert parse_date("done 11/2", today)[0] == date(2026, 11, 2)
    assert parse_date("did it yesterday", today)[0] == date(2026, 11, 8)
    assert parse_date("on Nov 3", today)[0] == date(2026, 11, 3)


def test_palette_runs_actions(client):
    client.post("/tasks", data={"name": "Replace HVAC air filter", "repeat": "90", "next_due": "2026-12-01"})
    r = client.get("/palette?q=log filter replaced 11/1").json()
    assert r["action"]["kind"] == "done" and "Replace HVAC air filter" in r["action"]["label"]
    out = client.post("/palette/run", json={"text": "log filter replaced 11/1"}).json()
    assert out["ok"] and out["href"] == "/tasks/1"
    t = tasks()[0]
    assert t["next_due"] == (date(date.today().year if date(date.today().year, 11, 1) <= date.today()
                                  else date.today().year - 1, 11, 1) + timedelta(days=90)).isoformat()
    client.post("/palette/run", json={"text": "add eggs, bread to list"})
    assert "eggs" in client.get("/shopping").text
    client.post("/palette/run", json={"text": "new task wash windows every 3 months"})
    assert any(x["name"] == "wash windows" and x["interval_days"] == 90 for x in tasks())
    assert client.post("/palette/run", json={"text": "go settings"}).json()["href"] == "/settings"
    assert client.get("/palette?q=filter").json()["results"][0]["title"] == "Replace HVAC air filter"


def test_nav_today_plan_stuff_money_and_dark_default(client):
    html = client.get("/").text
    for label in (">Today<", ">Plan", ">Stuff<", ">Money<"):
        assert label in html, label
    assert 'data-mode="dark"' in html
    client.cookies.set("mode", "light")
    assert 'data-mode="light"' in client.get("/").text
