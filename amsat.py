"""AMSAT Live OSCAR Satellite Status letöltő.

Ez a "mi hallható éppen" forrás: amatőrök jelentik be világszerte, hogy egy
műholdat hallottak-e. Nem menetrend, hanem élő állapot — a "Crew Active"
státusz például azt jelenti, hogy az ISS-en hangforgalom zajlik.

A katalógus bejegyzései "ISS_[FM]" alakúak, NORAD ID nélkül; a hozzárendelést
a SatNOGS-ból származó nevek alapján végezzük, ezért ezt a scriptet a
satnogs.py után érdemes futtatni.
"""

import argparse
import re
import sys

import requests

import db

BASE_URL = "https://www.amsat.org/status/api/v1"
USER_AGENT = "n2yo-collector/1.0"

DEFAULT_HOURS = 72
MAX_LIMIT = 500

# Az AMSAT-nevekben az aktivitás szögletes zárójelben van: "ISS_[FM]".
NAME_RE = re.compile(r"^(?P<base>.+?)_\[(?P<activity>.+)\]$")


def get_json(path, params=None, timeout=60):
    resp = requests.get(f"{BASE_URL}/{path}", params=params,
                        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("data", [])


def split_name(amsat_name):
    """"ISS_[FM]" -> ("ISS", "FM")"""
    m = NAME_RE.match(amsat_name)
    if not m:
        return amsat_name, None
    return m.group("base"), m.group("activity")


def norm_key(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def resolve_norad(base_name, index):
    """AMSAT alapnév -> NORAD ID a SatNOGS nevek alapján."""
    return index.get(norm_key(base_name))


def catalog_names():
    """A katalógusban szereplő műholdnevek normalizálva.

    A satnogs.py ezt használja: amit az AMSAT státuszoldala listáz, az
    amatőr műhold akkor is, ha a SatNOGS-ban nincs aktív amatőr sávú
    transzpondere.
    """
    return {norm_key(split_name(item["name"])[0])
            for item in get_json("catalog.php")}


def collect(conn, hours=DEFAULT_HOURS, verbose=True):
    index = db.name_index(conn)

    catalog = get_json("catalog.php", {"include_stats": "true"})
    entries, unresolved = [], []
    for item in catalog:
        base, activity = split_name(item["name"])
        norad = resolve_norad(base, index)
        if norad is None:
            unresolved.append(item["name"])
        entries.append({
            "amsat_name": item["name"],
            "display_name": item.get("display_name"),
            "activity": activity,
            "norad_id": norad,
            "website": item.get("website"),
            "report_count": item.get("report_count"),
            "latest_report": item.get("latest_reported_time"),
        })

    cat_new, cat_upd = db.save_amsat_catalog(conn, entries)

    # Csak azokra kérünk jelentést, ahol az elmúlt időszakban volt is forgalom
    # — így nem terheljük az API-t 85 üres lekérdezéssel.
    summary = get_json("summary.php", {"hours": hours})
    known = {e["amsat_name"] for e in entries}
    summary_names = {s["name"] for s in summary}
    # A summary régi elnevezéseket is visszaad ("IO-117", "QO-100_NB"), amiket
    # a reports.php már nem ismer — ezekre 404-et kapnánk.
    active_names = sorted(summary_names & known)
    stale_names = sorted(summary_names - known)

    reports = []
    for name in active_names:
        for r in get_json("reports.php", {"name": name, "hours": hours,
                                          "limit": MAX_LIMIT}):
            reports.append({
                "id": r["id"],
                "amsat_name": r["name"],
                "reported_time": r["reported_time"],
                "callsign": r.get("callsign"),
                "report": r.get("report"),
                "grid_square": r.get("grid_square"),
            })

    rep_new, rep_dup = db.save_amsat_reports(conn, reports)

    if verbose:
        print(f"AMSAT: {len(catalog)} katalógus-bejegyzés, "
              f"{len(active_names)} aktív az elmúlt {hours} órában")
        print(f"  katalógus: {cat_new} új, {cat_upd} frissítve")
        print(f"  jelentés:  {rep_new} új, {rep_dup} már megvolt")
        if stale_names:
            print(f"  a summary elavult neveit kihagytuk: {', '.join(stale_names)}")
        if unresolved:
            print(f"  NORAD ID nélkül maradt ({len(unresolved)}): "
                  f"{', '.join(unresolved)}")
    return {"catalog": len(entries), "reports": len(reports),
            "unresolved": unresolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help=f"hány órára visszamenőleg (max 720, "
                             f"alapértelmezett: {DEFAULT_HOURS})")
    args = parser.parse_args()

    conn = db.connect(args.db)
    try:
        collect(conn, hours=min(args.hours, 720))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
