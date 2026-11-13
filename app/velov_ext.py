"""
Vélo'v Control extensions. Reloaded automatically when this file changes,
so new endpoints don't need an app restart.

GET  /api/x/<name>  -> get_<name>(query, ctx)
POST /api/x/<name>  -> post_<name>(body, ctx)

ctx = {"state": dict kept across reloads, "db": connect function}
"""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.cyclocity.fr"
WEB_KEY = {"code": "vls.web.lyon:PRD",
           "key": "c3d9f5c22a9157a7cc7fe0e38269573bdd2f13ec48f867360ecdcbd35b196f87"}
UA = "velov-control/1.0 (personal research)"
DOCK_CACHE_S = 60          # one live request per station per minute at most
TOKEN_TTL_S = 100 * 60     # tokens last 2 h; renew a little early


def _req(url, data=None, headers=None):
    h = {"User-Agent": UA, **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h),
                                timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _token(state):
    t = state.get("token")
    if t and time.time() - state.get("token_at", 0) < TOKEN_TTL_S:
        return t
    t = _req(f"{API}/auth/environments/PRD/client_tokens", WEB_KEY)["accessToken"]
    state.update(token=t, token_at=time.time())
    return t


def stars_of(rating, checked=False):
    """The official app shows 1-3 stars. Votes are worth 100 (recommended), 60 or 30
    (not recommended, by defect), and `value` is their average since the last
    workshop check; 100/60/30 read as 3/2/1 stars. None = no vote, 0 = "Révisé"."""
    r = rating or {}
    v, n = r.get("value"), r.get("count") or 0
    if not n or v is None:
        return 0 if checked else None
    return 3 if v >= 80 else 2 if v >= 45 else 1


def is_foreign(number):
    """99xxx bikes show up in the feed but are not part of the Vélo'v network."""
    return isinstance(number, int) and 99000 <= number <= 99999


def _battery(b):
    bat = b.get("battery") or {}
    p = bat.get("percentage")
    if isinstance(p, (int, float)) and 0 <= p <= 100 and bat.get("type") != "REMOVABLE":
        return round(p)
    return None


PUBLIC_DOCKS_PER_MIN = 20   # live lookups allowed per minute for requests from the internet


def get_docks(q, ctx):
    n = int(q["n"][0])
    state = ctx["state"]
    cache = state.setdefault("docks", {})
    hit = cache.get(n)
    if hit and time.time() - hit[0] < DOCK_CACHE_S:
        return hit[1]
    if "_public" in q:
        recent = [t for t in state.get("public_docks", []) if time.time() - t < 60]
        if len(recent) >= PUBLIC_DOCKS_PER_MIN:
            if hit:
                return hit[1]
            raise RuntimeError("busy, try again in a minute")
        state["public_docks"] = recent + [time.time()]

    with ctx["db"]() as db:
        meta = db.execute("SELECT name, bike_stands FROM stations WHERE number=?", (n,)).fetchone()
        snap = db.execute("""SELECT fetched_at, bikes, docks_free, mechanical, electrical
                             FROM snapshots WHERE number=? ORDER BY fetched_at DESC LIMIT 1""",
                          (n,)).fetchone()
    name, capacity = (meta or (str(n), None))
    free = snap[2] if snap else None

    bikes, live, at, error = [], True, None, None
    try:
        raw = _req(f"{API}/contracts/lyon/bikes?stationNumber={n}",
                   headers={"Authorization": f"Taknv1 {_token(state)}",
                            "Accept": "application/vnd.bikes.v4+json"})
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for b in raw:
            r = b.get("rating") or {}
            bikes.append({"stand": b.get("standNumber"), "number": b.get("number"),
                          "type": b.get("type"), "reserved": bool(b.get("isReserved")),
                          "status": b.get("status"), "battery": _battery(b),
                          "rating": r.get("value"), "ratings": r.get("count") or 0,
                          "stars": stars_of(r, b.get("checked")), "checked": bool(b.get("checked"))})
    except Exception as e:
        # fall back to the last bike-number fetch stored in the database
        live, error = False, repr(e)
        state.pop("token", None)
        with ctx["db"]() as db:
            try:
                last = db.execute("SELECT MAX(fetched_at) FROM bike_snapshots "
                                  "WHERE station_number=?", (n,)).fetchone()[0]
                rows = db.execute("""SELECT stand_number, bike_number, type, reserved, status
                                     FROM bike_snapshots WHERE station_number=? AND fetched_at=?""",
                                  (n, last)).fetchall() if last else []
            except Exception:
                last, rows = None, []
        at = last
        bikes = [{"stand": s, "number": num, "type": t, "reserved": bool(rv), "status": st,
                  "battery": None, "rating": None, "ratings": 0, "stars": None, "checked": False}
                 for s, num, t, rv, st in rows]

    # a non-Vélo'v bike (99xxx) still fills its dock, but is shown without its number or details
    for b in bikes:
        if is_foreign(b["number"]):
            b.update(foreign=True, number=None, type=None, reserved=False, battery=None,
                     rating=None, ratings=0, stars=None, checked=False)
    by_stand = {b["stand"]: b for b in bikes if b["stand"] is not None}
    size = max([capacity or 0, *by_stand.keys()] or [0])
    docks = [{"stand": i, "bike": by_stand.get(i)} for i in range(1, size + 1)]
    empty = sum(1 for d in docks if not d["bike"])
    # docks holding a bike that is out of service are not listed by the API
    unavailable = max(0, empty - free) if (free is not None and live) else None

    out = {"number": n, "name": name, "capacity": capacity, "live": live, "at": at,
           "error": error, "docks": docks, "free": free, "unavailable": unavailable,
           "counts": {"mechanical": sum(1 for b in bikes if b["type"] == "MECHANICAL"),
                      "electrical": sum(1 for b in bikes if b["type"] == "ELECTRICAL"),
                      "reserved": sum(1 for b in bikes if b["reserved"])}}
    cache[n] = (time.time(), out)
    return out


# ---------------------------------------------------------------- boundaries
import os

WFS = ("https://data.grandlyon.com/geoserver/metropole-de-lyon/ows?SERVICE=WFS&VERSION=2.0.0"
       "&request=GetFeature&typename={layer}&outputFormat=application/json&SRSNAME=EPSG:4326")
LAYERS = {
    "communes": ["metropole-de-lyon:adr_voie_lieu.adrcomgl"],
    "arrondissements": ["metropole-de-lyon:adr_voie_lieu.adrarrond",
                        "metropole-de-lyon:adr_voie_lieu.adrarrondlyon"],
}
BOUNDARY_FILE = os.path.join(os.environ.get("VELOV_DATA") or os.path.dirname(os.path.abspath(__file__)), "velov_boundaries.json")


def _round_coords(c):
    if isinstance(c, (int, float)):
        return round(c, 5)
    return [_round_coords(x) for x in c]


def _name(props):
    for k in ("nom", "nomreduit", "libelle", "name", "nom_arrondissement"):
        if props.get(k):
            return props[k]
    return ""


def get_boundaries(q, ctx):
    if os.path.exists(BOUNDARY_FILE) and "refresh" not in q:
        with open(BOUNDARY_FILE, encoding="utf-8") as f:
            return json.load(f)
    out, errors = {}, {}
    for key, candidates in LAYERS.items():
        for layer in candidates:
            try:
                fc = _req(WFS.format(layer=layer))
                feats = []
                for ft in fc.get("features", []):
                    g = ft.get("geometry")
                    if not g:
                        continue
                    p = ft.get("properties") or {}
                    feats.append({"type": "Feature",
                                  "properties": {"name": _name(p), "insee": p.get("insee")},
                                  "geometry": {"type": g["type"],
                                               "coordinates": _round_coords(g["coordinates"])}})
                if feats:
                    out[key] = {"type": "FeatureCollection", "features": feats}
                    errors.pop(key, None)
                    break
            except Exception as e:
                errors[key] = f"{layer}: {e!r}"
    out["errors"] = errors
    if "communes" in out and "arrondissements" in out:
        with open(BOUNDARY_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return out


# ---------------------------------------------------------------- public export
from collections import Counter, defaultdict
from datetime import timedelta

PUBLIC_FILE = os.path.join(os.environ.get("VELOV_DATA") or os.path.dirname(os.path.abspath(__file__)), "velov_public.json")
TRUCK_MIN = 4   # this many bikes moving between the same two stations in one window = rebalancing


def get_public(q, ctx):
    """Write a compact, read-only summary to velov_public.json (for the shared page)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    since = (now - timedelta(hours=24)).isoformat()
    with ctx["db"]() as db:
        last = db.execute("SELECT MAX(fetched_at) FROM snapshots").fetchone()[0]
        st = db.execute("""SELECT s.number, s.name, s.commune, s.lat, s.lng, s.bike_stands,
                                  n.bikes, n.mechanical, n.electrical, n.docks_free, n.status
                           FROM snapshots n JOIN stations s USING(number)
                           WHERE n.fetched_at=?""", (last,)).fetchall()
        series = db.execute("""SELECT n.fetched_at, SUM(n.bikes), SUM(n.electrical),
                                      SUM(CASE WHEN s.commune='Villeurbanne' THEN n.bikes END),
                                      SUM(CASE WHEN s.commune='Villeurbanne' THEN n.electrical END),
                                      COUNT(*)
                               FROM snapshots n JOIN stations s USING(number)
                               WHERE n.fetched_at >= ? GROUP BY n.fetched_at ORDER BY n.fetched_at""",
                            (since,)).fetchall()
        try:
            moves = db.execute("""SELECT bike_number, type, from_station, to_station, left_after,
                                         arrived_before, loop FROM bike_moves
                                  WHERE arrived_before >= ?""", (since,)).fetchall()
            fetches = db.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM bike_fetches").fetchone()
            total_moves = db.execute("SELECT COUNT(*) FROM bike_moves").fetchone()[0]
        except Exception:
            moves, fetches, total_moves = [], (0, None, None), 0
        first = db.execute("SELECT MIN(fetched_at), COUNT(DISTINCT fetched_at) FROM snapshots").fetchone()

    names = {r[0]: r[1] for r in st}
    # rebalancing: many bikes, same pair, same arrival window
    groups = Counter((m[2], m[3], m[5]) for m in moves if not m[6])
    trucks = {k for k, c in groups.items() if c >= TRUCK_MIN}
    rides = [m for m in moves if (m[2], m[3], m[5]) not in trucks]
    reb = [m for m in moves if (m[2], m[3], m[5]) in trucks]

    out_c, in_c = Counter(), Counter()
    pairs = Counter()
    hourly = defaultdict(lambda: [0, 0])          # hour start -> [mechanical, electric]
    durations = []
    for b, typ, a, z, t0, t1, loop in rides:
        out_c[a] += 1
        in_c[z] += 1
        if not loop:
            pairs[(a, z)] += 1
        h = t1[:13] + ":00:00+00:00"
        hourly[h][1 if typ == "ELECTRICAL" else 0] += 1
        try:
            durations.append((datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 60)
        except Exception:
            pass

    def short(n):
        return re.sub(r"^\d+\s*-\s*", "", names.get(n, str(n)))

    # keep the Métropole only (n°202 is a JCDecaux test platform near Paris)
    st = [r for r in st if r[3] is not None and 45.55 < r[3] < 45.95 and 4.65 < r[4] < 5.15]
    stations = [[n, short(n), c, round(la, 5), round(lo, 5), cap, b, me, el, d,
                 0 if s == "CLOSED" else 1, out_c.get(n, 0), in_c.get(n, 0)]
                for n, _nm, c, la, lo, cap, b, me, el, d, s in st if la is not None]
    data = {
        "generated": now.isoformat(), "last_snapshot": last,
        "collecting_since": first[0], "snapshots": first[1],
        "bike_fetches": fetches[0], "moves_total": total_moves,
        "station_fields": ["number", "name", "commune", "lat", "lng", "stands", "bikes",
                           "mechanical", "electrical", "docks", "open", "out24", "in24"],
        "stations": stations,
        "series": [[t, b, e, vb, ve, k] for t, b, e, vb, ve, k in series],
        "rides24": len(rides), "loops24": sum(1 for m in rides if m[6]),
        "rebalanced24": len(reb),
        "e_share24": round(sum(1 for m in rides if m[1] == "ELECTRICAL") / len(rides), 3) if rides else None,
        "median_gap_min": sorted(durations)[len(durations) // 2] if durations else None,
        "hourly": sorted([h, v[0], v[1]] for h, v in hourly.items()),
        "top_routes": [[short(a), short(z), c] for (a, z), c in pairs.most_common(12)],
    }
    if "compact" in q:
        return _compact(data)
    with open(PUBLIC_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return {"ok": True, "file": os.path.basename(PUBLIC_FILE), "bytes": os.path.getsize(PUBLIC_FILE),
            "stations": len(stations), "rides24": len(rides)}


def _compact(d):
    """Same content as velov_public.json, ~4x smaller: station names and places are
    built into the shared page, so only the changing numbers travel."""
    t0 = d["series"][0][0] if d["series"] else d["generated"]
    base = datetime.fromisoformat(t0)
    series, last_q = [], None
    for t, _b, _e, vb, ve, _k in d["series"]:
        if vb is None:
            continue
        q = t[:14] + f"{int(t[14:16]) // 15 * 15:02d}"          # one point per quarter hour
        if q == last_q:
            continue
        last_q = q
        series.append([round((datetime.fromisoformat(t) - base).total_seconds() / 60), vb, ve])
    st = ";".join(f"{r[0]}:{r[6]}.{r[7]}.{r[8]}.{r[9]}.{r[10]}.{r[11]}.{r[12]}" for r in d["stations"])
    h0 = d["hourly"][0][0] if d["hourly"] else d["generated"]
    hbase = datetime.fromisoformat(h0)
    hourly = [[round((datetime.fromisoformat(h) - hbase).total_seconds() / 3600), m, e] for h, m, e in d["hourly"]]
    return {"v": 2, "g": d["generated"], "ls": d["last_snapshot"], "cs": d["collecting_since"],
            "sn": d["snapshots"], "bf": d["bike_fetches"], "mt": d["moves_total"],
            "r24": d["rides24"], "l24": d["loops24"], "rb24": d["rebalanced24"], "e24": d["e_share24"],
            "s": st, "t0": t0, "se": series, "h0": h0, "h": hourly, "tr": d["top_routes"]}


# ---------------------------------------------------------------- about page
def get_about(q, ctx):
    """Live figures for the About tab: what was collected, when, and how much is stored."""
    try:
        from zoneinfo import ZoneInfo
        paris = ZoneInfo("Europe/Paris")
    except Exception:
        paris = timezone(timedelta(hours=1))
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    today0 = datetime.now(paris).replace(hour=0, minute=0, second=0, microsecond=0)
    today0 = today0.astimezone(timezone.utc).isoformat()
    out = {"tables": {}, "hours": [], "today": {}}
    with ctx["db"]() as db:
        def one(sql, *a):
            try:
                return db.execute(sql, a).fetchone()[0]
            except Exception:
                return None
        for name, sql in [
            ("station readings", "SELECT COUNT(*) FROM snapshots"),
            ("station snapshots", "SELECT COUNT(DISTINCT fetched_at) FROM snapshots"),
            ("stations known", "SELECT COUNT(*) FROM stations"),
            ("bike-number fetches", "SELECT COUNT(*) FROM bike_fetches"),
            ("bike moves", "SELECT COUNT(*) FROM bike_moves"),
            ("bikes tracked", "SELECT COUNT(*) FROM bike_state"),
            ("bike ratings", "SELECT COUNT(*) FROM bike_ratings"),
        ]:
            out["tables"][name] = one(sql)
        out["first_snapshot"] = one("SELECT MIN(fetched_at) FROM snapshots")
        snaps = [r[0] for r in db.execute(
            "SELECT DISTINCT fetched_at FROM snapshots WHERE fetched_at >= ?", (since,))]
        try:
            fetches = [r[0] for r in db.execute(
                "SELECT fetched_at FROM bike_fetches WHERE fetched_at >= ?", (since,))]
        except Exception:
            fetches = []
        out["today"] = {
            "snapshots": sum(1 for t in snaps if t >= today0),
            "bike_fetches": sum(1 for t in fetches if t >= today0),
        }
        try:
            out["stars"] = dict(db.execute(
                "SELECT COALESCE(stars, -1), COUNT(*) FROM bike_ratings GROUP BY 1").fetchall())
        except Exception:
            out["stars"] = {}
    # per local hour over the last 24 h, oldest first
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    buckets = [[(start + timedelta(hours=i)).isoformat(), 0, 0] for i in range(24)]
    def bucket(t):
        i = int((datetime.fromisoformat(t) - start).total_seconds() // 3600)
        return buckets[i] if 0 <= i < 24 else None
    for t in snaps:
        b = bucket(t)
        if b: b[1] += 1
    for t in fetches:
        b = bucket(t)
        if b: b[2] += 1
    out["hours"] = buckets
    db_path = os.path.join(ctx.get("data") or ".", "velov.sqlite")
    out["db_bytes"] = os.path.getsize(db_path) if os.path.exists(db_path) else None
    out["data_dir"] = ctx.get("data")
    return out


# ---------------------------------------------------------------- heatmaps
import math
import sqlite3
import threading
try:
    from zoneinfo import ZoneInfo
    _LOCAL = ZoneInfo("Europe/Paris")
except Exception:                       # Windows Python without tzdata: winter time is close enough
    _LOCAL = timezone(timedelta(hours=1))
HEAT_HOURS = {"all": None, "morning": (6, 10), "day": (10, 16), "evening": (16, 20), "night": (20, 6)}
HEAT_PAIRS_MAX = 6000
DIST_BINS = [0.5, 1, 2, 3, 5]          # km, straight line
WX_DRIZZLE, WX_RAIN = 0.2, 1.0          # mm per hour


def _short(name, n):
    return re.sub(r"^\s*\d+\s*\W+\s*", "", name or "") or str(n)


def _is_workshop(name):
    return "ATELIER" in (name or "").upper()


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _iso(t):
    return datetime.fromisoformat(t)


def _local_day_bounds(day):
    d0 = datetime.fromisoformat(day).replace(tzinfo=_LOCAL)
    return d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)


# ---- side cache: weather and street routes (kept apart from the collector's database)
def _cache(ctx):
    db = sqlite3.connect(os.path.join(ctx["data"], "velov_cache.sqlite"), timeout=10)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS weather (hour TEXT PRIMARY KEY, precip REAL, temp REAL, code INTEGER);
      CREATE TABLE IF NOT EXISTS routes (k TEXT PRIMARY KEY, poly TEXT, dist REAL, dur REAL, ok INTEGER, at TEXT);""")
    return db


def _wx_class(mm):
    return None if mm is None else "dry" if mm < WX_DRIZZLE else "drizzle" if mm < WX_RAIN else "rain"


def _weather_refresh(ctx):
    """Hourly rain and temperature for Lyon from Open-Meteo (free, no key): the last 92 days
    in one call, at most once an hour, in the background. Stored, so history builds up."""
    st = ctx["state"]
    if time.time() - st.get("wx_at", 0) < 3600 or st.get("wx_busy"):
        return
    st["wx_busy"] = True

    def run():
        try:
            d = _req("https://api.open-meteo.com/v1/forecast?latitude=45.76&longitude=4.85"
                     "&hourly=precipitation,temperature_2m,weather_code&past_days=92&forecast_days=1&timezone=GMT")
            h = d["hourly"]
            rows = [(t[:13], p, te, c) for t, p, te, c in zip(h["time"], h["precipitation"],
                                                            h["temperature_2m"], h["weather_code"]) if p is not None]
            with _cache(ctx) as db:
                db.executemany("INSERT OR REPLACE INTO weather VALUES (?,?,?,?)", rows)
            st["wx_at"], st["wx_err"] = time.time(), None
        except Exception as e:
            st["wx_err"], st["wx_at"] = repr(e), time.time() - 3000      # retry in ~10 min
        finally:
            st["wx_busy"] = False
    threading.Thread(target=run, daemon=True).start()


def _weather(ctx):
    _weather_refresh(ctx)
    try:
        with _cache(ctx) as db:
            return {h: (p, te, c) for h, p, te, c in db.execute("SELECT * FROM weather")}
    except Exception:
        return {}


def get_heat(q, ctx):
    """Trips for the Heatmaps tab: who goes where, filtered by period or dates, hours, days,
    bike type and weather. A trip's time is the middle of the window it happened in (between
    two fetches), in Lyon time. Rebalancing (TRUCK_MIN+ bikes, same two stations, same
    window) is split out as trucks."""
    arg = lambda k, d: (q.get(k) or [d])[0]
    days, hours, dow, kind, wx = arg("days", "7"), arg("hours", "all"), arg("dow", "all"), arg("type", "all"), arg("wx", "all")
    d_from, d_to = arg("from", ""), arg("to", "")
    with ctx["db"]() as db:
        try:
            first, last = db.execute("SELECT MIN(left_after), MAX(arrived_before) FROM bike_moves").fetchone()
        except Exception:
            first = last = None
        st = db.execute("SELECT number, name, commune, lat, lng FROM stations").fetchall()
        moves, fetch_times = [], []
        if last:
            if d_from or d_to:
                lo = _local_day_bounds(d_from)[0].isoformat() if d_from else ""
                hi = _local_day_bounds(d_to)[1].isoformat() if d_to else "9999"
            else:
                lo = "" if days == "all" else (_iso(last) - timedelta(days=float(days))).isoformat()
                hi = "9999"
            moves = db.execute("""SELECT type, from_station, to_station, left_after, arrived_before, loop
                                  FROM bike_moves WHERE arrived_before > ? AND arrived_before <= ?""", (lo, hi)).fetchall()
            fetch_times = [r[0] for r in db.execute(
                "SELECT fetched_at FROM bike_fetches WHERE fetched_at > ? AND fetched_at <= ?", (lo, hi))]
    stations = [[n, _short(nm, n), c, round(la, 5), round(lo_, 5)]
                for n, nm, c, la, lo_ in st
                if la is not None and 45.55 < la < 45.95 and 4.65 < lo_ < 5.15 and not _is_workshop(nm)]
    pos = {s[0]: (s[3], s[4]) for s in stations}
    weather = _weather(ctx)

    groups = Counter((m[1], m[2], m[4]) for m in moves if not m[5])
    truck_keys = {k for k, c in groups.items() if c >= TRUCK_MIN}
    hr = HEAT_HOURS.get(hours)

    def time_ok(loc):
        if dow == "wd" and loc.weekday() >= 5 or dow == "we" and loc.weekday() < 5:
            return False
        h = loc.hour
        return not hr or (hr[0] <= h < hr[1] if hr[0] < hr[1] else (h >= hr[0] or h < hr[1]))

    def wx_of(utc):
        w = weather.get(utc.strftime("%Y-%m-%dT%H"))
        return _wx_class(w[0]) if w else None

    # hours actually observed (a fetch happened), per weather class: the denominator for rates
    seen_hours = {}
    for t in fetch_times:
        u = _iso(t).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        seen_hours[u] = True
    wx_hours = Counter()
    for u in seen_hours:
        if time_ok(u.astimezone(_LOCAL)):
            wx_hours[wx_of(u) or "unknown"] += 1

    pairs, trucks = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, set()])
    dep, arr, loops = Counter(), Counter(), Counter()
    hourly, weekly, gaps = [0] * 24, [0] * 7, []
    wx_rides = Counter()
    dist_bins = [[0, 0] for _ in range(len(DIST_BINS) + 1)]     # [mechanical, electric]
    dists = {"m": [], "e": []}
    rides = elec = loop_n = truck_bikes = 0
    span = [None, None]
    for typ, a, z, t0, t1, loop in moves:
        if a not in pos or z not in pos:
            continue
        e = typ == "ELECTRICAL"
        if kind == "mech" and e or kind == "elec" and not e:
            continue
        ta, tb = _iso(t0), _iso(t1)
        mid = ta + (tb - ta) / 2
        loc = mid.astimezone(_LOCAL)
        if not time_ok(loc):
            continue
        cls = wx_of(mid.astimezone(timezone.utc))
        if wx != "all" and cls != wx:
            continue
        if (a, z, t1) in truck_keys:
            trucks[(a, z)][0] += 1
            trucks[(a, z)][1].add(t1)
            truck_bikes += 1
            continue
        rides += 1
        elec += e
        hourly[loc.hour] += 1
        weekly[loc.weekday()] += 1
        wx_rides[cls or "unknown"] += 1
        gaps.append((tb - ta).total_seconds() / 60)
        span = [min(span[0] or t0, t0), max(span[1] or t1, t1)]
        if loop:
            loop_n += 1
            loops[a] += 1
            continue
        km = _km(pos[a], pos[z])
        dists["e" if e else "m"].append(km)
        dist_bins[sum(1 for b in DIST_BINS if km >= b)][1 if e else 0] += 1
        dep[a] += 1
        arr[z] += 1
        p = pairs[(a, z)]
        p[0] += 1
        p[1] += e
    top = sorted(pairs.items(), key=lambda kv: -kv[1][0])[:HEAT_PAIRS_MAX]
    gaps.sort()
    med = lambda v: round(sorted(v)[len(v) // 2], 2) if v else None
    wx_known = any(weather)
    return {
        "first": first, "last": last, "from": span[0], "to": span[1], "fetches": len(fetch_times),
        "filters": {"days": days, "hours": hours, "dow": dow, "type": kind, "wx": wx, "from": d_from, "to": d_to},
        "stations": stations,
        "rides": rides, "elec": elec, "loops": loop_n,
        "truck_bikes": truck_bikes, "truck_runs": sum(len(v[1]) for v in trucks.values()),
        "median_gap": round(gaps[len(gaps) // 2]) if gaps else None,
        "pairs": [[a, z, n, e] for (a, z), (n, e) in top],
        "pairs_total": len(pairs),
        "trucks": sorted(([a, z, n, len(w)] for (a, z), (n, w) in trucks.items()), key=lambda r: -r[2]),
        "dep": dep, "arr": arr, "loop_by": loops,
        "hourly": hourly, "weekly": weekly,
        "dist": {"bins": DIST_BINS, "counts": dist_bins, "median_m": med(dists["m"]), "median_e": med(dists["e"]),
                 "n": len(dists["m"]) + len(dists["e"])},
        "weather": {"ready": wx_known, "error": ctx["state"].get("wx_err") if not wx_known else None,
                    "hours": wx_hours, "rides": wx_rides,
                    "thresholds": [WX_DRIZZLE, WX_RAIN]},
    }


# ---- street routes: OpenStreetMap bike routing (FOSSGIS server), one request per pair, cached forever
ROUTER = "https://routing.openstreetmap.de/routed-bike/route/v1/driving/{a};{b}?overview=full&geometries=polyline"
ROUTE_MAX_KM = 20


def _rkey(a, b):
    return f"{a[0]:.4f},{a[1]:.4f};{b[0]:.4f},{b[1]:.4f}"


def _route_worker(ctx):
    st = ctx["state"]
    queue = st.setdefault("route_q", [])
    while queue:
        k = queue.pop(0)
        (la1, lo1), (la2, lo2) = [map(float, p.split(",")) for p in k.split(";")]
        try:
            d = _req(ROUTER.format(a=f"{lo1},{la1}", b=f"{lo2},{la2}"))
            r = d["routes"][0]
            row = (k, r["geometry"], r["distance"], r["duration"], 1)
            st["route_err"] = None
        except Exception as e:
            row = (k, None, None, None, 0)
            st["route_err"] = repr(e)
        try:
            with _cache(ctx) as db:
                db.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?)",
                           (*row, datetime.now(timezone.utc).isoformat()))
        except Exception:
            pass
        st["route_done"] = st.get("route_done", 0) + 1
        time.sleep(1.1)                  # the server's fair-use rule: at most one request a second
    st["route_busy"] = False


def post_routes(body, ctx):
    """body = {"pairs": [[lat,lng,lat,lng], ...]} -> cached routes; missing ones are queued
    and fetched in the background (about one a second). Failed ones are retried after a day."""
    want = {}
    for p in (body.get("pairs") or [])[:800]:
        a, b = (p[0], p[1]), (p[2], p[3])
        if _km(a, b) <= ROUTE_MAX_KM:
            want[_rkey(a, b)] = True
    out, missing = {}, []
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with _cache(ctx) as db:
        keys = list(want)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for k, poly, dist, dur, ok, at in db.execute(
                    f"SELECT * FROM routes WHERE k IN ({','.join('?' * len(chunk))})", chunk):
                if ok:
                    out[k] = [poly, round(dist), round(dur)]
                want[k] = bool(ok) or at > day_ago
    st = ctx["state"]
    queue = st.setdefault("route_q", [])
    qs = set(queue)
    for k, have in want.items():
        if not have and k not in qs:
            queue.append(k)
    if queue and not st.get("route_busy"):
        st["route_busy"] = True
        threading.Thread(target=_route_worker, args=(ctx,), daemon=True).start()
    return {"routes": out, "pending": len(queue), "error": st.get("route_err")}


# ---- replay of one day
def get_replay(q, ctx):
    """Everything to replay one day (Lyon time): station levels at each fetch and every trip."""
    with ctx["db"]() as db:
        days = db.execute("SELECT substr(fetched_at,1,10) d, COUNT(DISTINCT fetched_at) FROM snapshots "
                          "GROUP BY d ORDER BY d").fetchall()
        day = (q.get("day") or [None])[0]
        if not day and days:
            day = datetime.now(timezone.utc).astimezone(_LOCAL).date().isoformat()
            if day > days[-1][0]:
                day = days[-1][0]
        if not day:
            return {"days": [], "day": None}
        t0, t1 = _local_day_bounds(day)
        lo, hi = (t0 - timedelta(minutes=10)).isoformat(), (t1 + timedelta(minutes=10)).isoformat()
        st = db.execute("SELECT number, name, lat, lng, bike_stands FROM stations").fetchall()
        rows = db.execute("""SELECT fetched_at, number, bikes, electrical, docks_free FROM snapshots
                             WHERE fetched_at >= ? AND fetched_at < ? ORDER BY fetched_at""", (lo, hi)).fetchall()
        try:
            mv = db.execute("""SELECT bike_number, type, from_station, to_station, left_after, arrived_before, loop
                               FROM bike_moves WHERE arrived_before > ? AND left_after < ?""",
                            (t0.isoformat(), t1.isoformat())).fetchall()
        except Exception:
            mv = []
    stations = [s for s in st if s[2] is not None and 45.55 < s[2] < 45.95 and 4.65 < s[3] < 5.15
                and not _is_workshop(s[1])]
    idx = {s[0]: i for i, s in enumerate(stations)}
    frames = {}
    for t, n, b, e, d in rows:
        i = idx.get(n)
        if i is None:
            continue
        f = frames.setdefault(t, [[-1] * len(stations), [-1] * len(stations), [-1] * len(stations)])
        f[0][i], f[1][i], f[2][i] = b if b is not None else -1, e if e is not None else -1, d if d is not None else -1
    minute = lambda t: round((_iso(t) - t0).total_seconds() / 60, 1)
    groups = Counter((m[2], m[3], m[5]) for m in mv if not m[6])
    trips = [[b, 1 if typ == "ELECTRICAL" else 0, idx[a], idx[z], minute(ta), minute(tb),
              1 if groups[(a, z, tb)] >= TRUCK_MIN else 0, loop]
             for b, typ, a, z, ta, tb, loop in mv if a in idx and z in idx]
    # dayparts and weather strip for the clock
    weather = _weather(ctx)
    wx = []
    for h in range(25):
        u = (t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H")
        w = weather.get(u)
        wx.append([round(w[0], 1), round(w[1])] if w else None)
    return {"day": day, "days": [[d, n] for d, n in days],
            "start": t0.isoformat(),
            "stations": [[s[0], _short(s[1], s[0]), s[2], s[3], s[4]] for s in stations],
            "times": [minute(t) for t in sorted(frames)],
            "frames": [[",".join(map(str, c)) for c in frames[t]] for t in sorted(frames)],
            "trips": trips, "weather": wx}


# ---- one bike's story
def get_bike(q, ctx):
    n = int(q["n"][0])
    if is_foreign(n):
        return {"number": n, "foreign": True}
    with ctx["db"]() as db:
        names = {r[0]: _short(r[1], r[0]) for r in db.execute("SELECT number, name FROM stations")}
        try:
            mv = db.execute("""SELECT type, from_station, to_station, left_after, arrived_before, loop
                               FROM bike_moves WHERE bike_number=? ORDER BY arrived_before""", (n,)).fetchall()
            state = db.execute("SELECT type, station_number, stand_number, first_seen, last_seen FROM bike_state "
                               "WHERE bike_number=?", (n,)).fetchone()
        except Exception:
            mv, state = [], None
        try:
            rating = db.execute("SELECT stars, value, votes, checked, last_rating, updated_at FROM bike_ratings "
                                "WHERE bike_number=?", (n,)).fetchone()
        except Exception:
            rating = None
    pos = {}
    with ctx["db"]() as db:
        pos = {r[0]: (r[1], r[2]) for r in db.execute("SELECT number, lat, lng FROM stations")}
    km = sum(_km(pos[a], pos[z]) for _t, a, z, *_r in mv if a in pos and z in pos and a != z)
    return {"number": n, "type": state[0] if state else (mv[-1][0] if mv else None),
            "trips": [[a, names.get(a, str(a)), z, names.get(z, str(z)), t0, t1, loop] for _t, a, z, t0, t1, loop in mv],
            "km": round(km, 1),
            "now": {"station": state[1], "name": names.get(state[1], ""), "stand": state[2], "since": state[3],
                    "seen": state[4]} if state else None,
            "rating": dict(zip(["stars", "value", "votes", "checked", "last_rating", "updated_at"], rating)) if rating else None}


# ---- "Aujourd'hui" tab: today in Lyon at a glance
def _moves_between(db, lo, hi):
    return db.execute("""SELECT type, from_station, to_station, left_after, arrived_before, loop
                         FROM bike_moves WHERE arrived_before > ? AND arrived_before <= ?""", (lo, hi)).fetchall()


def get_today(q, ctx):
    """Today's trips hour by hour against a typical day, the busiest stations, bikes away from
    stations, and the weather for the rest of the day. Falls back to the last day with trips."""
    now = datetime.now(timezone.utc)
    with ctx["db"]() as db:
        st = {r[0]: (_short(r[1], r[0]), r[2], r[3]) for r in db.execute("SELECT number, name, lat, lng FROM stations")
              if r[2] is not None and 45.55 < r[2] < 45.95 and 4.65 < r[3] < 5.15 and not _is_workshop(r[1])}
        try:
            last_move = db.execute("SELECT MAX(arrived_before) FROM bike_moves").fetchone()[0]
        except Exception:
            last_move = None
        day = now.astimezone(_LOCAL).date().isoformat()
        stale = False
        t0, t1 = _local_day_bounds(day)
        if last_move and _iso(last_move) < t0:
            day, stale = (_iso(last_move) - timedelta(minutes=30)).astimezone(_LOCAL).date().isoformat(), True
            t0, t1 = _local_day_bounds(day)
        moves = _moves_between(db, t0.isoformat(), t1.isoformat()) if last_move else []
        week = _moves_between(db, (t0 - timedelta(days=7)).isoformat(), t0.isoformat()) if last_move else []
        fetch = db.execute("SELECT fetched_at, fleet, parked FROM bike_fetches ORDER BY fetched_at DESC LIMIT 1").fetchone() \
            if last_move else None
        days_seen = {_iso(r[0]).astimezone(_LOCAL).date().isoformat()
                     for r in db.execute("SELECT fetched_at FROM bike_fetches WHERE fetched_at >= ? AND fetched_at < ?",
                                         ((t0 - timedelta(days=7)).isoformat(), t0.isoformat()))} if last_move else set()

    def by_hour(rows):
        groups = Counter((m[1], m[2], m[4]) for m in rows if not m[5])
        hours, out = [0] * 24, []
        for typ, a, z, ta, tb, loop in rows:
            if loop or a not in st or z not in st or groups[(a, z, tb)] >= TRUCK_MIN:
                continue
            mid = _iso(ta) + (_iso(tb) - _iso(ta)) / 2
            hours[mid.astimezone(_LOCAL).hour] += 1
            out.append((typ, a, z))
        return hours, out, sum(1 for k, c in groups.items() if c >= TRUCK_MIN)

    hours, trips, truck_runs = by_hour(moves)
    week_hours, _w, _t = by_hour(week)
    n_days = max(1, len(days_seen))
    typical = [round(h / n_days, 1) for h in week_hours] if days_seen else None
    dep, arr = Counter(a for _t, a, _z in trips), Counter(z for _t, _a, z in trips)
    busiest = sorted(set(dep) | set(arr), key=lambda n: -(dep[n] + arr[n]))[:6]
    weather = _weather(ctx)
    wx = []
    for h in range(24):
        w = weather.get((t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H"))
        wx.append([round(w[0], 1), round(w[1]), w[2]] if w else None)
    return {
        "day": day, "stale": stale, "now": now.isoformat(),
        "trips": len(trips), "elec": sum(1 for t, _a, _z in trips if t == "ELECTRICAL"),
        "truck_runs": truck_runs, "hours": hours, "typical": typical, "typical_days": len(days_seen),
        "busiest": [[n, st[n][0], dep[n], arr[n]] for n in busiest],
        "away": ({"at": fetch[0], "fleet": fetch[1], "parked": fetch[2], "away": (fetch[1] or 0) - (fetch[2] or 0)}
                 if fetch and fetch[1] else None),
        "weather": wx, "weather_ready": any(weather),
    }


def get_triplist(q, ctx):
    """Trajets tab: the most common routes over the last 7 days and the latest trips, Métropole-wide."""
    with ctx["db"]() as db:
        names = {r[0]: _short(r[1], r[0]) for r in db.execute("SELECT number, name FROM stations")}
        try:
            last = db.execute("SELECT MAX(arrived_before) FROM bike_moves").fetchone()[0]
        except Exception:
            last = None
        if not last:
            return {"last": None, "top": [], "recent": [], "total": 0}
        lo = (_iso(last) - timedelta(days=7)).isoformat()
        rows = db.execute("""SELECT bike_number, type, from_station, to_station, left_after, arrived_before, loop
                             FROM bike_moves WHERE arrived_before > ? ORDER BY arrived_before DESC""", (lo,)).fetchall()
    groups = Counter((r[2], r[3], r[5]) for r in rows if not r[6])
    rides = [r for r in rows if not r[6] and groups[(r[2], r[3], r[5])] < TRUCK_MIN]
    pairs = Counter((r[2], r[3]) for r in rides)
    return {"last": last, "since": lo, "total": len(rides),
            "top": [[names.get(a, str(a)), names.get(z, str(z)), n] for (a, z), n in pairs.most_common(12)],
            "recent": [[b, 1 if t == "ELECTRICAL" else 0, names.get(a, str(a)), names.get(z, str(z)), ta, tb]
                       for b, t, a, z, ta, tb, _l in rides[:80]]}


# ---- following several bikes through one day
BIKES_MAX = 12


def get_bikes(q, ctx):
    """Days of several bikes at once (Lyon time): where each was parked, every trip, and the
    current state. Also the busiest bikes of that day, to pick from."""
    nums = []
    for part in ",".join(q.get("n") or []).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and not is_foreign(int(part)) and int(part) not in nums:
            nums.append(int(part))
    nums = nums[:BIKES_MAX]
    with ctx["db"]() as db:
        try:
            last_move = db.execute("SELECT MAX(arrived_before) FROM bike_moves").fetchone()[0]
        except Exception:
            last_move = None
        today = datetime.now(timezone.utc).astimezone(_LOCAL).date().isoformat()
        day = (q.get("day") or [None])[0]
        per_day = Counter(_iso(r[0]).astimezone(_LOCAL).date().isoformat()
                          for r in db.execute("SELECT fetched_at FROM bike_fetches"))
        if not day:
            full = sorted(d for d, c in per_day.items() if c >= 3)
            day = today if today in per_day or not full else full[-1]
        t0, t1 = _local_day_bounds(day)
        lo, hi = t0.isoformat(), t1.isoformat()
        st = {r[0]: r[1:] for r in db.execute("SELECT number, name, lat, lng FROM stations")}
        fetches = [r[0] for r in db.execute(
            "SELECT fetched_at FROM bike_fetches WHERE fetched_at >= ? AND fetched_at < ? ORDER BY fetched_at", (lo, hi))]
        per_day = Counter(_iso(r[0]).astimezone(_LOCAL).date().isoformat()
                          for r in db.execute("SELECT fetched_at FROM bike_fetches"))
        days = sorted(d for d, c in per_day.items() if c >= 3)       # skip a midnight tail of 1-2 fetches
        top = db.execute("""SELECT bike_number, type, COUNT(*) c FROM bike_moves
                            WHERE arrived_before > ? AND left_after < ? AND loop = 0
                            GROUP BY bike_number ORDER BY c DESC LIMIT 8""", (lo, hi)).fetchall()
        # trucks: same two stations, same window, TRUCK_MIN+ bikes
        groups = Counter((a, z, tb) for a, z, tb in db.execute(
            "SELECT from_station, to_station, arrived_before FROM bike_moves "
            "WHERE arrived_before > ? AND left_after < ? AND loop = 0", (lo, hi)))
        bikes = []
        for n in nums:
            mv = db.execute("""SELECT type, from_station, to_station, left_after, arrived_before, loop
                               FROM bike_moves WHERE bike_number=? AND arrived_before > ? AND left_after < ?
                               ORDER BY arrived_before""", (n, lo, hi)).fetchall()
            before = db.execute("""SELECT to_station, arrived_before FROM bike_moves WHERE bike_number=?
                                   AND arrived_before <= ? ORDER BY arrived_before DESC LIMIT 1""", (n, lo)).fetchone()
            state = db.execute("SELECT type, station_number, stand_number, first_seen, last_seen FROM bike_state "
                               "WHERE bike_number=?", (n,)).fetchone()
            try:
                rating = db.execute("SELECT stars, value, votes, checked, updated_at FROM bike_ratings "
                                    "WHERE bike_number=?", (n,)).fetchone()
            except Exception:
                rating = None
            total = db.execute("SELECT COUNT(*), MIN(left_after) FROM bike_moves WHERE bike_number=? AND loop=0",
                               (n,)).fetchone()
            typ = state[0] if state else (mv[0][0] if mv else None)
            trips = [{"from": a, "to": z, "t0": t0_, "t1": t1_, "loop": bool(lp),
                      "truck": bool(not lp and groups[(a, z, t1_)] >= TRUCK_MIN),
                      "km": round(_km(st[a][1:], st[z][1:]), 2) if a in st and z in st and st[a][1] and st[z][1] else None}
                     for _t, a, z, t0_, t1_, lp in mv]
            # where it started the day: the previous trip's arrival, else this day's first departure
            start = before[0] if before else (mv[0][1] if mv else None)
            if not mv and state and state[4] and state[4] >= lo and state[3] <= hi:
                start = state[1]
            bikes.append({
                "number": n, "type": typ, "trips": trips, "start": start,
                "now": {"station": state[1], "stand": state[2], "since": state[3], "seen": state[4]} if state else None,
                "rating": dict(zip(["stars", "value", "votes", "checked", "updated_at"], rating)) if rating else None,
                "all_trips": total[0], "tracked_since": total[1],
                "known": bool(state or mv or total[0]),
            })
    used = {b["start"] for b in bikes} | {t[k] for b in bikes for t in b["trips"] for k in ("from", "to")}
    if any(b["now"] for b in bikes):
        used |= {b["now"]["station"] for b in bikes if b["now"]}
    return {"day": day, "today": today, "days": days, "start": lo, "end": hi,
            "fetches": [round((_iso(f) - t0).total_seconds() / 60, 1) for f in fetches],
            "last_fetch": fetches[-1] if fetches else None,
            "stations": {n: [_short(st[n][0], n), round(st[n][1], 6), round(st[n][2], 6)]
                         for n in used if n in st and st[n][1] is not None},
            "bikes": bikes,
            "busiest": [[b, 1 if t == "ELECTRICAL" else 0, c] for b, t, c in top]}


# ---- phone page: every station's latest numbers and the trend over the last hour
def get_near(q, ctx):
    with ctx["db"]() as db:
        last = db.execute("SELECT MAX(fetched_at) FROM snapshots").fetchone()[0]
        if not last:
            return {"at": None, "stations": []}
        ago = (_iso(last) - timedelta(minutes=60)).isoformat()
        prev = db.execute("SELECT MIN(fetched_at) FROM snapshots WHERE fetched_at >= ?", (ago,)).fetchone()[0]
        rows = db.execute("""SELECT s.number, s.name, s.lat, s.lng, s.bike_stands, n.bikes, n.mechanical,
                                    n.electrical, n.docks_free, n.status
                             FROM snapshots n JOIN stations s USING(number) WHERE n.fetched_at=?""", (last,)).fetchall()
        before = dict(db.execute("SELECT number, bikes FROM snapshots WHERE fetched_at=?", (prev,)).fetchall()) if prev and prev != last else {}
    out = []
    for n, nm, la, lo, cap, b, me, el, d, status in rows:
        if la is None or not (45.55 < la < 45.95 and 4.65 < lo < 5.15) or _is_workshop(nm):
            continue
        out.append([n, _short(nm, n), round(la, 6), round(lo, 6), cap, b, me, el, d,
                    1 if status != "CLOSED" else 0, (b - before[n]) if n in before and b is not None and before[n] is not None else None])
    return {"at": last, "trend_since": prev, "fields": ["number", "name", "lat", "lng", "stands", "bikes",
            "mechanical", "electrical", "docks", "open", "trend60"], "stations": out}


# ---- backfill test: page through the Grand Lyon history, save raw pages, write nothing else ----
HIST_URL = ("https://data.grandlyon.com/fr/datapusher/ws/timeseries/"
            "jcd_jcdecaux.historiquevelov/all.json?compact=false&start={start}&maxfeatures=1000")


def post_backfill_test(body, ctx):
    """body = {"pages": 5, "start": 1, "pause": 2}. Saves each raw page to data/backfill/ and
    returns what it found: rows, date range, stations and the stop reason. Stops early at an
    empty or short page. Nothing is written to the database."""
    pages = max(1, min(int(body.get("pages", 5)), 20))
    start = max(1, int(body.get("start", 1)))
    pause = max(1.0, float(body.get("pause", 2)))
    outdir = os.path.join(ctx["data"], "backfill")
    os.makedirs(outdir, exist_ok=True)
    report, rows, stations, first, last, stop = [], 0, set(), None, None, "pages reached"
    for i in range(pages):
        pos = start + i * 1000
        try:
            page = _req(HIST_URL.format(start=pos))
        except Exception as e:
            report.append({"start": pos, "error": str(e)[:200]})
            stop = "request failed"
            break
        values = page.get("values") or []
        with open(os.path.join(outdir, f"page_{pos}.json"), "w", encoding="utf-8") as f:
            json.dump(page, f, ensure_ascii=False)
        times = sorted(v["horodate"] for v in values if v.get("horodate"))
        if times:
            first = times[0] if first is None else min(first, times[0])
            last = times[-1] if last is None else max(last, times[-1])
        stations.update(v.get("number") for v in values)
        rows += len(values)
        report.append({"start": pos, "rows": len(values), "from": times[0] if times else None,
                       "to": times[-1] if times else None})
        if len(values) < 1000:
            stop = "short or empty page (end of history)"
            break
        time.sleep(pause)
    return {"pages": report, "rows": rows, "stations": len(stations - {None}),
            "first": first, "last": last, "stop": stop, "saved_to": outdir}


# ---- history backfill: page the Grand Lyon history (1 request a second by default), keep
# ---- DAILY PER-STATION SUMMARIES only in data/history.sqlite. Raw pages are not kept.
from collections import deque as _deque

HIST_DB = "history.sqlite"
HIST_UNTIL = "2026-11-01"          # default end of the backfill (local day, inclusive)
_hist_lock = threading.Lock()
_hist_stop = threading.Event()
_hist_thread = None


def _hist_db(ctx):
    db = sqlite3.connect(os.path.join(ctx["data"], HIST_DB), timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS daily (day TEXT, station INTEGER, n INTEGER,
        dep INTEGER, arr INTEGER, sum_bikes INTEGER, sum_elec INTEGER, sum_mech INTEGER,
        empty_n INTEGER, full_n INTEGER, PRIMARY KEY (day, station)) WITHOUT ROWID""")
    db.execute("""CREATE TABLE IF NOT EXISTS weather (ts TEXT PRIMARY KEY, precip REAL,
        temp REAL, code INTEGER) WITHOUT ROWID""")
    return db


def _hist_state_path(ctx):
    return os.path.join(ctx["data"], "backfill", "state.json")


def _hist_state(ctx):
    try:
        with open(_hist_state_path(ctx), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"next_start": 1, "pages": 0, "rows": 0, "kept": 0, "dups": 0,
                "running": False, "done": False, "stopped": False, "error": None}


def _hist_save(ctx, st):
    path = _hist_state_path(ctx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(path + ".tmp", path)


def _hist_process(db, values, until):
    """Add one page to the daily per-station summaries. Returns the number of rows kept.
    Departures and arrivals are net bike drops and rises between two consecutive records of
    the same station (gaps over 6 h are ignored). They are estimates, not counted trips."""
    by = {}
    for v in values:
        try:
            ts = datetime.fromisoformat(v["horodate"])
        except Exception:
            continue
        day = ts.astimezone(_LOCAL).date().isoformat()
        if until and day > until:
            continue
        av = (v.get("total_stands") or {}).get("availabilities") or {}
        rec = (ts, day, int(av.get("bikes") or 0), int(av.get("electricalBikes") or 0),
               int(av.get("mechanicalBikes") or 0), int(av.get("stands") or 0))
        by.setdefault(v.get("number"), []).append(rec)
    agg, kept = {}, 0
    for number, recs in by.items():
        recs.sort(key=lambda r: r[0])
        prev = None
        for ts, day, b, e, m, s in recs:
            kept += 1
            a = agg.setdefault((day, number), [0] * 8)   # n, dep, arr, bikes, elec, mech, empty, full
            a[0] += 1
            a[3] += b
            a[4] += e
            a[5] += m
            a[6] += b == 0
            a[7] += s == 0
            if prev and 0 < (ts - prev[0]).total_seconds() <= 6 * 3600:
                d = b - prev[2]
                if d < 0:
                    a[1] += -d
                elif d > 0:
                    a[2] += d
            prev = (ts, day, b, e, m, s)
    for (day, number), a in agg.items():
        db.execute("""INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(day, station) DO UPDATE SET n=n+excluded.n, dep=dep+excluded.dep,
            arr=arr+excluded.arr, sum_bikes=sum_bikes+excluded.sum_bikes,
            sum_elec=sum_elec+excluded.sum_elec, sum_mech=sum_mech+excluded.sum_mech,
            empty_n=empty_n+excluded.empty_n, full_n=full_n+excluded.full_n""",
                   (day, number, *a))
    return kept


def _hist_run(ctx, until, max_pages, pause):
    db = _hist_db(ctx)
    st = _hist_state(ctx)
    st.update(running=True, error=None, done=False, stopped=False)
    _hist_save(ctx, st)
    recent = _deque(maxlen=3)          # keys of the last three pages, to catch overlapping pages
    try:
        for _ in range(max_pages):
            if _hist_stop.is_set():
                st["stopped"] = True
                break
            pos = st["next_start"]
            page = None
            for attempt in range(4):
                try:
                    page = _req(HIST_URL.format(start=pos))
                    break
                except Exception as e:
                    st["error"] = f"start {pos}: {str(e)[:160]}"
                    _hist_save(ctx, st)
                    time.sleep(30 * (attempt + 1))
            if page is None:
                break
            values = page.get("values") or []
            keys = {(v.get("horodate"), v.get("number")) for v in values}
            seen = set().union(*recent) if recent else set()
            fresh = [v for v in values if (v.get("horodate"), v.get("number")) not in seen]
            with _hist_lock:
                kept = _hist_process(db, fresh, until)
                db.commit()
            recent.append(keys)
            st.update(next_start=pos + 1000, pages=st["pages"] + 1,
                      rows=st["rows"] + len(values), kept=st["kept"] + kept,
                      dups=st.get("dups", 0) + len(values) - len(fresh), error=None)
            _hist_save(ctx, st)
            if len(values) < 1000:
                st["done"] = True
                break
            time.sleep(pause)
    finally:
        st["running"] = False
        _hist_save(ctx, st)
        db.close()


def post_backfill_start(body, ctx):
    """body = {"until": "2026-11-01", "max_pages": 200000, "pause": 1.0}. Starts the job in the
    background and resumes from data/backfill/state.json. Only one job runs at a time."""
    global _hist_thread
    if _hist_thread and _hist_thread.is_alive():
        return {"ok": False, "reason": "already running"}
    until = str(body.get("until") or HIST_UNTIL)
    max_pages = max(1, min(int(body.get("max_pages", 200000)), 200000))
    pause = max(1.0, float(body.get("pause", 1.0)))
    _hist_stop.clear()
    _hist_thread = threading.Thread(target=_hist_run, args=(ctx, until, max_pages, pause),
                                    daemon=True)
    _hist_thread.start()
    return {"ok": True, "until": until, "pause": pause}


def post_backfill_stop(body, ctx):
    _hist_stop.set()
    return {"ok": True}


def get_backfill_status(q, ctx):
    st = _hist_state(ctx)
    st["running"] = bool(_hist_thread and _hist_thread.is_alive())
    with _hist_lock:
        db = _hist_db(ctx)
        days, stations, rows = db.execute(
            "SELECT COUNT(DISTINCT day), COUNT(DISTINCT station), COUNT(*) FROM daily").fetchone()
        first, last = db.execute("SELECT MIN(day), MAX(day) FROM daily").fetchone()
        db.close()
    st.update(days=days, stations=stations, daily_rows=rows, first_day=first, last_day=last)
    try:
        st["db_mb"] = round(os.path.getsize(os.path.join(ctx["data"], HIST_DB)) / 1048576, 1)
    except OSError:
        st["db_mb"] = 0
    return st


def post_weather_archive(body, ctx):
    """One request to the Open-Meteo archive for Lyon, stored hourly in the history database."""
    start = body.get("start", "2023-03-27")
    end = body.get("end", HIST_UNTIL)
    url = ("https://archive-api.open-meteo.com/v1/archive?latitude=45.7640&longitude=4.8357"
           f"&start_date={start}&end_date={end}"
           "&hourly=temperature_2m,precipitation,weather_code&timezone=Europe%2FParis")
    j = _req(url)
    h = j.get("hourly") or {}
    times = h.get("time") or []
    db = _hist_db(ctx)
    for t, p, te, c in zip(times, h.get("precipitation") or [], h.get("temperature_2m") or [],
                           h.get("weather_code") or []):
        db.execute("INSERT OR REPLACE INTO weather VALUES (?,?,?,?)", (t, p, te, c))
    db.commit()
    db.close()
    return {"hours": len(times), "from": times[0] if times else None,
            "to": times[-1] if times else None}
