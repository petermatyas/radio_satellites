"""Adatfrissítés a webes felületről, háttérszálon.

A SatNOGS import bő tízezer rekord, a gyűjtés összesen fél percig is
eltarthat — ezt nem lehet egy HTTP kérésen belül kivárni. Ezért a munka
külön szálon fut, a felület pedig lekérdezi az állapotát.
"""

import contextlib
import io
import threading
import time
import traceback

import amsat
import amsat_freq
import db
import hamsat
import main as n2yo
import satnogs
import sstv
import tle

# Megjelenítendő név -> (kulcs, függvény). A sorrend számít: a SatNOGS
# katalógus adja a NORAD ID-ket, amikre a többi forrás hivatkozik.
SOURCES = [
    ("passes", "Átvonulások (N2YO)"),
    ("satnogs", "Műholdak és transzponderek (SatNOGS)"),
    ("amsat", "Élő státusz (AMSAT)"),
    ("amsatfreq", "Frekvenciatáblák (AMSAT)"),
    ("hamsat", "Aktivációk (hams.at)"),
    ("sstv", "SSTV események (ARISS)"),
    ("tle", "Pályaelemek (Celestrak)"),
]
SOURCE_KEYS = [key for key, _ in SOURCES]


def _run_source(key, conn):
    if key == "passes":
        # days/min_elevation nélkül: a Beállításokban mentett érték a mérvadó.
        return n2yo.collect_tracked(conn)
    if key == "satnogs":
        hints = set()
        try:
            hints = amsat.catalog_names()
        except Exception as exc:
            print(f"figyelem: AMSAT névlista nem elérhető ({exc})")
        return satnogs.collect(conn, extra_names=hints)
    if key == "amsat":
        return amsat.collect(conn)
    if key == "amsatfreq":
        return amsat_freq.collect(conn)
    if key == "hamsat":
        return hamsat.collect(conn)
    if key == "sstv":
        events = [e for e in sstv.parse_events(sstv.fetch_html())
                  if e["start_utc"] or e["end_utc"]]
        new, updated = db.save_sstv_events(conn, events)
        print(f"ARISS: {len(events)} bejegyzés ({new} új, {updated} frissítve)")
        return {"events": len(events)}
    if key == "tle":
        return tle.collect(conn)
    raise ValueError(f"ismeretlen forrás: {key}")


class Refresher:
    """Egyszerre egy frissítés futhat; az állapotát a felület kérdezi le."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._thread = None
        self._state = {
            "running": False,
            "sources": [],
            "done": [],
            "current": None,
            "log": [],
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def status(self):
        with self._lock:
            return dict(self._state, log=list(self._state["log"]),
                        done=list(self._state["done"]))

    def start(self, keys):
        keys = [k for k in SOURCE_KEYS if k in keys]
        if not keys:
            return False, "nincs kiválasztott forrás"

        with self._lock:
            if self._state["running"]:
                return False, "már fut egy frissítés"
            self._state.update(running=True, sources=keys, done=[],
                               current=None, log=[], error=None,
                               started_at=time.time(), finished_at=None)

        self._thread = threading.Thread(target=self._work, args=(keys,),
                                        daemon=True)
        self._thread.start()
        return True, "elindult"

    def _log(self, text):
        with self._lock:
            self._state["log"].extend(
                line for line in text.splitlines() if line.strip())

    def _work(self, keys):
        labels = dict(SOURCES)
        # Saját kapcsolat: az SQLite objektumok nem oszthatók meg szálak közt.
        conn = db.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            for key in keys:
                with self._lock:
                    self._state["current"] = labels[key]
                buffer = io.StringIO()
                try:
                    # A gyűjtők a szabványos kimenetre írnak; azt fogjuk fel,
                    # hogy ugyanaz a részletes napló jelenjen meg a felületen.
                    with contextlib.redirect_stdout(buffer):
                        _run_source(key, conn)
                    self._log(buffer.getvalue())
                except Exception as exc:
                    self._log(buffer.getvalue())
                    self._log(f"HIBA ({labels[key]}): "
                              f"{exc.__class__.__name__}: {exc}")
                    traceback.print_exc()
                with self._lock:
                    self._state["done"].append(key)
        except Exception as exc:  # a szál sosem halhat el némán
            with self._lock:
                self._state["error"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            conn.close()
            with self._lock:
                self._state.update(running=False, current=None,
                                   finished_at=time.time())
