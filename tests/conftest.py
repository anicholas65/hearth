import pytest


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch, request):
    """Most tests exercise pages, not sign-in. Tests marked `auth` run with sign-in on (the real default)."""
    if request.node.get_closest_marker("auth"):
        monkeypatch.delenv("HEARTH_AUTH", raising=False)
    else:
        monkeypatch.setenv("HEARTH_AUTH", "off")
    from app import auth
    auth._fails.clear()
