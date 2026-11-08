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
