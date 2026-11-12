"""User accounts and sign-in for the web UI.

Who is signed in, in order:
1. Tailscale identity (set by TailscaleIdentityMiddleware from the trusted loopback proxy).
2. A session cookie from /login (sessions table, token stored as SHA-256).
3. HTTP Basic against an account (for scripts/curl).

With no accounts yet, every page sends you to /welcome to create the first (owner) account.
HEARTH_USERNAME / HEARTH_PASSWORD, if set, create that account at startup instead.
HEARTH_AUTH=off disables sign-in (trusted networks only). Demo mode never asks.
/api/v1 is untouched: it has its own device tokens.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from .db import demo_mode, get_db

COOKIE = "hearth_session"
SESSION_DAYS = 90
MIN_PASSWORD = 8
PUBLIC_PREFIXES = ("/static/", "/api/")
PUBLIC_PATHS = {"/login", "/welcome", "/logout", "/sw.js", "/favicon.ico"}

_N, _R, _P = 2 ** 14, 8, 1


def enabled() -> bool:
    return os.environ.get("HEARTH_AUTH", "on").lower() not in ("off", "0", "false", "no") and not demo_mode()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


# ---------------- passwords ----------------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, dk = (stored or "").split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def password_problem(pw: str) -> str | None:
    if len(pw or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


def norm_username(u: str) -> str:
    return (u or "").strip().lower()


# ---------------- accounts ----------------

def has_accounts() -> bool:
    with get_db() as db:
        return db.execute("SELECT 1 FROM members WHERE password_hash IS NOT NULL LIMIT 1").fetchone() is not None


def list_accounts() -> list[dict]:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, login, name, role, created_at, last_seen_at, password_hash IS NOT NULL AS has_password "
            "FROM members ORDER BY role = 'owner' DESC, created_at")]


def get_member(member_id: int) -> dict | None:
    with get_db() as db:
        r = db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        return dict(r) if r else None


def create_account(username: str, password: str, name: str | None = None, role: str = "member") -> dict:
    login = norm_username(username)
    if not login or any(c.isspace() for c in login):
        raise ValueError("Choose a username without spaces.")
    if (p := password_problem(password)):
        raise ValueError(p)
    role = "owner" if role == "owner" else "member"
    with get_db() as db:
        row = db.execute("SELECT * FROM members WHERE login=?", (login,)).fetchone()
        if row and row["password_hash"]:
            raise ValueError("That username is taken.")
        if row:  # e.g. a Tailscale member getting a password for LAN use
            db.execute("UPDATE members SET password_hash=?, name=COALESCE(NULLIF(?, ''), name) WHERE id=?",
                       (hash_password(password), name, row["id"]))
            mid = row["id"]
        else:
            mid = db.execute("INSERT INTO members (login, name, role, created_at, password_hash) VALUES (?,?,?,?,?)",
                             (login, (name or "").strip() or login, role, _iso(_now()),
                              hash_password(password))).lastrowid
        return dict(db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone())


def set_password(member_id: int, password: str) -> None:
    if (p := password_problem(password)):
        raise ValueError(p)
    with get_db() as db:
        db.execute("UPDATE members SET password_hash=? WHERE id=?", (hash_password(password), member_id))
        db.execute("DELETE FROM sessions WHERE member_id=?", (member_id,))  # sign out everywhere


def delete_account(member_id: int) -> None:
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE member_id=?", (member_id,))
        db.execute("DELETE FROM members WHERE id=?", (member_id,))


def owner_count() -> int:
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM members WHERE role='owner'").fetchone()[0]


def authenticate(username: str, password: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM members WHERE login=?", (norm_username(username),)).fetchone()
    if row and verify_password(password or "", row["password_hash"]):
        return dict(row)
    verify_password(password or "", _DUMMY)  # same cost whether or not the user exists
    return None


_DUMMY = hash_password(secrets.token_hex(8))


def bootstrap_from_env() -> None:
    """HEARTH_USERNAME/HEARTH_PASSWORD (the old single shared login) become an owner account once."""
    user, pw = os.environ.get("HEARTH_USERNAME"), os.environ.get("HEARTH_PASSWORD")
    if not (user and pw):
        return
    with get_db() as db:
        row = db.execute("SELECT password_hash FROM members WHERE login=?", (norm_username(user),)).fetchone()
    if row and row["password_hash"]:
        return
    try:
        create_account(user, pw, name=user, role="owner")
    except ValueError:  # too-short env password: hash it anyway so existing installs keep working
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO members (login, name, role, created_at) VALUES (?,?,?,?)",
                       (norm_username(user), user, "owner", _iso(_now())))
            db.execute("UPDATE members SET password_hash=? WHERE login=?", (hash_password(pw), norm_username(user)))


# ---------------- sessions ----------------

def _token_hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def start_session(member_id: int, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with get_db() as db:
        db.execute("INSERT INTO sessions (token_hash, member_id, created_at, last_seen_at, expires_at, user_agent) "
                   "VALUES (?,?,?,?,?,?)", (_token_hash(token), member_id, _iso(now), _iso(now),
                                            _iso(now + timedelta(days=SESSION_DAYS)), (user_agent or "")[:200]))
        db.execute("UPDATE members SET last_seen_at=? WHERE id=?", (_iso(now), member_id))
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
    return token


def member_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    now = _now()
    with get_db() as db:
        row = db.execute("SELECT s.id AS sid, s.last_seen_at AS s_seen, s.expires_at, m.* FROM sessions s "
                         "JOIN members m ON m.id = s.member_id WHERE s.token_hash=?", (_token_hash(token),)).fetchone()
        if not row or row["expires_at"] < _iso(now) or not row["password_hash"]:
            return None
        if row["s_seen"][:13] != _iso(now)[:13]:  # sliding expiry, at most one write an hour
            db.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
                       (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), row["sid"]))
            db.execute("UPDATE members SET last_seen_at=? WHERE id=?", (_iso(now), row["id"]))
        m = dict(row)
    for k in ("sid", "s_seen", "expires_at", "password_hash"):
        m.pop(k, None)
    return m


def end_session(token: str | None) -> None:
    if token:
        with get_db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def member_for_basic(header: str | None) -> dict | None:
    scheme, _, cred = (header or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        u, _, p = base64.b64decode(cred).decode().partition(":")
    except Exception:
        return None
    m = authenticate(u, p)
    if m:
        m.pop("password_hash", None)
    return m


# ---------------- sign-in throttling ----------------

_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW = 10, 15 * 60


def throttled(ip: str) -> bool:
    t = time.monotonic()
    _fails[ip] = [x for x in _fails.get(ip, []) if t - x < WINDOW]
    return len(_fails[ip]) >= MAX_FAILS


def record_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.monotonic())


def clear_failures(ip: str) -> None:
    _fails.pop(ip, None)


def safe_next(n: str | None) -> str:
    n = n or "/"
    if not n.startswith("/") or n.startswith("//") or "\\" in n or n.startswith(("/login", "/welcome", "/logout")):
        return "/"
    return n


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
