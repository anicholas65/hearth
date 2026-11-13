"""Fast capture: product lookup by barcode, OCR text parsers for labels and receipts, manual fetch.

Product lookup is a chain of providers (first hit wins). OCR runs in the browser (Tesseract.js) by
default; set OCR_URL to send images to a local/cloud model instead (POST image → {"text": "..."}).
"""
import os
import re
from datetime import date, datetime

import requests

UA = {"User-Agent": "Hearth-home-manager/2 (self-hosted)"}
_cache: dict[str, dict] = {}


# ---------------- product lookup ----------------

def _openfoodfacts(code: str) -> dict | None:
    r = requests.get(f"https://world.openfoodfacts.org/api/v2/product/{code}.json",
                     params={"fields": "product_name,brands,quantity,categories_tags,image_front_small_url"},
                     headers=UA, timeout=6)
    if r.status_code != 200:
        return None
    j = r.json()
    p = j.get("product") or {}
    if j.get("status") != 1 or not p.get("product_name"):
        return None
    cats = [c.split(":", 1)[-1].replace("-", " ") for c in p.get("categories_tags") or []]
    return {"name": p["product_name"].strip(), "brand": (p.get("brands") or "").split(",")[0].strip() or None,
            "quantity": p.get("quantity") or None, "category": cats[-1] if cats else None,
            "image": p.get("image_front_small_url"), "kind": "grocery", "source": "Open Food Facts"}


def _upcitemdb(code: str) -> dict | None:
    """General goods (tools, appliances, supplies). Free trial endpoint, rate-limited."""
    r = requests.get("https://api.upcitemdb.com/prod/trial/lookup", params={"upc": code}, headers=UA, timeout=6)
    if r.status_code != 200:
        return None
    items = r.json().get("items") or []
    if not items:
        return None
    it = items[0]
    return {"name": it.get("title"), "brand": it.get("brand") or None, "model": it.get("model") or None,
            "category": (it.get("category") or "").split(">")[-1].strip() or None,
            "image": (it.get("images") or [None])[0], "kind": "goods", "source": "UPCitemdb"}


PROVIDERS = [_openfoodfacts, _upcitemdb]


def lookup(code: str) -> dict:
    code = re.sub(r"\D", "", code or "")
    if not 6 <= len(code) <= 14:
        return {"found": False, "code": code, "error": "Not a product barcode"}
    if code in _cache:
        return _cache[code]
    out = {"found": False, "code": code}
    for provider in PROVIDERS:
        try:
            hit = provider(code)
        except (requests.RequestException, ValueError):
            continue
        if hit and hit.get("name"):
            out = {"found": True, "code": code, **hit}
            break
    _cache[code] = out
    return out


# ---------------- OCR text parsers ----------------

KNOWN_BRANDS = ["Whirlpool", "Samsung", "LG", "GE", "Frigidaire", "Bosch", "KitchenAid", "Maytag", "Kenmore",
                "Electrolux", "Rheem", "A.O. Smith", "Bradford White", "Carrier", "Trane", "Lennox", "Goodman",
                "Rinnai", "Navien", "Honeywell", "Nest", "Ecobee", "Chamberlain", "LiftMaster", "Dyson", "Miele",
                "Sharp", "Panasonic", "Sony", "Toshiba", "Haier", "Amana", "Speed Queen", "Weber", "Toro", "Ryobi",
                "DeWalt", "Milwaukee", "Makita", "Craftsman", "Kohler", "Moen", "Delta", "InSinkErator"]


def _clean(v: str) -> str:
    return v.strip().strip(":#.-").replace(" ", "").upper()


def parse_label(text: str) -> dict:
    """Appliance rating plate → brand, model, serial, manufacture date (whatever is legible)."""
    t = text.replace("\r", "")
    out: dict = {}
    m = re.search(r"\b(?:MODEL|MOD|M/N|MDL|MODEL\s*NO|MODEL\s*NUMBER)\b\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/\.]{3,})", t, re.I)
    if m:
        out["model"] = _clean(m.group(1))
    m = re.search(r"\b(?:SERIAL|SER|S/N|SN|SERIAL\s*NO|SERIAL\s*NUMBER)\b\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,})", t, re.I)
    if m:
        out["serial"] = _clean(m.group(1))
    for b in KNOWN_BRANDS:
        if re.search(r"\b" + re.escape(b) + r"\b", t, re.I):
            out["brand"] = b
            break
    m = re.search(r"\b(?:MFG|MFD|MANUFACTURED|DATE)\b[^\n0-9]{0,12}(\d{1,2})[/\-](\d{2,4})\b", t, re.I)
    if m:
        y = int(m.group(2))
        out["manufactured"] = f"{y + 2000 if y < 100 else y}-{int(m.group(1)):02d}"
    return out


DATE_PATTERNS = [
    (r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", "mdy"), (r"\b(\d{4})-(\d{2})-(\d{2})\b", "ymd"),
    (r"\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b", "mdy"),
    (r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", "mon"),
]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}


def _find_date(t: str) -> str | None:
    for pat, kind in DATE_PATTERNS:
        m = re.search(pat, t, re.I)
        if not m:
            continue
        try:
            if kind == "ymd":
                d = date(int(m[1]), int(m[2]), int(m[3]))
            elif kind == "mon":
                d = date(int(m[3]), MONTHS[m[1].lower()[:3]], int(m[2]))
            else:
                y = int(m[3])
                d = date(y + 2000 if y < 100 else y, int(m[1]), int(m[2]))
            if d <= date.today():
                return d.isoformat()
        except ValueError:
            continue
    return None


def parse_receipt(text: str) -> dict:
    """Receipt → total, date, store. Totals prefer 'TOTAL'/'AMOUNT DUE' lines over 'SUBTOTAL'."""
    lines = [l.strip() for l in text.replace("\r", "").splitlines() if l.strip()]
    out: dict = {}
    money = r"\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})\b"
    best = None
    for l in lines:
        low = l.lower()
        if re.search(r"sub\s*-?\s*total|tax|change|savings|discount|tip", low):
            continue
        if re.search(r"\b(grand\s+total|total\s+due|amount\s+due|balance\s+due|total)\b", low):
            m = re.search(money, l)
            if m:
                best = float(m.group(1).replace(",", ""))
    if best is None:  # fall back to the largest amount on the receipt
        amounts = [float(a.replace(",", "")) for a in re.findall(money, text)]
        best = max(amounts) if amounts else None
    if best is not None:
        out["total"] = best
    d = _find_date(text)
    if d:
        out["date"] = d
    for l in lines[:5]:
        letters = re.sub(r"[^A-Za-z&' ]", "", l).strip()
        if len(letters) >= 3 and not re.search(r"receipt|welcome|store\s*#|tel|phone|www|\d{3}-\d{4}", l, re.I):
            out["store"] = letters.title() if letters.isupper() else letters
            break
    return out


# ---------------- server-side reading (optional) ----------------
# If OCR_URL is set, photos are POSTed there (e.g. a self-hosted Tesseract/PaddleOCR wrapper) → {"text": "..."}.
# Otherwise the browser reads the photo with Tesseract.js. Fields are always parsed locally (parse_label/receipt).


def ocr_provider() -> str | None:
    return "url" if os.environ.get("OCR_URL") else None


def ocr_available() -> bool:
    return ocr_provider() is not None


def ocr_remote(data: bytes, mime: str) -> str:
    """POST the image to OCR_URL and return its text."""
    r = requests.post(os.environ["OCR_URL"], files={"image": ("capture", data, mime)}, timeout=60)
    r.raise_for_status()
    return r.json().get("text", "")


# ---------------- manuals ----------------

def manual_links(brand: str | None, model: str | None) -> list[dict]:
    from urllib.parse import quote_plus
    q = " ".join(x for x in (brand, model) if x)
    if not q:
        return []
    return [{"label": "ManualsLib", "url": f"https://www.manualslib.com/brand/?q={quote_plus(q)}"},
            {"label": "Web search (PDF)", "url": f"https://duckduckgo.com/?q={quote_plus(q + ' manual filetype:pdf')}"},
            {"label": "Manufacturer support", "url": f"https://duckduckgo.com/?q={quote_plus(q + ' support owners manual')}"}]


MAX_MANUAL = 40 * 1024 * 1024


def fetch_manual(url: str) -> tuple[bytes, str]:
    """Download a PDF manual. Returns (bytes, filename). Raises ValueError with a readable reason."""
    if not re.match(r"^https?://", url or ""):
        raise ValueError("Enter a web link starting with http(s)://")
    with requests.get(url, headers=UA, timeout=20, stream=True, allow_redirects=True) as r:
        if r.status_code != 200:
            raise ValueError(f"The site answered {r.status_code}")
        ctype = r.headers.get("content-type", "")
        buf = b""
        for chunk in r.iter_content(1 << 16):
            buf += chunk
            if len(buf) > MAX_MANUAL:
                raise ValueError("That file is larger than 40 MB")
        if "pdf" not in ctype and not buf.startswith(b"%PDF"):
            raise ValueError("That link isn't a PDF (open the manual and copy the PDF's own link)")
    name = url.rstrip("/").split("/")[-1].split("?")[0] or f"manual-{datetime.now():%Y%m%d}.pdf"
    return buf, name if name.lower().endswith(".pdf") else name + ".pdf"
