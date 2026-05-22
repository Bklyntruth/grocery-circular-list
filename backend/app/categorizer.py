"""Keyword-based grocery item categorizer.

Used as a fallback when structural category data (e.g. Foodway section headers,
Flipp category fields) is unavailable.  First match wins — order matters.
"""

_RULES: list[tuple[str, list[str]]] = [
    ("Produce", [
        "apple", "banana", "orange", "grape", "berry", "berries", "cherry",
        "cherries", "melon", "watermelon", "corn", "lettuce", "tomato",
        "potato", "onion", "garlic", "avocado", "broccoli", "spinach",
        "kale", "cucumber", "pepper", "carrot", "celery", "mushroom",
        "salad", "blueberry", "strawberry", "lemon", "lime", "mango",
        "pineapple", "peach", "plum", "nectarine", "asparagus", "zucchini",
        "squash", "eggplant", "artichoke", "scallion", "radish", "leek",
        "cabbage", "cauliflower", "beet", "yam", "sweet potato",
        "california cherries", "fresh corn",
    ]),
    ("Seafood", [
        "shrimp", "salmon", "tuna", "lobster", "crab", "scallop", "clam",
        "oyster", "bass", "tilapia", "cod", "flounder", "swordfish", "mahi",
        "seafood", "fillet", "catfish", "trout", "halibut", "sardine",
        "anchovy", "calamari", "squid", "octopus", "mussel", "snapper",
        "grouper", "sole", "pollock", "herring",
    ]),
    ("Meat", [
        "beef", "chicken", "pork", "turkey", "lamb", "veal", "steak",
        "ground", "chop", "rib", "roast", "brisket", "ham", "sausage",
        "bacon", "hot dog", "burger", "cutlet", "breast", "thigh",
        "drumstick", "wing", "tenderloin", "loin", "sirloin", "flank",
        "chuck", "shoulder", "butt", "porterhouse", "t-bone", "strip steak",
        "perdue", "butterball", "bell & evans",
    ]),
    ("Dairy & Eggs", [
        "milk", "cheese", "yogurt", "butter", "cream", "egg", "eggs",
        "sour cream", "cottage cheese", "cheddar", "mozzarella", "parmesan",
        "swiss", "brie", "feta", "ricotta", "cream cheese", "half and half",
        "whipped cream",
    ]),
    ("Frozen", [
        "frozen", "ice cream", "sorbet", "gelato", "popsicle", "waffle",
        "tater tot", "french fries", "edamame", "nestle drumstick",
    ]),
    ("Bakery", [
        "bread", "cake", "bagel", "muffin", "pastry", "donut", "pie",
        "roll", "bun", "croissant", "tortilla", "pita", "english muffin",
        "biscuit",
    ]),
    ("Deli", [
        "deli", "cold cut", "salami", "prosciutto", "pastrami", "bologna",
        "mortadella", "pepperoni", "liverwurst",
    ]),
    ("Beverages", [
        "soda", "juice", "water", "cola", "pepsi", "sprite", "snapple",
        "gatorade", "energy drink", "sparkling", "lemonade", "iced tea",
        "kombucha", "cider", "coca-cola", "coca cola",
    ]),
    ("Snacks", [
        "chip", "chips", "cracker", "pretzel", "popcorn", "candy",
        "chocolate", "granola bar", "trail mix", "doritos", "ritz",
        "keebler", "frito", "lay snack", "cookie", "cookies",
    ]),
    ("Pantry", [
        "pasta", "rice", "cereal", "soup", "canned", "oil", "sauce",
        "vinegar", "spice", "flour", "sugar", "honey", "jam",
        "peanut butter", "ketchup", "mustard", "mayo", "salsa",
        "tomato sauce", "broth", "stock", "beans", "lentil",
    ]),
    ("Household", [
        "detergent", "paper towel", "toilet paper", "cleaning", "dish soap",
        "laundry", "bleach", "trash bag", "sponge", "foil", "plastic wrap",
    ]),
    ("Personal Care", [
        "shampoo", "soap", "toothpaste", "deodorant", "lotion",
        "sunscreen", "conditioner", "razor",
    ]),
]


def categorize(name: str) -> str:
    n = name.lower()
    for cat, keywords in _RULES:
        if any(kw in n for kw in keywords):
            return cat
    return "Other"
