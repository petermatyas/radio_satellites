"""SatNOGS DB letöltő: műhold-katalógus és transzponderek.

Ez adja az adatbázis gerincét — melyik műhold létezik még, és milyen
frekvencián, milyen üzemmódban hallható. API kulcs nem kell, az adatok
CC BY-SA licencűek.
"""

import argparse
import re
import sys

import requests

import db

SATELLITES_URL = "https://db.satnogs.org/api/satellites/?format=json"
TRANSMITTERS_URL = "https://db.satnogs.org/api/transmitters/?format=json"
USER_AGENT = "n2yo-collector/1.0"

# Amatőr műholdas sávok (Hz). A SatNOGS "service" mezeje a rekordok több mint
# kétharmadánál "Unknown", ezért a besorolást a frekvenciára alapozzuk.
AMATEUR_BANDS = [
    (28_000_000, 29_700_000),        # 10 m
    (144_000_000, 148_000_000),      # 2 m
    (222_000_000, 225_000_000),      # 1.25 m
    (430_000_000, 440_000_000),      # 70 cm
    (1_240_000_000, 1_300_000_000),  # 23 cm
    (2_300_000_000, 2_450_000_000),  # 13 cm
    (3_400_000_000, 3_410_000_000),  # 9 cm
    (5_650_000_000, 5_925_000_000),  # 5 cm
    (10_450_000_000, 10_500_000_000),  # 3 cm
    (24_000_000_000, 24_250_000_000),  # 1.2 cm
]


def get_json(url, timeout=120):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def in_amateur_band(hz):
    return hz is not None and any(lo <= hz <= hi for lo, hi in AMATEUR_BANDS)


def is_amateur_transmitter(tx):
    """Amatőr sávban adó vagy vevő transzponder."""
    if tx.get("service") == "Amateur":
        return True
    return any(in_amateur_band(tx.get(key)) for key in
               ("downlink_low", "downlink_high", "uplink_low", "uplink_high"))


def normalize_transmitter(tx):
    return {
        "uuid": tx["uuid"],
        "norad_id": tx.get("norad_cat_id"),
        "description": tx.get("description"),
        "type": tx.get("type"),
        "status": tx.get("status"),
        "service": tx.get("service"),
        "uplink_low": tx.get("uplink_low"),
        "uplink_high": tx.get("uplink_high"),
        "downlink_low": tx.get("downlink_low"),
        "downlink_high": tx.get("downlink_high"),
        "mode": tx.get("mode"),
        "uplink_mode": tx.get("uplink_mode"),
        "baud": tx.get("baud"),
        "invert": tx.get("invert"),
        "is_amateur": is_amateur_transmitter(tx),
        "updated": tx.get("updated"),
    }


def normalize_satellite(sat, is_amateur):
    return {
        "norad_id": sat["norad_cat_id"],
        "name": sat.get("name"),
        "sat_id": sat.get("sat_id"),
        "alt_names": sat.get("names") or None,
        "status": sat.get("status"),
        "decayed": sat.get("decayed"),
        "launched": sat.get("launched"),
        "operator": sat.get("operator"),
        "countries": sat.get("countries"),
        "website": sat.get("website") or None,
        "is_amateur": is_amateur,
        "updated": sat.get("updated"),
    }


def name_tokens(sat):
    """A műhold nevének és alternatív neveinek normalizált szavai."""
    raw = f"{sat.get('name') or ''} {sat.get('names') or ''}"
    return {re.sub(r"[^A-Z0-9]", "", tok.upper())
            for tok in re.split(r"[\s;,()\[\]]+", raw)}


def collect(conn, extra_names=None, verbose=True):
    """Letöltés és mentés; visszatér a beszúrt/frissített darabszámokkal.

    extra_names: normalizált nevek, amiket akkor is amatőrnek veszünk, ha
    nincs aktív amatőr sávú transzponderük — ezt az AMSAT katalógusa adja,
    ami önmagában bizonyíték arra, hogy amatőr műholdról van szó.
    """
    extra_names = extra_names or set()
    sats = get_json(SATELLITES_URL)
    txs = get_json(TRANSMITTERS_URL)

    transmitters = [normalize_transmitter(t) for t in txs]
    amateur_norads = {
        t["norad_id"] for t in transmitters
        if t["is_amateur"] and t["status"] == "active" and t["norad_id"]
    }

    # A már követett műholdakat (amikhez van átvonulásunk) akkor is
    # kiegészítjük SatNOGS-adatokkal, ha nem amatőr — pl. NOAA 19.
    tracked = {r["norad_id"] for r in
               conn.execute("SELECT DISTINCT norad_id FROM passes")}

    wanted = []
    for s in sats:
        norad = s.get("norad_cat_id")
        if not norad or s.get("decayed"):
            continue
        amateur = norad in amateur_norads or bool(name_tokens(s) & extra_names)
        if amateur or norad in tracked:
            wanted.append(normalize_satellite(s, amateur))

    keep = {s["norad_id"] for s in wanted}
    transmitters = [t for t in transmitters if t["norad_id"] in keep]

    sat_new, sat_upd = db.save_satellite_details(conn, wanted)
    tx_new, tx_upd = db.save_transmitters(conn, transmitters)

    if verbose:
        print(f"SatNOGS: {len(sats)} műhold, {len(txs)} transzponder a forrásban")
        print(f"  műhold:       {sat_new} új, {sat_upd} frissítve "
              f"({len(amateur_norads)} amatőr)")
        print(f"  transzponder: {tx_new} új, {tx_upd} frissítve")
    return {"satellites": len(wanted), "transmitters": len(transmitters)}


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
