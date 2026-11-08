"""Shopping list helpers: quick-add parsing and auto-categorisation."""
import re

LISTS = {"groceries": "Groceries", "supplies": "Supplies"}

# Display order (roughly store layout). Matching uses MATCH_ORDER below.
CATEGORIES = [
    ("Produce", "apple banana lettuce tomato onion potato carrot avocado lemon lime berries berry "
                "spinach pepper garlic cucumber celery broccoli grape orange fruit salad herbs cilantro "
                "mushroom zucchini kale ginger"),
    ("Dairy & Eggs", "milk cheese yogurt yoghurt butter egg eggs cream sour creamer"),
    ("Meat & Seafood", "chicken beef pork fish salmon tuna turkey bacon sausage shrimp ham steak"),
    ("Bakery", "bread bagel bagels tortilla tortillas buns bun muffin croissant rolls"),
    ("Frozen", "frozen ice icecream popsicle"),
    ("Drinks", "water juice soda beer wine seltzer kombucha"),
    ("Pantry", "rice pasta flour sugar cereal oil sauce beans soup coffee tea spice salt "
               "peanut oats snacks chips crackers honey jam vinegar ketchup mustard mayo noodles "
               "broth stock nuts granola"),
    ("Filters", "filter filters"),
    ("Batteries & Bulbs", "battery batteries aa aaa 9v bulb bulbs led"),
    ("Plumbing", "pipe plunger valve faucet drain pex fitting teflon supply-line"),
    ("Electrical", "outlet switch wire breaker gfci extension cord plug"),
    ("Paint", "paint primer brush roller tray spackle"),
    ("Hardware", "screw screws nail nails bolt bolts anchor anchors hinge tape glue caulk "
                 "sandpaper wd-40 wd40 lubricant"),
    ("Garden", "mulch seed seeds fertilizer hose soil weed grass plant planter"),
    ("Safety", "smoke detector extinguisher co alarm gloves mask goggles"),
    ("Cleaning", "detergent soap bleach cleaner sponge sponges dish dishwasher pods wipes "
                 "disinfectant mop broom vacuum"),
    ("Household", "paper towel towels toilet tissue tissues trash bag bags foil wrap napkins "
                  "ziploc storage"),
    ("Personal Care", "shampoo conditioner toothpaste toothbrush deodorant razor lotion floss "
                      "sunscreen"),
]
CATEGORY_NAMES = [c for c, _ in CATEGORIES] + ["Other"]
# Most specific first: "water filter" -> Filters, "peanut butter" -> Pantry, "ice cream" -> Frozen,
# "apple juice" -> Drinks, "chicken soup" -> Pantry.
MATCH_ORDER = ["Filters", "Batteries & Bulbs", "Plumbing", "Electrical", "Paint", "Hardware",
               "Safety", "Garden", "Frozen", "Drinks", "Pantry", "Bakery", "Dairy & Eggs",
               "Meat & Seafood", "Produce", "Cleaning", "Household", "Personal Care"]
_WORDS = {c: set(words.split()) for c, words in CATEGORIES}
_KEYWORDS = [(c, _WORDS[c]) for c in MATCH_ORDER]

# "2 milk", "2x milk", "1.5 lb chicken", "3 gal water", "1/2 dozen eggs"
_QTY = re.compile(
    r"^\s*(?P<q>(\d+(?:[.,/]\d+)?)\s*(x|×)?\s*"
    r"(?:(lbs?|oz|kg|g|gal|gallons?|l|liters?|ml|pk|packs?|boxes|box|bags?|cans?|bottles?|dozen|doz|ct|rolls?)\b)?)"
    r"\s+(?P<name>.+)$", re.I)


def parse_quick(text: str) -> tuple[str, str | None]:
    """Split '2 gal milk' into ('milk', '2 gal')."""
    text = " ".join(text.split())
    m = _QTY.match(text)
    if m and m.group("name"):
        q = re.sub(r"\s*(x|×)\s*$", "", m.group("q").strip(), flags=re.I)
        return m.group("name").strip(), q or None
    return text, None


def guess_category(name: str, list_key: str = "groceries") -> str:
    words = set(re.findall(r"[a-z0-9\-]+", name.lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}
    for cat, kw in _KEYWORDS:
        if words & kw:
            return cat
    return "Other"
