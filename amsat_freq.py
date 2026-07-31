"""Az amsat.org üzemi frekvenciatábláinak letöltése.

A státusz-API csak azt mondja meg, hallották-e a műholdat; a tényleges
üzemi adatok — fel- és lemenő frekvencia, CTCSS hang, invertáló-e a
transzponder — négy külön HTML-oldalon vannak, táblázatos formában.
Ezek azok az oldalak, amikre a státuszoldalon a műhold nevére kattintva
jut az ember.
"""

import argparse
import html as html_mod
import re
import sys

import requests

import db

USER_AGENT = "Mozilla/5.0 (compatible; n2yo-collector/1.0)"

# Oldalanként más az oszlopkiosztás, ezért mezőnevekkel írjuk le.
PAGES = [
    {
        "category": "FM",
        "url": "https://www.amsat.org/live-fm-satellites/",
        "columns": ["name", "uplink", "downlink", "comment"],
    },
    {
        "category": "Linear",
        "url": "https://www.amsat.org/live-linear-satellites/",
        "columns": ["name", "frequencies", "comment"],
    },
    {
        "category": "Digipeater",
        "url": "https://www.amsat.org/live-digipeater-satellites/",
        "columns": ["name", "downlink", "via", "comment"],
    },
    {
        "category": "Image",
        "url": "https://www.amsat.org/image-transmitting-satellites/",
        "columns": ["name", "downlink", "comment"],
    },
]

CELL_RE = re.compile(r"(?is)<t([dh])\b[^>]*>(.*?)</t\1>")
ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
HREF_RE = re.compile(r'(?is)<a\s[^>]*href="([^"]+)"')

FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(MHz|GHz)", re.IGNORECASE)
CTCSS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Hz\s*CTCSS", re.IGNORECASE)
# "U/v Inverting Analog", "V/a Non-Inverting Analog", "C/x Non-inverting"
MODE_RE = re.compile(r"\b([A-Z]/[a-z])\b\s*(non-)?inverting", re.IGNORECASE)


def fetch(url, timeout=40):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def cell_text(fragment):
    """Cella szövege; a <br> sortörés marad, mert az választja el a mezőket."""
    text = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if ln]


def parse_rows(page_html):
    """A tábla adatsorai: cellánként (sorok listája, első link)."""
    rows = []
    for row_html in ROW_RE.findall(page_html):
        cells = CELL_RE.findall(row_html)
        if not cells or any(tag == "h" for tag, _ in cells):
            continue  # fejléc
        parsed = []
        for _tag, inner in cells:
            href = HREF_RE.search(inner)
            parsed.append({"lines": cell_text(inner),
                           "href": href.group(1) if href else None})
        rows.append(parsed)
    return rows


def name_candidates(sat_name):
    """Egyre rövidebb névváltozatok az illesztéshez.

    A táblákban a név mellé odakerül a régi jelölés vagy az üzemmód is
    ("AO-73 (FUNcube-1)", "AO-7 Mode A"), ezért fokozatosan csupaszítjuk.
    """
    yield sat_name
    without_paren = re.sub(r"\(.*?\)", " ", sat_name).strip()
    if without_paren != sat_name:
        yield without_paren
    first = without_paren.split()
    if first:
        yield first[0]


def resolve_norad(sat_name, index):
    for candidate in name_candidates(sat_name):
        norad = index.get(re.sub(r"[^A-Z0-9]", "", candidate.upper()))
        if norad:
            return norad
    return None


# "TEVEL2-1 thru TEVEL2-9" — egy sor, ami kilenc azonos felépítésű műholdra
# vonatkozik; ezeket külön rekordokra bontjuk.
RANGE_RE = re.compile(r"^(?P<prefix>.*?)(?P<from>\d+)\s+thru\s+(?P=prefix)(?P<to>\d+)$",
                      re.IGNORECASE)


def expand_range(sat_name):
    m = RANGE_RE.match(sat_name.strip())
    if not m:
        return [sat_name]
    start, end = int(m.group("from")), int(m.group("to"))
    if not 0 < end - start < 50:
        return [sat_name]
    return [f"{m.group('prefix')}{n}" for n in range(start, end + 1)]


def to_mhz(value, unit):
    return float(value) * (1000 if unit.lower() == "ghz" else 1)


def parse_freqs(text):
    """Frekvencia(tartomány) MHz-ben: (alsó, felső vagy None)."""
    found = [to_mhz(v, u) for v, u in FREQ_RE.findall(text)]
    if not found:
        return None, None
    if len(found) == 1:
        return found[0], None
    return found[0], found[1]


def split_linear(lines):
    """A linear oldal "Frequencies" cellája: uplink és downlink sorok."""
    up = down = ""
    for line in lines:
        if re.match(r"(?i)\s*uplink", line):
            up = line
        elif re.match(r"(?i)\s*downlink", line):
            down = line
    return up, down


def parse_page(page, index):
    """Egy oldal sorai -> mentésre kész rekordok."""
    rows = parse_rows(fetch(page["url"]))
    columns = page["columns"]
    out, unresolved = [], []

    for cells in rows:
        if len(cells) < len(columns):
            continue
        by_name = {col: cells[i] for i, col in enumerate(columns)}

        name_lines = by_name["name"]["lines"]
        if not name_lines:
            continue
        raw_name = name_lines[0].strip()

        entry = {
            "category": page["category"],
            "sat_label": " ".join(name_lines),
            "sat_name": raw_name,
            "norad_id": None,
            "mode": None,
            "uplink_mhz": None, "uplink_high_mhz": None, "ctcss_hz": None,
            "downlink_mhz": None, "downlink_high_mhz": None,
            "via": None,
            "comment": " ".join(by_name["comment"]["lines"]) or None,
            "detail_url": by_name["name"]["href"],
            "source_url": page["url"],
        }

        if "frequencies" in by_name:  # Linear: egy cellában van minden
            up, down = split_linear(by_name["frequencies"]["lines"])
            entry["uplink_mhz"], entry["uplink_high_mhz"] = parse_freqs(up)
            entry["downlink_mhz"], entry["downlink_high_mhz"] = parse_freqs(down)
            # A mód a névcella második sorában van: "U/v Inverting Analog"
            mode_text = " ".join(name_lines[1:])
            entry["mode"] = mode_text or None
        else:
            if "uplink" in by_name:
                text = " ".join(by_name["uplink"]["lines"])
                entry["uplink_mhz"], entry["uplink_high_mhz"] = parse_freqs(text)
                ctcss = CTCSS_RE.search(text)
                if ctcss:
                    entry["ctcss_hz"] = float(ctcss.group(1))
            if "downlink" in by_name:
                text = " ".join(by_name["downlink"]["lines"])
                entry["downlink_mhz"], entry["downlink_high_mhz"] = parse_freqs(text)
                # "(SSTV Robot36)", "(9k6 GFSK/G3RUH)" — a zárójeles rész a mód
                extra = re.search(r"\(([^)]+)\)", text)
                if extra and "CTCSS" not in extra.group(1).upper():
                    entry["mode"] = extra.group(1)
            if "via" in by_name:
                entry["via"] = " ".join(by_name["via"]["lines"]) or None

        # A digipeater egy frekvencián vesz és ad; a tábla ezt egy oszlopban
        # közli. Több frekvenciánál (crossband) nem tippelünk.
        if (page["category"] == "Digipeater" and entry["downlink_mhz"]
                and not entry["downlink_high_mhz"]):
            entry["uplink_mhz"] = entry["downlink_mhz"]

        # Egy sor több műholdra is vonatkozhat ("TEVEL2-1 thru TEVEL2-9").
        for name in expand_range(raw_name):
            row = dict(entry, sat_name=name, norad_id=resolve_norad(name, index))
            if name != raw_name:
                row["sat_label"] = f"{entry['sat_label']} [{name}]"
            if row["norad_id"] is None:
                unresolved.append(f"{page['category']}:{name}")
            out.append(row)

    return out, unresolved


COMPARED_FIELDS = ("uplink_mhz", "downlink_mhz", "ctcss_hz", "mode", "via")


def dedupe(entries):
    """Az azonos (kategória, név) sorok kezelése.

    Egy oldalon belül ugyanaz a műhold több szekcióban is szerepelhet (az
    Image oldalon az RS83S az SSTV-nél és az SSDV-nél is). Ha a sorok
    tartalma megegyezik, egyet tartunk meg; ha eltér, megkülönböztetjük,
    különben az adatbázis kulcsa némán eldobná az egyiket.
    """
    out, seen = [], {}
    for entry in entries:
        key = (entry["category"], entry["sat_label"])
        previous = seen.get(key)
        if previous is not None:
            if all(previous.get(f) == entry.get(f) for f in COMPARED_FIELDS):
                continue
            suffix = 2
            while (entry["category"], f"{entry['sat_label']} #{suffix}") in seen:
                suffix += 1
            entry["sat_label"] = f"{entry['sat_label']} #{suffix}"
            key = (entry["category"], entry["sat_label"])
        seen[key] = entry
        out.append(entry)
    return out


def collect(conn, verbose=True):
    index = db.name_index(conn)
    entries, unresolved = [], []
    for page in PAGES:
        rows, missing = parse_page(page, index)
        entries += rows
        unresolved += missing
        if verbose:
            print(f"  {page['category']:<11} {len(rows)} sor")

    entries = dedupe(entries)
    new, updated = db.save_amsat_frequencies(conn, entries)
    if verbose:
        print(f"AMSAT frekvenciatáblák: {len(entries)} sor "
              f"({new} új, {updated} frissítve)")
        if unresolved:
            print(f"  NORAD ID nélkül ({len(unresolved)}): "
                  f"{', '.join(unresolved)}")
    return {"frequencies": len(entries), "unresolved": unresolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect(args.db)
    try:
        if args.dry_run:
            index = db.name_index(conn)
            for page in PAGES:
                rows, _ = parse_page(page, index)
                print(f"=== {page['category']} ({len(rows)}) ===")
                for e in rows:
                    print(f"  {e['sat_name']:<22} NORAD {e['norad_id']}"
                          f"  up={e['uplink_mhz']} ctcss={e['ctcss_hz']}"
                          f"  down={e['downlink_mhz']}  mode={e['mode']}")
        else:
            collect(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
