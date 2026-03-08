# ============================================================
# ZYLON BY A.H — F1 LIVE SERVER
# ============================================================
# SETUP (one time):
#   pip install fastf1 flask flask-cors
#
# RUN on race weekend:
#   python f1_live_server.py
#
# Then open live-timing.html in your browser
# ============================================================

import threading, time, json, traceback
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Shared state ──────────────────────────────────────────
_lock  = threading.Lock()
_state = {
    "session": None,
    "drivers": [],
    "weather": {},
    "track_status": "clear",
}

# ── Driver map built from live feed ──────────────────────
_drivers_map    = {}   # number → static info
_timing_data    = {}   # number → latest timing
_prev_positions = {}   # for position tracking

# ============================================================
# FLASK API ENDPOINT
# Called by live-timing.html every second
# ============================================================
@app.route("/api/live")
def api_live():
    with _lock:
        return jsonify(_state)

@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, "drivers": len(_state["drivers"])})

# ============================================================
# FASTF1 LIVE TIMING THREAD
# ============================================================
def build_driver_row(num, t, d_info):
    """Convert raw timing line into clean dict for the frontend."""
    pos = int(t.get("Position") or t.get("position") or 99)

    # Gap / interval
    gap_raw = t.get("GapToLeader") or t.get("gap_to_leader") or ""
    if pos == 1 or gap_raw in ("", None):
        gap = "LEADER" if pos == 1 else "—"
    else:
        try:
            gap = f"+{float(gap_raw):.3f}" if not str(gap_raw).startswith("+") else str(gap_raw)
        except:
            gap = str(gap_raw)

    # Best lap
    bl_val = ""
    bl_sec = 9999.0
    bl_raw = t.get("BestLapTime") or t.get("best_lap") or {}
    if isinstance(bl_raw, dict):
        bl_val = bl_raw.get("Value") or bl_raw.get("value") or ""
    elif isinstance(bl_raw, str):
        bl_val = bl_raw
    try:
        bl_sec = lap_str_to_sec(bl_val)
    except:
        pass

    # Last lap
    ll_val = ""
    ll_raw = t.get("LastLapTime") or t.get("last_lap") or {}
    if isinstance(ll_raw, dict):
        ll_val = ll_raw.get("Value") or ll_raw.get("value") or ""
    elif isinstance(ll_raw, str):
        ll_val = ll_raw

    # Sectors
    sectors = t.get("Sectors") or []
    s1 = _get_sector(sectors, 0, t)
    s2 = _get_sector(sectors, 1, t)
    s3 = _get_sector(sectors, 2, t)

    # Current sector (which one is actively being driven)
    cur_sec = 0
    if not s3 and s2: cur_sec = 3
    elif not s2 and s1: cur_sec = 2
    elif not s1: cur_sec = 1

    # Tyre
    tyre_raw = t.get("Tyre") or t.get("tyre") or t.get("compound") or "—"
    if isinstance(tyre_raw, dict):
        tyre_raw = tyre_raw.get("Compound") or tyre_raw.get("compound") or "—"
    tyre_age = t.get("TyreAge") or t.get("tyre_age") or 0
    pits = t.get("Pits") or t.get("pits") or 0

    # Status
    in_pit  = bool(t.get("InPit") or t.get("in_pit"))
    out_lap = bool(t.get("OutLap") or t.get("out_lap"))
    dnf     = bool(t.get("Retired") or t.get("dnf"))
    drs     = bool(t.get("DRS") or t.get("drs"))

    return {
        "driver_number": num,
        "code":          d_info.get("code") or f"#{num}",
        "name":          d_info.get("name") or "",
        "team":          d_info.get("team") or "—",
        "team_color":    d_info.get("team_color") or "#666",
        "position":      pos,
        "gap":           gap,
        "best_lap":      bl_val,
        "best_lap_seconds": bl_sec,
        "last_lap":      ll_val,
        "last_lap_pb":   bool(t.get("PersonalFastest") or t.get("personal_fastest")),
        "s1": s1, "s2": s2, "s3": s3,
        "current_sector": cur_sec,
        "sector_best":   [False, False, False],
        "tyre":          str(tyre_raw).upper(),
        "tyre_age":      int(tyre_age) if tyre_age else 0,
        "pits":          int(pits) if pits else 0,
        "in_pit":        in_pit,
        "out_lap":       out_lap,
        "dnf":           dnf,
        "drs_open":      drs,
        "speed":         int(t.get("Speed") or t.get("speed") or 0),
    }

def _get_sector(sectors_list, idx, timing_dict):
    """Extract sector time from either list format or dict keys."""
    # List format from SignalR
    if sectors_list and len(sectors_list) > idx:
        s = sectors_list[idx]
        if isinstance(s, dict):
            return s.get("Value") or s.get("value") or ""
        return str(s) if s else ""
    # OpenF1 flat keys
    key = f"duration_sector_{idx+1}"
    val = timing_dict.get(key)
    if val:
        try: return f"{float(val):.3f}"
        except: return str(val)
    return ""

def lap_str_to_sec(s):
    """'1:23.456' → 83.456"""
    if not s or s in ("—", "", None): return 9999.0
    s = str(s).strip()
    if ":" in s:
        parts = s.split(":")
        return float(parts[0]) * 60 + float(parts[1])
    return float(s)

def update_state(drivers_list, session_info=None, weather_info=None, track_status=None):
    """Thread-safe update of shared state."""
    with _lock:
        if drivers_list is not None:
            _state["drivers"] = drivers_list
        if session_info is not None:
            _state["session"] = session_info
        if weather_info is not None:
            _state["weather"] = weather_info
        if track_status is not None:
            _state["track_status"] = track_status

# ============================================================
# FASTF1 SIGNALR CLIENT
# ============================================================
def run_fastf1():
    """Connect to F1 live timing via FastF1's SignalR client."""
    try:
        import fastf1.livetiming.client as f1c
        import fastf1.livetiming.base   as f1b
    except ImportError:
        print("❌  FastF1 not installed. Run: pip install fastf1 flask flask-cors")
        run_openf1_fallback()
        return

    print("🔴  Connecting to F1 Live Timing (FastF1 SignalR)...")

    drivers_map   = {}
    timing_lines  = {}
    session_info  = {}
    weather       = {}
    track_status  = "clear"

    class ZylonClient(f1c.SignalRClient):
        def _on_message(self, msgs):
            try:
                self._process(msgs)
            except Exception as e:
                print(f"Message error: {e}")

        def _process(self, msgs):
            nonlocal drivers_map, timing_lines, session_info, weather, track_status

            for cat, data, _ in msgs:

                # ── Driver list ──────────────────────────────
                if cat == "DriverList":
                    for num, d in data.items():
                        drivers_map[num] = {
                            "code":       d.get("Tla") or f"#{num}",
                            "name":       f"{d.get('FirstName','')} {d.get('LastName','')}".strip(),
                            "team":       d.get("TeamName") or "—",
                            "team_color": f"#{d.get('TeamColour','666')}",
                        }

                # ── Session info ─────────────────────────────
                elif cat == "SessionInfo":
                    s = data
                    session_info = {
                        "name":    s.get("Name") or "LIVE",
                        "type":    _session_type(s.get("Name") or ""),
                        "round":   s.get("Meeting", {}).get("Key") or "—",
                        "year":    (s.get("StartDate") or "2026")[:4],
                        "circuit": s.get("Meeting", {}).get("Circuit", {}).get("ShortName") or "—",
                    }
                    update_state(None, session_info=session_info)

                # ── Extrapolated clock (remaining time) ──────
                elif cat == "ExtrapolatedClock":
                    remaining = data.get("Remaining")
                    if remaining and session_info:
                        session_info["remaining"] = remaining
                        update_state(None, session_info=session_info)

                # ── Lap count ────────────────────────────────
                elif cat == "LapCount":
                    if session_info:
                        session_info["lap"]        = data.get("CurrentLap")
                        session_info["total_laps"] = data.get("TotalLaps")
                        update_state(None, session_info=session_info)

                # ── Timing data (MAIN FEED) ───────────────────
                elif cat == "TimingData":
                    lines = data.get("Lines") or {}
                    for num, t in lines.items():
                        if num not in timing_lines:
                            timing_lines[num] = {}
                        timing_lines[num] = _deep_merge(timing_lines[num], t)

                    # Rebuild drivers list
                    rows = []
                    for num, t in timing_lines.items():
                        info = drivers_map.get(num) or {}
                        rows.append(build_driver_row(num, t, info))

                    rows.sort(key=lambda r: r["position"])
                    update_state(rows)

                # ── Weather ───────────────────────────────────
                elif cat == "WeatherData":
                    weather = {
                        "air":      data.get("AirTemp"),
                        "track":    data.get("TrackTemp"),
                        "wind":     data.get("WindSpeed"),
                        "humidity": data.get("Humidity"),
                        "rainfall": data.get("Rainfall") == "1",
                    }
                    update_state(None, weather_info=weather)

                # ── Track status ──────────────────────────────
                elif cat == "TrackStatus":
                    status_map = {
                        "1":"clear","2":"yellow","3":"double_yellow",
                        "4":"sc","5":"red","6":"vsc","7":"vsc_ending"
                    }
                    track_status = status_map.get(str(data.get("Status")), "clear")
                    update_state(None, track_status=track_status)

                # ── Race control messages (flags) ─────────────
                elif cat == "RaceControlMessages":
                    msgs_list = data.get("Messages") or []
                    if msgs_list:
                        latest = msgs_list[-1]
                        flag   = (latest.get("Flag") or "").upper()
                        msg    = (latest.get("Message") or "").upper()
                        if flag == "GREEN" or "GREEN" in msg:
                            track_status = "green"
                        elif "SAFETY CAR" in msg and "VIRTUAL" not in msg:
                            track_status = "sc"
                        elif "VIRTUAL SAFETY CAR" in msg:
                            track_status = "vsc"
                        elif flag == "RED":
                            track_status = "red"
                        elif flag == "CHEQUERED":
                            track_status = "chequered"
                        update_state(None, track_status=track_status)

    # Write raw data to a temp file (FastF1 requirement)
    import tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), "zylon_f1_live.txt")

    client = ZylonClient(filename=tmp, logger=None)
    print("✅  FastF1 client started — waiting for session data...")

    try:
        client.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"⚠️  SignalR error: {e}")
        print("Switching to OpenF1 fallback...")
        run_openf1_fallback()

# ============================================================
# OPENF1 FALLBACK (polls every 2 seconds if FastF1 fails)
# ============================================================
def run_openf1_fallback():
    import urllib.request

    BASE = "https://api.openf1.org/v1"

    def get(endpoint):
        url = BASE + endpoint
        try:
            req  = urllib.request.Request(url, headers={"Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=5)
            return json.loads(resp.read())
        except:
            return []

    print("📡  OpenF1 fallback mode (2s delay)...")

    # Get latest session
    while True:
        sessions = get("/sessions?order_by=-date_start&limit=1")
        if sessions:
            break
        print("   Waiting for active session...")
        time.sleep(5)

    session = sessions[0]
    sk = session["session_key"]

    session_info = {
        "name":    session.get("session_name") or "LIVE",
        "type":    _session_type(session.get("session_name") or ""),
        "round":   str(session.get("meeting_key") or "—"),
        "year":    str(session.get("year") or "2026"),
        "circuit": session.get("circuit_short_name") or session.get("location") or "—",
    }
    update_state(None, session_info=session_info)
    print(f"✅  Session: {session_info['name']} — {session_info['circuit']}")

    drivers_map = {}
    drv_raw = get(f"/drivers?session_key={sk}")
    for d in drv_raw:
        num = str(d["driver_number"])
        drivers_map[num] = {
            "code":       d.get("name_acronym") or f"#{num}",
            "name":       f"{d.get('first_name','')} {d.get('last_name','')}".strip().upper(),
            "team":       d.get("team_name") or "—",
            "team_color": f"#{d.get('team_colour','666')}",
        }

    print(f"✅  {len(drivers_map)} drivers loaded")

    # Poll loop
    while True:
        try:
            laps      = get(f"/laps?session_key={sk}")
            intervals = get(f"/intervals?session_key={sk}")
            weather   = get(f"/weather?session_key={sk}")
            flags     = get(f"/race_control?session_key={sk}")
            car_data  = get(f"/car_data?session_key={sk}&speed>=0")

            # Build per-driver timing
            laps_by_driver = {}
            for l in laps:
                n = str(l["driver_number"])
                if n not in laps_by_driver:
                    laps_by_driver[n] = []
                laps_by_driver[n].append(l)

            interval_map = {str(i["driver_number"]): i for i in intervals}
            car_map      = {str(c["driver_number"]): c for c in car_data}

            rows = []
            for num, d_laps in laps_by_driver.items():
                last  = d_laps[-1]
                valid = [l for l in d_laps if l.get("lap_duration")]
                best  = sorted(valid, key=lambda l: l["lap_duration"])[0] if valid else None

                intv = interval_map.get(num) or {}
                car  = car_map.get(num) or {}
                info = drivers_map.get(num) or {}

                pos  = intv.get("position") or 99
                gap  = intv.get("gap_to_leader")
                gap_str = "LEADER" if pos == 1 else (f"+{gap:.3f}" if isinstance(gap, (int, float)) else "—")

                bl_val = ""
                bl_sec = 9999.0
                if best:
                    bl_sec = best["lap_duration"]
                    m = int(bl_sec // 60)
                    s = bl_sec % 60
                    bl_val = f"{m}:{s:06.3f}"

                ll_val = ""
                if last.get("lap_duration"):
                    ld = last["lap_duration"]
                    m = int(ld // 60)
                    s = ld % 60
                    ll_val = f"{m}:{s:06.3f}"

                s1 = f"{last['duration_sector_1']:.3f}" if last.get("duration_sector_1") else ""
                s2 = f"{last['duration_sector_2']:.3f}" if last.get("duration_sector_2") else ""
                s3 = f"{last['duration_sector_3']:.3f}" if last.get("duration_sector_3") else ""

                tyre    = (last.get("compound") or "—").upper()
                tyre_age = last.get("tyre_age_at_start") or 0
                pits    = max(0, (last.get("stint_number") or 1) - 1)

                in_pit  = bool(last.get("pit_in_time") and not last.get("pit_out_time"))
                drs_val = car.get("drs") or 0
                drs_open = int(drs_val) >= 10

                rows.append({
                    "driver_number": num,
                    "code":      info.get("code") or f"#{num}",
                    "name":      info.get("name") or "",
                    "team":      info.get("team") or "—",
                    "team_color":info.get("team_color") or "#666",
                    "position":  int(pos),
                    "gap":       gap_str,
                    "best_lap":  bl_val,
                    "best_lap_seconds": bl_sec,
                    "last_lap":  ll_val,
                    "last_lap_pb": bl_val == ll_val and ll_val != "",
                    "s1": s1, "s2": s2, "s3": s3,
                    "current_sector": 3 if s1 and s2 and not s3 else (2 if s1 and not s2 else (1 if not s1 else 0)),
                    "sector_best": [False, False, False],
                    "tyre":      tyre,
                    "tyre_age":  int(tyre_age),
                    "pits":      pits,
                    "in_pit":    in_pit,
                    "out_lap":   False,
                    "dnf":       False,
                    "drs_open":  drs_open,
                    "speed":     car.get("speed") or 0,
                })

            rows.sort(key=lambda r: r["position"])
            update_state(rows)

            # Weather
            if weather:
                w = weather[-1]
                update_state(None, weather_info={
                    "air":      w.get("air_temperature"),
                    "track":    w.get("track_temperature"),
                    "wind":     w.get("wind_speed"),
                    "humidity": w.get("humidity"),
                    "rainfall": (w.get("rainfall") or 0) > 0,
                })

            # Flags
            if flags:
                flag_msgs = [f for f in flags if f.get("flag")]
                if flag_msgs:
                    latest_flag = flag_msgs[-1]["flag"].lower()
                    flag_map = {
                        "green":"green","yellow":"yellow","double yellow":"double_yellow",
                        "red":"red","chequered":"chequered","safety car":"sc",
                        "virtual safety car":"vsc"
                    }
                    update_state(None, track_status=flag_map.get(latest_flag, "clear"))

            # Lap count
            if laps:
                max_lap = max(l.get("lap_number") or 0 for l in laps)
                with _lock:
                    if _state["session"]:
                        _state["session"]["lap"] = max_lap

        except Exception as e:
            print(f"OpenF1 poll error: {e}")

        time.sleep(2)  # OpenF1 limit — poll every 2s

# ============================================================
# HELPERS
# ============================================================
def _session_type(name):
    name = name.lower()
    if "race" in name:   return "RACE"
    if "qualifying" in name: return "QUALIFYING"
    if "sprint" in name: return "SPRINT"
    return "PRACTICE"

def _deep_merge(target, source):
    if not source:
        return target
    result = dict(target)
    for k, v in source.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 55)
    print("  ZYLON BY A.H — F1 LIVE TIMING SERVER")
    print("=" * 55)
    print("  API:  http://localhost:5000/api/live")
    print("  Open: live-timing.html in your browser")
    print("=" * 55)

    # Start FastF1 in background thread
    t = threading.Thread(target=run_fastf1, daemon=True)
    t.start()

    # Start Flask (blocking)
    app.run(host="0.0.0.0", port=5000, debug=False)
