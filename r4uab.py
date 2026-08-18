"""SSTV-adások az R4UAB hírcsatornájából.

Az ariss.org csak az ISS-t fedi le. Ez a forrás az orosz amatőrműholdak
(UMKA-1, SAKHACUBE-CHOLBON, MONITOR-3, ARCTICSAT-1, VIZARD-METEO, QMR-KWT-2…)
SSTV-kampányait hozza, tehát nem átfedés, hanem kiegészítés.

A dedikált SSTV-kategória RSS-e a forrás, nem a nyers HTML: ott minden
bejegyzés SSTV-ről szól, és a szerkezet stabil (WordPress).

Amit a feed AD: dátumtartomány (a címben, orosz prózában), a műhold neve és
RS-jelzése, hivatkozás a bejegyzésre.
Amit NEM ad: pontos időpont, frekvencia, üzemmód. Ezek a mezők üresen
maradnak — a felület elviseli, csak kevesebb adat látszik, mint az ARISS-nél.
A frekvenciát és a módot azért megpróbáljuk kiolvasni a szövegtörzsből: ha egy
bejegyzés mégis tartalmazza, kár lenne eldobni.
"""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

import db

URL = "https://r4uab.ru/category/sstv/feed/"
USER_AGENT = "Mozilla/5.0 (compatible; n2yo-sstv/1.0)"

# Orosz hónapnevek birtokos alakban — a címekben így állnak ("28 июля").
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_MONTH_RE = "|".join(MONTHS)

# "28 июля — 01 августа 2026 года", "20 — 26 июля 2026 года": az első dátumnál
# elmaradhat a hónap, az évszám pedig csak a végén szerepel.
RANGE_RE = re.compile(
    rf"(?P<d1>\d{{1,2}})\s*(?:(?P<m1>{_MONTH_RE})\s*)?[—–-]\s*"
    rf"(?P<d2>\d{{1,2}})\s+(?P<m2>{_MONTH_RE})\s+(?P<y>\d{{4}})")

# "17 мая 2026 года" — egynapos esemény.
SINGLE_RE = re.compile(rf"(?P<d>\d{{1,2}})\s+(?P<m>{_MONTH_RE})\s+(?P<y>\d{{4}})")

# A műhold RS-jelzése a címben: «UMKA-1 (RS40S)». Ezen a kódon találjuk meg a
# katalógusban a NORAD azonosítót — a névre illesztés bizonytalanabb lenne.
DESIGNATOR_RE = re.compile(r"\bRS\d{2}S\b", re.IGNORECASE)

FREQ_RE = re.compile(r"(\d{2,3}[.,]\d{1,4})\s*(?:МГц|MHz)", re.IGNORECASE)
MODE_RE = re.compile(r"\b(PD\s*\d{2,3}|Robot\s*\d{2}|Martin\s*\d|Scottie\s*\d|MP\s*\d{2,3})\b",
                     re.IGNORECASE)

TAG_RE = re.compile(r"<[^>]+>")


def fetch(url=URL, timeout=30):
    response = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=timeout)
    response.raise_for_status()
    return response.content


def strip_html(text):
    """A leírás nyers szövege: címkék nélkül, egy sorba húzva."""
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", text or "")).strip()


def _day_bounds(year, month, day, end=False):
    """A nap kezdete, vagy a végénél a nap UTC szerinti utolsó másodperce.

    A feed csak dátumot ad, órát nem — a tartomány záró napja viszont egészben
    beletartozik az eseménybe, ezért nyúlik a nap végéig.
    """
    try:
        start = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:            # pl. 31 апреля
        return None
    if end:
        start += timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp())


def parse_period(title):
    """A címben szereplő dátumtartomány -> (start_utc, end_utc).

    Az évszám a tartomány VÉGÉHEZ tartozik; ha a kezdő hónap későbbi, mint a
    záró (december -> január), a kezdet az előző évre esik.
    """
    match = RANGE_RE.search(title)
    if match:
        month2 = MONTHS[match.group("m2")]
        month1 = MONTHS[match.group("m1")] if match.group("m1") else month2
        year2 = int(match.group("y"))
        year1 = year2 - 1 if month1 > month2 else year2
        return (_day_bounds(year1, month1, int(match.group("d1"))),
                _day_bounds(year2, month2, int(match.group("d2")), end=True))

    match = SINGLE_RE.search(title)
    if match:
        month, day, year = (MONTHS[match.group("m")], int(match.group("d")),
                            int(match.group("y")))
        return (_day_bounds(year, month, day),
                _day_bounds(year, month, day, end=True))

    return None, None


def parse_items(raw_xml):
    """Az RSS bejegyzései nyers alakban: cím, link, dátum, kategóriák, szöveg."""
    root = ElementTree.fromstring(raw_xml)
    items = []
    for item in root.findall("./channel/item"):
        published = item.findtext("pubDate")
        try:
            posted = parsedate_to_datetime(published).date().isoformat()
        except (TypeError, ValueError):
            posted = None
        items.append({
            "title": re.sub(r"\s+", " ", (item.findtext("title") or "")).strip(),
            "link": (item.findtext("link") or "").strip(),
            "posted_date": posted,
            "categories": [c.text for c in item.findall("category") if c.text],
            "text": strip_html(item.findtext("description")),
        })
    return items


def to_event(item, norad_by_designator, source_url=URL):
    """Egy RSS bejegyzés -> a save_sstv_events() által várt rekord."""
    title = item["title"]
    start, end = parse_period(title)

    event = {
        "posted_date": item["posted_date"],
        "title": title,
        "norad_id": None,
        "start_utc": start,
        "end_utc": end,
        "frequency_mhz": None,
        "mode": None,
        "source_text": item["text"][:1000] or None,
        "source_url": source_url,
        "info_url": item["link"] or None,
    }

    designator = DESIGNATOR_RE.search(title) or DESIGNATOR_RE.search(
        " ".join(item["categories"]))
    if designator:
        event["norad_id"] = norad_by_designator.get(designator.group(0).upper())

    full = f"{title} {item['text']}"
    freq = FREQ_RE.search(full)
    if freq:
        event["frequency_mhz"] = float(freq.group(1).replace(",", "."))
    mode = MODE_RE.search(full)
    if mode:
        event["mode"] = re.sub(r"\s+", "", mode.group(1)).upper()

    return event


def collect(conn, verbose=True):
    """Letöltés, feldolgozás, mentés. A gyűjtők közös alakja."""
    items = parse_items(fetch())

    # A jelzés -> NORAD megfeleltetést egyszer olvassuk be: a katalógus az
    # alt_names mezőben tárolja az RS-kódokat (pl. "RS40S, УмКА-1").
    designators = {d for item in items
                   for d in DESIGNATOR_RE.findall(
                       item["title"] + " " + " ".join(item["categories"]))}
    norad_by_designator = db.get_norad_by_designator(
        conn, [d.upper() for d in designators])

    events = [to_event(item, norad_by_designator) for item in items]
    # Dátum nélküli bejegyzés nem esemény: a felület időrendben rendez.
    events = [e for e in events if e["start_utc"] and e["posted_date"]]

    new, updated = db.save_sstv_events(conn, events)

    if verbose:
        unmatched = sorted({d for d in designators
                            if d.upper() not in norad_by_designator})
        print(f"R4UAB: {len(events)} SSTV esemény ({new} új, {updated} frissítve)"
              + (f"; ismeretlen jelzés: {', '.join(unmatched)}" if unmatched else ""))

    return {"events": len(events), "new": new, "updated": updated}


def main():
    conn = db.connect()
    try:
        collect(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
