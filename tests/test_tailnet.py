"""Tailscale identity header trust rule."""
import pytest
from fastapi.testclient import TestClient

TS = {"Tailscale-User-Login": "alan@example.com", "Tailscale-User-Name": "Alan Nicholas"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    monkeypatch.setenv("HEARTH_USERNAME", "u")
    monkeypatch.setenv("HEARTH_PASSWORD", "p")
    from app.main import app
    return app


def members():
    from app.db import get_db
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT login, name, role FROM members")]


def test_headers_from_lan_are_stripped(app):
    with TestClient(app, client=("192.168.1.50", 50000)) as c:
        assert c.get("/", headers=TS).status_code == 401  # not signed in; basic auth still required
        spoof = {**TS, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1"}
        assert c.get("/", headers=spoof).status_code == 401
        assert c.get("/", headers=TS, auth=("u", "p")).status_code == 200
        assert members() == []  # header never reached the app


def test_headers_from_loopback_sign_in(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.get("/", headers=TS)
        assert r.status_code == 200 and "Alan Nicholas" in c.get("/settings", headers=TS).text
        assert members() == [{"login": "alan@example.com", "name": "Alan Nicholas", "role": "owner"}]
        c.get("/", headers={"Tailscale-User-Login": "sam@example.com", "Tailscale-User-Name": "Sam"})
        assert [m["role"] for m in members()] == ["owner", "member"]
        assert c.get("/").status_code == 401  # loopback alone is not a sign-in
    with TestClient(app, client=("::1", 50000)) as c:
        assert c.get("/", headers=TS).status_code == 200


def test_identity_does_not_replace_api_tokens(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/v1/tasks", headers=TS).status_code == 401


def test_loopback_detection():
    from app.tailnet import is_loopback
    assert all(map(is_loopback, ["127.0.0.1", "127.8.9.1", "::1", "localhost"]))
    assert not any(map(is_loopback, ["192.168.1.64", "100.101.102.103", "", None, "testclient", "::ffff:10.0.0.1"]))


def test_pairing_page_shows_qr_and_devices(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        page = c.post("/settings/pair", headers=TS)
        assert page.status_code == 200 and 'aria-label="Pairing QR code"' in page.text and 'pairCode' in page.text
        import re
        code = re.search(r'"pairCode":"([A-Z0-9]{8})"', page.text.replace("&#34;", '"')).group(1)
        assert c.post("/api/v1/pair", json={"pairCode": code, "deviceName": "Pixel"}).status_code == 200
        s = c.get("/settings", headers=TS).text
        assert "Pixel" in s and "Revoke" in s and "Funnel" in s
