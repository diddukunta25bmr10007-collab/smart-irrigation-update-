import json, re, threading, time, urllib.parse, urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
DATA = BASE / "data.json"
VERSION = "v3 multi-bed"
CHANNELS = 3                                  # valve relays CH1-3 in the control box
EXPIRING = {"start", "schedule", "newday"}    # stale watering commands are dropped; stop/e-stop never are
app = Flask(__name__, static_folder=None)
lock = threading.RLock()

cfg = {"beds": [{"bed": 1, "name": "Bed 1", "minutes": 2}, {"bed": 2, "name": "Bed 2", "minutes": 2},
                {"bed": 3, "name": "Bed 3", "minutes": 1}],
       "timers": [], "next_timer": 1, "rain_skip": 60,
       "loc": {"name": "Ashta, Madhya Pradesh", "lat": 23.02, "lon": 76.72}}
status = {"pump": False, "estop": False, "currentBed": 0, "battery": 0, "rain": False, "beds": [],
          "message": "Waiting for the laptop simulation...", "updated": 0}
queue, events, backlog, runq = deque(), deque(maxlen=30), [], []
wx = {"t": 0, "key": None, "data": None, "err": None}
ctr = {"id": 0, "last_start": 0}

def load():
    if DATA.exists():
        try: cfg.update(json.loads(DATA.read_text()))
        except Exception: pass
def save():
    tmp = DATA.with_suffix(".tmp"); tmp.write_text(json.dumps(cfg, indent=1)); tmp.replace(DATA)
def log(m): events.appendleft({"t": datetime.now().strftime("%H:%M:%S"), "m": m})
def online(): return time.time() - status["updated"] < 4
def busy(): return status["pump"] or time.time() - ctr["last_start"] < 4
def body(): return request.get_json(silent=True) or {}
def err(m, code=400): return jsonify({"ok": False, "error": m}), code
def ok(): return jsonify({"ok": True})
def mins(v, default=5, hi=120):
    try: return max(1, min(hi, int(float(v))))
    except (TypeError, ValueError): return default
def find_bed(n): return next((b for b in cfg["beds"] if b["bed"] == n), None)

# ---------- weather (Open-Meteo, no API key) ----------
def refresh_weather(force=False):
    loc = cfg["loc"]; key = (loc["lat"], loc["lon"])
    if not force and wx["data"] and wx["key"] == key and time.time() - wx["t"] < 600: return
    q = urllib.parse.urlencode({"latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 5,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,et0_fao_evapotranspiration"})
    try:
        with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=8) as r:
            wx.update(t=time.time(), key=key, data=json.load(r), err=None)
    except Exception as e:
        wx["err"] = str(e)[:100]

def rain_chance():
    refresh_weather()
    d = wx["data"]
    if not d or wx["key"] != (cfg["loc"]["lat"], cfg["loc"]["lon"]): return None
    if (d.get("current") or {}).get("precipitation", 0) > 0: return 100
    return (d.get("daily", {}).get("precipitation_probability_max") or [None])[0]

# ---------- commands ----------
def submit(command, bed=None, minutes=None, raw=""):
    if command not in {"start", "stop", "schedule", "estop", "reset", "newday"}: return {"ok": False, "error": "Unknown command"}, 400
    if command == "start":
        try: bed = int(bed)
        except (TypeError, ValueError): return {"ok": False, "error": "Choose a bed first"}, 400
        b = find_bed(bed)
        if not b: return {"ok": False, "error": "That bed is not set up"}, 400
        if status["estop"]: return {"ok": False, "error": "E-stop is active. Reset it first"}, 409
        if not online(): return {"ok": False, "error": "Laptop simulation is offline. Open the dashboard on the laptop"}, 503
        minutes = mins(minutes, b["minutes"])
    else:
        bed = minutes = None
    with lock:
        if command in ("stop", "estop"):
            for c in [c for c in queue if c["command"] in EXPIRING]: queue.remove(c)
            runq.clear()          # STOP / E-STOP also cancel beds waiting their turn
        if command == "start": ctr["last_start"] = time.time()
        ctr["id"] += 1
        c = {"id": ctr["id"], "command": command, "bed": bed, "minutes": minutes, "raw": raw or command, "ts": time.time()}
        queue.append(c); log(c["raw"])
    return {"ok": True, "queued": c}, 200

# ---------- timers ----------
def plan_run(ids, minutes, label):
    """Queue beds to run one after another (one pump, one bed at a time). minutes=None uses each bed's default."""
    items = [{"bed": n, "minutes": mins(minutes, find_bed(n)["minutes"]), "raw": f"{label}: {find_bed(n)['name']}"} for n in ids if find_bed(n)]
    if not items: return False
    with lock: runq.extend(items)
    log(f"{label}: " + ", ".join(find_bed(i["bed"])["name"] for i in items))
    return True

def advance():
    """Start the next waiting bed once the previous one has finished."""
    with lock:
        if runq and not online(): runq.clear(); log("Run cancelled: laptop simulation went offline")
        elif runq and not busy() and not status["estop"]:
            it = runq.pop(0); submit("start", it["bed"], it["minutes"], it["raw"])

def fire(t):
    """Run a timer's beds. Returns False when it should be retried because something is still running."""
    ids = [n for n in (t.get("beds") or [t["bed"]]) if find_bed(n)]
    if not ids: log("Timer skipped: bed was removed"); return True
    n = ", ".join(find_bed(i)["name"] for i in ids)
    if not online(): log(f"{n}: timer missed, laptop simulation offline"); return True
    if status["estop"]: log(f"{n}: timer skipped, E-stop active"); return True
    if busy() or runq: return False
    rc = rain_chance()
    if rc is not None and rc >= cfg["rain_skip"]: log(f"{n}: timer skipped, rain chance {rc}%"); return True
    plan_run(ids, t.get("minutes"), "Timer"); advance()
    return True

def scheduler():
    while True:
        try:
            now = datetime.now(); today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
            with lock:
                for t in list(cfg["timers"]):
                    if not t["on"]: continue
                    if t.get("at"):
                        if time.time() < t["at"]: continue
                        cfg["timers"].remove(t); save()
                        if time.time() - t["at"] > 120: log("One-time timer missed (server was off)"); continue
                        backlog.append((t, time.time() + 900))
                    elif t.get("time") == hm and t.get("last") != today and (not t["days"] or now.weekday() in t["days"]):
                        t["last"] = today; save(); backlog.append((t, time.time() + 900))
            for item in list(backlog):
                if fire(item[0]): backlog.remove(item)
                elif time.time() > item[1]: backlog.remove(item); log("Timer dropped: bed stayed busy for 15 min")
            advance()
        except Exception as e:
            print("scheduler:", e)
        time.sleep(2)

# ---------- pages ----------
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store"     # phones always get the newest app files
    return r

def static(name): return send_from_directory(BASE, name)
@app.get("/")
def laptop(): return static("irry_2d_simulation.html")
@app.get("/mobile")
def mobile(): return static("mobile.html")
@app.get("/manifest.webmanifest")
def manifest(): return static("manifest.webmanifest")
@app.get("/sw.js")
def sw(): return static("sw.js")
@app.get("/icon-<int:n>.png")
def icon(n): return static(f"icon-{n}.png") if n in (192, 512) else ("", 404)

# ---------- command + status API (used by the laptop simulation) ----------
@app.get("/api/command")
def get_command():
    with lock:
        while queue:
            c = queue.popleft()
            if c["command"] in EXPIRING and time.time() - c["ts"] > 30: log("Dropped stale command: " + c["raw"]); continue
            return jsonify(c)
    return jsonify({})

@app.post("/api/command")
def post_command():
    d = body(); res, code = submit(str(d.get("command", "")).lower().strip(), d.get("bed"), d.get("minutes"), str(d.get("raw", "")))
    return jsonify(res), code

NUM = {"1": 1, "one": 1, "2": 2, "two": 2, "to": 2, "too": 2, "3": 3, "three": 3}
@app.post("/api/command-text")
def command_text():
    raw = str(body().get("text", "")).strip(); low = raw.lower(); bed = None
    has = lambda *w: any(x in low for x in w)
    if has("reset"): command = "reset"                     # must come before "emergency"
    elif has("emergency", "e-stop", "estop"): command = "estop"
    elif has("stop", "halt", "turn off", "switch off"): command = "stop"
    elif has("new day", "next day"): command = "newday"
    elif has("schedule", "automatic", "auto run"): command = "schedule"
    elif has("start", "irrigate", "water", "open"):
        nums = [b["bed"] for b in cfg["beds"]] if "all" in low.split() else sorted({int(x) for x in re.findall(r"\b[123]\b", low)})
        if len(nums) > 1:
            if status["estop"] or not online() or busy() or runq: return err("Cannot start now: E-stop, laptop offline or already running")
            plan_run(nums, None, "Voice"); advance(); return ok()
        m = re.search(r"\b(?:bed|b)\s*(1|2|3|one|two|to|too|three)\b", low) or re.search(r"\b(1|2|3|one|two|three)\b", low)
        if not m: return err("Say a bed number, e.g. 'start bed 2'")
        command, bed = "start", NUM[m.group(1)]
    else: return err("Command not recognized")
    res, code = submit(command, bed, None, raw); return jsonify(res), code

@app.get("/api/status")
def get_status():
    with lock: return jsonify(status)

@app.post("/api/status")
def set_status():
    with lock: status.update(body()); status["updated"] = time.time()
    return ok()

@app.post("/api/run")
def run_beds():
    d = body()
    if status["estop"]: return err("E-stop is active. Reset it first", 409)
    if not online(): return err("Laptop simulation is offline. Open the dashboard on the laptop", 503)
    ids = [int(x) for x in d.get("beds", []) if str(x).isdigit()]
    if not ids: return err("Select at least one bed")
    if busy() or runq: return err("Irrigation is already running. Press STOP first", 409)
    if not plan_run(ids, d.get("minutes") or None, "Phone"): return err("Those beds are not set up")
    advance(); return ok()

# ---------- app API ----------
@app.get("/api/state")
def get_state():
    with lock:
        live = {b.get("bed"): b for b in status.get("beds", [])}
        used = {b["bed"] for b in cfg["beds"]}
        return jsonify({"online": online(), "pump": status["pump"], "estop": status["estop"], "currentBed": status["currentBed"],
            "battery": status["battery"], "message": status["message"], "now": time.time(), "version": VERSION,
            "beds": [{**b, **live.get(b["bed"], {})} for b in cfg["beds"]],
            "free": [n for n in range(1, CHANNELS + 1) if n not in used],
            "timers": cfg["timers"], "rain_skip": cfg["rain_skip"], "loc": cfg["loc"], "events": list(events), "runq": [i["bed"] for i in runq]})

@app.post("/api/beds")
def add_bed():
    d = body()
    with lock:
        free = [n for n in range(1, CHANNELS + 1) if n not in {b["bed"] for b in cfg["beds"]}]
        if not free: return err("All valve channels are in use", 409)
        try: ch = int(d.get("bed") or free[0])
        except (TypeError, ValueError): ch = free[0]
        if ch not in free: return err("That valve channel is taken", 409)
        cfg["beds"].append({"bed": ch, "name": (str(d.get("name", "")).strip() or f"Bed {ch}")[:24], "minutes": mins(d.get("minutes"), 5)})
        cfg["beds"].sort(key=lambda b: b["bed"]); save(); log(f"Bed added on channel {ch}")
    return ok()

@app.patch("/api/beds/<int:n>")
def edit_bed(n):
    d = body()
    with lock:
        b = find_bed(n)
        if not b: return err("No such bed", 404)
        if "name" in d: b["name"] = (str(d["name"]).strip() or b["name"])[:24]
        if "minutes" in d: b["minutes"] = mins(d["minutes"], b["minutes"])
        save()
    return ok()

@app.delete("/api/beds/<int:n>")
def del_bed(n):
    with lock:
        if status["pump"] and status["currentBed"] == n: return err("Stop irrigation first", 409)
        cfg["beds"] = [b for b in cfg["beds"] if b["bed"] != n]
        for t in cfg["timers"]:
            t["beds"] = [x for x in (t.get("beds") or [t["bed"]]) if x != n]
            if t["beds"]: t["bed"] = t["beds"][0]
        cfg["timers"] = [t for t in cfg["timers"] if t["beds"]]; save(); log(f"Bed on channel {n} removed")
    return ok()

@app.post("/api/timers")
def add_timer():
    d = body()
    with lock:
        ids = sorted({int(x) for x in d.get("beds", []) if str(x).isdigit() and find_bed(int(x))})
        if not ids: return err("Choose at least one bed")
        t = {"id": cfg["next_timer"], "bed": ids[0], "beds": ids, "minutes": mins(d["minutes"]) if d.get("minutes") else None,
             "on": True, "days": [], "time": None, "at": None, "last": ""}
        if d.get("delay"): t["at"] = time.time() + mins(d["delay"], 1, 1440) * 60
        elif re.fullmatch(r"\d\d:\d\d", str(d.get("time", ""))):
            t["time"] = d["time"]; t["days"] = sorted({x for x in d.get("days", []) if isinstance(x, int) and 0 <= x <= 6})
        else: return err("Set a start time or a countdown")
        cfg["next_timer"] += 1; cfg["timers"].append(t); save()
    return ok()

@app.patch("/api/timers/<int:i>")
def edit_timer(i):
    with lock:
        t = next((x for x in cfg["timers"] if x["id"] == i), None)
        if not t: return err("No such timer", 404)
        t["on"] = bool(body().get("on")); save()
    return ok()

@app.delete("/api/timers/<int:i>")
def del_timer(i):
    with lock: cfg["timers"] = [t for t in cfg["timers"] if t["id"] != i]; save()
    return ok()

@app.get("/api/weather")
def get_weather():
    refresh_weather(request.args.get("force") == "1")
    good = bool(wx["data"]) and wx["key"] == (cfg["loc"]["lat"], cfg["loc"]["lon"])
    return jsonify({"ok": good, "data": wx["data"] if good else None, "age": int(time.time() - wx["t"]) if good else None,
                    "error": wx["err"], "loc": cfg["loc"]})

@app.get("/api/geocode")
def geocode():
    q = request.args.get("q", "").strip()
    if len(q) < 2: return jsonify({"results": []})
    try:
        with urllib.request.urlopen("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": q, "count": 5}), timeout=8) as r:
            found = json.load(r).get("results", [])
    except Exception:
        return jsonify({"results": [], "error": "Could not search. Does the laptop have internet?"}), 502
    return jsonify({"results": [{"name": ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x),
                                 "lat": p["latitude"], "lon": p["longitude"]} for p in found]})

@app.post("/api/location")
def set_location():
    d = body()
    try:
        lat, lon = float(d["lat"]), float(d["lon"]); assert -90 <= lat <= 90 and -180 <= lon <= 180
    except Exception: return err("Invalid location")
    with lock: cfg["loc"] = {"name": str(d.get("name") or f"{lat:.2f}, {lon:.2f}")[:60], "lat": lat, "lon": lon}; save()
    return ok()

@app.post("/api/settings")
def settings():
    try: v = int(body()["rain_skip"]); assert 0 <= v <= 100
    except Exception: return err("Rain limit must be 0-100")
    with lock: cfg["rain_skip"] = v; save()
    return ok()

if __name__ == "__main__":
    load()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"\nSMART IRRIGATION SERVER {VERSION}\nLaptop simulation: http://localhost:5000/\nPhone app:         http://<LAPTOP-IP>:5000/mobile\n")
    app.run(host="0.0.0.0", port=5000, threaded=True)
