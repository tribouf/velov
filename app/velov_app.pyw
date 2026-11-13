#!/usr/bin/env python3
"""
Vélo'v Control — local control panel for the Villeurbanne Vélo'v project.

Double-click this file (it runs with pythonw, so no console window), then open
http://localhost:8765 in the Claude app's built-in browser.

- Built-in collector: station availability every N minutes (replaces
  start_velov_collector.bat).
- Buttons for every command, with live output.
- Map (OpenStreetMap), observed bike trips, files, autostart with Windows.

Standard library only. The page (velov_app.html) is re-read on every load,
so UI updates don't need a restart.
"""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))          # code and pages
DATA = os.environ.get("VELOV_DATA", HERE)                  # database, logs, CSVs
os.makedirs(DATA, exist_ok=True)
os.chdir(DATA)
sys.path.insert(0, HERE)

PORT = int(os.environ.get("VELOV_PORT", "8765"))
HOST = os.environ.get("VELOV_HOST", "127.0.0.1")           # 0.0.0.0 in Docker
DB = os.path.join(DATA, "velov.sqlite")
LOG = os.path.join(DATA, "velov_collector.log")
SETTINGS = os.path.join(DATA, "velov_app_settings.json")
APP_ID = "velov-control"
APP_VERSION = "1.5 (2026-11-11)"   # French app shell, Aujourd'hui, phone bottom bar, colour-blind-safe colours
COMMUNE = "Villeurbanne"
# the whole Métropole: inside the Lyon area, without the workshop pseudo-station
METRO_SQL = ("s.lat BETWEEN 45.55 AND 45.95 AND s.lng BETWEEN 4.65 AND 5.15 "
             "AND UPPER(COALESCE(s.name,'')) NOT LIKE '%ATELIER%'")
REBALANCE = 8
WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WIN else 0

WFS_URL = (
    "https://data.grandlyon.com/geoserver/metropole-de-lyon/ows"
    "?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
    "&typename=metropole-de-lyon%3Ajcd_jcdecaux.jcdvelov"
    "&outputFormat=application%2Fjson&SRSNAME=EPSG%3A4326"
)
UA = "velov-control/1.0 (personal research)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    number INTEGER PRIMARY KEY, name TEXT, address TEXT, commune TEXT,
    lat REAL, lng REAL, bike_stands INTEGER);
CREATE TABLE IF NOT EXISTS snapshots (
    fetched_at TEXT NOT NULL, number INTEGER NOT NULL, last_update TEXT, status TEXT,
    bikes INTEGER, docks_free INTEGER, mechanical INTEGER, electrical INTEGER, raw TEXT,
    PRIMARY KEY (fetched_at, number));
CREATE INDEX IF NOT EXISTS idx_snap_number ON snapshots(number, fetched_at);
"""


# ---------------------------------------------------------------- settings
DEFAULTS = {"collector_on": True, "interval_min": 5, "bikes_every_min": 0, "scope": "metropole",
            "adaptive": True}


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return dict(DEFAULTS)


def save_settings(s):
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


settings = load_settings()
lock = threading.RLock()


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def log_line(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def db_connect(ro=False):
    if ro:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    return sqlite3.connect(DB, timeout=15)


# ---------------------------------------------------------------- adaptive pacing
# Fetch less when the city is asleep, never less during the day. From 06:30 to
# 21:30 (Paris time) the base intervals always apply, so the morning rush is
# caught from its first minutes; outside those hours the interval stretches when
# the last readings show little movement and snaps back as soon as it picks up.
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:                                    # no tz database: assume CET
    PARIS = timezone(timedelta(hours=1))
DAY_START, DAY_END = 6.5, 21.5          # local hours with full-rate collection
STATION_QUIET, STATION_CALM = 12, 40    # bikes changed per 5 min, whole Métropole
BIKES_QUIET, BIKES_CALM = 40, 120       # moves per 15 min, whole Métropole


def local_hour():
    t = datetime.now(PARIS)
    return t.hour + t.minute / 60


def station_activity():
    """Bikes that changed station counts between the last two snapshots, per 5 min."""
    try:
        with db_connect(ro=True) as db:
            ts = [r[0] for r in db.execute("SELECT DISTINCT fetched_at FROM snapshots "
                                           "ORDER BY fetched_at DESC LIMIT 2")]
            if len(ts) < 2:
                return None
            ch = db.execute("""SELECT SUM(ABS(a.bikes - b.bikes)) FROM snapshots a
                               JOIN snapshots b ON a.number = b.number
                               WHERE a.fetched_at = ? AND b.fetched_at = ?""", ts).fetchone()[0]
        mins = (datetime.fromisoformat(ts[0]) - datetime.fromisoformat(ts[1])).total_seconds() / 60
        return (ch or 0) * 5 / max(mins, 1)
    except Exception:
        return None


def bike_activity():
    """Moves seen by the last bike fetch, per 15 min."""
    try:
        with db_connect(ro=True) as db:
            rows = db.execute("SELECT fetched_at, moves FROM bike_fetches "
                              "ORDER BY fetched_at DESC LIMIT 2").fetchall()
        if len(rows) < 2:
            return None
        mins = (datetime.fromisoformat(rows[0][0]) - datetime.fromisoformat(rows[1][0])).total_seconds() / 60
        return (rows[0][1] or 0) * 15 / max(mins, 1)
    except Exception:
        return None


def station_pace(base_s):
    if not settings.get("adaptive", True):
        return base_s, "fixed"
    if DAY_START <= local_hour() < DAY_END:
        return base_s, "day"
    a = station_activity()
    if a is None:
        return base_s, "night"
    if a < STATION_QUIET:
        return max(base_s, 15 * 60), "quiet"
    if a < STATION_CALM:
        return max(base_s, 10 * 60), "calm"
    return base_s, "busy"


def bike_pace(base_min):
    if not settings.get("adaptive", True):
        return base_min, "fixed"
    if DAY_START <= local_hour() < DAY_END:
        return base_min, "day"
    a = bike_activity()
    if a is None:
        return base_min, "night"
    if a < BIKES_QUIET:
        return max(base_min, 60), "quiet"
    if a < BIKES_CALM:
        return max(base_min, 30), "calm"
    return base_min, "busy"


# ---------------------------------------------------------------- collector
collector = {"running": False, "last_ok": None, "last_error": None, "last_count": None,
             "next_at": None, "runs": 0, "busy": False, "interval_s": None, "pace": None}
wake = threading.Event()


def as_dict(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def collect_once():
    req = urllib.request.Request(WFS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        feats = json.load(r)["features"]
    ts = now_utc().isoformat()
    kept = 0
    with db_connect() as db:
        db.executescript(SCHEMA)
        for f in feats:
            p = f.get("properties", {})
            n = p.get("number")
            if n is None:
                continue
            commune = (p.get("commune") or "").strip()
            c = (f.get("geometry") or {}).get("coordinates") or [None, None]
            db.execute("INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?,?)",
                       (n, p.get("name"), p.get("address"), commune,
                        p.get("lat", c[1]), p.get("lng", c[0]), p.get("bike_stands")))
            if settings.get("scope") != "metropole" and commune.lower() != COMMUNE.lower():
                continue
            av = as_dict(as_dict(p.get("total_stands")).get("availabilities"))
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                       (ts, n, p.get("last_update"), p.get("status"),
                        p.get("available_bikes"), p.get("available_bike_stands"),
                        av.get("mechanicalBikes"), av.get("electricalBikes"), None))
            kept += 1
    return ts, kept, len(feats)


def other_collector_running():
    """True if the old console collector wrote to the log in the last 6 min."""
    try:
        with open(LOG, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        for l in reversed(lines[-20:]):
            if "[app]" in l:
                continue
            m = re.match(r"(\S+)\s+\d+ stations saved", l)
            if m:
                t = datetime.fromisoformat(m.group(1))
                return (now_utc() - t) < timedelta(minutes=6)
        return False
    except Exception:
        return False


def collector_loop():
    while True:
        with lock:
            on = settings["collector_on"]
            interval = max(1, int(settings["interval_min"])) * 60
        if on:
            collector.update(running=True, busy=True)
            try:
                ts, kept, total = collect_once()
                collector.update(last_ok=ts, last_count=kept, last_error=None,
                                 runs=collector["runs"] + 1)
                log_line(f"{ts}  {kept} stations saved (of {total} in feed) [app]")
            except Exception as e:
                collector["last_error"] = f"{now_utc().isoformat()}  {e!r}"
                log_line(f"{now_utc().isoformat()}  fetch failed: {e!r} [app]")
            collector["busy"] = False
            interval, pace = station_pace(interval)
            collector.update(interval_s=interval, pace=pace)
            # align to the interval grid so snapshots land on round times
            t = time.time()
            nxt = (int(t // interval) + 1) * interval
            # never sleep past the start of the day-rate window
            day_start = datetime.now(PARIS).replace(hour=int(DAY_START), minute=int(DAY_START % 1 * 60),
                                                     second=0, microsecond=0).timestamp()
            if t < day_start < nxt:
                nxt = day_start
        else:
            collector["running"] = False
            nxt = time.time() + 3600
        collector["next_at"] = datetime.fromtimestamp(nxt, timezone.utc).isoformat() if on else None
        wake.wait(max(1, nxt - time.time()))
        wake.clear()


# ---------------------------------------------------------------- jobs
jobs = {}
job_seq = [0]
JOBS = {
    "bikes": {"label": "Fetch bike numbers", "cmd": [os.path.join(HERE, "velov_bikes_once.py")]},
    "report": {"label": "Station report", "cmd": [os.path.join(HERE, "velov_report.py")]},
}
bike_sched = {"next_at": None, "interval_min": None, "pace": None}


def python_exe():
    exe = sys.executable
    if WIN and exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def start_job(kind, auto=False):
    spec = dict(JOBS[kind])
    if auto:
        spec["cmd"] = [*spec["cmd"], "--auto"]
        spec["label"] += " (auto)"
    with lock:
        for j in jobs.values():
            if j["kind"] == kind and j["status"] == "running":
                return j["id"]
        job_seq[0] += 1
        jid = str(job_seq[0])
        jobs[jid] = {"id": jid, "kind": kind, "label": spec["label"], "auto": auto, "status": "running",
                     "started": now_utc().isoformat(), "ended": None, "lines": []}
    threading.Thread(target=run_job, args=(jid, spec), daemon=True).start()
    return jid


def run_job(jid, spec):
    j = jobs[jid]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        p = subprocess.Popen([python_exe(), "-u", *spec["cmd"]], cwd=DATA, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=NO_WINDOW)
        for raw in p.stdout:
            j["lines"].append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        code = p.wait()
        j["status"] = "done" if code == 0 else "failed"
    except Exception as e:
        j["lines"].append(f"Could not start: {e!r}")
        j["status"] = "failed"
    j["ended"] = now_utc().isoformat()
    # keep only the 30 most recent jobs
    with lock:
        for old in sorted(jobs, key=int)[:-30]:
            jobs.pop(old, None)


def bike_scheduler():
    last_run = None
    while True:
        with lock:
            base = int(settings.get("bikes_every_min") or 0)
        if base <= 0:
            bike_sched.update(next_at=None, interval_min=None, pace=None)
            last_run = None
            time.sleep(20)
            continue
        every, pace = bike_pace(base)
        t = time.time()
        grid = 5 * 60
        if last_run is None:
            nxt = (int(t // (base * 60)) + 1) * base * 60
        else:
            nxt = -(-(last_run + every * 60 - 30) // grid) * grid   # next 5-min mark after the interval
        day_start = datetime.now(PARIS).replace(hour=int(DAY_START), minute=int(DAY_START % 1 * 60),
                                                 second=0, microsecond=0).timestamp()
        if t < day_start < nxt:
            nxt = day_start
        bike_sched.update(next_at=datetime.fromtimestamp(nxt, timezone.utc).isoformat(),
                          interval_min=every, pace=pace)
        time.sleep(max(1, min(60, nxt - t)))
        if time.time() >= nxt - 1:
            start_job("bikes", auto=True)
            last_run = nxt


# ---------------------------------------------------------------- queries
def q_status(_q):
    out = {"app": APP_ID, "now": now_utc().isoformat(), "settings": settings,
           "collector": collector, "bikes_next_at": bike_sched["next_at"],
           "bikes_pace": bike_sched["pace"], "bikes_interval_min": bike_sched["interval_min"],
           "host": "docker" if os.environ.get("VELOV_DATA") else "pc",
           "version": APP_VERSION,
           "remote_login": bool(ACCESS_TEAM and ACCESS_AUD),
           "remote_login_last_check": _access["last"],
           "pacing": {"day_start": DAY_START, "day_end": DAY_END,
                      "station_quiet": STATION_QUIET, "station_calm": STATION_CALM,
                      "bikes_quiet": BIKES_QUIET, "bikes_calm": BIKES_CALM,
                      "station_quiet_min": 15, "station_calm_min": 10,
                      "bikes_quiet_min": 60, "bikes_calm_min": 30},
           "old_collector": other_collector_running(), "autostart": autostart_enabled(),
           "db_bytes": os.path.getsize(DB) if os.path.exists(DB) else 0}
    if os.path.exists(DB):
        with db_connect(ro=True) as db:
            def one(sql, *a):
                try:
                    return db.execute(sql, a).fetchone()[0]
                except sqlite3.Error:
                    return None
            out["snapshots"] = one("SELECT COUNT(DISTINCT fetched_at) FROM snapshots")
            out["first_snapshot"] = one("SELECT MIN(fetched_at) FROM snapshots")
            out["bike_fetches"] = one("SELECT COUNT(DISTINCT fetched_at) FROM bike_snapshots")
            out["last_bike_fetch"] = one("SELECT MAX(fetched_at) FROM bike_fetches") or one("SELECT MAX(fetched_at) FROM bike_snapshots")
            out["moves"] = one("SELECT COUNT(*) FROM bike_moves")
            out["metro_fetches"] = one("SELECT COUNT(*) FROM bike_fetches")
            last = one("SELECT MAX(fetched_at) FROM snapshots")
            if last:
                r = db.execute("""SELECT SUM(n.bikes), SUM(n.electrical), SUM(n.mechanical),
                                  SUM(n.bikes=0), SUM(n.docks_free=0), COUNT(*)
                                  FROM snapshots n JOIN stations s USING(number)
                                  WHERE n.fetched_at=? AND """ + METRO_SQL, (last,)).fetchone()
                out["latest"] = dict(zip(["t", "bikes", "electrical", "mechanical",
                                          "empty", "full", "stations"], (last, *r)))
    return out


def q_series(q):
    hours = float(q.get("hours", ["24"])[0])
    since = (now_utc() - timedelta(hours=hours)).isoformat()
    with db_connect(ro=True) as db:
        rows = db.execute("""SELECT n.fetched_at, SUM(n.bikes), SUM(n.electrical), SUM(n.bikes=0)
                             FROM snapshots n JOIN stations s USING(number)
                             WHERE n.fetched_at >= ? AND """ + METRO_SQL + """
                             GROUP BY n.fetched_at ORDER BY n.fetched_at""", (since,)).fetchall()
    return [{"t": r[0], "bikes": r[1], "electrical": r[2], "empty": r[3]} for r in rows]


def q_trips(q):
    """Bikes seen at station A in one bike fetch and at station B in the next."""
    if not os.path.exists(DB):
        return {"fetches": [], "trips": []}
    with db_connect(ro=True) as db:
        try:
            fetches = [r[0] for r in db.execute(
                "SELECT DISTINCT fetched_at FROM bike_snapshots ORDER BY fetched_at")]
        except sqlite3.Error:
            return {"fetches": [], "trips": []}
        names = dict(db.execute("SELECT number, name FROM stations"))
        vil = {r[0] for r in db.execute(
            "SELECT number FROM stations WHERE commune=?", (COMMUNE,))}
        rows = db.execute("""SELECT bike_number, fetched_at, station_number, type
                             FROM bike_snapshots ORDER BY bike_number, fetched_at""").fetchall()
    idx = {t: i for i, t in enumerate(fetches)}
    trips, prev = [], {}
    for bike, t, st, typ in rows:
        p = prev.get(bike)
        if p and idx[t] == idx[p[0]] + 1 and p[1] != st and (p[1] in vil or st in vil):
            trips.append({"bike": bike, "type": typ, "from": p[1], "to": st,
                          "from_name": names.get(p[1], str(p[1])),
                          "to_name": names.get(st, str(st)),
                          "after": p[0], "before": t})
        prev[bike] = (t, st)
    trips.sort(key=lambda x: x["before"], reverse=True)
    pairs = defaultdict(int)
    for tr in trips:
        pairs[(tr["from_name"], tr["to_name"])] += 1
    top = sorted(({"from": a, "to": b, "n": n} for (a, b), n in pairs.items()),
                 key=lambda x: -x["n"])[:10]
    return {"fetches": fetches, "trips": trips[:300], "total": len(trips), "top": top}


def q_files(_q):
    out = []
    for name in sorted(os.listdir(DATA), reverse=True):
        if re.match(r"^[\w.\-]+\.(csv|log)$", name):
            st = os.stat(os.path.join(DATA, name))
            out.append({"name": name, "bytes": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    return out


def q_log(_q):
    try:
        with open(LOG, encoding="utf-8") as f:
            return f.read().splitlines()[-60:]
    except FileNotFoundError:
        return []


# map endpoints (same shapes as velov_map.py)
def q_times(_q):
    with db_connect(ro=True) as db:
        return [r[0] for r in db.execute(
            "SELECT DISTINCT fetched_at FROM snapshots ORDER BY fetched_at")]


def q_snapshot(q):
    t = q.get("t", [None])[0]
    with db_connect(ro=True) as db:
        if not t:
            t = db.execute("SELECT MAX(fetched_at) FROM snapshots").fetchone()[0]
        rows = db.execute(
            """SELECT s.number, s.name, s.address, s.lat, s.lng, s.bike_stands,
                      n.bikes, n.docks_free, n.mechanical, n.electrical, n.status, n.last_update
               FROM snapshots n JOIN stations s USING(number) WHERE n.fetched_at = ?""",
            (t,)).fetchall()
    keys = ["number", "name", "address", "lat", "lng", "stands", "bikes", "docks",
            "mechanical", "electrical", "status", "last_update"]
    return {"t": t, "stations": [dict(zip(keys, r)) for r in rows]}


def q_history(q):
    n = int(q["n"][0])
    with db_connect(ro=True) as db:
        rows = db.execute("""SELECT fetched_at, bikes, mechanical, electrical, docks_free
                             FROM snapshots WHERE number=? ORDER BY fetched_at""", (n,)).fetchall()
    return [{"t": r[0], "bikes": r[1], "mechanical": r[2], "electrical": r[3], "docks": r[4]}
            for r in rows]


def q_flows(q):
    with db_connect(ro=True) as db:
        rows = db.execute("""SELECT number, fetched_at, bikes FROM snapshots
                             WHERE bikes IS NOT NULL ORDER BY number, fetched_at""").fetchall()
    out = defaultdict(lambda: {"out": 0, "in": 0, "rebalance": 0})
    prev = {}
    for n, _t, b in rows:
        if n in prev:
            d = b - prev[n]
            if abs(d) >= REBALANCE:
                out[n]["rebalance"] += 1
            elif d < 0:
                out[n]["out"] -= d
            elif d > 0:
                out[n]["in"] += d
        else:
            out[n]
        prev[n] = b
    return {str(k): v for k, v in out.items()}


# ---------------------------------------------------------------- extensions
EXT_FILE = os.path.join(HERE, "velov_ext.py")
ext_state = {}
_ext = {"mod": None, "mtime": None}


def ext_module():
    m = os.path.getmtime(EXT_FILE)
    if _ext["mod"] is None or m != _ext["mtime"]:
        spec = importlib.util.spec_from_file_location("velov_ext", EXT_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ext.update(mod=mod, mtime=m)
    return _ext["mod"]


def ext_call(prefix, name, arg):
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        raise KeyError(name)
    fn = getattr(ext_module(), f"{prefix}_{name}", None)
    if fn is None:
        raise KeyError(name)
    return fn(arg, {"state": ext_state, "db": lambda: db_connect(ro=True), "data": DATA})


# ---------------------------------------------------------------- autostart
def startup_file():
    appdata = os.environ.get("APPDATA")
    if not WIN or not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "Velov Control.pyw")


def autostart_enabled():
    p = startup_file()
    return bool(p and os.path.exists(p))


def set_autostart(on):
    p = startup_file()
    if not p:
        raise RuntimeError("Not needed here: Docker restarts the app by itself.")
    if on:
        app = os.path.join(HERE, "velov_app.pyw")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Starts Velo'v Control at login. Delete this file to stop.\n"
                    "import os, runpy, sys\n"
                    f"app = {app!r}\n"
                    "os.chdir(os.path.dirname(app))\n"
                    "sys.argv = [app]\n"
                    "runpy.run_path(app, run_name='__main__')\n")
    elif os.path.exists(p):
        os.remove(p)


# ---------------------------------------------------------------- actions
def a_collector(body):
    with lock:
        if "on" in body:
            settings["collector_on"] = bool(body["on"])
        if "adaptive" in body:
            settings["adaptive"] = bool(body["adaptive"])
        if "interval_min" in body:
            settings["interval_min"] = int(body["interval_min"])
        save_settings(settings)
    if body.get("now"):
        wake.set()
    elif "on" in body or "interval_min" in body:
        wake.set()
    return {"ok": True}


def a_run(body):
    kind = body.get("job")
    if kind not in JOBS:
        raise ValueError("unknown job")
    return {"id": start_job(kind)}


def a_bikes_schedule(body):
    every = int(body.get("every_min") or 0)
    if every not in (0, 10, 15, 30, 60):
        raise ValueError("every_min must be 0, 10, 15, 30 or 60")
    with lock:
        settings["bikes_every_min"] = every
        save_settings(settings)
    return {"ok": True}


def a_autostart(body):
    set_autostart(bool(body.get("on")))
    return {"autostart": autostart_enabled()}


def a_restart(_body):
    def go():
        time.sleep(0.4)
        flags = (0x00000008 | 0x00000200 | NO_WINDOW) if WIN else 0
        subprocess.Popen([sys.executable, os.path.join(HERE, "velov_app.pyw"), "--replace"],
                         cwd=HERE, creationflags=flags, close_fds=True)
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


def a_shutdown(_body):
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


GET = {"/api/status": q_status, "/api/series": q_series, "/api/trips": q_trips,
       "/api/files": q_files, "/api/log": q_log, "/api/times": q_times,
       "/api/snapshot": q_snapshot, "/api/history": q_history, "/api/flows": q_flows,
       "/api/jobs": lambda q: sorted(jobs.values(), key=lambda j: -int(j["id"]))}
POST = {"/api/collector": a_collector, "/api/run": a_run, "/api/bikes_schedule": a_bikes_schedule,
        "/api/autostart": a_autostart, "/api/shutdown": a_shutdown,
        "/api/restart": a_restart}


# Requests arriving through a Cloudflare tunnel carry Cf-* headers. They get the full app, exactly
# as at home, only with a valid Cloudflare Access login (a signed token checked here against the
# team's public keys, audience and allowed emails). Without one, only the phone page is served.
ACCESS_TEAM = os.environ.get("VELOV_ACCESS_TEAM", "").strip().removesuffix(".cloudflareaccess.com")
ACCESS_AUD = os.environ.get("VELOV_ACCESS_AUD", "").strip()
ACCESS_EMAILS = {e.strip().lower() for e in os.environ.get("VELOV_ACCESS_EMAILS", "").split(",") if e.strip()}
_access = {"keys": {}, "at": 0, "seen": {}, "err": None, "last": None}


def _access_note(result, reason, email=None):
    """Last outcome of a remote login check, shown in /api/status (home network only)."""
    _access["last"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "result": result, "reason": reason, "email": email}
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def access_keys(refresh=False):
    if refresh or not _access["keys"] or time.time() - _access["at"] > 3600:
        url = f"https://{ACCESS_TEAM}.cloudflareaccess.com/cdn-cgi/access/certs"
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        _access["keys"] = {k["kid"]: (int.from_bytes(_b64(k["n"]), "big"), int.from_bytes(_b64(k["e"]), "big"))
                           for k in d.get("keys", []) if k.get("kty") == "RSA" and k.get("kid")}
        _access["at"] = time.time()
    return _access["keys"]


def verify_access(token):
    """Email of a valid Cloudflare Access token (RS256), else None. Standard library only."""
    if not (ACCESS_TEAM and ACCESS_AUD):
        _access_note("refused", "remote login not configured (VELOV_ACCESS_TEAM / VELOV_ACCESS_AUD empty)")
        return None
    if not token:
        _access_note("refused", "no Cloudflare Access token in the request: is the Access application "
                                "applied to this hostname, and did you log in with the email code?")
        return None
    hit = _access["seen"].get(token)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        h, p, sig = token.split(".")
        head = json.loads(_b64(h))
        if head.get("alg") != "RS256":
            _access_note("refused", f"unexpected token algorithm {head.get('alg')!r}")
            return None
        try:
            key = access_keys().get(head.get("kid")) or access_keys(refresh=True).get(head.get("kid"))
        except Exception as e:
            _access_note("refused", f"could not download the keys from https://{ACCESS_TEAM}.cloudflareaccess.com "
                                    f"(wrong team name, or no internet from the container): {e!r}")
            return None
        if not key:
            _access_note("refused", f"token signed by a key that {ACCESS_TEAM}.cloudflareaccess.com does not publish "
                                    "(team name probably wrong)")
            return None
        n, e = key
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(_b64(sig), "big"), e, n).to_bytes(k, "big")
        digest = hashlib.sha256(f"{h}.{p}".encode()).digest()
        want = b"\x00\x01" + b"\xff" * (k - 3 - len(_SHA256_INFO) - len(digest)) + b"\x00" + _SHA256_INFO + digest
        if not hmac.compare_digest(em, want):
            _access_note("refused", "invalid token signature")
            return None
        c = json.loads(_b64(p))
        now = time.time()
        aud = c.get("aud")
        aud = [aud] if isinstance(aud, str) else (aud or [])
        email = (c.get("email") or "").lower()
        why = ("token expired (check the Pi's clock)" if c.get("exp", 0) < now else
               "token not valid yet (check the Pi's clock)" if c.get("nbf", 0) > now + 60 else
               "token is for another Access application: VELOV_ACCESS_AUD does not match its AUD tag" if ACCESS_AUD not in aud else
               f"token issued by {c.get('iss')}, not https://{ACCESS_TEAM}.cloudflareaccess.com: check VELOV_ACCESS_TEAM"
               if c.get("iss") != f"https://{ACCESS_TEAM}.cloudflareaccess.com" else
               "this email is not in VELOV_ACCESS_EMAILS" if ACCESS_EMAILS and email not in ACCESS_EMAILS else None)
        if why:
            _access_note("refused", why, email)
            return None
        if len(_access["seen"]) > 200:
            _access["seen"].clear()
        _access["seen"][token] = (min(c["exp"], now + 600), email)
        _access["err"] = None
        _access_note("accepted", "valid Cloudflare Access login", email)
        return email
    except Exception as e:
        _access["err"] = repr(e)
        _access_note("refused", f"unreadable token: {e!r}")
        return None


PUBLIC_PATHS = {"/near", "/api/x/near", "/api/x/docks"}
PAGES = {"/bikes": "velov_bikes.html", "/map": "velov_map.html", "/heat": "velov_heat.html", "/replay": "velov_replay.html",
         "/near": "velov_near.html"}
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def tunneled(self):
        return any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "Cf-Ray", "Cdn-Loop"))

    def restricted(self):
        """True for an internet request without a valid Cloudflare Access login."""
        if not self.tunneled():
            return False
        token = self.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            m = re.search(r"(?:^|;\s*)CF_Authorization=([^;]+)", self.headers.get("Cookie") or "")
            token = m.group(1) if m else None
        return verify_access(token) is None

    def send(self, code, body, ctype, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False, default=str),
                  "application/json; charset=utf-8")

    def page(self, name):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if self.restricted() and u.path not in PUBLIC_PATHS and not u.path.startswith("/static/"):
                if u.path in ("/", "/index.html"):
                    return self.send(302, "", "text/plain", {"Location": "/near"})
                return self.send(403, "Only the nearby-stations page is available from outside.", "text/plain")
            if u.path in ("/", "/index.html"):
                return self.page("velov_app.html")
            if u.path in PAGES:
                return self.page(PAGES[u.path])
            if u.path.startswith("/static/"):
                name = os.path.basename(u.path[8:])
                ext = os.path.splitext(name)[1]
                if ext not in STATIC_TYPES or not re.match(r"^[\w.\-]+$", name):
                    return self.send(404, "not found", "text/plain")
                with open(os.path.join(HERE, "static", name), "rb") as f:
                    return self.send(200, f.read(), STATIC_TYPES[ext], {"Cache-Control": "max-age=86400"})
            if u.path.startswith("/files/"):
                name = os.path.basename(u.path[7:])
                if not re.match(r"^[\w.\-]+\.(csv|log)$", name):
                    return self.send(404, "not found", "text/plain")
                with open(os.path.join(DATA, name), "rb") as f:
                    return self.send(200, f.read(), "text/csv; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path == "/api/job":
                j = jobs.get(parse_qs(u.query).get("id", [""])[0])
                return self.json(j or {"error": "unknown job"}, 200 if j else 404)
            if u.path.startswith("/api/x/"):
                try:
                    q = parse_qs(u.query)
                    q.pop("_public", None)
                    if self.restricted():
                        q["_public"] = ["1"]
                    return self.json(ext_call("get", u.path[7:], q))
                except KeyError:
                    return self.send(404, "not found", "text/plain")
            fn = GET.get(u.path)
            if not fn:
                return self.send(404, "not found", "text/plain")
            self.json(fn(parse_qs(u.query)))
        except FileNotFoundError as e:
            self.json({"error": f"missing file: {e.filename}"}, 404)
        except Exception as e:
            self.json({"error": repr(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        fn = POST.get(u.path)
        if u.path.startswith("/api/x/"):
            name = u.path[7:]
            fn = lambda body: ext_call("post", name, body)
        if not fn:
            return self.send(404, "not found", "text/plain")
        if self.restricted():
            return self.json({"error": "read-only from outside"}, 403)
        # only accept requests from our own page (https when it comes through the tunnel)
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host") or ""
        if origin and origin not in ("http://" + host, "https://" + host):
            return self.json({"error": "forbidden origin"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self.json(fn(body))
        except Exception as e:
            self.json({"error": str(e)}, 400)


def port_owner():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2) as r:
            return json.load(r).get("app")
    except Exception:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return "other"
        except OSError:
            return None


def alert(msg):
    if WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Vélo'v Control", 0x40)
            return
        except Exception:
            pass
    print(msg)


def main():
    if "--replace" in sys.argv:          # restarting: wait for the old instance to exit
        for _ in range(40):
            if port_owner() is None:
                break
            time.sleep(0.5)
    owner = port_owner()
    if owner == APP_ID:
        alert(f"Vélo'v Control is already running.\nOpen http://localhost:{PORT}")
        return
    if owner:
        alert(f"Port {PORT} is used by another program, probably the old map window "
              "(start_velov_map.bat).\n\nClose that window, then start Vélo'v Control again.")
        return
    with db_connect() as db:
        db.executescript(SCHEMA)
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=bike_scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Vélo'v Control running at http://localhost:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(HERE, "velov_app_error.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()}\n{traceback.format_exc()}\n")
        alert("Vélo'v Control could not start. Details are in velov_app_error.log.")
