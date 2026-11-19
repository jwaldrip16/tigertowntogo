"""Third-party courier deliveries: Uber Direct and DoorDash Drive.

The board books a courier for an order when no driver is free. Keys live in
Dispatch settings (owner only), same pattern as the PayPal keys. Demo mode
simulates both providers end to end (quote, booking, status updates) so the
flow can be tried with no accounts at all.

Uber Direct: OAuth2 client credentials, then quotes + deliveries under
/v1/customers/{customer_id}.  DoorDash Drive: HS256 JWT (developer id, key
id, signing secret), then /drive/v2/quotes + /drive/v2/deliveries.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid as _uuid

import datetime as dt

PROVIDER_LABEL = {"uber": "Uber Direct", "doordash": "DoorDash"}

# Provider status -> the short label the board pill shows.
STATUS_LABEL = {
    "booked": "courier booked",
    "en_route_to_pickup": "heading to the restaurant",
    "at_pickup": "at the restaurant",
    "arrived_at_pickup": "at the restaurant",
    "picked_up": "picked up",
    "en_route_to_dropoff": "on the way",
    "at_dropoff": "at the customer",
    "arrived_at_dropoff": "at the customer",
    "dropped_off": "delivered",
    "completed": "delivered",
    "delivered": "delivered",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "returned": "cancelled",
}

# Demo run: courier booked -> picked up at 6 min -> delivered at 18 min.
DEMO_PICKUP_MIN = 6
DEMO_DELIVER_MIN = 18


class CourierError(Exception):
    pass


def status_label(st):
    st = (st or "").strip()
    return STATUS_LABEL.get(st, st or "booked")


def phone_e164(s):
    digits = "".join(c for c in (s or "") if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def _post_json(url, headers, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, (json.loads(raw) if raw.strip() else {})
            except ValueError:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            d = json.loads(raw)
            msg = d.get("message") or d.get("error_description") or d.get("error") or raw[:160]
            if isinstance(msg, dict):
                msg = json.dumps(msg)[:200]
        except ValueError:
            msg = raw[:160]
        raise CourierError("%s (HTTP %s)" % (msg or "the courier rejected the request", e.code))
    except CourierError:
        raise
    except Exception as e:  # network trouble, DNS, timeouts
        raise CourierError(str(e))


def _demo_quote(pickup, dropoff):
    seed = int(hashlib.md5(((pickup.get("address") or "") + "|" + (dropoff or "")).encode()).hexdigest()[:8], 16)
    return {"ok": True, "fee_cents": 699 + (seed % 500), "eta_min": 24 + (seed % 17), "demo": True}


# ------------------------------------------------------------------ Uber Direct

UBER_TOKEN_URL = "https://login.uber.com/oauth/v2/token"
UBER_API = "https://api.uber.com/v1/customers"


def _uber_headers(cfg):
    form = "&".join([
        "grant_type=client_credentials",
        "client_id=" + cfg["uber_client_id"],
        "client_secret=" + cfg["uber_client_secret"],
        "scope=eats.deliveries",
    ])
    req = urllib.request.Request(UBER_TOKEN_URL, data=form.encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:160]
        raise CourierError("Uber Direct sign-in failed (HTTP %s) %s" % (e.code, raw))
    except Exception as e:
        raise CourierError(str(e))
    tok = (d.get("access_token") or "").strip()
    if not tok:
        raise CourierError("Uber Direct sign-in gave no access token.")
    return {"Authorization": "Bearer " + tok}


def uber_quote(cfg, pickup, dropoff):
    h = _uber_headers(cfg)
    st, d = _post_json(UBER_API + "/" + cfg["uber_customer_id"] + "/quotes", h,
                       {"pickup_address": pickup.get("address") or "", "dropoff_address": dropoff or ""})
    fee = int(d.get("fee") or d.get("fee_total") or 0)
    dur = int(d.get("duration") or 0)
    return {"ok": True, "fee_cents": fee, "eta_min": 20 + dur // 60, "demo": False}


def uber_book(cfg, pickup, dropoff, items_note, customer_name, customer_phone, ref_hint):
    h = _uber_headers(cfg)
    st, q = _post_json(UBER_API + "/" + cfg["uber_customer_id"] + "/quotes", h,
                       {"pickup_address": pickup.get("address") or "", "dropoff_address": dropoff or ""})
    body = {
        "quote_id": q.get("quote_id") or q.get("id") or "",
        "pickup_location": {"address": pickup.get("address") or ""},
        "dropoff_location": {"address": dropoff or ""},
        "pickup_contact": {"name": pickup.get("name") or "Restaurant",
                           "phone": phone_e164(pickup.get("phone"))},
        "dropoff_contact": {"name": customer_name or "Customer", "phone": phone_e164(customer_phone)},
        "manifest_items": [{"name": (items_note or "Food order")[:100], "quantity": 1}],
    }
    st, d = _post_json(UBER_API + "/" + cfg["uber_customer_id"] + "/deliveries", h, body)
    ref = d.get("delivery_id") or d.get("id") or ""
    if not ref:
        raise CourierError("Uber Direct gave no delivery id.")
    return {"ok": True, "ref": ref, "fee_cents": int(d.get("fee") or q.get("fee") or 0),
            "eta_min": 20 + int(d.get("duration") or q.get("duration") or 0) // 60,
            "track_url": d.get("tracking_url") or "", "demo": False}


def uber_cancel(cfg, ref):
    h = _uber_headers(cfg)
    _post_json(UBER_API + "/" + cfg["uber_customer_id"] + "/deliveries/" + ref + "/cancel", h, {})
    return {"ok": True}


# ------------------------------------------------------------------ DoorDash Drive

DD_API = "https://open.doordash.com/drive/v2"


def _dd_headers(cfg):
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "dd-ver": "DD-JWT-V1", "kid": cfg["dd_key_id"]}
    payload = {"aud": "doordash", "iss": cfg["dd_developer_id"], "exp": now + 600, "iat": now}
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    seg = b64(json.dumps(header, separators=(",", ":")).encode()) + "." + \
          b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(cfg["dd_signing_secret"].encode(), seg.encode(), hashlib.sha256).digest()
    token = seg + "." + b64(sig)
    return {"Authorization": "Bearer " + token}


def _dd_fee(d):
    parts = d.get("fee_components")
    if isinstance(parts, list) and parts:
        try:
            return sum(int(p.get("amount", 0)) for p in parts)
        except (TypeError, ValueError):
            pass
    try:
        return int(d.get("fee") or 0)
    except (TypeError, ValueError):
        return 0


def dd_quote(cfg, pickup, dropoff):
    h = _dd_headers(cfg)
    body = {"external_delivery_id": "q-" + _uuid.uuid4().hex[:12],
            "pickup_address": pickup.get("address") or "", "dropoff_address": dropoff or ""}
    st, d = _post_json(DD_API + "/quotes", h, body)
    eta = int(d.get("estimated_pickup_minutes") or 0) + int(d.get("estimated_dropoff_minutes") or 0)
    if not eta:
        eta = int(d.get("estimated_delivery_minutes") or 30)
    return {"ok": True, "fee_cents": _dd_fee(d), "eta_min": max(20, eta), "demo": False}


def dd_book(cfg, pickup, dropoff, items_note, customer_name, customer_phone, ref_hint):
    h = _dd_headers(cfg)
    ref = "ff-" + "".join(c for c in (ref_hint or "").lower() if c.isalnum())[:16] + "-" + _uuid.uuid4().hex[:6]
    body = {
        "external_delivery_id": ref,
        "pickup_address": pickup.get("address") or "",
        "dropoff_address": dropoff or "",
        "pickup_business_name": pickup.get("name") or "Restaurant",
        "pickup_phone_number": phone_e164(pickup.get("phone")),
        "dropoff_phone_number": phone_e164(customer_phone),
        "order_value": 0,
        "items": [{"name": (items_note or "Food order")[:100], "quantity": 1}],
    }
    st, d = _post_json(DD_API + "/deliveries", h, body)
    did = d.get("delivery_id") or ""
    if not did:
        raise CourierError("DoorDash gave no delivery id.")
    return {"ok": True, "ref": did, "fee_cents": _dd_fee(d),
            "eta_min": max(20, int(d.get("estimated_delivery_minutes") or 30)),
            "track_url": d.get("tracking_url") or "", "demo": False}


def dd_cancel(cfg, ref):
    h = _dd_headers(cfg)
    _post_json(DD_API + "/deliveries/" + ref + "/cancel", h, {}, method="PUT")
    return {"ok": True}


# ------------------------------------------------------------------ Common surface

def quote(cfg, provider, pickup, dropoff):
    try:
        if cfg.get("demo"):
            return _demo_quote(pickup, dropoff)
        if provider == "uber":
            return uber_quote(cfg, pickup, dropoff)
        return dd_quote(cfg, pickup, dropoff)
    except CourierError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # anything unexpected still reads as a failed call
        return {"ok": False, "error": "The courier gave no quote: %s" % e}


def book(cfg, provider, pickup, dropoff, items_note="", customer_name="", customer_phone="", ref_hint=""):
    try:
        if cfg.get("demo"):
            r = _demo_quote(pickup, dropoff)
            return {"ok": True, "ref": "demo-" + _uuid.uuid4().hex[:10], "fee_cents": r["fee_cents"],
                    "eta_min": r["eta_min"], "track_url": "", "demo": True}
        if provider == "uber":
            return uber_book(cfg, pickup, dropoff, items_note, customer_name, customer_phone, ref_hint)
        return dd_book(cfg, pickup, dropoff, items_note, customer_name, customer_phone, ref_hint)
    except CourierError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": "The courier could not be booked: %s" % e}


def cancel(cfg, provider, ref):
    try:
        if cfg.get("demo") or (ref or "").startswith("demo-"):
            return {"ok": True}
        if provider == "uber":
            return uber_cancel(cfg, ref)
        return dd_cancel(cfg, ref)
    except CourierError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _find_key(obj, keys):
    """Depth-first search for the first string value of any of these keys."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            hit = _find_key(v, keys)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, keys)
            if hit:
                return hit
    return None


def parse_webhook(provider, payload):
    """Pull (reference, status) out of a webhook body from either provider."""
    ref = _find_key(payload, ("delivery_id", "external_delivery_id", "deliveryId", "id"))
    st = _find_key(payload, ("status", "delivery_status", "deliveryStatus", "event_status"))
    if not st or st not in STATUS_LABEL:
        st = _find_key(payload, ("event_name", "event_type", "type", "eventName"))
    st = (st or "").replace("deliveries.", "").replace("delivery.", "")
    return {"ok": bool(ref and st), "ref": ref, "status": st}


def demo_sweep(con, now):
    """Advance demo couriers: booked -> picked up -> delivered on a clock."""
    rows = con.execute("""SELECT id, courier_ref, courier_booked_at, courier_status, dispatch_status
                          FROM orders WHERE courier_demo=1 AND courier_provider<>''
                          AND dispatch_status NOT IN ('delivered','cancelled')""").fetchall()
    changed = False
    for o in rows:
        if not o["courier_booked_at"]:
            continue
        try:
            t = dt.datetime.fromisoformat(o["courier_booked_at"])
        except ValueError:
            continue
        if t.tzinfo is None and now.tzinfo is not None:
            t = t.replace(tzinfo=now.tzinfo)
        elif t.tzinfo is not None and now.tzinfo is None:
            t = t.replace(tzinfo=None)
        mins = (now - t).total_seconds() / 60.0
        new = None
        if mins >= DEMO_DELIVER_MIN and o["courier_status"] != "delivered":
            new = "delivered"
            con.execute("""UPDATE orders SET courier_status='delivered', dispatch_status='delivered',
                           delivered_at=? WHERE id=? AND dispatch_status NOT IN ('delivered','cancelled')""",
                        (now.isoformat(), o["id"]))
        elif mins >= DEMO_PICKUP_MIN and o["courier_status"] in ("booked", "en_route_to_pickup", "at_pickup"):
            new = "picked_up"
            con.execute("UPDATE orders SET courier_status='picked_up' WHERE id=?", (o["id"],))
        elif mins < DEMO_PICKUP_MIN and o["courier_status"] == "booked":
            new = "en_route_to_pickup"
            con.execute("UPDATE orders SET courier_status='en_route_to_pickup' WHERE id=?", (o["id"],))
        if new:
            changed = True
    if changed:
        con.commit()
