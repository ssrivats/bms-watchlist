"""
BMS Watchlist Backend – Hybrid (Final Version)
Compatible with your Chrome Extension
"""

import json, logging, os, re, threading, time, uuid, requests
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger(__name__)

TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
_local_store = {}

if REDIS_URL:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
    except:
        pass

MAX_WATCHLIST_PER_PHONE = 10

THEATERS = {
    "PVSR": {"name": "PVR: Sathyam"},
    "PVES": {"name": "HDFC Millennia PVR: Express Avenue"},
    "PVPZ": {"name": "PVR: Palazzo"},
}

BMS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def _save(watch_id, data):
    if _redis:
        _redis.set(f"watch:{watch_id}", json.dumps(data), ex=172800)
    else:
        _local_store[watch_id] = data

def _load(watch_id):
    if _redis:
        raw = _redis.get(f"watch:{watch_id}")
        return json.loads(raw) if raw else None
    return _local_store.get(watch_id)

def _log(watch_id, msg, kind="info"):
    item = _load(watch_id)
    if not item: return
    item.setdefault("logs", []).append({"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind})
    _save(watch_id, item)
    log.info("[%s] %s", watch_id, msg)

def _slugify(name):
    s = name.lower()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    return s.strip('-')

def _smart_interval(minutes_away):
    if minutes_away < 30: return 5
    if minutes_away < 120: return 30
    if minutes_away < 240: return 120
    return 1800

def _parse_seat_layout_api(data):
    try:
        categories = data.get("data", {}).get("categories") or data.get("categories", [])
        available = []
        for cat in categories:
            name = cat.get("name", "").lower()
            seats = int(cat.get("availableSeats") or 0)
            price = float(cat.get("price") or 0)
            if "elite" in name and seats > 0:
                available.append({"name": cat.get("name"), "price": price})
        if available:
            return {"found": True, "available": available}
    except:
        pass
    return {"found": False}

# Hybrid seeding (Playwright once)
def _seed_session(event_code, movie_slug, watch_id):
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=BMS_UA)
            page = context.new_page()

            seat_payloads = []
            def handle(response):
                if "seatlayout" in response.url.lower():
                    seat_payloads.append({"url": response.url, "json": response.json()})

            page.on("response", handle)

            page.goto(f"https://in.bookmyshow.com/movies/chennai/{movie_slug}/{event_code}", timeout=60000)
            page.wait_for_timeout(15000)   # wait for React

            page.locator('text=/Book tickets|Buy tickets/i').first.click()
            page.wait_for_timeout(15000)

            cookies = {c["name"]: c["value"] for c in context.cookies()}
            headers = {"User-Agent": BMS_UA}

            if seat_payloads:
                _log(watch_id, "✅ Session seeded successfully", "start")
                return {
                    "cookies": cookies,
                    "headers": headers,
                    "seatlayout_url": seat_payloads[0]["url"]
                }
    except Exception as e:
        _log(watch_id, f"Seeding failed: {e}", "error")
    return None

# Poll with requests
def _poll_seats(session_data):
    try:
        resp = requests.get(
            session_data["seatlayout_url"],
            cookies=session_data["cookies"],
            headers=session_data["headers"],
            timeout=15
        )
        if resp.status_code == 200:
            return _parse_seat_layout_api(resp.json())
    except:
        pass
    return {"found": False}

# Alert
def _send_alert(watch_id, movie_title, phone, found_seats):
    if not phone.startswith("+"):
        phone = f"+91{phone}"
    msg = f"🎬 *Back seats opened!*\n\n*{movie_title}*\n\n"
    for s in found_seats[:3]:
        msg += f"📍 PVR Chennai\n🕐 Soon\n💺 {s['name']} ₹{int(s['price'])}\n\n"
    msg += "_BMS Watchlist_"

    if TWILIO_SID and TWILIO_TOKEN:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=msg, from_=TWILIO_FROM, to=f"whatsapp:{phone}")

# Monitoring thread
def _run_monitor(watch_id):
    item = _load(watch_id)
    if not item: return

    event_code = item["eventCode"]
    phone = item["phone"]
    movie_title = item["movie"]
    movie_slug = item.get("movieSlug") or _slugify(movie_title)

    if not item.get("session_data"):
        session = _seed_session(event_code, movie_slug, watch_id)
        if not session:
            item["status"] = "error"
            _save(watch_id, item)
            return
        item["session_data"] = session
        _save(watch_id, item)

    item["status"] = "monitoring"
    _save(watch_id, item)

    while True:
        item = _load(watch_id)
        if item["status"] != "monitoring":
            break

        result = _poll_seats(item["session_data"])

        if result.get("found"):
            found_seats = result["available"]
            _send_alert(watch_id, movie_title, phone, found_seats)
            item["status"] = "alert_sent"
            _save(watch_id, item)
            break

        time.sleep(30)

@app.route("/api/watch", methods=["POST"])
def add_watch():
    data = request.json or {}
    phone = data.get("phone")
    event_code = data.get("eventCode")
    movie = data.get("movie")

    if not phone or not event_code or not movie:
        return jsonify({"error": "Missing fields"}), 400

    watch_id = str(uuid.uuid4())[:8]
    item = {
        "id": watch_id,
        "movie": movie,
        "movieSlug": _slugify(movie),
        "eventCode": event_code,
        "phone": phone,
        "status": "starting",
        "added_at": datetime.now().isoformat(),
        "logs": []
    }
    _save(watch_id, item)

    threading.Thread(target=_run_monitor, args=(watch_id,), daemon=True).start()

    return jsonify({"watch_id": watch_id, "status": "started"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)