"""SQLite tároló az N2YO radio pass adatokhoz.

A duplikációt a (norad_id, start_utc) páron lévő UNIQUE index akadályozza meg:
ugyanaz az átvonulás akkor sem kerül be kétszer, ha többször töltjük le.
"""

import re
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).with_name("sats.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS satellites (
    norad_id INTEGER PRIMARY KEY,
    name     TEXT
);

CREATE TABLE IF NOT EXISTS passes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_id          INTEGER NOT NULL REFERENCES satellites(norad_id),
    observer_lat      REAL    NOT NULL,
    observer_lon      REAL    NOT NULL,
    observer_alt      REAL    NOT NULL,
    start_utc         INTEGER NOT NULL,
    start_az          REAL,
    start_az_compass  TEXT,
    start_el          REAL,
    max_utc           INTEGER,
    max_az            REAL,
    max_az_compass    TEXT,
    max_el            REAL,
    end_utc           INTEGER,
    end_az            REAL,
    end_az_compass    TEXT,
    end_el            REAL,
    fetched_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (norad_id, observer_lat, observer_lon, start_utc)
);

CREATE INDEX IF NOT EXISTS idx_passes_start ON passes (start_utc);

-- Mit meddig töltöttünk már le: ebből tudjuk, kell-e egyáltalán hálózni.
CREATE TABLE IF NOT EXISTS fetch_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_id      INTEGER NOT NULL,
    observer_lat  REAL    NOT NULL,
    observer_lon  REAL    NOT NULL,
    observer_alt  REAL    NOT NULL,
    min_elevation REAL    NOT NULL,
    covered_until INTEGER NOT NULL,
    fetched_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_lookup
    ON fetch_log (norad_id, observer_lat, observer_lon, min_elevation);

-- ariss.org SSTV bejelentések. Egy bejegyzést a fejléc (dátum + cím) azonosít.
CREATE TABLE IF NOT EXISTS sstv_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    posted_date   TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    norad_id      INTEGER,
    start_utc     INTEGER,
    end_utc       INTEGER,
    frequency_mhz REAL,
    mode          TEXT,
    source_text   TEXT,
    fetched_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (posted_date, title)
);

CREATE INDEX IF NOT EXISTS idx_sstv_range ON sstv_events (start_utc, end_utc);

-- SatNOGS DB transzponderek. A UUID az API-tól jön, az az elsődleges kulcs.
CREATE TABLE IF NOT EXISTS transmitters (
    uuid          TEXT PRIMARY KEY,
    norad_id      INTEGER,
    description   TEXT,
    type          TEXT,
    status        TEXT,
    service       TEXT,
    uplink_low    INTEGER,
    uplink_high   INTEGER,
    downlink_low  INTEGER,
    downlink_high INTEGER,
    mode          TEXT,
    uplink_mode   TEXT,
    baud          REAL,
    invert        INTEGER,
    is_amateur    INTEGER NOT NULL DEFAULT 0,
    updated       TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_norad ON transmitters (norad_id, status);

-- amsat.org státusz-katalógus. A név formája "ISS_[FM]", NORAD ID nincs benne,
-- azt a SatNOGS katalógusból illesztjük hozzá (lásd amsat.py).
CREATE TABLE IF NOT EXISTS amsat_catalog (
    amsat_name     TEXT PRIMARY KEY,
    display_name   TEXT,
    activity       TEXT,
    norad_id       INTEGER,
    website        TEXT,
    report_count   INTEGER,
    latest_report  TEXT,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Egyedi észlelési bejelentések; az id az AMSAT API-tól jön, így nem duplikál.
CREATE TABLE IF NOT EXISTS amsat_reports (
    id            INTEGER PRIMARY KEY,
    amsat_name    TEXT NOT NULL,
    reported_time TEXT NOT NULL,
    callsign      TEXT,
    report        TEXT,
    grid_square   TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_amsat_reports
    ON amsat_reports (amsat_name, reported_time);

-- hams.at rover-aktivációk: előre bejelentett, időponthoz kötött forgalom.
CREATE TABLE IF NOT EXISTS activations (
    id             TEXT PRIMARY KEY,
    norad_id       INTEGER,
    sat_name       TEXT,
    callsign       TEXT,
    mode           TEXT,
    mhz            REAL,
    mhz_direction  TEXT,
    grids          TEXT,
    comment        TEXT,
    url            TEXT,
    max_elevation  REAL,
    start_utc      INTEGER,
    end_utc        INTEGER,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activations_range
    ON activations (norad_id, start_utc, end_utc);

-- Az amsat.org üzemi frekvenciatáblái (Live FM / Linear / Digipeater /
-- Image). Ezt a státusz-API nem adja: itt van a CTCSS hang és az, hogy a
-- transzponder invertáló-e.
CREATE TABLE IF NOT EXISTS amsat_frequencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,
    sat_label       TEXT NOT NULL,
    sat_name        TEXT,
    norad_id        INTEGER,
    mode            TEXT,
    uplink_mhz      REAL,
    uplink_high_mhz REAL,
    ctcss_hz        REAL,
    downlink_mhz    REAL,
    downlink_high_mhz REAL,
    via             TEXT,
    comment         TEXT,
    detail_url      TEXT,
    source_url      TEXT,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (category, sat_label)
);

CREATE INDEX IF NOT EXISTS idx_amsat_freq_norad
    ON amsat_frequencies (norad_id);

-- Felhasználói beállítások kulcs-érték párokban (megfigyelő pozíciója, a
-- lekérdezés hossza), hogy a felületről és parancssorból ugyanaz érvényesüljön.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Mely műholdakra töltsünk átvonulást.
CREATE TABLE IF NOT EXISTS tracked_satellites (
    norad_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Pályaelemek a "hol jár most" grafikához. Műholdanként egy sor: mindig a
-- legfrissebb elemhalmaz érdekes, a régi epocha csak pontatlanná tenné.
CREATE TABLE IF NOT EXISTS tle (
    norad_id   INTEGER PRIMARY KEY,
    name       TEXT,
    line1      TEXT NOT NULL,
    line2      TEXT NOT NULL,
    epoch      TEXT,
    source_url TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# A gyári beállítások; a settings tábla ezeket írja felül.
DEFAULT_SETTINGS = {
    "lat": 47.231248,
    "lon": 16.625222,
    "alt": 100.0,
    "days": 1,
    "min_elevation": 10,
}
SETTING_TYPES = {"lat": float, "lon": float, "alt": float,
                 "days": int, "min_elevation": int}

# A megfigyelő pozíciója az átvonulás-rekordok kulcsának része, ezért fix
# pontosságra kerekítjük: enélkül a 47.231248 és a 47.2312 két külön helynek
# számítana, és ugyanaz az átvonulás kétszer kerülne be. Hat tizedesjegy
# nagyjából tizedméteres felbontás — jóval a szükséges alatt.
COORD_DECIMALS = 6


def normalize_observer(lat, lon, alt):
    return (round(float(lat), COORD_DECIMALS),
            round(float(lon), COORD_DECIMALS),
            round(float(alt), 1))

# Utólag hozzávett oszlopok: a séma menet közben bővült, a meglévő
# adatbázisokban ALTER TABLE-lel pótoljuk őket.
ADDED_COLUMNS = {
    "satellites": {
        "sat_id": "TEXT",
        "alt_names": "TEXT",
        "status": "TEXT",
        "decayed": "TEXT",
        "launched": "TEXT",
        "operator": "TEXT",
        "countries": "TEXT",
        "website": "TEXT",
        "is_amateur": "INTEGER NOT NULL DEFAULT 0",
        "updated": "TEXT",
    },
    "sstv_events": {
        "source_url": "TEXT",  # a letöltött oldal
        "info_url": "TEXT",    # a bejegyzésben szereplő hivatkozás, ha van
    },
}


def migrate(conn):
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    seed_tracked(conn)
    conn.commit()


def seed_tracked(conn):
    """A követett műholdak listájának egyszeri feltöltése.

    A lista előbb létezett fájlban megadott NORAD ID-k formájában, ezért a
    már letöltött átvonulásokból indulunk. Csak egyszer fut le: enélkül az
    utolsó műhold törlése után a régi átvonulásokból újratöltődne a lista.
    """
    done = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'tracked_seeded'").fetchone()
    if done:
        return

    conn.execute(
        "INSERT OR IGNORE INTO tracked_satellites (norad_id) "
        "SELECT DISTINCT norad_id FROM passes")
    conn.execute("INSERT INTO settings (key, value) VALUES ('tracked_seeded', '1')")


def connect(db_path=DEFAULT_DB_PATH):
    """Kapcsolat nyitása és a séma létrehozása, ha még nincs meg."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def stale_positions(conn, observer):
    """Más megfigyelői pozícióra mentett átvonulások összesítése.

    Pozícióváltás után a régi helyre számolt sorok bent maradnak. Ugyanazok
    az események, más koordinátákkal — a listákból kiszűrjük őket, de a
    számlálókat torzítják, ezért felajánljuk a törlésüket.
    """
    lat, lon, _alt = normalize_observer(*observer)
    return conn.execute(
        "SELECT COUNT(*) AS passes, COUNT(DISTINCT norad_id) AS sats, "
        "       COUNT(DISTINCT observer_lat || ',' || observer_lon) AS places "
        "FROM passes WHERE observer_lat <> ? OR observer_lon <> ?",
        (lat, lon),
    ).fetchone()


def delete_stale_positions(conn, observer):
    """A nem a jelenlegi pozícióhoz tartozó átvonulások törlése."""
    lat, lon, _alt = normalize_observer(*observer)
    cur = conn.execute(
        "DELETE FROM passes WHERE observer_lat <> ? OR observer_lon <> ?",
        (lat, lon))
    deleted = cur.rowcount
    # A letöltési napló ugyanazzal a kulccsal dolgozik; enélkül a régi
    # bejegyzés azt hazudná, hogy az adat már megvan.
    conn.execute(
        "DELETE FROM fetch_log WHERE observer_lat <> ? OR observer_lon <> ?",
        (lat, lon))
    conn.commit()
    return deleted


def get_settings(conn):
    """A beállítások a gyári értékekkel kiegészítve, típushelyesen."""
    values = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] not in SETTING_TYPES:
            continue
        try:
            values[row["key"]] = SETTING_TYPES[row["key"]](row["value"])
        except (TypeError, ValueError):
            pass  # sérült érték: marad a gyári
    return values


def save_settings(conn, values):
    """Csak az ismert kulcsokat mentjük, típusellenőrzéssel."""
    saved = {}
    for key, raw in values.items():
        if key not in SETTING_TYPES or raw is None or raw == "":
            continue
        value = SETTING_TYPES[key](raw)
        if key in ("lat", "lon"):
            value = round(value, COORD_DECIMALS)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, str(value)),
        )
        saved[key] = value
    conn.commit()
    return saved


def get_tracked(conn):
    """A követett műholdak: (NORAD ID, név) párok."""
    return [(r["norad_id"], r["name"]) for r in conn.execute(
        "SELECT t.norad_id, s.name FROM tracked_satellites t "
        "LEFT JOIN satellites s ON s.norad_id = t.norad_id "
        "ORDER BY t.norad_id"
    )]


def set_tracked(conn, norad_ids):
    """A követett műholdak listájának felülírása."""
    wanted = sorted({int(n) for n in norad_ids})
    conn.execute("DELETE FROM tracked_satellites")
    conn.executemany("INSERT INTO tracked_satellites (norad_id) VALUES (?)",
                     [(n,) for n in wanted])
    conn.commit()
    return wanted


def track(conn, norad_id):
    """Egy műhold felvétele a listára."""
    conn.execute("INSERT OR IGNORE INTO tracked_satellites (norad_id) "
                 "VALUES (?)", (int(norad_id),))
    conn.commit()
    return [n for n, _ in get_tracked(conn)]


def untrack(conn, norad_id):
    """Egy műhold levétele a listáról; a letöltött átvonulásai megmaradnak."""
    cur = conn.execute("DELETE FROM tracked_satellites WHERE norad_id = ?",
                       (int(norad_id),))
    conn.commit()
    return cur.rowcount > 0


def save_satellite(conn, norad_id, name):
    conn.execute(
        "INSERT INTO satellites (norad_id, name) VALUES (?, ?) "
        "ON CONFLICT(norad_id) DO UPDATE SET name = excluded.name",
        (norad_id, name),
    )


def save_passes(conn, norad_id, observer, passes):
    """Átvonulások mentése.

    observer: (lat, lon, alt) tuple
    passes:   az N2YO válasz "passes" listája
    Visszatér: (új sorok száma, már meglévő – kihagyott – sorok száma)
    """
    lat, lon, alt = normalize_observer(*observer)
    inserted = 0

    for p in passes:
        cur = conn.execute(
            """
            INSERT INTO passes (
                norad_id, observer_lat, observer_lon, observer_alt,
                start_utc, start_az, start_az_compass, start_el,
                max_utc, max_az, max_az_compass, max_el,
                end_utc, end_az, end_az_compass, end_el
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (norad_id, observer_lat, observer_lon, start_utc)
            DO NOTHING
            """,
            (
                norad_id, lat, lon, alt,
                p.get("startUTC"), p.get("startAz"),
                p.get("startAzCompass"), p.get("startEl"),
                p.get("maxUTC"), p.get("maxAz"),
                p.get("maxAzCompass"), p.get("maxEl"),
                p.get("endUTC"), p.get("endAz"),
                p.get("endAzCompass"), p.get("endEl"),
            ),
        )
        inserted += cur.rowcount

    conn.commit()
    return inserted, len(passes) - inserted


def existing_keys(conn, table, columns):
    """A tábla meglévő kulcsai halmazként.

    Az ON CONFLICT DO UPDATE ág az SQLite-ban 1 érintett sort jelent, ezért a
    cursor.rowcount nem különbözteti meg az új sort a frissítettől. A beszúrás
    előtt kiolvasott kulcsokkal viszont pontosan tudjuk számolni.
    """
    cols = ", ".join(columns)
    rows = conn.execute(f"SELECT {cols} FROM {table}").fetchall()
    if len(columns) == 1:
        return {r[0] for r in rows}
    return {tuple(r) for r in rows}


def save_satellite_details(conn, sats):
    """SatNOGS műholdadatok mentése a satellites táblába.

    A name-t nem írjuk felül, ha az N2YO-tól már van rá érték... épp
    ellenkezőleg: a SatNOGS név a beszédesebb, viszont a passes tábla
    hivatkozik a sorra, ezért csak bővítünk, sosem törlünk.
    """
    known = existing_keys(conn, "satellites", ['norad_id'])
    inserted = 0
    for s in sats:
        conn.execute(
            """
            INSERT INTO satellites (
                norad_id, name, sat_id, alt_names, status, decayed, launched,
                operator, countries, website, is_amateur, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(norad_id) DO UPDATE SET
                name       = excluded.name,
                sat_id     = excluded.sat_id,
                alt_names  = excluded.alt_names,
                status     = excluded.status,
                decayed    = excluded.decayed,
                launched   = excluded.launched,
                operator   = excluded.operator,
                countries  = excluded.countries,
                website    = excluded.website,
                is_amateur = excluded.is_amateur,
                updated    = excluded.updated
            """,
            (s["norad_id"], s["name"], s.get("sat_id"), s.get("alt_names"),
             s.get("status"), s.get("decayed"), s.get("launched"),
             s.get("operator"), s.get("countries"), s.get("website"),
             1 if s.get("is_amateur") else 0, s.get("updated")),
        )
        inserted += s["norad_id"] not in known
    conn.commit()
    return inserted, len(sats) - inserted


def save_transmitters(conn, transmitters):
    """Transzponderek mentése; a SatNOGS UUID a kulcs."""
    known = existing_keys(conn, "transmitters", ['uuid'])
    inserted = 0
    for t in transmitters:
        conn.execute(
            """
            INSERT INTO transmitters (
                uuid, norad_id, description, type, status, service,
                uplink_low, uplink_high, downlink_low, downlink_high,
                mode, uplink_mode, baud, invert, is_amateur, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uuid) DO UPDATE SET
                norad_id      = excluded.norad_id,
                description   = excluded.description,
                type          = excluded.type,
                status        = excluded.status,
                service       = excluded.service,
                uplink_low    = excluded.uplink_low,
                uplink_high   = excluded.uplink_high,
                downlink_low  = excluded.downlink_low,
                downlink_high = excluded.downlink_high,
                mode          = excluded.mode,
                uplink_mode   = excluded.uplink_mode,
                baud          = excluded.baud,
                invert        = excluded.invert,
                is_amateur    = excluded.is_amateur,
                updated       = excluded.updated,
                fetched_at    = datetime('now')
            """,
            (t["uuid"], t.get("norad_id"), t.get("description"), t.get("type"),
             t.get("status"), t.get("service"), t.get("uplink_low"),
             t.get("uplink_high"), t.get("downlink_low"), t.get("downlink_high"),
             t.get("mode"), t.get("uplink_mode"), t.get("baud"),
             1 if t.get("invert") else 0, 1 if t.get("is_amateur") else 0,
             t.get("updated")),
        )
        inserted += t["uuid"] not in known
    conn.commit()
    return inserted, len(transmitters) - inserted


def save_amsat_catalog(conn, entries):
    known = existing_keys(conn, "amsat_catalog", ['amsat_name'])
    inserted = 0
    for e in entries:
        conn.execute(
            """
            INSERT INTO amsat_catalog (
                amsat_name, display_name, activity, norad_id, website,
                report_count, latest_report
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(amsat_name) DO UPDATE SET
                display_name  = excluded.display_name,
                activity      = excluded.activity,
                norad_id      = excluded.norad_id,
                website       = excluded.website,
                report_count  = excluded.report_count,
                latest_report = excluded.latest_report,
                fetched_at    = datetime('now')
            """,
            (e["amsat_name"], e.get("display_name"), e.get("activity"),
             e.get("norad_id"), e.get("website"), e.get("report_count"),
             e.get("latest_report")),
        )
        inserted += e["amsat_name"] not in known
    conn.commit()
    return inserted, len(entries) - inserted


def save_amsat_frequencies(conn, entries):
    """Az amsat.org frekvenciatábláinak sorai."""
    known = existing_keys(conn, "amsat_frequencies", ["category", "sat_label"])
    inserted = 0
    for e in entries:
        conn.execute(
            """
            INSERT INTO amsat_frequencies (
                category, sat_label, sat_name, norad_id, mode,
                uplink_mhz, uplink_high_mhz, ctcss_hz,
                downlink_mhz, downlink_high_mhz, via, comment,
                detail_url, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (category, sat_label) DO UPDATE SET
                sat_name          = excluded.sat_name,
                norad_id          = excluded.norad_id,
                mode              = excluded.mode,
                uplink_mhz        = excluded.uplink_mhz,
                uplink_high_mhz   = excluded.uplink_high_mhz,
                ctcss_hz          = excluded.ctcss_hz,
                downlink_mhz      = excluded.downlink_mhz,
                downlink_high_mhz = excluded.downlink_high_mhz,
                via               = excluded.via,
                comment           = excluded.comment,
                detail_url        = excluded.detail_url,
                source_url        = excluded.source_url,
                fetched_at        = datetime('now')
            """,
            (e["category"], e["sat_label"], e.get("sat_name"), e.get("norad_id"),
             e.get("mode"), e.get("uplink_mhz"), e.get("uplink_high_mhz"),
             e.get("ctcss_hz"), e.get("downlink_mhz"), e.get("downlink_high_mhz"),
             e.get("via"), e.get("comment"), e.get("detail_url"),
             e.get("source_url")),
        )
        inserted += (e["category"], e["sat_label"]) not in known
    conn.commit()
    return inserted, len(entries) - inserted


def get_amsat_websites(conn):
    """NORAD ID -> az AMSAT katalógus website mezője.

    A státuszoldalon a műhold nevére kattintva ide jut az ember (jellemzően
    a megfelelő "Live ... Satellites" táblázatra).
    """
    websites = {}
    # Egy műholdhoz több bejegyzés tartozhat (ISS_[FM], ISS_[SSTV]...), ezért
    # rögzített sorrendből vesszük az elsőt, hogy a link ne ugráljon.
    for row in conn.execute(
        "SELECT norad_id, website FROM amsat_catalog "
        "WHERE norad_id IS NOT NULL AND website IS NOT NULL AND website <> '' "
        "ORDER BY amsat_name"
    ):
        websites.setdefault(row["norad_id"], row["website"])
    return websites


def get_frequency_map(conn, norad_ids=None):
    """NORAD ID -> az amsat.org frekvenciatáblák sorai."""
    sql = "SELECT * FROM amsat_frequencies WHERE norad_id IS NOT NULL"
    params = []
    if norad_ids:
        sql += f" AND norad_id IN ({','.join('?' * len(norad_ids))})"
        params = list(norad_ids)
    sql += " ORDER BY category, sat_label"

    by_sat = {}
    for row in conn.execute(sql, params):
        by_sat.setdefault(row["norad_id"], []).append(row)
    return by_sat


def save_amsat_reports(conn, reports):
    """Észlelési bejelentések; az AMSAT id-je miatt az újrafuttatás nem duplikál."""
    inserted = 0
    for r in reports:
        cur = conn.execute(
            "INSERT INTO amsat_reports (id, amsat_name, reported_time, "
            "callsign, report, grid_square) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (r["id"], r["amsat_name"], r["reported_time"], r.get("callsign"),
             r.get("report"), r.get("grid_square")),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted, len(reports) - inserted


def save_activations(conn, activations):
    """hams.at aktivációk; az uuid a kulcs, a részletek frissülhetnek."""
    known = existing_keys(conn, "activations", ['id'])
    inserted = 0
    for a in activations:
        conn.execute(
            """
            INSERT INTO activations (
                id, norad_id, sat_name, callsign, mode, mhz, mhz_direction,
                grids, comment, url, max_elevation, start_utc, end_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                norad_id      = excluded.norad_id,
                sat_name      = excluded.sat_name,
                callsign      = excluded.callsign,
                mode          = excluded.mode,
                mhz           = excluded.mhz,
                mhz_direction = excluded.mhz_direction,
                grids         = excluded.grids,
                comment       = excluded.comment,
                url           = excluded.url,
                max_elevation = excluded.max_elevation,
                start_utc     = excluded.start_utc,
                end_utc       = excluded.end_utc,
                fetched_at    = datetime('now')
            """,
            (a["id"], a.get("norad_id"), a.get("sat_name"), a.get("callsign"),
             a.get("mode"), a.get("mhz"), a.get("mhz_direction"),
             a.get("grids"), a.get("comment"), a.get("url"),
             a.get("max_elevation"), a.get("start_utc"), a.get("end_utc")),
        )
        inserted += a["id"] not in known
    conn.commit()
    return inserted, len(activations) - inserted


def save_sstv_events(conn, events):
    """SSTV bejelentések mentése.

    A (posted_date, title) páron ütköző sort frissítjük, mert az ariss.org
    utólag pontosíthatja a még TBD adatokat. Visszatér: (új, frissített).
    """
    known = existing_keys(conn, "sstv_events", ['posted_date', 'title'])
    inserted = 0
    for e in events:
        conn.execute(
            """
            INSERT INTO sstv_events (
                posted_date, title, norad_id, start_utc, end_utc,
                frequency_mhz, mode, source_text, source_url, info_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (posted_date, title) DO UPDATE SET
                norad_id      = excluded.norad_id,
                start_utc     = excluded.start_utc,
                end_utc       = excluded.end_utc,
                frequency_mhz = excluded.frequency_mhz,
                mode          = excluded.mode,
                source_text   = excluded.source_text,
                source_url    = excluded.source_url,
                info_url      = excluded.info_url,
                fetched_at    = datetime('now')
            """,
            (e["posted_date"], e["title"], e.get("norad_id"),
             e.get("start_utc"), e.get("end_utc"), e.get("frequency_mhz"),
             e.get("mode"), e.get("source_text"), e.get("source_url"),
             e.get("info_url")),
        )
        inserted += (e["posted_date"], e["title"]) not in known
    conn.commit()
    return inserted, len(events) - inserted


# Ha egy bejelentésből nem sikerült véget kiolvasni, ennyi ideig tekintjük
# élőnek a kezdés után. Az ARISS kampányok jellemzően néhány naposak, enélkül
# a lezáratlan 2025-ös bejegyzések örökre "folyamatban" maradnának.
SSTV_ASSUMED_LENGTH = 7 * 86400


def get_sstv_events(conn, since=None):
    """SSTV bejelentések időrendben; since után még nem lezárult események."""
    sql = "SELECT * FROM sstv_events WHERE start_utc IS NOT NULL"
    params = []
    if since is not None:
        sql += " AND COALESCE(end_utc, start_utc + ?) >= ?"
        params += [SSTV_ASSUMED_LENGTH, int(since)]
    sql += " ORDER BY start_utc"
    return conn.execute(sql, params).fetchall()


def get_sstv_intervals(conn, since):
    """Lezárt idejű SSTV események, amik since után is tartanak.

    A norad_id nélküli bejelentéseket az ISS-hez soroljuk, mert az ARISS
    alapesetben arról szól.
    """
    return conn.execute(
        "SELECT *, COALESCE(norad_id, 25544) AS effective_norad "
        "FROM sstv_events "
        "WHERE start_utc IS NOT NULL AND end_utc IS NOT NULL AND end_utc >= ? "
        "ORDER BY start_utc",
        (int(since),),
    ).fetchall()


def get_satellites(conn, observer=None):
    """Azok a műholdak, amikhez van mentett átvonulás."""
    sql = ("SELECT s.norad_id, s.name, COUNT(p.id) AS pass_count "
           "FROM satellites s JOIN passes p ON p.norad_id = s.norad_id")
    params = []
    if observer is not None:
        sql += " WHERE p.observer_lat = ? AND p.observer_lon = ?"
        params += [observer[0], observer[1]]
    sql += " GROUP BY s.norad_id, s.name ORDER BY s.name"
    return conn.execute(sql, params).fetchall()


def name_index(conn):
    """Normalizált név -> NORAD ID, az AMSAT nevek beazonosításához.

    A satellites.name és az alt_names minden szava külön kulcs, mert a
    SatNOGS így tárolja az OSCAR-jelöléseket ("UOSAT 2" / "UO-11 OSCAR-11").
    """
    index = {}
    rows = conn.execute(
        "SELECT norad_id, name, alt_names FROM satellites WHERE norad_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        raw = f"{row['name'] or ''} {row['alt_names'] or ''}"
        for token in re.split(r"[\s;,()\[\]]+", raw):
            key = re.sub(r"[^A-Z0-9]", "", token.upper())
            if len(key) >= 3:
                index.setdefault(key, row["norad_id"])
    return index


def get_transmitter_map(conn, norad_ids=None):
    """NORAD ID -> aktív amatőr transzponderek listája."""
    sql = ("SELECT * FROM transmitters "
           "WHERE status = 'active' AND is_amateur = 1 AND norad_id IS NOT NULL")
    params = []
    if norad_ids:
        sql += f" AND norad_id IN ({','.join('?' * len(norad_ids))})"
        params = list(norad_ids)
    sql += " ORDER BY downlink_low"

    by_sat = {}
    for row in conn.execute(sql, params):
        by_sat.setdefault(row["norad_id"], []).append(row)
    return by_sat


def get_activations(conn, since):
    """Bejelentett rover-aktivációk, amik since után is tartanak."""
    return conn.execute(
        "SELECT * FROM activations "
        "WHERE start_utc IS NOT NULL AND end_utc IS NOT NULL AND end_utc >= ? "
        "ORDER BY start_utc",
        (int(since),),
    ).fetchall()


def get_reports(conn, norad_id, since_iso=None, activity=None, limit=500):
    """Az adott műholdról szóló egyedi AMSAT bejelentések.

    Ez a "ki és mikor hallotta" adat: hívójel, időpont, Maidenhead-négyzet.
    """
    sql = ("SELECT r.*, c.display_name, c.activity, c.norad_id "
           "FROM amsat_reports r "
           "JOIN amsat_catalog c ON c.amsat_name = r.amsat_name "
           "WHERE c.norad_id = ?")
    params = [norad_id]
    if since_iso:
        sql += " AND r.reported_time >= ?"
        params.append(since_iso)
    if activity:
        sql += " AND c.activity = ?"
        params.append(activity)
    sql += " ORDER BY r.reported_time DESC, r.callsign LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_report_activities(conn, norad_id, since_iso=None):
    """Milyen aktivitásokról van egyáltalán jelentésünk az adott műholdhoz.

    A since_iso ugyanazt az időablakot vágja le, mint a get_reports — így a
    szűrő-csempéken látszó darabszám a listával egyezik.
    """
    sql = ("SELECT c.activity, c.display_name, COUNT(*) AS reports, "
           "       MAX(r.reported_time) AS latest "
           "FROM amsat_reports r JOIN amsat_catalog c ON c.amsat_name = r.amsat_name "
           "WHERE c.norad_id = ?")
    params = [norad_id]
    if since_iso:
        sql += " AND r.reported_time >= ?"
        params.append(since_iso)
    sql += " GROUP BY c.activity, c.display_name ORDER BY latest DESC"
    return conn.execute(sql, params).fetchall()


def count_reports_before(conn, norad_id, before_iso):
    """Hány bejelentésünk van az időablakon kívülről — csak jelzésnek."""
    return conn.execute(
        "SELECT COUNT(*) FROM amsat_reports r "
        "JOIN amsat_catalog c ON c.amsat_name = r.amsat_name "
        "WHERE c.norad_id = ? AND r.reported_time < ?",
        (norad_id, before_iso),
    ).fetchone()[0]


def get_activity_map(conn, since_iso):
    """NORAD ID -> friss AMSAT észlelések aktivitásonként.

    Egy műholdhoz több AMSAT bejegyzés tartozhat (ISS_[FM], ISS_[SSTV]...),
    ezért aktivitásonként külön sort adunk vissza.
    """
    rows = conn.execute(
        """
        SELECT c.norad_id, c.display_name, c.activity,
               MAX(r.reported_time) AS latest,
               COUNT(*) AS reports
        FROM amsat_catalog c
        JOIN amsat_reports r ON r.amsat_name = c.amsat_name
        WHERE c.norad_id IS NOT NULL AND r.reported_time >= ?
          AND r.report IN ('Heard', 'Crew Active')
        GROUP BY c.norad_id, c.display_name, c.activity
        ORDER BY latest DESC
        """,
        (since_iso,),
    ).fetchall()

    activity = {}
    for row in rows:
        activity.setdefault(row["norad_id"], []).append(row)
    return activity


def get_amateur_satellites(conn):
    """Az aktív rádióamatőr műhold-katalógus, transzponderszámmal."""
    return conn.execute(
        """
        SELECT s.*,
               COUNT(t.uuid) AS tx_count,
               (SELECT COUNT(*) FROM passes p WHERE p.norad_id = s.norad_id)
                   AS pass_count,
               (SELECT MAX(r.reported_time) FROM amsat_catalog c
                  JOIN amsat_reports r ON r.amsat_name = c.amsat_name
                 WHERE c.norad_id = s.norad_id AND r.report IN
                       ('Heard', 'Crew Active')) AS last_heard
        FROM satellites s
        LEFT JOIN transmitters t
               ON t.norad_id = s.norad_id AND t.status = 'active'
              AND t.is_amateur = 1
        WHERE s.is_amateur = 1 AND s.decayed IS NULL
        GROUP BY s.norad_id
        ORDER BY last_heard DESC NULLS LAST, s.name
        """
    ).fetchall()


def coverage_until(conn, norad_id, observer, min_elevation):
    """Meddig (unix time) van már letöltve adat erre a műhold+megfigyelő párosra.

    A szigorúbb (kisebb) min_elevation-nel készült letöltés tartalmazza a
    lazábbat is, ezért az is elfogadható találat. Ha még nincs adat: 0.
    """
    lat, lon, _alt = normalize_observer(*observer)
    row = conn.execute(
        "SELECT MAX(covered_until) FROM fetch_log "
        "WHERE norad_id = ? AND observer_lat = ? AND observer_lon = ? "
        "  AND min_elevation <= ?",
        (norad_id, lat, lon, min_elevation),
    ).fetchone()
    return row[0] or 0


def record_fetch(conn, norad_id, observer, min_elevation, covered_until):
    """Letöltés tényének rögzítése."""
    lat, lon, alt = normalize_observer(*observer)
    conn.execute(
        "INSERT INTO fetch_log (norad_id, observer_lat, observer_lon, "
        "observer_alt, min_elevation, covered_until) VALUES (?, ?, ?, ?, ?, ?)",
        (norad_id, lat, lon, alt, min_elevation, int(covered_until)),
    )
    conn.commit()


def get_passes(conn, norad_id=None, limit=None, since=None, observer=None):
    """Mentett átvonulások lekérdezése időrendben.

    since:    unix time; csak azok az átvonulások, amik ekkor még nem értek
              véget (a most zajló átvonulás is benne marad).
    observer: (lat, lon); pozícióváltás után a régi helyre számolt
              átvonulások bent maradnak az adatbázisban, de nem érdekesek.
    """
    sql = (
        "SELECT p.*, s.name AS sat_name FROM passes p "
        "LEFT JOIN satellites s ON s.norad_id = p.norad_id"
    )
    where, params = [], []
    if norad_id is not None:
        where.append("p.norad_id = ?")
        params.append(norad_id)
    if since is not None:
        where.append("p.end_utc >= ?")
        params.append(int(since))
    if observer is not None:
        where.append("p.observer_lat = ? AND p.observer_lon = ?")
        params += [observer[0], observer[1]]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.start_utc"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def save_tle(conn, entries):
    """Pályaelemek mentése műholdanként; a meglévő sort felülírja.

    Egy TLE néhány nap alatt elavul, ezért nem gyűjtjük a történetet: mindig
    a legutóbb letöltött elemhalmaz marad.
    """
    new = updated = 0
    for e in entries:
        exists = conn.execute("SELECT 1 FROM tle WHERE norad_id = ?",
                              (e["norad_id"],)).fetchone()
        conn.execute(
            "INSERT INTO tle (norad_id, name, line1, line2, epoch, source_url,"
            "                 fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(norad_id) DO UPDATE SET "
            "  name = excluded.name, line1 = excluded.line1, "
            "  line2 = excluded.line2, epoch = excluded.epoch, "
            "  source_url = excluded.source_url, "
            "  fetched_at = excluded.fetched_at",
            (e["norad_id"], e.get("name"), e["line1"], e["line2"],
             e.get("epoch"), e.get("source_url")),
        )
        if exists:
            updated += 1
        else:
            new += 1
    conn.commit()
    return new, updated


def get_tle_map(conn, norad_ids=None):
    """NORAD ID -> pályaelem sor."""
    sql = "SELECT * FROM tle"
    params = []
    if norad_ids is not None:
        ids = list(norad_ids)
        if not ids:
            return {}
        sql += f" WHERE norad_id IN ({','.join('?' * len(ids))})"
        params = ids
    return {r["norad_id"]: r for r in conn.execute(sql, params)}


def tle_ages(conn, norad_ids):
    """NORAD ID -> hány órája töltöttük le a tárolt pályaelemet."""
    ids = list(norad_ids)
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT norad_id, (julianday('now') - julianday(fetched_at)) * 24 "
        f"AS hours FROM tle WHERE norad_id IN ({','.join('?' * len(ids))})",
        ids)
    return {r["norad_id"]: r["hours"] for r in rows}
