"""Search: SQLite FTS5 full-text index plus field filters, all local.

    fridge                     words match anywhere (prefix: "refrig" finds refrigerator)
    "wine fridge"              exact phrase
    dishwasher -bosch          exclude a word;  a OR b  either word
    room:kitchen price>500     filters on any field, including custom fields and formulas
    warranty<2027  purchased:2019  due<today  due<+30d
    in:items  sort:-price      limit to a kind; sort (- for descending)

The index (search_fts) is rebuilt on demand when triggers have marked data as changed (search_state).
"""
import re
from datetime import date, timedelta

from . import fields as FD
from .db import get_db

_TOKEN = re.compile(r'(-?)(?:([A-Za-z_][\w]*)(:|>=|<=|!=|>|<|=))?("[^"]*"?|\S+)')
KIND_ALIASES = {"item": "items", "inventory": "items", "task": "tasks", "appointments": "schedule",
                "appointment": "schedule", "doc": "documents", "docs": "documents", "document": "documents",
                "contractor": "contractors", "list": "shopping", "vehicle": "vehicles", "car": "vehicles",
                "project": "projects", "hobbies": "projects", "photo": "photos", "ledger": "budget",
                "expenses": "budget"}
NUMERIC = {"number", "money", "years", "days", "percent"}


# ---------------- index ----------------

def ensure_index(force: bool = False) -> None:
    with get_db() as db:
        row = db.execute("SELECT dirty FROM search_state WHERE id=1").fetchone()
        if not force and row and not row["dirty"]:
            return
        db.execute("UPDATE search_state SET dirty=0 WHERE id=1")
        db.execute("DELETE FROM search_fts")
        for kind, k in FD.KINDS.items():
            text = " || ' ' || ".join(f"COALESCE({c}, '')" for c in k["text"][1:]) or "''"
            where = f" WHERE {k['where']}" if k.get("where") else ""
            db.execute(f"INSERT INTO search_fts (kind, ref_id, title, body) SELECT ?, {k['id']}, "
                       f"COALESCE({k['text'][0]}, ''), {text} {k['frm']}{where}", (kind,))
        # text custom fields are searchable too
        db.execute("INSERT INTO search_fts (kind, ref_id, title, body) SELECT cv.entity, cv.record_id, '', "
                   "group_concat(cv.value, ' ') FROM custom_values cv JOIN custom_fields cf ON cf.id=cv.field_id "
                   "WHERE cf.kind='text' GROUP BY cv.entity, cv.record_id")


# ---------------- query parsing ----------------

def parse_query(q: str) -> dict:
    terms, negs, filters, kinds, sort, errors = [], [], [], [], None, []
    any_or = False
    for neg, field, op, val in _TOKEN.findall(q or ""):
        val = val.strip()
        quoted = val.startswith('"')
        val = val.strip('"')
        if not val:
            continue
        if field:
            f = field.lower()
            if f in ("in", "type", "is"):
                k = KIND_ALIASES.get(val.lower(), val.lower())
                if k in FD.KINDS:
                    kinds.append(k)
                else:
                    errors.append(f"Unknown kind “{val}”. Try: " + ", ".join(FD.KINDS))
            elif f == "sort":
                sort = (val.lstrip("-").lower(), val.startswith("-"))
            else:
                filters.append({"field": f, "op": "!=" if neg and op in (":", "=") else op, "value": val,
                                "text": f"{'-' if neg else ''}{field}{op}{val}"})
            continue
        if val == "OR" and not quoted:
            any_or = True
            continue
        (negs if neg else terms).append((val, quoted))
    return dict(terms=terms, negs=negs, filters=filters, kinds=kinds, sort=sort, errors=errors, any_or=any_or)


def _fts_query(terms, any_or) -> str:
    parts = []
    for val, quoted in terms:
        toks = re.findall(r"\w+", val, flags=re.UNICODE)
        if not toks:
            continue
        phrase = '"' + " ".join(toks) + '"'
        parts.append(phrase if quoted else phrase + "*")
    return (" OR " if any_or else " AND ").join(parts)


# ---------------- filter evaluation ----------------

def _date_bounds(raw: str):
    s = raw.strip().lower()
    t = date.today()
    if s == "today":
        return t.isoformat(), t.isoformat()
    m = re.fullmatch(r"([+-]?)(\d+)([dwmy])", s)
    if m:
        n = int(m[2]) * {"d": 1, "w": 7, "m": 30, "y": 365}[m[3]]
        d = t + timedelta(days=-n if m[1] == "-" else n)
        return d.isoformat(), d.isoformat()
    if re.fullmatch(r"\d{4}", s):
        return f"{s}-01-01", f"{s}-12-31"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01", f"{s}-31"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s, s
    return None


def _match(v, ftype: str, op: str, raw: str) -> bool:
    if ftype == "date" or isinstance(v, date):
        b = _date_bounds(raw)
        if b is None:
            raise ValueError(f"“{raw}” isn't a date. Use 2027, 2027-06, 2027-06-01, today or +30d")
        if v is None or v == "":
            return op == "!="
        s = (v.isoformat() if isinstance(v, date) else str(v))[:10]
        lo, hi = b
        return {":": lo <= s <= hi, "=": lo <= s <= hi, "!=": not (lo <= s <= hi), "<": s < lo,
                "<=": s <= hi, ">": s > hi, ">=": s >= lo}[op]
    if ftype in NUMERIC or (isinstance(v, (int, float)) and not isinstance(v, bool)):
        n = FD.parse_number(raw)
        if n is None:
            raise ValueError(f"“{raw}” isn't a number")
        if v is None or v == "" or isinstance(v, str):
            return op == "!="
        return {":": abs(v - n) < 1e-9, "=": abs(v - n) < 1e-9, "!=": abs(v - n) >= 1e-9, "<": v < n,
                "<=": v <= n, ">": v > n, ">=": v >= n}[op]
    if op in ("<", ">", "<=", ">="):
        raise ValueError(f"Use field:{raw} for text fields")
    hay = "" if v is None else str(v).lower()
    if isinstance(v, bool):
        hay = "yes" if v else "no"
    needle = raw.lower()
    return {":": needle in hay, "=": hay == needle, "!=": needle not in hay}[op]


# ---------------- search ----------------

def search_full(q: str, limit: int = 8) -> dict:
    q = (q or "").strip()
    out = dict(groups=[], filters=[], errors=[], total=0, q=q, sort=None)
    if not q:
        return out
    p = parse_query(q)
    out["errors"] += p["errors"]
    ensure_index()
    fts = _fts_query(p["terms"], p["any_or"])
    with get_db() as db:
        cats = {k: FD.catalog(k, db) for k in FD.KINDS}
        kinds = p["kinds"] or list(FD.KINDS)
        need = [f["field"] for f in p["filters"]] + ([p["sort"][0]] if p["sort"] else [])
        for f in dict.fromkeys(need):
            if not any(f in cats[k] for k in kinds):
                known = sorted({key for k in kinds for key in cats[k]})
                out["errors"].append(f"Unknown field “{f}”. Fields you can use: {', '.join(known[:40])}")
        if out["errors"]:
            return out
        kinds = [k for k in kinds if all(f in cats[k] for f in need)]
        out["filters"] = [{**f, "label": next(cats[k][f["field"]]["label"] for k in FD.KINDS
                                               if f["field"] in cats[k])} for f in p["filters"]]

        rank: dict[tuple, float] = {}
        text_mode = bool(fts)
        if text_mode:
            try:
                rows = db.execute("SELECT kind, ref_id, bm25(search_fts, 0, 0, 8.0, 1.0) AS r FROM search_fts "
                                  "WHERE search_fts MATCH ? ORDER BY r LIMIT 2000", (fts,)).fetchall()
            except Exception:
                rows = []
            if not rows:  # fallback: substring anywhere (mid-word, punctuation)
                pats = [f"%{v.lower()}%" for v, _ in p["terms"]]
                cond = (" OR " if p["any_or"] else " AND ").join(["lower(title || ' ' || body) LIKE ?"] * len(pats))
                rows = db.execute(f"SELECT kind, ref_id, 0 AS r FROM search_fts WHERE {cond} LIMIT 2000",
                                  pats).fetchall()
            for r in rows:
                key = (r["kind"], int(r["ref_id"]))
                rank[key] = min(rank.get(key, 0), r["r"])

        groups = []
        for kind in kinds:
            ids = sorted({i for (k, i) in rank if k == kind}) if text_mode else None
            if text_mode and not ids:
                continue
            recs = FD.load(kind, ids, db)
            cat = cats[kind]
            keep = []
            for r in recs:
                try:
                    ok = all(_match(r["values"].get(f["field"]), cat[f["field"]]["type"], f["op"], f["value"])
                             for f in p["filters"])
                except ValueError as e:
                    out["errors"].append(str(e))
                    out["groups"] = []
                    return out
                if ok and p["negs"]:
                    hay = " ".join(str(v) for v in r["values"].values() if v is not None).lower() + \
                        " " + (r["title"] or "").lower()
                    ok = not any(v.lower() in hay for v, _ in p["negs"])
                if ok:
                    keep.append(r)
            if not keep:
                continue
            if p["sort"]:
                sk, desc = p["sort"]
                with_v = [r for r in keep if r["values"].get(sk) not in (None, "")]
                without = [r for r in keep if r["values"].get(sk) in (None, "")]
                with_v.sort(key=lambda r: _sortable(r["values"][sk]), reverse=desc)
                keep = with_v + without
            elif text_mode:
                keep.sort(key=lambda r: rank.get((kind, r["id"]), 0))
            else:  # filters only: order by the first filtered number/date field, else by name
                fk = next((f["field"] for f in p["filters"] if cat[f["field"]]["type"] != "text"), None)
                keep.sort(key=lambda r: (r["values"].get(fk) in (None, ""),
                                         _sortable(r["values"].get(fk)) if fk and r["values"].get(fk) not in (None, "")
                                         else (3, ""), (r["title"] or "").lower()))
            show = list(dict.fromkeys([f["field"] for f in p["filters"]] + ([p["sort"][0]] if p["sort"] else [])))
            rows_out = []
            for r in keep[:limit]:
                facts = [f"{cat[key]['label']}: {FD.fmt_value(r['values'].get(key), cat[key]['type'])}"
                         for key in show]
                rows_out.append({"id": r["id"], "title": r["title"], "sub": r["sub"], "href": r["href"],
                                 "facts": facts})
            best = min((rank.get((kind, r["id"]), 0) for r in keep), default=0)
            groups.append(dict(label=FD.KINDS[kind]["label"], icon=FD.KINDS[kind]["icon"], kind=kind,
                               rows=rows_out, count=len(keep), best=best))
        if text_mode and not p["sort"]:
            groups.sort(key=lambda g: g["best"])
        out["groups"] = groups
        out["total"] = sum(g["count"] for g in groups)
        out["sort"] = p["sort"]
    return out


def _sortable(v):
    if isinstance(v, date):
        return (1, v.isoformat())
    if isinstance(v, (int, float)):
        return (0, v)
    return (2, str(v).lower())


def search(q: str, limit: int = 8) -> list[dict]:
    """Back-compatible: list of groups (palette, older callers)."""
    return search_full(q, limit)["groups"]
