"""Pályaelem (TLE) letöltő a Celestrak GP katalógusból.

Ebből számoljuk ki, hogy a műhold éppen a Föld melyik pontja felett jár, és
merre látszik a megfigyelő égboltján. Csak a követett műholdakra van szükség,
de egy csoportos lekérés olcsóbb a Celestraknak, mint műholdanként egy: ezért
a 16 kB-os "amateur" katalógust töltjük le, és csak az abból hiányzó
műholdakat (időjárási, kutató) kérjük külön.

A Celestrak ugyanazt az állományt két óránál sűrűbben kérve 403-mal tiltja
ki a klienst, ezért a MIN_REFRESH_HOURS-nál frissebb elemhalmazt nem töltjük
le újra. A TLE néhány nap alatt amúgy is csak lassan avul.
"""

import argparse
import sys

import requests

import db

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"
USER_AGENT = "n2yo-collector/1.0"

# Ennél régebbi epochájú elemhalmazt már nem tekintünk használhatónak.
MAX_AGE_DAYS = 14

# Ennél frissebb elemhalmazt nem töltünk le újra (lásd a modul leírását).
MIN_REFRESH_HOURS = 6

DEFAULT_GROUP = "amateur"


def fetch_group(group=DEFAULT_GROUP, timeout=180):
    """A csoport nyers TLE szövege (három soros formátum)."""
    resp = requests.get(GP_URL, params={"GROUP": group, "FORMAT": "tle"},
                        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_one(norad_id, timeout=60):
    """Egyetlen műhold elemhalmaza, ha a csoportban nem szerepelt."""
    resp = requests.get(GP_URL, params={"CATNR": norad_id, "FORMAT": "tle"},
                        headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    # A Celestrak ismeretlen katalógusszámra 200-at ad "No GP data found"
    # szöveggel, nem hibakódot — a parse úgyis üresen tér vissza rá.
    return resp.text


def parse(text, source_url=None):
    """Három soros TLE szöveg -> bejegyzések NORAD ID szerint.

    A katalógusszám az első adatsor 3-7. karakterén van; a névsor a
    csoportos letöltésben szerepel, az egyedinél olykor hiányzik.
    """
    entries = {}
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        name = None
        if not lines[i].startswith("1 "):
            name = lines[i].strip()
            i += 1
        if i + 1 >= len(lines):
            break
        line1, line2 = lines[i], lines[i + 1]
        i += 2
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            continue
        try:
            norad = int(line1[2:7])
        except ValueError:
            continue
        entries[norad] = {
            "norad_id": norad,
            "name": name,
            "line1": line1,
            "line2": line2,
            "epoch": line1[18:32].strip(),
            "source_url": source_url or f"{GP_URL}?CATNR={norad}&FORMAT=tle",
        }
    return entries


def collect(conn, norad_ids=None, force=False, verbose=True):
    """A követett műholdak pályaelemeinek letöltése és mentése."""
    wanted = list(norad_ids) if norad_ids is not None else [
        norad for norad, _ in db.get_tracked(conn)]
    if not wanted:
        if verbose:
            print("TLE: nincs követett műhold")
        return {"satellites": 0, "fresh": 0, "missing": []}

    have = db.get_tle_map(conn, wanted)
    ages = {} if force else db.tle_ages(conn, wanted)
    fresh = {n for n, hours in ages.items() if hours < MIN_REFRESH_HOURS}
    todo = [n for n in wanted if n not in fresh]
    if not todo:
        if verbose:
            print(f"TLE: mind a {len(wanted)} elemhalmaz {MIN_REFRESH_HOURS} "
                  f"óránál frissebb, nincs letöltés (--force felülírja)")
        return {"satellites": 0, "fresh": len(fresh), "missing": []}

    group_url = f"{GP_URL}?GROUP={DEFAULT_GROUP}&FORMAT=tle"
    try:
        catalog = parse(fetch_group(), source_url=group_url)
    except requests.RequestException as exc:
        # A csoportos letöltés kimaradhat (403 a túl sűrű kérés miatt); a
        # műholdankénti lekérés ilyenkor is működik.
        if verbose:
            print(f"figyelem: a Celestrak csoportos lista nem elérhető ({exc})")
        catalog = {}

    entries, missing = [], []
    for norad in todo:
        entry = catalog.get(norad)
        if entry is None:
            # Nem minden követett műhold amatőr (NOAA, METEOR, SARAL) —
            # ezeket egyesével kérjük le.
            try:
                entry = parse(fetch_one(norad)).get(norad)
            except requests.RequestException:
                entry = None
        if entry is None:
            missing.append(norad)
        else:
            entries.append(entry)

    new, updated = db.save_tle(conn, entries)
    if verbose:
        print(f"Celestrak: {len(entries)} pályaelem "
              f"({new} új, {updated} frissítve)"
              + (f", {len(fresh)} már friss volt" if fresh else ""))
        if missing:
            kept = [n for n in missing if n in have]
            print(f"  nincs friss pályaelem ({len(missing)}): "
                  f"{', '.join(str(n) for n in missing)}"
                  + (f" — {len(kept)} esetben a korábbi marad" if kept else ""))
    return {"satellites": len(entries), "fresh": len(fresh),
            "missing": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("norad_id", nargs="*", type=int,
                        help="NORAD ID-k (alapértelmezésben a követettek)")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("-f", "--force", action="store_true",
                        help="akkor is töltsön le, ha a tárolt TLE friss")
    args = parser.parse_args()

    conn = db.connect(args.db)
    try:
        collect(conn, args.norad_id or None, force=args.force)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
