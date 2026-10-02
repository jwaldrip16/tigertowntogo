
import base64, difflib, os, json, math, re, secrets, sqlite3, threading, time, datetime as dt, urllib.parse, urllib.request
import dbx
from flask import Flask, g, request, session, redirect, url_for, render_template, jsonify

# ---------------------------------------------------------------- local time
# Hosts like Railway and Render run on UTC. The whole app (order times, shop hours,
# driver schedules, the SQLite 'localtime' stamps) runs on Central time instead.
# Set APP_TZ to change it. If the host has no time zone files, fall back to the
# built-in Central rule so daylight saving still switches on its own.
POSIX_TZ = {"America/Chicago": "CST6CDT,M3.2.0,M11.1.0",
            "America/New_York": "EST5EDT,M3.2.0,M11.1.0"}

def _pick_tz():
    env_tz = (os.environ.get("TZ") or "").strip()
    if env_tz.upper() in ("", "UTC", "ETC/UTC", "GMT", ":/ETC/LOCALTIME"):
        env_tz = ""
    return (os.environ.get("APP_TZ") or env_tz or "America/Chicago").strip()

def set_app_tz(name):
    if "/" in name and not os.path.exists(os.path.join("/usr/share/zoneinfo", name)):
        name = POSIX_TZ.get(name, name)
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    return name

APP_TZ = _pick_tz()
set_app_tz(APP_TZ)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "delivery.db"))
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

app = Flask(__name__)
# Railway/Render sit in front of the app and talk to it over plain http. Trust their
# forwarded headers so links we build use https.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS restaurants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, pin TEXT NOT NULL,
  address TEXT NOT NULL, phone TEXT, lat REAL, lng REAL,
  hours TEXT NOT NULL, closed_override INTEGER NOT NULL DEFAULT 0,
  open_24 INTEGER NOT NULL DEFAULT 0,
  prep_default INTEGER NOT NULL DEFAULT 15);

CREATE TABLE IF NOT EXISTS menu_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  restaurant_id INTEGER NOT NULL, name TEXT NOT NULL,
  description TEXT, price_cents INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS option_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL, name TEXT NOT NULL,
  min_select INTEGER NOT NULL DEFAULT 1, max_select INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS options (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL, name TEXT NOT NULL,
  price_delta_cents INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS drivers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, pin TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'offline',
  pending_request TEXT,
  max_stack INTEGER NOT NULL DEFAULT 3,
  roster TEXT NOT NULL DEFAULT 'scheduled',
  last_seen TEXT);

CREATE TABLE IF NOT EXISTS availability (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id INTEGER NOT NULL,
  dow INTEGER NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  note TEXT);

CREATE TABLE IF NOT EXISTS time_off (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT,
  decided_at TEXT,
  reply TEXT,
  created_at TEXT);

CREATE TABLE IF NOT EXISTS week_submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id INTEGER NOT NULL,
  week_start TEXT NOT NULL,
  submitted_at TEXT,
  note TEXT,
  UNIQUE(driver_id, week_start));

CREATE TABLE IF NOT EXISTS closures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  restaurant_id INTEGER,
  day TEXT NOT NULL,
  reason TEXT,
  UNIQUE(restaurant_id, day));

CREATE TABLE IF NOT EXISTS broadcasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  audience TEXT NOT NULL, body TEXT NOT NULL,
  sent_to INTEGER NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS dispatchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
  created_at TEXT);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  restaurant_id INTEGER NOT NULL,
  customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL,
  address TEXT NOT NULL, address_note TEXT, dispatch_note TEXT, lat REAL, lng REAL,
  items TEXT NOT NULL,
  subtotal_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL,
  tax_cents INTEGER NOT NULL DEFAULT 0, tip_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL,
  miles REAL NOT NULL DEFAULT 0,
  kitchen_status TEXT NOT NULL DEFAULT 'pending',   -- pending|preparing|ready
  dispatch_status TEXT NOT NULL DEFAULT 'held',     -- held|queued|assigned|picked_up|delivered|cancelled
  hold_reason TEXT,
  driver_id INTEGER, stack_seq INTEGER,
  prep_minutes INTEGER, prep_started TEXT, ready_at TEXT,
  placed_by TEXT NOT NULL DEFAULT 'customer',
  delivered_at TEXT,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id INTEGER NOT NULL, sender TEXT NOT NULL,
  body TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rest_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  restaurant_id INTEGER NOT NULL, sender TEXT NOT NULL, who TEXT,
  body TEXT NOT NULL, created_at TEXT NOT NULL,
  seen_by_dispatch INTEGER DEFAULT 0, seen_by_rest INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_rest_messages ON rest_messages(restaurant_id, id);

CREATE TABLE IF NOT EXISTS card_vault (
  order_id INTEGER PRIMARY KEY, blob TEXT NOT NULL, brand TEXT, last4 TEXT,
  created_at TEXT, viewed_at TEXT, viewed_by TEXT);

CREATE TABLE IF NOT EXISTS call_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  who TEXT NOT NULL,                -- driver | restaurant
  driver_id INTEGER, restaurant_id INTEGER, order_id INTEGER,
  name TEXT NOT NULL, phone TEXT, note TEXT,
  created_at TEXT NOT NULL, cleared_at TEXT);

CREATE TABLE IF NOT EXISTS geocache (
  q TEXT PRIMARY KEY, formatted TEXT, lat REAL, lng REAL, ok INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, detail TEXT, created_at TEXT);
"""

DEFAULT_HOURS = {str(i): ["11:00", "21:00"] for i in range(7)}

def db():
    if "db" not in g:
        g.db = dbx.connect(DB_PATH)
        dbx.tune(g.db)
    return g.db

@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()

def now():
    return dt.datetime.now().isoformat(timespec="seconds")

def log(kind, detail):
    db().execute("INSERT INTO events(kind,detail,created_at) VALUES(?,?,?)", (kind, detail, now()))

def ensure_column(con, table, col, decl):
    cols = dbx.columns(con, table)
    if col not in cols:
        con.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + decl)

def init_db():
    con = dbx.connect(DB_PATH)
    con.executescript(SCHEMA)
    cur = con.execute("SELECT COUNT(*) c FROM restaurants")
    if cur.fetchone()["c"] == 0:
        seed(con)
    topup_restaurants(con)
    for k, v in [("base_fee_cents", "399"), ("base_miles", "3"), ("per_mile_cents", "100"),
                 ("tax_rate_bp", "900"), ("service_fee_bp", "0"), ("business_open", "0"), ("future_lead_min", "45"), ("kitchen_accept_min", "5"), ("driver_accept_min", "3"), ("late_sound_after_min", "3"), ("driver_done_cleared_at", ""), ("auto_assign", "1"), ("max_stack_default", "3"),
                 ("assign_on_pending", "0"), ("week_open_dow", "4"), ("week_open_date", ""), ("one_run_at_a_time", "0"),
                 ("tip_prompt", "1"), ("dispatch_phone", "3342092844"),
                 ("business_name", "Fleet Delivery"),
                 ("business_address", "216 S 8th St, Opelika, AL 36801"),
                 ("unlimited_stack", "1"),
                 ("order_tokens", "Online,App,Phone call,Third party"),
                 ]:
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.execute("UPDATE settings SET value='0' WHERE key='assign_on_pending'")
    # Business now starts Closed. Existing databases are closed once, then dispatch opens it.
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('order_keep_days','0')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('order_purge_last','')")
    if not con.execute("SELECT 1 FROM settings WHERE key='biz_default_closed_v1'").fetchone():
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_open','0')")
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('biz_default_closed_v1','1')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('unlimited_stack','1')")
    con.execute("UPDATE drivers SET max_stack=999 WHERE max_stack IS NULL OR max_stack < 999")
    con.execute("""CREATE TABLE IF NOT EXISTS blocked_customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, name TEXT, reason TEXT,
        created_at TEXT)""")
    ensure_column(con, "drivers", "roster_day", "TEXT")
    ensure_column(con, "drivers", "online_since", "TEXT")
    ensure_column(con, "drivers", "last_assigned_at", "TEXT")
    ensure_column(con, "drivers", "last_completed_at", "TEXT")
    ensure_column(con, "drivers", "roster", "TEXT NOT NULL DEFAULT 'scheduled'")
    ensure_column(con, "availability", "status", "TEXT NOT NULL DEFAULT 'approved'")
    ensure_column(con, "availability", "week_start", "TEXT")
    _ws = (dt.date.today() - dt.timedelta(days=dt.date.today().weekday())).isoformat()
    con.execute("UPDATE availability SET week_start=? WHERE week_start IS NULL OR week_start=''", (_ws,))
    ensure_column(con, "card_vault", "paid_at", "TEXT")
    ensure_column(con, "week_submissions", "opened_at", "TEXT")
    ensure_column(con, "week_submissions", "locked_at", "TEXT")
    ensure_column(con, "availability", "created_at", "TEXT")
    ensure_column(con, "availability", "decided_by", "TEXT")
    ensure_column(con, "availability", "decided_at", "TEXT")
    ensure_column(con, "availability", "reply", "TEXT")
    ensure_column(con, "orders", "issue", "TEXT")
    ensure_column(con, "orders", "issue_note", "TEXT")
    ensure_column(con, "orders", "cloned_from", "TEXT")
    ensure_column(con, "orders", "item_fee_cents", "INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE drivers SET roster='scheduled' WHERE roster NOT IN ('scheduled','unavailable')")
    ensure_column(con, "orders", "dispatch_note", "TEXT")
    ensure_column(con, "orders", "address_ok", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(con, "orders", "source", "TEXT NOT NULL DEFAULT 'website'")
    ensure_column(con, "menu_items", "section", "TEXT")
    # a pickup a dispatcher typed in, for a restaurant that is not on our list
    ensure_column(con, "orders", "pickup_name", "TEXT")
    ensure_column(con, "orders", "pickup_address", "TEXT")
    ensure_column(con, "orders", "pickup_phone", "TEXT")
    ensure_column(con, "orders", "pickup_lat", "REAL")
    ensure_column(con, "orders", "pickup_lng", "REAL")
    # one hidden row so every existing restaurant lookup still finds something
    row = con.execute("SELECT id FROM restaurants WHERE slug='oneoff'").fetchone()
    if not row:
        con.execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,
                        closed_override,open_24,prep_default)
                        VALUES('Typed-in pickup','oneoff',?,'','',NULL,NULL,?,0,1,15)""",
                    (secrets.token_hex(8), json.dumps(DEFAULT_HOURS)))
    con.executescript("""
    CREATE TABLE IF NOT EXISTS status_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_status_log_order ON status_log(order_id, id);
    """)
    if not dbx.PG:
        con.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_order_open AFTER INSERT ON orders BEGIN
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'order','placed',COALESCE(new.created_at,datetime('now','localtime')));
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'kitchen',new.kitchen_status,COALESCE(new.created_at,datetime('now','localtime')));
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'dispatch',new.dispatch_status,COALESCE(new.created_at,datetime('now','localtime')));
    END;

    CREATE TRIGGER IF NOT EXISTS trg_kitchen_status AFTER UPDATE OF kitchen_status ON orders
    WHEN old.kitchen_status <> new.kitchen_status BEGIN
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'kitchen',new.kitchen_status,datetime('now','localtime'));
    END;

    CREATE TRIGGER IF NOT EXISTS trg_dispatch_status AFTER UPDATE OF dispatch_status ON orders
    WHEN old.dispatch_status <> new.dispatch_status BEGIN
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'dispatch',new.dispatch_status,datetime('now','localtime'));
    END;

    CREATE TRIGGER IF NOT EXISTS trg_payment_status AFTER UPDATE OF payment_status ON orders
    WHEN old.payment_status <> new.payment_status BEGIN
      INSERT INTO status_log(order_id,kind,status,at)
        VALUES(new.id,'payment',new.payment_status,datetime('now','localtime'));
    END;
    """)
    ensure_column(con, "messages", "seen_by_dispatch", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "menu_items", "sort", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "payment_status", "TEXT NOT NULL DEFAULT 'unpaid'")
    ensure_column(con, "orders", "pay_method", "TEXT")
    ensure_column(con, "orders", "paid_at", "TEXT")
    ensure_column(con, "orders", "pay_link", "TEXT")
    ensure_column(con, "orders", "pay_ref", "TEXT")
    ensure_column(con, "orders", "stripe_session", "TEXT")
    ensure_column(con, "orders", "stripe_intent", "TEXT")
    ensure_column(con, "orders", "stripe_pm", "TEXT")
    ensure_column(con, "orders", "stripe_customer", "TEXT")
    ensure_column(con, "orders", "paid_cents", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "extra_charges", "TEXT")
    ensure_column(con, "orders", "refunded_cents", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "refund_id", "TEXT")
    ensure_column(con, "orders", "refund_note", "TEXT")
    ensure_column(con, "orders", "tip_sig", "TEXT")
    ensure_column(con, "orders", "tip_signed_at", "TEXT")
    ensure_column(con, "orders", "tip_charge_id", "TEXT")
    ensure_column(con, "orders", "tip_declined", "INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE orders SET payment_status='unpaid' "
                "WHERE payment_status IS NULL OR payment_status=''")
    con.execute("UPDATE orders SET refunded_cents=0 WHERE refunded_cents IS NULL")
    ensure_column(con, "dispatchers", "created_at", "TEXT")
    ensure_column(con, "messages", "dispatcher_id", "INTEGER")
    ensure_column(con, "messages", "sender_name", "TEXT")
    ensure_column(con, "drivers", "last_lat", "REAL")
    ensure_column(con, "drivers", "last_lng", "REAL")
    ensure_column(con, "drivers", "last_loc_at", "TEXT")
    ensure_column(con, "orders", "service_cents", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "drivers", "last_addr", "TEXT")
    ensure_column(con, "drivers", "last_addr_lat", "REAL")
    ensure_column(con, "drivers", "last_addr_lng", "REAL")
    con.execute("CREATE TABLE IF NOT EXISTS revgeo (k TEXT PRIMARY KEY, address TEXT, created_at TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   driver_id INTEGER NOT NULL, lat REAL, lng REAL, address TEXT, status TEXT,
                   event TEXT, created_at TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_driver_log ON driver_log(driver_id, created_at)")
    ensure_column(con, "orders", "ref_code", "TEXT")
    ensure_column(con, "orders", "paged_at", "TEXT")
    ensure_column(con, "orders", "kitchen_sent_at", "TEXT")
    ensure_column(con, "orders", "driver_paged_at", "TEXT")
    # Stamp when the kitchen got the ticket and when a driver got paged, whatever code path did it.
    _ts = "strftime('%Y-%m-%dT%H:%M:%S','now','localtime')"
    con.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_kitchen_sent_ins AFTER INSERT ON orders
      WHEN NEW.kitchen_status='pending'
      BEGIN UPDATE orders SET kitchen_sent_at=""" + _ts + """ WHERE id=NEW.id; END;
    CREATE TRIGGER IF NOT EXISTS trg_kitchen_sent_upd AFTER UPDATE OF kitchen_status ON orders
      WHEN NEW.kitchen_status='pending' AND OLD.kitchen_status IS NOT 'pending'
      BEGIN UPDATE orders SET kitchen_sent_at=""" + _ts + """ WHERE id=NEW.id; END;
    CREATE TRIGGER IF NOT EXISTS trg_driver_paged_upd AFTER UPDATE OF dispatch_status, driver_id, paged_at ON orders
      WHEN NEW.dispatch_status='assigned' AND (OLD.dispatch_status IS NOT 'assigned'
           OR NEW.driver_id IS NOT OLD.driver_id OR NEW.paged_at IS NOT OLD.paged_at)
      BEGIN UPDATE orders SET driver_paged_at=""" + _ts + """ WHERE id=NEW.id; END;
    """)
    con.execute("""UPDATE orders SET kitchen_sent_at=created_at WHERE kitchen_sent_at IS NULL
                   AND kitchen_status='pending'""")
    con.execute("""UPDATE orders SET driver_paged_at=COALESCE(paged_at, created_at) WHERE driver_paged_at IS NULL
                   AND dispatch_status='assigned'""")
    ensure_column(con, "orders", "scheduled_for", "TEXT")
    ensure_column(con, "orders", "release_at", "TEXT")
    ensure_column(con, "orders", "sched_kitchen", "TEXT")
    ensure_column(con, "orders", "sched_dispatch", "TEXT")
    ensure_column(con, "orders", "sched_hold", "TEXT")
    ensure_column(con, "orders", "redo_driver_id", "INTEGER")
    ensure_column(con, "orders", "token", "TEXT")
    ensure_column(con, "restaurants", "cuisine", "TEXT")
    con.execute("UPDATE drivers SET roster='scheduled' WHERE roster IS NULL OR roster=''")
    con.commit()
    # The status history triggers, installed once the
    # late-added columns above exist
    dbx.install_triggers(con)
    con.close()


# The Fleet Delivery store list. Blank address means dispatch has to fill it in
# under Dispatch > Manage before that store can take an order.
TTG_LIST = [
    ('Popeyes Chicken', 'popeyeschicken', 'American, Sandwiches', '1999 Opelika Road, Auburn, AL 36830', '', 32.628878, -85.4396338),
    ('Savanh Thai Kitchen', 'savanhthaikitchen', 'Thai', '', '', None, None),
    ("Zaxby's East University", 'zaxbyseastuniversity', 'American', '', '', None, None),
    ("Bruster's Real Ice Cream", 'brustersrealicecream', 'Ice Cream, Desserts, Shakes', '2172 East University Drive, Auburn, AL 36830', '+1-334-821-9988', 32.6291833, -85.4532784),
    ('La Morenita Taqueria and Bar', 'lamorenitataqueriaandbar', 'Mexican', '', '', None, None),
    ('Mikata Japanese Steak House', 'mikatajapanesesteakhouse', 'Japanese, Sushi', '', '', None, None),
    ('El Jefe Mexican Cuisine', 'eljefemexicancuisine', 'Mexican', '', '', None, None),
    ("Moe's Original BBQ Bent Creek", 'moesoriginalbbqbentcreek', 'Bar-B-Q', '2319 Bent Creek Road, Auburn, AL 36830', '+1 334-329-7049', 32.6076, -85.429277),
    ('Sushiya Japanese Restaurant', 'sushiyajapaneserestauran', 'Japanese, Sushi', '', '', None, None),
    ('Umami', 'umami', 'Sushi, Asian, Korean', '', '', None, None),
    ("Baumhower's Victory Grille", 'baumhowersvictorygrille', 'American, Wings, Burgers', '2353 Bent Creek Road, Auburn, AL 36830', '+1 334-246-4180', 32.6071464, -85.4285586),
    ('Niffers Place', 'niffersplace', 'Chinese, Sandwiches, Burgers', '1151 Opelika Road, Auburn, AL 36830', '', 32.6211754, -85.45732),
    ('Agave Loco Mexican Grill', 'agavelocomexicangrill', 'Mexican', '1409 South College Street, Auburn, AL 36832', '+1-334-501-9197', 32.580376, -85.4949338),
    ('Bow & Arrow', 'bowarrow', 'BBQ', '1977 East Samford Avenue, Auburn, AL 36830', '', 32.6039061, -85.4411248),
    ("Country's Barbecue", 'countrysbarbecue', 'Bar-B-Q, BBQ', '', '', None, None),
    ('TCBY', 'tcby', 'Smoothies, Desserts, Frozen Yogurt', '300 North Dean Road, Auburn, AL 36830', '', 32.6112328, -85.463778),
    ('New China', 'newchina', 'Chinese', '1515 2nd Avenue, Opelika, AL 36801', '', 32.6423414, -85.3906454),
    ('Pho Lee', 'pholee', 'Vietnamese, Asian Fusion', '756 East Glenn Avenue, Auburn, AL 36830', '', 32.6083916, -85.4658884),
    ("Rocco's Chicken Joint", 'roccoschickenjoint', 'American, Sandwiches, Wings', '2415 Moores Mill Road, Auburn, AL 36830', '+1-334-209-0957', 32.5856806, -85.4384309),
    ("Don Julio's Mexican Restaurant", 'donjuliosmexicanrestaura', 'Mexican', '2356 Moores Mill Road, Auburn, AL 36830', '', 32.5842195, -85.437528),
    ('China Garden', 'chinagarden', 'Chinese', '1888 Ogletree Road, Auburn, AL 36830', '', 32.5839303, -85.4382585),
    ('Sushi Bistro', 'sushibistro', 'Japanese, Sushi', '1888 Ogletree Road, Auburn, AL 36830', '', 32.5836418, -85.4382564),
    ('The Depot', 'thedepot', 'American, Steakhouse', '124 Mitcham Avenue, Auburn, AL 36830', '', 32.6102094, -85.4807659),
    ("Hamilton's on Ogletree Lunch", 'hamiltonsonogletreelunch', 'American', '174 East Magnolia Avenue, Auburn, AL 36830', '+13348872677', 32.6063791, -85.4800379),
    ("Hamilton's on Ogletree Dinner", 'hamiltonsonogletreedinne', 'American', '174 East Magnolia Avenue, Auburn, AL 36830', '+13348872677', 32.6063791, -85.4800379),
    ('The Hound', 'thehound', 'New American', '124 Tichenor Avenue, Auburn, AL 36830', '', 32.6078875, -85.4809687),
    ("Hamilton's on Magnolia Lunch", 'hamiltonsonmagnolialunch', 'American', '174 East Magnolia Avenue, Auburn, AL 36830', '+13348872677', 32.6063791, -85.4800379),
    ('One Forty Grill', 'onefortygrill', 'American, New American', '140 North College Street, Auburn, AL 36830', '', 32.6078048, -85.481517),
    ("Hamilton's on Magnolia Dinner", 'hamiltonsonmagnoliadinne', 'American', '174 East Magnolia Avenue, Auburn, AL 36830', '+13348872677', 32.6063791, -85.4800379),
    ('Mellow Mushroom', 'mellowmushroom', 'Italian, Pizza', '128 North College Street, Auburn, AL 36830', '', 32.6073586, -85.4815314),
    ("Moe's Original BBQ", 'moesoriginalbbq', 'BBQ', '2319 Bent Creek Road, Auburn, AL 36830', '+1 334-329-7049', 32.6076, -85.429277),
    ('Little Italy', 'littleitaly', 'Pizza', '129 East Magnolia Avenue, AL', '+13348216161', 32.6068275, -85.4809336),
    ("Moe's Original BBQ Downtown", 'moesoriginalbbqdowntown', 'BBQ', '2319 Bent Creek Road, Auburn, AL 36830', '+1 334-329-7049', 32.6076, -85.429277),
    ('Tekila Mexican Bar & Grill', 'tekilamexicanbargrill', 'Mexican', '', '', None, None),
    ('Ariccia Cucina Italiana Dinner', 'aricciacucinaitalianadin', 'Italian, American', '', '', None, None),
    ('Umika Sushi', 'umikasushi', 'Japanese, Sushi', '', '', None, None),
    ("Taziki's Mediterranean Cafe", 'tazikismediterraneancafe', 'Greek, Mediterranean', '339 South College Street, Auburn, AL 36830', '', 32.5996554, -85.4815956),
    ('Amsterdam Cafe Lunch', 'amsterdamcafelunch', 'Salads, Sandwiches, Gourmet', '410 South Gay Street, Auburn, AL 36830', '', 32.5982217, -85.480157),
    ('Beyond The Wok', 'beyondthewok', 'Asian', '', '', None, None),
    ('Amsterdam Cafe Dinner', 'amsterdamcafedinner', 'Salads, Sandwiches, Gourmet', '410 South Gay Street, Auburn, AL 36830', '', 32.5982217, -85.480157),
    ('Amsterdam Cafe Brunch', 'amsterdamcafebrunch', 'Salads, Sandwiches, Gourmet', '410 South Gay Street, Auburn, AL 36830', '', 32.5982217, -85.480157),
    ("Proud Willie's Wings and Stuff", 'proudwillieswingsandstuf', 'American, Sandwiches, Wings', '', '', None, None),
    ('Insomnia Steak & Grill', 'insomniasteakgrill', 'American, Sandwiches, Grill', '', '', None, None),
    ("Salsarita's", 'salsaritas', 'Mexican', '1111 South College Street, Auburn, AL 36832', '+1 334-209-2255', 32.583561, -85.4902775),
    ("Big Mike's Steakhouse", 'bigmikessteakhouse', 'Steaks', '', '', None, None),
    ("Acapulco's Mexican Grill", 'acapulcosmexicangrill', 'Mexican', '1409 South College Street, Auburn, AL 36832', '+1-334-501-9197', 32.580376, -85.4949338),
    ('Savanh Thai Takeout', 'savanhthaitakeout', 'Thai', '', '', None, None),
    ('Panda', 'panda', 'Chinese', '', '', None, None),
    ('Taqueria La Plaza', 'taquerialaplaza', 'Mexican', '', '', None, None),
    ("Zaxby's South College", 'zaxbyssouthcollege', 'American', '', '', None, None),
    ('El Dorado', 'eldorado', 'Mexican', '1658 South College Street, Auburn, AL 36832', '', 32.5746852, -85.4995614),
    ("Momma Goldberg's Deli Longleaf", 'mommagoldbergsdelilongle', 'American, Sandwiches, Deli', '2701 Frederick Road, Opelika, AL 36801', '', 32.6203097, -85.4158587),
    ("Jim 'N Nick's BBQ", 'jimnnicksbbq', 'BBQ', '', '', None, None),
    ('Yummi Crab', 'yummicrab', 'Seafood, Cajun, Creole', '', '', None, None),
]


def topup_restaurants(con):
    """Add any Fleet Delivery store that is not on file yet. Runs once, never
    overwrites a store a dispatcher has already edited, and never re-adds a deleted one."""
    ensure_column(con, "restaurants", "cuisine", "TEXT")
    # renamed brand: the house store keeps its sign-in code, only the name changes
    con.execute("UPDATE restaurants SET name='Fleet Delivery' WHERE slug='tigertowntogo' AND name=?",
                ("Tiger Town " + "To Go",))
    done = con.execute("SELECT value FROM settings WHERE key='store_list_loaded'").fetchone()
    if done and done["value"] == "1":
        return
    def key(n):
        return "".join(ch for ch in n.lower() if ch.isalnum())
    have = {key(r["name"]): r["id"] for r in con.execute("SELECT id, name FROM restaurants").fetchall()}
    slugs = {r["slug"] for r in con.execute("SELECT slug FROM restaurants").fetchall()}
    hours = json.dumps(DEFAULT_HOURS)
    added = 0
    for name, slug, cuisine, addr, phone, lat, lng in TTG_LIST:
        if key(name) in have:
            con.execute("UPDATE restaurants SET cuisine=COALESCE(NULLIF(cuisine,''),?) WHERE id=?",
                        (cuisine, have[key(name)]))
            continue
        while slug in slugs:
            slug += "x"
        slugs.add(slug)
        # No address yet means no distance, so the store starts paused until one is added.
        con.execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,
                       closed_override,open_24,prep_default,cuisine)
                       VALUES(?,?,'1111',?,?,?,?,?,?,0,20,?)""",
                    (name, slug, addr, phone, lat, lng, hours, 0 if addr else 1, cuisine))
        added += 1
    # The brand store stays on file for pickup runs, but customers only see the
    # stores on the list above, so it loads paused.
    con.execute("UPDATE restaurants SET closed_override=1 WHERE slug='tigertowntogo'")
    keep = {key(n) for n, *_ in TTG_LIST} | {"tigertowntogo"}
    for r in con.execute("SELECT id, name, slug FROM restaurants").fetchall():
        if r["slug"] in ("oneoff",) or key(r["name"]) in keep:
            continue
        used = con.execute("SELECT COUNT(*) c FROM orders WHERE restaurant_id=?", (r["id"],)).fetchone()["c"]
        if used == 0:
            con.execute("DELETE FROM menu_items WHERE restaurant_id=?", (r["id"],))
            con.execute("DELETE FROM restaurants WHERE id=?", (r["id"],))
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('store_list_loaded','1')")
    con.commit()
    return added


def seed(con):
    hours = json.dumps(DEFAULT_HOURS)
    # Real Auburn / Opelika businesses, the kind of list Fleet Delivery carries.
    # Addresses, phones and coordinates are real; the menu items below are sample
    # lines you edit per store from Dispatch > Manage > Menu items.
    rows = [
        ("Fleet Delivery", "tigertowntogo", "1111", "216 S 8th St, Opelika, AL 36801",
         "334-209-2844", 32.6470902, -85.3774403, hours, 20),
        ("Niffer's Place", "niffers", "1111", "1151 Opelika Rd, Auburn, AL 36830",
         "334-821-3118", 32.6211822, -85.4573359, hours, 20),
        ("Amsterdam Cafe", "amsterdam", "1111", "410 S Gay St, Auburn, AL 36830",
         "334-826-8181", 32.5982594, -85.4803198, hours, 20),
        ("Hamilton's On Magnolia", "hamiltons", "1111", "174 E Magnolia Ave, Auburn, AL 36830",
         "334-887-2677", 32.6063159, -85.4800355, hours, 25),
        ("Bow & Arrow", "bowarrow", "1111", "1977 E Samford Ave, Auburn, AL 36830",
         "334-246-2546", 32.603836, -85.441101, hours, 20),
        ("Roni's Mac Bar", "ronis", "1111", "138 N College St, Auburn, AL 36830",
         "334-203-4295", 32.6076887, -85.4815303, hours, 15),
        ("Waldo's Chicken & Beer", "waldos", "1111", "1120 S College St, Auburn, AL 36832",
         "334-780-0706", 32.5843518, -85.4908511, hours, 18),
        ("Jim 'N Nick's Bar-B-Q", "jimnnicks", "1111", "1920 S College St, Auburn, AL 36832",
         "334-246-5197", 32.5700098, -85.5010911, hours, 20),
        ("Baumhower's Victory Grille", "baumhowers", "1111", "2353 Bent Creek Rd, Auburn, AL 36830",
         "334-246-4180", 32.6070722, -85.4285063, hours, 20),
        ("Byron's Smokehouse", "byrons", "1111", "436 Opelika Rd, Auburn, AL 36830",
         "334-887-9981", 32.6128569, -85.4738284, hours, 15),
        ("Auburn Draft House", "drafthouse", "1111", "161 E Magnolia Ave, Auburn, AL 36830",
         "334-521-2739", 32.6067015, -85.4802928, hours, 20),
        ("The Depot", "depot", "1111", "124 Mitcham Ave, Auburn, AL 36830",
         "334-521-5177", 32.610198, -85.4807851, hours, 25),
        ("Hey Day Market", "heyday", "1111", "211 S College St, Auburn, AL 36830",
         "334-844-1300", 32.6036677, -85.4814784, hours, 15),
    ]
    for r in rows:
        con.execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,prep_default)
                       VALUES(?,?,?,?,?,?,?,?,?)""", r)
    menus = {
        "tigertowntogo": [("Restaurant pickup run", "We collect an order you placed yourself", 0),
                          ("Catering run", "Large pickup, call dispatch first", 0),
                          ("Grocery pickup", "Short list, receipt on delivery", 0)],
        "niffers": [("Buffalo Chicken Fingers", "Hand breaded, ranch", 1299),
                    ("Niffer Burger", "Cheddar, bacon, fries", 1349),
                    ("Chicken Philly", "Peppers, onions, provolone", 1249),
                    ("Loaded Potato Soup", "Bowl", 699),
                    ("House Salad", "Greens, tomato, cucumber", 799)],
        "amsterdam": [("Chicken Salad Plate", "Croissant, fruit", 1199),
                      ("Amsterdam Burger", "Swiss, mushrooms", 1299),
                      ("Shrimp and Grits", "Andouille, cream sauce", 1699),
                      ("Fried Green Tomatoes", "Remoulade", 899),
                      ("Sweet Tea (32oz)", "", 299)],
        "hamiltons": [("Fried Chicken Plate", "Two sides", 1799),
                      ("Shrimp and Grits", "Stone ground grits", 1999),
                      ("Magnolia Burger", "Pimento cheese", 1599),
                      ("Fried Okra", "Basket", 799),
                      ("Peach Cobbler", "", 799)],
        "bowarrow": [("Pulled Pork Plate", "Two sides, bread", 1449),
                     ("Half Rack Ribs", "Dry rub", 2199),
                     ("Smoked Wings", "Eight, Alabama white", 1299),
                     ("Brunswick Stew", "Pint", 749),
                     ("Banana Pudding", "", 549)],
        "ronis": [("Classic Mac", "Three cheese", 999),
                  ("Buffalo Chicken Mac", "Ranch drizzle", 1249),
                  ("Brisket Mac", "Smoked brisket, onion", 1399),
                  ("Garlic Bread", "", 449),
                  ("Fountain Drink", "", 249)],
        "waldos": [("Half Chicken Plate", "Two sides", 1399),
                   ("Chicken Tenders", "Four, sauce", 1099),
                   ("Chicken Sandwich", "Pickles, slaw", 1049),
                   ("Mac and Cheese", "Side", 449),
                   ("Lemonade", "", 329)],
        "jimnnicks": [("Pulled Pork Sandwich", "Cheese biscuit", 1199),
                      ("Rib Plate", "Two sides", 2099),
                      ("Smoked Turkey Plate", "Two sides", 1599),
                      ("Collard Greens", "Side", 429),
                      ("Sweet Tea (32oz)", "", 299)],
        "baumhowers": [("Wings (10)", "Choice of sauce", 1599),
                       ("Victory Burger", "Bacon, cheddar", 1399),
                       ("Chicken Tender Basket", "Fries", 1299),
                       ("Fried Pickles", "Ranch", 899),
                       ("Fountain Drink", "", 299)],
        "byrons": [("Pork Plate", "Two sides", 1399),
                   ("Chopped Pork Sandwich", "Slaw", 999),
                   ("Smoked Sausage Plate", "Two sides", 1349),
                   ("Baked Beans", "Side", 399),
                   ("Sweet Tea (32oz)", "", 279)],
        "drafthouse": [("Draft Burger", "Cheddar, fries", 1399),
                       ("Buffalo Wings (10)", "Celery, ranch", 1549),
                       ("Fish and Chips", "Tartar", 1599),
                       ("Loaded Fries", "Bacon, cheese", 999),
                       ("Fountain Drink", "", 299)],
        "depot": [("Shrimp Po'boy", "Remoulade, fries", 1699),
                  ("Fried Catfish Plate", "Two sides", 1799),
                  ("Crab Cakes", "Two, slaw", 2199),
                  ("Hushpuppies", "Basket", 699),
                  ("Key Lime Pie", "", 849)],
        "heyday": [("Smash Burger", "Double, fries", 1299),
                   ("Chicken Tikka Bowl", "Rice, naan", 1399),
                   ("Street Tacos (3)", "Salsa verde", 1249),
                   ("Loaded Nachos", "Queso, jalapeno", 1099),
                   ("Fountain Drink", "", 279)],
    }
    for slug, items in menus.items():
        rid = con.execute("SELECT id FROM restaurants WHERE slug=?", (slug,)).fetchone()["id"]
        for name, desc, price in items:
            con.execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents)
                           VALUES(?,?,?,?)""", (rid, name, desc, price))
    for dname, duser, dpass in [("Dispatch Desk", "admin", "dispatch123"),
                                ("Nicole Frazier", "nicole", "night123")]:
        con.execute("INSERT INTO dispatchers(name,username,password,created_at) VALUES(?,?,?,?)",
                    (dname, duser, dpass, dt.datetime.now().isoformat(timespec="seconds")))
    for name, phone in [("Marcus Hill", "3345550111"), ("Dana Reed", "3345550122"),
                        ("Chris Boyd", "3345550133")]:
        con.execute("INSERT INTO drivers(name,phone,pin) VALUES(?,?,?)", (name, phone, "1234"))
    demo = [
        ("2302 waverly parkway, opelika, al 36801", "2302 Waverly Pkwy, Opelika, AL 36801", 32.6514, -85.3968),
        ("600 s college st, auburn, al 36832", "600 S College St, Auburn, AL 36832", 32.5932, -85.4855),
        ("1700 fob james dr, valley, al 36854", "1700 Fob James Dr, Valley, AL 36854", 32.8172, -85.1839),
    ]
    for q, f, la, ln in demo:
        con.execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok) VALUES(?,?,?,?,1)",
                    (q, f, la, ln))

def setting(key, cast=int):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return cast(row["value"]) if row else None

# ---------------------------------------------------------------- geo + fees

def haversine_miles(a_lat, a_lng, b_lat, b_lng):
    R = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

ROAD_FACTOR = 1.3   # straight-line -> driving estimate when no routing key is set

def geocode(raw):
    """Validate + normalise a customer address. Returns dict(ok, formatted, lat, lng, source)."""
    q = " ".join(raw.lower().split())
    row = db().execute("SELECT * FROM geocache WHERE q=?", (q,)).fetchone()
    if row:
        return {"ok": bool(row["ok"]), "formatted": row["formatted"],
                "lat": row["lat"], "lng": row["lng"], "source": "cache"}
    res = None
    try:
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?address="
                   + urllib.parse.quote(raw) + "&key=" + GOOGLE_KEY)
            data = json.loads(urllib.request.urlopen(url, timeout=8).read())
            if data.get("status") == "OK":
                top = data["results"][0]
                loc = top["geometry"]["location"]
                res = {"ok": True, "formatted": top["formatted_address"],
                       "lat": loc["lat"], "lng": loc["lng"], "source": "google"}
        else:
            url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
                   + urllib.parse.quote(raw))
            req = urllib.request.Request(url, headers={"User-Agent": "fleetdelivery/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read())
            if data:
                top = data[0]
                res = {"ok": True, "formatted": top["display_name"],
                       "lat": float(top["lat"]), "lng": float(top["lon"]), "source": "osm"}
    except Exception:
        res = None
    if res is None:
        return {"ok": False, "formatted": None, "lat": None, "lng": None, "source": "none"}
    db().execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok) VALUES(?,?,?,?,1)",
                 (q, res["formatted"], res["lat"], res["lng"]))
    db().commit()
    return res

def fee_for_miles(miles):
    base_fee = setting("base_fee_cents")
    base_miles = setting("base_miles")
    per_mile = setting("per_mile_cents")
    if miles <= base_miles:
        return base_fee
    return base_fee + int(math.ceil(miles - base_miles)) * per_mile

def quote(restaurant, lat, lng):
    miles = round(haversine_miles(restaurant["lat"], restaurant["lng"], lat, lng) * ROAD_FACTOR, 2)
    return miles, fee_for_miles(miles)

def money(cents):
    return "${:,.2f}".format((cents or 0) / 100.0)

app.jinja_env.filters["money"] = money

# ---------------------------------------------------------------- hours

WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

def closed_days(restaurant_id=None):
    rows = db().execute("""SELECT day, reason, restaurant_id FROM closures
                           WHERE restaurant_id IS NULL OR restaurant_id=?
                           ORDER BY day""", (restaurant_id,)).fetchall()
    return [{"day": r["day"], "reason": r["reason"],
             "all_stores": r["restaurant_id"] is None} for r in rows]

def is_closed_day(restaurant_id, when=None):
    when = when or dt.datetime.now()
    day = when.strftime("%Y-%m-%d")
    row = db().execute("""SELECT reason FROM closures WHERE day=?
                          AND (restaurant_id IS NULL OR restaurant_id=?)""",
                       (day, restaurant_id)).fetchone()
    return row["reason"] if row else None

def is_open(restaurant, when=None):
    if restaurant["closed_override"]:
        return False
    if is_closed_day(restaurant["id"], when):
        return False
    if restaurant["open_24"]:
        return True
    when = when or dt.datetime.now()
    hours = json.loads(restaurant["hours"])
    span = hours.get(str(when.weekday()))
    if not span or span[0] == "" or span[1] == "":
        return False
    o = dt.datetime.strptime(span[0], "%H:%M").time()
    c = dt.datetime.strptime(span[1], "%H:%M").time()
    t = when.time()
    return o <= t <= c if o <= c else (t >= o or t <= c)

def hours_label(restaurant):
    shut = is_closed_day(restaurant["id"])
    if shut:
        return "Closed today (" + shut + ")" if shut.strip() else "Closed today"
    if restaurant["open_24"]:
        return "Open 24 hours"
    hours = json.loads(restaurant["hours"])
    span = hours.get(str(dt.datetime.now().weekday()))
    if not span or not span[0]:
        return "Closed today"
    return "Today " + span[0] + " - " + span[1]

# ---------------------------------------------------------------- queue / dispatch

def on_shift_drivers():
    """Everyone clocked on, in rotation order: fewest live orders first, then whoever has
    waited longest since their last one."""
    return db().execute("""
        SELECT d.*, (SELECT COUNT(*) FROM orders o
                     WHERE o.driver_id=d.id AND o.dispatch_status IN ('assigned','received','at_restaurant','enroute')) AS load
        FROM drivers d WHERE d.status='online'
        ORDER BY load ASC,
                 MAX(COALESCE(d.last_completed_at,''), COALESCE(d.last_assigned_at,''),
                     COALESCE(d.online_since,'')) ASC,
                 d.id ASC""").fetchall()


def available_drivers():
    """Who auto dispatch may hand an order to. One order each, taken in turn: a driver
    already holding one waits until the rotation comes back around. With fewer than two
    drivers on shift nothing goes out automatically, the dispatcher assigns by hand."""
    rows = on_shift_drivers()
    if len(rows) < 2:
        return []
    return [r for r in rows if r["load"] == 0]


def recompute_queue():
    """Queue = orders with no driver yet, oldest first, with the reason each one is waiting."""
    con = db()
    waiting = con.execute("""SELECT * FROM orders
                             WHERE dispatch_status IN ('queued','held')
                             ORDER BY created_at ASC, id ASC""").fetchall()
    shift = on_shift_drivers()
    free = available_drivers()
    auto_on = bool(setting("auto_assign"))
    stages = ("pending", "preparing", "ready") if setting("assign_on_pending") else ("preparing", "ready")
    if not shift:
        short = "no driver on shift, dispatcher to assign"
    elif len(shift) < 2:
        short = "only one driver on shift, dispatcher to assign"
    else:
        short = "every driver has an order, dispatcher to assign"
    for i, o in enumerate(waiting):
        if o["address_ok"] == 0:
            status, reason = "held", "address needs dispatch approval"
        elif o["kitchen_status"] not in stages:
            status, reason = "held", "waiting on kitchen"
        elif not auto_on:
            status, reason = "queued", "auto dispatch off, assign by hand"
        elif i >= len(free):
            status, reason = "held", short
        else:
            status, reason = "queued", None
        con.execute("UPDATE orders SET dispatch_status=?, hold_reason=? WHERE id=?",
                    (status, reason, o["id"]))
    con.commit()

def queue_position(order_id):
    rows = db().execute("""SELECT id FROM orders WHERE dispatch_status IN ('queued','held')
                           ORDER BY created_at ASC, id ASC""").fetchall()
    for i, r in enumerate(rows):
        if r["id"] == order_id:
            return i + 1
    return None

def place_redo_orders():
    """A new order made from a finished one can go back to its original driver. It goes to
    them as soon as it is released to dispatch (paid card or cash), even with auto dispatch off,
    and waits in Pending if that driver is off shift."""
    con = db()
    rows = con.execute("""SELECT * FROM orders WHERE redo_driver_id IS NOT NULL AND driver_id IS NULL
                          AND dispatch_status IN ('queued','held') AND kitchen_status!='waiting'""").fetchall()
    for o in rows:
        d = con.execute("SELECT * FROM drivers WHERE id=?", (o["redo_driver_id"],)).fetchone()
        if not d:
            con.execute("UPDATE orders SET redo_driver_id=NULL WHERE id=?", (o["id"],))
            continue
        if d["status"] not in ("online", "break"):
            con.execute("UPDATE orders SET dispatch_status='held', hold_reason=? WHERE id=?",
                        ("waiting on " + d["name"] + " (original driver) to come on shift", o["id"]))
            continue
        seq = con.execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                             AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                          (d["id"],)).fetchone()["s"]
        con.execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned', stack_seq=?, hold_reason=NULL,
                       redo_driver_id=NULL, paged_at=? WHERE id=?""", (d["id"], seq, now(), o["id"]))
        con.execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                    (d["id"], "dispatch", "New order " + o["code"] + " (redo of " + (o["cloned_from"] or "an earlier order") +
                     ((": " + o["issue"]) if o["issue"] else "") + ") was sent to you. Tap Received to accept it.", now()))
        log("redo", o["code"] + " sent to original driver " + d["name"])
    con.commit()

def auto_assign():
    try:
        release_scheduled()
    except Exception as e:
        print("future release skipped:", e)
    try:
        place_redo_orders()
    except Exception as e:
        print("redo placement skipped:", e)
    if not setting("auto_assign"):
        recompute_queue()
        return
    con = db()
    while True:
        free = available_drivers()
        if not free:
            break
        stages = ("'pending','preparing','ready'" if setting("assign_on_pending")
                  else "'preparing','ready'")
        o = con.execute("""SELECT * FROM orders
                           WHERE driver_id IS NULL AND redo_driver_id IS NULL
                             AND dispatch_status IN ('queued','held')
                             AND kitchen_status IN (""" + stages + """)
                           ORDER BY created_at ASC LIMIT 1""").fetchone()
        if not o:
            break
        d = free[0]
        seq = con.execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders
                             WHERE driver_id=? AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                          (d["id"],)).fetchone()["s"]
        con.execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned',
                       hold_reason=NULL, stack_seq=? WHERE id=?""", (d["id"], seq, o["id"]))
        con.execute("UPDATE drivers SET last_assigned_at=? WHERE id=?", (now(), d["id"]))
        con.execute("""INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)""",
                    (d["id"], "system",
                     "Order " + o["code"] + " assigned to you (stop #" + str(seq) + ").", now()))
        log("assign", o["code"] + " -> " + d["name"])
        con.commit()
    recompute_queue()

def nav_url(dest, lat=None, lng=None, name=None):
    """Driving directions. Coordinates when we have them, so the pin lands on the door,
    with the place name as the label the driver sees."""
    target = (str(lat) + "," + str(lng)) if lat and lng else (dest or "")
    url = ("https://www.google.com/maps/dir/?api=1&travelmode=driving&destination="
           + urllib.parse.quote(target))
    if name:
        url += "&destination_name=" + urllib.parse.quote(name)
    return url

app.jinja_env.globals["nav_url"] = nav_url

def customer_nav_url(address, lat=None, lng=None):
    """Customer directions go to the street address itself, so Maps shows the house number,
    street and city instead of bare coordinates. Coordinates are only the fallback when the
    address has no city on it."""
    addr = " ".join(str(address or "").split())
    if addr and "," in addr:
        return ("https://www.google.com/maps/dir/?api=1&travelmode=driving&destination="
                + urllib.parse.quote(addr))
    return nav_url(addr, lat, lng)

def digits(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def item_options(item_id):
    groups = []
    for g in db().execute("SELECT * FROM option_groups WHERE item_id=? ORDER BY sort, id",
                          (item_id,)).fetchall():
        opts = db().execute("SELECT * FROM options WHERE group_id=? ORDER BY sort, id",
                            (g["id"],)).fetchall()
        groups.append({"id": g["id"], "name": g["name"], "min": g["min_select"],
                       "max": g["max_select"],
                       "options": [{"id": o["id"], "name": o["name"],
                                    "delta_cents": o["price_delta_cents"],
                                    "delta": money(o["price_delta_cents"])} for o in opts]})
    return groups


def menu_payload(rid):
    """Items in menu order: sections in the order a dispatcher set, items inside them."""
    out = []
    for it in db().execute("""SELECT * FROM menu_items WHERE restaurant_id=? AND active=1
                              ORDER BY sort, id""", (rid,)).fetchall():
        out.append({"id": it["id"], "name": it["name"], "description": it["description"],
                    "price_cents": it["price_cents"], "price": money(it["price_cents"]),
                    "section": (it["section"] or "").strip(),
                    "groups": item_options(it["id"])})
    return out


def menu_sections(rid):
    """Section names already in use at this restaurant, in menu order."""
    rows = db().execute("""SELECT section, MIN(sort) s, MIN(id) i FROM menu_items
                           WHERE restaurant_id=? AND section IS NOT NULL AND section<>''
                           GROUP BY section ORDER BY s, i""", (rid,)).fetchall()
    return [r["section"] for r in rows]


def line_label(it):
    """One order line written out in full, combo picks included."""
    base = "%d x %s" % (it.get("qty", 1), it.get("name", "item"))
    picks = it.get("options") or []
    if picks:
        base += " (" + ", ".join(
            (p.get("group", "") + ": " if p.get("group") else "") + p.get("name", "")
            + ((" +" + money(p["delta_cents"])) if p.get("delta_cents") else "")
            for p in picks) + ")"
    if it.get("note"):
        base += " - " + it["note"]
    return base


def new_code():
    issue_key = (payload.get("issue") or "").strip()
    issue_label, issue_note, from_code = "", (payload.get("issue_note") or "").strip(), ""
    src_id = payload.get("from_order_id")
    if src_id:
        src = db().execute("SELECT * FROM orders WHERE id=?", (src_id,)).fetchone()
        if src:
            from_code = src["code"]
            if issue_key and issue_key not in REDO_REASONS:
                return jsonify({"ok": False, "error": "Pick what went wrong with the first order."}), 400
            if not issue_key and src["dispatch_status"] in ("delivered", "cancelled"):
                return jsonify({"ok": False,
                                "error": "Say why " + src["code"] + " is going out again."}), 400
            if issue_key:
                issue_label = REDO_REASONS[issue_key]
                db().execute("UPDATE orders SET issue=?, issue_note=? WHERE id=?",
                             (issue_label, issue_note, src["id"]))
    code = "FF" + dt.datetime.now().strftime("%H%M%S")
    while db().execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
        issue_key = (payload.get("issue") or "").strip()
    issue_label, issue_note, from_code = "", (payload.get("issue_note") or "").strip(), ""
    src_id = payload.get("from_order_id")
    if src_id:
        src = db().execute("SELECT * FROM orders WHERE id=?", (src_id,)).fetchone()
        if src:
            from_code = src["code"]
            if issue_key and issue_key not in REDO_REASONS:
                return jsonify({"ok": False, "error": "Pick what went wrong with the first order."}), 400
            if not issue_key and src["dispatch_status"] in ("delivered", "cancelled"):
                return jsonify({"ok": False,
                                "error": "Say why " + src["code"] + " is going out again."}), 400
            if issue_key:
                issue_label = REDO_REASONS[issue_key]
                db().execute("UPDATE orders SET issue=?, issue_note=? WHERE id=?",
                             (issue_label, issue_note, src["id"]))
    code = "FF" + dt.datetime.now().strftime("%H%M%S") + str(secrets.randbelow(900) + 100)
    return code

STATUS_WORDS = {
    ("order", "placed"): "Order placed",
    ("kitchen", "waiting"): "Waiting on payment",
    ("kitchen", "scheduled"): "Scheduled",
    ("dispatch", "scheduled"): "Future order",
    ("kitchen", "pending"): "Sent to kitchen",
    ("kitchen", "preparing"): "Kitchen started cooking",
    ("kitchen", "ready"): "Food ready",
    ("dispatch", "held"): "Held by dispatch",
    ("dispatch", "queued"): "In the queue",
    ("dispatch", "assigned"): "Assigned to a driver",
    ("dispatch", "received"): "Driver accepted",
    ("dispatch", "at_restaurant"): "Driver at restaurant",
    ("dispatch", "picked_up"): "Picked up",
    ("dispatch", "enroute"): "On the way",
    ("dispatch", "delivered"): "Delivered",
    ("dispatch", "cancelled"): "Cancelled",
    ("payment", "unpaid"): "Payment due",
    ("payment", "paid"): "Paid",
    ("payment", "part_refunded"): "Partly refunded",
    ("payment", "refunded"): "Refunded",
}


def clock(ts):
    """2026-09-29T14:12:06 -> 2:12 PM. Falls back to the raw stamp if it is odd."""
    if not ts:
        return ""
    try:
        d = dt.datetime.fromisoformat(ts.replace(" ", "T"))
    except ValueError:
        return ts
    return d.strftime("%-I:%M %p") if os.name != "nt" else d.strftime("%I:%M %p").lstrip("0")


def stamp_label(kind, status):
    return STATUS_WORDS.get((kind, status), status.replace("_", " ").capitalize())


def order_timeline(oid):
    """Every status this order has been through, with the time it happened."""
    rows = db().execute("""SELECT kind, status, at FROM status_log
                           WHERE order_id=? ORDER BY id""", (oid,)).fetchall()
    out = []
    for r in rows:
        out.append({"kind": r["kind"], "status": r["status"],
                    "label": stamp_label(r["kind"], r["status"]),
                    "at": r["at"], "time": clock(r["at"]),
                    "day": (r["at"] or "")[:10]})
    return out


def stamped_at(oid, kind, status):
    """When a given status was reached, for a one-line reference."""
    r = db().execute("""SELECT at FROM status_log WHERE order_id=? AND kind=? AND status=?
                        ORDER BY id DESC LIMIT 1""", (oid, kind, status)).fetchone()
    return r["at"] if r else None



def oneoff_id():
    row = db().execute("SELECT id FROM restaurants WHERE slug='oneoff'").fetchone()
    return row["id"] if row else None

def pickup_of(o, r):
    """Where the driver actually collects this order. A typed-in pickup wins over the
    restaurant row, so a place that is not on our list still shows up properly
    everywhere: board, kitchen ticket, driver stop and navigation."""
    name = (o["pickup_name"] or "").strip() if "pickup_name" in o.keys() else ""
    if not name:
        return {"name": r["name"] if r else "Pickup", "address": r["address"] if r else "",
                "phone": (r["phone"] or "") if r else "", "lat": r["lat"] if r else None,
                "lng": r["lng"] if r else None, "listed": True}
    return {"name": name, "address": (o["pickup_address"] or "").strip(),
            "phone": (o["pickup_phone"] or "").strip(),
            "lat": o["pickup_lat"], "lng": o["pickup_lng"], "listed": False}

def token_list():
    """The short tags dispatch puts on an order. Editable in settings."""
    raw = db().execute("SELECT value FROM settings WHERE key='order_tokens'").fetchone()
    raw = raw["value"] if raw else "Online,App,Phone call,Third party"
    return [t.strip() for t in raw.split(",") if t.strip()]


def clean_token(v):
    v = (v or "").strip()[:24]
    if not v:
        return ""
    for t in token_list():
        if t.lower() == v.lower():
            return t
    return v


def token_from_source(src):
    return {"call_in": "Phone call", "dispatch_online": "Online",
            "website": "Online"}.get(src, "")


def clean_ref(v):
    """Their number: whatever the call-in slip calls this order."""
    return (v or "").strip()[:32]


def ref_in_use(ref, skip_id=None):
    """Warn on a repeat, do not block it. Two shops can hand out the same number."""
    if not ref:
        return None
    row = db().execute("""SELECT code FROM orders WHERE ref_code=? AND id IS NOT ?
                          AND created_at >= ? ORDER BY id DESC LIMIT 1""",
                       (ref, skip_id,
                        (dt.datetime.now() - dt.timedelta(days=2)).isoformat(timespec="seconds"))).fetchone()
    return row["code"] if row else None


def _safe_items(raw):
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []

def order_dict(o):
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() if o["driver_id"] else None
    eta = None
    if o["prep_started"] and o["prep_minutes"]:
        end = dt.datetime.fromisoformat(o["prep_started"]) + dt.timedelta(minutes=o["prep_minutes"])
        eta = int((end - dt.datetime.now()).total_seconds())
    pu = pickup_of(o, r)
    return {
        "id": o["id"], "code": o["code"],
        "scheduled_for": o["scheduled_for"], "scheduled_label": when_label(o["scheduled_for"]) if o["scheduled_for"] else "",
        "release_label": when_label(o["release_at"]) if o["release_at"] else "", "ref": (o["ref_code"] or ""), "restaurant": pu["name"], "restaurant_address": pu["address"],
        "restaurant_phone": pu["phone"], "restaurant_tel": "tel:" + digits(pu["phone"]),
        "pickup_listed": pu["listed"],
        "customer_tel": "tel:" + digits(o["customer_phone"] or ""),
        "restaurant_nav": nav_url(pu["address"], pu["lat"], pu["lng"], pu["name"]),
        "customer": o["customer_name"], "phone": o["customer_phone"],
        "address": o["address"], "note": o["address_note"],
        "dispatch_note": o["dispatch_note"],
        "customer_nav": customer_nav_url(o["address"], o["lat"], o["lng"]),
        "paged_at": (o["paged_at"] if "paged_at" in o.keys() else "") or "",
        "items": _safe_items(o["items"]),
        "lines": [line_label(x) for x in json.loads(o["items"])],
        "item_count": sum(int(x.get("qty", 1)) for x in json.loads(o["items"])),
        "timeline": order_timeline(o["id"]),
        "placed_time": clock(o["created_at"]),
        "ready_time": clock(o["ready_at"]),
        "delivered_time": clock(o["delivered_at"]),
        "payment_status": o["payment_status"] or "unpaid",
        "pay_method": o["pay_method"] or "",
        "paid": (o["payment_status"] or "unpaid") in ("paid", "part_refunded", "refunded"),
        "pay_link": o["pay_link"] or "",
        "refunded": money(o["refunded_cents"] or 0),
        "refunded_cents": int(o["refunded_cents"] or 0),
        "refund_note": o["refund_note"] or "",
        "tip_cents": int(o["tip_cents"] or 0),
        "tip_sig": o["tip_sig"] or "",
        "tip_signed_at": o["tip_signed_at"] or "",
        "tip_declined": bool(o["tip_declined"]),
        "cash": is_cash(o),
        "card_on_file": bool(card_info(o["id"])),
        "card_last4": (card_info(o["id"]) or {"last4": ""})["last4"] or "",
        "card_brand": (card_info(o["id"]) or {"brand": ""})["brand"] or "",
        "card_viewed_by": (card_info(o["id"]) or {"viewed_by": ""})["viewed_by"] or "",
        "card_paid": bool((card_info(o["id"]) or {"paid_at": None})["paid_at"]),
        "paid_cents": int(o["paid_cents"] or 0),
        "balance_cents": balance_cents(o) if (o["payment_status"] or "") in ("paid", "part_refunded") else 0,
        "balance": money(abs(balance_cents(o))) if (o["payment_status"] or "") in ("paid", "part_refunded") else "",
        "subtotal_cents": int(o["subtotal_cents"] or 0),
        "subtotal": money(o["subtotal_cents"]), "fee": money(o["fee_cents"]),
        "tax": money(o["tax_cents"]), "tip": money(o["tip_cents"]), "total": money(o["total_cents"]),
        "service_cents": int(o["service_cents"] or 0) if "service_cents" in o.keys() else 0,
        "service": money((o["service_cents"] or 0) if "service_cents" in o.keys() else 0),
        "miles": (o["miles"] if o["address_ok"] else None), "kitchen_status": o["kitchen_status"],
        "address_ok": bool(o["address_ok"]),
        "source": o["source"], "source_label": SOURCES.get(o["source"], "Online"),
        "token": (o["token"] or ""),
        "needs_address_approval": (not o["address_ok"]) or o["kitchen_status"] == "waiting",
        "dispatch_status": o["dispatch_status"], "hold_reason": o["hold_reason"],
        "issue": o["issue"] or "", "issue_note": o["issue_note"] or "",
        "cloned_from": o["cloned_from"] or "",
        "driver": d["name"] if d else None, "driver_id": o["driver_id"], "stack_seq": o["stack_seq"],
        "prep_minutes": o["prep_minutes"], "timer_seconds": eta,
        "placed_by": o["placed_by"], "created_at": o["created_at"],
        "delivered_at": o["delivered_at"],
        "queue_position": queue_position(o["id"]),
    }

# ---------------------------------------------------------------- customer site

@app.route("/")
def home():
    rs = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    cards = [{"r": r, "open": is_open(r), "hours": hours_label(r)} for r in rs]
    return render_template("index.html", cards=cards)

@app.route("/r/<slug>")
def menu(slug):
    r = db().execute("SELECT * FROM restaurants WHERE slug=?", (slug,)).fetchone()
    if not r:
        return redirect(url_for("home"))
    items = db().execute("SELECT * FROM menu_items WHERE restaurant_id=? AND active=1", (r["id"],)).fetchall()
    return render_template("menu.html", r=r, items=items, open=is_open(r), hours=hours_label(r),
                           base_fee=money(setting("base_fee_cents")),
                           base_miles=setting("base_miles"),
                           per_mile=money(setting("per_mile_cents")))

@app.post("/api/quote")
def api_quote():
    data = request.get_json(force=True)
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (data.get("restaurant_id"),)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 400
    pu_addr = (data.get("pickup_address") or "").strip()
    if pu_addr:
        # a pickup the dispatcher typed in: mileage runs from that address
        gp = geocode(pu_addr)
        if not gp["ok"]:
            return jsonify({"ok": False, "error":
                            "We could not find that pickup address. Add the city, state and ZIP."})
        r = dict(r)
        r["name"] = (data.get("pickup_name") or "Typed-in pickup").strip()
        r["address"], r["lat"], r["lng"] = gp["formatted"], gp["lat"], gp["lng"]
    g1 = geocode(data.get("address", ""))
    if not g1["ok"]:
        return jsonify({"ok": False, "error": "We could not verify that address. Add the city, state and ZIP."})
    miles, fee = quote(r, g1["lat"], g1["lng"])
    if miles > 60:
        return jsonify({"ok": False, "error":
                        "That matched a place %s mi from %s. Add the street number, city, state and ZIP."
                        % (int(miles), r["name"])})
    return jsonify({"ok": True, "formatted": g1["formatted"], "lat": g1["lat"], "lng": g1["lng"],
                    "miles": miles, "fee_cents": fee, "fee": money(fee),
                    "restaurant": r["name"], "restaurant_address": r["address"],
                    "restaurant_lat": r["lat"], "restaurant_lng": r["lng"]})

@app.post("/api/validate-address")
def api_validate_address():
    """Same answer as /api/quote. The dispatcher create-order screen calls this name."""
    return api_quote()

@app.get("/api/address-suggest")
def api_address_suggest():
    """Type-ahead for the delivery address box, used by the customer site and dispatch."""
    q = " ".join((request.args.get("q") or "").lower().split())
    if len(q) < 4:
        return jsonify({"ok": True, "suggestions": []})
    out, seen = [], set()
    for row in db().execute("""SELECT formatted, lat, lng FROM geocache WHERE ok=1
                               AND (q LIKE ? OR lower(formatted) LIKE ?) LIMIT 6""",
                            ("%" + q + "%", "%" + q + "%")).fetchall():
        if row["formatted"] and row["formatted"] not in seen:
            seen.add(row["formatted"])
            out.append({"formatted": row["formatted"], "lat": row["lat"], "lng": row["lng"]})
    try:
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?address="
                   + urllib.parse.quote(q) + "&components=country:US&key=" + GOOGLE_KEY)
            data = json.loads(urllib.request.urlopen(url, timeout=6).read())
            hits = data.get("results", []) if data.get("status") == "OK" else []
            for h in hits[:6]:
                loc = h["geometry"]["location"]
                if h["formatted_address"] not in seen:
                    seen.add(h["formatted_address"])
                    out.append({"formatted": h["formatted_address"],
                                "lat": loc["lat"], "lng": loc["lng"]})
        else:
            url = ("https://nominatim.openstreetmap.org/search?format=json&limit=6"
                   "&countrycodes=us&viewbox=-85.75,32.95,-85.05,32.40&q=" + urllib.parse.quote(q))
            req = urllib.request.Request(url, headers={"User-Agent": "fleetdelivery/1.0"})
            for h in json.loads(urllib.request.urlopen(req, timeout=6).read()):
                if h["display_name"] not in seen:
                    seen.add(h["display_name"])
                    out.append({"formatted": h["display_name"],
                                "lat": float(h["lat"]), "lng": float(h["lon"])})
    except Exception:
        pass
    for c in out:                      # so picking one is an instant, exact match later
        db().execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok) VALUES(?,?,?,?,1)",
                     (" ".join(c["formatted"].lower().split()), c["formatted"], c["lat"], c["lng"]))
    db().commit()
    return jsonify({"ok": True, "suggestions": out[:6]})

# ---------------------------------------------------------------- payments
# Payments are taken outside this app. Dispatch types the card into the card box
# (it stays in the browser and is never saved), runs it on its own card terminal,
# then marks the order paid with the last four digits. Cash orders are collected
# by the driver and marked paid on delivery.

# ---------------------------------------------------------------- card on file
# A customer types their card on the website. It is checked, encrypted with a key
# taken from CARD_KEY (or SECRET_KEY), and held only until dispatch runs it on the
# outside terminal and marks the order paid. Then it is deleted. Anything left is
# wiped after CARD_HOLD_HOURS (default 24) or when the order closes.

CARD_HOLD_HOURS = float(os.environ.get("CARD_HOLD_HOURS", "24"))
CARD_KEEP_DAYS = float(os.environ.get("CARD_KEEP_DAYS", "30"))

def _fernet():
    from cryptography.fernet import Fernet
    import hashlib
    raw = os.environ.get("CARD_KEY", "")
    if raw:
        try:
            return Fernet(raw.encode())
        except Exception:
            pass
    seed = (raw or app.secret_key or "").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"fleet-card|" + seed).digest()))

def card_brand(n):
    if re.match(r"^4", n): return "Visa"
    if re.match(r"^(5[1-5]|2[2-7])", n): return "Mastercard"
    if re.match(r"^3[47]", n): return "Amex"
    if re.match(r"^(6011|65|64[4-9])", n): return "Discover"
    return ""

def luhn_ok(n):
    if len(n) < 13:
        return False
    total, alt = 0, False
    for ch in reversed(n):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0

def check_card(c):
    """Same rules as the card box in the browser. Returns (clean_card, error)."""
    c = c or {}
    name = " ".join(str(c.get("name") or "").split())
    num = "".join(ch for ch in str(c.get("number") or "") if ch.isdigit())
    exp = str(c.get("exp") or "").strip()
    cvc = str(c.get("cvc") or "").strip()
    zp = str(c.get("zip") or "").strip()
    if not name:
        return None, "Enter the name on the card."
    if not re.match(r"^[A-Za-z][A-Za-z .'\-]*$", name):
        return None, "The name on the card can only have letters, spaces, periods, hyphens and apostrophes."
    if len(name.split(" ")) < 2:
        return None, "Enter the first and last name as printed on the card."
    b = card_brand(num)
    lens = [15] if b == "Amex" else [13, 16, 19] if b == "Visa" else [16] if b else list(range(13, 20))
    if not num:
        return None, "Enter the card number."
    if len(num) not in lens:
        return None, (b or "This card") + " numbers are " + " or ".join(map(str, lens)) + " digits."
    if not luhn_ok(num):
        return None, "That card number does not check out. Please re-check it."
    m = re.match(r"^(\d{2})/(\d{2})$", exp)
    if not m:
        return None, "Enter the expiration date as MM/YY."
    mm, yy = int(m.group(1)), 2000 + int(m.group(2))
    today = dt.datetime.now()
    if not 1 <= mm <= 12:
        return None, "The expiration month must be 01 to 12."
    if yy < today.year or (yy == today.year and mm < today.month):
        return None, "That card has expired."
    if yy > today.year + 20:
        return None, "That expiration year is too far out. Please re-check it."
    need = 4 if b == "Amex" else 3
    if not (cvc.isdigit() and len(cvc) == need):
        return None, ("Amex uses the 4-digit code on the front." if b == "Amex"
                      else "Enter the 3-digit security code from the back of the card.")
    if zp and not re.match(r"^\d{5}(-?\d{4})?$", zp):
        return None, "The billing ZIP is 5 digits (or ZIP+4)."
    return {"name": name, "number": num, "exp": exp, "cvc": cvc, "zip": zp, "brand": b}, None

def store_card(order_id, card):
    blob = _fernet().encrypt(json.dumps(card).encode()).decode()
    db().execute("""INSERT OR REPLACE INTO card_vault(order_id,blob,brand,last4,created_at)
                    VALUES(?,?,?,?,?)""", (order_id, blob, card["brand"], card["number"][-4:], now()))
    db().commit()

def card_info(order_id):
    return db().execute("SELECT brand,last4,created_at,viewed_at,viewed_by,paid_at FROM card_vault WHERE order_id=?",
                        (order_id,)).fetchone()

def drop_card(order_id):
    db().execute("DELETE FROM card_vault WHERE order_id=?", (order_id,))
    db().commit()

def keep_card_after_paid(order_id):
    """Once the card has been run, the security code is dropped for good. Name,
    number, expiration and ZIP stay encrypted so dispatch can look them up later."""
    row = db().execute("SELECT blob FROM card_vault WHERE order_id=?", (order_id,)).fetchone()
    if not row:
        return
    try:
        card = json.loads(_fernet().decrypt(row["blob"].encode()))
    except Exception:
        db().execute("DELETE FROM card_vault WHERE order_id=?", (order_id,))
        return
    card.pop("cvc", None)
    db().execute("UPDATE card_vault SET blob=?, paid_at=? WHERE order_id=?",
                 (_fernet().encrypt(json.dumps(card).encode()).decode(), now(), order_id))

def cancel_unpaid(o, reason, who="dispatch"):
    """The card could not be charged: cancel the order and forget the card."""
    db().execute("""UPDATE orders SET dispatch_status='cancelled', payment_status='declined',
                    kitchen_status='waiting', hold_reason=?, stack_seq=NULL,
                    delivered_at=COALESCE(delivered_at, ?) WHERE id=?""", (reason, now(), o["id"]))
    if o["driver_id"]:
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (o["driver_id"], "system",
                      "Order " + o["code"] + " was cancelled, the card could not be charged.", now()))
    db().execute("DELETE FROM card_vault WHERE order_id=?", (o["id"],))
    db().commit()
    log("cancel", o["code"] + " cancelled by " + who + ": " + reason)

@app.post("/api/order/payment-failed")
def api_payment_failed():
    """Dispatch could not get the card to go through."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (data.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if o["payment_status"] == "paid":
        return jsonify({"ok": False, "error": o["code"] + " is already marked paid. Refund it instead."}), 400
    if o["dispatch_status"] == "cancelled":
        return jsonify({"ok": True, "already": True})
    note = (data.get("note") or "").strip()[:120]
    who = dispatcher_row()
    cancel_unpaid(o, "card could not be charged" + (": " + note if note else ""),
                  who["name"] if who else "dispatch")
    auto_assign()
    return jsonify({"ok": True})

ORDER_KEEP_MIN, ORDER_KEEP_MAX = 30, 3650

def order_purge_cutoff(days):
    """Orders placed before this date (YYYY-MM-DD, midnight local) are purged."""
    return (dt.date.today() - dt.timedelta(days=int(days))).isoformat()

def order_purge_ids(days):
    cut = order_purge_cutoff(days)
    rows = db().execute("""SELECT id FROM orders WHERE dispatch_status IN ('delivered','cancelled')
                           AND created_at < ?""", (cut,)).fetchall()
    return [r["id"] for r in rows], cut

def purge_old_orders(days=None, force=False):
    """Deletes finished customer orders (delivered or cancelled) placed more than
    `days` ago, plus their card records, status history and call alerts.
    Live and future orders are never touched. Returns how many orders went."""
    if days is None:
        days = setting("order_keep_days") or 0
    days = int(days or 0)
    if days < ORDER_KEEP_MIN:
        return 0
    if not force:
        last = setting("order_purge_last", str) or ""
        if last and last > (dt.datetime.now() - dt.timedelta(hours=6)).isoformat(timespec="seconds"):
            return 0
    ids, _cut = order_purge_ids(days)
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('order_purge_last',?)",
                 (dt.datetime.now().isoformat(timespec="seconds"),))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for table in ("card_vault", "status_log", "call_alerts"):
            db().execute("DELETE FROM " + table + " WHERE order_id IN (" + q + ")", chunk)
        db().execute("DELETE FROM orders WHERE id IN (" + q + ")", chunk)
    db().commit()
    if ids:
        db().execute("INSERT INTO events(kind,detail,created_at) VALUES('purge',?,?)",
                     ("%d orders older than %d days" % (len(ids), days),
                      dt.datetime.now().isoformat(timespec="seconds")))
        db().commit()
        try:
            db().execute("VACUUM")   # hand the freed space back to the volume
        except Exception:
            pass
    return len(ids)


def purge_cards():
    """Unpaid cards go after CARD_HOLD_HOURS, paid ones after CARD_KEEP_DAYS,
    and a cancelled order's card right away."""
    hold = (dt.datetime.now() - dt.timedelta(hours=CARD_HOLD_HOURS)).isoformat(timespec="seconds")
    keep = (dt.datetime.now() - dt.timedelta(days=CARD_KEEP_DAYS)).isoformat(timespec="seconds")
    for o in db().execute("""SELECT * FROM orders WHERE dispatch_status='awaiting_payment'
                             AND COALESCE(release_at, created_at) < ? AND created_at < ?""", (hold, hold)).fetchall():
        cancel_unpaid(o, "never paid, cancelled after " + str(int(CARD_HOLD_HOURS)) + " hours", "the system")
    db().execute("""DELETE FROM card_vault WHERE
                    (paid_at IS NULL AND created_at < ? AND order_id NOT IN
                        (SELECT id FROM orders WHERE dispatch_status='scheduled'
                         OR COALESCE(release_at,'') >= ?))
                    OR (paid_at IS NOT NULL AND paid_at < ?)
                    OR order_id IN (SELECT id FROM orders WHERE dispatch_status='cancelled')""", (hold, hold, keep))
    db().commit()

@app.post("/api/order/card-save")
def api_card_save():
    """Dispatch typed a card into the card box and closed it: keep it on the order."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    o = db().execute("SELECT id, code, payment_status FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    card, err = check_card(b.get("card"))
    if not card:
        return jsonify({"ok": False, "error": err}), 400
    store_card(o["id"], card)
    if o["payment_status"] == "paid":
        keep_card_after_paid(o["id"])
        db().commit()
    who = dispatcher_row()
    log("payment", (who["name"] if who else "dispatch") + " saved a card on " + o["code"] +
        " (" + (card["brand"] or "card") + " ending " + card["number"][-4:] + ")")
    return jsonify({"ok": True, "last4": card["number"][-4:], "brand": card["brand"]})

@app.post("/api/order/card-view")
def api_card_view():
    """Dispatch opens the card a customer typed in, to run it on the outside terminal."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    oid = (request.get_json(force=True) or {}).get("order_id")
    row = db().execute("SELECT * FROM card_vault WHERE order_id=?", (oid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "No card on file for that order (it may have been wiped)."}), 404
    try:
        card = json.loads(_fernet().decrypt(row["blob"].encode()))
    except Exception:
        return jsonify({"ok": False, "error": "That card can no longer be read (the server key changed). "
                                              "Call the customer for their card."}), 410
    who = dispatcher_row()
    name = who["name"] if who else "dispatch"
    db().execute("UPDATE card_vault SET viewed_at=?, viewed_by=? WHERE order_id=?", (now(), name, oid))
    db().commit()
    o = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
    log("payment", name + " opened the card on " + (o["code"] if o else str(oid)) +
        " (" + (row["brand"] or "card") + " ending " + (row["last4"] or "") + ")")
    return jsonify({"ok": True, "paid": bool(row["paid_at"]),
                    "card": {k: card.get(k, "") for k in ("name", "number", "exp", "cvc", "zip")}})


def sget(key):
    return (setting(key, cast=str) or "").strip()

def is_cash(o):
    return (o["pay_method"] or "") == "cash"

def balance_cents(o):
    """What is still owed: order total less what was collected, plus refunds.
    Negative means the customer was charged more than the order now comes to."""
    kept = int(o["paid_cents"] or 0) - int(o["refunded_cents"] or 0)
    return int(o["total_cents"] or 0) - kept

def mark_paid(o, method="recorded", ref="", cents=None):
    """Payment landed: record it and let the order into the queue."""
    cents = int(o["total_cents"]) if cents is None else int(cents)
    released = o["dispatch_status"] == "awaiting_payment"
    db().execute("""UPDATE orders SET payment_status='paid', paid_at=?, pay_method=?,
                    pay_ref=?, paid_cents=? WHERE id=?""",
                 (now(), method, ref, cents, o["id"]))
    if released and o["kitchen_status"] in ("preparing", "ready"):
        # The kitchen already has it: leave the timer alone and just open it to drivers.
        db().execute("""UPDATE orders SET dispatch_status='held', hold_reason=NULL
                        WHERE id=? AND driver_id IS NULL""", (o["id"],))
        db().execute("""UPDATE orders SET dispatch_status='assigned'
                        WHERE id=? AND driver_id IS NOT NULL AND dispatch_status='awaiting_payment'""", (o["id"],))
    elif released:
        kitchen = "pending" if o["address_ok"] else "waiting"
        reason = "waiting on kitchen" if o["address_ok"] else "address needs dispatch approval"
        db().execute("""UPDATE orders SET kitchen_status=?, dispatch_status='held', hold_reason=?
                        WHERE id=?""", (kitchen, reason, o["id"]))
    db().commit()
    keep_card_after_paid(o["id"])
    db().commit()
    log("payment", o["code"] + " paid " + money(cents) + " (" + method.replace("_", " ") + ")")
    auto_assign()

def add_extra_charge(o, cents, label, ref):
    rows = json.loads(o["extra_charges"] or "[]")
    rows.append({"cents": int(cents), "label": label, "ref": ref, "at": now()})
    db().execute("UPDATE orders SET paid_cents=paid_cents+?, extra_charges=? WHERE id=?",
                 (int(cents), json.dumps(rows), o["id"]))
    db().commit()


# ---------------------------------------------------------------- future orders
FUTURE_LEAD_ERR = []
FUTURE_MIN_AHEAD = 30      # a future order has to be at least this far out
FUTURE_MAX_DAYS = 14

def future_lead():
    try:
        return max(10, min(240, int(setting("future_lead_min") or 45)))
    except Exception:
        return 45

def when_label(s):
    try:
        w = dt.datetime.fromisoformat(s)
    except Exception:
        return s or ""
    day = w.date()
    today = dt.date.today()
    if day == today:
        d = "Today"
    elif day == today + dt.timedelta(days=1):
        d = "Tomorrow"
    else:
        d = w.strftime("%a %b ") + str(w.day)
    return d + " at " + w.strftime("%I:%M %p").lstrip("0")

def parse_future(s):
    """'2026-10-02T18:30' or '2026-10-02 18:30' -> datetime, or None."""
    s = (s or "").strip().replace(" ", "T")[:16]
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s).replace(second=0, microsecond=0)
    except ValueError:
        return None

def future_slots(r, day):
    """Delivery times a customer can pick on one day: every 15 minutes the restaurant
    is open, at least FUTURE_MIN_AHEAD minutes from now."""
    earliest = dt.datetime.now() + dt.timedelta(minutes=FUTURE_MIN_AHEAD)
    out = []
    t = dt.datetime.combine(day, dt.time(0, 0))
    end = t + dt.timedelta(days=1)
    while t < end:
        if t >= earliest and is_open(r, t):
            out.append(t.strftime("%Y-%m-%dT%H:%M"))
        t += dt.timedelta(minutes=15)
    return out

def release_scheduled():
    """A future order stays off every screen until it is close to its time. Then it goes
    out exactly like a fresh order: cash to the kitchen, card to dispatch to run."""
    con = db()
    rows = con.execute("""SELECT * FROM orders WHERE dispatch_status='scheduled'
                          AND release_at IS NOT NULL AND release_at <= ?""", (now(),)).fetchall()
    for o in rows:
        con.execute("""UPDATE orders SET kitchen_status=?, dispatch_status=?, hold_reason=?,
                       created_at=? WHERE id=? AND dispatch_status='scheduled'""",
                    (o["sched_kitchen"] or "pending", o["sched_dispatch"] or "held",
                     o["sched_hold"] or "waiting on kitchen", now(), o["id"]))
        con.commit()
        log("order", o["code"] + " future order for " + when_label(o["scheduled_for"]) +
            " released to " + ("dispatch to run the card" if (o["sched_dispatch"] == "awaiting_payment")
                               else "the kitchen"))
    return len(rows)



# ---------------------------------------------------------------- dispatch <-> restaurant chat
def rest_msg_dict(m):
    try:
        at = dt.datetime.fromisoformat(m["created_at"]).strftime("%I:%M %p").lstrip("0")
    except Exception:
        at = m["created_at"]
    return {"id": m["id"], "sender": m["sender"], "who": m["who"] or "", "body": m["body"], "at": at}

def rest_chat_rows(rid, limit=200):
    rows = db().execute("""SELECT * FROM rest_messages WHERE restaurant_id=?
                           ORDER BY id DESC LIMIT ?""", (rid, limit)).fetchall()
    return [rest_msg_dict(m) for m in reversed(rows)]

def rest_chat_unread_for_dispatch():
    n = db().execute("SELECT COUNT(*) c FROM rest_messages WHERE sender='restaurant' AND seen_by_dispatch=0").fetchone()["c"]
    m = db().execute("""SELECT m.id, m.restaurant_id, m.body, r.name FROM rest_messages m
                        JOIN restaurants r ON r.id=m.restaurant_id
                        WHERE m.sender='restaurant' AND m.seen_by_dispatch=0
                        ORDER BY m.id DESC LIMIT 1""").fetchone()
    return n, ({"id": m["id"], "restaurant_id": m["restaurant_id"], "name": m["name"], "body": m["body"]} if m else None)

@app.route("/api/restaurant/chat", methods=["GET", "POST"])
def api_rest_chat():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    if request.method == "POST":
        body = " ".join(((request.get_json(silent=True) or {}).get("body") or "").split())[:1000]
        if not body:
            return jsonify({"ok": False, "error": "Type a message first."}), 400
        db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_rest)
                        VALUES(?,?,?,?,?,1)""", (rid, "restaurant", session.get("restaurant_name") or "", body, now()))
        db().commit()
    elif request.args.get("seen") == "1":
        db().execute("UPDATE rest_messages SET seen_by_rest=1 WHERE restaurant_id=? AND sender='dispatch'", (rid,))
        db().commit()
    return jsonify({"ok": True, "messages": rest_chat_rows(rid)})

@app.get("/api/dispatch/rest-chats")
def api_dispatch_rest_chats():
    """Every restaurant for the picker, unread first."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("""SELECT r.id, r.name,
            (SELECT COUNT(*) FROM rest_messages m WHERE m.restaurant_id=r.id AND m.sender='restaurant'
               AND m.seen_by_dispatch=0) unread,
            (SELECT MAX(id) FROM rest_messages m WHERE m.restaurant_id=r.id) last_id
            FROM restaurants r ORDER BY unread DESC, last_id IS NULL, last_id DESC, r.name""").fetchall()
    return jsonify({"ok": True, "restaurants": [{"id": r["id"], "name": r["name"], "unread": r["unread"]} for r in rows]})

@app.route("/api/dispatch/rest-chat/<int:rid>", methods=["GET", "POST"])
def api_dispatch_rest_chat(rid):
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not db().execute("SELECT 1 FROM restaurants WHERE id=?", (rid,)).fetchone():
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 404
    if request.method == "POST":
        body = " ".join(((request.get_json(silent=True) or {}).get("body") or "").split())[:1000]
        if not body:
            return jsonify({"ok": False, "error": "Type a message first."}), 400
        db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_dispatch)
                        VALUES(?,?,?,?,?,1)""", (rid, "dispatch", session.get("dispatcher_name") or "Dispatch", body, now()))
    db().execute("UPDATE rest_messages SET seen_by_dispatch=1 WHERE restaurant_id=? AND sender='restaurant'", (rid,))
    db().commit()
    return jsonify({"ok": True, "messages": rest_chat_rows(rid)})

@app.get("/api/future-slots")
def api_future_slots():
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (request.args.get("restaurant_id"),)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 404
    days = []
    for n in range(FUTURE_MAX_DAYS):
        day = dt.date.today() + dt.timedelta(days=n)
        slots = future_slots(r, day)
        if slots:
            days.append({"date": day.isoformat(),
                         "label": when_label(slots[0]).split(" at ")[0],
                         "slots": [{"value": s, "label": when_label(s).split(" at ")[1]} for s in slots]})
    return jsonify({"ok": True, "open_now": is_open(r), "days": days, "lead_min": future_lead()})

@app.get("/api/dispatch/future")
def api_dispatch_future():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("""SELECT * FROM orders WHERE dispatch_status='scheduled'
                           ORDER BY scheduled_for ASC""").fetchall()
    return jsonify({"ok": True, "lead_min": future_lead(), "orders": [order_dict(o) for o in rows]})

@app.post("/api/dispatch/future-cancel")
def api_future_cancel():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if o["dispatch_status"] != "scheduled":
        return jsonify({"ok": False, "error": o["code"] + " already went out. Cancel it from the board."}), 400
    why = (b.get("reason") or "").strip()[:160] or "no reason given"
    who = session.get("dispatcher_name") or "dispatch"
    db().execute("""UPDATE orders SET dispatch_status='cancelled', kitchen_status='waiting',
                    hold_reason=?, delivered_at=? WHERE id=?""",
                 ("future order cancelled by " + who + ": " + why, now(), o["id"]))
    db().execute("DELETE FROM card_vault WHERE order_id=?", (o["id"],))
    db().commit()
    log("cancel", o["code"] + " (future order for " + when_label(o["scheduled_for"]) + ") cancelled by " + who + ": " + why)
    return jsonify({"ok": True})

@app.post("/api/dispatch/future-release")
def api_future_release():
    """Send a future order out now instead of waiting for its time."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o or o["dispatch_status"] != "scheduled":
        return jsonify({"ok": False, "error": "That future order already went out."}), 400
    db().execute("UPDATE orders SET release_at=? WHERE id=?", (now(), o["id"]))
    db().commit()
    auto_assign()
    return jsonify({"ok": True})

@app.post("/checkout")
def checkout():
    payload = request.get_json(force=True)
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (payload["restaurant_id"],)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 400
    placed_by = payload.get("placed_by", "customer")
    sched = None
    if (payload.get("scheduled_for") or "").strip():
        sched = parse_future(payload.get("scheduled_for"))
        if not sched:
            return jsonify({"ok": False, "error": "Pick a date and time for the future order."}), 400
        is_disp = bool(dispatcher_required())
        soonest = dt.datetime.now() + dt.timedelta(minutes=(5 if is_disp else FUTURE_MIN_AHEAD - 1))
        if sched < soonest:
            return jsonify({"ok": False, "error": "A future order has to be at least " +
                            ("5" if is_disp else str(FUTURE_MIN_AHEAD)) + " minutes from now."}), 400
        if sched > dt.datetime.now() + dt.timedelta(days=FUTURE_MAX_DAYS):
            return jsonify({"ok": False, "error": "Future orders can be up to " +
                            str(FUTURE_MAX_DAYS) + " days out."}), 400
        if not is_disp and not is_open(r, sched):
            return jsonify({"ok": False, "error": r["name"] + " is not open at " +
                            when_label(sched.isoformat()) + ". Pick another time."}), 400
    if not sched and not is_open(r) and placed_by == "customer":
        return jsonify({"ok": False, "error": r["name"] + " is closed right now. "
                        "You can still schedule the order for a time they are open."}), 400
    dg = "".join(ch for ch in str(payload.get("customer_phone", "")) if ch.isdigit())
    blocked = db().execute("SELECT * FROM blocked_customers WHERE phone=?", (dg,)).fetchone()
    if blocked and placed_by == "customer":
        return jsonify({"ok": False,
                        "error": "This number cannot place orders online. Please call dispatch."}), 403
    typed = (payload.get("address") or "").strip()
    if not typed:
        return jsonify({"ok": False, "error": "Enter a delivery address."}), 400
    items = clean_items(payload["items"])
    subtotal = sum(i["price_cents"] * i["qty"] for i in items)
    ifee = item_fees(items)
    if subtotal <= 0:
        return jsonify({"ok": False, "error": "Your cart is empty."}), 400
    card = None
    if placed_by == "customer":
        card, card_err = check_card(payload.get("card"))
        if not card:
            return jsonify({"ok": False, "error": card_err, "field": "card"}), 400

    # An address our map cannot place no longer stops the order. It goes to dispatch
    # for approval, priced at the base fee until a dispatcher confirms the distance.
    # a pickup the dispatcher typed in: it prices and navigates off that address
    pu_name = (payload.get("pickup_name") or "").strip()[:80]
    pu_addr = (payload.get("pickup_address") or "").strip()[:160]
    pu_phone = (payload.get("pickup_phone") or "").strip()[:24]
    pu_lat = pu_lng = None
    if pu_name or pu_addr:
        if not (pu_name and pu_addr):
            return jsonify({"ok": False,
                            "error": "A typed-in pickup needs both a name and an address."}), 400
        if not dispatcher_required():
            return jsonify({"ok": False, "error": "Dispatch sign-in required."}), 403
        gp = geocode(pu_addr)
        if gp["ok"]:
            pu_addr, pu_lat, pu_lng = gp["formatted"], gp["lat"], gp["lng"]
        r = dict(r)
        r["name"], r["address"] = pu_name, pu_addr
        r["phone"], r["lat"], r["lng"] = pu_phone, pu_lat, pu_lng

    g1 = geocode(typed)
    address_ok = 1 if g1["ok"] else 0
    if address_ok:
        formatted, lat, lng = g1["formatted"], g1["lat"], g1["lng"]
        miles, fee = (quote(r, lat, lng) if r["lat"] and r["lng"]
                      else (0, setting("base_fee_cents")))
    else:
        formatted, lat, lng = typed, None, None
        miles, fee = 0, setting("base_fee_cents")
    if payload.get("fee_cents_override") not in (None, ""):
        fee = max(0, int(round(float(payload["fee_cents_override"]))))
    tax = int(round(subtotal * setting("tax_rate_bp") / 10000.0))
    service = int(round(subtotal * setting("service_fee_bp") / 10000.0))
    tip = int(payload.get("tip_cents", 0))
    total = subtotal + fee + ifee + tax + service + tip

    issue_key = (payload.get("issue") or "").strip()
    issue_label, issue_note, from_code = "", (payload.get("issue_note") or "").strip(), ""
    src_id = payload.get("from_order_id")
    if src_id:
        src = db().execute("SELECT * FROM orders WHERE id=?", (src_id,)).fetchone()
        if src:
            from_code = src["code"]
            if issue_key and issue_key not in REDO_REASONS:
                return jsonify({"ok": False, "error": "Pick what went wrong with the first order."}), 400
            if not issue_key and src["dispatch_status"] in ("delivered", "cancelled"):
                return jsonify({"ok": False,
                                "error": "Say why " + src["code"] + " is going out again."}), 400
            if issue_key:
                issue_label = REDO_REASONS[issue_key]
                db().execute("UPDATE orders SET issue=?, issue_note=? WHERE id=?",
                             (issue_label, issue_note, src["id"]))

    src = (payload.get("source") or "").strip()
    if src not in SOURCES:
        src = {"customer": "website", "dispatcher": "call_in"}.get(placed_by, "website")
    if src in ("call_in", "dispatch_online") and not dispatcher_required():
        return jsonify({"ok": False, "error": "Dispatch sign-in required."}), 403
    if session.get("restaurant_id") and not dispatcher_required():
        return jsonify({"ok": False, "error":
                        "Restaurants cannot place delivery orders. Call dispatch."}), 403
    kitchen_status = "pending" if address_ok else "waiting"
    hold_reason = "waiting on kitchen" if address_ok else "address needs dispatch approval"
    dstat = "held"
    if not (bool(payload.get("cash")) and placed_by == "dispatch" and bool(dispatcher_required())):
        # Card order: the kitchen never sees it until dispatch marks the card paid.
        kitchen_status, dstat = "waiting", "awaiting_payment"
        hold_reason = "card on file, run it" if card else "waiting on card"
    code = "FF" + dt.datetime.now().strftime("%H%M%S") + str(secrets.randbelow(900) + 100)
    while db().execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
        code = "FF" + dt.datetime.now().strftime("%H%M%S") + str(secrets.randbelow(900) + 100)
    cur = db().execute("""INSERT INTO orders(code,restaurant_id,customer_name,customer_phone,address,
        address_note,dispatch_note,lat,lng,items,subtotal_cents,fee_cents,item_fee_cents,tax_cents,
        tip_cents,total_cents,miles,issue,issue_note,cloned_from,address_ok,source,ref_code,token,
        kitchen_status,dispatch_status,hold_reason,placed_by,created_at,
        pickup_name,pickup_address,pickup_phone,pickup_lat,pickup_lng,service_cents)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, r["id"], payload["customer_name"], payload["customer_phone"], formatted,
         payload.get("note", ""), payload.get("dispatch_note", ""), lat, lng,
         json.dumps(items), subtotal, fee, ifee, tax, tip, total, miles,
         issue_label, issue_note, from_code, address_ok, src,
         clean_ref(payload.get("ref")) if dispatcher_required() else None,
         (clean_token(payload.get("token")) or token_from_source(src)),
         kitchen_status, dstat, hold_reason, placed_by, now(),
         pu_name or None, pu_addr if pu_name else None, pu_phone if pu_name else None,
         pu_lat if pu_name else None, pu_lng if pu_name else None, service))
    db().commit()
    log("order", code + " placed for " + r["name"] +
        ("" if address_ok else " (address not verified, waiting on dispatch approval)") +
        (" (from " + from_code + (": " + issue_label if issue_label else "") + ")" if from_code else ""))
    oid = cur.lastrowid
    cash = bool(payload.get("cash")) and placed_by == "dispatch" and bool(dispatcher_required())
    if cash:
        db().execute("UPDATE orders SET pay_method='cash', payment_status='cash_due' WHERE id=?", (oid,))
        db().commit()
    else:
        # Card order: it waits off the kitchen screen and out of the driver queue until
        # dispatch runs the card on its own terminal and marks it paid.
        db().execute("""UPDATE orders SET payment_status='unpaid', kitchen_status='waiting',
                        dispatch_status='awaiting_payment', hold_reason=?
                        WHERE id=?""", ("card on file, run it" if card else "waiting on card", oid))
        db().commit()
        if card:
            store_card(oid, card)
    future_note = ""
    if sched:
        rel = sched - dt.timedelta(minutes=future_lead())
        cur_o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        db().execute("UPDATE orders SET scheduled_for=?, release_at=? WHERE id=?",
                     (sched.isoformat(timespec="seconds"), rel.isoformat(timespec="seconds"), oid))
        if rel > dt.datetime.now():
            db().execute("""UPDATE orders SET sched_kitchen=?, sched_dispatch=?, sched_hold=?,
                            kitchen_status='scheduled', dispatch_status='scheduled', hold_reason=?
                            WHERE id=?""",
                         (cur_o["kitchen_status"], cur_o["dispatch_status"], cur_o["hold_reason"],
                          "future order for " + when_label(sched.isoformat()), oid))
            log("order", code + " scheduled for " + when_label(sched.isoformat()))
        db().commit()
        future_note = "Scheduled for " + when_label(sched.isoformat()) + "."
        if dispatcher_required() and not is_open(r, sched):
            future_note += " Heads up: " + r["name"] + " is not normally open then."
    send_note = ""
    if src_id and dispatcher_required() and payload.get("send_to") == "driver":
        srow = db().execute("SELECT driver_id FROM orders WHERE id=?", (src_id,)).fetchone()
        if srow and srow["driver_id"]:
            db().execute("UPDATE orders SET redo_driver_id=? WHERE id=?", (srow["driver_id"], oid))
            db().commit()
            dn = db().execute("SELECT name FROM drivers WHERE id=?", (srow["driver_id"],)).fetchone()
            send_note = "Going to " + (dn["name"] if dn else "the original driver") + \
                        (" once the card is marked paid." if not cash else ".")
    if address_ok and cash and not (sched and dt.datetime.now() < sched - dt.timedelta(minutes=future_lead())):
        auto_assign()
    return jsonify({"future_note": future_note, "ok": True, "cash": cash, "code": code, "order_id": oid, "total": money(total),
                    "send_note": send_note,
                    "address_ok": bool(address_ok),
                    "message": ("" if address_ok and cash else
                                "Thanks! Your card is being run now. Your order goes to the "
                                "kitchen as soon as the payment goes through." if address_ok else
                                "We could not verify that address, so your order is pending "
                                "dispatch approval. Dispatch will confirm it shortly and your "
                                "delivery fee may change with the distance.")})

@app.post("/api/order/approve-address")
def api_approve_address():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    p = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (p.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    addr = (p.get("address") or o["address"]).strip()
    g1 = geocode(addr)
    if g1["ok"]:
        addr_out, lat, lng = g1["formatted"], g1["lat"], g1["lng"]
        miles, fee = quote(r, lat, lng)
    else:
        # dispatcher stands behind an address the map cannot place: keep it as typed
        addr_out, lat, lng = addr, o["lat"], o["lng"]
        miles, fee = o["miles"] or 0, o["fee_cents"]
    if p.get("fee_cents") not in (None, ""):
        fee = max(0, int(round(float(p["fee_cents"]))))
    total = o["subtotal_cents"] + fee + o["item_fee_cents"] + o["tax_cents"] + (o["service_cents"] or 0) + o["tip_cents"]
    kitchen = "pending" if o["kitchen_status"] == "waiting" else o["kitchen_status"]
    hold = "waiting on kitchen" if o["hold_reason"] == "address needs dispatch approval" else o["hold_reason"]
    db().execute("""UPDATE orders SET address=?, lat=?, lng=?, miles=?, fee_cents=?, total_cents=?,
                    address_ok=1, kitchen_status=?, hold_reason=? WHERE id=?""",
                 (addr_out, lat, lng, miles, fee, total, kitchen, hold, o["id"]))
    db().commit()
    log("order", o["code"] + " address approved by dispatch (" + addr_out + ")")
    auto_assign()
    return jsonify({"ok": True, "address": addr_out, "miles": miles,
                    "fee": money(fee), "total": money(total), "verified": bool(g1["ok"])})

@app.route("/track/<code>")
def track(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return render_template("track.html", order=None, code=code)
    return render_template("track.html", order=order_dict(o), code=code)

@app.get("/api/track/<code>")
def api_track(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "order": order_dict(o)})

# ---------------------------------------------------------------- dispatcher

def dispatcher_required():
    return session.get("dispatcher_id") is not None

@app.route("/dispatch/login", methods=["GET", "POST"])
def dispatch_login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        row = db().execute("SELECT * FROM dispatchers WHERE username=? AND password=?", (u, p)).fetchone()
        if row:
            session["dispatcher_id"] = row["id"]
            session["dispatcher_name"] = row["name"]
            return redirect(url_for("dispatch"))
        err = "Wrong username or password."
    return render_template("dispatch_login.html", err=err)

@app.route("/dispatch/logout")
def dispatch_logout():
    session.pop("dispatcher_id", None)
    return redirect(url_for("dispatch_login"))

@app.route("/dispatch")
def dispatch():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch.html")


def status_label(d):
    """Shift status on the driver card."""
    return {"online": "available", "break": "on break",
            "offline": "offline"}.get(d["status"], d["status"])


def roster_label(d):
    """Group tag beside the status. Working while not on today's schedule reads
    as an unscheduled driver, so the card shows available AND unscheduled."""
    grp = driver_group(d)
    if d["status"] != "offline" and grp == "unavailable":
        return "unscheduled driver"
    if grp == "unavailable" and d["roster"] == "scheduled":
        return "not scheduled today"
    return grp

@app.get("/api/clock")
def api_clock():
    """Quick check that the server is on Central time."""
    return jsonify({"ok": True, "now": now(), "tz": APP_TZ,
                    "abbr": time.strftime("%Z"), "utc_offset": time.strftime("%z")})

@app.get("/api/dispatch/board")
def api_board():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    auto_assign()   # safety net: anything an earlier event missed is placed on the next refresh
    purge_cards()
    try:
        purge_old_orders()
    except Exception:
        pass
    live = db().execute("""SELECT * FROM orders WHERE dispatch_status NOT IN ('delivered','cancelled','scheduled')
                           ORDER BY created_at ASC""").fetchall()
    future_count = db().execute("SELECT COUNT(*) c FROM orders WHERE dispatch_status='scheduled'").fetchone()["c"]
    done_day = (request.args.get("done_day") or dt.date.today().isoformat())[:10]
    done = db().execute("""SELECT * FROM orders WHERE dispatch_status IN ('delivered','cancelled')
                           AND substr(COALESCE(delivered_at, created_at),1,10)=?
                           ORDER BY COALESCE(delivered_at, created_at) DESC LIMIT 500""", (done_day,)).fetchall()
    drivers = db().execute("""SELECT d.*, (SELECT COUNT(*) FROM orders o WHERE o.driver_id=d.id
                              AND o.dispatch_status IN ('assigned','received','at_restaurant','enroute')) load
                              FROM drivers d ORDER BY d.name""").fetchall()
    rotation = {d["id"]: i + 1 for i, d in enumerate(available_drivers())}
    unread = {r["driver_id"]: r["c"] for r in db().execute(
        """SELECT driver_id, COUNT(*) c FROM messages
           WHERE sender='driver' AND seen_by_dispatch=0 GROUP BY driver_id""").fetchall()}
    newest = db().execute(
        """SELECT m.id, m.driver_id, m.body, m.created_at, d.name FROM messages m
           JOIN drivers d ON d.id=m.driver_id
           WHERE m.sender='driver' AND m.seen_by_dispatch=0
           ORDER BY m.id DESC LIMIT 1""").fetchone()
    rests = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    return jsonify({
        "ok": True,
        "auto": bool(setting("auto_assign")),
        "tokens": token_list(),
        "alerts": open_call_alerts(),
        "awaiting": awaiting_accept(),
        "business_open": business_is_open(),
        "future_count": future_count,
        "late": late_accepts(),
        "rest_chat_unread": rest_chat_unread_for_dispatch()[0],
        "rest_chat_latest": rest_chat_unread_for_dispatch()[1],
        "orders": [order_dict(o) for o in live],
        "completed": [order_dict(o) for o in done],
        "done_day": done_day,
        "chat_unread": sum(unread.values()),
        "chat_latest": ({"id": newest["id"], "driver_id": newest["driver_id"],
                         "driver": newest["name"], "body": newest["body"],
                         "at": newest["created_at"][11:16]} if newest else None),
        "drivers": [{"id": d["id"], "name": d["name"], "phone": d["phone"], "status": d["status"],
                     "pending_request": d["pending_request"], "load": d["load"],
                     "unread": unread.get(d["id"], 0),
                     "max_stack": d["max_stack"], "up_next": rotation.get(d["id"]),
                     "roster": d["roster"], "group": driver_group(d),
                     "today_shift": ", ".join(scheduled_today(d["id"])),
                     "availability": availability_for(d["id"]), "location": loc_block(d),
                     "status_label": status_label(d), "roster_label": roster_label(d),
                     "on_orders": [{"id": x["id"], "code": x["code"], "stage": x["dispatch_status"],
                                    "restaurant": x["restaurant"], "customer": x["customer"],
                                    "stop": x["stack_seq"]}
                                   for x in [order_dict(y) for y in db().execute(
                                       """SELECT * FROM orders WHERE driver_id=? AND dispatch_status
                                          IN ('assigned','received','at_restaurant','enroute')
                                          ORDER BY stack_seq ASC""", (d["id"],)).fetchall()]]}
                    for d in drivers],
        "restaurants": [{"id": r["id"], "name": r["name"], "open": is_open(r),
                         "paused": bool(r["closed_override"]), "open_24": bool(r["open_24"]),
                         "hours": hours_label(r)}
                        for r in rests],
    })

@app.post("/api/dispatch/driver-status")
def api_driver_status():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    status = data["status"]
    if status not in ("online", "break", "offline"):
        return jsonify({"ok": False, "error": "bad status"}), 400
    set_driver_status(data["driver_id"], status, "Dispatch set you " + status + ".")
    return jsonify({"ok": True})

@app.post("/api/dispatch/pause-restaurant")
def api_pause_restaurant():
    """Pause or resume a kitchen straight from the board."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rid = request.get_json(force=True)["restaurant_id"]
    r = db().execute("SELECT closed_override FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"ok": False}), 404
    db().execute("UPDATE restaurants SET closed_override=? WHERE id=?",
                 (0 if r["closed_override"] else 1, rid))
    db().commit()
    return jsonify({"ok": True, "paused": not r["closed_override"]})


@app.post("/api/dispatch/assign")
def api_assign():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    oid, did = data["order_id"], data.get("driver_id")
    if did in (None, "", 0, "0"):
        db().execute("""UPDATE orders SET driver_id=NULL, stack_seq=NULL, dispatch_status='queued'
                        WHERE id=?""", (oid,))
    else:
        prev = db().execute("SELECT driver_id, dispatch_status FROM orders WHERE id=?",
                            (oid,)).fetchone()
        prev_did = prev["driver_id"] if prev else None
        if prev_did and str(prev_did) == str(did):
            return jsonify({"ok": True, "moved": False})
        seq = db().execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                              AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                           (did,)).fetchone()["s"]
        # a stop already picked up stays where it is: the food is in that driver's car
        if prev and prev["dispatch_status"] == "enroute":
            return jsonify({"ok": False,
                            "error": "That order is already picked up and en route. "
                                     "Complete it or send it back to the queue first."}), 400
        db().execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned', hold_reason=NULL,
                        stack_seq=? WHERE id=?""", (did, seq, oid))
        if prev_did and str(prev_did) != str(did):
            # close the gap in the old driver's run and tell them it left
            gone = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()["code"]
            rest = db().execute("""SELECT id FROM orders WHERE driver_id=?
                                   AND dispatch_status IN ('assigned','received','at_restaurant','enroute')
                                   ORDER BY stack_seq, id""", (prev_did,)).fetchall()
            for i, row in enumerate(rest, start=1):
                db().execute("UPDATE orders SET stack_seq=? WHERE id=?", (i, row["id"]))
            newname = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()["name"]
            db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                         (prev_did, "dispatch",
                          "Order " + gone + " moved off your run to " + newname + ".", now()))
        db().execute("UPDATE drivers SET last_assigned_at=? WHERE id=?", (now(), did))
        o = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (did, "dispatch", "You have order " + o["code"] + " (stop #" + str(seq) + ").", now()))
    db().commit()
    auto_assign()
    return jsonify({"ok": True})

@app.post("/api/order/status")
def api_order_status():
    data = request.get_json(force=True)
    oid = data["order_id"]
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    if session.get("driver_id") and not dispatcher_required() and not session.get("restaurant_id"):
        if o["driver_id"] != session["driver_id"]:
            return jsonify({"ok": False, "error": "not your order"}), 403
    k = data.get("kitchen_status")
    d = data.get("dispatch_status")
    if k in ("pending", "preparing", "ready") and o["dispatch_status"] == "awaiting_payment":
        return jsonify({"ok": False, "error": "Mark the card paid first. The restaurant gets this "
                        "order the moment it is paid."}), 400
    if k in ("pending", "preparing", "ready"):
        if k == "preparing":
            mins = int(data.get("prep_minutes") or 15)
            db().execute("UPDATE orders SET kitchen_status=?, prep_minutes=?, prep_started=? WHERE id=?",
                         (k, mins, now(), oid))
        elif k == "ready":
            db().execute("UPDATE orders SET kitchen_status=?, ready_at=? WHERE id=?", (k, now(), oid))
        else:
            db().execute("UPDATE orders SET kitchen_status=? WHERE id=?", (k, oid))
    if d in ("held", "queued", "assigned", "received", "at_restaurant", "enroute",
             "delivered", "cancelled"):
        db().execute("UPDATE orders SET dispatch_status=? WHERE id=?", (d, oid))
        if d in ("delivered", "cancelled"):
            db().execute("UPDATE orders SET stack_seq=NULL, delivered_at=? WHERE id=?", (now(), oid))
            if d == "delivered" and is_cash(o) and o["payment_status"] != "paid":
                db().execute("""UPDATE orders SET payment_status='paid', paid_at=?,
                                paid_cents=total_cents WHERE id=?""", (now(), oid))
            if o["driver_id"]:
                # finishing a run sends the driver to the back of the rotation
                db().execute("UPDATE drivers SET last_completed_at=? WHERE id=?", (now(), o["driver_id"]))
            if o["driver_id"]:
                db().execute("""INSERT INTO messages(driver_id,sender,body,created_at)
                                VALUES(?,?,?,?)""",
                             (o["driver_id"], "system",
                              "Order " + o["code"] + " marked " + d + ".", now()))
    db().commit()
    if d and o["driver_id"]:
        who = "Driver marked" if (session.get("driver_id") == o["driver_id"] and not dispatcher_required()) else "Dispatch marked"
        log_driver(o["driver_id"], who + " " + o["code"] + " " + STATUS_WORDS.get(("dispatch", d), d))
    auto_assign()
    return jsonify({"ok": True})

@app.post("/api/order/mark-paid")
def api_mark_paid():
    """Dispatch records a payment taken outside the site: cash the driver brought
    back, or a card typed into the card box and run on another terminal. Only the
    last four digits are ever sent here; the full number never leaves the browser."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    ref = (data.get("ref") or "").strip()
    if o["payment_status"] == "paid":
        bal = balance_cents(o)
        if bal > 0:
            last4 = "".join(ch for ch in str(data.get("last4") or "") if ch.isdigit())[-4:]
            add_extra_charge(o, bal, "Card ending " + last4 if last4 else "Recorded by dispatch", ref)
            return jsonify({"ok": True, "balance_recorded": money(bal)})
        return jsonify({"ok": True, "already": True})
    last4 = "".join(ch for ch in str(data.get("last4") or "") if ch.isdigit())[-4:]
    if is_cash(o):
        method = "cash"
    elif data.get("method") == "card_keyed" or last4:
        method = "card_keyed"
    else:
        method = "recorded"
    note = ("card ending " + last4 if last4 else "") + ((" ref " + ref) if ref else "")
    mark_paid(o, method=method, ref=note.strip()[:80])
    return jsonify({"ok": True})

@app.post("/api/order/cash")
def api_order_cash():
    """Switch an order between cash and card. Dispatch only."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    if o["payment_status"] == "paid" and not is_cash(o):
        return jsonify({"ok": False, "error": "That order is already paid by card."}), 400
    if data.get("cash"):
        db().execute("UPDATE orders SET pay_method='cash', payment_status='cash_due' WHERE id=?", (o["id"],))
        if o["dispatch_status"] == "awaiting_payment":
            kitchen = "pending" if o["address_ok"] else "waiting"
            db().execute("""UPDATE orders SET kitchen_status=?, dispatch_status='held',
                            hold_reason='waiting on kitchen' WHERE id=?""", (kitchen, o["id"]))
        log("payment", o["code"] + " switched to cash by dispatch")
    else:
        db().execute("UPDATE orders SET pay_method=NULL, payment_status='unpaid' WHERE id=?", (o["id"],))
        if o["kitchen_status"] in ("pending", "waiting") and o["dispatch_status"] in ("held", "queued"):
            # Kitchen has not started it: pull it back until the card is run.
            db().execute("""UPDATE orders SET kitchen_status='waiting', dispatch_status='awaiting_payment',
                            hold_reason='waiting on card', driver_id=NULL, stack_seq=NULL WHERE id=?""",
                         (o["id"],))
        log("payment", o["code"] + " switched back to card by dispatch")
    db().commit()
    auto_assign()
    return jsonify({"ok": True})

@app.post("/api/order/refund")
def api_refund():
    """Full or partial refund, dispatcher only. cents blank = everything collected."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    paid = int(o["paid_cents"] or 0)
    already = int(o["refunded_cents"] or 0)
    left = paid - already
    if left <= 0:
        return jsonify({"ok": False, "error": "Nothing collected on that order is left to refund."}), 400
    raw = data.get("cents")
    cents = left if raw in (None, "", "all") else int(round(float(raw)))
    if cents <= 0:
        return jsonify({"ok": False, "error": "Enter an amount to refund."}), 400
    if cents > left:
        return jsonify({"ok": False, "error": "That is more than is left to refund (" + money(left) + ")."}), 400
    note = (data.get("note") or "").strip()
    rid = "cash" if is_cash(o) else ""
    if not is_cash(o):
        # refunded on whatever card terminal took the payment; we just record it
        rid = (data.get("ref") or "recorded").strip()[:60]
    total_ref = already + cents
    db().execute("""UPDATE orders SET refunded_cents=?, refund_id=?, refund_note=?,
                    payment_status=? WHERE id=?""",
                 (total_ref, rid, note,
                  "refunded" if total_ref >= paid else "part_refunded", o["id"]))
    cancelled = total_ref >= paid and o["dispatch_status"] != "cancelled"
    if cancelled:
        db().execute("""UPDATE orders SET dispatch_status='cancelled', stack_seq=NULL,
                        hold_reason='refunded in full', delivered_at=COALESCE(delivered_at, ?)
                        WHERE id=?""", (now(), o["id"]))
        if o["driver_id"] and o["dispatch_status"] not in ("delivered",):
            db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                         (o["driver_id"], "system",
                          "Order " + o["code"] + " was refunded in full and cancelled. Do not pick it up.", now()))
        db().execute("DELETE FROM card_vault WHERE order_id=?", (o["id"],))
    db().commit()
    if cancelled:
        log("cancel", o["code"] + " cancelled after a full refund")
        auto_assign()
    who = dispatcher_row()
    log("refund", (who["name"] if who else "dispatch") + " refunded " + money(cents) +
        " on " + o["code"] + (": " + note if note else ""))
    return jsonify({"ok": True, "refunded": money(total_ref), "left": money(paid - total_ref),
                    "refund_id": rid, "cancelled": cancelled})

@app.post("/api/driver/tip-sign")
def api_tip_sign():
    """Customer adds a tip at the door and signs for it on the driver's phone."""
    did = session.get("driver_id")
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    if not dispatcher_required():
        if not did or o["driver_id"] != did:
            return jsonify({"ok": False, "error": "not your order"}), 403
    if data.get("declined"):
        db().execute("UPDATE orders SET tip_declined=1 WHERE id=?", (o["id"],))
        db().commit()
        return jsonify({"ok": True, "tip": money(o["tip_cents"]), "declined": True})
    cents = int(round(float(data.get("cents") or 0)))
    if cents <= 0:
        return jsonify({"ok": False, "error": "Enter a tip amount."}), 400
    sig = (data.get("signature") or "").strip()
    if not sig.startswith("data:image"):
        return jsonify({"ok": False, "error": "Have the customer sign before you save."}), 400
    if o["tip_charge_id"]:
        return jsonify({"ok": False, "error": "A tip was already signed for on this order."}), 400
    folder = os.path.join(APP_DIR, "static", "signatures")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, o["code"] + ".png")
    try:
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(sig.split(",", 1)[1]))
    except Exception:
        return jsonify({"ok": False, "error": "That signature did not save."}), 400
    charge = ""
    db().execute("""UPDATE orders SET tip_cents=tip_cents+?, total_cents=total_cents+?,
                    tip_sig=?, tip_signed_at=?, tip_charge_id=?, tip_declined=0 WHERE id=?""",
                 (cents, cents, "/static/signatures/" + o["code"] + ".png", now(),
                  charge or "signed", o["id"]))
    db().commit()
    log("tip", money(cents) + " tip signed at the door on " + o["code"])
    return jsonify({"ok": True, "tip": money(int(o["tip_cents"]) + cents),
                    "charged": bool(charge)})

@app.post("/api/order/timer")
def api_timer():
    """Set or nudge the kitchen timer. minutes = absolute, delta = +/- from what is running."""
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    current = o["prep_minutes"] or 15
    if data.get("minutes") not in (None, ""):
        mins = int(float(data["minutes"]))
    else:
        mins = current + int(data.get("delta", 0))
    mins = max(1, min(180, mins))
    # Putting time back on the clock means the food is not ready after all:
    # a ready order drops back to preparing and the ready stamp is cleared,
    # so the board and the kitchen both stop showing "Ready".
    if o["kitchen_status"] == "waiting":
        # Unpaid card or unapproved address: the kitchen has not been sent this order,
        # so just remember the minutes. The clock starts when the kitchen gets it.
        db().execute("UPDATE orders SET prep_minutes=? WHERE id=?", (mins, o["id"]))
        db().commit()
        recompute_queue()
        return jsonify({"ok": True, "prep_minutes": mins, "timer_seconds": None})
    restart = o["kitchen_status"] == "ready"
    started = now() if restart else (o["prep_started"] or now())
    db().execute("""UPDATE orders SET prep_minutes=?, prep_started=?, kitchen_status='preparing',
                    ready_at=NULL WHERE id=?""", (mins, started, o["id"]))

    db().commit()
    auto_assign()
    fresh = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    return jsonify({"ok": True, "prep_minutes": mins,
                    "timer_seconds": order_dict(fresh)["timer_seconds"]})


@app.post("/api/order/edit")
def api_order_edit():
    """Dispatcher edits: line items, delivery fee, tip. Totals are recomputed."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    if "ref" in data:
        db().execute("UPDATE orders SET ref_code=? WHERE id=?", (clean_ref(data.get("ref")), o["id"]))
    if "token" in data:
        db().execute("UPDATE orders SET token=? WHERE id=?", (clean_token(data.get("token")), o["id"]))
    items = data.get("items")
    if items is None:
        items = json.loads(o["items"])
    items = clean_items(items)
    subtotal = sum(int(i["price_cents"]) * int(i["qty"]) for i in items)
    ifee = item_fees(items)
    fee = o["fee_cents"] if data.get("fee_cents") in (None, "") else int(round(float(data["fee_cents"])))
    tip = o["tip_cents"] if data.get("tip_cents") in (None, "") else int(round(float(data["tip_cents"])))
    fee, tip = max(0, fee), max(0, tip)
    tax = int(round(subtotal * setting("tax_rate_bp") / 10000.0))
    service = int(round(subtotal * setting("service_fee_bp") / 10000.0))
    total = subtotal + fee + ifee + tax + service + tip
    db().execute("""UPDATE orders SET items=?, subtotal_cents=?, fee_cents=?, item_fee_cents=?,
                    tax_cents=?, service_cents=?, tip_cents=?, total_cents=? WHERE id=?""",
                 (json.dumps(items), subtotal, fee, ifee, tax, service, tip, total, o["id"]))
    db().commit()
    log("edit", o["code"] + " edited by dispatch")
    dupe = ref_in_use(clean_ref(data.get("ref")), o["id"]) if "ref" in data else None
    return jsonify({"ok": True, "dupe": dupe, "subtotal": money(subtotal), "fee": money(fee),
                    "item_fee": money(ifee), "tax": money(tax),
                    "service": money(service), "tip": money(tip), "total": money(total)})


@app.post("/api/dispatch/block")
def api_block():
    """Block or unblock a customer number from ordering on the website."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    phone = "".join(ch for ch in str(data.get("phone", "")) if ch.isdigit())
    if not phone:
        return jsonify({"ok": False, "error": "Need a phone number."}), 400
    row = db().execute("SELECT * FROM blocked_customers WHERE phone=?", (phone,)).fetchone()
    if row:
        db().execute("DELETE FROM blocked_customers WHERE phone=?", (phone,))
        db().commit()
        return jsonify({"ok": True, "blocked": False})
    db().execute("""INSERT INTO blocked_customers(phone,name,reason,created_at)
                    VALUES(?,?,?,?)""",
                 (phone, data.get("name", ""), data.get("reason", ""), now()))
    db().commit()
    log("block", "blocked " + phone)
    return jsonify({"ok": True, "blocked": True})


@app.get("/api/dispatch/blocked")
def api_blocked():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("SELECT * FROM blocked_customers ORDER BY created_at DESC").fetchall()
    return jsonify({"ok": True, "blocked": [{"phone": r["phone"], "name": r["name"],
                                             "reason": r["reason"]} for r in rows]})


@app.get("/dispatch/new-order")
def dispatch_new_order():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    rests = db().execute("SELECT * FROM restaurants ORDER BY name").fetchall()
    menus = {}
    for r in rests:
        menus[r["id"]] = [dict(m) for m in db().execute(
            "SELECT * FROM menu_items WHERE restaurant_id=? AND active=1 ORDER BY name",
            (r["id"],)).fetchall()]
    src = None
    fid = request.args.get("from")
    if fid:
        o = db().execute("SELECT * FROM orders WHERE id=?", (fid,)).fetchone()
        if o:
            src = {"id": o["id"], "code": o["code"], "restaurant_id": o["restaurant_id"],
                   "customer_name": o["customer_name"], "customer_phone": o["customer_phone"],
                   "address": o["address"], "address_note": o["address_note"] or "",
                   "dispatch_note": o["dispatch_note"] or "",
                   "items": json.loads(o["items"]), "tip_cents": o["tip_cents"],
                   "fee_cents": o["fee_cents"], "driver_id": o["driver_id"],
                   "driver": (db().execute("SELECT name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() or {"name": ""})["name"] if o["driver_id"] else ""}
    oneoff = oneoff_id()
    return render_template("dispatch_new_order.html",
                           restaurants=[dict(r) for r in rests if r["slug"] != "oneoff"],
                           menus=menus, src=src, oneoff=oneoff, tokens=token_list(),
                           reasons=[{"key": k, "label": v} for k, v in REDO_REASONS.items()])


# ---------------------------------------------------------------- roster / groups

ROSTERS = ("scheduled", "unavailable")

@app.post("/api/order/note")
def api_order_note():
    """Any order can carry a dispatch note. Drivers and the kitchen both see it."""
    data = request.get_json(force=True)
    oid = data["order_id"]
    note = (data.get("note") or "").strip()
    o = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    db().execute("UPDATE orders SET dispatch_note=? WHERE id=?", (note or None, oid))
    did = db().execute("SELECT driver_id FROM orders WHERE id=?", (oid,)).fetchone()["driver_id"]
    if did and note:
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (did, "dispatch", "Note on " + o["code"] + ": " + note, now()))
        db().execute("UPDATE messages SET sender_name=?, dispatcher_id=? WHERE id=last_insert_rowid()",
                     (session.get("dispatcher_name"), session.get("dispatcher_id")))
    log("note", o["code"] + " " + note[:80])
    db().commit()
    return jsonify({"ok": True, "note": note})

def clean_items(raw):
    """Normalise a cart. A dispatcher can add a custom line that is not on the menu:
    it carries its own name, price and an optional fee charged on top of the food price."""
    out = []
    for i in raw or []:
        qty = int(i.get("qty", 1))
        if qty <= 0:
            continue
        name = (i.get("name") or "Custom item").strip()[:80]
        line = {"menu_item_id": i.get("menu_item_id"), "name": name, "qty": qty,
                "price_cents": max(0, int(round(float(i.get("price_cents", 0))))),
                "fee_cents": max(0, int(round(float(i.get("fee_cents", 0) or 0)))),
                "custom": bool(i.get("custom") or not i.get("menu_item_id")),
                "note": (i.get("note") or "")[:120]}
        out.append(line)
    return out


def item_fees(items):
    return sum(int(i.get("fee_cents", 0) or 0) * int(i["qty"]) for i in items)


SOURCES = {"website": "Online", "call_in": "Call-in", "dispatch_online": "Dispatch online"}

REDO_REASONS = {
    "missing_item": "Restaurant left an item off",
    "wrong_item": "Restaurant made the wrong item",
    "food_quality": "Food was cold or wrong temperature",
    "wrong_address": "Driver delivered to the wrong address",
    "never_delivered": "Order never reached the customer",
    "damaged": "Order was damaged in transit",
    "late": "Order was too late",
    "other": "Other",
}

@app.get("/api/redo-reasons")
def api_redo_reasons():
    return jsonify({"ok": True, "reasons": [{"key": k, "label": v} for k, v in REDO_REASONS.items()]})


def dispatcher_row():
    did = session.get("dispatcher_id")
    return db().execute("SELECT * FROM dispatchers WHERE id=?", (did,)).fetchone() if did else None

@app.get("/api/dispatch/users")
def api_dispatch_users():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("SELECT * FROM dispatchers ORDER BY name").fetchall()
    return jsonify({"ok": True, "me": session.get("dispatcher_id"),
                    "users": [{"id": r["id"], "name": r["name"], "username": r["username"],
                               "created_at": (r["created_at"] or "")[:10]} for r in rows]})

@app.post("/api/dispatch/user")
def api_dispatch_user_save():
    """Create a dispatcher, or edit any dispatcher's name, username and password."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    uid = data.get("id")
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not name or not username:
        return jsonify({"ok": False, "error": "Name and username are both required."}), 400
    clash = db().execute("SELECT id FROM dispatchers WHERE username=? AND id IS NOT ?",
                         (username, uid)).fetchone()
    if clash:
        return jsonify({"ok": False, "error": "That username is already taken."}), 400
    if uid:
        if password:
            db().execute("UPDATE dispatchers SET name=?,username=?,password=? WHERE id=?",
                         (name, username, password, uid))
        else:
            db().execute("UPDATE dispatchers SET name=?,username=? WHERE id=?", (name, username, uid))
        if uid == session.get("dispatcher_id"):
            session["dispatcher_name"] = name
        log("dispatcher", "updated " + username)
    else:
        if len(password) < 4:
            return jsonify({"ok": False, "error": "Give the new dispatcher a password of at least 4 characters."}), 400
        cur = db().execute("INSERT INTO dispatchers(name,username,password,created_at) VALUES(?,?,?,?)",
                           (name, username, password, now()))
        uid = cur.lastrowid
        log("dispatcher", "created " + username)
    db().commit()
    return jsonify({"ok": True, "id": uid, "name": name, "username": username})

@app.post("/api/dispatch/user-delete")
def api_dispatch_user_delete():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    uid = request.get_json(force=True)["id"]
    if uid == session.get("dispatcher_id"):
        return jsonify({"ok": False, "error": "You cannot remove the account you are signed in with."}), 400
    if db().execute("SELECT COUNT(*) c FROM dispatchers").fetchone()["c"] <= 1:
        return jsonify({"ok": False, "error": "Keep at least one dispatcher account."}), 400
    db().execute("DELETE FROM dispatchers WHERE id=?", (uid,))
    log("dispatcher", "removed id " + str(uid))
    db().commit()
    return jsonify({"ok": True})

@app.get("/api/dispatch/staff-chat")
def api_staff_chat():
    """The dispatcher room: every dispatcher on duty sees this thread."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("""SELECT * FROM messages WHERE driver_id=0 ORDER BY id DESC LIMIT 60""").fetchall()
    return jsonify({"ok": True, "me": session.get("dispatcher_name"),
                    "messages": [{"sender": r["sender_name"] or r["sender"], "body": r["body"],
                                  "mine": r["dispatcher_id"] == session.get("dispatcher_id"),
                                  "at": r["created_at"][11:16]} for r in reversed(rows)]})

@app.post("/api/dispatch/staff-chat")
def api_staff_chat_send():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    body = (request.get_json(force=True).get("body") or "").strip()
    if not body:
        return jsonify({"ok": False}), 400
    db().execute("""INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at)
                    VALUES(0,'dispatch',?,?,?,?)""",
                 (session.get("dispatcher_name"), session.get("dispatcher_id"), body, now()))
    db().commit()
    return jsonify({"ok": True})

@app.route("/dispatch/account", methods=["GET"])
def dispatch_account():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_account.html", me=dispatcher_row())

import threading as _threading
_REV_LOCK = _threading.Lock()
_REV_LAST = [0.0]

def _short_addr(parts):
    """House number + street, city, state zip from an OpenStreetMap reverse lookup."""
    a = parts or {}
    street = " ".join(x for x in [a.get("house_number"), a.get("road") or a.get("pedestrian")
                                  or a.get("footway") or a.get("parking")] if x)
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county") or ""
    state = a.get("state") or ""
    if state == "Alabama":
        state = "AL"
    tail = " ".join(x for x in [state, a.get("postcode")] if x)
    return ", ".join(x for x in [street, city, tail] if x)

def nearest_address(lat, lng):
    """Closest street address to a GPS fix. Cached per ~10 metres so a parked
    driver costs one lookup. Uses Google when GOOGLE_MAPS_API_KEY is set,
    otherwise OpenStreetMap (kept to one lookup a second, which their rules ask for)."""
    if lat is None or lng is None:
        return None
    k = "%.4f,%.4f" % (lat, lng)
    row = db().execute("SELECT address FROM revgeo WHERE k=?", (k,)).fetchone()
    if row and row["address"]:
        return row["address"]
    addr = None
    try:
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?latlng=%f,%f&key=%s"
                   % (lat, lng, urllib.parse.quote(GOOGLE_KEY)))
            with urllib.request.urlopen(url, timeout=4) as resp:
                js = json.loads(resp.read().decode("utf-8"))
            if js.get("results"):
                addr = js["results"][0].get("formatted_address", "").replace(", USA", "")
        else:
            with _REV_LOCK:
                wait = 1.1 - (time.time() - _REV_LAST[0])
                if wait > 0:
                    time.sleep(wait)
                _REV_LAST[0] = time.time()
            url = ("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&addressdetails=1"
                   "&lat=%f&lon=%f" % (lat, lng))
            req = urllib.request.Request(url, headers={"User-Agent": "fleetdelivery/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                js = json.loads(resp.read().decode("utf-8"))
            addr = _short_addr(js.get("address")) or js.get("display_name")
    except Exception:
        addr = None
    if addr:
        db().execute("INSERT OR REPLACE INTO revgeo(k,address,created_at) VALUES(?,?,?)", (k, addr, now()))
        db().commit()
    return addr

def update_driver_addr(did, lat, lng, force=False):
    """Refresh the driver's closest address when they have moved about 30 metres."""
    d = db().execute("SELECT last_addr,last_addr_lat,last_addr_lng FROM drivers WHERE id=?", (did,)).fetchone()
    if d and d["last_addr"] and not force and d["last_addr_lat"] is not None:
        moved = miles_between(d["last_addr_lat"], d["last_addr_lng"], lat, lng) or 0
        if moved < 0.025:
            return d["last_addr"]
    addr = nearest_address(lat, lng)
    if addr:
        db().execute("UPDATE drivers SET last_addr=?,last_addr_lat=?,last_addr_lng=? WHERE id=?",
                     (addr, lat, lng, did))
        db().commit()
        return addr
    return d["last_addr"] if d else None

GPS_LOG_KEEP_DAYS = int(os.environ.get("GPS_LOG_KEEP_DAYS", "90"))

def log_driver(did, event, lat=None, lng=None, address=None, status=None):
    """One row in the driver's GPS and status history. Status and order events use the
    last known position when the phone did not send one with the event."""
    try:
        d = db().execute("SELECT status,last_lat,last_lng,last_addr FROM drivers WHERE id=?", (did,)).fetchone()
        if not d:
            return
        if lat is None:
            lat, lng = d["last_lat"], d["last_lng"]
        if address is None:
            address = d["last_addr"]
        db().execute("""INSERT INTO driver_log(driver_id,lat,lng,address,status,event,created_at)
                        VALUES(?,?,?,?,?,?,?)""", (did, lat, lng, address, status or d["status"], event, now()))
        db().commit()
    except Exception as e:
        print("driver log skipped:", e)

def log_gps_fix(did, lat, lng, addr):
    """GPS rows every 60 seconds, or sooner after a move of about 250 feet."""
    last = db().execute("""SELECT lat,lng,created_at FROM driver_log WHERE driver_id=? AND event='gps'
                           ORDER BY id DESC LIMIT 1""", (did,)).fetchone()
    if last and last["lat"] is not None:
        try:
            age = (dt.datetime.now() - dt.datetime.fromisoformat(last["created_at"])).total_seconds()
        except ValueError:
            age = 999
        moved = miles_between(last["lat"], last["lng"], lat, lng) or 0
        if age < 60 and moved < 0.05:
            return
    log_driver(did, "gps", lat, lng, addr)

def purge_driver_log():
    cut = (dt.datetime.now() - dt.timedelta(days=GPS_LOG_KEEP_DAYS)).isoformat(timespec="seconds")
    db().execute("DELETE FROM driver_log WHERE created_at < ?", (cut,))
    db().commit()

def driver_has_open_call(did):
    return bool(db().execute("SELECT 1 FROM call_alerts WHERE driver_id=? AND who='driver' AND cleared_at IS NULL",
                             (did,)).fetchone())

def loc_block(d):
    """Where a driver was the last time their phone checked in.
    Only tracked while they are online or on break, never off shift."""
    if d["status"] == "offline" or d["last_lat"] is None or d["last_lng"] is None:
        return None
    ago = None
    try:
        ago = int((dt.datetime.now() - dt.datetime.fromisoformat(d["last_loc_at"])).total_seconds() // 60)
    except Exception:
        pass
    ll = str(round(d["last_lat"], 6)) + "," + str(round(d["last_lng"], 6))
    return {"lat": d["last_lat"], "lng": d["last_lng"], "minutes_ago": ago,
            "stale": (ago is None or ago > 10),
            "address": (d["last_addr"] if "last_addr" in d.keys() else None),
            "map_url": "https://www.google.com/maps/search/?api=1&query=" + ll,
            "nav_url": "https://www.google.com/maps/dir/?api=1&destination=" + ll}

def miles_between(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(R * 2 * math.asin(math.sqrt(a)) * 1.3, 1)

@app.post("/api/driver/ping")
def api_driver_ping():
    """The driver app posts a GPS fix every 30 seconds while the driver is signed in."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    row = db().execute("SELECT status FROM drivers WHERE id=?", (did,)).fetchone()
    if not row or row["status"] == "offline":
        # off shift is off the map: drop whatever was there and tell the app to stop
        db().execute("UPDATE drivers SET last_lat=NULL,last_lng=NULL,last_loc_at=NULL,last_addr=NULL,last_addr_lat=NULL,last_addr_lng=NULL WHERE id=?",
                     (did,))
        db().commit()
        return jsonify({"ok": True, "tracking": False})
    data = request.get_json(force=True)
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad fix"}), 400
    db().execute("UPDATE drivers SET last_lat=?,last_lng=?,last_loc_at=? WHERE id=?",
                 (lat, lng, now(), did))
    db().commit()
    addr = update_driver_addr(did, lat, lng)
    log_gps_fix(did, lat, lng, addr)
    # while the driver's call is open on the board, the phone checks in every 8 seconds
    return jsonify({"ok": True, "tracking": True, "address": addr, "fast": driver_has_open_call(did)})

@app.get("/api/dispatch/locations")
def api_locations():
    """Every driver's last known spot, with how far they are from their next stop."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    out = []
    for d in db().execute("SELECT * FROM drivers ORDER BY name").fetchall():
        loc = loc_block(d)
        nxt = db().execute("""SELECT o.*, r.name rname, r.lat rlat, r.lng rlng FROM orders o
                              JOIN restaurants r ON r.id=o.restaurant_id
                              WHERE o.driver_id=? AND o.dispatch_status IN
                                ('assigned','received','at_restaurant','enroute')
                              ORDER BY o.stack_seq LIMIT 1""", (d["id"],)).fetchone()
        stop = None
        if nxt:
            heading_to_customer = nxt["dispatch_status"] == "enroute"
            tlat = nxt["lat"] if heading_to_customer else nxt["rlat"]
            tlng = nxt["lng"] if heading_to_customer else nxt["rlng"]
            stop = {"code": nxt["code"],
                    "target": nxt["customer_name"] if heading_to_customer else nxt["rname"],
                    "where": nxt["address"] if heading_to_customer else nxt["rname"],
                    "miles_away": miles_between(d["last_lat"], d["last_lng"], tlat, tlng)
                                  if loc else None}
        out.append({"id": d["id"], "name": d["name"], "phone": d["phone"], "status": d["status"],
                    "roster": d["roster"], "location": loc, "next_stop": stop})
    return jsonify({"ok": True, "drivers": out})

@app.get("/dispatch/map")
def dispatch_map():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_map.html")

@app.route("/restaurant/account")
def rest_account():
    if not session.get("restaurant_id"):
        return redirect(url_for("rest_login"))
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (session["restaurant_id"],)).fetchone()
    return render_template("rest_account.html", r=r)

@app.post("/api/restaurant/account")
def api_rest_account():
    """The store changes its own login: display name, store code and PIN."""
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    name = (b.get("name") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "", (b.get("slug") or "").lower())[:24]
    pin = (b.get("pin") or "").strip()
    phone = (b.get("phone") or "").strip()
    if not name or not slug:
        return jsonify({"ok": False, "error": "Store name and store code are both required."}), 400
    if pin and len(pin) < 4:
        return jsonify({"ok": False, "error": "Use a PIN of at least 4 digits."}), 400
    clash = db().execute("SELECT id FROM restaurants WHERE slug=? AND id IS NOT ?", (slug, rid)).fetchone()
    if clash:
        return jsonify({"ok": False, "error": "Another store already uses that code."}), 400
    db().execute("UPDATE restaurants SET name=?, slug=?, phone=COALESCE(?,phone) WHERE id=?",
                 (name, slug, phone or None, rid))
    if pin:
        db().execute("UPDATE restaurants SET pin=? WHERE id=?", (pin, rid))
    session["restaurant_name"] = name
    log("restaurant", "account updated " + slug)
    db().commit()
    return jsonify({"ok": True, "name": name, "slug": slug})

@app.post("/api/dispatch/driver-roster")
def api_driver_roster():
    """Move a driver between scheduled and unavailable."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    roster = data.get("roster")
    if roster not in ROSTERS:
        return jsonify({"ok": False, "error": "unknown group"}), 400
    did = data["driver_id"]
    db().execute("UPDATE drivers SET roster=?, roster_day=? WHERE id=?",
                 (roster, dt.date.today().isoformat() if roster == "scheduled" else None, did))
    if roster == "unavailable":
        load = db().execute("""SELECT COUNT(*) c FROM orders WHERE driver_id=?
                               AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                            (did,)).fetchone()["c"]
        if load:
            return jsonify({"ok": False,
                            "error": "That driver still has " + str(load) + " live order(s)."}), 400
        db().execute("UPDATE drivers SET status='offline', online_since=NULL WHERE id=?", (did,))
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (did, "dispatch", "Dispatch moved you to " + roster.replace("_", " ") + ".", now()))
    db().commit()
    auto_assign()
    return jsonify({"ok": True})

@app.post("/api/dispatch/broadcast")
def api_broadcast():
    """Mass text a group: scheduled, unavailable, active, all, or picked ids."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "Write a message first."}), 400
    audience = data.get("audience", "all")
    if audience == "ids":
        ids = [int(x) for x in data.get("driver_ids", [])]
        rows = db().execute("SELECT * FROM drivers WHERE id IN (%s)" %
                            ",".join("?" * len(ids)), ids).fetchall() if ids else []
    elif audience == "active":
        rows = db().execute("SELECT * FROM drivers WHERE status='online'").fetchall()
    elif audience in ROSTERS:
        rows = [r for r in db().execute("SELECT * FROM drivers").fetchall()
                if driver_group(r) == audience]
    else:
        audience = "all"
        rows = db().execute("SELECT * FROM drivers").fetchall()
    for d in rows:
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (d["id"], "dispatch", body, now()))
        send_text(d["phone"], body)
    db().execute("INSERT INTO broadcasts(audience,body,sent_to,created_at) VALUES(?,?,?,?)",
                 (audience, body, len(rows), now()))
    log("broadcast", audience + " x" + str(len(rows)))
    db().commit()
    return jsonify({"ok": True, "sent_to": len(rows),
                    "names": [d["name"] for d in rows]})

@app.get("/api/dispatch/broadcasts")
def api_broadcast_log():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rows = db().execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 15").fetchall()
    return jsonify({"ok": True, "sent": [{"audience": r["audience"], "body": r["body"],
                                          "sent_to": r["sent_to"],
                                          "at": r["created_at"][11:16]} for r in rows]})

def send_text(phone, body):
    """Real SMS hook. Set TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM and the same message that
    lands in the driver chat also goes out as a text. Without them it stays in-app only."""
    sid = os.environ.get("TWILIO_SID")
    token = os.environ.get("TWILIO_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    if not (sid and token and frm):
        return False
    try:
        url = "https://api.twilio.com/2010-04-01/Accounts/" + sid + "/Messages.json"
        payload = urllib.parse.urlencode({"To": phone, "From": frm, "Body": body}).encode()
        req = urllib.request.Request(url, data=payload)
        auth = base64.b64encode((sid + ":" + token).encode()).decode()
        req.add_header("Authorization", "Basic " + auth)
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception as exc:
        log("sms_error", str(exc)[:200])
        return False

# ---------------------------------------------------------------- availability

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def monday_of(d):
    """The Monday that starts the week holding date d."""
    return d - dt.timedelta(days=d.weekday())


def current_week_start():
    return monday_of(dt.date.today())


def open_week_start():
    """The week drivers are filling in: next week, live from Friday."""
    return current_week_start() + dt.timedelta(days=7)


def week_is_open(week_start):
    """True once Friday has come around and the deadline has not passed."""
    return dt.datetime.now() >= week_opens(week_start)


def ensure_open_week():
    """On Friday, stand up next week's blank schedule for every driver, and save off
    any week that has finished so it becomes a locked record."""
    ws = open_week_start()
    stamp = now()
    if week_is_open(ws):
        for d in db().execute("SELECT id FROM drivers").fetchall():
            db().execute("""INSERT OR IGNORE INTO week_submissions(driver_id,week_start,opened_at)
                            VALUES(?,?,?)""", (d["id"], ws.isoformat(), stamp))
    done = db().execute("""SELECT * FROM week_submissions
                           WHERE week_start < ? AND locked_at IS NULL""",
                        (current_week_start().isoformat(),)).fetchall()
    for r in done:
        db().execute("UPDATE week_submissions SET locked_at=? WHERE id=?", (stamp, r["id"]))
    if done:
        log("availability", str(len(done)) + " finished week(s) saved to the schedule history")
    db().commit()


def week_opens(week_start):
    """A week's calendar goes live on the day dispatch picked, the week before it starts.
    Default is Friday. A one-off date set by dispatch wins for that week."""
    once = setting("week_open_date", str) or ""
    if once:
        try:
            d = dt.date.fromisoformat(once)
            if monday_of(d) + dt.timedelta(days=7) == week_start:
                return dt.datetime.combine(d, dt.time(0, 0))
        except ValueError:
            pass
    dow = setting("week_open_dow") or 4
    dow = min(max(int(dow), 0), 6)
    return dt.datetime.combine(week_start - dt.timedelta(days=7 - dow), dt.time(0, 0))


def week_due(week_start):
    """Submissions close Sunday at midnight, the Sunday before the week begins."""
    sunday = week_start - dt.timedelta(days=1)
    return dt.datetime.combine(sunday, dt.time(23, 59, 59))


def pretty_day(d):
    return d.strftime("%a %b ") + str(d.day)


def _ordinal(n):
    return str(n) + ("th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))

def range_label(week_start):
    """Week as people say it: 9/28 - Oct 4th 2026."""
    end = week_start + dt.timedelta(days=6)
    return (str(week_start.month) + "/" + str(week_start.day) + " - " +
            end.strftime("%b") + " " + _ordinal(end.day) + " " + str(end.year))

def short_date(d):
    return str(d.month) + "/" + str(d.day)

def week_label(week_start):
    end = week_start + dt.timedelta(days=6)
    return pretty_day(week_start) + " to " + pretty_day(end)


def week_block(driver_id, week_start):
    """One driver's week: their submitted days, whether it is in, and the deadline."""
    ws = week_start.isoformat()
    rows = db().execute("""SELECT * FROM availability WHERE driver_id=? AND week_start=?
                           ORDER BY dow, start_time""", (driver_id, ws)).fetchall()
    sub = db().execute("SELECT * FROM week_submissions WHERE driver_id=? AND week_start=?",
                       (driver_id, ws)).fetchone()
    days = []
    for i in range(7):
        date = week_start + dt.timedelta(days=i)
        mine = [r for r in rows if r["dow"] == i]
        days.append({
            "dow": i, "day": DOW_NAMES[i], "date": date.isoformat(),
            "label": pretty_day(date), "off": not mine,
            "start": mine[0]["start_time"] if mine else "",
            "end": mine[0]["end_time"] if mine else "",
            "status": (mine[0]["status"] or "pending") if mine else "",
            "reply": (mine[0]["reply"] or "") if mine else "",
            "id": mine[0]["id"] if mine else None,
            "closed": bool(off_today(driver_id, date.isoformat())),
        })
    due = week_due(week_start)
    opens = week_opens(week_start)
    return {"week_start": ws, "label": week_label(week_start),
            "opens_at": opens.isoformat(timespec="minutes"),
            "opens_human": pretty_day(week_opens(week_start).date()),
            "window_open": dt.datetime.now() >= opens and dt.datetime.now() <= due,
            "not_yet": dt.datetime.now() < opens,
            "saved": bool(sub and sub["locked_at"]),
            "due_at": due.isoformat(timespec="minutes"),
            "due_human": pretty_day(week_start - dt.timedelta(days=1)) + " at midnight",
            "past_due": dt.datetime.now() > due,
            "locked": week_start < current_week_start(),
            "submitted": bool(sub and sub["submitted_at"]),
            "submitted_at": (sub["submitted_at"] if sub else "") or "",
            "hours": sum(1 for d in days if not d["off"]),
            "days": days}


def parse_week(raw):
    try:
        return monday_of(dt.date.fromisoformat((raw or "")[:10]))
    except Exception:
        return open_week_start()


def availability_for(driver_id, only_approved=False):
    q = "SELECT * FROM availability WHERE driver_id=?"
    if only_approved:
        q += " AND status='approved'"
    rows = db().execute(q + " ORDER BY week_start, dow, start_time", (driver_id,)).fetchall()
    return [{"id": r["id"], "dow": r["dow"], "day": DOW_NAMES[r["dow"]],
             "start": r["start_time"], "end": r["end_time"], "note": r["note"] or "",
             "status": r["status"] or "pending", "reply": r["reply"] or "",
             "decided_by": r["decided_by"] or "",
             "week_start": r["week_start"] or "",
             "date": ((dt.date.fromisoformat(r["week_start"]) + dt.timedelta(days=r["dow"])).isoformat()
                      if r["week_start"] else ""),
             "date_label": (short_date(dt.date.fromisoformat(r["week_start"]) + dt.timedelta(days=r["dow"]))
                            if r["week_start"] else "")}
            for r in rows]


def time_off_for(driver_id):
    rows = db().execute("""SELECT * FROM time_off WHERE driver_id=?
                           ORDER BY start_date DESC LIMIT 20""", (driver_id,)).fetchall()
    return [{"id": r["id"], "start": r["start_date"], "end": r["end_date"],
             "reason": r["reason"] or "", "status": r["status"],
             "reply": r["reply"] or "", "decided_by": r["decided_by"] or ""} for r in rows]


def off_today(driver_id, day=None):
    """True when approved time off covers the given date (default today)."""
    day = day or dt.date.today().isoformat()
    row = db().execute("""SELECT 1 FROM time_off WHERE driver_id=? AND status='approved'
                          AND start_date<=? AND end_date>=?""", (driver_id, day, day)).fetchone()
    return bool(row)



def scheduled_today(driver_id, day=None):
    """Today's approved shifts for this driver, like ['16:30-21:00']. Empty when
    they have nothing on today's schedule or have approved time off today."""
    day = day or dt.date.today()
    if off_today(driver_id, day.isoformat()):
        return []
    rows = db().execute("""SELECT start_time, end_time FROM availability
                           WHERE driver_id=? AND dow=? AND COALESCE(status,'approved')='approved'
                           AND week_start=?
                           ORDER BY start_time""",
                        (driver_id, day.weekday(), monday_of(day).isoformat())).fetchall()
    return [r["start_time"] + "-" + r["end_time"] for r in rows]


def driver_group(d):
    """Which roster tab a driver sits in. Scheduled means an approved shift today
    and not pulled by dispatch. Anyone not on today's schedule is Unavailable."""
    if d["roster"] == "unavailable":
        return "unavailable"
    # dispatch moved this driver to Scheduled by hand today: that wins for the day
    try:
        if d["roster_day"] and d["roster_day"] == dt.date.today().isoformat():
            return "scheduled"
    except (IndexError, KeyError):
        pass
    return "scheduled" if scheduled_today(d["id"]) else "unavailable"

@app.post("/api/driver/availability")
def api_driver_availability():
    """Drivers build their weekly schedule here. Anything they add goes to dispatch
    as a request; dispatch approves or denies it."""
    did = session.get("driver_id") or (request.get_json(force=True) or {}).get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    if data.get("delete_id"):
        db().execute("DELETE FROM availability WHERE id=? AND driver_id=?", (data["delete_id"], did))
    else:
        dow = int(data.get("dow", 0))
        start = data.get("start") or "09:00"
        end = data.get("end") or "17:00"
        if end <= start:
            return jsonify({"ok": False, "error": "The end time has to be after the start time."}), 400
        if data.get("date"):
            try:
                _d = dt.date.fromisoformat(str(data["date"])[:10])
                dow = _d.weekday()
            except ValueError:
                return jsonify({"ok": False, "error": "Pick a date."}), 400
        else:
            _d = dt.date.today() + dt.timedelta(days=(dow - dt.date.today().weekday()) % 7)
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,created_at,
                        week_start) VALUES(?,?,?,?,?,'pending',?,?)""",
                     (did, dow, start, end, data.get("note", ""), now(), monday_of(_d).isoformat()))
        name = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()["name"]
        log("availability", name + " asked for " + DOW_NAMES[dow] + " " + start + "-" + end)
    db().commit()
    return jsonify({"ok": True, "availability": availability_for(did), "time_off": time_off_for(did)})


@app.get("/api/driver/week")
def api_driver_week():
    """The calendar a driver fills in each week. Defaults to the week they owe."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    ensure_open_week()
    ws = parse_week(request.args.get("start"))
    weeks = [(current_week_start() + dt.timedelta(days=7 * k)).isoformat() for k in range(-4, 2)]
    return jsonify({"ok": True, "week": week_block(did, ws), "weeks": weeks,
                    "week_labels": {w: range_label(dt.date.fromisoformat(w)) for w in weeks},
                    "open_week": open_week_start().isoformat(),
                    "this_week": current_week_start().isoformat()})


@app.post("/api/driver/week")
def api_driver_week_save():
    """Save a whole week in one go. Every day sent goes to dispatch as a request."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    ws = parse_week(data.get("week_start"))
    if ws < current_week_start():
        return jsonify({"ok": False, "error": "That week is closed and saved. Ask dispatch to change it."}), 400
    if dt.datetime.now() < week_opens(ws):
        return jsonify({"ok": False, "error": "That schedule opens " +
                        pretty_day(week_opens(ws).date()) + "."}), 400
    if dt.datetime.now() > week_due(ws) and ws > current_week_start():
        return jsonify({"ok": False, "error": "Submissions closed " + pretty_day(ws - dt.timedelta(days=1)) +
                        " at midnight. Message dispatch and they can still add you."}), 400
    days = data.get("days") or []
    clean = []
    for d in days:
        if d.get("off"):
            continue
        start, end = (d.get("start") or "").strip(), (d.get("end") or "").strip()
        if not start or not end:
            return jsonify({"ok": False,
                            "error": DOW_NAMES[int(d.get("dow", 0))] + " needs a start and an end time."}), 400
        if end <= start:
            return jsonify({"ok": False,
                            "error": DOW_NAMES[int(d.get("dow", 0))] + ": the end time has to be after the start."}), 400
        clean.append((int(d.get("dow", 0)), start, end, (d.get("note") or "").strip()))
    stamp = now()
    key = ws.isoformat()
    db().execute("DELETE FROM availability WHERE driver_id=? AND week_start=?", (did, key))
    for dow, start, end, note in clean:
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                        created_at,week_start) VALUES(?,?,?,?,?,'pending',?,?)""",
                     (did, dow, start, end, note, stamp, key))
    db().execute("""INSERT INTO week_submissions(driver_id,week_start,submitted_at,note)
                    VALUES(?,?,?,?)
                    ON CONFLICT(driver_id,week_start)
                    DO UPDATE SET submitted_at=excluded.submitted_at, note=excluded.note""",
                 (did, key, stamp, (data.get("note") or "").strip()))
    name = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()["name"]
    late = " (after the deadline)" if dt.datetime.now() > week_due(ws) else ""
    log("availability", name + " sent availability for " + week_label(ws) + ": " +
        (str(len(clean)) + " day" + ("" if len(clean) == 1 else "s") if clean else "no days") + late)
    db().commit()
    return jsonify({"ok": True, "week": week_block(did, ws)})


@app.post("/api/dispatch/week-window")
def api_week_window():
    """Dispatch decides which day the coming week's calendar opens."""
    if not session.get("dispatcher_id"):
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    if "open_dow" in b:
        dow = min(max(int(b.get("open_dow") or 4), 0), 6)
        db().execute("INSERT INTO settings(key,value) VALUES('week_open_dow',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(dow),))
    if "open_date" in b:
        raw = (b.get("open_date") or "").strip()
        if raw:
            try:
                d = dt.date.fromisoformat(raw)
            except ValueError:
                return jsonify({"ok": False, "error": "That date did not read as a date."}), 400
            if d >= open_week_start():
                return jsonify({"ok": False,
                                "error": "Pick a day before the week starts, " +
                                         pretty_day(open_week_start()) + "."}), 400
            raw = d.isoformat()
        db().execute("INSERT INTO settings(key,value) VALUES('week_open_date',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
    who = db().execute("SELECT name FROM dispatchers WHERE id=?",
                       (session["dispatcher_id"],)).fetchone()["name"]
    log("availability", who + " set the schedule to open " +
        pretty_day((week_opens(open_week_start())).date()))
    db().commit()
    ensure_open_week()
    return jsonify({"ok": True})


@app.get("/api/dispatch/week")
def api_dispatch_week():
    """Every driver's week, and who still owes one."""
    if not session.get("dispatcher_id"):
        return jsonify({"ok": False}), 403
    ensure_open_week()
    ws = parse_week(request.args.get("start"))
    rows = db().execute("SELECT * FROM drivers ORDER BY name").fetchall()
    drivers = []
    for d in rows:
        block = week_block(d["id"], ws)
        block.update({"driver_id": d["id"], "driver": d["name"], "roster": d["roster"] or "scheduled"})
        drivers.append(block)
    due = week_due(ws)
    return jsonify({"ok": True, "week_start": ws.isoformat(), "label": week_label(ws),
                    "due_human": pretty_day(ws - dt.timedelta(days=1)) + " at midnight",
                    "past_due": dt.datetime.now() > due,
                    "opens_human": pretty_day(week_opens(ws).date()),
                    "open_dow": setting("week_open_dow") or 4,
                    "open_date": setting("week_open_date", str) or "",
                    "window_open": week_is_open(ws) and dt.datetime.now() <= due,
                    "weeks": [(current_week_start() + dt.timedelta(days=7 * k)).isoformat()
                              for k in range(-6, 3)],
                    "week_labels": {(current_week_start() + dt.timedelta(days=7 * k)).isoformat():
                                    range_label(current_week_start() + dt.timedelta(days=7 * k))
                                    for k in range(-6, 3)},
                    "range": range_label(ws),
                    "dates": [short_date(ws + dt.timedelta(days=i)) for i in range(7)],
                    "missing": [d["driver"] for d in drivers if not d["submitted"]],
                    "drivers": drivers})


@app.post("/api/driver/timeoff")
def api_driver_timeoff():
    """Drivers ask for days off. Dispatch approves or denies."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    if data.get("cancel_id"):
        db().execute("DELETE FROM time_off WHERE id=? AND driver_id=? AND status='pending'",
                     (data["cancel_id"], did))
        db().commit()
        return jsonify({"ok": True, "time_off": time_off_for(did)})
    s1 = (data.get("start") or "").strip()
    s2 = (data.get("end") or s1).strip()
    if not s1:
        return jsonify({"ok": False, "error": "Pick a start date."}), 400
    if s2 < s1:
        return jsonify({"ok": False, "error": "The last day cannot be before the first day."}), 400
    db().execute("""INSERT INTO time_off(driver_id,start_date,end_date,reason,status,created_at)
                    VALUES(?,?,?,?,'pending',?)""", (did, s1, s2, data.get("reason", ""), now()))
    db().commit()
    name = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()["name"]
    log("time_off", name + " asked off " + s1 + " to " + s2)
    return jsonify({"ok": True, "time_off": time_off_for(did)})


@app.get("/api/driver/schedule")
def api_driver_schedule():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "availability": availability_for(did), "time_off": time_off_for(did)})


@app.get("/api/dispatch/availability/<int:did>")
def api_read_availability(did):
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "availability": availability_for(did), "time_off": time_off_for(did)})


@app.get("/api/dispatch/schedule")
def api_dispatch_schedule():
    """Every driver's week in one place, plus whatever is waiting on approval."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    out, pending = [], 0
    today = dt.date.today().isoformat()
    for d in db().execute("SELECT * FROM drivers ORDER BY name").fetchall():
        av, off = availability_for(d["id"]), time_off_for(d["id"])
        pending += len([a for a in av if a["status"] == "pending"])
        pending += len([o for o in off if o["status"] == "pending"])
        out.append({"id": d["id"], "name": d["name"], "phone": d["phone"],
                    "roster": d["roster"], "status": d["status"],
                    "off_today": off_today(d["id"], today),
                    "availability": av, "time_off": off})
    return jsonify({"ok": True, "days": DOW_NAMES, "today": today,
                    "drivers": out, "pending": pending})


@app.post("/api/dispatch/schedule")
def api_dispatch_schedule_edit():
    """Dispatch can approve, deny, add, change or drop any driver's hours.
    Anything dispatch writes is approved on the spot."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    who = session.get("dispatcher_name", "dispatch")
    if op == "decide":
        dec = "approved" if b.get("approve") else "denied"
        row = db().execute("SELECT * FROM availability WHERE id=?", (b["id"],)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        db().execute("""UPDATE availability SET status=?, decided_by=?, decided_at=?, reply=?
                        WHERE id=?""", (dec, who, now(), b.get("reply", ""), b["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (row["driver_id"], "dispatch", who,
                      DOW_NAMES[row["dow"]] + " " + row["start_time"] + "-" + row["end_time"] +
                      " was " + dec + ".", now()))
    elif op == "decide_off":
        dec = "approved" if b.get("approve") else "denied"
        row = db().execute("SELECT * FROM time_off WHERE id=?", (b["id"],)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        db().execute("""UPDATE time_off SET status=?, decided_by=?, decided_at=?, reply=?
                        WHERE id=?""", (dec, who, now(), b.get("reply", ""), b["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (row["driver_id"], "dispatch", who,
                      "Time off " + row["start_date"] + " to " + row["end_date"] + " was " + dec + ".",
                      now()))
        if dec == "approved":
            db().execute("UPDATE drivers SET roster='unavailable' WHERE id=?", (row["driver_id"],)) \
                if row["start_date"] <= dt.date.today().isoformat() <= row["end_date"] else None
    elif op in ("add", "set_day") and b.get("date"):
        # hours for one date only, never repeating
        try:
            day = dt.date.fromisoformat(str(b["date"])[:10])
        except ValueError:
            return jsonify({"ok": False, "error": "Pick a date."}), 400
        ws, dow = monday_of(day).isoformat(), day.weekday()
        did = b["driver_id"]
        if b.get("off"):
            db().execute("DELETE FROM availability WHERE driver_id=? AND week_start=? AND dow=?", (did, ws, dow))
            body = "Dispatch has you off on " + DOW_NAMES[dow] + " " + short_date(day) + "."
        else:
            start, end = (b.get("start") or "").strip(), (b.get("end") or "").strip()
            if not start or not end:
                return jsonify({"ok": False, "error": "Enter a start and an end time."}), 400
            if end <= start:
                return jsonify({"ok": False, "error": "The end time has to be after the start time."}), 400
            if op == "set_day":
                db().execute("DELETE FROM availability WHERE driver_id=? AND week_start=? AND dow=?",
                             (did, ws, dow))
            db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                            decided_by,decided_at,created_at,week_start)
                            VALUES(?,?,?,?,?,'approved',?,?,?,?)""",
                         (did, dow, start, end, b.get("note", ""), who, now(), now(), ws))
            body = ("Dispatch put you on for " + DOW_NAMES[dow] + " " + short_date(day) + " " +
                    start + "-" + end + ".")
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (did, "dispatch", who, body, now()))
    elif op == "add":
        dow = int(b.get("dow", 0))
        start, end = b.get("start") or "09:00", b.get("end") or "17:00"
        if end <= start:
            return jsonify({"ok": False, "error": "The end time has to be after the start time."}), 400
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                        decided_by,decided_at,created_at) VALUES(?,?,?,?,?,'approved',?,?,?)""",
                     (b["driver_id"], dow, start, end, b.get("note", ""), who, now(), now()))
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (b["driver_id"], "dispatch", who,
                      "Dispatch put you on for " + DOW_NAMES[dow] + " " + start + "-" + end + ".", now()))
    elif op == "update":
        row = db().execute("SELECT * FROM availability WHERE id=?", (b["id"],)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        start = b.get("start") or row["start_time"]
        end = b.get("end") or row["end_time"]
        if end <= start:
            return jsonify({"ok": False, "error": "The end time has to be after the start time."}), 400
        db().execute("""UPDATE availability SET dow=?, start_time=?, end_time=?, status='approved',
                        decided_by=?, decided_at=? WHERE id=?""",
                     (int(b.get("dow", row["dow"])), start, end, who, now(), b["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (row["driver_id"], "dispatch", who,
                      "Dispatch changed your " + DOW_NAMES[row["dow"]] + " hours to " +
                      start + "-" + end + ".", now()))
    elif op == "delete":
        row = db().execute("SELECT * FROM availability WHERE id=?", (b["id"],)).fetchone()
        db().execute("DELETE FROM availability WHERE id=?", (b["id"],))
        if row:
            db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                         (row["driver_id"], "dispatch", who,
                          "Dispatch took " + DOW_NAMES[row["dow"]] + " " + row["start_time"] +
                          "-" + row["end_time"] + " off your schedule.", now()))
    elif op == "add_off":
        db().execute("""INSERT INTO time_off(driver_id,start_date,end_date,reason,status,
                        decided_by,decided_at,created_at) VALUES(?,?,?,?,'approved',?,?,?)""",
                     (b["driver_id"], b["start"], b.get("end") or b["start"],
                      b.get("reason", ""), who, now(), now()))
    elif op == "delete_off":
        db().execute("DELETE FROM time_off WHERE id=?", (b["id"],))
    else:
        return jsonify({"ok": False, "error": "unknown op"}), 400
    db().commit()
    log("schedule", op)
    return jsonify({"ok": True})


@app.get("/dispatch/schedule")
def dispatch_schedule_page():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_schedule.html", days=DOW_NAMES)


# ---------------------------------------------------------------- closed days calendar

@app.get("/api/dispatch/closures")
def api_closures():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rid = request.args.get("restaurant_id")
    rid = int(rid) if rid and rid != "all" else None
    rows = db().execute("SELECT * FROM closures ORDER BY day").fetchall()
    return jsonify({"ok": True,
                    "closures": [{"id": r["id"], "day": r["day"], "reason": r["reason"] or "",
                                  "restaurant_id": r["restaurant_id"]} for r in rows],
                    "restaurants": [{"id": r["id"], "name": r["name"],
                                     "hours": json.loads(r["hours"]), "open_24": r["open_24"],
                                     "paused": r["closed_override"]}
                                    for r in db().execute(
                                        "SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()]})

@app.post("/api/dispatch/closure")
def api_closure():
    """Toggle a calendar day closed. restaurant_id null closes every store that day."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    day = data.get("day")
    if not day:
        return jsonify({"ok": False, "error": "pick a day"}), 400
    rid = data.get("restaurant_id")
    rid = int(rid) if rid not in (None, "", "all") else None
    row = db().execute("""SELECT id FROM closures WHERE day=? AND
                          ((restaurant_id IS NULL AND ? IS NULL) OR restaurant_id=?)""",
                       (day, rid, rid)).fetchone()
    if row:
        db().execute("DELETE FROM closures WHERE id=?", (row["id"],))
        state = "open"
    else:
        db().execute("INSERT INTO closures(restaurant_id,day,reason) VALUES(?,?,?)",
                     (rid, day, data.get("reason", "")))
        state = "closed"
    db().commit()
    return jsonify({"ok": True, "day": day, "state": state})

@app.get("/dispatch/hours")
def dispatch_hours_page():
    if not dispatcher_required():
        return redirect("/dispatch/login")
    return render_template("dispatch_hours.html")

@app.post("/api/dispatch/reorder")
def api_reorder():
    """Drag and drop within a driver's run: order_ids in the new stop order."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    did = data.get("driver_id")
    for seq, oid in enumerate(data.get("order_ids", []), start=1):
        db().execute("""UPDATE orders SET stack_seq=? WHERE id=? AND driver_id=?""",
                     (seq, oid, did))
    db().commit()
    return jsonify({"ok": True})


@app.post("/api/order/hold")
def api_hold():
    """Hold sends the order back to the pending column. If a driver already had it,
    it comes off their run, their remaining stops renumber, and they get a note."""
    data = request.get_json(force=True)
    oid = data["order_id"]
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    had = o["driver_id"]
    if had and o["dispatch_status"] == "enroute":
        return jsonify({"ok": False, "error":
                        "That order is already picked up and en route. "
                        "Complete it or send it back to the queue first."}), 400
    if o["dispatch_status"] == "awaiting_payment" or o["payment_status"] == "unpaid":
        db().execute("""UPDATE orders SET dispatch_status='awaiting_payment', kitchen_status='waiting',
                        driver_id=NULL, stack_seq=NULL WHERE id=?""", (oid,))
    else:
        db().execute("""UPDATE orders SET dispatch_status='held', kitchen_status='pending',
                        hold_reason=?, driver_id=NULL, stack_seq=NULL WHERE id=?""",
                     (data.get("reason", "held by dispatch"), oid))
    if had:
        rest = db().execute("""SELECT id FROM orders WHERE driver_id=? AND dispatch_status
                               IN ('assigned','received','at_restaurant','enroute')
                               ORDER BY stack_seq ASC""", (had,)).fetchall()
        for i, row in enumerate(rest, start=1):
            db().execute("UPDATE orders SET stack_seq=? WHERE id=?", (i, row["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (had, "dispatch",
                      "Order " + o["code"] + " came off your run and is back on hold with dispatch.",
                      now()))
    db().commit()
    log("hold", o["code"])
    recompute_queue()
    return jsonify({"ok": True, "pulled_from_driver": bool(had)})

@app.get("/api/dispatch/auto")
def api_auto_get():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "auto": bool(setting("auto_assign"))})


@app.post("/api/dispatch/auto")
def api_auto_set():
    """Turn automatic dispatch on or off. Off means orders wait in the queue
    until a dispatcher assigns them by hand."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    on = bool(request.get_json(force=True).get("auto"))
    db().execute("UPDATE settings SET value=? WHERE key='auto_assign'", ("1" if on else "0",))
    db().commit()
    log("auto_dispatch", "on" if on else "off")
    if on:
        auto_assign()
    return jsonify({"ok": True, "auto": on})

@app.post("/api/order/send-to-driver")
def api_send_to_driver():
    """Push a held or pending order straight onto a driver from the pending column.
    With no driver named it goes to whoever is next up."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (b["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    db().execute("""UPDATE orders SET dispatch_status='queued', hold_reason=NULL WHERE id=?""",
                 (o["id"],))
    db().commit()
    did = b.get("driver_id")
    if did:
        d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
        if not d:
            return jsonify({"ok": False, "error": "Unknown driver."}), 404
        if d["status"] != "online":
            return jsonify({"ok": False,
                            "error": d["name"] + " is not on shift. Put them online first."}), 400
        seq = db().execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                              AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                           (did,)).fetchone()["s"]
        db().execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned', stack_seq=?,
                        hold_reason=NULL WHERE id=?""", (did, seq, o["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (did, "dispatch", session.get("dispatcher_name", "dispatch"),
                      "Order " + o["code"] + " was sent to you from the pending column.", now()))
        db().commit()
        log("send_to_driver", o["code"] + " -> " + d["name"])
        return jsonify({"ok": True, "driver": d["name"]})
    auto_assign()
    row = db().execute("SELECT driver_id FROM orders WHERE id=?", (o["id"],)).fetchone()
    if not row["driver_id"]:
        return jsonify({"ok": True, "queued": True,
                        "error": "No driver free right now, it is first in the queue."})
    name = db().execute("SELECT name FROM drivers WHERE id=?", (row["driver_id"],)).fetchone()["name"]
    log("send_to_driver", o["code"] + " -> " + name)
    return jsonify({"ok": True, "driver": name})

@app.post("/api/order/reopen")
def api_reopen():
    """Dispatch can pull a completed order back onto the live board."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    keep = bool(data.get("keep_driver")) and o["driver_id"]
    if keep:
        seq = db().execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                              AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                           (o["driver_id"],)).fetchone()["s"]
        # back to "assigned" so the driver's phone pages again and the board flashes
        # until the driver taps Received
        db().execute("""UPDATE orders SET dispatch_status='assigned', delivered_at=NULL, stack_seq=?,
                        paged_at=? WHERE id=?""", (seq, now(), o["id"]))
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (o["driver_id"], "dispatch",
                      "Order " + o["code"] + " was reopened and sent back to you. Tap Received to accept it.", now()))
    else:
        db().execute("""UPDATE orders SET dispatch_status='queued', delivered_at=NULL, driver_id=NULL,
                        stack_seq=NULL, hold_reason=NULL WHERE id=?""", (o["id"],))
    db().commit()
    log("reopen", o["code"])
    if keep:
        log_driver(o["driver_id"], "Dispatch reopened " + o["code"] + " and re-paged the driver")
    auto_assign()
    return jsonify({"ok": True})


@app.route("/dispatch/restaurants", methods=["GET", "POST"])
def dispatch_restaurants():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    saved = False
    if request.method == "POST":
        rid = request.form["restaurant_id"]
        hours = {}
        for i in range(7):
            o = request.form.get("open_" + str(i), "")
            c = request.form.get("close_" + str(i), "")
            hours[str(i)] = [o, c] if o and c else ["", ""]
        db().execute("""UPDATE restaurants SET hours=?, closed_override=?, open_24=?, prep_default=?,
                        phone=? WHERE id=?""",
                     (json.dumps(hours), 1 if request.form.get("closed_override") else 0,
                      1 if request.form.get("open_24") else 0,
                      int(request.form.get("prep_default") or 15), request.form.get("phone", ""), rid))
        name = (request.form.get("name") or "").strip()
        if name:
            db().execute("UPDATE restaurants SET name=? WHERE id=?", (name, rid))
        addr = (request.form.get("address") or "").strip()
        if addr:
            cur_addr = db().execute("SELECT address FROM restaurants WHERE id=?", (rid,)).fetchone()["address"]
            if addr != cur_addr:
                g1 = geocode(addr)
                if g1["ok"]:
                    db().execute("UPDATE restaurants SET address=?, lat=?, lng=? WHERE id=?",
                                 (g1["formatted"], g1["lat"], g1["lng"], rid))
                else:
                    db().execute("UPDATE restaurants SET address=? WHERE id=?", (addr, rid))
        db().commit()
        saved = True
    rs = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    data = [{"r": r, "hours": json.loads(r["hours"]), "open": is_open(r)} for r in rs]
    return render_template("dispatch_restaurants.html", data=data, week=WEEK, saved=saved)

@app.route("/dispatch/restaurants/delete", methods=["POST"])
def dispatch_delete_restaurant():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    rid = request.form.get("restaurant_id")
    live = db().execute("""SELECT COUNT(*) c FROM orders WHERE restaurant_id=?
                           AND dispatch_status NOT IN ('delivered','cancelled')""", (rid,)).fetchone()["c"]
    if live:
        return redirect(url_for("dispatch_restaurants", err="live"))
    for iid in [x["id"] for x in db().execute("SELECT id FROM menu_items WHERE restaurant_id=?",
                                              (rid,)).fetchall()]:
        for gid in [x["id"] for x in db().execute("SELECT id FROM option_groups WHERE item_id=?",
                                                  (iid,)).fetchall()]:
            db().execute("DELETE FROM options WHERE group_id=?", (gid,))
        db().execute("DELETE FROM option_groups WHERE item_id=?", (iid,))
    r = db().execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()
    db().execute("DELETE FROM menu_items WHERE restaurant_id=?", (rid,))
    db().execute("DELETE FROM restaurants WHERE id=?", (rid,))
    db().commit()
    log("restaurant", (r["name"] if r else "restaurant") + " removed by dispatch")
    return redirect(url_for("dispatch_restaurants", deleted=1))

@app.route("/dispatch/restaurants/new", methods=["POST"])
def dispatch_add_restaurant():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    f = request.form
    slug = "".join(ch for ch in f.get("slug", "").strip().lower() if ch.isalnum())
    address = f.get("address", "").strip()
    lat, lng = f.get("lat", "").strip(), f.get("lng", "").strip()
    if not lat or not lng:
        g1 = geocode(address)
        if not g1["ok"]:
            return redirect(url_for("dispatch_restaurants", err="address"))
        address, lat, lng = g1["formatted"], g1["lat"], g1["lng"]
    db().execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,prep_default)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                 (f.get("name", "").strip(), slug, f.get("pin", "1111").strip(), address,
                  f.get("phone", ""), float(lat), float(lng), json.dumps(DEFAULT_HOURS),
                  int(f.get("prep_default") or 15)))
    db().commit()
    return redirect(url_for("dispatch_menu", rid=db().execute(
        "SELECT id FROM restaurants WHERE slug=?", (slug,)).fetchone()["id"]))


@app.route("/dispatch/menu/<int:rid>", methods=["GET", "POST"])
def dispatch_menu(rid):
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not r:
        return redirect(url_for("dispatch_restaurants"))
    if request.method == "POST":
        op = request.form.get("op")
        if op == "add":
            price = request.form.get("price", "0").replace("$", "").strip()
            sec = (request.form.get("section") or "").strip()
            nxt = db().execute("SELECT COALESCE(MAX(sort),0)+1 n FROM menu_items WHERE restaurant_id=?",
                               (rid,)).fetchone()["n"]
            if sec:
                same = db().execute("""SELECT MAX(sort) m FROM menu_items
                                       WHERE restaurant_id=? AND section=?""", (rid, sec)).fetchone()["m"]
                if same is not None:
                    nxt = same
            db().execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents,section,sort)
                            VALUES(?,?,?,?,?,?)""",
                         (rid, request.form["name"].strip(), request.form.get("description", "").strip(),
                          int(round(float(price or 0) * 100)), sec, nxt))
        elif op == "section":
            db().execute("UPDATE menu_items SET section=? WHERE id=? AND restaurant_id=?",
                         ((request.form.get("section") or "").strip(), request.form["item_id"], rid))
        elif op == "move-section":
            # nudge a whole section up or down the menu
            sec = (request.form.get("section") or "").strip()
            step = -1 if request.form.get("dir") == "up" else 1
            db().execute("UPDATE menu_items SET sort=MAX(0,sort+?) WHERE restaurant_id=? AND section=?",
                         (step * 2, rid, sec))
        elif op == "toggle":
            db().execute("UPDATE menu_items SET active=1-active WHERE id=? AND restaurant_id=?",
                         (request.form["item_id"], rid))
        elif op == "price":
            price = request.form.get("price", "0").replace("$", "").strip()
            db().execute("UPDATE menu_items SET price_cents=? WHERE id=? AND restaurant_id=?",
                         (int(round(float(price or 0) * 100)), request.form["item_id"], rid))
        elif op == "delete":
            db().execute("DELETE FROM menu_items WHERE id=? AND restaurant_id=?",
                         (request.form["item_id"], rid))
        db().commit()
        return redirect(url_for("dispatch_menu", rid=rid))
    items = db().execute("SELECT * FROM menu_items WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()
    return render_template("dispatch_menu.html", r=r, items=items, sections=menu_sections(rid))


@app.route("/dispatch/settings", methods=["GET", "POST"])
def dispatch_settings():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    saved = False
    if request.method == "POST":
        for _k in ("kitchen_accept_min", "driver_accept_min", "late_sound_after_min"):
            if _k in request.form:
                try:
                    _v = int(request.form[_k] or 0)
                except ValueError:
                    _v = -1
                if 0 <= _v <= 60:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, str(_v)))
                else:
                    FUTURE_LEAD_ERR.append(2)
        if "future_lead_min" in request.form:
            try:
                lm = int(request.form["future_lead_min"])
            except ValueError:
                lm = -1
            if 10 <= lm <= 240:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('future_lead_min',?)", (str(lm),))
            else:
                FUTURE_LEAD_ERR.append(1)
        for key in ("base_fee_cents", "base_miles", "per_mile_cents", "tax_rate_bp", "auto_assign"):
            if key in request.form:
                db().execute("UPDATE settings SET value=? WHERE key=?", (request.form[key], key))
        errs = []
        if FUTURE_LEAD_ERR:
            FUTURE_LEAD_ERR.clear()
            errs.append("Check the minutes: future orders take 10 to 240, accept warnings take 0 to 60.")
        if "order_keep_days" in request.form:
            try:
                kd = int((request.form["order_keep_days"] or "0").strip())
            except ValueError:
                kd = -1
            if kd == 0 or ORDER_KEEP_MIN <= kd <= ORDER_KEEP_MAX:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('order_keep_days',?)", (str(kd),))
            else:
                errs.append("Keep orders for 0 (forever) or %d to %d days." % (ORDER_KEEP_MIN, ORDER_KEEP_MAX))
        for field, key, label in (("tax_pct", "tax_rate_bp", "Tax"), ("service_pct", "service_fee_bp", "Service fee")):
            if field in request.form:
                raw = request.form[field].replace("%", "").strip() or "0"
                try:
                    pct = float(raw)
                except ValueError:
                    pct = -1
                if 0 <= pct <= 30:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                 (key, str(int(round(pct * 100)))))
                else:
                    errs.append(label + " needs to be a percent between 0 and 30, like 9 or 9.5.")
        if "business_name" in request.form:
            nm = " ".join(request.form["business_name"].split())[:60]
            if nm:
                db().execute("UPDATE settings SET value=? WHERE key='business_name'", (nm,))
            else:
                errs.append("Business name cannot be blank.")
        if "business_address" in request.form:
            ad = " ".join(request.form["business_address"].split())[:160]
            db().execute("UPDATE settings SET value=? WHERE key='business_address'", (ad,))
        if "dispatch_phone" in request.form:
            ph = "".join(c for c in request.form["dispatch_phone"] if c.isdigit())
            if len(ph) == 11 and ph.startswith("1"):
                ph = ph[1:]
            if len(ph) == 10:
                db().execute("UPDATE settings SET value=? WHERE key='dispatch_phone'", (ph,))
            else:
                errs.append("Dispatch phone needs 10 digits, like 334-209-2844.")
        if errs:
            db().commit()
            rows = db().execute("SELECT * FROM settings").fetchall()
            return render_template("dispatch_settings.html", s={r["key"]: r["value"] for r in rows},
                                   saved=False, errors=errs)
        if "order_tokens" in request.form:
            tags = ",".join(t.strip()[:24] for t in request.form["order_tokens"].split(",") if t.strip())
            db().execute("UPDATE settings SET value=? WHERE key='order_tokens'", (tags,))
        db().commit()
        saved = True
    rows = db().execute("SELECT * FROM settings").fetchall()
    return render_template("dispatch_settings.html", s={r["key"]: r["value"] for r in rows}, saved=saved)

# ---------------------------------------------------------------- chat

@app.get("/api/chat/<int:driver_id>")
def api_chat(driver_id):
    if dispatcher_required() and request.args.get("peek") != "1":
        db().execute("""UPDATE messages SET seen_by_dispatch=1
                        WHERE driver_id=? AND sender='driver' AND seen_by_dispatch=0""", (driver_id,))
        db().commit()
    rows = db().execute("""SELECT * FROM messages WHERE driver_id=? ORDER BY id DESC LIMIT 60""",
                        (driver_id,)).fetchall()
    msgs = [{"id": r["id"], "sender": r["sender"],
              "who": (r["sender_name"] + " (dispatch)") if r["sender_name"] else
                     ("Dispatch" if r["sender"] == "dispatch" else r["sender"]),
              "body": r["body"], "at": r["created_at"][11:16]}
            for r in reversed(rows)]
    return jsonify({"ok": True, "messages": msgs})

@app.post("/api/chat/<int:driver_id>")
def api_chat_send(driver_id):
    data = request.get_json(force=True)
    sender = data.get("sender", "dispatch")
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False}), 400
    who = session.get("dispatcher_name") if sender == "dispatch" else None
    db().execute("""INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at,
                                        seen_by_dispatch)
                    VALUES(?,?,?,?,?,?,?)""",
                 (driver_id, sender, who, session.get("dispatcher_id") if sender == "dispatch" else None,
                  body, now(), 0 if sender == "driver" else 1))
    db().commit()
    low = body.lower()
    if sender == "driver":
        want = None
        if "online" in low or "clock in" in low or "ready to roll" in low:
            want = "online"
        elif "break" in low or "lunch" in low:
            want = "break"
        elif "offline" in low or "clock out" in low or "done for" in low:
            want = "offline"
        if want:
            request_status(driver_id, want)
    return jsonify({"ok": True})


def request_status(driver_id, want):
    """A driver can only ASK. Dispatch is the one who flips the switch."""
    db().execute("UPDATE drivers SET pending_request=? WHERE id=?", (want, driver_id))
    log_driver(driver_id, "Driver marked: requested " + want)
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (driver_id, "system",
                  "Request sent to dispatch: " + want + ". Waiting on dispatch to approve.", now()))
    db().commit()


def set_driver_status(driver_id, status, reply):
    """Dispatch-only. Nothing in the driver app calls this directly."""
    was = db().execute("SELECT status FROM drivers WHERE id=?", (driver_id,)).fetchone()
    log_driver(driver_id, "Dispatch set " + status + " (was " + (was["status"] if was else "?") + ")", status=status)
    db().execute("UPDATE drivers SET status=?, pending_request=NULL, last_seen=? WHERE id=?",
                 (status, now(), driver_id))
    if status == "online" and (not was or was["status"] != "online"):
        # clocking on puts you at the back of the line, not the front
        db().execute("UPDATE drivers SET online_since=?, last_assigned_at=NULL WHERE id=?",
                     (now(), driver_id))
    elif status != "online":
        db().execute("UPDATE drivers SET online_since=NULL WHERE id=?", (driver_id,))
    if status == "offline":
        db().execute("UPDATE drivers SET last_lat=NULL,last_lng=NULL,last_loc_at=NULL,last_addr=NULL,last_addr_lat=NULL,last_addr_lng=NULL WHERE id=?",
                     (driver_id,))
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (driver_id, "dispatch", reply, now()))
    db().commit()
    if status != "online":
        db().execute("""UPDATE orders SET driver_id=NULL, stack_seq=NULL, dispatch_status='queued'
                        WHERE driver_id=? AND dispatch_status='assigned'""", (driver_id,))
        db().commit()
    auto_assign()

# ---------------------------------------------------------------- driver app

@app.route("/driver/login", methods=["GET", "POST"])
def driver_login():
    err = None
    if request.method == "POST":
        phone = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit())
        pin = request.form.get("pin", "")
        row = db().execute("SELECT * FROM drivers WHERE phone=? AND pin=?", (phone, pin)).fetchone()
        if row:
            session["driver_id"] = row["id"]
            session["driver_name"] = row["name"]
            return redirect(url_for("driver"))
        err = "No driver with that phone and PIN."
    return render_template("driver_login.html", err=err)

@app.route("/driver/logout")
def driver_logout():
    session.pop("driver_id", None)
    return redirect(url_for("driver_login"))

@app.route("/driver")
def driver():
    if not session.get("driver_id"):
        return redirect(url_for("driver_login"))
    return render_template("driver.html", driver_id=session["driver_id"],
                           driver_name=session["driver_name"],
                           dispatch_phone=dispatch_phone(), dispatch_tel=tel_digits(dispatch_phone()))

@app.get("/api/driver/state")
def api_driver_state():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    auto_assign()
    d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    recompute_queue()
    rotation = {x["id"]: i + 1 for i, x in enumerate(available_drivers())}
    waiting = db().execute("""SELECT COUNT(*) c FROM orders
                              WHERE dispatch_status IN ('queued','held')""").fetchone()["c"]
    mine = db().execute("""SELECT * FROM orders WHERE driver_id=? AND dispatch_status IN
                           ('assigned','received','at_restaurant','enroute')
                           ORDER BY stack_seq ASC""", (did,)).fetchall()
    # Completed stays on the driver's phone until dispatch presses Close at the end of the day.
    done = db().execute("""SELECT * FROM orders WHERE driver_id=? AND dispatch_status IN ('delivered','cancelled')
                           AND COALESCE(delivered_at, created_at) > ?
                           ORDER BY COALESCE(delivered_at, created_at) DESC LIMIT 150""",
                        (did, setting("driver_done_cleared_at", str) or "")).fetchall()
    scheduled = d["status"] != "offline" or driver_group(d) == "scheduled"
    return jsonify({"ok": True,
                    "business_open": business_is_open(),
                    "dispatch_tel": tel_digits(dispatch_phone()),
                    "late": [x for x in late_accepts() if x["kind"] == "driver" and x["driver_id"] == did],
                    "business_name": (setting("business_name", str) or "Fleet Delivery"),
                    "scheduled": scheduled,
                    "done": [order_dict(o) for o in done],
                    "driver": {"name": d["name"], "status": d["status"],
                               "pending_request": d["pending_request"], "max_stack": d["max_stack"],
                               "up_next": rotation.get(d["id"]), "waiting_count": waiting,
                               "roster": d["roster"]},
                    "availability": availability_for(did),
                    "stack": [order_dict(o) for o in mine]})

@app.post("/api/driver/request")
def api_driver_request():
    """Drivers request a status change. Only dispatch can grant it."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    want = request.get_json(force=True).get("status")
    if want not in ("online", "break", "offline"):
        return jsonify({"ok": False}), 400
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (did, "driver", "Requesting " + want + ".", now()))
    db().commit()
    request_status(did, want)
    return jsonify({"ok": True})

# ---------------------------------------------------------------- exports + GPS history

def _xlsx_response(wb, filename):
    import io
    from flask import send_file
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def _sheet(ws, headers, rows, money_cols=(), widths=None):
    from openpyxl.styles import Font, PatternFill, Alignment
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="0F172A")
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append(r)
    for col in money_cols:
        for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            for c in row:
                c.number_format = '"$"#,##0.00'
    for i, h in enumerate(headers, start=1):
        w = (widths or {}).get(h) or max(10, min(45, max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:300]]) + 2))
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

def _day_range():
    today = dt.date.today().isoformat()
    f = (request.args.get("from") or today)[:10]
    t = (request.args.get("to") or f)[:10]
    if t < f:
        f, t = t, f
    return f, t

@app.get("/api/dispatch/purge-preview")
def api_purge_preview():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    try:
        days = int(request.args.get("days") or setting("order_keep_days") or 0)
    except ValueError:
        days = 0
    if days < ORDER_KEEP_MIN or days > ORDER_KEEP_MAX:
        return jsonify({"ok": False, "error": "Pick %d to %d days." % (ORDER_KEEP_MIN, ORDER_KEEP_MAX)})
    ids, cut = order_purge_ids(days)
    last_day = (dt.date.fromisoformat(cut) - dt.timedelta(days=1)).isoformat()
    first = db().execute("SELECT MIN(substr(created_at,1,10)) d FROM orders").fetchone()["d"] or last_day
    return jsonify({"ok": True, "count": len(ids), "days": days, "before": cut,
                    "export_url": "/dispatch/export/orders.xlsx?from=%s&to=%s" % (first, last_day)})

@app.post("/api/dispatch/purge-orders")
def api_purge_orders():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True) or {}
    if data.get("confirm") != "PURGE":
        return jsonify({"ok": False, "error": "Type PURGE to confirm."}), 400
    try:
        days = int(data.get("days") or 0)
    except ValueError:
        days = 0
    if days < ORDER_KEEP_MIN or days > ORDER_KEEP_MAX:
        return jsonify({"ok": False, "error": "Pick %d to %d days." % (ORDER_KEEP_MIN, ORDER_KEEP_MAX)}), 400
    n = purge_old_orders(days=days, force=True)
    return jsonify({"ok": True, "deleted": n})

@app.get("/dispatch/export/orders.xlsx")
def export_orders():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    from openpyxl import Workbook
    f, t = _day_range()
    rows = db().execute("""SELECT * FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ?
                           ORDER BY created_at ASC""", (f, t)).fetchall()
    out = []
    for r in rows:
        o = order_dict(r)
        c = lambda k: round((o.get(k) or 0) / 100.0, 2)
        out.append([o["code"], o.get("ref") or "", (o.get("created_at") or "").replace("T", " "),
                    (o.get("delivered_at") or "").replace("T", " "), o["dispatch_status"], o.get("kitchen_status") or "",
                    o["restaurant"], o["customer"], o.get("phone") or "", o["address"], o.get("miles") or 0,
                    "; ".join(o.get("lines") or []), c("subtotal_cents"),
                    round(r["fee_cents"] / 100.0, 2), c("service_cents"),
                    round((r["tax_cents"] if "tax_cents" in r.keys() else 0) / 100.0, 2), c("tip_cents"),
                    round(r["total_cents"] / 100.0, 2), c("refunded_cents"),
                    ("Cash" if o.get("cash") else (o.get("pay_method") or "Card")), o.get("payment_status") or "",
                    o.get("driver") or "", o.get("source_label") or "", o.get("token") or "",
                    o.get("note") or "", o.get("dispatch_note") or ""])
    wb = Workbook(); ws = wb.active; ws.title = "Orders"
    _sheet(ws, ["Order", "Ref", "Placed", "Delivered", "Status", "Kitchen", "Restaurant", "Customer", "Phone",
                "Address", "Miles", "Items", "Food subtotal", "Delivery fee", "Service fee", "Tax", "Tip",
                "Total", "Refunded", "Payment", "Payment status", "Driver", "Source", "Tag",
                "Customer note", "Dispatch note"], out, money_cols=(13, 14, 15, 16, 17, 18, 19),
           widths={"Items": 45, "Address": 38})
    done = [x for x in out if x[4] == "delivered"]
    s = wb.create_sheet("Summary")
    _sheet(s, ["From", "To", "Orders", "Delivered", "Cancelled", "Food", "Delivery fees", "Service fees",
               "Tax", "Tips", "Total collected"],
           [[f, t, len(out), len(done), sum(1 for x in out if x[4] == "cancelled"),
             round(sum(x[12] for x in done), 2), round(sum(x[13] for x in done), 2),
             round(sum(x[14] for x in done), 2), round(sum(x[15] for x in done), 2),
             round(sum(x[16] for x in done), 2), round(sum(x[17] - x[18] for x in done), 2)]],
           money_cols=(6, 7, 8, 9, 10, 11))
    by = {}
    for x in done:
        k = x[21] or "No driver"
        v = by.setdefault(k, [k, 0, 0.0, 0.0, 0.0])
        v[1] += 1; v[2] += x[13]; v[3] += x[16]; v[4] += x[17]
    s2 = wb.create_sheet("By driver")
    _sheet(s2, ["Driver", "Deliveries", "Delivery fees", "Tips", "Order totals"],
           [[v[0], v[1], round(v[2], 2), round(v[3], 2), round(v[4], 2)] for v in sorted(by.values())],
           money_cols=(3, 4, 5))
    name = (setting("business_name", str) or "orders").replace(" ", "-")
    return _xlsx_response(wb, "%s-orders-%s%s.xlsx" % (name, f, "" if f == t else "-to-" + t))

def _gps_rows(driver_id, f, t):
    sql = """SELECT l.*, d.name FROM driver_log l JOIN drivers d ON d.id=l.driver_id
             WHERE substr(l.created_at,1,10) BETWEEN ? AND ?"""
    args = [f, t]
    if driver_id:
        sql += " AND l.driver_id=?"; args.append(int(driver_id))
    return db().execute(sql + " ORDER BY l.created_at ASC, l.id ASC LIMIT 20000", args).fetchall()

@app.get("/dispatch/gps-log")
def dispatch_gps_log():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    purge_driver_log()
    f, t = _day_range()
    did = request.args.get("driver") or ""
    events_only = request.args.get("events") == "1"
    rows = _gps_rows(did, f, t)
    if events_only:
        rows = [r for r in rows if r["event"] != "gps"]
    drivers = db().execute("SELECT id,name FROM drivers ORDER BY name").fetchall()
    pts = [r for r in rows if r["lat"] is not None]
    route = ""
    if did and len(pts) >= 2:
        step = max(1, len(pts) // 9)
        picks = pts[::step][:10]
        route = ("https://www.google.com/maps/dir/" +
                 "/".join("%.6f,%.6f" % (p["lat"], p["lng"]) for p in picks))
    return render_template("dispatch_gps.html", rows=rows, drivers=drivers, f=f, t=t, did=did,
                           events_only=events_only, route=route, keep_days=GPS_LOG_KEEP_DAYS)

@app.get("/dispatch/export/gps.xlsx")
def export_gps():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    from openpyxl import Workbook
    f, t = _day_range()
    rows = _gps_rows(request.args.get("driver") or "", f, t)
    wb = Workbook(); ws = wb.active; ws.title = "GPS log"
    _sheet(ws, ["Time", "Driver", "Status", "Event", "Address", "Latitude", "Longitude", "Map"],
           [[r["created_at"].replace("T", " "), r["name"], r["status"] or "",
             "GPS fix" if r["event"] == "gps" else r["event"], r["address"] or "", r["lat"], r["lng"],
             ("https://www.google.com/maps?q=%.6f,%.6f" % (r["lat"], r["lng"])) if r["lat"] is not None else ""]
            for r in rows], widths={"Event": 40, "Address": 40, "Map": 20})
    return _xlsx_response(wb, "gps-log-%s%s.xlsx" % (f, "" if f == t else "-to-" + t))

# ---------------------------------------------------------------- restaurant app

@app.route("/restaurant/login", methods=["GET", "POST"])
def rest_login():
    err = None
    if request.method == "POST":
        row = db().execute("SELECT * FROM restaurants WHERE slug=? AND pin=?",
                           (request.form.get("slug", "").strip().lower(),
                            request.form.get("pin", ""))).fetchone()
        if row:
            session["restaurant_id"] = row["id"]
            session["restaurant_name"] = row["name"]
            return redirect(url_for("rest_home"))
        err = "Wrong store code or PIN."
    return render_template("rest_login.html", err=err)

@app.route("/restaurant/logout")
def rest_logout():
    session.pop("restaurant_id", None)
    return redirect(url_for("rest_login"))

@app.route("/restaurant")
def rest_home():
    if not session.get("restaurant_id"):
        return redirect(url_for("rest_login"))
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (session["restaurant_id"],)).fetchone()
    return render_template("rest.html", r=r, open=is_open(r), hours=hours_label(r),
                           dispatch_phone=dispatch_phone(), dispatch_tel=tel_digits(dispatch_phone()))

@app.get("/api/restaurant/orders")
def api_rest_orders():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    # Active plus anything finished today, so the kitchen's Completed tab has a history.
    # An order leaves Active the moment the driver is en route, and comes straight back
    # if that gets undone (status put back to received or at the restaurant).
    try:
        release_scheduled()
    except Exception as e:
        print("future release skipped:", e)
    day = (request.args.get("day") or "")[:10]
    if day:
        done_sql, arg = "substr(COALESCE(delivered_at, created_at),1,10)=?", day
    else:
        # finished orders stay on the tablet until dispatch presses Close for the day
        done_sql, arg = "COALESCE(delivered_at, created_at) > ?", (setting("driver_done_cleared_at", str) or "")
    rows = db().execute("""SELECT * FROM orders WHERE restaurant_id=? AND kitchen_status NOT IN ('waiting','scheduled')
                           AND dispatch_status NOT IN ('awaiting_payment','scheduled')
                           AND (dispatch_status NOT IN ('delivered','cancelled') OR """ + done_sql + """)
                           ORDER BY created_at ASC""", (rid, arg)).fetchall()
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
    rc_unread = db().execute("""SELECT COUNT(*) c FROM rest_messages WHERE restaurant_id=?
                                AND sender='dispatch' AND seen_by_rest=0""", (rid,)).fetchone()["c"]
    rc_last = db().execute("SELECT MAX(id) m FROM rest_messages WHERE restaurant_id=?", (rid,)).fetchone()["m"] or 0
    return jsonify({"late": [x for x in late_accepts() if x["kind"] == "kitchen" and x["restaurant_id"] == rid],
                    "chat_unread": rc_unread, "chat_last_id": rc_last, "ok": True, "orders": [order_dict(o) for o in rows],
                    "open": is_open(r), "open_24": bool(r["open_24"]),
                    "hours": hours_label(r), "prep_default": r["prep_default"]})

@app.post("/api/restaurant/toggle")
def api_rest_toggle():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    r = db().execute("SELECT closed_override FROM restaurants WHERE id=?", (rid,)).fetchone()
    db().execute("UPDATE restaurants SET closed_override=? WHERE id=?",
                 (0 if r["closed_override"] else 1, rid))
    db().commit()
    return jsonify({"ok": True})

@app.post("/api/restaurant/open24")
def api_rest_open24():
    rid = session.get("restaurant_id") if not dispatcher_required() else \
        request.get_json(force=True).get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    r = db().execute("SELECT open_24 FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"ok": False}), 404
    flip = 0 if r["open_24"] else 1
    db().execute("UPDATE restaurants SET open_24=?, closed_override=0 WHERE id=?", (flip, rid))
    db().commit()
    return jsonify({"ok": True, "open_24": bool(flip)})


# ---------------- dispatcher management APIs ----------------
@app.get("/api/dispatch/catalog")
def api_catalog():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rests = []
    for r in db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall():
        items = []
        for it in db().execute("SELECT * FROM menu_items WHERE restaurant_id=? ORDER BY id",
                               (r["id"],)).fetchall():
            items.append({"id": it["id"], "name": it["name"], "description": it["description"],
                          "price": money(it["price_cents"]), "price_cents": it["price_cents"],
                          "active": it["active"], "groups": item_options(it["id"])})
        rests.append({"id": r["id"], "name": r["name"], "slug": r["slug"], "address": r["address"],
                      "phone": r["phone"], "prep_default": r["prep_default"],
                      "paused": r["closed_override"], "open_24": r["open_24"], "items": items})
    drivers = [{"id": d["id"], "name": d["name"], "phone": d["phone"], "pin": d["pin"],
                "status": d["status"],
                "active_orders": db().execute("""SELECT COUNT(*) c FROM orders WHERE driver_id=?
                                   AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                                              (d["id"],)).fetchone()["c"]}
               for d in db().execute("SELECT * FROM drivers ORDER BY name").fetchall()]
    return jsonify({"ok": True, "restaurants": rests, "drivers": drivers})


@app.post("/api/dispatch/restaurant")
def api_restaurant_crud():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op == "create":
        name = (b.get("name") or "").strip()
        addr = (b.get("address") or "").strip()
        if not name or not addr:
            return jsonify({"ok": False, "error": "Name and address are required."}), 400
        slug = (b.get("slug") or re.sub(r"[^a-z0-9]+", "", name.lower()))[:24] or "rest"
        n, i = slug, 2
        while db().execute("SELECT 1 FROM restaurants WHERE slug=?", (slug,)).fetchone():
            slug = "%s%d" % (n, i); i += 1
        g = geocode(addr)
        db().execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,prep_default)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                     (name, slug, (b.get("pin") or "1111").strip(), g["formatted"] if g else addr,
                      (b.get("phone") or "").strip(), g["lat"] if g else None, g["lng"] if g else None,
                      json.dumps(b.get("hours") or DEFAULT_HOURS)
                      if not isinstance(b.get("hours") or DEFAULT_HOURS, str)
                      else (b.get("hours") or DEFAULT_HOURS),
                      int(b.get("prep_default") or 15)))
        db().commit()
        rid = db().execute("SELECT id FROM restaurants WHERE slug=?", (slug,)).fetchone()["id"]
        return jsonify({"ok": True, "slug": slug, "restaurant_id": rid, "geocoded": bool(g)})
    if op == "delete":
        rid = b.get("restaurant_id")
        live = db().execute("""SELECT COUNT(*) c FROM orders WHERE restaurant_id=?
                               AND dispatch_status NOT IN ('delivered','cancelled')""",
                            (rid,)).fetchone()["c"]
        if live:
            return jsonify({"ok": False, "error":
                            "%d live order(s) on this restaurant. Finish or cancel them first." % live}), 400
        ids = [x["id"] for x in db().execute("SELECT id FROM menu_items WHERE restaurant_id=?",
                                             (rid,)).fetchall()]
        for iid in ids:
            gids = [g["id"] for g in db().execute("SELECT id FROM option_groups WHERE item_id=?",
                                                  (iid,)).fetchall()]
            for gid in gids:
                db().execute("DELETE FROM options WHERE group_id=?", (gid,))
            db().execute("DELETE FROM option_groups WHERE item_id=?", (iid,))
        db().execute("DELETE FROM menu_items WHERE restaurant_id=?", (rid,))
        db().execute("DELETE FROM restaurants WHERE id=?", (rid,))
        db().commit()
        return jsonify({"ok": True})
    if op == "update":
        if b.get("slug"):
            newslug = re.sub(r"[^a-z0-9]+", "", (b.get("slug") or "").lower())[:24]
            if not newslug:
                return jsonify({"ok": False, "error": "Store code can only use letters and numbers."}), 400
            clash = db().execute("SELECT id FROM restaurants WHERE slug=? AND id IS NOT ?",
                                 (newslug, b.get("restaurant_id"))).fetchone()
            if clash:
                return jsonify({"ok": False, "error": "Another restaurant already uses that store code."}), 400
            db().execute("UPDATE restaurants SET slug=? WHERE id=?", (newslug, b.get("restaurant_id")))
        db().execute("""UPDATE restaurants SET name=COALESCE(?,name), address=COALESCE(?,address),
                        phone=COALESCE(?,phone), pin=COALESCE(?,pin),
                        prep_default=COALESCE(?,prep_default) WHERE id=?""",
                     (b.get("name"), b.get("address"), b.get("phone"), b.get("pin"),
                      b.get("prep_default"), b.get("restaurant_id")))
        if b.get("address"):
            g = geocode(b["address"])
            if g:
                db().execute("UPDATE restaurants SET address=?, lat=?, lng=? WHERE id=?",
                             (g["formatted"], g["lat"], g["lng"], b["restaurant_id"]))
        db().commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown op"}), 400


@app.post("/api/dispatch/menu-item")
def api_menu_item():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op == "create":
        cur = db().execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents,section)
                              VALUES(?,?,?,?,?)""",
                           (b["restaurant_id"], (b.get("name") or "Item").strip(),
                            (b.get("description") or "").strip(), int(b.get("price_cents") or 0),
                            (b.get("section") or "").strip()))
        db().commit()
        return jsonify({"ok": True, "item_id": cur.lastrowid})
    if op == "update":
        db().execute("""UPDATE menu_items SET name=COALESCE(?,name),
                        description=COALESCE(?,description), price_cents=COALESCE(?,price_cents),
                        section=COALESCE(?,section) WHERE id=?""",
                     (b.get("name"), b.get("description"), b.get("price_cents"),
                      b.get("section"), b["item_id"]))
        db().commit()
        return jsonify({"ok": True})
    if op == "toggle":
        db().execute("UPDATE menu_items SET active=1-active WHERE id=?", (b["item_id"],))
        db().commit()
        return jsonify({"ok": True})
    if op == "delete":
        gids = [g["id"] for g in db().execute("SELECT id FROM option_groups WHERE item_id=?",
                                              (b["item_id"],)).fetchall()]
        for gid in gids:
            db().execute("DELETE FROM options WHERE group_id=?", (gid,))
        db().execute("DELETE FROM option_groups WHERE item_id=?", (b["item_id"],))
        db().execute("DELETE FROM menu_items WHERE id=?", (b["item_id"],))
        db().commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown op"}), 400


@app.post("/api/dispatch/option-group")
def api_option_group():
    """Sub layers on an item: 'Pick your side', 'Spice', 'Drink' on a combo."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op == "create":
        cur = db().execute("""INSERT INTO option_groups(item_id,name,min_select,max_select,sort)
                              VALUES(?,?,?,?,?)""",
                           (b["item_id"], (b.get("name") or "Choose").strip(),
                            int(b.get("min_select", 1)), int(b.get("max_select", 1)),
                            int(b.get("sort") or 0)))
        db().commit()
        return jsonify({"ok": True, "group_id": cur.lastrowid})
    if op == "delete":
        db().execute("DELETE FROM options WHERE group_id=?", (b["group_id"],))
        db().execute("DELETE FROM option_groups WHERE id=?", (b["group_id"],))
        db().commit()
        return jsonify({"ok": True})
    if op == "update":
        db().execute("""UPDATE option_groups SET name=COALESCE(?,name),
                        min_select=COALESCE(?,min_select), max_select=COALESCE(?,max_select)
                        WHERE id=?""",
                     (b.get("name"), b.get("min_select"), b.get("max_select"), b["group_id"]))
        db().commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown op"}), 400


@app.post("/api/dispatch/option")
def api_option():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op == "create":
        cur = db().execute("""INSERT INTO options(group_id,name,price_delta_cents,sort)
                              VALUES(?,?,?,?)""",
                           (b["group_id"], (b.get("name") or "Option").strip(),
                            int(b.get("price_delta_cents") or 0), int(b.get("sort") or 0)))
        db().commit()
        return jsonify({"ok": True, "option_id": cur.lastrowid})
    if op == "delete":
        db().execute("DELETE FROM options WHERE id=?", (b["option_id"],))
        db().commit()
        return jsonify({"ok": True})
    if op == "update":
        db().execute("""UPDATE options SET name=COALESCE(?,name),
                        price_delta_cents=COALESCE(?,price_delta_cents) WHERE id=?""",
                     (b.get("name"), b.get("price_delta_cents"), b["option_id"]))
        db().commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown op"}), 400


@app.post("/api/dispatch/driver")
def api_driver_crud():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op == "create":
        name = (b.get("name") or "").strip()
        phone = digits(b.get("phone") or "")
        pin = (b.get("pin") or "").strip() or "1234"
        if not name or len(phone) < 10:
            return jsonify({"ok": False, "error": "Name and a 10 digit phone are required."}), 400
        if db().execute("SELECT 1 FROM drivers WHERE phone=?", (phone,)).fetchone():
            return jsonify({"ok": False, "error": "That phone is already on a driver."}), 400
        cur = db().execute("INSERT INTO drivers(name,phone,pin,status) VALUES(?,?,?,'offline')",
                           (name, phone, pin))
        db().commit()
        return jsonify({"ok": True, "driver_id": cur.lastrowid})
    if op == "update":
        phone = digits(b.get("phone") or "") if b.get("phone") else None
        if phone and db().execute("SELECT 1 FROM drivers WHERE phone=? AND id!=?",
                                  (phone, b["driver_id"])).fetchone():
            return jsonify({"ok": False, "error": "Another driver already uses that phone."}), 400
        db().execute("""UPDATE drivers SET name=COALESCE(?,name), phone=COALESCE(?,phone),
                        pin=COALESCE(?,pin) WHERE id=?""",
                     (b.get("name"), phone, (b.get("pin") or "").strip() or None, b["driver_id"]))
        db().commit()
        return jsonify({"ok": True})
    if op == "delete":
        live = db().execute("""SELECT COUNT(*) c FROM orders WHERE driver_id=?
                               AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                            (b["driver_id"],)).fetchone()["c"]
        if live:
            return jsonify({"ok": False, "error":
                            "That driver is on %d live order(s). Reassign them first." % live}), 400
        db().execute("DELETE FROM messages WHERE driver_id=?", (b["driver_id"],))
        db().execute("DELETE FROM drivers WHERE id=?", (b["driver_id"],))
        db().commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown op"}), 400


@app.get("/dispatch/manage")
def dispatch_manage():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_manage.html")


@app.get("/api/menu/<int:rid>")
def api_menu(rid):
    return jsonify({"ok": True, "items": menu_payload(rid)})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now()})

init_db()

def current_portal():
    """Which portal the signed-in person belongs to, if any."""
    if session.get("dispatcher_id"):
        return "dispatch"
    if session.get("driver_id"):
        return "driver"
    if session.get("restaurant_id"):
        return "kitchen"
    return None


@app.context_processor
def inject_portal():
    """Templates use this to show a portal's own menu and nothing else."""
    try:
        _ph = dispatch_phone()
        _d = "".join(c for c in _ph if c.isdigit())
        biz = {"biz_name": (setting("business_name", str) or "Fleet Delivery").strip() or "Fleet Delivery",
               "biz_address": (setting("business_address", str) or "").strip(),
               "biz_phone": ("(%s) %s-%s" % (_d[:3], _d[3:6], _d[6:])) if len(_d) == 10 else _ph,
               "biz_tel": tel_digits(_d),
               "tax_bp": setting("tax_rate_bp") or 0, "service_bp": setting("service_fee_bp") or 0}
    except Exception:
        biz = {"biz_name": "Fleet Delivery", "biz_address": "", "biz_phone": "", "biz_tel": "",
               "tax_bp": 900, "service_bp": 0}
    return {**biz, "portal": current_portal(),
            "portal_name": session.get("dispatcher_name") or session.get("driver_name")
                           or session.get("restaurant_name") or ""}


# Staff areas are invisible to anyone not signed in to that exact area. A driver
# who types the dispatch URL lands back on the driver app, a customer lands on
# the ordering site, and nobody sees a portal they do not belong to.
PORTAL_PREFIXES = (("/dispatch", "dispatch"), ("/driver", "driver"), ("/restaurant", "kitchen"))
PORTAL_HOME = {"dispatch": "/dispatch", "driver": "/driver", "kitchen": "/restaurant"}
PORTAL_LOGIN = {"dispatch": "/dispatch/login", "driver": "/driver/login", "kitchen": "/restaurant/login"}

@app.before_request
def guard_portals():
    path = request.path or "/"
    area = None
    for prefix, name in PORTAL_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            area = name
            break
    if area is None:
        return None
    if path == PORTAL_LOGIN[area] or path.endswith("/logout"):
        return None
    who = current_portal()
    if who == area:
        return None
    if who is None:
        if path.startswith("/api/"):
            return jsonify({"error": "Please sign in."}), 401
        return redirect(PORTAL_LOGIN[area])
    # signed in somewhere else: send them back to their own app, never show this one
    if path.startswith("/api/"):
        return jsonify({"error": "Not your area."}), 403
    return redirect(PORTAL_HOME[who])


# --- one app, four front doors -------------------------------------------
# Set SUBDOMAIN_ROUTING=1 and point these subdomains at the same deployment:
#   order.yourdomain.com     -> customer ordering site
#   dispatch.yourdomain.com  -> dispatcher portal
#   driver.yourdomain.com    -> driver app
#   kitchen.yourdomain.com   -> restaurant app
# Everything stays one Python process; the host name just picks the landing page
# and keeps each portal on its own address.
PORTAL_HOSTS = {
    "order": "/", "www": "/", "shop": "/",
    "dispatch": "/dispatch", "disp": "/dispatch",
    "driver": "/driver", "drivers": "/driver",
    "kitchen": "/restaurant", "restaurant": "/restaurant", "store": "/restaurant",
}

@app.before_request
def portal_front_door():
    if os.environ.get("SUBDOMAIN_ROUTING", "0") != "1":
        return None
    host = (request.host or "").split(":")[0].lower()
    sub = host.split(".")[0]
    home = PORTAL_HOSTS.get(sub)
    if home and home != "/" and request.path == "/":
        return redirect(home)
    return None


def dispatch_phone():
    return (setting("dispatch_phone", str) or "").strip()

def tel_digits(p):
    """Phone number in the form every phone dials: +1 and ten digits."""
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return ("+1" + d) if len(d) == 10 else d

def raise_call_alert(who, name, phone, note, driver_id=None, restaurant_id=None, order_id=None):
    """Somebody hit the call button. Put it on the dispatch board so it is not missed."""
    db().execute("""INSERT INTO call_alerts(who,driver_id,restaurant_id,order_id,name,phone,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (who, driver_id, restaurant_id, order_id, name, phone, note or "", now()))
    db().commit()
    log("call_alert", who + ": " + name + (" / " + note if note else ""))

def open_call_alerts():
    rows = db().execute("""SELECT a.*, o.code FROM call_alerts a
                           LEFT JOIN orders o ON o.id=a.order_id
                           WHERE a.cleared_at IS NULL ORDER BY a.id DESC LIMIT 20""").fetchall()
    out = []
    for a in rows:
        item = {"id": a["id"], "who": a["who"], "name": a["name"],
                "phone": a["phone"] or "", "tel": tel_digits(a["phone"]),
                "note": a["note"] or "", "order": a["code"] or "",
                "at": clock(a["created_at"]), "when": a["created_at"], "location": None}
        if a["who"] == "driver" and a["driver_id"]:
            d = db().execute("SELECT * FROM drivers WHERE id=?", (a["driver_id"],)).fetchone()
            if d:
                loc = loc_block(d)
                if loc and not loc.get("address"):
                    loc["address"] = update_driver_addr(d["id"], d["last_lat"], d["last_lng"])
                if loc:
                    loc["at"] = clock(d["last_loc_at"])
                item["location"] = loc
        out.append(item)
    return out


def accept_limit(key, default):
    try:
        return max(0, min(60, int(setting(key) or default)))
    except Exception:
        return default

def late_accepts():
    """Tickets the kitchen has not accepted, and pages a driver has not tapped Received on,
    past the minutes set in Settings. 0 turns a check off."""
    out = []
    n = dt.datetime.now()
    km, dm = accept_limit("kitchen_accept_min", 5), accept_limit("driver_accept_min", 3)
    if km:
        cut = (n - dt.timedelta(minutes=km)).isoformat(timespec="seconds")
        for x in db().execute("""SELECT o.id, o.code, o.restaurant_id, o.kitchen_sent_at, r.name rname FROM orders o
                                 LEFT JOIN restaurants r ON r.id=o.restaurant_id
                                 WHERE o.kitchen_status='pending' AND o.kitchen_sent_at IS NOT NULL
                                   AND o.kitchen_sent_at < ?
                                   AND o.dispatch_status NOT IN ('scheduled','awaiting_payment','cancelled','delivered')
                                 ORDER BY o.kitchen_sent_at""", (cut,)).fetchall():
            mins = int((n - dt.datetime.fromisoformat(x["kitchen_sent_at"])).total_seconds() // 60)
            out.append({"kind": "kitchen", "id": x["id"], "code": x["code"], "who": x["rname"] or "The kitchen",
                        "restaurant_id": x["restaurant_id"], "minutes": mins,
                        "loud": True})
    if dm:
        cut = (n - dt.timedelta(minutes=dm)).isoformat(timespec="seconds")
        for x in db().execute("""SELECT o.id, o.code, o.driver_id, o.driver_paged_at, d.name dname FROM orders o
                                 JOIN drivers d ON d.id=o.driver_id
                                 WHERE o.dispatch_status='assigned' AND o.driver_paged_at IS NOT NULL
                                   AND o.driver_paged_at < ?
                                 ORDER BY o.driver_paged_at""", (cut,)).fetchall():
            mins = int((n - dt.datetime.fromisoformat(x["driver_paged_at"])).total_seconds() // 60)
            out.append({"kind": "driver", "id": x["id"], "code": x["code"], "who": x["dname"],
                        "driver_id": x["driver_id"], "minutes": mins,
                        "loud": True})
    return out

def awaiting_accept():
    """Orders handed to a driver who has not tapped Received yet."""
    rows = db().execute("""SELECT o.id, o.code, o.restaurant_id, d.name dname, r.name rname FROM orders o
                           JOIN drivers d ON d.id=o.driver_id
                           LEFT JOIN restaurants r ON r.id=o.restaurant_id
                           WHERE o.dispatch_status='assigned' AND o.driver_id IS NOT NULL
                           ORDER BY o.id""").fetchall()
    return [{"id": x["id"], "code": x["code"], "driver": x["dname"], "restaurant": x["rname"] or ""} for x in rows]

@app.post("/api/dispatch/business")
def api_dispatch_business():
    """Open Business / Close. Closed shows drivers a closed screen instead of the app."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    on = bool((request.get_json(silent=True) or {}).get("open"))
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_open',?)", ("1" if on else "0",))
    if not on:
        # end of day: drivers start tomorrow with an empty Completed tab. Dispatch keeps the history.
        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('driver_done_cleared_at',?)", (now(),))
    # every open and every close starts chat fresh: driver chats, kitchen chats and mass texts
    cleared = purge_chats()
    db().commit()
    log("business", ("opened" if on else "closed") + " by " + (session.get("dispatcher_name") or "dispatch")
        + ("; cleared %d chat messages" % cleared if cleared else ""))
    try:
        if cleared:
            db().execute("VACUUM")
    except Exception:
        pass
    return jsonify({"ok": True, "business_open": on, "chats_cleared": cleared})

def purge_chats():
    """Deletes every driver chat, restaurant chat and mass text. Returns how many went."""
    n = 0
    for table in ("messages", "rest_messages", "broadcasts"):
        n += db().execute("SELECT COUNT(*) c FROM " + table).fetchone()["c"]
        db().execute("DELETE FROM " + table)
    return n

def business_is_open():
    try:
        return str(setting("business_open", str) or "0") == "1"
    except Exception:
        return False

@app.get("/api/dispatch/alerts")
def api_dispatch_alerts():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "alerts": open_call_alerts(), "awaiting": awaiting_accept()})

@app.post("/api/driver/call-dispatch")
def api_driver_call_dispatch():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    data = request.get_json(silent=True) or {}
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        lat = lng = None
    if lat is not None and d["status"] != "offline":
        db().execute("UPDATE drivers SET last_lat=?,last_lng=?,last_loc_at=? WHERE id=?", (lat, lng, now(), did))
        db().commit()
        update_driver_addr(did, lat, lng, force=True)
    raise_call_alert("driver", d["name"], d["phone"], (data.get("note") or "").strip()[:160],
                     driver_id=did, order_id=data.get("order_id") or None)
    return jsonify({"ok": True, "phone": dispatch_phone()})

@app.post("/api/restaurant/call-dispatch")
def api_rest_call_dispatch():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    o = db().execute("SELECT * FROM orders WHERE code=? AND restaurant_id=?", (code, rid)).fetchone() if code else None
    raise_call_alert("restaurant", r["name"], r["phone"], (data.get("note") or "").strip()[:160],
                     restaurant_id=rid, order_id=(o["id"] if o else None))
    return jsonify({"ok": True, "phone": dispatch_phone()})

@app.post("/api/dispatch/alert-clear")
def api_alert_clear():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(silent=True) or {}
    aid = data.get("id")
    if aid:
        db().execute("UPDATE call_alerts SET cleared_at=? WHERE id=?", (now(), aid))
    else:
        db().execute("UPDATE call_alerts SET cleared_at=? WHERE cleared_at IS NULL", (now(),))
    db().commit()
    return jsonify({"ok": True, "alerts": open_call_alerts()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
