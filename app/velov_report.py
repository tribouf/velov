#!/usr/bin/env python3
"""
Quick report on collected Vélo'v snapshots.

Estimates departures/arrivals per station from 5-minute count changes.
Changes of 8+ bikes in one step are flagged as likely truck rebalancing
and excluded. Counts are lower bounds: a rental and a return in the same
5 minutes cancel out.

Usage: python3 velov_report.py [--db velov.sqlite]
"""
import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
REBALANCE_THRESHOLD = 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="velov.sqlite")
    args = ap.parse_args()
    db = sqlite3.connect(args.db)

    names = dict(db.execute("SELECT number, name FROM stations"))
    rows = db.execute(
        "SELECT number, fetched_at, bikes, status FROM snapshots "
        "WHERE bikes IS NOT NULL ORDER BY number, fetched_at"
    ).fetchall()
    if not rows:
        print("No data yet.")
        return

    first, last = rows[0][1], max(r[1] for r in rows)
    n_snap = db.execute("SELECT COUNT(DISTINCT fetched_at) FROM snapshots").fetchone()[0]
    print(f"Data: {n_snap} snapshots, {len(names)} stations, {first} → {last}\n")

    dep = defaultdict(int)
    arr = defaultdict(int)
    by_hour = defaultdict(lambda: [0, 0])
    rebal = defaultdict(int)
    empty = defaultdict(int)
    obs = defaultdict(int)

    prev = {}
    for number, ts, bikes, status in rows:
        obs[number] += 1
        if bikes == 0:
            empty[number] += 1
        if number in prev:
            delta = bikes - prev[number]
            if abs(delta) >= REBALANCE_THRESHOLD:
                rebal[number] += 1
            elif delta:
                hour = datetime.fromisoformat(ts).astimezone(PARIS).hour
                if delta < 0:
                    dep[number] += -delta
                    by_hour[hour][0] += -delta
                else:
                    arr[number] += delta
                    by_hour[hour][1] += delta
        prev[number] = bikes

    def label(n):
        return f"{names.get(n, n)}"[:38]

    print("Busiest stations (est. departures + arrivals):")
    for n in sorted(names, key=lambda n: -(dep[n] + arr[n]))[:10]:
        print(f"  {label(n):38}  out {dep[n]:4}  in {arr[n]:4}")

    print("\nMost often empty (% of snapshots with 0 bikes):")
    for n in sorted(names, key=lambda n: -empty[n] / max(obs[n], 1))[:5]:
        print(f"  {label(n):38}  {100 * empty[n] / max(obs[n], 1):5.1f}%")

    print("\nActivity by hour (Paris time):  out / in")
    for h in sorted(by_hour):
        o, i = by_hour[h]
        print(f"  {h:02d}h  {o:4} / {i:4}  " + "#" * min(60, (o + i) // 2))

    if rebal:
        print(f"\nLikely truck rebalancing events: {sum(rebal.values())} "
              f"(excluded from counts)")


if __name__ == "__main__":
    main()
