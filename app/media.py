"""Image helpers shared by the web UI and the API."""
import io
import uuid

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from .db import data_dir


def save_image(data: bytes) -> tuple[str, str]:
    """Re-encode an upload as a ≤2000px JPEG plus a 480x360 thumbnail. Returns (stored, thumb)."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Unsupported image (use JPEG, PNG, WebP or GIF)")
    base = uuid.uuid4().hex
    up = data_dir() / "uploads"
    full = img.copy()
    full.thumbnail((2000, 2000))
    full.save(up / f"{base}.jpg", "JPEG", quality=85, optimize=True)
    ImageOps.fit(img, (480, 360), Image.LANCZOS).save(up / f"{base}_t.jpg", "JPEG", quality=80, optimize=True)
    return f"{base}.jpg", f"{base}_t.jpg"


def ensure_thumb(stored_name: str) -> str:
    """Thumbnail for an already-stored upload (API photos). Non-images fall back to the original."""
    up = data_dir() / "uploads"
    thumb = stored_name.rsplit(".", 1)[0] + "_t.jpg"
    if (up / thumb).exists():
        return thumb
    try:
        img = ImageOps.exif_transpose(Image.open(up / stored_name)).convert("RGB")
        ImageOps.fit(img, (480, 360), Image.LANCZOS).save(up / thumb, "JPEG", quality=80, optimize=True)
        return thumb
    except (UnidentifiedImageError, OSError, FileNotFoundError):
        return stored_name
