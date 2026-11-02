
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
from flask import Flask, request, Response
from flask_sock import Sock
import requests
from urllib.parse import quote as _urlquote
from openai import OpenAI

app = Flask(__name__)
sock = Sock(app)

FLEETFOOT_URL = (os.environ.get("FLEETFOOT_URL") or "").rstrip("/")
TOKEN = (os.environ.get("FLEETFOOT_VOICE_TOKEN") or "").strip()
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
DISPATCH_TEXT = os.environ.get("DISPATCH_TEXT") or "(334) 555-0100"
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
    "Report a problem with an order (wrong item, missing item, late, cold food). "
    "Files it to dispatch.",
    {"code": {"type": "string"}, "issue": {"type": "string"},
     "note": {"type": "string"}}, ["code", "issue"])

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
    {"note": {"type": "string"}, "transfer": {"type": "boolean"}}, ["note", "transfer"])

TOOLS = [MENU_TOOL, ADDRESS_TOOL, FIND_TOOL, PLACE_TOOL, CANCEL_TOOL, MODIFY_TOOL,
         ISSUE_TOOL, DRV_ORDERS_TOOL, DRV_MSG_TOOL, DRV_REQ_TOOL, HANDOFF_TOOL]


REST_BRIEF_TOOL = _tool("get_order_brief",
    "Get the order you are calling about: the order number, every item with its options "
    "and notes, the total, and the delivery address. Call this once, before reading anything.",
    {}, [])
REST_CONFIRM_TOOL = _tool("confirm_order",
    "Record what the restaurant decided. accept true when they will make it, false when "
    "they refuse, and use unclear only when they could not answer clearly. note carries "
    "anything they changed or said. This ends the call.",
    {"accept": {"type": "boolean"}, "note": {"type": "string"}}, ["accept"])

PRESS_TOOL = _tool("press",
    "Press touch tones (DTMF digits) on the call to get through a phone menu. "
    "Press one menu step at a time. Allowed characters: 0-9, * and #; use w for a "
    "half-second wait (ww for a full second).",
    {"digits": {"type": "string"}}, ["digits"])

RESTAURANT_TOOLS = [REST_BRIEF_TOOL, REST_CONFIRM_TOOL, HANDOFF_TOOL, PRESS_TOOL]

REST_SYSTEM = (
    "You are the delivery line of the brand %s, calling a restaurant to read them a "
    "delivery order. Be brief and courteous, like a dispatcher reading a ticket.\n"
    "Flow:\n"
    "1. Call get_order_brief, then say: this is the %s delivery line, order <number> "
    "for delivery. Read each item with its options and any note, then the total, then the "
    "delivery address and the customer's first name.\n"
    "2. Ask: can you take care of this?\n"
    "3. When they accept, confirm you are sending the order over and call confirm_order "
    "with accept true. If they refuse or want changes, call confirm_order with accept false "
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

def restaurant_prompt(brand, ivr=""):
    base = REST_SYSTEM % (brand, brand)
    if (ivr or "").strip():
        base += REST_IVR % ivr.strip()
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
        out = ff_call("/api/voice/place_order", args)
        if out.get("ok"):
            # A new order: hand the caller straight to dispatch, who takes the
            # card and the tip. Remakes and changes to an existing order never
            # reach here, so they never transfer.
            if ctx.get("sid"):
                CALLS[ctx["sid"]] = {"transfer": True}
            code = str(out.get("code") or "").replace(" ", "")
            spoken = " ".join(code) if code else ""
            out["end_call"] = True
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
                        "issue": args.get("issue"), "note": args.get("note")})
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
            CALLS[ctx["sid"]] = {"transfer": True}
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
             'ttsLanguage="en-US" voice="rime.jorah" '
             'transcriptionLanguage="en-US"/></Connect></Response>')
    return Response(twiml, mimetype="text/xml")


@app.get("/")
def health():
    return {"ok": True, "fleet_foot_set": bool(FLEETFOOT_URL and TOKEN)}


def system_prompt(brand):
    return (
        "You are the phone agent for %s, a food delivery service. You talk with "
        "callers, place and change delivery orders, take driver check-ins, and report "
        "problems to dispatch. Speak in short plain sentences, one thought at a time.\n"
        "Rules:\n"
        "- Call get_menu first so you only offer real menu items and prices. Never invent items.\n"
        "- Before placing an order: confirm the items and the delivery address back to the caller, then read the total back and get a clear yes.\n"
        "- Payment and the tip are taken by dispatch, so never ask for card numbers or a tip. The moment a new order is placed, the call transfers to dispatch automatically so they can take the card and the tip; just tell the caller dispatch is picking up. Remakes and changes to an existing order do not transfer.\n"
        "- Only offer a remake, a credit, or a refund if a tool says it is allowed. The API, not you, decides what is allowed.\n"
        "- If a tool refuses (for example the order is already with the kitchen), explain that simply and offer to send a message to dispatch with report_issue.\n"
        "- If the caller is a driver, use the driver tools; status changes go to dispatch for approval.\n"
        "- If the caller is angry, confused, or wants something no tool covers, say you are connecting them to a person, call handoff_to_dispatch, and tell them dispatch will call back at %s."
    ) % (brand, DISPATCH_TEXT)


@app.route("/twilio/outbound", methods=["GET", "POST"])
def twilio_outbound():
    """TwiML for the outbound call to a restaurant: the AI is in restaurant mode."""
    code = request.values.get("code", "")
    ivr = request.values.get("ivr", "").replace("&", "and").replace('"', "").replace("<", "").replace(">", "")
    ws = PUBLIC_URL + "/media"
    twiml = ('<Response><Connect action="/twilio/after">'
             '<Parameter name="mode" value="restaurant"/>'
             '<Parameter name="order" value="' + code.replace('"', '') + '"/>'
             '<Parameter name="ivr" value="' + ivr + '"/>'
             '<ConversationRelay url="' + ws + '" ttsLanguage="en-US" voice="rime.jorah" '
             'transcriptionLanguage="en-US"/></Connect></Response>')
    return Response(twiml, mimetype="text/xml")


@app.post("/call/restaurant")
def call_restaurant():
    """Dial a restaurant with the order. Called by the Fleet Foot app (X-Voice-Token)."""
    if not TOKEN or (request.headers.get("X-Voice-Token") or "").strip() != TOKEN:
        return {"ok": False, "error": "Bad voice token."}, 403
    code = (request.get_json(silent=True) or {}).get("code", "")
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
                                   + "&ivr=" + _urlquote((brief.get("restaurant", {}).get("ivr_notes") or ""))},
                      timeout=25)
    if r.status_code >= 300:
        return {"ok": False, "error": "Twilio refused the call: " + (r.json().get("message") or r.text[:120])}, 400
    return {"ok": True, "sid": r.json().get("sid")}


@app.route("/twilio/after", methods=["POST"])
def twilio_after():
    """Runs when the AI session ends. A marked call dials dispatch; anything else hangs up."""
    sid = request.values.get("CallSid", "")
    state = CALLS.pop(sid, None)
    target = dispatch_number_for(request.values.get("To", ""))
    if state and state.get("transfer") and target:
        return Response('<Response><Dial>' + target + '</Dial></Response>',
                        mimetype="text/xml")
    return Response('<Response><Hangup/></Response>', mimetype="text/xml")


@sock.route("/media")
def media(ws):
    brand = request.args.get("brand") or os.environ.get("VOICE_DEFAULT_BRAND") or "Fleet Foot Delivery"
    ctx = {"brand": brand, "caller": "", "mode": "customer", "order": "", "ivr": "", "ws": ws}
    history = [{"role": "system", "content": system_prompt(brand)}]
    tools = TOOLS
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
            cp = msg.get("customParameters") or msg.get("custom_parameters") or {}
            if isinstance(cp, dict):
                if (cp.get("mode") or "") == "restaurant":
                    ctx["mode"] = "restaurant"
                    ctx["order"] = (cp.get("order") or "").strip()
                    ctx["brand"] = (cp.get("brand") or "").strip() or brand
                    ctx["ivr"] = (cp.get("ivr") or "").strip()
                    history = [{"role": "system", "content": restaurant_prompt(ctx["brand"], ctx["ivr"])}]
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
