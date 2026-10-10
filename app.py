
import urllib.error
import base64, contextlib, contextvars, difflib, hashlib, os, json, math, re, secrets, sqlite3, threading, time, datetime as dt, urllib.parse, urllib.request
import dbx
from flask import Flask, g, has_request_context, request, session, redirect, url_for, render_template, render_template_string, jsonify, send_from_directory, flash, get_flashed_messages, Response, make_response
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

# Each region can run on its own time zone (Athens GA on Eastern while Opelika stays Central).
# Every stamp is still saved in APP_TZ; a region's hours, scheduled times and the times shown
# for its orders are turned into that region's local time.
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
TZ_CHOICES = [("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"),
              ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "Pacific"),
              ("America/Anchorage", "Alaska"), ("Pacific/Honolulu", "Hawaii")]
TZ_SHORT = {"America/New_York": "ET", "America/Chicago": "CT", "America/Denver": "MT", "America/Phoenix": "MST",
            "America/Los_Angeles": "PT", "America/Anchorage": "AKT", "Pacific/Honolulu": "HT"}
_ZCACHE = {}

def _zone(name):
    if not name or ZoneInfo is None:
        return None
    if name not in _ZCACHE:
        try:
            _ZCACHE[name] = ZoneInfo(name)
        except Exception:
            _ZCACHE[name] = None
    return _ZCACHE[name]

def tz_shift(naive, src, dst):
    """A wall-clock time in zone src -> the same moment as a wall-clock time in zone dst."""
    if naive is None or not src or not dst or src == dst:
        return naive
    zs, zd = _zone(src), _zone(dst)
    if zs is None or zd is None:
        return naive
    return naive.replace(tzinfo=zs).astimezone(zd).replace(tzinfo=None)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "delivery.db"))
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
# Optional second key for the map pictures the browser loads. Lock it to your website in
# Google Cloud. Falls back to GOOGLE_MAPS_API_KEY when it is not set.
GOOGLE_TILE_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "") or GOOGLE_KEY

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
# Railway/Render sit in front of the app and talk to it over plain http. Trust their
# forwarded headers so links we build use https.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"
# A blank SECRET_KEY in Railway used to break every login with an error page.
# Blank or missing now falls back to a built-in key so sign-in keeps working;
# set a long random SECRET_KEY in Railway for real security.
app.secret_key = (os.environ.get("SECRET_KEY") or "").strip() or "dev-secret-change-me"

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

CREATE TABLE IF NOT EXISTS day_picks (
  kind TEXT NOT NULL, person_id INTEGER NOT NULL, day TEXT NOT NULL, region_ids TEXT DEFAULT '',
  PRIMARY KEY(kind, person_id, day)
);
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

REORDER_WINDOW_MIN = 15

def reorder_closed(o):
    """New orders can be made from a delivered order only until 15 minutes after it was delivered."""
    try:
        if o["dispatch_status"] != "delivered" or not o["delivered_at"]:
            return False
        at = dt.datetime.fromisoformat(str(o["delivered_at"]).replace(" ", "T")[:19])
        return dt.datetime.now() - at > dt.timedelta(minutes=REORDER_WINDOW_MIN)
    except Exception:
        return False

def reorder_closed_msg(o):
    return ("It's been more than %d minutes since %s was delivered, so you can't make a new order from it. "
            "Create a new order instead." % (REORDER_WINDOW_MIN, o["code"]))

def now():
    return dt.datetime.now().isoformat(timespec="seconds")

def log(kind, detail):
    db().execute("INSERT INTO events(kind,detail,created_at) VALUES(?,?,?)", (kind, detail, now()))

DROP_STYLES = {"door": "Meet at door", "no_contact": "No contact delivery", "call": "Call on delivery"}
def clean_drop_style(v):
    v = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in DROP_STYLES else "door"


def ensure_column(con, table, col, decl):
    cols = dbx.columns(con, table)
    if col not in cols:
        con.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + decl)

def seed_dev_account(con):
    """Set DEV_USERNAME and DEV_PASSWORD (and optionally DEV_NAME) on the server to guarantee a
    developer login exists on this copy. It is created if missing and its password follows the
    server setting on every start, so the developer can always get back in."""
    u = (os.environ.get("DEV_USERNAME") or "").strip().lower()
    pw = (os.environ.get("DEV_PASSWORD") or "").strip()
    if not u or not pw:
        return
    name = (os.environ.get("DEV_NAME") or "Developer").strip() or "Developer"
    row = con.execute("SELECT id FROM dispatchers WHERE username=?", (u,)).fetchone()
    if row:
        con.execute("UPDATE dispatchers SET is_dev=1, password=? WHERE id=?", (pw, row["id"]))
    else:
        con.execute("INSERT INTO dispatchers(name,username,password,created_at,is_dev) VALUES(?,?,?,?,1)",
                    (name, u, pw, dt.datetime.now().isoformat(timespec="seconds")))


def init_db():
    con = dbx.connect(DB_PATH)
    dbx.install_functions(con)
    con.executescript(SCHEMA)
    ensure_column(con, "drivers", "payout_wallet", "TEXT DEFAULT 'paypal'")
    ensure_column(con, "call_alerts", "kind", "TEXT")       # '911' when a driver hit Call 911
    ensure_column(con, "call_alerts", "lat", "REAL")        # where the driver was when they hit it
    ensure_column(con, "call_alerts", "lng", "REAL")
    ensure_column(con, "drivers", "payout_branch_id", "TEXT")      # the driver's Branch worker ID
    ensure_column(con, "drivers", "payout_branch_ids", "TEXT")     # JSON {Branch account id: worker ID} when it differs per account
    ensure_column(con, "drivers", "auto_pay", "INTEGER DEFAULT 1")   # 0 = never auto pay this driver
    ensure_column(con, "drivers", "branch_account_id", "INTEGER")    # which Branch account pays them (blank = by brand)
    con.execute("""CREATE TABLE IF NOT EXISTS branch_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL, mode TEXT DEFAULT 'sandbox', org_id TEXT, api_key TEXT,
                   site_id INTEGER, active INTEGER DEFAULT 1, created_at TEXT)""")
    ensure_column(con, "drivers", "payout_email", "TEXT")
    ensure_column(con, "orders", "drop_style", "TEXT")   # door / no_contact / call: how the customer wants it handed off
    ensure_column(con, "drivers", "payout_phone", "TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, driver_id INTEGER NOT NULL,
        cents INTEGER NOT NULL, wallet TEXT, receiver TEXT, sender_id TEXT, batch_id TEXT, item_id TEXT,
        status TEXT, error TEXT, created_at TEXT, created_by TEXT, checked_at TEXT)""")
    ensure_column(con, "driver_payouts", "kind", "TEXT DEFAULT 'trip'")
    ensure_column(con, "driver_payouts", "pp_acct", "INTEGER")   # which PayPal keys sent it (brand id, 0 = main)
    ensure_column(con, "driver_payouts", "reason", "TEXT")
    ensure_column(con, "driver_payouts", "ref", "TEXT")
    ensure_column(con, "driver_payouts", "br_account_id", "INTEGER")
    ensure_column(con, "drivers", "bank_name", "TEXT")
    ensure_column(con, "drivers", "bank_last4", "TEXT")
    # Bank transfer is no longer a saved (auto pay) way to pay a driver: PayPal or Venmo only.
    con.execute("UPDATE drivers SET payout_wallet='paypal' WHERE LOWER(COALESCE(payout_wallet,''))='bank'")
    con.execute("""CREATE TABLE IF NOT EXISTS rest_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        period_start TEXT, period_end TEXT, order_count INTEGER DEFAULT 0,
        food_cents INTEGER DEFAULT 0, tax_cents INTEGER DEFAULT 0, include_tax INTEGER DEFAULT 1,
        commission_pct REAL DEFAULT 0, commission_cents INTEGER DEFAULT 0,
        adjust_cents INTEGER DEFAULT 0, adjust_note TEXT, total_cents INTEGER DEFAULT 0,
        status TEXT DEFAULT 'unpaid', pay_method TEXT, check_number TEXT, pay_ref TEXT,
        paid_at TEXT, paid_by TEXT, notes TEXT, created_at TEXT, created_by TEXT)""")
    ensure_column(con, "orders", "rest_invoice_id", "INTEGER")
    con.execute("""CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
        url TEXT NOT NULL, listed INTEGER DEFAULT 1, active INTEGER DEFAULT 1, created_at TEXT)""")
    ensure_column(con, "companies", "site_id", "INTEGER DEFAULT 0")   # brand shown in the shared apps (0 = match by web address)
    cur = con.execute("SELECT COUNT(*) c FROM restaurants")
    _fresh = con.execute("SELECT value FROM settings WHERE key='fresh_start'").fetchone()
    if cur.fetchone()["c"] == 0 and not (_fresh and _fresh[0] == "1"):
        seed(con)   # a copy that was wiped with Start fresh stays empty
    topup_restaurants(con)
    for k, v in [("base_fee_cents", "399"), ("base_miles", "3"), ("per_mile_cents", "100"),
                 ("tax_rate_bp", "900"), ("service_fee_bp", "0"), ("business_open", "0"), ("future_lead_min", "45"), ("kitchen_accept_min", "5"), ("driver_accept_min", "3"), ("late_sound_after_min", "3"), ("driver_done_cleared_at", ""), ("auto_assign", "1"), ("kitchen_hold", "1"), ("keep_awake_driver", "1"), ("keep_awake_kitchen", "1"), ("auto_driver_pay", "1"), ("auto_pay_cap_cents", "2500"), ("auto_pay_delay_min", "15"), ("max_stack_default", "3"), ("sched_lead_min", "60"), ("stack_by_location", "1"), ("stack_pickup_mi", "0.5"), ("stack_detour_mi", "2"),
                 ("assign_on_pending", "0"), ("week_open_dow", "4"), ("week_open_date", ""), ("one_run_at_a_time", "0"),
                 ("tip_prompt", "1"), ("dispatch_phone", "3342092844"),
                 ("loyalty_on", "1"), ("points_per_dollar", "1"), ("reward_points", "100"),
                 ("reward_value_cents", "500"), ("confirm_call", "1"),
                 ("signup_points", "50"), ("review_points", "25"), ("reward_options", "150:300,250:500,400:1000"),
                 ("tier_vip_points", "750"), ("tier_elite_points", "1500"), ("bonus_weekday", "1"),
                 ("bonus_starter_pct", "50"), ("bonus_vip_pct", "100"), ("bonus_elite_pct", "200"),
                 ("gift_min_cents", "1000"), ("gift_max_cents", "50000"),
                 ("business_name", "Fleet Foot Delivery"),
                 ("business_address", "216 S 8th St, Opelika, AL 36801"),
                 ("order_tokens", "Online,App,Phone call,Third party"),
                 ]:
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Business now starts Closed. Existing databases are closed once, then dispatch opens it.
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('order_keep_days','0')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('allow_cash','0')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('gps_help_text','')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('order_purge_last','')")
    if not con.execute("SELECT 1 FROM settings WHERE key='biz_default_closed_v1'").fetchone():
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_open','0')")
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('biz_default_closed_v1','1')")
    if not con.execute("SELECT 1 FROM settings WHERE key='stack_limit_v2'").fetchone():
        # Stack limit is back on: every driver starts at 3. Dispatch can raise it per driver,
        # up to unlimited (stored as 999).
        con.execute("UPDATE drivers SET max_stack=3")
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('stack_limit_v2','1')")
    _ms = con.execute("SELECT value FROM settings WHERE key='max_stack_default'").fetchone()
    try:
        _ms = int(float(_ms[0])) if _ms else 3
    except (TypeError, ValueError):
        _ms = 3
    con.execute("UPDATE drivers SET max_stack=? WHERE max_stack IS NULL OR max_stack < 1", (_ms if _ms >= 1 else 3,))
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
    ensure_column(con, "restaurants", "logo", "TEXT")
    ensure_column(con, "drivers", "short_alert_at", "TEXT")
    ensure_column(con, "restaurants", "zup_id", "TEXT")
    ensure_column(con, "restaurants", "phone_checked", "INTEGER DEFAULT 0")
    ensure_column(con, "restaurants", "places_checked", "INTEGER DEFAULT 0")
    ensure_column(con, "geocache", "src", "TEXT")   # google / osm: OpenStreetMap results are re-asked once Google is on
    ensure_column(con, "restaurants", "eta_min", "INTEGER")
    ensure_column(con, "menu_items", "zup_id", "TEXT")
    ensure_column(con, "option_groups", "max_each", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(con, "orders", "payment_status", "TEXT NOT NULL DEFAULT 'unpaid'")
    ensure_column(con, "orders", "pay_method", "TEXT")
    ensure_column(con, "orders", "paid_at", "TEXT")
    ensure_column(con, "orders", "pay_link", "TEXT")
    ensure_column(con, "orders", "pp_vault_id", "TEXT")
    ensure_column(con, "orders", "pp_vault_src", "TEXT")
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
    ensure_column(con, "orders", "tip_sig_data", "TEXT")   # the signature PNG itself, so a copy is always on file
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
    ensure_column(con, "drivers", "track_id", "TEXT")
    ensure_column(con, "drivers", "left_app_at", "TEXT")
    ensure_column(con, "drivers", "last_bg_at", "TEXT")
    # customer accounts, saved cards (kept at PayPal), rewards points and gift cards
    con.executescript("""
    CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT UNIQUE,
        email TEXT, pw_hash TEXT, points INTEGER NOT NULL DEFAULT 0, pp_customer_id TEXT,
        created_at TEXT, last_login_at TEXT);
    CREATE TABLE IF NOT EXISTS saved_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
        vault_id TEXT UNIQUE, brand TEXT, last4 TEXT, expiry TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS points_log (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
        order_id INTEGER, points INTEGER NOT NULL, note TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS gift_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, ref TEXT UNIQUE,
        initial_cents INTEGER NOT NULL, balance_cents INTEGER NOT NULL DEFAULT 0, buyer_name TEXT, buyer_phone TEXT,
        buyer_email TEXT, to_name TEXT, message TEXT, status TEXT NOT NULL DEFAULT 'pending', pay_method TEXT,
        pay_ref TEXT, pp_order_id TEXT, sold_by TEXT, customer_id INTEGER, created_at TEXT, activated_at TEXT);
    CREATE TABLE IF NOT EXISTS gift_txns (id INTEGER PRIMARY KEY AUTOINCREMENT, gift_card_id INTEGER NOT NULL,
        order_id INTEGER, cents INTEGER NOT NULL, note TEXT, by_name TEXT, created_at TEXT);
    """)
    for _c, _t in (("customer_id", "INTEGER"), ("gift_card_id", "INTEGER"),
                   ("gift_cents", "INTEGER NOT NULL DEFAULT 0"), ("reward_cents", "INTEGER NOT NULL DEFAULT 0"),
                   ("reward_points", "INTEGER NOT NULL DEFAULT 0"), ("credits_settled", "INTEGER NOT NULL DEFAULT 0"),
                   ("points_awarded", "INTEGER"), ("confirm_state", "TEXT"), ("save_card", "INTEGER NOT NULL DEFAULT 0"),
                   ("multi_with", "TEXT"), ("cust_comments", "INTEGER NOT NULL DEFAULT 0"),
                   ("credit_cents", "INTEGER NOT NULL DEFAULT 0")):
        ensure_column(con, "orders", _c, _t)
    ensure_column(con, "orders", "points_reversed", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "discount_cents", "INTEGER NOT NULL DEFAULT 0")   # dispatch discount on a placed order
    ensure_column(con, "orders", "discount_note", "TEXT")
    ensure_column(con, "points_log", "kind", "TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER UNIQUE,
        customer_id INTEGER, restaurant_id INTEGER, stars INTEGER NOT NULL, comment TEXT, created_at TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS revgeo (k TEXT PRIMARY KEY, address TEXT, created_at TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS regions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT UNIQUE NOT NULL, sort INTEGER DEFAULT 0, created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_regions (driver_id INTEGER NOT NULL, region_id INTEGER NOT NULL,
                   PRIMARY KEY(driver_id, region_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS dispatcher_regions (dispatcher_id INTEGER NOT NULL,
                   region_id INTEGER NOT NULL, PRIMARY KEY(dispatcher_id, region_id))""")
    ensure_column(con, "restaurants", "region_id", "INTEGER")
    ensure_column(con, "regions", "paused", "INTEGER DEFAULT 0")
    ensure_column(con, "regions", "paused_by", "TEXT")
    ensure_column(con, "regions", "paused_at", "TEXT")
    ensure_column(con, "regions", "hours", "TEXT")          # blank = use the business hours
    ensure_column(con, "regions", "closed_dates", "TEXT")   # "2026-11-26,2026-12-25"
    ensure_column(con, "regions", "closed_reasons", "TEXT")   # {"2026-11-26": "Thanksgiving"} optional, shown to customers
    ensure_column(con, "regions", "closed_hours", "TEXT")   # {"2026-11-26": ["15:00","23:59"]} closed only that window
    ensure_column(con, "regions", "phone", "TEXT")          # blank = the business dispatch number
    con.execute("""CREATE TABLE IF NOT EXISTS sites (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   phone TEXT, domains TEXT, logo TEXT, sort INTEGER DEFAULT 0, created_at TEXT)""")
    ensure_column(con, "regions", "site_id", "INTEGER DEFAULT 0")   # 0 = not tied to a brand site
    ensure_column(con, "sites", "design", "TEXT")                   # JSON: colors, font, photos, home page text
    ensure_column(con, "menu_items", "image_src", "TEXT")          # where an item picture first came from
    ensure_column(con, "regions", "min_order_cents", "INTEGER")   # blank = no minimum
    ensure_column(con, "regions", "max_miles", "REAL")            # blank = no radius limit
    ensure_column(con, "regions", "base_fee_cents", "INTEGER")    # blank = the business delivery fee
    ensure_column(con, "regions", "base_miles", "REAL")           # blank = the business's first miles
    ensure_column(con, "regions", "per_mile_cents", "INTEGER")    # blank = the business per-mile fee
    ensure_column(con, "regions", "tz", "TEXT")                   # blank = the app's time zone (APP_TZ)
    ensure_column(con, "regions", "faq_text", "TEXT")
    ensure_column(con, "regions", "stats_with", "INTEGER DEFAULT 0")
    ensure_column(con, "regions", "drive_with", "INTEGER DEFAULT 0")   # drivers treat this region and another as one area   # count this region's statistics with another region
    ensure_column(con, "regions", "auto_kitchen", "INTEGER DEFAULT 0")   # 1 = Send to kitchen happens on its own             # blank = the business FAQ
    ensure_column(con, "restaurants", "min_order_cents", "INTEGER")  # blank = use the region's
    ensure_column(con, "restaurants", "partner", "INTEGER NOT NULL DEFAULT 1")  # 0 = non-partner service fee
    for _k in ("service_fee_np_bp", "driver_pay_base_cents", "driver_pay_mile_cents"):
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?, '')", (_k,))
    ensure_column(con, "restaurants", "max_miles", "REAL")           # blank = use the region's
    ensure_column(con, "drivers", "active", "INTEGER DEFAULT 1")
    ensure_column(con, "orders", "region_id", "INTEGER")
    ensure_column(con, "availability", "region_ids", "TEXT")
    ensure_column(con, "dispatchers", "is_owner", "INTEGER DEFAULT 0")
    ensure_column(con, "dispatchers", "is_dev", "INTEGER DEFAULT 0")
    seed_dev_account(con)
    ensure_column(con, "messages", "region_id", "INTEGER")
    con.execute("""CREATE TABLE IF NOT EXISTS dispatcher_availability (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   dispatcher_id INTEGER NOT NULL, dow INTEGER NOT NULL, start_time TEXT NOT NULL,
                   end_time TEXT NOT NULL, note TEXT, created_by TEXT, created_at TEXT)""")
    ensure_column(con, "dispatcher_availability", "region_ids", "TEXT")
    ensure_column(con, "dispatcher_availability", "status", "TEXT DEFAULT 'approved'")
    ensure_column(con, "dispatcher_availability", "decided_by", "TEXT")
    ensure_column(con, "dispatcher_availability", "decided_at", "TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS active_time (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind TEXT NOT NULL, person_id INTEGER NOT NULL, state TEXT NOT NULL,
                   started_at TEXT NOT NULL, last_beat TEXT NOT NULL, ended_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_active_time ON active_time(kind, person_id, ended_at)")
    con.execute("""CREATE TABLE IF NOT EXISTS driver_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   driver_id INTEGER NOT NULL, lat REAL, lng REAL, address TEXT, status TEXT,
                   event TEXT, created_at TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_driver_log ON driver_log(driver_id, created_at)")
    ensure_column(con, "orders", "ref_code", "TEXT")
    ensure_column(con, "orders", "paged_at", "TEXT")
    ensure_column(con, "orders", "kitchen_sent_at", "TEXT")
    # Restaurants not on the restaurant app yet: dispatch places the order with them by hand.
    ensure_column(con, "restaurants", "uses_app", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "restaurants", "call_method", "TEXT NOT NULL DEFAULT 'phone'")   # called-in stores: phone or online
    ensure_column(con, "restaurants", "order_url", "TEXT")                               # where dispatch orders online
    ensure_column(con, "orders", "manual_state", "TEXT")
    # PayPal / Venmo / card through PayPal: hold at checkout, charge after delivery (late tips included)
    for _c, _d in (("pp_order_id", "TEXT"), ("pp_auth_id", "TEXT"), ("pp_auth_cents", "INTEGER"),
                   ("pp_state", "TEXT"), ("pp_captured_cents", "INTEGER"), ("pp_source", "TEXT"),
                   ("pp_error", "TEXT"), ("pp_auth_at", "TEXT")):
        ensure_column(con, "orders", _c, _d)
    # Which PayPal keys took an order's payment: a brand's id, or 0 for the main keys.
    ensure_column(con, "orders", "pp_acct", "INTEGER")
    con.execute("UPDATE orders SET pp_acct=0 WHERE pp_acct IS NULL AND (pp_state IS NOT NULL OR pp_order_id IS NOT NULL)")
    ensure_column(con, "saved_cards", "pp_acct", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "gift_cards", "pp_acct", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "orders", "manual_at", "TEXT")
    ensure_column(con, "orders", "manual_by", "TEXT")
    ensure_column(con, "orders", "driver_paged_at", "TEXT")
    ensure_column(con, "orders", "driver_reminded_for", "TEXT")
    ensure_column(con, "orders", "loc_stacked", "INTEGER DEFAULT 0")
    # Send to kitchen: a new order stays off the kitchen until dispatch taps Send to kitchen,
    # even after it is paid. kitchen_go=1 means dispatch released it (old orders count as released).
    ensure_column(con, "orders", "kitchen_go", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(con, "orders", "auto_pay_note", "TEXT")
    # customer list: dispatch can enter customers as existing (existing customers skip the confirm call)
    for _c, _d in (("address", "TEXT"), ("verified", "INTEGER NOT NULL DEFAULT 0"), ("notes", "TEXT"),
                   ("source", "TEXT"), ("added_by", "TEXT"), ("reset_hash", "TEXT"), ("reset_expires", "TEXT"),
                   ("reset_tries", "INTEGER NOT NULL DEFAULT 0")):
        ensure_column(con, "customers", _c, _d)
    con.execute("""CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                   name TEXT, phone TEXT, email TEXT, region_ids TEXT, data TEXT, status TEXT NOT NULL DEFAULT 'new',
                   notes TEXT, created_at TEXT, updated_at TEXT, updated_by TEXT)""")
    for _k, _v in (("business_email", ""), ("social_x", ""), ("social_facebook", ""), ("social_instagram", ""),
                   ("home_headline", "Delivering the area's finest restaurants to your door!"), ("faq_text", "")):
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (_k, _v))
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('kitchen_hold','1')")
    con.executescript("""
    CREATE TRIGGER IF NOT EXISTS trg_kitchen_gate AFTER UPDATE OF kitchen_status ON orders
      WHEN NEW.kitchen_go=0 AND NEW.kitchen_status='pending'
      BEGIN UPDATE orders SET kitchen_status='waiting', kitchen_sent_at=NULL,
        hold_reason=CASE WHEN dispatch_status IN ('held','queued') THEN 'tap Send to kitchen' ELSE hold_reason END
        WHERE id=NEW.id; END;
    """)
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
    ensure_column(con, "orders", "primary_no", "TEXT")
    ensure_column(con, "orders", "primary_seq", "INTEGER")
    ensure_column(con, "orders", "primary_day", "TEXT")
    ensure_column(con, "orders", "auto_kitchen_at", "TEXT")   # when the region's automatic Send to kitchen fired
    ensure_column(con, "restaurants", "cuisine", "TEXT")
    con.execute("UPDATE drivers SET roster='scheduled' WHERE roster IS NULL OR roster=''")
    con.commit()
    # First start on Postgres: bring over everything from the old SQLite file (once).
    dbx.copy_from_sqlite_once(con, DB_PATH)
    dbx.forget_table_info()
    # The status history triggers, installed once the
    # late-added columns above exist
    dbx.install_triggers(con)
    presets.autoload(con)
    con.commit()
    con.close()


# The Fleet Foot Delivery store list. Blank address means dispatch has to fill it in
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
    """Add any Fleet Foot Delivery store that is not on file yet. Runs once, never
    overwrites a store a dispatcher has already edited, and never re-adds a deleted one."""
    ensure_column(con, "restaurants", "cuisine", "TEXT")
    # renamed brand: the house store keeps its sign-in code, only the name changes
    con.execute("UPDATE restaurants SET name='Fleet Foot Delivery' WHERE slug='tigertowntogo' AND name IN (?,?)",
                ("Tiger Town " + "To Go", "Fleet " + "Delivery"))
    # renamed app: a business name still on the old default picks up the new one
    con.execute("UPDATE settings SET value='Fleet Foot Delivery' WHERE key='business_name' AND value=?",
                ("Fleet " + "Delivery",))
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
    # Real Auburn / Opelika businesses, the kind of list Fleet Foot Delivery carries.
    # Addresses, phones and coordinates are real; the menu items below are sample
    # lines you edit per store from Dispatch > Manage > Menu items.
    rows = [
        ("Fleet Foot Delivery", "tigertowntogo", "1111", "216 S 8th St, Opelika, AL 36801",
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

_SITE_KEYS = {"business_name": "name", "dispatch_phone": "phone"}
_STAFF_PREFIXES = ("/dispatch", "/driver", "/restaurant", "/api/dispatch", "/api/driver", "/api/restaurant",
                   "/go/", "/api/hub", "/manifest/hub")


def _norm_host(h):
    h = (h or "").strip().lower()
    for pre in ("https://", "http://"):
        if h.startswith(pre):
            h = h[len(pre):]
    h = h.split("/")[0].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def site_domains(s):
    return [d for d in (_norm_host(x) for x in re.split(r"[\s,]+", (s["domains"] or "") if s else "")) if d]


def site_by_id(sid):
    try:
        sid = int(sid or 0)
    except (TypeError, ValueError):
        return None
    if not sid:
        return None
    return db().execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()


def _flag_on(key):
    """An on/off switch from Settings (on unless saved as 0), read once per request."""
    cache = None
    try:
        if has_request_context():
            cache = g.get("_flags")
            if cache is None:
                cache = g._flags = {}
            if key in cache:
                return cache[key]
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        v = not (row is not None and str(row["value"]).strip() == "0")
    except Exception:
        return True
    if cache is not None:
        cache[key] = v
    return v


def brands_on():
    """Settings > Brands and regions: off means one brand, so every page uses the main business."""
    return _flag_on("brands_on")


def regions_on():
    """Settings > Brands and regions: off means one area, so nobody picks a region."""
    return _flag_on("regions_on")


def site_of_region(rid):
    if not brands_on():
        return None
    try:
        rid = int(rid or 0)
    except (TypeError, ValueError):
        return None
    if not rid:
        return None
    r = db().execute("SELECT site_id FROM regions WHERE id=?", (rid,)).fetchone()
    return site_by_id(r["site_id"]) if r else None


def site_region_ids(sid):
    return {r["id"] for r in db().execute("SELECT id FROM regions WHERE COALESCE(site_id,0)=?", (int(sid),)).fetchall()}


def current_site():
    """The brand this request shows: a forced order/restaurant brand, the web address's brand,
    or (on a shared address) the brand of the area the customer picked. None on staff pages."""
    if not has_request_context():
        return None
    if not brands_on():
        return None
    if "_site_forced" in g:
        return g._site_forced
    if "_site" in g:
        return g._site
    s = host_site()
    if s is None:
        try:
            if not request.path.startswith(_STAFF_PREFIXES):
                pick = request.args.get("region")
                if pick is None:
                    pick = session.get("cust_region") or ""
                if str(pick).isdigit():
                    s = site_of_region(int(pick))
        except Exception:
            s = None
    g._site = s
    return s


def host_site():
    """The brand site of the web address itself (or a ?site= preview). None on staff pages."""
    if not has_request_context():
        return None
    if not brands_on():
        return None
    if "_hsite" in g:
        return g._hsite
    s = None
    try:
        if not request.path.startswith(_STAFF_PREFIXES):
            pv = request.args.get("site")
            if pv is not None and pv.isdigit():
                session["site_preview"] = int(pv)
            elif pv is not None or request.args.get("brand") is not None:
                session.pop("site_preview", None)   # ?site=0 or a brand pick ends a preview
            host = _norm_host(request.host)
            for row in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
                if host in site_domains(row):
                    s = row
                    break
            if s is None and session.get("site_preview"):
                s = site_by_id(session.get("site_preview"))
            if s is None and not dev_all_brands_mode():
                s = home_brand_site()   # no shared "All brands" website: every address shows one brand
    except Exception:
        s = None
    g._hsite = s
    return s


BRAND_LOCK_ON = False   # the Locked switch was removed (Oct 8, 2026): every brand is live


def site_locked(sid):
    """A locked brand is not live yet: its restaurants are hidden from customers and online ordering,
    and its company is left out of the shared driver/restaurant app picker. Unlock it to bring it in.
    Turned off with BRAND_LOCK_ON, so any lock saved earlier no longer hides a brand."""
    if not BRAND_LOCK_ON:
        return False
    try:
        return str(setting("brand_locked_%d" % int(sid), str) or "") == "1"
    except Exception:
        return False


def locked_region_ids():
    """Regions attached to a locked brand."""
    try:
        return {r["id"] for r in db().execute("SELECT id, site_id FROM regions").fetchall()
                if r["site_id"] and site_locked(r["site_id"])}
    except Exception:
        return set()


def board_hidden_regions():
    """Regions of a locked brand come off the dispatch board (queues, pause buttons,
    I'm working chips, Your regions). The developer All brands test view still shows them."""
    try:
        if dev_all_brands_mode():
            return set()
    except Exception:
        pass
    return locked_region_ids()


def restaurant_locked(r):
    """True when this restaurant's region belongs to a locked brand (developers in the test view still see it)."""
    try:
        rid = r["region_id"] if r is not None else None
    except Exception:
        rid = None
    return bool(rid) and rid in locked_region_ids() and not dev_all_brands_mode()


def dev_all_brands_mode():
    """Developer test view: with the switch on in Account > Developer access, a signed-in developer
    sees every brand on an address that belongs to no brand (the Railway address), with the old
    brand picker, all logos and the All brands FAQ. Customers and everyone else never see it."""
    try:
        if not has_request_context() or not session.get("dispatcher_id"):
            return False
        if "_devall" in g:
            return g._devall
        on = str(setting("dev_all_brands") or 0) == "1" and is_dev()
        g._devall = on
        return on
    except Exception:
        return False


def home_brand_site():
    """The brand shown on a web address that belongs to no brand (like the Railway address):
    the one picked in Settings > Regions > Brand sites, else the first brand in the list."""
    try:
        v = (db().execute("SELECT value FROM settings WHERE key='home_site_id'").fetchone() or [""])[0]
        s = site_by_id(v) if str(v or "").isdigit() else None
        if s is not None and site_locked(s["id"]):
            s = None   # a locked brand is not live: show the first unlocked brand instead
        if s is None:
            for row in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
                if not site_locked(row["id"]):
                    s = row
                    break
        if s is None:
            s = db().execute("SELECT * FROM sites ORDER BY sort, id LIMIT 1").fetchone()
        return s
    except Exception:
        return None


class order_site:
    """with order_site(region_id): texts and pages use that region's brand name and number."""
    def __init__(self, rid):
        self.rid = rid
    def __enter__(self):
        if has_request_context():
            self.had = "_site_forced" in g
            self.old = g.get("_site_forced")
            s = site_of_region(self.rid)
            if s is not None:
                g._site_forced = s
            elif not self.had:
                self.had = None
        return self
    def __exit__(self, *a):
        if has_request_context() and self.had is not None:
            if self.had:
                g._site_forced = self.old
            else:
                g.pop("_site_forced", None)
        return False


# ---------------------------------------------------------------- brand site designs
# Each brand site can have its own look. Anything left blank uses the main website's.
BRAND_FONTS = {"": ("", ""), "poppins": ("'Poppins',system-ui,sans-serif", "Poppins"),
               "montserrat": ("'Montserrat',system-ui,sans-serif", "Montserrat"),
               "nunito": ("'Nunito',system-ui,sans-serif", "Nunito"),
               "oswald": ("'Oswald','Arial Narrow',sans-serif", "Oswald"),
               "lora": ("'Lora',Georgia,serif", "Lora"),
               "roboto-slab": ("'Roboto Slab',Georgia,serif", "Roboto+Slab"),
               "georgia": ("Georgia,'Times New Roman',serif", ""),
               "arial": ("Arial,Helvetica,sans-serif", "")}
BRAND_FONT_LABELS = (("", "Standard"), ("poppins", "Poppins (modern)"), ("montserrat", "Montserrat (bold)"),
                     ("nunito", "Nunito (rounded)"), ("oswald", "Oswald (tall, sporty)"), ("lora", "Lora (classic)"),
                     ("roboto-slab", "Roboto Slab (diner)"), ("georgia", "Georgia (serif)"), ("arial", "Arial (plain)"))
BRAND_CORNERS = {"": None, "round": 16, "soft": 8, "square": 0}
BRAND_COLORS = ("brand", "brand2", "bg", "ink", "header")
BRAND_IMAGES = ("hero_image", "pocket_image")
# setting keys a brand can override on its own website
BRAND_TEXT_KEYS = ("home_headline", "home_sub", "site_announce", "how_title", "how1_t", "how1_p", "how2_t", "how2_p",
                   "how3_t", "how3_p", "rest_title", "closed_msg", "any_text", "pocket_title", "pocket_text",
                   "business_email", "business_address", "social_x", "social_facebook", "social_instagram", "faq_text")
_BRAND_SETTING_KEYS = set(BRAND_TEXT_KEYS) | set(BRAND_IMAGES)


def site_design(s):
    if s is None:
        return {}
    try:
        d = json.loads(s["design"] or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _hex_ok(v):
    v = (v or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    return v if re.fullmatch(r"#[0-9a-f]{6}", v) else ""


def _is_dark(hexv):
    r, g_, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
    return (0.299 * r + 0.587 * g_ + 0.114 * b) < 150


def brand_look(s):
    """CSS, phone bar color and font link for a brand site's own design."""
    d = site_design(s)
    css, rules = [], []
    v = {k: _hex_ok(d.get(k)) for k in BRAND_COLORS}
    if v["brand"]:
        css.append("--brand:" + v["brand"])
        css.append("--brand2:" + (v["brand2"] or v["brand"]))
    elif v["brand2"]:
        css.append("--brand2:" + v["brand2"])
    if v["bg"]:
        css.append("--bg:" + v["bg"])
    if v["ink"]:
        css.append("--ink:" + v["ink"])
    out = ":root{" + ";".join(css) + "}" if css else ""
    fam, gname = BRAND_FONTS.get(d.get("font") or "", ("", ""))
    if fam:
        rules.append("body,button,input,select,textarea{font-family:" + fam + "}")
    rad = BRAND_CORNERS.get(d.get("corners") or "")
    if rad is not None:
        rules.append(".cust .card,.cust .panel,.cust .mcard,.cust .rcard,.cust .hero,.cust .noimg,.cust .imimg"
                     "{border-radius:%dpx}" % rad)
        rules.append(".cust .btn,.cust input,.cust select,.cust textarea{border-radius:%dpx}" % min(rad, 10))
    if v["header"]:
        rules.append(".topbar{background:%s;border-bottom-color:%s}" % (v["header"], v["header"]))
        if _is_dark(v["header"]):
            rules.append(".topbar nav a,.topbar .brand{color:#fff}.topbar nav a{opacity:.9}")
    font_link = ("https://fonts.googleapis.com/css2?family=" + gname + ":wght@400;600;700;800&display=swap") if gname else ""
    return {"brand_css": out + "".join(rules), "brand_theme": v["brand"] or "", "brand_font": font_link}


def default_stack_limit():
    """Stack limit new drivers start with (Dispatch settings > Default stack limit). 999 = unlimited."""
    try:
        r = db().execute("SELECT value FROM settings WHERE key='max_stack_default'").fetchone()
        v = int(r["value"]) if r and str(r["value"]).strip() else 3
    except Exception:
        v = 3
    return v if v >= 999 else max(1, min(20, v))

def setting(key, cast=int):
    if key in _SITE_KEYS:
        s = current_site()
        if s is not None and (s[_SITE_KEYS[key]] or "").strip():
            return cast(s[_SITE_KEYS[key]].strip())
    if key in _BRAND_SETTING_KEYS:
        s = current_site()
        if s is not None:
            dv = str(site_design(s).get(key) or "").strip()
            if dv and (key not in BRAND_IMAGES or os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(dv)))):
                return cast(dv)
    # A page view reads the same settings many times (once per restaurant card); remember
    # them for the rest of that one page view. Only for page views (GET), never while saving.
    cache = None
    try:
        if has_request_context() and request.method == "GET":
            cache = g.__dict__.setdefault("_setting_cache", {})
    except Exception:
        cache = None
    if cache is not None and key in cache:
        val = cache[key]
    else:
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        val = row["value"] if row else None
        if cache is not None:
            cache[key] = val
    return cast(val) if val is not None else None

# ---------------------------------------------------------------- geo + fees

def haversine_miles(a_lat, a_lng, b_lat, b_lng):
    R = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

ROAD_FACTOR = 1.3   # straight-line -> driving estimate when no routing key is set

# Address lookups go out to Google/OpenStreetMap while the order screen waits. Keep them short,
# remember misses for a while (so pricing then placing the same bad address doesn't wait twice),
# and when the lookup service stops answering, stop asking it for a minute instead of making
# every new order wait for it to time out.
GEO_TIMEOUT = 4
_GEO_MISS = {}            # lookup text -> time it failed
_GEO_MISS_SECS = 600
_GEO_DOWN_UNTIL = [0.0]   # lookup service timed out / errored: skip it until this time
_GEO_LOCK = threading.Lock()


def _geo_missed(q):
    with _GEO_LOCK:
        t = _GEO_MISS.get(q)
        if t and time.time() - t < _GEO_MISS_SECS:
            return True
        _GEO_MISS.pop(q, None)
        return False


def _geo_miss(q):
    with _GEO_LOCK:
        if len(_GEO_MISS) > 5000:
            _GEO_MISS.clear()
        _GEO_MISS[q] = time.time()


def _geo_down():
    return time.time() < _GEO_DOWN_UNTIL[0]


def _geo_trouble():
    _GEO_DOWN_UNTIL[0] = time.time() + 60


def geocode(raw):
    """Validate + normalise a customer address. Returns dict(ok, formatted, lat, lng, source)."""
    q = " ".join(raw.lower().split())
    row = db().execute("SELECT * FROM geocache WHERE q=?", (q,)).fetchone()
    cached = None
    if row:
        cached = {"ok": bool(row["ok"]), "formatted": row["formatted"],
                  "lat": row["lat"], "lng": row["lng"], "source": "cache"}
        if not GOOGLE_KEY or (row["src"] or "") == "google":
            return cached
        # saved from OpenStreetMap before the Google key was added: ask Google fresh,
        # unless Google just failed on it or isn't answering right now
        if cached["ok"] and (_geo_missed(q) or _geo_down()):
            return cached
    if not q or _geo_missed(q) or _geo_down():
        if cached and cached["ok"]:
            return cached
        return {"ok": False, "formatted": None, "lat": None, "lng": None, "source": "none"}
    res = None
    try:
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?address="
                   + urllib.parse.quote(raw) + "&key=" + GOOGLE_KEY)
            data = json.loads(urllib.request.urlopen(url, timeout=GEO_TIMEOUT).read())
            if data.get("status") == "OK":
                top = data["results"][0]
                loc = top["geometry"]["location"]
                res = {"ok": True, "formatted": top["formatted_address"],
                       "lat": loc["lat"], "lng": loc["lng"], "source": "google"}
        else:
            url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
                   + urllib.parse.quote(raw))
            req = urllib.request.Request(url, headers={"User-Agent": "fleetdelivery/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=GEO_TIMEOUT).read())
            if data:
                top = data[0]
                res = {"ok": True, "formatted": top["display_name"],
                       "lat": float(top["lat"]), "lng": float(top["lon"]), "source": "osm"}
    except Exception:
        res = None
        _geo_trouble()
    if res is None:
        _geo_miss(q)
        if cached and cached["ok"]:
            return cached          # Google didn't answer: keep using the saved one
        return {"ok": False, "formatted": None, "lat": None, "lng": None, "source": "none"}
    db().execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok,src) VALUES(?,?,?,?,1,?)",
                 (q, res["formatted"], res["lat"], res["lng"], res["source"]))
    db().commit()
    return res

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

def zip_of(text):
    hits = ZIP_RE.findall(text or "")
    return hits[-1] if hits else None

def zip_center(z):
    """Center point of a 5-digit US ZIP code, cached. None when it can't be found."""
    key = "zip:" + z
    row = db().execute("SELECT * FROM geocache WHERE q=? AND ok=1", (key,)).fetchone()
    if row and (not GOOGLE_KEY or (row["src"] or "") == "google" or _geo_missed(key) or _geo_down()):
        return {"lat": row["lat"], "lng": row["lng"], "formatted": row["formatted"]}
    if _geo_missed(key) or _geo_down():
        return None
    res = None
    try:
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?components=postal_code:"
                   + z + "|country:US&key=" + GOOGLE_KEY)
            data = json.loads(urllib.request.urlopen(url, timeout=GEO_TIMEOUT).read())
            if data.get("status") == "OK":
                loc = data["results"][0]["geometry"]["location"]
                res = {"lat": loc["lat"], "lng": loc["lng"], "formatted": data["results"][0]["formatted_address"]}
        else:
            url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=us&postalcode=" + z
            req = urllib.request.Request(url, headers={"User-Agent": "fleetdelivery/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=GEO_TIMEOUT).read())
            if data:
                res = {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"]), "formatted": data[0]["display_name"]}
    except Exception:
        res = None
        _geo_trouble()
    if not res:
        _geo_miss(key)
    if res:
        db().execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok,src) VALUES(?,?,?,?,1,?)",
                     (key, res["formatted"], res["lat"], res["lng"], "google" if GOOGLE_KEY else "osm"))
        db().commit()
    elif row:
        return {"lat": row["lat"], "lng": row["lng"], "formatted": row["formatted"]}
    return res

def zip_check(r, typed):
    """For an address we couldn't verify: is its ZIP code inside the delivery radius?"""
    z = zip_of(typed)
    if not z:
        return {"zip": None, "found": False}
    zc = zip_center(z)
    if not zc or not _rv(r, "lat") or not _rv(r, "lng"):
        return {"zip": z, "found": False}
    miles = round(haversine_miles(r["lat"], r["lng"], zc["lat"], zc["lng"]) * ROAD_FACTOR, 2)
    rules = delivery_rules(r)
    return {"zip": z, "found": True, "miles": miles, "fee": fee_for_miles(miles, _rv(r, "region_id")),
            "max_miles": rules["max_miles"],
            "within": (not rules["max_miles"]) or miles <= rules["max_miles"]}

def fee_rules(region_id=None):
    """Delivery fee for a region: its own base fee, first miles and per-mile fee, each
    falling back to the business's (Settings > Delivery fees) when left blank."""
    out = {"base_fee": setting("base_fee_cents"), "base_miles": setting("base_miles"),
           "per_mile": setting("per_mile_cents"), "from": "business"}
    reg = _region(region_id) if region_id else None
    if reg is not None:
        for k, col in (("base_fee", "base_fee_cents"), ("base_miles", "base_miles"), ("per_mile", "per_mile_cents")):
            v = _rv(reg, col)
            if v is not None:
                out[k], out["from"] = v, "region"
    return out

def fee_for_miles(miles, region_id=None):
    fr = fee_rules(region_id)
    base_fee, base_miles, per_mile = int(fr["base_fee"] or 0), float(fr["base_miles"] or 0), int(fr["per_mile"] or 0)
    if miles <= base_miles:
        return base_fee
    return base_fee + int(math.ceil(miles - base_miles)) * per_mile

def service_bp_for(r):
    """Service fee (basis points of food) for a restaurant: the business rate for partners,
    the non-partner rate (Settings > Delivery fees) for a restaurant marked non-partner."""
    try:
        bp = int(setting("service_fee_bp") or 0)
    except (TypeError, ValueError):
        bp = 0
    if r is not None and str(_rv(r, "partner")) == "0":
        raw = str(setting("service_fee_np_bp", str) or "").strip()
        if raw:
            try:
                bp = int(raw)
            except ValueError:
                pass
    return bp

def _rv(row, key):
    try:
        return row[key]
    except Exception:
        return None

def default_max_miles():
    """Business-wide delivery radius (Settings > Delivery fees), used when neither the
    restaurant nor its region has its own. 0 or blank means no limit."""
    try:
        row = db().execute("SELECT value FROM settings WHERE key='default_max_miles'").fetchone()
        v = float(row[0]) if row and str(row[0]).strip() else 0.0
    except Exception:
        v = 0.0
    return v if v > 0 else 0.0

def delivery_rules(r):
    """Minimum order and delivery radius for a restaurant. The restaurant's own setting wins;
    blank uses its region's; blank there means no limit."""
    reg = _region(_rv(r, "region_id"))
    mn, mn_src = _rv(r, "min_order_cents"), "restaurant"
    if mn is None:
        mn, mn_src = (_rv(reg, "min_order_cents") if reg is not None else None), "region"
    mx, mx_src = _rv(r, "max_miles"), "restaurant"
    if mx is None:
        mx, mx_src = (_rv(reg, "max_miles") if reg is not None else None), "region"
    if not mx:
        mx, mx_src = default_max_miles(), "business"
    return {"min_cents": int(mn or 0), "max_miles": float(mx or 0),
            "min_from": mn_src if mn else "", "miles_from": mx_src if mx else ""}

def quote(restaurant, lat, lng):
    miles = round(haversine_miles(restaurant["lat"], restaurant["lng"], lat, lng) * ROAD_FACTOR, 2)
    return miles, fee_for_miles(miles, _rv(restaurant, "region_id"))

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

def paused_region_ids():
    try:
        return {r["id"] for r in db().execute("SELECT id FROM regions WHERE COALESCE(paused,0)=1").fetchall()}
    except Exception:
        return set()


def region_paused_for(restaurant):
    rg = restaurant["region_id"] if "region_id" in restaurant.keys() else None
    return bool(rg) and rg in paused_region_ids()


# The Open 24 hours button was removed: every restaurant follows its hours table.
OPEN_24_ON = False

def is_open(restaurant, when=None):
    if restaurant["closed_override"]:
        return False
    if region_paused_for(restaurant):
        return False
    when = to_region(when or dt.datetime.now(), _rv(restaurant, "region_id"))
    if is_closed_day(restaurant["id"], when):
        return False
    if OPEN_24_ON and restaurant["open_24"]:
        return True
    hours = json.loads(restaurant["hours"])
    span = hours.get(str(when.weekday()))
    if not span or span[0] == "" or span[1] == "":
        return False
    o = dt.datetime.strptime(span[0], "%H:%M").time()
    c = dt.datetime.strptime(span[1], "%H:%M").time()
    t = when.time()
    return o <= t <= c if o <= c else (t >= o or t <= c)

def hours_label(restaurant):
    if region_paused_for(restaurant):
        return "Not taking orders right now"
    local = region_now(_rv(restaurant, "region_id"))
    shut = is_closed_day(restaurant["id"], local)
    if shut:
        return "Closed today (" + shut + ")" if shut.strip() else "Closed today"
    if OPEN_24_ON and restaurant["open_24"]:
        return "Open 24 hours"
    hours = json.loads(restaurant["hours"])
    span = hours.get(str(local.weekday()))
    if not span or not span[0]:
        return "Closed today"
    return "Today " + _clock12(span[0]) + " - " + _clock12(span[1])

def _clock12(hm):
    """'19:30' -> '7:30 PM'. Anything that isn't HH:MM is shown as it is."""
    try:
        t = dt.datetime.strptime((hm or "").strip(), "%H:%M")
    except ValueError:
        return hm
    return t.strftime("%I:%M %p").lstrip("0")

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


def slot_regions_for(kind, pid, raw, by_self):
    """Regions for one availability slot. Only regions dispatch (drivers) or the owner
    (dispatchers) assigned count; nothing assigned means no availability at all."""
    assigned = driver_region_ids(pid) if kind == "driver" else dispatcher_region_ids(pid)
    name = ""
    if not by_self:
        row = db().execute("SELECT name FROM " + ("drivers" if kind == "driver" else "dispatchers") +
                           " WHERE id=?", (pid,)).fetchone()
        name = row["name"] if row else "This person"
    if not assigned:
        if by_self:
            boss = "dispatch" if kind == "driver" else "the owner"
            return None, ("You are not assigned a region yet, so you can't set your availability. "
                          "Ask " + boss + " to assign you one.")
        boss = "dispatch or the owner" if kind == "driver" else "the owner"
        return None, name + " is not assigned a region yet. " + boss[0].upper() + boss[1:] + " has to assign one first."
    picked = parse_rids(clean_region_ids(raw))
    extra = picked - assigned
    if extra:
        return None, (("You can" if by_self else name + " can") + " only pick " +
                      ("your" if by_self else "their") + " assigned regions: " + region_names(assigned) + ".")
    return ",".join(str(x) for x in sorted(picked or assigned)), None


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


def today_slot_regions(kind, pid):
    """Regions on this person's approved availability for today (overnight shifts from
    yesterday that are still running count too)."""
    if not pid:
        return set()
    nowdt = dt.datetime.now()
    today = nowdt.date()
    out = set()
    if kind == "driver":
        for s in db().execute("""SELECT * FROM availability WHERE driver_id=? AND status='approved'
                                 AND COALESCE(region_ids,'')!=''""", (pid,)).fetchall():
            if s["week_start"]:
                try:
                    if dt.date.fromisoformat(s["week_start"]) + dt.timedelta(days=s["dow"]) != today:
                        continue
                except ValueError:
                    continue
            elif s["dow"] != today.weekday():
                continue
            out |= parse_rids(s["region_ids"])
    else:
        for s in db().execute("""SELECT * FROM dispatcher_availability WHERE dispatcher_id=?
                                 AND COALESCE(region_ids,'')!='' AND COALESCE(status,'approved')='approved'""",
                              (pid,)).fetchall():
            if s["dow"] == today.weekday() or _disp_slot_on(s, nowdt):
                out |= parse_rids(s["region_ids"])
    return out & {r["id"] for r in all_regions()}


def day_pick(kind, pid):
    """Regions this person chose to work today. Empty = no choice made, so every region
    on today's availability applies. A pick made last night still holds before 6 am."""
    if not pid:
        return set()
    nowdt = dt.datetime.now()
    days = [nowdt.date().isoformat()]
    if nowdt.hour < 6:
        days.append((nowdt.date() - dt.timedelta(days=1)).isoformat())
    for d in days:
        r = db().execute("SELECT region_ids FROM day_picks WHERE kind=? AND person_id=? AND day=?",
                         (kind, pid, d)).fetchone()
        if r and (r["region_ids"] or ""):
            return parse_rids(r["region_ids"]) & today_slot_regions(kind, pid)
    return set()


def save_day_pick(kind, pid, regions):
    """Store today's region choice. Returns an error string, or None when saved."""
    allowed = today_slot_regions(kind, pid)
    if not allowed:
        return "You don't have availability with a region set for today, so there is no region to pick."
    chosen = set()
    for x in regions or []:
        try:
            chosen.add(int(x))
        except (TypeError, ValueError):
            pass
    bad = chosen - allowed
    if bad:
        return "You can only pick the regions on today's availability: " + region_names(allowed) + "."
    db().execute("INSERT OR REPLACE INTO day_picks(kind,person_id,day,region_ids) VALUES(?,?,?,?)",
                 (kind, pid, dt.date.today().isoformat(), ",".join(str(x) for x in sorted(chosen))))
    db().commit()
    return None


def dispatch_driver_pick(did):
    """Regions dispatch put this driver in today (set from the Online / Off buttons).
    Empty = dispatch made no choice. A choice made last night still holds before 6 am."""
    if not did:
        return set()
    nowdt = dt.datetime.now()
    days = [nowdt.date().isoformat()]
    if nowdt.hour < 6:
        days.append((nowdt.date() - dt.timedelta(days=1)).isoformat())
    valid = {r["id"] for r in all_regions()}
    for d in days:
        r = db().execute("SELECT region_ids FROM day_picks WHERE kind='driver_set' AND person_id=? AND day=?",
                         (did, d)).fetchone()
        if r and (r["region_ids"] or ""):
            return parse_rids(r["region_ids"]) & valid
    return set()


def clear_dispatch_driver_pick(did):
    db().execute("DELETE FROM day_picks WHERE kind='driver_set' AND person_id=?", (did,))


def driver_region_choices(did, dispatcher_id=None):
    """Regions dispatch may put this driver in: the regions the driver is eligible for
    (assigned to them; none assigned = every region). A non-owner dispatcher only gets the
    regions they work."""
    regs = all_regions()
    mine = driver_region_ids(did)
    ids = [r["id"] for r in regs if not mine or r["id"] in mine]
    if dispatcher_id and not is_owner(dispatcher_id):
        view = dispatcher_view_regions(dispatcher_id)
        if view:
            ids = [i for i in ids if i in view]
    names = {r["id"]: r["name"] for r in regs}
    return [{"id": i, "name": names[i], "drive_lead": drive_lead_of(i)} for i in ids]


def drive_lead_of(rid):
    """The shared id of a combined driver area (Auburn + Downtown Auburn), else 0."""
    try:
        return _drive_map().get(rid, rid) if rid and len(drive_group(rid)) > 1 else 0
    except Exception:
        return 0


def driver_locked_regions(did):
    """Regions a driver has live orders in. They stay in that region until it is delivered."""
    out = set()
    for o in db().execute(
            """SELECT region_id FROM orders WHERE driver_id=? AND region_id IS NOT NULL AND region_id != 0
               AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""", (did,)).fetchall():
        out |= drive_group(o["region_id"])   # a combined area counts as one region here
    return out


_ALL = object()


def dispatcher_driver_scope():
    """Which drivers the signed-in dispatcher may see. None = every driver (owners, and
    dispatchers with no region). Otherwise the set of regions they work."""
    me = session.get("dispatcher_id")
    if not me or is_owner(me):
        return None
    return dispatcher_view_regions(me) or None


def driver_in_scope(drv_id, scope=_ALL):
    """A dispatcher sees a driver who is assigned to, working in, or on a live order in one of
    the dispatcher's regions. A driver with no region covers every region, so everyone sees them."""
    if scope is _ALL:
        scope = dispatcher_driver_scope()
    if scope is None or not drv_id:
        return True
    try:
        drv_id = int(drv_id)
    except (TypeError, ValueError):
        return False
    assigned = driver_region_ids(drv_id)
    if not assigned:
        return True
    return bool((assigned & scope) or (driver_work_regions(drv_id) & scope) or (driver_locked_regions(drv_id) & scope))


def scoped_drivers(rows):
    scope = dispatcher_driver_scope()
    if scope is None:
        return list(rows)
    return [d for d in rows if driver_in_scope(d["id"], scope)]


def out_of_scope(drv_id):
    """403 response when the driver isn't in this dispatcher's regions, else None."""
    if session.get("dispatcher_id") and not driver_in_scope(drv_id):
        return jsonify({"ok": False, "error": "That driver is not in your region."}), 403
    return None


def driver_work_regions(did):
    """Regions a driver works right now: the regions dispatch put them in today, else the
    regions picked on the availability they are working at this moment, otherwise their
    usual regions."""
    set_by_dispatch = dispatch_driver_pick(did)
    if set_by_dispatch:
        return set_by_dispatch
    chosen = day_pick("driver", did)
    if chosen:
        return chosen
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
    chosen = day_pick("dispatcher", did)
    if chosen:
        return chosen
    nowdt = dt.datetime.now()
    picked = set()
    for s in db().execute("""SELECT * FROM dispatcher_availability WHERE dispatcher_id=?
                             AND COALESCE(region_ids,'')!='' AND COALESCE(status,'approved')='approved'""", (did,)).fetchall():
        if _disp_slot_on(s, nowdt):
            picked |= parse_rids(s["region_ids"])
    return picked or dispatcher_region_ids(did)


def dispatcher_view_regions(did):
    """What a dispatcher sees on the board: every region they are assigned, plus any
    region picked on the shift they are working now. None assigned = all regions."""
    if not did:
        return set()
    chosen = day_pick("dispatcher", did)
    if chosen:
        return chosen      # they picked the regions they work today, so the board shows just those
    return dispatcher_region_ids(did) | dispatcher_work_regions(did)


def order_lock_on():
    """Settings > Brands and regions: only dispatchers assigned to a region can create its orders."""
    try:
        row = db().execute("SELECT value FROM settings WHERE key='dispatch_region_lock'").fetchone()
        return row is not None and str(row["value"]).strip() == "1"
    except Exception:
        return False


def can_create_in_region(rid, did=None):
    """With the lock on, a dispatcher creates orders only for restaurants in regions assigned
    to them. Owners always can, and so can anyone for a restaurant or pickup with no region."""
    did = did if did is not None else session.get("dispatcher_id")
    try:
        rid = int(rid or 0)
    except (TypeError, ValueError):
        rid = 0
    if not rid or not order_lock_on() or is_owner(did):
        return True
    return rid in dispatcher_region_ids(did)


def region_label(rid):
    """'Auburn (Bulldawg Food)': region name plus the brand it belongs to, when brands are on."""
    cache = g.setdefault("_rglabels", {}) if has_request_context() else {}
    rid = int(rid or 0)
    if rid in cache:
        return cache[rid]
    if not rid:
        lab = "No region"
    else:
        row = db().execute("SELECT name FROM regions WHERE id=?", (rid,)).fetchone()
        lab = row["name"] if row else "No region"
        try:
            so = site_of_region(rid)
            if row and so is not None and (so["name"] or "").strip() and so["name"].strip().lower() != lab.lower():
                lab += " (" + so["name"].strip() + ")"
        except Exception:
            pass
    cache[rid] = lab
    return lab


def is_owner(did=None):
    """Owners and developers: full access to the business. (Developers still can't delete
    orders, or change them unless an owner allows it; see dev_guard.)"""
    did = did if did is not None else session.get("dispatcher_id")
    if not did:
        return False
    r = db().execute("SELECT is_owner, COALESCE(is_dev,0) AS is_dev FROM dispatchers WHERE id=?", (did,)).fetchone()
    return bool(r and (r["is_owner"] or r["is_dev"]))


def is_dev(did=None):
    did = did if did is not None else session.get("dispatcher_id")
    if not did:
        return False
    r = db().execute("SELECT COALESCE(is_dev,0) AS d FROM dispatchers WHERE id=?", (did,)).fetchone()
    return bool(r and r["d"])


def is_real_owner(did=None):
    """An owner account that is not a developer: the business itself."""
    did = did if did is not None else session.get("dispatcher_id")
    if not did:
        return False
    r = db().execute("SELECT is_owner, COALESCE(is_dev,0) AS d FROM dispatchers WHERE id=?", (did,)).fetchone()
    return bool(r and r["is_owner"] and not r["d"])


def dev_can_edit_orders():
    return (setting("dev_order_edit") or 0) == 1


# Order changes a developer needs the owner's permission for.
DEV_ORDER_EDIT_PATHS = {
    "/api/order/approve-address", "/api/order/status", "/api/order/mark-paid", "/api/order/send-kitchen",
    "/api/order/cash", "/api/order/credit", "/api/order/discount", "/api/order/refund", "/api/order/timer",
    "/api/order/edit", "/api/order/note", "/api/order/hold", "/api/order/send-to-driver", "/api/order/reopen",
    "/api/order/card-save", "/api/paypal/replace-card", "/api/dispatch/assign", "/api/dispatch/reorder",
    "/api/dispatch/future-cancel", "/api/dispatch/future-release", "/api/dispatch/confirm-call",
}
# Never allowed for a developer, permission or not.
DEV_NO_DELETE_PATHS = {"/api/dispatch/delete-orders", "/api/dispatch/purge-orders"}


@app.before_request
def dev_guard():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    path = request.path or ""
    if path not in DEV_NO_DELETE_PATHS and path not in DEV_ORDER_EDIT_PATHS:
        return None
    if not session.get("dispatcher_id") or not is_dev():
        return None
    if path in DEV_NO_DELETE_PATHS:
        return jsonify({"ok": False, "error": "Developer accounts can't delete orders."}), 403
    if not dev_can_edit_orders():
        return jsonify({"ok": False, "error": "Developer accounts can't change orders until an owner turns on "
                                              "\"Let developers change orders\" on the Dispatcher accounts page."}), 403
    return None


# --- pause switch for non-payment ------------------------------------------
# SERVICE_SUSPENDED in Railway: 1 = site runs normally, 2 = site is blocked (paused).
# When blocked, customers, drivers, dispatchers and kitchens see a "paused" page; a
# signed-in developer keeps full access. Nothing is deleted. Any value other than 2
# (including a missing variable) keeps the site running.
# Optional SUSPEND_MESSAGE replaces the wording on the paused page.
SUSPEND_OPEN_PREFIXES = ("/static/", "/brand/", "/media/", "/uploads/", "/.well-known/", "/manifest/", "/privacy", "/delete-account")
SUSPEND_OPEN_PATHS = {"/dispatch/login", "/dispatch/logout", "/favicon.ico", "/robots.txt", "/sw.js"}


def service_suspended():
    return (os.environ.get("SERVICE_SUSPENDED") or "").strip() == "2"


def suspend_guard():
    if not service_suspended():
        return None
    path = request.path or "/"
    if path in SUSPEND_OPEN_PATHS or path.startswith(SUSPEND_OPEN_PREFIXES):
        return None
    try:
        if session.get("dispatcher_id") and is_dev():
            return None
    except Exception:
        pass
    msg = (os.environ.get("SUSPEND_MESSAGE") or "").strip() or \
        "Online ordering is temporarily unavailable. Please check back soon."
    if path.startswith("/api/") or request.is_json or "application/json" in (request.headers.get("Accept") or ""):
        resp = jsonify({"ok": False, "suspended": True, "error": msg})
    else:
        try:
            biz = (setting("business_name", str) or "Fleet Foot Delivery").strip() or "Fleet Foot Delivery"
            logo = logo_url()
        except Exception:
            biz, logo = "Fleet Foot Delivery", DEFAULT_LOGO
        e = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta name="robots" content="noindex"><title>' + e(biz) + ' - temporarily unavailable</title>'
                '<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;'
                'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f6f8;color:#222}'
                '.card{background:#fff;max-width:440px;margin:24px;padding:36px 28px;border-radius:16px;'
                'box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center}'
                'img{width:96px;height:96px;object-fit:contain;margin-bottom:12px}'
                'h1{font-size:22px;margin:0 0 10px}p{font-size:16px;line-height:1.5;color:#555;margin:0}</style>'
                '</head><body><div class="card"><img src="' + e(logo) + '" alt="">'
                '<h1>' + e(biz) + '</h1><p>' + e(msg) + '</p></div></body></html>')
        resp = app.response_class(html, mimetype="text/html")
    resp.status_code = 503
    resp.headers["Retry-After"] = "3600"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Runs before every other check so nothing slips past while paused.
app.before_request_funcs.setdefault(None, []).insert(0, suspend_guard)


# --- a brand hosted on the Railway address but marked unavailable there --------------------
# Regions page > Brand sites > Edit > "On the Railway address": the brand shown on the Railway
# address (and any address set up for no brand) can be marked unavailable. Customers on that
# address then see an "unavailable here" page that points to the brand's own web address.
# The brand's own web addresses, staff pages, the shared apps and signed-in staff are not touched.

def rail_off(sid):
    try:
        return str(setting("rail_off_%d" % int(sid), str) or "") == "1"
    except Exception:
        return False


RAIL_OPEN_PREFIXES = SUSPEND_OPEN_PREFIXES + _STAFF_PREFIXES


def rail_guard():
    try:
        if not brands_on():
            return None
        path = request.path or "/"
        if path in SUSPEND_OPEN_PATHS or path.startswith(RAIL_OPEN_PREFIXES):
            return None
        if session.get("dispatcher_id"):
            return None   # staff can still look at it
        host = _norm_host(request.host)
        rows = db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall()
        if any(host in site_domains(r) for r in rows):
            return None   # a brand's own web address
        h = home_brand_site()
        if h is None or not rail_off(h["id"]):
            return None
        name = (h["name"] or "").strip() or "This brand"
        doms = [d for d in site_domains(h) if not d.endswith(".up.railway.app")]
        msg = name + " isn't available at this web address."
        if doms:
            msg += " Please order at " + doms[0] + "."
        if path.startswith("/api/") or request.is_json or "application/json" in (request.headers.get("Accept") or ""):
            resp = jsonify({"ok": False, "unavailable": True, "error": msg})
        else:
            lg = (h["logo"] or "").strip()
            logo = media_url(lg) if lg and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(lg))) else ""
            e = lambda x: (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            link = ('<p style="margin-top:18px"><a href="https://' + e(doms[0]) + '" style="display:inline-block;'
                    'background:#222;color:#fff;padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:600">'
                    'Go to ' + e(doms[0]) + '</a></p>') if doms else ""
            html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<meta name="robots" content="noindex"><title>' + e(name) + ' - unavailable here</title>'
                    '<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;'
                    'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f6f8;color:#222}'
                    '.card{background:#fff;max-width:440px;margin:24px;padding:36px 28px;border-radius:16px;'
                    'box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center}'
                    'img{width:96px;height:96px;object-fit:contain;margin-bottom:12px}'
                    'h1{font-size:22px;margin:0 0 10px}p{font-size:16px;line-height:1.5;color:#555;margin:0}</style>'
                    '</head><body><div class="card">' + ('<img src="' + e(logo) + '" alt="">' if logo else '') +
                    '<h1>' + e(name) + '</h1><p>' + e(msg) + '</p>' + link + '</div></body></html>')
            resp = app.response_class(html, mimetype="text/html")
        resp.status_code = 503
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        return None


app.before_request_funcs[None].insert(1, rail_guard)


def covers(region_ids, order_region):
    """Does someone covering these regions see an order in this region? No regions = all."""
    return not region_ids or not order_region or order_region in region_ids


def _drive_map():
    """{region_id: lead region} for regions whose drivers are combined (Auburn + Downtown Auburn)."""
    cache = g.get("_drive_map") if has_request_context() else None
    if cache is not None:
        return cache
    try:
        rows = db().execute("SELECT id, COALESCE(drive_with,0) dw FROM regions").fetchall()
    except Exception:
        rows = []
    m = {r["id"]: (r["dw"] or r["id"]) for r in rows}
    if has_request_context():
        g._drive_map = m
    return m


def drive_group(rid):
    """Every region a driver treats as the same area as this one (just itself when not combined)."""
    try:
        rid = int(rid or 0)
    except (TypeError, ValueError):
        return set()
    if not rid:
        return set()
    m = _drive_map()
    root = m.get(rid, rid)
    return {x for x, r in m.items() if r == root} | {rid}


def driver_covers(region_ids, order_region):
    """covers() for drivers: regions combined for drivers count as one area. Restaurants and
    pausing stay per region, so a combined region can still be paused on its own."""
    if covers(region_ids, order_region):
        return True
    return bool(set(region_ids) & drive_group(order_region))


def region_names(ids):
    if not ids:
        return "All regions"
    regs = [r for r in all_regions() if r["id"] in ids]
    # regions combined for drivers read as one area: "Auburn + Downtown Auburn"
    out, seen = [], set()
    for r in regs:
        if r["id"] in seen:
            continue
        grp = [x for x in regs if x["id"] in drive_group(r["id"])] if drive_lead_of(r["id"]) else [r]
        seen |= {x["id"] for x in grp}
        out.append(" + ".join(x["name"] for x in grp))
    return ", ".join(out) or "All regions"


def on_shift_drivers():
    """Everyone clocked on, in rotation order: fewest live orders first, then whoever has
    waited longest since their last one."""
    return db().execute("""
        SELECT d.*, (SELECT COUNT(*) FROM orders o
                     WHERE o.driver_id=d.id
                       AND o.dispatch_status NOT IN ('delivered','cancelled')) AS load
        FROM drivers d WHERE d.status='online' AND COALESCE(d.active,1)=1
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


def cross_region_driver(o, shift=None):
    """A driver already holding orders can still take an order in ANOTHER region they work,
    when no free driver covers that region. Same-region orders still wait for the rotation."""
    rg = o["region_id"]
    if not rg:
        return None
    shift = shift if shift is not None else on_shift_drivers()
    if len(shift) < 2:
        return None
    for r in shift:
        if r["load"] == 0 or r["load"] >= (r["max_stack"] or 1):
            continue
        if not driver_covers(driver_work_regions(r["id"]), rg):
            continue
        held = {(x["region_id"] or 0) for x in db().execute(
            """SELECT region_id FROM orders WHERE driver_id=?
               AND dispatch_status NOT IN ('delivered','cancelled')""", (r["id"],)).fetchall()}
        if rg in held:
            continue
        return r
    return None


def region_conflict(did, order_region, exclude_id=None):
    """A driver holding a live order in one region can't take an order in a different
    region until that order is finished. Returns the error text, or None when it's fine."""
    if not did or not order_region:
        return None
    for x in db().execute("""SELECT region_id FROM orders WHERE driver_id=? AND id!=?
                             AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                          (did, exclude_id or 0)).fetchall():
        rg = x["region_id"] or 0
        if rg and rg != order_region and rg not in drive_group(order_region):
            names = {r["id"]: r["name"] for r in all_regions()}
            d = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()
            def an(w):
                return ("an " if w[:1].lower() in "aeiou" else "a ") + w
            return ((d["name"] if d else "This driver") + " is still on " + an(names.get(rg, "other region")) +
                    " order. They can take " + an(names.get(order_region, "different region")) +
                    " order once that one is delivered.")
    return None


def region_queues(region_ids=None, detail=True):
    """Waiting orders per region. region_ids empty/None = every region."""
    _hid = board_hidden_regions()
    regs = [r for r in all_regions() if (not region_ids or r["id"] in region_ids) and r["id"] not in _hid]
    pinfo = {r["id"]: r for r in db().execute("SELECT id, paused, paused_by, paused_at FROM regions").fetchall()}
    rows = db().execute("""SELECT o.*, r.name rname FROM orders o LEFT JOIN restaurants r ON r.id=o.restaurant_id
                           WHERE o.dispatch_status IN ('queued','held')
                           ORDER BY o.created_at ASC, o.id ASC""").fetchall()
    shift = db().execute("SELECT id, name FROM drivers WHERE status='online'").fetchall()
    nowdt = dt.datetime.now()
    def mins(o):
        try:
            return max(0, int((nowdt - dt.datetime.fromisoformat((o["created_at"] or "")[:19])).total_seconds() // 60))
        except ValueError:
            return 0
    out = []
    lineups = region_lineups()
    buckets = [(r["id"], r["name"]) for r in regs]
    if any(not (o["region_id"] or 0) for o in rows):
        buckets.append((0, "No region"))
    for rid, name in buckets:
        mine = [o for o in rows if (o["region_id"] or 0) == rid]
        if rid and rid in lineups:
            on = [(_line_ord(x["pos"]) + " up: " + x["name"]) if x["pos"] else
                  (x["name"] + (" (locked to " + x["locked_to"] + " until delivered)" if x.get("locked_to") else " (at stack limit)"))
                  for x in lineups[rid]["line"]]
        elif rid:
            on = [d["name"] for d in shift if driver_covers(driver_work_regions(d["id"]), rid)]
        else:
            on = [d["name"] for d in shift]
        choices = [{"id": d["id"], "name": d["name"]} for d in shift
                   if not rid or driver_covers(driver_work_regions(d["id"]), rid)]
        pi = pinfo.get(rid)
        q = {"id": rid, "name": name, "waiting": len(mine),
             "paused": bool(pi and pi["paused"]), "paused_by": (pi["paused_by"] if pi and pi["paused"] else "") or "",
             "oldest_min": mins(mine[0]) if mine else 0,
             "drivers_on": len(on),
             # regions combined for drivers (Auburn + Downtown Auburn) share this id so the board shows one queue
             "drive_lead": (_drive_map().get(rid, rid) if rid and len(drive_group(rid)) > 1 else 0)}
        if detail:
            q["drivers"] = on
            q["driver_choices"] = choices
            q["label"] = region_label(rid)
            q["orders"] = [{"id": o["id"], "code": o["code"], "pos": i + 1, "status": o["dispatch_status"],
                            "reason": o["hold_reason"] or "", "restaurant": o["rname"] or "",
                            "minutes": mins(o)} for i, o in enumerate(mine)]
        out.append(q)
    return out


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


def _line_ord(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def region_lineups():
    """Per region: the on-shift drivers working it, in rotation order, with each one's place in
    that region's line. A driver holding their stack limit drops out of a region's line unless
    they are the only driver working that region."""
    rows = on_shift_drivers()
    regs = all_regions()
    rnames = {g["id"]: g["name"] for g in regs}
    locks = {r["id"]: driver_locked_regions(r["id"]) for r in rows}
    out = {}
    for rg in regs:
        members = [r for r in rows if driver_covers(driver_work_regions(r["id"]), rg["id"])]
        n, line = 0, []
        for r in members:
            lk = locks.get(r["id"]) or set()
            if lk and rg["id"] not in lk:
                # on a live order in another region: out of this line until it is delivered
                line.append({"id": r["id"], "name": r["name"], "pos": None,
                             "locked_to": ", ".join(rnames.get(x, "another region") for x in sorted(lk))})
                continue
            full = r["load"] >= (r["max_stack"] or 1)
            if full and len(members) > 1:
                line.append({"id": r["id"], "name": r["name"], "pos": None})
            else:
                n += 1
                line.append({"id": r["id"], "name": r["name"], "pos": n})
        out[rg["id"]] = {"name": rg["name"], "line": line}
    return out


def driver_region_lines(lineups, did):
    """[{region_id, region, pos}] for one driver; pos None = holding their stack limit there.
    Regions combined for drivers show once, as 'Auburn + Downtown Auburn'."""
    out, seen = [], set()
    for rid, v in lineups.items():
        for x in v["line"]:
            if x["id"] != did:
                continue
            grp = drive_group(rid)
            key = min(grp) if len(grp) > 1 else rid
            if key in seen:
                continue
            seen.add(key)
            name = v["name"]
            if len(grp) > 1:
                names = [lineups[k]["name"] for k in lineups if k in grp]
                name = " + ".join(names) or name
            out.append({"region_id": rid, "region": name, "pos": x["pos"], "locked_to": x.get("locked_to") or ""})
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
    paused = paused_region_ids()
    rnames = {r["id"]: r["name"] for r in all_regions()}
    for i, o in enumerate(waiting):
        fit = next((fd for fd in pool if driver_covers(driver_work_regions(fd["id"]), o["region_id"])), None)
        if o["address_ok"] == 0:
            status, reason = "held", "address needs dispatch approval"
        elif o["region_id"] and o["region_id"] in paused:
            status, reason = "held", rnames.get(o["region_id"], "this region") + " is paused by dispatch"
        elif o["kitchen_status"] not in stages and not (
                o["kitchen_status"] == "pending"
                and ("manual_state" in o.keys() and o["manual_state"] == "ordering")):
            # called-in order: "Ordering in process" makes it ready for a driver
            status, reason = "held", "waiting on kitchen"
        elif not auto_on:
            status, reason = "queued", "auto dispatch off, assign by hand"
        elif fit is None:
            status, reason = "held", short
            if len(shift) >= 2 and o["region_id"] and not any(
                    driver_covers(driver_work_regions(sd["id"]), o["region_id"]) for sd in shift):
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
        if region_conflict(d["id"], o["region_id"], o["id"]):
            con.execute("UPDATE orders SET dispatch_status='held', hold_reason=? WHERE id=?",
                        ("waiting on " + d["name"] + " to finish an order in another region", o["id"]))
            continue
        seq = con.execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                             AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                          (d["id"],)).fetchone()["s"]
        con.execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned', stack_seq=?, hold_reason=NULL,
                       redo_driver_id=NULL, paged_at=? WHERE id=?""", (d["id"], seq, now(), o["id"]))
        log("redo", o["code"] + " sent to original driver " + d["name"])
    con.commit()


# ---------- Stacking by location ----------
LIVE_STOP = ('assigned', 'received', 'at_restaurant', 'enroute')
NOT_PICKED = ('assigned', 'received', 'at_restaurant')

def _num_setting(key, default, lo, hi):
    try:
        v = setting(key, str)
        if v is None or str(v).strip() == "":
            return default
        return max(lo, min(hi, float(v)))
    except Exception:
        return default

def _pick_ll(o):
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    p = pickup_of(o, r)
    return (float(p["lat"]), float(p["lng"])) if p["lat"] and p["lng"] else None

def _drop_ll(o):
    return (float(o["lat"]), float(o["lng"])) if o["lat"] and o["lng"] else None

def _road_mi(a, b):
    return haversine_miles(a[0], a[1], b[0], b[1]) * ROAD_FACTOR

def _nn_path(start, pts):
    """Nearest-next ordering of drop-offs from a start point. Returns (index order, miles)."""
    left = list(range(len(pts)))
    cur, total, order = start, 0.0, []
    while left:
        j = min(left, key=lambda k: _road_mi(cur, pts[k]))
        total += _road_mi(cur, pts[j])
        cur = pts[j]
        order.append(j)
        left.remove(j)
    return order, total

def resequence_route(did):
    """Puts a driver's stops in the shortest drop-off order. Orders already picked up
    (en route) stay first in their current order; the rest are sorted nearest-next from
    the pickup. Stops with no map point keep their place at the end."""
    live = db().execute("SELECT * FROM orders WHERE driver_id=? AND dispatch_status IN (%s) ORDER BY COALESCE(stack_seq,999), id"
                        % ",".join("?" * len(LIVE_STOP)), (did,) + LIVE_STOP).fetchall()
    fixed = [o for o in live if o["dispatch_status"] == "enroute"]
    rest = [o for o in live if o["dispatch_status"] != "enroute"]
    mapped = [o for o in rest if _drop_ll(o) and _pick_ll(o)]
    unmapped = [o for o in rest if o not in mapped]
    if len(mapped) >= 2:
        start = _drop_ll(fixed[-1]) if fixed else _pick_ll(mapped[0])
        order, _ = _nn_path(start, [_drop_ll(o) for o in mapped])
        mapped = [mapped[i] for i in order]
    final = fixed + mapped + unmapped
    changed = False
    for seq, o in enumerate(final, start=1):
        if (o["stack_seq"] or 0) != seq:
            changed = True
        db().execute("UPDATE orders SET stack_seq=? WHERE id=?", (seq, o["id"]))
    db().commit()
    return changed, [o["code"] for o in final]

def stack_match(cand, shift):
    """A busy driver this order can ride along with: same region, under their stack limit,
    nothing picked up yet, a pickup within the set distance of one of theirs, and the
    drop-off adding no more than the set extra miles to their run. Smallest detour wins."""
    if not setting("stack_by_location"):
        return None
    P, D = _pick_ll(cand), _drop_ll(cand)
    if not P or not D:
        return None
    max_pick = _num_setting("stack_pickup_mi", 0.5, 0.05, 10)
    max_det = _num_setting("stack_detour_mi", 2.0, 0.0, 30)
    best = None
    for r in shift:
        load = r["load"] or 0
        if load == 0 or load >= (r["max_stack"] or 3):
            continue
        if not driver_covers(driver_work_regions(r["id"]), cand["region_id"]) or region_conflict(r["id"], cand["region_id"]):
            continue
        live = db().execute("SELECT * FROM orders WHERE driver_id=? AND dispatch_status NOT IN ('delivered','cancelled')",
                            (r["id"],)).fetchall()
        if not live or any(o["dispatch_status"] not in NOT_PICKED for o in live):
            continue
        picks = [_pick_ll(o) for o in live]
        drops = [_drop_ll(o) for o in live]
        if any(x is None for x in picks + drops):
            continue
        if min(_road_mi(P, q) for q in picks) > max_pick:
            continue
        start = picks[0]
        _, before = _nn_path(start, drops)
        _, after = _nn_path(start, drops + [D])
        detour = after - before
        if detour <= max_det and (best is None or detour < best[1]):
            best = (r, detour)
    return best

# ---------------------------------------------------------------- automatic messages
# Every message the system sends on its own. Owners switch each one on or off in Settings.
AUTO_MSGS = [
    ("short_staff", "Busy alert to unavailable drivers", "Texts and messages drivers marked Unavailable when orders are waiting and there aren't enough free drivers to take them.", 1),
    ("drv_reminder", "Reminder to accept an order", "One reminder when a driver hasn't tapped Received in time.", 1),
    ("drv_moved", "Order moved off a driver's run", "Tells a driver an order was moved to someone else or put back on hold.", 1),
    ("drv_cancelled", "Order cancelled or refunded", "Tells the driver not to pick up an order that was cancelled or refunded.", 1),
    ("drv_reopen", "Order reopened", "Tells the driver a delivered order was reopened and sent back to them.", 1),
    ("drv_tip", "Tip changed", "Tells the driver when a customer changes the tip.", 1),
    ("drv_status", "Order marked delivered or picked up", "Confirms to the driver when an order's status is marked.", 1),
    ("drv_roster", "Moved to Scheduled or Unavailable", "Tells a driver when dispatch moves them between Scheduled and Unavailable.", 1),
    ("drv_schedule", "Schedule and time off decisions", "Tells a driver when their hours or time off are approved, denied, added, changed or removed, including when a closed date added for their region changes hours they already sent in.", 1),
    ("drv_request_ack", "Status request received", "Confirms to a driver that their online or offline request reached dispatch.", 1),
    ("rest_order", "Kitchen app: order changes", "Tells a restaurant in its kitchen app chat when dispatch changes, pulls back, cancels or adds a note to one of its orders.", 1),
    ("rest_status", "Kitchen app: restaurant status changes", "Tells a restaurant in its kitchen app chat when dispatch pauses or resumes it, sets it to open 24 hours, or changes its hours or prep time.", 1),
]
AUTO_MSG_KEYS = {k: d for k, _l, _h, d in AUTO_MSGS}
# Only a developer can switch these on or off; owners don't see them.
DEV_AUTO_MSGS = {"rest_order", "rest_status"}

def auto_msg_on(key):
    try:
        row = db().execute("SELECT value FROM settings WHERE key=?", ("am_" + key,)).fetchone()
    except Exception:
        row = None
    if not row or row["value"] in (None, ""):
        return bool(AUTO_MSG_KEYS.get(key, 1))
    return str(row["value"]) == "1"

def auto_msg(key, sql, params):
    """Write an automatic message only when that message type is switched on in Settings."""
    if auto_msg_on(key):
        return db().execute(sql, params)
    return None

def short_staff_alert():
    """Orders waiting and not enough free drivers: message every Unavailable driver who works
    that region, then repeat every few minutes while it stays that way."""
    try:
        if not auto_msg_on("short_staff") or not business_is_open():
            return
        every = setting("short_staff_every_min") or 15
        con = db()
        waiting = con.execute("""SELECT id, region_id FROM orders
                                 WHERE driver_id IS NULL AND redo_driver_id IS NULL
                                   AND dispatch_status IN ('queued','held')
                                   AND COALESCE(region_id,0) NOT IN (SELECT id FROM regions WHERE COALESCE(paused,0)=1)""").fetchall()
        if not waiting:
            return
        free = [d for d in on_shift_drivers() if d["load"] == 0]
        need = {}
        for o in waiting:
            need[o["region_id"]] = need.get(o["region_id"], 0) + 1
        short = {}
        for reg, n in need.items():
            have = sum(1 for d in free if driver_covers(driver_work_regions(d["id"]), reg))
            if n > have:
                short[reg] = n
        if not short:
            return
        cut = (dt.datetime.now() - dt.timedelta(minutes=int(every))).strftime("%Y-%m-%d %H:%M:%S")
        biz = (setting("business_name", str) or "Fleet Foot Delivery").strip() or "Fleet Foot Delivery"
        rows = con.execute("""SELECT * FROM drivers WHERE roster='unavailable' AND COALESCE(active,1)=1
                              AND status <> 'online'
                              AND (short_alert_at IS NULL OR short_alert_at < ?)""", (cut,)).fetchall()
        for d in rows:
            mine = driver_region_ids(d["id"])
            regs = [r for r in short if driver_covers(mine, r)]
            if not regs:
                continue
            n = sum(short[r] for r in regs)
            body = (biz + ": we're busy. " + str(n) + (" order is" if n == 1 else " orders are") +
                    " waiting and there aren't enough drivers. Can you come online? Open the driver app and tap Request online.")
            con.execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                        (d["id"], "system", body, now()))
            con.execute("UPDATE drivers SET short_alert_at=? WHERE id=?", (now(), d["id"]))
            ph = phone_digits(d["phone"] or "")
            if len(ph) >= 10:
                send_text("+1" + ph[-10:], body)
            log("busy_alert", "Asked " + d["name"] + " to come online (" + str(n) + " waiting)")
        con.commit()
    except Exception as e:
        print("busy alert skipped:", e)


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
        shift_now = on_shift_drivers()
        if len(shift_now) < 2:
            break
        stages = ("'pending','preparing','ready'" if setting("assign_on_pending")
                  else "'preparing','ready'")
        waiting = con.execute("""SELECT * FROM orders
                           WHERE driver_id IS NULL AND redo_driver_id IS NULL
                             AND dispatch_status IN ('queued','held')
                             AND COALESCE(region_id,0) NOT IN (SELECT id FROM regions WHERE COALESCE(paused,0)=1)
                             AND (kitchen_status IN (""" + stages + """)
                                  OR (kitchen_status='pending' AND manual_state='ordering'))
                           ORDER BY created_at ASC""").fetchall()
        pick = None
        for cand in waiting:
            m = stack_match(cand, shift_now)
            if m:
                pick = (cand, m[0], m[1])
                break
            for fd in free:
                if driver_covers(driver_work_regions(fd["id"]), cand["region_id"]) and not region_conflict(fd["id"], cand["region_id"]):
                    pick = (cand, fd, None)
                    break
            if pick:
                break
        if not pick:
            break
        o, d, detour = pick
        seq = con.execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders
                             WHERE driver_id=? AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                          (d["id"],)).fetchone()["s"]
        con.execute("""UPDATE orders SET driver_id=?, dispatch_status='assigned',
                       hold_reason=NULL, stack_seq=?, loc_stacked=? WHERE id=?""",
                    (d["id"], seq, 1 if detour is not None else 0, o["id"]))
        if detour is None:
            con.execute("UPDATE drivers SET last_assigned_at=? WHERE id=?", (now(), d["id"]))
        con.commit()
        if detour is not None:
            resequence_route(d["id"])
            log("assign", o["code"] + " -> " + d["name"] + " (stacked by location, +%.1f mi)" % detour)
        else:
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
                             AND COALESCE(o.loc_stacked,0) = 0
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
                if (fd["id"] != cand["driver_id"] and driver_covers(driver_work_regions(fd["id"]), cand["region_id"])
                        and not region_conflict(fd["id"], cand["region_id"])):
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
        if old:
            auto_msg("drv_moved", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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


def brand_sub(text, name):
    """'www.crimson2go.com is a restaurant delivery service...' -> 'Crimson 2 Go is a restaurant delivery
    service...'. A line copied from an old site often starts with its web address or a squashed name
    ('Tigertowntogo'); show the business name there instead. Other wording is left as written."""
    text = text or ""
    name = (name or "").strip()
    if not name:
        return text
    m = re.match(r"\s*(\S+)(\s+is\s+an?\s)", text, re.I)
    if not m:
        return text
    first = m.group(1)
    squashed = _norm_name(first) == _norm_name(name) or "." in first or (
        _norm_name(first) and _norm_name(first) in _norm_name(name.replace(" ", "")))
    lead = first.lower().replace("www.", "").split(".")[0]
    if squashed or _norm_name(lead) in (_norm_name(name), _norm_name(name).replace("to", "2"), _norm_name(name).replace("2", "to")):
        return name + text[m.end(1):]
    return text

app.jinja_env.filters["brandsub"] = brand_sub

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
                    "image": item_picture(it),
                    "groups": item_options(it["id"]), **avail_fields(it)})
    return out


def menus_payload(rids):
    """menu_payload for many restaurants at once: three queries in total instead of
    several per menu item, so the Create order screen opens fast with big menus."""
    rids = [int(x) for x in rids if x is not None]
    out = {rid: [] for rid in rids}
    if not rids:
        return out
    marks = ",".join("?" * len(rids))
    items = db().execute("SELECT * FROM menu_items WHERE active=1 AND restaurant_id IN (" + marks + ")"
                         " ORDER BY restaurant_id, sort, id", rids).fetchall()
    groups_of, opts_of = {}, {}
    if items:
        for g in db().execute("""SELECT g.* FROM option_groups g JOIN menu_items i ON i.id=g.item_id
                                 WHERE i.active=1 AND i.restaurant_id IN (""" + marks + """)
                                 ORDER BY g.sort, g.id""", rids).fetchall():
            groups_of.setdefault(g["item_id"], []).append(g)
        for o in db().execute("""SELECT o.* FROM options o JOIN option_groups g ON g.id=o.group_id
                                 JOIN menu_items i ON i.id=g.item_id
                                 WHERE i.active=1 AND i.restaurant_id IN (""" + marks + """)
                                 ORDER BY o.sort, o.id""", rids).fetchall():
            opts_of.setdefault(o["group_id"], []).append(o)
    try:
        have = set(os.listdir(UPLOAD_DIR))
    except OSError:
        have = set()

    def pic(it):
        img = (it["image"] or "").strip()
        if img and (img.startswith("http") or os.path.basename(img) in have):
            return media_url(img)
        try:
            src = (it["image_src"] or "").strip()
        except (IndexError, KeyError):
            src = ""
        return src if src.startswith("http") else ""

    for it in items:
        groups = [{"id": g["id"], "name": g["name"], "min": g["min_select"],
                   "max": g["max_select"], "each": max(1, int(g["max_each"] or 1)),
                   "options": [{"id": o["id"], "name": o["name"],
                                "delta_cents": o["price_delta_cents"],
                                "delta": money(o["price_delta_cents"])} for o in opts_of.get(g["id"], [])]}
                  for g in groups_of.get(it["id"], [])]
        out.setdefault(it["restaurant_id"], []).append(
            {"id": it["id"], "name": it["name"], "description": it["description"],
             "price_cents": it["price_cents"], "price": money(it["price_cents"]),
             "section": (it["section"] or "").strip(),
             "tab": (it["menu_tab"] or "").strip(),
             "image": pic(it),
             "groups": groups, **avail_fields(it)})
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


def line_parts(it):
    """One order line split for display: the item, then each add-on, side and note on its own row."""
    subs = []
    for p_ in (it.get("options") or []):
        if not isinstance(p_, dict):
            continue
        grp = str(p_.get("group") or "").strip()
        if grp == "Instructions":
            grp = "Note"
        grp = grp.rstrip("?").strip()
        txt = (grp + ": " if grp else "") + str(p_.get("name") or "").strip()
        if p_.get("delta_cents"):
            txt += " (+" + money(int(p_["delta_cents"])) + ")"
        if txt.strip():
            subs.append(txt)
    note = str(it.get("note") or "").strip()
    if note and not any(x == "Note: " + note for x in subs):
        subs.append("Note: " + note)
    return {"main": "%d x %s" % (int(it.get("qty", 1) or 1), it.get("name", "item")), "subs": subs}


def new_code():
    issue_key = (payload.get("issue") or "").strip()
    issue_label, issue_note, from_code = "", (payload.get("issue_note") or "").strip(), ""
    src_id = payload.get("from_order_id")
    if src_id:
        src = db().execute("SELECT * FROM orders WHERE id=?", (src_id,)).fetchone()
        if src and reorder_closed(src):
            return jsonify({"ok": False, "error": reorder_closed_msg(src)}), 400
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
        if src and reorder_closed(src):
            return jsonify({"ok": False, "error": reorder_closed_msg(src)}), 400
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

# ---- Order numbers -------------------------------------------------------------------
# Primary number: what the restaurant and driver call the order. Brand letters plus a count
# that starts at 1 for each restaurant (TT1, TT2 at Acre; TT1 at the next place). The FF code
# stays as the secondary number: unique across the whole system, used on tracking links.
PRIMARY_STYLES = {
    "plain":  "Letters then number: TT1, TT2",
    "dash":   "Letters, dash, number: TT-1, TT-2",
    "padded": "Letters then padded number: TT0001",
    "dashpad": "Letters, dash, padded number: TT-0001",
}
SECONDARY_STYLES = {
    "time":     "Prefix + time + 3 random digits: FF135508123 (current)",
    "sequence": "Prefix + running number: FF000001",
    "date":     "Prefix + date + count for the day: FF261006-1",
}


def _ordset(key, default=""):
    try:
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        v = (row["value"] if row is not None else "") or ""
        return v.strip() or default
    except Exception:
        return default


def default_prefix(name):
    n = (name or "").lower().replace(" ", "")
    if "tiger" in n:
        return "TT"
    if "crimson" in n:
        return "CTG"
    if "bulldawg" in n or "bulldog" in n:
        return "BF"
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    return ("".join(w[0] for w in words).upper() or "R")[:5]


def clean_prefix(v):
    return re.sub(r"[^A-Za-z0-9]", "", v or "").upper()[:6]


def brand_prefix(site_id=None, name=None):
    """Letters for one brand's restaurant order numbers. site_id None/0 = the main business."""
    key = "primary_prefix_%d" % int(site_id) if site_id else "primary_prefix_main"
    if name is None:
        if site_id:
            row = db().execute("SELECT name FROM sites WHERE id=?", (site_id,)).fetchone()
            name = row["name"] if row else ""
        else:
            name = _ordset("business_name", "")
    return clean_prefix(_ordset(key, "")) or default_prefix(name)


def primary_on():
    return _ordset("primary_on", "1") != "0"


def format_primary(prefix, n):
    style = _ordset("primary_style", "plain")
    try:
        digits = max(1, min(8, int(_ordset("primary_digits", "4"))))
    except ValueError:
        digits = 4
    num = str(n).zfill(digits) if style in ("padded", "dashpad") else str(n)
    return prefix + ("-" if style in ("dash", "dashpad") else "") + num


def primary_group(rid):
    """Which count an order joins: the restart marks (Settings > Order numbers > Start over)
    plus today's date when numbers start over daily. A new group starts again at 1."""
    day = dt.datetime.now().strftime("%Y-%m-%d") if _ordset("primary_reset", "never") == "daily" else ""
    ep = _ordset("primary_epoch", "0") + "." + _ordset("primary_epoch_r%d" % int(rid or 0), "0")
    return ("" if ep == "0.0" else ep + "|") + day


def reset_numbering(rid=None, secondary=True):
    """Start order numbers over (after test orders). rid = one restaurant only; None = every one."""
    stamp = str(int(time.time()))
    if rid:
        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("primary_epoch_r%d" % int(rid), stamp))
    else:
        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('primary_epoch',?)", (stamp,))
        if secondary:
            row = db().execute("SELECT MAX(id) m FROM orders").fetchone()
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('secondary_base',?)", (str(int(row["m"] or 0)),))
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('secondary_reset_at',?)",
                         (dt.datetime.now().isoformat(timespec="seconds"),))
    db().commit()


def assign_primary(order_id):
    """Give a new order its restaurant order number. Counts per restaurant, optionally starting
    over each day (Settings > Order numbers)."""
    if not primary_on():
        return None
    o = db().execute("SELECT id, restaurant_id, region_id, primary_no FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or (o["primary_no"] or "").strip():
        return o["primary_no"] if o else None
    so = site_of_region(o["region_id"]) if o["region_id"] else None
    if so is None:
        r = db().execute("SELECT region_id FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
        so = site_of_region(r["region_id"]) if r and r["region_id"] else None
    prefix = brand_prefix(so["id"] if so is not None else None, so["name"] if so is not None else None)
    day = primary_group(o["restaurant_id"])
    row = db().execute("""SELECT MAX(primary_seq) m FROM orders WHERE restaurant_id=? AND COALESCE(primary_day,'')=?
                          AND id<>?""", (o["restaurant_id"], day, order_id)).fetchone()
    n = int((row["m"] if row and row["m"] else 0)) + 1
    no = format_primary(prefix, n)
    db().execute("UPDATE orders SET primary_no=?, primary_seq=?, primary_day=? WHERE id=?", (no, n, day, order_id))
    return no


_BACKFILL_AT = [0.0]
def backfill_primary(force=False):
    """Orders placed before restaurant numbers were turned on (or whose number was skipped)
    get one now, oldest first, so every open order shows its TT/CTG/BF number."""
    if not primary_on():
        return 0
    if not force and time.time() - _BACKFILL_AT[0] < 20:
        return 0
    _BACKFILL_AT[0] = time.time()
    since = (dt.datetime.now() - dt.timedelta(days=3)).isoformat(timespec="seconds")
    n = 0
    try:
        rows = db().execute("""SELECT id FROM orders WHERE (primary_no IS NULL OR primary_no='')
                               AND (dispatch_status NOT IN ('delivered','cancelled') OR created_at>=?)
                               AND created_at>=? ORDER BY id LIMIT 200""",
                            (dt.datetime.now().strftime("%Y-%m-%d"), since)).fetchall()
        for r in rows:
            if assign_primary(r["id"]):
                n += 1
        if n:
            db().commit()
    except Exception as e:
        print("backfill restaurant numbers:", e)
    return n


ORDSHOW_CHOICES = {"both": "Restaurant number first, system number small next to it",
                   "primary": "Restaurant number only (TT1)",
                   "secondary": "System number only (FF number)"}

def ordshow(screen):
    """Which order number a screen shows: dispatch, rest (restaurant app) or driver."""
    v = _ordset("ordshow_" + screen, "both")
    return v if v in ORDSHOW_CHOICES else "both"


def ord_label(primary_no, code, screen="dispatch"):
    """Order number as Settings > Order numbers says to show it on that screen:
    'TT12 (FF2213...)', 'TT12' or 'FF2213...'."""
    show, p, c = ordshow(screen), (primary_no or "").strip(), (code or "").strip()
    main = p if (show != "secondary" and p) else c
    sub = c if (show == "both" and p and c and p != c) else ""
    return main + ((" (" + sub + ")") if sub else "")


def make_order_code():
    """Secondary (system) order number, unique across every order. Format from Settings > Order numbers."""
    prefix = clean_prefix(_ordset("secondary_prefix", "FF")) or "FF"
    style = _ordset("secondary_style", "time")
    now = dt.datetime.now()
    for attempt in range(50):
        if style == "sequence":
            try:
                digits = max(3, min(10, int(_ordset("secondary_digits", "6"))))
            except ValueError:
                digits = 6
            row = db().execute("SELECT MAX(id) m FROM orders").fetchone()
            try:
                base = int(_ordset("secondary_base", "0"))
            except ValueError:
                base = 0
            code = prefix + str(max(1, int(row["m"] or 0) - base + 1 + attempt)).zfill(digits)
        elif style == "date":
            base = prefix + now.strftime("%y%m%d") + "-"
            row = db().execute("SELECT COUNT(*) c FROM orders WHERE code LIKE ? AND created_at>=?",
                               (base + "%", _ordset("secondary_reset_at", "0"))).fetchone()
            code = base + str(int(row["c"] or 0) + 1 + attempt)
        else:
            code = prefix + now.strftime("%H%M%S") + str(secrets.randbelow(900) + 100)
        if not db().execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
            return code
    return prefix + now.strftime("%H%M%S") + secrets.token_hex(3).upper()


STATUS_WORDS = {
    ("order", "placed"): "Order placed",
    ("kitchen", "waiting"): "Not sent to kitchen yet",
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


def clock(ts, rid=None):
    """2026-09-29T14:12:06 -> 2:12 PM (in the region's own time, tagged ' ET' when it is not
    the app's zone). Falls back to the raw stamp if it is odd."""
    if not ts:
        return ""
    try:
        d = dt.datetime.fromisoformat(ts.replace(" ", "T"))
    except ValueError:
        return ts
    tag = ""
    if rid:
        d, tag = to_region(d, rid), tz_tag(rid)
    return (d.strftime("%-I:%M %p") if os.name != "nt" else d.strftime("%I:%M %p").lstrip("0")) + tag


def stamp_label(kind, status):
    return STATUS_WORDS.get((kind, status), status.replace("_", " ").capitalize())


def order_uses_app(o):
    """True when the restaurant gets orders on its tablet. Called-in restaurants (dispatch
    phones the order in) never show 'sent to kitchen' anywhere."""
    r = db().execute("SELECT uses_app FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    return bool(r["uses_app"]) if r is not None and "uses_app" in r.keys() else True


def order_method(r):
    """app = restaurant tablet, online = dispatch orders on the store's website,
    phone = dispatch calls the order in."""
    if r is None:
        return "app"
    if "uses_app" in r.keys() and r["uses_app"]:
        return "app"
    m = (r["call_method"] if "call_method" in r.keys() else "") or "phone"
    return "online" if m == "online" else "phone"


METHOD_WORDS = {"app": "Restaurant app", "online": "Called in by dispatch: online",
                "phone": "Called in by dispatch: telephone"}


def called_in_words(o, text):
    if not text or order_uses_app(o):
        return text
    return (text.replace("tap Send to kitchen", "tap Release order")
                .replace("waiting on kitchen", "waiting on dispatch to call it in"))


def order_timeline(oid):
    """Every status this order has been through, with the time it happened."""
    rows = db().execute("""SELECT kind, status, at FROM status_log
                           WHERE order_id=? ORDER BY id""", (oid,)).fetchall()
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    called_in = o is not None and not order_uses_app(o)
    trg = _rv(o, "region_id") if o is not None else None
    out = []
    placed = None
    if called_in and "manual_state" in o.keys() and o["manual_state"] == "placed" and o["manual_at"]:
        placed = {"kind": "manual", "status": "placed", "label": "Order placed with the restaurant",
                  "at": o["manual_at"], "time": clock(o["manual_at"], trg), "day": (o["manual_at"] or "")[:10]}
    for r in rows:
        if called_in and r["kind"] == "kitchen" and r["status"] == "pending":
            continue
        if placed and r["kind"] == "kitchen" and r["status"] == "preparing":
            out.append(placed)   # dispatch placing the call is what starts the cooking timer
            placed = None
        out.append({"kind": r["kind"], "status": r["status"],
                    "label": stamp_label(r["kind"], r["status"]),
                    "at": r["at"], "time": clock(r["at"], trg),
                    "day": (r["at"] or "")[:10]})
    if placed:
        out.append(placed)
    # A status picked early and then picked again (driver and dispatch both tapping it, or a
    # step undone and redone) shows once, at the time it finally stuck.
    last = {}
    for i, e in enumerate(out):
        last[(e["kind"], e["status"])] = i
    return [e for i, e in enumerate(out) if last[(e["kind"], e["status"])] == i]


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


def clean_ref(v, phone=None):
    """Their number: whatever the call-in slip calls this order. The browser's autofill
    sometimes drops the customer's phone (or its last 4 digits) in here, so a value that
    is just the customer's phone number, or the end of it, is thrown away."""
    v = (v or "").strip().lstrip("#").strip()[:32]
    d = "".join(ch for ch in v if ch.isdigit())
    pd = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if d and len(d) == len(v.replace("-", "").replace(" ", "").replace("(", "").replace(")", "").replace("+", "")) \
            and len(pd) >= 7 and len(d) >= 4 and (pd.endswith(d) or d.endswith(pd[-10:])):
        return ""
    return v


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
    try:
        _e = eta_info(o, r, d)
    except Exception:
        _e = {"eta_min": None, "eta_clock": "", "eta_note": ""}
    _so = site_of_region(o["region_id"] if "region_id" in _okeys(o) else None)
    return {
        "id": o["id"], "code": o["code"], "site_name": (_so["name"] if _so is not None else ""),
        "region_id": int(_rv(o, "region_id") or 0), "region_label": region_label(_rv(o, "region_id")),
        "primary_no": (_rv(o, "primary_no") or ""),
        "site_phone": (nice_phone(_so["phone"] or "") if _so is not None and (_so["phone"] or "").strip() else ""),
        "site_logo": site_logo_url(_so),
        "eta_min": _e["eta_min"], "eta_clock": _e["eta_clock"], "eta_note": _e["eta_note"],
        "scheduled_for": o["scheduled_for"], "scheduled_label": when_label(o["scheduled_for"], _rv(o, "region_id")) if o["scheduled_for"] else "",
        "release_label": when_label(o["release_at"], _rv(o, "region_id")) if o["release_at"] else "", "tz": region_tz(_rv(o, "region_id")), "tz_tag": tz_tag(_rv(o, "region_id")).strip(), "ref": (o["ref_code"] or ""), "restaurant": pu["name"], "restaurant_address": pu["address"],
        "restaurant_phone": pu["phone"], "restaurant_tel": "tel:" + digits(pu["phone"]),
        "pickup_listed": pu["listed"],
        "customer_tel": "tel:" + digits(o["customer_phone"] or ""),
        "restaurant_nav": nav_url(pu["address"], pu["lat"], pu["lng"], pu["name"]),
        "restaurant_ll": [pu["lat"], pu["lng"]] if pu["lat"] and pu["lng"] else None,
        "customer_ll": [o["lat"], o["lng"]] if o["lat"] and o["lng"] else None,
        "customer": o["customer_name"], "phone": o["customer_phone"],
        "address": o["address"], "note": o["address_note"],
        "dispatch_note": o["dispatch_note"],
        "customer_nav": customer_nav_url(o["address"], o["lat"], o["lng"]),
        "paged_at": (o["paged_at"] if "paged_at" in o.keys() else "") or "",
        "items": _safe_items(o["items"]),
        "lines": [line_label(x) for x in json.loads(o["items"])],
        "line_parts": [line_parts(x) for x in _safe_items(o["items"]) if isinstance(x, dict)],
        "item_count": sum(int(x.get("qty", 1)) for x in json.loads(o["items"])),
        "timeline": order_timeline(o["id"]),
        "placed_time": clock(o["created_at"], _rv(o, "region_id")),
        "ready_time": clock(o["ready_at"], _rv(o, "region_id")),
        "delivered_time": clock(o["delivered_at"], _rv(o, "region_id")),
        "payment_status": o["payment_status"] or "unpaid",
        "pay_method": o["pay_method"] or "",
        "kitchen_go": int(o["kitchen_go"] if "kitchen_go" in o.keys() and o["kitchen_go"] is not None else 1),
        "can_send_kitchen": ("kitchen_go" in o.keys() and o["kitchen_go"] == 0 and o["kitchen_status"] == "waiting"
                             and o["dispatch_status"] not in ("awaiting_payment", "scheduled", "cancelled", "delivered")
                             and bool(o["address_ok"])),
        "can_unsend_kitchen": (int(o["kitchen_go"] if "kitchen_go" in o.keys() and o["kitchen_go"] is not None else 1) == 1
                               and o["kitchen_status"] == "pending"
                               and o["dispatch_status"] not in ("enroute", "delivered", "cancelled", "awaiting_payment", "scheduled")),
        "paid": (o["payment_status"] or "unpaid") in ("paid", "part_refunded", "refunded"),
        "pay_link": o["pay_link"] or "",
        "refunded": money(o["refunded_cents"] or 0),
        "refunded_cents": int(o["refunded_cents"] or 0),
        "refund_note": o["refund_note"] or "",
        "tip_cents": int(o["tip_cents"] or 0),
        "tip_pct": (int(round(int(o["tip_cents"] or 0) * 100.0 / int(o["subtotal_cents"]))) if int(o["subtotal_cents"] or 0) > 0 else None),
        "tip_sig": o["tip_sig"] or "",
        "tip_signed_at": o["tip_signed_at"] or "",
        "tip_declined": bool(o["tip_declined"]),
        "cash": is_cash(o),
        "house": (_rv(o, "pay_method") or "") == "house_account",
        "house_name": house_name_of(o),
        "multi_group": multi_codes(o),
        "credits": order_credits(o), "credit_cents": int(o["credit_cents"] or 0) if "credit_cents" in _okeys(o) else 0,
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
        "discount_cents": order_discount(o), "discount": money(order_discount(o)) if order_discount(o) else "",
        "discount_note": (o["discount_note"] or "") if "discount_note" in o.keys() else "",
        "gift": money(o["gift_cents"]) if ("gift_cents" in o.keys() and o["gift_cents"]) else "",
        "reward": money(o["reward_cents"]) if ("reward_cents" in o.keys() and o["reward_cents"]) else "",
        "due": money(due_cents(o)), "due_cents": due_cents(o),
        "confirm_waiting": confirm_needed(o), "confirm_call": confirm_call_info(o),
        "rewards_member": bool("customer_id" in o.keys() and o["customer_id"]),
        "drv_pay": drv_pay_info(o),
        "service_cents": int(o["service_cents"] or 0) if "service_cents" in o.keys() else 0,
        "service": money((o["service_cents"] or 0) if "service_cents" in o.keys() else 0),
        "miles": (o["miles"] if o["address_ok"] else None), "kitchen_status": o["kitchen_status"],
        "address_ok": bool(o["address_ok"]),
        "source": o["source"], "source_label": SOURCES.get(o["source"], "Web"),
        "token": (o["token"] or ""),
        "needs_address_approval": not o["address_ok"],
        "dispatch_status": o["dispatch_status"], "hold_reason": ("customer must call to confirm" if (confirm_needed(o) and o["dispatch_status"] in ("held", "queued", "awaiting_payment")) else None) or called_in_words(o, ("tap Send to kitchen" if ("kitchen_go" in o.keys() and o["kitchen_go"] == 0 and o["kitchen_status"] == "waiting" and o["dispatch_status"] in ("held", "queued") and o["address_ok"]) else o["hold_reason"])),
        "issue": o["issue"] or "", "issue_note": o["issue_note"] or "",
        "cloned_from": o["cloned_from"] or "",
        "drop_style": clean_drop_style(_rv(o, "drop_style")),
        "drop_label": DROP_STYLES[clean_drop_style(_rv(o, "drop_style"))],
        "driver": d["name"] if d else None, "driver_id": o["driver_id"], "stack_seq": o["stack_seq"],
        "prep_minutes": o["prep_minutes"], "timer_seconds": eta,
        "placed_by": o["placed_by"], "created_at": o["created_at"],
        "delivered_at": o["delivered_at"],
        "reorder_ok": not reorder_closed(o),
        "queue_position": queue_position(o["id"]),
        "uses_app": bool(r["uses_app"]) if r is not None and "uses_app" in r.keys() else True,
        "order_method": order_method(r),
        "order_url": ((r["order_url"] or "") if r is not None and "order_url" in r.keys() else ""),
        "manual_state": (o["manual_state"] or "") if "manual_state" in o.keys() else "",
        "manual_by": (o["manual_by"] or "") if "manual_by" in o.keys() else "",
        "manual_time": (clock(o["manual_at"], _rv(o, "region_id")) if "manual_at" in o.keys() and o["manual_at"] else ""),
        "pp": pp_info(o),
    }

# ---------------------------------------------------------------- customer site

LOCKED_SITE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Coming soon</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f9;color:#1d2330;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center}
.box{background:#fff;border-radius:16px;padding:40px 28px;max-width:420px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
h1{margin:0 0 10px;font-size:28px}p{color:#5b6475;line-height:1.5;margin:8px 0}.small{font-size:13px;margin-top:22px}</style>
</head><body><div class="box"><h1>Coming soon</h1>
<p>This website isn't open for online orders yet. Please check back soon.</p>
<p class="small">Powered by Fleet Foot Delivery</p></div></body></html>"""


def _locked_site_page(site):
    """The customer website of a locked brand (on its own address or the Railway address)
    shows a Coming soon page. The developer All brands test view and a dispatcher's
    Preview of that brand still show the real site."""
    try:
        if not brands_on() or dev_all_brands_mode():
            return None
        s = site if site is not None else home_brand_site()
        if s is None or not site_locked(s["id"]):
            return None
        if session.get("site_preview") and session.get("dispatcher_id") and \
                str(session.get("site_preview")) == str(s["id"]):
            return None
        return Response(LOCKED_SITE_HTML, status=200, mimetype="text/html")
    except Exception:
        return None


@app.route("/")
def home():
    rs = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    if not dev_all_brands_mode():
        _lk = locked_region_ids()
        rs = [r for r in rs if not (r["region_id"] and r["region_id"] in _lk)]   # locked brands are not live yet
    rs_all = rs
    _site = host_site()
    if _locked_site_page(_site):
        return _locked_site_page(_site)
    # is this web address itself a brand's own address (not just a Preview in this browser)?
    _own_addr = _site is not None and _norm_host(request.host) in site_domains(_site)
    if _site is not None:
        _sreg = site_region_ids(_site["id"])
        rs = [r for r in rs if r["region_id"] and r["region_id"] in _sreg]
    biz_on = business_is_open()
    # region picker: only regions that actually have restaurants
    used = {r["region_id"] for r in rs if r["region_id"]}
    paused = paused_region_ids()
    regions = [{"id": g["id"], "name": g["name"], "paused": g["id"] in paused}
               for g in all_regions() if g["id"] in used]
    # one brand per web address: no brand picker and no "All brands" page,
    # except for a developer with the All brands test switch on
    brands, brand_site = [], None
    if dev_all_brands_mode() and not _own_addr and brands_on():
        reg_site = {g["id"]: (g["site_id"] or 0) for g in db().execute("SELECT id, site_id FROM regions").fetchall()}
        used_all = {r["region_id"] for r in rs_all if r["region_id"]}
        used_sites = {reg_site.get(rid, 0) for rid in used_all}
        for srow in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
            brands.append({"id": srow["id"], "name": srow["name"], "logo": site_logo_url(srow),
                           "empty": srow["id"] not in used_sites, "locked": site_locked(srow["id"])})
        bpick = request.args.get("brand")
        if bpick is not None:
            session["cust_brand"] = bpick if bpick.isdigit() else ""
            session["cust_region"] = ""
        elif request.args.get("region") is not None:
            session["cust_brand"] = ""
        bsel = session.get("cust_brand") or ""
        if bpick is not None and _site is not None:
            _site = None
            rs = rs_all
            used = {r["region_id"] for r in rs if r["region_id"]}
            regions = [{"id": g["id"], "name": g["name"], "paused": g["id"] in paused}
                       for g in all_regions() if g["id"] in used]
        if bsel.isdigit() and int(bsel) in {b["id"] for b in brands}:
            brand_site = site_by_id(int(bsel))
            _breg = site_region_ids(brand_site["id"])
            rs = [r for r in rs if r["region_id"] and r["region_id"] in _breg]
            regions = [g for g in regions if g["id"] in _breg]
        elif bsel:
            session["cust_brand"] = ""
        elif _site is not None:
            brand_site = _site
    if not regions_on():
        regions = []   # regions are off: one area, no "Where are you?" buttons
    pick = request.args.get("region")
    if pick is not None:
        session["cust_region"] = pick if pick.isdigit() else ""
    sel = session.get("cust_region") or ""
    sel_id = int(sel) if sel.isdigit() and int(sel) in {g["id"] for g in regions} else 0
    if sel_id:
        # restaurants with no region show in every area
        rs = [r for r in rs if not r["region_id"] or r["region_id"] == sel_id]
    cards = [{"r": r, "open": biz_on and business_in_hours(rid=r["region_id"]) and is_open(r),
              "hours": hours_label(r), "rules": delivery_rules(r)} for r in rs]
    sel_name = next((g["name"] for g in regions if g["id"] == sel_id), "")
    if sel_id:
        biz = biz_on and business_in_hours(rid=sel_id)
    elif regions:
        # open when any area shown here is open; a region's closed date closes it even if the business hours say open
        biz = biz_on and any(business_in_hours(rid=g["id"]) for g in regions)
    else:
        biz = biz_on and business_in_hours()
    _hr = sel_id or (regions[0]["id"] if len(regions) == 1 else 0)
    _hr_name = sel_name or (regions[0]["name"] if len(regions) == 1 else "")
    if brand_site is not None and not sel_id:
        g._site = brand_site   # show the picked brand's logo, name, phone and design
    far_name = ""
    if request.args.get("far"):
        _fr = db().execute("SELECT name FROM restaurants WHERE slug=?", (request.args.get("far"),)).fetchone()
        far_name = _fr["name"] if _fr else ""
    return render_template("index.html", cards=cards, biz_open=biz, regions=regions, far_name=far_name,
                           brands=brands, sel_brand=brand_site["id"] if brand_site is not None else 0,
                           dev_all=bool(brands),
                           sel_region=sel_id, sel_region_name=sel_name, hours_region_name=_hr_name,
                           # several areas and none picked: the footer lists every area's hours instead
                           hours_text=(business_hours_label(_hr) if _hr else ("" if regions else business_hours_label())),
                           closed_text=closed_dates_label(_hr) if _hr else "",
                           closed_today=[(("" if _hr else g["name"] + ": ") + region_closed_today(g["id"]))
                                         for g in ([{"id": _hr, "name": _hr_name}] if _hr else regions)
                                         if region_closed_today(g["id"])],
                           any_on=any_rest_on(), any_open=biz and any_rest_open())

@app.route("/r/<slug>")
def menu(slug):
    r = db().execute("SELECT * FROM restaurants WHERE slug=?", (slug,)).fetchone()
    if not r:
        return redirect(url_for("home"))
    if r["slug"] == "oneoff" and not any_rest_on():
        return redirect(url_for("home"))
    if restaurant_locked(r):
        return redirect(url_for("home"))   # its brand is locked (not live yet)
    _site = host_site()
    if _site is not None and r["slug"] != "oneoff" and (r["region_id"] or 0) not in site_region_ids(_site["id"]):
        _own = site_of_region(r["region_id"])
        _doms = site_domains(_own) if _own is not None else []
        if _doms and _norm_host(request.host) not in _doms:
            return redirect("https://%s/r/%s" % (_doms[0], r["slug"]))
        return redirect(url_for("home"))
    if _site is None and r["slug"] != "oneoff":
        _rs = site_of_region(r["region_id"])
        if _rs is not None:
            g._site_forced = _rs   # restaurant's brand: logo, name and phone on its menu page
    hg = session.get("home_geo")
    if (r["slug"] != "oneoff" and hg and r["lat"] is not None and not session.get("dispatcher_id")
            and request.args.get("anyway") != "1"):
        try:
            far = round(haversine_miles(r["lat"], r["lng"], float(hg[0]), float(hg[1])) * ROAD_FACTOR, 1)
            mx = delivery_rules(r)["max_miles"] or 60
            if far > mx:
                return redirect("/?far=%s#restaurants" % r["slug"])
        except (TypeError, ValueError, IndexError):
            pass
    items = db().execute("SELECT * FROM menu_items WHERE restaurant_id=? AND active=1", (r["id"],)).fetchall()
    biz = business_is_open() and business_in_hours(rid=r["region_id"])
    open_now = biz and (any_rest_open() if r["slug"] == "oneoff" else is_open(r))
    return render_template("menu.html", r=r, rules=delivery_rules(r), service_bp=service_bp_for(r), items=items, open=open_now, biz_open=biz,
                           hours=hours_label(r), custom=(r["slug"] == "oneoff"),
                           base_fee=money(fee_rules(r["region_id"])["base_fee"]),
                           base_miles="%g" % float(fee_rules(r["region_id"])["base_miles"] or 0),
                           per_mile=money(fee_rules(r["region_id"])["per_mile"]))

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
        # street not found: fall back to the ZIP code and the delivery radius
        zc = zip_check(r, data.get("address", ""))
        if zc["found"] and zc["within"]:
            return jsonify({"ok": False, "zip_ok": True, "zip": zc["zip"], "miles": zc["miles"],
                            "fee_cents": zc["fee"], "fee": money(zc["fee"]),
                            "error": "We couldn't find that exact street, but ZIP %s is in %s's delivery area (about %.1f mi)."
                                     % (zc["zip"], r["name"], zc["miles"])})
        if zc["found"]:
            return jsonify({"ok": False, "out_of_range": True, "zip": zc["zip"], "error":
                            "ZIP %s is about %.1f mi from %s. %s delivers up to %g mi."
                            % (zc["zip"], zc["miles"], r["name"], r["name"], zc["max_miles"])})
        if zc["zip"]:
            return jsonify({"ok": False, "error": "We could not verify that address or find ZIP %s. "
                                                  "Check the ZIP code or call dispatch." % zc["zip"]})
        return jsonify({"ok": False, "need_zip": True,
                        "error": "We couldn't find that street. Add your 5-digit ZIP code and you can still "
                                 "place the order; dispatch will confirm the exact address."})
    miles, fee = quote(r, g1["lat"], g1["lng"])
    _typed = data.get("address", "") or ""
    if miles > 60 and re.match(r"\s*\d+\s+\S", _typed) and re.search(r"\b\d{5}\b", _typed):
        return jsonify({"ok": False, "out_of_range": True, "error":
                        "That address is %d mi from %s. %s doesn't deliver that far."
                        % (int(miles), r["name"], r["name"])})
    if miles > 60:
        return jsonify({"ok": False, "out_of_range": True, "error":
                        "That matched a place %s mi from %s. Add the street number, city, state and ZIP."
                        % (int(miles), r["name"])})
    rules = delivery_rules(r)
    warning = ""
    if rules["max_miles"] and miles > rules["max_miles"]:
        txt = ("That address is %.1f mi from %s. %s delivers up to %g mi."
               % (miles, r["name"], r["name"], rules["max_miles"]))
        if dispatcher_required():
            warning = txt + " Dispatch can still place it."
        else:
            return jsonify({"ok": False, "out_of_range": True, "error": txt})
    return jsonify({"ok": True, "formatted": g1["formatted"], "lat": g1["lat"], "lng": g1["lng"],
                    "warning": warning, "min_order_cents": rules["min_cents"],
                    "min_order": money(rules["min_cents"]) if rules["min_cents"] else "",
                    "max_miles": rules["max_miles"],
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
    gsrc = "google" if GOOGLE_KEY else "osm"
    for row in db().execute("""SELECT formatted, lat, lng FROM geocache WHERE ok=1
                               AND (q LIKE ? OR lower(formatted) LIKE ?)""" +
                            (" AND src='google'" if GOOGLE_KEY else "") + " LIMIT 6",
                            ("%" + q + "%", "%" + q + "%")).fetchall():
        if row["formatted"] and row["formatted"] not in seen:
            seen.add(row["formatted"])
            out.append({"formatted": row["formatted"], "lat": row["lat"], "lng": row["lng"]})
    try:
        if len(out) >= 6 or _geo_down():
            raise LookupError("enough saved suggestions, or the lookup service isn't answering")
        if GOOGLE_KEY:
            url = ("https://maps.googleapis.com/maps/api/geocode/json?address="
                   + urllib.parse.quote(q) + "&components=country:US&key=" + GOOGLE_KEY)
            data = json.loads(urllib.request.urlopen(url, timeout=3).read())
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
            for h in json.loads(urllib.request.urlopen(req, timeout=3).read()):
                if h["display_name"] not in seen:
                    seen.add(h["display_name"])
                    out.append({"formatted": h["display_name"],
                                "lat": float(h["lat"]), "lng": float(h["lon"])})
    except LookupError:
        pass
    except Exception:
        _geo_trouble()
    for c in out:                      # so picking one is an instant, exact match later
        db().execute("INSERT OR REPLACE INTO geocache(q,formatted,lat,lng,ok,src) VALUES(?,?,?,?,1,?)",
                     (" ".join(c["formatted"].lower().split()), c["formatted"], c["lat"], c["lng"], gsrc))
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
        auto_msg("drv_cancelled", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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
    if (_rv(o, "pay_method") or "") == "house_account":
        return 0      # billed to the house account for whatever the order comes to
    kept = int(o["paid_cents"] or 0) - int(o["refunded_cents"] or 0)
    return int(o["total_cents"] or 0) - credits_cents(o) - kept

def mark_paid(o, method="recorded", ref="", cents=None):
    """Payment landed: record it and let the order into the queue."""
    cents = due_cents(o) if cents is None else int(cents)
    released = o["dispatch_status"] == "awaiting_payment"
    # Payment never sends the order to the kitchen. Dispatch taps Send to kitchen.
    if o["kitchen_status"] not in ("preparing", "ready") and o["dispatch_status"] not in ("delivered", "cancelled"):
        db().execute("UPDATE orders SET kitchen_go=0 WHERE id=? AND kitchen_status NOT IN ('preparing','ready')", (o["id"],))
    if o["dispatch_status"] == "scheduled" and (o["sched_dispatch"] or "") == "awaiting_payment":
        db().execute("""UPDATE orders SET sched_dispatch='held', sched_kitchen=?, sched_hold=? WHERE id=?""",
                     ("pending" if o["address_ok"] else "waiting",
                      "waiting on kitchen" if o["address_ok"] else "address needs dispatch approval", o["id"]))
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
# PayPal keys live in Dispatch settings (owner only): a sandbox pair, a live pair and a Sandbox/Live switch.
# Railway variables are not used for PayPal.
# At checkout the money is only held (authorized). After delivery the site charges the final total,
# so a tip added after delivery is included. PayPal lets that charge run up to 15% or $75 over the
# hold, whichever is less; anything past that shows on the tracking page as a small Pay the rest button.
PP_MODES = ("sandbox", "live")
PP_KEYS = ("pp_mode", "pp_sandbox_client", "pp_sandbox_secret", "pp_live_client", "pp_live_secret")
PP_SOURCES = {"venmo": "Venmo", "paypal": "PayPal", "card": "card"}   # venmo kept only for old orders
_pp_toks = {}
# The PayPal keys in use right now: a brand's id, 0 for the main keys, None = the page's brand.
_PP_ACCT = contextvars.ContextVar("pp_acct", default=None)
_pp_lock = threading.Lock()
_pp_last_sweep = [0.0]

def _pp_settings(keys=PP_KEYS):
    """The saved PayPal keys. Works inside a page request or a background job."""
    keys = tuple(keys)
    q = "SELECT key, value FROM settings WHERE key IN (%s)" % ",".join("?" * len(keys))
    try:
        rows = db().execute(q, keys).fetchall()
    except RuntimeError:          # no request running
        con = dbx.connect(DB_PATH)
        try:
            rows = con.execute(q, keys).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    return {r[0]: (r[1] or "").strip() for r in rows}

# Each brand can have its own PayPal keys (its own PayPal account), saved as pp_b<brand id>_<mode>_client
# and _secret. A brand without its own keys uses the main keys. One Sandbox/Live switch covers all of them.
def _pp_bkeys(sid):
    return ["pp_b%d_%s_%s" % (int(sid), m, f) for m in PP_MODES for f in ("client", "secret")]

def pp_brand_keys(sid):
    """A brand's own saved keys as {"sandbox_client": .., "sandbox_secret": .., "live_client": .., ...}."""
    pre = "pp_b%d_" % int(sid)
    return {k[len(pre):]: v for k, v in _pp_settings(_pp_bkeys(sid)).items()}

def pp_brand_ready(sid, mode=None):
    """The brand has its own client ID and secret for the mode in use."""
    try:
        sid = int(sid or 0)
    except (TypeError, ValueError):
        return False
    if not sid:
        return False
    if mode is None:
        mode = _pp_settings().get("pp_mode")
        mode = mode if mode in PP_MODES else "sandbox"
    b = pp_brand_keys(sid)
    return bool(b.get(mode + "_client") and b.get(mode + "_secret"))

def _pp_page_acct():
    """On a brand's website, that brand's own PayPal keys when it has them; else (and on staff pages
    and background jobs) the main keys."""
    try:
        if has_request_context():
            s = current_site()
            return int(s["id"]) if (s and pp_brand_ready(s["id"])) else 0
    except Exception:
        pass
    return 0

def pp_conf(acct=None):
    v = _pp_settings()
    mode = v.get("pp_mode") if v.get("pp_mode") in PP_MODES else "sandbox"
    cid, sec, used = v.get("pp_%s_client" % mode, ""), v.get("pp_%s_secret" % mode, ""), 0
    if acct is None:
        acct = _PP_ACCT.get()
    if acct is None:
        acct = _pp_page_acct()
    try:
        acct = int(acct or 0)
    except (TypeError, ValueError):
        acct = 0
    if acct:
        b = pp_brand_keys(acct)
        if b.get(mode + "_client") and b.get(mode + "_secret"):
            cid, sec, used = b[mode + "_client"], b[mode + "_secret"], acct
    return {"mode": mode, "client": cid, "secret": sec, "acct": used,
            "base": "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com",
            "all": v}

def pp_client_id(acct=None):
    return pp_conf(acct)["client"]

def pp_enabled(acct=None):
    c = pp_conf(acct)
    return bool(c["client"] and c["secret"])

def pp_any_enabled():
    """Main keys or any brand's keys are set, so something may need charging."""
    if pp_enabled(0):
        return True
    try:
        ids = [r[0] for r in db().execute("SELECT id FROM sites").fetchall()]
    except Exception:
        ids = []
    return any(pp_brand_ready(i) for i in ids)

def pp_region_acct(rid):
    """Which keys a new payment uses for a restaurant in this region: its brand's own, else the main ones."""
    try:
        s = site_of_region(rid)
    except Exception:
        s = None
    return int(s["id"]) if (s and pp_brand_ready(s["id"])) else 0

def pp_new_acct(o):
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    return pp_region_acct(_rv(r, "region_id") if r else None)

def pp_order_acct(o):
    """The keys this order's PayPal money went through (holds, charges, refunds must use the same ones)."""
    if "pp_acct" in o.keys() and o["pp_acct"] is not None and (o["pp_state"] or o["pp_order_id"]):
        return int(o["pp_acct"])
    return pp_new_acct(o)

@contextlib.contextmanager
def pp_for(acct):
    tok = _PP_ACCT.set(int(acct or 0))
    try:
        yield
    finally:
        _PP_ACCT.reset(tok)

def pp_token(conf=None):
    c = conf or pp_conf()
    who = c["mode"] + ":" + c["client"] + ":" + hashlib.sha256(c["secret"].encode()).hexdigest()
    with _pp_lock:
        t = _pp_toks.get(who)
        if t and time.time() < t[1] - 60:
            return t[0]
        auth = base64.b64encode((c["client"] + ":" + c["secret"]).encode()).decode()
        req = urllib.request.Request(c["base"] + "/v1/oauth2/token", data=b"grant_type=client_credentials",
                                     headers={"Authorization": "Basic " + auth,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read())
        _pp_toks[who] = (j["access_token"], time.time() + int(j.get("expires_in", 3000)))
        return j["access_token"]

def pp_settings_view():
    """What the owner sees in Settings. Secrets never leave the server; only the last 4 characters."""
    c = pp_conf(0); v = c["all"]; out = {"mode": c["mode"], "on": bool(c["client"] and c["secret"])}
    for m in PP_MODES:
        sec = v.get("pp_%s_secret" % m, "")
        out[m] = {"client": v.get("pp_%s_client" % m, ""), "has_secret": bool(sec), "tail": sec[-4:] if len(sec) >= 8 else ""}
    out["brands"] = []
    try:
        sites = db().execute("SELECT id, name FROM sites ORDER BY name").fetchall() if brands_on() else []
    except Exception:
        sites = []
    for st in sites:
        b = pp_brand_keys(st["id"]); row = {"id": st["id"], "name": st["name"], "ready": pp_brand_ready(st["id"], c["mode"])}
        for m in PP_MODES:
            sec = b.get(m + "_secret", "")
            row[m] = {"client": b.get(m + "_client", ""), "has_secret": bool(sec), "tail": sec[-4:] if len(sec) >= 8 else ""}
        row["own"] = any(row[m]["client"] or row[m]["has_secret"] for m in PP_MODES)
        out["brands"].append(row)
    return out

def pp_test_keys(mode, cid, sec):
    """Ask PayPal whether a key pair works. Returns (ok, message)."""
    base = "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"
    try:
        pp_token({"mode": mode, "client": cid, "secret": sec, "base": base})
        return True, "PayPal accepted the %s keys." % mode
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "PayPal turned down the %s keys. Check the client ID and secret, and that they are %s keys." % (mode, mode)
        return False, "PayPal answered with an error (%s) when checking the %s keys." % (e.code, mode)
    except Exception:
        return False, "Could not reach PayPal to check the %s keys. They are saved; try again later." % mode

# ---------------------------------------------------------------- Branch (driver pay)
# Branch keys live in Dispatch settings (owner only): a sandbox org ID + API key, a live
# org ID + API key and a Sandbox/Live switch. Drivers set to Branch get each trip's pay
# as a Branch disbursement to their Branch worker ID.
BR_MODES = ("sandbox", "live")

def _br_rows():
    try:
        return db().execute("SELECT * FROM branch_accounts ORDER BY id").fetchall()
    except Exception:
        try:
            con = dbx.connect(DB_PATH)
            rows = con.execute("SELECT * FROM branch_accounts ORDER BY id").fetchall(); con.close()
            return rows
        except Exception:
            return []

def br_conf(acct):
    """Connection details for one Branch account row (or None)."""
    if not acct:
        return None
    mode = acct["mode"] if acct["mode"] in BR_MODES else "sandbox"
    return {"id": acct["id"], "name": acct["name"], "mode": mode, "org": (acct["org_id"] or "").strip(),
            "key": (acct["api_key"] or "").strip(),
            "base": "https://api.branchapp.com" if mode == "live" else "https://sandbox.branchapp.com"}

def _br_ready(a):
    return bool(a and a["active"] != 0 and (a["org_id"] or "").strip() and (a["api_key"] or "").strip())

def br_enabled():
    return any(_br_ready(a) for a in _br_rows())

def br_pick(d=None, o=None):
    """Which Branch account pays this driver for this order: the driver's own pick, else the
    account linked to the order's brand, else an account for all brands, else the only one."""
    ready = [a for a in _br_rows() if _br_ready(a)]
    if not ready:
        return None
    if d is not None and "branch_account_id" in d.keys() and d["branch_account_id"]:
        for a in ready:
            if a["id"] == d["branch_account_id"]:
                return a
        return None                      # the driver's account is off or has no keys: don't pay from another
    sid = None
    if o is not None:
        try:
            r = db().execute("SELECT region_id FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
            st = site_of_region(r["region_id"]) if r else None
            sid = st["id"] if st else None
        except Exception:
            sid = None
    if sid:
        for a in ready:
            if a["site_id"] == sid:
                return a
    for a in ready:
        if not a["site_id"]:
            return a
    return ready[0] if len(ready) == 1 else None

def br_settings_view():
    out = []
    for a in _br_rows():
        k = (a["api_key"] or "")
        out.append({"id": a["id"], "name": a["name"], "mode": a["mode"] or "sandbox", "org": a["org_id"] or "",
                    "site_id": a["site_id"] or 0, "active": 0 if a["active"] == 0 else 1,
                    "has_key": bool(k), "tail": k[-4:] if len(k) >= 8 else "", "ready": _br_ready(a)})
    return out

def br_api(method, path, body=None, conf=None):
    """Returns (http status, json). Never raises."""
    c = conf
    if not c:
        return 0, {"message": "no Branch account"}
    try:
        h = {"apikey": c["key"], "Content-Type": "application/json", "Accept": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(c["base"] + path, data=data, headers=h, method=method)
        resp = urllib.request.urlopen(req, timeout=25)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"message": str(e)}

def br_test_keys(mode, org, key, name=""):
    """Ask Branch whether an org ID + API key work (Get Organization)."""
    base = "https://api.branchapp.com" if mode == "live" else "https://sandbox.branchapp.com"
    st, j = br_api("GET", "/v1/organizations/%s" % urllib.parse.quote(str(org)), conf={"key": key, "base": base})
    mode = (name + " " + mode).strip()
    if st == 200:
        return True, "Branch accepted the %s keys." % mode
    if st in (401, 403):
        return False, "Branch turned down the %s keys. Check the organization ID and API key, and that they are %s keys." % (mode, mode)
    if st == 404:
        return False, "Branch didn't find organization %s with the %s key." % (org, mode)
    if st == 0:
        return False, "Could not reach Branch to check the %s keys. They are saved; try again later." % mode
    return False, "Branch answered with an error (%s) when checking the %s keys." % (st, mode)

BR_STATUS = {"COMPLETED": "SUCCESS", "SCHEDULED": "PENDING", "PENDING": "PENDING", "PROCESSING": "PENDING",
             "CREATED": "PENDING", "FAILED": "FAILED", "CANCELED": "CANCELED", "CANCELLED": "CANCELED",
             "SKIPPED": "FAILED", "REVERSED": "REVERSED"}

def br_worker_ids(d):
    """The driver's Branch worker IDs per Branch account ({account id: worker ID})."""
    try:
        raw = json.loads((d["payout_branch_ids"] if d is not None and "payout_branch_ids" in d.keys() else "") or "{}")
        return {int(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
    except Exception:
        return {}

def br_worker_for(d, acct_id):
    """Worker ID for this driver at one Branch account: its own one, else the driver's main worker ID."""
    if d is None:
        return ""
    return br_worker_ids(d).get(int(acct_id or 0)) or ((d["payout_branch_id"] if "payout_branch_id" in d.keys() else "") or "").strip()

def _branch_send(r, brand, what):
    """Create (or safely look up again, same external_id) one Branch disbursement. Returns (status, batch_id, error)."""
    acct = None
    if r["br_account_id"]:
        acct = db().execute("SELECT * FROM branch_accounts WHERE id=?", (r["br_account_id"],)).fetchone()
    else:
        d = db().execute("SELECT * FROM drivers WHERE id=?", (r["driver_id"],)).fetchone()
        o = db().execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone() if r["order_id"] else None
        acct = br_pick(d, o)
        if acct is not None:
            wid = br_worker_for(d, acct["id"]) or r["receiver"]
            db().execute("UPDATE driver_payouts SET br_account_id=?, receiver=? WHERE id=?", (acct["id"], wid, r["id"]))
            db().commit()
            r = db().execute("SELECT * FROM driver_payouts WHERE id=?", (r["id"],)).fetchone()
    if not _br_ready(acct):
        return "ERROR", None, "No Branch account with keys is set up for this driver yet."
    c = br_conf(acct)
    body = {"amount": int(r["cents"]), "external_id": r["sender_id"][:64], "type": "DELIVERY",
            "description": what[:256], "display_header_label": what[:32], "display_sub_label": brand[:32],
            "retry": False}
    st, j = br_api("POST", "/v2/organizations/%s/workers/%s/disbursements"
                   % (urllib.parse.quote(str(c["org"])), urllib.parse.quote(str(r["receiver"]), safe="")), body, conf=c)
    j = j if isinstance(j, dict) else {}
    bid = str(j.get("id") or j.get("disbursement_id") or "") or None
    raw = str(j.get("status") or "").upper()
    reason = str(j.get("reason_code") or j.get("message") or j.get("error") or "")
    if st in (200, 201, 202):
        stat = BR_STATUS.get(raw, "SUCCESS" if st == 201 and not raw else "PENDING")
        return stat, bid, ("Branch: " + reason)[:300] if stat in PAYOUT_BAD and reason else None
    if st == 0 or st >= 500:
        return "UNKNOWN", bid, "Could not hear back from Branch. Tap Check before paying again."
    if st == 429:
        return "UNKNOWN", bid, "Branch is busy or the daily pay limit was reached. Tap Check later."
    if st in (401, 403):
        return "ERROR", bid, "Branch turned down the API key. Check the Branch keys in Settings."
    if st == 404:
        return "ERROR", bid, "Branch doesn't know worker ID %s. Check the driver's Branch worker ID." % r["receiver"]
    return "ERROR", bid, ("Branch did not accept the payment" + (": " + reason if reason else "."))[:300]

def pay_rail_on(wallet, d=None, o=None):
    """Is the money rail for this payout wallet set up? BRANCH needs a Branch account for this
    driver/order, PAYPAL/VENMO need PayPal keys."""
    w = (wallet or "").upper()
    if w == "BRANCH":
        return br_pick(d, o) is not None if (d is not None or o is not None) else br_enabled()
    if w in ("PAYPAL", "VENMO"):
        return pp_enabled(pp_payout_acct(o)) if o is not None else pp_any_enabled()
    return True

def pp_payout_acct(o):
    """PayPal keys a driver's trip pay comes from: the keys that took the customer's payment for
    that order (its brand's own PayPal account when it has one), else the brand's keys, else the main keys."""
    if o is None:
        return 0
    try:
        return pp_order_acct(o)
    except Exception:
        return 0

def payout_brand_name(o):
    """Name the driver sees on the payment: the order's brand, else the business name."""
    try:
        r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone() if o is not None else None
        st = site_of_region(_rv(r, "region_id")) if r else None
        if st and (st["name"] or "").strip():
            return st["name"].strip()
    except Exception:
        pass
    return setting("business_name", str) or "Fleet Foot Delivery"

def pay_rail_name(wallet):
    return "Branch" if (wallet or "").upper() == "BRANCH" else "PayPal"

_pp_retry = threading.local()

def pp_api(method, path, body=None, request_id=None):
    """Returns (http status, json). Never raises."""
    try:
        c = pp_conf()
        h = {"Authorization": "Bearer " + pp_token(c), "Content-Type": "application/json",
             "Prefer": "return=representation"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(c["base"] + path, data=data, headers=h, method=method)
        resp = urllib.request.urlopen(req, timeout=20)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() or b"{}"
            j = json.loads(raw)
        except Exception:
            j = {"message": "PayPal answered with error %s." % e.code}
        if e.code == 401 and not getattr(_pp_retry, "on", False):
            # a stale or revoked sign-in token: get a fresh one and try once more
            with _pp_lock:
                _pp_toks.clear()
            _pp_retry.on = True
            try:
                return pp_api(method, path, body, request_id)
            finally:
                _pp_retry.on = False
        print("PayPal %s %s -> %s %s" % (method, path, e.code, json.dumps(j)[:600]))
        return e.code, j
    except Exception as e:
        print("PayPal %s %s failed: %s" % (method, path, e))
        return 0, {"message": "Could not reach PayPal: " + str(e)[:200]}

def pp_money(cents):
    return {"currency_code": "USD", "value": "%.2f" % (int(cents) / 100.0)}

def pp_err(j, fallback="PayPal did not accept that."):
    j = j if isinstance(j, dict) else {}
    d = (j.get("details") or [{}])[0] or {}
    issue = d.get("issue") or j.get("name") or j.get("error") or ""
    if issue == "INSTRUMENT_DECLINED":
        return "That card or account was declined. Try another way to pay."
    if j.get("error") == "invalid_client" or issue in ("AUTHENTICATION_FAILURE", "invalid_token"):
        return "PayPal turned down the API keys (" + (j.get("error_description") or issue) + "). Check Sandbox/Live in Settings matches the keys."
    if issue == "NOT_AUTHORIZED" or issue == "PERMISSION_DENIED":
        return "PayPal says this account isn't allowed to do that yet (" + (d.get("description") or j.get("message") or issue) + ")."
    msg = d.get("description") or j.get("message") or j.get("error_description") or ""
    if msg and issue and issue not in msg:
        msg += " (" + issue + ")"
    if not msg:
        return fallback + (" PayPal said: " + issue if issue else "")
    return msg

PP_DECLINES = {
    "5120": "Declined: not enough money on the card.", "5400": "Declined: the card is expired.",
    "5180": "Declined: the card number isn't valid.", "1330": "Declined: the card isn't valid.",
    "0500": "Declined by the bank (do not honor).", "0580": "Declined: the card isn't valid.",
    "5110": "Declined: the security code (CVV) doesn't match.", "00N7": "Declined: the security code (CVV) doesn't match.",
    "0880": "Declined: the security code (CVV) doesn't match.", "5100": "Declined by the bank.",
    "9500": "Declined: the bank flagged it as possible fraud.", "5650": "Declined: the bank wants the card holder to verify it.",
    "1000": "Declined: partial approval only. Use another card.", "5200": "Declined: the card is restricted.",
    "0800": "Declined: the bank couldn't process it. Try again or use another card.",
    "INSUFFICIENT_FUNDS": "Declined: not enough money on the card.", "CARD_EXPIRED": "Declined: the card is expired.",
    "INSTRUMENT_DECLINED": "Declined: the card or account was declined. Use another card.",
    "TRANSACTION_REFUSED": "Declined: PayPal refused the payment. Use another card.",
    "CARD_CLOSED": "Declined: the card is closed.", "INVALID_CVV": "Declined: the security code (CVV) doesn't match.",
}

def pp_decline_msg(j, auth=None):
    """The bank's or PayPal's reason in plain words when a card or account is declined, else ''."""
    try:
        a = auth or {}
        pr = a.get("processor_response") or {}
        code = str(pr.get("response_code") or "").upper()
        if a.get("status") in ("DECLINED", "DENIED", "VOIDED", "EXPIRED") or (code and code not in ("0000", "00", "0")):
            return PP_DECLINES.get(code) or ("Declined by the bank" + (" (code " + code + ")" if code else "") + ". Use another card.")
        d = ((j or {}).get("details") or [{}])[0] if isinstance(j, dict) else {}
        issue = str(d.get("issue") or "").upper()
        if issue in PP_DECLINES:
            return PP_DECLINES[issue]
    except Exception:
        pass
    return ""

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

def _pp_settle(o, why="after delivery"):
    """Charge the held payment for the order's current total (late tip included)."""
    if (o["pp_state"] or "") != "authorized" or not o["pp_auth_id"]:
        return {"ok": False, "error": "No PayPal hold on this order."}
    auth = int(o["pp_auth_cents"] or 0)
    due = int(o["total_cents"] or 0) - credits_cents(o) - int(o["refunded_cents"] or 0)
    if due <= 0:
        return _pp_void(o, "nothing owed")
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

def _pp_void(o, why="cancelled"):
    if (o["pp_state"] or "") != "authorized" or not o["pp_auth_id"]:
        return {"ok": False, "error": "No PayPal hold on this order."}
    st, j = pp_api("POST", "/v2/payments/authorizations/" + o["pp_auth_id"] + "/void")
    issue = str((((j or {}).get("details") or [{}])[0] or {}).get("issue") or "").upper() if isinstance(j, dict) else ""
    gone = issue in ("PREVIOUSLY_VOIDED", "AUTHORIZATION_VOIDED", "AUTHORIZATION_EXPIRED", "AUTHORIZATION_ALREADY_VOIDED")
    if st not in (200, 204) and not gone:
        # Ask PayPal what the hold looks like now: if it is already voided, expired or denied, nothing is held.
        try:
            s2, a2 = pp_api("GET", "/v2/payments/authorizations/" + o["pp_auth_id"])
            if s2 == 200 and str((a2 or {}).get("status") or "").upper() in ("VOIDED", "EXPIRED", "DENIED"):
                gone = True
        except Exception:
            pass
    if gone:
        # PayPal already let go of this hold (released earlier or it expired): just catch our records up.
        why = why + ", PayPal had already released it"
    if st in (200, 204) or gone:
        db().execute("UPDATE orders SET pp_state='voided', paid_cents=0, pp_error=NULL WHERE id=?", (o["id"],))
        if o["dispatch_status"] not in ("delivered", "cancelled"):
            # Order is still open: it is unpaid again, so a new card can go on.
            db().execute("""UPDATE orders SET pp_state=NULL, pp_auth_id=NULL, pp_auth_cents=NULL, pp_order_id=NULL,
                            pp_source=NULL, pp_acct=NULL, payment_status='unpaid', paid_at=NULL, pay_method=NULL, pay_ref=NULL
                            WHERE id=?""", (o["id"],))
            if o["dispatch_status"] == "scheduled":
                # a future order must not go to the kitchen without a card on it
                db().execute("""UPDATE orders SET sched_dispatch='awaiting_payment', sched_kitchen='waiting',
                                sched_hold='waiting on card' WHERE id=?""", (o["id"],))
        db().commit()
        log("payment", o["code"] + " PayPal hold released (" + why + ")")
        return {"ok": True, "voided": True}
    msg = pp_err(j, "PayPal would not release the hold.")
    db().execute("UPDATE orders SET pp_error=? WHERE id=?", (msg[:300], o["id"]))
    db().commit()
    return {"ok": False, "error": msg}

@app.get("/api/paypal/client")
def api_pp_client():
    """The PayPal client ID for an order (its brand's keys) or, with no order, for this page."""
    acct = None
    code = (request.args.get("code") or "").strip()
    if code:
        o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
        if o:
            acct = pp_order_acct(o)
    c = pp_conf(acct)
    return jsonify({"ok": True, "enabled": bool(c["client"] and c["secret"]), "client_id": c["client"], "env": c["mode"]})

@app.post("/api/paypal/replace-card")
def api_pp_replace_card():
    """Dispatch swaps the card on a current or future order: release the old hold, then a new card goes on."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    o = db().execute("SELECT * FROM orders WHERE id=?", (request.get_json(force=True).get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    if o["dispatch_status"] in ("delivered", "cancelled"):
        return jsonify({"ok": False, "error": "This order is closed, so the card can't be changed."}), 400
    st = o["pp_state"] or ""
    if st == "captured":
        return jsonify({"ok": False, "error": "This card was already charged. Refund it first, then put the new card on."}), 400
    if st == "authorized":
        r = pp_void(o, "card replaced by dispatch")
        if not r["ok"]:
            return jsonify(r), 400
    db().execute("""UPDATE orders SET pp_state=NULL, pp_auth_id=NULL, pp_auth_cents=NULL, pp_order_id=NULL,
                    pp_source=NULL, pp_error=NULL, payment_status='unpaid', paid_at=NULL, pay_method=NULL,
                    pay_ref=NULL, paid_cents=0 WHERE id=?""", (o["id"],))
    if o["dispatch_status"] == "scheduled":
        db().execute("""UPDATE orders SET sched_dispatch='awaiting_payment', sched_kitchen='waiting',
                        sched_hold='waiting on card' WHERE id=?""", (o["id"],))
    db().execute("DELETE FROM card_vault WHERE order_id=?", (o["id"],))
    db().commit()
    log("payment", o["code"] + " card removed by " + (session.get("dispatcher_name") or "dispatch") + " to put a new one on")
    return jsonify({"ok": True})

def pp_sweep(force=False):
    """Charge delivered orders once the tip window is over; release holds on cancelled ones."""
    if not pp_any_enabled():
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

def pp_can_pay(o, st=None):
    """A card can go on this order through PayPal: open, not cash, nothing held or charged yet."""
    if st is None:
        st = (o["pp_state"] or "") if "pp_state" in o.keys() else ""
    return bool(pp_enabled(pp_order_acct(o)) and st not in ("authorized", "captured")
                and not is_cash(o)
                and (o["payment_status"] or "") not in ("paid", "cash_due")
                and o["dispatch_status"] not in ("delivered", "cancelled"))

def pp_info(o):
    """Payment bits the tracking page and the dispatch card need."""
    st = (o["pp_state"] or "") if "pp_state" in o.keys() else ""
    owed = balance_cents(o) if st == "captured" else 0
    return {"enabled": pp_enabled(pp_order_acct(o)), "state": st, "source": PP_SOURCES.get(o["pp_source"] or "", ""),
            "pay_url": "/pay/" + o["code"],
            "can_pay": pp_can_pay(o, st),
            "tip_editable": st in ("authorized", "captured") and o["dispatch_status"] != "cancelled",
            "owed_cents": max(0, owed), "owed": money(max(0, owed)),
            "held": money(o["pp_auth_cents"] or 0) if st else "",
            "charged": money(o["pp_captured_cents"] or 0) if st == "captured" else "",
            "error": (o["pp_error"] or "") if "pp_error" in o.keys() else "",
            "tip_window_min": pp_tip_window_min(),
            "extra": pp_extra(o, st)}

def pp_extra(o, st):
    """When dispatch adds items or changes the tip on an order PayPal already holds or charged:
    how the extra gets paid without anyone typing the card again."""
    try:
        if st not in ("authorized", "captured") or is_house(o):
            return {}
        due = int(o["total_cents"] or 0) - credits_cents(o) - int(o["refunded_cents"] or 0)
        saved = bool(_rv(o, "pp_vault_id"))
        if not saved and (o["pp_source"] or "") == "card" and o["customer_id"]:
            saved = bool(db().execute("SELECT 1 FROM saved_cards WHERE customer_id=? AND COALESCE(pp_acct,0)=? LIMIT 1",
                                      (o["customer_id"], int(pp_order_acct(o) or 0))).fetchone())
        if st == "authorized":
            held = int(o["pp_auth_cents"] or 0)
            more = due - held
            if more <= 0:
                return {}
            over = max(0, due - pp_cap_cents(held))
            return {"more": money(more), "over": money(over), "over_cents": over, "saved": saved,
                    "note": (money(more) + " more comes off the held payment at delivery") if not over else
                            (money(due - over - held) + " more comes off the held payment at delivery, " + money(over) +
                             (" goes on the saved payment" if saved else " is left for the customer's Pay the rest link"))}
        owed = balance_cents(o)
        if owed <= 0:
            return {}
        return {"more": money(owed), "saved": saved, "owed_cents": owed}
    except Exception as e:
        print("pp_extra failed:", e)
        return {}

@app.post("/api/paypal/collect-rest")
def api_pp_collect_rest():
    """Dispatch charges what's still owed after delivery to the payment the customer already used."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    res = pp_collect_rest(o, "charged by dispatch")
    return jsonify(res), (200 if res.get("ok") else 400)

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
                           pp_ready=pp_enabled(pp_order_acct(o)), client_id=pp_client_id(pp_order_acct(o)),
                           is_dispatch=bool(dispatcher_required()))

@app.post("/api/paypal/create")
def api_pp_create():
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE code=?", ((b.get("code") or "").strip(),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    kind = "balance" if b.get("kind") == "balance" else "order"
    acct = pp_new_acct(o) if (kind == "order" and (o["pp_state"] or "") not in ("authorized", "captured")) else pp_order_acct(o)
    if not pp_enabled(acct):
        return jsonify({"ok": False, "error": "PayPal is not set up yet."}), 400
    with pp_for(acct):
        return _pp_create(o, b, kind, acct)

def _pp_create(o, b, kind, acct):
    if kind == "order":
        if not pp_can_pay(o):
            return jsonify({"ok": False, "error": "This order is already paid."}), 400
        cents, intent = due_cents(o), "AUTHORIZE"
        if cents <= 0:
            return jsonify({"ok": False, "error": "Nothing is owed on this order."}), 400
    else:
        cents, intent = pp_info(o)["owed_cents"], "CAPTURE"
        if cents <= 0:
            return jsonify({"ok": False, "error": "Nothing is owed on this order."}), 400
    body = {
        "intent": intent,
        "purchase_units": [{"reference_id": o["code"], "custom_id": o["code"],
                            "description": ("Delivery order " if kind == "order" else "Rest of order ") + o["code"],
                            "amount": pp_money(cents)}],
        "application_context": {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                                "brand_name": (setting("business_name", str) or "Fleet Foot Delivery")[:120]}}
    if kind == "order":
        # Every order payment is saved with PayPal so fees and tips added later can go on it.
        if b.get("card") and b.get("no_save"):
            pass      # second try after PayPal turned down saving the card: just take the payment
        else:
            if b.get("card"):
                # Typed cards: only ask PayPal to keep the card when the customer ticked Save this card.
                # Saving makes PayPal run extra name/ZIP checks that turn down good cards.
                vs = pp_vault_source(o)
                if vs:
                    body["payment_source"] = vs
            else:
                body["payment_source"] = pp_vault_any("paypal", o)
    plain = {k: v for k, v in body.items() if k != "payment_source"}
    if "paypal" in (body.get("payment_source") or {}):
        body.pop("application_context", None)     # PayPal wants its settings inside payment_source then
    st, j = pp_api("POST", "/v2/checkout/orders", body)
    if "payment_source" in body and (st not in (200, 201) or not j.get("id")):
        # These keys can't save payments (saving not turned on, or a customer ID from other keys):
        # take the payment the normal way so the customer is never stuck.
        log("payment", o["code"] + " PayPal would not save this payment (" + ("account %s" % acct if acct else "main keys") +
            "): " + pp_err(j, "no reason given") + " Took it without saving.")
        st, j = pp_api("POST", "/v2/checkout/orders", plain)
    if st not in (200, 201) or not j.get("id"):
        msg = pp_err(j, "PayPal could not start the payment.")
        log("payment", o["code"] + " PayPal could not start the payment: " + msg +
            " [status %s, %s keys, debug %s]" % (st, pp_conf()["mode"], (j or {}).get("debug_id", "")))
        return jsonify({"ok": False, "error": msg + (" (PayPal ref " + j["debug_id"] + ")" if (j or {}).get("debug_id") else "")}), 400
    if kind == "order":
        db().execute("UPDATE orders SET pp_order_id=?, pp_acct=? WHERE id=?", (j["id"], acct, o["id"]))
        db().commit()
    return jsonify({"ok": True, "id": j["id"]})

def order_discount(o):
    return int(o["discount_cents"] or 0) if (o is not None and "discount_cents" in o.keys()) else 0

def delivered_lock(o):
    """Once an order is delivered, only an owner can change it. Returns an error response or None."""
    if o and o["dispatch_status"] == "delivered" and session.get("dispatcher_id") and not is_owner():
        return jsonify({"ok": False, "error": "This order was already delivered. Only an owner can edit it now."}), 403
    return None


@app.post("/api/paypal/approve")
def api_pp_approve():
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE code=?", ((b.get("code") or "").strip(),)).fetchone()
    ppid = (b.get("id") or "").strip()
    if not o or not ppid:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    acct = pp_order_acct(o)
    if not pp_enabled(acct):
        return jsonify({"ok": False, "error": "PayPal is not set up yet."}), 400
    with pp_for(acct):
        return _pp_approve(o, b, ppid, acct)

def _pp_approve(o, b, ppid, acct):
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
    dec = pp_decline_msg(j, auth)
    if dec or st not in (200, 201) or auth.get("status") not in ("CREATED", "PENDING") or not auth.get("id"):
        if auth.get("id") and auth.get("status") in ("CREATED", "PENDING"):
            try:   # the bank said no: never leave a hold behind
                pp_api("POST", "/v2/payments/authorizations/" + auth["id"] + "/void", request_id="declvoid-" + auth["id"])
            except Exception:
                pass
        msg = dec or pp_err(j, "The payment did not go through.")
        db().execute("UPDATE orders SET pp_error=? WHERE id=?", (msg[:300], o["id"]))
        db().commit()
        log("payment", o["code"] + " card not accepted: " + msg + " [PayPal status %s, debug %s]" % (st, (j or {}).get("debug_id", "")))
        return jsonify({"ok": False, "declined": bool(dec), "error": msg}), 400
    cents = int(round(float(auth["amount"]["value"]) * 100))
    src = next(iter(j.get("payment_source") or {"paypal": 1}))
    db().execute("""UPDATE orders SET pp_auth_id=?, pp_auth_cents=?, pp_state='authorized', pp_source=?,
                    pp_error=NULL, pp_auth_at=? WHERE id=?""", (auth["id"], cents, src, now(), o["id"]))
    db().commit()
    saved = pp_keep_vaulted(o, j, acct)
    pp_keep_order_vault(o, j)
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    mark_paid(o, src if src in ("venmo", "paypal") else "card_paypal", auth["id"], cents)
    return jsonify({"ok": True, "held": money(cents), "source": PP_SOURCES.get(src, "PayPal"),
                    "saved_card": saved})

def _pp_refund(o, cents, note=""):
    """Refund money PayPal already charged, newest-first over the main charge and any
    Pay the rest charges. Returns (ok, refund ids, error)."""
    extras = [x for x in json.loads(o["extra_charges"] or "[]") if str(x.get("label", "")).startswith(("Rest of tip", "Rest of order"))]
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

def pp_settle(o, why="after delivery"):
    with pp_for(pp_order_acct(o)):
        res = _pp_settle(o, why)
    if res.get("ok") and not res.get("voided"):
        rest = pp_collect_rest(o, why)
        if rest.get("charged") or rest.get("error"):
            res["rest"] = rest
    return res

def is_house(o):
    return (_rv(o, "pay_method") or "") == "house_account"

def house_sync(o):
    """House account orders are billed to the account later, so whatever the order now
    comes to goes on the account: nothing is ever left owing on a card."""
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    if not is_house(o) or (o["payment_status"] or "") not in ("paid", "part_refunded"):
        return
    bal = balance_cents(o)
    if bal:
        db().execute("UPDATE orders SET paid_cents=paid_cents+? WHERE id=?", (bal, o["id"]))
        db().commit()

def pp_collect_rest(o, why="extra charges"):
    """The order was already charged and now comes to more (fees dispatch added, a tip added later).
    Charge the difference through PayPal to the card the customer paid with and kept on file.
    Without a card on file the customer gets a Pay the rest link (tracking page, and a text if texting is set up)."""
    try:
        o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
        if is_house(o) or (o["pp_state"] or "") != "captured":
            return {"ok": False, "skipped": True}
        owed = balance_cents(o)
        if owed <= 0:
            return {"ok": True, "nothing": True}
        acct = pp_order_acct(o)
        sc = None
        if _rv(o, "pp_vault_id"):
            vsrc = _rv(o, "pp_vault_src") or "card"
            sc = {"vault_id": o["pp_vault_id"], "pp_acct": acct, "src": vsrc,
                  "brand": {"paypal": "PayPal", "venmo": "Venmo"}.get(vsrc, "card"), "last4": ""}
        elif (o["pp_source"] or "") == "card" and o["customer_id"]:
            sc = db().execute("""SELECT * FROM saved_cards WHERE customer_id=? AND COALESCE(pp_acct,0)=?
                                 ORDER BY id DESC LIMIT 1""", (o["customer_id"], int(acct or 0))).fetchone()
        link = "/pay/" + o["code"] + "?kind=balance"
        if not sc:
            texted = False
            try:
                texted = send_text(o["customer_phone"], "%s: %s more is owed on order %s. Pay here: %s%s" % (
                    payout_brand_name(o), money(owed), o["code"], request.host_url.rstrip("/"), link))
            except Exception:
                texted = False
            log("payment", o["code"] + " " + money(owed) + " still owed (" + why + "), no card on file: customer pays from the link")
            return {"ok": False, "owed": money(owed), "pay_url": link, "texted": bool(texted),
                    "error": "No card on file for this customer, so " + money(owed) + " shows as Pay the rest on their tracking page" +
                             (" and was texted to them." if texted else ".")}
        with pp_for(int(sc["pp_acct"] or 0)):
            st, j = pp_api("POST", "/v2/checkout/orders", {
                "intent": "CAPTURE",
                "purchase_units": [{"reference_id": o["code"], "custom_id": o["code"],
                                    "description": "Rest of order " + o["code"], "amount": pp_money(owed)}],
                "payment_source": {(sc["src"] if isinstance(sc, dict) else "card"): {"vault_id": sc["vault_id"]}}},
                request_id="rest-" + o["code"] + "-" + str(int(o["paid_cents"] or 0)) + "-" + str(owed))
        cap = (((j.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
        if st not in (200, 201) or cap.get("status") not in ("COMPLETED", "PENDING"):
            msg = pp_err(j, "the card on file was declined.")
            log("payment", o["code"] + " could not charge the rest (" + money(owed) + "): " + msg)
            return {"ok": False, "owed": money(owed), "pay_url": link,
                    "error": "Card on file didn't take " + money(owed) + ": " + msg + " It shows as Pay the rest on their tracking page."}
        cents = int(round(float(cap["amount"]["value"]) * 100))
        card = (sc["brand"] or "card") if (isinstance(sc, dict) and sc["src"] != "card") else \
            ((sc["brand"] or "card").title() + (" ending " + sc["last4"] if sc["last4"] else " on file"))
        add_extra_charge(o, cents, "Rest of order (" + card + ")", cap.get("id", ""))
        db().execute("UPDATE orders SET pp_captured_cents=COALESCE(pp_captured_cents,0)+? WHERE id=?", (cents, o["id"]))
        db().commit()
        log("payment", o["code"] + " charged the rest, " + money(cents) + " on " + card + " (" + why + ")")
        return {"ok": True, "charged": money(cents), "card": card}
    except Exception as e:
        print("collect rest failed:", e)
        return {"ok": False, "error": "Could not charge the rest: " + str(e)[:120]}

def pp_void(o, why="cancelled"):
    with pp_for(pp_order_acct(o)):
        return _pp_void(o, why)

def pp_refund(o, cents, note=""):
    with pp_for(pp_order_acct(o)):
        return _pp_refund(o, cents, note)

@app.post("/api/paypal/settle")
def api_pp_settle():
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
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
        auto_msg("drv_tip", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (o["driver_id"], "system", "Tip on " + o["code"] + " is now " + money(cents) + ".", now()))
        db().commit()
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    out = {"ok": True, "tip": money(cents), "total": money(o["total_cents"])}
    if o["pp_state"] == "authorized" and o["dispatch_status"] == "delivered":
        out["charge"] = pp_settle(o, "tip added after delivery")
    elif o["pp_state"] == "captured" and diff > 0:
        out["charge"] = pp_collect_rest(o, "tip added after delivery")
    out["pay"] = pp_info(db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone())
    return jsonify(out)

@app.context_processor
def inject_paypal():
    return {"pp_enabled": pp_enabled(), "pp_on": pp_enabled()}


@app.context_processor
def inject_brand_look():
    try:
        if request.path.startswith(_STAFF_PREFIXES):
            return {"brand_css": "", "brand_theme": "", "brand_font": ""}
        s = current_site()
        return brand_look(s) if s is not None else {"brand_css": "", "brand_theme": "", "brand_font": ""}
    except Exception:
        return {"brand_css": "", "brand_theme": "", "brand_font": ""}


@app.context_processor
def inject_modes():
    try:
        return {"brands_on": brands_on(), "regions_on": regions_on(), "region_lock": order_lock_on()}
    except Exception:
        return {"brands_on": True, "regions_on": True}


@app.context_processor
def inject_dev_flag():
    try:
        on = bool(session.get("dispatcher_id"))
        return {"dev_user": on and is_dev(), "owner_user": on and is_owner()}
    except Exception:
        return {"dev_user": False, "owner_user": False}


# ---------------------------------------------------------------- future orders
FUTURE_LEAD_ERR = []
FUTURE_MIN_AHEAD = 30      # a future order has to be at least this far out
FUTURE_MAX_DAYS = 14

def future_lead():
    try:
        return max(10, min(240, int(setting("future_lead_min") or 45)))
    except Exception:
        return 45

def when_label(s, rid=None, local=False):
    """'Today at 6:30 PM'. With a region, an app-time stamp is shown in that region's time
    (local=True means s is already the region's time)."""
    try:
        w = dt.datetime.fromisoformat(s)
    except Exception:
        return s or ""
    tag = ""
    if rid:
        if not local:
            w = to_region(w, rid)
        tag = tz_tag(rid)
    day = w.date()
    today = region_now(rid).date() if rid else dt.date.today()
    if day == today:
        d = "Today"
    elif day == today + dt.timedelta(days=1):
        d = "Tomorrow"
    else:
        d = w.strftime("%a %b ") + str(w.day)
    return d + " at " + w.strftime("%I:%M %p").lstrip("0") + tag

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
    rg = _rv(r, "region_id")
    t = dt.datetime.combine(day, dt.time(0, 0))     # the restaurant's local day
    end = t + dt.timedelta(days=1)
    while t < end:
        ta = from_region(t, rg)
        if (ta >= earliest and is_open(r, ta) and business_in_hours(ta, rg) and
                (r["slug"] != "oneoff" or dispatcher_required() or item_available(any_rest_row(), ta))):
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
        log("order", o["code"] + " future order for " + when_label(o["scheduled_for"], _rv(o, "region_id")) +
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

REST_CHAT_OK_SQL = "COALESCE(r.uses_app,0)=1 AND COALESCE(r.slug,'')!='oneoff'"

def rest_chat_allowed(rid):
    """Only restaurants that take orders on the tablet get a chat. Called-in restaurants
    and typed-in pickups are phoned, not messaged."""
    r = db().execute("SELECT uses_app, slug FROM restaurants WHERE id=?", (rid,)).fetchone()
    return bool(r) and bool(r["uses_app"]) and (r["slug"] or "") != "oneoff"


def rest_ord_no(o):
    """The order number the kitchen sees on its cards."""
    try:
        return (o["primary_no"] or o["code"]) if "primary_no" in o.keys() else o["code"]
    except Exception:
        return o["code"]


def rest_auto_status(rid, body):
    rest_auto(rid, body, key="rest_status")

def rest_auto(rid, body, key="rest_order"):
    """Automatic chat message from dispatch to a kitchen. Restaurants that don't use the
    app have no chat, so they get nothing. Dispatch doesn't see it as unread.
    Owners switch order-change and status-change messages off in Settings."""
    try:
        if not rid or not auto_msg_on(key) or not rest_chat_allowed(rid):
            return
        db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_rest,seen_by_dispatch)
                        VALUES(?,?,?,?,?,0,1)""", (rid, "dispatch", "Automatic message", "Automatic message: " + body, now()))
        db().commit()
    except Exception as e:
        print("rest_auto skipped", rid, e)

def rest_chat_unread_for_dispatch():
    n = db().execute("""SELECT COUNT(*) c FROM rest_messages m JOIN restaurants r ON r.id=m.restaurant_id
                        WHERE m.sender='restaurant' AND m.seen_by_dispatch=0 AND """ + REST_CHAT_OK_SQL).fetchone()["c"]
    m = db().execute("""SELECT m.id, m.restaurant_id, m.body, r.name FROM rest_messages m
                        JOIN restaurants r ON r.id=m.restaurant_id
                        WHERE m.sender='restaurant' AND m.seen_by_dispatch=0 AND """ + REST_CHAT_OK_SQL + """
                        ORDER BY m.id DESC LIMIT 1""").fetchone()
    return n, ({"id": m["id"], "restaurant_id": m["restaurant_id"], "name": m["name"], "body": m["body"]} if m else None)

@app.route("/api/restaurant/chat", methods=["GET", "POST"])
def api_rest_chat():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False}), 403
    if not rest_chat_allowed(rid):
        return jsonify({"ok": False, "no_chat": True, "messages": [],
                        "error": "Chat is only for restaurants that take orders in the app. Call dispatch instead."}), 403
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
    rows = db().execute("""SELECT * FROM (SELECT r.id, r.name,
            (SELECT COUNT(*) FROM rest_messages m WHERE m.restaurant_id=r.id AND m.sender='restaurant'
               AND m.seen_by_dispatch=0) unread,
            (SELECT MAX(id) FROM rest_messages m WHERE m.restaurant_id=r.id) last_id
            FROM restaurants r WHERE """ + REST_CHAT_OK_SQL + """) x
            ORDER BY unread DESC, last_id IS NULL, last_id DESC, name""").fetchall()
    return jsonify({"ok": True, "restaurants": [{"id": r["id"], "name": r["name"], "unread": r["unread"]} for r in rows]})

@app.route("/api/dispatch/rest-chat/<int:rid>", methods=["GET", "POST"])
def api_dispatch_rest_chat(rid):
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    rr = db().execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not rr:
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 404
    if not rest_chat_allowed(rid):
        return jsonify({"ok": False, "no_chat": True, "messages": [],
                        "error": rr["name"] + " doesn't take orders in the app, so there's no chat. Call them instead."}), 400
    if request.method == "POST":
        body = " ".join(((request.get_json(silent=True) or {}).get("body") or "").split())[:1000]
        if not body:
            return jsonify({"ok": False, "error": "Type a message first."}), 400
        db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_dispatch)
                        VALUES(?,?,?,?,?,1)""", (rid, "dispatch", (session.get("dispatcher_name") or "Dispatch") + (" (Developer)" if is_dev() else ""), body, now()))
    db().execute("UPDATE rest_messages SET seen_by_dispatch=1 WHERE restaurant_id=? AND sender='restaurant'", (rid,))
    db().commit()
    return jsonify({"ok": True, "messages": rest_chat_rows(rid)})

@app.get("/api/future-slots")
def api_future_slots():
    r = db().execute("SELECT * FROM restaurants WHERE id=?", (request.args.get("restaurant_id"),)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Unknown restaurant"}), 404
    days = []
    rg = _rv(r, "region_id")
    for n in range(FUTURE_MAX_DAYS):
        day = region_now(rg).date() + dt.timedelta(days=n)      # the restaurant's own today
        slots = future_slots(r, day)
        if slots:
            days.append({"date": day.isoformat(),
                         "label": when_label(slots[0], rg, local=True).split(" at ")[0],
                         "slots": [{"value": s, "label": when_label(s, rg, local=True).split(" at ")[1]} for s in slots]})
    return jsonify({"ok": True, "open_now": is_open(r), "days": days, "lead_min": future_lead(),
                    "tz": region_tz(rg), "tz_tag": tz_tag(rg).strip()})

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
    log("cancel", o["code"] + " (future order for " + when_label(o["scheduled_for"], _rv(o, "region_id")) + ") cancelled by " + who + ": " + why)
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
    if r is not None and restaurant_locked(r):
        # A locked brand takes no orders at all, from customers or dispatch (developer test view excepted).
        if dispatcher_required():
            return jsonify({"ok": False, "error": region_label(_rv(r, "region_id")) + " belongs to a locked brand, so no orders can be "
                            "created there. Unlock the brand in Settings > Regions > Brand sites first."}), 403
        return jsonify({"ok": False, "error": "This restaurant is not taking online orders yet."}), 403
    placed_by = payload.get("placed_by", "customer")
    if placed_by == "dispatch" and dispatcher_required() and not can_create_in_region(_rv(r, "region_id")):
        return jsonify({"ok": False, "error": "You're not assigned to " + region_label(_rv(r, "region_id")) +
                        ", so you can't create orders there. Ask the owner to add you to that region."}), 403
    house = bool(payload.get("house")) and placed_by == "dispatch" and bool(dispatcher_required())
    multi_root, _merr = multi_resolve(payload.get("multi_with"), payload.get("customer_phone"),
                                      placed_by == "dispatch" and bool(dispatcher_required()))
    if _merr:
        return jsonify({"ok": False, "error": _merr}), 400
    if payload.get("cash") and not house and not cash_allowed():
        return jsonify({"ok": False, "error": "We don't take cash orders. Pay by card, PayPal, Venmo, gift card, or house account."}), 400
    if house and not str(payload.get("house_account") or "").strip():
        return jsonify({"ok": False, "error": "Type the business name for the house account."}), 400
    if house:
        payload["cash"] = True   # created like a cash order (no card), then marked paid to the house account
    sched = None
    if (payload.get("scheduled_for") or "").strip():
        sched = parse_future(payload.get("scheduled_for"))
        if not sched:
            return jsonify({"ok": False, "error": "Pick a date and time for the future order."}), 400
        sched = from_region(sched, _rv(r, "region_id"))   # picked in the restaurant's own time
        is_disp = bool(dispatcher_required())
        soonest = dt.datetime.now() + dt.timedelta(minutes=(5 if is_disp else FUTURE_MIN_AHEAD - 1))
        if sched < soonest:
            return jsonify({"ok": False, "error": "A future order has to be at least " +
                            ("5" if is_disp else str(FUTURE_MIN_AHEAD)) + " minutes from now."}), 400
        if sched > dt.datetime.now() + dt.timedelta(days=FUTURE_MAX_DAYS):
            return jsonify({"ok": False, "error": "Future orders can be up to " +
                            str(FUTURE_MAX_DAYS) + " days out."}), 400
        if not is_disp and not business_in_hours(sched, r["region_id"]):
            cl = closed_dates_label(r["region_id"])
            return jsonify({"ok": False, "error": "We are not open at " + when_label(sched.isoformat(), r["region_id"]) + "." +
                            ((" Our hours are " + business_hours_label(r["region_id"]) + ".") if business_hours_label(r["region_id"]) else "") +
                            ((" " + cl + ".") if cl else "")}), 400
        if not is_disp and not is_open(r, sched):
            return jsonify({"ok": False, "error": r["name"] + " is not open at " +
                            when_label(sched.isoformat(), r["region_id"]) + ". Pick another time."}), 400
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
    if not dispatcher_required():
        _mr = delivery_rules(r)
        if _mr["min_cents"] and subtotal < _mr["min_cents"]:
            return jsonify({"ok": False, "below_minimum": True, "error":
                            "%s has a %s minimum order. Add %s more to check out."
                            % (r["name"], money(_mr["min_cents"]), money(_mr["min_cents"] - subtotal))}), 400
    # one restaurant per order (customers, drivers and dispatch alike)
    for i in items:
        if i.get("menu_item_id"):
            _row = db().execute("SELECT restaurant_id FROM menu_items WHERE id=?", (i["menu_item_id"],)).fetchone()
            if _row and _row["restaurant_id"] != r["id"]:
                return jsonify({"ok": False, "error": "One restaurant per order. Take the other restaurant's items out "
                                "and place them as a separate order. The order minimum and delivery fee apply to each order."}), 400
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
    use_pp = (bool(payload.get("paypal")) or placed_by == "customer") and pp_enabled(pp_region_acct(_rv(r, "region_id")))
    _credit_try = bool(payload.get("gift_code") or payload.get("redeem_rewards") or payload.get("saved_card_id"))
    if placed_by == "customer" and not use_pp and not _credit_try:
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
                      else (0, fee_rules(_rv(r, "region_id"))["base_fee"]))
    else:
        formatted, lat, lng = typed, None, None
        miles, fee = 0, fee_rules(_rv(r, "region_id"))["base_fee"]
        _zc = zip_check(r, typed)
        if _zc["found"]:                 # priced from the ZIP code's center until dispatch confirms the address
            miles, fee = _zc["miles"], _zc["fee"]
    if payload.get("fee_cents_override") not in (None, ""):
        fee = max(0, int(round(float(payload["fee_cents_override"]))))
    if not dispatcher_required():
        # customers and drivers: the restaurant's (or its region's) minimum and delivery radius.
        # Dispatch can still place an order outside them for a call-in.
        rules = delivery_rules(r)
        if not address_ok:
            if not _zc["found"]:
                return jsonify({"ok": False, "need_zip": True, "error":
                                "We couldn't find that street. Add your 5-digit ZIP code to the address and "
                                "you can still place the order."}), 400
            if not _zc["within"]:
                return jsonify({"ok": False, "out_of_range": True, "error":
                                "ZIP %s is about %.1f mi from %s. %s delivers up to %g mi."
                                % (_zc["zip"], _zc["miles"], r["name"], r["name"], _zc["max_miles"])}), 400
        if rules["max_miles"] and address_ok and miles > rules["max_miles"]:
            return jsonify({"ok": False, "out_of_range": True, "error":
                            "That address is %.1f mi from %s. %s delivers up to %g mi."
                            % (miles, r["name"], r["name"], rules["max_miles"])}), 400
    tax = int(round(subtotal * setting("tax_rate_bp") / 10000.0))
    service = int(round(subtotal * service_bp_for(r) / 10000.0))
    try:
        tip = int(round(float(payload.get("tip_cents", 0) or 0)))
    except Exception:
        return jsonify({"ok": False, "error": "Type a tip amount, like 5.00."}), 400
    if tip < 0:
        return jsonify({"ok": False, "error": "The tip can't be a negative amount."}), 400
    total = subtotal + fee + ifee + tax + service + tip
    # rewards account, rewards, gift card and saved card
    cr = checkout_credits(payload, placed_by, dg, subtotal, total)
    if cr.get("error"):
        return jsonify({"ok": False, "error": cr["error"]}), 400
    due_now = max(0, total - cr["gift_cents"] - cr["reward_cents"])
    if placed_by == "customer" and not use_pp and card is None and due_now > 0 and not cr["saved_card"]:
        card, card_err = check_card(payload.get("card"))
        if not card:
            return jsonify({"ok": False, "error": card_err, "field": "card"}), 400

    issue_key = (payload.get("issue") or "").strip()
    issue_label, issue_note, from_code = "", (payload.get("issue_note") or "").strip(), ""
    src_id = payload.get("from_order_id")
    if src_id:
        src = db().execute("SELECT * FROM orders WHERE id=?", (src_id,)).fetchone()
        if src and reorder_closed(src):
            return jsonify({"ok": False, "error": reorder_closed_msg(src)}), 400
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

    if not sched and (not business_is_open() or (not dispatcher_required() and not business_in_hours(rid=r["region_id"]))):
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
    code = make_order_code()
    cur = db().execute("""INSERT INTO orders(code,restaurant_id,customer_name,customer_phone,address,
        address_note,dispatch_note,lat,lng,items,subtotal_cents,fee_cents,item_fee_cents,tax_cents,
        tip_cents,total_cents,miles,issue,issue_note,cloned_from,address_ok,source,ref_code,token,
        kitchen_status,dispatch_status,hold_reason,placed_by,created_at,
        pickup_name,pickup_address,pickup_phone,pickup_lat,pickup_lng,service_cents,drop_style)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, r["id"], payload["customer_name"], payload["customer_phone"], formatted,
         payload.get("note", ""), payload.get("dispatch_note", ""), lat, lng,
         json.dumps(items), subtotal, fee, ifee, tax, tip, total, miles,
         issue_label, issue_note, from_code, address_ok, src,
         (clean_ref(payload.get("ref"), payload.get("customer_phone")) or None) if dispatcher_required() else None,
         (clean_token(payload.get("token")) or token_from_source(src)),
         kitchen_status, dstat, hold_reason, placed_by, now(),
         pu_name or None, pu_addr if pu_name else None, pu_phone if pu_name else None,
         pu_lat if pu_name else None, pu_lng if pu_name else None, service,
         clean_drop_style(payload.get("drop_style"))))
    db().commit()
    log("order", code + " placed for " + r["name"] +
        ("" if address_ok else " (address not verified, waiting on dispatch approval)") +
        (" (from " + from_code + (": " + issue_label if issue_label else "") + ")" if from_code else ""))
    oid = cur.lastrowid
    try:
        assign_primary(oid)
        db().commit()
    except Exception as _pe:
        log("order", "restaurant order number skipped: %s" % _pe)
    if True:  # every order waits for Send to kitchen, paid or not
        db().execute("""UPDATE orders SET kitchen_go=0, kitchen_sent_at=NULL,
            hold_reason=CASE WHEN kitchen_status='pending' THEN 'tap Send to kitchen' ELSE hold_reason END,
            kitchen_status=CASE WHEN kitchen_status='pending' THEN 'waiting' ELSE kitchen_status END
            WHERE id=?""", (oid,))
        db().commit()
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
                          "future order for " + when_label(sched.isoformat(), r["region_id"]), oid))
            log("order", code + " scheduled for " + when_label(sched.isoformat(), r["region_id"]))
        db().commit()
        future_note = "Scheduled for " + when_label(sched.isoformat(), r["region_id"]) + "."
        if dispatcher_required() and not is_open(r, sched):
            future_note += " Heads up: " + r["name"] + " is not normally open then."
    if multi_root:
        db().execute("UPDATE orders SET multi_with=? WHERE id=?", (multi_root, oid))
        db().commit()
        log("order", code + " is part of a multiple order with " + multi_root)
    credit_note = apply_checkout_credits(oid, code, cr, placed_by)
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
    if house:
        mark_paid(db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone(), method="house_account",
                  ref=("House account " + str(payload.get("house_account") or "").strip()[:60]).strip()[:80])
    paid_by_credit = credit_note.get("paid", False)
    _orow = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    confirm = confirm_call_info(_orow)
    return jsonify({"pay_url": ("/pay/" + code) if (use_pp and not cash and not paid_by_credit) else "",
                    "credit": credit_note, "confirm_call": confirm,
                    "future_note": future_note, "ok": True, "cash": cash, "code": code, "order_id": oid, "total": money(total),
                    "send_note": send_note,
                    "address_ok": bool(address_ok),
                    "message": (credit_note.get("message", "") + " " if credit_note.get("message") else "") + ("" if address_ok and cash else
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
    lk = delivered_lock(o)
    if lk:
        return lk
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
    total = o["subtotal_cents"] + fee + o["item_fee_cents"] + o["tax_cents"] + (o["service_cents"] or 0) + o["tip_cents"] \
        - order_discount(o)
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
                    "sent_to_kitchen": (db().execute("SELECT kitchen_status FROM orders WHERE id=?", (o["id"],)).fetchone()[0] == "pending") and o["kitchen_status"] == "waiting",
                    "waiting_on_payment": unpaid_card})

def _norm_no(v):
    return re.sub(r"[^A-Za-z0-9]", "", v or "").upper()


def orders_by_primary(num, days=45):
    """Recent orders whose restaurant order number matches (TT12, TT-12 and tt12 all match)."""
    n = _norm_no(num)
    if not n:
        return []
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows = db().execute("""SELECT * FROM orders WHERE primary_no IS NOT NULL AND primary_no<>'' AND created_at>=?
                           ORDER BY id DESC""", (since,)).fetchall()
    return [r for r in rows if _norm_no(r["primary_no"]) == n]


@app.route("/track/<code>", methods=["GET", "POST"])
def track(code):
    code = (code or "").strip()
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone() or \
        db().execute("SELECT * FROM orders WHERE UPPER(code)=?", (code.upper(),)).fetchone()
    if o and o["code"] != code:
        return redirect("/track/" + o["code"])
    if not o:
        # Restaurant order numbers (TT12) are short and easy to guess, so the tracking page,
        # which shows the address, also asks for the last 4 digits of the phone on the order.
        matches = orders_by_primary(code)
        if matches:
            last4 = re.sub(r"\D", "", request.values.get("phone4", ""))[-4:]
            err = ""
            if last4:
                hit = next((m for m in matches if phone_digits(m["customer_phone"])[-4:] == last4), None)
                if hit:
                    return redirect("/track/" + hit["code"])
                err = "That doesn't match the phone number on order %s." % matches[0]["primary_no"]
            return render_template("track.html", order=None, code=code, need_phone=True,
                                   primary=matches[0]["primary_no"], phone_err=err)
        return render_template("track.html", order=None, code=code)
    if True:   # the order's own brand, whatever web address the link was opened on
        _ts = site_of_region(o["region_id"])
        if _ts is not None:
            g._site_forced = _ts   # the order's brand logo, name and phone on its tracking page
    return render_template("track.html", order={"code": o["code"],
                           "primary_no": (o["primary_no"] if "primary_no" in o.keys() else "") or ""}, code=code)

TRACK_FIELDS = ("code", "primary_no", "multi_group", "note", "credits", "dispatch_status", "kitchen_status", "restaurant", "restaurant_nav", "address",
                "scheduled_label", "needs_address_approval", "timeline", "timer_seconds",
                "queue_position", "hold_reason", "miles", "subtotal", "fee", "service", "service_cents",
                "tax", "tip", "total", "delivered_time", "lines", "uses_app", "manual_state",
                "gift", "reward", "due", "confirm_call")
TRACK_AVG_MPH = 25.0       # town driving speed used for the customer's rough arrival time
TRACK_FIX_FRESH_MIN = 10   # an older GPS fix is not shown to the customer
ETA_STOP_MIN = 5           # minutes added for each stop a driver makes before this one
ETA_NO_DRIVER_MIN = 10     # assumed time for a driver to reach the restaurant when none is on it yet
_LEG_CACHE = {}            # (rounded from, rounded to) -> (stamp, minutes)


# ---------------- Google Routes API (replaces the old Directions / Distance Matrix) ----------------
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _gwp(lat, lng):
    return {"location": {"latLng": {"latitude": float(lat), "longitude": float(lng)}}}


def _gsecs(v):
    try:
        return float(str(v or "0").rstrip("s"))
    except ValueError:
        return 0.0


def google_routes(a, b, mask, timeout=8):
    """One driving route from Google's Routes API with live traffic. Raises on any error."""
    body = {"origin": _gwp(a[0], a[1]), "destination": _gwp(b[0], b[1]), "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE", "units": "IMPERIAL", "languageCode": "en-US"}
    req = urllib.request.Request(ROUTES_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_KEY,
                                          "X-Goog-FieldMask": mask})
    try:
        res = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError("Routes API %s %s" % (e.code, msg))
    if not res.get("routes"):
        raise RuntimeError("Routes API found no route")
    return res["routes"][0]


_GMAN = {"TURN_LEFT": "left", "TURN_RIGHT": "right", "TURN_SLIGHT_LEFT": "slight left",
         "TURN_SLIGHT_RIGHT": "slight right", "TURN_SHARP_LEFT": "sharp left", "TURN_SHARP_RIGHT": "sharp right",
         "UTURN_LEFT": "uturn", "UTURN_RIGHT": "uturn", "RAMP_LEFT": "slight left", "RAMP_RIGHT": "slight right",
         "FORK_LEFT": "slight left", "FORK_RIGHT": "slight right", "MERGE": "straight", "STRAIGHT": "straight",
         "ROUNDABOUT_LEFT": "left", "ROUNDABOUT_RIGHT": "right", "ROUNDABOUT_CLOCKWISE": "right",
         "ROUNDABOUT_COUNTERCLOCKWISE": "left", "FERRY_BOAT": "straight", "FERRY_TRAIN": "straight",
         "NAME_CHANGE": "straight", "DEPART": "straight"}


def _route_google_routes(a, b):
    rt = google_routes(a, b, "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline,"
                             "routes.legs.endLocation,routes.legs.steps.distanceMeters,"
                             "routes.legs.steps.staticDuration,routes.legs.steps.startLocation,"
                             "routes.legs.steps.navigationInstruction", timeout=12)
    steps = []
    for leg in rt.get("legs", []):
        for st in leg.get("steps", []):
            ni = st.get("navigationInstruction") or {}
            man = ni.get("maneuver", "")
            ll = (st.get("startLocation") or {}).get("latLng") or {}
            if "latitude" not in ll:
                continue
            steps.append({"text": ni.get("instructions") or "Continue",
                          "type": "depart" if (man == "DEPART" or not steps) else "turn",
                          "modifier": _GMAN.get(man, "straight"),
                          "lat": ll["latitude"], "lng": ll["longitude"],
                          "dist_m": st.get("distanceMeters", 0), "dur_s": _gsecs(st.get("staticDuration"))})
    end = ((rt.get("legs") or [{}])[-1].get("endLocation") or {}).get("latLng") or {"latitude": b[0], "longitude": b[1]}
    steps.append({"text": "You have arrived", "type": "arrive", "modifier": "",
                  "lat": end["latitude"], "lng": end["longitude"], "dist_m": 0, "dur_s": 0})
    return {"coords": _decode_poly((rt.get("polyline") or {}).get("encodedPolyline", "")),
            "steps": steps, "distance_m": rt.get("distanceMeters", 0),
            "duration_s": _gsecs(rt.get("duration")), "source": "google"}


def leg_minutes(a_lat, a_lng, b_lat, b_lng):
    """Driving minutes between two points. Live traffic from Google when
    GOOGLE_MAPS_API_KEY is set (cached 90 seconds), otherwise distance at town speed."""
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    key = ("%.3f,%.3f" % (a_lat, a_lng), "%.3f,%.3f" % (b_lat, b_lng))
    hit = _LEG_CACHE.get(key)
    if hit and time.time() - hit[0] < 90:
        return hit[1]
    mins = None
    if GOOGLE_KEY:
        try:
            secs = _gsecs(google_routes((a_lat, a_lng), (b_lat, b_lng), "routes.duration", timeout=4).get("duration"))
            mins = secs / 60.0 if secs else None
        except Exception:
            mins = None
    if mins is None and GOOGLE_KEY:
        try:
            url = ("https://maps.googleapis.com/maps/api/distancematrix/json?origins=%f,%f&destinations=%f,%f"
                   "&departure_time=now&units=imperial&key=%s"
                   % (a_lat, a_lng, b_lat, b_lng, urllib.parse.quote(GOOGLE_KEY)))
            with urllib.request.urlopen(url, timeout=4) as resp:
                js = json.loads(resp.read().decode("utf-8"))
            el = js["rows"][0]["elements"][0]
            if el.get("status") == "OK":
                secs = (el.get("duration_in_traffic") or el.get("duration") or {}).get("value")
                if secs:
                    mins = secs / 60.0
        except Exception:
            mins = None
    if mins is None:
        mins = haversine_miles(a_lat, a_lng, b_lat, b_lng) * ROAD_FACTOR / TRACK_AVG_MPH * 60
    _LEG_CACHE[key] = (time.time(), mins)
    if len(_LEG_CACHE) > 2000:
        _LEG_CACHE.clear()
    return mins


def _fresh_fix(d):
    if not d or d["last_lat"] is None or d["last_lng"] is None or not d["last_loc_at"]:
        return None
    try:
        age = (dt.datetime.now() - dt.datetime.fromisoformat(d["last_loc_at"])).total_seconds() / 60
    except ValueError:
        return None
    return (d["last_lat"], d["last_lng"]) if age <= TRACK_FIX_FRESH_MIN else None


def eta_info(o, r=None, d=None):
    """Live estimate of when the food reaches the customer, for the tracking page and the board.
    Adds up what is left: the kitchen timer, the driver getting to the restaurant (from their
    live GPS when it is fresh), stops ahead of this one in the driver's stack, and the drive out."""
    st = o["dispatch_status"]
    if st in ("delivered", "cancelled"):
        return {"eta_min": None, "eta_clock": "", "eta_note": ""}
    nowdt = dt.datetime.now()
    if o["scheduled_for"] and not o["prep_started"]:
        try:
            when = dt.datetime.fromisoformat(o["scheduled_for"])
            if when > nowdt + dt.timedelta(minutes=10):
                return {"eta_min": int((when - nowdt).total_seconds() // 60),
                        "eta_clock": clock(when.isoformat(), _rv(o, "region_id")), "eta_note": "scheduled"}
        except ValueError:
            pass
    if r is None:
        r = db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    if d is None and o["driver_id"]:
        d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone()
    pu = pickup_of(o, r)
    # kitchen time left
    ks = o["kitchen_status"]
    if ks == "ready" or st == "enroute":
        kitchen = 0.0
    elif o["prep_started"] and o["prep_minutes"]:
        end = dt.datetime.fromisoformat(o["prep_started"]) + dt.timedelta(minutes=o["prep_minutes"])
        kitchen = max(0.0, (end - nowdt).total_seconds() / 60)
    else:
        kitchen = float((r["prep_default"] if r is not None and r["prep_default"] else 15))
    # the drive from the restaurant to the customer
    out = leg_minutes(pu["lat"], pu["lng"], o["lat"], o["lng"])
    if out is None:
        out = (o["miles"] or 3) / TRACK_AVG_MPH * 60
    fix = _fresh_fix(d)
    ahead = 0
    if d:
        ahead = db().execute("""SELECT COUNT(*) n FROM orders WHERE driver_id=? AND id<>?
                                AND dispatch_status IN ('assigned','received','at_restaurant','enroute')
                                AND COALESCE(stack_seq,0) < ?""",
                             (d["id"], o["id"], o["stack_seq"] or 0)).fetchone()["n"]
    note = ""
    if st == "enroute":
        to_home = leg_minutes(fix[0], fix[1], o["lat"], o["lng"]) if fix else None
        total = (to_home if to_home is not None else out) + ahead * ETA_STOP_MIN
        note = "live GPS" if fix else ""
    elif st == "at_restaurant":
        total = kitchen + ahead * ETA_STOP_MIN + out
    elif d and st in ("assigned", "received"):
        to_rest = leg_minutes(fix[0], fix[1], pu["lat"], pu["lng"]) if fix else None
        if to_rest is None:
            to_rest = ETA_NO_DRIVER_MIN
        total = max(kitchen, to_rest + ahead * ETA_STOP_MIN) + out
        note = "live GPS" if fix else ""
    else:
        total = max(kitchen, ETA_NO_DRIVER_MIN) + out
        note = "no driver yet"
    total = max(2, int(round(total)))
    return {"eta_min": total, "eta_clock": clock((nowdt + dt.timedelta(minutes=total)).isoformat(), _rv(o, "region_id")),
            "eta_note": note}


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
         "dispatch_tel": tel_digits(dispatch_phone(o["region_id"]))}
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
    e = eta_info(o, r, d)
    t["eta_min"], t["eta_clock"], t["eta_note"] = e["eta_min"], e["eta_clock"], e["eta_note"]
    out["track"] = t
    return out


@app.get("/api/track/<code>")
def api_track(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    pp_sweep()
    credit_sweep()
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
    try:
        picks = dispatch_pick_companies()
    except Exception:
        picks = []
    here_names = [c["brand"] or c["name"] for c in picks if c["here"]]
    return render_template("dispatch_login.html", err=err, picks=picks, here_names=here_names)


@app.get("/dispatch-company")
def dispatch_company_code():
    """A dispatcher types their company code on the main dispatch sign-in page. A company on a separate
    platform opens its own dispatch sign-in; a brand on this platform stays here. Sign-ins never carry
    over: a separate company's accounts only work on its own platform."""
    code = (request.args.get("code") or "").strip().lower()
    r = db().execute("SELECT * FROM companies WHERE code=? AND COALESCE(active,1)=1", (code,)).fetchone() if code else None
    if not r or company_locked(dict(r)):
        return redirect("/dispatch/login?nocode=1")
    r = dict(r)
    if company_here(r):
        return redirect("/dispatch/login")
    return redirect(live_company_url(r["url"]).rstrip("/") + "/dispatch/login")

@app.route("/dispatch/logout")
def dispatch_logout():
    try:
        activity_mark("dispatcher", session.get("dispatcher_id"), None)
    except Exception:
        pass
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


# ---------------- driver pay (PayPal Payouts) ----------------
# Dispatch pays the driver for each delivered trip from the PayPal business balance,
# to the driver's PayPal email or Venmo phone. Every send is its own row so a later
# extra (a tip that came in after) is just another payment.
PAYOUT_BAD = {"FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED", "ERROR"}
PAYOUT_OPEN = {"SENDING", "UNKNOWN", "PENDING", "PROCESSING", "NEW", "ONHOLD", "UNCLAIMED"}
PAYOUT_LABEL = {"SUCCESS": "paid", "PENDING": "sending", "PROCESSING": "sending", "NEW": "sending",
                "SENDING": "sending", "ONHOLD": "on hold at PayPal", "UNCLAIMED": "waiting for driver to claim",
                "UNKNOWN": "not confirmed yet", "FAILED": "failed", "RETURNED": "returned", "BLOCKED": "blocked",
                "REFUNDED": "returned", "REVERSED": "reversed", "DENIED": "denied", "CANCELED": "cancelled",
                "ERROR": "failed", "BANK_PAID": "paid by bank", "CHECK_PAID": "paid by check"}
_payout_last = [0.0]

def _pct_setting(key, default):
    try:
        v = int(setting(key, str) or default)
    except (TypeError, ValueError):
        v = default
    return max(0, min(100, v))

def drv_pay_rule():
    try:
        flat = int(setting("driver_pay_flat_cents", str) or 0)
    except (TypeError, ValueError):
        flat = 0
    def _c(k):
        raw = str(setting(k, str) or "").strip()
        try:
            return max(0, min(50000, int(raw))) if raw != "" else None
        except ValueError:
            return None
    return {"fee_pct": _pct_setting("driver_pay_fee_pct", 100),
            "tip_pct": _pct_setting("driver_pay_tip_pct", 100),
            "flat_cents": max(0, min(50000, flat)),
            "base_cents": _c("driver_pay_base_cents"), "mile_cents": _c("driver_pay_mile_cents")}

def drv_fee_pay(o, r=None):
    """Driver's part of the delivery fee. With a set driver base pay (Settings > Driver pay),
    the driver gets that for the base fee and the per-mile pay for each extra mile billed;
    otherwise the share of the fee in percent."""
    r = r or drv_pay_rule()
    fee = int(o["fee_cents"] or 0)
    if r.get("base_cents") is None:
        return fee * r["fee_pct"] // 100
    rid = None
    try:
        rid = _rv(o, "region_id") or _rv(db().execute("SELECT region_id FROM restaurants WHERE id=?",
                                                      (o["restaurant_id"],)).fetchone(), "region_id")
    except Exception:
        rid = None
    fr = fee_rules(rid or None)
    bf, pm = int(fr["base_fee"] or 0), int(fr["per_mile"] or 0)
    base_part = min(fee, bf) if bf else fee
    pay = int(round(r["base_cents"] * (base_part / float(bf)))) if bf else min(fee, r["base_cents"])
    extra = fee - base_part
    if extra > 0:
        if pm and r.get("mile_cents") is not None:
            pay += int(round(extra * r["mile_cents"] / float(pm)))
        else:
            pay += extra * r["fee_pct"] // 100
    return pay

def drv_pay_suggest(o):
    r = drv_pay_rule()
    return (r["flat_cents"] + drv_fee_pay(o, r)
            + int(o["tip_cents"] or 0) * r["tip_pct"] // 100)

def driver_payout_target(d):
    """(wallet, recipient_type, receiver, label) or None when the driver has nothing on file."""
    if not d:
        return None
    keys = d.keys()
    wallet = ((d["payout_wallet"] if "payout_wallet" in keys else "") or "paypal").lower()
    if wallet == "check":
        return ("CHECK", "CHECK", "Check", "Check")
    if wallet == "bank":
        # only when dispatch picks "Bank transfer (record it)" for one payment; a saved
        # bank choice is no longer used, so auto pay never treats a driver as bank paid
        if "_manual_bank" in keys:
            return ("BANK", "BANK", bank_label(d), bank_label(d))
        wallet = "paypal"
    if wallet == "branch":
        wid = ((d["payout_branch_id"] if "payout_branch_id" in keys else "") or "").strip()
        if not wid:
            wid = next(iter(br_worker_ids(d).values()), "")
        if not wid:
            return None
        return ("BRANCH", "WORKER", wid, "Branch (worker " + wid + ")")
    if wallet == "venmo":
        ph = digits((d["payout_phone"] if "payout_phone" in keys else "") or d["phone"] or "")
        if len(ph) == 11 and ph.startswith("1"):
            ph = ph[1:]
        if len(ph) != 10:
            return None
        return ("VENMO", "PHONE", "+1" + ph, "Venmo (%s) %s-%s" % (ph[:3], ph[3:6], ph[6:]))
    em = ((d["payout_email"] if "payout_email" in keys else "") or "").strip()
    if "@" not in em or "." not in em.split("@")[-1]:
        return None
    return ("PAYPAL", "EMAIL", em, "PayPal " + em)

def bank_label(d):
    keys = d.keys() if d else []
    nm = ((d["bank_name"] if "bank_name" in keys else "") or "").strip()
    l4 = ((d["bank_last4"] if "bank_last4" in keys else "") or "").strip()
    return "Bank transfer" + ((" " + nm) if nm else "") + ((" ending " + l4) if l4 else "")


def pay_target_for(d, method):
    """method: '' / default = the driver's saved way, or paypal / venmo / bank for this one payment."""
    m = (method or "").lower()
    if m not in ("paypal", "venmo", "bank", "check", "branch") or not d:
        return driver_payout_target(d)
    fake = dict(d)
    fake["payout_wallet"] = m
    if m == "bank":
        fake["_manual_bank"] = 1

    class _Row(dict):
        def keys(self):
            return list(dict.keys(self))
    return driver_payout_target(_Row(fake))


def parse_cents(v):
    try:
        return int(round(float(str(v or "").replace("$", "").replace(",", "").strip()) * 100))
    except ValueError:
        return None


def drv_pay_info(o):
    try:
        rows = db().execute("SELECT * FROM driver_payouts WHERE order_id=? ORDER BY id", (o["id"],)).fetchall()
    except Exception:
        rows = []
    paid = sum(int(r["cents"]) for r in rows if (r["status"] or "") not in PAYOUT_BAD)
    d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() if o["driver_id"] else None
    t = driver_payout_target(d)
    sug = drv_pay_suggest(o)
    note = (o["auto_pay_note"] if "auto_pay_note" in o.keys() else None) or ""
    if not rows and not note and d is not None and "auto_pay" in d.keys() and d["auto_pay"] == 0:
        note = "auto pay is off for this driver"
    if not rows and not note and setting("auto_driver_pay"):
        note = (("auto pay waits for %s keys" % pay_rail_name(t[0] if t else "")) if t and not pay_rail_on(t[0], d, o) else
                ("auto pay waits for the customer's payment" if not ((o["payment_status"] or "") in ("paid", "part_refunded") or is_cash(o))
                 else "auto pay goes out about " + str(_int_setting("auto_pay_delay_min", 15, 0, 1440)) + " min after delivery"))
    return {"auto_note": note,
            "paid_cents": paid, "paid": money(paid), "suggest_cents": sug, "suggest": money(sug),
            "owed_cents": max(0, sug - paid), "to": t[3] if t else "", "ready": bool(t) and t[0] != "CHECK",
            "wallet": ((d["payout_wallet"] if d is not None and "payout_wallet" in d.keys() else "") or "paypal"),
            "open": any((r["status"] or "") in PAYOUT_OPEN for r in rows),
            "rows": [{"id": r["id"], "amount": money(r["cents"]), "status": r["status"] or "",
                      "label": PAYOUT_LABEL.get(r["status"] or "", (r["status"] or "").lower()),
                      "error": r["error"] or "", "to": r["receiver"] or "", "by": r["created_by"] or "",
                      "at": (r["created_at"] or "").replace("T", " ")[:16]} for r in rows]}

def _payout_send(row_id):
    """Send (or safely re-send, same PayPal-Request-Id) one payout row. Returns the row."""
    r = db().execute("SELECT * FROM driver_payouts WHERE id=?", (row_id,)).fetchone()
    o = db().execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone() if r["order_id"] else None
    brand = payout_brand_name(o)[:60]
    what = ("Pay for trip " + o["code"]) if o else ("Extra pay" + ((": " + r["reason"]) if r["reason"] else ""))
    short = (brand + " trip " + o["code"]) if o else (brand + " extra pay" + ((" - " + r["reason"]) if r["reason"] else ""))
    if (r["wallet"] or "") == "BRANCH":
        stat, bid, err = _branch_send(r, brand, what)
        db().execute("UPDATE driver_payouts SET batch_id=COALESCE(?,batch_id), status=?, error=?, checked_at=? WHERE id=?",
                     (bid, stat, err, dt.datetime.now().isoformat(timespec="seconds"), row_id))
        db().commit()
        return db().execute("SELECT * FROM driver_payouts WHERE id=?", (row_id,)).fetchone()
    wallet = "VENMO" if (r["wallet"] or "") == "VENMO" else "PAYPAL"
    body = {"sender_batch_header": {"sender_batch_id": r["sender_id"],
                                    "email_subject": "You have a payment from " + brand,
                                    "email_message": what[:900] + ". Thank you!"},
            "items": [{"recipient_type": "PHONE" if wallet == "VENMO" else "EMAIL",
                       "amount": {"value": "%.2f" % (int(r["cents"]) / 100.0), "currency": "USD"},
                       "receiver": r["receiver"], "note": short[:4000],
                       "sender_item_id": r["sender_id"], "recipient_wallet": wallet}]}
    acct = r["pp_acct"] if r["pp_acct"] is not None else pp_payout_acct(o)
    if r["pp_acct"] is None:
        db().execute("UPDATE driver_payouts SET pp_acct=? WHERE id=?", (int(acct or 0), row_id))
        db().commit()
    with pp_for(acct):    # the order's brand PayPal account (main keys for extra pay or brands without their own)
        st, j = pp_api("POST", "/v1/payments/payouts", body, request_id=r["sender_id"])
    now = dt.datetime.now().isoformat(timespec="seconds")
    if st in (200, 201) and (j.get("batch_header") or {}).get("payout_batch_id"):
        bh = j["batch_header"]
        db().execute("UPDATE driver_payouts SET batch_id=?, status=?, error=NULL, checked_at=? WHERE id=?",
                     (bh["payout_batch_id"], (bh.get("batch_status") or "PENDING").upper(), now, row_id))
    elif st == 0 or st >= 500:
        db().execute("UPDATE driver_payouts SET status='UNKNOWN', error=?, checked_at=? WHERE id=?",
                     ("Could not hear back from PayPal. Tap Check before paying again.", now, row_id))
    else:
        msg = pp_err(j, "PayPal did not accept the payment.")
        name = (j.get("name") or "") if isinstance(j, dict) else ""
        if name == "INSUFFICIENT_FUNDS":
            msg = "Your PayPal balance is too low to send this. Add money to PayPal and try again."
        elif name in ("AUTHORIZATION_ERROR", "NOT_AUTHORIZED", "PERMISSION_DENIED"):
            msg = "PayPal Payouts is not turned on for your account yet. Ask PayPal to enable Payouts."
        db().execute("UPDATE driver_payouts SET status='ERROR', error=?, checked_at=? WHERE id=?",
                     (msg[:300], now, row_id))
    db().commit()
    return db().execute("SELECT * FROM driver_payouts WHERE id=?", (row_id,)).fetchone()

def _payout_check(r):
    if (r["wallet"] or "") == "BRANCH":
        # asking again with the same external_id never pays twice: Branch hands back the one it has
        return _payout_send(r["id"]) if br_enabled() else r
    if r["status"] == "UNKNOWN" or (r["status"] == "SENDING" and not r["batch_id"]):
        return _payout_send(r["id"])
    if not r["batch_id"]:
        return r
    with pp_for(int(r["pp_acct"] or 0)):
        st, j = pp_api("GET", "/v1/payments/payouts/" + r["batch_id"])
    now = dt.datetime.now().isoformat(timespec="seconds")
    if st == 200:
        it = (j.get("items") or [{}])[0]
        status = (it.get("transaction_status") or (j.get("batch_header") or {}).get("batch_status") or r["status"]).upper()
        err = ((it.get("errors") or {}).get("message") or "") if status in PAYOUT_BAD else ""
        db().execute("UPDATE driver_payouts SET status=?, item_id=COALESCE(?,item_id), error=?, checked_at=? WHERE id=?",
                     (status, it.get("payout_item_id"), err[:300] or None, now, r["id"]))
    else:
        db().execute("UPDATE driver_payouts SET checked_at=? WHERE id=?", (now, r["id"]))
    db().commit()
    return db().execute("SELECT * FROM driver_payouts WHERE id=?", (r["id"],)).fetchone()

def payout_sweep(force=False):
    if not (pp_any_enabled() or br_enabled()):
        return
    if not force and time.time() - _payout_last[0] < 60:
        return
    _payout_last[0] = time.time()
    cut = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(timespec="seconds")
    try:
        for r in db().execute("""SELECT * FROM driver_payouts WHERE status IN
                                 ('SENDING','UNKNOWN','PENDING','PROCESSING','NEW','ONHOLD','UNCLAIMED')
                                 AND COALESCE(created_at,'') >= ? ORDER BY id LIMIT 25""", (cut,)).fetchall():
            if r["status"] in ("SENDING", "UNKNOWN"):
                continue        # those only go again when someone taps Check
            if not pay_rail_on(r["wallet"]):
                continue
            _payout_check(r)
    except Exception as e:
        print("payout sweep:", e)

_autopay_lock = threading.Lock()
_autopay_last = [0.0]

def _int_setting(key, default, lo, hi):
    try:
        v = int(float(setting(key, str) or default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))

def auto_driver_pay_sweep(force=False):
    """Pay each driver automatically for a delivered trip: the suggested trip pay (fee/tip share
    + flat), by PayPal or Venmo, once the customer's payment is settled and the delay has passed.
    Anything it can't do (no PayPal or Venmo on file, over the limit, a PayPal error) is left
    on the order for a dispatcher, and it never pays the same trip twice."""
    if not setting("auto_driver_pay") or not (pp_any_enabled() or br_enabled()):
        return
    if not force and time.time() - _autopay_last[0] < 60:
        return
    if not _autopay_lock.acquire(blocking=False):
        return
    try:
        _autopay_last[0] = time.time()
        delay = _int_setting("auto_pay_delay_min", 15, 0, 1440)
        cap = _int_setting("auto_pay_cap_cents", 2500, 0, 50000)
        nowd = dt.datetime.now()
        upto = (nowd - dt.timedelta(minutes=delay)).isoformat(timespec="seconds")
        since = (nowd - dt.timedelta(days=2)).isoformat(timespec="seconds")
        rows = db().execute("""SELECT * FROM orders WHERE dispatch_status='delivered' AND driver_id IS NOT NULL
                               AND auto_pay_note IS NULL AND delivered_at IS NOT NULL
                               AND delivered_at <= ? AND delivered_at >= ?
                               AND NOT EXISTS (SELECT 1 FROM driver_payouts p WHERE p.order_id=orders.id)
                               ORDER BY delivered_at LIMIT 10""", (upto, since)).fetchall()
        for o in rows:
            def note(t, oid=o["id"]):
                db().execute("UPDATE orders SET auto_pay_note=? WHERE id=?", (t, oid))
                db().commit()
            ps = o["payment_status"] or ""
            if not (ps in ("paid", "part_refunded") or is_cash(o)):
                continue            # customer's payment isn't settled yet; looked at again next time
            d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone()
            if d is not None and "auto_pay" in d.keys() and d["auto_pay"] == 0:
                note("auto pay is off for this driver, pay by hand")
                continue
            t = driver_payout_target(d) if d else None
            cents = drv_pay_suggest(o)
            if cents <= 0:
                note("nothing to pay on this trip")
                continue
            if not t:
                note("no PayPal email, Venmo phone or Branch worker ID on file, pay by hand")
                continue
            if t[0] in ("PAYPAL", "VENMO", "BRANCH") and not pay_rail_on(t[0], d, o):
                continue            # that rail's keys aren't saved yet; looked at again once they are
            if t[0] in ("BANK", "CHECK"):
                note("driver is paid by " + ("check" if t[0] == "CHECK" else "bank") + ", pay by hand")
                continue
            if cents > cap:
                note("over the auto pay limit of " + money(cap) + ", pay by hand")
                continue
            cur = db().execute("UPDATE orders SET auto_pay_note='sending' WHERE id=? AND auto_pay_note IS NULL", (o["id"],))
            db().commit()
            if cur.rowcount != 1 or db().execute("SELECT 1 FROM driver_payouts WHERE order_id=?", (o["id"],)).fetchone():
                continue            # someone else got to it
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            cur = db().execute("""INSERT INTO driver_payouts(order_id,driver_id,cents,wallet,receiver,status,created_at,created_by)
                                  VALUES(?,?,?,?,?,'SENDING',?,'Auto pay')""", (o["id"], d["id"], cents, t[0], t[2], stamp))
            pid = cur.lastrowid
            db().execute("UPDATE driver_payouts SET sender_id=? WHERE id=?", ("FD-%s-%d" % (o["code"], pid), pid))
            db().commit()
            r = _payout_send(pid)
            if r["status"] == "ERROR":
                note("auto pay failed: " + (r["error"] or (pay_rail_name(t[0]) + " error"))[:120])
            else:
                note("auto paid")
            log("driver pay", "Auto pay " + money(cents) + " to " + d["name"] + " for " + o["code"] + " (" +
                (r["status"] or "").lower() + ")")
    except Exception as e:
        print("auto driver pay:", e)
    finally:
        _autopay_lock.release()


@app.post("/api/dispatch/driver-pay")
def api_driver_pay():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (b.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if b.get("op") == "check":
        if not (pp_any_enabled() or br_enabled()):
            return jsonify({"ok": False, "error": "PayPal and Branch keys are not set up yet."}), 400
        for r in db().execute("""SELECT * FROM driver_payouts WHERE order_id=? AND status IN
                                 ('SENDING','UNKNOWN','PENDING','PROCESSING','NEW','ONHOLD','UNCLAIMED')""",
                              (o["id"],)).fetchall():
            _payout_check(r)
        return jsonify({"ok": True, "pay": drv_pay_info(db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone())})
    if o["dispatch_status"] != "delivered" or not o["driver_id"]:
        return jsonify({"ok": False, "error": "Drivers can be paid once the trip is delivered."}), 400
    d = db().execute("SELECT * FROM drivers WHERE id=?", (o["driver_id"],)).fetchone()
    t = pay_target_for(d, b.get("method"))
    if t and t[0] not in ("BANK", "CHECK") and not pay_rail_on(t[0], d, o):
        return jsonify({"ok": False, "error": ("Branch keys are not set up yet, so drivers can't be paid by Branch from here. Use Paid by check instead."
                                               if t[0] == "BRANCH" else
                                               "PayPal keys are not set up yet, so drivers can't be paid by PayPal or Venmo from here. Use Paid by check instead.")}), 400
    if not t:
        return jsonify({"ok": False, "error": (d["name"] if d else "This driver") +
                        " has no PayPal email, Venmo phone or Branch worker ID on file. Add it under Restaurants and drivers."}), 400
    try:
        cents = int(round(float(str(b.get("amount", "")).replace("$", "").replace(",", "")) * 100))
    except ValueError:
        return jsonify({"ok": False, "error": "Enter an amount like 7.50"}), 400
    if cents < 1 or cents > 50000:
        return jsonify({"ok": False, "error": "Driver pay must be between $0.01 and $500."}), 400
    info = drv_pay_info(o)
    if info["open"]:
        return jsonify({"ok": False, "error": "A payment for this trip is still going through. Tap Check first."}), 400
    if info["paid_cents"] and not b.get("extra"):
        return jsonify({"ok": False, "error": "This trip was already paid " + info["paid"] + ". Use Pay extra to send more."}), 400
    now = dt.datetime.now().isoformat(timespec="seconds")
    if t[0] in ("BANK", "CHECK"):
        ref = " ".join(str(b.get("ref") or "").split())[:60]
        st = t[0] + "_PAID"
        db().execute("""INSERT INTO driver_payouts(order_id,driver_id,cents,wallet,receiver,status,created_at,created_by,
                        kind,ref) VALUES(?,?,?,?,?,?,?,?,'trip',?)""",
                     (o["id"], d["id"], cents, t[0], t[2], st, now, session.get("dispatcher_name") or "Dispatch", ref or None))
        db().commit()
        log("driver pay", (session.get("dispatcher_name") or "Dispatch") + " recorded " + money(cents) +
            (" check pay" + (" #" + ref if ref else "") if t[0] == "CHECK" else " bank pay") + " to " + d["name"] + " for " + o["code"])
        fresh = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
        return jsonify({"ok": True, "status": st, "to": t[3], "amount": money(cents), "pay": drv_pay_info(fresh)})
    cur = db().execute("""INSERT INTO driver_payouts(order_id,driver_id,cents,wallet,receiver,status,created_at,created_by)
                          VALUES(?,?,?,?,?,'SENDING',?,?)""",
                       (o["id"], d["id"], cents, t[0], t[2], now, session.get("dispatcher_name") or "Dispatch"))
    pid = cur.lastrowid
    db().execute("UPDATE driver_payouts SET sender_id=? WHERE id=?", ("FD-%s-%d" % (o["code"], pid), pid))
    db().commit()
    r = _payout_send(pid)
    fresh = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    if r["status"] == "ERROR":
        return jsonify({"ok": False, "error": r["error"], "pay": drv_pay_info(fresh)}), 400
    return jsonify({"ok": True, "status": r["status"], "to": t[3], "amount": money(cents), "pay": drv_pay_info(fresh)})

def extra_pay_payload(driver_id=None):
    con = db()
    today = dt.date.today()
    wk = (today - dt.timedelta(days=today.weekday())).isoformat()
    drivers = []
    for d in con.execute("SELECT * FROM drivers ORDER BY name").fetchall():
        t = driver_payout_target(d)
        tot = con.execute("""SELECT COALESCE(SUM(CASE WHEN substr(created_at,1,10)>=? THEN cents END),0) wk,
                             COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN cents END),0) td
                             FROM driver_payouts WHERE driver_id=? AND COALESCE(status,'') NOT IN (%s)""" %
                          ",".join("'%s'" % x for x in PAYOUT_BAD), (wk, today.isoformat(), d["id"])).fetchone()
        drivers.append({"id": d["id"], "name": d["name"], "to": t[3] if t else "",
                        "wallet": (d["payout_wallet"] or "paypal"), "ready": bool(t),
                        "week": money(tot["wk"]), "today": money(tot["td"])})
    q = """SELECT p.*, d.name driver, o.code, o.primary_no FROM driver_payouts p JOIN drivers d ON d.id=p.driver_id
           LEFT JOIN orders o ON o.id=p.order_id"""
    args = ()
    if driver_id:
        q += " WHERE p.driver_id=?"
        args = (driver_id,)
    rows = con.execute(q + " ORDER BY p.id DESC LIMIT 100", args).fetchall()
    hist = [{"id": r["id"], "driver": r["driver"], "driver_id": r["driver_id"], "amount": money(r["cents"]),
             "kind": ("Trip " + ord_label(r["primary_no"], r["code"], "dispatch")) if r["code"] else "Extra pay",
             "reason": r["reason"] or "", "ref": r["ref"] or "", "to": r["receiver"] or r["wallet"] or "",
             "status": r["status"] or "", "open": (r["status"] or "") in PAYOUT_OPEN,
             "label": PAYOUT_LABEL.get(r["status"] or "", (r["status"] or "").lower()), "error": r["error"] or "",
             "by": r["created_by"] or "", "at": (r["created_at"] or "").replace("T", " ")[:16]} for r in rows]
    return {"ok": True, "paypal_ready": pp_any_enabled(), "drivers": drivers, "history": hist}


@app.get("/dispatch/driver-pay")
def dispatch_driver_pay_page():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_driver_pay.html")


@app.post("/api/dispatch/driver-extra")
def api_driver_extra():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    op = b.get("op") or "list"
    who = session.get("dispatcher_name") or "Dispatch"
    if op == "list":
        return jsonify(extra_pay_payload(b.get("driver_id") or None))
    if op == "check":
        r = db().execute("SELECT * FROM driver_payouts WHERE id=?", (b.get("id"),)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "That payment is not on file."}), 404
        if (r["status"] or "") in PAYOUT_OPEN and pp_any_enabled():
            _payout_check(r)
        return jsonify(extra_pay_payload(b.get("driver_id") or None))
    if op != "pay":
        return jsonify({"ok": False, "error": "Unknown action."}), 400
    d = db().execute("SELECT * FROM drivers WHERE id=?", (b.get("driver_id"),)).fetchone()
    if not d:
        return jsonify({"ok": False, "error": "Pick a driver."}), 400
    bad = out_of_scope(d["id"])
    if bad:
        return bad
    cents = parse_cents(b.get("amount"))
    if cents is None:
        return jsonify({"ok": False, "error": "Enter an amount like 25.00"}), 400
    if cents < 1 or cents > 50000:
        return jsonify({"ok": False, "error": "Extra pay must be between $0.01 and $500."}), 400
    reason = " ".join(str(b.get("reason") or "").split())[:120]
    if not reason:
        return jsonify({"ok": False, "error": "Add a reason, like: Friday bonus or gas money."}), 400
    t = pay_target_for(d, b.get("method"))
    if not t:
        return jsonify({"ok": False, "error": d["name"] + " has no PayPal email or Venmo phone on file. "
                        "Add it under Restaurants and drivers, or pick Check to record a check."}), 400
    now = dt.datetime.now().isoformat(timespec="seconds")
    if t[0] in ("BANK", "CHECK"):
        ref = " ".join(str(b.get("ref") or "").split())[:60]
        st = t[0] + "_PAID"
        db().execute("""INSERT INTO driver_payouts(order_id,driver_id,cents,wallet,receiver,status,created_at,created_by,
                        kind,reason,ref) VALUES(0,?,?,?,?,?,?,?,'extra',?,?)""",
                     (d["id"], cents, t[0], t[2], st, now, who, reason, ref or None))
        db().commit()
        log("driver pay", who + " recorded " + money(cents) + " extra " + ("check" if t[0] == "CHECK" else "bank") +
            " pay to " + d["name"] + " (" + reason + ")")
        out = extra_pay_payload(b.get("driver_id"))
        out.update({"status": st, "amount": money(cents), "to": t[3]})
        return jsonify(out)
    if not pay_rail_on(t[0], d):
        return jsonify({"ok": False, "error": "%s keys are not set up yet. Pick Check to record a check you wrote." % pay_rail_name(t[0])}), 400
    if db().execute("""SELECT 1 FROM driver_payouts WHERE driver_id=? AND kind='extra' AND status IN
                       ('SENDING','UNKNOWN') LIMIT 1""", (d["id"],)).fetchone():
        return jsonify({"ok": False, "error": "An extra payment to " + d["name"] + " is not confirmed yet. Tap Check on it first."}), 400
    cur = db().execute("""INSERT INTO driver_payouts(order_id,driver_id,cents,wallet,receiver,status,created_at,created_by,
                          kind,reason) VALUES(0,?,?,?,?,'SENDING',?,?,'extra',?)""",
                       (d["id"], cents, t[0], t[2], now, who, reason))
    pid = cur.lastrowid
    db().execute("UPDATE driver_payouts SET sender_id=? WHERE id=?", ("FD-X-%d" % pid, pid))
    db().commit()
    r = _payout_send(pid)
    log("driver pay", who + " sent " + money(cents) + " extra pay to " + d["name"] + " (" + reason + ")")
    out = extra_pay_payload(b.get("driver_id"))
    if r["status"] == "ERROR":
        out.update({"ok": False, "error": r["error"]})
        return jsonify(out), 400
    out.update({"status": r["status"], "amount": money(cents), "to": t[3]})
    return jsonify(out)


_DONE_CACHE = {}   # finished orders: {id: (row values, built dict, when)}


def done_order_dict(o):
    """Delivered/cancelled orders barely change, so reuse the last build for up to 2 minutes
    while the order row itself is unchanged. Any dispatcher action clears this (see below)."""
    key = tuple(o)
    hit = _DONE_CACHE.get(o["id"])
    if hit and hit[0] == key and time.time() - hit[2] < 120:
        return hit[1]
    d = order_dict(o)
    _DONE_CACHE[o["id"]] = (key, d, time.time())
    if len(_DONE_CACHE) > 3000:
        _DONE_CACHE.clear()
    return d


@app.after_request
def _clear_done_cache(resp):
    if request.method != "GET":
        _DONE_CACHE.clear()   # a payment, edit or refund shows on the very next refresh
    return resp


@app.get("/api/dispatch/board")
def api_board():
    backfill_primary()
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    auto_assign()   # safety net: anything an earlier event missed is placed on the next refresh
    activity_mark("dispatcher", session.get("dispatcher_id"), "active")
    activity_sync_drivers()
    credit_sweep()
    auto_kitchen_sweep()
    pp_sweep()      # charge delivered PayPal/Venmo orders once the tip window is over
    payout_sweep()  # update driver pay that is still going through PayPal
    auto_driver_pay_sweep()  # pay drivers for delivered trips when auto pay is on
    remind_unreceived()      # one automatic reminder only if a driver hasn't tapped Received in time
    short_staff_alert()      # busy: ask Unavailable drivers to come online
    purge_cards()
    try:
        purge_old_orders()
    except Exception:
        pass
    live = db().execute("""SELECT * FROM orders WHERE dispatch_status NOT IN ('delivered','cancelled','scheduled')
                           ORDER BY created_at ASC""").fetchall()
    live_all = live          # every live order, before any region filter (drivers' stops come from here)
    _od = {}
    def od(o):
        """order_dict once per order per refresh (it was being built twice for every stop)."""
        if o["id"] not in _od:
            _od[o["id"]] = order_dict(o)
        return _od[o["id"]]
    future_count = db().execute("SELECT COUNT(*) c FROM orders WHERE dispatch_status='scheduled'").fetchone()["c"]
    done_day = (request.args.get("done_day") or dt.date.today().isoformat())[:10]
    done = db().execute("""SELECT * FROM orders WHERE dispatch_status IN ('delivered','cancelled')
                           AND substr(COALESCE(delivered_at, created_at),1,10)=?
                           ORDER BY COALESCE(delivered_at, created_at) DESC LIMIT 500""", (done_day,)).fetchall()
    drivers = db().execute("""SELECT d.*, (SELECT COUNT(*) FROM orders o WHERE o.driver_id=d.id
                              AND o.dispatch_status IN ('assigned','received','at_restaurant','enroute')) load
                              FROM drivers d WHERE COALESCE(d.active,1)=1 ORDER BY d.name""").fetchall()
    lines = line_positions()
    rotation = {k: v["pos"] for k, v in lines.items()}
    lineups = region_lineups()
    unread = {r["driver_id"]: r["c"] for r in db().execute(
        """SELECT driver_id, COUNT(*) c FROM messages
           WHERE sender='driver' AND seen_by_dispatch=0 GROUP BY driver_id""").fetchall()}
    newest = db().execute(
        """SELECT m.id, m.driver_id, m.body, m.created_at, d.name FROM messages m
           JOIN drivers d ON d.id=m.driver_id
           WHERE m.sender='driver' AND m.seen_by_dispatch=0
           ORDER BY m.id DESC LIMIT 1""").fetchone()
    rests = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    my_regions = dispatcher_view_regions(session.get("dispatcher_id"))
    owner_view = is_owner()
    show_all = request.args.get("all") == "1" and owner_view
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
    if dispatcher_driver_scope() is not None:
        # dispatchers only see drivers in their own regions
        drivers = scoped_drivers(drivers)
        vis = {d["id"] for d in drivers}
        unread = {k: v for k, v in unread.items() if k in vis}
        if newest and newest["driver_id"] not in vis:
            newest = next((m for m in db().execute(
                """SELECT m.id, m.driver_id, m.body, m.created_at, d.name FROM messages m
                   JOIN drivers d ON d.id=m.driver_id
                   WHERE m.sender='driver' AND m.seen_by_dispatch=0
                   ORDER BY m.id DESC LIMIT 200""").fetchall() if m["driver_id"] in vis), None)
    return jsonify({
        "ok": True,
        "regions_label": region_names(my_regions - board_hidden_regions()) if my_regions else region_names(my_regions),
        "region_filtered": bool(my_regions),
        "showing_all": show_all,
        "is_owner": owner_view,
        # queue rows follow the regions checked in I'm working; Show all regions brings back every one
        "region_queues": region_queues(myr),
        "queues_all": not myr,
        "auto": bool(setting("auto_assign")),
        "tokens": token_list(),
        "alerts": open_call_alerts(),
        "awaiting": awaiting_accept(),
        "business_open": business_is_open(),
        "future_count": future_count,
        "late": late_accepts(),
        "rest_chat_unread": rest_chat_unread_for_dispatch()[0],
        "rest_chat_latest": rest_chat_unread_for_dispatch()[1],
        "orders": [od(o) for o in live],
        "completed": [done_order_dict(o) for o in done],
        "owner": is_owner(),
        "done_day": done_day,
        "chat_unread": sum(unread.values()),
        "chat_latest": ({"id": newest["id"], "driver_id": newest["driver_id"],
                         "driver": newest["name"], "body": newest["body"],
                         "at": newest["created_at"][11:16]} if newest else None),
        "drivers": [{"id": d["id"], "name": d["name"], "phone": d["phone"], "status": d["status"],
                     "pending_request": d["pending_request"], "load": d["load"],
                     "unread": unread.get(d["id"], 0),
                     "max_stack": d["max_stack"], "up_next": rotation.get(d["id"]),
                     "region_lines": driver_region_lines(lineups, d["id"]),
                     "work_regions": sorted(driver_work_regions(d["id"])),
                     "work_region_names": region_names(driver_work_regions(d["id"])) if all_regions() else "",
                     "region_choices": driver_region_choices(d["id"], session.get("dispatcher_id")),
                     "region_locked": sorted(driver_locked_regions(d["id"])),
                     "at_limit": (lines.get(d["id"]) or {}).get("at_limit", False),
                     "roster": d["roster"], "group": driver_group(d),
                     "today_shift": ", ".join(scheduled_today(d["id"])),
                     "availability": availability_for(d["id"]), "location": loc_block(d),
                     "status_label": status_label(d), "roster_label": roster_label(d),
                     "on_orders": [{"id": x["id"], "code": x["code"], "stage": x["dispatch_status"],
                                    "restaurant": x["restaurant"], "customer": x["customer"],
                                    "stop": x["stack_seq"]}
                                   for x in [od(y) for y in sorted(
                                       (y for y in live_all if y["driver_id"] == d["id"] and y["dispatch_status"]
                                        in ('assigned','received','at_restaurant','enroute')),
                                       key=lambda y: (y["stack_seq"] is None, y["stack_seq"] or 0))]]}
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
    did = data["driver_id"]
    bad = out_of_scope(did)
    if bad:
        return bad
    _dv = db().execute("SELECT name, active FROM drivers WHERE id=?", (did,)).fetchone()
    if status != "offline" and not business_is_open():
        return jsonify({"ok": False, "error": "The business is closed. Open the business first, then put drivers online."}), 400
    if _dv and status != "offline" and not (_dv["active"] if _dv["active"] is not None else 1):
        return jsonify({"ok": False, "error": _dv["name"] + " is inactive. Make them active on the Drivers page first."}), 400
    msg = "Dispatch set you " + status + "."
    if "regions" in data:
        drow = db().execute("SELECT name FROM drivers WHERE id=?", (did,)).fetchone()
        if not drow:
            return jsonify({"ok": False, "error": "Unknown driver."}), 404
        allowed = {c["id"] for c in driver_region_choices(did, session.get("dispatcher_id"))}
        chosen = set()
        for x in data.get("regions") or []:
            try:
                chosen.add(int(x))
            except (TypeError, ValueError):
                pass
        if chosen - allowed:
            return jsonify({"ok": False, "error": drow["name"] + " is only eligible for: " +
                            (region_names(allowed) if allowed else "no regions you work") + "."}), 400
        locked = driver_locked_regions(did)
        keep = locked - chosen if chosen else set()
        if keep:
            return jsonify({"ok": False, "error": drow["name"] + " has an order in " + region_names(keep) +
                            ", so " + region_names(keep) + " has to stay checked until it is delivered."}), 400
        if chosen:
            db().execute("INSERT OR REPLACE INTO day_picks(kind,person_id,day,region_ids) VALUES('driver_set',?,?,?)",
                         (did, dt.date.today().isoformat(), ",".join(str(x) for x in sorted(chosen))))
            where = region_names(chosen)
            msg = "Dispatch set you " + status + " in " + where + "."
        else:
            clear_dispatch_driver_pick(did)
            where = "their usual regions"
        db().commit()
        log("region", (session.get("dispatcher_name") or "dispatch") + " put " + drow["name"] + " " + status + " in " + where)
    set_driver_status(did, status, msg)
    auto_assign()
    return jsonify({"ok": True, "work_regions": sorted(driver_work_regions(did))})

@app.post("/api/dispatch/pause-region")
def api_pause_region():
    """Pause or resume a whole region. Customers can't order from its restaurants while it
    is paused, and orders already placed there hold in the queue until it is resumed."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    try:
        rid = int(b.get("region_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    r = db().execute("SELECT * FROM regions WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Unknown region."}), 404
    mine = dispatcher_view_regions(session.get("dispatcher_id"))
    if not is_owner() and mine and rid not in mine:
        return jsonify({"ok": False, "error": "You can only pause regions you are assigned to."}), 403
    want = bool(b["paused"]) if "paused" in b else not bool(r["paused"])
    who = session.get("dispatcher_name", "dispatch")
    db().execute("UPDATE regions SET paused=?, paused_by=?, paused_at=? WHERE id=?",
                 (1 if want else 0, who if want else None, now() if want else None, rid))
    db().commit()
    log("region_pause", r["name"] + (" paused by " if want else " resumed by ") + who)
    auto_assign()
    return jsonify({"ok": True, "paused": want, "region": r["name"]})


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
    rest_auto_status(rid, "Dispatch paused your restaurant. Customers can't order from you until dispatch resumes it."
              if not r["closed_override"] else "Dispatch resumed your restaurant. You are taking orders again.")
    return jsonify({"ok": True, "paused": not r["closed_override"]})


@app.post("/api/dispatch/assign")
def api_assign():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    oid, did = data["order_id"], data.get("driver_id")
    if did:
        bad = out_of_scope(did)
        if bad:
            return bad
    if did in (None, "", 0, "0"):
        db().execute("""UPDATE orders SET driver_id=NULL, stack_seq=NULL, dispatch_status='queued'
                        WHERE id=?""", (oid,))
    else:
        prev = db().execute("SELECT driver_id, dispatch_status FROM orders WHERE id=?",
                            (oid,)).fetchone()
        prev_did = prev["driver_id"] if prev else None
        if prev_did and str(prev_did) == str(did):
            return jsonify({"ok": True, "moved": False})
        orow = db().execute("SELECT region_id FROM orders WHERE id=?", (oid,)).fetchone()
        rc = region_conflict(int(did), orow["region_id"] if orow else None, oid)
        if rc:
            return jsonify({"ok": False, "error": rc}), 400
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
            auto_msg("drv_moved", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                         (prev_did, "dispatch",
                          "Order " + gone + " moved off your run to " + newname + ".", now()))
        db().execute("UPDATE drivers SET last_assigned_at=? WHERE id=?", (now(), did))
        o = db().execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
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
        if data.get("dispatch_status") == "delivered" and needs_door_signature(o):
            return jsonify({"ok": False, "need_signature": True,
                            "error": "No tip on this order, so the customer has to sign before you complete it."}), 400
        if data.get("dispatch_status") == "received" and o["dispatch_status"] == "assigned":
            rc = region_conflict(session["driver_id"], o["region_id"], o["id"])
            if rc:
                return jsonify({"ok": False, "error": "Finish your current order first. " + rc}), 400
    if o["dispatch_status"] == "delivered":
        if not session.get("dispatcher_id"):
            return jsonify({"ok": False, "error": "This order was already delivered."}), 403
        lk = delivered_lock(o)
        if lk:
            return lk
    k = data.get("kitchen_status")
    if k and dispatcher_required():
        db().execute("UPDATE orders SET kitchen_go=1 WHERE id=?", (o["id"],))
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
                auto_msg("drv_status", """INSERT INTO messages(driver_id,sender,body,created_at)
                                VALUES(?,?,?,?)""",
                             (o["driver_id"], "system",
                              "Order " + o["code"] + " marked " + d + ".", now()))
    db().commit()
    if not session.get("restaurant_id") or dispatcher_required():
        _no = rest_ord_no(o)
        _kw = {"pending": "is back to waiting for you to confirm", "preparing": "was marked preparing",
               "ready": "was marked ready"}
        _dw = {"at_restaurant": "Your driver is at the restaurant for order " + _no + ".",
               "enroute": "Order " + _no + " was picked up and is on the way to the customer.",
               "delivered": "Order " + _no + " was delivered.",
               "cancelled": "Order " + _no + " was cancelled. Please don't make it.",
               "held": "Order " + _no + " is on hold."}
        _was_k = o["kitchen_status"]
        if k in _kw and k != _was_k and dispatcher_required():
            rest_auto(o["restaurant_id"], "Order " + _no + " " + _kw[k] + " by dispatch.")
        if d in _dw and d != o["dispatch_status"]:
            rest_auto(o["restaurant_id"], _dw[d])
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
    lk = delivered_lock(o)
    if lk:
        return lk
    ref = (data.get("ref") or "").strip()
    if o["payment_status"] == "paid":
        bal = balance_cents(o)
        if bal > 0:
            last4 = "".join(ch for ch in str(data.get("last4") or "") if ch.isdigit())[-4:]
            add_extra_charge(o, bal, "Card ending " + last4 if last4 else "Recorded by dispatch", ref)
            return jsonify({"ok": True, "balance_recorded": money(bal)})
        return jsonify({"ok": True, "already": True})
    last4 = "".join(ch for ch in str(data.get("last4") or "") if ch.isdigit())[-4:]
    if data.get("method") == "house":
        acct = (data.get("account") or "").strip()[:60]
        if not acct:
            return jsonify({"ok": False, "error": "Type the business name for the house account."}), 400
        mark_paid(o, method="house_account", ref=("House account " + acct + ((" ref " + ref) if ref else "")).strip()[:80])
        return jsonify({"ok": True})
    if is_cash(o):
        method = "cash"
    elif data.get("method") == "card_keyed" or last4:
        method = "card_keyed"
    else:
        method = "recorded"
    note = ("card ending " + last4 if last4 else "") + ((" ref " + ref) if ref else "")
    mark_paid(o, method=method, ref=note.strip()[:80])
    return jsonify({"ok": True})

@app.post("/api/order/send-kitchen")
def api_send_kitchen():
    """Dispatch releases a paid (or cash) order to the kitchen."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    if o["dispatch_status"] in ("cancelled", "delivered"):
        return jsonify({"ok": False, "error": "This order is closed."}), 400
    if o["dispatch_status"] == "awaiting_payment":
        return jsonify({"ok": False, "error": "Not paid yet. Mark it paid, use House account, or switch it to Cash first."}), 400
    if o["dispatch_status"] == "scheduled":
        return jsonify({"ok": False, "error": "This is a future order. It comes to the board for sending at its release time."}), 400
    if not o["address_ok"]:
        return jsonify({"ok": False, "error": "Approve the address first."}), 400
    release_to_kitchen(o, session.get("dispatcher_name") or "dispatch")
    auto_assign()
    return jsonify({"ok": True})


@app.post("/api/order/unsend-kitchen")
def api_unsend_kitchen():
    """Dispatch pulls an order back from the kitchen (or back from call-in) before the kitchen
    confirms it. It waits on the board for Send to kitchen again. Once the kitchen confirms, it stays."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    if o["dispatch_status"] in ("cancelled", "delivered"):
        return jsonify({"ok": False, "error": "This order is closed."}), 400
    if o["dispatch_status"] == "enroute":
        return jsonify({"ok": False, "error": "The driver is already on the way with this order."}), 400
    if o["kitchen_status"] in ("preparing", "ready"):
        return jsonify({"ok": False, "error": "The kitchen already confirmed this order, so it can't be unsent."}), 400
    if o["kitchen_status"] != "pending":
        return jsonify({"ok": False, "error": "This order is not at the kitchen."}), 400
    who = session.get("dispatcher_name") or "dispatch"
    started = False
    db().execute("""UPDATE orders SET kitchen_go=0, kitchen_status='waiting', kitchen_sent_at=NULL,
                    auto_kitchen_at=COALESCE(auto_kitchen_at, ?),
                    hold_reason=CASE WHEN dispatch_status IN ('held','queued') THEN 'tap Send to kitchen' ELSE hold_reason END
                    WHERE id=? AND kitchen_status='pending'""", (now(), o["id"]))
    if db().execute("SELECT kitchen_status FROM orders WHERE id=?", (o["id"],)).fetchone()[0] != "waiting":
        db().rollback()
        return jsonify({"ok": False, "error": "The kitchen just confirmed this order, so it can't be unsent."}), 400
    # A future order that isn't due yet goes back to Future orders and comes out again at its
    # normal release time. One that is due now stays on the board waiting for Send to kitchen.
    back_to_future, unassigned = False, ""
    sf = _rv(o, "scheduled_for")
    if sf:
        try:
            due = dt.datetime.fromisoformat(str(sf)[:19])
        except Exception:
            due = None
        # "Due" follows Dispatch settings > future orders lead time: the order is due once it is
        # within that many minutes of its scheduled time.
        rel = (due - dt.timedelta(minutes=future_lead())) if due else None
        if rel and rel > dt.datetime.now():
            back_to_future = True
            if o["driver_id"] and o["dispatch_status"] in ("assigned", "received", "at_restaurant"):
                _d = db().execute("SELECT name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone()
                unassigned = _d["name"] if _d else "the driver"
                auto_msg("drv_cancelled", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                         (o["driver_id"], "system", "Order " + o["code"] + " was moved back to a future order and taken off your run.", now()))
            db().execute("""UPDATE orders SET sched_kitchen='pending', sched_dispatch='held', sched_hold='waiting on kitchen',
                            kitchen_status='scheduled', dispatch_status='scheduled', release_at=?, auto_kitchen_at=NULL,
                            driver_id=NULL, stack_seq=NULL, hold_reason=? WHERE id=?""",
                         (rel.isoformat(timespec="seconds"),
                          "future order for " + when_label(str(sf)[:19], _rv(o, "region_id")), o["id"]))
    db().commit()
    if order_uses_app(o):
        rest_auto(o["restaurant_id"], "Order " + rest_ord_no(o) + " was pulled back by dispatch. Don't start it yet." +
                  " We'll send it again when it's ready to go.")
    log("order", o["code"] + (" pulled back from the kitchen by " if order_uses_app(o) else " pulled back from call-in by ") +
        who + ((" and moved back to future orders for " + when_label(str(sf)[:19], _rv(o, "region_id"))) if back_to_future else "") +
        ((", " + unassigned + " taken off it") if unassigned else ""))
    return jsonify({"ok": True, "started": started, "future": back_to_future,
                    "when": when_label(str(sf)[:19], _rv(o, "region_id")) if back_to_future else "",
                    "unassigned": unassigned})


def release_to_kitchen(o, who):
    db().execute("UPDATE orders SET kitchen_go=1, confirm_state=CASE WHEN confirm_state='waiting' THEN 'confirmed' ELSE confirm_state END WHERE id=?", (o["id"],))
    if o["kitchen_status"] == "waiting":
        db().execute("""UPDATE orders SET kitchen_status='pending',
                        hold_reason=CASE WHEN dispatch_status IN ('held','queued') THEN 'waiting on kitchen' ELSE hold_reason END
                        WHERE id=?""", (o["id"],))
    db().commit()
    log("order", o["code"] + (" sent to the kitchen by " if order_uses_app(o) else " released to call in by ") + who)


def auto_kitchen_sweep():
    """Regions with automatic Send to kitchen: release each placed order once it could be sent
    by hand (paid or cash, address approved, not a future order, new-customer call done).
    Each order is released automatically only once, so an order dispatch pulls back stays back."""
    try:
        if not db().execute("SELECT 1 FROM regions WHERE COALESCE(auto_kitchen,0)=1 LIMIT 1").fetchone():
            return 0
        rows = db().execute("""SELECT o.* FROM orders o LEFT JOIN restaurants r ON r.id=o.restaurant_id
            LEFT JOIN regions g ON g.id=COALESCE(NULLIF(o.region_id,0), r.region_id)
            WHERE COALESCE(g.auto_kitchen,0)=1 AND COALESCE(o.kitchen_go,0)=0 AND o.kitchen_status='waiting'
              AND o.dispatch_status NOT IN ('cancelled','delivered','awaiting_payment','scheduled')
              AND COALESCE(o.address_ok,1)=1 AND COALESCE(o.confirm_state,'')<>'waiting'
              AND o.auto_kitchen_at IS NULL""").fetchall()
    except Exception as e:
        log("order", "automatic send to kitchen skipped: %s" % e)
        return 0
    for o in rows:
        db().execute("UPDATE orders SET auto_kitchen_at=? WHERE id=?", (now(), o["id"]))
        release_to_kitchen(o, "automatic send (region setting)")
    if rows:
        try:
            auto_assign()
        except Exception:
            pass
    return len(rows)

@app.post("/api/order/cash")
def api_order_cash():
    """Switch an order between cash and card. Dispatch only."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    if o["payment_status"] == "paid" and not is_cash(o):
        return jsonify({"ok": False, "error": "That order is already paid by card."}), 400
    if data.get("cash") and not cash_allowed():
        return jsonify({"ok": False, "error": "Cash orders are turned off in Settings."}), 400
    if data.get("cash"):
        db().execute("UPDATE orders SET pay_method='cash', payment_status='cash_due' WHERE id=?", (o["id"],))
        if o["dispatch_status"] == "awaiting_payment":
            kitchen = "pending" if o["address_ok"] else "waiting"
            db().execute("UPDATE orders SET kitchen_go=0 WHERE id=?", (o["id"],))
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

def order_credits(o):
    rows = db().execute("""SELECT * FROM gift_cards WHERE pay_method='credit' AND pay_ref=? AND status!='void'
                           ORDER BY id""", (o["code"],)).fetchall()
    return [{"code": g["code"], "amount": money(g["initial_cents"]), "balance": money(g["balance_cents"]),
             "balance_cents": int(g["balance_cents"])} for g in rows]

@app.post("/api/order/credit")
def api_order_credit():
    """Store credit instead of a refund. Dispatchers and owners. The customer gets a credit code
    (it works like a gift card on the website and on phone orders)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (data.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    left = int(o["paid_cents"] or 0) - int(o["refunded_cents"] or 0) - int(o["credit_cents"] or 0)
    if left <= 0:
        return jsonify({"ok": False, "error": "Nothing collected on that order is left to credit or refund."}), 400
    try:
        raw = data.get("cents")
        cents = left if raw in (None, "", "all") else int(round(float(raw)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Enter an amount."}), 400
    if cents <= 0:
        return jsonify({"ok": False, "error": "Enter an amount."}), 400
    if cents > left:
        return jsonify({"ok": False, "error": "That is more than is left on the order (" + money(left) + ")."}), 400
    note = (data.get("note") or "").strip()[:120]
    who = dispatcher_row()
    by = who["name"] if who else "dispatch"
    code = gift_new_code()
    cur = db().execute("""INSERT INTO gift_cards (code, ref, initial_cents, balance_cents, buyer_name, buyer_phone,
                          to_name, message, status, pay_method, pay_ref, sold_by, customer_id, created_at, activated_at)
                          VALUES (?,?,?,0,?,?,?,?,'active','credit',?,?,?,?,?)""",
                       (code, secrets.token_hex(8), cents, o["customer_name"], phone_digits(o["customer_phone"] or ""),
                        o["customer_name"], "Credit for order " + o["code"] + (": " + note if note else ""),
                        o["code"], by, (o["customer_id"] if "customer_id" in _okeys(o) else None), now(), now()))
    g = db().execute("SELECT * FROM gift_cards WHERE id=?", (cur.lastrowid,)).fetchone()
    gift_move(g, cents, "Credit for order " + o["code"] + (": " + note if note else ""), o["id"], by)
    db().execute("UPDATE orders SET credit_cents=credit_cents+? WHERE id=?", (cents, o["id"]))
    db().commit()
    log("refund", by + " gave " + money(cents) + " store credit on " + o["code"] + " (code ending " + code[-4:] + ")" +
        (": " + note if note else ""))
    texted = False
    if data.get("text_customer") and o["customer_phone"]:
        texted = send_text("+1" + phone_digits(o["customer_phone"])[-10:],
                           (setting("business_name", str) or "Dispatch") + ": you have a " + money(cents) +
                           " credit for order " + o["code"] + ". Use code " + code + " on your next order.")
    return jsonify({"ok": True, "code": code, "amount": money(cents), "left": money(left - cents), "texted": texted})

@app.post("/api/order/discount")
def api_order_discount():
    """Dispatch takes money off an order that was already placed: a dollar amount or a percent
    of the food. It never cuts into the driver's tip. 0 or blank removes the discount."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True) or {}
    o = db().execute("SELECT * FROM orders WHERE id=?", (data.get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    if o["dispatch_status"] == "cancelled":
        return jsonify({"ok": False, "error": "That order was cancelled."}), 400
    raw = str(data.get("value") if data.get("value") is not None else "").strip().replace("$", "")
    pct = raw.endswith("%") or data.get("kind") == "percent"
    raw = raw.rstrip("%").strip()
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        return jsonify({"ok": False, "error": "Enter dollars like 5 or a percent like 10%."}), 400
    if v < 0 or (pct and v > 100):
        return jsonify({"ok": False, "error": "Enter dollars like 5 or a percent like 10%."}), 400
    sub = int(o["subtotal_cents"] or 0)
    cents = int(round(sub * v / 100.0)) if pct else int(round(v * 100))
    cap = sub + int(o["fee_cents"] or 0) + int(o["item_fee_cents"] or 0) + int(o["tax_cents"] or 0) + \
        int((o["service_cents"] if "service_cents" in o.keys() else 0) or 0)
    capped = cents > cap
    cents = max(0, min(cents, cap))
    old = order_discount(o)
    note = (data.get("note") or "").strip()[:120]
    db().execute("UPDATE orders SET discount_cents=?, discount_note=?, total_cents=total_cents-? WHERE id=?",
                 (cents, note if cents else None, cents - old, o["id"]))
    db().commit()
    who = dispatcher_row()
    by = who["name"] if who else "dispatch"
    if cents:
        log("edit", by + " gave a " + money(cents) + " discount on " + o["code"] +
            (" (" + raw + "% of the food)" if pct else "") + (": " + note if note else ""))
    else:
        log("edit", by + " removed the discount on " + o["code"])
    o2 = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    paid = (o2["payment_status"] or "") in ("paid", "part_refunded") and int(o2["paid_cents"] or 0) > 0
    pp_hold = ((o2["pp_state"] or "") if "pp_state" in o2.keys() else "") == "authorized"
    bal = balance_cents(o2) if paid else 0
    over = (-bal) if (paid and bal < 0 and not pp_hold) else 0
    return jsonify({"ok": True, "discount": money(cents), "discount_cents": cents, "total": money(o2["total_cents"]),
                    "capped": capped, "refund_due": money(over) if over else "", "refund_due_cents": over,
                    "hold": pp_hold})

@app.post("/api/order/refund")
def api_refund():
    """Full or partial refund, dispatcher only. cents blank = everything collected."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True)
    o = db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    lk = delivered_lock(o)
    if lk:
        return lk
    paid = int(o["paid_cents"] or 0) - int(o["credit_cents"] or 0)
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
            auto_msg("drv_cancelled", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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

@app.get("/dispatch/signature/<int:oid>")
def dispatch_signature(oid):
    """The customer's signature on an order (tip or no tip). Dispatch only."""
    if not dispatcher_required():
        return redirect("/dispatch/login")
    o = db().execute("SELECT code, tip_sig_data FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return "No such order.", 404
    raw = None
    if _rv(o, "tip_sig_data"):
        try:
            raw = base64.b64decode(o["tip_sig_data"])
        except Exception:
            raw = None
    if raw is None:
        p = os.path.join(APP_DIR, "static", "signatures", o["code"] + ".png")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                raw = fh.read()
    if raw is None:
        return "No signature on file for this order.", 404
    resp = make_response(raw)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Content-Disposition"] = 'inline; filename="signature-%s.png"' % o["code"]
    resp.headers["Cache-Control"] = "private, no-store"
    return resp

def needs_door_signature(o):
    """A card order with no tip and no signature yet: the driver must get the customer to sign."""
    return not is_cash(o) and not int(o["tip_cents"] or 0) and not (o["tip_sig"] or "")

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
    sig = (data.get("signature") or "").strip()
    if not sig.startswith("data:image") or "," not in sig:
        return jsonify({"ok": False, "error": "Have the customer sign in the box first. "
                        "A signature is kept on file for every order with no tip."}), 400
    try:
        raw = base64.b64decode(sig.split(",", 1)[1])
        if len(raw) < 100 or len(raw) > 2000000:
            raise ValueError("bad size")
    except Exception:
        return jsonify({"ok": False, "error": "That signature did not save. Have the customer sign again."}), 400
    # the PNG is kept in the database (always on file, survives redeploys) and as a file when the disk allows
    try:
        folder = os.path.join(APP_DIR, "static", "signatures")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, o["code"] + ".png"), "wb") as fh:
            fh.write(raw)
    except Exception:
        pass
    sig_b64 = base64.b64encode(raw).decode("ascii")
    sig_url = "/dispatch/signature/%d" % o["id"]
    if data.get("declined"):
        db().execute("""UPDATE orders SET tip_declined=1, tip_sig=?, tip_sig_data=?, tip_signed_at=?
                        WHERE id=?""", (sig_url, sig_b64, now(), o["id"]))
        db().commit()
        log("tip", "no tip on " + o["code"] + ": customer signed, signature on file")
        return jsonify({"ok": True, "tip": money(o["tip_cents"]), "declined": True})
    cents = int(round(float(data.get("cents") or 0)))
    if cents <= 0:
        return jsonify({"ok": False, "error": "Enter a tip amount."}), 400
    if o["tip_charge_id"]:
        return jsonify({"ok": False, "error": "A tip was already signed for on this order."}), 400
    charge = ""
    db().execute("""UPDATE orders SET tip_cents=tip_cents+?, total_cents=total_cents+?,
                    tip_sig=?, tip_sig_data=?, tip_signed_at=?, tip_charge_id=?, tip_declined=0 WHERE id=?""",
                 (cents, cents, sig_url, sig_b64, now(),
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
    lk = delivered_lock(o)
    if lk:
        return lk
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
                    "primary_no": (o["primary_no"] if "primary_no" in o.keys() else "") or "",
                    "customer_name": o["customer_name"] or "", "customer_phone": o["customer_phone"] or "",
                    "address": o["address"] or "", "address_note": o["address_note"] or "",
                   "drop_style": clean_drop_style(_rv(o, "drop_style")),
                    "miles": o["miles"] or 0,
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
    lk = delivered_lock(o)
    if lk:
        return lk
    if "ref" in data:
        db().execute("UPDATE orders SET ref_code=? WHERE id=?",
                     (clean_ref(data.get("ref"), data.get("customer_phone") or o["customer_phone"]) or None, o["id"]))
    if "token" in data:
        db().execute("UPDATE orders SET token=? WHERE id=?", (clean_token(data.get("token")), o["id"]))
    # Customer details: name, phone, address can be fixed after the order is placed.
    changed, addr_msg = [], ""
    if "customer_name" in data:
        nm = " ".join(str(data.get("customer_name") or "").split())[:80]
        if not nm:
            return jsonify({"ok": False, "error": "The customer needs a name."}), 400
        if nm != (o["customer_name"] or ""):
            db().execute("UPDATE orders SET customer_name=? WHERE id=?", (nm, o["id"])); changed.append("name")
    if "customer_phone" in data:
        ph = phone_digits(data.get("customer_phone"))
        if len(ph) != 10:
            return jsonify({"ok": False, "error": "Enter a 10-digit phone number."}), 400
        if ph != phone_digits(o["customer_phone"]):
            db().execute("UPDATE orders SET customer_phone=? WHERE id=?", (ph, o["id"])); changed.append("phone")
    if "drop_style" in data:
        ds = clean_drop_style(data.get("drop_style"))
        if ds != clean_drop_style(_rv(o, "drop_style")):
            db().execute("UPDATE orders SET drop_style=? WHERE id=?", (ds, o["id"])); changed.append("hand-off")
    if "address_note" in data:
        an = str(data.get("address_note") or "").strip()[:200]
        if an != (o["address_note"] or ""):
            db().execute("UPDATE orders SET address_note=? WHERE id=?", (an, o["id"])); changed.append("apt/note")
    new_fee_from_address = None
    if "address" in data:
        ad = " ".join(str(data.get("address") or "").split())[:200]
        if not ad:
            return jsonify({"ok": False, "error": "The order needs an address."}), 400
        if ad != (o["address"] or ""):
            gq = geocode(ad)
            if gq.get("ok") and gq.get("lat") is not None:
                src_lat = o["pickup_lat"] if (o["pickup_lat"] is not None) else None
                src_lng = o["pickup_lng"] if (o["pickup_lng"] is not None) else None
                if src_lat is None:
                    rr = db().execute("SELECT lat, lng, region_id FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
                    src_lat, src_lng = (rr["lat"], rr["lng"]) if rr else (None, None)
                mi = round(haversine_miles(src_lat, src_lng, gq["lat"], gq["lng"]) * ROAD_FACTOR, 2) if src_lat is not None else (o["miles"] or 0)
                db().execute("UPDATE orders SET address=?, lat=?, lng=?, miles=?, address_ok=1 WHERE id=?",
                             (gq.get("formatted") or ad, gq["lat"], gq["lng"], mi, o["id"]))
                rid_fee = o["region_id"] if "region_id" in o.keys() else None
                new_fee_from_address = fee_for_miles(mi, rid_fee)
                addr_msg = "New address is %.1f mi away. Delivery fee for that distance is %s." % (mi, money(new_fee_from_address))
            else:
                db().execute("UPDATE orders SET address=?, address_ok=0 WHERE id=?", (ad, o["id"]))
                addr_msg = "Saved, but that address couldn't be found on the map, so the driver map and miles weren't updated."
            changed.append("address")
    if changed and data.get("recalc_fee") and new_fee_from_address is not None:
        data["fee_cents"] = new_fee_from_address
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    items = data.get("items")
    if items is None:
        items = json.loads(o["items"])
    items = clean_items(items)
    subtotal = sum(int(i["price_cents"]) * int(i["qty"]) for i in items)
    ifee = item_fees(items)
    fee = o["fee_cents"] if data.get("fee_cents") in (None, "") else int(round(float(data["fee_cents"])))
    tip = o["tip_cents"] if data.get("tip_cents") in (None, "") else int(round(float(data["tip_cents"])))
    if fee < 0 or tip < 0:
        return jsonify({"ok": False, "error": "The delivery fee and tip can't be negative."}), 400
    tax = int(round(subtotal * setting("tax_rate_bp") / 10000.0))
    service = int(round(subtotal * service_bp_for(db().execute(
        "SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()) / 10000.0))
    disc = min(order_discount(o), subtotal + fee + ifee + tax + service)
    total = subtotal + fee + ifee + tax + service + tip - disc
    db().execute("""UPDATE orders SET items=?, subtotal_cents=?, fee_cents=?, item_fee_cents=?,
                    tax_cents=?, service_cents=?, tip_cents=?, total_cents=?, discount_cents=? WHERE id=?""",
                 (json.dumps(items), subtotal, fee, ifee, tax, service, tip, total, disc, o["id"]))
    db().commit()
    who = (db().execute("SELECT name FROM dispatchers WHERE id=?", (session.get("dispatcher_id"),)).fetchone() or {"name": "dispatch"})["name"]
    log("edit", o["code"] + " edited by " + who + (" (changed customer " + ", ".join(changed) + ")" if changed else ""))
    try:
        _rch = []
        if json.dumps(clean_items(json.loads(o["items"])), sort_keys=True) != json.dumps(items, sort_keys=True):
            _rch.append("items")
        _rch += ["customer " + c for c in changed]
        if _rch:
            _lines = "; ".join("%sx %s" % (i["qty"], i["name"]) for i in items)
            rest_auto(o["restaurant_id"], "Dispatch changed order " + rest_ord_no(o) + " (" + ", ".join(_rch) + ")." +
                      (" The order is now: " + _lines + "." if "items" in _rch else "") + " Open the order to see the details.")
    except Exception as e:
        print("edit rest msg skipped", e)
    dupe = ref_in_use(clean_ref(data.get("ref"), data.get("customer_phone") or o["customer_phone"]), o["id"]) if "ref" in data else None
    house_sync(o)
    rest = pp_collect_rest(o, "changed by " + who) if (o["pp_state"] or "") == "captured" else {}
    return jsonify({"ok": True, "dupe": dupe, "changed": changed, "address_msg": addr_msg, "rest": rest, "subtotal": money(subtotal), "fee": money(fee),
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


def ordnum_view():
    """Settings > Order numbers: current choices, brand letters, restaurants for a single reset."""
    sites = db().execute("SELECT id, name FROM sites ORDER BY sort, id").fetchall() if brands_on() else []
    main = _ordset("business_name", "Main business")
    return {"on": primary_on(), "style": _ordset("primary_style", "plain"), "digits": _ordset("primary_digits", "4"),
            "reset": _ordset("primary_reset", "never"), "sec_prefix": _ordset("secondary_prefix", "FF"),
            "sec_style": _ordset("secondary_style", "time"), "sec_digits": _ordset("secondary_digits", "6"),
            "styles": PRIMARY_STYLES, "sec_styles": SECONDARY_STYLES,
            "show_choices": ORDSHOW_CHOICES,
            "show": {"dispatch": ordshow("dispatch"), "rest": ordshow("rest"), "driver": ordshow("driver")},
            "brands": [{"field": "primary_prefix_main", "name": main + " (main business)",
                        "value": _ordset("primary_prefix_main", ""), "default": default_prefix(main),
                        "example": format_primary(brand_prefix(None, main), 1)}] +
                      [{"field": "primary_prefix_%d" % x["id"], "name": x["name"],
                        "value": _ordset("primary_prefix_%d" % x["id"], ""), "default": default_prefix(x["name"]),
                        "example": format_primary(brand_prefix(x["id"], x["name"]), 1)} for x in sites],
            "restaurants": [dict(id=r["id"], name=r["name"]) for r in
                            db().execute("SELECT id, name FROM restaurants WHERE slug<>'oneoff' ORDER BY name").fetchall()]}


app.jinja_env.globals["ordnum_view"] = ordnum_view
app.jinja_env.globals["ordshow"] = ordshow


def autokitchen_view():
    return [{"id": r["id"], "label": region_label(r["id"]), "on": bool(r["auto_kitchen"])}
            for r in db().execute("SELECT id, COALESCE(auto_kitchen,0) auto_kitchen FROM regions ORDER BY name").fetchall()]


app.jinja_env.globals["autokitchen_view"] = autokitchen_view


@app.post("/api/dispatch/reset-numbering")
def api_reset_numbering():
    """Owner only: start order numbers over, e.g. after test orders."""
    if not dispatcher_required() or not is_owner(session.get("dispatcher_id")):
        return jsonify({"ok": False, "error": "Only an owner can start order numbers over."}), 403
    b = request.get_json(silent=True) or {}
    rid = int(b.get("restaurant_id") or 0) or None
    if rid and not db().execute("SELECT 1 FROM restaurants WHERE id=?", (rid,)).fetchone():
        return jsonify({"ok": False, "error": "Unknown restaurant."}), 400
    reset_numbering(rid, secondary=not rid)
    who = (db().execute("SELECT name FROM dispatchers WHERE id=?", (session.get("dispatcher_id"),)).fetchone() or {"name": ""})["name"]
    rname = db().execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()["name"] if rid else "every restaurant"
    log("settings", "%s started order numbers over for %s" % (who, rname))
    return jsonify({"ok": True, "message": "Order numbers start over at 1 for %s on the next order." % rname})


@app.get("/dispatch/new-order")
def dispatch_new_order():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    rests = db().execute("SELECT * FROM restaurants ORDER BY name").fetchall()
    src = None
    fid = request.args.get("from")
    if fid:
        o = db().execute("SELECT * FROM orders WHERE id=?", (fid,)).fetchone()
        if o and reorder_closed(o):
            flash(reorder_closed_msg(o))
            return redirect(url_for("dispatch_new_order"))
        if o:
            src = {"id": o["id"], "code": o["code"], "restaurant_id": o["restaurant_id"],
                   "customer_name": o["customer_name"], "customer_phone": o["customer_phone"],
                   "address": o["address"], "address_note": o["address_note"] or "",
                   "drop_style": clean_drop_style(_rv(o, "drop_style")),
                   "dispatch_note": o["dispatch_note"] or "",
                   "items": json.loads(o["items"]), "tip_cents": o["tip_cents"],
                   "fee_cents": o["fee_cents"], "driver_id": o["driver_id"],
                   "pay_method": _rv(o, "pay_method") or "",
                   "house_name": house_name_of(o),
                   "driver": (db().execute("SELECT name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() or {"name": ""})["name"] if o["driver_id"] else ""}
    oneoff = oneoff_id()
    multi_src = None
    mc = (request.args.get("multi") or "").strip().upper()
    if mc:
        mo = db().execute("SELECT * FROM orders WHERE code=?", (mc,)).fetchone()
        if not mo:
            mo = db().execute("SELECT * FROM orders WHERE UPPER(primary_no)=? ORDER BY id DESC LIMIT 1", (mc,)).fetchone()
        if mo:
            _mr = db().execute("SELECT region_id FROM restaurants WHERE id=?", (mo["restaurant_id"],)).fetchone()
            multi_src = {"code": mo["code"], "restaurant_id": mo["restaurant_id"], "customer_name": mo["customer_name"],
                         "customer_phone": mo["customer_phone"], "address": mo["address"],
                         "address_note": mo["address_note"] or "",
                         "drop_style": clean_drop_style(_rv(mo, "drop_style")),
                         "region_id": int((_mr["region_id"] if _mr else 0) or 0),
                         "pay_method": _rv(mo, "pay_method") or "", "house_name": house_name_of(mo)}
    shown = [r for r in rests if r["slug"] != "oneoff" and can_create_in_region(_rv(r, "region_id"))
             and not restaurant_locked(r)]   # a locked brand's restaurants can't take dispatch orders
    order_of = {x["id"]: i for i, x in enumerate(all_regions())}
    groups = {}
    for r in shown:
        rid = int(_rv(r, "region_id") or 0)
        groups.setdefault(rid, []).append(dict(r))
    rest_groups = [{"id": rid, "label": region_label(rid), "rests": groups[rid]}
                   for rid in sorted(groups, key=lambda k: (k == 0, order_of.get(k, 9999)))]
    locked_out = order_lock_on() and not is_owner() and len(shown) < len([r for r in rests if r["slug"] != "oneoff"])
    rest_region = {str(r["id"]): int(_rv(r, "region_id") or 0) for r in shown}
    # The page opens right away: menus load one restaurant at a time when it is picked
    # (/api/dispatch/menu/<id>). Only the restaurant of an order being copied comes along.
    has_menu = {}
    for row in db().execute("SELECT restaurant_id, COUNT(*) AS n FROM menu_items WHERE active=1"
                            " GROUP BY restaurant_id").fetchall():
        has_menu[str(row["restaurant_id"])] = int(row["n"] or 0)
    menus = menus_payload([src["restaurant_id"]]) if src else {}
    _lkr = set() if dev_all_brands_mode() else locked_region_ids()
    locked_brands = sorted({region_label(int(_rv(r, "region_id") or 0)) for r in rests
                            if r["slug"] != "oneoff" and int(_rv(r, "region_id") or 0) in _lkr})
    return render_template("dispatch_new_order.html", rest_groups=rest_groups, locked_out=locked_out, rest_region=rest_region,
                           locked_brands=locked_brands,
                           restaurants=[dict(r) for r in shown],
                           svc_map={str(r["id"]): service_bp_for(r) for r in shown},
                           menus=menus, has_menu=has_menu, src=src, multi_src=multi_src, multi_policy=MULTI_POLICY, oneoff=oneoff, tokens=token_list(),
                           reasons=[{"key": k, "label": v} for k, v in REDO_REASONS.items()])


@app.get("/api/dispatch/menu/<int:rid>")
def api_dispatch_menu(rid):
    """One restaurant's menu for the Create order screen, fetched when it is picked."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "items": menus_payload([rid]).get(rid, [])})


# ---------------------------------------------------------------- roster / groups

ROSTERS = ("scheduled", "unavailable")

@app.post("/api/order/note")
def api_order_note():
    """Any order can carry a dispatch note. Drivers and the kitchen both see it."""
    data = request.get_json(force=True)
    oid = data["order_id"]
    note = (data.get("note") or "").strip()
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    db().execute("UPDATE orders SET dispatch_note=? WHERE id=?", (note or None, oid))
    if note and note != (o["dispatch_note"] or "") and not (session.get("restaurant_id") and not dispatcher_required()):
        rest_auto(o["restaurant_id"], "Note added to order " + rest_ord_no(o) + ": " + note)
    did = db().execute("SELECT driver_id FROM orders WHERE id=?", (oid,)).fetchone()["driver_id"]
    if did and note:
        db().execute("INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at) VALUES(?,?,?,?,?,?)",
                     (did, "dispatch", session.get("dispatcher_name"), session.get("dispatcher_id"),
                      "Note on " + o["code"] + ": " + note, now()))
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


SOURCES = {"website": "Web", "call_in": "Call-in", "dispatch_online": "Dispatch online"}

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
                    "i_am_owner": is_owner(), "i_am_dev": is_dev(), "i_am_real_owner": is_real_owner(),
                    "dev_order_edit": dev_can_edit_orders(),
                    "dev_all_brands": str(setting("dev_all_brands") or 0) == "1",
                    "users": [{"id": r["id"], "name": r["name"], "username": r["username"],
                               "owner": bool(r["is_owner"]), "dev": bool(r["is_dev"]),
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
    if uid and is_dev(uid) and not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can change a developer account."}), 403
    if uid and is_owner(uid) and not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change an owner account's name, username or password."}), 403
    make_dev = bool(data.get("developer")) and not uid
    if make_dev and not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can create a developer account."}), 403
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
        cur = db().execute("INSERT INTO dispatchers(name,username,password,created_at,is_dev) VALUES(?,?,?,?,?)",
                           (name, username, password, now(), 1 if make_dev else 0))
        uid = cur.lastrowid
        log("dispatcher", ("created developer " if make_dev else "created ") + username)
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

@app.post("/api/dispatch/dev-order-edit")
def api_dev_order_edit():
    """Only the business's own owner accounts (not developers) can let developers change orders."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_real_owner():
        return jsonify({"ok": False, "error": "Only an owner can change this."}), 403
    on = 1 if (request.get_json(force=True) or {}).get("on") else 0
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('dev_order_edit',?)", (str(on),))
    log("dispatcher", (session.get("dispatcher_name") or "owner") + (" let developers change orders" if on else " stopped developers changing orders"))
    db().commit()
    return jsonify({"ok": True, "on": bool(on)})

@app.post("/api/dispatch/dev-all-brands")
def api_dev_all_brands():
    """Developer test switch: every brand and website on the Railway address, for developers only."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can change this."}), 403
    on = 1 if (request.get_json(force=True) or {}).get("on") else 0
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('dev_all_brands',?)", (str(on),))
    if not on:
        session.pop("cust_brand", None)
    log("dispatcher", (session.get("dispatcher_name") or "developer") + (" turned on" if on else " turned off") +
        " the developer All brands test view")
    db().commit()
    return jsonify({"ok": True, "on": bool(on)})

# --- Start fresh: a new company wipes its own copy -------------------------
# A separate company's copy of the app starts with this app's sample stores, menus and settings.
# A developer or the owner can wipe everything on THEIR copy and start empty. Dispatcher sign-ins
# for developers and the person doing it are kept so nobody gets locked out. Never allowed on the
# main platform (the copy that lists separate companies, this app's own railway.app address, or a
# copy with the Railway variable PROTECT_DATA=1).
FRESH_KEEP_TABLES = {"dispatchers", "settings"}
FRESH_TABLES = ["active_time", "applications", "availability", "blocked_customers", "branch_accounts",
                "broadcasts", "call_alerts", "card_vault", "closures", "companies", "customers", "day_picks",
                "dispatcher_availability", "dispatcher_regions", "driver_log", "driver_payouts", "driver_regions",
                "drivers", "events", "geocache", "gift_cards", "gift_txns", "menu_items", "messages",
                "option_groups", "options", "orders", "points_log", "regions", "rest_invoices", "rest_messages",
                "restaurants", "revgeo", "reviews", "saved_cards", "sites", "staff_resets", "status_log",
                "time_off", "week_submissions"]
# settings kept through a wipe: sign-in security and the developer test switch
FRESH_KEEP_SETTINGS = ("auto_secret_key", "dev_all_brands", "dev_perm", "platform_role")


def platform_host():
    return _norm_host(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or (request.host if has_request_context() else ""))


def platform_role():
    """'main' (the Fleet Foot platform that lists the other companies), 'separate' (a company's own
    copy) or '' when a developer hasn't said yet. The choice is saved with the web address it was made
    on, so a copy whose database came from another copy shows 'not set' instead of the other copy's role."""
    if (os.environ.get("PROTECT_DATA") or "").strip() == "1":
        return "main"
    raw = str(setting("platform_role", str) or "")
    role, _, host = raw.partition("|")
    if role in ("main", "separate") and host and host == platform_host():
        return role
    return ""


def start_fresh_blocked():
    """Why this copy can't be wiped, or '' when it can."""
    if (os.environ.get("PROTECT_DATA") or "").strip() == "1":
        return "This copy is protected (PROTECT_DATA is on in Railway)."
    if platform_host() in old_own_railway_hosts():
        return "This is the main Fleet Foot platform. Start fresh only works on a new company's own copy."
    role = platform_role()
    if role == "main":
        return "This copy is marked as the main Fleet Foot platform. Start fresh only works on a separate company's copy."
    if role != "separate":
        return "First mark this copy as a separate company platform under Developer access, This platform."
    return ""


@app.get("/api/dispatch/platform")
def api_platform_info():
    if not dispatcher_required() or not (is_dev() or is_owner()):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "role": platform_role(), "host": platform_host(),
                    "protected": (os.environ.get("PROTECT_DATA") or "").strip() == "1",
                    "name": setting("business_name", str) or ""})


@app.post("/api/dispatch/platform")
def api_platform_set():
    if not dispatcher_required() or not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can change this."}), 403
    if (os.environ.get("PROTECT_DATA") or "").strip() == "1":
        return jsonify({"ok": False, "error": "This copy is protected (PROTECT_DATA is on), so it stays the main platform."}), 400
    role = ((request.get_json(silent=True) or {}).get("role") or "").strip()
    if role not in ("main", "separate"):
        return jsonify({"ok": False, "error": "Pick main or separate."}), 400
    if role == "separate" and platform_host() in old_own_railway_hosts():
        return jsonify({"ok": False, "error": "This is the main Fleet Foot address, so it can't be a separate company."}), 400
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('platform_role',?)", (role + "|" + platform_host(),))
    log("dispatcher", (session.get("dispatcher_name") or "developer") + " marked this copy as the " +
        ("main Fleet Foot platform" if role == "main" else "separate company platform"))
    db().commit()
    return jsonify({"ok": True, "role": platform_role(), "host": platform_host()})


# ---------------------------------------------------------------- move a brand to its own platform
# A brand (its website, regions, restaurants and menus, drivers, dispatchers, customers and
# order history) is downloaded as one file on this platform and brought in on a company's own
# copy. PayPal and Branch keys are never put in the file: enter them again on the new copy.
BRAND_FILE_VERSION = 1
_MEDIA_RE = re.compile(r"[\w.-]+\.(?:png|jpe?g|webp|gif|svg|ico|mp3|mp4|pdf)", re.I)


def _rows(sql, params=()):
    return [dict(r) for r in db().execute(sql, params).fetchall()]


def _in(ids):
    ids = [int(i) for i in ids if i is not None]
    return (",".join("?" * len(ids)) or "NULL"), ids


def brand_package(site_id):
    con = db()
    site = con.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        return None
    out = {"version": BRAND_FILE_VERSION, "from": platform_host(), "made_at": dt.datetime.now().isoformat(timespec="seconds"),
           "sites": [dict(site)]}
    out["regions"] = _rows("SELECT * FROM regions WHERE site_id=?", (site_id,))
    q, rg = _in([r["id"] for r in out["regions"]])
    out["restaurants"] = _rows("SELECT * FROM restaurants WHERE region_id IN (%s)" % q, rg) if rg else []
    q, rs = _in([r["id"] for r in out["restaurants"]])
    out["menu_items"] = _rows("SELECT * FROM menu_items WHERE restaurant_id IN (%s)" % q, rs) if rs else []
    out["closures"] = _rows("SELECT * FROM closures WHERE restaurant_id IN (%s)" % q, rs) if rs else []
    q2, its = _in([r["id"] for r in out["menu_items"]])
    out["option_groups"] = _rows("SELECT * FROM option_groups WHERE item_id IN (%s)" % q2, its) if its else []
    q3, gs = _in([r["id"] for r in out["option_groups"]])
    out["options"] = _rows("SELECT * FROM options WHERE group_id IN (%s)" % q3, gs) if gs else []
    q, rg = _in(rg)
    out["driver_regions"] = _rows("SELECT * FROM driver_regions WHERE region_id IN (%s)" % q, rg) if rg else []
    qd, ds = _in(sorted({r["driver_id"] for r in out["driver_regions"]}))
    out["drivers"] = _rows("SELECT * FROM drivers WHERE id IN (%s)" % qd, ds) if ds else []
    out["dispatcher_regions"] = _rows("SELECT * FROM dispatcher_regions WHERE region_id IN (%s)" % q, rg) if rg else []
    qp, ps = _in(sorted({r["dispatcher_id"] for r in out["dispatcher_regions"]}))
    out["dispatchers"] = _rows("SELECT * FROM dispatchers WHERE id IN (%s) AND COALESCE(is_dev,0)=0" % qp, ps) if ps else []
    out["orders"] = _rows("SELECT * FROM orders WHERE region_id IN (%s) OR restaurant_id IN (%s)"
                          % (q, _in(rs)[0]), rg + rs) if rg else []
    qc, cs = _in(sorted({o["customer_id"] for o in out["orders"] if o.get("customer_id")}))
    out["customers"] = _rows("SELECT * FROM customers WHERE id IN (%s)" % qc, cs) if cs else []
    for o in out["orders"]:          # card and payment-processor references stay behind
        for k in list(o):
            if k.startswith(("pp_vault", "stripe_")) or k in ("save_card", "tip_sig_data"):
                o[k] = None
    for c in out["customers"]:
        c["pp_customer_id"] = None
    for d in out["drivers"]:
        d["branch_account_id"] = None
        d["payout_branch_id"] = None
        d["payout_branch_ids"] = None
    files = set()
    for t, rows in out.items():
        if isinstance(rows, list):
            for r in rows:
                for v in r.values():
                    if isinstance(v, str):
                        for m in _MEDIA_RE.findall(v):
                            if os.path.isfile(os.path.join(UPLOAD_DIR, os.path.basename(m))):
                                files.add(os.path.basename(m))
    out["files"] = sorted(files)
    return out


@app.get("/api/dispatch/brand-move")
def api_brand_move_info():
    if not dispatcher_required() or not is_dev():
        return jsonify({"ok": False}), 403
    brands = []
    for st_ in db().execute("SELECT id, name FROM sites ORDER BY sort, id").fetchall():
        regs = db().execute("SELECT id FROM regions WHERE site_id=?", (st_["id"],)).fetchall()
        q, rg = _in([r["id"] for r in regs])
        n = db().execute("SELECT COUNT(*) c FROM restaurants WHERE region_id IN (%s)" % q, rg).fetchone()["c"] if rg else 0
        brands.append({"id": st_["id"], "name": st_["name"], "regions": len(regs), "restaurants": n})
    return jsonify({"ok": True, "role": platform_role(), "brands": brands})


@app.get("/api/dispatch/brand-export")
def api_brand_export():
    """Download one brand as a file to bring in on its own Railway copy."""
    if not dispatcher_required() or not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can do this."}), 403
    try:
        sid = int(request.args.get("site_id") or 0)
    except ValueError:
        sid = 0
    pkg = brand_package(sid)
    if not pkg:
        return jsonify({"ok": False, "error": "Brand not found."}), 404
    import io, zipfile
    from flask import send_file
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("brand.json", json.dumps(pkg, default=str))
        for f in pkg["files"]:
            try:
                z.write(os.path.join(UPLOAD_DIR, f), "media/" + f)
            except OSError:
                pass
    buf.seek(0)
    name = re.sub(r"[^A-Za-z0-9]+", "-", pkg["sites"][0]["name"] or "brand").strip("-") or "brand"
    log("brand_move", "%s downloaded %s to move it to its own platform" %
        (session.get("dispatcher_name") or "developer", pkg["sites"][0]["name"]))
    return send_file(buf, as_attachment=True, mimetype="application/zip", download_name=name + "-brand.zip")


def _insert_row(table, row, cols_cache):
    cols = cols_cache.get(table)
    if cols is None:
        cols = cols_cache[table] = [c for c in dbx.columns(db(), table)]
    use = [c for c in row if c in cols and c != "id"]
    cur = db().execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(use), ",".join("?" * len(use))),
                       [row[c] for c in use])
    return cur.lastrowid


def brand_bring_in(pkg, media):
    """Add a brand file's data to this copy with new ids. Returns counts."""
    if int(pkg.get("version") or 0) != BRAND_FILE_VERSION:
        raise ValueError("That file is from a different version of the app. Update both copies and download it again.")
    con, cc, n = db(), {}, {}
    mp = {t: {} for t in ("sites", "regions", "restaurants", "menu_items", "option_groups", "drivers",
                          "dispatchers", "customers", "orders")}
    def add(t, r):
        n[t] = n.get(t, 0) + 1
        return _insert_row(t, r, cc)
    for r in pkg.get("sites", []):
        mp["sites"][r["id"]] = add("sites", r)
    for r in pkg.get("regions", []):
        r = dict(r); r["site_id"] = mp["sites"].get(r.get("site_id"))
        if con.execute("SELECT 1 FROM regions WHERE name=?", (r["name"],)).fetchone():
            r["name"] = r["name"] + " (moved)"
        mp["regions"][r["id"]] = add("regions", r)
    for r in pkg.get("restaurants", []):
        r = dict(r); r["region_id"] = mp["regions"].get(r.get("region_id"))
        base, k = r["slug"], 2
        while con.execute("SELECT 1 FROM restaurants WHERE slug=?", (r["slug"],)).fetchone():
            r["slug"] = "%s-%d" % (base, k); k += 1
        mp["restaurants"][r["id"]] = add("restaurants", r)
    for r in pkg.get("menu_items", []):
        r = dict(r); r["restaurant_id"] = mp["restaurants"].get(r["restaurant_id"])
        mp["menu_items"][r["id"]] = add("menu_items", r)
    for r in pkg.get("option_groups", []):
        r = dict(r); r["item_id"] = mp["menu_items"].get(r["item_id"])
        mp["option_groups"][r["id"]] = add("option_groups", r)
    for r in pkg.get("options", []):
        r = dict(r); r["group_id"] = mp["option_groups"].get(r["group_id"])
        add("options", r)
    for r in pkg.get("closures", []):
        r = dict(r); r["restaurant_id"] = mp["restaurants"].get(r["restaurant_id"])
        if not con.execute("SELECT 1 FROM closures WHERE restaurant_id=? AND day=?", (r["restaurant_id"], r["day"])).fetchone():
            add("closures", r)
    for r in pkg.get("drivers", []):
        ex = con.execute("SELECT id FROM drivers WHERE phone=?", (r["phone"],)).fetchone()
        mp["drivers"][r["id"]] = ex["id"] if ex else add("drivers", dict(r, status="offline"))
    for r in pkg.get("driver_regions", []):
        d, g_ = mp["drivers"].get(r["driver_id"]), mp["regions"].get(r["region_id"])
        if d and g_ and not con.execute("SELECT 1 FROM driver_regions WHERE driver_id=? AND region_id=?", (d, g_)).fetchone():
            con.execute("INSERT INTO driver_regions(driver_id, region_id) VALUES(?,?)", (d, g_))
    for r in pkg.get("dispatchers", []):
        ex = con.execute("SELECT id FROM dispatchers WHERE username=?", (r["username"],)).fetchone()
        mp["dispatchers"][r["id"]] = ex["id"] if ex else add("dispatchers", dict(r, is_dev=0))
    for r in pkg.get("dispatcher_regions", []):
        d, g_ = mp["dispatchers"].get(r["dispatcher_id"]), mp["regions"].get(r["region_id"])
        if d and g_ and not con.execute("SELECT 1 FROM dispatcher_regions WHERE dispatcher_id=? AND region_id=?", (d, g_)).fetchone():
            con.execute("INSERT INTO dispatcher_regions(dispatcher_id, region_id) VALUES(?,?)", (d, g_))
    for r in pkg.get("customers", []):
        ex = con.execute("SELECT id FROM customers WHERE phone=?", (r["phone"],)).fetchone() if r.get("phone") else None
        mp["customers"][r["id"]] = ex["id"] if ex else add("customers", r)
    for r in sorted(pkg.get("orders", []), key=lambda o: o["id"]):
        r = dict(r)
        r["restaurant_id"] = mp["restaurants"].get(r.get("restaurant_id"))
        if not r["restaurant_id"]:
            continue
        r["region_id"] = mp["regions"].get(r.get("region_id"))
        r["driver_id"] = mp["drivers"].get(r.get("driver_id"))
        r["redo_driver_id"] = mp["drivers"].get(r.get("redo_driver_id"))
        r["customer_id"] = mp["customers"].get(r.get("customer_id"))
        for k in ("rest_invoice_id", "gift_card_id", "cloned_from", "multi_with"):
            if k in r:
                r[k] = None
        if r.get("code") and con.execute("SELECT 1 FROM orders WHERE code=?", (r["code"],)).fetchone():
            continue
        mp["orders"][r["id"]] = add("orders", r)
    for name, data in media.items():
        dst = os.path.join(UPLOAD_DIR, os.path.basename(name))
        if not os.path.exists(dst):
            with open(dst, "wb") as fh:
                fh.write(data)
            n["files"] = n.get("files", 0) + 1
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('fresh_start','1')")
    return n


@app.post("/api/dispatch/brand-import")
def api_brand_import():
    """On a company's own copy: bring in a brand file downloaded from the main platform."""
    if not dispatcher_required() or not is_dev():
        return jsonify({"ok": False, "error": "Only a developer can do this."}), 403
    if platform_role() != "separate":
        return jsonify({"ok": False, "error": "Mark this copy as a separate company platform first "
                                             "(This platform, above)."}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Pick the brand file you downloaded."}), 400
    import io, zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
        pkg = json.loads(z.read("brand.json").decode("utf-8"))
        media = {n_[6:]: z.read(n_) for n_ in z.namelist()
                 if n_.startswith("media/") and len(n_) > 6 and z.getinfo(n_).file_size <= 25 * 1024 * 1024}
    except Exception:
        return jsonify({"ok": False, "error": "That isn't a brand file from Developer access."}), 400
    try:
        counts = brand_bring_in(pkg, media)
        db().commit()
    except ValueError as e:
        db().rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        db().rollback()
        return jsonify({"ok": False, "error": "Nothing was added. The file could not be read in: " + str(e)[:200]}), 400
    log("brand_move", "%s brought in %s from %s" % (session.get("dispatcher_name") or "developer",
                                                     (pkg.get("sites") or [{}])[0].get("name", "a brand"),
                                                     pkg.get("from") or "another platform"))
    db().commit()
    return jsonify({"ok": True, "counts": counts})


@app.get("/api/dispatch/start-fresh")
def api_start_fresh_info():
    if not dispatcher_required() or not (is_dev() or is_owner()):
        return jsonify({"ok": False}), 403
    counts = {}
    for t in ("orders", "restaurants", "drivers", "customers", "regions", "sites"):
        try:
            counts[t] = db().execute("SELECT COUNT(*) c FROM " + t).fetchone()[0]
        except Exception:
            counts[t] = 0
    return jsonify({"ok": True, "blocked": start_fresh_blocked(), "counts": counts})


@app.post("/api/dispatch/start-fresh")
def api_start_fresh():
    if not dispatcher_required() or not (is_dev() or is_owner()):
        return jsonify({"ok": False, "error": "Only a developer or the owner can do this."}), 403
    why = start_fresh_blocked()
    if why:
        return jsonify({"ok": False, "error": why}), 403
    f = request.get_json(silent=True) or {}
    if (f.get("confirm") or "").strip() != "DELETE EVERYTHING":
        return jsonify({"ok": False, "error": "Type DELETE EVERYTHING to confirm."}), 400
    me = session.get("dispatcher_id")
    row = db().execute("SELECT password FROM dispatchers WHERE id=?", (me,)).fetchone()
    if not row or (f.get("password") or "") != row[0]:
        return jsonify({"ok": False, "error": "That password is not right."}), 400
    new_name = (f.get("business_name") or "").strip()[:80]
    con = db()
    for t in FRESH_TABLES:
        try:
            con.execute("DELETE FROM " + t)
        except Exception as e:
            print("start fresh skipped", t, e)
    keep = ",".join("?" for _ in FRESH_KEEP_SETTINGS)
    con.execute("DELETE FROM settings WHERE key NOT IN (" + keep + ")", FRESH_KEEP_SETTINGS)
    # keep developers and the person doing this, so the copy can still be signed in to
    con.execute("DELETE FROM dispatchers WHERE COALESCE(is_dev,0)=0 AND id<>?", (me,))
    for k, v in (("fresh_start", "1"), ("store_list_loaded", "1"), ("biz_default_closed_v1", "1"),
                 ("business_open", "0"), ("dispatch_phone", ""), ("business_address", ""),
                 ("business_name", new_name or "New company")):
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit()
    # uploaded photos and logos on this copy
    removed = 0
    try:
        for fn in os.listdir(UPLOAD_DIR):
            fp = os.path.join(UPLOAD_DIR, fn)
            if os.path.isfile(fp):
                os.remove(fp)
                removed += 1
    except Exception as e:
        print("start fresh photos skipped:", e)
    # put back the default settings (fees, timers, rewards) without the sample stores
    try:
        init_db()
        con = dbx.connect(DB_PATH)
        for k, v in (("dispatch_phone", ""), ("business_address", ""), ("business_name", new_name or "New company")):
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        con.commit()
        con.close()
    except Exception as e:
        print("start fresh defaults skipped:", e)
    print("START FRESH by dispatcher", me, "photos removed", removed)
    return jsonify({"ok": True})


@app.post("/api/dispatch/user-delete")
def api_dispatch_user_delete():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    uid = request.get_json(force=True)["id"]
    if is_dev(uid):
        if not is_dev():
            return jsonify({"ok": False, "error": "Only a developer can remove a developer account."}), 403
        if db().execute("SELECT COUNT(*) c FROM dispatchers WHERE is_dev=1").fetchone()["c"] <= 1:
            return jsonify({"ok": False, "error": "This is the only developer account, so it can't be deleted. Add another developer first."}), 400
    if uid == session.get("dispatcher_id"):
        return jsonify({"ok": False, "error": "You cannot remove the account you are signed in with."}), 400
    if db().execute("SELECT COUNT(*) c FROM dispatchers").fetchone()["c"] <= 1:
        return jsonify({"ok": False, "error": "Keep at least one dispatcher account."}), 400
    if is_owner(uid) and not is_dev(uid):
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
    rows = db().execute("""SELECT m.*, COALESCE(d.is_owner,0) AS from_owner, COALESCE(d.is_dev,0) AS from_dev FROM messages m
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
                    "mine": r["dispatcher_id"] == me, "owner": bool(r["from_owner"]) and not r["from_dev"],
                    "dev": bool(r["from_dev"]),
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

# ---- Driver tracking ID (kept for older records) ----

def driver_track_id(did):
    row = db().execute("SELECT track_id FROM drivers WHERE id=?", (did,)).fetchone()
    if not row:
        return None
    if row["track_id"]:
        return row["track_id"]
    while True:
        tid = str(secrets.randbelow(90000000) + 10000000)
        if not db().execute("SELECT 1 FROM drivers WHERE track_id=?", (tid,)).fetchone():
            break
    db().execute("UPDATE drivers SET track_id=? WHERE id=?", (tid, did))
    db().commit()
    return tid


# ---- One restaurant per order. A customer who wants two restaurants places two orders;
# the second is linked to the first so dispatch can coordinate (it may still go with a different driver).
MULTI_POLICY = ("One restaurant per order. Want food from another restaurant too? Place a separate order. "
                "The order minimum and delivery fee apply to each order, and a different driver may bring it, "
                "so split the tip between your orders.")

app.jinja_env.globals["MULTI_POLICY"] = MULTI_POLICY

def house_name_of(o):
    """The house account name typed on an order ("House account John" -> "John")."""
    try:
        if (_rv(o, "pay_method") or "") != "house_account":
            return ""
        ref = str(_rv(o, "pay_ref") or "")
        n = ref[len("House account"):].strip() if ref.startswith("House account") else ""
        return n.split(" ref ")[0].strip()
    except Exception:
        return ""

def multi_resolve(code, phone, by_dispatch):
    code = (code or "").strip().upper()
    if not code:
        return "", ""
    o = db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if not o:
        # the restaurant number (TT-0003) works too
        o = db().execute("SELECT * FROM orders WHERE UPPER(primary_no)=? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    if not o or o["dispatch_status"] == "cancelled":
        return "", "We couldn't find order " + code + " to link this order to."
    if not by_dispatch and phone_digits(o["customer_phone"] or "")[-10:] != phone_digits(phone or "")[-10:]:
        return "", "Order " + code + " was placed with a different phone number."
    return (o["multi_with"] or o["code"]), ""

def multi_codes(o):
    root = (o["multi_with"] if "multi_with" in _okeys(o) else "") or o["code"]
    rows = db().execute("""SELECT code FROM orders WHERE (code=? OR multi_with=?) AND dispatch_status!='cancelled'
                           ORDER BY id""", (root, root)).fetchall()
    return [x["code"] for x in rows if x["code"] != o["code"]]

@app.post("/api/track/<code>/comment")
def api_track_comment(code):
    o = db().execute("SELECT * FROM orders WHERE code=?", (code.upper(),)).fetchone()
    if not o:
        return jsonify({"ok": False, "error": "Order not found."}), 404
    if o["dispatch_status"] in ("delivered", "cancelled"):
        return jsonify({"ok": False, "error": "This order is finished. Call dispatch if you need anything."}), 400
    if int(o["cust_comments"] or 0) >= 5:
        return jsonify({"ok": False, "error": "You've sent the most comments for this order. Please call dispatch."}), 429
    b = request.get_json(force=True) or {}
    text = " ".join((b.get("text") or "").split())[:300]
    other = (b.get("other_code") or "").strip().upper()
    if other and other != o["code"]:
        root, err = multi_resolve(other, o["customer_phone"], False)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        mine = o["multi_with"] or o["code"]
        # join the two groups under the older root
        keep, drop = sorted([root, mine])[0], sorted([root, mine])[1]
        if keep != drop:
            db().execute("UPDATE orders SET multi_with=? WHERE multi_with=? OR code=?", (keep, drop, drop))
        if o["code"] != keep:
            db().execute("UPDATE orders SET multi_with=? WHERE id=?", (keep, o["id"]))
    if b.get("multi") and not text:
        text = "This is part of a multiple order."
    if not text and not other:
        return jsonify({"ok": False, "error": "Type a comment."}), 400
    if text:
        note = ((o["address_note"] or "").strip() + " | Customer: " + text).strip(" |")[:600]
        db().execute("UPDATE orders SET address_note=?, cust_comments=cust_comments+1 WHERE id=?", (note, o["id"]))
    db().commit()
    log("order", o["code"] + " customer comment: " + (text or "") + ((" (linked to " + other + ")") if other else ""))
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    return jsonify({"ok": True, "multi_group": multi_codes(o)})

def cash_allowed():
    return (setting("allow_cash", str) or "0") == "1"

DEFAULT_GPS_HELP = ""

def gps_help_for(d):
    t = (setting("gps_help_text", str) or "").strip() or DEFAULT_GPS_HELP
    return (t.replace("{business}", setting("business_name", str) or "Dispatch")
             .replace("{id}", driver_track_id(d["id"]) or "")
             .replace("{url}", gps_server_url())
             .replace("{phone}", nice_phone(setting("dispatch_phone", str) or ""))
             .replace("{name}", (d["name"] or "").split(" ")[0]))

def gps_settings_ctx():
    ds = db().execute("SELECT id, name, phone, track_id, COALESCE(active,1) AS active FROM drivers ORDER BY name").fetchall()
    return {"drivers": [{"id": d["id"], "name": d["name"], "phone": nice_phone(d["phone"] or ""),
                         "active": bool(d["active"])} for d in ds],
            "text": (setting("gps_help_text", str) or "").strip() or DEFAULT_GPS_HELP,
            "texting": texting_on(), "allow_cash": cash_allowed()}
app.jinja_env.globals["gps_settings_ctx"] = gps_settings_ctx

def gps_server_url():
    root = request.url_root
    host = request.host.split(":")[0]
    if root.startswith("http://") and host not in ("localhost", "127.0.0.1") and not host.startswith("192.168."):
        root = "https://" + root[len("http://"):]
    return root.rstrip("/")


def _gps_ts(v):
    """ISO time, or seconds / milliseconds since 1970. Returns local time."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        if x > 1e11:
            x = x / 1000.0
        return dt.datetime.fromtimestamp(x)
    except (TypeError, ValueError):
        pass
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
        if t.tzinfo:
            t = t.astimezone().replace(tzinfo=None)
        return t
    except ValueError:
        return None


# ---- Drivers stay in the app while on a run (no Google Maps / Apple Maps) ----
def _driver_on_run(did):
    return db().execute("""SELECT code FROM orders WHERE driver_id=? AND dispatch_status NOT IN
                           ('assigned','delivered','cancelled')""", (did,)).fetchall()

@app.post("/api/driver/app-left")
def api_driver_app_left():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    runs = _driver_on_run(did)
    if not runs:
        return jsonify({"ok": True})
    db().execute("UPDATE drivers SET left_app_at=? WHERE id=?", (now(), did))
    log_driver(did, "Left the driver app during a delivery")
    db().commit()
    return jsonify({"ok": True})

@app.post("/api/driver/app-back")
def api_driver_app_back():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    b = request.get_json(silent=True) or {}
    try:
        secs = max(0, int(b.get("secs") or 0))
    except (TypeError, ValueError):
        secs = 0
    d = db().execute("SELECT name,left_app_at FROM drivers WHERE id=?", (did,)).fetchone()
    if not d or not d["left_app_at"]:
        return jsonify({"ok": True})
    db().execute("UPDATE drivers SET left_app_at=NULL WHERE id=?", (did,))
    mins = "%d min %d sec" % (secs // 60, secs % 60) if secs >= 60 else "%d sec" % secs
    log_driver(did, "Came back to the driver app after " + mins)
    if secs >= 20:
        codes = ", ".join(r["code"] for r in _driver_on_run(did))
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (did, "driver", "[Automatic] I left the driver app for " + mins + " during a delivery"
                      + (" (" + codes + ")" if codes else "") + ".", now()))
        log("driver", (d["name"] or "Driver") + " left the driver app for " + mins + " during a delivery")
    db().commit()
    return jsonify({"ok": True})

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
    for d in scoped_drivers(db().execute("SELECT * FROM drivers ORDER BY name").fetchall()):
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
        bg_on = False
        if d["last_bg_at"]:
            try:
                bg_on = (dt.datetime.now() - dt.datetime.fromisoformat(d["last_bg_at"])).total_seconds() < 900
            except ValueError:
                pass
        out.append({"id": d["id"], "name": d["name"], "phone": d["phone"], "status": d["status"],
                    "roster": d["roster"], "location": loc, "next_stop": stop,
                    "track_id": driver_track_id(d["id"]), "bg_gps": bg_on,
                    "region_ids": sorted(driver_work_regions(d["id"]))})
    me = session.get("dispatcher_id")
    mine = set() if is_owner(me) else dispatcher_view_regions(me)
    regs = [{"id": r["id"], "label": region_label(r["id"])} for r in all_regions() if not mine or r["id"] in mine]
    return jsonify({"ok": True, "drivers": out, "regions": regs})

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
        activity_mark("driver", did, None)
    auto_msg("drv_roster", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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
    rows = scoped_drivers(rows)   # dispatchers only text drivers in their regions
    if not rows:
        return jsonify({"ok": False, "error": "No drivers in your region match that group."}), 400
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


def region_closed_label_for(rids, day):
    """'Auburn closed (Staff training)' when every one of these regions is closed all day on this date,
    so nobody can put hours there. Blank when at least one region is open."""
    rids = sorted(parse_rids(rids) if not isinstance(rids, (set, list, tuple)) else rids)
    if not rids:
        return ""
    iso = day.isoformat()
    parts = []
    for rid in rids:
        if iso not in set(region_closed_dates(rid)):
            return ""
        r = _region(rid)
        why = region_closed_reasons(rid).get(iso)
        parts.append(((r["name"] if r else "Region") + " closed") + ((" (" + why + ")") if why else ""))
    return "; ".join(parts)


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
            "region_closed": region_closed_label_for(driver_region_ids(driver_id), date),
        })
    due = week_due(week_start)
    opens = week_opens(week_start)
    _rg = driver_region_ids(driver_id)
    return {"week_start": ws, "label": week_label(week_start),
            "regions_label": region_names(_rg) if _rg else "",
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
        rids, rerr = slot_regions_for("driver", did, data.get("regions"), True)
        if rerr:
            return jsonify({"ok": False, "error": rerr}), 400
        herr = slot_hours_error(dow, start, end, rids, _d)
        if herr:
            return jsonify({"ok": False, "error": herr}), 400
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,created_at,
                        week_start,region_ids) VALUES(?,?,?,?,?,'pending',?,?,?)""",
                     (did, dow, start, end, data.get("note", ""), now(), monday_of(_d).isoformat(), rids))
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
        rids, rerr = slot_regions_for("driver", did, d.get("regions"), True)
        if rerr:
            return jsonify({"ok": False, "error": rerr}), 400
        _dw = int(d.get("dow", 0))
        herr = slot_hours_error(_dw, start, end, rids, ws + dt.timedelta(days=_dw))
        if herr:
            return jsonify({"ok": False, "error": herr}), 400
        clean.append((int(d.get("dow", 0)), start, end, (d.get("note") or "").strip(), rids))
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
    rows = scoped_drivers(db().execute("SELECT * FROM drivers ORDER BY name").fetchall())
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
    for d in scoped_drivers(db().execute("SELECT * FROM drivers ORDER BY name").fetchall()):
        av, off = availability_for(d["id"]), time_off_for(d["id"])
        pending += len([a for a in av if a["status"] == "pending"])
        pending += len([o for o in off if o["status"] == "pending"])
        out.append({"id": d["id"], "name": d["name"], "phone": d["phone"],
                    "regions_label": region_names(driver_region_ids(d["id"])) if driver_region_ids(d["id"]) else "",
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
    if b.get("driver_id"):
        bad = out_of_scope(b.get("driver_id"))
        if bad:
            return bad
    if op == "decide":
        dec = "approved" if b.get("approve") else "denied"
        row = db().execute("SELECT * FROM availability WHERE id=?", (b["id"],)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        bad = out_of_scope(row["driver_id"])
        if bad:
            return bad
        db().execute("""UPDATE availability SET status=?, decided_by=?, decided_at=?, reply=?
                        WHERE id=?""", (dec, who, now(), b.get("reply", ""), b["id"]))
        auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
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
        auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
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
            rids, rerr = slot_regions_for("driver", did, b.get("regions"), False)
            if rerr:
                return jsonify({"ok": False, "error": rerr}), 400
            herr = slot_hours_error(dow, start, end, rids, day)
            if herr:
                return jsonify({"ok": False, "error": herr}), 400
            if op == "set_day":
                db().execute("DELETE FROM availability WHERE driver_id=? AND week_start=? AND dow=?",
                             (did, ws, dow))
            db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                            decided_by,decided_at,created_at,week_start,region_ids)
                            VALUES(?,?,?,?,?,'approved',?,?,?,?,?)""",
                         (did, dow, start, end, b.get("note", ""), who, now(), now(), ws, rids))
            body = ("Dispatch put you on for " + DOW_NAMES[dow] + " " + short_date(day) + " " +
                    start + "-" + end + ".")
        auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (did, "dispatch", who, body, now()))
    elif op == "add":
        dow = int(b.get("dow", 0))
        start, end = b.get("start") or "09:00", b.get("end") or "17:00"
        if end <= start:
            return jsonify({"ok": False, "error": "The end time has to be after the start time."}), 400
        rids, rerr = slot_regions_for("driver", b["driver_id"], b.get("regions"), False)
        if rerr:
            return jsonify({"ok": False, "error": rerr}), 400
        herr = slot_hours_error(dow, start, end, rids)
        if herr:
            return jsonify({"ok": False, "error": herr}), 400
        db().execute("""INSERT INTO availability(driver_id,dow,start_time,end_time,note,status,
                        decided_by,decided_at,created_at,region_ids) VALUES(?,?,?,?,?,'approved',?,?,?,?)""",
                     (b["driver_id"], dow, start, end, b.get("note", ""), who, now(), now(), rids))
        auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
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
        _udow = int(b.get("dow", row["dow"]))
        herr = slot_hours_error(_udow, start, end, row["region_ids"] if "region_ids" in row.keys() else None,
                                (dt.date.fromisoformat(row["week_start"]) + dt.timedelta(days=_udow)) if row["week_start"] else None)
        if herr:
            return jsonify({"ok": False, "error": herr}), 400
        db().execute("""UPDATE availability SET dow=?, start_time=?, end_time=?, status='approved',
                        decided_by=?, decided_at=? WHERE id=?""",
                     (int(b.get("dow", row["dow"])), start, end, who, now(), b["id"]))
        auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (row["driver_id"], "dispatch", who,
                      "Dispatch changed your " + DOW_NAMES[row["dow"]] + " hours to " +
                      start + "-" + end + ".", now()))
    elif op == "delete":
        row = db().execute("SELECT * FROM availability WHERE id=?", (b["id"],)).fetchone()
        db().execute("DELETE FROM availability WHERE id=?", (b["id"],))
        if row:
            auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
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


# ---------------------------------------------------------------- hours guard for schedules

_FULL_DAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]


def slot_hours_error(dow, start, end, rids=None, day=None):
    """Availability can only sit inside the hours the business (or the slot's region) is open.
    Returns an error message, or None when the slot fits."""
    s, e = _hm(start), _hm(end)
    if s is None or e is None:
        return None
    if e <= s:
        e += 1440
    dow = int(dow)
    targets = sorted(parse_rids(rids)) if rids else []
    for rid in (targets or [None]):
        rname = (_region(rid)["name"] if rid and _region(rid) else "The business")
        if day is not None and rid and day.isoformat() in set(region_closed_dates(rid)):
            _why = region_closed_reasons(rid).get(day.isoformat())
            return (rname + " is closed all day on " + short_date(day) + ((" (" + _why + ")") if _why else "") +
                    ", so no hours can be set that day.")
        _part = region_closed_hours(rid).get(day.isoformat()) if (day is not None and rid) else None
        if _part:
            pa, pb = _hm(_part[0]), _hm(_part[1])
            pb = 1440 if pb == 23 * 60 + 59 else pb
            if s < pb and pa < e:
                _why = region_closed_reasons(rid).get(day.isoformat())
                return (rname + " is closed " + _ampm(_part[0]) + " to " +
                        ("close" if _part[1] == "23:59" else _ampm(_part[1])) + " on " + short_date(day) +
                        ((" (" + _why + ")") if _why else "") + ". Pick hours outside that.")
        h = business_hours(rid)
        if not h:
            continue
        wins = []
        span = h.get(str(dow)) or ["", ""]
        o = _hm(span[0]) if span and span[0] else None
        c = _hm(span[1]) if span and len(span) > 1 and span[1] else None
        if o is not None and c is not None:
            wins.append((o, c if c > o else c + 1440))
        y = h.get(str((dow - 1) % 7)) or ["", ""]
        yo = _hm(y[0]) if y and y[0] else None
        yc = _hm(y[1]) if y and len(y) > 1 and y[1] else None
        if yo is not None and yc is not None and yc <= yo:
            wins.append((0, yc))
        if not any(a_ <= s and e <= b_ for a_, b_ in wins):
            if o is None or c is None:
                return rname + " is closed on " + _FULL_DAYS[dow] + ", so no hours can be set that day."
            return (rname + " is open " + _ampm(span[0]) + " to " + _ampm(span[1]) + " on " + _FULL_DAYS[dow] +
                    ". Pick hours inside that.")
    return None


# ---------------------------------------------------------------- live active time

ACTIVE_GAP_MIN = 5      # a dispatcher with no screen activity this long is counted as gone


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None


def activity_mark(kind, pid, state):
    """Record live time. kind is 'driver' or 'dispatcher'. state is 'online' or 'break'
    for drivers, 'active' for dispatchers, or None when they stop."""
    if not pid:
        return
    t = dt.datetime.now()
    row = db().execute("""SELECT * FROM active_time WHERE kind=? AND person_id=? AND ended_at IS NULL
                          ORDER BY id DESC LIMIT 1""", (kind, pid)).fetchone()
    if row:
        lb = _ts(row["last_beat"]) or t
        stale = kind == "dispatcher" and (t - lb).total_seconds() > ACTIVE_GAP_MIN * 60
        if state and row["state"] == state and not stale:
            if (t - lb).total_seconds() >= 30:
                db().execute("UPDATE active_time SET last_beat=? WHERE id=?", (t.isoformat(timespec="seconds"), row["id"]))
                db().commit()
            return
        end = row["last_beat"] if stale else t.isoformat(timespec="seconds")
        db().execute("UPDATE active_time SET ended_at=?, last_beat=? WHERE id=?", (end, end, row["id"]))
    if state:
        ts = t.isoformat(timespec="seconds")
        db().execute("INSERT INTO active_time(kind,person_id,state,started_at,last_beat) VALUES(?,?,?,?,?)",
                     (kind, pid, state, ts, ts))
    db().commit()


_LAST_DRIVER_SYNC = [0.0]


def activity_sync_drivers():
    """Safety net: line up driver time with each driver's current status (runs at most every 30 s)."""
    if time.time() - _LAST_DRIVER_SYNC[0] < 30:
        return
    _LAST_DRIVER_SYNC[0] = time.time()
    for d in db().execute("SELECT id, status FROM drivers").fetchall():
        st = d["status"] or "offline"
        activity_mark("driver", d["id"], None if st == "offline" else ("break" if "break" in st else "online"))


def _fmt_min(m):
    m = int(round(m))
    return (str(m // 60) + "h " + str(m % 60).zfill(2) + "m") if m >= 60 else (str(m) + "m")


def active_week(kind, week_start, person_id=None):
    """Per person, per day minutes of live time for one week, plus the sessions."""
    ws = dt.datetime.combine(week_start, dt.time())
    we = ws + dt.timedelta(days=7)
    t = dt.datetime.now()
    q = """SELECT * FROM active_time WHERE kind=? AND started_at < ? AND (ended_at IS NULL OR ended_at > ?)"""
    args = [kind, we.isoformat(), ws.isoformat()]
    if person_id:
        q += " AND person_id=?"
        args.append(person_id)
    rows = db().execute(q + " ORDER BY started_at", args).fetchall()
    if kind == "driver":
        people = db().execute("SELECT id, name, status FROM drivers WHERE COALESCE(active,1)=1 OR id IN (%s) ORDER BY name"
                              % (",".join(str(r["person_id"]) for r in rows) or "0")).fetchall()
    else:
        people = db().execute("SELECT id, name FROM dispatchers ORDER BY name").fetchall()
    if person_id:
        people = [p for p in people if p["id"] == int(person_id)]
    out = {}
    for p in people:
        out[p["id"]] = {"id": p["id"], "name": p["name"], "days": [{"online": 0.0, "break": 0.0} for _ in range(7)],
                        "sessions": [], "now": ""}
    for r in rows:
        if r["person_id"] not in out:
            continue
        st = _ts(r["started_at"])
        if r["ended_at"]:
            en = _ts(r["ended_at"])
            live = False
        else:
            lb = _ts(r["last_beat"]) or t
            if kind == "dispatcher" and (t - lb).total_seconds() > ACTIVE_GAP_MIN * 60:
                en, live = lb, False
            else:
                en, live = t, True
        if not st or not en or en <= st:
            continue
        key = "break" if r["state"] == "break" else "online"
        a_, b_ = max(st, ws), min(en, we)
        cur = a_
        while cur < b_:
            nxt = min(b_, dt.datetime.combine(cur.date() + dt.timedelta(days=1), dt.time()))
            out[r["person_id"]]["days"][(cur.date() - week_start).days][key] += (nxt - cur).total_seconds() / 60
            cur = nxt
        if live:
            out[r["person_id"]]["now"] = "On break" if key == "break" else ("Online" if kind == "driver" else "Active now")
        out[r["person_id"]]["sessions"].append({
            "id": r["id"], "live": live,
            "state": ("On break" if key == "break" else ("Online" if kind == "driver" else "Active")),
            "day": DOW_NAMES[st.weekday()][:3] + " " + st.strftime("%-m/%-d"),
            "start": st.strftime("%-I:%M %p"), "end": ("now" if live else en.strftime("%-I:%M %p")),
            "length": _fmt_min((en - st).total_seconds() / 60)})
    # scheduled (approved) minutes for comparison
    for pid, o in out.items():
        sched = 0
        if kind == "driver":
            srows = db().execute("""SELECT start_time, end_time FROM availability WHERE driver_id=? AND week_start=?
                                    AND COALESCE(status,'approved')='approved'""", (pid, week_start.isoformat())).fetchall()
        else:
            srows = db().execute("""SELECT start_time, end_time FROM dispatcher_availability WHERE dispatcher_id=?
                                    AND COALESCE(status,'approved')='approved'""", (pid,)).fetchall()
        for s in srows:
            s0, s1 = _hm(s["start_time"]), _hm(s["end_time"])
            if s0 is not None and s1 is not None:
                sched += (s1 - s0) if s1 > s0 else (s1 + 1440 - s0)
        tot_on = sum(d["online"] for d in o["days"])
        tot_br = sum(d["break"] for d in o["days"])
        o["days"] = [{"online": _fmt_min(d["online"]) if d["online"] >= 1 else "", "break": _fmt_min(d["break"]) if d["break"] >= 1 else ""}
                     for d in o["days"]]
        o.update({"total": _fmt_min(tot_on), "total_min": int(round(tot_on)), "break_total": _fmt_min(tot_br),
                  "scheduled": _fmt_min(sched) if sched else "", "scheduled_min": sched})
        o["sessions"].reverse()
    return {"ok": True, "kind": kind, "week_start": week_start.isoformat(),
            "week_label": week_label(week_start) if "week_label" in globals() else week_start.isoformat(),
            "days": [DOW_NAMES[i][:3] + " " + (week_start + dt.timedelta(days=i)).strftime("%-m/%-d") for i in range(7)],
            "people": sorted(out.values(), key=lambda x: (-x["total_min"], x["name"]))}


@app.get("/api/dispatch/active-time")
def api_active_time():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    kind = "dispatcher" if request.args.get("kind") == "dispatcher" else "driver"
    activity_sync_drivers()
    ws = parse_week(request.args.get("week") or monday_of(dt.date.today()).isoformat())
    me = session.get("dispatcher_id")
    pid = None if (kind == "driver" or is_owner()) else me
    out = active_week(kind, ws, pid)
    out["can_delete"] = is_owner(me)     # owners and developers
    return jsonify(out)


@app.post("/api/dispatch/active-time/delete")
def api_active_time_delete():
    """Owners and developers can delete live active time: one stretch (id), or every stretch
    for one person in one week (person_id + week)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner or developer can delete active time."}), 403
    b = request.get_json(force=True) or {}
    kind = "dispatcher" if b.get("kind") == "dispatcher" else "driver"
    who = session.get("dispatcher_name") or "dispatch"
    if b.get("id"):
        r = db().execute("SELECT * FROM active_time WHERE id=? AND kind=?", (int(b["id"]), kind)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "That stretch is already gone."}), 404
        db().execute("DELETE FROM active_time WHERE id=?", (r["id"],))
        log("active_time", "deleted " + kind + " " + str(r["person_id"]) + " stretch from " + str(r["started_at"]) + " by " + who)
        db().commit()
        return jsonify({"ok": True, "deleted": 1})
    if b.get("person_id") and b.get("week"):
        ws = parse_week(b.get("week"))
        a = dt.datetime.combine(ws, dt.time()).isoformat()
        z = (dt.datetime.combine(ws, dt.time()) + dt.timedelta(days=7)).isoformat()
        cur = db().execute("DELETE FROM active_time WHERE kind=? AND person_id=? AND started_at>=? AND started_at<?",
                           (kind, int(b["person_id"]), a, z))
        log("active_time", "deleted " + kind + " " + str(b["person_id"]) + " week of " + ws.isoformat() + " by " + who)
        db().commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    return jsonify({"ok": False, "error": "Nothing picked to delete."}), 400


@app.get("/api/driver/active-time")
def api_driver_active_time():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    ws = parse_week(request.args.get("week") or monday_of(dt.date.today()).isoformat())
    return jsonify(active_week("driver", ws, did))


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
                  "note": s["note"] or "",
                  "status": (s["status"] or "approved"), "decided_by": s["decided_by"] or "",
                  "on_now": (s["status"] or "approved") == "approved" and _disp_slot_on(s, nowdt),
                  "regions": sorted(slot_regions(s)),
                  "region_label": (region_names(slot_regions(s)) if slot_regions(s) else "")} for s in rows]
        mins = 0
        for s in rows:
            if (s["status"] or "approved") != "approved":
                continue
            a, b = _hm(s["start_time"]), _hm(s["end_time"])
            if a is not None and b is not None:
                mins += (b - a) if b > a else (1440 - a + b)
        ph = d["phone"] or ""
        out.append({"id": d["id"], "name": d["name"], "me": d["id"] == me, "slots": slots, "phone": ph,
                    "phone_label": ("(%s) %s-%s" % (ph[:3], ph[3:6], ph[6:])) if len(ph) == 10 else ph,
                    "on_now": any(x["on_now"] for x in slots), "hours": round(mins / 60, 1)})
    owner = is_owner(me)
    pend = sum(1 for d in out for x in d["slots"] if x["status"] == "pending")
    return {"ok": True, "me": me, "owner": owner, "pending": pend,
            "dispatchers": out if owner else [d for d in out if d["me"]] + [d for d in out if not d["me"]]}


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
    me = session.get("dispatcher_id")
    owner = is_owner(me)
    if b.get("op") == "decide":
        if not owner:
            return jsonify({"ok": False, "error": "Only an owner can approve dispatcher schedules."}), 403
        s = con.execute("SELECT * FROM dispatcher_availability WHERE id=?", (b.get("id"),)).fetchone()
        if not s:
            return jsonify({"ok": False, "error": "That time is already gone."}), 404
        dec = "approved" if b.get("decision") == "approve" else "denied"
        con.execute("UPDATE dispatcher_availability SET status=?, decided_by=?, decided_at=? WHERE id=?",
                    (dec, who, now(), s["id"]))
        nm = con.execute("SELECT name FROM dispatchers WHERE id=?", (s["dispatcher_id"],)).fetchone()
        log("availability", who + " " + dec + " " + (nm["name"] if nm else "a dispatcher") + "'s " +
            DOW_NAMES[s["dow"]] + " " + s["start_time"] + "-" + s["end_time"])
        con.commit()
        return jsonify(dispatcher_avail_payload())
    if b.get("op") == "phone":
        if not owner and str(b.get("dispatcher_id")) != str(me):
            return jsonify({"ok": False, "error": "You can only change your own phone number."}), 403
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
        if not owner and s["dispatcher_id"] != me:
            return jsonify({"ok": False, "error": "You can only remove your own times."}), 403
        con.execute("DELETE FROM dispatcher_availability WHERE id=?", (s["id"],))
        nm = con.execute("SELECT name FROM dispatchers WHERE id=?", (s["dispatcher_id"],)).fetchone()
        log("availability", who + " removed " + (nm["name"] if nm else "a dispatcher") + "'s " +
            DOW_NAMES[s["dow"]] + " " + s["start_time"] + "-" + s["end_time"])
        con.commit()
        return jsonify(dispatcher_avail_payload())
    did = (b.get("dispatcher_id") or me) if owner else me
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
    rids, rerr = slot_regions_for("dispatcher", d["id"], b.get("regions"),
                                  d["id"] == session.get("dispatcher_id"))
    if rerr:
        return jsonify({"ok": False, "error": rerr}), 400
    for dow in days:
        herr = slot_hours_error(dow, st, en, rids)
        if herr:
            return jsonify({"ok": False, "error": herr}), 400
    added = 0
    for dow in days:
        if con.execute("""SELECT 1 FROM dispatcher_availability WHERE dispatcher_id=? AND dow=?
                          AND start_time=? AND end_time=?""", (d["id"], dow, st, en)).fetchone():
            continue
        con.execute("""INSERT INTO dispatcher_availability(dispatcher_id,dow,start_time,end_time,note,
                       created_by,created_at,region_ids,status,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (d["id"], dow, st, en, note, who, now(), rids,
                     "approved" if owner else "pending", who if owner else None, now() if owner else None))
        added += 1
    con.commit()
    log("availability", who + " set " + d["name"] + " available " + ", ".join(DOW_NAMES[x] for x in days) +
        " " + st + "-" + en)
    out = dispatcher_avail_payload()
    out["added"] = added
    out["pending_note"] = "" if owner else "Sent to the owner for approval."
    return jsonify(out)


# ---------------------------------------------------------------- regions page

def sites_payload():
    out = []
    _home = home_brand_site()
    for s in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
        lg = (s["logo"] or "").strip()
        out.append({"id": s["id"], "name": s["name"], "phone": nice_phone(s["phone"] or "") if (s["phone"] or "").strip() else "",
                    "domains": ", ".join(site_domains(s)),
                    "logo": media_url(lg) if lg and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(lg))) else "",
                    "regions": sorted(site_region_ids(s["id"])),
                    "home": bool(_home is not None and _home["id"] == s["id"]),
                    "locked": site_locked(s["id"]),
                    "rail_off": rail_off(s["id"]),
                    "design": _design_payload(s)})
    return out


def _design_payload(s):
    d = site_design(s)
    out = {k: _hex_ok(d.get(k)) for k in BRAND_COLORS}
    out["font"] = d.get("font") or ""
    out["corners"] = d.get("corners") or ""
    for k in BRAND_TEXT_KEYS:
        out[k] = str(d.get(k) or "")
    for k in BRAND_IMAGES:
        nm = str(d.get(k) or "").strip()
        out[k] = media_url(nm) if nm and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(nm))) else ""
    return out


def save_site_design(con, sid, changes):
    """Merge changes into a brand's design. Blank text or color removes that part."""
    row = con.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
    d = site_design(row)
    for k, val in changes.items():
        if val in (None, ""):
            d.pop(k, None)
        else:
            d[k] = val
    con.execute("UPDATE sites SET design=? WHERE id=?", (json.dumps(d), sid))


def sites_edit(b, con, who):
    """Owner only: the brand sites (name, phone, web addresses) and which regions belong to each."""
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change the brand sites."}), 403
    op = b.get("op")
    if op == "site_lock" and not BRAND_LOCK_ON:
        return jsonify({"ok": False, "error": "Brand locking was removed. Every brand is live."}), 400
    if op == "site_lock":
        sid = int(b.get("id") or 0)
        srow = con.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
        if not srow:
            return jsonify({"ok": False, "error": "That brand is gone."}), 404
        on = str(b.get("locked") or "").lower() in ("1", "true", "on", "yes")
        con.execute("DELETE FROM settings WHERE key=?", ("brand_locked_%d" % sid,))
        if on:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("brand_locked_%d" % sid, "1"))
        log("site", who + (" locked " if on else " unlocked ") + srow["name"] + (" (not live)" if on else " (live)"))
        con.commit()
        return jsonify({"ok": True, **regions_payload()})
    if op in ("add_site", "edit_site"):
        name = " ".join(str(b.get("name") or "").split())[:60]
        if not name:
            return jsonify({"ok": False, "error": "Give the site a business name."}), 400
        ph = "".join(c for c in str(b.get("phone") or "") if c.isdigit())
        if len(ph) == 11 and ph.startswith("1"):
            ph = ph[1:]
        if ph and len(ph) != 10:
            return jsonify({"ok": False, "error": "Enter a 10-digit phone number."}), 400
        doms = []
        for d in re.split(r"[\s,]+", str(b.get("domains") or "")):
            d = _norm_host(d)
            if not d:
                continue
            if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
                hint = " Railway addresses use dashes, not underscores or spaces." if ("_" in d or " " in d) else ""
                return jsonify({"ok": False, "error": d + " is not a web address. Use something like tigertowntogo.com." + hint}), 400
            if d == platform_host() or d in old_own_railway_hosts():
                return jsonify({"ok": False, "error": d + " is this platform's own Railway address. Leave it out and tick "
                                "\"Show this brand on the Railway address\" instead."}), 400
            if d not in doms:
                doms.append(d)
        sid = int(b.get("id") or 0) if op == "edit_site" else 0
        if op == "edit_site" and not site_by_id(sid):
            return jsonify({"ok": False, "error": "That site is gone."}), 404
        for s in con.execute("SELECT * FROM sites WHERE id IS NOT ?", (sid or None,)).fetchall():
            if s["name"].lower() == name.lower():
                return jsonify({"ok": False, "error": "There is already a site called " + name + "."}), 400
            # another platform's Railway address never reaches this copy, so two brands moved to the
            # same Railway copy can both list it
            both = {d for d in set(doms) & set(site_domains(s)) if not d.endswith(".up.railway.app")}
            if both:
                return jsonify({"ok": False, "error": sorted(both)[0] + " already belongs to " + s["name"] + "."}), 400
        if op == "add_site":
            srt = con.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM sites").fetchone()["s"]
            cur = con.execute("INSERT INTO sites(name,phone,domains,sort,created_at) VALUES(?,?,?,?,?)",
                              (name, ph, ",".join(doms), srt, now()))
            sid = cur.lastrowid
            log("site", who + " added brand site " + name)
        else:
            con.execute("UPDATE sites SET name=?, phone=?, domains=? WHERE id=?", (name, ph, ",".join(doms), sid))
            log("site", who + " changed brand site " + name)
        rail = str(b.get("rail") or "").strip().lower()
        if rail not in ("host", "unavailable", "none"):
            rail = "host" if str(b.get("home") or "").lower() in ("1", "true", "on", "yes") else "none"
        if rail in ("host", "unavailable"):
            con.execute("DELETE FROM settings WHERE key='home_site_id'")
            con.execute("INSERT INTO settings(key,value) VALUES('home_site_id',?)", (str(sid),))
        con.execute("DELETE FROM settings WHERE key=?", ("rail_off_%d" % int(sid),))
        if rail == "unavailable":
            con.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("rail_off_%d" % int(sid), "1"))
        if rail == "host":
            log("site", who + " made " + name + " the brand on the Railway address")
        elif rail == "unavailable":
            log("site", who + " put " + name + " on the Railway address, marked unavailable there")
        con.commit()
        return jsonify({"ok": True, "id": sid, **regions_payload()})
    if op == "site_design":
        s = site_by_id(b.get("id"))
        if not s:
            return jsonify({"ok": False, "error": "That site is gone."}), 404
        ch = {}
        for k in BRAND_COLORS:
            if k in b:
                raw = str(b.get(k) or "").strip()
                hv = _hex_ok(raw)
                if raw and not hv:
                    return jsonify({"ok": False, "error": "Pick a color for " + k + " with the color box."}), 400
                ch[k] = hv
        if "font" in b:
            if (b.get("font") or "") not in BRAND_FONTS:
                return jsonify({"ok": False, "error": "Pick a font from the list."}), 400
            ch["font"] = b.get("font") or ""
        if "corners" in b:
            if (b.get("corners") or "") not in BRAND_CORNERS:
                return jsonify({"ok": False, "error": "Pick a corner style from the list."}), 400
            ch["corners"] = b.get("corners") or ""
        lim = {k: n for k, _d, n in SITE_TEXT}
        lim.update({"home_headline": 120, "business_email": 120, "business_address": 160, "social_x": 200,
                    "social_facebook": 200, "social_instagram": 200, "faq_text": 12000})
        for k in BRAND_TEXT_KEYS:
            if k in b:
                val = str(b.get(k) or "")
                val = val.strip()[:lim[k]] if k == "faq_text" else " ".join(val.split())[:lim[k]]
                if k.startswith("social_") and val and not val.startswith("http"):
                    val = "https://" + val
                ch[k] = val
        save_site_design(con, s["id"], ch)
        con.commit()
        log("site", who + " changed the design of " + s["name"])
        return jsonify({"ok": True, **regions_payload()})
    if op == "delete_site":
        s = site_by_id(b.get("id"))
        if not s:
            return jsonify({"ok": False, "error": "That site is gone."}), 404
        con.execute("UPDATE regions SET site_id=0 WHERE site_id=?", (s["id"],))
        con.execute("DELETE FROM sites WHERE id=?", (s["id"],))
        drop_media((s["logo"] or "").strip())
        for _k in BRAND_IMAGES:
            drop_media(str(site_design(s).get(_k) or "").strip())
        con.commit()
        log("site", who + " deleted brand site " + s["name"])
        return jsonify({"ok": True, **regions_payload()})
    # set_region_site
    try:
        rid = int(b.get("region_id") or 0)
        sid = int(b.get("site_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Pick a region."}), 400
    if not con.execute("SELECT 1 FROM regions WHERE id=?", (rid,)).fetchone():
        return jsonify({"ok": False, "error": "That region is gone."}), 404
    if sid and not site_by_id(sid):
        return jsonify({"ok": False, "error": "That site is gone."}), 404
    con.execute("UPDATE regions SET site_id=? WHERE id=?", (sid, rid))
    con.commit()
    log("site", who + " moved a region to " + (site_by_id(sid)["name"] if sid else "no site"))
    return jsonify({"ok": True, **regions_payload()})


@app.post("/api/dispatch/site-logo")
def api_site_logo():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change the brand sites."}), 403
    s = site_by_id(request.form.get("id"))
    if not s:
        return jsonify({"ok": False, "error": "That site is gone."}), 404
    which = request.form.get("which") or "logo"
    if which in BRAND_IMAGES:
        return _site_photo(s, which)
    old = (s["logo"] or "").strip()
    if request.form.get("op") == "reset":
        drop_media(old)
        db().execute("UPDATE sites SET logo='' WHERE id=?", (s["id"],))
        db().commit()
        return jsonify({"ok": True, "logo": ""})
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
    db().execute("UPDATE sites SET logo=? WHERE id=?", (name, s["id"]))
    db().commit()
    return jsonify({"ok": True, "logo": media_url(name)})


def _site_photo(s, which):
    """A brand's own big top photo or app section photo."""
    old = str(site_design(s).get(which) or "").strip()
    if request.form.get("op") == "reset":
        drop_media(old)
        save_site_design(db(), s["id"], {which: ""})
        db().commit()
        return jsonify({"ok": True, "url": ""})
    fs = request.files.get("photo")
    if not fs:
        return jsonify({"ok": False, "error": "Choose a picture to upload."}), 400
    raw = fs.read(PHOTO_MAX_BYTES + 1)
    if len(raw) > PHOTO_MAX_BYTES:
        return jsonify({"ok": False, "error": "That picture is over 10 MB. Pick a smaller one."}), 400
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((2000, 2000))
        name = secrets.token_hex(10) + ".jpg"
        im.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85, optimize=True)
    except Exception:
        return jsonify({"ok": False, "error": "That picture could not be read. Try a PNG or JPG."}), 400
    drop_media(old)
    save_site_design(db(), s["id"], {which: name})
    db().commit()
    return jsonify({"ok": True, "url": media_url(name)})


def regions_payload():
    con = db()
    regs = all_regions()
    return {"ok": True, "me": session.get("dispatcher_id"), "owner": is_owner(),
            "sites": sites_payload(), "fonts": BRAND_FONT_LABELS,
            "business_fees": fee_rules(None), "tz_choices": TZ_CHOICES, "app_tz": APP_TZ,
            "app_tz_name": dict(TZ_CHOICES).get(APP_TZ, APP_TZ), "business_faq": (setting("faq_text", str) or "").strip() or DEFAULT_FAQ,
            "regions": [{"id": r["id"], "name": r["name"], "site_id": (_region(r["id"])["site_id"] or 0),
                         "own_hours": bool(region_own_hours(r["id"])),
                         "min_order_cents": _rv(_region(r["id"]), "min_order_cents"),
                         "max_miles": _rv(_region(r["id"]), "max_miles"),
                         "base_fee_cents": _rv(_region(r["id"]), "base_fee_cents"),
                         "base_miles": _rv(_region(r["id"]), "base_miles"),
                         "per_mile_cents": _rv(_region(r["id"]), "per_mile_cents"),
                         "fees": fee_rules(r["id"]),
                         "tz": (_rv(_region(r["id"]), "tz") or ""), "tz_now": clock(now(), r["id"]) if region_tz(r["id"]) != APP_TZ else clock(now()),
                         "faq_text": _rv(_region(r["id"]), "faq_text") or "",
                         "stats_with": int(_rv(_region(r["id"]), "stats_with") or 0),
                         "drive_with": int(_rv(_region(r["id"]), "drive_with") or 0),
                         "phone": nice_phone((_region(r["id"])["phone"] or "")) if (_region(r["id"])["phone"] or "") else "",
                         "business_phone": nice_phone(dispatch_phone()),
                         "hours_label": business_hours_label(r["id"]) or "no hours limit",
                         "closed_label": closed_dates_label(r["id"]),
                         "restaurants": con.execute("SELECT COUNT(*) c FROM restaurants WHERE region_id=? AND slug!='oneoff'",
                                                    (r["id"],)).fetchone()["c"]} for r in regs],
            "restaurants": [{"id": r["id"], "name": r["name"], "address": r["address"] or "",
                             "region_id": r["region_id"] or 0,
                             "min_order_cents": r["min_order_cents"], "max_miles": r["max_miles"],
                             "partner": 0 if str(_rv(r, "partner")) == "0" else 1,
                             "rules": delivery_rules(r)}
                            for r in con.execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()],
            "drivers": [{"id": d["id"], "name": d["name"], "regions": sorted(driver_region_ids(d["id"]))}
                        for d in con.execute("SELECT id, name FROM drivers ORDER BY name").fetchall()],
            "dispatchers": [{"id": d["id"], "name": d["name"], "regions": sorted(dispatcher_region_ids(d["id"]))}
                            for d in con.execute("SELECT id, name FROM dispatchers ORDER BY name").fetchall()]}



_DEL_FAILS = {}   # dispatcher id -> list of wrong-password times
PAYOUT_DEAD = ("FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED", "CANCELLED")


@app.post("/api/dispatch/delete-orders")
def api_delete_orders():
    """Owner-only: wipe test orders for good. The owner re-enters their own password."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    did = session.get("dispatcher_id")
    if not is_owner(did):
        return jsonify({"ok": False, "error": "Only an owner can delete orders."}), 403
    b = request.get_json(force=True) or {}
    nowt = time.time()
    fails = [t for t in _DEL_FAILS.get(did, []) if nowt - t < 600]
    _DEL_FAILS[did] = fails
    if len(fails) >= 5:
        return jsonify({"ok": False, "error": "Too many wrong passwords. Try again in 10 minutes."}), 429
    pw = b.get("password") or ""
    me = db().execute("SELECT name, username FROM dispatchers WHERE id=? AND password=?", (did, pw)).fetchone()
    if not pw or not me:
        fails.append(nowt)
        return jsonify({"ok": False, "error": "That password is not right."}), 403
    want = set()
    for x in (b.get("ids") or []):
        try:
            want.add(int(x))
        except Exception:
            pass
    codes = [c.strip().upper() for c in re.split(r"[\s,]+", b.get("codes") or "") if c.strip()]
    missing = []
    for c in codes:
        r = db().execute("SELECT id FROM orders WHERE UPPER(code)=?", (c,)).fetchone()
        if r:
            want.add(r["id"])
        else:
            missing.append(c)
    if not want and not missing:
        return jsonify({"ok": False, "error": "Pick at least one order."}), 400
    live = pp_conf()["mode"] == "live"
    deleted, skipped = [], []
    for oid in sorted(want):
        o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            continue
        cols = o.keys()
        if "pp_state" in cols and (o["pp_state"] or "") == "authorized":
            try:
                v = pp_void(o, "order deleted")
            except Exception as e:
                v = {"ok": False, "error": str(e)[:200]}
            if not v.get("ok") and not live:
                # Sandbox holds are test money: never let a stuck test hold block deleting the order.
                log("payment", o["code"] + " sandbox hold not released before delete: " + v.get("error", ""))
            elif not v.get("ok"):
                skipped.append({"code": o["code"], "why": "PayPal hold could not be released: " + v.get("error", "")})
                continue
        if live and "pp_captured_cents" in cols and int(o["pp_captured_cents"] or 0) > 0:
            skipped.append({"code": o["code"], "why": "PayPal charged " + money(int(o["pp_captured_cents"])) +
                            ". Refund it first, or keep it for your records."})
            continue
        if live:
            paid = db().execute("SELECT COUNT(*) n FROM driver_payouts WHERE order_id=? AND UPPER(COALESCE(status,'')) NOT IN (" +
                                ",".join("?" * len(PAYOUT_DEAD)) + ")", (oid,) + PAYOUT_DEAD).fetchone()["n"]
            if paid:
                skipped.append({"code": o["code"], "why": "A driver was paid for it, so it stays for your pay records."})
                continue
        for table in ("card_vault", "status_log", "call_alerts", "driver_payouts"):
            db().execute("DELETE FROM " + table + " WHERE order_id=?", (oid,))
        db().execute("DELETE FROM orders WHERE id=?", (oid,))
        deleted.append(o["code"])
    db().commit()
    if deleted:
        log("order_delete", "%s deleted %d order(s): %s" % (me["name"] or me["username"], len(deleted), ", ".join(deleted)[:900]))
        db().commit()
    _DEL_FAILS.pop(did, None)
    return jsonify({"ok": True, "deleted": deleted, "skipped": skipped, "missing": missing})


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
    myr = dispatcher_view_regions(session.get("dispatcher_id"))
    out = []
    for o in rows:
        x = order_dict(o)
        d = db().execute("SELECT name FROM drivers WHERE id=?", (o["driver_id"],)).fetchone() if o["driver_id"] else None
        out.append({"id": o["id"], "code": o["code"], "customer": x.get("customer") or o["customer_name"],
                    "restaurant": x.get("restaurant") or "", "status": o["dispatch_status"],
                    "kitchen": o["kitchen_status"], "hold_reason": o["hold_reason"] or "",
                    "driver": d["name"] if d else "", "created": (o["created_at"] or "")[:16].replace("T", " "),
                    "region": names.get(o["region_id"] or 0, "No region"),
                    "site_name": (lambda _s: _s["name"] if _s is not None else "")(site_of_region(o["region_id"])),
                    "site_logo": site_logo_url(site_of_region(o["region_id"])),
                    "mine": covers(myr, o["region_id"]),
                    "web": (o["source"] or "") == "website",
                    "track": "/track/" + o["code"]})
    return jsonify({"ok": True, "results": out})


@app.get("/api/regions-list")
def api_regions_list():
    if not (session.get("dispatcher_id") or session.get("driver_id")):
        return jsonify({"ok": False}), 403
    stamp_regions()
    as_driver = bool(session.get("driver_id") and not session.get("dispatcher_id"))
    mine = (driver_region_ids(session["driver_id"]) if as_driver
            else dispatcher_region_ids(session.get("dispatcher_id")))
    regs = all_regions()
    if as_driver:
        regs = [r for r in regs if r["id"] in mine]   # drivers only see the regions dispatch gave them
    kind = "driver" if as_driver else "dispatcher"
    pid = session.get("driver_id") if as_driver else session.get("dispatcher_id")
    allowed = today_slot_regions(kind, pid)
    # locked = its brand is locked (schedules mark it); hidden = kept off the dispatch board
    _lk = locked_region_ids()
    _hid = board_hidden_regions() if not as_driver else set()
    names = {r["id"]: r["name"] for r in all_regions()}
    return jsonify({"ok": True, "regions": [{"id": r["id"], "name": r["name"], "drive_lead": drive_lead_of(r["id"]),
                                             "locked": r["id"] in _lk, "hidden": r["id"] in _hid} for r in regs],
                    "today_allowed": [{"id": x, "name": names[x], "drive_lead": drive_lead_of(x),
                                       "locked": x in _lk, "hidden": x in _hid} for x in sorted(allowed, key=lambda i: names[i].lower())],
                    "today_pick": sorted(day_pick(kind, pid)),
                    "mine": sorted(mine), "driver": as_driver, "none_assigned": as_driver and not mine,
                    "owner": is_owner() if session.get("dispatcher_id") else False})


@app.post("/api/day-regions")
def api_day_regions():
    """Drivers and dispatchers pick which of today's availability regions they work."""
    as_driver = bool(session.get("driver_id") and not session.get("dispatcher_id"))
    pid = session.get("driver_id") if as_driver else session.get("dispatcher_id")
    if not pid:
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    kind = "driver" if as_driver else "dispatcher"
    b = request.get_json(force=True) or {}
    err = save_day_pick(kind, pid, b.get("regions"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if as_driver:
        clear_dispatch_driver_pick(pid)
        db().commit()
    chosen = day_pick(kind, pid)
    nm = (db().execute("SELECT name FROM drivers WHERE id=?", (pid,)).fetchone()["name"] if as_driver
          else (session.get("dispatcher_name") or "dispatch"))
    log("region", nm + " is working " + (region_names(chosen) if chosen else "every region on today's availability") + " today")
    return jsonify({"ok": True, "today_pick": sorted(chosen)})


def _can_edit_region(rid):
    if is_owner():
        return True
    mine = dispatcher_view_regions(session.get("dispatcher_id"))
    return not mine or rid in mine


@app.get("/api/dispatch/region-hours")
def api_region_hours_get():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    try:
        rid = int(request.args.get("region_id") or 0)
    except ValueError:
        rid = 0
    r = _region(rid)
    if not r:
        return jsonify({"ok": False, "error": "Unknown region."}), 404
    own = bool(region_own_hours(rid))
    return jsonify({"ok": True, "region": r["name"], "own": own, "can_edit": _can_edit_region(rid),
                    "rows": business_hours_rows(rid) if own else business_hours_rows(),
                    "business_label": business_hours_label() or "no hours limit",
                    "closed_dates": region_closed_list(rid), "closed_hours": region_closed_hours(rid),
                    "closed_reasons": region_closed_reasons(rid), "days": BH_DAYS})


@app.post("/api/dispatch/region-hours")
def api_region_hours_save():
    """Set a region's own hours (or go back to the business hours) and its closed dates."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    try:
        rid = int(b.get("region_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    r = _region(rid)
    if not r:
        return jsonify({"ok": False, "error": "Unknown region."}), 404
    if not _can_edit_region(rid):
        return jsonify({"ok": False, "error": "You can only change hours for regions you are assigned to."}), 403
    hours_json = ""
    if b.get("own"):
        hrs, bad = {}, []
        src = b.get("hours") or {}
        for k, name in enumerate(BH_DAYS):
            d = src.get(str(k)) or {}
            if d.get("closed"):
                hrs[str(k)] = ["", ""]
                continue
            o = (d.get("open") or "").strip()[:5]
            c = (d.get("close") or "").strip()[:5]
            if _hm(o) is None or _hm(c) is None or _hm(o) == _hm(c):
                bad.append(name)
            else:
                hrs[str(k)] = [o, c]
        if bad:
            return jsonify({"ok": False, "error": "Give " + ", ".join(bad) + " an open and a close time, or check Closed."}), 400
        if not any(v[0] for v in hrs.values()):
            return jsonify({"ok": False, "error": "Every day is closed. Leave at least one day open, or use closed dates."}), 400
        hours_json = json.dumps(hrs)
    today = dt.date.today()
    dates = set()
    for x in b.get("closed_dates") or []:
        try:
            d = dt.date.fromisoformat(str(x).strip()[:10])
        except ValueError:
            return jsonify({"ok": False, "error": "That closed date is not a real date: " + str(x)[:12]}), 400
        if d < today:
            continue
        if d > today + dt.timedelta(days=730):
            return jsonify({"ok": False, "error": "Closed dates can be up to 2 years out."}), 400
        dates.add(d.isoformat())
    if len(dates) > 60:
        return jsonify({"ok": False, "error": "Keep it to 60 closed dates or fewer."}), 400
    part = {}
    for d, v in (b.get("closed_hours") or {}).items():
        d = str(d)[:10]
        if d not in dates or not isinstance(v, (list, tuple)) or len(v) != 2:
            continue
        a, c = str(v[0] or "").strip()[:5], str(v[1] or "").strip()[:5]
        if _hm(a) is None or _hm(c) is None:
            return jsonify({"ok": False, "error": "Give " + d + " a from and a to time, or make it all day."}), 400
        if _hm(c) <= _hm(a):
            return jsonify({"ok": False, "error": "On " + d + " the closed-until time has to be after the from time."}), 400
        part[d] = [a, c]
    why = {}
    for d, v in (b.get("closed_reasons") or {}).items():
        d, v = str(d)[:10], " ".join(str(v or "").split())[:80]
        if d in dates and v:
            why[d] = v
    old_all = set(region_closed_dates(rid))
    old_part = region_closed_hours(rid)
    db().execute("UPDATE regions SET hours=?, closed_dates=?, closed_hours=?, closed_reasons=? WHERE id=?",
                 (hours_json, ",".join(sorted(dates)), json.dumps(part), json.dumps(why), rid))
    who = session.get("dispatcher_name", "dispatch")
    # New closed dates (or changed closed hours) knock drivers' submitted hours off those days.
    changed = sorted(d for d in dates if (d not in part and d not in old_all) or (d in part and old_part.get(d) != part[d]))
    bumped = clear_closed_from_schedules(rid, changed, part, why, who) if changed else 0
    db().commit()
    log("region_hours", r["name"] + ": " + ("own hours " + business_hours_label(rid) if hours_json else "business hours") +
        ("; " + closed_dates_label(rid, 60) if dates else "") + " by " + who)
    return jsonify({"ok": True, "label": business_hours_label(rid) or "no hours limit",
                    "closed": closed_dates_label(rid, 60), "drivers_changed": bumped})


def clear_closed_from_schedules(rid, days, part, why, who):
    """A region was just closed on these dates. Every driver hour already sent in for that region
    on those dates is taken off (or trimmed around set closed hours), and the driver gets a message.
    Returns how many drivers were changed."""
    con = db()
    rname = (_region(rid) or {"name": "Your region"})["name"]
    hit = {}
    for iso in days:
        try:
            day = dt.date.fromisoformat(iso)
        except ValueError:
            continue
        ws, dow = monday_of(day).isoformat(), day.weekday()
        span = part.get(iso)
        rows = con.execute("""SELECT * FROM availability WHERE week_start=? AND dow=?
                              AND COALESCE(status,'pending')!='denied'""", (ws, dow)).fetchall()
        for a in rows:
            regs = slot_regions(a) or driver_region_ids(a["driver_id"])
            if rid not in regs:
                continue
            s_, e_ = _hm(a["start_time"]), _hm(a["end_time"])
            if s_ is None or e_ is None:
                continue
            reason = (" (" + why[iso] + ")") if why.get(iso) else ""
            when = DOW_NAMES[dow] + " " + short_date(day)
            was = a["start_time"] + "-" + a["end_time"]
            others = sorted(x for x in regs if x != rid)
            if span:
                pa, pb = _hm(span[0]), _hm(span[1])
                pb = 1440 if span[1] == "23:59" else pb
                if not (s_ < pb and pa < e_):
                    continue            # their hours don't touch the closed hours
                closed_txt = rname + " is closed " + _ampm(span[0]) + " to " + \
                    ("close" if span[1] == "23:59" else _ampm(span[1])) + " on " + when + reason
            else:
                closed_txt = rname + " is closed all day " + when + reason
            if others:
                # still works their other region(s) those hours; just drop this one
                con.execute("UPDATE availability SET region_ids=? WHERE id=?",
                            (",".join(str(x) for x in others), a["id"]))
                body = closed_txt + ". Your " + was + " hours now only cover " + region_names(set(others)) + "."
            elif span and s_ < pa:
                ne = span[0]
                con.execute("UPDATE availability SET end_time=? WHERE id=?", (ne, a["id"]))
                body = closed_txt + ". Your hours were changed from " + was + " to " + a["start_time"] + "-" + ne + "."
            elif span and e_ > pb and span[1] != "23:59":
                ns = span[1]
                con.execute("UPDATE availability SET start_time=? WHERE id=?", (ns, a["id"]))
                body = closed_txt + ". Your hours were changed from " + was + " to " + ns + "-" + a["end_time"] + "."
            else:
                con.execute("DELETE FROM availability WHERE id=?", (a["id"],))
                body = closed_txt + ". Your " + was + " hours that day were taken off your schedule."
            auto_msg("drv_schedule", "INSERT INTO messages(driver_id,sender,sender_name,body,created_at) VALUES(?,?,?,?,?)",
                     (a["driver_id"], "dispatch", who, body, now()))
            hit[a["driver_id"]] = True
            log("availability", "Closed date " + iso + " in " + rname + " changed driver " + str(a["driver_id"]) + ": " + body)
    return len(hit)


@app.post("/api/dispatch/region-phone")
def api_region_phone():
    """Set a region's own dispatch number. Blank goes back to the business number."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    try:
        rid = int(b.get("region_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    r = _region(rid)
    if not r:
        return jsonify({"ok": False, "error": "Unknown region."}), 404
    if not _can_edit_region(rid):
        return jsonify({"ok": False, "error": "You can only change the number for regions you are assigned to."}), 403
    d = "".join(c for c in str(b.get("phone") or "") if c.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if d and len(d) != 10:
        return jsonify({"ok": False, "error": "Enter a 10-digit phone number, or leave it blank to use the business number."}), 400
    db().execute("UPDATE regions SET phone=? WHERE id=?", (d, rid))
    db().commit()
    log("region_phone", r["name"] + " dispatch number " + (nice_phone(d) if d else "back to the business number") +
        " by " + session.get("dispatcher_name", "dispatch"))
    return jsonify({"ok": True, "phone": nice_phone(d) if d else "", "business": nice_phone(dispatch_phone())})


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

    if op in ("add_site", "edit_site", "delete_site", "set_region_site", "site_design", "site_lock"):
        return sites_edit(b, con, who)
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
        con.execute("UPDATE regions SET stats_with=0 WHERE stats_with=?", (rid,))
        con.execute("UPDATE regions SET drive_with=0 WHERE drive_with=?", (rid,))
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
    elif op == "drive_with":
        rid = int(b.get("id") or 0)
        tgt = int(b.get("with") or 0)
        if rid not in valid or (tgt and tgt not in valid):
            return jsonify({"ok": False, "error": "That region is gone."}), 404
        if tgt == rid:
            tgt = 0
        if tgt:
            row = con.execute("SELECT COALESCE(drive_with,0) dw FROM regions WHERE id=?", (tgt,)).fetchone()
            tgt = (row["dw"] if row and row["dw"] else tgt)   # join the area that region is already in
            if tgt == rid:
                return jsonify({"ok": False, "error": "That region already shares drivers with this one."}), 400
        con.execute("UPDATE regions SET drive_with=? WHERE id=?", (tgt, rid))
        if tgt:   # anything sharing drivers with this region now shares with the new lead too
            con.execute("UPDATE regions SET drive_with=? WHERE drive_with=?", (tgt, rid))
        g.pop("_drive_map", None)
        nm = con.execute("SELECT name FROM regions WHERE id=?", (rid,)).fetchone()["name"]
        tn = con.execute("SELECT name FROM regions WHERE id=?", (tgt,)).fetchone()["name"] if tgt else ""
        log("region", who + (" combined %s drivers with %s" % (nm, tn) if tgt else " gave %s its own drivers" % nm))
    elif op == "stats_with":
        if not is_owner():
            return jsonify({"ok": False, "error": "Only an owner can combine region statistics."}), 403
        rid = int(b.get("id") or 0)
        tgt = int(b.get("with") or 0)
        if rid not in valid or (tgt and tgt not in valid):
            return jsonify({"ok": False, "error": "That region is gone."}), 404
        if tgt == rid:
            tgt = 0
        if tgt:
            tgt = stats_root(tgt)
            if tgt == rid:   # picking one of my own members: make it count with me instead
                return jsonify({"ok": False, "error": "That region already counts its statistics with this one."}), 400
        con.execute("UPDATE regions SET stats_with=? WHERE id=?", (tgt, rid))
        if tgt:   # anything counted with this region now counts with the new one too
            con.execute("UPDATE regions SET stats_with=? WHERE stats_with=?", (tgt, rid))
        _shift_cache.clear()
        nm = con.execute("SELECT name FROM regions WHERE id=?", (rid,)).fetchone()["name"]
        tn = con.execute("SELECT name FROM regions WHERE id=?", (tgt,)).fetchone()["name"] if tgt else ""
        log("region", who + (" counts %s statistics with %s" % (nm, tn) if tgt else " counts %s statistics on its own" % nm))
    elif op == "set_self":
        me = session.get("dispatcher_id")
        if not me:
            return jsonify({"ok": False, "error": "Sign in again."}), 403
        if not is_owner(me):
            return jsonify({"ok": False, "error": "Only an owner can change which regions a dispatcher works."}), 403
        chosen = ids(b.get("regions"))
        con.execute("DELETE FROM dispatcher_regions WHERE dispatcher_id=?", (me,))
        for x in chosen:
            con.execute("INSERT INTO dispatcher_regions(dispatcher_id,region_id) VALUES(?,?)", (me, x))
        log("region", who + " picked " + region_names(chosen) + " for themselves")
    elif op in ("set_driver", "set_dispatcher"):
        if op == "set_dispatcher" and not is_owner():
            return jsonify({"ok": False, "error": "Only an owner can change which regions a dispatcher works."}), 403
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


@app.get("/dispatch/dispatcher-schedule")
def dispatcher_schedule_page():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    return render_template("dispatch_dispatcher_schedule.html", days=DOW_NAMES)


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

STACK_UNLIMITED = 999

@app.post("/api/dispatch/driver-active")
def api_driver_active():
    """Make a driver inactive (can't sign in, go online or get orders) or active again.
    Their history and pay records stay."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    try:
        did = int(b.get("driver_id") or 0)
    except Exception:
        did = 0
    d = db().execute("SELECT id, name FROM drivers WHERE id=?", (did,)).fetchone()
    if not d:
        return jsonify({"ok": False, "error": "Driver not found."}), 404
    if out_of_scope(did):
        return jsonify({"ok": False, "error": "That driver is not in your region."}), 403
    want = 1 if str(b.get("active")).lower() in ("1", "true", "yes") else 0
    if not want:
        live = db().execute("""SELECT COUNT(*) c FROM orders WHERE driver_id=?
                               AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                            (did,)).fetchone()["c"]
        if live:
            return jsonify({"ok": False, "error": "%s still has %d live order%s. Move or finish %s first."
                            % (d["name"], live, "" if live == 1 else "s", "it" if live == 1 else "them")}), 400
        db().execute("UPDATE drivers SET active=0, status='offline', pending_request=NULL WHERE id=?", (did,))
        activity_mark("driver", did, None)
    else:
        db().execute("UPDATE drivers SET active=1 WHERE id=?", (did,))
    db().commit()
    log("driver_active", d["name"] + (" active" if want else " inactive") + " by " + (session.get("dispatcher_name") or "dispatch"))
    auto_assign()
    return jsonify({"ok": True, "active": want, "driver": d["name"]})


@app.post("/api/dispatch/region-tz")
def api_dispatch_region_tz():
    """A region's own time zone. Blank = the app's zone. Its hours, scheduled orders and
    the times on the dispatch, driver and kitchen screens for its orders follow it."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    rid = int(b.get("id") or 0)
    if _region(rid) is None:
        return jsonify({"ok": False, "error": "Region not found."}), 404
    if not _can_edit_region(rid):
        return jsonify({"ok": False, "error": "You can only change your own regions."}), 403
    tz = (b.get("tz") or "").strip()
    if tz and tz not in dict(TZ_CHOICES):
        return jsonify({"ok": False, "error": "Pick a time zone from the list."}), 400
    if tz == APP_TZ:
        tz = ""
    db().execute("UPDATE regions SET tz=? WHERE id=?", (tz or None, rid))
    db().commit()
    name = _region(rid)["name"]
    log("settings", "%s region time zone set to %s by %s" % (name, dict(TZ_CHOICES).get(tz or APP_TZ, tz or APP_TZ),
                                                           session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True, "tz": tz, "now": clock(now(), rid)})

@app.post("/api/dispatch/region-fees")
def api_region_fees():
    """A region's own delivery fees (dollars and miles) and/or its own FAQ.
    Blank clears a value so the region uses the business's from Settings."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    try:
        oid = int(b.get("id") or 0)
    except Exception:
        oid = 0
    reg = _region(oid)
    if reg is None:
        return jsonify({"ok": False, "error": "Region not found."}), 404
    if not _can_edit_region(oid):
        return jsonify({"ok": False, "error": "You can only change regions you're assigned to."}), 403
    def num(v, lo, hi, what):
        v = str(v if v is not None else "").strip().replace("$", "").replace(",", "")
        if v == "":
            return None, None
        try:
            f = float(v)
        except Exception:
            return None, "Enter a number for the " + what + ", or leave it blank."
        if f < lo or f > hi:
            return None, "The %s has to be from %g to %g, or blank." % (what, lo, hi)
        return f, None
    what = []
    if "base_fee" in b or "base_miles" in b or "per_mile" in b:
        bf, e1 = num(b.get("base_fee"), 0, 100, "delivery fee")
        bm, e2 = num(b.get("base_miles"), 0, 50, "number of miles the fee covers")
        pm, e3 = num(b.get("per_mile"), 0, 20, "fee per extra mile")
        if e1 or e2 or e3:
            return jsonify({"ok": False, "error": e1 or e2 or e3}), 400
        db().execute("UPDATE regions SET base_fee_cents=?, base_miles=?, per_mile_cents=? WHERE id=?",
                     (int(round(bf * 100)) if bf is not None else None, bm,
                      int(round(pm * 100)) if pm is not None else None, oid))
        what.append("delivery fees")
    if "faq_text" in b:
        ft = str(b.get("faq_text") or "").replace("\r\n", "\n").strip()[:12000]
        db().execute("UPDATE regions SET faq_text=? WHERE id=?", (ft or None, oid))
        what.append("FAQ")
    if not what:
        return jsonify({"ok": False, "error": "Nothing to save."}), 400
    db().commit()
    fr = fee_rules(oid)
    log("region_fees", "%s: %s saved by %s (fee %s first %g mi, then %s/mi, %s)" % (
        reg["name"], " and ".join(what), session.get("dispatcher_name") or "dispatch",
        money(fr["base_fee"]), float(fr["base_miles"] or 0), money(fr["per_mile"]), fr["from"]))
    return jsonify({"ok": True, "fees": fr})


@app.post("/api/dispatch/restaurant-partner")
def api_restaurant_partner():
    """Partner restaurants pay the business service fee; non-partners the non-partner rate."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    try:
        oid = int(b.get("id") or 0)
    except Exception:
        oid = 0
    r = db().execute("SELECT id, name, region_id FROM restaurants WHERE id=?", (oid,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Restaurant not found."}), 404
    if not _can_edit_region(r["region_id"] or 0):
        return jsonify({"ok": False, "error": "You can only change restaurants in your regions."}), 403
    on = 1 if b.get("partner") in (1, True, "1", "true") else 0
    db().execute("UPDATE restaurants SET partner=? WHERE id=?", (on, oid))
    db().commit()
    log("partner", "%s: %s by %s" % (r["name"], "partner" if on else "non-partner",
                                    session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True, "partner": on})


@app.post("/api/dispatch/delivery-rules")
def api_delivery_rules():
    """Minimum order (dollars) and delivery radius (miles) for a region or one restaurant.
    Blank clears it: a restaurant then uses its region's, a region then has no limit."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    kind = b.get("kind")
    try:
        oid = int(b.get("id") or 0)
    except Exception:
        oid = 0
    def num(v, lo, hi, what):
        v = str(v if v is not None else "").strip().replace("$", "").replace(",", "")
        if v == "":
            return None, None
        try:
            f = float(v)
        except Exception:
            return None, "Enter a number for the " + what + ", or leave it blank."
        if f < lo or f > hi:
            return None, "The %s has to be from %g to %g, or blank." % (what, lo, hi)
        return f, None
    mn, e1 = num(b.get("min_order"), 0, 500, "minimum order")
    mx, e2 = num(b.get("max_miles"), 0.5, 100, "delivery radius")
    if e1 or e2:
        return jsonify({"ok": False, "error": e1 or e2}), 400
    mn_c = int(round(mn * 100)) if mn is not None else None
    if kind == "region":
        if not _region(oid):
            return jsonify({"ok": False, "error": "Region not found."}), 404
        if not _can_edit_region(oid):
            return jsonify({"ok": False, "error": "You can only change regions you're assigned to."}), 403
        db().execute("UPDATE regions SET min_order_cents=?, max_miles=? WHERE id=?", (mn_c, mx, oid))
        name = _region(oid)["name"]
    elif kind == "restaurant":
        r = db().execute("SELECT id, name, region_id FROM restaurants WHERE id=?", (oid,)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "Restaurant not found."}), 404
        if not _can_edit_region(r["region_id"] or 0):
            return jsonify({"ok": False, "error": "You can only change restaurants in your regions."}), 403
        db().execute("UPDATE restaurants SET min_order_cents=?, max_miles=? WHERE id=?", (mn_c, mx, oid))
        name = r["name"]
    else:
        return jsonify({"ok": False, "error": "Pick a region or a restaurant."}), 400
    db().commit()
    log("delivery_rules", "%s: min %s, radius %s by %s" % (name, money(mn_c) if mn_c else "none",
                                                          ("%g mi" % mx) if mx else "none",
                                                          session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True})


@app.post("/api/dispatch/driver-stack")
def api_driver_stack():
    """Dispatch sets how many orders one driver can carry at once: 1 to 20, or unlimited."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    try:
        did = int(b.get("driver_id") or 0)
    except Exception:
        did = 0
    d = db().execute("SELECT id, name, max_stack FROM drivers WHERE id=?", (did,)).fetchone()
    if not d:
        return jsonify({"ok": False, "error": "Driver not found."}), 404
    if out_of_scope(did):
        return jsonify({"ok": False, "error": "That driver is not in your region."}), 403
    raw = str(b.get("limit", "")).strip().lower()
    if raw in ("unlimited", "u", "none", "no limit", "999"):
        lim = STACK_UNLIMITED
    else:
        try:
            lim = int(raw)
        except Exception:
            return jsonify({"ok": False, "error": "Enter a number from 1 to 20, or pick Unlimited."}), 400
        if lim < 1 or lim > 20:
            return jsonify({"ok": False, "error": "Enter a number from 1 to 20, or pick Unlimited."}), 400
    db().execute("UPDATE drivers SET max_stack=? WHERE id=?", (lim, did))
    db().commit()
    label = "unlimited" if lim >= STACK_UNLIMITED else str(lim)
    log("stack_limit", d["name"] + " -> " + label + " by " + (session.get("dispatcher_name") or "dispatch"))
    auto_assign()
    return jsonify({"ok": True, "driver": d["name"], "max_stack": lim, "label": label})


@app.post("/api/dispatch/route-sort")
def api_route_sort():
    """Dispatch button: put this driver's stops in the shortest drop-off order."""
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 403
    b = request.get_json(silent=True) or {}
    try:
        did = int(b.get("driver_id") or 0)
    except Exception:
        did = 0
    if not db().execute("SELECT 1 FROM drivers WHERE id=?", (did,)).fetchone():
        return jsonify({"ok": False, "error": "Driver not found."}), 404
    if out_of_scope(did):
        return jsonify({"ok": False, "error": "That driver is not in your region."}), 403
    changed, codes = resequence_route(did)
    return jsonify({"ok": True, "changed": changed, "order": codes})


@app.post("/api/dispatch/reorder")
def api_reorder():
    """Dispatch sets the driver's stop order: order_ids in the new order. Only that
    driver's live stops are renumbered 1..n. No chat message is sent; the driver app just shows the new order."""
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
    lk = delivered_lock(o)
    if lk:
        return lk
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
        auto_msg("drv_moved", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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
    lk = delivered_lock(o)
    if lk:
        return lk
    did = b.get("driver_id")
    if did:
        rc = region_conflict(int(did), o["region_id"], o["id"])
        if rc:
            return jsonify({"ok": False, "error": rc}), 400
    db().execute("""UPDATE orders SET dispatch_status='queued', hold_reason=NULL WHERE id=?""",
                 (o["id"],))
    db().commit()
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
    lk = delivered_lock(o)
    if lk:
        return lk
    keep = bool(data.get("keep_driver")) and o["driver_id"]
    if keep:
        seq = db().execute("""SELECT COALESCE(MAX(stack_seq),0)+1 s FROM orders WHERE driver_id=?
                              AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                           (o["driver_id"],)).fetchone()["s"]
        # back to "assigned" so the driver's phone pages again and the board flashes
        # until the driver taps Received
        db().execute("""UPDATE orders SET dispatch_status='assigned', delivered_at=NULL, stack_seq=?,
                        paged_at=? WHERE id=?""", (seq, now(), o["id"]))
        auto_msg("drv_reopen", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
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
    lk = delivered_lock(o)
    if lk:
        return lk
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
        _before = db().execute("SELECT hours, closed_override, open_24, prep_default FROM restaurants WHERE id=?", (rid,)).fetchone()
        hours = {}
        for i in range(7):
            o = request.form.get("open_" + str(i), "")
            c = request.form.get("close_" + str(i), "")
            if request.form.get("closed_" + str(i)):
                o = c = ""   # marked Closed that day
            hours[str(i)] = [o, c] if o and c else ["", ""]
        db().execute("""UPDATE restaurants SET hours=?, closed_override=?, open_24=?, prep_default=?,
                        phone=? WHERE id=?""",
                     (json.dumps(hours), 1 if request.form.get("closed_override") else 0,
                      0,
                      int(request.form.get("prep_default") or 15), request.form.get("phone", ""), rid))
        if _before:
            _ch = []
            if bool(_before["closed_override"]) != bool(request.form.get("closed_override")):
                _ch.append("paused your restaurant" if request.form.get("closed_override") else "resumed your restaurant")
            try:
                _oldh = json.loads(_before["hours"] or "{}")
            except Exception:
                _oldh = {}
            if {str(x): list(_oldh.get(str(x)) or ["", ""]) for x in range(7)} != hours:
                _ch.append("updated your hours")
            if int(_before["prep_default"] or 15) != int(request.form.get("prep_default") or 15):
                _ch.append("changed your usual prep time to %d minutes" % int(request.form.get("prep_default") or 15))
            if _ch:
                rest_auto_status(rid, "Dispatch " + ", ".join(_ch[:-1]) + (" and " if len(_ch) > 1 else "") + _ch[-1] + ".")
        meth = request.form.get("order_method")
        if meth in ("app", "online", "phone"):
            db().execute("UPDATE restaurants SET uses_app=?, call_method=? WHERE id=?",
                         (1 if meth == "app" else 0, "online" if meth == "online" else "phone", rid))
        else:   # older form: just the checkbox
            db().execute("UPDATE restaurants SET uses_app=? WHERE id=?",
                         (1 if request.form.get("uses_app") else 0, rid))
        if "order_url" in request.form:
            url = (request.form.get("order_url") or "").strip()[:300]
            if url and not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            db().execute("UPDATE restaurants SET order_url=? WHERE id=?", (url, rid))
        if "region_id" in request.form:
            try:
                _rg = int(request.form.get("region_id") or 0)
            except ValueError:
                _rg = -1
            if _rg == 0 or _rg in {g["id"] for g in all_regions()}:
                db().execute("UPDATE restaurants SET region_id=? WHERE id=?", (_rg, rid))
                db().execute("""UPDATE orders SET region_id=? WHERE restaurant_id=? AND COALESCE(pickup_address,'')=''
                                AND dispatch_status NOT IN ('delivered','cancelled')""", (_rg, rid))
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
    stamp_regions()
    regs = [{"id": g["id"], "name": g["name"]} for g in all_regions()]
    rank = {g["id"]: i for i, g in enumerate(regs)}
    rname = {g["id"]: g["name"] for g in regs}
    rs = db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' ORDER BY name").fetchall()
    data = []
    for r in rs:
        rg = int(r["region_id"] or 0)
        if rg not in rname:
            rg = 0   # no region, or a region that was removed
        data.append({"r": r, "hours": json.loads(r["hours"]), "open": is_open(r), "method": order_method(r),
                     "rg": rg, "rg_name": rname.get(rg, "No region")})
    # Grouped by region in the Regions page order, restaurants with no region last, A to Z inside each region.
    data.sort(key=lambda d: (rank.get(d["rg"], len(regs)), (d["r"]["name"] or "").lower()))
    counts = {}
    for d in data:
        counts[d["rg"]] = counts.get(d["rg"], 0) + 1
    return render_template("dispatch_restaurants.html", is_owner_view=is_owner(session.get("dispatcher_id")), data=data, week=WEEK, saved=saved,
                           regions=regs, rg_counts=counts)

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
    pp_msgs = []
    br_msgs = []
    if request.method == "POST":
        _am_owner = is_owner(session.get("dispatcher_id"))
        _am_dev = is_dev()
        if "automsg_present" in request.form and (_am_owner or _am_dev):
            for _k, _l, _h, _d in AUTO_MSGS:
                if (_k in DEV_AUTO_MSGS and not _am_dev) or (_k not in DEV_AUTO_MSGS and not _am_owner):
                    continue
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                             ("am_" + _k, "1" if request.form.get("am_" + _k) else "0"))
            try:
                _e = max(5, min(120, int(request.form.get("short_staff_every_min") or 15)))
            except ValueError:
                _e = 15
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('short_staff_every_min',?)", (str(_e),))
            for _k in ("sa_nodrv", "sa_idle"):
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, "1" if request.form.get(_k) else "0"))
            for _k, (_lo, _hi) in SA_LIMITS.items():
                try:
                    _v = max(_lo, min(_hi, int(request.form.get(_k) or SA_DEFAULTS[_k])))
                except ValueError:
                    _v = int(SA_DEFAULTS[_k])
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, str(_v)))
        if "pp_present" in request.form and is_owner(session.get("dispatcher_id")):
            _cur = _pp_settings()
            _new = dict(_cur)
            for _m in PP_MODES:
                _c = (request.form.get("pp_%s_client" % _m) or "").strip()[:200]
                _new["pp_%s_client" % _m] = _c
                _sv = (request.form.get("pp_%s_secret" % _m) or "").strip()[:200]
                if request.form.get("pp_%s_clear" % _m):
                    _new["pp_%s_client" % _m], _new["pp_%s_secret" % _m] = "", ""
                elif _sv:
                    _new["pp_%s_secret" % _m] = _sv
            _mode = request.form.get("pp_mode") if request.form.get("pp_mode") in PP_MODES else "sandbox"
            _old_mode = _cur.get("pp_mode") if _cur.get("pp_mode") in PP_MODES else "sandbox"
            if _mode != _old_mode:
                _held = [r[0] for r in db().execute(
                    "SELECT code FROM orders WHERE pp_state='authorized' ORDER BY id DESC LIMIT 20").fetchall()]
                if _held:
                    pp_msgs.append(("bad", "Still on %s: these orders have a PayPal hold made with the %s keys, "
                                    "so they must be charged or released first: %s" % (_old_mode, _old_mode, ", ".join(_held))))
                    _mode = _old_mode
            _new["pp_mode"] = _mode
            for _k in PP_KEYS:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, _new.get(_k, "")))
            db().commit()
            for _m in PP_MODES:
                _c, _sv = _new.get("pp_%s_client" % _m, ""), _new.get("pp_%s_secret" % _m, "")
                _changed = (_c, _sv) != (_cur.get("pp_%s_client" % _m, ""), _cur.get("pp_%s_secret" % _m, ""))
                if _c and _sv and (_changed or _m == _mode and _mode != _old_mode):
                    _ok, _msg = pp_test_keys(_m, _c, _sv)
                    pp_msgs.append(("ok" if _ok else "bad", _msg))
                elif bool(_c) != bool(_sv) and (_c or _sv):
                    pp_msgs.append(("bad", "The %s keys need both a client ID and a secret." % _m))
            # Each brand's own keys (blank = the brand uses the main keys above)
            _sites = db().execute("SELECT id, name FROM sites ORDER BY name").fetchall() if brands_on() else []
            for _st in _sites:
                if ("ppb_%d_present" % _st["id"]) not in request.form:
                    continue
                _bcur = pp_brand_keys(_st["id"]); _bnew = dict(_bcur)
                for _m in PP_MODES:
                    _f = "ppb_%d_%s_" % (_st["id"], _m)
                    _bnew[_m + "_client"] = (request.form.get(_f + "client") or "").strip()[:200]
                    _sv = (request.form.get(_f + "secret") or "").strip()[:200]
                    if request.form.get(_f + "clear"):
                        _bnew[_m + "_client"], _bnew[_m + "_secret"] = "", ""
                    elif _sv:
                        _bnew[_m + "_secret"] = _sv
                for _m in PP_MODES:
                    _was = (_bcur.get(_m + "_client", ""), _bcur.get(_m + "_secret", ""))
                    _now = (_bnew.get(_m + "_client", ""), _bnew.get(_m + "_secret", ""))
                    if _was != _now and _was[0] and _m == _mode:
                        _held = [r[0] for r in db().execute(
                            "SELECT code FROM orders WHERE pp_state='authorized' AND pp_acct=? ORDER BY id DESC LIMIT 20",
                            (_st["id"],)).fetchall()]
                        if _held:
                            pp_msgs.append(("bad", "%s: kept the old %s keys. These orders have a PayPal hold made with them, "
                                            "so charge or release them first: %s" % (_st["name"], _m, ", ".join(_held))))
                            _bnew[_m + "_client"], _bnew[_m + "_secret"] = _was
                            continue
                    for _f2 in ("client", "secret"):
                        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                     ("pp_b%d_%s_%s" % (_st["id"], _m, _f2), _bnew.get(_m + "_" + _f2, "")))
                    if _now[0] and _now[1] and _now != _was:
                        _ok, _msg = pp_test_keys(_m, _now[0], _now[1])
                        pp_msgs.append(("ok" if _ok else "bad", _st["name"] + ": " + _msg))
                    elif bool(_now[0]) != bool(_now[1]):
                        pp_msgs.append(("bad", "%s: the %s keys need both a client ID and a secret." % (_st["name"], _m)))
            db().commit()
            if not (_new.get("pp_%s_client" % _mode) and _new.get("pp_%s_secret" % _mode)):
                if any(pp_brand_ready(_st["id"], _mode) for _st in _sites):
                    pp_msgs.append(("bad", "No main %s keys: brands without their own keys cannot take PayPal payments." % _mode))
                else:
                    pp_msgs.append(("bad", "PayPal is off: there are no %s keys saved, so customers cannot pay online." % _mode))
        if "br_present" in request.form and is_owner(session.get("dispatcher_id")):
            _op = request.form.get("br_op") or "save"
            try:
                _bid = int(request.form.get("br_id") or 0)
            except ValueError:
                _bid = 0
            _cur = db().execute("SELECT * FROM branch_accounts WHERE id=?", (_bid,)).fetchone() if _bid else None
            if _op == "delete" and _cur:
                db().execute("DELETE FROM branch_accounts WHERE id=?", (_bid,))
                db().execute("UPDATE drivers SET branch_account_id=NULL WHERE branch_account_id=?", (_bid,))
                db().commit()
                br_msgs.append(("ok", "Removed the Branch account %s. Its drivers now use the account for their order's brand." % _cur["name"]))
            else:
                _nm = " ".join(str(request.form.get("br_name") or "").split())[:60]
                _md = request.form.get("br_mode") if request.form.get("br_mode") in BR_MODES else "sandbox"
                _org = " ".join(str(request.form.get("br_org") or "").split())[:40]
                _kv = str(request.form.get("br_key") or "").strip()[:400]
                try:
                    _sid = int(request.form.get("br_site") or 0) or None
                except ValueError:
                    _sid = None
                _act = 1 if request.form.get("br_active", "1") == "1" else 0
                if not _nm:
                    br_msgs.append(("bad", "Give the Branch account a name, like Tiger Town To Go."))
                else:
                    if _cur and not _kv:
                        _kv = _cur["api_key"] or ""
                    _changed = (not _cur) or (_org, _kv, _md) != ((_cur["org_id"] or ""), (_cur["api_key"] or ""), (_cur["mode"] or "sandbox"))
                    if _cur:
                        db().execute("UPDATE branch_accounts SET name=?, mode=?, org_id=?, api_key=?, site_id=?, active=? WHERE id=?",
                                     (_nm, _md, _org, _kv, _sid, _act, _bid))
                    else:
                        db().execute("INSERT INTO branch_accounts(name,mode,org_id,api_key,site_id,active,created_at) VALUES(?,?,?,?,?,?,?)",
                                     (_nm, _md, _org, _kv, _sid, _act, dt.datetime.now().isoformat(timespec="seconds")))
                    db().commit()
                    if _org and _kv and _changed:
                        _ok, _msg = br_test_keys(_md, _org, _kv, _nm)
                        br_msgs.append(("ok" if _ok else "bad", _msg))
                    elif not (_org and _kv):
                        br_msgs.append(("bad", "Saved %s, but it needs both an organization ID and an API key before it can pay drivers." % _nm))
                    else:
                        br_msgs.append(("ok", "Saved %s." % _nm))
        if "modes_present" in request.form and is_owner(session.get("dispatcher_id")):
            for _k in ("brands_on", "regions_on", "dispatch_region_lock"):
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                             (_k, "1" if request.form.get(_k) else "0"))
            db().commit()
            g.pop("_flags", None)
            g.pop("_site", None)
            g.pop("_hsite", None)
        if "autokitchen_present" in request.form and is_owner(session.get("dispatcher_id")):
            for x in db().execute("SELECT id FROM regions").fetchall():
                db().execute("UPDATE regions SET auto_kitchen=? WHERE id=?",
                             (1 if request.form.get("auto_kitchen_%d" % x["id"]) else 0, x["id"]))
            db().commit()
            auto_kitchen_sweep()
        if "ordnum_present" in request.form and is_owner(session.get("dispatcher_id")):
            f = request.form
            vals = {"primary_on": "1" if f.get("primary_on") else "0",
                    "primary_style": f.get("primary_style") if f.get("primary_style") in PRIMARY_STYLES else "plain",
                    "primary_digits": str(max(1, min(8, int(f.get("primary_digits") or 4)))) if (f.get("primary_digits") or "4").isdigit() else "4",
                    "primary_reset": "daily" if f.get("primary_reset") == "daily" else "never",
                    "secondary_prefix": clean_prefix(f.get("secondary_prefix")) or "FF",
                    "secondary_style": f.get("secondary_style") if f.get("secondary_style") in SECONDARY_STYLES else "time",
                    "secondary_digits": str(max(3, min(10, int(f.get("secondary_digits") or 6)))) if (f.get("secondary_digits") or "6").isdigit() else "6",
                    "primary_prefix_main": clean_prefix(f.get("primary_prefix_main"))}
            for _scr in ("dispatch", "rest", "driver"):
                _v = f.get("ordshow_" + _scr)
                vals["ordshow_" + _scr] = _v if _v in ORDSHOW_CHOICES else "both"
            for x in db().execute("SELECT id FROM sites").fetchall():
                vals["primary_prefix_%d" % x["id"]] = clean_prefix(f.get("primary_prefix_%d" % x["id"]))
            for k, v in vals.items():
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
            db().commit()
        if "cashgps_present" in request.form:
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('allow_cash',?)",
                         ("1" if request.form.get("allow_cash") else "0",))
        if "site_present" in request.form:
            for _k, _d, _n in SITE_TEXT:
                if _k in request.form:
                    _v = (request.form.get(_k) or "").strip()[:_n]
                    if _v == _d:
                        _v = ""
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, _v))
            for _k, _n in (("business_email", 120), ("home_headline", 120), ("social_x", 200),
                           ("social_facebook", 200), ("social_instagram", 200), ("faq_text", 12000)):
                _v = (request.form.get(_k) or "").strip()[:_n]
                if _k.startswith("social_") and _v and not _v.startswith("http"):
                    _v = "https://" + _v
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, _v))
        if "loyalty_present" in request.form:
            def _num(k, lo, hi, mult=1):
                try:
                    v = int(round(float((request.form.get(k) or "").replace("$", "")) * mult))
                except ValueError:
                    return None
                return v if lo <= v <= hi else None
            _vals = {"loyalty_on": "1" if request.form.get("loyalty_on") else "0",
                     "confirm_call": "1" if request.form.get("confirm_call") else "0"}
            _ro = []
            for _i in (1, 2, 3, 4):
                _p, _d = _num("ro_points_%d" % _i, 1, 100000), _num("ro_value_%d" % _i, 1, 100000, 100)
                if _p and _d:
                    _ro.append("%d:%d" % (_p, _d))
            if _ro:
                _vals["reward_options"] = ",".join(_ro)
            if "bonus_weekday" in request.form:
                _bw = _num("bonus_weekday", -1, 6)
                if _bw is not None:
                    _vals["bonus_weekday"] = str(_bw)
            for _k, _sk, _lo, _hi, _m in (("points_per_dollar", "points_per_dollar", 0, 20, 1),
                                          ("signup_points", "signup_points", 0, 10000, 1),
                                          ("review_points", "review_points", 0, 10000, 1),
                                          ("tier_vip_points", "tier_vip_points", 1, 1000000, 1),
                                          ("tier_elite_points", "tier_elite_points", 2, 1000000, 1),
                                          ("bonus_starter_pct", "bonus_starter_pct", 0, 1000, 1),
                                          ("bonus_vip_pct", "bonus_vip_pct", 0, 1000, 1),
                                          ("bonus_elite_pct", "bonus_elite_pct", 0, 1000, 1),
                                          ("gift_min", "gift_min_cents", 100, 50000, 100),
                                          ("gift_max", "gift_max_cents", 500, 200000, 100)):
                _v = _num(_k, _lo, _hi, _m)
                if _v is not None:
                    _vals[_sk] = str(_v)
            for _k, _v in _vals.items():
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, _v))
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
        for _k in ("driver_pay_fee_pct", "driver_pay_tip_pct"):
            if _k in request.form:
                try:
                    _v = int(request.form[_k] or 0)
                except ValueError:
                    _v = -1
                if 0 <= _v <= 100:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, str(_v)))
        for _f, _k in (("driver_pay_base", "driver_pay_base_cents"), ("driver_pay_mile", "driver_pay_mile_cents")):
            if _f in request.form:
                _raw = (request.form[_f] or "").replace("$", "").strip()
                if _raw == "":
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?, '')", (_k,))
                else:
                    try:
                        _v = int(round(float(_raw) * 100))
                    except ValueError:
                        _v = -1
                    if 0 <= _v <= 50000:
                        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_k, str(_v)))
        if "driver_pay_flat" in request.form:
            try:
                _v = int(round(float((request.form["driver_pay_flat"] or "0").replace("$", "")) * 100))
            except ValueError:
                _v = -1
            if 0 <= _v <= 50000:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('driver_pay_flat_cents',?)", (str(_v),))
        if "sched_orders_per_hr" in request.form:
            try:
                _f = float(request.form["sched_orders_per_hr"] or 0)
            except ValueError:
                _f = 0
            if 0.5 <= _f <= 10:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('sched_orders_per_hr',?)", (("%g" % _f),))
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
        if "default_max_miles" in request.form:
            _dm = str(request.form.get("default_max_miles") or "").strip()
            try:
                _dmv = float(_dm) if _dm else 0.0
            except ValueError:
                _dmv = -1
            if _dmv == 0 or 0.5 <= _dmv <= 100:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('default_max_miles',?)",
                             ("%g" % _dmv if _dmv else "",))
        for key in ("base_fee_cents", "base_miles", "per_mile_cents", "tax_rate_bp", "auto_assign"):
            if key in request.form:
                db().execute("UPDATE settings SET value=? WHERE key=?", (request.form[key], key))
        for key in ("keep_awake_driver", "keep_awake_kitchen"):
            if key in request.form:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                             (key, "1" if str(request.form.get(key)).strip() == "1" else "0"))
        if "assign_on_pending" in request.form:
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('assign_on_pending',?)",
                         ("1" if str(request.form.get("assign_on_pending")).strip() == "1" else "0",))
        if "stack_by_location" in request.form:
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('stack_by_location',?)",
                         ("1" if str(request.form.get("stack_by_location")).strip() == "1" else "0",))
        if "max_stack_default" in request.form:
            _raw = str(request.form.get("max_stack_default") or "").strip().lower()
            _lim = None
            if _raw in ("u", "unlimited", "none", "no limit", "999"):
                _lim = STACK_UNLIMITED
            else:
                try:
                    _lim = max(1, min(20, int(_raw)))
                except ValueError:
                    _lim = None
            if _lim is not None:
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('max_stack_default',?)", (str(_lim),))
                if request.form.get("max_stack_apply_all") == "1":
                    db().execute("UPDATE drivers SET max_stack=?", (_lim,))
                    log("stack_limit", "all drivers -> " + ("unlimited" if _lim >= STACK_UNLIMITED else str(_lim)) +
                        " by " + (session.get("dispatcher_name") or "dispatch"))
        for key, lo, hi in (("stack_pickup_mi", 0.05, 10), ("stack_detour_mi", 0, 30)):
            if key in request.form:
                try:
                    v = max(lo, min(hi, float(str(request.form.get(key)).strip())))
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, "%g" % v))
                except Exception:
                    pass
        if "auto_driver_pay" in request.form:
            db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_driver_pay',?)",
                         ("1" if request.form.get("auto_driver_pay") == "1" else "0",))
        if "auto_pay_cap" in request.form:
            try:
                capc = int(round(float(request.form["auto_pay_cap"].replace("$", "").strip() or 0) * 100))
                if 0 <= capc <= 50000:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_pay_cap_cents',?)", (str(capc),))
            except ValueError:
                pass
        if "auto_pay_delay_min" in request.form:
            try:
                dm = int(request.form["auto_pay_delay_min"])
                if 0 <= dm <= 1440:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('auto_pay_delay_min',?)", (str(dm),))
            except ValueError:
                pass
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
        if "service_np_pct" in request.form:
            _raw = request.form["service_np_pct"].replace("%", "").strip()
            if _raw == "":
                db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('service_fee_np_bp', '')")
            else:
                try:
                    _pct = float(_raw)
                except ValueError:
                    _pct = -1
                if 0 <= _pct <= 30:
                    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('service_fee_np_bp',?)",
                                 (str(int(round(_pct * 100))),))
                else:
                    errs.append("Non-partner service fee needs to be a percent between 0 and 30, or blank.")
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
            return render_template("dispatch_settings.html", pp=pp_settings_view(), pp_msgs=pp_msgs, br=br_settings_view(), br_msgs=br_msgs, br_sites=[dict(id=x["id"], name=x["name"]) for x in db().execute("SELECT id, name FROM sites ORDER BY name").fetchall()] if brands_on() else [], auto_msgs=AUTO_MSGS, am_on=auto_msg_on, dev_am=DEV_AUTO_MSGS, dev_view=is_dev(), owner_view=is_owner(session.get("dispatcher_id")), site=site_text(raw=True), s={r["key"]: r["value"] for r in rows},
                                   saved=False, errors=errs, bh=business_hours_rows(),
                                   bh_on=bool(business_hours()), any_on=any_rest_on(), any_row=any_rest_row(), bh_days=BH_DAYS)
        if "order_tokens" in request.form:
            tags = ",".join(t.strip()[:24] for t in request.form["order_tokens"].split(",") if t.strip())
            db().execute("UPDATE settings SET value=? WHERE key='order_tokens'", (tags,))
        db().commit()
        saved = True
    rows = db().execute("SELECT * FROM settings").fetchall()
    return render_template("dispatch_settings.html", pp=pp_settings_view(), pp_msgs=pp_msgs, br=br_settings_view(), br_msgs=br_msgs, br_sites=[dict(id=x["id"], name=x["name"]) for x in db().execute("SELECT id, name FROM sites ORDER BY name").fetchall()] if brands_on() else [], auto_msgs=AUTO_MSGS, am_on=auto_msg_on, dev_am=DEV_AUTO_MSGS, dev_view=is_dev(), owner_view=is_owner(session.get("dispatcher_id")), site=site_text(raw=True), s={r["key"]: r["value"] for r in rows}, saved=saved,
                           bh=business_hours_rows(), bh_on=bool(business_hours()), any_on=any_rest_on(), any_row=any_rest_row(), bh_days=BH_DAYS)

# ---------------------------------------------------------------- chat

@app.get("/api/chat/<int:driver_id>")
def api_chat(driver_id):
    if session.get("driver_id") != driver_id:
        bad = out_of_scope(driver_id)
        if bad:
            return bad
    if dispatcher_required() and request.args.get("peek") != "1":
        db().execute("""UPDATE messages SET seen_by_dispatch=1
                        WHERE driver_id=? AND sender='driver' AND seen_by_dispatch=0""", (driver_id,))
        db().commit()
    rows = db().execute("""SELECT m.*, COALESCE(d.is_dev,0) AS from_dev FROM messages m
                           LEFT JOIN dispatchers d ON d.id=m.dispatcher_id
                           WHERE m.driver_id=? ORDER BY m.id DESC LIMIT 60""",
                        (driver_id,)).fetchall()
    msgs = [{"id": r["id"], "sender": r["sender"], "dev": bool(r["from_dev"]),
              "who": (r["sender_name"] + (" (Developer)" if r["from_dev"] else " (dispatch)")) if r["sender_name"] else
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
    if sender == "dispatch":
        bad = out_of_scope(driver_id)
        if bad:
            return bad
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
    auto_msg("drv_request_ack", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (driver_id, "system",
                  "Request sent to dispatch: " + want + ". Waiting on dispatch to approve.", now()))
    db().commit()


def set_driver_status(driver_id, status, reply):
    """Dispatch-only. Nothing in the driver app calls this directly."""
    was = db().execute("SELECT status FROM drivers WHERE id=?", (driver_id,)).fetchone()
    log_driver(driver_id, "Dispatch set " + status + " (was " + (was["status"] if was else "?") + ")", status=status)
    db().execute("UPDATE drivers SET status=?, pending_request=NULL, last_seen=? WHERE id=?",
                 (status, now(), driver_id))
    activity_mark("driver", driver_id, None if status == "offline" else ("break" if "break" in status else "online"))
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

def driver_site_ids(did):
    """Brands a driver works for, from the regions dispatch assigned them (0 = a region not tied to a brand)."""
    return {int(r["sid"] or 0) for r in db().execute(
        """SELECT COALESCE(rg.site_id,0) sid FROM driver_regions dr JOIN regions rg ON rg.id=dr.region_id
           WHERE dr.driver_id=?""", (did,)).fetchall()}


def main_named_site_ids():
    """Brand rows that carry the main business's own name (a region tied to a "Tiger Town To Go"
    brand row still belongs to Tiger Town To Go when it is the main business)."""
    def norm(x):
        return "".join(ch for ch in str(x or "").lower() if ch.isalnum()).replace("2", "to")
    names = {norm(main_brand_name()), norm(session.get("staff_brand_name"))} - {""}
    try:
        return {int(r["id"]) for r in db().execute("SELECT id, name FROM sites").fetchall()
                if norm(r["name"]) in names}
    except Exception:
        return set()


def driver_home_brand(did):
    """The brand to switch a driver to when they picked a company they don't drive for:
    'main' for regions not tied to a brand, else the one brand their regions belong to."""
    sids = driver_site_ids(did)
    if not sids or 0 in sids:
        return "main"
    if len(sids) == 1:
        sid = next(iter(sids))
        return sid if site_by_id(sid) is not None else None
    return None


def driver_fits_brand(did, site=None):
    """True when this driver may sign in under the brand being shown. Regions not tied to a
    brand (or tied to a brand row carrying the main business's name) belong to Tiger Town To Go;
    a driver may sign in under another brand only when dispatch gave them a region of that brand."""
    site = staff_brand_site() if site is None else site
    if site is None:   # Tiger Town To Go (the main business): every driver may sign in under it
        return True
    sid = int(site["id"])
    sids = driver_site_ids(did)
    if sid in sids:
        return True
    mains = main_named_site_ids()
    if sid in mains:   # a brand row named Tiger Town To Go counts as the main business
        return (not sids) or (0 in sids) or bool(sids & mains)
    return False


def restaurant_fits_brand(row, site=None):
    """True when this restaurant belongs to the brand being shown (by its region's brand).
    A restaurant with no region, or a region not tied to a brand, belongs to Tiger Town To Go."""
    site = staff_brand_site() if site is None else site
    if row is None or site is None:   # Tiger Town To Go (the main business) takes every restaurant
        return True
    sid = int(site["id"])
    rs = site_of_region(row["region_id"]) if row["region_id"] else None
    if rs is not None and int(rs["id"]) == sid:
        return True
    mains = main_named_site_ids()
    if sid in mains:
        return rs is None or int(rs["id"]) in mains
    return False


def home_brand_name_for(sids):
    """Name of the company an account belongs to, for the wrong-company message."""
    real = [x for x in sids if x and x not in main_named_site_ids()]
    if len(real) == 1:
        st = site_by_id(real[0])
        if st is not None:
            return st["name"]
    if not real:
        return main_company_name()
    return None


def main_company_name():
    """The main business's name as the Switch company list shows it (Tiger Town To Go)."""
    try:
        for c in company_rows():
            if company_look(c).get("bs") == "main":
                return c.get("name") or main_brand_name()
    except Exception:
        pass
    return main_brand_name()


def wrong_brand_msg(site, who, home=None):
    nm = site["name"] if site is not None else (session.get("staff_brand_name") or main_brand_name())
    if home and home != nm:
        return ("This %s account belongs to %s, not %s. Tap Switch company and pick %s."
                % (who, home, nm, home))
    return ("This %s account isn't set up for %s. Pick your company again with Switch company, "
            "or call dispatch." % (who, nm))


@app.route("/driver/login", methods=["GET", "POST"])
def driver_login():
    err = None
    if request.method == "POST":
        phone = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit())
        pin = request.form.get("pin", "")
        row = db().execute("SELECT * FROM drivers WHERE phone=? AND pin=?", (phone, pin)).fetchone()
        if row and not (row["active"] if row["active"] is not None else 1):
            err = "Your driver account is inactive. Call dispatch."
        elif row and not driver_fits_brand(row["id"]):
            # Right phone and PIN but another company's app: keep them out and name their company.
            err = wrong_brand_msg(staff_brand_site(), "driver", home_brand_name_for(driver_site_ids(row["id"])))
        elif row:
            session["driver_id"] = row["id"]
            session["driver_name"] = row["name"]
            return redirect(url_for("driver"))
        else:
            err = "No driver with that phone and PIN."
    if request.method == "GET" and session.get("driver_id"):
        if driver_fits_brand(session["driver_id"]):
            return redirect(url_for("driver"))
        err = wrong_brand_msg(staff_brand_site(), "driver", home_brand_name_for(driver_site_ids(session["driver_id"])))   # switched to a brand they don't drive for
        session.pop("driver_id", None)
    return render_template("driver_login.html", err=err)

@app.route("/driver/logout")
def driver_logout():
    session.pop("driver_id", None)
    return redirect(url_for("driver_login"))

@app.route("/driver")
def driver():
    if not session.get("driver_id") or not driver_fits_brand(session["driver_id"]):
        return redirect(url_for("driver_login"))
    _ph = dispatch_phone(driver_phone_region(session["driver_id"]))
    _bg = db().execute("SELECT last_bg_at FROM drivers WHERE id=?", (session["driver_id"],)).fetchone()
    return render_template("driver.html", driver_id=session["driver_id"],
                           driver_name=session["driver_name"],
                           track_id=driver_track_id(session["driver_id"]), gps_url=gps_server_url(),
                           bg_seen=(_bg["last_bg_at"] if _bg else None),
                           dispatch_phone=nice_phone(_ph), dispatch_tel=tel_digits(_ph),
                           awake_default=awake_default("driver"))


def awake_default(app_name):
    """Business default for the Keep screen awake switch. Each device can still turn it off."""
    v = setting("keep_awake_" + app_name, str)
    return "0" if str(v) == "0" else "1"



# ---------------- Google map pictures (Map Tiles API) ----------------
_TILE_SESSION = {}


def google_tile_session(site_url):
    """One Google map session, reused for about two weeks. None when Google maps are off
    or the key isn't allowed to use the Map Tiles API."""
    if not GOOGLE_TILE_KEY:
        return None
    hit = _TILE_SESSION.get("s")
    if hit and hit["until"] > time.time():
        return hit
    if _TILE_SESSION.get("fail_until", 0) > time.time():
        return None
    try:
        body = json.dumps({"mapType": "roadmap", "language": "en-US", "region": "US",
                           "scale": "scaleFactor2x", "highDpi": True}).encode()
        req = urllib.request.Request("https://tile.googleapis.com/v1/createSession?key="
                                     + urllib.parse.quote(GOOGLE_TILE_KEY), data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Referer": site_url})
        res = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        tok = res.get("session")
        if not tok:
            raise ValueError("no session")
        exp = float(res.get("expiry") or 0) or (time.time() + 13 * 86400)
        copy = "Map data \u00a9" + str(dt.date.today().year) + " Google"
        try:
            vu = ("https://tile.googleapis.com/tile/v1/viewport?session=%s&key=%s&zoom=12"
                  "&north=33.0&south=32.4&east=-85.0&west=-85.7" % (tok, urllib.parse.quote(GOOGLE_TILE_KEY)))
            vreq = urllib.request.Request(vu, headers={"Referer": site_url})
            vv = json.loads(urllib.request.urlopen(vreq, timeout=8).read().decode())
            if vv.get("copyright"):
                copy = vv["copyright"]
        except Exception:
            pass
        hit = {"session": tok, "until": min(exp, time.time() + 13 * 86400) - 3600,
               "size": res.get("tileWidth") or 512, "copyright": copy}
        _TILE_SESSION["s"] = hit
        return hit
    except Exception as e:
        app.logger.warning("Google map tiles unavailable: %s", e)
        _TILE_SESSION["fail_until"] = time.time() + 600
        return None


@app.get("/api/map-tiles")
def api_map_tiles():
    """Which map pictures to draw: Google when a key is set, OpenStreetMap otherwise."""
    ses = google_tile_session(request.host_url)
    if not ses:
        return jsonify({"ok": True, "provider": "osm",
                        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                        "attribution": "&copy; OpenStreetMap", "max_zoom": 19, "tile_size": 256})
    return jsonify({"ok": True, "provider": "google",
                    "url": "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=%s&key=%s"
                           % (ses["session"], urllib.parse.quote(GOOGLE_TILE_KEY)),
                    "attribution": ses["copyright"], "max_zoom": 22, "tile_size": ses["size"]})


@app.get("/api/maps-status")
def api_maps_status():
    """Owner check: which Google map features are working right now."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    out = {"server_key": bool(GOOGLE_KEY), "browser_key": bool(os.environ.get("GOOGLE_MAPS_BROWSER_KEY")),
           "map_tiles": bool(google_tile_session(request.host_url))}
    def probe(url):
        try:
            r = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())
            return r.get("status", "?") + ((": " + r["error_message"]) if r.get("error_message") else "")
        except Exception as e:
            return "error: " + str(e)[:120]
    if GOOGLE_KEY:
        k = urllib.parse.quote(GOOGLE_KEY)
        out["geocoding"] = probe("https://maps.googleapis.com/maps/api/geocode/json?address=Opelika,AL&key=" + k)
        try:
            rt = google_routes((32.6454, -85.3783), (32.6099, -85.4808), "routes.duration,routes.distanceMeters")
            out["routes"] = "OK (%.1f mi, %d min)" % (rt.get("distanceMeters", 0) / 1609.34, round(_gsecs(rt.get("duration")) / 60))
        except Exception as e:
            out["routes"] = "error: " + str(e)[:200]
    return jsonify(dict(out, ok=True))

# ---------------- in-app navigation ----------------
_ROUTE_CACHE = {}


def _ll(v):
    try:
        a, b = [float(x) for x in str(v).split(",")[:2]]
        if -90 <= a <= 90 and -180 <= b <= 180:
            return a, b
    except (TypeError, ValueError):
        pass
    return None


def _decode_poly(enc):
    pts, idx, lat, lng = [], 0, 0, 0
    while idx < len(enc):
        for which in (0, 1):
            shift = res = 0
            while True:
                b = ord(enc[idx]) - 63; idx += 1
                res |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else (res >> 1)
            if which == 0:
                lat += d
            else:
                lng += d
        pts.append([lat / 1e5, lng / 1e5])
    return pts


_TURN = {"left": "left", "right": "right", "slight left": "slight left", "slight right": "slight right",
         "sharp left": "sharp left", "sharp right": "sharp right", "uturn": "uturn", "straight": "straight"}


def _osrm_text(st):
    m = st.get("maneuver") or {}
    t, mod = m.get("type", ""), m.get("modifier", "")
    road = st.get("name") or st.get("ref") or ""
    onto = (" onto " + road) if road else ""
    if t == "depart":
        return "Head out" + ((" on " + road) if road else "")
    if t == "arrive":
        return "You have arrived"
    if t in ("roundabout", "rotary"):
        ex = m.get("exit")
        return "At the roundabout take the " + ((_ordinal(ex) + " exit") if ex else "exit") + onto
    if t in ("merge",):
        return "Merge" + ((" " + mod) if mod and mod != "straight" else "") + onto
    if t in ("on ramp",):
        return "Take the ramp" + ((" on the " + mod) if mod in ("left", "right") else "") + onto
    if t in ("off ramp",):
        return "Take the exit" + ((" on the " + mod) if mod in ("left", "right") else "") + onto
    if t == "fork":
        return "Keep " + (mod.replace("slight ", "") or "straight") + " at the fork" + onto
    if t == "end of road":
        return "At the end of the road turn " + (mod or "") + onto
    if mod == "uturn":
        return "Make a U-turn" + onto
    if mod == "straight" or t == "new name" or t == "continue":
        return "Continue" + onto if road else "Continue straight"
    return "Turn " + (mod or "") + onto


def _ordinal(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return str(n) + ("th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _route_osrm(a, b):
    url = ("https://router.project-osrm.org/route/v1/driving/%f,%f;%f,%f"
           "?overview=full&geometries=geojson&steps=true" % (a[1], a[0], b[1], b[0]))
    req = urllib.request.Request(url, headers={"User-Agent": "FleetDelivery/1.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    rt = data["routes"][0]
    steps = []
    for leg in rt["legs"]:
        for st in leg["steps"]:
            loc = st["maneuver"]["location"]
            steps.append({"text": _osrm_text(st), "type": st["maneuver"].get("type", ""),
                          "modifier": st["maneuver"].get("modifier", ""),
                          "lat": loc[1], "lng": loc[0], "dist_m": st.get("distance", 0),
                          "dur_s": st.get("duration", 0)})
    return {"coords": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]], "steps": steps,
            "distance_m": rt.get("distance", 0), "duration_s": rt.get("duration", 0), "source": "osm"}


def _route_google(a, b):
    url = ("https://maps.googleapis.com/maps/api/directions/json?origin=%f,%f&destination=%f,%f"
           "&mode=driving&departure_time=now&key=%s" % (a[0], a[1], b[0], b[1], GOOGLE_KEY))
    data = json.loads(urllib.request.urlopen(url, timeout=12).read().decode())
    if data.get("status") != "OK" or not data.get("routes"):
        return None
    leg = data["routes"][0]["legs"][0]
    steps, coords = [], []
    for st in leg["steps"]:
        txt = re.sub(r"<div[^>]*>", ". ", st.get("html_instructions", ""))
        txt = re.sub(r"<[^>]+>", "", txt).replace("&nbsp;", " ").replace("&amp;", "&").strip()
        man = st.get("maneuver", "") or ""
        mod = ("uturn" if "uturn" in man else "slight left" if "slight-left" in man else
               "slight right" if "slight-right" in man else "sharp left" if "sharp-left" in man else
               "sharp right" if "sharp-right" in man else "left" if "left" in man else
               "right" if "right" in man else "straight")
        steps.append({"text": txt, "type": "depart" if not steps else "turn", "modifier": mod,
                      "lat": st["start_location"]["lat"], "lng": st["start_location"]["lng"],
                      "dist_m": st["distance"]["value"], "dur_s": st["duration"]["value"]})
        coords += _decode_poly(st["polyline"]["points"])
    steps.append({"text": "You have arrived", "type": "arrive", "modifier": "",
                  "lat": leg["end_location"]["lat"], "lng": leg["end_location"]["lng"],
                  "dist_m": 0, "dur_s": 0})
    dur = (leg.get("duration_in_traffic") or leg["duration"])["value"]
    return {"coords": coords, "steps": steps, "distance_m": leg["distance"]["value"],
            "duration_s": dur, "source": "google"}


@app.get("/api/driver/geocode")
def api_driver_geocode():
    """Look up a typed address for the driver app's built-in navigation."""
    if not session.get("driver_id") and not dispatcher_required():
        return jsonify({"ok": False}), 403
    q = " ".join((request.args.get("q") or "").split())[:200]
    if len(q) < 3:
        return jsonify({"ok": False, "error": "Type an address first."}), 400
    near = _ll(request.args.get("near"))
    def far(res):
        if not near or not res.get("ok"):
            return False
        return hav_m(near, (res["lat"], res["lng"])) > 160000
    tries = [q]
    biz = (setting("business_address", str) or "").split(",")
    if len(biz) >= 3:
        area = ",".join(biz[-2:]).strip()
        tries.append(q + ", " + area)
    best = None
    for t in tries:
        try:
            res = geocode(t)
        except Exception:
            res = {"ok": False}
        if res.get("ok") and not far(res):
            best = res
            break
        if res.get("ok") and best is None:
            best = res
    if not best or not best.get("ok"):
        return jsonify({"ok": False, "error": "Couldn't find that address. Add the city or zip and try again."}), 404
    return jsonify({"ok": True, "lat": best["lat"], "lng": best["lng"], "formatted": best["formatted"]})

def hav_m(a, b):
    import math
    R = 6371000.0
    t = math.pi / 180
    dl, dn = (b[0] - a[0]) * t, (b[1] - a[1]) * t
    x = math.sin(dl / 2) ** 2 + math.cos(a[0] * t) * math.cos(b[0] * t) * math.sin(dn / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))

@app.get("/api/driver/route")
def api_driver_route():
    """Turn-by-turn driving directions for the driver app's built-in navigation."""
    if not session.get("driver_id") and not dispatcher_required():
        return jsonify({"ok": False}), 403
    a, b = _ll(request.args.get("from")), _ll(request.args.get("to"))
    if not a or not b:
        return jsonify({"ok": False, "error": "Need your location and the stop's location."}), 400
    key = "%.4f,%.4f>%.5f,%.5f" % (a[0], a[1], b[0], b[1])
    hit = _ROUTE_CACHE.get(key)
    if hit and time.time() - hit[0] < 120:
        return jsonify(dict(hit[1], ok=True))
    rt = None
    for fn in ((_route_google_routes, _route_google, _route_osrm) if GOOGLE_KEY else (_route_osrm,)):
        try:
            rt = fn(a, b)
        except Exception:
            rt = None
        if rt:
            break
    if not rt:
        return jsonify({"ok": False, "error": "Couldn't get directions right now. Check your signal and try again."}), 502
    if len(_ROUTE_CACHE) > 500:
        _ROUTE_CACHE.clear()
    _ROUTE_CACHE[key] = (time.time(), rt)
    return jsonify(dict(rt, ok=True))

@app.get("/api/driver/state")
def api_driver_state():
    backfill_primary()
    remind_unreceived()
    short_staff_alert()
    try:
        payout_sweep(); auto_driver_pay_sweep()
    except Exception as e:
        print('driver poll pay sweep:', e)
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    if not d or not (d["active"] if d["active"] is not None else 1):
        session.pop("driver_id", None)
        return jsonify({"ok": False, "inactive": True,
                        "error": "Your driver account is inactive. Call dispatch."}), 403
    auto_assign()
    d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    recompute_queue()
    lines = line_positions()
    rotation = {k: v["pos"] for k, v in lines.items()}
    lineups = region_lineups()
    dr = driver_work_regions(did)
    waiting = len([1 for o in db().execute("""SELECT region_id FROM orders
                              WHERE dispatch_status IN ('queued','held')""").fetchall() if driver_covers(dr, o["region_id"])])
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
                    "dispatch_tel": tel_digits(dispatch_phone(driver_phone_region(did))),
                    "dispatch_phone": nice_phone(dispatch_phone(driver_phone_region(did))),
                    "late": [x for x in late_accepts() if x["kind"] == "driver" and x["driver_id"] == did],
                    "business_name": (setting("business_name", str) or "Fleet Foot Delivery"),
                    "scheduled": scheduled,
                    "done": [order_dict(o) for o in done],
                    "driver": {"name": d["name"], "status": d["status"],
                               "pending_request": d["pending_request"], "max_stack": d["max_stack"],
                               "up_next": rotation.get(d["id"]), "waiting_count": waiting,
                               "region_lines": driver_region_lines(lineups, d["id"]),
                               "at_limit": (lines.get(d["id"]) or {}).get("at_limit", False),
                               "roster": d["roster"],
                               "region_queues": region_queues(dr, detail=False)},
                    "availability": availability_for(did),
                    "stack": [order_dict(o) for o in mine]})

@app.post("/api/driver/request")
def api_driver_request():
    """Drivers request a status change. Only dispatch can grant it."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    body = request.get_json(force=True) or {}
    want = body.get("status")
    if want not in ("online", "break", "offline"):
        return jsonify({"ok": False}), 400
    extra = ""
    if want == "online" and "regions" in body and today_slot_regions("driver", did):
        err = save_day_pick("driver", did, body.get("regions"))
        if err:
            return jsonify({"ok": False, "error": err}), 400
        extra = " for " + region_names(day_pick("driver", did) or today_slot_regions("driver", did))
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (did, "driver", "Requesting " + want + extra + ".", now()))
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

# Full data export for the developer login. The service agreement promises the client an
# export of its data (customers, orders, drivers, restaurants, rewards, gift cards...) on
# request, so a developer can download every table as a CSV file inside one zip. Passwords,
# PINs, reset codes, saved-card vault ids and API keys/secrets are never included.
_EXPORT_SKIP_TABLES = {"geocache", "sqlite_sequence", "ff_meta"}
_EXPORT_SECRET_COL = re.compile(r"(password|passwd|pw_hash|pin|pin_hash|code_hash|secret|vault_id|vault_src|api_key|access_token|refresh_token)$", re.I)
_EXPORT_SECRET_SETTING = re.compile(r"(secret|password|passwd|api_key|apikey|client_id|token|_key$|^key_|pin$)", re.I)


def _export_tables():
    con = db()
    if dbx.PG:
        rows = con.raw("""SELECT table_name FROM information_schema.tables
                          WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name""")
        names = [r[0] for r in rows]
    else:
        names = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    return [n for n in names if n.lower() not in _EXPORT_SKIP_TABLES]


@app.get("/dispatch/export/all.zip")
def export_all_data():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    if not is_dev():
        return "Only a developer account can export all data.", 403
    import csv, io, zipfile
    from flask import send_file
    buf = io.BytesIO()
    summary = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table in _export_tables():
            try:
                cols = dbx.columns(db(), table)
                keep = [c for c in cols if not _EXPORT_SECRET_COL.search(c)]
                if not keep:
                    continue
                rows = db().execute("SELECT %s FROM %s" % (",".join(keep), table)).fetchall()
            except Exception as e:
                summary.append("%s: skipped (%s)" % (table, str(e)[:120]))
                continue
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(keep)
            n = 0
            for r in rows:
                vals = list(r)
                if table == "settings" and keep[:1] == ["key"] and _EXPORT_SECRET_SETTING.search(str(vals[0] or "")):
                    continue
                w.writerow(["" if v is None else (v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray, memoryview)) and not isinstance(v, memoryview) else (bytes(v).decode("utf-8", "replace") if isinstance(v, memoryview) else v)) for v in vals])
                n += 1
            z.writestr(table + ".csv", "\ufeff" + s.getvalue())
            hidden = [c for c in cols if c not in keep]
            summary.append("%s.csv: %d rows%s" % (table, n, (" (left out: " + ", ".join(hidden) + ")") if hidden else ""))
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        z.writestr("README.txt",
                   "Data export from %s\nMade %s by %s\n\n"
                   "Every table is one CSV file (opens in Excel or Google Sheets).\n"
                   "Passwords, PINs, reset codes, saved-card vault ids and API keys/secrets are not included.\n\n%s\n"
                   % (setting("business_name", str) or "Fleet Foot Delivery", stamp,
                      session.get("dispatcher_name") or "developer", "\n".join(summary)))
    log("export", "%s exported all data (%d tables)" % (session.get("dispatcher_name") or "developer", len(summary)))
    buf.seek(0)
    name = (setting("business_name", str) or "data").replace(" ", "-")
    return send_file(buf, as_attachment=True, mimetype="application/zip",
                     download_name="%s-data-export-%s.zip" % (name, dt.date.today().isoformat()))

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
        if row and not restaurant_fits_brand(row):
            # Right store code and PIN but another company's app: keep them out and name their company.
            rs = site_of_region(row["region_id"]) if row["region_id"] else None
            err = wrong_brand_msg(staff_brand_site(), "restaurant",
                                  home_brand_name_for({int(rs["id"])} if rs is not None else set()))
        elif row:
            session["restaurant_id"] = row["id"]
            session["restaurant_name"] = row["name"]
            return redirect(url_for("rest_home"))
        else:
            err = "Wrong store code or PIN."
    if request.method == "GET" and session.get("restaurant_id"):
        site = staff_brand_site()
        r = db().execute("SELECT * FROM restaurants WHERE id=?", (session["restaurant_id"],)).fetchone()
        if restaurant_fits_brand(r, site):
            return redirect(url_for("rest_home"))
        rs = site_of_region(r["region_id"]) if r is not None and r["region_id"] else None
        err = wrong_brand_msg(site, "restaurant", home_brand_name_for({int(rs["id"])} if rs is not None else set()))   # switched to another brand's company
        session.pop("restaurant_id", None)
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
    _ph = dispatch_phone(r["region_id"])
    return render_template("rest.html", r=r, open=is_open(r), hours=hours_label(r),
                           awake_default=awake_default("kitchen"),
                           dispatch_phone=nice_phone(_ph), dispatch_tel=tel_digits(_ph),
                           region_phone=nice_phone(_ph) if r["region_id"] and _ph != dispatch_phone() else "")

@app.get("/api/restaurant/menu")
def api_restaurant_menu():
    """The kitchen app's own menu, so the restaurant can block items it has run out of."""
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    rows = db().execute("""SELECT id, name, section, price_cents, active, menu_tab FROM menu_items
                           WHERE restaurant_id=? ORDER BY menu_tab, sort, id""", (rid,)).fetchall()
    return jsonify({"ok": True, "items": [{"id": r["id"], "name": r["name"], "section": r["section"] or "",
                                           "tab": r["menu_tab"] or "", "price": money(r["price_cents"] or 0),
                                           "active": int(r["active"] or 0)} for r in rows]})


@app.post("/api/restaurant/menu-block")
def api_restaurant_menu_block():
    rid = session.get("restaurant_id")
    if not rid:
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    b = request.get_json(force=True) or {}
    it = db().execute("SELECT id, name FROM menu_items WHERE id=? AND restaurant_id=?",
                      (b.get("item_id"), rid)).fetchone()
    if not it:
        return jsonify({"ok": False, "error": "That item is not on your menu."}), 404
    on = 0 if b.get("block") else 1
    db().execute("UPDATE menu_items SET active=? WHERE id=?", (on, it["id"]))
    db().commit()
    log("menu", (session.get("restaurant_name") or "Restaurant") + (" unblocked " if on else " blocked ") + it["name"])
    return jsonify({"ok": True, "active": on})


@app.get("/api/restaurant/orders")
def api_rest_orders():
    backfill_primary()
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
    auto_kitchen_sweep()
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
                    "dispatch_ordering": dispatch_ordering, "chat_on": rest_chat_allowed(rid)})

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
    if not OPEN_24_ON:
        return jsonify({"ok": False, "error": "Open 24 hours was removed. Set the hours in Restaurant hours."}), 400
    flip = 0 if r["open_24"] else 1
    db().execute("UPDATE restaurants SET open_24=?, closed_override=0 WHERE id=?", (flip, rid))
    db().commit()
    if not session.get("restaurant_id") or dispatcher_required():
        rest_auto_status(rid, "Dispatch set your restaurant to open 24 hours." if flip
                  else "Dispatch turned off 24 hours. Your regular hours are back.")
    return jsonify({"ok": True, "open_24": bool(flip)})


# ---------------- dispatcher management APIs ----------------
def manage_driver_rows():
    """Driver cards for Restaurants and drivers (no menus, so it is quick)."""
    return [{"id": d["id"], "name": d["name"], "phone": d["phone"], "pin": d["pin"],
                "status": d["status"], "payout_wallet": d["payout_wallet"] or "paypal",
                "active": 0 if d["active"] == 0 else 1,
                "bank_name": d["bank_name"] or "", "bank_last4": d["bank_last4"] or "",
                "payout_email": d["payout_email"] or "", "payout_phone": d["payout_phone"] or "",
                "payout_branch_id": d["payout_branch_id"] or "", "auto_pay": 0 if d["auto_pay"] == 0 else 1,
                "branch_account_id": d["branch_account_id"] or 0,
                "payout_branch_ids": {str(k): v for k, v in br_worker_ids(d).items()},
                "active_orders": db().execute("""SELECT COUNT(*) c FROM orders WHERE driver_id=?
                                   AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""",
                                              (d["id"],)).fetchone()["c"],
                "regions_label": region_names(driver_region_ids(d["id"])) if driver_region_ids(d["id"]) else "No region",
                "locked": not driver_unlocked(d["id"]),
                "unlock_block": driver_unlock_block(d["id"]) or ""}
               for d in scoped_drivers(db().execute("SELECT * FROM drivers ORDER BY name").fetchall())]


@app.get("/api/dispatch/drivers-manage")
def api_drivers_manage():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "drivers": manage_driver_rows(), "owner": is_owner(),
                    "branch_accounts": [{"id": a["id"], "name": a["name"]} for a in _br_rows()]})


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
    drivers = manage_driver_rows()
    return jsonify({"ok": True, "restaurants": rests, "drivers": drivers, "owner": is_owner(),
                    "branch_accounts": [{"id": a["id"], "name": a["name"]} for a in _br_rows()]})


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


DRIVER_UNLOCK_SECS = 15 * 60


def driver_unlocked(drv_id):
    """Owners never see the lock. Others need a fresh username + password unlock for this driver."""
    if is_owner():
        return True
    t = (session.get("drv_unlock") or {}).get(str(drv_id))
    return bool(t and time.time() - t < DRIVER_UNLOCK_SECS)


def driver_unlock_block(drv_id):
    """Why this dispatcher can't unlock this driver, or None when they can."""
    if is_owner():
        return None
    mine = dispatcher_region_ids(session.get("dispatcher_id"))
    theirs = driver_region_ids(drv_id)
    if not theirs:
        return None
    if not mine:
        return "You are not assigned a region yet, so you can't edit drivers. Ask the owner to assign you one."
    if not (mine & theirs):
        return "This driver works " + region_names(theirs) + ". Only a dispatcher assigned to that region can edit them."
    return None


def relock_driver(drv_id):
    u = dict(session.get("drv_unlock") or {})
    if u.pop(str(drv_id), None) is not None:
        session["drv_unlock"] = u


@app.post("/api/dispatch/driver-unlock")
def api_driver_unlock():
    if not dispatcher_required():
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    me = session.get("dispatcher_id")
    b = request.get_json(force=True) or {}
    try:
        drv = int(b.get("driver_id") or 0)
    except (TypeError, ValueError):
        drv = 0
    d = db().execute("SELECT id, name FROM drivers WHERE id=?", (drv,)).fetchone()
    if not d:
        return jsonify({"ok": False, "error": "Driver not found."}), 404
    if b.get("op") == "lock":
        relock_driver(drv)
        return jsonify({"ok": True})
    if is_owner(me):
        return jsonify({"ok": True})
    why = driver_unlock_block(drv)
    if why:
        return jsonify({"ok": False, "error": why}), 403
    nowt = time.time()
    fails = [t for t in _DEL_FAILS.get(me, []) if nowt - t < 600]
    _DEL_FAILS[me] = fails
    if len(fails) >= 5:
        return jsonify({"ok": False, "error": "Too many wrong tries. Try again in 10 minutes."}), 429
    user = (b.get("username") or "").strip()
    pw = b.get("password") or ""
    row = db().execute("SELECT name, username FROM dispatchers WHERE id=? AND password=?", (me, pw)).fetchone()
    if not user or not pw or not row or row["username"].lower() != user.lower():
        fails.append(nowt)
        return jsonify({"ok": False, "error": "That username or password is not right."}), 403
    _DEL_FAILS.pop(me, None)
    u = dict(session.get("drv_unlock") or {})
    u[str(drv)] = nowt
    session["drv_unlock"] = u
    log("driver", row["name"] + " unlocked " + d["name"] + " for editing")
    return jsonify({"ok": True, "minutes": DRIVER_UNLOCK_SECS // 60})


@app.post("/api/dispatch/driver")
def api_driver_crud():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True)
    op = b.get("op")
    if op in ("update", "delete"):
        bad = out_of_scope(b.get("driver_id"))
        if bad:
            return bad
    if op in ("update", "delete") and not driver_unlocked(b.get("driver_id")):
        return jsonify({"ok": False, "error": "This driver is locked. Press Unlock and enter your username and password first."}), 403
    if op in ("update", "delete") and not is_owner():
        relock_driver(b.get("driver_id"))      # one save per unlock, then it locks again
    if op == "create":
        name = (b.get("name") or "").strip()
        phone = digits(b.get("phone") or "")
        pin = (b.get("pin") or "").strip() or "1234"
        if not name or len(phone) < 10:
            return jsonify({"ok": False, "error": "Name and a 10 digit phone are required."}), 400
        if db().execute("SELECT 1 FROM drivers WHERE phone=?", (phone,)).fetchone():
            return jsonify({"ok": False, "error": "That phone is already on a driver."}), 400
        cur = db().execute("INSERT INTO drivers(name,phone,pin,status,max_stack) VALUES(?,?,?,'offline',?)",
                           (name, phone, pin, default_stack_limit()))
        db().commit()
        return jsonify({"ok": True, "driver_id": cur.lastrowid})
    if op == "update":
        phone = digits(b.get("phone") or "") if b.get("phone") else None
        if phone and db().execute("SELECT 1 FROM drivers WHERE phone=? AND id!=?",
                                  (phone, b["driver_id"])).fetchone():
            return jsonify({"ok": False, "error": "Another driver already uses that phone."}), 400
        if "payout_wallet" in b:
            w = (b.get("payout_wallet") or "").lower()
            w = w if w in ("venmo", "check", "branch") else "paypal"   # auto pay sends PayPal / Venmo / Branch; check is paid by hand
            bw = " ".join(str(b.get("payout_branch_id") or "").split())[:64]
            _has_ids = isinstance(b.get("payout_branch_ids"), dict) and any(str(v or "").strip() for v in b["payout_branch_ids"].values())
            if w == "branch" and not bw and not _has_ids:
                return jsonify({"ok": False, "error": "Enter the driver's Branch worker ID to pay them by Branch."}), 400
            db().execute("UPDATE drivers SET payout_branch_id=? WHERE id=?", (bw or None, b["driver_id"]))
            try:
                _ba = int(b.get("branch_account_id") or 0) or None
            except (TypeError, ValueError):
                _ba = None
            db().execute("UPDATE drivers SET branch_account_id=? WHERE id=?", (_ba, b["driver_id"]))
            if isinstance(b.get("payout_branch_ids"), dict):
                _ids = {}
                for _k, _v in b["payout_branch_ids"].items():
                    try:
                        _k = int(_k)
                    except (TypeError, ValueError):
                        continue
                    _v = " ".join(str(_v or "").split())[:64]
                    if _k and _v:
                        _ids[str(_k)] = _v
                db().execute("UPDATE drivers SET payout_branch_ids=? WHERE id=?", (json.dumps(_ids) if _ids else None, b["driver_id"]))
            bn = " ".join(str(b.get("bank_name") or "").split())[:40]
            l4 = digits(b.get("bank_last4") or "")
            if l4 and len(l4) != 4:
                return jsonify({"ok": False, "error": "For the bank, enter only the last 4 digits of the account."}), 400
            db().execute("UPDATE drivers SET bank_name=?, bank_last4=? WHERE id=?", (bn or None, l4 or None, b["driver_id"]))
            em = (b.get("payout_email") or "").strip()[:120]
            vp = digits(b.get("payout_phone") or "")
            if len(vp) == 11 and vp.startswith("1"):
                vp = vp[1:]
            if em and ("@" not in em or "." not in em.split("@")[-1]):
                return jsonify({"ok": False, "error": "That PayPal email doesn't look right."}), 400
            if vp and len(vp) != 10:
                return jsonify({"ok": False, "error": "The Venmo phone needs 10 digits."}), 400
            db().execute("UPDATE drivers SET payout_wallet=?, payout_email=?, payout_phone=? WHERE id=?",
                         (w, em or None, vp or None, b["driver_id"]))
        if "auto_pay" in b:
            db().execute("UPDATE drivers SET auto_pay=? WHERE id=?", (1 if b.get("auto_pay") in (1, "1", True) else 0, b["driver_id"]))
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
    try:
        init = {"ok": True, "drivers": manage_driver_rows(), "owner": is_owner(),
                "branch_accounts": [{"id": a["id"], "name": a["name"]} for a in _br_rows()]}
    except Exception:
        init = None
    return render_template("dispatch_manage.html", init_drivers=init)


# ---------------- photos ----------------
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
PHOTO_MAX_BYTES = 10 * 1024 * 1024


def media_url(name):
    if not name:
        return ""
    if name.startswith("http://") or name.startswith("https://"):
        return name
    return "/media/" + name


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
                      "image": item_picture(it), "groups": item_options(it["id"]),
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

# Home page text dispatch can edit in Settings > Customer website. Blank goes back to the default.
SITE_TEXT = (
    ("site_announce", "", 300),
    ("home_sub", "", 300),
    ("how_title", "How it works", 60),
    ("how1_t", "Tell us where you are", 60),
    ("how1_p", "Type your delivery address above and we show you who delivers to you and what the delivery fee is.", 300),
    ("how2_t", "Fill your basket", 60),
    ("how2_p", "Pick a restaurant below and tell us what you'd like. Order now or schedule it for later.", 300),
    ("how3_t", "Sit back and relax", 60),
    ("how3_p", "Check out, and you'll usually have your food in 30 to 60 minutes. Track your driver the whole way.", 300),
    ("rest_title", "Restaurants", 60),
    ("closed_msg", "We are closed right now. You can still place a future order: pick a restaurant and choose Schedule for later.", 300),
    ("any_text", "Order from any restaurant in the area. Tell us the place, the items, and the prices, and your driver picks it up.", 300),
    ("pocket_title", "From your pocket to your front porch", 80),
    ("pocket_text", "Order from your phone in a few taps. Add {business} to your home screen and it opens like an app.", 400),
)
SITE_IMAGES = {"hero_image": "/static/home-hero.jpg", "pocket_image": "/static/home-burger.jpg"}


def site_text(raw=False):
    out = {}
    biz_name = (setting("business_name", str) or "Fleet Foot Delivery").strip() or "Fleet Foot Delivery"
    for k, d, _n in SITE_TEXT:
        v = (setting(k, str) or "").strip() or d
        out[k] = v if raw else v.replace("{business}", biz_name)
    for k, d in SITE_IMAGES.items():
        name = (setting(k, str) or "").strip()
        out[k] = media_url(name) if name and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name))) else d
    return out


@app.post("/api/dispatch/site-image")
def api_site_image():
    """Change (or reset) the home page's big top photo or the phone photo."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    key = request.form.get("which") or ""
    if key not in SITE_IMAGES:
        return jsonify({"ok": False, "error": "Unknown picture."}), 400
    old = (setting(key, str) or "").strip()
    if request.form.get("op") == "reset":
        drop_media(old)
        db().execute("DELETE FROM settings WHERE key=?", (key,))
        db().commit()
        return jsonify({"ok": True, "url": SITE_IMAGES[key]})
    fs = request.files.get("photo")
    if not fs:
        return jsonify({"ok": False, "error": "Choose a picture to upload."}), 400
    raw = fs.read(PHOTO_MAX_BYTES + 1)
    if len(raw) > PHOTO_MAX_BYTES:
        return jsonify({"ok": False, "error": "That picture is over 10 MB. Pick a smaller one."}), 400
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((2000, 2000))
        name = secrets.token_hex(10) + ".jpg"
        im.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85, optimize=True)
    except Exception:
        return jsonify({"ok": False, "error": "That picture could not be read. Try a PNG or JPG."}), 400
    drop_media(old)
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, name))
    db().commit()
    return jsonify({"ok": True, "url": media_url(name)})


def site_logo_url(s):
    """A brand site's own logo, or '' when it has none."""
    if s is not None and (s["logo"] or "").strip() and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(s["logo"]))):
        return media_url(s["logo"])
    return ""


def logo_url():
    s = current_site()
    if s is not None and (s["logo"] or "").strip() and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(s["logo"]))):
        return media_url(s["logo"])
    name = (setting("logo_image", str) or "").strip()
    if name and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name))):
        return media_url(name)
    return DEFAULT_LOGO


def logo_path():
    s = current_site()
    if s is not None and (s["logo"] or "").strip():
        sp = os.path.join(UPLOAD_DIR, os.path.basename(s["logo"]))
        if os.path.exists(sp):
            return sp
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


# --- phone apps (Google Play / App Store via PWABuilder) ---------------------
# Each app has its own install manifest. Names follow the business name in Settings,
# so every client's copy shows its own brand. Enter these links in PWABuilder:
#   customer  https://<domain>/          driver  https://<domain>/driver
#   kitchen   https://<domain>/restaurant
APP_MANIFESTS = {
    "customer": {"suffix": "", "short": None, "start": "/", "scope": "/", "orientation": "portrait",
                 "bg": "#ffffff", "theme": "#e53935",
                 "desc": "Order food delivery from local restaurants, track your driver and earn rewards."},
    "driver": {"name": "Fleet Foot Driver", "suffix": " Driver", "short": "FF Driver", "start": "/driver", "scope": "/driver",
               "orientation": "portrait", "bg": "#0f172a", "theme": "#0f172a",
               "desc": "Driver app: go online, accept deliveries, navigate and get paid."},
    "kitchen": {"name": "Fleet Foot Restaurant", "suffix": " Kitchen", "short": "FF Restaurant", "start": "/restaurant", "scope": "/restaurant",
                "orientation": "portrait", "bg": "#7c2d12", "theme": "#7c2d12",
                "desc": "Restaurant app: receive delivery orders, mark them ready and chat with dispatch."},
    # the shared staff apps (one store listing for every client company)
    "hub-driver": {"name": "Fleet Foot Driver", "suffix": "", "short": "FF Driver", "start": "/go/driver",
                   "scope": "/go/driver", "orientation": "portrait", "bg": "#0f172a", "theme": "#0f172a",
                   "desc": "Driver app for every delivery company on Fleet Foot Delivery. Pick your company and sign in."},
    "hub-kitchen": {"name": "Fleet Foot Restaurant", "suffix": "", "short": "FF Restaurant", "start": "/go/kitchen",
                    "scope": "/go/kitchen", "orientation": "portrait", "bg": "#7c2d12", "theme": "#7c2d12",
                    "desc": "Restaurant app for every delivery company on Fleet Foot Delivery. Pick your company and sign in."},
    "tracker": {"suffix": " Tracker", "short": "Tracker", "start": "/dispatch/map", "scope": "/dispatch/map",
                "orientation": "any", "bg": "#1B2A41", "theme": "#1B2A41",
                "desc": "Live map of drivers for dispatchers."},
}


@app.get("/manifest/<which>.json")
def app_manifest(which):
    m = APP_MANIFESTS.get(which)
    if not m:
        return jsonify({"error": "unknown app"}), 404
    try:
        biz = (setting("business_name", str) or "Fleet Foot Delivery").strip() or "Fleet Foot Delivery"
    except Exception:
        biz = "Fleet Foot Delivery"
    name = m.get("name") or (biz + m["suffix"])[:45]
    short = m["short"] or (biz if len(biz) <= 12 else biz.split()[0][:12])
    if which in ("driver", "hub-driver", "kitchen", "hub-kitchen"):
        # the staff apps are Fleet Foot Delivery's own: always its logo, never a client brand's
        _ic = "driver" if "driver" in which else "kitchen"
        icons = [{"src": "/static/icons/%s-%d.png" % (_ic, s), "sizes": "%dx%d" % (s, s), "type": "image/png",
                  "purpose": "any"} for s in (192, 512)]
    else:
        icons = [{"src": "/brand/icon-%d.png" % s, "sizes": "%dx%d" % (s, s), "type": "image/png", "purpose": "any"}
                 for s in (192, 512)]
        icons += [{"src": "/brand/maskable-%d.png" % s, "sizes": "%dx%d" % (s, s), "type": "image/png",
                   "purpose": "maskable"} for s in (192, 512)]
    body = {
        "id": m["start"], "name": name, "short_name": short, "description": m["desc"],
        "start_url": m["start"], "scope": m["scope"], "display": "standalone",
        "orientation": m["orientation"], "background_color": m["bg"], "theme_color": m["theme"],
        "lang": "en-US", "dir": "ltr", "categories": ["food", "business"],
        "prefer_related_applications": False, "icons": icons,
    }
    import json as _json
    resp = app.response_class(_json.dumps(body, indent=2), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp


def _tab_logo_file(src):
    """The file behind a logo address the page shows (/static/... or /media/...), else ''."""
    try:
        from urllib.parse import urlparse
        path = urlparse(src or "").path or ""
    except Exception:
        return ""
    if path.startswith("/static/"):
        f = os.path.normpath(os.path.join(APP_DIR_STATIC, path[len("/static/"):]))
        if f.startswith(APP_DIR_STATIC + os.sep) and os.path.isfile(f):
            return f
    if path.startswith("/media/"):
        f = os.path.join(UPLOAD_DIR, os.path.basename(path))
        if os.path.isfile(f):
            return f
    return ""


def _tab_icon_many(files, size):
    """Several brand logos in one square icon: two on top and one centered below (or a 2x2)."""
    from PIL import Image
    import io
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    half = size // 2
    cells = [(0, 0), (half, 0), (half // 2, half)] if len(files) == 3 else \
            [(0, 0), (half, 0), (0, half), (half, half)][:len(files)]
    if len(files) == 2:
        cells = [(0, half // 2), (half, half // 2)]
    for f, (x, y) in zip(files, cells):
        im = Image.open(f).convert("RGBA")
        bb = im.getbbox()
        if bb:
            im = im.crop(bb)
        box = int(half * 0.96)
        k = box / max(im.size)
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
        canvas.paste(im, (x + (half - im.width) // 2, y + (half - im.height) // 2), im)
    out = io.BytesIO()
    canvas.save(out, "PNG")
    resp = app.response_class(out.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/brand/tab-<int:size>.png")
def brand_tab_icon(size):
    """Browser-tab icon made from the logo the page itself shows (?src=), so each brand's
    tab carries that brand's logo. The address differs per logo, so tabs never mix them up."""
    size = size if size in (32, 64, 180) else 64
    files = [x for x in (_tab_logo_file(v) for v in request.args.getlist("src")[:4]) if x]
    if len(files) > 1:
        try:
            return _tab_icon_many(files, size)
        except Exception:
            pass
    f = files[0] if files else os.path.join(APP_DIR_STATIC, "brand", "logo-default.png")
    try:
        from PIL import Image
        import io
        src = Image.open(f).convert("RGBA")
        bbox = src.getbbox()
        if bbox:
            src = src.crop(bbox)
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        box = int(size * 0.94)
        k = box / max(src.size)
        src = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
        canvas.paste(src, ((size - src.width) // 2, (size - src.height) // 2), src)
        out = io.BytesIO()
        canvas.save(out, "PNG")
        resp = app.response_class(out.getvalue(), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:
        return redirect(DEFAULT_LOGO)


@app.get("/brand/maskable-<int:size>.png")
def brand_maskable(size):
    """Android adaptive icon: the logo kept inside the safe circle on a solid background."""
    size = size if size in (192, 512) else 512
    try:
        from PIL import Image
        import io
        src = Image.open(logo_path()).convert("RGBA")
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        box = int(size * 0.62)
        k = box / max(src.size)
        src = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
        canvas.paste(src, ((size - src.width) // 2, (size - src.height) // 2), src)
        out = io.BytesIO()
        canvas.convert("RGB").save(out, "PNG")
        resp = app.response_class(out.getvalue(), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:
        return redirect("/brand/icon-%d.png" % size)


@app.get("/sw.js")
def root_service_worker():
    """Service worker served from the site root so it covers every page (needed for the store apps)."""
    resp = send_from_directory(APP_DIR_STATIC, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp



# --- privacy policy and account deletion pages (needed for Google Play and the App Store) ---
# Contact shown on both pages: Railway variable PRIVACY_CONTACT_EMAIL (e.g. privacy@fleetfootdelivery.com).
LEGAL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:760px;margin:0 auto;padding:24px 18px 60px;color:#1f2937;line-height:1.55}
h1{font-size:26px;margin:8px 0 4px}h2{font-size:18px;margin:26px 0 6px}.muted{color:#6b7280;font-size:14px}
header{display:flex;align-items:center;gap:10px}header img{height:44px}</style></head><body>
<header><a href="/"><img src="{{ logo }}" alt="{{ brand }}"></a><strong>{{ brand }}</strong></header>
{{ body|safe }}
<p class="muted" style="margin-top:36px"><a href="/">Back to {{ brand }}</a> &middot; <a href="/privacy">Privacy policy</a> &middot;
<a href="/delete-account">Delete account</a><br>{{ brand }} runs on Fleet Foot Delivery software.</p></body></html>"""


def _legal_brand():
    """Name and logo of the brand whose web address this is (Tiger Town To Go, Bulldawg Food,
    Crimson To Go...), else Fleet Foot Delivery."""
    name, logo = "Fleet Foot Delivery", DEFAULT_LOGO
    try:
        s = current_site()
        if s is not None:
            name = (s["name"] or "").strip() or name
        else:
            name = (setting("business_name", str) or "").strip() or name
        logo = logo_url() or logo
    except Exception:
        pass
    return name, logo


def _privacy_contact():
    em = (os.environ.get("PRIVACY_CONTACT_EMAIL") or "").strip()
    if em:
        return '<a href="mailto:%s">%s</a>' % (em, em)
    return "the dispatch office of the delivery company you use, or Fleet Foot Delivery LLC, Opelika, Alabama"


@app.get("/privacy")
def privacy_policy():
    c = _privacy_contact()
    body = """<h1>Privacy policy</h1><p class="muted">Effective October 7, 2026</p>
<p>Fleet Foot Delivery LLC ("Fleet Foot") makes the Fleet Foot Driver app, the Fleet Foot Kitchen (restaurant) app
and the ordering websites used by local delivery companies such as Tiger Town To Go, Bulldawg Food and Crimson To Go.
This policy explains what information these apps and websites collect and how it is used.</p>
<h2>Information we collect</h2>
<p><b>Drivers:</b> name, mobile number, sign-in PIN, the regions and companies you work for, your location while you
are online or on a delivery, delivery history, earnings and tips, the payout handle you choose (PayPal, Venmo or Branch),
and messages with dispatch.</p>
<p><b>Restaurants:</b> restaurant name, contact name and phone number, sign-in PIN, orders, invoices and messages with dispatch.</p>
<p><b>Customers:</b> name, phone number, delivery address, order details and delivery instructions. Payments are made
through PayPal; we never see or store your card or bank details.</p>
<h2>How we use it</h2>
<p>Only to run deliveries: sending orders to restaurants, assigning and tracking drivers, showing customers and dispatch
where an order is, paying drivers and restaurants, sending order and shift text messages, and providing support.
Location is used only while a driver is online or on a delivery.</p>
<h2>Who we share it with</h2>
<p>The delivery company you order from or work for, and the service providers we need to run the service: PayPal
(payments), Google Maps (maps and directions), our text message provider, and our hosting provider. We do not sell
personal information and we do not show ads.</p>
<h2>How long we keep it</h2>
<p>Order and payment records are kept as long as needed for accounting and legal reasons. Chat messages are cleared
regularly when the business opens or closes. Driver and restaurant accounts are kept until they are closed.</p>
<h2>Your choices</h2>
<p>You can ask to see, correct or delete your information at any time. See <a href="/delete-account">how to delete your account</a>.
Drivers can stop location sharing by going offline or turning off location for the app in phone settings.</p>
<h2>Children</h2><p>The apps are not meant for children under 13, and we do not knowingly collect their information.</p>
<h2>Changes</h2><p>If this policy changes, the new version will be posted on this page with a new effective date.</p>
<h2>Contact</h2><p>Questions or requests: %s.</p>""" % c
    _b, _l = _legal_brand()
    return render_template_string(LEGAL_PAGE, title="Privacy policy | " + _b, body=body, brand=_b, logo=_l)


@app.get("/delete-account")
def delete_account_info():
    c = _privacy_contact()
    body = """<h1>Delete your account</h1>
<p>Drivers, restaurants and customers of Fleet Foot Delivery (Fleet Foot Driver, Fleet Foot Kitchen and the
Tiger Town To Go, Bulldawg Food and Crimson To Go websites) can ask for their account and personal information to be deleted.</p>
<h2>How to ask</h2><ol><li>Contact %s, or call the dispatch office of the company you work with or order from.</li>
<li>Give your name and the mobile number on the account.</li>
<li>We confirm it is you and delete the account within 30 days.</li></ol>
<h2>What is deleted</h2><p>Your sign-in, name, phone number, saved addresses, location history and messages.</p>
<h2>What we keep</h2><p>Records of completed orders and payments, kept only as long as tax and accounting rules require,
then deleted.</p>""" % c
    _b, _l = _legal_brand()
    return render_template_string(LEGAL_PAGE, title="Delete your account | " + _b, body=body, brand=_b, logo=_l)


@app.get("/.well-known/assetlinks.json")
def android_asset_links():
    """Proves to Android that the Play Store apps belong to this website, so they open full
    screen with no address bar. Paste the assetlinks.json text PWABuilder gives you for each app
    into Railway variables ANDROID_ASSETLINKS_CUSTOMER, ANDROID_ASSETLINKS_DRIVER and
    ANDROID_ASSETLINKS_KITCHEN (any variable starting with ANDROID_ASSETLINKS works)."""
    import json as _json
    out = []
    for k in sorted(os.environ):
        if not k.startswith("ANDROID_ASSETLINKS"):
            continue
        try:
            v = _json.loads(os.environ[k])
        except Exception:
            continue
        out.extend(v if isinstance(v, list) else [v])
    resp = app.response_class(_json.dumps(out, indent=2), mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# --- restaurant invoices (what the business owes each restaurant for its food) ------
# An owner picks a restaurant and dates; the invoice adds up the food (and tax, if chosen)
# on that restaurant's delivered orders not already on another invoice, less any
# commission, plus or minus an adjustment. Paid by check (with the check number) or PayPal.
# Restaurants see their own invoices in the kitchen app.
INV_METHODS = {"check": "Check", "paypal": "PayPal"}


def inv_number(i):
    return "INV-%d" % (1000 + int(i))


def inv_cents(v):
    try:
        return int(round(float(str(v or "0").replace("$", "").replace(",", "").strip() or 0) * 100))
    except Exception:
        return None


def inv_orders(rid, start, end, invoice_id=None):
    day = "substr(COALESCE(delivered_at, created_at),1,10)"
    if invoice_id:
        q = "SELECT * FROM orders WHERE rest_invoice_id=? ORDER BY " + day + ", id"
        return db().execute(q, (invoice_id,)).fetchall()
    q = ("SELECT * FROM orders WHERE restaurant_id=? AND dispatch_status='delivered' AND rest_invoice_id IS NULL"
         " AND " + day + " BETWEEN ? AND ? ORDER BY " + day + ", id")
    return db().execute(q, (rid, start, end)).fetchall()


def inv_math(rows, include_tax, pct, adjust):
    food = sum(int(o["subtotal_cents"] or 0) for o in rows)
    tax = sum(int(o["tax_cents"] or 0) for o in rows) if include_tax else 0
    comm = int(round(food * pct / 100.0))
    return {"orders": len(rows), "food_cents": food, "tax_cents": tax, "commission_cents": comm,
            "adjust_cents": adjust, "total_cents": food + tax - comm + adjust}


def inv_dict(r):
    d = dict(r)
    d["number"] = inv_number(r["id"])
    rr = db().execute("SELECT name FROM restaurants WHERE id=?", (r["restaurant_id"],)).fetchone()
    d["restaurant"] = rr["name"] if rr else "Restaurant"
    for k in ("food_cents", "tax_cents", "commission_cents", "adjust_cents", "total_cents"):
        d[k[:-6]] = money(d[k] or 0)
    d["method_label"] = INV_METHODS.get(d.get("pay_method") or "", "")
    return d


def inv_args(f):
    rid = int(f.get("restaurant_id") or 0)
    start = (f.get("start") or "")[:10]
    end = (f.get("end") or "")[:10]
    try:
        dt.date.fromisoformat(start); dt.date.fromisoformat(end)
    except Exception:
        return None, "Pick a start and end date."
    if end < start:
        return None, "The end date is before the start date."
    if not db().execute("SELECT 1 FROM restaurants WHERE id=?", (rid,)).fetchone():
        return None, "Pick a restaurant."
    try:
        pct = max(0.0, min(100.0, float(f.get("commission_pct") or 0)))
    except Exception:
        return None, "Commission must be a number from 0 to 100."
    adj = inv_cents(f.get("adjust"))
    if adj is None:
        return None, "The adjustment must be a dollar amount, like 12.50 or -5."
    inc = str(f.get("include_tax", "1")).lower() in ("1", "true", "on", "yes")
    return {"rid": rid, "start": start, "end": end, "pct": pct, "adj": adj, "inc": inc,
            "adjust_note": " ".join(str(f.get("adjust_note") or "").split())[:120],
            "notes": str(f.get("notes") or "").strip()[:500]}, None


@app.get("/dispatch/invoices")
def dispatch_invoices():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    if not is_owner():
        return redirect("/dispatch")
    rests = [dict(id=r["id"], name=r["name"]) for r in
             db().execute("SELECT id, name FROM restaurants WHERE slug<>'oneoff' ORDER BY name COLLATE NOCASE").fetchall()]
    invs = [inv_dict(r) for r in db().execute("SELECT * FROM rest_invoices ORDER BY id DESC LIMIT 300").fetchall()]
    return render_template("dispatch_invoices.html", portal="dispatch", rests=rests, invoices=invs,
                           today=dt.date.today().isoformat())


@app.post("/api/dispatch/invoices")
def api_dispatch_invoices():
    if not dispatcher_required() or not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can make or pay restaurant invoices."}), 403
    f = request.get_json(silent=True) or request.form
    op = f.get("op", "preview")
    who = session.get("dispatcher_name") or "Owner"
    now = dt.datetime.now().isoformat(timespec="seconds")
    if op in ("preview", "create"):
        a, err = inv_args(f)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        rows = inv_orders(a["rid"], a["start"], a["end"])
        m = inv_math(rows, a["inc"], a["pct"], a["adj"])
        out = {k: money(v) if k.endswith("_cents") else v for k, v in m.items()}
        if op == "preview":
            return jsonify({"ok": True, "summary": out})
        if not rows and not a["adj"]:
            return jsonify({"ok": False, "error": "No delivered orders for that restaurant in those dates that aren't already on an invoice."}), 400
        if m["total_cents"] < 0:
            return jsonify({"ok": False, "error": "The total comes out below $0. Check the commission and adjustment."}), 400
        cur = db().execute("""INSERT INTO rest_invoices(restaurant_id, period_start, period_end, order_count,
            food_cents, tax_cents, include_tax, commission_pct, commission_cents, adjust_cents, adjust_note,
            total_cents, status, notes, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'unpaid',?,?,?)""",
            (a["rid"], a["start"], a["end"], m["orders"], m["food_cents"], m["tax_cents"], 1 if a["inc"] else 0,
             a["pct"], m["commission_cents"], a["adj"], a["adjust_note"] or None, m["total_cents"],
             a["notes"] or None, now, who))
        iid = cur.lastrowid
        if rows:
            db().executemany("UPDATE orders SET rest_invoice_id=? WHERE id=?", [(iid, o["id"]) for o in rows])
        db().commit()
        try:
            log("invoice", who + " made " + inv_number(iid) + " for " + money(m["total_cents"]))
        except Exception:
            pass
        return jsonify({"ok": True, "invoice": inv_dict(db().execute("SELECT * FROM rest_invoices WHERE id=?", (iid,)).fetchone())})
    iid = int(f.get("id") or 0)
    r = db().execute("SELECT * FROM rest_invoices WHERE id=?", (iid,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "That invoice wasn't found."}), 404
    if op == "pay":
        if r["status"] == "void":
            return jsonify({"ok": False, "error": "That invoice was voided."}), 400
        meth = (f.get("method") or "check").lower()
        if meth not in INV_METHODS:
            return jsonify({"ok": False, "error": "Pick Check or PayPal."}), 400
        chk = "".join(str(f.get("check_number") or "").split())[:20]
        ref = " ".join(str(f.get("ref") or "").split())[:60]
        if meth == "check" and not chk:
            return jsonify({"ok": False, "error": "Enter the check number."}), 400
        paid = (f.get("paid_on") or dt.date.today().isoformat())[:10]
        try:
            dt.date.fromisoformat(paid)
        except Exception:
            return jsonify({"ok": False, "error": "Pick the date it was paid."}), 400
        db().execute("UPDATE rest_invoices SET status='paid', pay_method=?, check_number=?, pay_ref=?, paid_at=?, paid_by=? WHERE id=?",
                     (meth, chk or None, ref or None, paid, who, iid))
        txt = "paid by check #" + chk if meth == "check" else "paid by PayPal"
    elif op == "unpay":
        db().execute("UPDATE rest_invoices SET status='unpaid', pay_method=NULL, check_number=NULL, pay_ref=NULL, paid_at=NULL, paid_by=NULL WHERE id=?", (iid,))
        txt = "marked unpaid"
    elif op == "void":
        if r["status"] == "paid":
            return jsonify({"ok": False, "error": "Mark it unpaid first, then void it."}), 400
        db().execute("UPDATE rest_invoices SET status='void' WHERE id=?", (iid,))
        db().execute("UPDATE orders SET rest_invoice_id=NULL WHERE rest_invoice_id=?", (iid,))
        txt = "voided (its orders can go on a new invoice)"
    else:
        return jsonify({"ok": False, "error": "Unknown action."}), 400
    db().commit()
    try:
        log("invoice", who + " " + txt + ": " + inv_number(iid))
    except Exception:
        pass
    return jsonify({"ok": True, "invoice": inv_dict(db().execute("SELECT * FROM rest_invoices WHERE id=?", (iid,)).fetchone())})


def render_invoice(r, viewer):
    rest = db().execute("SELECT * FROM restaurants WHERE id=?", (r["restaurant_id"],)).fetchone()
    lines = []
    for o in inv_orders(None, None, None, invoice_id=r["id"]):
        lines.append({"code": o["code"], "day": (o["delivered_at"] or o["created_at"] or "")[:10],
                      "food": money(o["subtotal_cents"] or 0),
                      "tax": money(o["tax_cents"] or 0) if r["include_tax"] else ""})
    try:
        logo = logo_url()
    except Exception:
        logo = DEFAULT_LOGO
    return render_template("invoice_view.html", inv=inv_dict(r), rest=rest, lines=lines, viewer=viewer,
                           logo=logo, biz=setting("business_name", str) or "Fleet Foot Delivery",
                           biz_address=setting("business_address", str) or "")


@app.get("/dispatch/invoices/<int:iid>")
def dispatch_invoice_view(iid):
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    if not is_owner():
        return redirect("/dispatch")
    r = db().execute("SELECT * FROM rest_invoices WHERE id=?", (iid,)).fetchone()
    if not r:
        return redirect("/dispatch/invoices")
    return render_invoice(r, "owner")


@app.get("/restaurant/invoices")
def rest_invoices():
    if not session.get("restaurant_id"):
        return redirect(url_for("rest_login"))
    invs = [inv_dict(r) for r in db().execute(
        "SELECT * FROM rest_invoices WHERE restaurant_id=? AND status<>'void' ORDER BY id DESC LIMIT 200",
        (session["restaurant_id"],)).fetchall()]
    return render_template("rest_invoices.html", portal="kitchen", invoices=invs)


@app.get("/restaurant/invoices/<int:iid>")
def rest_invoice_view(iid):
    if not session.get("restaurant_id"):
        return redirect(url_for("rest_login"))
    r = db().execute("SELECT * FROM rest_invoices WHERE id=? AND restaurant_id=? AND status<>'void'",
                     (iid, session["restaurant_id"])).fetchone()
    if not r:
        return redirect("/restaurant/invoices")
    return render_invoice(r, "kitchen")


# --- shared Fleet Foot Driver / Fleet Foot Kitchen apps ----------------------
# One store app each for every client. The app opens /go/driver or /go/kitchen on this
# (the main Fleet Foot) site, the worker picks their company once, and from then on the
# app goes straight to that company's own site. "Switch company" signs them out and
# brings the picker back. Developers manage the company list at /dispatch/companies.
HUB_APPS = {"driver": ("Fleet Foot Driver", "/driver/login", "hub-driver"),
            "kitchen": ("Fleet Foot Restaurant", "/restaurant/login", "hub-kitchen")}


def clean_site_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    from urllib.parse import urlparse
    pr = urlparse(u)
    if not pr.netloc or " " in pr.netloc:
        return ""
    return pr.scheme + "://" + pr.netloc.lower()


def this_platform_hosts():
    """Every web address this copy of the app answers on: its Railway address, the address in use
    right now, each brand's own domains and this app's old Railway names."""
    hosts = set(old_own_railway_hosts())
    for h in (os.environ.get("RAILWAY_PUBLIC_DOMAIN"), request.host if has_request_context() else ""):
        h = _norm_host(h)
        if h:
            hosts.add(h)
    try:
        for r in db().execute("SELECT * FROM sites").fetchall():
            hosts.update(site_domains(r))
    except Exception:
        pass
    return hosts


def company_here(c):
    """True when the company runs on THIS platform (one of this app's brands, like Tiger Town To Go,
    Bulldawg Food and Crimson To Go, sharing one dispatch). False for a separate company running its
    own copy of the app at its own address, with its own database and its own sign-ins."""
    return _norm_host(c.get("url")) in this_platform_hosts()


def companies_with_look():
    return [dict(r, here=company_here(r), **company_look(r)) for r in company_rows(False)]


def dispatch_pick_companies():
    """Companies a dispatcher can pick on this platform's dispatch sign-in page. Only listed, active,
    unlocked companies. Empty when every company is on this platform (nothing to pick), and always
    empty on a separate company's copy, which never shows or reaches other companies."""
    if platform_role() == "separate":
        return []
    rows = []
    for r in company_rows():
        if not (r.get("listed") if r.get("listed") is not None else 1) or company_locked(r):
            continue
        rows.append(dict(name=r["name"], code=r["code"], here=company_here(r),
                         go=live_company_url(r["url"]).rstrip("/") + "/dispatch/login", **company_look(r)))
    return rows if any(not c["here"] for c in rows) else []


def company_rows(only_active=True):
    q = "SELECT * FROM companies" + (" WHERE COALESCE(active,1)=1" if only_active else "") + " ORDER BY name COLLATE NOCASE"
    return [dict(r) for r in db().execute(q).fetchall()]


def company_site(c):
    """The brand a company in the shared apps shows: the brand picked on Companies, else the
    brand whose web address matches the company's, else the brand with the same name."""
    try:
        s = site_by_id(c.get("site_id") or 0)
        if s is not None:
            return s
        rows = db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall()
        host = _norm_host(c.get("url"))
        for row in rows:
            if host and host in site_domains(row):
                return row
        nm = re.sub(r"[^a-z0-9]+", "", (c.get("name") or "").lower())
        for row in rows:
            if nm and re.sub(r"[^a-z0-9]+", "", (row["name"] or "").lower()) == nm:
                return row   # "Tiger Town To Go" matches "TigerTownToGo"
    except Exception:
        pass
    return None


def brand_logo(s):
    """The logo a brand's own website shows: its Design logo, else the main business logo
    (Tiger Town's tiger), else Fleet Foot Delivery's."""
    logo = site_logo_url(s) if s is not None else ""
    if not logo and s is not None:
        key = (s["name"] or "").lower().replace(" ", "")
        if key.startswith("tigertown") or "tigertown" in (s["domains"] or "").lower():
            logo = "/static/brand/tigertown-logo.png"
    if not logo:
        try:
            name = (setting("logo_image", str) or "").strip()
            if name and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name))):
                logo = media_url(name)
        except Exception:
            logo = ""
    return logo or DEFAULT_LOGO


def main_brand_logo():
    """The main business's own logo (Tiger Town's tiger), else the tiger file, else Fleet Foot's."""
    try:
        name = (setting("logo_image", str) or "").strip()
        if name and os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name))):
            return media_url(name)
    except Exception:
        pass
    return "/static/brand/tigertown-logo.png"


def main_brand_name():
    return (setting("business_name", str) or "").strip() or "Tiger Town To Go"


def company_look(c):
    """Brand name, logo and brand id for a company's button in the shared apps."""
    s = company_site(c)
    if s is not None:
        logo, brand, bs = brand_logo(s), s["name"] or c.get("name") or "", s["id"]
    else:
        # no brand row: the company is the main business (Tiger Town To Go), with the main logo
        logo, brand, bs = main_brand_logo(), c.get("name") or main_brand_name(), "main"
    if logo.startswith("/") and has_request_context():
        logo = request.host_url.rstrip("/") + logo
    return {"brand": brand, "logo": logo, "bs": bs}


def old_own_railway_hosts():
    """Railway addresses THIS app used before it was renamed. Only these get moved to the current
    address; another company's own railway.app instance is left alone. Add more old names with the
    Railway variable PREVIOUS_RAILWAY_DOMAINS (comma separated)."""
    extra = re.split(r"[\s,]+", os.environ.get("PREVIOUS_RAILWAY_DOMAINS") or "")
    return {d for d in (_norm_host(x) for x in ["tigertowntogo.up.railway.app"] + extra) if d}


def live_company_url(u):
    """A company saved with this app's OLD Railway address (the app's railway.app name was changed) opens
    on the address this app runs on now, so the shared driver and restaurant apps keep working. A second
    company running its own instance on a different railway.app address keeps its own address."""
    try:
        h = _norm_host(u)
        now_h = _norm_host(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or (request.host if has_request_context() else ""))
        if h and now_h and h != now_h and h in old_own_railway_hosts() and now_h.endswith(".up.railway.app"):
            return "https://" + now_h
    except Exception:
        pass
    return u


def company_locked(c):
    """A company whose brand is locked in Settings > Regions > Brand sites stays out of the shared
    driver and restaurant apps (/go/driver, /go/kitchen) until the brand is unlocked."""
    try:
        s = company_site(c)
        return bool(s is not None and site_locked(s["id"]))
    except Exception:
        return False


def hub_company(c):
    return dict({"name": c["name"], "code": c["code"], "url": live_company_url(c["url"])}, **company_look(c))


def fix_old_railway_company_urls():
    """Each start: companies saved with an older railway.app address get the current one."""
    try:
        cur = _norm_host(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "")
        if not cur.endswith(".up.railway.app"):
            return
        con = dbx.connect(DB_PATH)
        for r in con.execute("SELECT id, url FROM companies").fetchall():
            h = _norm_host(r[1])
            if h in old_own_railway_hosts() and h != cur:
                con.execute("UPDATE companies SET url=? WHERE id=?", ("https://" + cur, r[0]))
        con.commit()
        con.close()
    except Exception as e:
        print("company address fix skipped:", e)


def staff_brand_site():
    """The brand a driver or restaurant page shows: the brand of the web address, else the
    signed-in restaurant's brand, else the brand picked in the shared app."""
    if not has_request_context() or not brands_on():
        return None
    p = request.path
    if not (p.startswith("/driver") or p.startswith("/restaurant") or p.startswith("/reset/")):
        return None
    if "_staff_site" in g:
        return g._staff_site
    s = None
    try:
        # a company picked in the shared driver/restaurant app wins, so one app address works for every brand
        _pick = session.get("staff_brand")
        if _pick and _pick != "main":
            s = site_by_id(_pick)
        host = _norm_host(request.host)
        for row in (db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall() if s is None and _pick != "main" else []):
            if host in site_domains(row):
                s = row
                break
        if s is None and session.get("restaurant_id") and not p.startswith("/driver") \
                and session.get("staff_brand") != "main":   # picked Tiger Town To Go: show Tiger Town To Go
            r = db().execute("SELECT region_id FROM restaurants WHERE id=?", (session["restaurant_id"],)).fetchone()
            if r is not None:
                s = site_of_region(r["region_id"])
        if s is None and session.get("staff_brand") and session.get("staff_brand") != "main":
            s = site_by_id(session.get("staff_brand"))
    except Exception:
        s = None
    g._staff_site = s
    return s


def host_brand_site():
    """The brand whose own web address this is, or None on a shared address."""
    try:
        host = _norm_host(request.host)
        for row in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
            if host in site_domains(row):
                return row
    except Exception:
        pass
    return None


def staff_main_brand():
    """True when a driver/restaurant page is shown under the main business (Tiger Town To Go),
    picked in the shared app."""
    if not has_request_context():
        return False
    p = request.path
    if not (p.startswith("/driver") or p.startswith("/restaurant") or p.startswith("/reset/")):
        return False
    return session.get("staff_brand") == "main" and staff_brand_site() is None


def _session_brand_site():
    """The brand the signed-in driver/kitchen picked (or the web address belongs to), for API calls."""
    bs = session.get("staff_brand")
    if bs and bs != "main":   # the company picked in the shared app wins, on any web address
        try:
            s = site_by_id(int(bs))
            if s is not None:
                return s
        except (TypeError, ValueError):
            pass
    if bs == "main":
        return None
    return host_brand_site()


@app.before_request
def keep_staff_in_their_company():
    """An app left open (or signed in before a fix) keeps calling the server in the background.
    If the driver or kitchen no longer belongs to the company that app is showing, sign them out
    there too, not only when the page reloads."""
    try:
        if not brands_on():
            return None
        p = request.path
        disp = bool(session.get("dispatcher_id"))
        if session.get("driver_id") and (p.startswith("/api/driver/") or
                                         (not disp and (p.startswith("/api/chat/") or p.startswith("/api/order/")))):
            site = _session_brand_site()
            if site is not None and not driver_fits_brand(session["driver_id"], site):
                session.pop("driver_id", None)
                resp = jsonify({"ok": False, "signed_out": True, "error": "Signed out: this account belongs to another company."})
                resp.status_code = 401
                resp.headers["X-Signed-Out"] = "/driver/login"
                return resp
        if session.get("restaurant_id") and p.startswith("/api/restaurant/"):
            site = _session_brand_site()
            r = db().execute("SELECT * FROM restaurants WHERE id=?", (session["restaurant_id"],)).fetchone()
            if site is not None and r is not None and not restaurant_fits_brand(r, site):
                session.pop("restaurant_id", None)
                resp = jsonify({"ok": False, "signed_out": True, "error": "Signed out: this kitchen belongs to another company."})
                resp.status_code = 401
                resp.headers["X-Signed-Out"] = "/restaurant/login"
                return resp
    except Exception:
        return None
    return None


@app.before_request
def remember_staff_brand():
    """The shared apps pass ?bs=<brand id> when a worker picks their company."""
    try:
        bs = request.args.get("bs")
        if bs is not None and (request.path.startswith("/driver") or request.path.startswith("/restaurant")):
            if bs.isdigit() and int(bs) and site_by_id(int(bs)) is not None:
                session["staff_brand"] = int(bs)
            elif bs == "main":
                session["staff_brand"] = "main"
                co = (request.args.get("co") or "").strip()[:80]
                if co:
                    session["staff_brand_name"] = co
            else:
                session.pop("staff_brand", None)
    except Exception:
        pass


@app.context_processor
def inject_all_brand_logos():
    """On the shared customer website with no brand picked (All brands), every brand's logo,
    so the header and the browser tab show all of them together."""
    try:
        # the All brands website is gone (every address shows one brand), except in the developer test view
        if not dev_all_brands_mode() or request.path.startswith(_STAFF_PREFIXES) or request.path.startswith(("/go/", "/reset/")):
            return {"all_logos": []}
        if not brands_on() or current_site() is not None:
            return {"all_logos": []}
        logos = []
        for srow in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
            lg = brand_logo(srow)
            if lg and lg not in logos:
                logos.append(lg)
        return {"all_logos": logos if len(logos) > 1 else []}
    except Exception:
        return {"all_logos": []}


@app.context_processor
def inject_staff_brand():
    try:
        s = staff_brand_site()
        if s is None:
            if staff_main_brand():
                return {"staff_brand": {"name": session.get("staff_brand_name") or main_brand_name(),
                                        "logo": main_brand_logo()}}
            return {"staff_brand": None}
        return {"staff_brand": {"name": s["name"], "logo": brand_logo(s)}}
    except Exception:
        return {"staff_brand": None}


@app.get("/kitchen")
@app.get("/driver-app")
def hub_short_alias():
    # Short addresses people type by hand land on the shared app picker instead of a Not Found page.
    return redirect("/go/kitchen" if request.path == "/kitchen" else "/go/driver")


@app.get("/go/<which>")
def hub_pick(which):
    if which not in HUB_APPS:
        return redirect("/go/driver")
    title, login_path, manifest = HUB_APPS[which]
    logo = DEFAULT_LOGO   # the shared apps always open on the Fleet Foot Delivery logo
    return render_template("hub_pick.html", which=which, title=title, login_path=login_path,
                           manifest=manifest, logo=logo)


@app.get("/api/hub/companies")
def api_hub_companies():
    rows = [hub_company(r) for r in company_rows() if (r.get("listed") if r.get("listed") is not None else 1)
            and not company_locked(r)]   # locked brands stay hidden until unlocked
    return jsonify({"ok": True, "companies": rows})


@app.get("/api/hub/find")
def api_hub_find():
    code = (request.args.get("code") or "").strip().lower()
    r = db().execute("SELECT * FROM companies WHERE code=? AND COALESCE(active,1)=1", (code,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "No company with that code. Check with your manager."}), 404
    if company_locked(dict(r)):
        return jsonify({"ok": False, "error": "That company is not open in the app yet. Check with your manager."}), 403
    return jsonify({"ok": True, "company": hub_company(dict(r))})


@app.get("/dispatch/companies")
def dispatch_companies():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    if not is_dev():
        return redirect("/dispatch")
    sites = [{"id": r["id"], "name": r["name"], "logo": site_logo_url(r) or DEFAULT_LOGO}
             for r in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall()]
    return render_template("dispatch_companies.html", portal="dispatch", companies=companies_with_look(), sites=sites)


@app.post("/api/dispatch/companies")
def api_dispatch_companies():
    if not dispatcher_required() or not is_dev():
        return jsonify({"ok": False, "error": "Only a developer account can change the company list."}), 403
    f = request.get_json(silent=True) or request.form
    op = f.get("op", "save")
    cid = int(f.get("id") or 0)
    if op == "delete":
        db().execute("DELETE FROM companies WHERE id=?", (cid,))
        db().commit()
        return jsonify({"ok": True})
    name = (f.get("name") or "").strip()[:80]
    code = "".join(ch for ch in (f.get("code") or "").strip().lower() if ch.isalnum() or ch in "-_")[:40]
    url = clean_site_url(f.get("url"))
    if not name or not code or not url:
        return jsonify({"ok": False, "error": "Enter a company name, a code (letters and numbers) and their web address."}), 400
    listed = 1 if str(f.get("listed", "1")).lower() in ("1", "true", "on", "yes") else 0
    try:
        site_id = int(f.get("site_id") or 0)
    except (TypeError, ValueError):
        site_id = 0
    if site_id and site_by_id(site_id) is None:
        site_id = 0
    active = 1 if str(f.get("active", "1")).lower() in ("1", "true", "on", "yes") else 0
    dup = db().execute("SELECT id FROM companies WHERE code=? AND id<>?", (code, cid)).fetchone()
    if dup:
        return jsonify({"ok": False, "error": "Another company already uses that code."}), 400
    if cid:
        db().execute("UPDATE companies SET name=?, code=?, url=?, listed=?, active=?, site_id=? WHERE id=?",
                     (name, code, url, listed, active, site_id, cid))
    else:
        db().execute("INSERT INTO companies(name, code, url, listed, active, site_id, created_at) VALUES(?,?,?,?,?,?,?)",
                     (name, code, url, listed, active, site_id, dt.datetime.now().isoformat(timespec="seconds")))
    db().commit()
    return jsonify({"ok": True, "companies": companies_with_look()})


def seed_brand_photos():
    """First start after this update: put the Popeyes photo on Popeyes if it has none."""
    try:
        con = dbx.connect(DB_PATH)
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


def seed_tiger_town_logo():
    """First start after this update: put the tiger (static/brand/tigertown-logo.png) on the
    Tiger Town To Go brand as its Design logo. Runs once."""
    try:
        con = dbx.connect(DB_PATH)
        if con.execute("SELECT 1 FROM settings WHERE key='tt_brand_logo_v2'").fetchone():
            con.close()
            return
        src = os.path.join(APP_DIR_STATIC, "brand", "tigertown-logo.png")
        done = False
        if src and os.path.exists(src):
            for s in con.execute("SELECT id, name, domains, logo FROM sites").fetchall():
                key = (s["name"] or "").lower().replace(" ", "")
                doms = (s["domains"] or "").lower()
                if not (key.startswith("tigertown") or "tigertown" in doms):
                    continue
                import shutil
                name = secrets.token_hex(10) + (os.path.splitext(src)[1] or ".png")
                shutil.copyfile(src, os.path.join(UPLOAD_DIR, name))
                con.execute("UPDATE sites SET logo=? WHERE id=?", (name, s["id"]))
                done = True
            # only mark it finished once the brand exists, so it still runs after the brand is added
            if done or con.execute("SELECT 1 FROM sites WHERE LOWER(REPLACE(name,' ','')) LIKE 'tigertown%' "
                                   "OR LOWER(COALESCE(domains,'')) LIKE '%tigertown%'").fetchone():
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('tt_brand_logo_v2','1')")
        con.commit()
        con.close()
        if done:
            print("Tiger Town To Go brand logo set to the tiger")
    except Exception as e:
        print("tiger town logo seed skipped:", e)


@app.get("/api/menu/<int:rid>")
def api_menu(rid):
    return jsonify({"ok": True, "items": menu_payload(rid)})



# ---------------------------------------------------------------- TigerTownToGo (Zuppler) import
TIGERTOWN_FILE = os.path.join(APP_DIR, "data", "tigertown_import.json")

import json

def _hhmm(m):
    m = int(m)
    if m >= 1440:
        return "23:59"
    return "%02d:%02d" % (m // 60, m % 60)

def zup_convert(rest, menus, details):
    """One Zuppler restaurant (+ its menus and item details) -> Fleet import record."""
    hours = {}
    hoo = rest.get("hoursOfOperation") or []
    for d in range(7):
        zi = (d + 1) % 7
        rng = hoo[zi] if zi < len(hoo) else []
        rng = [x for x in (rng or []) if x and len(x) == 2]
        hours[str(d)] = ([_hhmm(min(x[0] for x in rng)), _hhmm(max(x[1] for x in rng))] if rng else ["", ""])
    loc = ((rest.get("locations") or [{}])[0] or {}).get("address") or {}
    geo = loc.get("geo") or {}
    parts = [loc.get("street") or loc.get("nickname") or ""]
    cs = ", ".join(x for x in [loc.get("city") or "", ((loc.get("state") or "") + " " + (loc.get("zip") or "")).strip()] if x)
    addr = ", ".join(x for x in parts + [cs] if x)
    svc = next((s for s in rest.get("services") or [] if s.get("id") == "DELIVERY"), None) or \
          ((rest.get("services") or [None])[0] or {})
    # Zuppler's service contact phone is the delivery company's own number (the same on every
    # restaurant), never the restaurant's. The real number is looked up afterwards (fix_rest_phones).
    phone = ""
    st = rest.get("settings") or {}
    act_menus = [m for m in (menus or []) if m.get("active", True)]
    multi = len(act_menus) > 1
    items, sort = [], 0
    for m in act_menus:
        for c in sorted(m.get("categories") or [], key=lambda c: (c.get("priority") or 0)):
            if not c.get("active", True) or "order it again" in (c.get("name") or "").lower():
                continue
            for it in sorted(c.get("items") or [], key=lambda i: (i.get("priority") or 0)):
                if not it.get("active", True):
                    continue
                det = details.get("%s:%s" % (rest["id"], it["id"])) or {}
                sizes = sorted([s for s in det.get("sizes") or [] if s.get("active", True)],
                               key=lambda s: (s.get("priority") or 0))
                groups = []
                if sizes:
                    base = min(float(s.get("price") or 0) for s in sizes)
                    if len(sizes) > 1:
                        groups.append({"name": "Size", "min": 1, "max": 1,
                                       "options": [{"name": s.get("sizeName") or "Size",
                                                    "delta": int(round((float(s.get("price") or 0) - base) * 100))}
                                                   for s in sizes]})
                    for g in sorted(sizes[0].get("modifiers") or [], key=lambda g: (g.get("priority") or 0)):
                        if not g.get("active", True):
                            continue
                        opts = [{"name": o.get("name") or "", "delta": int(round(float(o.get("price") or 0) * 100))}
                                for o in sorted(g.get("options") or [], key=lambda o: (o.get("priority") or 0))
                                if o.get("active", True)]
                        if not opts:
                            continue
                        mx = g.get("maxSelections")
                        if not mx:
                            mx = len(opts) if g.get("multipleSelections") else 1
                        groups.append({"name": g.get("name") or "Choose", "min": int(g.get("minSelections") or 0),
                                       "max": int(min(mx, len(opts))), "options": opts})
                    price = int(round(base * 100))
                else:
                    price = int(round(float(it.get("price") or it.get("minPrice") or 0) * 100))
                sort += 1
                items.append({"name": (it.get("name") or "").strip(), "description": (it.get("description") or "").strip(),
                              "price_cents": price, "section": (c.get("name") or "").strip(),
                              "tab": (m.get("name") or "").strip() if multi else "",
                              "image": ((it.get("image") or {}).get("medium")) or "", "sort": sort,
                              "groups": groups, "zid": it["id"]})
    return {"zid": str(rest["id"]), "name": (rest.get("name") or "").strip(), "cuisine": rest.get("cuisines") or "",
            "address": addr, "lat": geo.get("lat"), "lng": geo.get("lng"), "phone": phone,
            "hours": hours, "hours_raw": hoo,
            "photo": ((rest.get("featuredImage") or {}).get("medium")) or "",
            "logo": ((rest.get("logo") or {}).get("medium")) or "",
            "min_order_cents": int(round(float(svc.get("min_order") or 0) * 100)),
            "delivery_fee_cents": int(round(float(svc.get("defaultChargeAmount") or 0) * 100)),
            "eta_min": svc.get("defaultTime"), "prep": st.get("preparationTime"),
            "paused": bool(st.get("pause_online_ordering")), "items": items}

# ---------------------------------------------------------------- import from any Zuppler ordering website
ZUP_GQL = "https://restaurants-api5.zuppler.com/graphql"
ZUP_API = "https://api.zuppler.com/v3/channels/"
ZUP_STATE = {"running": False, "stage": "", "done": 0, "total": 0, "error": "", "site": "", "name": ""}
_ZUP_LOCK = threading.Lock()

def zuppler_file():
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "zuppler_import.json")

def _zup_get(url, data=None, origin=None, timeout=30, tries=3):
    last = None
    for a in range(tries):
        try:
            h = {"User-Agent": "Mozilla/5.0"}
            if data is not None:
                h["content-type"] = "application/json"
            if origin:
                h["Origin"] = origin
            req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=h)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(1 + a)
    raise last

def _zup_gql(query, origin):
    for a in range(3):
        try:
            d = json.loads(_zup_get(ZUP_GQL, {"query": query}, origin, timeout=60, tries=1))
            if d.get("data") is not None:
                return d["data"]
        except Exception:
            pass
        time.sleep(1 + a)
    return None

def zuppler_find_channel(site):
    """Finds the Zuppler channel behind an ordering website (or takes the channel code itself)."""
    site = (site or "").strip()
    if not site:
        raise ValueError("Type the website address.")
    found = []
    if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", site) and "." not in site:
        found.append(site)
    else:
        url = site if site.startswith("http") else "https://" + site
        html = _zup_get(url, timeout=20).decode("utf-8", "replace")
        pats = [r"channels/([A-Za-z0-9_-]+)\.json", r"channels/([A-Za-z0-9_-]+)/",
                r"""channel(?:_id|Id|_permalink|Permalink|)["']?\s*[:=]\s*["']([A-Za-z0-9_-]{4,40})["']""",
                r"""data-channel(?:-id)?=["']([A-Za-z0-9_-]{4,40})["']""",
                r"zuppler\.com/(?:channels|portal)/([A-Za-z0-9_-]{4,40})"]
        def scan(text):
            for p in pats:
                for m in re.findall(p, text):
                    if m not in found:
                        found.append(m)
        scan(html)
        if not found:
            base = urllib.parse.urlsplit(url)
            for src in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", html)[:15]:
                full = urllib.parse.urljoin(url, src)
                if urllib.parse.urlsplit(full).netloc != base.netloc:
                    continue
                try:
                    scan(_zup_get(full, timeout=15, tries=1).decode("utf-8", "replace"))
                except Exception:
                    pass
                if found:
                    break
    for code in found:
        try:
            ch = json.loads(_zup_get(ZUP_API + code + ".json", timeout=20, tries=2))
            if ch.get("success") and ch.get("channel"):
                c = ch["channel"]
                return {"permalink": c.get("permalink") or code, "name": c.get("name") or code,
                        "url": c.get("url") or site}
        except Exception:
            continue
    raise ValueError("That website doesn't look like a Zuppler, Data Dreamers or DeliverLogic ordering site. Check the address and try again.")

ZUP_REST_Q = """{ restaurant(id: %s) { id name cuisines hoursOfOperation timezone { offset }
  locations { id address { street city state zip nickname geo { lat lng } } }
  services { id min_order defaultTime defaultChargeAmount defaultChargePercent contact { phone } }
  logo { medium } featuredImage { medium } settings { preparationTime pause_online_ordering } } }"""
ZUP_MENU_Q = """{ menus(restaurantId: %s, channelId: "%s") { id name active default categories { id name active priority
  items { id name description active price minPrice maxPrice multipleSizes priority image { medium } } } } }"""
ZUP_ITEM_Q = """{ item(restaurantId: %s, itemId: %s, channelId: "%s") { id name sizes { id sizeName price active priority
  modifiers { id name active minSelections maxSelections multipleSelections priority
  options { id name price active priority default } } } } }"""

def _zuppler_worker(site):
    import concurrent.futures as cf
    st = ZUP_STATE
    try:
        st.update(stage="Finding the ordering site...", done=0, total=0, error="")
        import multi_import
        kind = multi_import.detect(site)
        if kind in ("datadreamers", "deliverlogic"):
            pull = multi_import.dd_pull if kind == "datadreamers" else multi_import.dl_pull
            data = pull(site, st)
            out = sorted(data["restaurants"], key=lambda z: z["name"].lower())
            data.update(restaurants=out, pulled=dt.date.today().isoformat(), channel="")
            tmp = zuppler_file() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, zuppler_file())
            st.update(stage="Done. Found %d restaurants and %d menu items." % (len(out), sum(len(z["items"]) for z in out)))
            return
        ch = zuppler_find_channel(site)
        code, origin = ch["permalink"], (ch["url"] or "").rstrip("/") or None
        if origin and not origin.startswith("http"):
            origin = "https://" + origin
        if origin:
            sp = urllib.parse.urlsplit(origin)
            origin = sp.scheme + "://" + sp.netloc
        st.update(name=ch["name"], stage="Getting the restaurant list from " + ch["name"] + "...")
        integ = json.loads(_zup_get(ZUP_API + code + "/integrations.json", timeout=40))
        ids = []
        for it in integ.get("integrations") or []:
            rid = (it.get("restaurant") or {}).get("id")
            if rid and not it.get("disabled") and rid not in ids:
                ids.append(rid)
        if not ids:
            raise ValueError(ch["name"] + " has no restaurants listed.")
        st.update(stage="Reading restaurants...", total=len(ids), done=0)
        rests, menus = {}, {}
        def one_rest(rid):
            r = (_zup_gql(ZUP_REST_Q % rid, origin) or {}).get("restaurant")
            m = (_zup_gql(ZUP_MENU_Q % (rid, code), origin) or {}).get("menus")
            return rid, r, m
        with cf.ThreadPoolExecutor(8) as ex:
            for rid, r, m in ex.map(one_rest, ids):
                if r:
                    rests[str(rid)], menus[str(rid)] = r, m or []
                st["done"] += 1
        jobs = []
        for rid, ms in menus.items():
            for m in ms:
                if not m.get("active", True):
                    continue
                for c in m.get("categories") or []:
                    if not c.get("active", True):
                        continue
                    for it in c.get("items") or []:
                        if it.get("active", True):
                            jobs.append((rid, it["id"]))
        jobs = list(dict.fromkeys(jobs))
        st.update(stage="Reading menu items, sizes and add-ons...", total=len(jobs), done=0)
        details = {}
        def one_item(j):
            return j, (_zup_gql(ZUP_ITEM_Q % (j[0], j[1], code), origin) or {}).get("item")
        with cf.ThreadPoolExecutor(12) as ex:
            for j, v in ex.map(one_item, jobs):
                details["%s:%s" % j] = v
                st["done"] += 1
        out = []
        for rid in [str(i) for i in ids]:
            if rid in rests:
                try:
                    out.append(zup_convert(rests[rid], menus.get(rid), details))
                except Exception as e:
                    print("zuppler convert skipped", rid, e)
        out.sort(key=lambda z: z["name"].lower())
        data = {"source": ch["name"] + " (Zuppler)", "site": ch["url"] or site, "channel": code,
                "pulled": dt.date.today().isoformat(), "restaurants": out}
        tmp = zuppler_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, zuppler_file())
        st.update(stage="Done. Found %d restaurants and %d menu items." % (len(out), sum(len(z["items"]) for z in out)))
    except Exception as e:
        st.update(error=str(e) or "Couldn't read that website.", stage="")
    finally:
        st["running"] = False

def start_zuppler_pull(site):
    with _ZUP_LOCK:
        if ZUP_STATE["running"]:
            return False
        ZUP_STATE.update(running=True, site=site, name="", error="", stage="Starting...", done=0, total=0)
    threading.Thread(target=_zuppler_worker, args=(site,), daemon=True).start()
    return True

@app.get("/api/dispatch/zuppler-status")
def api_zuppler_status():
    me = session.get("dispatcher_id")
    if not me or not is_owner(me):
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, **ZUP_STATE})


def tigertown_data():
    """The restaurants last pulled from a Zuppler site (kept next to the database)."""
    try:
        with open(zuppler_file()) as f:
            return json.load(f)
    except Exception:
        return None

def _norm_name(n):
    return "".join(ch for ch in (n or "").lower() if ch.isalnum())

def tigertown_import(pick_ids=None, skip_paused=True, replace_menu=True, region_id=None):
    """Bring restaurants, photos, logos, hours, menus, sizes and add-ons from a Zuppler ordering site.
    A restaurant already here with the same name is updated instead of duplicated."""
    data = tigertown_data()
    if not data:
        return {"ok": False, "error": "Find the restaurants on an ordering website first."}
    con = db()
    have = {}
    try:
        region_id = int(region_id) if region_id else None
    except (TypeError, ValueError):
        region_id = None
    for r in con.execute("SELECT * FROM restaurants").fetchall():
        if region_id and (r["region_id"] or 0) not in (0, region_id):
            # a restaurant with the same name in another region (a chain like Cheba Hut in Athens and
            # Tuscaloosa) is a different store: never update or move it
            continue
        have[_norm_name(r["name"])] = r
        if r["zup_id"]:
            have["z" + str(r["zup_id"])] = r
    made = updated = items = 0
    skipped = []
    for z in data["restaurants"]:
        if pick_ids is not None and str(z["zid"]) not in pick_ids:
            continue
        low = z["name"].lower()
        if skip_paused and (z.get("paused") or "(old)" in low or " dnd" in low):
            skipped.append(z["name"])
            continue
        hours = json.dumps(z["hours"])
        cur = have.get("z" + str(z["zid"])) or have.get(_norm_name(z["name"]))
        if cur:
            rid = cur["id"]
            con.execute("""UPDATE restaurants SET zup_id=?, cuisine=COALESCE(NULLIF(cuisine,''),?),
                           image=CASE WHEN image IS NULL OR image='' THEN ? ELSE image END,
                           logo=?, address=CASE WHEN address IS NULL OR address='' THEN ? ELSE address END,
                           lat=COALESCE(lat,?), lng=COALESCE(lng,?), hours=?, eta_min=COALESCE(?,eta_min),
                           phone=CASE WHEN phone IS NULL OR phone='' THEN ? ELSE phone END,
                           min_order_cents=COALESCE(?,min_order_cents), region_id=COALESCE(?,region_id) WHERE id=?""",
                        (str(z["zid"]), z["cuisine"], z["photo"] or "", z["logo"] or "", z["address"], z["lat"], z["lng"],
                         hours, z.get("eta_min"), z.get("phone") or "", z.get("min_order_cents") or None,
                         region_id or None, rid))
            updated += 1
        else:
            base = "".join(ch if ch.isalnum() else "-" for ch in z["name"].lower()).strip("-")
            base = "-".join(x for x in base.split("-") if x) or "restaurant"
            slug, n = base, 2
            while con.execute("SELECT 1 FROM restaurants WHERE slug=?", (slug,)).fetchone():
                slug = "%s-%d" % (base, n); n += 1
            pin = "%04d" % secrets.randbelow(10000)
            cur2 = con.execute("""INSERT INTO restaurants(name,slug,pin,address,phone,lat,lng,hours,prep_default,
                                   image,logo,cuisine,zup_id,eta_min,min_order_cents,region_id)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (z["name"], slug, pin, z["address"] or "Auburn, AL", z.get("phone") or "", z["lat"], z["lng"], hours,
                                int(z.get("prep") or 15), z["photo"], z["logo"], z["cuisine"], str(z["zid"]),
                                z.get("eta_min"), z.get("min_order_cents") or None, region_id))
            rid = cur2.lastrowid
            made += 1
        if replace_menu:
            old = [r["id"] for r in con.execute("SELECT id FROM menu_items WHERE restaurant_id=?", (rid,)).fetchall()]
            if old:
                marks = ",".join("?" * len(old))
                gids = [r["id"] for r in con.execute("SELECT id FROM option_groups WHERE item_id IN (%s)" % marks, old).fetchall()]
                if gids:
                    con.execute("DELETE FROM options WHERE group_id IN (%s)" % ",".join("?" * len(gids)), gids)
                con.execute("DELETE FROM option_groups WHERE item_id IN (%s)" % marks, old)
                con.execute("DELETE FROM menu_items WHERE restaurant_id=?", (rid,))
        for it in z["items"]:
            c = con.execute("""INSERT INTO menu_items(restaurant_id,name,description,price_cents,active,section,sort,image,menu_tab,zup_id,image_src)
                               VALUES(?,?,?,?,1,?,?,?,?,?,?)""",
                            (rid, it["name"][:120], it["description"][:1000], int(it["price_cents"]), it["section"][:80],
                             int(it["sort"]), it["image"], it["tab"][:60], str(it["zid"]),
                             it["image"] if (it["image"] or "").startswith("http") else ""))
            iid = c.lastrowid
            items += 1
            for gi, g in enumerate(it["groups"]):
                gc = con.execute("INSERT INTO option_groups(item_id,name,min_select,max_select,sort) VALUES(?,?,?,?,?)",
                                 (iid, g["name"][:80], int(g["min"]), int(g["max"]), gi))
                gid = gc.lastrowid
                con.executemany("INSERT INTO options(group_id,name,price_delta_cents,sort) VALUES(?,?,?,?)",
                                [(gid, o["name"][:80], int(o["delta"]), oi) for oi, o in enumerate(g["options"])])
    con.commit()
    threading.Thread(target=_startup_phone_repair, daemon=True).start()   # phones and addresses from Google
    return {"ok": True, "created": made, "updated": updated, "items": items, "skipped": skipped}


# ---------------------------------------------------------------- copy imported pictures onto this server
_PIC_LOCK = threading.Lock()
_PIC_STATE = {"running": False, "done": 0, "failed": 0}

def _local_pic_ok(name):
    name = (name or "").strip()
    if not name:
        return False
    if name.startswith("http"):
        return True
    return os.path.exists(os.path.join(UPLOAD_DIR, os.path.basename(name)))


def item_picture(it):
    """An item's picture: the copy on this server, or where it came from when that copy went missing."""
    img = (it["image"] or "").strip()
    if _local_pic_ok(img):
        return media_url(img)
    try:
        src = (it["image_src"] or "").strip()
    except (IndexError, KeyError):
        src = ""
    return src if src.startswith("http") else ""


def fill_missing_pictures(con=None):
    """Give back item and restaurant pictures that are blank or whose copy is gone, from the last menu
    imports kept on this server. Matches restaurants by their import id or name, items by id or name."""
    con = con or db()
    srcs = []
    for path in (zuppler_file(), os.path.join(APP_DIR, "data", "tigertown_import.json")):
        try:
            srcs.append(json.load(open(path)))
        except Exception:
            pass
    byrest = {}
    for d in srcs:
        for z in d.get("restaurants") or []:
            ent = byrest.setdefault("z" + str(z.get("zid")), {"photo": "", "logo": "", "items": {}})
            byrest.setdefault(_norm_name(z.get("name") or ""), ent)
            ent["photo"] = ent["photo"] or (z.get("photo") or "")
            ent["logo"] = ent["logo"] or (z.get("logo") or "")
            for it in z.get("items") or []:
                if (it.get("image") or "").startswith("http"):
                    ent["items"].setdefault("z" + str(it.get("zid")), it["image"])
                    ent["items"].setdefault(_norm_name(it.get("name") or "") + "|" + _norm_name(it.get("section") or ""), it["image"])
                    ent["items"].setdefault(_norm_name(it.get("name") or ""), it["image"])
    fixed = 0
    if not byrest:
        return 0
    for r in con.execute("SELECT id,name,zup_id,image,logo FROM restaurants").fetchall():
        ent = byrest.get("z" + str(r["zup_id"])) if r["zup_id"] else None
        ent = ent or byrest.get(_norm_name(r["name"] or ""))
        if not ent:
            continue
        if not _local_pic_ok(r["image"]) and ent["photo"]:
            con.execute("UPDATE restaurants SET image=? WHERE id=?", (ent["photo"], r["id"])); fixed += 1
        if not _local_pic_ok(r["logo"]) and ent["logo"]:
            con.execute("UPDATE restaurants SET logo=? WHERE id=?", (ent["logo"], r["id"])); fixed += 1
        for it in con.execute("SELECT id,name,section,zup_id,image,image_src FROM menu_items WHERE restaurant_id=?",
                              (r["id"],)).fetchall():
            if _local_pic_ok(it["image"]):
                continue
            url = (it["image_src"] or "").strip() if (it["image_src"] or "").startswith("http") else ""
            url = url or (ent["items"].get("z" + str(it["zup_id"])) if it["zup_id"] else None) \
                or ent["items"].get(_norm_name(it["name"] or "") + "|" + _norm_name(it["section"] or "")) \
                or ent["items"].get(_norm_name(it["name"] or ""))
            if url:
                con.execute("UPDATE menu_items SET image=?, image_src=? WHERE id=?", (url, url, it["id"])); fixed += 1
    con.commit()
    return fixed


def remote_picture_count():
    con = db()
    n = con.execute("SELECT COUNT(*) FROM restaurants WHERE image LIKE 'http%'").fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM restaurants WHERE logo LIKE 'http%'").fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM menu_items WHERE image LIKE 'http%'").fetchone()[0]
    return n

def _copy_pictures_worker():
    import hashlib
    try:
        con = dbx.connect(DB_PATH)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        urls = set()
        for sql in ("SELECT image u FROM restaurants WHERE image LIKE 'http%'",
                    "SELECT logo u FROM restaurants WHERE logo LIKE 'http%'",
                    "SELECT DISTINCT image u FROM menu_items WHERE image LIKE 'http%'"):
            urls.update(r["u"] for r in con.execute(sql).fetchall())
        for url in sorted(urls):
            try:
                path = urllib.parse.urlparse(url).path
                ext = os.path.splitext(path)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                name = "tt_" + hashlib.sha1(url.encode()).hexdigest()[:20] + ext
                dest = os.path.join(UPLOAD_DIR, name)
                if not os.path.exists(dest):
                    _u = urllib.parse.urlsplit(url)
                    safe = urllib.parse.urlunsplit((_u.scheme, _u.netloc, urllib.parse.quote(urllib.parse.unquote(_u.path)),
                                                    _u.query, ""))
                    req = urllib.request.Request(safe, headers={"User-Agent": "Mozilla/5.0"})
                    blob = urllib.request.urlopen(req, timeout=20).read()
                    if len(blob) < 200 or len(blob) > 10 * 1024 * 1024:
                        raise ValueError("bad size")
                    with open(dest, "wb") as f:
                        f.write(blob)
                con.execute("UPDATE restaurants SET image=? WHERE image=?", (name, url))
                con.execute("UPDATE restaurants SET logo=? WHERE logo=?", (name, url))
                con.execute("UPDATE menu_items SET image_src=? WHERE image=? AND COALESCE(image_src,'')=''", (url, url))
                con.execute("UPDATE menu_items SET image=? WHERE image=?", (name, url))
                con.commit()
                _PIC_STATE["done"] += 1
            except Exception as e:
                _PIC_STATE["failed"] += 1
                print("picture copy failed:", url[:120], e)
        con.close()
    finally:
        _PIC_STATE["running"] = False

def start_picture_copy():
    """Copies every imported picture into this server's photo folder so nothing depends on the old site."""
    with _PIC_LOCK:
        if _PIC_STATE["running"]:
            return False
        _PIC_STATE.update({"running": True, "done": 0, "failed": 0})
    threading.Thread(target=_copy_pictures_worker, daemon=True).start()
    return True


@app.route("/api/dispatch/picture-copy", methods=["GET", "POST"])
def api_picture_copy():
    me = session.get("dispatcher_id")
    if not me or not is_owner(me):
        return jsonify({"ok": False}), 403
    started = start_picture_copy() if request.method == "POST" else False
    return jsonify({"ok": True, "started": started, "running": _PIC_STATE["running"],
                    "copied": _PIC_STATE["done"], "failed": _PIC_STATE["failed"],
                    "left": remote_picture_count()})


@app.get("/dispatch/import-tigertown")
def dispatch_import_tigertown_old():
    return redirect("/dispatch/import-zuppler")

# ---------------- copy a whole website into the customer website settings ----------------
WEBCOPY_TEXT = (("business_name", "Business name", 60), ("home_headline", "Big headline", 120),
                ("home_sub", "Line under the headline", 300), ("how_title", "How it works title", 60),
                ("how1_t", "Step 1 title", 60), ("how1_p", "Step 1 text", 300),
                ("how2_t", "Step 2 title", 60), ("how2_p", "Step 2 text", 300),
                ("how3_t", "Step 3 title", 60), ("how3_p", "Step 3 text", 300),
                ("pocket_title", "App section title", 80), ("pocket_text", "App section text", 400),
                ("business_email", "Email", 120), ("business_address", "Address", 160),
                ("dispatch_phone", "Phone", 20))


def webcopy_file():
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "website_import.json")


def main_fill(con, data):
    """Fill the main website (Settings) from a read website: name, phone, logo, photos and text.
    Used when brand sites are off. Returns (filled, not filled) labels."""
    done, failed = [], []
    def put(k, v):
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    for k, label, n in WEBCOPY_TEXT:
        v = data.get("phone") if k == "dispatch_phone" else data.get(k)
        v = " ".join(str(v or "").split())[:n]
        if k == "dispatch_phone":
            v = re.sub(r"\D", "", v)[-10:]
        if v:
            put(k, v)
            done.append(label)
    if data.get("logo"):
        try:
            name = _webcopy_picture(data["logo"], logo=True)
            drop_media((setting("logo_image", str) or "").strip())
            put("logo_image", name)
            done.append("Logo")
        except Exception:
            failed.append("Logo")
    pics = data.get("pictures") or []
    for i, (key, label) in enumerate((("hero_image", "Top photo"), ("pocket_image", "App section photo"))):
        if i < len(pics):
            try:
                name = _webcopy_picture(pics[i])
                drop_media((setting(key, str) or "").strip())
                put(key, name)
                done.append(label)
            except Exception:
                failed.append(label)
    con.commit()
    return done, failed


def _webcopy_picture(url, logo=False):
    """Download a picture from the copied site onto your own server. Returns the saved file name."""
    from PIL import Image
    import io
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read(PHOTO_MAX_BYTES + 1)
    if len(raw) > PHOTO_MAX_BYTES:
        raise ValueError("too big")
    im = Image.open(io.BytesIO(raw))
    if logo:
        im = im.convert("RGBA")
        im.thumbnail((512, 512))
        name = secrets.token_hex(10) + ".png"
        im.save(os.path.join(UPLOAD_DIR, name), "PNG", optimize=True)
    else:
        im = im.convert("RGB")
        im.thumbnail((2000, 2000))
        name = secrets.token_hex(10) + ".jpg"
        im.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85, optimize=True)
    return name


def logo_colors(name):
    """Bright colors in a saved logo, most used first (used when a website hides its colors)."""
    try:
        from PIL import Image
        from collections import Counter
        im = Image.open(os.path.join(UPLOAD_DIR, os.path.basename(name))).convert("RGBA")
        im.thumbnail((96, 96))
        cnt = Counter()
        for r, g_, b, a in im.getdata():
            if a < 200:
                continue
            hi, lo = max(r, g_, b), min(r, g_, b)
            if hi - lo > 60 and hi > 70 and lo < 215:
                cnt[(r // 24 * 24 + 12, g_ // 24 * 24 + 12, b // 24 * 24 + 12)] += 1
        out = []
        for (r, g_, b), _n in cnt.most_common(8):
            hv = "#%02x%02x%02x" % (min(r, 255), min(g_, 255), min(b, 255))
            if hv not in out:
                out.append(hv)
        return out[:2]
    except Exception:
        return []


def brand_target(con, pick, name_hint):
    """'12' = that brand, 'new' = a new brand named after the website. Returns the brand id or 0."""
    pick = str(pick or "").strip()
    if pick.isdigit() and site_by_id(int(pick)):
        return int(pick)
    if pick != "new":
        return 0
    base = " ".join(str(name_hint or "").split())[:60] or "New brand"
    name, n = base, 2
    taken = {r["name"].lower() for r in con.execute("SELECT name FROM sites").fetchall()}
    while name.lower() in taken:
        name = (base[:55] + " " + str(n)); n += 1
    srt = con.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM sites").fetchone()["s"]
    cur = con.execute("INSERT INTO sites(name,phone,domains,sort,created_at) VALUES(?,?,?,?,?)",
                      (name, "", "", srt, now()))
    con.commit()
    return cur.lastrowid


def brand_fill(con, sid, data, parts, picks=None):
    """Fill a brand site from a website someone read: name, phone, logo, colors, photos and home page text.
    parts says which pieces; picks can hold edited text and the chosen photos. Returns (done, failed)."""
    picks = picks or {}
    s = site_by_id(sid)
    done, failed, ch = [], [], {}
    if not s:
        return done, ["Brand"]
    if "name" in parts:
        nm = " ".join(str(picks.get("business_name") or data.get("business_name") or "").split())[:60]
        if nm and nm.lower() != s["name"].lower():
            if con.execute("SELECT 1 FROM sites WHERE lower(name)=? AND id<>?", (nm.lower(), sid)).fetchone():
                failed.append("Name (another brand already uses " + nm + ")")
            else:
                con.execute("UPDATE sites SET name=? WHERE id=?", (nm, sid)); done.append("Name")
    if "phone" in parts:
        ph = re.sub(r"\D", "", str(picks.get("dispatch_phone") or data.get("phone") or ""))[-10:]
        if len(ph) == 10:
            con.execute("UPDATE sites SET phone=? WHERE id=?", (ph, sid)); done.append("Phone")
    logo_name = ""
    if "logo" in parts and data.get("logo"):
        try:
            logo_name = _webcopy_picture(data["logo"], logo=True)
            drop_media((s["logo"] or "").strip())
            con.execute("UPDATE sites SET logo=? WHERE id=?", (logo_name, sid)); done.append("Logo")
        except Exception:
            failed.append("Logo")
    if "colors" in parts:
        cols = [c for c in (picks.get("colors") or data.get("colors") or []) if _hex_ok(c)]
        if not cols and logo_name:
            cols = logo_colors(logo_name)
        if cols:
            ch["brand"] = _hex_ok(cols[0]); ch["brand2"] = _hex_ok(cols[1] if len(cols) > 1 else cols[0])
            done.append("Colors")
    d = site_design(s)
    for key, label in (("hero_image", "Top photo"), ("pocket_image", "App section photo")):
        if key not in parts:
            continue
        url = picks.get(key)
        if url is None:   # automatic: only when the brand has none yet
            if (d.get(key) or "").strip():
                continue
            pics = data.get("pictures") or []
            url = pics[0 if key == "hero_image" else 1] if len(pics) > (0 if key == "hero_image" else 1) else ""
        if not url:
            continue
        try:
            nm = _webcopy_picture(url)
            drop_media(str(d.get(key) or "").strip())
            ch[key] = nm; done.append(label)
        except Exception:
            failed.append(label)
    if "text" in parts:
        n = 0
        for k in BRAND_TEXT_KEYS:
            if k in ("faq_text",) or k.startswith("social_"):
                continue
            v = picks.get(k) if k in picks else data.get(k)
            v = " ".join(str(v or "").split())
            if v:
                ch[k] = v[:400]; n += 1
        if n:
            done.append("Home page text")
    if "socials" in parts:
        for k in ("x", "facebook", "instagram"):
            v = (picks.get("social_" + k) if ("social_" + k) in picks else (data.get("socials") or {}).get(k)) or ""
            v = v.strip()[:200]
            if v:
                ch["social_" + k] = v if v.startswith("http") else "https://" + v
                done.append({"x": "X (Twitter)", "facebook": "Facebook", "instagram": "Instagram"}[k])
    if "faq_text" in picks:
        ch["faq_text"] = picks["faq_text"]; done.append("FAQs")
    if ch:
        save_site_design(con, sid, ch)
    con.commit()
    return done, failed


def _is_this_app(site):
    """True when a brand's web address already points at this app (so there's no old site to read)."""
    try:
        u = site if "://" in site else "https://" + site
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=15).read(200000).decode("utf-8", "ignore")
        return 'content="Fleet Foot Delivery app"' in page
    except Exception:
        return False


@app.route("/dispatch/import-website", methods=["GET", "POST"])
def dispatch_import_website():
    me = session.get("dispatcher_id")
    if not me:
        return redirect("/dispatch/login")
    if not is_owner(me):
        return "Only an owner can copy a website.", 403
    import multi_import
    data, result = None, None
    try:
        data = json.load(open(webcopy_file()))
    except Exception:
        data = None
    act = request.form.get("action") if request.method == "POST" else ""
    if act == "read":
        site = (request.form.get("site") or "").strip()
        _own = _norm_host(re.sub(r"^[a-z]+://", "", site.lower()).split("/")[0].split("?")[0])
        _mine = {_norm_host(request.host)}
        if _own and _own not in _mine:
            for _srow in db().execute("SELECT * FROM sites").fetchall():
                if _own in site_domains(_srow) and _is_this_app(site):
                    _mine.add(_own)
        if not site:
            result = {"ok": False, "error": "Type the website address."}
        elif _own in _mine:
            result = {"ok": False, "error": "That's this app's own address, so copying it just reads back what's already in your Settings. "
                      "Type the old website you want to copy from instead, like tigertowntogo.com or orderbulldawgfood.com, "
                      "or change the text and photos directly under Settings or Regions > Brand sites > Design."}
        else:
            try:
                data = multi_import.site_pull(site)
                with open(webcopy_file(), "w") as f:
                    json.dump(data, f)
                result = {"ok": True, "read": True}
            except Exception:
                result = {"ok": False, "error": "I couldn't open that website. Check the address and try again."}
    elif act == "apply" and data:
        if not request.form.get("permission"):
            result = {"ok": False, "error": "Tick the box to confirm you own this website or have the owner's permission."}
        else:
            con, done, failed = db(), [], []
            reg_done, reg_faq = [], None
            _fr = (request.form.get("faq_region") or "").strip()
            if request.form.get("use_faqs") and _fr.isdigit() and _region(int(_fr)) is not None:
                reg_faq = int(_fr)
                pairs = []
                for i, (q, a) in enumerate(data.get("faqs") or []):
                    if request.form.get("fk%d" % i):
                        pairs.append("Q: %s\nA: %s" % (" ".join((request.form.get("fq%d" % i) or q).split()),
                                                        " ".join((request.form.get("fa%d" % i) or a).split())))
                if pairs:
                    con.execute("UPDATE regions SET faq_text=? WHERE id=?", ("\n\n".join(pairs)[:12000], reg_faq))
                    con.commit()
                    rn = _region(reg_faq)["name"]
                    reg_done.append("%d FAQs for the %s region" % (len(pairs), rn))
                    log("site", "%s region FAQ filled from %s (%d questions)" % (rn, data.get("site") or "a website", len(pairs)))
            done = list(reg_done)
            bsid = brand_target(con, request.form.get("brand"), request.form.get("business_name") or data.get("business_name"))
            if bsid:
                picks, parts = {}, {"colors"} if request.form.get("use_colors") else set()
                for k, label, n in WEBCOPY_TEXT:
                    if request.form.get("use_" + k):
                        picks[k] = " ".join((request.form.get(k) or "").split())[:n]
                        parts.add({"business_name": "name", "dispatch_phone": "phone"}.get(k, "text"))
                for k in BRAND_TEXT_KEYS:
                    if k not in picks and not k.startswith("social_") and k != "faq_text":
                        picks[k] = ""   # unticked: leave the brand's own text alone
                for k in ("x", "facebook", "instagram"):
                    picks["social_" + k] = (request.form.get("social_" + k) or "").strip() if request.form.get("use_social_" + k) else ""
                    if picks["social_" + k]:
                        parts.add("socials")
                if request.form.get("use_faqs") and not reg_faq:
                    pairs = []
                    for i, (q, a) in enumerate(data.get("faqs") or []):
                        if request.form.get("fk%d" % i):
                            pairs.append("Q: %s\nA: %s" % (" ".join((request.form.get("fq%d" % i) or q).split()),
                                                            " ".join((request.form.get("fa%d" % i) or a).split())))
                    if pairs:
                        picks["faq_text"] = "\n\n".join(pairs)[:12000]
                cols = [c for c in request.form.getlist("color") if _hex_ok(c)]
                if cols:
                    picks["colors"] = cols
                if request.form.get("use_logo"):
                    parts.add("logo")
                for key in ("hero_image", "pocket_image"):
                    pick = request.form.get(key) or ""
                    if pick.isdigit() and int(pick) < len(data.get("pictures") or []):
                        picks[key] = data["pictures"][int(pick)]; parts.add(key)
                # only the ticked text goes in: drop blank picks so the brand keeps its own
                picks = {k: v for k, v in picks.items() if v not in ("", None)}
                done, failed = brand_fill(con, bsid, data, parts, picks)
                done = reg_done + list(done)
                b = site_by_id(bsid)
                log("site", "Brand " + b["name"] + " filled from " + (data.get("site") or "a website") + ": " + ", ".join(done))
                result = {"ok": True, "done": done, "failed": failed, "brand": b["name"], "brand_id": bsid}
                return render_template("dispatch_import_website.html", data=data, result=result, fields=WEBCOPY_TEXT,
                                       faqs=(data or {}).get("faqs") or [], faq_regions=faq_region_list(), sites=db().execute("SELECT id,name FROM sites ORDER BY sort,id").fetchall(),
                                       phone_fmt=nice_phone((data or {}).get("phone") or ""))
            def put(k, v):
                con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
            for k, label, n in WEBCOPY_TEXT:
                if request.form.get("use_" + k):
                    v = " ".join((request.form.get(k) or "").split())[:n]
                    if k == "dispatch_phone":
                        v = re.sub(r"\D", "", v)[-10:]
                    put(k, v)
                    done.append(label)
            for k in ("x", "facebook", "instagram"):
                if request.form.get("use_social_" + k):
                    v = (request.form.get("social_" + k) or "").strip()[:200]
                    if v and not v.startswith("http"):
                        v = "https://" + v
                    put("social_" + k, v)
                    done.append({"x": "X (Twitter)", "facebook": "Facebook", "instagram": "Instagram"}[k])
            if request.form.get("use_faqs") and not reg_faq:
                qs = [(request.form.get("fq%d" % i) or "").strip() for i in range(len(data.get("faqs") or []))]
                pairs = []
                for i, (q, a) in enumerate(data.get("faqs") or []):
                    if request.form.get("fk%d" % i):
                        pairs.append("Q: %s\nA: %s" % (" ".join((request.form.get("fq%d" % i) or q).split()),
                                                        " ".join((request.form.get("fa%d" % i) or a).split())))
                if pairs:
                    put("faq_text", "\n\n".join(pairs)[:12000])
                    done.append("%d FAQs" % len(pairs))
            if request.form.get("use_logo") and data.get("logo"):
                try:
                    name = _webcopy_picture(data["logo"], logo=True)
                    drop_media((setting("logo_image", str) or "").strip())
                    put("logo_image", name)
                    done.append("Logo")
                except Exception:
                    failed.append("Logo")
            for key, label in (("hero_image", "Top photo"), ("pocket_image", "App section photo")):
                pick = request.form.get(key) or ""
                if pick.isdigit() and int(pick) < len(data.get("pictures") or []):
                    try:
                        name = _webcopy_picture(data["pictures"][int(pick)])
                        drop_media((setting(key, str) or "").strip())
                        put(key, name)
                        done.append(label)
                    except Exception:
                        failed.append(label)
            con.commit()
            log("settings", "Customer website copied from " + (data.get("site") or "a website") + ": " + ", ".join(done))
            result = {"ok": True, "done": done, "failed": failed}
    faqs = (data or {}).get("faqs") or []
    return render_template("dispatch_import_website.html", data=data, result=result, fields=WEBCOPY_TEXT,
                           faqs=faqs, faq_regions=faq_region_list(), sites=db().execute("SELECT id,name FROM sites ORDER BY sort,id").fetchall(),
                           phone_fmt=nice_phone((data or {}).get("phone") or ""))


@app.route("/dispatch/import-zuppler", methods=["GET", "POST"])
def dispatch_import_zuppler():
    me = session.get("dispatcher_id")
    if not me:
        return redirect("/dispatch/login")
    if not is_owner(me):
        return "Only an owner can import restaurants.", 403
    data = tigertown_data()
    result = None
    if request.method == "POST" and request.form.get("action") == "fetch":
        site = (request.form.get("site") or "").strip()
        if not site:
            result = {"ok": False, "error": "Type the ordering website address."}
        elif not start_zuppler_pull(site):
            result = {"ok": False, "error": "Already reading a website. Wait for it to finish."}
        else:
            return redirect("/dispatch/import-zuppler")
    elif request.method == "POST" and request.form.get("action") == "match_min":
        fixed = 0
        if data:
            con = db()
            byz = {}
            for r in con.execute("SELECT id,name,zup_id,min_order_cents FROM restaurants").fetchall():
                byz["z" + str(r["zup_id"])] = r
                byz.setdefault(_norm_name(r["name"]), r)
            for z in data["restaurants"]:
                m = byz.get("z" + str(z["zid"])) or byz.get(_norm_name(z["name"]))
                want = z.get("min_order_cents") or None
                if m and m["min_order_cents"] != want:
                    con.execute("UPDATE restaurants SET min_order_cents=? WHERE id=?", (want, m["id"]))
                    log("settings", "Minimum order for " + m["name"] + " set to " + (money(want) if want else "none") + " to match " + ((data or {}).get("source") or "the Zuppler site"))
                    fixed += 1
            con.commit()
        result = {"ok": True, "min_fixed": fixed}
    elif request.method == "POST":
        picks = request.form.getlist("pick")
        rg = (request.form.get("region_id") or "") if regions_on() else ""
        result = tigertown_import(pick_ids=set(picks) if picks else set(),
                                  skip_paused=False, replace_menu=True,
                                  region_id=int(rg) if rg.isdigit() else None)
        if result.get("ok") and rg.isdigit() and request.form.get("use_fee") and _region(int(rg)) is not None:
            # the old site's delivery fee becomes that region's own delivery fee (the most common one)
            from collections import Counter
            want = {str(x) for x in picks}
            fees = [int(z.get("delivery_fee_cents") or 0) for z in (data or {}).get("restaurants") or []
                    if (not want or str(z["zid"]) in want) and int(z.get("delivery_fee_cents") or 0) > 0]
            if fees:
                cnt = Counter(fees)
                fee = cnt.most_common(1)[0][0]
                db().execute("UPDATE regions SET base_fee_cents=? WHERE id=?", (fee, int(rg)))
                db().commit()
                rn = _region(int(rg))["name"]
                result["fee_set"], result["fee_region"] = money(fee), rn
                result["fee_mixed"] = len(cnt) > 1
                log("region_fees", "%s delivery fee set to %s from %s" % (rn, money(fee), (data or {}).get("source") or "the import"))
            else:
                result["fee_none"] = True
        if result.get("ok") and not brands_on() and request.form.get("main_fill"):
            web = (data or {}).get("site") or re.sub(r"\s*\(.*\)$", "", (data or {}).get("source") or "")
            pulled = None
            if web:
                try:
                    import multi_import
                    pulled = multi_import.site_pull(web)
                except Exception:
                    pulled = None
            if pulled:
                result["main_done"], result["main_failed"] = main_fill(db(), pulled)
                log("settings", "Main website filled from " + web + ": " + ", ".join(result["main_done"]))
            else:
                result["main_failed"] = ["I couldn't read " + (web or "the website") + " for the website details"]
        if result.get("ok") and brands_on() and (request.form.get("brand") or "").strip():
            con = db()
            web = (data or {}).get("site") or re.sub(r"\s*\(.*\)$", "", (data or {}).get("source") or "")
            pulled = None
            if request.form.get("brand_fill") and web:
                try:
                    import multi_import
                    pulled = multi_import.site_pull(web)
                except Exception:
                    pulled = None
            hint = (pulled or {}).get("business_name") or re.sub(r"\s*\(.*\)$", "", (data or {}).get("source") or "")
            bsid = brand_target(con, request.form.get("brand"), hint)
            if bsid:
                bname = site_by_id(bsid)["name"]
                if rg.isdigit():
                    con.execute("UPDATE regions SET site_id=? WHERE id=?", (bsid, int(rg)))
                    con.commit()
                    result["brand_region"] = True
                if request.form.get("brand_fill"):
                    if pulled:
                        bd, bf = brand_fill(con, bsid, pulled,
                                            {"name", "phone", "logo", "colors", "hero_image", "pocket_image", "text", "socials"})
                        result["brand_done"], result["brand_failed"] = bd, bf
                    else:
                        result["brand_failed"] = ["I couldn't read " + (web or "the website") + " for the brand details"]
                result["brand"] = site_by_id(bsid)["name"]
                log("site", "Menu import tied to brand " + result["brand"])
        if result.get("ok"):
            fill_missing_pictures()
            start_picture_copy()
    rows = []
    if data:
        here = {}
        for r in db().execute("SELECT id,name,zup_id,min_order_cents FROM restaurants").fetchall():
            here[_norm_name(r["name"])] = r
            if r["zup_id"]:
                here["z" + str(r["zup_id"])] = r
        for z in data["restaurants"]:
            low = z["name"].lower()
            m = here.get("z" + str(z["zid"])) or here.get(_norm_name(z["name"]))
            rows.append({"zid": z["zid"], "name": z["name"], "cuisine": z["cuisine"], "address": z["address"],
                         "min_old": z.get("min_order_cents") or 0,
                         "min_here": (m["min_order_cents"] if m else None),
                         "min_ok": (not m) or ((m["min_order_cents"] or 0) == (z.get("min_order_cents") or 0)),
                         "photo": z["photo"], "logo": z["logo"], "items": len(z["items"]),
                         "pics": sum(1 for i in z["items"] if i["image"]),
                         "paused": bool(z.get("paused") or "(old)" in low or " dnd" in low),
                         "here": bool(m)})
    return render_template("dispatch_import.html", rows=rows, result=result, regions=faq_region_list(),
                           sites=db().execute("SELECT id,name FROM sites ORDER BY sort,id").fetchall(),
                           region_site={r["id"]: (r["site_id"] or 0) for r in db().execute("SELECT id,site_id FROM regions").fetchall()},
                           remote_pics=remote_picture_count(),
                           pulled=(data or {}).get("pulled", ""), source=(data or {}).get("source", ""),
                           site=(data or {}).get("site", ""), zup=ZUP_STATE)

def brand_phone_digits():
    """Phone numbers that belong to the delivery brands, never to a restaurant."""
    out = {"3342092844"}
    try:
        for r in db().execute("SELECT phone FROM sites").fetchall():
            d = digits(r["phone"])[-10:]
            if len(d) == 10:
                out.add(d)
    except Exception:
        pass
    for k in ("phone", "support_phone", "dispatch_phone", "business_phone"):
        try:
            row = db().execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            d = digits(row["value"] if row else "")[-10:]
            if len(d) == 10:
                out.add(d)
        except Exception:
            pass
    # one number on five or more restaurants is a delivery company's, not each restaurant's
    seen = {}
    for r in db().execute("SELECT phone FROM restaurants WHERE slug!='oneoff'").fetchall():
        d = digits(r["phone"])[-10:]
        if len(d) == 10:
            seen[d] = seen.get(d, 0) + 1
    out.update(d for d, n in seen.items() if n >= 5)
    return out


def places_lookup(name, address, lat=None, lng=None):
    """The restaurant's own phone and address from Google Places.
    Returns None when Google can't be asked (no key, error), {} when nothing matched."""
    if not GOOGLE_KEY or not (name or "").strip():
        return None
    body = {"textQuery": ", ".join(x for x in [name, address] if x), "maxResultCount": 3}
    if lat and lng:
        body["locationBias"] = {"circle": {"center": {"latitude": float(lat), "longitude": float(lng)},
                                           "radius": 2000.0}}
    req = urllib.request.Request("https://places.googleapis.com/v1/places:searchText",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_KEY,
                                          "X-Goog-FieldMask": "places.nationalPhoneNumber,places.formattedAddress,"
                                                              "places.location,places.displayName"})
    try:
        res = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    except Exception as e:
        print("places lookup failed:", name, e)
        return None
    for p in res.get("places") or []:
        loc = p.get("location") or {}
        if lat and lng and loc.get("latitude") is not None:
            try:
                if haversine_miles(float(lat), float(lng), loc["latitude"], loc["longitude"]) > 1.5:
                    continue
            except Exception:
                pass
        return {"phone": nice_phone(p.get("nationalPhoneNumber") or ""),
                "address": (p.get("formattedAddress") or "").replace(", USA", ""),
                "lat": loc.get("latitude"), "lng": loc.get("longitude")}
    return {}


def places_phone(name, address, lat=None, lng=None):
    return ((places_lookup(name, address, lat, lng) or {}).get("phone")) or ""


def _addr_needs_fix(a):
    a = (a or "").strip()
    return (not a) or (not any(ch.isdigit() for ch in a.split(",")[0])) or a.lower() in ("auburn, al", "auburn al")


def fix_rest_phones(only_ids=None, force=False):
    """Takes the delivery company's number off restaurants and fills in each restaurant's own phone
    number and street address from Google. force=True (the per-restaurant button) uses Google's
    phone and address even when the restaurant already has one."""
    brand = brand_phone_digits()
    rows = db().execute("""SELECT r.id, r.name, r.address, r.phone, r.lat, r.lng, COALESCE(r.places_checked,0) AS pc,
                                  g.name AS rg FROM restaurants r LEFT JOIN regions g ON g.id=r.region_id
                           WHERE r.slug!='oneoff'""").fetchall()
    fixed = found = addrs = 0
    out = {}
    for r in rows:
        if only_ids is not None and r["id"] not in only_ids:
            continue
        d = digits(r["phone"])[-10:]
        wrong = d in brand
        if wrong:
            db().execute("UPDATE restaurants SET phone='' WHERE id=?", (r["id"],))
            fixed += 1
        need_phone = wrong or len(d) != 10
        need_addr = _addr_needs_fix(r["address"])
        if (force or ((need_phone or need_addr) and not r["pc"])):
            where = r["address"] if not need_addr else ", ".join(x for x in [r["address"], r["rg"] or "", "AL"] if x)
            g = places_lookup(r["name"], where, r["lat"], r["lng"])
            if g is None:
                continue          # Google not set up or not answering: try again next time
            ph = g.get("phone") or ""
            if ph and digits(ph)[-10:] not in brand and (need_phone or force):
                db().execute("UPDATE restaurants SET phone=? WHERE id=?", (ph, r["id"]))
                found += 1
            if g.get("address") and (need_addr or force):
                db().execute("UPDATE restaurants SET address=?, lat=COALESCE(?,lat), lng=COALESCE(?,lng) WHERE id=?",
                             (g["address"], g.get("lat"), g.get("lng"), r["id"]))
                addrs += 1
            db().execute("UPDATE restaurants SET places_checked=1 WHERE id=?", (r["id"],))
            out = g
        db().commit()
    print("restaurants: removed %d delivery-company numbers, found %d phones and %d addresses" % (fixed, found, addrs))
    return {"removed": fixed, "found": found, "addresses": addrs, "last": out}


def _startup_phone_repair():
    time.sleep(15)   # let the database finish its start-up changes first
    try:
        with app.app_context():
            ensure_column(db(), "restaurants", "places_checked", "INTEGER DEFAULT 0")
            fix_rest_phones()
    except Exception as e:
        print("phone repair skipped:", e)


threading.Thread(target=_startup_phone_repair, daemon=True).start()


@app.post("/api/dispatch/restaurant-google")
def api_dispatch_restaurant_google():
    """Dispatch button: fill one restaurant's phone and address from Google."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not GOOGLE_KEY:
        return jsonify({"ok": False, "error": "Add GOOGLE_MAPS_API_KEY in Railway first."}), 400
    try:
        rid = int((request.get_json(force=True, silent=True) or {}).get("restaurant_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    r = db().execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not r:
        return jsonify({"ok": False, "error": "Restaurant not found."}), 404
    res = fix_rest_phones(only_ids={rid}, force=True)
    row = db().execute("SELECT phone,address FROM restaurants WHERE id=?", (rid,)).fetchone()
    if not res["last"]:
        return jsonify({"ok": False, "error": "Google couldn't find " + r["name"] + ". Type the phone and address in yourself."}), 404
    return jsonify({"ok": True, "phone": row["phone"], "address": row["address"]})


def _startup_picture_repair():
    """Once at start-up: put back item pictures that went blank or missing, then copy them here."""
    try:
        with app.app_context():
            if fill_missing_pictures():
                start_picture_copy()
    except Exception as e:
        print("picture repair skipped:", e)


if os.environ.get("PICTURE_REPAIR", "1") != "0":
    threading.Thread(target=_startup_picture_repair, daemon=True).start()


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now()})

init_db()


def _auto_secret_key():
    """No SECRET_KEY in Railway: make a random one the first time the app starts and
    keep it in the database, so sign-ins stay secure and survive every deploy."""
    if (os.environ.get("SECRET_KEY") or "").strip():
        return
    try:
        con = dbx.connect(DB_PATH)
        row = con.execute("SELECT value FROM settings WHERE key='auto_secret_key'").fetchone()
        key = (row[0] if row else "") or ""
        if len(key) < 32:
            key = secrets.token_hex(32)
            con.execute("DELETE FROM settings WHERE key='auto_secret_key'")
            con.execute("INSERT INTO settings(key, value) VALUES('auto_secret_key', ?)", (key,))
            con.commit()
        con.close()
        app.secret_key = key
    except Exception as e:
        print("auto secret key not saved, using the built-in key:", e)


_auto_secret_key()
seed_brand_photos()
fix_old_railway_company_urls()
seed_tiger_town_logo()

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
        biz = {"biz_name": (setting("business_name", str) or "Fleet Foot Delivery").strip() or "Fleet Foot Delivery",
               "biz_address": (setting("business_address", str) or "").strip(),
               "biz_phone": ("(%s) %s-%s" % (_d[:3], _d[3:6], _d[6:])) if len(_d) == 10 else _ph,
               "biz_tel": tel_digits(_d),
               "tax_bp": setting("tax_rate_bp") or 0, "service_bp": setting("service_fee_bp") or 0,
               "logo_url": logo_url()}
        if request.path == "/":
            biz["site"] = site_text()
        _cp = current_portal()
        if not _cp:
            _bc = brand_contact()
            if _bc is not None:
                biz["biz_address"] = _bc["address"]
            biz.update({"biz_email": _bc["email"] if _bc is not None else (setting("business_email", str) or "").strip(),
                        "biz_hours": business_hours_label(None) or "",
                        "home_headline": (setting("home_headline", str) or "").strip(),
                        "socials": [(k, (setting("social_" + k, str) or "").strip()) for k in ("x", "facebook", "instagram")
                                    if (setting("social_" + k, str) or "").strip().startswith("http")],
                        "year": dt.date.today().year})
        elif _cp == "dispatch" and session.get("dispatcher_id"):
            biz["app_new"] = new_application_count()
            biz["is_owner_flag"] = is_owner()
            biz["allow_cash"] = cash_allowed()
            biz["texting_ok"] = texting_on()
    except Exception:
        biz = {"biz_name": "Fleet Foot Delivery", "biz_address": "", "biz_phone": "", "biz_tel": "",
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


def dispatch_phone(rid=None):
    """The dispatch number for a region when it has its own, otherwise the business number."""
    if rid:
        try:
            r = db().execute("SELECT phone FROM regions WHERE id=?", (int(rid),)).fetchone()
            if r and (r["phone"] or "").strip():
                return r["phone"].strip()
            s = site_of_region(rid)
            if s is not None and (s["phone"] or "").strip():
                return s["phone"].strip()
        except Exception:
            pass
    return (setting("dispatch_phone", str) or "").strip()


def driver_phone_region(did):
    """Which region's dispatch number a driver should call: the region of their live order,
    else the one region they are working. None when it's unclear (business number)."""
    if not did:
        return None
    lk = driver_locked_regions(did)
    if len(lk) == 1:
        return next(iter(lk))
    wr = driver_work_regions(did)
    if len(wr) == 1:
        return next(iter(wr))
    return None


def nice_phone(p):
    d = "".join(c for c in (p or "") if c.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return ("(%s) %s-%s" % (d[:3], d[3:6], d[6:])) if len(d) == 10 else (p or "")

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
        item = {"id": a["id"], "who": a["who"], "name": a["name"], "driver_id": a["driver_id"],
                "phone": a["phone"] or "", "tel": tel_digits(a["phone"]),
                "note": a["note"] or "", "order": a["code"] or "",
                "at": clock(a["created_at"]), "when": a["created_at"], "location": None}
        _ak = a.keys()
        item["kind"] = (a["kind"] if "kind" in _ak else None) or ""
        item["trip"] = None
        if item["kind"] == "911" and a["order_id"]:
            try:
                _o = db().execute("SELECT * FROM orders WHERE id=?", (a["order_id"],)).fetchone()
                if _o:
                    _r = db().execute("SELECT name FROM restaurants WHERE id=?", (_o["restaurant_id"],)).fetchone()
                    item["trip"] = {"code": _o["code"], "primary_no": (_rv(_o, "primary_no") or ""),
                                    "restaurant": (_r["name"] if _r else ""), "customer": _o["customer_name"] or "",
                                    "address": _o["address"] or ""}
            except Exception:
                pass
        if a["who"] == "driver" and a["driver_id"]:
            d = db().execute("SELECT * FROM drivers WHERE id=?", (a["driver_id"],)).fetchone()
            if d:
                loc = loc_block(d)
                if not loc and item["kind"] == "911" and "lat" in _ak and a["lat"] is not None and a["lng"] is not None:
                    _ll = str(round(a["lat"], 6)) + "," + str(round(a["lng"], 6))
                    loc = {"lat": a["lat"], "lng": a["lng"], "address": None, "at": clock(a["created_at"]),
                           "map_url": "https://www.google.com/maps/search/?api=1&query=" + _ll}
                if loc and not loc.get("address"):
                    loc["address"] = update_driver_addr(d["id"], d["last_lat"], d["last_lng"])
                if loc and not loc.get("at"):
                    loc["at"] = clock(d["last_loc_at"])
                item["location"] = loc
        out.append(item)
    return out


def accept_limit(key, default):
    """Minutes from Settings. A saved 0 means off, so only a missing value uses the default."""
    try:
        v = setting(key, str)
        if v is None or str(v).strip() == "":
            return default
        return max(0, min(60, int(float(v))))
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

def remind_unreceived():
    """No chat message goes out when an order is handed to a driver. Only if they still
    haven't tapped Received after the minutes set in Settings (Driver must tap Received
    within) does an automatic reminder go out, once per hand-off. 0 turns reminders off."""
    dm = accept_limit("driver_accept_min", 3)
    if not dm:
        return 0
    cut = (dt.datetime.now() - dt.timedelta(minutes=dm)).isoformat(timespec="seconds")
    rows = db().execute("""SELECT id, code, driver_id, driver_paged_at FROM orders
                           WHERE dispatch_status='assigned' AND driver_id IS NOT NULL
                             AND driver_paged_at IS NOT NULL AND driver_paged_at < ?
                             AND COALESCE(driver_reminded_for,'') != driver_paged_at""", (cut,)).fetchall()
    for o in rows:
        auto_msg("drv_reminder", "INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                     (o["driver_id"], "system", "Order " + o["code"] + " is still waiting for you. "
                      "Tap Received to accept it, or call dispatch if you can't take it.", now()))
        db().execute("UPDATE orders SET driver_reminded_for=? WHERE id=?", (o["driver_paged_at"], o["id"]))
    if rows:
        db().commit()
    return len(rows)


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
    if on and not business_is_open():
        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_opened_at',?)", (now(),))
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('business_open',?)", ("1" if on else "0",))
    if not on:
        # end of day: drivers start tomorrow with an empty Completed tab. Dispatch keeps the history.
        db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('driver_done_cleared_at',?)", (now(),))
        # closing the business clocks every driver out
        for _d in db().execute("SELECT id FROM drivers WHERE COALESCE(status,'offline')!='offline'").fetchall():
            try:
                set_driver_status(_d["id"], "offline", "The business is closed, so you are offline now.")
            except Exception as e:
                print("close: driver offline skipped", _d["id"], e)
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


def region_tz(rid):
    """The region's own time zone, or the app's when it has none set."""
    if not rid:
        return APP_TZ
    try:
        row = db().execute("SELECT tz FROM regions WHERE id=?", (int(rid),)).fetchone()
    except Exception:
        return APP_TZ
    tz = ((row["tz"] if row else "") or "").strip()
    return tz if tz and _zone(tz) is not None else APP_TZ

def to_region(when, rid):
    """App time -> the region's local time."""
    return tz_shift(when, APP_TZ, region_tz(rid))

def from_region(when, rid):
    """The region's local time -> app time (what gets saved)."""
    return tz_shift(when, region_tz(rid), APP_TZ)

def region_now(rid):
    return to_region(dt.datetime.now().replace(microsecond=0), rid)

def tz_tag(rid):
    """' ET' after a time when the region is not on the app's zone, so dispatch can tell."""
    tz = region_tz(rid)
    return "" if tz == APP_TZ else " " + TZ_SHORT.get(tz, tz.split("/")[-1].replace("_", " "))

def _region(rid):
    if not rid:
        return None
    try:
        return db().execute("SELECT * FROM regions WHERE id=?", (int(rid),)).fetchone()
    except Exception:
        return None


def region_own_hours(rid):
    """A region's own hours, or {} when it uses the business hours."""
    r = _region(rid)
    if not r:
        return {}
    try:
        h = json.loads(r["hours"] or "{}")
    except Exception:
        h = {}
    return h if isinstance(h, dict) else {}


def region_closed_list(rid):
    """Every closed date on the region (all day or set hours). Past dates drop off."""
    r = _region(rid)
    if not r:
        return []
    try:
        today = region_now(rid).date().isoformat()   # the region's own date (Athens GA runs on Eastern)
    except Exception:
        today = dt.date.today().isoformat()
    return sorted({d for d in (r["closed_dates"] or "").split(",") if d and d >= today})


def region_closed_hours(rid):
    """{"2026-11-26": ["15:00", "23:59"]}: dates the region is closed only for set hours."""
    r = _region(rid)
    if not r or "closed_hours" not in r.keys():
        return {}
    try:
        h = json.loads(r["closed_hours"] or "{}")
    except Exception:
        h = {}
    if not isinstance(h, dict):
        return {}
    keep = set(region_closed_list(rid))
    return {d: v for d, v in h.items() if d in keep and isinstance(v, list) and len(v) == 2
            and _hm(v[0]) is not None and _hm(v[1]) is not None}


def region_closed_reasons(rid):
    """{"2026-11-26": "Thanksgiving"}: the optional reason dispatch gave for a closed date."""
    r = _region(rid)
    if not r or "closed_reasons" not in r.keys():
        return {}
    try:
        h = json.loads(r["closed_reasons"] or "{}")
    except Exception:
        h = {}
    if not isinstance(h, dict):
        return {}
    keep = set(region_closed_list(rid))
    return {d: str(v).strip() for d, v in h.items() if d in keep and str(v or "").strip()}


def region_closed_today(rid):
    """'Closed today for Thanksgiving' when the region is closed (all day or set hours) today."""
    if not rid:
        return ""
    today = region_now(rid).date().isoformat()
    if today not in set(region_closed_list(rid)):
        return ""
    part = region_closed_hours(rid).get(today)
    why = region_closed_reasons(rid).get(today, "")
    t = "Closed today" + ((" " + _ampm(part[0]) + " - " + ("close" if part[1] == "23:59" else _ampm(part[1]))) if part else "")
    return t + ((" for " + why) if why else "")


def region_closed_dates(rid):
    """Dates a region is closed all day, like holidays. Past dates drop off."""
    part = region_closed_hours(rid)
    return [d for d in region_closed_list(rid) if d not in part]


def region_closed_now(rid, when):
    """Inside one of the region's set closed hours for that date?"""
    if not rid:
        return False
    span = region_closed_hours(rid).get(when.date().isoformat())
    if not span:
        return False
    m = when.hour * 60 + when.minute
    a, b = _hm(span[0]), _hm(span[1])
    if b == 23 * 60 + 59:
        b = 1440
    return a <= m < b


def business_hours(rid=None):
    """{"0": ["10:00", "22:00"], ...} Monday is 0. Empty means no hours limit is set.
    A region with its own hours uses those; otherwise the business hours."""
    own = region_own_hours(rid) if rid else {}
    if own:
        return own
    try:
        h = json.loads(setting("business_hours", str) or "{}")
    except Exception:
        h = {}
    return h if isinstance(h, dict) else {}


def business_in_hours(when=None, rid=None):
    """Inside operating hours (the region's when it has its own) and not on one of the
    region's closed dates? True when no hours are set and no closed date applies."""
    when = to_region(when or dt.datetime.now(), rid)
    if region_closed_now(rid, when):
        return False
    closed = set(region_closed_dates(rid)) if rid else set()
    h = business_hours(rid)
    if not h:
        return when.date().isoformat() not in closed
    m = when.hour * 60 + when.minute
    span = h.get(str(when.weekday())) or ["", ""]
    o, c = _hm(span[0]) if span[0] else None, _hm(span[1]) if span[1] else None
    if o is not None and c is not None:
        if (c > o and o <= m < c) or (c <= o and m >= o):
            return when.date().isoformat() not in closed
    # the night before running past midnight belongs to the day before
    y = h.get(str((when.weekday() - 1) % 7)) or ["", ""]
    yo, yc = _hm(y[0]) if y[0] else None, _hm(y[1]) if y[1] else None
    if yo is not None and yc is not None and yc <= yo and m < yc:
        return (when.date() - dt.timedelta(days=1)).isoformat() not in closed
    return False


def business_hours_rows(rid=None):
    h = business_hours(rid)
    rows = []
    for k, name in enumerate(BH_DAYS):
        span = h.get(str(k))
        rows.append({"k": k, "day": name,
                     "open": (span or ["", ""])[0], "close": (span or ["", ""])[1],
                     "closed": bool(h) and not (span and span[0] and span[1])})
    return rows


def business_hours_label(rid=None):
    """'Mon - Fri 10:00 AM - 10:00 PM, Sat 11:00 AM - 11:00 PM, Sun closed'."""
    h = business_hours(rid)
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


def closed_dates_label(rid, limit=3):
    """'Closed Thu Nov 26, Fri Dec 25' for the next few closed dates."""
    ds = region_closed_list(rid)[:limit]
    if not ds:
        return ""
    part = region_closed_hours(rid)
    why = region_closed_reasons(rid)
    def one(d):
        t = dt.date.fromisoformat(d).strftime("%a %b %-d")
        if d in part:
            a, b = part[d]
            t += " " + _ampm(a) + " - " + ("close" if b == "23:59" else _ampm(b))
        if why.get(d):
            t += " (" + why[d] + ")"
        return t
    return "Closed " + ", ".join(one(d) for d in ds)


app.jinja_env.globals["closed_dates_label"] = closed_dates_label


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

# ---------------------------------------------------------------- staffing alerts for dispatch
# Two warnings on the dispatch board, worked out for each region on their own:
#  * orders waiting there and no free driver who works that region
#  * no new orders there for a while and more free drivers than needed (overstaffed)
SA_DEFAULTS = {"sa_nodrv": "1", "sa_nodrv_min": "3", "sa_idle": "1", "sa_idle_min": "30", "sa_idle_drivers": "2"}
SA_LIMITS = {"sa_nodrv_min": (1, 60), "sa_idle_min": (10, 240), "sa_idle_drivers": (1, 20)}

def sa_get(key):
    v = setting(key, str)
    if v in (None, ""):
        v = SA_DEFAULTS[key]
    if key in SA_LIMITS:
        lo, hi = SA_LIMITS[key]
        try:
            return max(lo, min(hi, int(float(v))))
        except (TypeError, ValueError):
            return int(SA_DEFAULTS[key])
    return str(v) == "1"

def _sa_snoozed():
    try:
        raw = json.loads(setting("sa_snooze", str) or "{}")
    except Exception:
        raw = {}
    stamp = now()
    return {k: v for k, v in raw.items() if isinstance(v, str) and v > stamp}

def _sa_ago(ts):
    try:
        mins = int((dt.datetime.now() - dt.datetime.fromisoformat(str(ts)[:19])).total_seconds() // 60)
    except Exception:
        return ""
    return ("%d min" % mins) if mins < 120 else ("%d hr %d min" % (mins // 60, mins % 60))

def staff_alerts():
    """Live staffing warnings for the dispatch board, one per region that needs one."""
    try:
        if not business_is_open() or not (sa_get("sa_nodrv") or sa_get("sa_idle")):
            return []
        con = db()
        regs = con.execute("SELECT id, name, COALESCE(paused,0) paused FROM regions ORDER BY sort, name").fetchall()
        areas = [(r["id"], r["name"]) for r in regs if not r["paused"]] if regs else [(0, "All areas")]
        shift = on_shift_drivers()
        work = {d["id"]: driver_work_regions(d["id"]) for d in shift}
        snoozed = _sa_snoozed()
        out = []
        nowdt = dt.datetime.now()
        waiting = con.execute("""SELECT id, code, region_id, created_at FROM orders
                                 WHERE driver_id IS NULL AND redo_driver_id IS NULL
                                   AND dispatch_status IN ('queued','held')""").fetchall()
        opened = setting("business_opened_at", str) or ""
        for rid, rname in areas:
            mine = [o for o in waiting if not rid or (o["region_id"] or 0) in (rid, 0)]
            on = [d for d in shift if driver_covers(work.get(d["id"]), rid)]
            free = [d for d in on if d["load"] == 0]
            if sa_get("sa_nodrv"):
                cut = (nowdt - dt.timedelta(minutes=sa_get("sa_nodrv_min"))).isoformat(timespec="seconds")
                late = [o for o in mine if (o["created_at"] or "") <= cut]
                key = "nodrv-%d" % rid
                if late and not free and key not in snoozed:
                    n = len(mine)
                    codes = ", ".join(o["code"] for o in mine[:4]) + (" and more" if n > 4 else "")
                    why = ("no drivers on shift for " + rname) if not on else \
                          ("all %d driver%s on shift for %s %s busy" % (len(on), "" if len(on) == 1 else "s", rname,
                                                                        "is" if len(on) == 1 else "are"))
                    out.append({"key": key, "kind": "nodrv", "region_id": rid, "region": rname,
                                "title": "%s: %d order%s waiting, no driver free" % (rname, n, "" if n == 1 else "s"),
                                "body": codes + " waiting " + _sa_ago(min(o["created_at"] or now() for o in mine)) +
                                        ", and " + why + ". Call in a driver or hold the orders."})
            if sa_get("sa_idle") and not mine and len(free) >= sa_get("sa_idle_drivers"):
                idle_min = sa_get("sa_idle_min")
                if rid:
                    last = con.execute("SELECT MAX(created_at) m FROM orders WHERE region_id=?", (rid,)).fetchone()["m"]
                else:
                    last = con.execute("SELECT MAX(created_at) m FROM orders").fetchone()["m"]
                since = max(last or "", opened or "")
                cut = (nowdt - dt.timedelta(minutes=idle_min)).isoformat(timespec="seconds")
                key = "idle-%d" % rid
                if since and since <= cut and key not in snoozed:
                    names = ", ".join(d["name"] for d in free[:5]) + (" and more" if len(free) > 5 else "")
                    out.append({"key": key, "kind": "idle", "region_id": rid, "region": rname,
                                "title": "%s: no orders in %s" % (rname, _sa_ago(since)),
                                "body": "%d drivers are free (%s). You may have more drivers on than you need right now."
                                        % (len(free), names)})
        return out
    except Exception as e:
        print("staff alerts skipped:", e)
        return []

@app.post("/api/dispatch/staff-alert-dismiss")
def api_staff_alert_dismiss():
    """Got it: hide one staffing alert for a while (10 min for no drivers, the idle window for idle)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    key = str((request.get_json(silent=True) or {}).get("key") or "")[:40]
    if not re.fullmatch(r"(nodrv|idle)-\d+", key):
        return jsonify({"ok": False, "error": "Unknown alert."}), 400
    mins = 10 if key.startswith("nodrv") else sa_get("sa_idle_min")
    snz = _sa_snoozed()
    snz[key] = (dt.datetime.now() + dt.timedelta(minutes=mins)).isoformat(timespec="seconds")
    db().execute("INSERT OR REPLACE INTO settings(key,value) VALUES('sa_snooze',?)", (json.dumps(snz),))
    db().commit()
    log("staff_alert", key + " snoozed " + str(mins) + " min by " + (session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True, "staff": staff_alerts()})

@app.get("/api/dispatch/alerts")
def api_dispatch_alerts():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    alerts = open_call_alerts()
    if dispatcher_driver_scope() is not None:
        alerts = [a for a in alerts if a.get("who") != "driver" or driver_in_scope(a.get("driver_id"))]
    return jsonify({"ok": True, "alerts": alerts, "awaiting": awaiting_accept(), "staff": staff_alerts()})

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
    oreg = None
    if data.get("order_id"):
        _o = db().execute("SELECT region_id FROM orders WHERE id=? AND driver_id=?", (data.get("order_id"), did)).fetchone()
        oreg = _o["region_id"] if _o else None
    return jsonify({"ok": True, "phone": dispatch_phone(oreg or driver_phone_region(did))})

@app.post("/api/driver/call-911")
def api_driver_call_911():
    """The driver hit Call 911. Their phone dials 911 itself; this puts an urgent alert on the
    dispatch board with the order they are on right now and where they are."""
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    d = db().execute("SELECT * FROM drivers WHERE id=?", (did,)).fetchone()
    data = request.get_json(silent=True) or {}
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        lat = lng = None
    if lat is not None:
        db().execute("UPDATE drivers SET last_lat=?,last_lng=?,last_loc_at=? WHERE id=?", (lat, lng, now(), did))
        db().commit()
        try:
            update_driver_addr(did, lat, lng, force=True)
        except Exception:
            pass
    # the order they are on right now: furthest along first (en route, at restaurant, accepted, waiting)
    oid = None
    rank = {"enroute": 0, "at_restaurant": 1, "received": 2, "assigned": 3}
    live = db().execute("""SELECT id, dispatch_status FROM orders WHERE driver_id=?
                           AND dispatch_status IN ('assigned','received','at_restaurant','enroute')""", (did,)).fetchall()
    if live:
        oid = sorted(live, key=lambda r: (rank.get(r["dispatch_status"], 9), r["id"]))[0]["id"]
    stage_words = {"enroute": "on the way to the customer (food already picked up)", "at_restaurant": "at the restaurant",
                   "received": "heading to the restaurant", "assigned": "not accepted yet"}
    note = "URGENT: 911 is being called right now. Follow up with the driver."
    reason = " ".join(str(data.get("reason") or "").split())[:200]
    if reason:
        note += " Reason: " + reason + ("" if reason.endswith((".", "!", "?")) else ".")
    # food already picked up (en route) stays with the driver; anything not picked up goes back in the queue
    keep = [r for r in live if r["dispatch_status"] == "enroute"]
    moved = [r for r in live if r["dispatch_status"] != "enroute"]
    if live:
        top = [r for r in live if r["id"] == oid][0]
        note += " They were " + stage_words.get(top["dispatch_status"], top["dispatch_status"]) + "."
        note += " They are on break now."
        if keep:
            note += " %s stayed with them (not reassigned)." % (
                "The order they picked up" if len(keep) == 1 else "The %d orders they picked up" % len(keep))
        if moved:
            note += " %s back in the queue for another driver." % (
                "1 order not picked up yet went" if len(moved) == 1 else "%d orders not picked up yet went" % len(moved))
    else:
        note += " They were not on an order. They are on break now."
    db().execute("""INSERT INTO call_alerts(who,driver_id,order_id,name,phone,note,created_at,kind,lat,lng)
                    VALUES('driver',?,?,?,?,?,?,'911',?,?)""",
                 (did, oid, d["name"], d["phone"], note, now(), lat, lng))
    db().commit()
    log("call_alert", "driver " + (d["name"] or "") + ": CALLING 911" + (" (order id %s)" % oid if oid else ""))
    # automatic message from the driver in their dispatch chat
    _code = None
    if oid:
        _c = db().execute("SELECT code, primary_no FROM orders WHERE id=?", (oid,)).fetchone()
        _code = (_rv(_c, "primary_no") or _c["code"]) if _c else None
    db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)",
                 (did, "driver", "Automatic message: I am calling 911 right now. Please follow up with me." +
                  (" I was on order " + _code + "." if _code else "") +
                  (" Reason: " + reason if reason else ""), now()))
    db().commit()
    # hand every live order back to the queue so another driver gets it
    if moved:
        db().execute("""UPDATE orders SET driver_id=NULL, stack_seq=NULL, dispatch_status='queued'
                        WHERE driver_id=? AND dispatch_status IN ('assigned','received','at_restaurant')""", (did,))
        db().commit()
        for r in moved:
            log("order", "order id %s taken off %s (driver called 911) and put back in the queue" % (r["id"], d["name"] or "driver"))
    # on break so nothing new comes to them; set_driver_status also reassigns the queue (en route orders stay)
    set_driver_status(did, "break", "You hit Call 911, so you are on break" +
                      (". The order you picked up is still yours" if keep else "") +
                      (" and your order that wasn't picked up went to another driver" if moved and keep else
                       " and your order went to another driver" if moved else "") + ". Dispatch will check on you.")
    return jsonify({"ok": True})

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
    return jsonify({"ok": True, "phone": dispatch_phone((o["region_id"] if o else None) or r["region_id"])})

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


# ---------------------------------------------------------------- customer accounts, rewards, gift cards
# Customers can make an account (phone + password) to earn rewards points and keep cards on file.
# Cards on file live in PayPal's vault: the site only keeps the brand, last 4 and PayPal's token.
from werkzeug.security import generate_password_hash, check_password_hash

GIFT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

def _okeys(o):
    try:
        return o.keys()
    except Exception:
        return []

def credits_cents(o):
    k = _okeys(o)
    g = int(o["gift_cents"] or 0) if "gift_cents" in k else 0
    r = int(o["reward_cents"] or 0) if "reward_cents" in k else 0
    return g + r

def due_cents(o):
    """What the customer still pays after gift cards and rewards."""
    return max(0, int(o["total_cents"] or 0) - credits_cents(o))

def phone_digits(p):
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[1:] if (len(d) == 11 and d.startswith("1")) else d

def current_customer():
    cid = session.get("customer_id")
    if not cid:
        return None
    return db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()

def loyalty_on():
    return bool(setting("loyalty_on"))

TIER_NAMES = ("Starter", "VIP", "Elite")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
EARN_KINDS = ("earn", "bonus", "signup", "review", "reverse")

def _sint(key, dflt):
    try:
        v = setting(key)
        return dflt if v is None else int(v)
    except (TypeError, ValueError):
        return dflt

def reward_options():
    """The rewards a customer can pick at checkout, cheapest first: [{points, cents, value}]."""
    out = []
    for part in str(setting("reward_options", str) or "150:300,250:500,400:1000").split(","):
        try:
            a, b = part.split(":")
            pts, cents = int(a), int(b)
        except ValueError:
            continue
        if pts > 0 and cents > 0:
            out.append({"points": pts, "cents": cents, "value": money(cents)})
    return sorted(out, key=lambda x: x["points"])

def tier_rules():
    vip, elite = max(1, _sint("tier_vip_points", 750)), max(2, _sint("tier_elite_points", 1500))
    return [{"name": "Starter", "min": 0, "bonus_pct": max(0, _sint("bonus_starter_pct", 50))},
            {"name": "VIP", "min": vip, "bonus_pct": max(0, _sint("bonus_vip_pct", 100))},
            {"name": "Elite", "min": max(elite, vip + 1), "bonus_pct": max(0, _sint("bonus_elite_pct", 200))}]

def bonus_label(pct):
    return {50: "50% more points", 100: "double points", 200: "triple points", 300: "4x points"}.get(int(pct), str(int(pct)) + "% more points")

def reward_rules():
    opts = reward_options()
    wd = _sint("bonus_weekday", 1)
    tiers = tier_rules()
    for t in tiers:
        t["bonus"] = bonus_label(t["bonus_pct"]) if t["bonus_pct"] else ""
    return {"on": loyalty_on(), "per_dollar": max(0, _sint("points_per_dollar", 1)),
            "signup_points": max(0, _sint("signup_points", 50)), "review_points": max(0, _sint("review_points", 25)),
            "options": opts, "tiers": tiers, "bonus_weekday": wd if 0 <= wd <= 6 else -1,
            "bonus_day": WEEKDAYS[wd] if 0 <= wd <= 6 else "", "bonus_day_short": WEEKDAYS[wd][:3] if 0 <= wd <= 6 else "",
            # kept for older pages: the smallest reward
            "points": opts[0]["points"] if opts else 100, "value_cents": opts[0]["cents"] if opts else 0}

def tier_points(cid):
    """Points earned in the past 365 days (spending points doesn't lower it)."""
    since = (dt.datetime.now() - dt.timedelta(days=365)).isoformat(timespec="seconds")
    r = db().execute("SELECT COALESCE(SUM(points),0) AS n FROM points_log WHERE customer_id=? AND created_at>=? AND kind IN "
                     "('earn','bonus','signup','review','reverse')", (cid, since)).fetchone()
    return max(0, int(r["n"] or 0))

def tier_for(tp):
    cur = tier_rules()[0]
    for t in tier_rules():
        if tp >= t["min"]:
            cur = t
    return cur

def rewards_available(points):
    if not loyalty_on():
        return 0
    return len([o for o in reward_options() if int(points or 0) >= o["points"]])

def customer_public(c):
    if not c:
        return None
    rr = reward_rules()
    cards = db().execute("SELECT id, brand, last4, expiry FROM saved_cards WHERE customer_id=? AND COALESCE(pp_acct,0)=? "
                         "ORDER BY id DESC", (c["id"], pp_conf()["acct"])).fetchall()
    pts = int(c["points"] or 0)
    tp = tier_points(c["id"])
    tier = tier_for(tp)
    nxt = next((t for t in rr["tiers"] if t["min"] > tp), None)
    opts = [dict(o, ok=pts >= o["points"]) for o in rr["options"]]
    nxt_reward = next((o for o in rr["options"] if o["points"] > pts), None)
    return {"id": c["id"], "name": c["name"] or "", "phone": nice_phone(c["phone"]), "email": c["email"] or "",
            "address": c["address"] or "", "existing": bool(c["verified"]),
            "points": pts, "rewards": len([o for o in opts if o["ok"]]) if rr["on"] else 0,
            "reward_options": opts, "reward_value": money(rr["value_cents"]),
            "reward_points": rr["points"], "loyalty_on": rr["on"],
            "next_reward_in": (nxt_reward["points"] - pts) if (rr["on"] and nxt_reward) else 0,
            "next_reward_value": nxt_reward["value"] if nxt_reward else "",
            "tier": tier["name"], "tier_points": tp, "tier_bonus": tier.get("bonus") or bonus_label(tier["bonus_pct"]),
            "next_tier": nxt["name"] if nxt else "", "next_tier_in": (nxt["min"] - tp) if nxt else 0,
            "tier_pct": 100 if not nxt else int(100 * (tp - tier["min"]) / max(1, nxt["min"] - tier["min"])),
            "cards": [{"id": x["id"], "label": (x["brand"] or "Card").title() + " ending " + (x["last4"] or "????"),
                       "expiry": x["expiry"] or ""} for x in cards]}

def gift_norm(code):
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())

def gift_find(code):
    n = gift_norm(code)
    if len(n) < 8:
        return None
    for g in db().execute("SELECT * FROM gift_cards WHERE status IN ('active','used','void')").fetchall():
        if gift_norm(g["code"]) == n:
            return g
    return None

def gift_new_code():
    while True:
        raw = "".join(secrets.choice(GIFT_ALPHABET) for _ in range(12))
        code = "FD-" + raw[:4] + "-" + raw[4:8] + "-" + raw[8:]
        if not db().execute("SELECT 1 FROM gift_cards WHERE code=?", (code,)).fetchone():
            return code

def gift_public(g, full=False):
    out = {"code": g["code"] if full else ("FD-****-****-" + g["code"][-4:]), "balance": money(g["balance_cents"]),
           "balance_cents": int(g["balance_cents"]), "initial": money(g["initial_cents"]), "status": g["status"]}
    if full:
        out.update({"id": g["id"], "buyer": g["buyer_name"] or "", "buyer_phone": nice_phone(g["buyer_phone"]),
                    "to": g["to_name"] or "", "message": g["message"] or "", "sold_by": g["sold_by"] or "",
                    "pay_method": (g["pay_method"] or "").replace("_", " "), "created": g["created_at"],
                    "activated": g["activated_at"] or "",
                    "history": [{"cents": t["cents"], "amount": money(abs(t["cents"])), "note": t["note"] or "",
                                 "by": t["by_name"] or "", "at": t["created_at"]}
                                for t in db().execute("SELECT * FROM gift_txns WHERE gift_card_id=? ORDER BY id DESC",
                                                      (g["id"],)).fetchall()]})
    return out

def gift_move(g, cents, note, order_id=None, by=""):
    """Change a gift card's balance (negative spends it). Returns the new balance."""
    db().execute("INSERT INTO gift_txns (gift_card_id, order_id, cents, note, by_name, created_at) VALUES (?,?,?,?,?,?)",
                 (g["id"], order_id, int(cents), note[:160], by[:60], now()))
    nb = int(g["balance_cents"]) + int(cents)
    db().execute("UPDATE gift_cards SET balance_cents=?, status=? WHERE id=?",
                 (nb, ("void" if g["status"] == "void" else ("used" if nb <= 0 else "active")), g["id"]))
    return nb

def points_move(cid, pts, note, order_id=None, kind="adjust"):
    db().execute("INSERT INTO points_log (customer_id, order_id, points, note, created_at, kind) VALUES (?,?,?,?,?,?)",
                 (cid, order_id, int(pts), note[:160], now(), kind))
    db().execute("UPDATE customers SET points=MAX(0, points+?) WHERE id=?", (int(pts), cid))

def checkout_credits(payload, placed_by, dg, subtotal, total):
    """Work out the rewards account, gift card, rewards and saved card for a new order (nothing is spent yet)."""
    out = {"error": "", "customer_id": None, "gift_card_id": None, "gift_cents": 0, "reward_cents": 0,
           "reward_points": 0, "saved_card": None, "save_card": False}
    cust = None
    if placed_by == "customer":
        cust = current_customer()
    elif placed_by == "dispatch" and dg:
        cust = db().execute("SELECT * FROM customers WHERE phone=?", (phone_digits(dg),)).fetchone()
    if placed_by == "dispatch" and payload.get("save_customer") and len(phone_digits(dg)) == 10:
        _ver = 1 if is_owner() else 0
        if cust:
            if _ver:
                db().execute("UPDATE customers SET verified=1 WHERE id=?", (cust["id"],))
        else:
            cur = db().execute("""INSERT INTO customers (name, phone, address, verified, source, added_by, created_at)
                                  VALUES (?,?,?,""" + str(_ver) + """,'dispatch',?,?)""",
                               ((payload.get("customer_name") or "").strip()[:80], phone_digits(dg),
                                (payload.get("address") or "").strip()[:200], session.get("dispatcher_name") or "dispatch", now()))
            cust = db().execute("SELECT * FROM customers WHERE id=?", (cur.lastrowid,)).fetchone()
        db().commit()
    if cust:
        out["customer_id"] = cust["id"]
    left = int(total)
    # rewards first (only against food), then the gift card
    # one reward per order: the customer picks one option by its points (150, 250, 400...)
    try:
        want = int(payload.get("redeem_reward") or 0)
    except (TypeError, ValueError):
        want = 0
    if not want and payload.get("redeem_rewards"):
        _ok = [o for o in reward_options() if cust and int(cust["points"] or 0) >= o["points"]]
        want = _ok[-1]["points"] if _ok else -1
    if want:
        if not cust:
            return dict(out, error="Sign in to your rewards account to use a reward.")
        if not loyalty_on():
            return dict(out, error="Rewards are turned off right now.")
        opt = next((o for o in reward_options() if o["points"] == want), None)
        if not opt:
            return dict(out, error="Pick one of the rewards shown.")
        if int(cust["points"] or 0) < opt["points"]:
            return dict(out, error="You need " + str(opt["points"]) + " points for that reward. You have "
                        + str(int(cust["points"] or 0)) + ".")
        cents = min(opt["cents"], int(subtotal), left)
        out["reward_cents"], out["reward_points"] = cents, opt["points"]
        left -= cents
    if (payload.get("gift_code") or "").strip():
        g = gift_find(payload.get("gift_code"))
        if not g or g["status"] == "void":
            return dict(out, error="That gift card number was not found.")
        if int(g["balance_cents"]) <= 0:
            return dict(out, error="That gift card has no balance left.")
        out["gift_card_id"], out["gift_cents"] = g["id"], min(int(g["balance_cents"]), left)
        left -= out["gift_cents"]
    sid = payload.get("saved_card_id")
    if sid and left > 0:
        if not cust or placed_by != "customer":
            return dict(out, error="Sign in to use a card on file.")
        sc = db().execute("SELECT * FROM saved_cards WHERE id=? AND customer_id=?", (int(sid), cust["id"])).fetchone()
        if not sc:
            return dict(out, error="That saved card was not found. Pick another card.")
        _rr = db().execute("SELECT * FROM restaurants WHERE id=?", (payload.get("restaurant_id"),)).fetchone()
        _acct = pp_region_acct(_rv(_rr, "region_id") if _rr else None)
        if int(sc["pp_acct"] or 0) != _acct:
            return dict(out, error="That card is saved for another of our brands. Pick another card or pay with a new one.")
        if not pp_enabled(_acct):
            return dict(out, error="Cards on file are not available right now.")
        out["saved_card"] = sc
    out["save_card"] = bool(payload.get("save_card")) and bool(cust) and placed_by == "customer" and not out["saved_card"]
    return out

def confirm_needed(o):
    return ("confirm_state" in _okeys(o)) and (o["confirm_state"] or "") == "waiting"

def confirm_call_info(o):
    if not o or not confirm_needed(o):
        return None
    ph = dispatch_phone(o["region_id"] if "region_id" in _okeys(o) else None)
    if not ph:
        return None
    return {"phone": nice_phone(ph), "tel": tel_digits(ph)}

def _reg0(oid):
    try:
        r = db().execute("SELECT region_id FROM orders WHERE id=?", (oid,)).fetchone()
        return r["region_id"] if r else None
    except Exception:
        return None

def apply_checkout_credits(oid, code, cr, placed_by):
    """Spend the gift card / rewards on the new order, charge a saved card, and set the confirm call."""
    note = {"paid": False, "message": "", "gift": "", "rewards": "", "saved_card": ""}
    who = session.get("dispatcher_name") or ("customer" if placed_by == "customer" else placed_by)
    db().execute("""UPDATE orders SET customer_id=?, gift_card_id=?, gift_cents=?, reward_cents=?, reward_points=?,
                    save_card=? WHERE id=?""", (cr["customer_id"], cr["gift_card_id"], cr["gift_cents"],
                    cr["reward_cents"], cr["reward_points"], 1 if cr["save_card"] else 0, oid))
    if cr["gift_cents"]:
        g = db().execute("SELECT * FROM gift_cards WHERE id=?", (cr["gift_card_id"],)).fetchone()
        nb = gift_move(g, -cr["gift_cents"], "Order " + code, oid, who)
        note["gift"] = money(cr["gift_cents"]) + " from gift card (" + money(nb) + " left)"
    if cr["reward_points"]:
        points_move(cr["customer_id"], -cr["reward_points"], "Reward used on " + code, oid, "redeem")
        note["rewards"] = money(cr["reward_cents"]) + " reward"
    _o0 = db().execute("SELECT customer_phone, address FROM orders WHERE id=?", (oid,)).fetchone()
    if placed_by == "customer" and cr["customer_id"] and _o0:
        db().execute("UPDATE customers SET address=? WHERE id=?", (_o0["address"], cr["customer_id"]))
    if placed_by == "customer" and setting("confirm_call") and dispatch_phone(_reg0(oid)) and _o0 and customer_is_new(_o0["customer_phone"]):
        db().execute("UPDATE orders SET confirm_state='waiting', kitchen_go=0 WHERE id=?", (oid,))
        db().execute("UPDATE orders SET kitchen_status='waiting', kitchen_sent_at=NULL WHERE id=? AND kitchen_status='pending'", (oid,))
    db().commit()
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if (cr["gift_cents"] or cr["reward_cents"]) and due_cents(o) <= 0 and (o["payment_status"] or "") != "paid":
        mark_paid(o, "gift_card" if cr["gift_cents"] else "rewards", "Covered by " + " + ".join(
            x for x in (note["gift"] and "gift card", note["rewards"] and "rewards") if x), 0)
        note["paid"] = True
    elif cr["saved_card"]:
        ok, msg = pp_charge_saved(o, cr["saved_card"])
        note["paid"] = ok
        note["saved_card"] = msg
        if not ok:
            note["message"] = msg
    parts = [x for x in (note["gift"], note["rewards"]) if x]
    if parts:
        note["message"] = ("Applied " + " and ".join(parts) + ". " + note["message"]).strip()
    return note

def pp_vault_any(src, o=None):
    """Ask PayPal to save this payment for later charges on the same order (added fees, late tips)."""
    if src == "card":
        return {"card": {"attributes": {"vault": {"store_in_vault": "ON_SUCCESS"},
                                        "verification": {"method": "SCA_WHEN_REQUIRED"}}}}
    host = ""
    try:
        host = request.host_url.rstrip("/")
    except Exception:
        pass
    back = host + "/track/" + (o["code"] if o is not None else "")
    return {src: {"attributes": {"vault": {"store_in_vault": "ON_SUCCESS", "usage_type": "MERCHANT",
                                            "customer_type": "CONSUMER"}},
                  "experience_context": {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                                         "brand_name": (setting("business_name", str) or "Fleet Foot Delivery")[:120],
                                         "return_url": back, "cancel_url": back}}}

def pp_keep_order_vault(o, j):
    """Remember on the order the PayPal token for what the customer paid with (never a card number)."""
    try:
        ps = j.get("payment_source") or {}
        for src in ("card", "paypal", "venmo"):
            v = ((ps.get(src) or {}).get("attributes") or {}).get("vault") or {}
            if v.get("id"):
                db().execute("UPDATE orders SET pp_vault_id=?, pp_vault_src=? WHERE id=?", (v["id"], src, o["id"]))
                db().commit()
                return src
    except Exception as e:
        print("keep vault failed:", e)
    return None

def pp_vault_source(o):
    """Ask PayPal to keep the card on file when a signed-in customer ticks Save this card."""
    k = _okeys(o)
    if not ("save_card" in k and o["save_card"] and o["customer_id"]):
        return None
    attrs = {"vault": {"store_in_vault": "ON_SUCCESS"}, "verification": {"method": "SCA_WHEN_REQUIRED"}}
    c = db().execute("SELECT pp_customer_id FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
    if c and c["pp_customer_id"] and not pp_conf()["acct"]:   # PayPal's customer ID belongs to the main keys
        attrs["customer"] = {"id": c["pp_customer_id"]}
    return {"card": {"attributes": attrs}}

def pp_keep_vaulted(o, j, acct=0):
    """After PayPal approves a card the customer asked us to save, keep PayPal's token (never the number)."""
    try:
        card = (j.get("payment_source") or {}).get("card") or {}
        v = (card.get("attributes") or {}).get("vault") or {}
        if not (v.get("id") and o["customer_id"]):
            return None
        db().execute("""INSERT OR IGNORE INTO saved_cards (customer_id, vault_id, brand, last4, expiry, created_at, pp_acct)
                        VALUES (?,?,?,?,?,?,?)""", (o["customer_id"], v["id"], (card.get("brand") or "card").lower(),
                        card.get("last_digits") or "", card.get("expiry") or "", now(), int(acct or 0)))
        pc = (v.get("customer") or {}).get("id")
        if pc and not acct:
            db().execute("UPDATE customers SET pp_customer_id=? WHERE id=? AND pp_customer_id IS NULL", (pc, o["customer_id"]))
        db().commit()
        return {"brand": card.get("brand") or "Card", "last4": card.get("last_digits") or ""}
    except Exception:
        return None

def pp_charge_saved(o, sc):
    """Put the hold on a card the customer keeps on file (same hold-then-charge-after-delivery as checkout).
    A saved card only works with the PayPal keys it was saved under."""
    acct = int(sc["pp_acct"] or 0) if "pp_acct" in sc.keys() else 0
    with pp_for(acct):
        return _pp_charge_saved(o, sc, acct)

def _pp_charge_saved(o, sc, acct):
    cents = due_cents(o)
    st, j = pp_api("POST", "/v2/checkout/orders", {
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": o["code"], "custom_id": o["code"], "description": "Delivery order " + o["code"],
                            "amount": pp_money(cents)}],
        "payment_source": {"card": {"vault_id": sc["vault_id"]}}}, request_id="saved-" + o["code"])
    auth = (((j.get("purchase_units") or [{}])[0].get("payments") or {}).get("authorizations") or [{}])[0]
    if st not in (200, 201) or auth.get("status") not in ("CREATED", "PENDING") or not auth.get("id"):
        log("payment", o["code"] + " card on file declined: " + pp_err(j, "declined"))
        return False, "Your card on file didn't go through. Finish paying with another card on the next page."
    db().execute("""UPDATE orders SET pp_order_id=?, pp_auth_id=?, pp_auth_cents=?, pp_state='authorized',
                    pp_source='card', pp_error=NULL, pp_auth_at=?, pp_acct=?, pp_vault_id=?, pp_vault_src='card' WHERE id=?""",
                 (j.get("id"), auth["id"], cents, now(), acct, sc["vault_id"], o["id"]))
    db().commit()
    o = db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    mark_paid(o, "card_paypal", auth["id"], cents)
    return True, (sc["brand"] or "Card").title() + " ending " + (sc["last4"] or "") + " charged after delivery."

def order_points(o):
    """Points an online order earns: 1 per $1 of food (after the reward), plus the tier bonus on the bonus day."""
    rr = reward_rules()
    base = max(0, (int(o["subtotal_cents"] or 0) - int(o["reward_cents"] or 0)) // 100 * rr["per_dollar"])
    bonus, label = 0, ""
    try:
        when = dt.datetime.fromisoformat(str(o["created_at"])[:19])
    except Exception:
        when = dt.datetime.now()
    if base and rr["bonus_weekday"] >= 0 and when.weekday() == rr["bonus_weekday"]:
        t = tier_for(tier_points(o["customer_id"]))
        if t["bonus_pct"]:
            bonus = base * t["bonus_pct"] // 100
            label = rr["bonus_day"] + " " + t["name"] + " bonus (" + bonus_label(t["bonus_pct"]) + ")"
    return base, bonus, label

def credit_sweep():
    """Give points for delivered online orders; take them back and return gift card money and redeemed points
    on cancelled orders."""
    if loyalty_on():
        for o in db().execute("""SELECT * FROM orders WHERE dispatch_status='delivered' AND customer_id IS NOT NULL
                                 AND points_awarded IS NULL""").fetchall():
            if (o["placed_by"] or "customer") != "customer":
                # phone, dispatch and in-store orders don't earn points
                db().execute("UPDATE orders SET points_awarded=0 WHERE id=?", (o["id"],))
                continue
            base, bonus, label = order_points(o)
            db().execute("UPDATE orders SET points_awarded=? WHERE id=?", (base + bonus, o["id"]))
            if base > 0:
                points_move(o["customer_id"], base, "Earned on " + o["code"], o["id"], "earn")
            if bonus > 0:
                points_move(o["customer_id"], bonus, label + ", " + o["code"], o["id"], "bonus")
    for o in db().execute("""SELECT * FROM orders WHERE dispatch_status='cancelled' AND customer_id IS NOT NULL
                             AND COALESCE(points_awarded,0)>0 AND COALESCE(points_reversed,0)=0""").fetchall():
        points_move(o["customer_id"], -int(o["points_awarded"]), "Points taken back, " + o["code"] + " cancelled",
                    o["id"], "reverse")
        db().execute("UPDATE orders SET points_reversed=1 WHERE id=?", (o["id"],))
    for o in db().execute("""SELECT * FROM orders WHERE dispatch_status='cancelled' AND credits_settled=0
                             AND (gift_cents>0 OR reward_points>0)""").fetchall():
        if o["gift_cents"] and o["gift_card_id"]:
            g = db().execute("SELECT * FROM gift_cards WHERE id=?", (o["gift_card_id"],)).fetchone()
            if g:
                gift_move(g, o["gift_cents"], "Returned, " + o["code"] + " cancelled", o["id"], "system")
        if o["reward_points"] and o["customer_id"]:
            points_move(o["customer_id"], o["reward_points"], "Reward points returned, " + o["code"] + " cancelled",
                        o["id"], "return")
        db().execute("UPDATE orders SET credits_settled=1 WHERE id=?", (o["id"],))
    db().commit()

def give_signup_points(cid):
    rr = reward_rules()
    if not rr["on"] or rr["signup_points"] <= 0:
        return
    if db().execute("SELECT 1 FROM points_log WHERE customer_id=? AND kind='signup'", (cid,)).fetchone():
        return
    points_move(cid, rr["signup_points"], "Welcome bonus for joining", None, "signup")


# ---- customer account pages
@app.route("/account/login", methods=["GET", "POST"])
def account_login():
    err, mode = "", request.args.get("mode", "login")
    nxt = request.args.get("next") or request.form.get("next") or "/account"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/account"
    if request.method == "POST":
        mode = request.form.get("mode", "login")
        who = (request.form.get("who") or request.form.get("phone") or "").strip()
        ph = phone_digits(request.form.get("phone") if mode == "signup" else who)
        pw = request.form.get("password") or ""
        fails = session.get("cust_fails", 0)
        if fails >= 8:
            err = "Too many tries. Close the page and try again in a few minutes."
        elif mode != "signup" and "@" in who:
            c = find_customer(who, need_pw=True)
            if c and check_password_hash(c["pw_hash"], pw):
                session["customer_id"] = c["id"]
                session.pop("cust_fails", None)
                db().execute("UPDATE customers SET last_login_at=? WHERE id=?", (now(), c["id"]))
                db().commit()
                return redirect(nxt)
            session["cust_fails"] = fails + 1
            err = "That email and password don't match."
        elif len(ph) != 10:
            err = "Enter your 10 digit phone number or your email."
        elif mode == "signup":
            name = (request.form.get("name") or "").strip()[:80]
            raw_email = (request.form.get("email") or "").strip()
            email = clean_email(raw_email)
            if len(pw) < 6:
                err = "Pick a password with at least 6 characters."
            elif not name:
                err = "Enter your name."
            elif raw_email and not email:
                err = "That email doesn't look right."
            elif email and email_taken(email, (find_customer(ph) or {"id": 0})["id"]):
                err = "That email is already on another account. Sign in with it, or use a different email."
            elif db().execute("SELECT 1 FROM customers WHERE phone=? AND pw_hash IS NOT NULL", (ph,)).fetchone():
                err = "That phone number already has an account. Sign in instead."
                mode = "login"
            elif db().execute("SELECT 1 FROM customers WHERE phone=?", (ph,)).fetchone():
                # dispatch already has this customer on file: turn it into an online account
                c0 = db().execute("SELECT * FROM customers WHERE phone=?", (ph,)).fetchone()
                db().execute("""UPDATE customers SET name=COALESCE(NULLIF(name,''), ?), email=COALESCE(NULLIF(?,''), email),
                                pw_hash=?, last_login_at=? WHERE id=?""", (name, email, generate_password_hash(pw), now(), c0["id"]))
                give_signup_points(c0["id"])
                db().commit()
                session["customer_id"] = c0["id"]
                session.pop("cust_fails", None)
                return redirect(nxt)
            else:
                cur = db().execute("""INSERT INTO customers (name, phone, email, pw_hash, created_at, last_login_at)
                                      VALUES (?,?,?,?,?,?)""", (name, ph, email, generate_password_hash(pw), now(), now()))
                give_signup_points(cur.lastrowid)
                db().commit()
                session["customer_id"] = cur.lastrowid
                session.pop("cust_fails", None)
                return redirect(nxt)
        else:
            c = db().execute("SELECT * FROM customers WHERE phone=?", (ph,)).fetchone()
            if c and c["pw_hash"] and check_password_hash(c["pw_hash"], pw):
                session["customer_id"] = c["id"]
                session.pop("cust_fails", None)
                db().execute("UPDATE customers SET last_login_at=? WHERE id=?", (now(), c["id"]))
                db().commit()
                return redirect(nxt)
            session["cust_fails"] = fails + 1
            err = "That phone number and password don't match."
    return render_template("account_login.html", err=err, mode=mode, nxt=nxt, rules=reward_rules(),
                           reward_value=money(reward_rules()["value_cents"]))

@app.route("/account/logout")
def account_logout():
    session.pop("customer_id", None)
    return redirect("/")

@app.route("/account")
def account_page():
    c = current_customer()
    if not c:
        return redirect("/account/login?next=/account")
    credit_sweep()
    orders = db().execute("""SELECT o.id, o.code, o.created_at, o.total_cents, o.dispatch_status, o.placed_by,
                                    COALESCE(o.points_awarded,0) AS pts, r.name AS rname,
                                    (SELECT stars FROM reviews v WHERE v.order_id=o.id) AS stars
                             FROM orders o LEFT JOIN restaurants r ON r.id=o.restaurant_id
                             WHERE o.customer_id=? ORDER BY o.id DESC LIMIT 15""", (c["id"],)).fetchall()
    log_rows = db().execute("SELECT * FROM points_log WHERE customer_id=? ORDER BY id DESC LIMIT 25", (c["id"],)).fetchall()
    return render_template("account.html", me=customer_public(c), orders=orders, plog=log_rows, money=money,
                           rules=reward_rules(), msg=request.args.get("msg", ""))

@app.post("/account/review")
def account_review():
    """A customer reviews one of their delivered orders (once per order) and gets the review points."""
    c = current_customer()
    if not c:
        return redirect("/account/login?next=/account")
    try:
        oid, stars = int(request.form.get("order_id") or 0), int(request.form.get("stars") or 0)
    except ValueError:
        oid, stars = 0, 0
    comment = (request.form.get("comment") or "").strip()[:1000]
    o = db().execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (oid, c["id"])).fetchone()
    if not o or o["dispatch_status"] != "delivered":
        return redirect("/account?msg=You+can+review+delivered+orders+only.#orders")
    if not 1 <= stars <= 5:
        return redirect("/account?msg=Pick+1+to+5+stars.#orders")
    if db().execute("SELECT 1 FROM reviews WHERE order_id=?", (oid,)).fetchone():
        return redirect("/account?msg=You+already+reviewed+that+order.#orders")
    db().execute("INSERT INTO reviews (order_id, customer_id, restaurant_id, stars, comment, created_at) VALUES (?,?,?,?,?,?)",
                 (oid, c["id"], o["restaurant_id"], stars, comment, now()))
    rr = reward_rules()
    got = ""
    if rr["on"] and rr["review_points"] > 0 and (o["placed_by"] or "customer") == "customer":
        points_move(c["id"], rr["review_points"], "Review of " + o["code"], oid, "review")
        got = "+" + str(rr["review_points"]) + "+points."
    db().commit()
    return redirect("/account?msg=Thanks+for+the+review!+" + got + "#orders")

@app.get("/rewards")
def rewards_page():
    rr = reward_rules()
    return render_template("rewards.html", rules=rr, me=customer_public(current_customer()))

# ---------------------------------------------------------------- best shifts (statistics for drivers)
# For each region: which day and time of day pays drivers best, from the last few weeks of
# delivered orders (driver pay = trip pay sent, else the pay rule's suggestion) and the hours
# drivers were online then. Times are the region's own local time.
SHIFT_BLOCKS = [("Breakfast", 6, 11), ("Lunch", 11, 14), ("Afternoon", 14, 17), ("Dinner", 17, 21), ("Late night", 21, 26)]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_shift_cache = {}

def _hr12(h):
    h = h % 24
    return ("12" if h % 12 == 0 else str(h % 12)) + (" AM" if h < 12 else " PM")

def _shift_slot(t):
    """Local datetime -> (weekday, hour 6..29). Best shifts are worked out hour by hour;
    hours before 6 AM count as the night before (1 AM Saturday is Friday night, hour 25)."""
    h = t.hour
    if h < 6:
        t = t - dt.timedelta(days=1)
        h += 24
    return t.weekday(), h

def _hour_span(h):
    return "%s to %s" % (_hr12(h), _hr12(h + 1))

# Custom shift hours for Drivers needed by shift, per region (owner set). Hours run 6..30,
# where 24..30 are after midnight (2 AM = 26), the same way the hourly counts work.
def shift_blocks(rid=0):
    rid = stats_root(rid)
    try:
        r = db().execute("SELECT value FROM settings WHERE key=?", ("shift_blocks_%d" % int(rid or 0),)).fetchone()
        raw = json.loads(r["value"]) if r and r["value"] else None
    except Exception:
        raw = None
    if raw:
        try:
            out = [(str(x[0])[:30], int(x[1]), int(x[2])) for x in raw]
            if out and all(6 <= a < b <= 30 for _n, a, b in out):
                return out
        except Exception:
            pass
    return list(SHIFT_BLOCKS)

def shift_blocks_custom(rid=0):
    return shift_blocks(rid) != list(SHIFT_BLOCKS)

def _norm_block(name, start, end):
    """Clock hours (0-23) -> (name, a, b) on the 6..30 scale, or an error string."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:30]
    if not name:
        return "Give every shift a name."
    try:
        a, b = int(start), int(end)
    except (TypeError, ValueError):
        return "Pick a start and end time for " + name + "."
    if not (0 <= a <= 23 and 0 <= b <= 23):
        return "Pick a start and end time for " + name + "."
    if a < 6:
        a += 24
    if b < 6:
        b += 24
    if b <= a:
        b += 24
    if b > 30:
        return name + " can't run past 6 AM."
    return (name, a, b)

def shift_stats(rid, weeks=8):
    """Best shifts for one region (rid 0 = every order). Cached 10 minutes."""
    weeks = max(2, min(26, int(weeks or 8)))
    rid = stats_root(rid)
    grp = stats_group(rid)
    ck = (int(rid or 0), weeks, tuple(sorted(grp)))
    hit = _shift_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    con = db()
    nowdt = dt.datetime.now().replace(microsecond=0)
    start = (nowdt - dt.timedelta(days=7 * weeks)).isoformat(timespec="seconds")
    slots = {}
    def slot(wd, bi):
        return slots.setdefault((wd, bi), {"orders": 0, "pay": 0, "tips": 0, "dh": 0.0})
    q = """SELECT id, created_at, fee_cents, tip_cents, region_id FROM orders
           WHERE dispatch_status='delivered' AND created_at>=?"""
    args = [start]
    if rid:
        ph, gi = _in_ids(grp)
        q += " AND region_id IN " + ph; args += gi
    orders = con.execute(q, args).fetchall()
    paid = {}
    if orders:
        for r in con.execute("""SELECT order_id, SUM(cents) c FROM driver_payouts
                                WHERE created_at>=? AND COALESCE(kind,'trip')='trip'
                                  AND COALESCE(status,'') NOT IN ('FAILED','RETURNED','BLOCKED','REFUNDED','REVERSED','DENIED','ERROR','CANCELED')
                                GROUP BY order_id""", (start,)).fetchall():
            paid[r["order_id"]] = int(r["c"] or 0)
    for o in orders:
        try:
            t = dt.datetime.fromisoformat(str(o["created_at"])[:19])
        except ValueError:
            continue
        t = to_region(t, rid or o["region_id"])
        sl = slot(*_shift_slot(t))
        sl["orders"] += 1
        sl["pay"] += paid.get(o["id"], drv_pay_suggest(o))
        sl["tips"] += int(o["tip_cents"] or 0)
    # driver hours online, for drivers who work this region (no regions picked = all regions)
    who = None
    if rid:
        allr = {r["driver_id"] for r in con.execute("SELECT DISTINCT driver_id FROM driver_regions").fetchall()}
        ph, gi = _in_ids(grp)
        mine = {r["driver_id"] for r in con.execute("SELECT driver_id FROM driver_regions WHERE region_id IN " + ph, gi).fetchall()}
        who = lambda did: did in mine or did not in allr
    for a in con.execute("""SELECT person_id, started_at, COALESCE(ended_at, last_beat) e FROM active_time
                            WHERE kind='driver' AND state='online' AND COALESCE(ended_at, last_beat)>=?""", (start,)).fetchall():
        if who and not who(a["person_id"]):
            continue
        try:
            t0 = max(dt.datetime.fromisoformat(str(a["started_at"])[:19]), dt.datetime.fromisoformat(start))
            t1 = min(dt.datetime.fromisoformat(str(a["e"])[:19]), nowdt)
        except ValueError:
            continue
        if (t1 - t0).total_seconds() > 20 * 3600:
            t1 = t0 + dt.timedelta(hours=20)   # a session left open for days is not real driving time
        t0, t1 = to_region(t0, rid), to_region(t1, rid)
        cur = t0
        while cur < t1:
            nxt = min(t1, cur.replace(minute=0, second=0) + dt.timedelta(hours=1))
            slot(*_shift_slot(cur))["dh"] += (nxt - cur).total_seconds() / 3600.0
            cur = nxt
    rows = []
    for (wd, bi), v in slots.items():
        a, b = bi, bi + 1
        name = _hr12(a)
        blen = 1
        n = v["orders"]
        avg = v["pay"] / n if n else 0
        per_hr = v["pay"] / v["dh"] if v["dh"] >= 1 else None
        est = per_hr if per_hr is not None else (n / float(weeks * blen)) * avg
        rows.append({"day": WEEKDAYS[wd], "wd": wd, "block": name, "bi": bi,
                     "hours": "%s to %s" % (_hr12(a), _hr12(b)),
                     "orders": n, "per_shift": round(n / float(weeks), 1),
                     "avg_pay": money(int(avg)), "avg_tip": money(int(v["tips"] / n)) if n else money(0),
                     "per_hr_cents": int(est), "per_hr": money(int(est)),
                     "driver_hours": round(v["dh"], 1), "measured": per_hr is not None,
                     "enough": n >= 2})
    best = sorted([r for r in rows if r["enough"]], key=lambda r: -r["per_hr_cents"])
    out = {"region_id": int(rid or 0), "weeks": weeks, "orders": len(orders),
           "best": best[:6], "worst": list(reversed(best[-3:])) if len(best) > 6 else [],
           "grid": sorted(rows, key=lambda r: (r["wd"], r["bi"]))}
    _shift_cache[ck] = (time.time(), out)
    return out

def shift_summary(st, rname, n=4):
    if not st["best"]:
        return ""
    parts = ["%s %s: about %s/hr, %s orders a week" % (r["day"][:3], r["hours"], r["per_hr"], r["per_shift"])
             for r in st["best"][:n]]
    return "Best shifts in %s, last %d weeks: " % (rname, st["weeks"]) + "; ".join(parts) + "."

def _region_rows():
    rows = db().execute("SELECT id, name FROM regions ORDER BY sort, name").fetchall()
    return [(r["id"], r["name"]) for r in rows] or [(0, "All areas")]

def _stats_map():
    """Regions in order, and the region each one's statistics are counted under."""
    rows = db().execute("SELECT id, name, COALESCE(stats_with,0) sw FROM regions ORDER BY sort, name").fetchall()
    par = {r["id"]: int(r["sw"] or 0) for r in rows}
    root = {}
    for r in rows:
        cur, seen = r["id"], set()
        while True:
            seen.add(cur)
            nxt = par.get(cur, 0)
            if not nxt or nxt not in par or nxt in seen:
                break
            cur = nxt
        root[r["id"]] = cur
    return rows, root

def stats_root(rid):
    try:
        rid = int(rid or 0)
    except (TypeError, ValueError):
        return 0
    if not rid:
        return 0
    return _stats_map()[1].get(rid, rid)

def stats_group(rid):
    """Every region counted together with rid for statistics (Best shifts, Drivers needed)."""
    try:
        rid = int(rid or 0)
    except (TypeError, ValueError):
        return set()
    if not rid:
        return set()
    root = _stats_map()[1]
    rt = root.get(rid, rid)
    return {i for i, x in root.items() if x == rt} or {rid}

def stats_region_rows():
    """Region picks for the statistics pages: combined regions show once, as 'Auburn + Downtown Auburn'."""
    rows, root = _stats_map()
    names = {r["id"]: r["name"] for r in rows}
    out = []
    for r in rows:
        if root[r["id"]] != r["id"]:
            continue
        others = [names[i] for i in names if root[i] == r["id"] and i != r["id"]]
        out.append((r["id"], " + ".join([r["name"]] + others)))
    return out or [(0, "All areas")]

def _in_ids(ids):
    ids = sorted(int(i) for i in ids)
    return "(" + ",".join("?" * len(ids)) + ")", ids

def staff_rate():
    """Orders one driver can handle in an hour (setting sched_orders_per_hr, default 2)."""
    try:
        r = float(setting("sched_orders_per_hr") or 2)
    except (TypeError, ValueError):
        r = 2.0
    return max(0.5, min(10.0, r))

def _hour_key(hs, ws):
    """Hour start -> (day index in the week of ws, hour 6..29) or None. Hours before 6 AM
    belong to the night before, like the Best shifts blocks."""
    h, day = hs.hour, hs.date()
    if h < 6:
        h += 24
        day = day - dt.timedelta(days=1)
    idx = (day - ws).days
    return (idx, h) if 0 <= idx <= 6 else None

def staffing_plan(rid, ws, weeks=8):
    """Drivers each shift should have in a region (from past orders) against who is
    scheduled for the week starting ws."""
    weeks = max(2, min(26, int(weeks or 8)))
    rid = stats_root(rid)
    grp = stats_group(rid)
    rate = staff_rate()
    blocks_def = shift_blocks(rid)
    con = db()
    nowdt = dt.datetime.now().replace(microsecond=0)
    start = (nowdt - dt.timedelta(days=7 * weeks)).isoformat(timespec="seconds")
    q = "SELECT created_at, region_id FROM orders WHERE dispatch_status='delivered' AND created_at>=?"
    args = [start]
    if rid:
        ph, gi = _in_ids(grp)
        q += " AND region_id IN " + ph; args += gi
    hourly = {}
    for o in con.execute(q, args).fetchall():
        try:
            t = dt.datetime.fromisoformat(str(o["created_at"])[:19])
        except ValueError:
            continue
        t = to_region(t, rid or o["region_id"])
        wd, h = t.weekday(), t.hour
        if h < 6:
            wd, h = (wd - 1) % 7, h + 24
        hourly[(wd, h)] = hourly.get((wd, h), 0) + 1
    names = {}
    for d in con.execute("SELECT id, name, COALESCE(active,1) a FROM drivers").fetchall():
        if d["a"]:
            names[d["id"]] = d["name"]
    allr = {r["driver_id"] for r in con.execute("SELECT DISTINCT driver_id FROM driver_regions").fetchall()}
    if rid:
        ph, gi = _in_ids(grp)
        mine = {r["driver_id"] for r in con.execute("SELECT driver_id FROM driver_regions WHERE region_id IN " + ph, gi).fetchall()}
    else:
        mine = set()
    cover, pend = {}, {}
    for r in con.execute("""SELECT * FROM availability WHERE week_start=?
                            AND COALESCE(status,'approved') IN ('approved','pending')""", (ws.isoformat(),)).fetchall():
        did = r["driver_id"]
        if did not in names:
            continue
        if rid:
            sr = slot_regions(r)
            if sr:
                if not (set(sr) & grp):
                    continue
            elif not (did in mine or did not in allr):
                continue
        day = ws + dt.timedelta(days=int(r["dow"]))
        approved = (r["status"] or "approved") == "approved"
        if off_today(did, day.isoformat()):
            continue
        try:
            s0 = dt.datetime.combine(day, dt.time.fromisoformat(str(r["start_time"]).strip()))
            s1 = dt.datetime.combine(day, dt.time.fromisoformat(str(r["end_time"]).strip()))
        except ValueError:
            continue
        if s1 <= s0:
            s1 += dt.timedelta(days=1)
        hs = s0.replace(minute=0, second=0)
        while hs < s1:
            he = hs + dt.timedelta(hours=1)
            if (min(s1, he) - max(s0, hs)).total_seconds() >= 1800:
                k = _hour_key(hs, ws)
                if k:
                    (cover if approved else pend).setdefault(k, set()).add(did)
            hs = he
    days, short = [], []
    for wd in range(7):
        date = ws + dt.timedelta(days=wd)
        blocks = []
        for bi, (bname, a, b) in enumerate(blocks_def):
            hrs = []
            for h in range(a, b):
                avg = hourly.get((wd, h), 0) / float(weeks)
                need = int(math.ceil(avg / rate - 1e-9)) if avg >= 0.2 else 0
                hrs.append({"h": h, "avg": avg, "need": need, "have": cover.get((wd, h), set())})
            need = max(x["need"] for x in hrs)
            busy = [x for x in hrs if x["need"] > 0] or hrs
            have = min(len(x["have"]) for x in busy)
            have_max = max(len(x["have"]) for x in hrs)
            gap, gap_h = max((x["need"] - len(x["have"]), -x["h"]) for x in hrs)
            gap_h = -gap_h
            who = {i for x in hrs for i in x["have"]}
            waiting = {i for h in range(a, b) for i in pend.get((wd, h), set())} - who
            peak = max(hrs, key=lambda x: x["avg"])
            if gap > 0:
                status = "short"
            elif need == 0 and have_max == 0:
                status = "quiet"
            elif (need > 0 and have - need >= 2) or (need == 0 and have_max >= 2):
                status = "over"
            else:
                status = "ok"
            row = {"block": bname, "bi": bi, "hours": "%s to %s" % (_hr12(a), _hr12(b)),
                   "orders": round(sum(x["avg"] for x in hrs), 1),
                   "peak_hour": _hr12(peak["h"]) if peak["avg"] >= 0.2 else "",
                   "peak_orders": round(peak["avg"], 1),
                   "need": need, "have": have, "status": status,
                   "gap": max(0, gap), "gap_at": _hr12(gap_h) if gap > 0 else "",
                   "extra": max(0, (have - need) if need else have_max),
                   "drivers": sorted(names[i] for i in who),
                   "pending": sorted(names[i] for i in waiting)}
            blocks.append(row)
            if status == "short":
                short.append({"day": WEEKDAYS[wd], "date": date.isoformat(), "date_label": short_date(date),
                              "block": bname, "hours": row["hours"], "gap": row["gap"],
                              "gap_at": row["gap_at"], "need": need, "have": have,
                              "pending": len(row["pending"])})
        days.append({"day": WEEKDAYS[wd], "date": date.isoformat(), "date_label": short_date(date),
                     "blocks": blocks})
    short.sort(key=lambda x: (-x["gap"], x["date"]))
    return {"region_id": int(rid or 0), "week_start": ws.isoformat(), "weeks": weeks,
            "rate": rate, "days": days, "short": short,
            "blocks": [x[0] for x in blocks_def],
            "block_defs": [{"name": n_, "start": a_ % 24, "end": b_ % 24, "hours": "%s to %s" % (_hr12(a_), _hr12(b_))}
                           for n_, a_, b_ in blocks_def],
            "custom": blocks_def != list(SHIFT_BLOCKS),
            "has_history": bool(hourly)}

@app.get("/api/dispatch/staffing")
def api_dispatch_staffing():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    regs = stats_region_rows()
    scope = dispatcher_driver_scope()
    if scope is not None:
        regs = [r for r in regs if stats_group(r[0]) & set(scope)] or regs
    try:
        rid = int(request.args.get("region") or regs[0][0])
        weeks = int(request.args.get("weeks") or 8)
    except ValueError:
        rid, weeks = regs[0][0], 8
    rid = stats_root(rid) or rid
    if rid not in dict(regs):
        rid = regs[0][0]
    ws = parse_week(request.args.get("week"))
    out = staffing_plan(rid, ws, weeks)
    out.update({"ok": True, "regions": [{"id": i, "name": n} for i, n in regs],
                "region_name": dict(regs).get(rid, "All areas"),
                "owner": is_owner(), "share_driver": staffing_shared()})
    return jsonify(out)

def staffing_shared():
    try:
        r = db().execute("SELECT value FROM settings WHERE key='share_staffing_driver'").fetchone()
        return bool(r and str(r["value"]) == "1")
    except Exception:
        return False

def _set_kv(key, value):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

@app.post("/api/dispatch/shift-blocks")
def api_dispatch_shift_blocks():
    """Owner sets a region's shift hours for Drivers needed by shift (empty list = back to the standard shifts)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change shift hours."}), 403
    b = request.get_json(force=True) or {}
    rid = stats_root(b.get("region") or 0)
    rows = b.get("blocks") or []
    if not rows:
        db().execute("DELETE FROM settings WHERE key=?", ("shift_blocks_%d" % rid,))
        db().commit()
        log("shift_blocks", "Shift hours for region %d set back to standard by %s" % (rid, session.get("dispatcher_name") or "dispatch"))
        return jsonify({"ok": True})
    if len(rows) > 8:
        return jsonify({"ok": False, "error": "Use 8 shifts or fewer."}), 400
    out = []
    for x in rows:
        nb = _norm_block(x.get("name"), x.get("start"), x.get("end"))
        if isinstance(nb, str):
            return jsonify({"ok": False, "error": nb}), 400
        out.append(nb)
    out.sort(key=lambda x: x[1])
    _set_kv("shift_blocks_%d" % rid, json.dumps(out))
    db().commit()
    log("shift_blocks", "Shift hours for region %d set to %s by %s" % (rid, "; ".join("%s %s-%s" % (n, _hr12(a), _hr12(b_)) for n, a, b_ in out),
                                                                   session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True})

@app.post("/api/dispatch/staffing-share")
def api_dispatch_staffing_share():
    """Owner turns on or off showing Drivers needed by shift in the driver app."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change this."}), 403
    on = bool((request.get_json(force=True) or {}).get("on"))
    _set_kv("share_staffing_driver", "1" if on else "0")
    db().commit()
    log("shift_blocks", "Drivers needed by shift %s the driver app by %s" % ("shared with" if on else "hidden from", session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True, "on": on})

@app.get("/dispatch/shift-stats")
def dispatch_shift_stats():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    regs = stats_region_rows()
    try:
        rid = int(request.args.get("region") or regs[0][0])
        weeks = int(request.args.get("weeks") or 8)
    except ValueError:
        rid, weeks = regs[0][0], 8
    rid = stats_root(rid) or rid
    rname = dict(regs).get(rid, "All areas")
    st = shift_stats(rid, weeks)
    hours_seen = sorted({r["bi"] for r in st["grid"] if r["orders"]})
    return render_template("dispatch_shift_stats.html", regs=regs, rid=rid, rname=rname, st=st,
                           hours=[(h, _hour_span(h)) for h in hours_seen], days=WEEKDAYS, summary=shift_summary(st, rname),
                           sent=request.args.get("sent"))

@app.post("/dispatch/shift-stats/send")
def dispatch_shift_stats_send():
    """Message every active driver who works the region the best-shift summary (and text it when asked)."""
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    try:
        rid = int(request.form.get("region") or 0)
        weeks = int(request.form.get("weeks") or 8)
    except ValueError:
        rid, weeks = 0, 8
    rid = stats_root(rid) or rid
    grp = stats_group(rid)
    rname = dict(stats_region_rows()).get(rid, "All areas")
    body = shift_summary(shift_stats(rid, weeks), rname)
    if not body:
        return redirect(url_for("dispatch_shift_stats", region=rid, weeks=weeks, sent="none"))
    allr = {r["driver_id"] for r in db().execute("SELECT DISTINCT driver_id FROM driver_regions").fetchall()}
    n = 0
    for d in db().execute("SELECT * FROM drivers WHERE COALESCE(active,1)=1").fetchall():
        if rid and d["id"] in allr and not (set(driver_region_ids(d["id"])) & grp):
            continue
        db().execute("INSERT INTO messages(driver_id,sender,body,created_at) VALUES(?,?,?,?)", (d["id"], "system", body, now()))
        if request.form.get("text"):
            ph = phone_digits(d["phone"] or "")
            if len(ph) >= 10:
                send_text("+1" + ph[-10:], body)
        n += 1
    db().commit()
    log("shift_stats", "Sent best shifts for %s to %d drivers" % (rname, n))
    return redirect(url_for("dispatch_shift_stats", region=rid, weeks=weeks, sent=str(n)))

@app.get("/api/driver/shift-stats")
def api_driver_shift_stats():
    did = session.get("driver_id")
    if not did:
        return jsonify({"ok": False}), 403
    mine = driver_region_ids(did)
    out = []
    for rid, rname in stats_region_rows():
        if rid and mine and not (stats_group(rid) & set(mine)):
            continue
        st = shift_stats(rid, 8)
        g_ = {"region": rname, "best": st["best"][:5], "worst": st["worst"], "orders": st["orders"], "weeks": st["weeks"]}
        if staffing_shared():
            # Drivers needed by shift, without anyone's names: just where more drivers are needed
            sp = staffing_plan(rid, open_week_start(), 8)
            g_["needed"] = {"week_label": "%s to %s" % (sp["days"][0]["date_label"], sp["days"][-1]["date_label"]),
                            "has_history": sp["has_history"],
                            "short": [{"day": x["day"], "date_label": x["date_label"], "block": x["block"], "hours": x["hours"],
                                       "gap": x["gap"], "gap_at": x["gap_at"]} for x in sp["short"]],
                            "days": [{"day": d["day"], "date_label": d["date_label"],
                                      "blocks": [{"block": k["block"], "hours": k["hours"], "need": k["need"],
                                                  "have": k["have"], "status": k["status"]} for k in d["blocks"]]}
                                     for d in sp["days"]]}
        out.append(g_)
    return jsonify({"ok": True, "regions": out, "share_driver": staffing_shared()})

@app.get("/dispatch/reviews")
def dispatch_reviews():
    if not dispatcher_required():
        return redirect(url_for("dispatch_login"))
    rows = db().execute("""SELECT v.*, o.code, r.name AS rname, c.name AS cname, c.phone AS cphone
                           FROM reviews v LEFT JOIN orders o ON o.id=v.order_id
                           LEFT JOIN restaurants r ON r.id=v.restaurant_id LEFT JOIN customers c ON c.id=v.customer_id
                           ORDER BY v.id DESC LIMIT 300""").fetchall()
    return render_template("dispatch_reviews.html", rows=rows, nice_phone=nice_phone)

@app.get("/api/account/me")
def api_account_me():
    return jsonify({"ok": True, "me": customer_public(current_customer()), "rules": reward_rules(),
                    "reward_value": money(reward_rules()["value_cents"]), "confirm_call": bool(setting("confirm_call")),
                    "cards_on": pp_enabled()})

@app.post("/api/account/card-delete")
def api_account_card_delete():
    c = current_customer()
    if not c:
        return jsonify({"ok": False, "error": "Sign in first."}), 403
    sc = db().execute("SELECT * FROM saved_cards WHERE id=? AND customer_id=?",
                      (int((request.get_json(force=True) or {}).get("id") or 0), c["id"])).fetchone()
    if not sc:
        return jsonify({"ok": False, "error": "Card not found."}), 404
    try:
        with pp_for(int(sc["pp_acct"] or 0)):
            pp_api("DELETE", "/v3/vault/payment-tokens/" + sc["vault_id"])
    except Exception:
        pass
    db().execute("DELETE FROM saved_cards WHERE id=?", (sc["id"],))
    db().commit()
    return jsonify({"ok": True, "me": customer_public(c)})

@app.post("/api/account/update")
def api_account_update():
    c = current_customer()
    if not c:
        return jsonify({"ok": False, "error": "Sign in first."}), 403
    b = request.get_json(force=True) or {}
    name = (b.get("name") or c["name"] or "").strip()[:80]
    raw_email = (b.get("email") or "").strip()
    email = clean_email(raw_email)
    if raw_email and not email:
        return jsonify({"ok": False, "error": "That email doesn't look right."}), 400
    if email_taken(email, c["id"]):
        return jsonify({"ok": False, "error": "That email is already on another account."}), 400
    if b.get("new_password"):
        if not check_password_hash(c["pw_hash"], b.get("password") or ""):
            return jsonify({"ok": False, "error": "Your current password is wrong."}), 400
        if len(b["new_password"]) < 6:
            return jsonify({"ok": False, "error": "Pick a password with at least 6 characters."}), 400
        db().execute("UPDATE customers SET pw_hash=? WHERE id=?", (generate_password_hash(b["new_password"]), c["id"]))
    db().execute("UPDATE customers SET name=?, email=? WHERE id=?", (name, email, c["id"]))
    db().commit()
    return jsonify({"ok": True})


# ---- gift cards, website
def gift_limits():
    return max(100, setting("gift_min_cents") or 1000), max(500, setting("gift_max_cents") or 50000)

@app.route("/gift-cards")
def gift_cards_page():
    lo, hi = gift_limits()
    return render_template("gift.html", lo=lo // 100, hi=hi // 100, pp_on=pp_enabled(),
                           pp_client=pp_client_id(), phone=nice_phone(dispatch_phone()),
                           me=customer_public(current_customer()))

@app.post("/api/gift/start")
def api_gift_start():
    b = request.get_json(force=True) or {}
    lo, hi = gift_limits()
    try:
        cents = int(round(float(b.get("amount") or 0) * 100))
    except Exception:
        cents = 0
    if cents < lo or cents > hi:
        return jsonify({"ok": False, "error": "Pick an amount from " + money(lo) + " to " + money(hi) + "."}), 400
    name = (b.get("buyer_name") or "").strip()[:80]
    ph = phone_digits(b.get("buyer_phone"))
    if not name or len(ph) != 10:
        return jsonify({"ok": False, "error": "Enter your name and 10 digit phone number."}), 400
    if not pp_enabled():
        return jsonify({"ok": False, "error": "Online gift card sales are off right now. Call dispatch to buy one."}), 400
    ref = secrets.token_urlsafe(10)
    c = current_customer()
    db().execute("""INSERT INTO gift_cards (code, ref, initial_cents, balance_cents, buyer_name, buyer_phone, buyer_email,
                    to_name, message, status, sold_by, customer_id, created_at) VALUES (?,?,?,0,?,?,?,?,?,'pending','website',?,?)""",
                 ("PENDING-" + ref, ref, cents, name, ph, (b.get("buyer_email") or "").strip()[:120],
                  (b.get("to_name") or "").strip()[:80], (b.get("message") or "").strip()[:240], c["id"] if c else None, now()))
    db().commit()
    return jsonify({"ok": True, "ref": ref, "amount": money(cents)})

def _gift_by_ref(ref):
    return db().execute("SELECT * FROM gift_cards WHERE ref=?", ((ref or "").strip(),)).fetchone()

def gift_activate(g, method, pay_ref, by):
    code = gift_new_code()
    db().execute("""UPDATE gift_cards SET code=?, status='active', balance_cents=0, pay_method=?, pay_ref=?,
                    activated_at=?, sold_by=COALESCE(NULLIF(?,''), sold_by) WHERE id=?""",
                 (code, method, pay_ref, now(), by, g["id"]))
    g = db().execute("SELECT * FROM gift_cards WHERE id=?", (g["id"],)).fetchone()
    gift_move(g, g["initial_cents"], "Bought (" + method.replace("_", " ") + ")", None, by or "website")
    db().commit()
    log("payment", "Gift card " + code[-4:] + " sold, " + money(g["initial_cents"]) + " (" + method.replace("_", " ") + ")")
    return db().execute("SELECT * FROM gift_cards WHERE id=?", (g["id"],)).fetchone()

@app.post("/api/gift/pp-create")
def api_gift_pp_create():
    g = _gift_by_ref((request.get_json(force=True) or {}).get("ref"))
    if not g or g["status"] != "pending":
        return jsonify({"ok": False, "error": "That gift card is already paid or was not found."}), 400
    acct = pp_conf()["acct"]
    with pp_for(acct):
        st, j = _gift_pp_order(g)
    if st not in (200, 201) or not j.get("id"):
        return jsonify({"ok": False, "error": pp_err(j, "PayPal could not start the payment.")}), 400
    db().execute("UPDATE gift_cards SET pp_order_id=?, pp_acct=? WHERE id=?", (j["id"], acct, g["id"]))
    db().commit()
    return jsonify({"ok": True, "id": j["id"]})

def _gift_pp_order(g):
    return pp_api("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": "GIFT-" + g["ref"], "custom_id": "GIFT-" + g["ref"],
                            "description": "Gift card " + money(g["initial_cents"]), "amount": pp_money(g["initial_cents"])}],
        "application_context": {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW",
                                "brand_name": (setting("business_name", str) or "Fleet Foot Delivery")[:120]}})

@app.post("/api/gift/pp-approve")
def api_gift_pp_approve():
    b = request.get_json(force=True) or {}
    g = _gift_by_ref(b.get("ref"))
    if not g:
        return jsonify({"ok": False, "error": "Gift card not found."}), 404
    if g["status"] != "pending":
        return jsonify({"ok": True, "card": gift_public(g, True)})
    if (b.get("id") or "") != (g["pp_order_id"] or ""):
        return jsonify({"ok": False, "error": "That payment is for something else."}), 400
    with pp_for(int(g["pp_acct"] or 0) if "pp_acct" in g.keys() else 0):
        st, j = pp_api("POST", "/v2/checkout/orders/" + g["pp_order_id"] + "/capture", request_id="gift-" + g["pp_order_id"])
    cap = (((j.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
    if st not in (200, 201) or cap.get("status") not in ("COMPLETED", "PENDING"):
        return jsonify({"ok": False, "error": pp_err(j, "The payment did not go through.")}), 400
    src = next(iter(j.get("payment_source") or {"paypal": 1}))
    by_disp = session.get("dispatcher_name") if session.get("dispatcher_id") else ""
    g = gift_activate(g, ("paypal_terminal" if by_disp else "card_paypal") if src == "card" else src,
                      cap.get("id", ""), by_disp or "website")
    return jsonify({"ok": True, "card": gift_public(g, True)})

@app.route("/gift/<ref>")
def gift_receipt(ref):
    g = _gift_by_ref(ref)
    if not g or g["status"] == "pending":
        return redirect("/gift-cards")
    return render_template("gift_card.html", g=gift_public(g, True), phone=nice_phone(dispatch_phone()))

@app.post("/api/gift/balance")
def api_gift_balance():
    n = session.get("gift_checks", [])
    n = [t for t in n if time.time() - t < 3600]
    if len(n) >= 15:
        return jsonify({"ok": False, "error": "Too many checks. Try again in an hour."}), 429
    n.append(time.time())
    session["gift_checks"] = n
    g = gift_find((request.get_json(force=True) or {}).get("code"))
    if not g or g["status"] == "void":
        return jsonify({"ok": False, "error": "That gift card number was not found."}), 404
    return jsonify({"ok": True, "card": gift_public(g)})


# ---- gift cards and rewards, dispatch
@app.route("/dispatch/gift-cards")
def dispatch_gift_cards():
    if not dispatcher_required():
        return redirect("/dispatch/login")
    lo, hi = gift_limits()
    rows = db().execute("SELECT * FROM gift_cards WHERE status!='pending' ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("dispatch_gifts.html", cards=[gift_public(g, True) for g in rows], lo=lo // 100, hi=hi // 100,
                           owner=is_owner(), pp_on=pp_enabled(), pp_client=pp_client_id())

@app.post("/api/dispatch/gift/sell")
def api_dispatch_gift_sell():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    lo, hi = gift_limits()
    try:
        cents = int(round(float(b.get("amount") or 0) * 100))
    except Exception:
        cents = 0
    if cents < lo or cents > hi:
        return jsonify({"ok": False, "error": "Pick an amount from " + money(lo) + " to " + money(hi) + "."}), 400
    method = b.get("method") or ""
    if method not in ("cash", "card_terminal", "house_account", "comp", "link", "paypal_terminal"):
        return jsonify({"ok": False, "error": "Pick how they paid."}), 400
    if method == "cash" and not cash_allowed():
        return jsonify({"ok": False, "error": "Cash is turned off in Settings."}), 400
    if method == "comp" and not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can give a free gift card."}), 403
    if method == "link" and not pp_enabled():
        return jsonify({"ok": False, "error": "PayPal is not set up, so a pay link can't be sent."}), 400
    if method == "paypal_terminal" and not pp_enabled():
        return jsonify({"ok": False, "error": "PayPal is not set up yet, so cards can't be typed in here."}), 400
    ph = phone_digits(b.get("buyer_phone"))
    name = (b.get("buyer_name") or "").strip()[:80]
    if not name or len(ph) != 10:
        return jsonify({"ok": False, "error": "Enter the buyer's name and 10 digit phone number."}), 400
    ref = secrets.token_urlsafe(10)
    who = session.get("dispatcher_name") or "dispatch"
    cust = db().execute("SELECT id FROM customers WHERE phone=?", (ph,)).fetchone()
    db().execute("""INSERT INTO gift_cards (code, ref, initial_cents, balance_cents, buyer_name, buyer_phone, buyer_email,
                    to_name, message, status, sold_by, customer_id, created_at) VALUES (?,?,?,0,?,?,?,?,?,'pending',?,?,?)""",
                 ("PENDING-" + ref, ref, cents, name, ph, (b.get("buyer_email") or "").strip()[:120],
                  (b.get("to_name") or "").strip()[:80], (b.get("message") or "").strip()[:240], who,
                  cust["id"] if cust else None, now()))
    db().commit()
    g = _gift_by_ref(ref)
    if method == "paypal_terminal":
        # dispatch types the buyer's card into PayPal's card form on this page; nothing is saved here
        return jsonify({"ok": True, "pending": True, "terminal": True, "ref": ref, "amount": money(cents)})
    if method == "link":
        return jsonify({"ok": True, "pending": True, "pay_link": request.host_url.rstrip("/") + "/gift-cards?pay=" + ref,
                        "message": "Send this link to the buyer. The card number shows once they pay."})
    g = gift_activate(g, method, (b.get("pay_ref") or "").strip()[:80], who)
    return jsonify({"ok": True, "card": gift_public(g, True),
                    "receipt": request.host_url.rstrip("/") + "/gift/" + ref})

@app.post("/api/dispatch/gift/lookup")
def api_dispatch_gift_lookup():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    g = gift_find((request.get_json(force=True) or {}).get("code"))
    if not g:
        return jsonify({"ok": False, "error": "That gift card number was not found."}), 404
    return jsonify({"ok": True, "card": gift_public(g, True)})

@app.post("/api/dispatch/gift/adjust")
def api_dispatch_gift_adjust():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can change a gift card balance."}), 403
    b = request.get_json(force=True) or {}
    g = gift_find(b.get("code"))
    if not g:
        return jsonify({"ok": False, "error": "That gift card number was not found."}), 404
    who = session.get("dispatcher_name") or "owner"
    if b.get("void"):
        if int(g["balance_cents"]) > 0:
            gift_move(g, -int(g["balance_cents"]), "Voided", None, who)
        db().execute("UPDATE gift_cards SET status='void' WHERE id=?", (g["id"],))
    else:
        try:
            cents = int(round(float(b.get("amount") or 0) * 100))
        except Exception:
            cents = 0
        if not cents or abs(cents) > 50000 or int(g["balance_cents"]) + cents < 0:
            return jsonify({"ok": False, "error": "Enter an amount (use a minus sign to take money off)."}), 400
        gift_move(g, cents, (b.get("note") or "Owner adjustment").strip()[:120], None, who)
    db().commit()
    return jsonify({"ok": True, "card": gift_public(gift_find(b.get("code")), True)})

@app.get("/api/dispatch/customer-lookup")
def api_dispatch_customer_lookup():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    ph = phone_digits(request.args.get("phone"))
    c = db().execute("SELECT * FROM customers WHERE phone=?", (ph,)).fetchone() if len(ph) == 10 else None
    if not c:
        return jsonify({"ok": True, "found": False})
    me = customer_public(c)
    me.pop("cards", None)
    return jsonify({"ok": True, "found": True, "customer": me})

@app.get("/api/dispatch/customer-search")
def api_dispatch_customer_search():
    """Type-ahead for the new-order form: customers on the list plus anyone who has ordered before."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    q = (request.args.get("q") or "").strip()[:80]
    dq = phone_digits(q)
    if len(q) < 2 and len(dq) < 3:
        return jsonify({"ok": True, "results": []})
    out, seen = [], set()
    like = "%" + q + "%"
    rows = db().execute("""SELECT * FROM customers WHERE name LIKE ? OR (? != '' AND phone LIKE ?) OR address LIKE ?
                           ORDER BY name LIMIT 15""", (like, dq if len(dq) >= 3 else "", "%" + dq + "%", like)).fetchall()
    for c in rows:
        ph = phone_digits(c["phone"])
        if not ph or ph in seen:
            continue
        seen.add(ph)
        last = next((o for o in db().execute("SELECT address, address_note, customer_phone, created_at FROM orders "
                                          "WHERE customer_phone LIKE ? ORDER BY id DESC LIMIT 20", ("%" + ph[-4:],)).fetchall()
                  if phone_digits(o["customer_phone"]) == ph), None)
        out.append({"name": c["name"] or "", "phone": nice_phone(ph), "phone_digits": ph,
                    "address": (c["address"] or (last["address"] if last else "") or ""),
                    "note": (last["address_note"] if last else "") or "",
                    "existing": bool(c["verified"]) or not customer_is_new(ph), "on_list": True,
                    "last_order": ((last["created_at"] or "")[:10] if last else "")})
    if len(out) < 10:
        orows = db().execute("""SELECT customer_name, customer_phone, address, address_note, created_at FROM orders
                                WHERE customer_name LIKE ? OR (? != '' AND customer_phone LIKE ?) OR address LIKE ?
                                ORDER BY id DESC LIMIT 300""",
                             (like, dq if len(dq) >= 3 else "", "%" + dq + "%", like)).fetchall()
        for o in orows:
            ph = phone_digits(o["customer_phone"])
            if not ph or ph in seen:
                continue
            if len(dq) >= 3 and dq not in ph and q.lower() not in (o["customer_name"] or "").lower() \
                    and q.lower() not in (o["address"] or "").lower():
                continue
            seen.add(ph)
            out.append({"name": o["customer_name"] or "", "phone": nice_phone(ph), "phone_digits": ph,
                        "address": o["address"] or "", "note": o["address_note"] or "",
                        "existing": not customer_is_new(ph), "on_list": False,
                        "last_order": (o["created_at"] or "")[:10]})
            if len(out) >= 10:
                break
    return jsonify({"ok": True, "results": out[:10]})

@app.post("/api/dispatch/confirm-call")
def api_dispatch_confirm_call():
    """The customer called in to confirm their online order: mark it confirmed (Send to kitchen still releases it)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    o = db().execute("SELECT * FROM orders WHERE id=?", ((request.get_json(force=True) or {}).get("order_id"),)).fetchone()
    if not o:
        return jsonify({"ok": False}), 404
    db().execute("UPDATE orders SET confirm_state='confirmed' WHERE id=?", (o["id"],))
    if is_owner():
        db().execute("UPDATE customers SET verified=1 WHERE phone=?", (phone_digits(o["customer_phone"]),))
    db().commit()
    log("order", o["code"] + " confirmed by phone with " + (session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True})



# ---------------------------------------------------------------- new vs existing customers, customer list
def customer_is_new(phone):
    """A customer is existing once dispatch marks them existing or they have had an order delivered."""
    ph = phone_digits(phone)
    if len(ph) != 10:
        return True
    c = db().execute("SELECT verified FROM customers WHERE phone=?", (ph,)).fetchone()
    if c and c["verified"]:
        return False
    for o in db().execute("SELECT customer_phone FROM orders WHERE dispatch_status='delivered' AND customer_phone LIKE ?",
                          ("%" + ph[-4:],)).fetchall():
        if phone_digits(o["customer_phone"]) == ph:
            return False
    return True

def customer_row_public(c):
    n = db().execute("SELECT COUNT(*) FROM orders WHERE customer_id=?", (c["id"],)).fetchone()[0]
    return {"id": c["id"], "name": c["name"] or "", "phone": nice_phone(c["phone"]), "phone_digits": c["phone"] or "",
            "email": c["email"] or "", "address": c["address"] or "", "notes": c["notes"] or "",
            "existing": bool(c["verified"]) or not customer_is_new(c["phone"]), "marked_existing": bool(c["verified"]),
            "online": bool(c["pw_hash"]), "points": int(c["points"] or 0), "orders": n,
            "added": (c["created_at"] or "")[:10], "added_by": c["added_by"] or ("website" if c["pw_hash"] else "")}

@app.route("/dispatch/customers")
def dispatch_customers():
    if not dispatcher_required():
        return redirect("/dispatch/login")
    return render_template("dispatch_customers.html", owner=is_owner())

@app.get("/api/dispatch/customers")
def api_dispatch_customers():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    q = (request.args.get("q") or "").strip()
    if q:
        dq = phone_digits(q)
        rows = db().execute("""SELECT * FROM customers WHERE name LIKE ? OR (? != '' AND phone LIKE ?) OR address LIKE ?
                               ORDER BY name LIMIT 200""", ("%" + q + "%", dq, "%" + dq + "%", "%" + q + "%")).fetchall()
    else:
        rows = db().execute("SELECT * FROM customers ORDER BY id DESC LIMIT 200").fetchall()
    total = db().execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    return jsonify({"ok": True, "total": total, "customers": [customer_row_public(c) for c in rows]})

@app.post("/api/dispatch/customer-save")
def api_dispatch_customer_save():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    ph = phone_digits(b.get("phone"))
    name = (b.get("name") or "").strip()[:80]
    if len(ph) != 10:
        return jsonify({"ok": False, "error": "Enter a 10 digit phone number."}), 400
    if not name:
        return jsonify({"ok": False, "error": "Enter the customer's name."}), 400
    cid = b.get("id")
    other = db().execute("SELECT * FROM customers WHERE phone=?", (ph,)).fetchone()
    _cur = db().execute("SELECT verified FROM customers WHERE id=?", (int(cid),)).fetchone() if cid else other
    if is_owner():
        _ver = 1 if b.get("existing", True) else 0
    else:
        _ver = int(_cur["verified"]) if _cur else 0      # unchanged: they become existing after a delivered order
    vals = (name, ph, (b.get("email") or "").strip()[:120], (b.get("address") or "").strip()[:200],
            (b.get("notes") or "").strip()[:300], _ver)
    if cid:
        c = db().execute("SELECT * FROM customers WHERE id=?", (int(cid),)).fetchone()
        if not c:
            return jsonify({"ok": False, "error": "Customer not found."}), 404
        if other and other["id"] != c["id"]:
            return jsonify({"ok": False, "error": "Another customer already has that phone number."}), 400
        db().execute("UPDATE customers SET name=?, phone=?, email=?, address=?, notes=?, verified=? WHERE id=?", vals + (c["id"],))
        msg = "Saved."
    elif other:
        db().execute("UPDATE customers SET name=?, phone=?, email=?, address=?, notes=?, verified=? WHERE id=?", vals + (other["id"],))
        cid, msg = other["id"], "That number was already on file, so it was updated."
    else:
        cur = db().execute("""INSERT INTO customers (name, phone, email, address, notes, verified, source, added_by, created_at)
                              VALUES (?,?,?,?,?,?,'dispatch',?,?)""", vals + (session.get("dispatcher_name") or "dispatch", now()))
        cid, msg = cur.lastrowid, "Added."
    db().commit()
    log("customer", name + " saved to the customer list" + (" as existing" if vals[-1] else "") + " by " +
        (session.get("dispatcher_name") or "dispatch"))
    c = db().execute("SELECT * FROM customers WHERE id=?", (int(cid),)).fetchone()
    if not is_owner() and b.get("existing") and customer_is_new(ph):
        msg += " They become an existing customer after their first delivered order (only an owner can mark them existing sooner)."
    return jsonify({"ok": True, "message": msg, "customer": customer_row_public(c)})


@app.post("/api/dispatch/customer-delete")
def api_dispatch_customer_delete():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can delete a customer."}), 403
    b = request.get_json(force=True) or {}
    try:
        cid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    c = db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not c:
        return jsonify({"ok": False, "error": "Customer not found."}), 404
    live = db().execute("""SELECT code FROM orders WHERE customer_id=? AND dispatch_status NOT IN ('delivered','cancelled')""",
                        (cid,)).fetchall()
    if live:
        return jsonify({"ok": False, "error": c["name"] + " has an open order (" + ", ".join(r["code"] for r in live) +
                        "). Finish or cancel it first."}), 400
    # Past orders stay in the records; they just stop pointing at this customer.
    db().execute("UPDATE orders SET customer_id=NULL WHERE customer_id=?", (cid,))
    db().execute("UPDATE gift_cards SET customer_id=NULL WHERE customer_id=?", (cid,))
    db().execute("DELETE FROM saved_cards WHERE customer_id=?", (cid,))
    db().execute("DELETE FROM points_log WHERE customer_id=?", (cid,))
    db().execute("DELETE FROM customers WHERE id=?", (cid,))
    db().commit()
    log("customer", (c["name"] or "A customer") + " (" + nice_phone(c["phone"]) + ") deleted from the customer list by " +
        (session.get("dispatcher_name") or "the owner"))
    return jsonify({"ok": True, "message": (c["name"] or "Customer") + " deleted."})


# ---------------------------------------------------------------- customer website: home search, FAQ, apply to drive / partner
DEFAULT_FAQ = """Q: How do I place an order?
A: {business} is a marketing and technology company bringing customers and restaurants closer together. You can place an order now by picking one of the open restaurants on the home page, or call {phone} and one of our customer service representatives will help you place your order.

Q: How long does it take for delivery?
A: The average delivery time depends on how busy it is and the time of day, but it is usually 30 minutes to an hour for dinner deliveries. Avoid delays by scheduling your delivery ahead of time: choose Schedule for later when you check out. This is great for special occasions or surprise dinners.

Q: What are my payment options?
A: Credit or debit card, PayPal, cash, or a {business} gift card. Businesses can ask us about a house account. If you are paying with cash and have a bill larger than $20.00, you must either leave a note in your online order or tell us over the phone. We accept Visa, Mastercard, American Express and Discover.

Q: Can I order from multiple restaurants?
A: Because of our wide delivery range and number of restaurants, we no longer allow multiple restaurants on the same order. You can still place two separate orders from two restaurants. The order minimum and delivery fee apply to each order. You can leave us a comment after placing your first order letting us know it is part of a multiple order; this helps us coordinate your delivery. Your separate orders may be delivered by separate drivers, so we recommend splitting the tip or treating each order as though a different driver will handle it.

Q: Can I cancel my order?
A: Orders cannot be canceled once they have been placed with the restaurant. If you need to cancel or change an advance order, call {phone} and a manager will help you.

Q: I have a problem with my order, what should I do?
A: If there is any problem with your order, you must call us within 15 minutes of the order being delivered. We will confirm the mistake with the restaurant. Once we verify with the restaurant that the problem was on their end, we will either have your self-employed delivery professional re-deliver the missing or incorrect item or refund you the amount. We do not issue refunds for orders on our own. If you have an issue with your food, we will be happy to speak with the restaurant to help get you a refund. We are contractually obligated not to refund any orders without consent from the restaurant.
"""

def faq_region_list():
    """Regions with their brand's name, for 'put these FAQs on' pickers."""
    out = []
    try:
        names = {r["id"]: r["name"] for r in db().execute("SELECT id, name FROM sites").fetchall()}
        for g in all_regions():
            reg = _region(g["id"])
            out.append({"id": g["id"], "name": g["name"], "brand": names.get((_rv(reg, "site_id") or 0), ""),
                        "own": bool((_rv(reg, "faq_text") or "").strip())})
    except Exception:
        pass
    return out

def _brand_is_main(site):
    """True when this brand is the business itself (its name is in the main name or email),
    so it may show the main email and address."""
    n = _norm_name(site["name"] if site is not None else "")
    if not n:
        return False
    def _biz(key):
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return (row["value"] if row else "") or ""
    return n in _norm_name(_biz("business_name")) or n in _norm_name(_biz("business_email").split("@")[0])

def brand_contact(site=None):
    """Email and address a brand's pages show: the brand's own from its design. A brand without its
    own never borrows another brand's (Bulldawg must not show Tiger Town's email); only the brand
    that is the business itself falls back to the main ones. None = no brand on this page."""
    if site is None:
        site = getattr(g, "_site_forced", None) or current_site()
    if site is None:
        return None
    d = site_design(site)
    em = str(d.get("business_email") or "").strip()
    ad = str(d.get("business_address") or "").strip()
    if (not em or not ad) and _brand_is_main(site):
        def _biz(key):
            row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return ((row["value"] if row else "") or "").strip()
        em = em or _biz("business_email")
        ad = ad or _biz("business_address")
    return {"email": em, "address": ad}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

def faq_target():
    """(region id, brand site) whose FAQ the customer sees. The region must belong to the brand
    of this web address; a picked brand with no area uses that brand. (0, None) = the business FAQ."""
    rid, site = 0, None
    try:
        hs = host_site()
        pick = request.args.get("region")
        if pick is None:
            pick = session.get("cust_region") or ""
        if str(pick).isdigit() and _region(int(pick)) is not None:
            rid = int(pick)
        if hs is not None:
            site = hs
            if rid and rid not in site_region_ids(hs["id"]):
                rid = 0   # an area from another brand: don't show its questions here
        elif rid:
            site = site_of_region(rid)
        elif brands_on():
            b = request.args.get("brand")
            if b is None:
                b = session.get("cust_brand") or ""
            if str(b).isdigit():
                site = site_by_id(int(b))
        if not rid and site is not None:
            for r2 in sorted(site_region_ids(site["id"])):
                reg = _region(r2)
                if reg is not None and (_rv(reg, "faq_text") or "").strip():
                    rid = r2
                    break
    except Exception:
        return 0, None
    return rid, site

def faq_region_id():
    return faq_target()[0]

def faq_all_brands():
    """True on the shared website's FAQ with no brand or area picked (All brands). Only in the developer test view."""
    if not dev_all_brands_mode():
        return False
    try:
        if not brands_on() or host_site() is not None:
            return False
        rid, site = faq_target()
        return not rid and site is None
    except Exception:
        return False


def brand_region_hours(site_row):
    """Each of a brand's regions with its hours, closed dates and own phone, for the customer site.
    These are the same hours and closed dates the driver schedule checks shifts against."""
    if site_row is None:
        return []
    brand_ph = nice_phone((site_row["phone"] or "").strip()) if (site_row["phone"] or "").strip() else ""
    out = []
    for r in db().execute("SELECT * FROM regions WHERE COALESCE(site_id,0)=? ORDER BY sort, id",
                          (int(site_row["id"]),)).fetchall():
        rp = (r["phone"] or "").strip()
        ph = nice_phone(rp) if rp else ""
        out.append({"id": r["id"], "name": (r["name"] or "").strip(),
                    "hours": business_hours_label(r["id"]) or "",
                    "closed": closed_dates_label(r["id"], 5),
                    "phone": ph if ph and ph != brand_ph else "",
                    "tel": "".join(ch for ch in ph if ch.isdigit())})
    return out


def faq_brand_contacts():
    """Every brand's name, phone, email, address, and each region's hours and closed dates,
    for the All brands pages."""
    out = []
    main_hours = business_hours_label() or ""
    for srow in db().execute("SELECT * FROM sites ORDER BY sort, id").fetchall():
        bc = brand_contact(srow) or {}
        ph = nice_phone((srow["phone"] or "").strip()) or ""
        out.append({"name": (srow["name"] or "").strip(), "phone": ph,
                    "tel": "".join(ch for ch in ph if ch.isdigit()),
                    "email": bc.get("email") or "", "address": bc.get("address") or "",
                    "logo": brand_logo(srow), "regions": brand_region_hours(srow), "hours": main_hours})
    return [b for b in out if b["name"]]


@app.context_processor
def inject_foot_brands():
    """Footer hours: every brand on the All brands site, or this brand's own regions on a brand site."""
    try:
        if request.path.startswith(_STAFF_PREFIXES) or request.path.startswith(("/go/", "/reset/")) or not brands_on():
            return {"foot_brands": [], "foot_regions": []}
        s = current_site()
        if s is not None:
            return {"foot_brands": [], "foot_regions": brand_region_hours(s)}
        n = db().execute("SELECT COUNT(*) c FROM sites").fetchone()["c"]
        if n > 1:
            return {"foot_brands": faq_brand_contacts(), "foot_regions": []}
        one = db().execute("SELECT * FROM sites ORDER BY sort, id LIMIT 1").fetchone()
        return {"foot_brands": [], "foot_regions": brand_region_hours(one) if one is not None else []}
    except Exception:
        return {"foot_brands": [], "foot_regions": []}


def faq_items(region_id=None, site=None, all_brands=False):
    reg = _region(region_id) if region_id else None
    if site is None and region_id:
        site = site_of_region(region_id)
    d = site_design(site) if site is not None else {}
    def _biz(key):
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return (row["value"] if row else "") or ""
    raw = ((_rv(reg, "faq_text") or "").strip() if reg is not None else "") \
        or str(d.get("faq_text") or "").strip() or _biz("faq_text").strip() or DEFAULT_FAQ
    ph = ((_rv(reg, "phone") or "") if reg is not None else "") \
        or ((site["phone"] or "") if site is not None else "") or dispatch_phone()
    name = ((site["name"] or "") if site is not None else "").strip() or _biz("business_name").strip() or "Fleet Foot Delivery"
    _bc = brand_contact(site) if site is not None else None
    rep_ = {"{business}": name, "{phone}": nice_phone(ph) or "dispatch",
            "{email}": _bc["email"] if _bc is not None else _biz("business_email").strip(),
            "{address}": _bc["address"] if _bc is not None else _biz("business_address").strip()}
    # FAQs copied from an old site carry that site's phone numbers and email: show this brand's
    raw = _PHONE_RE.sub("{phone}", raw)
    if rep_["{email}"] or all_brands:
        raw = _EMAIL_RE.sub("{email}", raw)
    if all_brands:
        # All brands: no single brand's name, phone or email. Speak for every brand.
        names = {name}
        for srow in db().execute("SELECT name FROM sites").fetchall():
            if (srow["name"] or "").strip():
                names.add(srow["name"].strip())
        for n in sorted(names, key=len, reverse=True):
            if not n:
                continue
            pat = r"\s*".join(re.escape(ch) for ch in n.replace(" ", ""))
            raw = re.sub(r"(?i)\b" + pat + r"\b", "{business}", raw)
        raw = re.sub(r"\{business\} is\b", "We are", raw)
        raw = re.sub(r"\{business\} has\b", "We have", raw)
        raw = re.sub(r"(?i)\b(call|text|at|contact) \{phone\}", r"\1 your brand's number below", raw)
        raw = re.sub(r"(?i)\b(email|at) \{email\}", r"\1 your brand's email below", raw)
        rep_ = {"{business}": "us", "{phone}": "your brand's number below",
                "{email}": "your brand's email below", "{address}": ""}
    items, q, a = [], None, []
    for line in raw.splitlines() + ["Q:"]:
        t = line.strip()
        if t[:2].upper() == "Q:":
            if q:
                items.append({"q": q, "a": " ".join(a).strip()})
            q, a = t[2:].strip(), []
        elif t[:2].upper() == "A:":
            a.append(t[2:].strip())
        elif t and q is not None:
            a.append(t)
    for it in items:
        for k, v in rep_.items():
            it["q"], it["a"] = it["q"].replace(k, v), it["a"].replace(k, v)
    return [it for it in items if it["q"]]

@app.route("/faq")
def faq_page():
    rid, site = faq_target()
    if site is not None:
        g._site_forced = site   # the page's logo, name, phone and address match that brand
    if faq_all_brands():
        return render_template("faq.html", faqs=faq_items(rid, site, all_brands=True),
                               all_contacts=faq_brand_contacts())
    return render_template("faq.html", faqs=faq_items(rid, site), all_contacts=None)

@app.post("/api/home-search")
def api_home_search():
    addr = ((request.get_json(force=True) or {}).get("address") or "").strip()
    if len(addr) < 5:
        return jsonify({"ok": False, "error": "Type your delivery address."}), 400
    g = geocode(addr)
    if not g.get("ok") or g.get("lat") is None:
        return jsonify({"ok": False, "error": "We couldn't find that address. Add the city and ZIP and try again."}), 400
    session["home_geo"] = [g["lat"], g["lng"], (g.get("formatted") or addr)[:200]]
    out = []
    for r in db().execute("SELECT * FROM restaurants WHERE slug!='oneoff' AND lat IS NOT NULL").fetchall():
        miles = round(haversine_miles(r["lat"], r["lng"], g["lat"], g["lng"]) * ROAD_FACTOR, 1)
        mx = delivery_rules(r)["max_miles"]
        out.append({"slug": r["slug"], "miles": miles, "fee": money(fee_for_miles(miles, r["region_id"])), "ok": (not mx) or miles <= mx})
    return jsonify({"ok": True, "formatted": g.get("formatted") or addr, "restaurants": out,
                    "count": len([x for x in out if x["ok"]])})

CAR_MAKES = ["Acura", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Fiat", "Ford", "Genesis",
             "GMC", "Honda", "Hyundai", "Infiniti", "Jaguar", "Jeep", "Kia", "Land Rover", "Lexus", "Lincoln", "Mazda",
             "Mercedes-Benz", "Mini", "Mitsubishi", "Nissan", "Pontiac", "Ram", "Saturn", "Scion", "Subaru", "Tesla",
             "Toyota", "Volkswagen", "Volvo", "Other"]
US_STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
             "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
             "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]

def _apply_rate_ok():
    n = [t for t in session.get("apply_times", []) if time.time() - t < 3600]
    if len(n) >= 5:
        return False
    n.append(time.time())
    session["apply_times"] = n
    return True

def _phone3(f, key):
    return phone_digits((f.get(key + "1", "") or "") + (f.get(key + "2", "") or "") + (f.get(key + "3", "") or "")
                        or f.get(key, ""))

def _region_choices():
    """Area picks on the Drive with us and Partner with us pages. On a brand's site only that
    brand's regions show, and regions combined for drivers (Auburn + Downtown Auburn) show once.
    The value carries every region id in the group, comma separated."""
    rows = all_regions()
    try:
        site = current_site()
    except Exception:
        site = None
    if site is not None:
        mine = site_region_ids(site["id"])
        rows = [r for r in rows if r["id"] in mine]
    names = {r["id"]: r["name"] for r in rows}
    out, seen = [], set()
    for r in rows:
        if r["id"] in seen:
            continue
        grp = [x["id"] for x in rows if x["id"] in drive_group(r["id"])] or [r["id"]]
        seen.update(grp)
        out.append({"id": ",".join(str(i) for i in grp), "name": " + ".join(names[i] for i in grp)})
    return out

def _picked_region_ids(values):
    """Region ids from the area picks, kept only when they are on this page's list."""
    allowed = set()
    for c in _region_choices():
        allowed.update(int(x) for x in c["id"].split(","))
    ids = []
    for v in values:
        for x in str(v or "").split(","):
            x = x.strip()
            if x.isdigit() and int(x) in allowed and int(x) not in ids:
                ids.append(int(x))
    return ids

def _force_page_brand():
    try:
        _ds = faq_target()[1]
        if _ds is not None:
            g._site_forced = _ds   # a picked brand's page names and shows only that brand
    except Exception:
        pass

@app.route("/drive", methods=["GET", "POST"])
def drive_apply():
    f, errs, done = request.form, [], False
    _force_page_brand()
    if request.method == "POST":
        if f.get("website"):
            return render_template("drive.html", done=True, errs=[], f={}, regions=_region_choices(), makes=CAR_MAKES, all_contacts=faq_brand_contacts())
        ph = _phone3(f, "phone")
        first, last = (f.get("first_name") or "").strip()[:60], (f.get("last_name") or "").strip()[:60]
        if not first or not last:
            errs.append("Enter your first and last name.")
        try:
            dob = dt.date.fromisoformat(f.get("dob") or "")
            today = dt.date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                errs.append("You must be at least 18 to drive with us.")
            elif age > 100:
                errs.append("Check your date of birth.")
        except ValueError:
            errs.append("Enter your date of birth.")
        if len((f.get("address") or "").strip()) < 6:
            errs.append("Enter your address.")
        if "@" not in (f.get("email") or ""):
            errs.append("Enter your email address.")
        if len(ph) != 10:
            errs.append("Enter your 10 digit phone number.")
        try:
            yr = int(f.get("car_year") or 0)
        except ValueError:
            yr = 0
        if not (f.get("car_color") or "").strip() or not (1980 <= yr <= dt.date.today().year + 1) \
                or not f.get("car_make") or not (f.get("car_model") or "").strip():
            errs.append("Fill in your car's color, year, make and model.")
        regs = f.getlist("regions")
        if _region_choices() and not _picked_region_ids(regs):
            errs.append("Pick the area you want to drive in.")
        if not errs and not _apply_rate_ok():
            errs.append("Too many applications from this device. Try again later.")
        if not errs:
            _ids = _picked_region_ids(regs)
            rids = ",".join(str(i) for i in _ids) if _ids else "any"
            data = {k: (f.get(k) or "").strip()[:120] for k in ("first_name", "last_name", "dob", "address", "email",
                                                                   "car_color", "car_year", "car_make", "car_model", "note")}
            db().execute("""INSERT INTO applications (kind, name, phone, email, region_ids, data, created_at)
                            VALUES ('driver',?,?,?,?,?,?)""", (first + " " + last, ph, data["email"], rids, json.dumps(data), now()))
            db().commit()
            log("application", "New driver application: " + first + " " + last)
            done = True
    return render_template("drive.html", done=done, errs=errs, f=f, regions=_region_choices(), makes=CAR_MAKES, all_contacts=faq_brand_contacts())

@app.route("/partner", methods=["GET", "POST"])
def partner_apply():
    f, errs, done = request.form, [], False
    _force_page_brand()
    if request.method == "POST":
        if f.get("website"):
            return render_template("partner.html", done=True, errs=[], f={}, regions=_region_choices(), states=US_STATES, all_contacts=faq_brand_contacts())
        ph, fax = _phone3(f, "phone"), _phone3(f, "fax")
        need = {"rest_name": "the restaurant name", "rest_address": "the restaurant address", "rest_city": "the city",
                "first_name": "your first name", "last_name": "your last name"}
        for k, label in need.items():
            if not (f.get(k) or "").strip():
                errs.append("Enter " + label + ".")
        if (f.get("rest_state") or "") not in US_STATES:
            errs.append("Pick the state.")
        zp = "".join(ch for ch in (f.get("rest_zip") or "") if ch.isdigit())
        if len(zp) != 5:
            errs.append("Enter the 5 digit ZIP code.")
        if len(ph) != 10:
            errs.append("Enter the restaurant's 10 digit phone number.")
        if fax and len(fax) != 10:
            errs.append("The fax number needs 10 digits, or leave it blank.")
        if "@" not in (f.get("email") or ""):
            errs.append("Enter your email address.")
        if _region_choices() and not _picked_region_ids([f.get("region")]):
            errs.append("Pick the area the restaurant is in.")
        if not errs and not _apply_rate_ok():
            errs.append("Too many applications from this device. Try again later.")
        if not errs:
            reg = f.get("region")
            _ids = _picked_region_ids([reg])
            rids = ",".join(str(i) for i in _ids) if _ids else "any"
            data = {k: (f.get(k) or "").strip()[:160] for k in ("rest_name", "rest_address", "rest_city", "rest_state",
                                                                   "first_name", "last_name", "email", "note")}
            data.update({"rest_zip": zp, "fax": fax})
            db().execute("""INSERT INTO applications (kind, name, phone, email, region_ids, data, created_at)
                            VALUES ('restaurant',?,?,?,?,?,?)""", (data["rest_name"], ph, data["email"], rids, json.dumps(data), now()))
            db().commit()
            log("application", "New restaurant application: " + data["rest_name"])
            done = True
    return render_template("partner.html", done=done, errs=errs, f=f, regions=_region_choices(), states=US_STATES, all_contacts=faq_brand_contacts())

def _apps_visible():
    rows = db().execute("SELECT * FROM applications ORDER BY id DESC LIMIT 500").fetchall()
    if is_owner():
        return rows
    mine = {str(x) for x in dispatcher_region_ids(session.get("dispatcher_id"))}
    return [a for a in rows if (a["region_ids"] or "any") == "any" or (mine & set((a["region_ids"] or "").split(",")))]

def new_application_count():
    try:
        return len([a for a in _apps_visible() if a["status"] == "new"])
    except Exception:
        return 0

@app.route("/dispatch/applications")
def dispatch_applications():
    if not dispatcher_required():
        return redirect("/dispatch/login")
    names = {str(g["id"]): g["name"] for g in all_regions()}
    out = []
    for a in _apps_visible():
        d = json.loads(a["data"] or "{}")
        rids = a["region_ids"] or "any"
        out.append({"id": a["id"], "kind": a["kind"], "name": a["name"], "phone": nice_phone(a["phone"]),
                    "tel": tel_digits(a["phone"]), "email": a["email"] or "", "status": a["status"], "notes": a["notes"] or "",
                    "regions": "Any area" if rids == "any" else ", ".join(names.get(x, "Area " + x) for x in rids.split(",") if x),
                    "created": (a["created_at"] or "").replace("T", " ")[:16], "updated_by": a["updated_by"] or "", "d": d})
    return render_template("dispatch_applications.html", apps=out, kind=request.args.get("kind") or "driver", owner=is_owner())

@app.post("/api/dispatch/application-update")
def api_dispatch_application_update():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    b = request.get_json(force=True) or {}
    a = next((x for x in _apps_visible() if x["id"] == int(b.get("id") or 0)), None)
    if not a:
        return jsonify({"ok": False, "error": "Application not found."}), 404
    st = b.get("status") or a["status"]
    if st not in ("new", "contacted", "approved", "declined"):
        return jsonify({"ok": False, "error": "Pick a status."}), 400
    db().execute("UPDATE applications SET status=?, notes=?, updated_at=?, updated_by=? WHERE id=?",
                 (st, (b.get("notes") if b.get("notes") is not None else (a["notes"] or ""))[:500], now(),
                  session.get("dispatcher_name") or "dispatch", a["id"]))
    db().commit()
    return jsonify({"ok": True})

@app.post("/api/dispatch/application-delete")
def api_dispatch_application_delete():
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    if not is_owner():
        return jsonify({"ok": False, "error": "Only an owner can delete an application."}), 403
    b = request.get_json(force=True) or {}
    ids = [int(x) for x in (b.get("ids") or [b.get("id")]) if str(x or "").isdigit()]
    if not ids:
        return jsonify({"ok": False, "error": "Pick an application to delete."}), 400
    con = db()
    gone = []
    for i in ids[:500]:
        a = con.execute("SELECT id, kind, name FROM applications WHERE id=?", (i,)).fetchone()
        if a:
            con.execute("DELETE FROM applications WHERE id=?", (i,))
            gone.append(("restaurant" if a["kind"] == "restaurant" else "driver") + " application from " + (a["name"] or "?"))
    con.commit()
    for g in gone:
        log("application", "Deleted " + g + " by " + (session.get("dispatcher_name") or "owner"))
    return jsonify({"ok": True, "deleted": len(gone)})


# ---------------------------------------------------------------- customer password reset
# The customer gets a 6 digit code by text (when TWILIO_* is set) or from dispatch over the phone.
def texting_on():
    return bool(os.environ.get("TWILIO_SID") and os.environ.get("TWILIO_TOKEN") and os.environ.get("TWILIO_FROM"))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

def clean_email(v):
    v = (v or "").strip().lower()[:120]
    return v if EMAIL_RE.match(v) else ""

def email_on():
    """Email goes out through Resend (RESEND_API_KEY + EMAIL_FROM), which works on every Railway
    plan, or plain SMTP (SMTP_HOST/SMTP_USER/SMTP_PASS + EMAIL_FROM), which Railway only allows
    on the Pro plan."""
    return bool(os.environ.get("EMAIL_FROM") and (os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_HOST")))

def send_email(to, subject, text):
    frm = os.environ.get("EMAIL_FROM", "")
    biz = (setting("business_name", str) or "Fleet Foot Delivery").strip()
    if frm and "<" not in frm:
        frm = biz + " <" + frm + ">"
    if not (frm and to):
        return False
    try:
        if os.environ.get("RESEND_API_KEY"):
            body = json.dumps({"from": frm, "to": [to], "subject": subject, "text": text}).encode()
            req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST")
            req.add_header("Authorization", "Bearer " + os.environ["RESEND_API_KEY"])
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "fleetfoot/1.0")
            urllib.request.urlopen(req, timeout=10).read()
            return True
        if os.environ.get("SMTP_HOST"):
            import smtplib
            from email.message import EmailMessage
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = frm, to, subject
            m.set_content(text)
            port = int(os.environ.get("SMTP_PORT") or 587)
            if port == 465:
                s = smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, timeout=10)
            else:
                s = smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=10)
                s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(m)
            s.quit()
            return True
    except Exception as exc:
        try:
            err = exc.read().decode()[:200]
        except Exception:
            err = str(exc)[:200]
        log("email_error", err)
    return False

def find_customer(who, need_pw=False):
    """A customer by 10 digit phone or by email. With an email shared by more than one
    record, the online account (has a password) and the newest sign-in win."""
    who = (who or "").strip()
    extra = " AND pw_hash IS NOT NULL" if need_pw else ""
    if "@" in who:
        em = who.lower()
        return db().execute("SELECT * FROM customers WHERE lower(trim(email))=?" + extra +
                            " ORDER BY (pw_hash IS NULL), COALESCE(last_login_at,'') DESC, id DESC LIMIT 1",
                            (em,)).fetchone()
    ph = phone_digits(who)
    if len(ph) != 10:
        return None
    return db().execute("SELECT * FROM customers WHERE phone=?" + extra, (ph,)).fetchone()

def email_taken(em, not_id=0):
    return bool(em) and bool(db().execute("SELECT 1 FROM customers WHERE lower(trim(email))=? AND pw_hash IS NOT NULL AND id<>?",
                                          (em, not_id)).fetchone())

def make_reset_code(c, minutes=15):
    code = str(secrets.randbelow(900000) + 100000)
    exp = (dt.datetime.now() + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db().execute("UPDATE customers SET reset_hash=?, reset_expires=?, reset_tries=0 WHERE id=?",
                 (generate_password_hash(code), exp, c["id"]))
    db().commit()
    return code

@app.route("/account/reset", methods=["GET", "POST"])
def account_reset():
    """Forgot password: a 6 digit code goes to the customer's email (when email is set up) or
    texted to their phone (when texting is set up); otherwise dispatch reads them one."""
    step, msg, err = request.form.get("step") or request.args.get("step") or "send", "", ""
    who = (request.form.get("who") or request.form.get("phone") or request.args.get("who")
           or request.args.get("phone") or "").strip()[:120]
    by_email = "@" in who
    biz = (setting("business_name", str) or "Fleet Foot Delivery").strip()
    if request.method == "POST" and step == "send":
        sends = [x for x in session.get("reset_sends", []) if time.time() - x < 3600]
        if by_email and not clean_email(who):
            err = "That email doesn't look right."
        elif not by_email and len(phone_digits(who)) != 10:
            err = "Enter your 10 digit phone number or your email."
        elif len(sends) >= 3:
            err = "Too many codes asked for. Try again in an hour, or call dispatch."
        else:
            sends.append(time.time())
            session["reset_sends"] = sends
            c = find_customer(who)
            call = ("Call dispatch at " + (nice_phone(dispatch_phone()) or "our number") +
                    " and they'll give you a reset code, then enter it below.")
            if by_email and email_on():
                if c:
                    code = make_reset_code(c)
                    link = request.host_url.rstrip("/") + "/account/reset?step=verify&who=" + urllib.parse.quote(who)
                    send_email(who, biz + " password reset code: " + code,
                               "Hi " + ((c["name"] or "").split(" ")[0] or "there") + ",\n\n"
                               "Your " + biz + " password reset code is " + code + ". It works for 15 minutes.\n\n"
                               "Enter it here to pick a new password:\n" + link + "\n\n"
                               "If you didn't ask for this, you can ignore this email. Your password stays the same.")
                msg = "If that email has an account, we just sent it a 6 digit code. Check your spam folder too."
            elif by_email and texting_on() and c and len(phone_digits(c["phone"])) == 10:
                send_text("+1" + phone_digits(c["phone"]), biz + " password reset code: " + make_reset_code(c) +
                          ". It works for 15 minutes.")
                msg = "If that email has an account, we just texted a 6 digit code to the phone on it."
            elif not by_email and texting_on():
                if c:
                    send_text("+1" + phone_digits(who), biz + " password reset code: " + make_reset_code(c) +
                              ". It works for 15 minutes.")
                msg = "If that number has an account, we just texted it a 6 digit code."
            else:
                msg = call
            step = "verify"
    elif request.method == "POST" and step == "verify":
        code = "".join(ch for ch in (request.form.get("code") or "") if ch.isdigit())
        pw = request.form.get("password") or ""
        c = find_customer(who)
        if len(pw) < 6:
            err = "Pick a password with at least 6 characters."
        elif pw != (request.form.get("password2") or ""):
            err = "The two passwords don't match."
        elif not c or not c["reset_hash"] or (c["reset_expires"] or "") < now():
            err = "That code has expired or is wrong. Ask for a new one."
        elif int(c["reset_tries"] or 0) >= 5:
            err = "Too many wrong tries. Ask for a new code."
        elif not check_password_hash(c["reset_hash"], code):
            db().execute("UPDATE customers SET reset_tries=reset_tries+1 WHERE id=?", (c["id"],))
            db().commit()
            err = "That code is wrong. Check it and try again."
        else:
            db().execute("""UPDATE customers SET pw_hash=?, reset_hash=NULL, reset_expires=NULL, reset_tries=0,
                            last_login_at=? WHERE id=?""", (generate_password_hash(pw), now(), c["id"]))
            db().commit()
            session["customer_id"] = c["id"]
            session.pop("cust_fails", None)
            log("customer", (c["name"] or "Customer") + " reset their password")
            return redirect("/account")
    shown = who if by_email else (nice_phone(phone_digits(who)) if phone_digits(who) else "")
    return render_template("account_reset.html", step=step, msg=msg, err=err, who=shown, phone=shown,
                           texting=texting_on(), emailing=email_on(), biz_phone=nice_phone(dispatch_phone()))

@app.post("/api/dispatch/customer-reset-code")
def api_dispatch_customer_reset_code():
    """Dispatch reads a reset code to a customer who called in (good for 30 minutes)."""
    if not dispatcher_required():
        return jsonify({"ok": False}), 403
    c = db().execute("SELECT * FROM customers WHERE id=?", (int((request.get_json(force=True) or {}).get("id") or 0),)).fetchone()
    if not c:
        return jsonify({"ok": False, "error": "Customer not found."}), 404
    code = make_reset_code(c, 30)
    log("customer", "Reset code made for " + (c["name"] or nice_phone(c["phone"])) + " by " + (session.get("dispatcher_name") or "dispatch"))
    return jsonify({"ok": True, "code": code, "message": "Read this code to the customer. They go to " +
                    request.host_url.rstrip("/") + "/account/reset?step=verify and enter it with their phone number or email. It works for 30 minutes."})



# ---------------------------------------------------------------- staff password / PIN reset
# Dispatchers (username), drivers (mobile number) and restaurants (store code) can reset their
# own password or PIN with a 6 digit code texted to the phone on file. Without texting set up
# (TWILIO_*), or with no phone on file, the page tells them who to ask instead.
STAFF_RESET = {
    "dispatch":   {"title": "Dispatcher", "ask": "Username", "secret": "password", "login": "/dispatch/login"},
    "driver":     {"title": "Driver", "ask": "Mobile number", "secret": "PIN", "login": "/driver/login"},
    "restaurant": {"title": "Restaurant", "ask": "Store code", "secret": "PIN", "login": "/restaurant/login"},
}

def _staff_reset_table():
    db().execute("""CREATE TABLE IF NOT EXISTS staff_resets (
        kind TEXT NOT NULL, ref_id INTEGER NOT NULL, code_hash TEXT NOT NULL,
        expires TEXT NOT NULL, tries INTEGER DEFAULT 0, created_at TEXT,
        PRIMARY KEY (kind, ref_id))""")

def _staff_find(kind, who):
    who = (who or "").strip()
    if not who:
        return None, ""
    if kind == "dispatch":
        r = db().execute("SELECT * FROM dispatchers WHERE lower(username)=lower(?)", (who,)).fetchone()
        return r, phone_digits((r["phone"] if r else "") or "")
    if kind == "driver":
        ph = phone_digits(who)[-10:]
        r = db().execute("SELECT * FROM drivers WHERE phone=? AND COALESCE(active,1)=1", (ph,)).fetchone() if len(ph) == 10 else None
        return r, phone_digits((r["phone"] if r else "") or "")
    r = db().execute("SELECT * FROM restaurants WHERE slug=? AND slug!='oneoff'", (who.lower(),)).fetchone()
    return r, phone_digits((r["phone"] if r else "") or "")

@app.route("/reset/<kind>", methods=["GET", "POST"])
def staff_reset(kind):
    if kind not in STAFF_RESET:
        return redirect("/")
    cfg = STAFF_RESET[kind]
    _staff_reset_table()
    step = request.form.get("step") or request.args.get("step") or "send"
    who = (request.form.get("who") or request.args.get("who") or "").strip()[:80]
    msg, err = "", ""
    biz = (setting("business_name", str) or "Fleet Foot Delivery").strip()
    helper = "an owner" if kind == "dispatch" else "dispatch"
    call = nice_phone(dispatch_phone())
    if request.method == "POST" and step == "send":
        key = "staff_reset_sends_" + kind
        sends = [t for t in session.get(key, []) if time.time() - t < 3600]
        if not who:
            err = "Enter your " + cfg["ask"].lower() + "."
        elif len(sends) >= 3:
            err = "Too many codes asked for. Try again in an hour, or ask " + helper + " to reset it."
        else:
            sends.append(time.time()); session[key] = sends
            row, ph = _staff_find(kind, who)
            if not texting_on():
                msg = ("Text codes aren't set up yet. Ask " + helper + (" at " + call if call and kind != "dispatch" else "") +
                       " to set a new " + cfg["secret"] + " for you.")
                step = "send"
            else:
                if row and len(ph) >= 10:
                    code = str(secrets.randbelow(900000) + 100000)
                    exp = (dt.datetime.now() + dt.timedelta(minutes=15)).isoformat(timespec="seconds")
                    db().execute("INSERT OR REPLACE INTO staff_resets(kind,ref_id,code_hash,expires,tries,created_at) VALUES(?,?,?,?,0,?)",
                                 (kind, row["id"], generate_password_hash(code), exp, now()))
                    db().commit()
                    send_text("+1" + ph[-10:], biz + " " + cfg["title"].lower() + " " + cfg["secret"] + " reset code: " + code + ". It works for 15 minutes.")
                    log("reset", cfg["title"] + " reset code sent for " + (row["name"] or who))
                msg = ("If that matches an account with a phone on file, we just texted a 6 digit code. "
                       "No text? Ask " + helper + " to set a new " + cfg["secret"] + " for you.")
                step = "verify"
    elif request.method == "POST" and step == "verify":
        code = "".join(ch for ch in (request.form.get("code") or "") if ch.isdigit())
        new = (request.form.get("new") or "").strip()
        row, _ph = _staff_find(kind, who)
        rec = db().execute("SELECT * FROM staff_resets WHERE kind=? AND ref_id=?", (kind, row["id"])).fetchone() if row else None
        if kind == "dispatch" and len(new) < 6:
            err = "Pick a password with at least 6 characters."
        elif kind != "dispatch" and not (new.isdigit() and 4 <= len(new) <= 8):
            err = "Your new PIN has to be 4 to 8 numbers."
        elif new != (request.form.get("new2") or "").strip():
            err = "The two entries don't match."
        elif not rec or (rec["expires"] or "") < now():
            err = "That code has expired or is wrong. Ask for a new one."
        elif int(rec["tries"] or 0) >= 5:
            err = "Too many wrong tries. Ask for a new code."
        elif not check_password_hash(rec["code_hash"], code):
            db().execute("UPDATE staff_resets SET tries=tries+1 WHERE kind=? AND ref_id=?", (kind, row["id"]))
            db().commit()
            err = "That code is wrong. Check it and try again."
        else:
            if kind == "dispatch":
                db().execute("UPDATE dispatchers SET password=? WHERE id=?", (new, row["id"]))
            elif kind == "driver":
                db().execute("UPDATE drivers SET pin=? WHERE id=?", (new, row["id"]))
            else:
                db().execute("UPDATE restaurants SET pin=? WHERE id=?", (new, row["id"]))
            db().execute("DELETE FROM staff_resets WHERE kind=? AND ref_id=?", (kind, row["id"]))
            db().commit()
            log("reset", cfg["title"] + " " + (row["name"] or who) + " reset their " + cfg["secret"])
            return redirect(cfg["login"] + "?reset=1")
    return render_template("staff_reset.html", kind=kind, cfg=cfg, step=step, who=who, msg=msg, err=err,
                           texting=texting_on())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
