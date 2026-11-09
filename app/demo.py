"""HEARTH_DEMO=1: a realistic sample household in $DATA_DIR/demo/ (never the real database)."""
import io
import json
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw

from . import plan
from .db import data_dir, get_db


def _img(c1, c2, shape) -> tuple[str, str]:
    from .media import save_image
    im = Image.new("RGB", (900, 675))
    d = ImageDraw.Draw(im)
    for y in range(675):
        t = y / 675
        d.line([(0, y), (900, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    d.rounded_rectangle(shape, radius=24, fill=(240, 238, 230), outline=(50, 55, 52), width=6)
    b = io.BytesIO()
    im.save(b, "JPEG")
    return save_image(b.getvalue())


def seed_if_empty() -> bool:
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            return False
    t = date.today()
    D = lambda n: (t + timedelta(days=n)).isoformat()  # noqa: E731
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        rooms = {}
        for r in ["Kitchen", "Living Room", "Garage", "Basement", "Laundry", "Primary Bedroom"]:
            rooms[r] = db.execute("INSERT INTO rooms (name) VALUES (?)", (r,)).lastrowid
        shelf = db.execute("INSERT INTO rooms (name, parent_id) VALUES ('Shelf 2', ?)", (rooms["Garage"],)).lastrowid
        items = {}
        for name, room, brand, model, serial, pd, price, war, life, rc, cat in [
            ("Refrigerator", "Kitchen", "Samsung", "RF28R7351SG", "0B4H43BR", "2019-04-12", 2199, D(23), 13, 2400, "Appliance"),
            ("Dishwasher", "Kitchen", "Bosch", "SHPM88Z75N", "FD9903", "2021-08-03", 1149, D(240), 10, 1200, "Appliance"),
            ("Range", "Kitchen", "GE", "JGB735SPSS", None, "2016-02-20", 899, None, 12, 1100, "Appliance"),
            ("TV", "Living Room", "LG", "OLED65C3", "311MXAB", "2023-11-24", 1799, "2025-11-24", 8, 1600, "Electronics"),
            ("Water heater", "Basement", "Rheem", "XG50T06EC36U1", "Q412345", "2017-01-10", 1100, None, 12, 1368, "Plumbing"),
            ("Furnace", "Basement", "Carrier", "59SC5A080E17", "2412A1234", "2012-10-10", 3800, None, 18, 5200, "HVAC"),
            ("Washer", "Laundry", "LG", "WM4000HWA", "009KWZ", "2020-03-14", 849, None, 11, 950, "Appliance"),
            ("Garage door opener", "Garage", "Chamberlain", "B6765T", None, "2022-05-30", 329, D(560), 12, 400, "Electrical"),
            ("Cordless drill", "Garage", "DeWalt", "DCD791D2", None, "2021-06-01", 179, None, 8, 180, "Tools"),
        ]:
            rid = shelf if name == "Cordless drill" else rooms[room]
            items[name] = db.execute(
                "INSERT INTO items (name, room_id, brand, model, serial, purchase_date, price, warranty_until, life_years, "
                "replace_cost, category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, rid, brand, model, serial, pd, price, war, life, rc, cat)).lastrowid
        cons = {}
        for name, trade, phone, email in [("Mike Alvarez", "Plumber", "555-0142", "mike@example.com"),
                                          ("Brightline Electric", "Electrician", "555-0188", None),
                                          ("Sarah Chen", "HVAC", "555-0110", "sarah@example.com")]:
            cons[trade] = db.execute("INSERT INTO contractors (name, trade, phone, email) VALUES (?,?,?,?)",
                                     (name, trade, phone, email)).lastrowid
    # photos (outside the transaction above: files are written first)
    colors = [((80, 90, 100), (30, 35, 40)), ((200, 200, 205), (120, 120, 130)), ((60, 60, 60), (20, 20, 20)),
              ((180, 170, 150), (90, 85, 70)), ((150, 160, 170), (70, 80, 95))]
    with get_db() as db:
        for n, name in enumerate(["Refrigerator", "Dishwasher", "TV", "Water heater", "Washer"]):
            a, b = _img(*colors[n % len(colors)], (250, 120, 650, 640))
            db.execute("INSERT INTO photos (item_id, stored_name, thumb_name, is_cover, created_at) VALUES (?,?,?,1,?)",
                       (items[name], a, b, now))
        a, b = _img((210, 170, 120), (120, 80, 50), (150, 250, 750, 600))
        db.execute("INSERT INTO photos (room_id, stored_name, thumb_name, is_cover, created_at) VALUES (?,?,?,1,?)",
                   (rooms["Kitchen"], a, b, now))
        # tasks: staggered seed + item tasks
        taken = []
        from .main import DEFAULT_TASKS
        for name, days in DEFAULT_TASKS:
            due = plan.stagger_date(days, taken, t)
            taken.append(due)
            kind = "safety" if any(w in name.lower() for w in ("smoke", "co ", "extinguisher", "gfci")) else "chore"
            db.execute("INSERT INTO tasks (name, interval_days, next_due, kind) VALUES (?,?,?,?)", (name, days, due, kind))
        db.execute("INSERT INTO tasks (name, interval_days, next_due, item_id) VALUES ('Descale dishwasher', 60, ?, ?)",
                   (D(-4), items["Dishwasher"]))
        db.execute("INSERT INTO tasks (name, interval_days, next_due, item_id) VALUES ('Clean fridge coils', 180, ?, ?)",
                   (D(0), items["Refrigerator"]))
        hvac = db.execute("SELECT id FROM tasks WHERE name='Service HVAC system'").fetchone()[0]
        flush = db.execute("SELECT id FROM tasks WHERE name='Flush water heater'").fetchone()[0]
        filt = db.execute("SELECT id FROM tasks WHERE name='Replace HVAC air filter'").fetchone()[0]
        db.execute("UPDATE tasks SET item_id=? WHERE id IN (?, ?)", (items["Furnace"], hvac, filt))
        db.execute("UPDATE tasks SET item_id=? WHERE id=?", (items["Water heater"], flush))
        for tid, d, cost, cid in [(hvac, D(-56), 185, cons["HVAC"]), (flush, D(-38), 240, cons["Plumber"]), (filt, D(-8), 32, None)]:
            db.execute("INSERT INTO task_logs (task_id, done_on, cost, contractor_id) VALUES (?,?,?,?)", (tid, d, cost, cid))
        for title, d, st, et, cid, kind, rm in [("Fix kitchen sink leak", D(1), "10:00", "11:30", cons["Plumber"], "appointment", 60),
                                                ("Annual HVAC tune-up", D(3), "08:00", "10:00", cons["HVAC"], "appointment", 1440),
                                                ("Take out recycling", D(0), "19:00", None, None, "reminder", 0),
                                                ("Panel inspection", D(9), "13:00", "14:00", cons["Electrician"], "appointment", 120)]:
            db.execute("INSERT INTO appointments (kind, title, starts_at, ends_at, contractor_id, remind_minutes, location) "
                       "VALUES (?,?,?,?,?,?,?)", (kind, title, f"{d}T{st}", f"{d}T{et}" if et else None, cid, rm,
                                                  "Home" if cid else None))
        # money
        db.execute("INSERT INTO budgets (year, amount) VALUES (?, 4800)", (t.year,))
        db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note) VALUES (?, 'allocation', 1500, 'Opening balance')",
                   (f"{t.year - 1}-06-01",))
        for m in range(1, t.month + 1):
            db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note) VALUES (?, 'allocation', 350, 'Monthly transfer')",
                       (f"{t.year}-{m:02d}-01",))
        for d, amt, note, cat in [(f"{t.year}-02-17", 420, "Sump pump replacement", "Repairs"),
                                  (f"{t.year}-07-22", 1350, "Roof patch after storm", "Repairs"),
                                  (f"{t.year}-09-20", 320, "Garage door spring", "Repairs"),
                                  (f"{t.year}-10-11", 85, "Caulk and filters", "Supplies")]:
            db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, category) VALUES (?, 'expense', ?, ?, ?)",
                       (d, amt, note, cat))
        db.execute("INSERT INTO funds (name, target_amount, target_date, balance) VALUES ('Roof replacement', 14000, ?, 2500)",
                   (f"{t.year + 6}-05-01",))
        # lists and pantry
        for lk, txt in [("groceries", ["2 gal milk", "bananas", "sourdough bread", "12 eggs", "coffee", "spinach"]),
                        ("supplies", ["16x25x1 furnace filter", "AA batteries", "caulk"])]:
            for x in txt:
                from .shopping import guess_category, parse_quick
                name, qty = parse_quick(x)
                db.execute("INSERT INTO shopping_items (list, name, qty, category, added_at) VALUES (?,?,?,?,?)",
                           (lk, name, qty, guess_category(name), now))
        for name, qty, unit, mn, bb, cat in [("Rice", 1, "bag", 1, None, "Pantry"), ("Olive oil", 1, "bottle", 1, None, "Pantry"),
                                             ("Greek yogurt", 2, "tub", None, D(2), "Dairy & Eggs"),
                                             ("Coffee beans", 0, "bag", 1, None, "Pantry"),
                                             ("Paper towels", 6, "roll", 4, None, "Household")]:
            db.execute("INSERT INTO pantry_items (name, qty, unit, min_qty, best_before, category) VALUES (?,?,?,?,?,?)",
                       (name, qty, unit, mn, bb, cat))
        # vehicle + project
        vid = db.execute("INSERT INTO vehicles (name, make, model, year, odometer, purchase_odometer, odometer_log, plate) "
                         "VALUES ('Family Outback', 'Subaru', 'Outback', 2019, 61840, 52000, ?, 'HRT-2041')",
                         (json.dumps([{"date": D(-200), "miles": 55200}, {"date": D(-90), "miles": 58900},
                                      {"date": D(-3), "miles": 61840}]),)).lastrowid
        oil = db.execute("INSERT INTO tasks (name, interval_days, next_due, vehicle_id, interval_miles, next_due_miles) "
                         "VALUES ('Oil change', 180, ?, ?, 5000, 63900)", (D(64), vid)).lastrowid
        db.execute("INSERT INTO task_logs (task_id, done_on, cost, odometer) VALUES (?,?,?,?)", (oil, D(-116), 79, 58900))
        db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, category, vehicle_id) "
                   "VALUES (?, 'expense', 640, 'Four new tires', 'Vehicle', ?)", (D(-150), vid))
        pid = db.execute("INSERT INTO projects (name, kind, status, budget, notes, materials, started_on) "
                         "VALUES ('Rebuild back deck', 'project', 'active', 2500, 'Composite boards, keep the existing joists.', ?, ?)",
                         (json.dumps([{"name": "Composite deck boards", "qty": 40, "cost": 32},
                                      {"name": "Hidden fasteners", "qty": 4, "cost": 45},
                                      {"name": "Cordless drill", "qty": 1, "item_id": items["Cordless drill"]}]), D(-20))).lastrowid
        db.execute("INSERT INTO fund_entries (entry_date, kind, amount, note, project_id, category) "
                   "VALUES (?, 'expense', 980, 'Deck boards (first half)', ?, 'Improvements')", (D(-12), pid))
        hob = db.execute("INSERT INTO projects (name, kind, status, notes, materials) VALUES "
                         "('3D printing', 'hobby', 'active', 'Bambu P1S in the office.', '[]')").lastrowid
        db.execute("INSERT INTO pantry_items (name, qty, unit, min_qty, category, project_id) VALUES "
                   "('PLA filament, black', 1, 'spool', 2, 'Consumables', ?)", (hob,))
        from .shopping import pantry_restock_check
        for (pid,) in db.execute("SELECT id FROM pantry_items").fetchall():
            pantry_restock_check(db, pid)  # same rule as real use: below minimum -> on the list
    (data_dir() / "DEMO_DATA_ONLY").write_text("This folder holds Hearth demo data (HEARTH_DEMO=1).\n")
    return True
