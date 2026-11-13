"""Spreadsheet-style formulas, evaluated safely (no eval, no attribute access, no imports).

    = (price + upkeep) / max(age, 1)
    = if(warranty_days < 0, "expired", warranty_days)
    = round(replace_cost / life / 12, 2)

Syntax: numbers, "text", field names, + - * / % ^ (power), comparisons (= == != <> < <= > >=),
and/or/not, parentheses, and the functions in FUNCS. Dates are fields like purchased, warranty;
date - date gives days; date + number adds days. A blank input makes the result blank, not an error.
"""
import ast
import math
import re
from datetime import date, timedelta

MAX_LEN, MAX_NODES, MAX_POW = 400, 200, 64


class FormulaError(ValueError):
    pass


def _d(v):
    if isinstance(v, date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v[:10] or ""):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _need_date(v, fn):
    d = _d(v)
    if v is not None and d is None:
        raise FormulaError(f"{fn}() needs a date")
    return d


def _days_since(v):
    d = _need_date(v, "days_since")
    return None if d is None else (date.today() - d).days


def _days_until(v):
    d = _need_date(v, "days_until")
    return None if d is None else (d - date.today()).days


def _years_since(v):
    n = _days_since(v)
    return None if n is None else n / 365.25


def _months_since(v):
    d = _need_date(v, "months_since")
    if d is None:
        return None
    t = date.today()
    return (t.year - d.year) * 12 + t.month - d.month - (1 if t.day < d.day else 0)


def _year(v):
    d = _need_date(v, "year")
    return None if d is None else d.year


def _date(v):
    d = _d(v)
    if d is None:
        raise FormulaError('date() needs "YYYY-MM-DD"')
    return d


def _add_years(v, n):
    d = _need_date(v, "add_years")
    if d is None or n is None:
        return None
    y = d.year + int(n)
    return date(y, d.month, min(d.day, 28) if d.month == 2 else d.day)


def _round(x, n=0):
    return None if x is None else round(x, int(n or 0))


def _num_args(fn):
    def wrap(*a):
        vals = [x for x in a if x is not None]
        if not vals:
            return None
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
            raise FormulaError(f"{fn.__name__}() needs numbers")
        return fn(vals)
    return wrap


FUNCS = {
    "min": _num_args(min), "max": _num_args(max), "sum": _num_args(sum),
    "avg": _num_args(lambda v: sum(v) / len(v)),
    "round": _round, "abs": lambda x: None if x is None else abs(x),
    "floor": lambda x: None if x is None else math.floor(x), "ceil": lambda x: None if x is None else math.ceil(x),
    "sqrt": lambda x: None if x is None else (math.sqrt(x) if x >= 0 else None),
    "coalesce": lambda *a: next((x for x in a if x is not None and x != ""), None),
    "today": lambda: date.today(), "date": _date, "year": _year,
    "days_since": _days_since, "days_until": _days_until, "months_since": _months_since,
    "years_since": _years_since, "add_years": _add_years,
    "_if": None,  # lazy, handled in the evaluator
}
FUNC_HELP = {
    "if(cond, a, b)": "a when cond is true, else b", "min(a, b, …)": "smallest", "max(a, b, …)": "largest",
    "sum(a, b, …)": "total", "avg(a, b, …)": "average", "round(x, n)": "round to n decimals",
    "abs(x)": "absolute value", "floor(x) / ceil(x)": "round down / up", "sqrt(x)": "square root",
    "coalesce(a, b, …)": "first value that isn't blank", "today()": "today's date",
    "date(\"2027-06-01\")": "a fixed date", "year(d)": "year of a date", "days_since(d) / days_until(d)": "days",
    "months_since(d) / years_since(d)": "elapsed time", "add_years(d, n)": "date n years later",
}

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name,
            ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
            ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _prep(src: str) -> str:
    s = (src or "").strip()
    if s.startswith("="):
        s = s[1:].strip()
    s = s.replace("<>", "!=").replace("^", "**")
    s = re.sub(r"(?<![<>=!])=(?!=)", "==", s)  # spreadsheet "=" means equals
    s = re.sub(r"\bif\s*\(", "_if(", s, flags=re.I)
    s = re.sub(r"\b(AND|OR|NOT|TRUE|FALSE)\b", lambda m: {"TRUE": "True", "FALSE": "False"}.get(m[1], m[1].lower()), s)
    return s


def parse(src: str) -> ast.Expression:
    if not (src or "").strip().lstrip("="):
        raise FormulaError("Enter a formula")
    if len(src) > MAX_LEN:
        raise FormulaError(f"Keep formulas under {MAX_LEN} characters")
    try:
        tree = ast.parse(_prep(src), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Can't read the formula near column {max((e.offset or 1) - 1, 1)}") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise FormulaError("Formula is too long")
    for n in nodes:
        if not isinstance(n, _ALLOWED):
            raise FormulaError(f"“{ast.unparse(n) if hasattr(n, 'lineno') else type(n).__name__}” isn't allowed")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id.lower() not in FUNCS or n.keywords:
                name = n.func.id if isinstance(n.func, ast.Name) else "?"
                raise FormulaError(f"Unknown function {name}()")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (int, float, str, bool)):
            raise FormulaError("Only numbers and \"text\" are allowed")
    return tree


def names(src: str) -> set[str]:
    """Field names a formula refers to (functions excluded)."""
    tree = parse(src)
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in calls
            and n.id not in ("True", "False")}


def _num(v, op):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    raise FormulaError(f"{op} needs numbers")


def _binop(op, a, b):
    if a is None or b is None:
        return None
    da, db = _d(a) if not isinstance(a, (int, float)) else None, _d(b) if not isinstance(b, (int, float)) else None
    if isinstance(op, ast.Sub) and da and db:
        return (da - db).days
    if isinstance(op, (ast.Add, ast.Sub)) and da and isinstance(b, (int, float)):
        return da + timedelta(days=b if isinstance(op, ast.Add) else -b)
    if isinstance(op, ast.Add) and isinstance(a, str) and isinstance(b, str):
        return a + b
    a, b = _num(a, "Math"), _num(b, "Math")
    if isinstance(op, ast.Add):
        return a + b
    if isinstance(op, ast.Sub):
        return a - b
    if isinstance(op, ast.Mult):
        return a * b
    if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
        if b == 0:
            return None  # shown as blank, like a spreadsheet's #DIV/0 but quieter
        return a / b if isinstance(op, ast.Div) else a // b if isinstance(op, ast.FloorDiv) else a % b
    if isinstance(op, ast.Pow):
        if abs(b) > MAX_POW or abs(a) > 1e6:
            raise FormulaError("Power is too large")
        return a ** b
    raise FormulaError("Unsupported operator")


def _cmp(op, a, b):
    if a is None or b is None:
        return None
    da, db = _d(a), _d(b)
    if da and db:
        a, b = da, db
    elif isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    elif isinstance(a, str) != isinstance(b, str) or isinstance(a, date) != isinstance(b, date):
        if isinstance(op, (ast.Eq, ast.NotEq)):
            return isinstance(op, ast.NotEq)
        raise FormulaError("Can't compare text with numbers")
    return {ast.Eq: a == b, ast.NotEq: a != b}.get(type(op)) if isinstance(op, (ast.Eq, ast.NotEq)) else \
        (a < b if isinstance(op, ast.Lt) else a <= b if isinstance(op, ast.LtE) else
         a > b if isinstance(op, ast.Gt) else a >= b)


def _truthy(v):
    return bool(v) if v is not None else False


def evaluate(src_or_tree, env: dict):
    tree = parse(src_or_tree) if isinstance(src_or_tree, str) else src_or_tree
    lower_env = {k.lower(): v for k, v in env.items()}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in ("True", "False"):
                return n.id == "True"
            k = n.id.lower()
            if k not in lower_env:
                raise FormulaError(f"Unknown field “{n.id}”")
            v = lower_env[k]
            return None if v == "" else v
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.Not):
                return not _truthy(v)
            if v is None:
                return None
            v = _num(v, "Minus")
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            return _binop(n.op, ev(n.left), ev(n.right))
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return all(_truthy(ev(x)) for x in n.values)
            return any(_truthy(ev(x)) for x in n.values)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right_n in zip(n.ops, n.comparators):
                right = ev(right_n)
                r = _cmp(op, left, right)
                if not r:
                    return r
                left = right
            return True
        if isinstance(n, ast.IfExp):
            return ev(n.body) if _truthy(ev(n.test)) else ev(n.orelse)
        if isinstance(n, ast.Call):
            fn = n.func.id.lower()
            if fn == "_if":
                if len(n.args) not in (2, 3):
                    raise FormulaError("if() takes (condition, then, else)")
                if _truthy(ev(n.args[0])):
                    return ev(n.args[1])
                return ev(n.args[2]) if len(n.args) == 3 else None
            try:
                return FUNCS[fn](*[ev(a) for a in n.args])
            except TypeError:
                raise FormulaError(f"Wrong number of values for {fn}()") from None
        raise FormulaError("Unsupported expression")

    out = ev(tree)
    if isinstance(out, float) and (math.isnan(out) or math.isinf(out)):
        return None
    return out
