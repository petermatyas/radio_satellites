"""Webes felület: a következő műhold-átvonulások és a hozzájuk tartozó
rádiós információk (frekvencia, üzemmód, bejelentett aktivitás)."""

import argparse
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

import amsat_freq
import db
import refresh as refresh_module
import sstv

app = Flask(__name__)
app.config["DB_PATH"] = db.DEFAULT_DB_PATH
refresher = refresh_module.Refresher(app.config["DB_PATH"])

# Ennyi ideig tekintünk egy AMSAT észlelést "friss"-nek.
ACTIVITY_WINDOW_HOURS = 72

AMSAT_STATUS_URL = "https://www.amsat.org/status/index.php"
SATNOGS_SAT_URL = "https://db.satnogs.org/satellite/{}/"


def satnogs_url(sat_id, norad_id):
    """A műhold SatNOGS DB oldala; sat_id hiányában a NORAD ID is működik."""
    return SATNOGS_SAT_URL.format(sat_id or norad_id)


def host_of(url):
    """Domain a link címkéjéhez: "ariss-usa.org/ARISS_SSTV/..." -> ariss-usa.org"""
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def sstv_module_url():
    return sstv.URL


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
        out.append({
            "activity": row["activity"] or row["display_name"],
            "latest": latest,
            "age_hours": int((datetime.now(timezone.utc)
                              - latest.replace(tzinfo=timezone.utc))
                             .total_seconds() // 3600),
            "reports": row["reports"],
        })
    return out


def overlapping(intervals, start, end, norad_id, norad_key="norad_id"):
    """Azok az intervallumok, amik átfedik az adott átvonulást."""
    return [row for row in intervals
            if row[norad_key] == norad_id
            and row["start_utc"] <= end and row["end_utc"] >= start]


def format_pass(row, now, extras):
    start, end = row["start_utc"], row["end_utc"]
    return {
        "norad_id": row["norad_id"],
        "sat_name": row["sat_name"] or f"NORAD {row['norad_id']}",
        "start": datetime.fromtimestamp(start),
        "end": datetime.fromtimestamp(end),
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


@app.route("/")
def index():
    now = int(time.time())
    norad_id = request.args.get("norad_id", type=int)
    since_iso = ((datetime.now(timezone.utc)
                  - timedelta(hours=ACTIVITY_WINDOW_HOURS))
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    conn = db.connect(app.config["DB_PATH"])
    try:
        # Csak a beállított pozícióra számolt átvonulások: a korábbi helyre
        # letöltöttek bent maradnak az adatbázisban, de nem keverednek ide.
        config = db.get_settings(conn)
        observer = (config["lat"], config["lon"])
        rows = db.get_passes(conn, norad_id=norad_id, since=now,
                             observer=observer)
        satellites = db.get_satellites(conn, observer=observer)
        # Néhány műholdra sok átvonulás jut, ezért a kiegészítő adatokat
        # egyszer olvassuk be, és memóriában párosítjuk.
        norads = {r["norad_id"] for r in rows}
        sat_ids = {r["norad_id"]: r["sat_id"] for r in
                   conn.execute("SELECT norad_id, sat_id FROM satellites")}
        transmitters = db.get_transmitter_map(conn, norads)
        frequencies = db.get_frequency_map(conn, norads)
        websites = db.get_amsat_websites(conn)
        activity = db.get_activity_map(conn, since_iso)
        sstv_events = db.get_sstv_intervals(conn, now)
        activations = db.get_activations(conn, now)
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
        passes.append(format_pass(row, now, {
            "sstv": format_sstv(sstv[0]) if sstv else None,
            "activations": [format_activation(a) for a in roves],
            "transmitters": [format_transmitter(t)
                             for t in transmitters.get(sat, [])],
            "frequencies": freqs,
            "activity": format_activity(activity.get(sat, [])),
            "satnogs_url": satnogs_url(sat_ids.get(sat), sat),
            "amsat_url": amsat_main,
            "amsat_extra": amsat_extra,
        }))

    # Napokra bontva, hogy a lista olvasható maradjon.
    days = []
    for p in passes:
        day = p["start"].date()
        if not days or days[-1]["date"] != day:
            days.append({"date": day, "passes": []})
        days[-1]["passes"].append(p)

    return render_template(
        "index.html",
        days=days,
        total=len(passes),
        sstv_count=sum(1 for p in passes if p["sstv"]),
        rove_count=sum(len(p["activations"]) for p in passes),
        satellites=satellites,
        selected=norad_id,
        observer=observer,
        now=datetime.fromtimestamp(now),
    )


@app.route("/events")
def events():
    """Várható rádiós események: SSTV kampányok és bejelentett aktivációk.

    Az átvonulás-listán csak az látszik, ami időben átfedi a helyi
    átvonulásainkat — egy tengerentúli rover ablaka viszont ritkán fedi át,
    ezért az események önmagukban is megjelennek itt.
    """
    now = int(time.time())

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
        links.append(("ariss.org", row["source_url"] or sstv_module_url()))
        items.append({
            "kind": "sstv",
            "start": datetime.fromtimestamp(row["start_utc"]),
            "end": datetime.fromtimestamp(row["end_utc"]) if row["end_utc"] else None,
            "starts_in": row["start_utc"] - now,
            "title": row["title"],
            "who": "ARISS",
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
    return render_template("events.html", items=items,
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
        sats.append({
            "norad_id": row["norad_id"],
            "name": row["name"],
            "alt_names": row["alt_names"],
            "countries": row["countries"],
            "launched": (row["launched"] or "")[:4],
            "website": row["website"],
            "tracked": row["norad_id"] in tracked,
            "satnogs_url": satnogs_url(row["sat_id"], row["norad_id"]),
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


REPORT_LABELS = {
    "Heard": ("hallotta", "ok"),
    "Crew Active": ("legénység adott", "ok"),
    "Telemetry Only": ("csak telemetria", "warn"),
    "Not Heard": ("nem hallotta", "bad"),
}


@app.route("/reports/<int:norad_id>")
def reports(norad_id):
    """Ki és mikor hallotta — az AMSAT bejelentések tételesen."""
    activity = request.args.get("activity") or None

    conn = db.connect(app.config["DB_PATH"])
    try:
        rows = db.get_reports(conn, norad_id, activity=activity)
        activities = db.get_report_activities(conn, norad_id)
        sat = conn.execute(
            "SELECT norad_id, name, sat_id FROM satellites WHERE norad_id = ?",
            (norad_id,)).fetchone()
        websites = db.get_amsat_websites(conn)
        freqs = [format_frequency(f)
                 for f in db.get_frequency_map(conn, [norad_id]).get(norad_id, [])]
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
        })

    amsat_main, _extra = amsat_links(freqs, websites.get(norad_id))
    return render_template(
        "reports.html",
        items=items,
        activities=activities,
        selected=activity,
        norad_id=norad_id,
        sat_name=(sat["name"] if sat else None) or f"NORAD {norad_id}",
        satnogs_url=satnogs_url(sat["sat_id"] if sat else None, norad_id),
        amsat_url=amsat_main,
        window_hours=ACTIVITY_WINDOW_HOURS,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    conn = db.connect(app.config["DB_PATH"])
    messages, errors = [], []
    submitted = None
    try:
        if request.method == "POST":
            # Előbb mindent ellenőrzünk, és csak hibátlan űrlapot mentünk: a
            # követett műholdak listája felülíródna, egy elgépelt szélesség
            # miatt nem veszhetnek el a többi mezőben megadott értékek.
            numbers, ids = {}, []
            for key, cast in db.SETTING_TYPES.items():
                raw = request.form.get(key)
                try:
                    numbers[key] = cast(raw)
                except (TypeError, ValueError):
                    errors.append(f"A(z) „{key}” mező értéke nem szám: {raw!r}")

            ids, invalid = parse_norad_ids(request.form.get("norad_ids"))
            if invalid:
                errors.append(f"Érvénytelen NORAD ID: {', '.join(invalid)}")
            elif not ids and db.get_tracked(conn):
                # Üresen hagyott mező többnyire véletlen; a szándékos ürítés
                # a soronkénti törlés gombbal megy.
                errors.append("A NORAD ID mező üres. Ha törölni szeretnél, "
                              "használd a lista melletti × gombot.")

            if errors:
                submitted = request.form
            else:
                db.save_settings(conn, numbers)
                db.set_tracked(conn, ids)
                messages.append(
                    f"Mentve: {numbers['lat']}, {numbers['lon']} "
                    f"({numbers['alt']:g} m), {numbers['days']} nap, "
                    f"{len(ids)} követett műhold.")

        values = db.get_settings(conn)
        tracked = db.get_tracked(conn)
        # A számláló csak a beállított pozícióra vonatkozzon, különben a
        # korábbi helyre letöltött, ugyanazokat az eseményeket leíró sorok
        # felduzzasztanák.
        observer = (values["lat"], values["lon"], values["alt"])
        counts = {
            norad: conn.execute(
                "SELECT COUNT(*) FROM passes WHERE norad_id = ? "
                "AND observer_lat = ? AND observer_lon = ?",
                (norad, values["lat"], values["lon"])
            ).fetchone()[0] for norad, _ in tracked
        }
        stale = db.stale_positions(conn, observer)
    finally:
        conn.close()

    # Hibás beküldés után a beírt értékeket mutatjuk vissza, ne kelljen újra
    # begépelni őket.
    if submitted:
        values = {key: submitted.get(key, values[key]) for key in values}
        norad_text = submitted.get("norad_ids", "")
    else:
        values = {key: format_number(v) for key, v in values.items()}
        norad_text = "\n".join(str(n) for n, _ in tracked)

    return render_template(
        "settings.html", values=values, tracked=tracked, counts=counts,
        messages=messages, errors=errors, norad_text=norad_text, stale=stale,
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
    """Gyors felvétel a műholdlistáról."""
    norad_id = request.form.get("norad_id", type=int)
    if norad_id:
        conn = db.connect(app.config["DB_PATH"])
        try:
            db.track(conn, norad_id)
        finally:
            conn.close()
    return redirect(request.form.get("next") or url_for("settings"), code=303)


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
        "current_path": request.full_path.rstrip("?"),
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
