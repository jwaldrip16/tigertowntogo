"""Built-in menus a dispatcher can load onto a restaurant with one click.

Popeyes comes from "Tiger Town to Go Popeyes.docx" (Oct 2026). Sides sold on
their own had no price in that document, so they load hidden at $0.00 until a
price is set in the menu editor.
"""

SIDES = ["Cajun Fries", "Cole Slaw", "Homestyle Mac & Cheese",
         "Mashed Potatoes With Cajun Gravy", "Red Beans & Rice"]
DRINKS = ["Coke", "Diet Coke", "Dr. Pepper", "Fanta Orange", "Fanta Strawberry",
          "Hawaiian Punch", "Minute Maid Lemonade", "Sprite", "Sweet Tea", "Unsweet Tea"]
EXTRA_SAUCES = ["Extra BBQ Sauce", "Extra Bayou Buffalo Sauce", "Extra Blackened Ranch Sauce",
                "Extra Cocktail Sauce", "Extra Ranch Sauce", "Extra Sweet Heat Sauce",
                "Extra Tartar Sauce"]


def _g(name, opts, mn=1, mx=1, each=1, price=0):
    return {"name": name, "min": mn, "max": mx, "each": each,
            "options": [(o, price) for o in opts]}


SAUCE_G = _g("Would you like any extra sauces?", EXTRA_SAUCES, 0, 21, 3, 30)
SIDE_G = _g("What side would you like?", SIDES)
DRINK_G = _g("What drink would you like?", DRINKS)
STYLE_G = _g("Classic or spicy sandwiches?", ["Classic", "Spicy"])


def large_sides(n):
    label = "Choose your large side" if n == 1 else "Choose your %d large sides" % n
    return _g(label, SIDES, n, n, n)


_BUN = ("A juicy chicken breast fillet marinated in Popeyes seasonings, hand battered and breaded "
        "in our buttermilk system, fried until golden brown. Sandwiched between two buttery toasted "
        "brioche buns, topped with our barrel cured pickle slices")
_BUN_COMBO = ("A juicy chicken breast fillet marinated in Popeyes seasonings, hand battered and "
              "breaded, fried until golden brown. Sandwiched between two buttery toasted brioche buns, "
              "topped with brined pickle slices")
_COMBO_TAIL = " Includes a regular signature side and drink of your choice."
_SIG = ("%s of our juicy signature chicken, marinated for 12hrs in our traditional savory Louisiana "
        "herbs and seasonings then battered up with our crunchy southern coating and fried until "
        "golden brown.")
_SIG_COMBO = ("%s pieces of our signature chicken, marinated for 12hrs in our traditional savory "
              "Louisiana herbs and seasonings then battered up with our crunchy southern coating and "
              "fried until golden brown. Includes a regular signature side, warm buttermilk biscuit, "
              "and drink of your choice.")
_TENDERS = ("%s pieces of our famous chicken tenders marinated in mouthwatering Louisiana herbs and "
            "seasonings then hand battered and breaded in our crunchy southern coating. Fried until "
            "golden brown.")
_OREO = ("A thick and rich cheesecake filling mixed with Oreos pieces, on an Oreo Cookie crust "
         "topped with Oreo crumbles.")
_CCB = "A sweet and salty biscuit filled with chocolate topped with icing"

COMBO_GROUPS = [SIDE_G, DRINK_G, SAUCE_G]

POPEYES = [
    ("Chicken Sandwiches", [
        ("Classic Chicken Sandwich", 583, _BUN + " and classic mayo.", [SAUCE_G]),
        ("Spicy Chicken Sandwich", 583, _BUN + " and spicy mayo.", [SAUCE_G]),
        ("Classic Bacon & Cheese Chicken Sandwich", 733,
         _BUN + ", classic mayo, havarti cheese and bacon.", [SAUCE_G]),
        ("Spicy Bacon & Cheese Chicken Sandwich", 733,
         _BUN + ", spicy mayo, havarti cheese and bacon.", [SAUCE_G]),
        ("Classic Chicken Sandwich Combo", 1168, _BUN_COMBO + " and classic mayo." + _COMBO_TAIL,
         COMBO_GROUPS),
        ("Spicy Chicken Sandwich Combo", 1168, _BUN_COMBO + " and spicy mayo." + _COMBO_TAIL,
         COMBO_GROUPS),
        ("Classic Bacon & Cheese Chicken Sandwich Combo", 1318,
         _BUN + ", classic mayo, havarti cheese and bacon." + _COMBO_TAIL.replace(" and drink", " and drink"),
         COMBO_GROUPS),
        ("Spicy Bacon & Cheese Chicken Sandwich Combo", 1318,
         _BUN + ", spicy mayo, havarti cheese and bacon." + _COMBO_TAIL, COMBO_GROUPS),
    ]),
    ("Individual Items", [
        ("8Pc Signature Chicken", 2208, _SIG % "8 pieces", []),
        ("12Pc Signature Chicken", 3248, _SIG % "12 pieces", []),
        ("16Pc Signature Chicken", 4158, _SIG % "16 pieces", []),
    ]),
    ("Family Meals", [
        ("8Pc Handcrafted Tenders Family Meal", 2159, (_TENDERS % "8") +
         " Includes dipping sauce, a large signature side and four warm buttermilk biscuits.",
         [large_sides(1)]),
        ("12Pc Handcrafted Tenders Family Meal", 2644, (_TENDERS % "12") +
         " Includes dipping sauce, two large signature sides and six warm buttermilk biscuits.",
         [large_sides(2)]),
        ("Big Sandwich Bundle Family Meal", 2750,
         "4 hand battered and breaded chicken sandwiches (classic or spicy) topped with crisp pickle "
         "slices and mayo accompanied with 4 pieces of our famous chicken tenders and a large "
         "signature side.", [STYLE_G, large_sides(1)]),
        ("8Pc Signature Chicken Family Meal", 2988, (_SIG % "8 pieces") +
         " Includes one large signature side and four warm buttermilk biscuits.", [large_sides(1)]),
        ("Family Feast", 3599, (_SIG % "6 pieces") +
         " 2 hand battered and breaded chicken sandwiches (classic or spicy) topped with crisp pickle "
         "slices and mayo, 2 large signature sides and 4 warm buttermilk biscuits.",
         [STYLE_G, large_sides(2)]),
        ("12Pc Signature Chicken Family Meal", 4028, (_SIG % "12 pieces") +
         " Includes two large signature sides and six warm buttermilk biscuits.", [large_sides(2)]),
        ("16Pc Signature Chicken Family Meal", 5588, (_SIG % "16 pieces") +
         " Includes three large signature sides and eight warm buttermilk biscuits.", [large_sides(3)]),
    ]),
    ("Combos", [
        ("Quarter Pound Popcorn Shrimp Combo", 1077,
         "Quarter Pound bite-sized shrimps seasoned to perfection. Served with cocktail sauce, a "
         "regular signature side, one biscuit, and a drink of your choice. *Weight based on "
         "pre-cooked shrimp weight.", COMBO_GROUPS),
        ("2Pc Signature Chicken Combo", 1168, _SIG_COMBO % "2", COMBO_GROUPS),
        ("3Pc Handcrafted Tenders Combo", 1168, (_TENDERS % "3") +
         " Includes dipping sauce, a regular signature side, warm buttermilk biscuit, and drink of "
         "your choice.", COMBO_GROUPS),
        ("3Pc Signature Chicken Combo", 1298, _SIG_COMBO % "3", COMBO_GROUPS),
        ("4Pc Signature Chicken Combo", 1428, _SIG_COMBO % "4", COMBO_GROUPS),
        ("5Pc Handcrafted Tenders Combo", 1428, (_TENDERS % "5") +
         " Includes dipping sauce, a regular signature side, warm buttermilk biscuit, and drink of "
         "your choice.", COMBO_GROUPS),
    ]),
    ("Signature Sides", [
        ("Red Beans & Rice", None, "", []),
        ("Cole Slaw", None, "", []),
        ("Mashed Potatoes With Cajun Gravy", None, "", []),
        ("Homestyle Mac & Cheese", None, "", []),
        ("Biscuit (1)", 93, "", []),
    ]),
    ("Sauces", [
        ("Tartar Sauce", 25, "", []),
        ("Sweet & Spicy Wings Sauce", 25, "", []),
        ("Wild Honey Mustard", 25, "A \u201ckicked up\u201d version of honey mustard", []),
        ("Bayou Buffalo Sauce", 25, "", []),
        ("Blackened Ranch Sauce", 25, "", []),
        ("Bold BQ Sauce", 25, "", []),
        ("Buttermilk Ranch Sauce", 25, "", []),
        ("Mardi Gras Mustard", 25, "Sweetened traditional creole mustard dipping sauce", []),
        ("Sweet Heat Sauce", 25, "", []),
        ("Cocktail Sauce", 25, "", []),
    ]),
    ("Drinks", [
        ("Small Drink", 229, "", [DRINK_G]),
        ("Medium Drink", 279, "", [DRINK_G]),
        ("Large Drink", 329, "", [DRINK_G]),
    ]),
    ("Desserts", [
        ("Cinnamon Apple Pie", 249,
         "Warm, crispy crust on the outside, hot cinnamon apple goodness on the inside.", []),
        ("Oreo Cheesecake Cup", 449, _OREO, []),
        ("2Pc Oreo Cheesecake Cup", 829, _OREO, []),
        ("Chocolate Chip Biscuit", 219, _CCB, []),
        ("2pc Chocolate Chip Biscuit", 359, _CCB, []),
        ("4pc Chocolate Chip Biscuit", 649, _CCB, []),
    ]),
]

PRESETS = {"popeyes": ("Popeyes menu", POPEYES)}


def available_for(slug, name):
    s = ((slug or "") + " " + (name or "")).lower()
    return [{"key": "popeyes", "label": PRESETS["popeyes"][0]}] if "popeye" in s else []


def count(key):
    return sum(len(items) for _, items in PRESETS[key][1])


def load(con, rid, key, replace=False, on_drop=None):
    """Write a preset menu onto a restaurant. Returns how many items were added."""
    label, sections = PRESETS[key]
    if replace:
        for it in con.execute("SELECT id, image FROM menu_items WHERE restaurant_id=?", (rid,)).fetchall():
            for gr in con.execute("SELECT id FROM option_groups WHERE item_id=?", (it[0],)).fetchall():
                con.execute("DELETE FROM options WHERE group_id=?", (gr[0],))
            con.execute("DELETE FROM option_groups WHERE item_id=?", (it[0],))
            if on_drop and it[1]:
                on_drop(it[1])
        con.execute("DELETE FROM menu_items WHERE restaurant_id=?", (rid,))
    sort = con.execute("SELECT COALESCE(MAX(sort),0) FROM menu_items WHERE restaurant_id=?",
                       (rid,)).fetchone()[0]
    added = 0
    for section, items in sections:
        sort += 1
        for name, price, desc, groups in items:
            cur = con.execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents,
                                 active,section,sort) VALUES(?,?,?,?,?,?,?)""",
                              (rid, name, desc, price or 0, 0 if price is None else 1, section, sort))
            iid = cur.lastrowid
            for gi, gr in enumerate(groups):
                gcur = con.execute("""INSERT INTO option_groups(item_id,name,min_select,max_select,
                                      sort,max_each) VALUES(?,?,?,?,?,?)""",
                                   (iid, gr["name"], gr["min"], gr["max"], gi + 1, gr["each"]))
                for oi, (oname, oprice) in enumerate(gr["options"]):
                    con.execute("""INSERT INTO options(group_id,name,price_delta_cents,sort)
                                   VALUES(?,?,?,?)""", (gcur.lastrowid, oname, oprice, oi + 1))
            added += 1
    return added


def autoload(con):
    """First start after this update: give Popeyes its menu if it has none yet."""
    if con.execute("SELECT 1 FROM settings WHERE key='popeyes_menu_v1'").fetchone():
        return
    r = con.execute("SELECT id FROM restaurants WHERE slug='popeyeschicken'").fetchone()
    if r and not con.execute("SELECT 1 FROM menu_items WHERE restaurant_id=?", (r[0],)).fetchone():
        load(con, r[0], "popeyes")
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('popeyes_menu_v1','1')")
