"""Minden forrás begyűjtése egy futtatással.

A sorrend számít: a SatNOGS katalógus adja a NORAD ID-ket, amikhez az AMSAT
státuszokat és a hams.at aktivációkat kötjük. Egy forrás hibája nem állítja
meg a többit — a hálózati végpontok egymástól függetlenül eshetnek ki.
"""

import argparse
import sys
import traceback

import amsat
import amsat_freq
import db
import hamsat
import satnogs
import sstv
import tle


def run(name, fn):
    print(f"--- {name} ---")
    try:
        fn()
        return True
    except Exception as exc:  # a többi forrás ettől még begyűjthető
        print(f"HIBA ({name}): {exc.__class__.__name__}: {exc}",
              file=sys.stderr)
        if "--traceback" in sys.argv:
            traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--hours", type=int, default=amsat.DEFAULT_HOURS,
                        help="AMSAT jelentések időablaka órában")
    parser.add_argument("--skip", nargs="*", default=[],
                        choices=["satnogs", "amsat", "amsatfreq", "hamsat", "sstv", "tle"],
                        help="kihagyandó források")
    parser.add_argument("--traceback", action="store_true")
    args = parser.parse_args()

    conn = db.connect(args.db)
    ok = 0
    total = 0
    try:
        if "satnogs" not in args.skip:
            total += 1
            # Az AMSAT katalógusa is bizonyíték az amatőr jellegre, ezért a
            # neveit átadjuk a SatNOGS-importnak.
            hints = set()
            try:
                hints = amsat.catalog_names()
            except Exception as exc:
                print(f"figyelem: AMSAT névlista nem elérhető ({exc})",
                      file=sys.stderr)
            ok += run("SatNOGS", lambda: satnogs.collect(conn, extra_names=hints))

        if "amsat" not in args.skip:
            total += 1
            ok += run("AMSAT státusz", lambda: amsat.collect(conn, hours=args.hours))

        if "amsatfreq" not in args.skip:
            total += 1
            ok += run("AMSAT frekvenciák", lambda: amsat_freq.collect(conn))

        if "hamsat" not in args.skip:
            total += 1
            ok += run("hams.at", lambda: hamsat.collect(conn))

        if "sstv" not in args.skip:
            total += 1
            ok += run("ARISS SSTV", lambda: save_sstv(conn))

        if "tle" not in args.skip:
            total += 1
            ok += run("Celestrak pályaelemek", lambda: tle.collect(conn))

        print(f"\n{ok}/{total} forrás sikeres")
        summary(conn)
    finally:
        conn.close()

    return 0 if ok == total else 1


def save_sstv(conn):
    events = [e for e in sstv.parse_events(sstv.fetch_html())
              if e["start_utc"] or e["end_utc"]]
    new, updated = db.save_sstv_events(conn, events)
    print(f"ARISS: {len(events)} dátumozott bejegyzés "
          f"({new} új, {updated} frissítve)")


def summary(conn):
    counts = {
        "amatőr műhold": "SELECT COUNT(*) FROM satellites WHERE is_amateur = 1",
        "transzponder": "SELECT COUNT(*) FROM transmitters WHERE status='active'",
        "AMSAT jelentés": "SELECT COUNT(*) FROM amsat_reports",
        "AMSAT frekv.": "SELECT COUNT(*) FROM amsat_frequencies",
        "aktiváció": "SELECT COUNT(*) FROM activations",
        "SSTV esemény": "SELECT COUNT(*) FROM sstv_events",
        "átvonulás": "SELECT COUNT(*) FROM passes",
        "pályaelem": "SELECT COUNT(*) FROM tle",
    }
    print("\nAdatbázis:")
    for label, sql in counts.items():
        print(f"  {label:<16} {conn.execute(sql).fetchone()[0]:>6}")


if __name__ == "__main__":
    sys.exit(main())
