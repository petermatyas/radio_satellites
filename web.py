"""Webes felület: a következő műhold-átvonulások és a hozzájuk tartozó
rádiós információk (frekvencia, üzemmód, bejelentett aktivitás)."""

import argparse
import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import amsat_freq
import countries
import db
import orbit
import refresh as refresh_module
import sstv

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
app.config["DB_PATH"] = db.DEFAULT_DB_PATH
app.config["APPLICATION_ROOT"] = "/sats"
refresher = refresh_module.Refresher(app.config["DB_PATH"])

# Ennyi ideig tekintünk egy AMSAT észlelést "friss"-nek.
ACTIVITY_WINDOW_HOURS = 72

# A "ki hallotta" lista alapból ennyi órára visszamenőleg mutat bejelentést.
REPORTS_WINDOW_HOURS = 24

# Választható időablakok a bejelentések lapján. A felső határ az AMSAT API
# maximuma (30 nap), ennél régebbit úgysem tudunk begyűjteni.
REPORTS_WINDOWS = [
    (24, "24 óra"),
    (48, "2 nap"),
    (72, "3 nap"),
    (168, "1 hét"),
    (720, "30 nap"),
]


def reports_url(norad_id, activity=None, age_hours=None):
    """Hivatkozás a bejelentésekre, elég tág időablakkal.

    Az átvonulás-listán 72 órán belüli észlelés is látszik, a lap viszont
    alapból csak {REPORTS_WINDOW_HOURS} órát mutat — ha a link nem vinné
    magával a szükséges ablakot, üres listára érkeznénk.
    """
    params = {}
    if activity:
        params["activity"] = activity
    if age_hours is not None and age_hours >= REPORTS_WINDOW_HOURS:
        params["hours"] = next(
            (hours for hours, _ in REPORTS_WINDOWS if hours > age_hours),
            REPORTS_WINDOWS[-1][0])
    query = f"?{urlencode(params)}" if params else ""
    prefix = request.script_root if request else app.config.get("APPLICATION_ROOT", "")
    return f"{prefix}/reports/{norad_id}{query}"

AMSAT_STATUS_URL = "https://www.amsat.org/status/index.php"
AMSAT_REPORTS_API = "https://www.amsat.org/status/api/v1/reports.php"
SATNOGS_SAT_URL = "https://db.satnogs.org/satellite/{}/"
N2YO_SAT_URL = "https://www.n2yo.com/satellite/?s={}"


def satnogs_url(sat_id, norad_id):
    """A műhold SatNOGS DB oldala; sat_id hiányában a NORAD ID is működik."""
    return SATNOGS_SAT_URL.format(sat_id or norad_id)


def n2yo_url(norad_id):
    """A műhold N2YO-oldala: élő követés, pályaadatok, TLE.

    Az átvonulásokat is innen kérjük le, de a felületen eddig nem szerepelt
    hivatkozás rá — pedig a keringési idő, a hajlásszög és a láthatósági kör
    csak ott látszik.
    """
    return N2YO_SAT_URL.format(norad_id)


def satellite_info_url(website, norad_id):
    """A műholdról szóló legbeszédesebb külső oldal.

    A saját honlap (misszió- vagy egyetemi oldal) mondja a legtöbbet arról,
    ami nálunk nem látszik; ha nincs, az N2YO pályaadatai jönnek. Visszaadja
    a címet és a domaint, hogy a hivatkozás ne legyen meglepetés.
    """
    url = website or n2yo_url(norad_id)
    return url, host_of(url)


def amsat_report_url(amsat_name, hours=None):
    """A bejelentést tartalmazó konkrét AMSAT lekérdezés.

    Az amsat.org-on nincs bejelentésenkénti oldal, és a státuszrács sem
    szűrhető linkkel — a legpontosabb, amire mutatni tudunk, az az API-hívás,
    amiből maga a sor származik: reports.php?name=ISS_[FM].
    """
    query = {"name": amsat_name, "hours": hours or ACTIVITY_WINDOW_HOURS}
    return f"{AMSAT_REPORTS_API}?{urlencode(query)}"


def host_of(url):
    """Domain a link címkéjéhez: "ariss-usa.org/ARISS_SSTV/..." -> ariss-usa.org"""
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def sstv_module_url():
    return sstv.URL


# Az SSTV eseményeknek több forrása van, ezért a "ki jelentette be" nem lehet
# beégetve. A domainből képezzük, a két ismert forrásnak rövid nevet adva.
SSTV_SOURCE_NAMES = {"ariss.org": "ARISS", "r4uab.ru": "R4UAB"}


def sstv_source_name(source_url):
    host = host_of(source_url or sstv_module_url())
    return SSTV_SOURCE_NAMES.get(host, host or "SSTV")


AMSAT_HOST = "amsat.org"

# Az amsat.org gyűjtőoldalai: ezek nem egy műholdról szólnak, tehát nem
# számítanak saját aloldalnak akkor sem, ha a névcella ezekre hivatkozik
# (az Image tábla ISS sora például a státuszoldalra mutat).
GENERIC_AMSAT_URLS = {
    "https://www.amsat.org/status",
    "https://www.amsat.org/status/index.php",
    *(page["url"].rstrip("/") for page in amsat_freq.PAGES),
}


def is_satellite_page(url):
    return (url and host_of(url) == AMSAT_HOST
            and url.rstrip("/") not in GENERIC_AMSAT_URLS)


def amsat_links(frequencies, website):
    """Az adott műholdhoz tartozó amsat.org hivatkozások.

    Az amsat.org-on nincs műholdankénti státuszoldal, ezért így rangsorolunk:
    saját aloldal (pl. /two-way-satellites/so-50-satellite-information/) >
    a katalógus website mezője > az adott kategória táblázata > a
    státuszoldal. Az első a "fő" link, a többi kiegészítés.
    """
    primary = None
    extra = []

    for row in frequencies:
        if primary is None and is_satellite_page(row["detail_url"]):
            primary = row["detail_url"]

    if primary is None:
        primary = website

    for row in frequencies:
        url = row["source_url"]
        if url and url != primary and url not in [u for _, u in extra]:
            extra.append((f"AMSAT {row['category']}", url))

    if primary is None:
        primary = extra.pop(0)[1] if extra else AMSAT_STATUS_URL

    return primary, extra


def mhz(hz):
    return None if hz is None else round(hz / 1_000_000, 4)


def format_transmitter(row):
    """Transzponder egysoros alakja: "FM  145.990 ↑ / 437.800 ↓"."""
    down, up = mhz(row["downlink_low"]), mhz(row["uplink_low"])
    parts = []
    if up:
        parts.append(f"{up:g}↑")
    if down:
        parts.append(f"{down:g}↓")
    return {
        "mode": row["mode"] or "?",
        "freq": " / ".join(parts) or "?",
        "description": row["description"],
        "type": row["type"],
        "baud": row["baud"],
    }


def format_frequency(row):
    """Az amsat.org üzemi tábláinak egy sora, megjelenítésre kész alakban."""
    def rng(low, high):
        if low is None:
            return None
        return f"{low:g}" if high is None else f"{low:g}–{high:g}"

    detail = row["detail_url"]
    if detail and host_of(detail) == AMSAT_HOST and not is_satellite_page(detail):
        detail = None

    return {
        "category": row["category"],
        "mode": row["mode"],
        "uplink": rng(row["uplink_mhz"], row["uplink_high_mhz"]),
        "downlink": rng(row["downlink_mhz"], row["downlink_high_mhz"]),
        "ctcss": row["ctcss_hz"],
        "via": row["via"],
        "comment": row["comment"],
        # A névcella hivatkozása gyakran a műhold saját oldalára visz
        # (crocube.hr, ariss.org...), ezért külön jelöljük a domainnel. Az
        # amsat.org gyűjtőoldalaira mutatót elhagyjuk: az a forrássorban van.
        "detail_url": detail,
        "detail_host": host_of(detail),
        "source_url": row["source_url"],
    }


def format_sstv(row):
    """SSTV esemény a megjelenítéshez; a nyers mezők None-ok lehetnek."""
    parts = []
    if row["frequency_mhz"]:
        parts.append(f"{row['frequency_mhz']:g} MHz")
    if row["mode"]:
        parts.append(row["mode"])
    return {
        "title": row["title"],
        "details": " · ".join(parts),
        "start": datetime.fromtimestamp(row["start_utc"]),
        "end": datetime.fromtimestamp(row["end_utc"]),
        "url": row["info_url"] or row["source_url"],
    }


def format_activation(row):
    parts = []
    if row["mode"]:
        parts.append(row["mode"])
    if row["mhz"]:
        arrow = "↓" if row["mhz_direction"] == "down" else "↑"
        parts.append(f"{row['mhz']:g} MHz{arrow}")
    return {
        "callsign": row["callsign"],
        "details": " · ".join(parts),
        "grids": row["grids"],
        "comment": row["comment"],
        "url": row["url"],
        "start": datetime.fromtimestamp(row["start_utc"]),
        "end": datetime.fromtimestamp(row["end_utc"]),
    }


def format_activity(rows):
    """AMSAT észlelések: mikor és milyen aktivitásban hallották utoljára."""
    out = []
    for row in rows:
        latest = datetime.strptime(row["latest"], "%Y-%m-%dT%H:%M:%SZ")
        activity = row["activity"] or row["display_name"]
        age_hours = int((datetime.now(timezone.utc)
                         - latest.replace(tzinfo=timezone.utc))
                        .total_seconds() // 3600)
        out.append({
            "activity": activity,
            "latest": latest,
            "age_hours": age_hours,
            "reports": row["reports"],
            "url": reports_url(row["norad_id"], activity, age_hours),
        })
    return out


# Az AMSAT észlelés az aktivitást nevezi meg (FM, SSTV, U/v), az amsat.org
# üzemi táblája viszont a szolgáltatás fajtáját (FM, Image, Linear) — ahol a
# kettő ugyanarról szól, ott az észlelést a frekvenciasorhoz kötjük, hogy ne
# kelljen a felhasználónak fejben összepárosítania.
#
# Csak az egyértelmű párokat soroljuk fel: a telemetria, a DATV vagy a
# QO-100 szélessávú átjátszója nem feleltethető meg egyetlen sornak sem,
# ezek külön címkeként maradnak.
ACTIVITY_CATEGORIES = {
    "FM": "FM",
    "Crew": "FM",          # az ISS-en a legénység hangforgalma az FM-en megy
    "SSTV": "Image",
    "SSDV": "Image",
    "VHF_Digi": "Digipeater",
    "UHF_Digi": "Digipeater",
    "V/u_Digi": "Digipeater",
    # A lineáris átjátszók jelölése az irányokat adja meg (uplink/downlink):
    # U/v = 70 cm fel, 2 m le, és így tovább.
    "U/v": "Linear",
    "V/u": "Linear",
    "V/a": "Linear",
    "C/x": "Linear",
}


def attach_activity(frequencies, activities):
    """Az észleléseket a hozzájuk tartozó frekvenciasorhoz fűzi.

    Egy műholdhoz kategóriánként legfeljebb egy üzemi sor tartozik, ezért a
    hozzárendelés egyértelmű. Visszaadja azokat az észleléseket, amiknek nem
    találtunk sort — ezek a kártyán önálló címkeként maradnak.
    """
    by_category = {(f["category"] or "").lower(): f for f in frequencies}
    unmatched = []
    for item in activities:
        category = ACTIVITY_CATEGORIES.get(item["activity"])
        row = by_category.get((category or "").lower())
        if row is None:
            unmatched.append(item)
        else:
            row.setdefault("heard", []).append(item)
    return unmatched


def overlapping(intervals, start, end, norad_id, norad_key="norad_id"):
    """Azok az intervallumok, amik átfedik az adott átvonulást."""
    return [row for row in intervals
            if row[norad_key] == norad_id
            and row["start_utc"] <= end and row["end_utc"] >= start]


# A pályaelem ennyi nap után már érezhetően pontatlan; a felületen jelezzük.
TLE_STALE_DAYS = 7


def map_xy(lat, lon):
    """Szélesség/hosszúság -> a 360x180-as térkép viewBox koordinátái."""
    return round(lon + 180, 2), round(90 - lat, 2)


def map_heading(row, when=None, seconds=60):
    """A haladási irány szöge a térkép koordinátáiban, fokban.

    A pillanatnyi és a "seconds" másodperccel későbbi hely különbségéből.
    Azért itt számoljuk, és nem a böngészőben: a ±180. hosszúsági fok
    átlépését így egy helyen kezeljük, a pályanyom szeletelésétől függetlenül.

    A visszaadott szög közvetlenül SVG rotate()-be tehető: a térkép y tengelye
    lefelé nő, ahogy a képernyőé is.
    """
    now = orbit.position(row, when=when)
    later = orbit.position(
        row, when=(when or datetime.now(timezone.utc)) + timedelta(seconds=seconds))

    dlon = later["lon"] - now["lon"]
    # Dátumvonal: a 179° -> -179° ugrás valójában 2 fok kelet felé.
    if dlon > 180:
        dlon -= 360
    elif dlon < -180:
        dlon += 360

    # A térképen y = 90 - lat, tehát az északi irány NEGATÍV y.
    return round(math.degrees(math.atan2(-(later["lat"] - now["lat"]), dlon)), 1)


def track_segments(points):
    """Pályanyom térkép-koordinátákban, a dátumvonalnál elvágva.

    A nyom a ±180. hosszúsági foknál átfordul; ha egyben rajzolnánk, egy
    vízszintes vonal szaladna át a térképen.
    """
    segments, current, previous = [], [], None
    for lat, lon in points:
        if previous is not None and abs(lon - previous) > 180:
            segments.append(current)
            current = []
        current.append(map_xy(lat, lon))
        previous = lon
    if current:
        segments.append(current)
    return [s for s in segments if len(s) > 1]


def live_positions(conn, norad_ids, observer, track=True):
    """NORAD ID -> hol jár most a műhold, és merről látszik.

    A pályaelem hiánya vagy elavulása nem hiba: a felület ilyenkor csak a
    térképet rajzolja ki, jelölő nélkül.
    """
    positions = {}
    for norad, row in db.get_tle_map(conn, norad_ids).items():
        try:
            now = orbit.position(row)
            look = orbit.look_angles(row, observer)
            points = orbit.ground_track(row) if track else []
        except (orbit.OrbitError, ValueError) as exc:
            positions[norad] = {"error": str(exc)}
            continue
        x, y = map_xy(now["lat"], now["lon"])
        age = orbit.epoch_age_days(row)
        positions[norad] = {
            "lat": round(now["lat"], 3),
            "lon": round(now["lon"], 3),
            "alt_km": round(now["alt_km"]),
            "x": x,
            "y": y,
            "az": round(look["az"], 1),
            "el": round(look["el"], 1),
            "range_km": round(look["range_km"]),
            # A jelölő mellé rajzolt nyíl szöge: merre halad a műhold.
            "heading": map_heading(row),
            "epoch_age_days": round(age, 1),
            "stale": age > TLE_STALE_DAYS,
        }
        if track:
            # Csak kérésre kerül bele. ÜRES listát sem küldünk helyette: a
            # felület a mező HIÁNYÁBÓL tudja, hogy a meglévő nyomot meg kell
            # tartania — egy üres lista azt jelentené, hogy nincs nyom, és
            # letörölné a térképről.
            positions[norad]["track"] = track_segments(points)
    return positions


def sky_point(az, el):
    """Azimut és magasság -> pont az égbolt-korongon.

    A korong sugara 1: a közepe a zenit, a széle a horizont, az észak
    felfelé, a kelet jobbra van — ahogy a hanyatt fekve tartott térképen.
    """
    if az is None or el is None:
        return None
    radius = max(0.0, min(1.0, (90.0 - el) / 90.0))
    angle = math.radians(az)
    return (round(radius * math.sin(angle), 3),
            round(-radius * math.cos(angle), 3))


def sky_arc(row):
    """Az átvonulás íve az égbolt-korongon, SVG útvonalként.

    Három pontunk van (kelés, tetőzés, nyugvás); a köztük lévő szakaszt egy
    másodfokú Bézier-görbe közelíti, aminek a kontrollpontját úgy választjuk
    meg, hogy a görbe átmenjen a tetőponton.
    """
    start = sky_point(row["start_az"], row["start_el"] or 0)
    top = sky_point(row["max_az"], row["max_el"])
    end = sky_point(row["end_az"], row["end_el"] or 0)
    if not (start and top and end):
        return None
    control = (round(2 * top[0] - (start[0] + end[0]) / 2, 3),
               round(2 * top[1] - (start[1] + end[1]) / 2, 3))
    return {
        "start": start,
        "top": top,
        "end": end,
        "control": control,
        "path": (f"M{start[0]:g} {start[1]:g} "
                 f"Q{control[0]:g} {control[1]:g} "
                 f"{end[0]:g} {end[1]:g}"),
    }


def arc_point(sky, t):
    """Pont az égbolt-íven: t=0 a kelés, t=0.5 a tetőzés, t=1 a nyugvás.

    A jelölő így pontosan a kirajzolt görbén marad — a pályaelemből számolt
    valódi irány ettől hajszálnyit eltérne, és a pont leugrana a vonalról.
    """
    t = max(0.0, min(1.0, t))
    u = 1 - t
    s, c, e = sky["start"], sky["control"], sky["end"]
    return (round(u * u * s[0] + 2 * u * t * c[0] + t * t * e[0], 3),
            round(u * u * s[1] + 2 * u * t * c[1] + t * t * e[1], 3))


def pass_progress(row, now):
    """Hol tart az átvonulás 0 és 1 között, a tetőzésre pontosan 0,5-öt adva.

    A tetőzés ritkán esik a kelés és a nyugvás felezőpontjára, az ív viszont
    a felénél megy át a tetőponton — ezért a két szakaszt külön skálázzuk.
    """
    start, end = row["start_utc"], row["end_utc"]
    top = row["max_utc"]
    if not (start and end) or end <= start:
        return None
    if not top or not start < top < end:
        return (now - start) / (end - start)
    if now <= top:
        return 0.5 * (now - start) / (top - start)
    return 0.5 + 0.5 * (now - top) / (end - top)


def format_pass(row, now, extras):
    start, end = row["start_utc"], row["end_utc"]
    return {
        "norad_id": row["norad_id"],
        "sat_name": row["sat_name"] or f"NORAD {row['norad_id']}",
        "start": datetime.fromtimestamp(start),
        "end": datetime.fromtimestamp(end),
        # A jelölő mozgatásához a böngészőnek is kellenek a nyers időpontok.
        "start_utc": start,
        "max_utc": row["max_utc"],
        "end_utc": end,
        "duration_min": round((end - start) / 60, 1),
        "max_el": row["max_el"],
        "start_compass": row["start_az_compass"],
        "end_compass": row["end_az_compass"],
        "max_compass": row["max_az_compass"],
        "starts_in": start - now,
        "in_progress": start <= now <= end,
        **extras,
    }


WEEKDAYS_HU = ["hétfő", "kedd", "szerda", "csütörtök",
               "péntek", "szombat", "vasárnap"]


def format_day(date):
    """Dátum magyar napnévvel; a mai/holnapi nap külön jelölve."""
    today = datetime.now().date()
    delta = (date - today).days
    prefix = {0: "Ma", 1: "Holnap"}.get(delta)
    label = f"{date:%Y. %m. %d.} ({WEEKDAYS_HU[date.weekday()]})"
    return f"{prefix} &middot; {label}" if prefix else label


def humanize(seconds):
    """Hátralévő idő rövid, olvasható alakban."""
    if seconds <= 0:
        return "most"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} perc múlva"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} óra {minutes} perc múlva"
    days, hours = divmod(hours, 24)
    return f"{days} nap {hours} óra múlva"


def humanize_age(hours):
    if hours < 1:
        return "az elmúlt órában"
    if hours < 24:
        return f"{hours} órája"
    return f"{hours // 24} napja"


def normalize_norad_id_values(raw_values):
    """Többszörös vagy vesszővel elkülönített NORAD ID-ket normalizálja."""
    values = raw_values or []
    if isinstance(values, (str, int)):
        values = [values]
    ids = []
    for raw in values:
        if isinstance(raw, (list, tuple, set)):
            ids.extend(normalize_norad_id_values(raw))
            continue
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            if part.isdigit():
                value = int(part)
                if value not in ids:
                    ids.append(value)
    return ids


# A szűrőhöz a sok tucat nyers üzemmódot néhány gyakorlati csoportba vonjuk
# össze: a hallgató arra szűr, hogy mit tud fogni, nem a moduláció betűszavára.
# A sorrend a csempék sorrendje is.
MODE_GROUPS = [
    ("fm", "FM"),
    ("ssb", "SSB/CW"),
    ("sstv", "SSTV/kép"),
    ("digi", "APRS/digi"),
    ("weather", "Időjárási kép"),
    ("data", "Telemetria"),
    ("datv", "DATV"),
]
MODE_LABELS = dict(MODE_GROUPS)

# Ennyi műhold-csempe látszik alapból a következő átvonulások szűrőjében; a
# többi a "+N további" feliratra nyílik ki. Néhány tucat követett műholdnál a
# teljes sor elnyomná magát a listát.
VISIBLE_CHIPS = 6

# A SatNOGS mód mezőjének első szava dönt: a "FSK AX.100 Mode 5" vagy a
# "BPSK PMT-A3" ugyanabba a csoportba tartozik, mint a puszta FSK és BPSK.
# Ami nincs a táblában (GMSK, LoRa, DOKA...), az telemetria.
MODE_ALIASES = {
    "FM": "fm", "FMN": "fm",
    "USB": "ssb", "LSB": "ssb", "SSB": "ssb", "CW": "ssb", "AM": "ssb",
    "SSTV": "sstv", "SSDV": "sstv",
    "APRS": "digi",
    "APT": "weather", "HRPT": "weather", "LRPT": "weather",
    "DVB-S2": "datv", "DVB-S": "datv", "DATV": "datv",
}

# Az amsat.org üzemi táblájának kategóriái — ez a felhasználói szemlélet, a
# moduláció helyett a szolgáltatás fajtája.
CATEGORY_MODES = {
    "fm": "fm",
    "linear": "ssb",
    "image": "sstv",
    "digipeater": "digi",
}

# Az időjárási képadás nem amatőr szolgálat, ezért ezek a módok csak a nem
# amatőr adók közül jönnek (lásd db.get_nonamateur_modes).
WEATHER_MODES = {"APT", "HRPT", "LRPT"}


def mode_group(raw):
    """Nyers üzemmód -> csoportkulcs; ismeretlen esetén telemetria."""
    token = (raw or "").strip().split()[0].upper() if (raw or "").strip() else ""
    if not token:
        return None
    return MODE_ALIASES.get(token, "data")


def pass_mode_groups(entry, extra_modes=()):
    """Egy átvonuláshoz tartozó módcsoportok halmaza.

    Ugyanabból az adatból dolgozik, amit a kártya is mutat: a transzponderek,
    az amsat.org üzemi sorai, a futó SSTV esemény és a bejelentett aktivitások.
    Az extra_modes a nem amatőr (időjárási) adók módjait hozza, amik a
    kártyán nem szerepelnek, de fogni lehet őket.
    """
    groups = set()
    for tx in entry["transmitters"]:
        # Az APRS digipeater csak a leírásban különül el a telemetriától.
        text = (tx["description"] or "").lower()
        if "aprs" in text or "digi" in text:
            groups.add("digi")
        else:
            groups.add(mode_group(tx["mode"]))
    for freq in entry["frequencies"]:
        groups.add(CATEGORY_MODES.get((freq["category"] or "").lower())
                   or mode_group(freq["mode"]))
    for activation in entry["activations"]:
        groups.add(mode_group(activation["details"].split(" · ")[0]))
    if entry["sstv"]:
        groups.add("sstv")
    for raw in extra_modes:
        if (raw or "").strip().split()[0].upper() in WEATHER_MODES:
            groups.add("weather")
    groups.discard(None)
    return groups


def normalize_modes(raw_values):
    """A kért módcsoportok, a csempék sorrendjében; az ismeretlent elhagyjuk."""
    wanted = {str(value).strip().lower() for value in raw_values or []}
    return [key for key, _ in MODE_GROUPS if key in wanted]


def pass_filter_url(norad_ids, modes):
    """A következő átvonulások lapja a megadott szűrőkkel."""
    params = ([("norad_id", n) for n in norad_ids]
              + [("mode", m) for m in modes])
    prefix = request.script_root if request else app.config.get("APPLICATION_ROOT", "")
    path = f"{prefix}/" if prefix else "/"
    return path + ("?" + urlencode(params) if params else "")


def toggled(values, value):
    """A kiválasztott elemek listája a value be- vagy kikapcsolása után."""
    if value in values:
        return [v for v in values if v != value]
    return list(values) + [value]


@app.route("/")
def index():
    now = int(time.time())
    selected = normalize_norad_id_values(request.args.getlist("norad_id"))
    modes = normalize_modes(request.args.getlist("mode"))
    since_iso = ((datetime.now(timezone.utc)
                  - timedelta(hours=ACTIVITY_WINDOW_HOURS))
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    conn = db.connect(app.config["DB_PATH"])
    try:
        # Csak a beállított pozícióra számolt átvonulások: a korábbi helyre
        # letöltöttek bent maradnak az adatbázisban, de nem keverednek ide.
        config = db.get_settings(conn)
        observer = (config["lat"], config["lon"])
        rows = db.get_passes(conn, norad_ids=selected or None, since=now,
                             observer=observer)
        satellites = db.get_satellites(conn, observer=observer)
        upcoming = db.count_upcoming_passes(conn, observer, now)
        # Néhány műholdra sok átvonulás jut, ezért a kiegészítő adatokat
        # egyszer olvassuk be, és memóriában párosítjuk.
        norads = {r["norad_id"] for r in rows}
        catalog = {r["norad_id"]: r for r in conn.execute(
            "SELECT norad_id, sat_id, website, countries FROM satellites")}
        transmitters = db.get_transmitter_map(conn, norads)
        frequencies = db.get_frequency_map(conn, norads)
        other_modes = db.get_nonamateur_modes(conn, norads)
        websites = db.get_amsat_websites(conn)
        activity = db.get_activity_map(conn, since_iso)
        sstv_events = db.get_sstv_intervals(conn, now)
        activations = db.get_activations(conn, now)
        # A "hol jár most" grafikához műholdanként egy pályaszámítás kell,
        # nem átvonulásonként — a kártyák ugyanazt a jelölőt használják. A
        # pályanyomot a böngésző kéri le külön, hogy ne kelljen minden
        # kártyába beleírni ugyanazt a néhány száz pontot.
        positions = live_positions(conn, norads,
                                   (config["lat"], config["lon"],
                                    config["alt"]), track=False)
    finally:
        conn.close()

    passes = []
    for row in rows:
        sat = row["norad_id"]
        start, end = row["start_utc"], row["end_utc"]
        sstv = overlapping(sstv_events, start, end, sat, "effective_norad")
        roves = overlapping(activations, start, end, sat)
        freqs = [format_frequency(f) for f in frequencies.get(sat, [])]
        amsat_main, amsat_extra = amsat_links(freqs, websites.get(sat))
        sky = sky_arc(row)
        entry = catalog.get(sat)
        info_url, info_host = satellite_info_url(
            entry["website"] if entry else None, sat)
        item = format_pass(row, now, {
            "sstv": format_sstv(sstv[0]) if sstv else None,
            "activations": [format_activation(a) for a in roves],
            "transmitters": [format_transmitter(t)
                             for t in transmitters.get(sat, [])],
            "frequencies": freqs,
            # Ami frekvenciasorhoz köthető, az ott jelenik meg; a maradék
            # marad önálló "hallották" címkének.
            "activity": attach_activity(freqs,
                                        format_activity(activity.get(sat, []))),
            "satnogs_url": satnogs_url(entry["sat_id"] if entry else None, sat),
            "amsat_url": amsat_main,
            "amsat_extra": amsat_extra,
            "n2yo_url": n2yo_url(sat),
            # A műhold nevére kattintva a róla szóló külső oldal nyílik meg:
            # küldetésleírás, pályaadatok — ami a listán nem fér el.
            "info_url": info_url,
            "info_host": info_host,
            # A katalógusból jövő országkód(ok); a név mellé zászlót rajzolunk.
            "countries": entry["countries"] if entry else None,
            "sky": sky,
            # Az égbolt-korongon csak akkor van jelölő, ha éppen ez az
            # átvonulás zajlik — máskor a műhold nem ezen az íven jár.
            "sky_now": (arc_point(sky, pass_progress(row, now))
                        if sky and row["start_utc"] <= now <= row["end_utc"]
                        else None),
        })
        item["modes"] = sorted(pass_mode_groups(item, other_modes.get(sat, ())),
                               key=lambda key: list(MODE_LABELS).index(key))
        passes.append(item)

    # A módcsempéket a műholdszűrés eredményéből számoljuk, de a módszűrés
    # előtti állapotból: így csak olyan csempe jelenik meg, amire van találat,
    # és a darabszám a rákattintás utáni listával egyezik.
    mode_counts = {key: sum(1 for p in passes if key in p["modes"])
                   for key, _ in MODE_GROUPS}
    if modes:
        passes = [p for p in passes if any(m in p["modes"] for m in modes)]

    # Napokra bontva, hogy a lista olvasható maradjon.
    days = []
    for p in passes:
        day = p["start"].date()
        if not days or days[-1]["date"] != day:
            days.append({"date": day, "passes": []})
        days[-1]["passes"].append(p)

    # A csempesor kompakt: alapból a legtöbb jövőbeli átvonulást adó néhány
    # műhold látszik, és amit épp kiválasztottunk — hogy a bekapcsolt szűrőt
    # mindig lehessen kikapcsolni. A sorrend a névsor marad, csak a rejtés
    # dől el a gyakoriság szerint.
    frequent = sorted(satellites,
                      key=lambda s: (-upcoming.get(s["norad_id"], 0),
                                     s["name"] or ""))[:VISIBLE_CHIPS]
    visible = {s["norad_id"] for s in frequent} | set(selected)
    sat_filters = [{
        "norad_id": s["norad_id"],
        "name": s["name"],
        "active": s["norad_id"] in selected,
        "extra": s["norad_id"] not in visible,
        "url": pass_filter_url(toggled(selected, s["norad_id"]), modes),
    } for s in satellites]

    # A két szűrő egymástól függetlenül kapcsolható: a csempe URL-je a másik
    # szűrő állapotát mindig megtartja.
    mode_filters = [{
        "key": key,
        "label": MODE_LABELS[key],
        "count": mode_counts[key],
        "active": key in modes,
        "url": pass_filter_url(selected, toggled(modes, key)),
    } for key, _ in MODE_GROUPS if mode_counts[key]]

    return render_template(
        "index.html",
        days=days,
        total=len(passes),
        sstv_count=sum(1 for p in passes if p["sstv"]),
        rove_count=sum(len(p["activations"]) for p in passes),
        selected=selected,
        sat_filters=sat_filters,
        hidden_count=sum(1 for s in sat_filters if s["extra"]),
        all_satellites_url=pass_filter_url([], modes),
        modes=modes,
        mode_filters=mode_filters,
        all_modes_url=pass_filter_url(selected, []),
        observer=observer,
        positions=positions,
        observer_xy=map_xy(observer[0], observer[1]),
        now=datetime.fromtimestamp(now),
    )


@app.route("/api/positions")
def api_positions():
    """A követett műholdak pillanatnyi helye — a grafikák ebből frissülnek.

    A számítás a tárolt pályaelemekből helyben történik, hálózat nélkül,
    ezért néhány másodpercenként is olcsón kérdezhető.
    """
    wanted = [int(n) for n in (request.args.get("norad") or "").split(",")
              if n.strip().isdigit()] or None

    conn = db.connect(app.config["DB_PATH"])
    try:
        config = db.get_settings(conn)
        observer = (config["lat"], config["lon"], config["alt"])
        if wanted is None:
            wanted = [norad for norad, _ in db.get_tracked(conn)]
        # A pályanyom a válasz négyötöde, de 5 másodperc alatt alig változik,
        # ezért a felület csak ritkán kéri (?track=0 a többi lekérdezésnél).
        want_track = request.args.get("track", "1") not in ("0", "false", "no")
        positions = live_positions(conn, wanted, observer, track=want_track)
    finally:
        conn.close()

    return jsonify({
        "at": int(time.time()),
        "observer": {"lat": observer[0], "lon": observer[1]},
        "positions": {str(k): v for k, v in positions.items()},
    })


@app.route("/events")
def events():
    """Várható rádiós események: SSTV kampányok és bejelentett aktivációk.

    Az átvonulás-listán csak az látszik, ami időben átfedi a helyi
    átvonulásainkat — egy tengerentúli rover ablaka viszont ritkán fedi át,
    ezért az események önmagukban is megjelennek itt.

    A ?kind=rove szűrő a hams.at-ról jövő egyéni aktivációkat hagyja meg,
    a ?kind=sstv az ARISS kampányokat.
    """
    now = int(time.time())
    kind = request.args.get("kind")
    if kind not in ("sstv", "rove"):
        kind = None

    conn = db.connect(app.config["DB_PATH"])
    try:
        sstv_rows = db.get_sstv_events(conn, since=now)
        rove_rows = db.get_activations(conn, now)
        tracked = {r["norad_id"] for r in db.get_satellites(conn)}
    finally:
        conn.close()

    items = []
    for row in sstv_rows:
        # A bejegyzésbe linkelt doppler-táblázat / sajtóközlemény többet mond,
        # mint a gyűjtőoldal — ha van, arra mutatunk.
        links = [(host_of(row["info_url"]), row["info_url"])] if row["info_url"] else []
        source_url = row["source_url"] or sstv_module_url()
        links.append((host_of(source_url), source_url))
        items.append({
            "kind": "sstv",
            "start": datetime.fromtimestamp(row["start_utc"]),
            "end": datetime.fromtimestamp(row["end_utc"]) if row["end_utc"] else None,
            "starts_in": row["start_utc"] - now,
            "title": row["title"],
            "who": sstv_source_name(row["source_url"]),
            "norad_id": row["norad_id"],
            "tracked": row["norad_id"] in tracked,
            "details": " · ".join(filter(None, [
                f"{row['frequency_mhz']:g} MHz" if row["frequency_mhz"] else None,
                row["mode"],
            ])),
            "extra": None,
            "links": links,
        })
    for row in rove_rows:
        links = [("hams.at", row["url"])] if row["url"] else []
        if row["norad_id"]:
            links.append(("SatNOGS", satnogs_url(None, row["norad_id"])))
        items.append({
            "kind": "rove",
            "start": datetime.fromtimestamp(row["start_utc"]),
            "end": datetime.fromtimestamp(row["end_utc"]),
            "starts_in": row["start_utc"] - now,
            "title": row["sat_name"] or f"NORAD {row['norad_id']}",
            "who": row["callsign"],
            "norad_id": row["norad_id"],
            "tracked": row["norad_id"] in tracked,
            "details": format_activation(row)["details"],
            "extra": " · ".join(filter(None, [row["grids"], row["comment"]])),
            "links": links,
        })

    items.sort(key=lambda i: i["start"])
    counts = {
        "sstv": sum(1 for i in items if i["kind"] == "sstv"),
        "rove": sum(1 for i in items if i["kind"] == "rove"),
    }
    if kind:
        items = [i for i in items if i["kind"] == kind]
    return render_template("events.html", items=items, kind=kind,
                           counts=counts, total=sum(counts.values()),
                           now=datetime.fromtimestamp(now))


@app.route("/satellites")
def satellites():
    """Az összegyűjtött rádióamatőr műhold-katalógus."""
    since_iso = ((datetime.now(timezone.utc)
                  - timedelta(hours=ACTIVITY_WINDOW_HOURS))
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))
    query = (request.args.get("q") or "").strip().lower()
    heard_only = request.args.get("heard") == "1"

    conn = db.connect(app.config["DB_PATH"])
    try:
        rows = db.get_amateur_satellites(conn)
        transmitters = db.get_transmitter_map(conn)
        frequencies = db.get_frequency_map(conn)
        websites = db.get_amsat_websites(conn)
        activity = db.get_activity_map(conn, since_iso)
        tracked = {norad for norad, _ in db.get_tracked(conn)}
    finally:
        conn.close()

    sats = []
    for row in rows:
        blob = f"{row['name']} {row['alt_names'] or ''} {row['norad_id']}".lower()
        if query and query not in blob:
            continue
        acts = format_activity(activity.get(row["norad_id"], []))
        if heard_only and not acts:
            continue
        freqs = [format_frequency(f)
                 for f in frequencies.get(row["norad_id"], [])]
        amsat_main, amsat_extra = amsat_links(freqs, websites.get(row["norad_id"]))
        info_url, info_host = satellite_info_url(row["website"], row["norad_id"])
        sats.append({
            "norad_id": row["norad_id"],
            "name": row["name"],
            "alt_names": row["alt_names"],
            "countries": row["countries"],
            "launched": (row["launched"] or "")[:4],
            "website": row["website"],
            "info_url": info_url,
            "info_host": info_host,
            "tracked": row["norad_id"] in tracked,
            "satnogs_url": satnogs_url(row["sat_id"], row["norad_id"]),
            "n2yo_url": n2yo_url(row["norad_id"]),
            "amsat_url": amsat_main,
            "amsat_extra": amsat_extra,
            "pass_count": row["pass_count"],
            "transmitters": [format_transmitter(t)
                             for t in transmitters.get(row["norad_id"], [])],
            "frequencies": freqs,
            "activity": acts,
        })

    return render_template(
        "satellites.html",
        sats=sats,
        total=len(rows),
        query=request.args.get("q") or "",
        heard_only=heard_only,
        window_hours=ACTIVITY_WINDOW_HOURS,
    )


NORAD_SPLIT_RE = re.compile(r"[\s,;]+")


def format_number(value):
    """Szám űrlapmezőbe: teljes pontossággal, felesleges nullák nélkül.

    A "%g" itt nem jó: hat értékes jegyre kerekít, azaz a 47.231248
    szélességből 47.2312 lenne, és a mentés új megfigyelői pozíciót hozna
    létre ugyanarra a helyre.
    """
    if isinstance(value, float):
        return f"{value:.{db.COORD_DECIMALS}f}".rstrip("0").rstrip(".")
    return value


def parse_norad_ids(text):
    """Szabad szövegből NORAD ID-k: vessző, szóköz, új sor mind elválasztó."""
    ids, invalid = [], []
    for token in NORAD_SPLIT_RE.split((text or "").strip()):
        if not token:
            continue
        # Az érvényes katalógusszámok 1 és 999999 közé esnek.
        if token.isdigit() and 0 < int(token) < 1_000_000:
            ids.append(int(token))
        else:
            invalid.append(token)
    return sorted(set(ids)), invalid


def redirect_with(target, **params):
    """303-as átirányítás, a nem üres paraméterekkel a cél URL query részében."""
    query = urlencode({key: value for key, value in params.items() if value})
    if query:
        target += ("&" if "?" in target else "?") + query
    return redirect(target, code=303)


def track_feedback(args):
    """A felvétel visszajelzése átirányítás után: (üzenetek, hibák).

    Session nincs, ezért a /settings/track a query stringben adja át, mi
    került fel a listára (added), mi volt már rajta (kept), és mit nem
    sikerült értelmezni (invalid).
    """
    messages, errors = [], []
    added, _ = parse_norad_ids(args.get("added"))
    kept, _ = parse_norad_ids(args.get("kept"))
    if added:
        messages.append("Felvéve a követett műholdak közé: "
                        + ", ".join(str(n) for n in added) + ".")
    if kept:
        messages.append("Már a listán volt: "
                        + ", ".join(str(n) for n in kept) + ".")
    _, invalid = parse_norad_ids(args.get("invalid"))
    if invalid:
        errors.append(f"Érvénytelen NORAD ID: {', '.join(invalid)}")
    if args.get("fetch") == "started":
        messages.append("Az adatok letöltése elindult: pályaelemek és "
                        "átvonulások. A frissítés végén az oldal újratölt.")
    elif args.get("fetch") == "busy":
        errors.append("Épp fut egy másik frissítés, ezért az új műhold adatai "
                      "még nem töltődtek le. Indítsd el a Frissítést, ha az "
                      "befejeződött.")
    return messages, errors


REPORT_LABELS = {
    "Heard": ("hallotta", "ok"),
    "Crew Active": ("legénység adott", "ok"),
    "Telemetry Only": ("csak telemetria", "warn"),
    "Not Heard": ("nem hallotta", "bad"),
}


@app.route("/reports/<int:norad_id>")
def reports(norad_id):
    """Ki és mikor hallotta — az AMSAT bejelentések tételesen.

    Alapból az elmúlt REPORTS_WINDOW_HOURS órát mutatjuk: ez a "most
    hallható-e" kérdésre válaszol. A ?hours= paraméterrel az ablak
    kiterjeszthető a régebbi bejelentésekre is.
    """
    activity = request.args.get("activity") or None
    hours = request.args.get("hours", type=int) or REPORTS_WINDOW_HOURS
    # Csak a felkínált ablakokat engedjük: a lista így kiszámítható marad, és
    # nem lehet egyetlen kéréssel az egész táblát végigolvastatni.
    if hours not in {h for h, _ in REPORTS_WINDOWS}:
        hours = REPORTS_WINDOW_HOURS
    since_iso = ((datetime.now(timezone.utc) - timedelta(hours=hours))
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    conn = db.connect(app.config["DB_PATH"])
    try:
        rows = db.get_reports(conn, norad_id, since_iso=since_iso,
                              activity=activity)
        activities = db.get_report_activities(conn, norad_id,
                                              since_iso=since_iso)
        older = db.count_reports_before(conn, norad_id, since_iso)
        sat = conn.execute(
            "SELECT norad_id, name, sat_id FROM satellites WHERE norad_id = ?",
            (norad_id,)).fetchone()
    finally:
        conn.close()

    items = []
    for row in rows:
        when = datetime.strptime(row["reported_time"], "%Y-%m-%dT%H:%M:%SZ")
        label, kind = REPORT_LABELS.get(row["report"], (row["report"], "warn"))
        items.append({
            "utc": when,
            # Az AMSAT UTC-ben jelent; helyi időben is kiírjuk, hogy az
            # átvonulás-listával összevethető legyen.
            "local": when.replace(tzinfo=timezone.utc).astimezone(),
            "callsign": row["callsign"],
            "grid": row["grid_square"],
            "activity": row["activity"] or row["display_name"],
            "label": label,
            "kind": kind,
            # A jelentés konkrét forrása: az a lekérdezés, amiből ez a sor jött.
            "source_url": amsat_report_url(row["amsat_name"], hours),
        })

    # A következő tágabb ablak: ide vezet a "van még régebbi" hivatkozás.
    wider = next((h for h, _ in REPORTS_WINDOWS if h > hours), None)
    return render_template(
        "reports.html",
        items=items,
        activities=activities,
        selected=activity,
        # Ha a szűkebb ablakban nincs bejelentés a kiválasztott aktivitásról,
        # a csempéje eltűnne — a szűrés viszont él, ezért külön jelezzük.
        selected_missing=bool(activity) and activity not in {
            row["activity"] for row in activities},
        norad_id=norad_id,
        older=older,
        windows=REPORTS_WINDOWS,
        wider=wider,
        sat_name=(sat["name"] if sat else None) or f"NORAD {norad_id}",
        window_hours=hours,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    conn = db.connect(app.config["DB_PATH"])
    messages, errors = [], []
    submitted = None
    try:
        if request.method == "POST":
            # Előbb mindent ellenőrzünk, és csak hibátlan űrlapot mentünk: egy
            # elgépelt szélesség miatt nem veszhetnek el a többi mezőben
            # megadott értékek.
            numbers = {}
            for key, cast in db.SETTING_TYPES.items():
                raw = request.form.get(key)
                try:
                    numbers[key] = cast(raw)
                except (TypeError, ValueError):
                    errors.append(f"A(z) „{key}” mező értéke nem szám: {raw!r}")

            if errors:
                submitted = request.form
            else:
                db.save_settings(conn, numbers)
                messages.append(
                    f"Mentve: {numbers['lat']}, {numbers['lon']} "
                    f"({numbers['alt']:g} m), {numbers['days']} nap.")
        else:
            # A követés felvétele külön végpont (/settings/track), ami ide
            # irányít vissza — a visszajelzését így a query stringből kapjuk.
            track_msgs, track_errs = track_feedback(request.args)
            messages += track_msgs
            errors += track_errs

        values = db.get_settings(conn)
        # A státusszal együtt: a nem aktív műholdakhoz nem tölt le átvonulást
        # a frissítés, ezt a lista is jelzi.
        tracked = db.get_tracked_status(conn)
        # A számláló csak a beállított pozícióra vonatkozzon, különben a
        # korábbi helyre letöltött, ugyanazokat az eseményeket leíró sorok
        # felduzzasztanák.
        observer = (values["lat"], values["lon"], values["alt"])
        counts = {
            sat["norad_id"]: conn.execute(
                "SELECT COUNT(*) FROM passes WHERE norad_id = ? "
                "AND observer_lat = ? AND observer_lon = ?",
                (sat["norad_id"], values["lat"], values["lon"])
            ).fetchone()[0] for sat in tracked
        }
        stale = db.stale_positions(conn, observer)
    finally:
        conn.close()

    # Hibás beküldés után a beírt értékeket mutatjuk vissza, ne kelljen újra
    # begépelni őket.
    if submitted:
        values = {key: submitted.get(key, values[key]) for key in values}
    else:
        values = {key: format_number(v) for key, v in values.items()}

    return render_template(
        "settings.html", values=values, tracked=tracked, counts=counts,
        messages=messages, errors=errors, stale=stale,
    )


@app.route("/settings/cleanup", methods=["POST"])
def cleanup_positions():
    """Más pozícióra mentett, duplikált átvonulások törlése."""
    conn = db.connect(app.config["DB_PATH"])
    try:
        config = db.get_settings(conn)
        db.delete_stale_positions(
            conn, (config["lat"], config["lon"], config["alt"]))
    finally:
        conn.close()
    return redirect(url_for("settings"), code=303)


@app.route("/settings/track", methods=["POST"])
def track_satellite():
    """Felvétel a követett listára, egyszerre több NORAD ID-vel is.

    A Műholdak lap egyetlen azonosítót küld a norad_id mezőben, a Beállítások
    szabad szöveges mezője pedig többet a norad_ids-ben — mindkettőt itt
    fogadjuk, hogy egy helyen legyen az ellenőrzés.
    """
    ids, invalid = parse_norad_ids(" ".join(
        request.form.getlist("norad_ids") + request.form.getlist("norad_id")))
    added = []
    if ids:
        conn = db.connect(app.config["DB_PATH"])
        try:
            added = db.track_many(conn, ids)
        finally:
            conn.close()

    # A frissen felvett műholdhoz rögtön lehúzzuk a pályaelemet és az
    # átvonulásokat, különben a következő teljes frissítésig üresen állna a
    # listákon. A kapcsolatot előbb lezártuk: a háttérszál sajátot nyit.
    fetch = ""
    if added:
        started, _ = refresher.start_for(added)
        fetch = "started" if started else "busy"

    target = request.form.get("next") or url_for("settings")
    # A Műholdak lapon a sor maga jelzi a felvételt ("követve"), ezért csak a
    # Beállítások lapra fűzzük hozzá a szöveges visszajelzést. Session nélkül
    # a query string az egyetlen módja, hogy az átirányítás után is megmaradjon.
    if urlparse(target).path != url_for("settings"):
        return redirect(target, code=303)
    return redirect_with(
        target,
        added=",".join(str(n) for n in added),
        kept=",".join(str(n) for n in ids if n not in added),
        invalid=" ".join(invalid),
        fetch=fetch,
    )


@app.route("/settings/untrack", methods=["POST"])
def untrack_satellite():
    """Levétel a követett listáról. A már letöltött átvonulások megmaradnak."""
    norad_id = request.form.get("norad_id", type=int)
    if norad_id:
        conn = db.connect(app.config["DB_PATH"])
        try:
            db.untrack(conn, norad_id)
        finally:
            conn.close()
    return redirect(request.form.get("next") or url_for("settings"), code=303)


@app.context_processor
def inject_refresh():
    """A frissítősáv minden oldalon megjelenik, ezért közös kontextus."""
    return {
        "refresh_sources": refresh_module.SOURCES,
        "refresh_state": refresher.status(),
        # A script_root KELL elé: a full_path a SCRIPT_NAME nélküli útvonalat
        # adja, így reverse proxy mögött (ha1mp.hu/sats) csak "/" lenne belőle.
        # Ez a "next" mezőbe kerül, és a POST-ok ide irányítanak vissza — prefix
        # nélkül a /sats-on kívülre, a landing oldalra dobná a böngészőt.
        # Proxy nélkül a script_root üres, tehát lokálisan változatlan.
        "current_path": request.script_root + request.full_path.rstrip("?"),
    }


@app.route("/refresh", methods=["POST"])
def start_refresh():
    """Frissítés indítása; a munka háttérszálon fut."""
    keys = request.form.getlist("source") or refresh_module.SOURCE_KEYS
    started, message = refresher.start(keys)

    if request.headers.get("Accept") == "application/json":
        return jsonify({"started": started, "message": message,
                        **refresher.status()})
    # A böngészőt visszaküldjük oda, ahonnan jött, hogy a frissítés után
    # ne az űrlap újraküldése történjen.
    return redirect(request.form.get("next") or url_for("index"), code=303)


@app.route("/refresh/status")
def refresh_status():
    status = refresher.status()
    labels = dict(refresh_module.SOURCES)
    status["done_labels"] = [labels[k] for k in status["done"]]
    status["total"] = len(status["sources"])
    return jsonify(status)


app.jinja_env.filters["humanize"] = humanize
app.jinja_env.filters["humanize_age"] = humanize_age
app.jinja_env.filters["day_label"] = format_day
# "RU,US" -> [{code, flag, name}]; a sablon ebbol rajzolja a zaszlokat.
app.jinja_env.filters["flags"] = countries.badges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app.config["DB_PATH"] = args.db
    refresher.db_path = args.db
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
