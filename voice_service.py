
# ---------------------------------------------------------------------------
# Fleet Foot voice line (starter): Twilio ConversationRelay + an LLM that
# works the Fleet Foot order system over the /api/voice/ tools.
#
# This runs as its own small service (a second Railway service). Railway
# variables it needs:
#   FLEETFOOT_URL          https://fleetfootdelivery.up.railway.app
#   FLEETFOOT_VOICE_TOKEN  the same token the app's voice_api_token setting holds
#   OPENAI_API_KEY         key for the LLM that talks
#   OPENAI_MODEL           optional, default gpt-4o-mini
#   PUBLIC_URL             https://<this voice service>.up.railway.app
#   VOICE_BRAND_<number digits>  brand for one Twilio number (repeatable)
#   VOICE_DEFAULT_BRAND    fallback brand name
#
# Twilio setup: on each phone number set "A call comes in" to a Webhook:
#   POST https://<this service>/twilio/voice
# ---------------------------------------------------------------------------
import os, json
from flask import Flask, request, Response, jsonify
from flask_sock import Sock
import requests
from urllib.parse import quote as _urlquote
from openai import OpenAI

app = Flask(__name__)
sock = Sock(app)

FLEETFOOT_URL = (os.environ.get("FLEETFOOT_URL") or "").rstrip("/")
TOKEN = (os.environ.get("FLEETFOOT_VOICE_TOKEN") or "").strip()

# Pay by phone (Twilio <Pay> through the Generic Pay Connector to PayPal).
#   PHONE_PAY=1            new call-in orders pay on the call by keypad instead of
#                          transferring to dispatch
#   VOICE_PAY_CONNECTOR    the Generic Pay Connector's unique name in the Twilio console
#   PAY_TEST_MODE=1        accept only Twilio's test cards without touching PayPal
PHONE_PAY_ON = (os.environ.get("PHONE_PAY") or "") == "1"
PAY_CONNECTOR = (os.environ.get("VOICE_PAY_CONNECTOR") or "Default").strip()
PAY_TEST_MODE = (os.environ.get("PAY_TEST_MODE") or "") == "1"
PAY_TX = {}   # transaction_id -> gateway response; Twilio retries reuse the answer
PAY_TEST_CARDS = {"4111111111111111", "5555555555554444", "378282246310005",
                  "6011111111111117", "3065930009020004", "3566002020360505",
                  "6200000000000005", "6771798021000008"}


def _dollars(cents):
    return "%.2f" % (int(cents or 0) / 100.0)
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
DISPATCH_TEXT = os.environ.get("DISPATCH_TEXT") or "(334) 555-0100"
TTS_VOICE = (os.environ.get("VOICE_TTS_VOICE") or "rime.luna").strip()
DISPATCH_NUMBER = (os.environ.get("DISPATCH_NUMBER") or "").strip()   # E.164, e.g. +13345550100
TWILIO_SID = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_TOKEN = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_FROM = (os.environ.get("TWILIO_FROM_NUMBER") or "").strip()   # caller id fallback
CALLS = {}   # callSid -> {"transfer": bool}; survives only this process
_client = OpenAI()   # uses OPENAI_API_KEY


def _tool(name, description, props, req):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": req}}}


MENU_TOOL = _tool("get_menu",
    "Get the restaurants, open status, hours and full menu for this brand.", {}, [])

ADDRESS_TOOL = _tool("check_address",
    "Check a delivery address: distance, delivery fee, whether it is inside the "
    "delivery area. Ask for the full street address and ZIP.",
    {"address": {"type": "string"}}, ["address"])

FIND_TOOL = _tool("find_my_orders",
    "Find this caller's recent orders. Use before changing or reporting on an order.",
    {"code": {"type": "string", "description": "order code, if the caller said one"}}, [])

ITEM_OPTS = {"type": "array", "items": {
    "type": "object",
    "properties": {"group": {"type": "string"}, "name": {"type": "string"}}}}

ITEM = {"type": "array", "items": {
    "type": "object",
    "properties": {
        "menu_item_id": {"type": "integer"},
        "name": {"type": "string"},
        "qty": {"type": "integer"},
        "note": {"type": "string"},
        "options": ITEM_OPTS}}}

PLACE_TOOL = _tool("place_order",
    "Place a delivery order. Confirm items, name, phone and address with the "
    "caller and read the total back before calling.",
    {"restaurant_name": {"type": "string"},
     "items": ITEM,
     "customer_name": {"type": "string"},
     "customer_phone": {"type": "string"},
     "address": {"type": "string"},
     "note": {"type": "string"},
     "tip_cents": {"type": "integer"},
     "dispatch_note": {"type": "string"}},
    ["items", "customer_name", "customer_phone", "address"])

CANCEL_TOOL = _tool("cancel_order",
    "Cancel an order. Only works before it is sent to the kitchen; the API "
    "refuses anything already cooking.",
    {"code": {"type": "string"}}, ["code"])

MODIFY_TOOL = _tool("modify_order",
    "Change an order that is not with the kitchen yet: a new delivery address "
    "or a note on the order.",
    {"code": {"type": "string"}, "address": {"type": "string"},
     "note": {"type": "string"}}, ["code"])

ISSUE_TOOL = _tool("report_issue",
    "Report a problem with an order (wrong item, missing item, late, cold food, "
    "wanting the food remade). Files it to dispatch.",
    {"code": {"type": "string"}, "issue": {"type": "string"},
     "remake": {"type": "boolean"},
     "note": {"type": "string"}}, ["code", "issue"])

REST_CALL_TOOL = _tool("restaurant_issue",
    "The caller is a restaurant calling about an order (a mistake, a missing item, or any other "
    "problem). Report it to dispatch with the restaurant's own order number.",
    {"number": {"type": "string",
                "description": "The restaurant's own order number, like TT01, or the FF number."},
     "issue": {"type": "string"},
     "note": {"type": "string"}}, ["number", "issue"])

DRV_ORDERS_TOOL = _tool("driver_orders",
    "Look up this driver's active orders. The caller's phone number identifies them.", {}, [])

DRV_MSG_TOOL = _tool("driver_message",
    "Send a message to dispatch for this driver (running late, at the door, car trouble).",
    {"body": {"type": "string"}}, ["body"])

DRV_REQ_TOOL = _tool("driver_request",
    "Ask dispatch for this driver to go online, on break, or offline. Dispatch approves it.",
    {"request": {"type": "string", "enum": ["online", "break", "offline"]}}, ["request"])

HANDOFF_TOOL = _tool("handoff_to_dispatch",
    "The caller is angry, confused, or asking for something no tool covers. "
    "With transfer true the caller is dialed through to dispatch right now; "
    "with transfer false dispatch is told to call them back.",
    {"note": {"type": "string"}, "transfer": {"type": "boolean"},
     "summary": {"type": "string",
                 "description": "Short briefing for the dispatcher: what the caller needs "
                                "and the order number if there is one."}}, ["note", "transfer"])

TOOLS = [MENU_TOOL, ADDRESS_TOOL, FIND_TOOL, PLACE_TOOL, CANCEL_TOOL, MODIFY_TOOL,
         ISSUE_TOOL, REST_CALL_TOOL, DRV_ORDERS_TOOL, DRV_MSG_TOOL, DRV_REQ_TOOL, HANDOFF_TOOL]


REST_BRIEF_TOOL = _tool("get_order_brief",
    "Get the order you are calling about: the order number, every item with its options "
    "and notes, the total, and the delivery address. Call this once, before reading anything.",
    {}, [])
REST_CONFIRM_TOOL = _tool("confirm_order",
    "Record what the restaurant decided. accept true when they will make it, false when "
    "they refuse, and use unclear only when they could not answer clearly. note carries "
    "anything they changed or said. eta_minutes is how long until the food is ready; "
    "after they accept, ask them and set it from their answer. This ends the call.",
    {"accept": {"type": "boolean"}, "note": {"type": "string"},
     "eta_minutes": {"type": "integer"}}, ["accept"])

PRESS_TOOL = _tool("press",
    "Press touch tones (DTMF digits) on the call to get through a phone menu. "
    "Press one menu step at a time. Allowed characters: 0-9, * and #; use w for a "
    "half-second wait (ww for a full second).",
    {"digits": {"type": "string"}}, ["digits"])

RESTAURANT_TOOLS = [REST_BRIEF_TOOL, REST_CONFIRM_TOOL, HANDOFF_TOOL, PRESS_TOOL]

END_CALL_TOOL = _tool("end_call",
    "End the call. say carries your last words: a short, friendly goodbye. "
    "Only call this when the caller is done and you have said goodbye in say.",
    {"say": {"type": "string"}}, [])


REST_SYSTEM = (
    "You are the delivery line of the brand %s, calling a restaurant to read them a "
    "delivery order. Be brief and courteous, like a dispatcher reading a ticket.\n"
    "Flow:\n"
    "1. Call get_order_brief, then say: this is the %s delivery line, order <number> "
    "for delivery. <number> is the restaurant order number from the brief (the "
    "primary_no, like TT01); only give the system FF code if they ask for it. Read each "
    "item with its options and any note, then the total, then the delivery address and "
    "the customer's first name.\n"
    "2. Ask: can you take care of this?\n"
    "3. When they accept, ask: about how long until that is ready? Then call confirm_order "
    "with accept true and eta_minutes set to their answer, and confirm you are sending the "
    "order over. If they refuse or want changes, call confirm_order with accept false "
    "or unclear and put what they said in note. Then end politely.\n"
    "Never take card numbers and never offer anything beyond confirming or refusing. "
    "If the person you speak with wants dispatch, say you are connecting them, and call "
    "handoff_to_dispatch with transfer true."
)

REST_IVR = (
    "\nIf the line answers with a phone menu, get through it with the press tool. The "
    "restaurant's saved menu path is: %s. Press one step at a time, then stay quiet and "
    "listen for the next menu prompt or a person. If after a few tries you cannot reach "
    "a person, call confirm_order with accept false and note that you could not get "
    "through the phone menu, then end the call."
)

def restaurant_prompt(brand, ivr="", script=""):
    base = REST_SYSTEM % (brand, brand)
    if (ivr or "").strip():
        base += REST_IVR % ivr.strip()
    s = (script or "").strip()
    if s:
        s = s[:1200].replace("&", "and").replace('"', "").replace("<", "").replace(">", "")
        base += ("\n\nDispatcher script for this call. Follow it as extra wording; if it "
                 "conflicts with the rules above, the rules above win:\n" + s)
    return base


def ff_call(path, body):
    body = dict(body or {})
    r = requests.post(FLEETFOOT_URL + path, json=body,
                      headers={"X-Voice-Token": TOKEN}, timeout=25)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": "The order system did not answer."}


def run_tool(name, args, ctx):
    """One tool call. ctx carries the brand and the caller's number."""
    site = ctx["brand"]
    caller = ctx["caller"]
    if name == "end_call":
        return {"ok": True, "end_call": True, "say": (args.get("say") or "").strip()[:300]}
    if name == "press":
        d = "".join(ch for ch in (args.get("digits") or "") if ch in "0123456789w*#")
        if not d:
            return {"ok": False, "error": "No digits to press."}
        w = ctx.get("ws")
        if w is None:
            return {"ok": False, "error": "No open call to press into."}
        try:
            w.send(json.dumps({"type": "sendDigits", "digits": d}))
        except Exception:
            return {"ok": False, "error": "Could not press the digits."}
        return {"ok": True, "pressed": d,
                "listen": "Digits sent. Stay quiet and listen for the next menu prompt or a person."}
    if name == "get_menu":
        return ff_call("/api/voice/menu", {"site": site})
    if name == "check_address":
        return ff_call("/api/voice/validate_address", {"site": site, "address": args.get("address")})
    if name == "find_my_orders":
        return ff_call("/api/voice/find_order", {"code": args.get("code"), "phone": caller})
    if name == "place_order":
        args = dict(args)
        args["site"] = site
        args.setdefault("customer_phone", caller)
        try:
            tip = float(args.pop("tip_dollars", 0) or 0)
        except (TypeError, ValueError):
            tip = 0.0
        if tip < 0:
            tip = 0.0
        if PHONE_PAY_ON:
            args["pay_on_call"] = True
            args["tip_cents"] = int(round(tip * 100))
        out = ff_call("/api/voice/place_order", args)
        if out.get("ok"):
            code = str(out.get("code") or "").replace(" ", "")
            spoken = _spoken_numbers(out, ctx)
            out["end_call"] = True
            if PHONE_PAY_ON and ctx.get("sid"):
                # Pay on the call: the keypad payment line takes the card next.
                CALLS[ctx["sid"]] = {"pay": {"code": code, "brand": ctx.get("brand") or "",
                                             "amount_cents": int(out.get("total_cents") or 0),
                                             "tries": 0}}
                out["say"] = (("Perfect, your order is in, number " + spoken + ". ")
                              if spoken else "Perfect, your order is in. ") + \
                             ("Your total is " + _dollars(out.get("total_cents")) +
                              " with your tip. Stay on the line and enter your card "
                              "details on your keypad when prompted.")
            else:
                # A new order: hand the caller straight to dispatch, who takes the
                # card and the tip. Remakes and changes to an existing order never
                # reach here, so they never transfer.
                if ctx.get("sid"):
                    CALLS[ctx["sid"]] = {"transfer": True, "whisper": _order_whisper(out, ctx)}
                out["say"] = ("Perfect, your order is in, number " + spoken +
                              ". I am connecting you with dispatch now to take "
                              "your payment and tip.") if spoken else \
                             ("Perfect, your order is in. I am connecting you "
                              "with dispatch now to take your payment and tip.")
        return out
    if name == "cancel_order":
        return ff_call("/api/voice/cancel_order", {"code": args.get("code"), "phone": caller})
    if name == "modify_order":
        return ff_call("/api/voice/modify_order",
                       {"code": args.get("code"), "phone": caller,
                        "address": args.get("address"), "note": args.get("note")})
    if name == "report_issue":
        return ff_call("/api/voice/report_issue",
                       {"code": args.get("code"), "phone": caller,
                        "issue": args.get("issue"),
                        "remake": bool(args.get("remake")),
                        "note": args.get("note")})
    if name == "restaurant_issue":
        return ff_call("/api/voice/restaurant_issue",
                       {"number": args.get("number"), "issue": args.get("issue"),
                        "note": args.get("note")})
    if name == "driver_orders":
        return ff_call("/api/voice/driver_orders", {"phone": caller})
    if name == "driver_message":
        return ff_call("/api/voice/driver_message", {"phone": caller, "body": args.get("body")})
    if name == "driver_request":
        return ff_call("/api/voice/driver_request", {"phone": caller, "request": args.get("request")})
    if name == "get_order_brief":
        return ff_call("/api/voice/order_brief", {"code": ctx.get("order")})
    if name == "confirm_order":
        out = ff_call("/api/voice/order_call_result",
                      {"code": ctx.get("order"),
                       "outcome": "accepted" if args.get("accept") else
                                  ("unclear" if str(args.get("note") or "").strip().lower().startswith("unclear") else "refused"),
                       "note": args.get("note") or ""})
        out["end_call"] = True
        out.setdefault("say", "Thanks, I am sending that over. Have a good day.")
        return out
    if name == "handoff_to_dispatch":
        if args.get("transfer") and ctx.get("sid"):
            CALLS[ctx["sid"]] = {"transfer": True,
                                 "whisper": args.get("summary") or ""}
            return {"ok": True, "end_call": True,
                    "say": "I understand, and I am sorry. Connecting you with dispatch now, "
                           "one moment."}
        return {"ok": True,
                "say": "I understand, and I am sorry. Dispatch will call you back at "
                       "this number shortly. Is there anything else while you wait?"}
    return {"ok": False, "error": "Unknown action."}


def dispatch_number_for(to_number):
    """One dispatch line per Twilio number, DISPATCH_NUMBER as the fallback."""
    digits = "".join(ch for ch in (to_number or "") if ch.isdigit())
    return (os.environ.get("DISPATCH_NUMBER_" + digits) or DISPATCH_NUMBER or "").strip()


def brand_for(to_number):
    digits = "".join(ch for ch in (to_number or "") if ch.isdigit())
    return (os.environ.get("VOICE_BRAND_" + digits)
            or os.environ.get("VOICE_DEFAULT_BRAND") or "Fleet Foot Delivery")


@app.route("/twilio/voice", methods=["GET", "POST"])
def twilio_voice():
    to = request.values.get("To", "")
    brand = brand_for(to)
    ws = PUBLIC_URL + "/media?brand=" + brand.replace(" ", "%20")
    greeting = "Thank you for calling " + brand + ". How can I help you today?"
    twiml = ('<Response><Connect action="/twilio/after"><ConversationRelay url="' + ws + '" '
             'welcomeGreeting="' + greeting + '" '
             'ttsLanguage="en-US" voice="' + TTS_VOICE + '" '
             'transcriptionLanguage="en-US"/></Connect></Response>')
    return Response(twiml, mimetype="text/xml")


@app.get("/")
def health():
    return {"ok": True, "fleet_foot_set": bool(FLEETFOOT_URL and TOKEN)}


def payment_rule(pay):
    """The payment paragraph of the customer prompt."""
    if pay:
        return ("- After the caller confirms the order, ask how much they would like "
                "to tip: whole dollars, zero is fine. Pass it as tip_dollars in "
                "place_order. Never ask for card numbers, expiration dates, zip codes, "
                "or security codes; the keypad prompts collect those right after the "
                "call. Once the order is placed, say the total with the tip included "
                "and tell the caller they will enter their card on their keypad next. "
                "Remakes and changes to an existing order never take a tip or a "
                "payment. ")
    return ("- Payment and the tip are taken by dispatch, so never ask for card "
            "numbers or a tip. The moment a new order is placed, the call transfers "
            "to dispatch automatically so they can take the card and the tip; just "
            "tell the caller dispatch is picking up. Remakes and changes to an "
            "existing order do not transfer. ")


def system_prompt(brand, pay=False):
    return (
        "You are the phone agent for %s, a food delivery service. You talk with "
        "callers, place and change delivery orders, take driver check-ins, and report "
        "problems to dispatch. Speak in short plain sentences, one thought at a time.\n"
        "Rules:\n"
        "- Call get_menu first so you only offer real menu items and prices. Never invent items.\n"
        "- Before placing an order: confirm the items and the delivery address back to the caller, then read the total back and get a clear yes.\n"
        "%s" +
        "- Only offer a remake, a credit, or a refund if a tool says it is allowed. A remake is only possible within 15 minutes of delivery; after that the API refuses it, so say a remake is not possible this late and connect the caller with dispatch. The API, not you, decides what is allowed.\n"
        "- If a tool refuses (for example the order is already with the kitchen), explain that simply and offer to send a message to dispatch with report_issue.\n"        "- If the caller says they are a restaurant calling about an order, ask for their order number (their own number like TT01, or the FF number) and what the problem is, then report it with restaurant_issue. Do not take or change an order with them.\n"
        "- If the caller is a driver, use the driver tools; status changes go to dispatch for approval.\n"
        "- If the caller is angry, confused, or wants something no tool covers, say you are connecting them to a person, call handoff_to_dispatch, and tell them dispatch will call back at %s."
    ) % (brand, payment_rule(pay), DISPATCH_TEXT)


@app.route("/twilio/outbound", methods=["GET", "POST"])
def twilio_outbound():
    """TwiML for the outbound call to a restaurant: the AI is in restaurant mode."""
    code = request.values.get("code", "")
    ivr = request.values.get("ivr", "").replace("&", "and").replace('"', "").replace("<", "").replace(">", "")
    script = request.values.get("script", "").replace("&", "and").replace('"', "").replace("<", "").replace(">", "")
    ws = PUBLIC_URL + "/media"
    twiml = ('<Response><Connect action="/twilio/after">'
             '<Parameter name="mode" value="restaurant"/>'
             '<Parameter name="order" value="' + code.replace('"', '') + '"/>'
             '<Parameter name="ivr" value="' + ivr + '"/>'
             '<Parameter name="script" value="' + script + '"/>'
             '<ConversationRelay url="' + ws + '" ttsLanguage="en-US" voice="' + TTS_VOICE + '" '
             'transcriptionLanguage="en-US"/></Connect></Response>')
    return Response(twiml, mimetype="text/xml")


@app.post("/call/restaurant")
def call_restaurant():
    """Dial a restaurant with the order. Called by the Fleet Foot app (X-Voice-Token)."""
    if not TOKEN or (request.headers.get("X-Voice-Token") or "").strip() != TOKEN:
        return {"ok": False, "error": "Bad voice token."}, 403
    _body = request.get_json(silent=True) or {}
    code = _body.get("code", "")
    script = (_body.get("script") or "").strip()[:1200]
    if not code:
        return {"ok": False, "error": "Say which order to call about."}, 400
    brief = ff_call("/api/voice/order_brief", {"code": code})
    if not brief.get("ok"):
        return {"ok": False, "error": brief.get("error") or "Could not load the order."}, 400
    phone = "".join(ch for ch in (brief.get("restaurant", {}).get("phone") or "") if ch.isdigit())
    if len(phone) < 10:
        return {"ok": False, "error": "The restaurant has no phone number on file."}, 400
    if not (TWILIO_SID and TWILIO_TOKEN):
        return {"ok": False, "error": "Twilio is not set up on the voice service."}, 400
    to = "+1" + phone[-10:]
    brand = (brief.get("site") or {}).get("name") or ""
    brand_name = (brief.get("restaurant", {}).get("name") or "").strip().lower()
    from_number = TWILIO_FROM
    for var, val in os.environ.items():
        if var.startswith("VOICE_BRAND_") and val.strip().lower() == brand_name:
            from_number = "+1" + var[len("VOICE_BRAND_"):]
            break
    r = requests.post("https://api.twilio.com/2010-04-01/Accounts/%s/Calls.json" % TWILIO_SID,
                      auth=(TWILIO_SID, TWILIO_TOKEN),
                      data={"To": to, "From": from_number,
                            "Url": PUBLIC_URL + "/twilio/outbound?code=" + code + "&brand=" + _urlquote(brand)
                                   + "&ivr=" + _urlquote((brief.get("restaurant", {}).get("ivr_notes") or ""))
                                   + ("&script=" + _urlquote(script) if script else "")},
                      timeout=25)
    if r.status_code >= 300:
        return {"ok": False, "error": "Twilio refused the call: " + (r.json().get("message") or r.text[:120])}, 400
    return {"ok": True, "sid": r.json().get("sid")}


def pay_twiml(pay, retry=False):
    """The card-entry TwiML: Twilio asks for the card, expiry, zip and security code."""
    code = (pay.get("code") or "").replace('"', "")
    brand = (pay.get("brand") or "").replace('"', "")
    amount = _dollars(pay.get("amount_cents"))
    head = ("Your total is " + amount + " dollars, tip included. "
            "Enter your card information on your keypad when prompted."
            if not retry else
            "The card did not go through. Let us try once more. "
            "Enter your card information on your keypad when prompted.")
    return ('<Response><Say>' + head + '</Say>'
            '<Pay chargeAmount="' + amount + '" paymentConnector="' + PAY_CONNECTOR +
            '" action="/twilio/pay-result">'
            '<Parameter name="code" value="' + code + '"/>'
            '<Parameter name="brand" value="' + brand + '"/>'
            '</Pay></Response>')


def ff_get(path, params):
    try:
        r = requests.get(FLEETFOOT_URL + path, params=params,
                         headers={"X-Voice-Token": TOKEN}, timeout=(5, 9))
        return r.json()
    except Exception:
        return {"ok": False}


def _clean_spoken(t):
    """Keep a briefing safe to put inside TwiML and pleasant to hear."""
    return ((t or "").replace("&", "and").replace('"', "").replace("<", "")
            .replace(">", "").strip())


def _spoken_numbers(out, ctx):
    """The order number(s) read to the customer, per the dispatch setting."""
    ff = str(out.get("code") or "").replace(" ", "")
    pr = str(out.get("primary_no") or "").replace(" ", "")
    ffs = " ".join(ff)
    prs = " ".join(pr)
    mode = ctx.get("num_mode") or "both"
    if prs and mode == "primary":
        return prs
    if prs and mode == "both":
        return prs + ", system number " + ffs
    return ffs


def _order_whisper(out, ctx):
    """What dispatch hears before a brand-new caller connects: the order summary."""
    code = _clean_spoken(str(out.get("code") or ""))[:20]
    brand = _clean_spoken(ctx.get("brand") or "")[:40]
    total = _dollars(out.get("total_cents"))
    pr = _clean_spoken(str(out.get("primary_no") or ""))[:20]
    txt = "New order" + (" for " + brand if brand else "")
    if pr:
        txt += ", order number " + " ".join(pr) + ", system " + " ".join(code) \
            + ", total " + total + ". Take the payment and the tip."
    elif code:
        txt += ", order number " + code + ", total " + total + ". Take the payment and the tip."
    else:
        txt += ", total " + total + "."
    return txt


def _dial_inner(target, whisper):
    """A Dial that lets dispatch hear the briefing before the caller connects."""
    if whisper and PUBLIC_URL:
        url = PUBLIC_URL + "/twilio/whisper?w=" + _urlquote(_clean_spoken(whisper)[:400])
        return '<Dial><Number url="' + url + '">' + target + '</Number></Dial>'
    return '<Dial>' + target + '</Dial>'


@app.route("/twilio/whisper", methods=["GET", "POST"])
def twilio_whisper():
    """The warm-transfer briefing Twilio plays to dispatch before connecting the caller."""
    text = _clean_spoken(request.values.get("w", ""))[:400]
    if not text:
        return Response('<Response><Hangup/></Response>', mimetype="text/xml")
    return Response('<Response><Say>' + text + '</Say></Response>', mimetype="text/xml")


@app.route("/twilio/after", methods=["POST"])
def twilio_after():
    """Runs when the AI session ends: a pay-by-phone call runs the card prompts,
    a marked call dials dispatch, anything else hangs up."""
    sid = request.values.get("CallSid", "")
    state = CALLS.pop(sid, None)
    if state and state.get("pay") and PAY_CONNECTOR:
        CALLS[sid] = state          # /twilio/pay-result still needs the order
        return Response(pay_twiml(state["pay"]), mimetype="text/xml")
    target = dispatch_number_for(request.values.get("To", ""))
    if state and state.get("transfer") and target:
        return Response('<Response>' + _dial_inner(target, state.get("whisper")) +
                        '</Response>', mimetype="text/xml")
    return Response('<Response><Hangup/></Response>', mimetype="text/xml")


@app.route("/twilio/pay", methods=["GET", "POST"])
def twilio_pay():
    """Re-runs the card prompts after a failed attempt."""
    pay = (CALLS.get(request.values.get("CallSid", "")) or {}).get("pay") or {}
    if not pay:
        return Response('<Response><Hangup/></Response>', mimetype="text/xml")
    return Response(pay_twiml(pay, retry=True), mimetype="text/xml")


@app.route("/twilio/pay-result", methods=["POST"])
def twilio_pay_result():
    """Twilio reports the keypad payment result here."""
    sid = request.values.get("CallSid", "")
    pay = (CALLS.get(sid) or {}).get("pay") or {}
    ref = (request.values.get("PayConnector_charge_ref")
           or request.values.get("PayConnector_charge_id") or "")
    if request.values.get("PaymentStatus") == "success" and pay.get("code"):
        ff_call("/api/voice/mark_paid",
                {"code": pay["code"], "ref": ref,
                 "amount_cents": int(pay.get("amount_cents") or 0)})
        if PUBLIC_URL:
            ws = (PUBLIC_URL + "/media?brand=" + _urlquote(pay.get("brand") or "")
                  + "&pay_done=" + _urlquote(pay.get("code") or ""))
            return Response('<Response><Connect action="/twilio/after">'
                            '<ConversationRelay url="' + ws + '" '
                            'ttsLanguage="en-US" voice="' + TTS_VOICE + '"'
                            '/></Connect></Response>', mimetype="text/xml")
        return Response('<Response><Say>Thank you. Your order is confirmed and paid. '
                        'Your driver will be on the way soon.</Say><Hangup/></Response>',
                        mimetype="text/xml")
    target = dispatch_number_for(request.values.get("To", ""))
    if pay:
        tries = int(pay.get("tries") or 0) + 1
        pay["tries"] = tries
        CALLS[sid] = {"pay": pay}
        if tries < 2:
            return Response('<Response><Redirect method="POST">/twilio/pay</Redirect>'
                            '</Response>', mimetype="text/xml")
    if target:
        code = _clean_spoken(str(pay.get("code") or ""))[:20]
        brief = ("The card failed twice on the keypad for order " + code +
                 ". They need payment help.") if code else \
                "The card failed twice on the keypad."
        return Response('<Response><Say>The card did not go through. I am connecting '
                        'you with dispatch, one moment.</Say>' + _dial_inner(target, brief) +
                        '</Response>', mimetype="text/xml")
    return Response('<Response><Say>The card did not go through. Dispatch will call '
                    'you back shortly to finish the payment.</Say><Hangup/></Response>',
                    mimetype="text/xml")


@app.post("/gateway/pay")
def gateway_pay():
    """The Generic Pay Connector's endpoint: charge the card through PayPal.

    Twilio posts the card details here; they go straight through to the order's
    own brand PayPal keys and are never written to disk or to a log. Twilio
    retries within 10 seconds reusing the same transaction_id, so the answer is
    remembered instead of charging twice.
    """
    b = request.get_json(force=True, silent=True) or {}
    params = b.get("parameters") or {}
    tx = str(b.get("transaction_id") or "")

    def done(resp):
        if tx:
            PAY_TX[tx] = resp
        return jsonify(resp)

    if tx and tx in PAY_TX:
        return jsonify(PAY_TX[tx])
    if str(b.get("method") or "") != "charge":
        return done({"charge_id": None, "error_code": "unsupported",
                     "error_message": "Only charge transactions are supported."})
    code = str(params.get("code") or "").strip()
    if not code:
        return done({"charge_id": None, "error_code": "missing_order",
                     "error_message": "No order code was passed."})
    card = str(b.get("cardnumber") or "").replace(" ", "")
    if PAY_TEST_MODE:
        if card in PAY_TEST_CARDS:
            cid = "TEST-" + (tx[:12] or "test")
            return done({"charge_id": cid, "error_code": None, "error_message": None,
                         "parameters": {"code": code, "charge_ref": cid}})
        return done({"charge_id": None, "error_code": "test_card",
                     "error_message": "Test mode accepts only Twilio's test cards."})
    keys = ff_get("/api/voice/pp_keys", {"code": code})
    if not keys.get("ok") or not (keys.get("client") and keys.get("secret")):
        return done({"charge_id": None, "error_code": "no_processor",
                     "error_message": "This brand's PayPal card keys are not set up."})
    base = keys.get("base") or "https://api-m.paypal.com"
    try:
        tok = requests.post(base + "/v1/oauth2/token",
                            auth=(keys["client"], keys["secret"]),
                            data={"grant_type": "client_credentials"}, timeout=(5, 9))
        tok.raise_for_status()
        at = tok.json().get("access_token") or ""
        hdr = {"Authorization": "Bearer " + at}
        expiry = (str(b.get("expiry_year") or "").zfill(4)[-4:] + "-" +
                  str(b.get("expiry_month") or "").zfill(2))
        order = requests.post(
            base + "/v2/checkout/orders", headers=hdr,
            json={"intent": "CAPTURE",
                  "purchase_units": [{"reference_id": code[:127],
                                      "description": str(b.get("description") or "delivery order")[:127],
                                      "amount": {"value": "%.2f" % float(b.get("amount") or 0),
                                                 "currency_code": str(b.get("currency_code") or "USD")}}],
                  "payment_source": {"card": {"number": card,
                                              "security_code": str(b.get("cvv") or ""),
                                              "expiry": expiry,
                                              "billing_address": {
                                                  "postal_code": str(b.get("postal_code") or ""),
                                                  "country_code": "US"}}}},
            timeout=(5, 9))
        if order.status_code >= 400:
            j = order.json() or {}
            msg = ""
            for iss in (j.get("details") or []):
                msg = iss.get("description") or msg
                break
            return done({"charge_id": None, "error_code": "declined",
                         "error_message": (msg or j.get("message") or "The card was declined.")[:200]})
        oid = (order.json() or {}).get("id") or ""
        cap = requests.post(base + "/v2/checkout/orders/" + oid + "/capture",
                            headers=hdr, json={}, timeout=(5, 9))
        cj = cap.json() or {}
        caps = ((cj.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or []
        if cap.status_code >= 400 or not caps or caps[0].get("status") not in ("COMPLETED", "PENDING"):
            msg = ""
            for iss in (cj.get("details") or []):
                msg = iss.get("description") or msg
                break
            return done({"charge_id": None, "error_code": "declined",
                         "error_message": (msg or "PayPal did not complete the charge.")[:200]})
        cid = caps[0].get("id") or oid
        return done({"charge_id": cid, "error_code": None, "error_message": None,
                     "parameters": {"code": code, "charge_ref": cid}})
    except Exception:
        return done({"charge_id": None, "error_code": "gateway",
                     "error_message": "The payment gateway could not be reached."})


@sock.route("/media")
def media(ws):
    brand = request.args.get("brand") or os.environ.get("VOICE_DEFAULT_BRAND") or "Fleet Foot Delivery"
    pay_done = (request.args.get("pay_done") or "").strip()
    ctx = {"brand": brand, "caller": "", "mode": "customer", "order": "", "ivr": "", "ws": ws}
    history = [{"role": "system", "content": system_prompt(brand, PHONE_PAY_ON)}]
    try:
        _sc = ff_get("/api/voice/script", {})
        _cs = (_sc.get("voice_customer_script") or "").strip() if _sc.get("ok") else ""
    except Exception:
        _cs = ""
    ctx["num_mode"] = ((_sc.get("voice_customer_number") or "") if _sc.get("ok") else "").strip()
    if ctx["num_mode"] not in ("primary", "ff"):
        ctx["num_mode"] = "both"
    if _cs:
        history[0]["content"] += ("\n\nExtra wording the owner wants on this line. Follow it as "
                                  "wording; if it conflicts with the rules above, the rules above "
                                  "win:\n" + _cs[:1200])
    tools = TOOLS
    if pay_done:
        history[0]["content"] += (
            "\n\nThis caller just paid for their order on the keypad and you have already "
            "confirmed it. Ask once whether there is anything else you can get them. If they "
            "want something more, treat it as a new order with your tools (it is paid for the "
            "same way). If they do not, say a short goodbye and call the end_call tool right "
            "away; do not keep them on the line.")
        tools = TOOLS + [END_CALL_TOOL]
    while True:
        try:
            raw = ws.receive(timeout=300)
        except Exception:
            return
        if raw is None:
            return
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mtype = msg.get("type")
        if mtype == "setup":
            ctx["caller"] = msg.get("from") or msg.get("caller") or ""
            ctx["sid"] = msg.get("callSid") or msg.get("callSID") or msg.get("sid") or ""
            CALLS[ctx["sid"]] = {"transfer": False}
            if pay_done:
                _say(ws, "Your payment went through and your order is confirmed and paid. "
                         "Is there anything else I can get for you today?")
            cp = msg.get("customParameters") or msg.get("custom_parameters") or {}
            if isinstance(cp, dict):
                if (cp.get("mode") or "") == "restaurant":
                    ctx["mode"] = "restaurant"
                    ctx["order"] = (cp.get("order") or "").strip()
                    ctx["brand"] = (cp.get("brand") or "").strip() or brand
                    ctx["ivr"] = (cp.get("ivr") or "").strip()
                    ctx["script"] = (cp.get("script") or "").strip()
                    history = [{"role": "system", "content": restaurant_prompt(ctx["brand"], ctx["ivr"], ctx.get("script", ""))}]
                    tools = RESTAURANT_TOOLS
        elif mtype == "prompt":
            voice_prompt = msg.get("voicePrompt") or msg.get("utterance") or ""
            if not voice_prompt.strip():
                continue
            history.append({"role": "user", "content": voice_prompt})
            # the LLM loop: it may call tools and then answer
            for _ in range(4):
                try:
                    resp = _client.chat.completions.create(
                        model=MODEL, messages=history, tools=tools, temperature=0.4)
                except Exception:
                    _say(ws, "Sorry, one moment.")
                    return
                choice = resp.choices[0]
                if choice.finish_reason == "tool_calls":
                    history.append(choice.message)
                    for tc in choice.message.tool_calls:
                        try:
                            out = run_tool(tc.function.name,
                                           json.loads(tc.function.arguments or "{}"), ctx)
                        except Exception:
                            out = {"ok": False, "error": "The order system did not answer."}
                        if isinstance(out, dict) and out.get("end_call"):
                            # A tool that ends the call speaks its own line, then
                            # the stream closes and Twilio runs the action URL
                            # (Dial to dispatch for a transfer, else Hangup).
                            _say(ws, out.get("say") or "Thanks for calling. Goodbye.")
                            st = CALLS.get(ctx.get("sid")) or {}
                            if not (st.get("transfer") or st.get("pay")):
                                CALLS.pop(ctx.get("sid"), None)
                            return
                        history.append({"role": "tool", "tool_call_id": tc.id,
                                        "content": json.dumps(out)[:4000]})
                    continue
                reply = choice.message.content or ""
                history.append({"role": "assistant", "content": reply})
                _say(ws, reply)
                break
        elif mtype == "interrupt":
            pass   # the caller talked over us; their next prompt replaces the tail


def _say(ws, text):
    if not text:
        return
    try:
        ws.send(json.dumps({"type": "text", "token": text, "last": True}))
    except Exception:
        pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
