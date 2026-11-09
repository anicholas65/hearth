"""Insurance claim packet (PDF) for the whole house or one room: photos, values, serials, receipts."""
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import __version__
from .db import data_dir, get_db

FOREST, CREAM, LINE = colors.HexColor("#36594B"), colors.HexColor("#F5F1E8"), colors.HexColor("#D9D2C3")


def packet_data(room_id: int | None) -> dict:
    with get_db() as db:
        rooms = db.execute("SELECT id, name FROM rooms" + (" WHERE id=?" if room_id else "") + " ORDER BY name",
                           (room_id,) if room_id else ()).fetchall()
        groups = []
        targets = [(r["id"], r["name"]) for r in rooms] + ([] if room_id else [(None, "No room")])
        for rid, rname in targets:
            items = db.execute(
                "SELECT i.*, (SELECT stored_name FROM photos p WHERE p.item_id=i.id ORDER BY is_cover DESC, id LIMIT 1) AS photo "
                "FROM items i WHERE " + ("i.room_id=?" if rid else "i.room_id IS NULL") + " ORDER BY i.name",
                (rid,) if rid else ()).fetchall()
            if not items:
                continue
            out = []
            for it in items:
                docs = [d["title"] for d in db.execute("SELECT title FROM documents WHERE item_id=? ORDER BY uploaded_at",
                                                       (it["id"],))]
                out.append({**dict(it), "docs": docs})
            groups.append({"room": rname, "items": out, "value": sum(i["price"] or 0 for i in out),
                           "replace": sum(i["replace_cost"] or i["price"] or 0 for i in out)})
    return {"groups": groups, "items": sum(len(g["items"]) for g in groups),
            "value": sum(g["value"] for g in groups), "replace": sum(g["replace"] for g in groups)}


def insurance_pdf(room_id: int | None = None, owner: str = "") -> bytes:
    data = packet_data(room_id)
    buf = io.BytesIO()
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=FOREST, alignment=0, fontSize=22)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=FOREST)
    small = ParagraphStyle("s", parent=ss["Normal"], fontSize=8.5, leading=11)
    body = ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=12)
    money = lambda v: f"${v:,.2f}" if v else "—"  # noqa: E731
    story = [Paragraph("Home inventory — insurance claim packet", h1),
             Paragraph(f"Prepared {date.today():%B %d, %Y}" + (f" for {owner}" if owner else "")
                       + (f" · Room: {data['groups'][0]['room']}" if room_id and data["groups"] else " · Whole house"), body),
             Spacer(1, 10)]
    summary = [["Rooms", "Items", "Recorded value", "Replacement estimate"],
               [str(len(data["groups"])), str(data["items"]), money(data["value"]), money(data["replace"])]]
    t = Table(summary, colWidths=[1.2 * inch, 1.0 * inch, 2.0 * inch, 2.2 * inch])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), CREAM), ("GRID", (0, 0), (-1, -1), .5, LINE),
                           ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9.5)]))
    story += [t, Spacer(1, 6), Paragraph(
        "Values are purchase prices recorded in Hearth; replacement estimates use the owner's figures where set. "
        "Receipts and manuals listed are stored in Hearth and can be exported on request.", small)]
    up = data_dir() / "uploads"
    for g in data["groups"]:
        story += [PageBreak(), Paragraph(f"{g['room']} — {len(g['items'])} items, {money(g['value'])}", h2)]
        for it in g["items"]:
            img = None
            if it["photo"] and (up / it["photo"]).exists():
                try:
                    img = Image(str(up / it["photo"]), width=1.6 * inch, height=1.2 * inch, kind="proportional")
                except Exception:
                    img = None
            facts = [["Brand / model", " ".join(x for x in (it["brand"], it["model"]) if x) or "—"],
                     ["Serial", it["serial"] or "—"], ["Purchased", (it["purchase_date"] or "—") + "  " + money(it["price"])],
                     ["Replacement", money(it["replace_cost"])], ["Warranty", it["warranty_until"] or "—"],
                     ["Receipts / docs", ", ".join(it["docs"]) or "none on file"]]
            ft = Table([[Paragraph(f"<b>{a}</b>", small), Paragraph(str(b), small)] for a, b in facts],
                       colWidths=[1.2 * inch, 3.6 * inch])
            ft.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            row = Table([[img or Paragraph("<i>no photo</i>", small),
                          [Paragraph(f"<b>{it['name']}</b>" + (f" · {it['category']}" if it.get("category") else ""), body), ft]]],
                        colWidths=[1.8 * inch, 4.9 * inch])
            row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), .5, LINE),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8)]))
            story.append(KeepTogether(row))

    def footer(c, d):
        c.saveState()
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.grey)
        c.drawString(0.6 * inch, 0.45 * inch, f"Hearth {__version__} · insurance claim packet")
        c.drawRightString(7.9 * inch, 0.45 * inch, f"Page {d.page}")
        c.restoreState()

    SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.6 * inch,
                      bottomMargin=0.7 * inch, title="Home inventory — insurance claim packet",
                      author=owner or "Hearth").build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
