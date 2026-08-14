"""
nmea.py — minimal NMEA parsers for the overlay (RMC + GGA)
==========================================================

RMC: lat/lon, speed, course. GGA: HDOP / sat count for an honesty circle.
Talker agnostic ($GP… / $GN… / …). Checksum required.
"""

from __future__ import annotations

import math
import re

# $--RMC,time,status,lat,N/S,lon,E/W,sog_kn,cog_deg,date,...*CS
_RMC = re.compile(
    r"^\$[A-Z]{2}RMC,"
    r"([^,]*),"          # 1 time
    r"([AV]),"           # 2 status
    r"([^,]*),"          # 3 lat
    r"([NS]),"           # 4
    r"([^,]*),"          # 5 lon
    r"([EW]),"           # 6
    r"([^,]*),"          # 7 speed knots
    r"([^,]*),"          # 8 course deg
    r"([^,]*)"           # 9 date (rest ignored)
)

# $--GGA,time,lat,N/S,lon,E/W,quality,sats,hdop,alt,...*CS
_GGA = re.compile(
    r"^\$[A-Z]{2}GGA,"
    r"([^,]*),"          # 1 time
    r"([^,]*),"          # 2 lat
    r"([NS]),"           # 3
    r"([^,]*),"          # 4 lon
    r"([EW]),"           # 5
    r"([0-9]),"          # 6 quality
    r"([^,]*),"          # 7 sats
    r"([^,]*)"           # 8 hdop
)

KNOTS_TO_KMH = 1.852
# Rough consumer horizontal 1-sigma from HDOP (meters). Not a lab number —
# enough to draw an honesty circle so walking-scale wander looks expected.
HDOP_TO_METERS = 5.0


def nmea_checksum_ok(sentence: str) -> bool:
    """True if '*' CS is present and matches XOR of the body.

    The XGPS160 never omits the checksum (26,104 of 26,104 in the
    captures). A truncated sentence that lost its '*' but kept its shape
    used to parse unverified; requiring the star costs nothing real.
    """
    if "*" not in sentence:
        return False
    body, _, cs = sentence.strip().partition("*")
    if not body.startswith("$") or len(cs) < 2:
        return False
    try:
        want = int(cs[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body[1:]:
        got ^= ord(ch)
    return got == want


def _strip_to_sentence(sentence: str) -> str | None:
    s = sentence.strip()
    i = s.find("$")
    if i < 0:
        return None
    s = s[i:]
    if not nmea_checksum_ok(s):
        return None
    return s


def dm_to_degrees(dm: str, hemi: str) -> float | None:
    """NMEA ddmm.mmm / dddmm.mmm + hemisphere -> signed decimal degrees."""
    if not dm:
        return None
    try:
        val = float(dm)
    except ValueError:
        return None
    deg = int(val // 100)
    minutes = val - deg * 100
    if minutes >= 60.0:
        return None
    dec = deg + minutes / 60.0
    if hemi in ("S", "W"):
        dec = -dec
    elif hemi not in ("N", "E"):
        return None
    return dec


def parse_rmc(sentence: str) -> dict | None:
    """Parse one RMC sentence into a fix dict, or None if unusable.

    Returns keys: lat, lon, speed_kmh, course_deg (float|None), valid (bool).
    course_deg is None when the field is empty (common when stationary).
    """
    s = _strip_to_sentence(sentence)
    if not s:
        return None
    m = _RMC.match(s)
    if not m:
        return None
    status = m.group(2)
    lat = dm_to_degrees(m.group(3), m.group(4))
    lon = dm_to_degrees(m.group(5), m.group(6))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    try:
        sog = float(m.group(7)) if m.group(7) else 0.0
    except ValueError:
        sog = 0.0
    course = None
    if m.group(8):
        try:
            course = float(m.group(8)) % 360.0
        except ValueError:
            course = None
    return {
        "lat": lat,
        "lon": lon,
        "speed_kmh": sog * KNOTS_TO_KMH,
        "course_deg": course,
        "valid": status == "A",
    }


def parse_gga(sentence: str) -> dict | None:
    """Parse GGA for quality / sats / HDOP. Position comes from RMC for v1."""
    s = _strip_to_sentence(sentence)
    if not s:
        return None
    m = _GGA.match(s)
    if not m:
        return None
    try:
        quality = int(m.group(6))
    except ValueError:
        return None
    try:
        sats = int(m.group(7)) if m.group(7) else 0
    except ValueError:
        sats = 0
    try:
        hdop = float(m.group(8)) if m.group(8) else None
    except ValueError:
        hdop = None
    acc = None
    if hdop is not None and hdop > 0:
        acc = hdop * HDOP_TO_METERS
    return {
        "quality": quality,
        "sats": sats,
        "hdop": hdop,
        "accuracy_m": acc,
    }


def enu_meters(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """East/north meters of (lat,lon) relative to (lat0,lon0), equirectangular."""
    # Good enough for a few km of overlay; not a survey tool.
    north = (lat - lat0) * 111_320.0
    east = (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))
    return east, north
