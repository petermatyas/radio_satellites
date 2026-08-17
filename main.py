"""N2YO letöltő script: radio passes lekérése és mentése SQLite-ba."""

import argparse
import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

import db

load_dotenv()

SECONDS_PER_DAY = 86400

# A megfigyelő pozíciója és a lekérdezés hossza az adatbázisban van (a webes
# Beállítások lapról szerkeszthető); ezek csak a végső tartalékértékek.
LAT = db.DEFAULT_SETTINGS["lat"]
LON = db.DEFAULT_SETTINGS["lon"]
ALT = db.DEFAULT_SETTINGS["alt"]
DAYS = db.DEFAULT_SETTINGS["days"]
MIN_ELEVATION_DEGREE = db.DEFAULT_SETTINGS["min_elevation"]

#NORAD_ID = 25544
NORAD_ID = 67279 # GALAPAGOS-UTE

BASE_URL = "https://api.n2yo.com/rest/v1/satellite/radiopasses"


def fetch_passes(norad_id, lat, lon, alt, days, min_elevation, api_key):
    url = f"{BASE_URL}/{norad_id}/{lat}/{lon}/{alt}/{days}/{min_elevation}"
    resp = requests.get(url, params={"apiKey": api_key}, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"N2YO hiba: {data['error']}")
    return data


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    # A None alapértelmezés azt jelzi, hogy a kapcsoló nem szerepelt, ilyenkor
    # az adatbázisban tárolt beállítás érvényes.
    parser.add_argument("norad_id", nargs="?", type=int,
                        help="NORAD ID (alapértelmezésben a követett műholdak)")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--alt", type=float)
    parser.add_argument("-d", "--days", type=int,
                        help="hány napra előre töltsön le")
    parser.add_argument("--min-elevation", type=int)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH,
                        help="SQLite fájl útvonala")
    parser.add_argument("-f", "--force", action="store_true",
                        help="akkor is töltsön le, ha már megvan az adat")
    return parser.parse_args()


def settings_of(conn, args=None):
    """A mentett beállítások, a parancssori kapcsolókkal felülírva."""
    values = db.get_settings(conn)
    for key in values:
        given = getattr(args, key, None) if args else None
        if given is not None:
            values[key] = given
    return values


def collect(conn, norad_id, observer=None, days=None,
            min_elevation=None, force=False, verbose=True):
    """Egy műhold átvonulásainak letöltése és mentése.

    Ha az adatok a kért napig már megvannak, nem hálózik. Visszatér a
    letöltött átvonulásokkal (üres lista, ha a gyorsítótár elég volt).
    """
    saved = db.get_settings(conn)
    observer = observer or (saved["lat"], saved["lon"], saved["alt"])
    days = saved["days"] if days is None else days
    min_elevation = (saved["min_elevation"] if min_elevation is None
                     else min_elevation)
    needed_until = int(time.time()) + days * SECONDS_PER_DAY

    covered = db.coverage_until(conn, norad_id, observer, min_elevation)
    # Napra kerekítve hasonlítunk: két azonos napra szóló kérés között
    # eltelt pár másodperc miatt ne induljon újra a letöltés.
    if day_of(covered) >= day_of(needed_until) and not force:
        if verbose:
            print(f"NORAD {norad_id}: az adatok már megvannak "
                  f"{datetime.fromtimestamp(covered):%Y-%m-%d %H:%M}-ig, "
                  f"nincs letöltés (--force felülírja)")
        return []

    api_key = os.getenv("N2YO_API_KEY")
    if not api_key:
        raise RuntimeError("Hiányzik az N2YO_API_KEY a .env fájlból")

    data = fetch_passes(norad_id, *observer, days, min_elevation, api_key)
    info = data.get("info", {})
    passes = data.get("passes") or []
    sat_name = info.get("satname", "?")

    db.save_satellite(conn, norad_id, sat_name)
    inserted, skipped = db.save_passes(conn, norad_id, observer, passes)
    db.record_fetch(conn, norad_id, observer, min_elevation, needed_until)

    if verbose:
        print(f"{sat_name} (NORAD {norad_id}) - {days} nap, "
              f"{len(passes)} átvonulás letöltve")
        print(f"Mentve: {inserted} új, {skipped} már szerepelt az adatbázisban")
    return passes


def collect_tracked(conn, days=None, min_elevation=None,
                    force=False, verbose=True):
    """Frissítés a Beállításokban megadott műholdakra és pozícióra.

    A katalógus szerint nem aktív műholdakat (visszatért, dead, még nem
    indult) kihagyjuk: érdemi átvonulás úgysem jönne rájuk, az N2YO napi
    kvótáját viszont fogyasztanák. A listán maradnak, csak nem töltünk hozzá.
    """
    saved = db.get_settings(conn)
    observer = (saved["lat"], saved["lon"], saved["alt"])
    tracked = db.get_tracked_status(conn)
    active = [sat["norad_id"] for sat in tracked if not sat["reason"]] or (
        [NORAD_ID] if not tracked else [])
    skipped = [sat for sat in tracked if sat["reason"]]

    if verbose:
        for sat in skipped:
            print(f"NORAD {sat['norad_id']}: nem aktív, kihagyva "
                  f"— {sat['reason']}")

    total = 0
    for norad_id in active:
        total += len(collect(conn, norad_id, observer, days,
                             min_elevation, force, verbose))
    if verbose:
        print(f"N2YO: {len(active)} műhold, {total} átvonulás letöltve "
              f"({observer[0]}, {observer[1]})"
              + (f"; {len(skipped)} nem aktív kihagyva" if skipped else ""))
    return {"satellites": len(active), "passes": total,
            "skipped": len(skipped)}


def main():
    args = parse_args()

    conn = db.connect(args.db)
    try:
        values = settings_of(conn, args)
        observer = (values["lat"], values["lon"], values["alt"])

        if args.norad_id is None:
            collect_tracked(conn, values["days"], values["min_elevation"],
                            args.force)
            return

        passes = collect(conn, args.norad_id, observer, values["days"],
                         values["min_elevation"], args.force)
        if passes:
            print_passes(passes)
        else:
            print_passes(row_to_pass(r)
                         for r in db.get_passes(conn, args.norad_id))
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    finally:
        conn.close()


def day_of(ts):
    """Unix time -> az adott (helyi idő szerinti) nap dátuma."""
    return datetime.fromtimestamp(ts).date()


def row_to_pass(row):
    """Adatbázis sor -> az API válaszával azonos alakú dict."""
    return {
        "startUTC": row["start_utc"],
        "endUTC": row["end_utc"],
        "maxEl": row["max_el"],
        "startAzCompass": row["start_az_compass"],
        "endAzCompass": row["end_az_compass"],
    }


def print_passes(passes):
    for p in passes:
        start = datetime.fromtimestamp(p["startUTC"])
        end = datetime.fromtimestamp(p["endUTC"])
        print(f"  {start:%Y-%m-%d %H:%M:%S} -> {end:%H:%M:%S} | "
              f"max elev: {p['maxEl']}° | "
              f"{p['startAzCompass']} -> {p['endAzCompass']}")


if __name__ == "__main__":
    main()
