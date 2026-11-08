"""Universal search across every kind of record."""
from .db import get_db

# (group label, icon, SQL selecting id, title, sub, href) — each takes the LIKE pattern N times
SOURCES = [
    ("Inventory", "box",
     "SELECT i.id, i.name AS title, trim(COALESCE(i.brand,'') || ' ' || COALESCE(i.model,'') || "
     "CASE WHEN r.name IS NOT NULL THEN ' · ' || r.name ELSE '' END) AS sub, '/items/' || i.id AS href "
     "FROM items i LEFT JOIN rooms r ON r.id=i.room_id "
     "WHERE i.name LIKE :q OR i.brand LIKE :q OR i.model LIKE :q OR i.serial LIKE :q OR i.notes LIKE :q "
     "OR r.name LIKE :q"),
    ("Tasks", "wrench",
     "SELECT id, name AS title, 'Next due ' || next_due AS sub, '/tasks/' || id AS href FROM tasks "
     "WHERE name LIKE :q OR notes LIKE :q"),
    ("Schedule", "calendar",
     "SELECT id, title, replace(substr(starts_at,1,16),'T',' ') || "
     "CASE WHEN location IS NOT NULL THEN ' · ' || location ELSE '' END AS sub, "
     "'/schedule/' || id AS href FROM appointments WHERE title LIKE :q OR notes LIKE :q OR location LIKE :q"),
    ("Documents", "file",
     "SELECT id, title, filename AS sub, '/documents/' || id || '/file' AS href FROM documents "
     "WHERE title LIKE :q OR filename LIKE :q"),
    ("Contractors", "users",
     "SELECT id, name AS title, trim(COALESCE(trade,'') || ' ' || COALESCE(phone,'')) AS sub, "
     "'/contractors/' || id AS href FROM contractors "
     "WHERE name LIKE :q OR trade LIKE :q OR phone LIKE :q OR email LIKE :q OR notes LIKE :q"),
    ("Shopping", "cart",
     "SELECT id, name AS title, CASE list WHEN 'groceries' THEN 'Groceries' ELSE 'Supplies' END || "
     "COALESCE(' · ' || qty, '') AS sub, '/shopping?list=' || list AS href FROM shopping_items "
     "WHERE status != 'bought' AND (name LIKE :q OR note LIKE :q OR store LIKE :q)"),
    ("Photos", "camera",
     "SELECT id, COALESCE(caption, 'Photo ' || substr(created_at,1,10)) AS title, "
     "substr(created_at,1,10) AS sub, '/gallery/' || id AS href FROM photos WHERE caption LIKE :q"),
    ("Budget", "wallet",
     "SELECT id, COALESCE(note, kind) AS title, entry_date || ' · $' || printf('%.2f', amount) AS sub, "
     "'/budget?year=' || substr(entry_date,1,4) AS href FROM fund_entries WHERE note LIKE :q"),
]


def search(q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    groups = []
    with get_db() as db:
        for label, icon, sql in SOURCES:
            sql = sql.replace("LIKE :q", "LIKE :q ESCAPE '\\'")
            rows = db.execute(f"{sql} LIMIT {limit}", {"q": pat}).fetchall()
            if rows:
                groups.append(dict(label=label, icon=icon, rows=[dict(r) for r in rows]))
    return groups
