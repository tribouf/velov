#!/usr/bin/env python3
"""
One-time snapshot of Vélo'v bike numbers.

EXPERIMENTAL / UNOFFICIAL: uses the private JCDecaux Cyclocity API that the
velov.grandlyon.com website uses (public website key, no account), as
documented by the community project yannlombard/cyclocity-api-doc (CC BY 4.0).
Not supported by JCDecaux or the Métropole; may stop working at any time.
Keep it personal and low-volume.

One run = 2 requests (a token, then the whole fleet in one call).
- bikes_<timestamp>.csv : bikes parked at Villeurbanne stations
- velov.sqlite, table bike_snapshots : every bike parked anywhere in Lyon
  (compact), so trips into and out of Villeurbanne can be traced later.
Standard library only.
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

API = "https://api.cyclocity.fr"
WEB_KEY = {"code": "vls.web.lyon:PRD",
           "key": "c3d9f5c22a9157a7cc7fe0e38269573bdd2f13ec48f867360ecdcbd35b196f87"}
UA = "velov-personal-research/1.0"
DB = "velov.sqlite"
FOREIGN_RANGE = (99000, 99999)   # bikes in the feed that are not Vélo'v (hidden everywhere)
OFF_STATION = 101010  # Cyclocity's "not at a station" placeholder
MAX_GAP_MIN = 90      # longer gap than the slowest night pace (60 min) = pause, not trips


def request(url, data=None, headers=None):
    h = {"User-Agent": UA, **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def villeurbanne_stations(db):
    try:
        nums = {r[0] for r in db.execute(
            "SELECT number FROM stations WHERE commune = 'Villeurbanne'")}
        names = dict(db.execute("SELECT number, name FROM stations"))
        return nums, names
    except sqlite3.Error:
        return set(), {}


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS bike_snapshots (
        fetched_at TEXT, bike_number INTEGER, type TEXT, status TEXT,
        station_number INTEGER, stand_number INTEGER, battery_mv INTEGER,
        PRIMARY KEY (fetched_at, bike_number))""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(bike_snapshots)")}
    for col, typ in (("reserved", "INTEGER"), ("last_trip_utc", "TEXT")):
        if col not in cols:
            db.execute(f"ALTER TABLE bike_snapshots ADD COLUMN {col} {typ}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_bike ON bike_snapshots(bike_number, fetched_at)")
    # compact trip tracking for the whole Métropole: last known position + moves
    db.executescript("""
    CREATE TABLE IF NOT EXISTS bike_fetches (fetched_at TEXT PRIMARY KEY, fleet INTEGER, parked INTEGER, moves INTEGER);
    CREATE TABLE IF NOT EXISTS bike_state (
        bike_number INTEGER PRIMARY KEY, type TEXT, station_number INTEGER, stand_number INTEGER,
        first_seen TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS bike_moves (
        bike_number INTEGER, type TEXT,
        from_station INTEGER, from_stand INTEGER, left_after TEXT,
        to_station INTEGER, to_stand INTEGER, arrived_before TEXT,
        loop INTEGER DEFAULT 0,
        PRIMARY KEY (bike_number, arrived_before));
    CREATE INDEX IF NOT EXISTS idx_moves_time ON bike_moves(arrived_before);
    """)


def stars_of(rating, checked=False):
    """The official app shows 1-3 stars. Votes are worth 100 (recommended), 60 or 30
    (not recommended, by defect), and `value` is their average since the last
    workshop check; 100/60/30 read as 3/2/1 stars. None = no vote, 0 = "Révisé"."""
    r = rating or {}
    v, n = r.get("value"), r.get("count") or 0
    if not n or v is None:
        return 0 if checked else None
    return 3 if v >= 80 else 2 if v >= 45 else 1


def record_ratings(db, parked, now_iso):
    """Latest rating of every parked bike (one row per bike, overwritten each fetch)."""
    db.execute("""CREATE TABLE IF NOT EXISTS bike_ratings (
        bike_number INTEGER PRIMARY KEY, type TEXT, stars INTEGER, value REAL, votes INTEGER,
        checked INTEGER, last_rating TEXT, station_number INTEGER, updated_at TEXT)""")
    db.executemany("INSERT OR REPLACE INTO bike_ratings VALUES (?,?,?,?,?,?,?,?,?)",
                   [(b.get("number"), b.get("type"), stars_of(b.get("rating"), b.get("checked")),
                     (b.get("rating") or {}).get("value"), (b.get("rating") or {}).get("count") or 0,
                     1 if b.get("checked") else 0, (b.get("rating") or {}).get("lastRatingDateTime"),
                     b.get("stationNumber"), now_iso) for b in parked])


def record_moves(db, parked, now_iso):
    """Compare with each bike's last known station; store only the changes."""
    prev_fetch = db.execute("SELECT MAX(fetched_at) FROM bike_fetches").fetchone()[0]
    state = {r[0]: r[1:] for r in db.execute(
        "SELECT bike_number, station_number, stand_number, first_seen, last_seen FROM bike_state")}
    moves, upd, new = [], [], []
    # after a pause (PC asleep, app stopped) positions are too old to pair: just re-baseline
    gap = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(prev_fetch)).total_seconds() / 60 \
        if prev_fetch else None
    if gap is not None and gap > MAX_GAP_MIN:
        db.execute("DELETE FROM bike_state")
        db.executemany("INSERT INTO bike_state VALUES (?,?,?,?,?,?)",
                       [(b.get("number"), b.get("type"), b.get("stationNumber"), b.get("standNumber"),
                         now_iso, now_iso) for b in parked])
        print(f"  last fetch was {gap / 60:.1f} h ago: positions reset, no moves recorded this time")
        return []
    for b in parked:
        n, st, sd, typ = b.get("number"), b.get("stationNumber"), b.get("standNumber"), b.get("type")
        p = state.get(n)
        if p is None:
            new.append((n, typ, st, sd, now_iso, now_iso))
            continue
        p_st, p_sd, p_first, p_last = p
        moved = p_st != st
        # back at the same station after being absent from at least one fetch = round trip
        loop = (not moved) and prev_fetch is not None and p_last < prev_fetch
        if moved or loop:
            moves.append((n, typ, p_st, p_sd, p_last, st, sd, now_iso, 1 if loop else 0))
            upd.append((typ, st, sd, now_iso, now_iso, n))
        else:
            upd.append((typ, st, sd, p_first, now_iso, n))
    db.executemany("INSERT OR IGNORE INTO bike_moves VALUES (?,?,?,?,?,?,?,?,?)", moves)
    db.executemany("UPDATE bike_state SET type=?, station_number=?, stand_number=?, first_seen=?, last_seen=? "
                   "WHERE bike_number=?", upd)
    db.executemany("INSERT OR REPLACE INTO bike_state VALUES (?,?,?,?,?,?)", new)
    return moves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="scheduled run: no CSV file")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    print("Getting a client token...", flush=True)
    token = request(f"{API}/auth/environments/PRD/client_tokens", WEB_KEY)["accessToken"]

    print("Fetching the bike list (one request, about 1-2 MB)...", flush=True)
    bikes = request(f"{API}/contracts/lyon/bikes",
                    headers={"Authorization": f"Taknv1 {token}",
                             "Accept": "application/vnd.bikes.v4+json"})
    foreign = sum(1 for b in bikes if FOREIGN_RANGE[0] <= (b.get("number") or 0) <= FOREIGN_RANGE[1])
    bikes = [b for b in bikes if not FOREIGN_RANGE[0] <= (b.get("number") or 0) <= FOREIGN_RANGE[1]]
    print(f"  {len(bikes)} bikes in the whole Lyon fleet ({foreign} non-Vélo'v 99xxx bikes ignored)", flush=True)

    with sqlite3.connect(DB) as db:
        ensure_table(db)
        vil, names = villeurbanne_stations(db)

        def at_station(b):
            n = b.get("stationNumber")
            return n is not None and n != OFF_STATION

        def in_vil(b):
            n = b.get("stationNumber")
            return n in vil if vil else (n is not None and 10000 <= n < 11000)

        # remove anything recorded earlier for non-Vélo'v bikes
        for table in ("bike_state", "bike_ratings", "bike_moves", "bike_snapshots"):
            try:
                db.execute(f"DELETE FROM {table} WHERE bike_number BETWEEN ? AND ?", FOREIGN_RANGE)
            except sqlite3.Error:
                pass
        parked = [b for b in bikes if at_station(b)]
        moves = record_moves(db, parked, now.isoformat())
        record_ratings(db, parked, now.isoformat())
        db.execute("INSERT OR REPLACE INTO bike_fetches VALUES (?,?,?,?)",
                   (now.isoformat(), len(bikes), len(parked), len(moves)))
        vil_rows = [b for b in parked if in_vil(b)]
        db.executemany(
            "INSERT OR IGNORE INTO bike_snapshots (fetched_at, bike_number, type, status,"
            " station_number, stand_number, battery_mv, reserved, last_trip_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(now.isoformat(), b.get("number"), b.get("type"), b.get("status"),
              b.get("stationNumber"), b.get("standNumber"), b.get("bikeBatteryMv"),
              1 if b.get("isReserved") else 0, b.get("lastTripDateTime"))
             for b in vil_rows])

    rows = sorted((b for b in parked if in_vil(b)),
                  key=lambda b: (b.get("stationNumber") or 0, b.get("standNumber") or 0))

    csv_path = None if args.auto else f"bikes_{now.strftime('%Y-%m-%d_%H%M')}.csv"
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["fetched_at", "bike_number", "type", "status", "station_number",
                        "station_name", "stand_number", "reserved", "last_trip_utc",
                        "battery_mv", "stars", "revised", "rating", "rating_count"])
            for b in rows:
                r = b.get("rating") or {}
                w.writerow([now.isoformat(), b.get("number"), b.get("type"), b.get("status"),
                            b.get("stationNumber"), names.get(b.get("stationNumber"), ""),
                            b.get("standNumber"), "yes" if b.get("isReserved") else "no",
                            b.get("lastTripDateTime"), b.get("bikeBatteryMv"),
                            stars_of(r, b.get("checked")), "yes" if b.get("checked") else "no",
                            r.get("value"), r.get("count")])

    elec = sum(1 for b in rows if b.get("type") == "ELECTRICAL")
    reserved = [b for b in rows if b.get("isReserved")]
    print(f"\n{len(rows)} bikes parked at {len({b.get('stationNumber') for b in rows})} "
          f"Villeurbanne stations ({elec} electric, {len(rows) - elec} mechanical)")
    print(f"{len(reserved)} of them are booked right now (15-minute reservation)")
    for b in reserved[:10]:
        print(f"  booked: bike {b.get('number')} at "
              f"{names.get(b.get('stationNumber'), b.get('stationNumber'))}")
    print(f"{len(parked)} bikes parked across the Métropole, {len(moves)} moves since the last fetch")
    if csv_path:
        print(f"Saved {csv_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFailed: {e!r}")
        print("The unofficial API may have changed or refused the request.")
        sys.exit(1)
