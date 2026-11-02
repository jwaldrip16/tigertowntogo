
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
from openai import OpenAI

app = Flask(__name__)
sock = Sock(app)

FLEETFOOT_URL = (os.environ.get("FLEETFOOT_URL") or "").rstrip("/")
TOKEN = (os.environ.get("FLEETFOOT_VOICE_TOKEN") or "").strip()
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
DISPATCH_TEXT = os.environ.get("DISPATCH_TEXT") or "(334) 555-0100"
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
    "Tells dispatch to call them back.",
    {"note": {"type": "string"}}, ["note"])

TOOLS = [MENU_TOOL, ADDRESS_TOOL, FIND_TOOL, PLACE_TOOL, CANCEL_TOOL, MODIFY_TOOL,
         ISSUE_TOOL, DRV_ORDERS_TOOL, DRV_MSG_TOOL, DRV_REQ_TOOL, HANDOFF_TOOL]


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
        return ff_call("/api/voice/place_order", args)
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
    if name == "handoff_to_dispatch":
        return {"ok": True,
                "say": "I understand, and I am sorry. Dispatch will call you back at "
                       "this number shortly. Is there anything else while you wait?"}
    return {"ok": False, "error": "Unknown action."}


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
    twiml = ('<Response><Connect><ConversationRelay url="' + ws + '" '
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
        "- Payment is taken by dispatch over the phone or on delivery, so never ask for card numbers.\n"
        "- Only offer a remake, a credit, or a refund if a tool says it is allowed. The API, not you, decides what is allowed.\n"
        "- If a tool refuses (for example the order is already with the kitchen), explain that simply and offer to send a message to dispatch with report_issue.\n"
        "- If the caller is a driver, use the driver tools; status changes go to dispatch for approval.\n"
        "- If the caller is angry, confused, or wants something no tool covers, say you are connecting them to a person, call handoff_to_dispatch, and tell them dispatch will call back at %s."
    ) % (brand, DISPATCH_TEXT)


@sock.route("/media")
def media(ws):
    brand = request.args.get("brand") or os.environ.get("VOICE_DEFAULT_BRAND") or "Fleet Foot Delivery"
    ctx = {"brand": brand, "caller": ""}
    history = [{"role": "system", "content": system_prompt(brand)}]
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
        elif mtype == "prompt":
            voice_prompt = msg.get("voicePrompt") or msg.get("utterance") or ""
            if not voice_prompt.strip():
                continue
            history.append({"role": "user", "content": voice_prompt})
            # the LLM loop: it may call tools and then answer
            for _ in range(4):
                try:
                    resp = _client.chat.completions.create(
                        model=MODEL, messages=history, tools=TOOLS, temperature=0.4)
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
