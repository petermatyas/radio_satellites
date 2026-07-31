"""hams.at letöltő: bejelentett rover-aktivációk.

Ez az egyetlen forrás, ami előre, konkrét időponttal mondja meg, hogy ki
lesz aktív melyik műholdon — a transzponderes műholdaknál ugyanis nincs
menetrend, ott az "esemény" az, ha valaki bejelentkezik.

Az /api/alerts/upcoming végpont API kulcs nélkül is elérhető.
"""

import argparse
import sys
from datetime import datetime, timezone

import requests

import db

URL = "https://hams.at/api/alerts/upcoming"
USER_AGENT = "n2yo-collector/1.0"


def parse_ts(value):
    if not value:
        return None
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


def fetch_alerts(url=URL, timeout=30):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/json"},
                        timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("data", [])


def normalize(alert):
    sat = alert.get("satellite") or {}
    return {
        "id": alert["id"],
        "norad_id": sat.get("number"),
        "sat_name": sat.get("name"),
        "callsign": alert.get("callsign"),
        "mode": alert.get("mode"),
        "mhz": alert.get("mhz"),
        "mhz_direction": alert.get("mhz_direction"),
        "grids": ", ".join(alert.get("grids") or []) or None,
        "comment": (alert.get("comment") or "").strip() or None,
        "url": alert.get("url"),
        "max_elevation": alert.get("max_elevation"),
        "start_utc": parse_ts(alert.get("aos_at")),
        "end_utc": parse_ts(alert.get("los_at")),
    }


def collect(conn, verbose=True):
    alerts = [normalize(a) for a in fetch_alerts()]
    new, updated = db.save_activations(conn, alerts)

    if verbose:
        print(f"hams.at: {len(alerts)} bejelentett aktiváció "
              f"({new} új, {updated} frissítve)")
        for a in alerts[:10]:
            when = (datetime.fromtimestamp(a["start_utc"]).strftime("%m-%d %H:%M")
                    if a["start_utc"] else "?")
            print(f"  {when}  {a['sat_name']:<12} {a['callsign']:<8} "
                  f"{a['mode'] or '?':<6} {a['mhz'] or '?'} MHz  {a['grids'] or ''}")
    return {"activations": len(alerts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = db.connect(args.db)
    try:
        collect(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
