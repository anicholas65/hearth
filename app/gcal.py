"""Google Calendar sync via a service account.

Setup: create a service account in Google Cloud, enable the Calendar API, download its
JSON key, upload it in Hearth Settings, then share your Google calendar with the service
account's email ("Make changes to events") and paste the calendar ID into Settings.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .db import data_dir, get_db, get_setting, set_setting

log = logging.getLogger("hearth.gcal")
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.readonly"]
API = "https://www.googleapis.com/calendar/v3"
TASK_COLOR, APPT_COLOR, REMINDER_COLOR = "6", "9", "5"  # tangerine, blueberry, banana


# ---------- configuration ----------

def key_path() -> Path:
    env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    return Path(env) if env else data_dir() / "google-service-account.json"


def calendar_id() -> str | None:
    return os.environ.get("GOOGLE_CALENDAR_ID") or get_setting("gcal_calendar_id")


def timezone() -> str:
    return get_setting("gcal_timezone") or os.environ.get("TZ") or "UTC"


def task_remind_minutes() -> int:
    return int(get_setting("gcal_task_remind", "900"))  # 900 = 9:00 the day before (all-day)


def enabled() -> bool:
    return get_setting("gcal_enabled", "1") == "1"


def configured() -> bool:
    return key_path().is_file() and bool(calendar_id())


def service_account_email() -> str | None:
    try:
        return json.loads(key_path().read_text()).get("client_email")
    except (OSError, ValueError):
        return None


def save_key(data: bytes) -> str:
    info = json.loads(data)
    if info.get("type") != "service_account" or "client_email" not in info:
        raise ValueError("Not a service account JSON key")
    p = data_dir() / "google-service-account.json"
    p.write_bytes(data)
    p.chmod(0o600)
    return info["client_email"]


# ---------- API client ----------

class Calendar:
    def __init__(self, key: Path, cal_id: str):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(str(key), scopes=SCOPES)
        self.s = AuthorizedSession(creds)
        self.base = f"{API}/calendars/{quote(cal_id, safe='')}"

    def _check(self, r):
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"Google Calendar {r.status_code}: {msg}")
        return r

    def info(self) -> dict:
        return self._check(self.s.get(self.base, timeout=15)).json()

    def insert(self, body: dict) -> str:
        return self._check(self.s.post(f"{self.base}/events", json=body, timeout=15)).json()["id"]

    def update(self, event_id: str, body: dict) -> str | None:
        r = self.s.put(f"{self.base}/events/{quote(event_id)}", json=body, timeout=15)
        if r.status_code in (404, 410):
            return None
        return self._check(r).json()["id"]

    def delete(self, event_id: str) -> None:
        r = self.s.delete(f"{self.base}/events/{quote(event_id)}", timeout=15)
        if r.status_code not in (404, 410):
            self._check(r)


def get_client():
    """Return a Calendar client or None when sync is off/not configured."""
    if not (enabled() and configured()):
        return None
    return Calendar(key_path(), calendar_id())


def _record(error: str | None) -> None:
    set_setting("gcal_last_sync", datetime.now().isoformat(timespec="seconds"))
    set_setting("gcal_last_error", error)


def _upsert(client, event_id: str | None, body: dict) -> str:
    if event_id:
        new = client.update(event_id, body)
        if new:
            return new
    return client.insert(body)


def _link(path: str) -> str:
    base = (get_setting("public_url") or "").rstrip("/")
    return f"\n\nOpen in Hearth: {base}{path}" if base else ""


# ---------- event bodies ----------

def task_body(t, item_name: str | None) -> dict:
    due = date.fromisoformat(t["next_due"])
    desc = [f"Repeats every {t['interval_days']} days."]
    if item_name:
        desc.append(f"Item: {item_name}")
    if t["notes"]:
        desc.append(t["notes"])
    return {
        "summary": f"🔧 {t['name']}",
        "description": "\n".join(desc) + _link(f"/tasks/{t['id']}"),
        "start": {"date": due.isoformat()},
        "end": {"date": (due + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "colorId": TASK_COLOR,
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": task_remind_minutes()}]},
        "extendedProperties": {"private": {"hearth": f"task:{t['id']}"}},
    }


def appointment_body(a, contractor=None, item_name: str | None = None) -> dict:
    tz = timezone()
    is_rem = a["kind"] == "reminder"
    if a["all_day"]:
        d0 = date.fromisoformat(a["starts_at"][:10])
        d1 = date.fromisoformat(a["ends_at"][:10]) if a["ends_at"] else d0
        start, end = {"date": d0.isoformat()}, {"date": (max(d0, d1) + timedelta(days=1)).isoformat()}
    else:
        s = datetime.fromisoformat(a["starts_at"])
        e = datetime.fromisoformat(a["ends_at"]) if a["ends_at"] else None
        if not e or e <= s:
            e = s + timedelta(minutes=15 if is_rem else 60)
        start = {"dateTime": s.isoformat(timespec="seconds"), "timeZone": tz}
        end = {"dateTime": e.isoformat(timespec="seconds"), "timeZone": tz}
    desc = []
    if contractor:
        desc.append(" · ".join(x for x in (contractor["name"], contractor["trade"],
                                           contractor["phone"], contractor["email"]) if x))
    if item_name:
        desc.append(f"Item: {item_name}")
    if a["notes"]:
        desc.append(a["notes"])
    title = a["title"] + (f" · {contractor['name']}" if contractor and not is_rem else "")
    body = {
        "summary": ("🔔 " if is_rem else "") + title,
        "description": "\n".join(desc) + _link(f"/schedule/{a['id']}"),
        "start": start, "end": end,
        "colorId": REMINDER_COLOR if is_rem else APPT_COLOR,
        "extendedProperties": {"private": {"hearth": f"appt:{a['id']}"}},
    }
    if a["location"]:
        body["location"] = a["location"]
    if a["remind_minutes"] is not None:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup", "minutes": int(a["remind_minutes"])}]}
    if is_rem:
        body["transparency"] = "transparent"
    return body


# ---------- sync operations (safe to run in background; never raise) ----------

def sync_task(task_id: int, client=None) -> bool:
    try:
        client = client or get_client()
        if not client:
            return False
        with get_db() as db:
            t = db.execute("SELECT t.*, i.name AS item_name FROM tasks t "
                           "LEFT JOIN items i ON i.id=t.item_id WHERE t.id=?", (task_id,)).fetchone()
        if not t:
            return False
        eid = _upsert(client, t["gcal_event_id"], task_body(t, t["item_name"]))
        with get_db() as db:
            db.execute("UPDATE tasks SET gcal_event_id=? WHERE id=?", (eid, task_id))
        _record(None)
        return True
    except Exception as e:
        log.exception("task sync failed")
        _record(str(e))
        return False


def sync_appointment(appt_id: int, client=None) -> bool:
    try:
        client = client or get_client()
        if not client:
            return False
        with get_db() as db:
            a = db.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
            if not a:
                return False
            c = db.execute("SELECT * FROM contractors WHERE id=?", (a["contractor_id"],)).fetchone() \
                if a["contractor_id"] else None
            it = db.execute("SELECT name FROM items WHERE id=?", (a["item_id"],)).fetchone() \
                if a["item_id"] else None
        eid = _upsert(client, a["gcal_event_id"], appointment_body(a, c, it[0] if it else None))
        with get_db() as db:
            db.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (eid, appt_id))
        _record(None)
        return True
    except Exception as e:
        log.exception("appointment sync failed")
        _record(str(e))
        return False


def delete_event(event_id: str | None) -> None:
    if not event_id:
        return
    try:
        client = get_client()
        if client:
            client.delete(event_id)
            _record(None)
    except Exception as e:
        log.exception("event delete failed")
        _record(str(e))


def sync_all() -> tuple[int, int]:
    """Push every task and upcoming appointment. Returns (synced, failed)."""
    client = get_client()
    if not client:
        return 0, 0
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with get_db() as db:
        tids = [r[0] for r in db.execute("SELECT id FROM tasks")]
        aids = [r[0] for r in db.execute("SELECT id FROM appointments WHERE starts_at >= ?", (cutoff,))]
    ok = sum(sync_task(t, client) for t in tids) + sum(sync_appointment(a, client) for a in aids)
    return ok, len(tids) + len(aids) - ok


def test_connection() -> str:
    """Raises on failure; returns the calendar's name."""
    if not configured():
        raise RuntimeError("Upload a service account key and set a calendar ID first")
    return Calendar(key_path(), calendar_id()).info().get("summary", calendar_id())
