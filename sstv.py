"""ARISS SSTV események letöltése az ariss.org oldalról.

Az oldal szabad szövegű bejelentésekből áll, nem táblázatból, és a
megfogalmazás bejegyzésenként változik ("The SSTV event begins Friday
May 8 10:30 UTC", "Start:  April 10", "start at 00:01 UTC December 5"...),
ezért kulcsszó + dátumminta alapú kinyerés kell. Amit nem sikerül
kiolvasni, az None marad — a bejegyzés a nyers szövegével együtt így is
bekerül az adatbázisba.
"""

import argparse
import calendar
import html as html_mod
import re
from datetime import datetime, timedelta, timezone

import requests

import db

URL = "https://www.ariss.org/upcoming-sstv-events.html"
USER_AGENT = "Mozilla/5.0 (compatible; n2yo-sstv/1.0)"

ISS_NORAD_ID = 25544

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

# Egy bejegyzés fejléce: 2026-05-06 ... vagy 12-04-2025 ...
HEADER_RE = re.compile(
    r"^\s*(?P<date>\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})\s+(?P<title>\S.*?)\s*:?\s*$"
)

# "May 8", "Nov.12, 2025", "April 10", "December 5" — a pont után nem mindig
# van szóköz ("Nov.12"), ezért az elválasztó opcionális.
DATE_RE = re.compile(
    rf"\b(?P<month>{MONTH_RE})\.?[\s,]*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>20\d{2}))?",
    re.IGNORECASE,
)
# "10:30", illetve a "1700 GMT" alak, amit néhány bejegyzés használ.
TIME_RE = re.compile(r"\b(?P<h>[0-2]?\d)[:.](?P<m>[0-5]\d)\b")
TIME_COMPACT_RE = re.compile(r"\b(?P<h>[0-2]\d)(?P<m>[0-5]\d)\s*(?:UTC|GMT|Z)\b",
                             re.IGNORECASE)

FREQ_RE = re.compile(r"(\d{3}\.\d{1,3})\s*MHz", re.IGNORECASE)
NORAD_RE = re.compile(r"NORAD\s*#?\s*(\d{4,6})", re.IGNORECASE)

# Az SSTV módok zárt halmaz — így nem kell a mondatszerkezetre hagyatkozni.
MODE_RE = re.compile(
    r"\b(Robot\s*(?:24|36|72)|PD\s*(?:50|90|120|160|180|240|290)|"
    r"Martin\s*[12]|Scottie\s*(?:DX|[12])|MP\s*\d{2,3}|SC2\s*\d{2,3})\b",
    re.IGNORECASE,
)

START_WORDS = re.compile(
    r"\b(begins?|beginning|starts?|starting|commences?|scheduled to start|"
    r"start time|switched on|turned on|activation)\b", re.IGNORECASE)
END_WORDS = re.compile(
    r"\b(concludes?|conclusion|ends?|ending|finish(?:es)?|terminates?|"
    r"end time|switched off|turned off|deactivat)\b", re.IGNORECASE)


def fetch_html(url=URL, timeout=30):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def content_html(raw):
    """A bejegyzéseket tartalmazó rész kivágása (menü és lábléc nélkül)."""
    start = raw.find('id="wsite-content"')
    if start == -1:
        return raw
    end = raw.find('id="footer-wrap"', start)
    return raw[start:end if end != -1 else len(raw)]


# A hivatkozásokat a tagek eltávolítása előtt jelöljük meg, hogy a bejegyzés
# szövegével együtt maradjanak — így tudjuk, melyik linkelt a melyik hírhez.
LINK_MARK = "\x02"
LINK_RE = re.compile(f"{LINK_MARK}(.*?){LINK_MARK}")


def clean_text(fragment, keep_links=False):
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", fragment)
    if keep_links:
        text = re.sub(r'(?is)<a\s[^>]*href="([^"]+)"[^>]*>',
                      f" {LINK_MARK}\\1{LINK_MARK} ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text).replace("\xa0", " ").replace("–", "-")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if ln]


def split_links(line):
    """A sorból kiszedi a beágyazott hivatkozásokat: (tiszta szöveg, urlek)."""
    urls = [u for u in LINK_RE.findall(line) if u.startswith("http")]
    return re.sub(r"\s+", " ", LINK_RE.sub("", line)).strip(), urls


def normalize_header(line):
    return re.sub(r"\s+", " ", line).strip().rstrip(":").strip()


def bold_lines(fragment):
    """A félkövér szövegrészek halmaza.

    A bejegyzések fejléce félkövér, a törzsszövegben felsorolt menetrend-sorok
    ("10-03-2025 1140 UTC start") viszont nem — enélkül azokat is önálló
    bejegyzésnek néznénk.
    """
    bold = set()
    # A tagnév után > vagy szóköz kell, különben a <br /> is "b" tagnak számít.
    pattern = r"(?is)<(?:strong|b|h[1-3])(?:\s[^>]*)?>(.*?)</(?:strong|b|h[1-3])\s*>"
    for chunk in re.findall(pattern, fragment):
        for line in clean_text(chunk):
            bold.add(normalize_header(line))
    return bold


def split_blocks(lines, bold):
    """Bejegyzésekre bontás a félkövér fejlécek mentén.

    A dátum nélküli fejléc (pl. "ARISS ANNOUNCES SSTV EVENT TO BEGIN JULY 14,
    2025") az előző bejegyzés dátumát örökli — az oldalon ezek a korábbi
    hírek, amikhez nincs külön kiírt közzétételi dátum.
    """
    blocks = []
    last_posted = None

    for raw in lines:
        line, urls = split_links(raw)
        if not line:
            continue
        key = normalize_header(line)
        if key not in bold:
            if blocks:
                blocks[-1]["lines"].append(line)
                blocks[-1]["urls"].extend(urls)
            continue

        header = HEADER_RE.match(line)
        if header:
            posted = header.group("date")
            if posted[2] == "-":  # MM-DD-YYYY
                mm, dd, yyyy = posted.split("-")
                posted = f"{yyyy}-{mm}-{dd}"
            last_posted = posted
            title = header.group("title").strip()
        elif last_posted:
            posted, title = last_posted, key
        else:
            continue  # fejléc a legelső bejegyzés előtt (pl. gallery-link)

        blocks.append({"posted_date": posted, "title": title,
                       "lines": [], "urls": list(urls)})

    return blocks


def resolve_year(month, day, posted_date, explicit_year=None):
    """Hiányzó évszám pótlása a bejelentés dátumából.

    A bejegyzések ritkán írnak évet, mert a közeljövőről szólnak. Ha az így
    kapott dátum több mint fél évvel a bejelentés előtt lenne, egy évvel
    későbbre esik (december végi bejelentés januári eseményről).
    """
    if explicit_year:
        return int(explicit_year)

    posted = datetime.strptime(posted_date, "%Y-%m-%d")
    year = posted.year
    if month < posted.month - 6:
        year += 1
    elif month > posted.month + 6:
        year -= 1

    # Február 29. rossz évvel: essünk vissza 28-ára a hiba helyett.
    if month == 2 and day == 29 and not calendar.isleap(year):
        day = 28
    return year


def parse_datetime(text, posted_date):
    """Dátum + (ha van) időpont kinyerése egy sorból, UTC unix time-ként."""
    date_m = DATE_RE.search(text)
    if not date_m:
        return None

    month = MONTHS[date_m.group("month").lower()]
    day = int(date_m.group("day"))
    year = resolve_year(month, day, posted_date, date_m.group("year"))

    if day > calendar.monthrange(year, month)[1]:
        return None

    time_m = TIME_RE.search(text) or TIME_COMPACT_RE.search(text)
    hour = minute = 0
    if time_m:
        hour, minute = int(time_m.group("h")), int(time_m.group("m"))
        if hour > 23:
            hour = minute = 0

    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int(dt.timestamp())


def keyword_segments(line):
    """(kind, szövegrész) párok: minden kulcsszó után a következő kulcsszóig.

    Egy sor tartalmazhat kezdést és véget is ("...start at 00:01 UTC December
    5. The campaign will conclude at 23:59 UTC December 13."), ezért a dátumot
    mindig a kulcsszó UTÁNI szakaszban keressük.
    """
    hits = []
    for kind, pattern in (("start", START_WORDS), ("end", END_WORDS)):
        for m in pattern.finditer(line):
            hits.append((m.start(), m.end(), kind))
    hits.sort()

    segments = []
    for i, (_begin, end, kind) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(line)
        segments.append((kind, line[end:stop]))
    return segments


def parse_block(block, source_url=URL):
    """Egy bejegyzésből strukturált esemény."""
    body = "\n".join(block["lines"])
    full = f"{block['title']}\n{body}"

    event = {
        "posted_date": block["posted_date"],
        "title": block["title"],
        "norad_id": None,
        "start_utc": None,
        "end_utc": None,
        "frequency_mhz": None,
        "mode": None,
        "source_text": full.strip(),
        "source_url": source_url,
        # Az ARISS gyakran belinkeli a részletes doppler-táblázatot vagy a
        # sajtóközleményt; ha van ilyen, az többet mond, mint a gyűjtőoldal.
        "info_url": next(iter(block.get("urls") or []), None),
    }

    # Egy kampányról több sor is szólhat (szünetek, újraindítás), ezért a
    # legkorábbi kezdés és a legkésőbbi vég adja az esemény időablakát.
    starts, ends = [], []
    for line in [block["title"]] + block["lines"]:
        for kind, segment in keyword_segments(line):
            ts = parse_datetime(segment, block["posted_date"])
            if ts is not None:
                (starts if kind == "start" else ends).append(ts)

    event["start_utc"] = min(starts) if starts else None
    event["end_utc"] = max(ends) if ends else None

    freq = FREQ_RE.search(full)
    if freq:
        event["frequency_mhz"] = float(freq.group(1))

    mode = MODE_RE.search(full)
    if mode:
        name = re.sub(r"\s+", " ", mode.group(1)).strip()
        # A betűszavas módok végig nagybetűsek (PD120), a többi szó (Robot 36).
        head = name.split()[0] if " " in name else re.match(r"[A-Za-z]+", name)[0]
        event["mode"] = (name.upper().replace(" ", "")
                         if head.upper() in ("PD", "MP", "SC2")
                         else name.title())

    norad = NORAD_RE.search(full)
    if norad:
        event["norad_id"] = int(norad.group(1))
    elif re.search(r"\bISS\b|Expedition|ARISS", full, re.IGNORECASE):
        event["norad_id"] = ISS_NORAD_ID

    # Elírt vagy félreolvasott sorrend: a vég nem lehet a kezdet előtt.
    if (event["start_utc"] and event["end_utc"]
            and event["end_utc"] < event["start_utc"]):
        event["end_utc"] = None

    return event


def parse_events(raw_html, source_url=URL):
    fragment = content_html(raw_html)
    blocks = split_blocks(clean_text(fragment, keep_links=True),
                          bold_lines(fragment))
    return [parse_block(b, source_url) for b in blocks]


def fmt(ts):
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--dry-run", action="store_true",
                        help="csak kiírja, nem ment")
    args = parser.parse_args()

    all_events = parse_events(fetch_html(args.url), source_url=args.url)
    # Dátum nélküli bejegyzés (általános hírek, félbehagyott menetrendek) nem
    # illeszthető átvonuláshoz, ezért nem is mentjük.
    events = [e for e in all_events if e["start_utc"] or e["end_utc"]]
    print(f"{len(all_events)} bejegyzés az ariss.org oldalról, "
          f"{len(events)} db kiolvasható dátummal")

    for e in events:
        details = []
        if e["frequency_mhz"]:
            details.append(f"{e['frequency_mhz']} MHz")
        if e["mode"]:
            details.append(e["mode"])
        if e["norad_id"]:
            details.append(f"NORAD {e['norad_id']}")
        print(f"  [{e['posted_date']}] {e['title'][:60]}")
        print(f"      {fmt(e['start_utc'])} -> {fmt(e['end_utc'])}"
              + (f" | {' | '.join(details)}" if details else ""))

    if args.dry_run:
        return

    conn = db.connect(args.db)
    try:
        inserted, updated = db.save_sstv_events(conn, events)
    finally:
        conn.close()
    print(f"Mentve: {inserted} új, {updated} frissítve")


if __name__ == "__main__":
    main()
