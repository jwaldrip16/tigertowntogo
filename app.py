
import base64, difflib, os, json, math, re, secrets, sqlite3, threading, time, datetime as dt, urllib.parse, urllib.request
import dbx
from flask import Flask, g, request, session, redirect, url_for, render_template, jsonify, send_from_directory
import presets

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
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
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
                 ("tax_rate_bp", "900"), ("service_fee_bp", "0"), ("business_open", "0"), ("future_lead_min", "45"), ("kitchen_accept_min", "5"), ("driver_accept_min", "3"), ("late_sound_after_min", "3"), ("driver_done_cleared_at", ""), ("auto_assign", "1"), ("max_stack_default", "3"), ("sched_lead_min", "60"),
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
    ensure_column(con, "menu_items", "image", "TEXT")
    ensure_column(con, "menu_items", "menu_tab", "TEXT")
    ensure_column(con, "menu_items", "avail_days", "TEXT")
    ensure_column(con, "menu_items", "avail_start", "TEXT")
    ensure_column(con, "menu_items", "avail_end", "TEXT")
    ensure_column(con, "restaurants", "image", "TEXT")
    ensure_column(con, "option_groups", "max_each", "INTEGER NOT NULL DEFAULT 1")
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
    ensure_column(con, "dispatchers", "phone", "TEXT")
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
    con.execute("""CREATE TABLE IF NOT EXISTS regions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT UNIQUE NOT NULL, sort INTEGER DEFAULT 0, created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_regions (driver_id INTEGER NOT NULL, region_id INTEGER NOT NULL,
                   PRIMARY KEY(driver_id, region_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS dispatcher_regions (dispatcher_id INTEGER NOT NULL,
                   region_id INTEGER NOT NULL, PRIMARY KEY(dispatcher_id, region_id))""")
    ensure_column(con, "restaurants", "region_id", "INTEGER")
    ensure_column(con, "orders", "region_id", "INTEGER")
    ensure_column(con, "availability", "region_ids", "TEXT")
    ensure_column(con, "dispatchers", "is_owner", "INTEGER DEFAULT 0")
    ensure_column(con, "messages", "region_id", "INTEGER")
    con.execute("""CREATE TABLE IF NOT EXISTS dispatcher_availability (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   dispatcher_id INTEGER NOT NULL, dow INTEGER NOT NULL, start_time TEXT NOT NULL,
                   end_time TEXT NOT NULL, note TEXT, created_by TEXT, created_at TEXT)""")
    ensure_column(con, "dispatcher_availability", "region_ids", "TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   driver_id INTEGER NOT NULL, lat REAL, lng REAL, address TEXT, status TEXT,
                   event TEXT, created_at TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_driver_log ON driver_log(driver_id, created_at)")
    ensure_column(con, "orders", "ref_code", "TEXT")
    ensure_column(con, "orders", "paged_at", "TEXT")
    ensure_column(con, "orders", "kitchen_sent_at", "TEXT")
    # Restaurants not on the restaurant app yet: dispatch places the order with them by hand.
    ensure_column(con, "restaurants", "uses_app", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "manual_state", "TEXT")
    # PayPal / Venmo / card through PayPal: hold at checkout, charge after delivery (late tips included)
    for _c, _d in (("pp_order_id", "TEXT"), ("pp_auth_id", "TEXT"), ("pp_auth_cents", "INTEGER"),
                   ("pp_state", "TEXT"), ("pp_captured_cents", "INTEGER"), ("pp_source", "TEXT"),
                   ("pp_error", "TEXT"), ("pp_auth_at", "TEXT")):
        ensure_column(con, "orders", _c, _d)
    ensure_column(con, "orders", "manual_at", "TEXT")
    ensure_column(con, "orders", "manual_by", "TEXT")
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
    # A finished order never keeps asking the kitchen to accept it, whichever screen finished it.
    con.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_done_kitchen_ready AFTER UPDATE OF dispatch_status ON orders
      WHEN NEW.dispatch_status='delivered' AND NEW.kitchen_status IN ('pending','preparing')
      BEGIN UPDATE orders SET kitchen_status='ready', ready_at=COALESCE(ready_at, """ + _ts + """) WHERE id=NEW.id; END;
    CREATE TRIGGER IF NOT EXISTS trg_cancel_kitchen_off AFTER UPDATE OF dispatch_status ON orders
      WHEN NEW.dispatch_status='cancelled' AND NEW.kitchen_status IN ('pending','preparing')
      BEGIN UPDATE orders SET kitchen_status='waiting' WHERE id=NEW.id; END;
    """)
    con.execute("""UPDATE orders SET kitchen_status='ready', ready_at=COALESCE(ready_at, delivered_at, created_at)
                   WHERE dispatch_status='delivered' AND kitchen_status IN ('pending','preparing')""")
    con.execute("""UPDATE orders SET kitchen_status='waiting'
                   WHERE dispatch_status='cancelled' AND kitchen_status IN ('pending','preparing')""")
    con.execute("""UPDATE orders SET kitchen_sent_at=created_at WHERE kitchen_sent_at IS NULL
                   AND kitchen_status='pending'""")
    # pull back unpaid card orders an address approval sent to the kitchen early
    con.execute("""UPDATE orders SET kitchen_status='waiting', kitchen_sent_at=NULL
                   WHERE dispatch_status='awaiting_payment' AND kitchen_status='pending'""")
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
    presets.autoload(con)
    con.commit()
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

# ---------------------------------------------------------------- regions
# A restaurant belongs to one region. Drivers and dispatchers can cover several regions;
# none checked means they cover every region. An order takes its restaurant's region
# (a typed-in pickup is matched by the town in its address). Region 0 means no region,
# which every driver and dispatcher sees.

DEFAULT_REGIONS = ("Opelika", "Auburn", "Valley")


def all_regions():
    return db().execute("SELECT id, name FROM regions ORDER BY sort, name").fetchall()


def region_match(text):
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    for r in all_regions():
        if " " + re.sub(r"[^a-z0-9]+", " ", r["name"].lower()).strip() + " " in t:
            return r["id"]
    return 0


def stamp_regions():
    """Seed the starting regions once, then give any new restaurant or order its region."""
    con = db()
    if not con.execute("SELECT 1 FROM regions LIMIT 1").fetchone():
        if not con.execute("SELECT 1 FROM settings WHERE key='regions_seeded'").fetchone():
            for i, n in enumerate(DEFAULT_REGIONS):
                con.execute("INSERT OR IGNORE INTO regions(name,sort,created_at) VALUES(?,?,?)", (n, i, now()))
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('regions_seeded','1')")
    if not con.execute("SELECT 1 FROM dispatchers WHERE is_owner=1").fetchone():
        con.execute("UPDATE dispatchers SET is_owner=1 WHERE id=(SELECT MIN(id) FROM dispatchers)")
    for r in con.execute("""SELECT id, address FROM restaurants
                            WHERE region_id IS NULL AND slug!='oneoff'""").fetchall():
        con.execute("UPDATE restaurants SET region_id=? WHERE id=?", (region_match(r["address"]), r["id"]))
    for o in con.execute("""SELECT o.id, o.pickup_address, r.slug, r.region_id FROM orders o
                            LEFT JOIN restaurants r ON r.id=o.restaurant_id
                            WHERE o.region_id IS NULL""").fetchall():
        rg = region_match(o["pickup_address"]) if (o["slug"] == "oneoff" or o["pickup_address"]) else (o["region_id"] or 0)
        con.execute("UPDATE orders SET region_id=? WHERE id=?", (rg or 0, o["id"]))
    con.commit()


def driver_region_ids(did):
    return {r["region_id"] for r in db().execute(
        "SELECT region_id FROM driver_regions WHERE driver_id=?", (did,)).fetchall()}


def dispatcher_region_ids(did):
    return {r["region_id"] for r in db().execute(
        "SELECT region_id FROM dispatcher_regions WHERE dispatcher_id=?", (did,)).fetchall()} if did else set()


def clean_region_ids(lst):
    """Checked regions from a form -> '1,3'. Blank means the person's usual regions."""
    valid = {r["id"] for r in all_regions()}
    out = set()
    for x in (lst if isinstance(lst, (list, tuple, set)) else str(lst or "").split(",")):
        try:
            x = int(x)
        except (TypeError, ValueError):
            continue
        if x in valid:
            out.add(x)
    return ",".join(str(x) for x in sorted(out))


def parse_rids(s):
    return {int(x) for x in str(s or "").split(",") if x.strip().isdigit()}


def slot_regions(s):
    try:
        return parse_rids(s["region_ids"])
    except (IndexError, KeyError):
        return set()


def driver_work_regions(did):
    """Regions a driver works right now: the regions picked on the availability they are
    working at this moment, otherwise their usual regions."""
    nowdt = dt.datetime.now()
    today = nowdt.date()
    hm = nowdt.strftime("%H:%M")
    picked = set()
    for s in db().execute("""SELECT * FROM availability WHERE driver_id=? AND status='approved'
                             AND COALESCE(region_ids,'')!=''""", (did,)).fetchall():
        if s["week_start"]:
            try:
                if dt.date.fromisoformat(s["week_start"]) + dt.timedelta(days=s["dow"]) != today:
                    continue
            except ValueError:
                continue
        elif s["dow"] != today.weekday():
            continue
        if s["start_time"] <= hm < s["end_time"]:
            picked |= parse_rids(s["region_ids"])
    return picked or driver_region_ids(did)


def dispatcher_work_regions(did):
    if not did:
        return set()
    nowdt = dt.datetime.now()
    picked = set()
    for s in db().execute("""SELECT * FROM dispatcher_availability WHERE dispatcher_id=?
                             AND COALESCE(region_ids,'')!=''""", (did,)).fetchall():
        if _disp_slot_on(s, nowdt):
            picked |= parse_rids(s["region_ids"])
    return picked or dispatcher_region_ids(did)


def is_owner(did=None):
    did = did if did is not None else session.get("dispatcher_id")
    if not did:
        return False
    r = db().execute("SELECT is_owner FROM dispatchers WHERE id=?", (did,)).fetchone()
    return bool(r and r["is_owner"])


def covers(region_ids, order_region):
    """Does someone covering these regions see an order in this region? No regions = all."""
    return not region_ids or not order_region or order_region in region_ids


def region_names(ids):
    if not ids:
        return "All regions"
    names = [r["name"] for r in all_regions() if r["id"] in ids]
    return ", ".join(names) or "All regions"


def on_shift_drivers():
    """Everyone clocked on, in rotation order: fewest live orders first, then whoever has
    waited longest since their last one."""
    return db().execute("""
        SELECT d.*, (SELECT COUNT(*) FROM orders o
                     WHERE o.driver_id=d.id
                       AND o.dispatch_status NOT IN ('delivered','cancelled')) AS load
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


def line_positions():
    """Where each on-shift driver stands for the next order, shown on the driver app and
    the board. Separate from auto dispatch: a lone driver is always first in line, even
    though with one driver on shift the dispatcher hands orders out by hand. A driver only
    drops out of line when other drivers are on shift and they are holding their stack limit."""
    rows = on_shift_drivers()
    out = {}
    if len(rows) == 1:
        r = rows[0]
        out[r["id"]] = {"pos": 1, "at_limit": r["load"] >= (r["max_stack"] or 1)}
        return out
    n = 0
    for r in rows:
        full = r["load"] >= (r["max_stack"] or 1)
        if full:
            out[r["id"]] = {"pos": None, "at_limit": True}
        else:
            n += 1
            out[r["id"]] = {"pos": n, "at_limit": False}
    return out


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
    pool = list(free)
    for i, o in enumerate(waiting):
        fit = next((fd for fd in pool if covers(driver_work_regions(fd["id"]), o["region_id"])), None)
        if o["address_ok"] == 0:
            status, reason = "held", "address needs dispatch approval"
        elif o["kitchen_status"] not in stages:
            status, reason = "held", "waiting on kitchen"
        elif not auto_on:
            status, reason = "queued", "auto dispatch off, assign by hand"
        elif fit is None:
            status, reason = "held", short
            if len(shift) >= 2 and o["region_id"] and not any(
                    covers(driver_work_regions(sd["id"]), o["region_id"]) for sd in shift):
                status, reason = "held", "no driver in this region on shift, dispatcher to assign"
        else:
            pool.remove(fit)
            status, reason = "queued", None
        con.execute("UPDATE orders SET dispatch_status=?, hold_reason=? WHERE id=?",
                    (status, reason, o["id"]))
    con.commit()

def queue_position(order_id):
    me = db().execute("SELECT region_id FROM orders WHERE id=?", (order_id,)).fetchone()
    rg = (me["region_id"] if me else 0) or 0
    rows = db().execute("""SELECT id FROM orders WHERE dispatch_status IN ('queued','held')
                           AND (? = 0 OR COALESCE(region_id,0) IN (0, ?))
                           ORDER BY created_at ASC, id ASC""", (rg, rg)).fetchall()
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
        stamp_regions()
    except Exception as e:
        print("region stamp skipped:", e)
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
        waiting = con.execute("""SELECT * FROM orders
                           WHERE driver_id IS NULL AND redo_driver_id IS NULL
                             AND dispatch_status IN ('queued','held')
                             AND kitchen_status IN (""" + stages + """)
                           ORDER BY created_at ASC""").fetchall()
        pick = None
        for cand in waiting:
            for fd in free:
                if covers(driver_work_regions(fd["id"]), cand["region_id"]):
                    pick = (cand, fd)
                    break
            if pick:
                break
        if not pick:
            break
        o, d = pick
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
    rebalance_stacks()
    recompute_queue()


def rebalance_stacks():
    """A second order stacked on a busy driver moves to a free driver once one is on shift.
    Only orders the driver has not tapped Received on yet, never one a driver placed himself,
    so nothing already in someone's hands gets pulled away."""
    con = db()
    moved = set()
    while True:
        free = available_drivers()
        if not free:
            break
        cands = con.execute("""SELECT o.* FROM orders o
                           WHERE o.dispatch_status='assigned' AND o.driver_id IS NOT NULL
                             AND COALESCE(o.placed_by,'') != 'driver'
                             AND EXISTS (SELECT 1 FROM orders x
                                         WHERE x.driver_id=o.driver_id AND x.id!=o.id
                                           AND x.dispatch_status NOT IN ('delivered','cancelled')
                                           AND (COALESCE(x.stack_seq,0) < COALESCE(o.stack_seq,0)
                                                OR (COALESCE(x.stack_seq,0) = COALESCE(o.stack_seq,0) AND x.id < o.id)))
                           ORDER BY o.created_at ASC""").fetchall()
        pick = None
        for cand in cands:
            if cand["id"] in moved:
                continue
            for fd in free:
                if fd["id"] != cand["driver_id"] and covers(driver_work_regions(fd["id"]), cand["region_id"]):
                    pick = (cand, fd)
                    break
            if pick:
                break
        if not pick:
            break
        o, d = pick
        moved.add(o["id"])
        old = con.execute("SELECT id, name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone()
        con.execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned', hold_reason=NULL,
                       stack_seq=1 WHERE id=?""", (d["id"], o["id"]))
        con.execute("UPDATE drivers SET last_assigned_at=? WHERE id=?", (now(), d["id"]))
        con.execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                    (d["id"], "system", "Order " + o["code"] + " assigned to you (stop #1).", now()))
        if old:
            con.execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                        (old["id"], "system", "Order " + o["code"] + " moved to another driver.", now()))
        log("assign", o["code"] + " -> " + d["name"] + " (moved off " + (old["name"] if old else "?") + ")")
        con.commit()

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
                       "max": g["max_select"], "each": max(1, int(g["max_each"] or 1)),
                       "options": [{"id": o["id"], "name": o["name"],
                                    "delta_cents": o["price_delta_cents"],
                                    "delta": money(o["price_delta_cents"])} for o in opts]})
    return groups


AV_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _hm(s):
    try:
        h, m = str(s).strip().split(":")[:2]
        h, m = int(h), int(m[:2])
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except Exception:
        return None


def _ampm(s):
    v = _hm(s)
    if v is None:
        return ""
    h, m = divmod(v, 60)
    return "%d:%02d %s" % ((h % 12) or 12, m, "AM" if h < 12 else "PM")


def item_avail_days(it):
    raw = (it["avail_days"] if "avail_days" in it.keys() else "") or ""
    return sorted({int(x) for x in str(raw).split(",") if x.strip().isdigit() and 0 <= int(x) <= 6})


def avail_label(it):
    """'Mon - Fri: 11:00 AM - 1:30 PM', or '' when the item can be ordered any time."""
    days = item_avail_days(it)
    st = (it["avail_start"] if "avail_start" in it.keys() else "") or ""
    en = (it["avail_end"] if "avail_end" in it.keys() else "") or ""
    timed = bool(st and en and _hm(st) is not None and _hm(en) is not None)
    if not days and not timed:
        return ""
    if not days or len(days) == 7:
        dl = "Every day"
    else:
        runs, start, prev = [], days[0], days[0]
        for d in days[1:]:
            if d == prev + 1:
                prev = d
                continue
            runs.append((start, prev))
            start = prev = d
        runs.append((start, prev))
        parts = []
        for a, b in runs:
            if b - a >= 2:
                parts.append(AV_DAYS[a] + " - " + AV_DAYS[b])
            else:
                parts += [AV_DAYS[x] for x in range(a, b + 1)]
        dl = ", ".join(parts)
    return dl + (": " + _ampm(st) + " - " + _ampm(en) if timed else "")


def item_available(it, when=None):
    """Can this item be ordered for this moment (now, or a future order's time)?"""
    when = when or dt.datetime.now()
    days = item_avail_days(it)
    if days and when.weekday() not in days:
        return False
    s = _hm((it["avail_start"] if "avail_start" in it.keys() else "") or "")
    e = _hm((it["avail_end"] if "avail_end" in it.keys() else "") or "")
    if s is None or e is None or s == e:
        return True
    m = when.hour * 60 + when.minute
    return (s <= m < e) if e > s else (m >= s or m < e)


def avail_fields(it):
    return {"avail_days": item_avail_days(it),
            "avail_start": (it["avail_start"] or "") if "avail_start" in it.keys() else "",
            "avail_end": (it["avail_end"] or "") if "avail_end" in it.keys() else "",
            "avail_label": avail_label(it), "available_now": item_available(it)}


def clean_avail(b):
    """Availability sent from the menu editor. None when the request did not touch it."""
    if not any(k in b for k in ("avail_days", "avail_start", "avail_end")):
        return None
    days = set()
    for x in (b.get("avail_days") or []):
        try:
            x = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= x <= 6:
            days.add(x)
    st = str(b.get("avail_start") or "").strip()[:5]
    en = str(b.get("avail_end") or "").strip()[:5]
    if bool(st) != bool(en):
        return {"err": "Set both a start and an end time, or leave both blank."}
    if st and (_hm(st) is None or _hm(en) is None):
        return {"err": "Times need to look like 11:00 AM."}
    if st and _hm(st) == _hm(en):
        return {"err": "The start and end time cannot be the same."}
    days = sorted(days)
    if len(days) == 7:
        days = []
    return {"days": ",".join(str(d) for d in days), "st": st, "en": en}


def menu_payload(rid):
    """Items in menu order: sections in the order a dispatcher set, items inside them."""
    out = []
    for it in db().execute("""SELECT * FROM menu_items WHERE restaurant_id=? AND active=1
                              ORDER BY sort, id""", (rid,)).fetchall():
        out.append({"id": it["id"], "name": it["name"], "description": it["description"],
                    "price_cents": it["price_cents"], "price": money(it["price_cents"]),
                    "section": (it["section"] or "").strip(),
                    "tab": (it["menu_tab"] or "").strip(),
                    "image": media_url(it["image"]),
                    "groups": item_options(it["id"]), **avail_fields(it)})
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
        "needs_address_approval": not o["address_ok"],
        "dispatch_status": o["dispatch_status"], "hold_reason": o["hold_reason"],
        "issue": o["issue"] or "", "issue_note": o["issue_note"] or "",
        "cloned_from": o["cloned_from"] or "",
        "driver": d["name"] if d else None, "driver_id": o["driver_id"], "stack_seq": o["stack_seq"],
        "prep_minutes": o["prep_minutes"], "timer_seconds": eta,
        "placed_by": o["placed_by"], "created_at": o["created_at"],
        "delivered_at": o["delivered_at"],
        "queue_position": queue_position(o["id"]),
        "uses_app": bool(r["uses_app"]) if r is not None and "uses_app" in r.keys() else True,
        "manual_state": (o["manual_state"] or "") if "manual_state" in o.keys() else "",
        "manual_by": (o["manual_by"] or "") if "manual_by" in o.keys() else "",
        "pp": pp_info(o),
    }

# ---------------------------------------------------------------- customer site

@app.route("/")
def home():
    rs = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    biz = business_is_open() and business_in_hours()
    cards = [{"r": r, "open": biz and is_open(r), "hours": hours_label(r)} for r in rs]
    return render_template("index.html", cards=cards, biz_open=biz,
                           any_on=any_rest_on(), any_open=biz and any_rest_open())

@app.route("/r/<slug>")
def menu(slug):
    r = db().execute("SELECT * FROM restaurants WHERE slug=?", (slug,)).fetchone()
    if not r:
        return redirect(url_for("home"))
    if r["slug"] == "oneoff" and not any_rest_on():
        return redirect(url_for("home"))
    items = db().execute("SELECT * FROM menu_items WHERE restaurant_id=? AND active=1", (r["id"],)).fetchall()
    biz = business_is_open() and business_in_hours()
    open_now = biz and (any_rest_open() if r["slug"] == "oneoff" else is_open(r))
    return render_template("menu.html", r=r, items=items, open=open_now, biz_open=biz,
                           hours=hours_label(r), custom=(r["slug"] == "oneoff"),
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


# ---------------------------------------------------------------- PayPal, Venmo and cards through PayPal
# Set PAYPAL_CLIENT_ID and PAYPAL_SECRET (and PAYPAL_ENV=live when you go live; sandbox otherwise).
# At checkout the money is only held (authorized). After delivery the site charges the final total,
# so a tip added after delivery is included. PayPal lets that charge run up to 15% or $75 over the
# hold, whichever is less; anything past that shows on the tracking page as a small Pay the rest button.
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "").strip()
PAYPAL_ENV = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").strip().lower()
PP_BASE = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"
PP_SOURCES = {"venmo": "Venmo", "paypal": "PayPal", "card": "card"}
_pp_tok = {"t": "", "exp": 0.0}
_pp_lock = threading.Lock()
_pp_last_sweep = [0.0]

def pp_enabled():
    return bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)

def pp_token():
    with _pp_lock:
        if _pp_tok["t"] and time.time() < _pp_tok["exp"] - 60:
            return _pp_tok["t"]
        auth = base64.b64encode((PAYPAL_CLIENT_ID + ":" + PAYPAL_SECRET).encode()).decode()
        req = urllib.request.Request(PP_BASE + "/v1/oauth2/token", data=b"grant_type=client_credentials",
                                     headers={"Authorization": "Basic " + auth,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read())
        _pp_tok["t"], _pp_tok["exp"] = j["access_token"], time.time() + int(j.get("expires_in", 3000))
        return _pp_tok["t"]

def pp_api(method, path, body=None, request_id=None):
    """Returns (http status, json). Never raises."""
    try:
        h = {"Authorization": "Bearer " + pp_token(), "Content-Type": "application/json",
             "Prefer": "return=representation"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(PP_BASE + path, data=data, headers=h, method=method)
        resp = urllib.request.urlopen(req, timeout=20)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"message": str(e)}

def pp_money(cents):
    return {"currency_code": "USD", "value": "%.2f" % (int(cents) / 100.0)}

def pp_err(j, fallback="PayPal did not accept that."):
    d = (j.get("details") or [{}])[0] if isinstance(j, dict) else {}
    issue = d.get("issue") or (j.get("name") if isinstance(j, dict) else "") or ""
    if issue == "INSTRUMENT_DECLINED":
        return "That card or account was declined. Try another way to pay."
    return (d.get("description") or (j.get("message") if isinstance(j, dict) else "") or fallback)

def pp_cap_cents(auth_cents):
    """Most PayPal lets us charge on a hold: 115% of it or $75 more, whichever is less."""
    a = int(auth_cents or 0)
    return min(a * 115 // 100, a + 7500)

def pp_tip_window_min():
    try:
        v = int(setting("pp_tip_window_min", str) or 60)
    except Exception:
        v = 60
    return max(0, min(v, 1440))

def pp_settle(o, why="after delivery"):
    """Charge the held payment for the order's current total (late tip included)."""
    if (o["pp_state"] or "") != "authorized" or not o["pp_auth_id"]:
        return {"ok": False, "error": "No PayPal hold on this order."}
    auth = int(o["pp_auth_cents"] or 0)
    due = int(o["total_cents"] or 0) - int(o["refunded_cents"] or 0)
    if due <= 0:
        return pp_void(o, "nothing owed")
    amt = min(due, pp_cap_cents(auth))
    st, j = pp_api("POST", "/v2/payments/authorizations/" + o["pp_auth_id"] + "/capture",
                   {"amount": pp_money(amt), "final_capture": True, "invoice_id": o["code"]},
                   request_id="cap-" + o["code"] + "-" + str(amt))
    if st not in (200, 201) and amt > auth:
        # Some accounts are not allowed to go over the hold: take the hold, the rest becomes a balance.
        amt = min(due, auth)
        st, j = pp_api("POST", "/v2/payments/authorizations/" + o["pp_auth_id"] + "/capture",
                       {"amount": pp_money(amt), "final_capture": True, "invoice_id": o["code"]},
                       request_id="cap-" + o["code"] + "-" + str(amt))
    if st in (200, 201) and (j.get("status") in ("COMPLETED", "PENDING")):
        db().execute("""UPDATE orders SET pp_state='captured', pp_captured_cents=?, paid_cents=?,
                        pay_ref=?, pp_error=NULL WHERE id=?""", (amt, amt, j.get("id", ""), o["id"]))
        db().commit()
        log("payment", o["code"] + " charged " + money(amt) + " on " +
            PP_SOURCES.get(o["pp_source"] or "", "PayPal") + " (" + why + ")" +
            ("" if amt >= due else ", " + money(due - amt) + " left for the customer to pay"))
        return {"ok": True, "charged": money(amt), "left": money(max(0, due - amt))}
    msg = pp_err(j, "PayPal would not charge the hold.")
    db().execute("UPDATE orders SET pp_error=? WHERE id=?", (msg[:300], o["id"]))
    db().commit()
    log("payment", o["code"] + " PayPal charge failed: " + msg)
    return {"ok": False, "error": msg}

def pp_void(o, why="cancelled"):
    if (o["pp_state"] or "") != "authorized" or not o["pp_auth_id"]:
        return {"ok": False, "error": "No PayPal hold on this order."}
    st, j = pp_api("POST", "/v2/payments/authorizations/" + o["pp_auth_id"] + "/void")
    if st in (200, 204):
        db().execute("UPDATE orders SET pp_state='voided', paid_cents=0, pp_error=NULL WHERE id=?", (o["id"],))
        db().commit()
        log("payment", o["code"] + " PayPal hold released (" + why + ")")
        return {"ok": True, "voided": True}
    msg = pp_err(j, "PayPal would not release the hold.")
    db().execute("UPDATE orders SET pp_error=? WHERE id=?", (msg[:300], o["id"]))
    db().commit()
    return {"ok": False, "error": msg}

def pp_sweep(force=False):
    """Charge delivered orders once the tip window is over; release holds on cancelled ones."""
    if not pp_enabled():
        return
    if not force and time.time() - _pp_last_sweep[0] < 30:
        return
    _pp_last_sweep[0] = time.time()
    cut = (dt.datetime.now() - dt.timedelta(minutes=pp_tip_window_min())).isoformat(timespec="seconds")
    try:
        for o in db().execute("""SELECT * FROM orders WHERE pp_state='authorized' AND pp_error IS NULL
                                 AND ((dispatch_status='delivered' AND COALESCE(delivered_at,'') <= ?)
                                      OR dispatch_status='cancelled')""", (cut,)).fetchall():
            if o["dispatch_status"] == "cancelled":
                pp_void(o)
            else:
                pp_settle(o, "tip window over")
    except Exception as e:
        print("paypal sweep skipped:", e)

def pp_info(o):
    """Payment bits the tracking page and the dispatch card need."""
    st = (o["pp_state"] or "") if "pp_state" in o.keys() else ""
    owed = balance_cents(o) if st == "captured" else 0
    return {"enabled": pp_enabled(), "state": st, "source": PP_SOURCES.get(o["pp_source"] or "", ""),
            "pay_url": "/pay/" + o["code"],
            "can_pay": pp_enabled() and o["dispatch_status"] == "awaiting_payment" and st != "authorized",
            "tip_editable": st in ("authorized", "captured") and o["dispatch_status"] != "cancelled",
            "owed_cents": max(0, owed), "owed": money(max(0, owed)),
            "held": money(o["pp_auth_cents"] or 0) if st else "",
            "charged": money(o["pp_captured_cents"] or 0) if st == "captured" else "",
            "error": (o["pp_error"] or "") if "pp_error" in o.keys() else "",
            "tip_window_min": pp_tip_window_min()}

@app.route("/pay/<code>")
def pay_page(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return render_template("pay.html", order=None, code=code, pp_ready=pp_enabled())
    r = db().execute("SELECT name FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    kind = "balance" if request.args.get("kind") == "balance" else "order"
    info = pp_info(o)
    amount = info["owed"] if kind == "balance" else money(o["total_cents"])
    return render_template("pay.html", order=o, code=code, kind=kind, amount=amount, info=info,
                           rname=(o["pickup_name"] or (r["name"] if r else "")),
                           pp_ready=pp_enabled(), client_id=PAYPAL_CLIENT_ID,
                           is_dispatch=bool(dispatcher_required()))

@app.post("/api/paypal/create")
def api_pp_create():
    if not pp_enabled():
        return jsonify({"ok": False, "error": "PayPal is not set up yet."}), 400
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE code=?", ((b.get("code") or "").strip(),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    kind = "balance" if b.get("kind") == "balance" else "order"
    if kind == "order":
        if o["dispatch_status"] != "awaiting_payment" or (o["pp_state"] or "") == "authorized":
            return jsonify({"ok": False, "error": "This order is already paid."}), 400
        cents, intent = int(o["total_cents"]), "AUTHORIZE"
    else:
        cents, intent = pp_info(o)["owed_cents"], "CAPTURE"
        if cents <= 0:
            return jsonify({"ok": False, "error": "Nothing is owed on this order."}), 400
    st, j = pp_api("POST", "/v2/checkout/orders", {
        "intent": intent,
        "purchase_units": [{"reference_id": o["code"], "custom_id": o["code"],
                            "description": ("Delivery order " if kind == "order" else "Rest of tip, order ") + o["code"],
                            "amount": pp_money(cents)}],
        "application_context": {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                                "brand_name": (setting("business_name", str) or "Fleet Delivery")[:120]}})
    if st not in (200, 201) or not j.get("id"):
        return jsonify({"ok": False, "error": pp_err(j, "PayPal could not start the payment.")}), 400
    if kind == "order":
        db().execute("UPDATE orders SET pp_order_id=? WHERE id=?", (j["id"], o["id"]))
        db().commit()
    return jsonify({"ok": True, "id": j["id"]})

@app.post("/api/paypal/approve")
def api_pp_approve():
    if not pp_enabled():
        return jsonify({"ok": False, "error": "PayPal is not set up yet."}), 400
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE code=?", ((b.get("code") or "").strip(),)).fetchone()
    ppid = (b.get("id") or "").strip()
    if not o or not ppid:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if b.get("kind") == "balance":
        st, j = pp_api("POST", "/v2/checkout/orders/" + ppid + "/capture", request_id="bal-" + ppid)
        cap = (((j.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
        if st not in (200, 201) or cap.get("status") not in ("COMPLETED", "PENDING"):
            return jsonify({"ok": False, "error": pp_err(j, "The payment did not go through.")}), 400
        if (((j.get("purchase_units") or [{}])[0]).get("reference_id")) not in (None, o["code"]):
            return jsonify({"ok": False, "error": "That payment is for a different order."}), 400
        cents = int(round(float(cap["amount"]["value"]) * 100))
        src = next(iter(j.get("payment_source") or {"paypal": 1}))
        add_extra_charge(o, cents, "Rest of tip (" + PP_SOURCES.get(src, "PayPal") + ")", cap.get("id", ""))
        db().execute("UPDATE orders SET pp_captured_cents=COALESCE(pp_captured_cents,0)+? WHERE id=?", (cents, o["id"]))
        db().commit()
        log("payment", o["code"] + " customer paid the rest, " + money(cents))
        return jsonify({"ok": True, "paid": money(cents)})
    if ppid != (o["pp_order_id"] or ""):
        return jsonify({"ok": False, "error": "That payment is for a different order."}), 400
    if (o["pp_state"] or "") == "authorized":
        return jsonify({"ok": True, "already": True})
    st, j = pp_api("POST", "/v2/checkout/orders/" + ppid + "/authorize", request_id="auth-" + ppid)
    auth = (((j.get("purchase_units") or [{}])[0].get("payments") or {}).get("authorizations") or [{}])[0]
    if st not in (200, 201) or auth.get("status") not in ("CREATED", "PENDING") or not auth.get("id"):
        return jsonify({"ok": False, "error": pp_err(j, "The payment did not go through.")}), 400
    cents = int(round(float(auth["amount"]["value"]) * 100))
    src = next(iter(j.get("payment_source") or {"paypal": 1}))
    db().execute("""UPDATE orders SET pp_auth_id=?, pp_auth_cents=?, pp_state='authorized', pp_source=?,
                    pp_error=NULL, pp_auth_at=? WHERE id=?""", (auth["id"], cents, src, now(), o["id"]))
    db().commit()
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    mark_paid(o, src if src in ("venmo", "paypal") else "card_paypal", auth["id"], cents)
    return jsonify({"ok": True, "held": money(cents), "source": PP_SOURCES.get(src, "PayPal")})

def pp_refund(o, cents, note=""):
    """Refund money PayPal already charged, newest-first over the main charge and any
    Pay the rest charges. Returns (ok, refund ids, error)."""
    extras = [x for x in json.loads(o["extra_charges"] or "[]") if str(x.get("label", "")).startswith("Rest of tip")]
    extra_total = sum(int(x["cents"]) for x in extras)
    main = int(o["pp_captured_cents"] or 0) - extra_total
    caps = [(o["pay_ref"], main)] + [(x.get("ref"), int(x["cents"])) for x in extras]
    already = int(o["refunded_cents"] or 0)
    room = []
    for cid, amt in caps:            # earlier refunds used up the oldest charges first
        used = min(already, amt)
        already -= used
        if cid and amt - used > 0:
            room.append((cid, amt - used))
    refs, want = [], int(cents)
    for cid, avail in reversed(room):
        if want <= 0:
            break
        take = min(want, avail)
        body = {"amount": pp_money(take)}
        if note:
            body["note_to_payer"] = note[:255]
        st, j = pp_api("POST", "/v2/payments/captures/" + cid + "/refund", body,
                       request_id="ref-" + cid + "-" + str(int(o["refunded_cents"] or 0)) + "-" + str(take))
        if st not in (200, 201) or j.get("status") not in ("COMPLETED", "PENDING"):
            done = int(cents) - want
            return False, refs, pp_err(j, "the refund did not go through.") + \
                (" (" + money(done) + " was already refunded before it stopped)" if done else "")
        refs.append(j.get("id", ""))
        want -= take
    if want > 0:
        return False, refs, "only " + money(int(cents) - want) + " could be refunded through PayPal."
    log("payment", o["code"] + " refunded " + money(cents) + " through " + PP_SOURCES.get(o["pp_source"] or "", "PayPal"))
    return True, refs, ""

@app.post("/api/paypal/settle")
def api_pp_settle():
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    res = pp_void(o, "released by dispatch") if b.get("op") == "void" else pp_settle(o, "charged by dispatch")
    return jsonify(res), (200 if res.get("ok") else 400)

@app.post("/api/track/<code>/tip")
def api_track_tip(code):
    """Customer adds or changes the tip, before or after delivery."""
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if not pp_info(o)["tip_editable"]:
        return jsonify({"ok": False, "error": "Tips can only be changed here on orders paid with Venmo, PayPal or card online."}), 400
    try:
        cents = int(round(float(str(request.get_json(force=True).get("tip", "0")).replace("$", "")) * 100))
    except Exception:
        return jsonify({"ok": False, "error": "Type a tip amount, like 5.00."}), 400
    if cents < 0 or cents > 20000:
        return jsonify({"ok": False, "error": "Tips can be $0.00 to $200.00."}), 400
    if o["pp_state"] == "captured" and cents < int(o["tip_cents"] or 0):
        return jsonify({"ok": False, "error": "Your card was already charged, so the tip can only go up. Call dispatch to lower it."}), 400
    diff = cents - int(o["tip_cents"] or 0)
    db().execute("UPDATE orders SET tip_cents=?, total_cents=total_cents+? WHERE id=?", (cents, diff, o["id"]))
    db().commit()
    log("order", o["code"] + " customer set the tip to " + money(cents))
    if o["driver_id"] and diff:
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (o["driver_id"], "system", "Tip on " + o["code"] + " is now " + money(cents) + ".", now()))
        db().commit()
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    out = {"ok": True, "tip": money(cents), "total": money(o["total_cents"])}
    if o["pp_state"] == "authorized" and o["dispatch_status"] == "delivered":
        out["charge"] = pp_settle(o, "tip added after delivery")
    out["pay"] = pp_info(db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone())
    return jsonify(out)

@app.context_processor
def inject_paypal():
    return {"pp_enabled": pp_enabled()}


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
        if (t >= earliest and is_open(r, t) and business_in_hours(t) and
                (r["slug"] != "oneoff" or dispatcher_required() or item_available(any_rest_row(), t))):
            out.append(t.strftime("%Y-%m-%dT%H:%M"))
        t += dt.timedelta(minutes=15)
    return out

def release_scheduled():
    """A future order stays off every screen until it is close to its time. Then it goes
    out exactly like a fresh order: cash to the kitchen, card to dispatch to run."""
    con = db()
    rows = con.execute("""SELECT * FROM orders WHERE dispatch_status='scheduled'
                          AND release_at IS NOT NULL AND release_at <= ?""", (now(),)).fetchall()
    sent = 0
    for o in rows:
        r = con.execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
        if not business_is_open():
            # the business is closed: future orders wait and go out once dispatch opens
            continue
        if r is not None and not is_open(r) and not o["cloned_from"]:
            # the kitchen is closed: the order waits and goes out the moment they open (redos still go)
            continue
        sent += 1
        con.execute("""UPDATE orders SET kitchen_status=?, dispatch_status=?, hold_reason=?,
                       created_at=? WHERE id=? AND dispatch_status='scheduled'""",
                    (o["sched_kitchen"] or "pending", o["sched_dispatch"] or "held",
                     o["sched_hold"] or "waiting on kitchen", now(), o["id"]))
        con.commit()
        log("order", o["code"] + " future order for " + when_label(o["scheduled_for"]) +
            " released to " + ("dispatch to run the card" if (o["sched_dispatch"] == "awaiting_payment")
                               else "the kitchen"))
    return sent



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
    myr = dispatcher_work_regions(session.get("dispatcher_id"))
    rows = [o for o in rows if covers(myr, o["region_id"])]
    out = []
    for o in rows:
        x = order_dict(o)
        r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
        x["kitchen_closed"] = bool(r is not None and not is_open(r) and not o["cloned_from"])
        x["kitchen_hours"] = hours_label(r) if r is not None else ""
        out.append(x)
    return jsonify({"ok": True, "lead_min": future_lead(), "orders": out})

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
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    if r is not None and not is_open(r) and not o["cloned_from"]:
        return jsonify({"ok": False, "error": r["name"] + " is closed right now (" + hours_label(r) + "), so "
                        "the order was not sent. It goes to the kitchen on its own as soon as they open."}), 400
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
        if not is_disp and not business_in_hours(sched):
            return jsonify({"ok": False, "error": "We are not open at " + when_label(sched.isoformat()) +
                            ". Our hours are " + business_hours_label() + "."}), 400
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
    # items with set days or hours: customers and drivers cannot order them outside those times.
    # A dispatcher can still add one by hand for a call-in.
    if not dispatcher_required():
        when = sched or dt.datetime.now()
        for i in items:
            if not i.get("menu_item_id"):
                continue
            row = db().execute("SELECT * FROM menu_items WHERE id=?", (i["menu_item_id"],)).fetchone()
            if row and not item_available(row, when):
                return jsonify({"ok": False, "error": row["name"] + " is only available " + avail_label(row) +
                                ". Take it out of your bag or pick a time when it is available."}), 400
    card = None
    use_pp = bool(payload.get("paypal")) and pp_enabled()
    if placed_by == "customer" and not use_pp:
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
    unlisted = r["slug"] == "oneoff"
    if unlisted and not dispatcher_required():
        if not any_rest_on():
            return jsonify({"ok": False, "error": "We are not taking orders from other restaurants right now."}), 400
        if not item_available(any_rest_row(), sched or dt.datetime.now()):
            return jsonify({"ok": False, "error": "Orders from restaurants we don't list are taken " +
                            any_rest_label() + ". Pick a time inside those hours."}), 400
    if unlisted and not dispatcher_required() and not (pu_name and pu_addr):
        return jsonify({"ok": False, "error": "Tell us the restaurant name and its address."}), 400
    if unlisted and not dispatcher_required():
        # a customer's own items at a restaurant we do not list: their prices are estimates,
        # no extra item fees, and every line has to say what it is
        for i in items:
            i["fee_cents"] = 0
            i["custom"] = True
            i["menu_item_id"] = None
        if any(not i["name"] or i["name"] == "Custom item" for i in items):
            return jsonify({"ok": False, "error": "Give every item a name."}), 400
        ifee = 0
    if pu_name or pu_addr:
        if not (pu_name and pu_addr):
            return jsonify({"ok": False,
                            "error": "A typed-in pickup needs both a name and an address."}), 400
        if not dispatcher_required() and not unlisted:
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

    if not sched and (not business_is_open() or (not dispatcher_required() and not business_in_hours())):
        # closed business: nothing goes out now, but future orders are still welcome
        msg = ("The business is closed. Open the business first, or schedule this order for later."
               if dispatcher_required() else
               "We are closed right now. You can still schedule your order for later.")
        return jsonify({"ok": False, "error": msg, "closed": True}), 400
    if not sched and not from_code and not is_open(r):
        # a closed kitchen only gets redos of orders it already made
        return jsonify({"ok": False, "error": r["name"] + " is closed right now (" + hours_label(r) + "). "
                        "Only redo orders go to a closed kitchen. Schedule this one for a time they are open."}), 400
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
                        WHERE id=?""", ("card on file, run it" if card else
                                       ("waiting on Venmo/PayPal" if use_pp else "waiting on card"), oid))
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
    return jsonify({"pay_url": ("/pay/" + code) if (use_pp and not cash) else "",
                    "future_note": future_note, "ok": True, "cash": cash, "code": code, "order_id": oid, "total": money(total),
                    "send_note": send_note,
                    "address_ok": bool(address_ok),
                    "message": ("" if address_ok and cash else
                                "Thanks! Waiting on payment. Your order has not gone to the "
                                "kitchen yet. It goes as soon as your payment is marked paid." if address_ok else
                                "We could not verify that address, so dispatch will confirm it shortly. "
                                "Your order has not gone to the kitchen yet. It goes once dispatch "
                                "confirms your address. Your delivery fee may change with the distance." if cash else
                                "We could not verify that address, so dispatch will confirm it shortly. "
                                "Your order has not gone to the kitchen yet. It goes once your address is "
                                "confirmed and your payment is marked paid. Your delivery fee may change "
                                "with the distance.")})

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
    unpaid_card = o["dispatch_status"] == "awaiting_payment"
    if unpaid_card:
        # card not run yet: the address is fine now, but the kitchen still waits on payment
        kitchen, hold = "waiting", (o["hold_reason"] or "waiting on card")
        if hold == "address needs dispatch approval":
            hold = "waiting on card"
    else:
        kitchen = "pending" if o["kitchen_status"] == "waiting" else o["kitchen_status"]
        hold = "waiting on kitchen" if o["hold_reason"] == "address needs dispatch approval" else o["hold_reason"]
    db().execute("""UPDATE orders SET address=?, lat=?, lng=?, miles=?, fee_cents=?, total_cents=?,
                    address_ok=1, kitchen_status=?, hold_reason=? WHERE id=?""",
                 (addr_out, lat, lng, miles, fee, total, kitchen, hold, o["id"]))
    db().commit()
    log("order", o["code"] + " address approved by dispatch (" + addr_out + ")")
    auto_assign()
    return jsonify({"ok": True, "address": addr_out, "miles": miles,
                    "fee": money(fee), "total": money(total), "verified": bool(g1["ok"]),
                    "sent_to_kitchen": kitchen == "pending" and o["kitchen_status"] == "waiting",
                    "waiting_on_payment": unpaid_card})

@app.route("/track/<code>")
def track(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return render_template("track.html", order=None, code=code)
    return render_template("track.html", order={"code": o["code"]}, code=code)

TRACK_FIELDS = ("code", "dispatch_status", "kitchen_status", "restaurant", "restaurant_nav", "address",
                "scheduled_label", "needs_address_approval", "timeline", "timer_seconds",
                "queue_position", "hold_reason", "miles", "subtotal", "fee", "service", "service_cents",
                "tax", "tip", "total", "delivered_time", "lines")
TRACK_AVG_MPH = 25.0       # town driving speed used for the customer's rough arrival time
TRACK_FIX_FRESH_MIN = 10   # an older GPS fix is not shown to the customer


def track_payload(o):
    """What the public tracking page may see. Card, signature, phone and dispatch-only
    fields stay off it, since anyone with the order code can open this page."""
    full = order_dict(o)
    out = {k: full.get(k) for k in TRACK_FIELDS}
    st = o["dispatch_status"]
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    pu = pickup_of(o, r)
    t = {"phase": "", "driver_name": "", "stops_before": 0, "eta_min": None,
         "driver_lat": None, "driver_lng": None, "driver_fix": "",
         "home_lat": o["lat"], "home_lng": o["lng"], "pickup_lat": pu["lat"], "pickup_lng": pu["lng"],
         "dispatch_tel": tel_digits(dispatch_phone())}
    d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() if o["driver_id"] else None
    if d and st in ("assigned", "received", "at_restaurant", "enroute", "delivered"):
        t["driver_name"] = (d["name"] or "").split()[0] if (d["name"] or "").strip() else "Your driver"
        t["phase"] = {"assigned": "Heading to " + pu["name"], "received": "Heading to " + pu["name"],
                      "at_restaurant": "At " + pu["name"] + " picking up your order",
                      "enroute": "On the way to you", "delivered": "Delivered"}[st]
        if st == "enroute":
            ahead = db().execute("""SELECT COUNT(*) n FROM orders WHERE driver_id=? AND id<>?
                                    AND dispatch_status='enroute' AND stack_seq < ?""",
                                 (d["id"], o["id"], o["stack_seq"] or 0)).fetchone()["n"]
            t["stops_before"] = ahead
            fresh = False
            if d["last_loc_at"] and d["last_lat"] is not None and d["last_lng"] is not None:
                try:
                    age = (dt.datetime.now() - dt.datetime.fromisoformat(d["last_loc_at"])).total_seconds() / 60
                    fresh = age <= TRACK_FIX_FRESH_MIN
                except ValueError:
                    fresh = False
            if fresh:
                t["driver_lat"], t["driver_lng"] = d["last_lat"], d["last_lng"]
                t["driver_fix"] = clock(d["last_loc_at"])
                if o["lat"] is not None and o["lng"] is not None:
                    mi = haversine_miles(d["last_lat"], d["last_lng"], o["lat"], o["lng"]) * ROAD_FACTOR
                    t["eta_min"] = max(2, int(round(mi / TRACK_AVG_MPH * 60 + ahead * 5)))
    out["track"] = t
    return out


@app.get("/api/track/<code>")
def api_track(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    pp_sweep()
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    return jsonify({"ok": True, "order": track_payload(o), "pay": pp_info(o)})

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
    pp_sweep()      # charge delivered PayPal/Venmo orders once the tip window is over
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
    lines = line_positions()
    rotation = {k: v["pos"] for k, v in lines.items()}
    unread = {r["driver_id"]: r["c"] for r in db().execute(
        """SELECT driver_id, COUNT(*) c FROM messages
           WHERE sender='driver' AND seen_by_dispatch=0 GROUP BY driver_id""").fetchall()}
    newest = db().execute(
        """SELECT m.id, m.driver_id, m.body, m.created_at, d.name FROM messages m
           JOIN drivers d ON d.id=m.driver_id
           WHERE m.sender='driver' AND m.seen_by_dispatch=0
           ORDER BY m.id DESC LIMIT 1""").fetchone()
    rests = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    my_regions = dispatcher_work_regions(session.get("dispatcher_id"))
    show_all = request.args.get("all") == "1"
    myr = set() if show_all else my_regions
    if myr:
        live = [o for o in live if covers(myr, o["region_id"])]
        done = [o for o in done if covers(myr, o["region_id"])]
        future_count = len([1 for o in db().execute(
            "SELECT region_id FROM orders WHERE dispatch_status='scheduled'").fetchall() if covers(myr, o["region_id"])])
        # a driver asking for dispatch or with unread messages shows for every dispatcher
        drivers = [d for d in drivers if not driver_work_regions(d["id"]) or (driver_work_regions(d["id"]) & myr)
                   or d["pending_request"] or unread.get(d["id"])]
        rests = [r for r in rests if covers(myr, r["region_id"])]
    return jsonify({
        "ok": True,
        "regions_label": region_names(my_regions),
        "region_filtered": bool(my_regions),
        "showing_all": show_all,
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
                     "at_limit": (lines.get(d["id"]) or {}).get("at_limit", False),
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
    pp_st = (o["pp_state"] or "") if "pp_state" in o.keys() else ""
    if pp_st == "authorized":
        # not charged yet: a full refund releases the hold, a partial one lowers what gets charged
        if cents >= left:
            r0 = pp_void(o, "refunded by dispatch")
            if not r0.get("ok"):
                return jsonify({"ok": False, "error": "PayPal: " + r0.get("error", "could not release the hold.")}), 400
            rid = "paypal hold released"
        else:
            rid = "paypal hold lowered"
    elif pp_st == "captured":
        ok, refs, err = pp_refund(o, cents, note)
        if not ok:
            return jsonify({"ok": False, "error": "PayPal: " + err}), 400
        rid = ("paypal " + ",".join(refs))[:60]
    elif not is_cash(o):
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


@app.get("/api/dispatch/order-edit/<int:oid>")
def api_dispatch_order_edit_load(oid):
    """Everything the order editor needs: the order's lines and that restaurant's full menu."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    r = db().execute("SELECT name FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    return jsonify({"ok": True, "id": o["id"], "code": o["code"],
                    "restaurant": r["name"] if r else "",
                    "items": json.loads(o["items"] or "[]"),
                    "fee_cents": o["fee_cents"], "tip_cents": o["tip_cents"],
                    "ref": o["ref_code"] or "",
                    "menu": menu_payload(o["restaurant_id"]) if o["restaurant_id"] else []})


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
        menus[r["id"]] = menu_payload(r["id"])
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
        picks = []
        for p in (i.get("options") or [])[:40]:
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                continue
            picks.append({"group": str(p.get("group") or "")[:80], "name": str(p.get("name"))[:120],
                          "delta_cents": max(0, int(round(float(p.get("delta_cents") or 0))))})
        if picks:
            line["options"] = picks
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
                    "i_am_owner": is_owner(),
                    "users": [{"id": r["id"], "name": r["name"], "username": r["username"],
                               "owner": bool(r["is_owner"]),
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
    if "owner" in data:
        want = bool(data.get("owner"))
        cur_owner = is_owner(uid)
        if want != cur_owner:
            if not is_owner():
                return jsonify({"ok": False, "error": "Only an owner can make someone an owner."}), 403
            if not want and db().execute("SELECT COUNT(*) c FROM dispatchers WHERE is_owner=1").fetchone()["c"] <= 1:
                return jsonify({"ok": False, "error": "Keep at least one owner account."}), 400
            db().execute("UPDATE dispatchers SET is_owner=? WHERE id=?", (1 if want else 0, uid))
            log("dispatcher", (session.get("dispatcher_name") or "dispatch") + (" made " if want else " removed owner from ") + username)
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
    if is_owner(uid):
        if not is_owner():
            return jsonify({"ok": False, "error": "Only an owner can remove an owner account."}), 403
        if db().execute("SELECT COUNT(*) c FROM dispatchers WHERE is_owner=1").fetchone()["c"] <= 1:
            return jsonify({"ok": False, "error": "Keep at least one owner account."}), 400
    db().execute("DELETE FROM dispatchers WHERE id=?", (uid,))
    log("dispatcher", "removed id " + str(uid))
    db().commit()
    return jsonify({"ok": True})

@app.get("/api/dispatch/staff-chat")
def api_staff_chat():
    """The dispatcher room: every dispatcher on duty sees this thread."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    stamp_regions()
    me = session.get("dispatcher_id")
    owner = is_owner(me)
    myr = dispatcher_region_ids(me)
    names = {r["id"]: r["name"] for r in all_regions()}
    room = request.args.get("room", "all")
    rows = db().execute("""SELECT m.*, COALESCE(d.is_owner,0) AS from_owner FROM messages m
                           LEFT JOIN dispatchers d ON d.id=m.dispatcher_id
                           WHERE m.driver_id=0 ORDER BY m.id DESC LIMIT 400""").fetchall()
    out = []
    for r in rows:
        rg = r["region_id"] or 0
        if not owner and myr and rg and rg not in myr:
            continue     # another region's room
        if room not in ("all", "") and str(rg) != room and not (rg == 0 and room.isdigit()):
            continue
        out.append({"sender": r["sender_name"] or r["sender"], "body": r["body"],
                    "mine": r["dispatcher_id"] == me, "owner": bool(r["from_owner"]),
                    "region": names.get(rg, "All regions") if rg else "All regions",
                    "at": r["created_at"][11:16]})
        if len(out) >= 80:
            break
    if owner or not myr:
        rooms = [{"id": 0, "name": "All regions"}] + [{"id": k, "name": v} for k, v in names.items()]
    else:
        rooms = [{"id": k, "name": v} for k, v in names.items() if k in myr]
    return jsonify({"ok": True, "me": session.get("dispatcher_name"), "owner": owner,
                    "rooms": rooms, "messages": list(reversed(out))})

@app.post("/api/dispatch/staff-chat")
def api_staff_chat_send():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    body = (b.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False}), 400
    me = session.get("dispatcher_id")
    myr = dispatcher_region_ids(me)
    try:
        rg = int(b.get("room") or 0)
    except (TypeError, ValueError):
        rg = 0
    valid = {r["id"] for r in all_regions()}
    if rg and rg not in valid:
        return jsonify({"ok": False, "error": "That region is gone."}), 400
    if not is_owner(me) and myr:
        if rg not in myr:
            if len(myr) == 1:
                rg = next(iter(myr))
            else:
                return jsonify({"ok": False, "error": "Pick one of your regions to send to."}), 400
    db().execute("""INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at,region_id)
                    VALUES(0,'dispatch',?,?,?,?,?)""",
                 (session.get("dispatcher_name"), me, body, now(), rg))
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

ADDR_LOOKUP_AT = {}   # driver id -> last street-address lookup (the pin itself moves every fix)
ADDR_LOOKUP_GAP = 15  # seconds between address lookups per driver

def update_driver_addr(did, lat, lng, force=False):
    """Refresh the driver's closest address when they have moved about 30 metres,
    at most every 15 seconds. The map pin updates on every fix regardless."""
    d = db().execute("SELECT last_addr,last_addr_lat,last_addr_lng FROM drivers WHERE id=?", (did,)).fetchone()
    if d and d["last_addr"] and not force and d["last_addr_lat"] is not None:
        moved = miles_between(d["last_addr_lat"], d["last_addr_lng"], lat, lng) or 0
        if moved < 0.025 or time.time() - ADDR_LOOKUP_AT.get(did, 0) < ADDR_LOOKUP_GAP:
            return d["last_addr"]
    ADDR_LOOKUP_AT[did] = time.time()
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
    """The driver app posts a GPS fix every 2-3 seconds while the driver is on shift."""
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
    # the hand move lasts for today only; tomorrow the approved schedule decides again
    db().execute("UPDATE drivers SET roster=?, roster_day=? WHERE id=?",
                 (roster, dt.date.today().isoformat(), did))
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
            "regions": sorted(slot_regions(mine[0])) if mine else [],
            "region_label": region_names(slot_regions(mine[0])) if mine and slot_regions(mine[0]) else "",
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
             "regions": sorted(slot_regions(r)),
             "region_label": (region_names(slot_regions(r)) if slot_regions(r) else ""),
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


def in_shift_window(driver_id, at=None):
    """True from sched_lead_min before an approved shift starts until it ends."""
    at = at or dt.datetime.now()
    lead = setting("sched_lead_min")
    lead = 60 if lead is None else lead
    for span in scheduled_today(driver_id, at.date()):
        try:
            a_, b_ = span.split("-")
            start = dt.datetime.combine(at.date(), dt.time.fromisoformat(a_.strip()))
            end = dt.datetime.combine(at.date(), dt.time.fromisoformat(b_.strip()))
        except ValueError:
            continue
        if end <= start:
            end += dt.timedelta(days=1)
        if start - dt.timedelta(minutes=lead) <= at <= end:
            return True
    return False


def driver_group(d):
    """Which roster tab a driver sits in. Scheduled starts sched_lead_min (setting,
    default 60) before an approved shift and lasts until it ends. A driver still
    working past the end stays Scheduled. A hand move by dispatch wins for that day.
    Approved time off today means Unavailable."""
    today = dt.date.today().isoformat()
    try:
        hand_day = d["roster_day"]
    except (IndexError, KeyError):
        hand_day = None
    if hand_day == today and d["roster"] in ROSTERS:
        return d["roster"]
    if d["status"] != "offline" and scheduled_today(d["id"]):
        return "scheduled"
    return "scheduled" if in_shift_window(d["id"]) else "unavailable"

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
                        week_start,region_ids) VALUES(?,?,?,?,?,'pending',?,?,?)""",
                     (did, dow, start, end, data.get("note", ""), now(), monday_of(_d).isoformat(),
                      clean_region_ids(data.get("regions"))))
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
        clean.append((int(d.get("dow", 0)), start, end, (d.get("note") or "").strip(),
                      clean_region_ids(d.get("regions"))))
    stamp = now()
    key = ws.isoformat()
    db().execute("DELETE FROM availability WHERE driver_id=? AND week_start=?", (did, key))
    for dow, start, end, note, rids in clean:
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                        created_at,week_start,region_ids) VALUES(?,?,?,?,?,'pending',?,?,?)""",
                     (did, dow, start, end, note, stamp, key, rids))
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
                    "roster": driver_group(d), "status": d["status"],
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
            db().execute("UPDATE drivers SET roster='unavailable', roster_day=? WHERE id=?",
                         (dt.date.today().isoformat(), row["driver_id"])) \
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
                            decided_by,decided_at,created_at,week_start,region_ids)
                            VALUES(?,?,?,?,?,'approved',?,?,?,?,?)""",
                         (did, dow, start, end, b.get("note", ""), who, now(), now(), ws,
                          clean_region_ids(b.get("regions"))))
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
                        decided_by,decided_at,created_at,region_ids) VALUES(?,?,?,?,?,'approved',?,?,?,?)""",
                     (b["driver_id"], dow, start, end, b.get("note", ""), who, now(), now(),
                      clean_region_ids(b.get("regions"))))
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


# ---------------------------------------------------------------- dispatcher availability

def _disp_slot_on(s, when):
    """Does this weekly slot cover this moment? An end before the start runs past midnight."""
    st, en = _hm(s["start_time"]), _hm(s["end_time"])
    if st is None or en is None:
        return False
    m = when.hour * 60 + when.minute
    if en > st:
        return s["dow"] == when.weekday() and st <= m < en
    return (s["dow"] == when.weekday() and m >= st) or (s["dow"] == (when.weekday() - 1) % 7 and m < en)


def dispatcher_avail_payload():
    con = db()
    me = session.get("dispatcher_id")
    nowdt = dt.datetime.now()
    out = []
    for d in con.execute("SELECT id, name, phone FROM dispatchers ORDER BY name").fetchall():
        rows = con.execute("""SELECT * FROM dispatcher_availability WHERE dispatcher_id=?
                              ORDER BY dow, start_time""", (d["id"],)).fetchall()
        slots = [{"id": s["id"], "dow": s["dow"], "day": DOW_NAMES[s["dow"]],
                  "start": s["start_time"], "end": s["end_time"],
                  "label": _ampm(s["start_time"]) + " - " + _ampm(s["end_time"]) +
                           (" (overnight)" if (_hm(s["end_time"]) or 0) <= (_hm(s["start_time"]) or 0) else ""),
                  "note": s["note"] or "", "on_now": _disp_slot_on(s, nowdt),
                  "regions": sorted(slot_regions(s)),
                  "region_label": (region_names(slot_regions(s)) if slot_regions(s) else "")} for s in rows]
        mins = 0
        for s in rows:
            a, b = _hm(s["start_time"]), _hm(s["end_time"])
            if a is not None and b is not None:
                mins += (b - a) if b > a else (1440 - a + b)
        ph = d["phone"] or ""
        out.append({"id": d["id"], "name": d["name"], "me": d["id"] == me, "slots": slots, "phone": ph,
                    "phone_label": ("(%s) %s-%s" % (ph[:3], ph[3:6], ph[6:])) if len(ph) == 10 else ph,
                    "on_now": any(x["on_now"] for x in slots), "hours": round(mins / 60, 1)})
    return {"ok": True, "me": me, "dispatchers": out}


@app.get("/api/dispatch/dispatcher-avail")
def api_dispatcher_avail():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    return jsonify(dispatcher_avail_payload())


@app.post("/api/dispatch/dispatcher-avail")
def api_dispatcher_avail_edit():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    con = db()
    who = session.get("dispatcher_name") or "dispatch"
    if b.get("op") == "phone":
        d = con.execute("SELECT id, name FROM dispatchers WHERE id=?", (b.get("dispatcher_id"),)).fetchone()
        if not d:
            return jsonify({"ok": False, "error": "Pick a dispatcher."}), 400
        ph = "".join(c for c in str(b.get("phone") or "") if c.isdigit())
        if len(ph) == 11 and ph.startswith("1"):
            ph = ph[1:]
        if ph and len(ph) != 10:
            return jsonify({"ok": False, "error": "The phone number needs 10 digits, like 334-209-2844."}), 400
        con.execute("UPDATE dispatchers SET phone=? WHERE id=?", (ph, d["id"]))
        con.commit()
        log("availability", who + (" set " if ph else " cleared ") + d["name"] + "'s phone number")
        return jsonify(dispatcher_avail_payload())
    if b.get("op") == "delete":
        s = con.execute("SELECT * FROM dispatcher_availability WHERE id=?", (b.get("id"),)).fetchone()
        if not s:
            return jsonify({"ok": False, "error": "That time is already gone."}), 404
        con.execute("DELETE FROM dispatcher_availability WHERE id=?", (s["id"],))
        nm = con.execute("SELECT name FROM dispatchers WHERE id=?", (s["dispatcher_id"],)).fetchone()
        log("availability", who + " removed " + (nm["name"] if nm else "a dispatcher") + "'s " +
            DOW_NAMES[s["dow"]] + " " + s["start_time"] + "-" + s["end_time"])
        con.commit()
        return jsonify(dispatcher_avail_payload())
    did = b.get("dispatcher_id") or session.get("dispatcher_id")
    d = con.execute("SELECT id, name FROM dispatchers WHERE id=?", (did,)).fetchone()
    if not d:
        return jsonify({"ok": False, "error": "Pick a dispatcher."}), 400
    days = sorted({int(x) for x in (b.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
    if not days:
        return jsonify({"ok": False, "error": "Check at least one day."}), 400
    st = str(b.get("start") or "").strip()[:5]
    en = str(b.get("end") or "").strip()[:5]
    if _hm(st) is None or _hm(en) is None:
        return jsonify({"ok": False, "error": "Set a start and an end time."}), 400
    if _hm(st) == _hm(en):
        return jsonify({"ok": False, "error": "The start and end time cannot be the same."}), 400
    note = " ".join(str(b.get("note") or "").split())[:80]
    added = 0
    for dow in days:
        if con.execute("""SELECT 1 FROM dispatcher_availability WHERE dispatcher_id=? AND dow=?
                          AND start_time=? AND end_time=?""", (d["id"], dow, st, en)).fetchone():
            continue
        con.execute("""INSERT INTO dispatcher_availability(dispatcher_id,dow,start_time,end_time,note,
                       created_by,created_at,region_ids) VALUES(?,?,?,?,?,?,?,?)""",
                    (d["id"], dow, st, en, note, who, now(), clean_region_ids(b.get("regions"))))
        added += 1
    con.commit()
    log("availability", who + " set " + d["name"] + " available " + ", ".join(DOW_NAMES[x] for x in days) +
        " " + st + "-" + en)
    out = dispatcher_avail_payload()
    out["added"] = added
    return jsonify(out)


# ---------------------------------------------------------------- regions page

def regions_payload():
    con = db()
    regs = all_regions()
    return {"ok": True, "me": session.get("dispatcher_id"),
            "regions": [{"id": r["id"], "name": r["name"],
                         "restaurants": con.execute("SELECT COUNT(*) c FROM restaurants WHERE region_id=? AND slug!='oneoff'",
                                                    (r["id"],)).fetchone()["c"]} for r in regs],
            "restaurants": [{"id": r["id"], "name": r["name"], "address": r["address"] or "",
                             "region_id": r["region_id"] or 0}
                            for r in con.execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()],
            "drivers": [{"id": d["id"], "name": d["name"], "regions": sorted(driver_region_ids(d["id"]))}
                        for d in con.execute("SELECT id, name FROM drivers ORDER BY name").fetchall()],
            "dispatchers": [{"id": d["id"], "name": d["name"], "regions": sorted(dispatcher_region_ids(d["id"]))}
                            for d in con.execute("SELECT id, name FROM dispatchers ORDER BY name").fetchall()]}


@app.get("/api/dispatch/find-order")
def api_find_order():
    """Look up any order in any region by code, phone or customer name."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"ok": True, "results": []})
    digits = "".join(c for c in q if c.isdigit())
    like = "%" + q.lower() + "%"
    rows = db().execute("""SELECT * FROM orders WHERE lower(code) LIKE ? OR lower(COALESCE(customer_name,'')) LIKE ?
                           OR (? != '' AND length(?) >= 4 AND replace(replace(replace(replace(COALESCE(customer_phone,''),'-',''),' ',''),'(',''),')','') LIKE ?)
                           ORDER BY created_at DESC LIMIT 25""",
                        (like, like, digits, digits, "%" + digits + "%")).fetchall()
    names = {r["id"]: r["name"] for r in all_regions()}
    myr = dispatcher_work_regions(session.get("dispatcher_id"))
    out = []
    for o in rows:
        x = order_dict(o)
        d = db().execute("SELECT name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() if o["driver_id"] else None
        out.append({"id": o["id"], "code": o["code"], "customer": x.get("customer") or o["customer_name"],
                    "restaurant": x.get("restaurant") or "", "status": o["dispatch_status"],
                    "kitchen": o["kitchen_status"], "hold_reason": o["hold_reason"] or "",
                    "driver": d["name"] if d else "", "created": (o["created_at"] or "")[:16].replace("T", " "),
                    "region": names.get(o["region_id"] or 0, "No region"),
                    "mine": covers(myr, o["region_id"]),
                    "track": "/track/" + o["code"]})
    return jsonify({"ok": True, "results": out})


@app.get("/api/regions-list")
def api_regions_list():
    if not (session.get("dispatcher_id") or session.get("driver_id")):
        return jsonify({"ok": False}), 403
    stamp_regions()
    mine = (driver_region_ids(session["driver_id"]) if session.get("driver_id") and not session.get("dispatcher_id")
            else dispatcher_region_ids(session.get("dispatcher_id")))
    return jsonify({"ok": True, "regions": [{"id": r["id"], "name": r["name"]} for r in all_regions()],
                    "mine": sorted(mine)})


@app.get("/dispatch/regions")
def dispatch_regions_page():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    stamp_regions()
    return render_template("dispatch_regions.html")


@app.get("/api/dispatch/regions")
def api_regions():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    stamp_regions()
    return jsonify(regions_payload())


@app.post("/api/dispatch/regions")
def api_regions_edit():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    con = db()
    op = b.get("op")
    who = session.get("dispatcher_name") or "dispatch"
    valid = {r["id"] for r in all_regions()}

    def ids(lst):
        out = set()
        for x in lst or []:
            try:
                x = int(x)
            except (TypeError, ValueError):
                continue
            if x in valid:
                out.add(x)
        return out

    if op in ("add_region", "rename_region"):
        name = " ".join(str(b.get("name") or "").split())[:40]
        if not name:
            return jsonify({"ok": False, "error": "Give the region a name."}), 400
        clash = con.execute("SELECT id FROM regions WHERE lower(name)=lower(?) AND id IS NOT ?",
                            (name, b.get("id") if op == "rename_region" else None)).fetchone()
        if clash:
            return jsonify({"ok": False, "error": "There is already a region called " + name + "."}), 400
        if op == "add_region":
            srt = (con.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM regions").fetchone()["s"])
            cur = con.execute("INSERT INTO regions(name,sort,created_at) VALUES(?,?,?)", (name, srt, now()))
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('regions_seeded','1')")
            log("region", who + " added region " + name)
            if b.get("match_towns"):
                for r in con.execute("""SELECT id, address FROM restaurants WHERE COALESCE(region_id,0)=0
                                        AND slug!='oneoff'""").fetchall():
                    if region_match(r["address"]) == cur.lastrowid:
                        con.execute("UPDATE restaurants SET region_id=? WHERE id=?", (cur.lastrowid, r["id"]))
        else:
            if int(b.get("id") or 0) not in valid:
                return jsonify({"ok": False, "error": "That region is gone."}), 404
            con.execute("UPDATE regions SET name=? WHERE id=?", (name, b["id"]))
            log("region", who + " renamed a region to " + name)
    elif op == "delete_region":
        rid = int(b.get("id") or 0)
        if rid not in valid:
            return jsonify({"ok": False, "error": "That region is gone."}), 404
        nm = con.execute("SELECT name FROM regions WHERE id=?", (rid,)).fetchone()["name"]
        con.execute("UPDATE restaurants SET region_id=0 WHERE region_id=?", (rid,))
        con.execute("UPDATE orders SET region_id=0 WHERE region_id=?", (rid,))
        con.execute("DELETE FROM driver_regions WHERE region_id=?", (rid,))
        con.execute("DELETE FROM dispatcher_regions WHERE region_id=?", (rid,))
        con.execute("DELETE FROM regions WHERE id=?", (rid,))
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('regions_seeded','1')")
        log("region", who + " deleted region " + nm)
    elif op == "set_restaurant":
        rid = int(b.get("region_id") or 0)
        if rid and rid not in valid:
            return jsonify({"ok": False, "error": "Pick a region."}), 400
        r = con.execute("SELECT id, name FROM restaurants WHERE id=?", (b.get("restaurant_id"),)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "Restaurant not found."}), 404
        con.execute("UPDATE restaurants SET region_id=? WHERE id=?", (rid, r["id"]))
        # orders still on the board follow the restaurant to its new region
        con.execute("""UPDATE orders SET region_id=? WHERE restaurant_id=? AND COALESCE(pickup_address,'')=''
                       AND dispatch_status NOT IN ('delivered','cancelled')""", (rid, r["id"]))
        log("region", who + " put " + r["name"] + " in " + region_names({rid} if rid else set()).replace("All regions", "no region"))
    elif op in ("set_driver", "set_dispatcher"):
        table, col = ("driver_regions", "driver_id") if op == "set_driver" else ("dispatcher_regions", "dispatcher_id")
        src = "drivers" if op == "set_driver" else "dispatchers"
        p = con.execute("SELECT id, name FROM " + src + " WHERE id=?", (b.get("id"),)).fetchone()
        if not p:
            return jsonify({"ok": False, "error": "Not found."}), 404
        chosen = ids(b.get("regions"))
        con.execute("DELETE FROM " + table + " WHERE " + col + "=?", (p["id"],))
        for x in chosen:
            con.execute("INSERT INTO " + table + "(" + col + ",region_id) VALUES(?,?)", (p["id"], x))
        log("region", who + " set " + p["name"] + " to " + region_names(chosen))
    else:
        return jsonify({"ok": False, "error": "Unknown change."}), 400
    con.commit()
    try:
        auto_assign()
    except Exception as e:
        print("auto assign after region change skipped:", e)
    return jsonify(regions_payload())


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
    """Dispatch sets the driver's stop order: order_ids in the new order. Only that
    driver's live stops are renumbered 1..n, and the driver gets a note with the new order."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True) or {}
    try:
        did = int(data.get("driver_id"))
        want = [int(x) for x in (data.get("order_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad request."}), 400
    live = db().execute("""SELECT id, code FROM orders WHERE driver_id=? AND dispatch_status
                           IN ('assigned','received','at_restaurant','enroute')
                           ORDER BY stack_seq, id""", (did,)).fetchall()
    have = [r["id"] for r in live]
    if sorted(want) != sorted(have):
        return jsonify({"ok": False, "error": "That run changed. The board will refresh, then try again."}), 409
    if want == have:
        return jsonify({"ok": True, "changed": False})
    codes = {r["id"]: r["code"] for r in live}
    for seq, oid in enumerate(want, start=1):
        db().execute("UPDATE orders SET stack_seq=? WHERE id=? AND driver_id=?", (seq, oid, did))
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (did, "dispatcher", "Dispatch changed your stop order: " +
                  ", ".join("#%d %s" % (i, codes[o]) for i, o in enumerate(want, start=1)), now()))
    db().commit()
    return jsonify({"ok": True, "changed": True})


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


@app.post("/api/dispatch/manual-order")
def api_manual_order():
    """For restaurants not on the restaurant app: dispatch places the order with the
    restaurant (on their website or by phone). Ordering in process, then Order placed,
    which starts the prep timer."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if o["dispatch_status"] in ("delivered", "cancelled"):
        return jsonify({"ok": False, "error": "That order is already finished."}), 400
    if o["dispatch_status"] == "awaiting_payment" or o["kitchen_status"] == "waiting":
        return jsonify({"ok": False, "error": "Mark the card paid first, then place the order."}), 400
    who = session.get("dispatcher_name") or "Dispatch"
    try:
        drow = db().execute("SELECT name FROM dispatchers WHERE id=?", (session.get("dispatcher_id"),)).fetchone()
        if drow:
            who = drow["name"]
    except Exception:
        pass
    op = b.get("op")
    if op == "ordering":
        db().execute("UPDATE orders SET manual_state='ordering', manual_at=?, manual_by=? WHERE id=?",
                     (now(), who, o["id"]))
    elif op == "placed":
        try:
            mins = int(b.get("prep_minutes") or 15)
        except Exception:
            mins = 15
        if mins < 1 or mins > 180:
            return jsonify({"ok": False, "error": "Set the timer between 1 and 180 minutes."}), 400
        db().execute("""UPDATE orders SET manual_state='placed', manual_at=?, manual_by=?,
                        kitchen_status='preparing', prep_minutes=?, prep_started=? WHERE id=?""",
                     (now(), who, mins, now(), o["id"]))
    elif op == "reset":
        db().execute("""UPDATE orders SET manual_state=NULL, manual_at=NULL, manual_by=NULL
                        WHERE id=? AND kitchen_status='pending'""", (o["id"],))
    else:
        return jsonify({"ok": False, "error": "Unknown step."}), 400
    db().commit()
    if o["driver_id"]:
        words = {"ordering": "is ordering", "placed": "placed", "reset": "reset ordering on"}[op]
        try:
            log_driver(o["driver_id"], who + " " + words + " " + o["code"])
        except Exception:
            pass
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
        db().execute("UPDATE restaurants SET uses_app=? WHERE id=?",
                     (1 if request.form.get("uses_app") else 0, rid))
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
        if "sched_lead_min" in request.form:
            try:
                _v = int(request.form["sched_lead_min"] or 0)
            except ValueError:
                _v = -1
            if 0 <= _v <= 240:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('sched_lead_min',?)", (str(_v),))
            else:
                FUTURE_LEAD_ERR.append(3)
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
        if "any_present" in request.form:
            st = (request.form.get("any_start") or "").strip()[:5]
            en = (request.form.get("any_end") or "").strip()[:5]
            days = ",".join(str(k) for k in range(7) if request.form.get("any_day_%d" % k))
            if bool(st) != bool(en) or (st and (_hm(st) is None or _hm(en) is None or _hm(st) == _hm(en))):
                errs.append("Restaurants not listed: set both a start and an end time (not the same), or leave both blank.")
            else:
                for k, v in (("any_on", "1" if request.form.get("any_on") else "0"), ("any_days", days),
                             ("any_start", st), ("any_end", en)):
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        if "bh_present" in request.form:
            if not request.form.get("bh_on"):
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_hours','{}')")
            else:
                hrs, bad = {}, []
                for k, name in enumerate(BH_DAYS):
                    if request.form.get("bh_closed_%d" % k):
                        hrs[str(k)] = ["", ""]
                        continue
                    o = (request.form.get("bh_open_%d" % k) or "").strip()[:5]
                    c = (request.form.get("bh_close_%d" % k) or "").strip()[:5]
                    if _hm(o) is None or _hm(c) is None or _hm(o) == _hm(c):
                        bad.append(name)
                    else:
                        hrs[str(k)] = [o, c]
                if bad:
                    errs.append("Business hours: give " + ", ".join(bad) +
                                " an open and a close time, or check Closed.")
                else:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_hours',?)",
                                 (json.dumps(hrs),))
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
                                   saved=False, errors=errs, bh=business_hours_rows(),
                                   bh_on=bool(business_hours()), any_on=any_rest_on(), any_row=any_rest_row(), bh_days=BH_DAYS)
        if "order_tokens" in request.form:
            tags = ",".join(t.strip()[:24] for t in request.form["order_tokens"].split(",") if t.strip())
            db().execute("UPDATE settings SET value=? WHERE key='order_tokens'", (tags,))
        db().commit()
        saved = True
    rows = db().execute("SELECT * FROM settings").fetchall()
    return render_template("dispatch_settings.html", s={r["key"]: r["value"] for r in rows}, saved=saved,
                           bh=business_hours_rows(), bh_on=bool(business_hours()), any_on=any_rest_on(), any_row=any_rest_row(), bh_days=BH_DAYS)

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
    lines = line_positions()
    rotation = {k: v["pos"] for k, v in lines.items()}
    dr = driver_work_regions(did)
    waiting = len([1 for o in db().execute("""SELECT region_id FROM orders
                              WHERE dispatch_status IN ('queued','held')""").fetchall() if covers(dr, o["region_id"])])
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
                               "at_limit": (lines.get(d["id"]) or {}).get("at_limit", False),
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
    dispatch_ordering = not r["uses_app"]
    if dispatch_ordering:
        rows = []
    rc_unread = db().execute("""SELECT COUNT(*) c FROM rest_messages WHERE restaurant_id=?
                                AND sender='dispatch' AND seen_by_rest=0""", (rid,)).fetchone()["c"]
    rc_last = db().execute("SELECT MAX(id) m FROM rest_messages WHERE restaurant_id=?", (rid,)).fetchone()["m"] or 0
    return jsonify({"late": [x for x in late_accepts() if x["kind"] == "kitchen" and x["restaurant_id"] == rid],
                    "chat_unread": rc_unread, "chat_last_id": rc_last, "ok": True, "orders": [order_dict(o) for o in rows],
                    "open": is_open(r), "open_24": bool(r["open_24"]),
                    "hours": hours_label(r), "prep_default": r["prep_default"],
                    "dispatch_ordering": dispatch_ordering})

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
    av = clean_avail(b)
    if av and av.get("err"):
        return jsonify({"ok": False, "error": av["err"]}), 400
    if op == "section_avail":
        if av is None:
            return jsonify({"ok": False, "error": "Nothing to set."}), 400
        n = db().execute("""UPDATE menu_items SET avail_days=?, avail_start=?, avail_end=?
                            WHERE restaurant_id=? AND TRIM(COALESCE(section,''))=?
                              AND TRIM(COALESCE(menu_tab,''))=?""",
                         (av["days"], av["st"], av["en"], b.get("restaurant_id"),
                          (b.get("section") or "").strip(), (b.get("tab") or "").strip())).rowcount
        db().commit()
        return jsonify({"ok": True, "updated": n})
    if op == "create":
        sec = (b.get("section") or "").strip()
        nxt = db().execute("SELECT COALESCE(MAX(sort),0)+1 n FROM menu_items WHERE restaurant_id=?",
                           (b["restaurant_id"],)).fetchone()["n"]
        if sec:
            same = db().execute("SELECT MAX(sort) m FROM menu_items WHERE restaurant_id=? AND section=?",
                                (b["restaurant_id"], sec)).fetchone()["m"]
            if same is not None:
                nxt = same
        cur = db().execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents,section,sort,active,menu_tab)
                              VALUES(?,?,?,?,?,?,?,?)""",
                           (b["restaurant_id"], (b.get("name") or "Item").strip(),
                            (b.get("description") or "").strip(), int(b.get("price_cents") or 0),
                            sec, nxt, 0 if b.get("active") in (0, False, "0") else 1,
                            (b.get("tab") or "").strip()[:40]))
        if av:
            db().execute("UPDATE menu_items SET avail_days=?, avail_start=?, avail_end=? WHERE id=?",
                         (av["days"], av["st"], av["en"], cur.lastrowid))
        db().commit()
        return jsonify({"ok": True, "item_id": cur.lastrowid})
    if op == "update":
        db().execute("""UPDATE menu_items SET name=COALESCE(?,name),
                        description=COALESCE(?,description), price_cents=COALESCE(?,price_cents),
                        section=COALESCE(?,section), active=COALESCE(?,active),
                        menu_tab=COALESCE(?,menu_tab) WHERE id=?""",
                     (b.get("name"), b.get("description"), b.get("price_cents"),
                      b.get("section"), (1 if b.get("active") else 0) if "active" in b else None,
                      (str(b["tab"]).strip()[:40] if b.get("tab") is not None else None),
                      b["item_id"]))
        if av:
            db().execute("UPDATE menu_items SET avail_days=?, avail_start=?, avail_end=? WHERE id=?",
                         (av["days"], av["st"], av["en"], b["item_id"]))
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
        old = db().execute("SELECT image FROM menu_items WHERE id=?", (b["item_id"],)).fetchone()
        if old:
            drop_media(old["image"])
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
        nsort = int(b.get("sort") or 0) or db().execute(
            "SELECT COALESCE(MAX(sort),0)+1 n FROM option_groups WHERE item_id=?", (b["item_id"],)).fetchone()["n"]
        cur = db().execute("""INSERT INTO option_groups(item_id,name,min_select,max_select,sort,max_each)
                              VALUES(?,?,?,?,?,?)""",
                           (b["item_id"], (b.get("name") or "Choose").strip(),
                            int(b.get("min_select", 1)), int(b.get("max_select", 1)),
                            nsort, max(1, int(b.get("max_each") or 1))))
        db().commit()
        return jsonify({"ok": True, "group_id": cur.lastrowid})
    if op == "delete":
        db().execute("DELETE FROM options WHERE group_id=?", (b["group_id"],))
        db().execute("DELETE FROM option_groups WHERE id=?", (b["group_id"],))
        db().commit()
        return jsonify({"ok": True})
    if op == "update":
        db().execute("""UPDATE option_groups SET name=COALESCE(?,name),
                        min_select=COALESCE(?,min_select), max_select=COALESCE(?,max_select),
                        max_each=COALESCE(?,max_each) WHERE id=?""",
                     (b.get("name"), b.get("min_select"), b.get("max_select"), b.get("max_each"),
                      b["group_id"]))
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


# ---------------- photos ----------------
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
PHOTO_MAX_BYTES = 10 * 1024 * 1024


def media_url(name):
    return ("/media/" + name) if name else ""


def drop_media(name):
    if not name:
        return
    path = os.path.join(UPLOAD_DIR, os.path.basename(name))
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def save_photo(fs):
    """Store an uploaded picture. Big phone photos are shrunk so menus load fast."""
    raw = fs.read(PHOTO_MAX_BYTES + 1)
    if not raw:
        raise ValueError("That file was empty.")
    if len(raw) > PHOTO_MAX_BYTES:
        raise ValueError("That picture is over 10 MB. Pick a smaller one.")
    head = raw[:12]
    if head[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif head[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
    else:
        raise ValueError("Use a JPG, PNG, WEBP or GIF picture.")
    name = secrets.token_hex(10)
    try:
        from PIL import Image, ImageOps
        import io
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((1400, 1400))
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        name += ".jpg"
        im.convert("RGB").save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=84, optimize=True)
    except ImportError:
        name += "." + ext
        with open(os.path.join(UPLOAD_DIR, name), "wb") as fh:
            fh.write(raw)
    return name


@app.get("/media/<path:name>")
def media(name):
    return send_from_directory(UPLOAD_DIR, os.path.basename(name), max_age=30 * 86400)


@app.post("/api/dispatch/photo")
def api_photo():
    """Add, replace or remove the picture on a menu item or a restaurant."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    kind = request.form.get("kind")
    table = {"item": "menu_items", "restaurant": "restaurants"}.get(kind)
    try:
        rid = int(request.form.get("id") or 0)
    except ValueError:
        rid = 0
    if not table or not rid:
        return jsonify({"ok": False, "error": "Pick an item or restaurant first."}), 400
    row = db().execute("SELECT image FROM " + table + " WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found."}), 404
    if request.form.get("op") == "remove":
        drop_media(row["image"])
        db().execute("UPDATE " + table + " SET image=NULL WHERE id=?", (rid,))
        db().commit()
        return jsonify({"ok": True, "image": ""})
    fs = request.files.get("photo")
    if not fs:
        return jsonify({"ok": False, "error": "Choose a picture to upload."}), 400
    try:
        name = save_photo(fs)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "That picture could not be read. Try a JPG or PNG."}), 400
    drop_media(row["image"])
    db().execute("UPDATE " + table + " SET image=? WHERE id=?", (name, rid))
    db().commit()
    return jsonify({"ok": True, "image": media_url(name)})


@app.get("/api/dispatch/menu-full/<int:rid>")
def api_menu_full(rid):
    """Everything on one restaurant's menu for the editor, hidden items included."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"ok": False}), 404
    items = []
    for it in db().execute("SELECT * FROM menu_items WHERE restaurant_id=? ORDER BY sort, id",
                           (rid,)).fetchall():
        items.append({"id": it["id"], "name": it["name"], "description": it["description"] or "",
                      "price_cents": it["price_cents"], "price": money(it["price_cents"]),
                      "section": (it["section"] or "").strip(), "active": it["active"],
                      "tab": (it["menu_tab"] or "").strip(),
                      "image": media_url(it["image"]), "groups": item_options(it["id"]),
                      **avail_fields(it)})
    return jsonify({"ok": True, "items": items, "restaurant": {
        "id": r["id"], "name": r["name"], "slug": r["slug"], "image": media_url(r["image"])},
        "presets": presets.available_for(r["slug"], r["name"])})


@app.post("/api/dispatch/load-preset")
def api_load_preset():
    """Fill a restaurant's menu from a built-in menu (Popeyes). Replacing wipes the old menu."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    rid = b.get("restaurant_id")
    if not db().execute("SELECT 1 FROM restaurants WHERE id=?", (rid,)).fetchone():
        return jsonify({"ok": False, "error": "Unknown restaurant."}), 404
    try:
        n = presets.load(db(), rid, b.get("preset") or "", replace=bool(b.get("replace")),
                         on_drop=drop_media)
    except KeyError:
        return jsonify({"ok": False, "error": "Unknown menu."}), 400
    db().commit()
    return jsonify({"ok": True, "items": n})


# ---------------- logo ----------------
DEFAULT_LOGO = "/static/brand/logo-default.png"


def logo_url():
    name = (setting("logo_image", str) or "").strip()
    if name and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name))):
        return media_url(name)
    return DEFAULT_LOGO


def logo_path():
    name = (setting("logo_image", str) or "").strip()
    p = os.path.join(UPLOAD_DIR, os.path.basename(name)) if name else ""
    if p and os.path.exists(p):
        return p
    return os.path.join(APP_DIR_STATIC, "brand", "logo-default.png")


APP_DIR_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.post("/api/dispatch/logo")
def api_logo():
    """Change the logo shown on every page and on the driver and kitchen app icons."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    old = (setting("logo_image", str) or "").strip()
    if request.form.get("op") == "reset":
        drop_media(old)
        db().execute("DELETE FROM settings WHERE key='logo_image'")
        db().commit()
        return jsonify({"ok": True, "logo": DEFAULT_LOGO})
    fs = request.files.get("photo")
    if not fs:
        return jsonify({"ok": False, "error": "Choose a picture to upload."}), 400
    raw = fs.read(PHOTO_MAX_BYTES + 1)
    if len(raw) > PHOTO_MAX_BYTES:
        return jsonify({"ok": False, "error": "That picture is over 10 MB. Pick a smaller one."}), 400
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((512, 512))
        name = secrets.token_hex(10) + ".png"
        im.save(os.path.join(UPLOAD_DIR, name), "PNG", optimize=True)
    except Exception:
        return jsonify({"ok": False, "error": "That picture could not be read. Try a PNG or JPG."}), 400
    drop_media(old)
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('logo_image',?)", (name,))
    db().commit()
    return jsonify({"ok": True, "logo": media_url(name)})


@app.get("/brand/icon-<int:size>.png")
def brand_icon(size):
    """Square app icon made from the current logo, for phones that install the apps."""
    size = size if size in (32, 180, 192, 512) else 192
    try:
        from PIL import Image
        import io
        src = Image.open(logo_path()).convert("RGBA")
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        box = int(size * 0.8)
        src.thumbnail((box, box), Image.LANCZOS) if max(src.size) > box else None
        if max(src.size) < box:
            k = box / max(src.size)
            src = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
        canvas.paste(src, ((size - src.width) // 2, (size - src.height) // 2), src)
        out = io.BytesIO()
        canvas.convert("RGB").save(out, "PNG")
        resp = app.response_class(out.getvalue(), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:
        return redirect(DEFAULT_LOGO)


def seed_brand_photos():
    """First start after this update: put the Popeyes photo on Popeyes if it has none."""
    try:
        con = sqlite3.connect(DB_PATH)
        if con.execute("SELECT 1 FROM settings WHERE key='popeyes_photo_v1'").fetchone():
            con.close()
            return
        r = con.execute("SELECT id, image FROM restaurants WHERE slug='popeyeschicken'").fetchone()
        src = os.path.join(APP_DIR_STATIC, "brand", "popeyes.jpg")
        if r and not r[1] and os.path.exists(src):
            import shutil
            name = secrets.token_hex(10) + ".jpg"
            shutil.copyfile(src, os.path.join(UPLOAD_DIR, name))
            con.execute("UPDATE restaurants SET image=? WHERE id=?", (name, r[0]))
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('popeyes_photo_v1','1')")
        con.commit()
        con.close()
    except Exception as e:
        print("brand photo seed skipped:", e)


@app.get("/api/menu/<int:rid>")
def api_menu(rid):
    return jsonify({"ok": True, "items": menu_payload(rid)})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now()})

init_db()
seed_brand_photos()

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
               "tax_bp": setting("tax_rate_bp") or 0, "service_bp": setting("service_fee_bp") or 0,
               "logo_url": logo_url()}
    except Exception:
        biz = {"biz_name": "Fleet Delivery", "biz_address": "", "biz_phone": "", "biz_tel": "",
               "tax_bp": 900, "service_bp": 0, "logo_url": DEFAULT_LOGO}
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
                                   AND COALESCE(r.uses_app,0)=1
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

BH_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def business_hours():
    """{"0": ["10:00", "22:00"], ...} Monday is 0. Empty means no hours limit is set."""
    try:
        h = json.loads(setting("business_hours", str) or "{}")
    except Exception:
        h = {}
    return h if isinstance(h, dict) else {}


def business_in_hours(when=None):
    """Inside the business's operating hours? True when no hours are set."""
    h = business_hours()
    if not h:
        return True
    when = when or dt.datetime.now()
    m = when.hour * 60 + when.minute
    span = h.get(str(when.weekday())) or ["", ""]
    o, c = _hm(span[0]) if span[0] else None, _hm(span[1]) if span[1] else None
    if o is not None and c is not None:
        if c > o and o <= m < c:
            return True
        if c <= o and m >= o:
            return True
    # the night before running past midnight
    y = h.get(str((when.weekday() - 1) % 7)) or ["", ""]
    yo, yc = _hm(y[0]) if y[0] else None, _hm(y[1]) if y[1] else None
    if yo is not None and yc is not None and yc <= yo and m < yc:
        return True
    return False


def business_hours_rows():
    h = business_hours()
    rows = []
    for k, name in enumerate(BH_DAYS):
        span = h.get(str(k))
        rows.append({"k": k, "day": name,
                     "open": (span or ["", ""])[0], "close": (span or ["", ""])[1],
                     "closed": bool(h) and not (span and span[0] and span[1])})
    return rows


def business_hours_label():
    """'Mon - Fri 10:00 AM - 10:00 PM, Sat 11:00 AM - 11:00 PM, Sun closed'."""
    h = business_hours()
    if not h:
        return ""
    def one(k):
        s = h.get(str(k))
        return (_ampm(s[0]) + " - " + _ampm(s[1])) if s and s[0] and s[1] else "closed"
    out, k = [], 0
    while k < 7:
        j = k
        while j + 1 < 7 and one(j + 1) == one(k):
            j += 1
        short = BH_DAYS[k][:3] + ((" - " + BH_DAYS[j][:3]) if j > k else "")
        out.append(short + " " + one(k))
        k = j + 1
    return ", ".join(out)


app.jinja_env.globals["business_hours_label"] = business_hours_label


def _set(key, default=""):
    try:
        v = setting(key, str)
    except Exception:
        v = None
    return default if v is None else v


def any_rest_on():
    """Is ordering from a restaurant we do not list turned on? On unless dispatch turns it off."""
    return _set("any_on", "1") == "1"


def any_rest_row():
    return {"avail_days": _set("any_days"), "avail_start": _set("any_start"), "avail_end": _set("any_end")}


def any_rest_open(when=None):
    return any_rest_on() and item_available(any_rest_row(), when)


def any_rest_label():
    return avail_label(any_rest_row())


app.jinja_env.globals["any_rest_label"] = any_rest_label


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
