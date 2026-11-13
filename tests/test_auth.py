"""Web sign-in: first-run account, login/logout, sessions, account management, throttling."""
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth
HTML = {"accept": "text/html"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app import auth
    auth._fails.clear()
    from app.main import app
    with TestClient(app, follow_redirects=False) as client:
        yield client


def welcome(c, username="alan", password="correct horse", name="Alan"):
    return c.post("/welcome", data={"name": name, "username": username, "password": password, "confirm": password})


def login(c, username="alan", password="correct horse", next="/"):
    return c.post("/login", data={"username": username, "password": password, "next": next})


def test_first_visit_creates_owner(c):
    r = c.get("/tasks", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    assert c.get("/tasks").status_code == 401  # non-browser callers get 401, not a redirect
    assert "Create your account" in c.get("/welcome").text
    assert welcome(c, password="short").status_code == 400
    r = welcome(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "hearth_session" in r.cookies and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/").status_code == 200
    assert "Signed in as" in c.get("/").text
    # welcome is closed once an account exists
    assert c.get("/welcome").headers["location"] == "/"
    c.cookies.clear()
    assert welcome(c, username="mallory").headers["location"] == "/login"
    from app.auth import list_accounts
    assert [(a["login"], a["role"]) for a in list_accounts()] == [("alan", "owner")]


def test_login_logout_and_next(c):
    welcome(c)
    c.cookies.clear()
    r = c.get("/budget?year=2025", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fbudget%3Fyear%3D2025"
    assert login(c, password="wrong").status_code == 401
    r = login(c, username="ALAN ", next="/budget?year=2025")
    assert r.status_code == 303 and r.headers["location"] == "/budget?year=2025"
    assert c.get("/settings").status_code == 200
    assert c.post("/logout").headers["location"] == "/login"
    assert c.get("/settings").status_code == 401


@pytest.mark.parametrize("bad", ["https://evil.example", "//evil.example", "/\\evil", "/logout"])
def test_next_is_never_offsite(c, bad):
    welcome(c)
    c.cookies.clear()
    assert login(c, next=bad).headers["location"] == "/"


def test_public_paths(c):
    welcome(c)
    c.cookies.clear()
    for p in ["/login", "/static/style.css", "/sw.js", "/api/health", "/api/v1/health"]:
        assert c.get(p).status_code == 200, p
    assert c.get("/api/v1/items").status_code == 401  # device tokens, unchanged


def test_basic_auth_against_accounts(c):
    welcome(c)
    c.cookies.clear()
    assert c.get("/", auth=("alan", "correct horse")).status_code == 200
    assert c.get("/", auth=("alan", "nope")).status_code == 401


def test_session_is_hashed_and_revoked_on_password_change(c):
    from app.db import get_db
    welcome(c)
    token = c.cookies.get("hearth_session")
    with get_db() as db:
        stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != token and len(stored) == 64
    other = TestClient(c.app, follow_redirects=False)
    other.cookies.set("hearth_session", token)
    assert other.get("/").status_code == 200
    r = c.post("/settings/accounts/1/password", data={"current": "correct horse", "password": "new password 1"})
    assert r.status_code == 303 and c.get("/").status_code == 200  # this browser stays signed in
    assert other.get("/").status_code == 401  # every other session is gone
    assert c.post("/settings/accounts/1/password", data={"current": "wrong", "password": "another one"}) \
        .headers["location"].endswith("#accounts") and login(c, password="new password 1").status_code == 303


def test_owner_manages_household(c):
    welcome(c)
    r = c.post("/settings/accounts", data={"name": "Sam", "username": "sam", "password": "sam-pass-1"})
    assert "Added+Sam" in r.headers["location"] or "Added%20Sam" in r.headers["location"]
    assert "taken" in c.post("/settings/accounts", data={"username": "sam", "password": "xxxxxxxx"}) \
        .headers["location"].replace("+", " ")
    assert "spaces" in c.post("/settings/accounts", data={"username": "a b", "password": "xxxxxxxx"}) \
        .headers["location"].replace("+", " ")
    page = c.get("/settings").text
    assert "Sam" in page and "Add someone" in page
    # members can sign in but can't manage others
    sam = TestClient(c.app, follow_redirects=False)
    assert login(sam, "sam", "sam-pass-1").status_code == 303
    assert "Add someone" not in sam.get("/settings").text
    assert sam.post("/settings/accounts", data={"username": "eve", "password": "xxxxxxxx"}).status_code == 403
    assert sam.post("/settings/accounts/1/password", data={"password": "hijacked!"}).status_code == 403
    assert sam.post("/settings/accounts/1/delete").status_code == 403
    # owner resets Sam's password, then removes Sam
    assert c.post("/settings/accounts/2/password", data={"password": "reset-pass"}).status_code == 303
    assert sam.get("/").status_code == 401
    assert "own account" in c.post("/settings/accounts/1/delete").headers["location"].replace("+", " ")
    c.post("/settings/accounts/2/delete")
    assert login(sam, "sam", "reset-pass").status_code == 401


def test_login_throttled(c):
    welcome(c)
    c.cookies.clear()
    for _ in range(10):
        assert login(c, password="wrong").status_code == 401
    assert login(c).status_code == 429


def test_env_login_becomes_owner_account(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    monkeypatch.setenv("HEARTH_USERNAME", "Alan")
    monkeypatch.setenv("HEARTH_PASSWORD", "pw")  # short legacy passwords still work
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/login").status_code == 200
        assert login(c, "alan", "pw").status_code == 303
    from app.auth import list_accounts
    assert [(a["login"], a["role"]) for a in list_accounts()] == [("alan", "owner")]


def test_auth_off_and_demo(c, monkeypatch):
    monkeypatch.setenv("HEARTH_AUTH", "off")
    assert c.get("/").status_code == 200
    assert c.get("/login").headers["location"] == "/"


def test_migration_v3_reversible(tmp_path):
    from app.migrations import current_version, migrate_down, migrate_up
    import sqlite3
    db = tmp_path / "hearth.db"
    from app.db import SCHEMA
    con = sqlite3.connect(db); con.executescript(SCHEMA); con.commit(); con.close()
    migrate_up(db)
    con = sqlite3.connect(db)
    assert current_version(con) == 4
    assert "password_hash" in {r[1] for r in con.execute("PRAGMA table_info(members)")}
    con.close()
    migrate_down(db, 2)
    con = sqlite3.connect(db)
    assert "password_hash" not in {r[1] for r in con.execute("PRAGMA table_info(members)")}
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone()
