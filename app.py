"""
BMS Watchlist Backend
─────────────────────
Add movies you want to watch → backend monitors Sathyam, HDFC, Palazzo
→ WhatsApp you the moment back seats open.

Smart polling:
  show > 4 hrs away  →  every 30 mins
  show 2-4 hrs away  →  every 10 mins
  show 30min-2hr     →  every 2 mins   ← the golden window
  show < 30 mins     →  stop (too late)

Dates monitored:
  Always today. If item added after 7 PM → also tomorrow.
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
_local_store = {}   # fallback when Redis not available

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

# ── Hardcoded theaters (confirmed from BMS live inspection) ──────────────────

THEATERS = {
    "PVSR": {
        "name": "PVR: Sathyam",
        "slug": "pvr-sathyam-royapettah",
        "city": "chennai",
    },
    "PVES": {
        "name": "HDFC Millennia PVR: Express Avenue",
        "slug": "hdfc-millennia-pvr-escape-express-avenue-mall",
        "city": "chennai",
    },
    "PVPZ": {
        "name": "PVR: Palazzo",
        "slug": "pvr-palazzo-the-nexus-vijaya-mall",
        "city": "chennai",
    },
}

# ── Playwright shared browser ─────────────────────────────────────────────────

_browser_lock = threading.Lock()
_shared_playwright = None
_shared_browser = None

BMS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _get_browser():
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser and _shared_browser.is_connected():
            return _shared_browser
        from playwright.sync_api import sync_playwright
        if _shared_playwright:
            try:
                _shared_playwright.stop()
            except Exception:
                pass
        _shared_playwright = sync_playwright().start()
        _shared_browser = _shared_playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        log.info("Playwright browser launched ✓")
        return _shared_browser


# ── Storage helpers ───────────────────────────────────────────────────────────

def _save(watch_id, data):
    if _redis:
        _redis.set(f"watch:{watch_id}", json.dumps(data), ex=172800)  # 48hr TTL
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


# ── Date helpers ─────────────────────────────────────────────────────────────

def _watch_dates():
    """Return dates to monitor: always today, add tomorrow if after 7 PM."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    return [today, tomorrow] if now.hour >= 19 else [today]


def _parse_show_time(time_str, date_str):
    """Parse '06:15 PM' + '20260323' → datetime object."""
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


def _smart_interval(minutes_away):
    """Return poll interval in seconds based on how far the show is."""
    if minutes_away > 240:
        return 1800   # 30 min
    if minutes_away > 120:
        return 600    # 10 min
    if minutes_away > 30:
        return 120    # 2 min
    return None       # stop — too late


# ── Core check: one theater, one date ────────────────────────────────────────

_CHECK_JS = """
(eventCode) => {
    try {
        const api = window.__INITIAL_STATE__?.venueShowtimesFunctionalApi;
        const queries = api?.queries || {};
        const key = Object.keys(queries)[0];
        const transformed = queries[key]?.data?.showDetailsTransformed;
        const EventMap = transformed?.Event;
        if (!EventMap) return { found: false, reason: 'no_state' };

        const venueCode = window.location.pathname.match(/buytickets\\/([^\\/]+)\\//)?.[1] || '';
        const date = window.location.pathname.match(/\\/(\\d{8})(?:$|\\/)/)?.[1] || '';
        const city = window.location.pathname.match(/\\/cinemas\\/([^\\/]+)\\//)?.[1] || 'chennai';

        const shows = [];

        for (const movie of Object.values(EventMap)) {
            for (const child of Object.values(movie.ChildEvents || {})) {
                if (child.EventCode !== eventCode) continue;
                for (const session of (child.ShowTimes || [])) {
                    const cats = session.Categories || [];

                    // Find cheapest category (back seats)
                    const backKw = /budget|back|economy|classic|standard|bronze|silver/i;
                    let cheapest = cats.find(c => backKw.test(c.PriceDesc || ''));
                    if (!cheapest) {
                        cheapest = cats.reduce((min, c) =>
                            parseFloat(c.CurPrice || '9999') < parseFloat(min?.CurPrice || '9999') ? c : min,
                        null);
                    }

                    const availableCats = cats
                        .filter(c => String(c.AvailStatus) === '1')
                        .map(c => ({ name: c.PriceDesc, price: parseFloat(c.CurPrice || '0') }))
                        .sort((a, b) => a.price - b.price);

                    shows.push({
                        time: session.ShowTime,
                        sessionId: session.SessionId,
                        movieTitle: movie.EventTitle,
                        cheapestSeat: cheapest ? {
                            name: cheapest.PriceDesc,
                            price: parseFloat(cheapest.CurPrice || '0'),
                            available: String(cheapest.AvailStatus) === '1'
                        } : null,
                        availableCats,
                        hasBackSeats: cheapest ? String(cheapest.AvailStatus) === '1' : false,
                        bookingUrl: `https://in.bookmyshow.com/movies/${city}/seat-layout/${eventCode}/${venueCode}/${session.SessionId}/${date}`
                    });
                }
            }
        }

        return { found: true, venueCode, date, shows };
    } catch(e) {
        return { found: false, reason: e.message };
    }
}
"""


def _check_movie_at_theater(event_code, venue_code, date, page):
    """Navigate to theater page and extract show data for a specific movie."""
    theater = THEATERS[venue_code]
    url = (
        f"https://in.bookmyshow.com/cinemas/{theater['city']}/"
        f"{theater['slug']}/buytickets/{venue_code}/{date}"
    )
    try:
        page.goto(url, timeout=25_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)  # wait for React hydration
        result = page.evaluate(_CHECK_JS, event_code)
        return result
    except Exception as e:
        return {"found": False, "reason": str(e)[:80]}


# ── Monitoring thread ─────────────────────────────────────────────────────────

def _run_watchlist_monitor(watch_id):
    """Background thread: polls all theaters for this movie until seats open."""
    from playwright.sync_api import TimeoutError as PwTimeout

    item = _load(watch_id)
    if not item:
        return

    event_code = item["eventCode"]
    phone = item["phone"]
    movie_title = item["movie"]

    item["status"] = "monitoring"
    _save(watch_id, item)
    _log(watch_id, f"Started watching '{movie_title}' ({event_code})", "start")

    try:
        browser = _get_browser()
        context = browser.new_context(
            user_agent=BMS_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.new_page()
        page.route("**/*.{png,jpg,gif,svg,woff,woff2,ttf,ico}", lambda r: r.abort())

        poll_count = 0

        while True:
            # Reload item to catch external stop signals
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
                        result = _check_movie_at_theater(event_code, venue_code, date, page)
                    except PwTimeout:
                        _log(watch_id, f"Timeout on {venue_code}/{date}", "warn")
                        continue
                    except Exception as e:
                        _log(watch_id, f"Error on {venue_code}/{date}: {e}", "warn")
                        continue

                    if not result.get("found"):
                        _log(watch_id, f"{venue_code}/{date}: no data ({result.get('reason','')})", "info")
                        continue

                    shows = result.get("shows", [])
                    date_label = "Today" if date == now.strftime("%Y%m%d") else "Tomorrow"

                    for show in shows:
                        show_dt = _parse_show_time(show["time"], date)
                        if not show_dt:
                            continue

                        mins_away = (show_dt - now).total_seconds() / 60

                        # Skip past shows or shows starting in < 30 mins
                        if mins_away < 30:
                            continue

                        interval = _smart_interval(mins_away)
                        if interval:
                            next_intervals.append(interval)

                        if show.get("hasBackSeats"):
                            seat = show.get("cheapestSeat", {})
                            found_seats.append({
                                "theater": THEATERS[venue_code]["name"],
                                "venue_code": venue_code,
                                "date_label": date_label,
                                "time": show["time"],
                                "seat_name": seat.get("name", "Back seats"),
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

            # Log summary
            status_msg = f"Poll #{poll_count}: checked {len(dates)} date(s) × 3 theaters, no back seats yet"
            item = _load(watch_id)
            item["last_checked"] = now.strftime("%H:%M:%S")
            item["last_result"] = status_msg
            _save(watch_id, item)
            _log(watch_id, status_msg, "poll")

            # Sleep until next poll
            wait = min(next_intervals) if next_intervals else 1800
            _log(watch_id, f"Next check in {wait // 60} min {wait % 60} sec", "info")
            time.sleep(wait)

        try:
            context.close()
        except Exception:
            pass

    except Exception as e:
        item = _load(watch_id)
        if item:
            item["status"] = "error"
            item["last_error"] = str(e)[:120]
            _save(watch_id, item)
        _log(watch_id, f"Fatal error: {e}", "error")


# ── WhatsApp alert ────────────────────────────────────────────────────────────

def _send_watchlist_alert(watch_id, movie_title, phone, found_seats):
    if not phone.startswith("+"):
        phone = f"+91{phone}"

    # Build message
    msg = f"🎬 *Back seats just opened!*\n\n*{movie_title}*\n\n"
    for s in found_seats[:3]:  # max 3 entries to keep message clean
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


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        # Quick health check — don't load all items (slow with Redis)
        status = {
            "status": "ok",
            "service": "bms-watchlist",
            "redis": bool(_redis),
            "twilio": bool(TWILIO_SID and TWILIO_TOKEN),
            "time": datetime.now().isoformat(),
        }

        # Try to ping Redis if available
        if _redis:
            try:
                _redis.ping()
                status["redis_connected"] = True
            except:
                status["redis_connected"] = False

        return jsonify(status), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/watch", methods=["POST"])
def add_watch():
    """Add a movie to the watchlist and start monitoring."""
    data = request.json or {}

    phone = data.get("phone", "").strip()
    event_code = data.get("eventCode", "").strip()
    movie = data.get("movie", "").strip()

    if not phone or not event_code or not movie:
        return jsonify({"error": "phone, eventCode and movie are required"}), 400

    if not phone.startswith("+"):
        phone = f"+91{phone}"

    # Enforce per-phone limit
    all_items = _load_all()
    active = [
        i for i in all_items.values()
        if i.get("phone") == phone and i.get("status") == "monitoring"
    ]
    if len(active) >= MAX_WATCHLIST_PER_PHONE:
        return jsonify({
            "error": f"You're already watching {len(active)} movies. Remove one first."
        }), 429

    # Prevent duplicate watches for same movie
    duplicate = next(
        (i for i in active if i.get("eventCode") == event_code), None
    )
    if duplicate:
        return jsonify({
            "error": "Already watching this movie",
            "watch_id": duplicate["id"]
        }), 409

    watch_id = str(uuid.uuid4())[:8]
    item = {
        "id": watch_id,
        "movie": movie,
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


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    """Get all active watchlist items for a phone number."""
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
