"""
BMS Watchlist Backend
─────────────────────
Add movies you want to watch → backend monitors Sathyam, HDFC, Palazzo
→ WhatsApp you the moment back seats open.

Smart polling (updated per your spec):
  < 30 min     → every 5 seconds
  30 min–2 hr   → every 30 seconds
  2–4 hrs       → every 2 minutes
  > 4 hrs       → every 30 minutes

Dates monitored: Always today. If added after 7 PM → also tomorrow.
"""

import json, logging, os, re, threading, time, uuid
from datetime import datetime, timedelta
from collections import defaultdict

from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
_local_store = {}   # fallback

if REDIS_URL:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
        log.info("Redis connected ✓")
    except Exception as e:
        log.warning("Redis unavailable (%s) — using in-memory store", e)
        _redis = None

MAX_WATCHLIST_PER_PHONE = 10

# ── Hardcoded theaters ───────────────────────────────────────────────────────

THEATERS = {
    "PVSR": {"name": "PVR: Sathyam", "slug": "pvr-sathyam-royapettah", "city": "chennai"},
    "PVES": {"name": "HDFC Millennia PVR: Express Avenue", "slug": "hdfc-millennia-pvr-escape-express-avenue-mall", "city": "chennai"},
    "PVPZ": {"name": "PVR: Palazzo", "slug": "pvr-palazzo-the-nexus-vijaya-mall", "city": "chennai"},
}

BMS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TARGET_CATEGORIES = ["elite"]

# ── Slugify helper (used for movie page URL) ─────────────────────────────────

def _slugify(name: str) -> str:
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    return s.strip('-')

# ── Storage helpers (unchanged) ──────────────────────────────────────────────

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
                try:
                    d = json.loads(raw)
                    result[d["id"]] = d
                except Exception:
                    pass
        return result
    return dict(_local_store)

def _log(watch_id, msg, kind="info"):
    item = _load(watch_id)
    if not item:
        return
    item.setdefault("logs", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "msg": msg,
        "kind": kind,
    })
    item["logs"] = item["logs"][-80:]
    _save(watch_id, item)
    log.info("[%s] %s", watch_id, msg)

# ── Date helpers (unchanged) ─────────────────────────────────────────────────

def _watch_dates():
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    return [today, tomorrow] if now.hour >= 19 else [today]

def _parse_show_time(time_str, date_str):
    try:
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
        if not m:
            return None
        h, mn, ap = int(m[1]), int(m[2]), m[3].upper()
        if ap == "PM" and h != 12:
            h += 12
        if ap == "AM" and h == 12:
            h = 0
        y, mo, d = date_str[:4], date_str[4:6], date_str[6:]
        return datetime.strptime(f"{y}-{mo}-{d} {h:02d}:{mn:02d}", "%Y-%m-%d %H:%M")
    except Exception:
        return None

# ── UPDATED smart interval per your spec ─────────────────────────────────────

def _smart_interval(minutes_away):
    """Return poll interval in seconds (even for <30 min)."""
    if minutes_away < 30:
        return 5          # every 5 seconds
    if minutes_away < 120:
        return 30         # every 30 seconds
    if minutes_away < 240:
        return 120        # every 2 minutes
    return 1800           # every 30 minutes

# ── Seat layout parser (unchanged) ───────────────────────────────────────────

def _parse_seat_layout_api(data, event_code, venue_code, date, session_id, show_time, booking_url):
    try:
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        categories = (
            payload.get("categories")
            or payload.get("areas")
            or data.get("categories", [])
            or data.get("areas", [])
        )

        available = []
        for category in categories:
            name = (
                category.get("name")
                or category.get("description")
                or category.get("areaDesc")
                or ""
            )
            seats = (
                category.get("availableSeats")
                or category.get("availSeat")
                or category.get("availabilityCount")
                or 0
            )
            price = category.get("price") or category.get("curPrice") or 0

            try:
                seats = int(seats)
            except Exception:
                seats = 0

            try:
                price = float(price)
            except Exception:
                price = 0

            if seats <= 0:
                continue

            available.append({
                "name": name,
                "price": price,
                "availableSeats": seats,
            })

        return {
            "found": True,
            "shows": [{
                "time": show_time,
                "sessionId": session_id,
                "availableCats": sorted(available, key=lambda cat: cat.get("price", 0)),
                "bookingUrl": booking_url,
            }],
        }
    except Exception as e:
        return {"found": False, "reason": str(e)}

# ── FIXED core check function – full navigation flow ─────────────────────────

def _check_movie_at_theater(event_code, venue_code, date, page, watch_id=None, movie_slug=None):
    """Debug version with screenshot when button is missing."""
    if not movie_slug:
        movie_slug = _slugify(_load(watch_id)["movie"]) if watch_id else ""

    url = f"https://in.bookmyshow.com/movies/chennai/{movie_slug}/{event_code}"

    seat_payloads = []
    clicked_times = []

    try:
        def handle_response(response):
            try:
                if "seatlayout" in response.url.lower():
                    if watch_id:
                        _log(watch_id, f"API HIT: {response.url}", "debug")
                    data = response.json()
                    seat_payloads.append(data)
            except Exception:
                pass

        page.on("response", handle_response)

        if watch_id:
            _log(watch_id, f"→ Opening Chennai movie page: {url}", "debug")

        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(8000)

        page.evaluate("window.scrollBy(0, 400)")
        page.wait_for_timeout(3000)

        book_btn = page.locator("button").filter(
            has_text=re.compile(r"book tickets|buy tickets", re.I)
        ).first

        try:
            book_btn.wait_for(state="visible", timeout=15000)
            if watch_id:
                _log(watch_id, "✅ Book tickets button found", "debug")
        except Exception:
            if watch_id:
                _log(watch_id, "❌ Button still not visible - saving screenshot", "error")
                try:
                    page.screenshot(path=f"debug_{watch_id}_{int(time.time())}.png")
                    _log(watch_id, f"Screenshot saved: debug_{watch_id}_*.png", "error")
                except Exception as e:
                    _log(watch_id, f"Failed to save screenshot: {e}", "error")
            return {"found": False, "reason": "book_button_missing"}

        if watch_id:
            _log(watch_id, "✅ Clicking Book tickets", "debug")

        book_btn.click()
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(6000)

        buttons = page.locator("button").filter(has_text=re.compile(r"\d{1,2}:\d{2}\s*(AM|PM)", re.I))
        count = buttons.count()

        if watch_id:
            _log(watch_id, f"Found {count} showtime button(s)", "debug")

        for i in range(min(count, 3)):
            try:
                button = buttons.nth(i)
                show_time_text = button.inner_text().strip()
                clicked_times.append(show_time_text)

                if watch_id:
                    _log(watch_id, f"Clicking show index {i} → {show_time_text}", "debug")

                button.click()
                page.wait_for_load_state("networkidle", timeout=10000)
                page.wait_for_timeout(7000)

                page.go_back()
                page.wait_for_timeout(3000)
            except Exception as e:
                if watch_id:
                    _log(watch_id, f"Click failed index {i}: {e}", "warn")
                continue

        if not seat_payloads:
            if watch_id:
                _log(watch_id, "❌ No seatlayout API captured", "error")
            return {"found": False, "reason": "no_api_data"}

        merged_shows = []
        for data, show_time in zip(seat_payloads, clicked_times):
            parsed = _parse_seat_layout_api(data, event_code, venue_code, None, "unknown", show_time, "")
            if parsed.get("found"):
                merged_shows.extend(parsed.get("shows", []))

        if merged_shows:
            return {"found": True, "shows": merged_shows}

        if watch_id:
            _log(watch_id, "No ELITE seats found", "debug")
        return {"found": False, "reason": "no_seats"}

    except Exception as e:
        if watch_id:
            _log(watch_id, f"Navigation error: {e}", "error")
        return {"found": False, "reason": str(e)[:80]}
    finally:
        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass

# ── Monitoring thread (polling updated + movie_slug passed) ───────────────────

def _run_watchlist_monitor(watch_id):
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    item = _load(watch_id)
    if not item:
        return

    event_code = item["eventCode"]
    phone = item["phone"]
    movie_title = item["movie"]
    movie_slug = item.get("movieSlug") or _slugify(movie_title)

    item["status"] = "monitoring"
    _save(watch_id, item)
    _log(watch_id, f"Started watching '{movie_title}' ({event_code})", "start")

    p = None
    browser = None
    context = None

    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=BMS_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        page = context.new_page()
        page.route("**/*.{png,jpg,gif,svg,woff,woff2,ttf,ico}", lambda r: r.abort())

        poll_count = 0

        while True:
            item = _load(watch_id)
            if not item or item["status"] not in ("monitoring", "starting"):
                _log(watch_id, "Stopped", "stop")
                break

            poll_count += 1
            now = datetime.now()
            dates = _watch_dates()

            found_seats = []
            next_intervals = []

            for date in dates:
                for venue_code in ["PVSR", "PVES", "PVPZ"]:
                    try:
                        result = _check_movie_at_theater(
                            event_code, venue_code, date, page,
                            watch_id=watch_id,
                            movie_slug=movie_slug
                        )
                    except PwTimeout:
                        _log(watch_id, f"Timeout on venue {venue_code}/{date}", "warn")
                        continue
                    except Exception as e:
                        _log(watch_id, f"Error on venue {venue_code}/{date}: {e}", "warn")
                        continue

                    if not result.get("found"):
                        continue

                    shows = result.get("shows", [])
                    date_label = "Today" if date == now.strftime("%Y%m%d") else "Tomorrow"

                    for show in shows:
                        show_dt = _parse_show_time(show["time"], date)
                        if not show_dt:
                            continue

                        mins_away = (show_dt - now).total_seconds() / 60

                        interval = _smart_interval(mins_away)
                        next_intervals.append(interval)

                        available = show.get("availableCats", [])
                        if not available:
                            continue

                        # Filter ELITE (existing logic)
                        matching = [
                            cat for cat in available
                            if "elite" in (cat.get("name") or "").lower()
                        ]

                        if not matching:
                            continue

                        seat = sorted(matching, key=lambda x: x.get("price", 0))[0]

                        found_seats.append({
                            "theater": THEATERS[venue_code]["name"],
                            "venue_code": venue_code,
                            "date_label": date_label,
                            "time": show["time"],
                            "seat_name": seat.get("name", "Available category"),
                            "seat_price": seat.get("price", 0),
                            "booking_url": show.get("bookingUrl", ""),
                        })

            if found_seats:
                item = _load(watch_id)
                item["status"] = "alert_sent"
                item["last_result"] = f"Seats found at {len(found_seats)} show(s)"
                _save(watch_id, item)
                _send_watchlist_alert(watch_id, movie_title, phone, found_seats)
                break

            status_msg = f"Poll #{poll_count}: no ELITE seats yet"
            item = _load(watch_id)
            item["last_checked"] = now.strftime("%H:%M:%S")
            item["last_result"] = status_msg
            _save(watch_id, item)
            _log(watch_id, status_msg, "poll")

            wait = min(next_intervals) if next_intervals else 1800
            _log(watch_id, f"Next check in {wait} seconds", "info")
            time.sleep(wait)

    except Exception as e:
        item = _load(watch_id)
        if item:
            item["status"] = "error"
            item["last_error"] = str(e)[:120]
            _save(watch_id, item)
        _log(watch_id, f"Fatal error: {e}", "error")

    finally:
        try:
            if context: context.close()
        except: pass
        try:
            if browser: browser.close()
        except: pass
        try:
            if p: p.stop()
        except: pass

# ── WhatsApp alert & API routes (unchanged except new movieSlug on creation) ─────

def _send_watchlist_alert(watch_id, movie_title, phone, found_seats):
    # ... (exactly the same as original)
    if not phone.startswith("+"):
        phone = f"+91{phone}"

    msg = f"🎬 *Back seats just opened!*\n\n*{movie_title}*\n\n"
    for s in found_seats[:3]:
        price_str = f"₹{int(s['seat_price'])}" if s["seat_price"] else ""
        msg += (
            f"📍 {s['theater']}\n"
            f"🕐 {s['date_label']} · {s['time']}\n"
            f"💺 {s['seat_name']} {price_str}\n"
            f"👉 {s['booking_url']}\n\n"
        )
    if len(found_seats) > 3:
        msg += f"...and {len(found_seats) - 3} more show(s)\n\n"
    msg += "_BMS Watchlist Alert_"

    _log(watch_id, f"Sending WhatsApp to {phone[:6]}****", "alert")

    if not TWILIO_SID or not TWILIO_TOKEN:
        _log(watch_id, "Twilio not configured — alert skipped", "warn")
        return

    to = f"whatsapp:{phone}"
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    for attempt in range(3):
        try:
            msg_obj = client.messages.create(body=msg, from_=TWILIO_FROM, to=to)
            _log(watch_id, f"✅ WhatsApp sent SID={msg_obj.sid}", "alert")
            return
        except Exception as e:
            wait = 2 ** attempt
            _log(watch_id, f"Twilio attempt {attempt+1} failed: {e} — retry in {wait}s", "warn")
            if attempt < 2:
                time.sleep(wait)
    _log(watch_id, "All WhatsApp attempts failed", "error")


@app.route("/")
def home():
    return "BMS Watchlist running (fixed navigation ✓)", 200

@app.route("/health")
def health():
    # ... (unchanged)
    status = {
        "status": "ok",
        "service": "bms-watchlist",
        "redis": bool(_redis),
        "twilio": bool(TWILIO_SID and TWILIO_TOKEN),
        "time": datetime.now().isoformat(),
    }
    if _redis:
        try:
            _redis.ping()
            status["redis_connected"] = True
        except:
            status["redis_connected"] = False
    return jsonify(status), 200


@app.route("/api/watch", methods=["POST"])
def add_watch():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    event_code = data.get("eventCode", "").strip()
    movie = data.get("movie", "").strip()

    if not phone or not event_code or not movie:
        return jsonify({"error": "phone, eventCode and movie are required"}), 400

    if not phone.startswith("+"):
        phone = f"+91{phone}"

    all_items = _load_all()
    active = [i for i in all_items.values() if i.get("phone") == phone and i.get("status") == "monitoring"]
    if len(active) >= MAX_WATCHLIST_PER_PHONE:
        return jsonify({"error": f"You're already watching {len(active)} movies. Remove one first."}), 429

    duplicate = next((i for i in active if i.get("eventCode") == event_code), None)
    if duplicate:
        return jsonify({"error": "Already watching this movie", "watch_id": duplicate["id"]}), 409

    watch_id = str(uuid.uuid4())[:8]
    item = {
        "id": watch_id,
        "movie": movie,
        "movieSlug": _slugify(movie),          # ← NEW
        "eventCode": event_code,
        "phone": phone,
        "status": "starting",
        "added_at": datetime.now().isoformat(),
        "added_hour": datetime.now().hour,
        "last_checked": None,
        "last_result": None,
        "last_error": None,
        "alert_sent": False,
        "logs": [],
    }
    _save(watch_id, item)

    thread = threading.Thread(target=_run_watchlist_monitor, args=(watch_id,), daemon=True)
    thread.start()

    return jsonify({
        "watch_id": watch_id,
        "movie": movie,
        "eventCode": event_code,
        "status": "started",
        "theaters": list(THEATERS.keys()),
        "dates": _watch_dates(),
    })


# The rest of the routes (get_watchlist, get_watch, stop_watch, test_whatsapp) are unchanged
# (they already work with the new movieSlug field)

@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "phone required"}), 400
    if not phone.startswith("+"):
        phone = f"+91{phone}"

    all_items = _load_all()
    items = [
        {
            "id": i["id"],
            "movie": i["movie"],
            "eventCode": i["eventCode"],
            "status": i["status"],
            "added_at": i["added_at"],
            "last_checked": i.get("last_checked"),
            "last_result": i.get("last_result"),
            "alert_sent": i.get("alert_sent", False),
        }
        for i in all_items.values()
        if i.get("phone") == phone
    ]
    items.sort(key=lambda x: x["added_at"], reverse=True)
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/watch/<watch_id>", methods=["GET"])
def get_watch(watch_id):
    item = _load(watch_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": item["id"],
        "movie": item["movie"],
        "eventCode": item["eventCode"],
        "status": item["status"],
        "added_at": item["added_at"],
        "last_checked": item.get("last_checked"),
        "last_result": item.get("last_result"),
        "alert_sent": item.get("alert_sent", False),
        "logs": item.get("logs", [])[-30:],
    })


@app.route("/api/watch/<watch_id>/stop", methods=["POST"])
def stop_watch(watch_id):
    item = _load(watch_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    item["status"] = "stopped"
    _save(watch_id, item)
    _log(watch_id, "Stopped by user", "stop")
    return jsonify({"status": "stopped"})


@app.route("/api/test-whatsapp", methods=["POST"])
def test_whatsapp():
    # ... (unchanged)
    data = request.json or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "phone required"}), 400
    if not phone.startswith("+"):
        phone = f"+91{phone}"

    if not TWILIO_SID or not TWILIO_TOKEN:
        return jsonify({"error": "Twilio not configured"}), 500

    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(
        body=(
            "👋 *BMS Watchlist — Test*\n\n"
            "✅ WhatsApp alerts are working!\n"
            "You'll get a message like this when back seats open for your movies.\n\n"
            "_BMS Watchlist_"
        ),
        from_=TWILIO_FROM,
        to=f"whatsapp:{phone}",
    )
    return jsonify({"status": "sent", "sid": msg.sid})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)