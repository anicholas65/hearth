"""Hearth REST API v1 (mounted at /api/v1). The Android client depends on this contract.

Auth: `Authorization: Bearer <device token>` (per-device, stored as SHA-256, revocable in Settings).
Unauthenticated: GET /health, POST /pair, GET /openapi.json.
"""
import hashlib
import mimetypes
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, create_model

from . import __version__, sync
from .db import data_dir, get_db, get_setting
from .sync import RESOURCES, ApiError, now_ts

api = FastAPI(title="Hearth API", version=__version__, openapi_url="/openapi.json", docs_url="/docs",
              servers=[{"url": "/api/v1"}],
              redoc_url=None, description=__doc__)

PAIR_TTL = timedelta(minutes=10)
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _hash(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@api.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


# ---------------- auth ----------------

class Device(BaseModel):
    id: str
    name: str
    created_at: str
    last_seen_at: Optional[str] = None
    revoked_at: Optional[str] = None


def require_device(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    with get_db() as db:
        d = db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked_at IS NULL", (_hash(token),)).fetchone()
        if not d:
            raise HTTPException(401, "Invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
        now = now_ts()
        if not d["last_seen_at"] or d["last_seen_at"][:16] != now[:16]:  # at most once a minute
            db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, d["id"]))
    return dict(d)


def create_pair_code() -> tuple[str, str]:
    """For Settings: a one-time code valid for 10 minutes. Returns (code, expires_at)."""
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    exp = (datetime.now(timezone.utc) + PAIR_TTL).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with get_db() as db:
        db.execute("DELETE FROM pair_codes WHERE expires_at < ? OR used_at IS NOT NULL", (now_ts(),))
        db.execute("INSERT INTO pair_codes (code_hash, expires_at, created_at) VALUES (?,?,?)",
                   (_hash(code), exp, now_ts()))
    return code, exp


def list_devices() -> list[dict]:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT uuid AS id, name, created_at, last_seen_at, revoked_at FROM devices ORDER BY created_at DESC")]


def revoke_device(device_id: str) -> None:
    with get_db() as db:
        db.execute("UPDATE devices SET revoked_at=? WHERE uuid=? AND revoked_at IS NULL", (now_ts(), device_id))


_attempts: dict[str, list[float]] = {}


class PairIn(BaseModel):
    pairCode: str = Field(min_length=4, max_length=32)
    deviceName: str = Field(min_length=1, max_length=80)


class PairOut(BaseModel):
    token: str
    deviceId: str


@api.post("/pair", response_model=PairOut, tags=["auth"],
          summary="Exchange a one-time pair code (from the Settings QR) for a device token")
def pair(body: PairIn, request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _attempts.get(ip, []) if t > time.time() - 600]  # failed attempts only
    if len(recent) >= 10:
        raise HTTPException(429, "Too many failed pairing attempts; try again in a few minutes")
    code = body.pairCode.strip().upper().replace("-", "").replace(" ", "")
    with get_db() as db:
        row = db.execute("SELECT * FROM pair_codes WHERE code_hash=?", (_hash(code),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now_ts():
            _attempts[ip] = recent + [time.time()]
            raise HTTPException(401, "Pair code is invalid, used or expired")
        db.execute("UPDATE pair_codes SET used_at=? WHERE code_hash=?", (now_ts(), row["code_hash"]))
        token = "hth_" + secrets.token_urlsafe(32)
        dev_id = str(uuidlib.uuid4())
        db.execute("INSERT INTO devices (uuid, name, token_hash, created_at) VALUES (?,?,?,?)",
                   (dev_id, body.deviceName.strip(), _hash(token), now_ts()))
    return {"token": token, "deviceId": dev_id}


# ---------------- service endpoints ----------------

class Health(BaseModel):
    status: Literal["ok"]
    version: str
    server_time: str


@api.get("/health", response_model=Health, tags=["service"], summary="Reachability + version + server clock")
def health():
    return {"status": "ok", "version": __version__, "server_time": now_ts()}


class NotifyConfig(BaseModel):
    ntfyServer: Optional[str] = None
    ntfyTopic: Optional[str] = None


def notify_config() -> dict:
    """ntfy server/topic from Settings, else parsed from an ntfy:// or ntfys:// Apprise URL."""
    server, topic = get_setting("ntfy_server"), get_setting("ntfy_topic")
    if not topic:
        from .notify import apprise_urls
        for u in apprise_urls():
            if u.startswith(("ntfy://", "ntfys://")):
                scheme = "https" if u.startswith("ntfys://") else "http"
                rest = u.split("://", 1)[1].split("?")[0]
                rest = rest.split("@", 1)[-1]  # drop credentials
                parts = [p for p in rest.split("/") if p]
                if len(parts) == 1:
                    server, topic = "https://ntfy.sh", parts[0]
                elif parts:
                    server, topic = f"{scheme}://{parts[0]}", parts[-1]
                break
    return {"ntfyServer": server or ("https://ntfy.sh" if topic else None), "ntfyTopic": topic}


@api.get("/notify-config", response_model=NotifyConfig, tags=["service"],
         summary="Where to subscribe for push (ntfy)", dependencies=[Depends(require_device)])
def get_notify_config():
    return notify_config()


@api.get("/me", response_model=Device, tags=["auth"], summary="The calling device")
def me(dev: dict = Depends(require_device)):
    return {"id": dev["uuid"], "name": dev["name"], "created_at": dev["created_at"],
            "last_seen_at": dev["last_seen_at"], "revoked_at": dev["revoked_at"]}


# ---------------- changes ----------------

class ChangeRecord(BaseModel):
    type: str
    record: dict[str, Any]


class DeletedRef(BaseModel):
    type: str
    id: str


class ChangesOut(BaseModel):
    changes: list[ChangeRecord]
    deleted: list[DeletedRef]
    cursor: str
    hasMore: bool


@api.get("/changes", response_model=ChangesOut, tags=["sync"], dependencies=[Depends(require_device)],
         summary="Everything changed after `since` (opaque cursor; start with 0)")
def get_changes(since: str = Query("0"), limit: int = Query(500, ge=1, le=500)):
    try:
        s = int(since or 0)
    except ValueError:
        raise HTTPException(422, "since must be a cursor returned by this endpoint")
    return sync.changes(s, limit)


# ---------------- files ----------------

class FileOut(BaseModel):
    id: str
    url: str


MAX_UPLOAD = 50 * 1024 * 1024


@api.post("/files", response_model=FileOut, tags=["files"], dependencies=[Depends(require_device)],
          summary="Upload a file (multipart field `file`); reference the returned id from documents/photos")
async def upload_file(file: UploadFile = File(...), id: Optional[str] = Form(None)):
    fid = (id or str(uuidlib.uuid4())).lower()
    if not sync.UUID_RE.match(fid):
        raise HTTPException(422, "id must be a UUID")
    with get_db() as db:
        if db.execute("SELECT 1 FROM files WHERE uuid=?", (fid,)).fetchone():  # idempotent retry
            return {"id": fid, "url": f"/api/v1/files/{fid}"}
    ext = Path(file.filename or "").suffix.lower()[:10]
    stored = f"{uuidlib.uuid4().hex}{ext}"
    size = 0
    with (data_dir() / "uploads" / stored).open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                (data_dir() / "uploads" / stored).unlink(missing_ok=True)
                raise HTTPException(413, "File too large (50 MB max)")
            fh.write(chunk)
    with get_db() as db:
        db.execute("INSERT INTO files (uuid, stored_name, filename, mime, size, created_at) VALUES (?,?,?,?,?,?)",
                   (fid, stored, Path(file.filename or stored).name,
                    file.content_type or mimetypes.guess_type(stored)[0], size, now_ts()))
    return {"id": fid, "url": f"/api/v1/files/{fid}"}


@api.get("/files/{file_id}", tags=["files"], dependencies=[Depends(require_device)],
         summary="Download a file", response_class=FileResponse)
def download_file(file_id: str):
    with get_db() as db:
        f = db.execute("SELECT * FROM files WHERE uuid=?", (file_id.lower(),)).fetchone()
    if not f or not (data_dir() / "uploads" / f["stored_name"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(data_dir() / "uploads" / f["stored_name"], filename=f["filename"],
                        media_type=f["mime"] or None)


# ---------------- resources ----------------

PY = {"str": str, "int": int, "num": float, "bool": bool, "date": str, "json": Any}


def _models(res):
    base = {"id": (Optional[str], None), "created_at": (Optional[str], None),
            "updated_at": (Optional[str], Field(None, description="Last-write-wins clock (ISO-8601 UTC)")),
            "deleted_at": (Optional[str], Field(None, description="Set to delete"))}
    fields = {}
    for name, f in res.fields.items():
        t = PY[f.kind]
        if f.choices:
            t = Literal[f.choices]  # type: ignore[valid-type]
        desc = f"UUID of {f.fk}" if f.fk else ("YYYY-MM-DD" if f.kind == "date" else None)
        fields[name] = (Optional[t], Field(None, description=desc))
    cname = "".join(p.capitalize() for p in res.type.replace("-", " ").split())
    model_in = create_model(f"{cname}In", **base, **fields)
    out_fields = {**fields}
    if res.type in ("documents", "photos"):
        out_fields["url"] = (Optional[str], None)
    model_out = create_model(f"{cname}", id=(str, ...), created_at=(Optional[str], None),
                             updated_at=(Optional[str], None), deleted_at=(Optional[str], None), **out_fields)
    return model_in, model_out


def _register(res):
    model_in, model_out = _models(res)
    tag = res.type

    def list_(updated_since: Optional[str] = Query(None, description="ISO-8601; only records changed after"),
              include_deleted: bool = Query(False), limit: int = Query(500, ge=1, le=1000),
              offset: int = Query(0, ge=0)):
        with get_db() as db:
            return sync.list_records(db, res.type, updated_since, include_deleted, limit, offset)

    def get_(id: str):
        with get_db() as db:
            rec = sync.get_record(db, res.type, id.lower())
        if not rec:
            raise HTTPException(404, f"{res.type} {id} not found")
        return rec

    def put_(id: str, body: model_in):  # type: ignore[valid-type]
        rec, _ = sync.upsert(res.type, id, body.model_dump(exclude_unset=True))
        return rec

    def delete_(id: str):
        rec = sync.delete(res.type, id)
        if not rec:
            raise HTTPException(404, f"{res.type} {id} not found")
        return rec

    deps = [Depends(require_device)]
    api.add_api_route(f"/{res.type}", list_, methods=["GET"], tags=[tag], dependencies=deps,
                      response_model=list[model_out], response_model_exclude_unset=True,
                      summary=f"List {res.type}", description=res.title)
    api.add_api_route(f"/{res.type}/{{id}}", get_, methods=["GET"], tags=[tag], dependencies=deps,
                      response_model=model_out, response_model_exclude_unset=True, summary=f"Get one {res.type}")
    api.add_api_route(f"/{res.type}/{{id}}", put_, methods=["PUT"], tags=[tag], dependencies=deps,
                      response_model=model_out, response_model_exclude_unset=True,
                      summary=f"Upsert {res.type} (idempotent, last-write-wins; returns the server copy)")
    api.add_api_route(f"/{res.type}/{{id}}", delete_, methods=["DELETE"], tags=[tag], dependencies=deps,
                      response_model=model_out, response_model_exclude_unset=True, summary=f"Soft-delete {res.type}")


for _res in RESOURCES.values():
    _register(_res)
