"""
BMS Watchlist Backend – HYBRID (Final)
Playwright seeds session ONCE → lightweight requests polling
Fully compatible with your Chrome Extension
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

# ── Config ───────────────────────────────────────────────────────────────────
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
        log.info("Redis connected ✓")
    except Exception as e:
        log.warning("Redis unavailable — using in-memory store")

MAX_WATCHLIST_PER_PHONE = 10

THEATERS = {
    "PVSR": {"name": "PVR: Sathyam", "slug": "pvr-sathyam-royapettah", "city": "chennai"},
    "PVES": {"name": "HDFC Millennia PVR: Express Avenue", "slug": "hdfc-millennia-pvr-escape-express-avenue-mall", "city": "chennai"},
    "PVPZ": {"name": "PVR: Palazzo", "slug": "pvr-palazzo-the-nexus-vijaya-mall", "city": "chennai"},
}

BMS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── Storage ──────────────────────────────────────────────────────────────────
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

def _load_all():
    if _redis:
        result = {}
        for key in _redis.keys("watch:*"):
            raw = _redis.get(key)
            if raw:
                d = json.loads(raw)
                result[d["id"]] = d
        return result
    return dict(_local_store)

def _log(watch_id, msg, kind="info"):
    item = _load(watch_id)
    if not item: return
    item.setdefault("logs", []).append({"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind})
    item["logs"] = item["logs"][-80:]
    _save(watch_id, item)
    log.info("[%s] %s", watch_id, msg)

# ── Helpers ──────────────────────────────────────────────────────────────────
def _watch_dates():
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    return [today, tomorrow] if now.hour >= 19 else [today]

def _smart_interval(minutes_away):
    if minutes_away < 30:   return 5
    if minutes_away < 120:  return 30
    if minutes_away < 240:  return 120
    return 1800

def _parse_show_time(time_str, date_str):
    try:
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
        if not m: return None
        h, mn, ap = int(m[1]), int(m[2]), m[3].upper()
        if ap == "PM" and h != 12: h += 12
        if ap == "AM" and h == 12: h = 0
        y, mo, d = date_str[:4], date_str[4:6], date_str[6:]
        return datetime.strptime(f"{y}-{mo}-{d} {h:02d}:{mn:02d}", "%Y-%m-%d %H:%M")
    except: return None

def _slugify(name):
    s = name.lower()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    return s.strip('-')

# ── Seat parser ──────────────────────────────────────────────────────────────
def _parse_seat_layout_api(data, show_time):
    try:
        categories = data.get("data", {}).get("categories") or data.get("categories", [])
        available = []
        for cat in categories:
            name = cat.get("name") or cat.get("description") or ""
            seats = int(cat.get("availableSeats") or cat.get("availSeat") or 0)
            price = float(cat.get("price") or cat.get("curPrice") or 0)
            if seats > 0:
                available.append({"name": name, "price": price, "availableSeats": seats})
        if available:
            return {"found": True, "shows": [{"time": show_time, "availableCats": sorted(available, key=lambda x: x.get("price", 0))}]}
    except: pass
    return {"found": False}

# ── Hybrid seeding (Playwright once) ────────────────────────────────────────
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
                    seat_payloads.append({"url": response.url, "json": response.json(), "headers": dict(response.headers)})

            page.on("response", handle)
            page.goto(f"https://in.bookmyshow.com/movies/chennai/{movie_slug}/{event_code}", wait_until="domcontentloaded")
            page.wait_for_timeout(10000)

            page.locator("text=/Book tickets|Buy tickets/i").first.click()
            page.wait_for_timeout(12000)

            cookies = {c["name"]: c["value"] for c in context.cookies()}
            headers = {"User-Agent": BMS_UA, "Referer": page.url}

            if seat_payloads:
                session_data = {
                    "cookies": cookies,
                    "headers": headers,
                    "seatlayout_url": seat_payloads[0]["url"]
                }
                _log(watch_id, "✅ Session seeded successfully", "start")
                return session_data
            else:
                _log(watch_id, "⚠️ No seatlayout captured during seeding", "warn")
                return None
    except Exception as e:
        _log(watch_id, f"Session seeding failed: {e}", "error")
        return None

# ── Lightweight polling ─────────────────────────────────────────────────────
def _poll_with_requests(session_data):
    try:
        cookies = session_data["cookies"]
        headers = session_data["headers"]
        url = session_data["seatlayout_url"]

        resp = requests.get(url, cookies=cookies, headers=headers, timeout=15)
        if resp.status_code == 200:
            return _parse_seat_layout_api(resp.json(), "")
        return {"found": False}
    except:
        return {"found": False}

# ── WhatsApp alert ───────────────────────────────────────────────────────────
def _send_watchlist_alert(watch_id, movie_title, phone, found_seats):
    if not phone.startswith("+"): phone = f"+91{phone}"
    msg = f"🎬 *Back seats just opened!*\n\n*{movie_title}*\n\n"
    for s in found_seats[:3]:
        price_str = f"₹{int(s['seat_price'])}" if s.get("seat_price") else ""
        msg += f"📍 {s['theater']}\n🕐 {s['date_label']} · {s['time']}\n💺 {s['seat_name']} {price_str}\n👉 {s.get('booking_url','')}\n\n"
    if len(found_seats) > 3:
        msg += f"...and {len(found_seats)-3} more\n\n"
    msg += "_BMS Watchlist Alert_"

    if not TWILIO_SID or not TWILIO_TOKEN:
        return
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=msg, from_=TWILIO_FROM, to=f"whatsapp:{phone}")

# ── Monitoring thread ────────────────────────────────────────────────────────
def _run_watchlist_monitor(watch_id):
    item = _load(watch_id)
    if not item: return

    event_code = item["eventCode"]
    phone = item["phone"]
    movie_title = item["movie"]
    movie_slug = item.get("movieSlug") or _slugify(movie_title)

    item["status"] = "monitoring"
    _save(watch_id, item)
    _log(watch_id, f"Started watching '{movie_title}'", "start")

    # Seed session once
    if not item.get("session_data"):
        session_data = _seed_session(event_code, movie_slug, watch_id)
        if not session_data:
            item["status"] = "error"
            _save(watch_id, item)
            return
        item["session_data"] = session_data
        _save(watch_id, item)

    poll_count = 0
    while True:
        item = _load(watch_id)
        if item["status"] != "monitoring": break

        poll_count += 1
        now = datetime.now()
        dates = _watch_dates()
        found_seats = []
        next_intervals = []

        for date in dates:
            for venue_code in ["PVSR", "PVES", "PVPZ"]:
                result = _poll_with_requests(item["session_data"])
                if not result.get("found"): continue

                shows = result.get("shows", [])
                date_label = "Today" if date == now.strftime("%Y%m%d") else "Tomorrow"

                for show in shows:
                    show_dt = _parse_show_time(show["time"], date)
                    if not show_dt: continue

                    mins_away = (show_dt - now).total_seconds() / 60
                    next_intervals.append(_smart_interval(mins_away))

                    available = show.get("availableCats", [])
                    matching = [cat for cat in available if "elite" in cat.get("name", "").lower()]
                    if not matching: continue

                    seat = sorted(matching, key=lambda x: x.get("price", 0))[0]

                    found_seats.append({
                        "theater": THEATERS[venue_code]["name"],
                        "date_label": date_label,
                        "time": show["time"],
                        "seat_name": seat.get("name", "ELITE"),
                        "seat_price": seat.get("price", 0),
                        "booking_url": ""
                    })

        if found_seats:
            item["status"] = "alert_sent"
            _save(watch_id, item)
            _send_watchlist_alert(watch_id, movie_title, phone, found_seats)
            break

        status_msg = f"Poll #{poll_count}: no ELITE seats yet"
        item["last_checked"] = now.strftime("%H:%M:%S")
        item["last_result"] = status_msg
        _save(watch_id, item)
        _log(watch_id, status_msg, "poll")

        wait = min(next_intervals) if next_intervals else 1800
        _log(watch_id, f"Next check in {wait} seconds", "info")
        time.sleep(wait)

# ── Flask Routes (exactly what your extension expects) ───────────────────────
@app.route("/")
def home(): return "BMS Watchlist Hybrid running ✓", 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "redis": bool(_redis), "twilio": bool(TWILIO_SID and TWILIO_TOKEN)})

@app.route("/api/watch", methods=["POST"])
def add_watch():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    event_code = data.get("eventCode", "").strip()
    movie = data.get("movie", "").strip()

    if not phone or not event_code or not movie:
        return jsonify({"error": "phone, eventCode and movie required"}), 400

    if not phone.startswith("+"): phone = f"+91{phone}"

    all_items = _load_all()
    active = [i for i in all_items.values() if i.get("phone") == phone and i.get("status") == "monitoring"]
    if len(active) >= MAX_WATCHLIST_PER_PHONE:
        return jsonify({"error": f"Already watching {len(active)} movies"}), 429

    duplicate = next((i for i in active if i.get("eventCode") == event_code), None)
    if duplicate:
        return jsonify({"error": "Already watching this movie", "watch_id": duplicate["id"]}), 409

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

    threading.Thread(target=_run_watchlist_monitor, args=(watch_id,), daemon=True).start()

    return jsonify({"watch_id": watch_id, "status": "started"})

@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    phone = request.args.get("phone", "").strip()
    if not phone.startswith("+"): phone = f"+91{phone}"
    all_items = _load_all()
    items = [i for i in all_items.values() if i.get("phone") == phone]
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/watch/<watch_id>/stop", methods=["POST"])
def stop_watch(watch_id):
    item = _load(watch_id)
    if not item: return jsonify({"error": "Not found"}), 404
    item["status"] = "stopped"
    _save(watch_id, item)
    _log(watch_id, "Stopped by user", "stop")
    return jsonify({"status": "stopped"})

@app.route("/api/test-whatsapp", methods=["POST"])
def test_whatsapp():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    if not phone.startswith("+"): phone = f"+91{phone}"
    if not TWILIO_SID or not TWILIO_TOKEN:
        return jsonify({"error": "Twilio not configured"}), 500
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(
        body="👋 *BMS Watchlist Test*\n\n✅ Alerts are working!\n_BMS Watchlist_",
        from_=TWILIO_FROM, to=f"whatsapp:{phone}"
    )
    return jsonify({"status": "sent", "sid": msg.sid})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)