"""Recurrence presets and labels. interval_days == 0 means a one-time task."""

PRESETS = [  # (days, label) — values match the defaults Hearth has always used
    (0, "Does not repeat"), (1, "Daily"), (7, "Weekly"), (14, "Every 2 weeks"), (30, "Monthly"),
    (90, "Every 3 months"), (180, "Every 6 months"), (365, "Yearly"),
]
PRESET_DAYS = {d for d, _ in PRESETS}


def repeat_label(days) -> str:
    days = int(days or 0)
    for d, label in PRESETS:
        if d == days:
            return label
    if days % 365 == 0:
        return f"Every {days // 365} years"
    if days % 30 == 0:
        return f"Every {days // 30} months"
    if days % 7 == 0:
        return f"Every {days // 7} weeks"
    return f"Every {days} days"


def parse_repeat(preset: str | None, custom: str | None, fallback: int | None = None) -> int:
    """Form -> interval_days. 'custom' uses the exact number entered; unknown keeps fallback."""
    if preset == "custom":
        try:
            return max(1, int(custom or 0))
        except ValueError:
            pass
    else:
        try:
            v = int(preset)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return fallback if fallback is not None else 90


ITEM_CATEGORIES = ["Appliance", "HVAC", "Plumbing", "Electrical", "Electronics", "Furniture",
                   "Outdoor & Garden", "Tools", "Structure", "Other"]
DEFAULT_EXPENSE_CATEGORIES = ["Maintenance", "Repairs", "Appliances", "Supplies", "Improvements",
                              "Utilities", "Other"]


# ---------------- scheduling: stagger + rebalance + freshness ----------------
from datetime import date as _date, timedelta as _td  # noqa: E402


def _load(dues: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in dues:
        out[d] = out.get(d, 0) + 1
    return out


def stagger_date(interval: int, taken: list[str], today: _date | None = None) -> str:
    """Pick the least-busy day in (today, today+interval] for a new recurring task.

    Busy-ness counts tasks due that day plus half of those on the neighbouring days, so tasks spread
    across the whole interval instead of piling up. Ties go to the earlier day (sooner is safer)."""
    today = today or _date.today()
    window = max(1, min(int(interval or 1), 365))
    load = _load(taken)
    best, best_score = None, None
    for k in range(1, window + 1):
        d = today + _td(days=k)
        score = (load.get(d.isoformat(), 0) * 2 + load.get((d - _td(days=1)).isoformat(), 0)
                 + load.get((d + _td(days=1)).isoformat(), 0))
        if best_score is None or score < best_score:
            best, best_score = d, score
    return best.isoformat()


def rebalance(tasks: list[dict], fixed_dues: list[str], today: _date | None = None) -> dict[int, str]:
    """New due dates for chores, spread over each one's interval. Shortest intervals are placed first
    (they have the fewest options). Safety tasks are passed in fixed_dues and never moved."""
    today = today or _date.today()
    taken = list(fixed_dues)
    out = {}
    for t in sorted(tasks, key=lambda t: (t["interval_days"], t["id"])):
        d = stagger_date(t["interval_days"], taken, today)
        out[t["id"]] = d
        taken.append(d)
    return out


def freshness(next_due: str, interval: int, today: _date | None = None) -> float:
    """1.0 just done → 0.0 when due (never negative: chores don't go 'red')."""
    today = today or _date.today()
    if not interval:
        return 1.0
    left = (_date.fromisoformat(next_due) - today).days
    return max(0.0, min(1.0, left / interval))
