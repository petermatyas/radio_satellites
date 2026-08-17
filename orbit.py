"""Pályaszámítás a tárolt TLE-kből: hol jár most a műhold, és merre látszik.

Az sgp4 könyvtár TEME vonatkoztatási rendszerben adja a helyvektort; a
"Föld melyik pontja felett" kérdéshez ezt a Földdel együtt forgó rendszerbe
kell forgatni (GMST szerint), majd geodetikus szélességre-hosszúságra
váltani. A megfigyelőhöz tartozó azimut és magasság ugyanebből a
helyvektorból jön, topocentrikus (SEZ) bontásban.

A pontosság így néhány kilométer / tized fok — a kis grafikákhoz bőven elég.
A pontos átvonulás-időket továbbra is az N2YO adja.
"""

import math
from datetime import datetime, timezone

from sgp4.api import SGP4_ERRORS, Satrec, jday

# WGS84 ellipszoid.
EARTH_RADIUS_KM = 6378.137
FLATTENING = 1 / 298.257223563
ECC_SQ = FLATTENING * (2 - FLATTENING)

SECONDS_PER_DAY = 86400


class OrbitError(Exception):
    """A pályaelem nem propagálható (elavult, hibás vagy lezuhant műhold)."""


def _satrec(row):
    return Satrec.twoline2rv(row["line1"], row["line2"])


def _jd(when):
    """datetime (UTC) -> az sgp4 által várt (jd, fr) pár."""
    when = when.astimezone(timezone.utc)
    return jday(when.year, when.month, when.day,
                when.hour, when.minute, when.second + when.microsecond / 1e6)


def gmst(jd, fr):
    """Greenwichi csillagidő radiánban (IAU 1982, Vallado nyomán)."""
    tut1 = (jd - 2451545.0 + fr) / 36525.0
    seconds = (67310.54841
               + (876600.0 * 3600 + 8640184.812866) * tut1
               + 0.093104 * tut1 * tut1
               - 6.2e-6 * tut1 * tut1 * tut1)
    return (math.radians(seconds / 240.0)) % (2 * math.pi)


def _teme_to_ecef(r, theta):
    """TEME -> Földdel együtt forgó derékszögű koordináták."""
    x, y, z = r
    return (x * math.cos(theta) + y * math.sin(theta),
            -x * math.sin(theta) + y * math.cos(theta),
            z)


def _geodetic(x, y, z):
    """ECEF (km) -> (szélesség°, hosszúság°, tengerszint feletti magasság km).

    Bowring iterációja: néhány kör alatt milliméteres pontosságot ad, és nem
    igényel külön könyvtárat.
    """
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - ECC_SQ))
    for _ in range(5):
        sin_lat = math.sin(lat)
        n = EARTH_RADIUS_KM / math.sqrt(1 - ECC_SQ * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - ECC_SQ * n / (n + alt)))
    sin_lat = math.sin(lat)
    n = EARTH_RADIUS_KM / math.sqrt(1 - ECC_SQ * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), (math.degrees(lon) + 180) % 360 - 180, alt


def _observer_ecef(lat_deg, lon_deg, alt_m):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    alt_km = (alt_m or 0) / 1000.0
    sin_lat = math.sin(lat)
    n = EARTH_RADIUS_KM / math.sqrt(1 - ECC_SQ * sin_lat * sin_lat)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon),
            (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1 - ECC_SQ) + alt_km) * sin_lat)


def _propagate(sat, when):
    """Helyvektor a Földdel együtt forgó rendszerben, km-ben."""
    jd, fr = _jd(when)
    error, r, _v = sat.sgp4(jd, fr)
    if error:
        raise OrbitError(SGP4_ERRORS.get(error, f"sgp4 hiba: {error}"))
    return _teme_to_ecef(r, gmst(jd, fr))


def position(row, when=None):
    """A műhold alatti pont és magassága az adott pillanatban.

    {"lat", "lon", "alt_km", "when"} — a lat/lon fok, az alt_km a
    tengerszint feletti magasság.
    """
    when = when or datetime.now(timezone.utc)
    lat, lon, alt = _geodetic(*_propagate(_satrec(row), when))
    return {"lat": lat, "lon": lon, "alt_km": alt, "when": when}


def look_angles(row, observer, when=None):
    """Azimut és magasság a megfigyelőtől nézve, fokban.

    observer: (szélesség°, hosszúság°, tengerszint feletti magasság m)
    """
    when = when or datetime.now(timezone.utc)
    sat = _propagate(_satrec(row), when)
    obs = _observer_ecef(*observer)
    dx, dy, dz = (sat[0] - obs[0], sat[1] - obs[1], sat[2] - obs[2])

    lat, lon = math.radians(observer[0]), math.radians(observer[1])
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    # SEZ: dél, kelet, zenit irányú összetevők.
    south = sin_lat * cos_lon * dx + sin_lat * sin_lon * dy - cos_lat * dz
    east = -sin_lon * dx + cos_lon * dy
    zenith = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    azimuth = (math.degrees(math.atan2(east, -south))) % 360
    elevation = math.degrees(math.asin(zenith / distance)) if distance else 90.0
    return {"az": azimuth, "el": elevation, "range_km": distance}


def ground_track(row, when=None, minutes_before=30, minutes_after=60,
                 step_minutes=2):
    """A műhold alatti pont útja egy időablakban, rajzoláshoz.

    Pontok listája (lat, lon) párokban, időrendben — a nulladik elem a
    minutes_before perccel korábbi hely.
    """
    when = when or datetime.now(timezone.utc)
    sat = _satrec(row)
    base = when.timestamp()
    points = []
    minute = -minutes_before
    while minute <= minutes_after:
        moment = datetime.fromtimestamp(base + minute * 60, timezone.utc)
        try:
            lat, lon, _alt = _geodetic(*_propagate(sat, moment))
        except OrbitError:
            break
        points.append((lat, lon))
        minute += step_minutes
    return points


def epoch_age_days(row, when=None):
    """Hány napos a pályaelem epochája — ennyire bízhatunk a számításban."""
    year = int(row["line1"][18:20])
    day = float(row["line1"][20:32])
    year += 2000 if year < 57 else 1900
    epoch = (datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
             + (day - 1) * SECONDS_PER_DAY)
    when = when or datetime.now(timezone.utc)
    return (when.timestamp() - epoch) / SECONDS_PER_DAY
