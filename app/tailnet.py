"""Tailscale integration: identity headers (trusted only from the local Serve proxy) and tailnet status.

Tailscale Serve runs in the same network namespace as Hearth (Unraid 7 "Use Tailscale", or the sidecar
in docker-compose.tailscale.yml) and proxies https://hearth.<tailnet>.ts.net to 127.0.0.1:8080, adding
Tailscale-User-Login / Tailscale-User-Name / Tailscale-User-Profile-Pic.

Rule: those headers are honoured only when the TCP peer is loopback. From any other peer (LAN clients
on 192.168.1.64:8080) they are removed before the app sees the request, so they can't be forged.
Uvicorn runs WITHOUT --proxy-headers, so the peer address can't be spoofed with X-Forwarded-For either.
"""
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone

TS_HEADERS = (b"tailscale-user-login", b"tailscale-user-name", b"tailscale-user-profile-pic")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_loopback(host: str | None) -> bool:
    return bool(host) and (host in LOOPBACK or host.startswith("127."))


class TailscaleIdentityMiddleware:
    """Pure ASGI, installed outermost: strips untrusted identity headers, maps trusted ones to a member."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = scope.get("headers") or []
            has_ts = any(k in TS_HEADERS for k, _ in headers)
            if has_ts:
                peer = (scope.get("client") or (None,))[0]
                if not is_loopback(peer):
                    scope = dict(scope, headers=[(k, v) for k, v in headers if k not in TS_HEADERS])
                else:
                    h = {k.decode(): v.decode("utf-8", "replace") for k, v in headers if k in TS_HEADERS}
                    login = h.get("tailscale-user-login", "").strip()
                    if login:
                        scope = dict(scope)
                        scope.setdefault("state", {})
                        scope["hearth_member"] = member_for(login, h.get("tailscale-user-name"))
        await self.app(scope, receive, send)


def member_for(login: str, name: str | None) -> dict:
    from .db import get_db
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db() as db:
        row = db.execute("SELECT * FROM members WHERE login=?", (login,)).fetchone()
        if not row:
            first = db.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0
            db.execute("INSERT INTO members (login, name, role, created_at, last_seen_at) VALUES (?,?,?,?,?)",
                       (login, name or login.split("@")[0], "owner" if first else "member", now, now))
            row = db.execute("SELECT * FROM members WHERE login=?", (login,)).fetchone()
        elif not row["last_seen_at"] or row["last_seen_at"][:16] != now[:16]:
            db.execute("UPDATE members SET last_seen_at=?, name=COALESCE(?, name) WHERE id=?", (now, name, row["id"]))
        return dict(row)


_status_cache: tuple[float, dict] = (0.0, {})


def status() -> dict:
    """Best-effort tailnet status from the local tailscale CLI (present with Unraid's integration/sidecar)."""
    global _status_cache
    if time.time() - _status_cache[0] < 30:
        return _status_cache[1]
    out = {"available": False}
    exe = shutil.which("tailscale")
    if exe:
        try:
            raw = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=3, text=True).stdout
            st = json.loads(raw)
            me = st.get("Self", {})
            dns = (me.get("DNSName") or "").rstrip(".")
            out = {"available": True, "backend": st.get("BackendState"), "online": me.get("Online"),
                   "dns_name": dns, "tailnet": (st.get("CurrentTailnet") or {}).get("Name"),
                   "magic_dns": (st.get("CurrentTailnet") or {}).get("MagicDNSEnabled"),
                   "url": f"https://{dns}" if dns else None, "ips": me.get("TailscaleIPs", [])}
        except (OSError, ValueError, subprocess.SubprocessError):
            out = {"available": True, "error": "tailscale CLI present but status unavailable"}
    _status_cache = (time.time(), out)
    return out
