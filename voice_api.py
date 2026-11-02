
# ---------------------------------------------------------------------------
# Voice line (Twilio) tools: what the AI voice agent may do by phone.
# Every endpoint here is token-protected (X-Voice-Token header) and is meant
# to be called only by the voice service, never a browser.
# Setup: set the voice API token in Settings (key voice_api_token) or as the
# VOICE_API_TOKEN variable on Railway. The voice service then calls
# https://<this app>/api/voice/...
# ---------------------------------------------------------------------------
import os
from functools import wraps
from flask import Blueprint, request, jsonify
import app as A

voice_bp = Blueprint("voice_line", __name__)

# the only statuses a caller may still change
_OPEN_STATUSES = ("held", "queued", "awaiting_payment", "scheduled")


def _voice_token():
    tok = ""
    try:
        row = A.db().execute("SELECT value FROM settings WHERE key='voice_api_token'").fetchone()
        if row and row["value"]:
            tok = str(row["value"]).strip()
    except Exception:
        pass
    return tok or os.environ.get("VOICE_API_TOKEN", "").strip()


def voice_auth(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        want = _voice_token()
        got = (request.headers.get("X-Voice-Token") or "").strip()
        if not want:
            return jsonify({"ok": False,
                            "error": "The voice line is not set up yet: set the voice API token."}), 403
        if not got or got != want:
            return jsonify({"ok": False, "error": "Bad voice token."}), 403
        return fn(*a, **kw)
    return wrap


def _digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _site(name):
    rows = A.db().execute("SELECT * FROM sites ORDER BY COALESCE(sort, 0), id").fetchall()
    nm = (name or "").strip().lower()
    for s in rows:
        if (s["name"] or "").strip().lower() == nm:
            return s
    return rows[0] if (rows and not nm) else None


def _site_rests(site):
    return A.db().execute(
        "SELECT r.* FROM restaurants r JOIN regions g ON g.id = r.region_id "
        "WHERE g.site_id = ? ORDER BY r.name", (site["id"],)).fetchall()


def _pick_rest(site, name):
    rests = _site_rests(site)
    if not rests:
        return None, rests
    nm = (name or "").strip().lower()
    if nm:
        for x in rests:
            if (x["name"] or "").strip().lower() == nm:
                return x, rests
        return None, rests
    for x in rests:
        if A.is_open(x):
            return x, rests
    return rests[0], rests


def _order_by(code, phone=None):
    code = (code or "").strip().upper()
    if not code:
        return None
    o = A.db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if o is None or (phone and _digits(phone) != _digits(o["customer_phone"])):
        return None
    return o


def _voice_change_ok(o):
    return (not (o["kitchen_sent_at"] or "")) and o["dispatch_status"] in _OPEN_STATUSES


def _cutoff_msg(o):
    if o["dispatch_status"] in ("delivered", "cancelled"):
        return "That order was already " + o["dispatch_status"] + "."
    return ("That order is already with the kitchen, so it cannot be changed or "
            "cancelled here. I can send your message to dispatch instead.")


def _order_view(o):
    return {"code": o["code"], "restaurant": o["rname"],
            "status": o["dispatch_status"], "kitchen": o["kitchen_status"],
            "sent_to_kitchen": bool(o["kitchen_sent_at"] or ""),
            "hold_reason": o["hold_reason"] or "",
            "total": A.money(o["total_cents"]), "total_cents": o["total_cents"],
            "address": o["address"], "placed_at": o["created_at"],
            "driver": bool(o["driver_id"])}


def _menu_rows(rid):
    return A.db().execute(
        "SELECT * FROM menu_items WHERE restaurant_id=? AND active=1 ORDER BY name", (rid,)).fetchall()


@voice_bp.get("/api/voice/health")
def voice_health():
    return jsonify({"ok": True, "token_set": bool(_voice_token())})


@voice_bp.post("/api/voice/menu")
@voice_auth
def voice_menu():
    b = request.get_json(force=True) or {}
    s = _site(b.get("site"))
    if s is None:
        return jsonify({"ok": False, "error": "No brand by that name."}), 404
    rests = _site_rests(s)
    if not rests:
        return jsonify({"ok": False,
                        "error": "No restaurants are tied to that brand yet."}), 404
    out = {"ok": True, "site": s["name"], "restaurants": []}
    for r in rests:
        rj = {"name": r["name"], "open": bool(A.is_open(r)),
              "hours": A.hours_label(r), "items": []}
        for it in _menu_rows(r["id"]):
            try:
                avail = bool(A.item_available(it))
            except Exception:
                avail = True
            groups = A.db().execute(
                "SELECT * FROM option_groups WHERE item_id=? ORDER BY sort", (it["id"],)).fetchall()
            gs = []
            for g in groups:
                opts = A.db().execute(
                    "SELECT * FROM options WHERE group_id=? ORDER BY sort", (g["id"],)).fetchall()
                gs.append({"name": g["name"], "min": g["min_select"], "max": g["max_select"],
                           "choices": [{"name": o["name"], "delta_cents": o["price_delta_cents"]}
                                       for o in opts]})
            rj["items"].append({"menu_item_id": it["id"], "name": it["name"],
                                "description": it["description"] or "",
                                "price_cents": it["price_cents"],
                                "price": A.money(it["price_cents"]),
                                "available_now": avail, "options": gs})
        out["restaurants"].append(rj)
    return jsonify(out)


@voice_bp.post("/api/voice/validate_address")
@voice_auth
def voice_validate():
    b = request.get_json(force=True) or {}
    s = _site(b.get("site"))
    if s is None:
        return jsonify({"ok": False, "error": "No brand by that name."}), 404
    r, rests = _pick_rest(s, b.get("restaurant_name"))
    if r is None:
        return jsonify({"ok": False, "error": "No restaurant on this brand yet."}), 404
    typed = (b.get("address") or "").strip()
    if not typed:
        return jsonify({"ok": False, "error": "Need a delivery address."}), 400
    g1 = A.geocode(typed)
    base_fee = A.fee_rules(A._rv(r, "region_id"))["base_fee"]
    out = {"ok": True, "site": s["name"], "restaurant": r["name"]}
    if g1["ok"]:
        miles, fee = (A.quote(r, g1["lat"], g1["lng"]) if r["lat"] and r["lng"]
                      else (0, base_fee))
        rules = A.delivery_rules(r)
        out.update({"formatted": g1["formatted"], "miles": miles, "fee_cents": fee,
                    "in_range": (not rules["max_miles"]) or miles <= rules["max_miles"],
                    "min_order_cents": rules["min_cents"]})
    else:
        zc = A.zip_check(r, typed)
        out.update({"formatted": typed, "miles": 0, "fee_cents": base_fee,
                    "in_range": bool(zc["found"]) and bool(zc["within"]),
                    "needs_zip": not zc["found"],
                    "note": ("The street could not be verified. Add the 5-digit ZIP code "
                             "to the address.") if not zc["found"] else ""})
    return jsonify(out)


@voice_bp.post("/api/voice/find_order")
@voice_auth
def voice_find_order():
    b = request.get_json(force=True) or {}
    code = (b.get("code") or "").strip().upper()
    phone = _digits(b.get("phone"))
    rows = A.db().execute(
        "SELECT o.*, r.name AS rname FROM orders o JOIN restaurants r ON r.id=o.restaurant_id "
        "ORDER BY o.id DESC LIMIT 300").fetchall()
    out = []
    for o in rows:
        if code:
            if o["code"].upper() != code:
                continue
        elif phone:
            if _digits(o["customer_phone"])[-10:] != phone[-10:]:
                continue
        else:
            break
        out.append(_order_view(o))
        if len(out) >= 10:
            break
    return jsonify({"ok": True, "orders": out})


@voice_bp.post("/api/voice/place_order")
@voice_auth
def voice_place():
    b = request.get_json(force=True) or {}
    s = _site(b.get("site"))
    if s is None:
        return jsonify({"ok": False, "error": "No brand by that name."}), 404
    r, rests = _pick_rest(s, b.get("restaurant_name"))
    if r is None:
        return jsonify({"ok": False, "error": "No restaurant on this brand yet."}), 404
    if not A.is_open(r):
        return jsonify({"ok": False,
                        "error": r["name"] + " is closed right now (" + A.hours_label(r) + ")."}), 400
    name = (b.get("customer_name") or "").strip()[:80]
    phone = (b.get("customer_phone") or "").strip()[:24]
    addr = (b.get("address") or "").strip()
    if not name or not _digits(phone):
        return jsonify({"ok": False, "error": "I need the caller's name and phone number."}), 400
    if not addr:
        return jsonify({"ok": False, "error": "I need the delivery address."}), 400
    items = []
    for it in (b.get("items") or []):
        if not isinstance(it, dict):
            continue
        row = None
        mid = it.get("menu_item_id")
        if mid:
            row = A.db().execute(
                "SELECT * FROM menu_items WHERE id=? AND restaurant_id=? AND active=1",
                (mid, r["id"])).fetchone()
        if row is None:
            nm = (it.get("name") or "").strip().lower()
            if nm:
                matches = [c for c in _menu_rows(r["id"]) if (c["name"] or "").lower() == nm]
                if not matches:
                    matches = [c for c in _menu_rows(r["id"])
                               if nm in (c["name"] or "").lower()
                               or (c["name"] or "").lower() in nm]
                if len(matches) == 1:
                    row = matches[0]
        if row is None:
            return jsonify({"ok": False,
                            "error": "Which item is \"" + str(it.get("name") or "?") +
                                     "\"? It is not on the menu."}), 400
        opts = []
        groups = A.db().execute(
            "SELECT * FROM option_groups WHERE item_id=? ORDER BY sort", (row["id"],)).fetchall()
        for want in (it.get("options") or []):
            wn = ((want.get("name") if isinstance(want, dict) else want) or "").strip().lower()
            gname = (want.get("group") if isinstance(want, dict) else "") or ""
            for g in groups:
                if gname and (g["name"] or "").lower() != gname.lower():
                    continue
                for o in A.db().execute(
                        "SELECT * FROM options WHERE group_id=? ORDER BY sort", (g["id"],)).fetchall():
                    if (o["name"] or "").strip().lower() == wn:
                        opts.append({"group": g["name"], "name": o["name"],
                                     "delta_cents": o["price_delta_cents"]})
                        break
                else:
                    continue
                break
        items.append({"menu_item_id": row["id"], "name": row["name"],
                      "qty": max(1, int(it.get("qty") or 1)),
                      "price_cents": row["price_cents"], "fee_cents": 0,
                      "note": (it.get("note") or "")[:120], "options": opts})
    if not items:
        return jsonify({"ok": False, "error": "No items were named."}), 400
    payload = {"restaurant_id": r["id"], "items": items,
               "customer_name": name, "customer_phone": phone, "address": addr,
               "note": (b.get("note") or "")[:200],
               "dispatch_note": ("Placed by the AI voice line. Take payment by phone or on "
                                 "delivery." + ((" " + str(b.get("dispatch_note"))[:200])
                                                if b.get("dispatch_note") else "")),
               "tip_cents": b.get("tip_cents") or 0}
    with A.app.test_request_context("/checkout", method="POST", json=payload):
        resp = A.checkout()
        data = resp.get_json()
    if not data.get("ok"):
        return jsonify({"ok": False, "error": data.get("error", "The order did not go through.")}), 400
    o = A.db().execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
    A.db().execute("UPDATE orders SET source='call_in' WHERE id=?", (o["id"],))
    A.db().commit()
    return jsonify({"ok": True, "code": o["code"], "order_id": o["id"],
                    "restaurant": r["name"], "total": A.money(o["total_cents"]),
                    "total_cents": o["total_cents"],
                    "hold_reason": o["hold_reason"] or "",
                    "pay_note": "Dispatch takes the payment by phone or on delivery."})


@voice_bp.post("/api/voice/cancel_order")
@voice_auth
def voice_cancel():
    b = request.get_json(force=True) or {}
    o = _order_by(b.get("code"), b.get("phone"))
    if o is None:
        return jsonify({"ok": False,
                        "error": "I could not find that order for this phone number."}), 404
    if not _voice_change_ok(o):
        return jsonify({"ok": False, "error": _cutoff_msg(o)}), 400
    A.db().execute("""UPDATE orders SET dispatch_status='cancelled', kitchen_status='waiting',
                      hold_reason='cancelled by the customer on the AI voice line', delivered_at=?
                      WHERE id=?""", (A.now(), o["id"]))
    if o["driver_id"]:
        A.db().execute("""INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at)
                          VALUES(?,?,?,?,?,?)""",
                       (o["driver_id"], "dispatch", None, None,
                        "Order " + o["code"] + " was cancelled by the customer on the phone.", A.now()))
    A.log("cancel", o["code"] + " cancelled by the customer on the AI voice line")
    A.db().commit()
    return jsonify({"ok": True, "message": "The order is cancelled."})


@voice_bp.post("/api/voice/modify_order")
@voice_auth
def voice_modify():
    b = request.get_json(force=True) or {}
    o = _order_by(b.get("code"), b.get("phone"))
    if o is None:
        return jsonify({"ok": False,
                        "error": "I could not find that order for this phone number."}), 404
    if not _voice_change_ok(o):
        return jsonify({"ok": False, "error": _cutoff_msg(o)}), 400
    note = (b.get("note") or "").strip()[:200]
    new_addr = (b.get("address") or "").strip()
    changes = []
    if note:
        dn = ((o["dispatch_note"] or "") + " | Voice line: " + note)[:400]
        A.db().execute("UPDATE orders SET dispatch_note=? WHERE id=?", (dn, o["id"]))
        changes.append("note added")
    if new_addr:
        r = A.db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
        g1 = A.geocode(new_addr)
        base_fee = A.fee_rules(A._rv(r, "region_id"))["base_fee"]
        if g1["ok"]:
            miles, fee = (A.quote(r, g1["lat"], g1["lng"]) if r["lat"] and r["lng"]
                          else (0, base_fee))
            rules = A.delivery_rules(r)
            if rules["max_miles"] and miles > rules["max_miles"]:
                return jsonify({"ok": False,
                                "error": "That address is %.1f miles from %s, outside the delivery area." % (miles, r["name"])}), 400
            subtotal = o["subtotal_cents"]
            ifee = o["item_fee_cents"] or 0
            tax = int(round(subtotal * A.setting("tax_rate_bp") / 10000.0))
            service = int(round(subtotal * A.service_bp_for(r) / 10000.0))
            total = subtotal + fee + ifee + tax + service + (o["tip_cents"] or 0)
            A.db().execute("""UPDATE orders SET address=?, lat=?, lng=?, address_ok=1, miles=?,
                              fee_cents=?, tax_cents=?, service_cents=?, total_cents=? WHERE id=?""",
                           (g1["formatted"], g1["lat"], g1["lng"], miles, fee, tax, service,
                            total, o["id"]))
            if (o["hold_reason"] or "") == "address needs dispatch approval":
                A.db().execute("UPDATE orders SET kitchen_status='pending', "
                               "hold_reason='tap Send to kitchen' WHERE id=?", (o["id"],))
            changes.append("address changed to " + g1["formatted"] + ", new total " + A.money(total))
        else:
            A.db().execute("UPDATE orders SET address=?, lat=NULL, lng=NULL, address_ok=0, "
                           "hold_reason='address needs dispatch approval' WHERE id=?",
                           (new_addr, o["id"]))
            changes.append("address updated but not verified; dispatch will confirm it")
    if not changes:
        return jsonify({"ok": False,
                        "error": "Say what to change: a new address or a note on the order."}), 400
    A.log("order", o["code"] + " changed on the AI voice line: " + "; ".join(changes))
    A.db().commit()
    o2 = A.db().execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    return jsonify({"ok": True, "changes": changes, "total": A.money(o2["total_cents"])})


@voice_bp.post("/api/voice/report_issue")
@voice_auth
def voice_issue():
    b = request.get_json(force=True) or {}
    o = _order_by(b.get("code"), b.get("phone"))
    if o is None:
        return jsonify({"ok": False,
                        "error": "I could not find that order for this phone number."}), 404
    issue = (b.get("issue") or "issue").strip()[:80]
    note = (b.get("note") or "").strip()[:300]
    txt = "CUSTOMER ISSUE (AI voice line): " + issue + ((" - " + note) if note else "")
    A.db().execute("UPDATE orders SET dispatch_note=? WHERE id=?",
                   (((o["dispatch_note"] or "") + " | " + txt)[:500], o["id"]))
    A.raise_call_alert("voice", o["customer_name"], o["customer_phone"], txt,
                       order_id=o["id"], restaurant_id=o["restaurant_id"])
    A.db().commit()
    return jsonify({"ok": True,
                    "message": "Dispatch has your report and will follow up."})


@voice_bp.post("/api/voice/driver_orders")
@voice_auth
def voice_driver_orders():
    b = request.get_json(force=True) or {}
    phone = _digits(b.get("phone"))[-10:]
    if not phone:
        return jsonify({"ok": False, "error": "No phone number on the call."}), 400
    d = None
    for x in A.db().execute("SELECT * FROM drivers").fetchall():
        if _digits(x["phone"])[-10:] == phone:
            d = x
            break
    if d is None:
        return jsonify({"ok": False, "error": "This number is not a driver on file."}), 404
    rows = A.db().execute("""SELECT o.*, r.name AS rname FROM orders o
                             JOIN restaurants r ON r.id=o.restaurant_id
                             WHERE o.driver_id=? AND o.dispatch_status IN
                             ('assigned','received','at_restaurant','enroute')
                             ORDER BY o.id""", (d["id"],)).fetchall()
    return jsonify({"ok": True, "driver": d["name"], "status": d["status"],
                    "pending_request": d["pending_request"] or "",
                    "orders": [_order_view(o) for o in rows]})


@voice_bp.post("/api/voice/driver_message")
@voice_auth
def voice_driver_message():
    b = request.get_json(force=True) or {}
    phone = _digits(b.get("phone"))[-10:]
    d = None
    for x in A.db().execute("SELECT * FROM drivers").fetchall():
        if _digits(x["phone"])[-10:] == phone:
            d = x
            break
    if d is None:
        return jsonify({"ok": False, "error": "This number is not a driver on file."}), 404
    body = (b.get("body") or "").strip()[:400]
    if not body:
        return jsonify({"ok": False, "error": "Nothing was said."}), 400
    A.db().execute("""INSERT INTO messages(driver_id,sender,sender_name,dispatcher_id,body,created_at)
                      VALUES(?,?,?,?,?,?)""", (d["id"], "driver", d["name"], None, body, A.now()))
    A.db().commit()
    return jsonify({"ok": True, "message": "Dispatch got your message."})


@voice_bp.post("/api/voice/driver_request")
@voice_auth
def voice_driver_request():
    b = request.get_json(force=True) or {}
    phone = _digits(b.get("phone"))[-10:]
    d = None
    for x in A.db().execute("SELECT * FROM drivers").fetchall():
        if _digits(x["phone"])[-10:] == phone:
            d = x
            break
    if d is None:
        return jsonify({"ok": False, "error": "This number is not a driver on file."}), 404
    want = (b.get("request") or "").strip().lower()
    if want not in ("online", "break", "offline"):
        return jsonify({"ok": False,
                        "error": "Ask for online, break, or offline."}), 400
    A.db().execute("UPDATE drivers SET pending_request=? WHERE id=?", (want, d["id"]))
    A.db().commit()
    return jsonify({"ok": True,
                    "message": "Dispatch got your request and will approve it shortly."})


# ---- restaurant call-in: the voice line dials a restaurant and reads the order ----

import json as _json
import urllib.request as _urllib
import urllib.error as _urlerr


def _voice_call_conf():
    """Where the voice service lives and the token it accepts from us."""
    url = tok = ""
    try:
        for k, col in (("voice_call_url", "url"), ("voice_call_token", "tok")):
            row = A.db().execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if row and row["value"]:
                if col == "url":
                    url = str(row["value"]).strip().rstrip("/")
                else:
                    tok = str(row["value"]).strip()
    except Exception:
        pass
    return url or os.environ.get("VOICE_CALL_URL", "").strip().rstrip("/"), \
        tok or _voice_token()


def _rest_items_lines(raw):
    lines = []
    try:
        items = _json.loads(raw or "[]")
    except Exception:
        items = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        qty = int(it.get("qty", 1) or 1)
        line = ("%d %s" % (qty, it.get("name") or "item")).strip()
        picks = it.get("options") or []
        if picks:
            line += " (" + "; ".join(
                str(p.get("name") or "").strip() for p in picks if str(p.get("name") or "").strip()
            ) + ")"
        note = (it.get("note") or "").strip()
        if note:
            line += " [" + note + "]"
        lines.append(line)
    return lines


def _order_for_call(b):
    code = (b.get("code") or "").strip()
    o = None
    if code:
        o = A.db().execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
    if o is None:
        return None
    r = A.db().execute("SELECT * FROM restaurants WHERE id=?", (o["restaurant_id"],)).fetchone()
    return o, r


def _brand_name(restaurant_id):
    """The brand line the customer called: restaurant -> region -> site."""
    row = A.db().execute("""SELECT s.name FROM restaurants r
                            LEFT JOIN regions g ON g.id=r.region_id
                            LEFT JOIN sites s ON s.id=g.site_id
                            WHERE r.id=?""", (restaurant_id,)).fetchone()
    if row and row["name"]:
        return row["name"]
    row = A.db().execute("SELECT name FROM sites ORDER BY COALESCE(sort,0), id LIMIT 1").fetchone()
    return row["name"] if row else "Fleet Foot Delivery"


@voice_bp.post("/api/voice/order_brief")
@voice_auth
def voice_order_brief():
    """Everything the AI needs to read a call-in order to a restaurant."""
    b = request.get_json(force=True) or {}
    found = _order_for_call(b)
    if not found:
        return jsonify({"ok": False, "error": "No order with that number."}), 404
    o, r = found
    if not r:
        return jsonify({"ok": False, "error": "That order has no restaurant on file."}), 404
    cents = lambda c: "$%s" % (("%.2f" % ((c or 0) / 100.0)).rstrip("0").rstrip(".") if (c or 0) % 100
                               else "%d" % ((c or 0) // 100))
    return jsonify({
        "ok": True,
        "code": o["code"],
        "site": {"name": _brand_name(o["restaurant_id"])},
        "restaurant": {"name": r["name"], "phone": (r["phone"] or ""),
                       "ivr_notes": (r["ivr_notes"] or "").strip()},
        "items": _rest_items_lines(o["items"]),
        "total": cents(o["total_cents"]),
        "delivery_address": o["address"] or "",
        "delivery_note": (o["address_note"] or "").strip(),
        "customer_first": (o["customer_name"] or "").split(" ")[0],
        "customer_phone_last4": _digits(o["customer_phone"])[-4:],
        "placed_at": o["created_at"] or "",
        "kitchen_status": o["kitchen_status"] or "",
    })


@voice_bp.post("/api/voice/restaurant_call")
@voice_auth
def voice_restaurant_call():
    """Dispatch asks the voice line to dial the restaurant and read the order aloud."""
    b = request.get_json(force=True) or {}
    found = _order_for_call(b)
    if not found:
        return jsonify({"ok": False, "error": "No order with that number."}), 404
    o, r = found
    if not r:
        return jsonify({"ok": False, "error": "That order has no restaurant on file."}), 404
    phone = _digits(r["phone"])
    if len(phone) < 10:
        return jsonify({"ok": False, "error": "No phone number on file for this restaurant."}), 400
    url, tok = _voice_call_conf()
    if not url:
        return jsonify({"ok": False, "error": "The voice service is not set up yet: set its address in Settings."}), 400
    payload = _json.dumps({"code": o["code"], "restaurant": r["name"]}).encode()
    req = _urllib.Request(url + "/call/restaurant", data=payload, method="POST",
                          headers={"Content-Type": "application/json", "X-Voice-Token": tok})
    try:
        with _urllib.urlopen(req, timeout=15) as resp:
            out = _json.loads(resp.read().decode() or "{}")
    except _urlerr.HTTPError as e:
        try:
            out = _json.loads(e.read().decode() or "{}")
        except Exception:
            out = {}
        if not out.get("error"):
            out = {"error": "The voice service refused the call request."}
    except Exception:
        return jsonify({"ok": False, "error": "Could not reach the voice service."}), 502
    if out.get("ok"):
        A.db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_rest,seen_by_dispatch)
                          VALUES(?,?,?,?,?,0,0)""",
                       (r["id"], "dispatch", "AI voice line",
                        "The AI voice line is calling you with order " + o["code"] + ". "
                        "Answering confirms you have it.", A.now()))
        A.db().commit()
    out["code"] = o["code"]
    return jsonify(out), (200 if out.get("ok") else 400)


@voice_bp.post("/api/voice/order_call_result")
@voice_auth
def voice_order_call_result():
    """The voice service reports what the restaurant said on the call."""
    b = request.get_json(force=True) or {}
    found = _order_for_call(b)
    if not found:
        return jsonify({"ok": False, "error": "No order with that number."}), 404
    o, r = found
    outcome = (b.get("outcome") or "").strip().lower()
    if outcome not in ("accepted", "refused", "unclear"):
        return jsonify({"ok": False, "error": "Outcome must be accepted, refused, or unclear."}), 400
    note = (b.get("note") or "").strip()[:400]
    words = {"accepted": "accepted the order", "refused": "declined the order",
             "unclear": "did not clearly confirm the order"}
    body = "Phone call: " + (r["name"] if r else "restaurant") + " " + words[outcome] + \
           " " + o["code"] + ("." if not note else ". They said: " + note)
    if r:
        A.db().execute("""INSERT INTO rest_messages(restaurant_id,sender,who,body,created_at,seen_by_rest,seen_by_dispatch)
                          VALUES(?,?,?,?,?,1,1)""",
                       (r["id"], "dispatch", "AI voice line", body, A.now()))
    if outcome != "accepted" and r:
        A.raise_call_alert("restaurant", r["name"], r["phone"] or "",
                           "Did not confirm order " + o["code"] + " by phone" + (": " + note if note else ""),
                           restaurant_id=r["id"], order_id=o["id"])
    A.db().commit()
    return jsonify({"ok": True, "code": o["code"], "outcome": outcome})
