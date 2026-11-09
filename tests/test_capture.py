"""Fast capture: parsers, product lookup chain, save actions, manual attach."""
import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_DISABLED", "1")
    from app.main import app
    with TestClient(app) as c:
        yield c


def rows(sql):
    from app.db import get_db
    with get_db() as db:
        return [dict(r) for r in db.execute(sql)]


LABEL = """WHIRLPOOL CORPORATION
MODEL NO. WDT730PAHZ0
SERIAL NO: FW1234567
MFD 03/19   120V 60Hz"""

RECEIPT = """THE HOME DEPOT
1234 Main St  Tel 555-0100
11/02/2026 14:31
WATER HEATER 50 GAL   849.00
SUBTOTAL   849.00
TAX         67.92
TOTAL     $916.92
CHANGE      0.00"""


def test_parse_label_and_receipt():
    from app.capture import parse_label, parse_receipt
    assert parse_label(LABEL) == {"model": "WDT730PAHZ0", "serial": "FW1234567", "brand": "Whirlpool",
                                  "manufactured": "2019-03"}
    assert parse_label("M/N: LRMVS3006S  S/N 912KWHJ0A23 LG Electronics")["brand"] == "LG"
    r = parse_receipt(RECEIPT)
    assert r == {"total": 916.92, "date": "2026-11-02", "store": "The Home Depot"}
    assert parse_receipt("Corner Shop\nmilk 3.49\nbread 2.99\n")["total"] == 3.49  # largest amount fallback
    assert parse_receipt("no numbers here") == {"store": "no numbers here"}


def test_lookup_chain(monkeypatch):
    from app import capture
    capture._cache.clear()
    calls = []
    monkeypatch.setattr(capture, "PROVIDERS", [
        lambda c: calls.append("off") or None,
        lambda c: calls.append("upc") or {"name": "Cordless drill", "brand": "Ryobi", "kind": "goods", "source": "x"}])
    hit = capture.lookup("0 33287 18559 6")
    assert hit["found"] and hit["name"] == "Cordless drill" and calls == ["off", "upc"]
    capture.lookup("033287185596")
    assert calls == ["off", "upc"]  # cached
    assert capture.lookup("12")["found"] is False


def test_openfoodfacts_mapping(monkeypatch):
    from app import capture

    class R:
        status_code = 200
        def json(self):
            return {"status": 1, "product": {"product_name": "Rolled Oats", "brands": "Quaker, PepsiCo",
                                             "quantity": "1.2 kg", "categories_tags": ["en:cereals", "en:oats"]}}
    monkeypatch.setattr(capture.requests, "get", lambda *a, **k: R())
    assert capture._openfoodfacts("123") == {"name": "Rolled Oats", "brand": "Quaker", "quantity": "1.2 kg",
                                             "category": "oats", "image": None, "kind": "grocery",
                                             "source": "Open Food Facts"}


def test_save_barcode_to_list_pantry_and_inventory(client, monkeypatch):
    from app import capture
    monkeypatch.setattr(capture, "lookup", lambda c: {"found": True, "code": "0123456789012", "name": "Oats"})
    assert client.get("/capture/lookup?code=0123456789012").json()["item"] is None
    client.post("/capture/barcode", data={"code": "0123456789012", "name": "Oats", "target": "shopping", "list": "groceries"})
    client.post("/capture/barcode", data={"code": "0123456789012", "name": "Oats", "target": "pantry"})
    client.post("/capture/barcode", data={"code": "0123456789012", "name": "Oats", "target": "pantry"})
    client.post("/capture/barcode", data={"code": "885911", "name": "Drill", "brand": "DeWalt", "target": "item"})
    assert rows("SELECT name, barcode FROM shopping_items") == [{"name": "Oats", "barcode": "0123456789012"}]
    assert rows("SELECT name, qty FROM pantry_items") == [{"name": "Oats", "qty": 2.0}]
    assert rows("SELECT name, brand, barcode FROM items") == [{"name": "Drill", "brand": "DeWalt", "barcode": "885911"}]
    assert client.get("/capture/lookup?code=0123456789012").json()["pantry"]["qty"] == 2


def test_save_label_creates_or_updates_item_with_photo(client):
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (400, 300)).save(b, "JPEG")
    client.post("/capture/label", data={"brand": "Whirlpool", "model": "WDT730PAHZ0", "serial": "FW1"},
                files={"photo": ("l.jpg", b.getvalue(), "image/jpeg")})
    assert rows("SELECT name, model, serial FROM items") == [{"name": "Whirlpool WDT730PAHZ0", "model": "WDT730PAHZ0", "serial": "FW1"}]
    assert rows("SELECT caption FROM photos") == [{"caption": "Rating label"}]
    client.post("/capture/label", data={"item_id": "1", "serial": "FW2"})
    assert rows("SELECT serial, model FROM items") == [{"serial": "FW2", "model": "WDT730PAHZ0"}]


def test_save_receipt_creates_expense_document_and_purchase(client):
    client.post("/items", data={"name": "Water heater"})
    client.post("/capture/receipt", data={"total": "916.92", "date": "2026-11-02", "store": "The Home Depot",
                                          "item_id": "1", "set_purchase": "1", "category": "Appliances"},
                files={"photo": ("r.jpg", b"jpegdata", "image/jpeg")})
    e = rows("SELECT amount, note, category, document_id FROM fund_entries")[0]
    assert (e["amount"], e["note"], e["category"]) == (916.92, "The Home Depot", "Appliances") and e["document_id"]
    assert rows("SELECT price, purchase_date FROM items") == [{"price": 916.92, "purchase_date": "2026-11-02"}]


def test_ocr_hook_and_page(client, monkeypatch):
    assert client.post("/capture/ocr", files={"image": ("x.jpg", b"x", "image/jpeg")}).status_code == 501
    from app import capture
    monkeypatch.setenv("OCR_URL", "http://ocr.local/read")
    monkeypatch.setattr(capture, "ocr_remote", lambda d, m: "TOTAL 9.99")
    assert client.post("/capture/ocr", files={"image": ("x.jpg", b"x", "image/jpeg")}).json() == {"text": "TOTAL 9.99"}
    assert client.post("/capture/parse", json={"mode": "receipt", "text": "TOTAL 9.99"}).json()["total"] == 9.99
    for m in ("barcode", "label", "receipt"):
        assert client.get(f"/capture?mode={m}").status_code == 200


def test_manual_attach(client, monkeypatch):
    from app import capture
    client.post("/items", data={"name": "Dishwasher"})
    client.post("/items/1", data={"name": "Dishwasher", "brand": "Bosch", "model": "SHPM88Z75N"})
    assert "SHPM88Z75N" in client.get("/items/1").text and "manualslib" in client.get("/items/1").text
    monkeypatch.setattr(capture, "fetch_manual", lambda u: (b"%PDF-1.7 manual", "shpm88.pdf"))
    client.post("/items/1/manual-url", data={"url": "https://example.com/shpm88.pdf"})
    assert rows("SELECT title, filename FROM documents") == [{"title": "Manual: Bosch SHPM88Z75N", "filename": "shpm88.pdf"}]
    monkeypatch.setattr(capture, "fetch_manual", lambda u: (_ for _ in ()).throw(ValueError("That link isn't a PDF")))
    r = client.post("/items/1/manual-url", data={"url": "https://example.com/page"}, follow_redirects=False)
    assert "PDF" in r.headers["location"].replace("+", " ")
