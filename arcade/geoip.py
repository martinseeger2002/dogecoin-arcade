"""Which country an IP address was allocated to, and a point to draw it.

The tables are the public RIR allocation files (delegated-*-extended), packed.
A point is that country's centre, shifted a little by the address so two nodes
in one country do not sit on the same pixel. It is a country, not a street.
"""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import math
import struct
from pathlib import Path

#: Geographic centres, enough to draw a node in the country it was allocated to.
CENTROIDS: dict[str, tuple[float, float, str]] = {
    "AD": (42.5, 1.5, "Andorra"), "AE": (24.0, 54.0, "United Arab Emirates"),
    "AF": (33.9, 67.7, "Afghanistan"), "AL": (41.2, 20.2, "Albania"),
    "AM": (40.1, 45.0, "Armenia"), "AO": (-11.2, 17.9, "Angola"),
    "AR": (-38.4, -63.6, "Argentina"), "AT": (47.5, 14.1, "Austria"),
    "AU": (-25.3, 133.8, "Australia"), "AZ": (40.1, 47.6, "Azerbaijan"),
    "BA": (43.9, 17.7, "Bosnia and Herzegovina"), "BD": (23.7, 90.4, "Bangladesh"),
    "BE": (50.5, 4.5, "Belgium"), "BF": (12.2, -1.6, "Burkina Faso"),
    "BG": (42.7, 25.5, "Bulgaria"), "BH": (26.0, 50.5, "Bahrain"),
    "BI": (-3.4, 29.9, "Burundi"), "BJ": (9.3, 2.3, "Benin"),
    "BN": (4.5, 114.7, "Brunei"), "BO": (-16.3, -63.6, "Bolivia"),
    "BR": (-10.8, -53.1, "Brazil"), "BS": (25.0, -77.4, "Bahamas"),
    "BT": (27.5, 90.4, "Bhutan"), "BW": (-22.3, 24.7, "Botswana"),
    "BY": (53.7, 28.0, "Belarus"), "BZ": (17.2, -88.5, "Belize"),
    "CA": (56.1, -106.3, "Canada"), "CD": (-2.9, 23.7, "DR Congo"),
    "CF": (6.6, 20.9, "Central African Republic"), "CG": (-0.2, 15.8, "Congo"),
    "CH": (46.8, 8.2, "Switzerland"), "CI": (7.5, -5.5, "Côte d'Ivoire"),
    "CL": (-35.7, -71.5, "Chile"), "CM": (7.4, 12.4, "Cameroon"),
    "CN": (35.9, 104.2, "China"), "CO": (4.6, -74.3, "Colombia"),
    "CR": (9.7, -83.8, "Costa Rica"), "CU": (21.5, -78.0, "Cuba"),
    "CV": (16.0, -24.0, "Cabo Verde"), "CY": (35.1, 33.4, "Cyprus"),
    "CZ": (49.8, 15.5, "Czechia"), "DE": (51.2, 10.4, "Germany"),
    "DJ": (11.8, 42.6, "Djibouti"), "DK": (56.3, 9.5, "Denmark"),
    "DO": (18.7, -70.2, "Dominican Republic"), "DZ": (28.0, 1.7, "Algeria"),
    "EC": (-1.8, -78.2, "Ecuador"), "EE": (58.6, 25.0, "Estonia"),
    "EG": (26.8, 30.8, "Egypt"), "ER": (15.2, 39.8, "Eritrea"),
    "ES": (40.5, -3.7, "Spain"), "ET": (9.1, 40.5, "Ethiopia"),
    "FI": (61.9, 25.7, "Finland"), "FJ": (-17.7, 178.1, "Fiji"),
    "FR": (46.2, 2.2, "France"), "GA": (-0.8, 11.6, "Gabon"),
    "GB": (54.0, -2.5, "United Kingdom"), "GE": (42.3, 43.4, "Georgia"),
    "GH": (7.9, -1.0, "Ghana"), "GM": (13.4, -15.3, "Gambia"),
    "GN": (9.9, -9.7, "Guinea"), "GQ": (1.7, 10.3, "Equatorial Guinea"),
    "GR": (39.1, 21.8, "Greece"), "GT": (15.8, -90.2, "Guatemala"),
    "GW": (12.0, -15.2, "Guinea-Bissau"), "GY": (4.9, -58.9, "Guyana"),
    "HK": (22.3, 114.2, "Hong Kong"), "HN": (15.2, -86.2, "Honduras"),
    "HR": (45.1, 15.2, "Croatia"), "HT": (18.97, -72.3, "Haiti"),
    "HU": (47.2, 19.5, "Hungary"), "ID": (-2.5, 118.0, "Indonesia"),
    "IE": (53.4, -8.2, "Ireland"), "IL": (31.0, 35.0, "Israel"),
    "IN": (22.0, 79.0, "India"), "IQ": (33.2, 43.7, "Iraq"),
    "IR": (32.4, 53.7, "Iran"), "IS": (64.96, -19.0, "Iceland"),
    "IT": (41.9, 12.6, "Italy"), "JM": (18.1, -77.3, "Jamaica"),
    "JO": (30.6, 36.2, "Jordan"), "JP": (36.2, 138.3, "Japan"),
    "KE": (0.0, 37.9, "Kenya"), "KG": (41.2, 74.8, "Kyrgyzstan"),
    "KH": (12.6, 105.0, "Cambodia"), "KR": (35.9, 127.8, "South Korea"),
    "KW": (29.3, 47.5, "Kuwait"), "KZ": (48.0, 68.0, "Kazakhstan"),
    "LA": (19.9, 102.5, "Laos"), "LB": (33.9, 35.9, "Lebanon"),
    "LK": (7.9, 80.8, "Sri Lanka"), "LR": (6.4, -9.4, "Liberia"),
    "LS": (-29.6, 28.2, "Lesotho"), "LT": (55.2, 23.9, "Lithuania"),
    "LU": (49.8, 6.1, "Luxembourg"), "LV": (56.9, 24.6, "Latvia"),
    "LY": (26.3, 17.2, "Libya"), "MA": (31.8, -7.1, "Morocco"),
    "MD": (47.4, 28.4, "Moldova"), "ME": (42.7, 19.4, "Montenegro"),
    "MG": (-18.8, 46.9, "Madagascar"), "MK": (41.6, 21.7, "North Macedonia"),
    "ML": (17.6, -4.0, "Mali"), "MM": (21.9, 95.9, "Myanmar"),
    "MN": (46.9, 103.8, "Mongolia"), "MO": (22.2, 113.5, "Macao"),
    "MR": (21.0, -10.9, "Mauritania"), "MT": (35.9, 14.4, "Malta"),
    "MU": (-20.3, 57.6, "Mauritius"), "MV": (3.2, 73.2, "Maldives"),
    "MW": (-13.3, 34.3, "Malawi"), "MX": (23.6, -102.5, "Mexico"),
    "MY": (4.2, 101.9, "Malaysia"), "MZ": (-18.7, 35.5, "Mozambique"),
    "NA": (-22.96, 18.5, "Namibia"), "NE": (17.6, 8.1, "Niger"),
    "NG": (9.1, 8.7, "Nigeria"), "NI": (12.9, -85.2, "Nicaragua"),
    "NL": (52.1, 5.3, "Netherlands"), "NO": (60.5, 8.5, "Norway"),
    "NP": (28.4, 84.1, "Nepal"), "NZ": (-40.9, 174.9, "New Zealand"),
    "OM": (21.5, 55.9, "Oman"), "PA": (8.5, -80.8, "Panama"),
    "PE": (-9.2, -75.0, "Peru"), "PG": (-6.3, 143.96, "Papua New Guinea"),
    "PH": (12.9, 121.8, "Philippines"), "PK": (30.4, 69.3, "Pakistan"),
    "PL": (51.9, 19.1, "Poland"), "PR": (18.2, -66.6, "Puerto Rico"),
    "PS": (31.95, 35.2, "Palestine"), "PT": (39.4, -8.2, "Portugal"),
    "PY": (-23.4, -58.4, "Paraguay"), "QA": (25.4, 51.2, "Qatar"),
    "RO": (45.9, 24.97, "Romania"), "RS": (44.0, 21.0, "Serbia"),
    "RU": (61.5, 105.3, "Russia"), "RW": (-1.9, 29.9, "Rwanda"),
    "SA": (23.9, 45.1, "Saudi Arabia"), "SD": (12.9, 30.2, "Sudan"),
    "SE": (60.1, 18.6, "Sweden"), "SG": (1.35, 103.8, "Singapore"),
    "SI": (46.2, 14.99, "Slovenia"), "SK": (48.7, 19.7, "Slovakia"),
    "SL": (8.5, -11.8, "Sierra Leone"), "SN": (14.5, -14.5, "Senegal"),
    "SO": (5.2, 46.2, "Somalia"), "SR": (3.9, -56.0, "Suriname"),
    "SS": (6.9, 31.3, "South Sudan"), "SV": (13.8, -88.9, "El Salvador"),
    "SY": (34.8, 39.0, "Syria"), "SZ": (-26.5, 31.5, "Eswatini"),
    "TD": (15.5, 18.7, "Chad"), "TG": (8.6, 0.8, "Togo"),
    "TH": (15.9, 100.99, "Thailand"), "TJ": (38.9, 71.3, "Tajikistan"),
    "TL": (-8.9, 125.7, "Timor-Leste"), "TM": (38.97, 59.6, "Turkmenistan"),
    "TN": (33.9, 9.5, "Tunisia"), "TR": (39.0, 35.2, "Türkiye"),
    "TT": (10.7, -61.2, "Trinidad and Tobago"), "TW": (23.7, 121.0, "Taiwan"),
    "TZ": (-6.4, 34.9, "Tanzania"), "UA": (48.4, 31.2, "Ukraine"),
    "UG": (1.4, 32.3, "Uganda"), "US": (39.8, -98.6, "United States"),
    "UY": (-32.5, -55.8, "Uruguay"), "UZ": (41.4, 64.6, "Uzbekistan"),
    "VE": (6.4, -66.6, "Venezuela"), "VN": (14.1, 108.3, "Vietnam"),
    "YE": (15.6, 48.5, "Yemen"), "ZA": (-30.6, 22.9, "South Africa"),
    "ZM": (-13.1, 27.8, "Zambia"), "ZW": (-19.0, 29.2, "Zimbabwe"),
}

_V4: list[tuple[int, int, str]] | None = None
_V6: dict[int, list[tuple[int, int, str]]] | None = None


def _load() -> None:
    global _V4, _V6
    if _V4 is not None:
        return
    data = Path(__file__).resolve().parent / "data"
    raw = gzip.decompress((data / "ip4.cc.gz").read_bytes())
    v4 = []
    for i in range(0, len(raw) - 9, 10):
        start, end = struct.unpack_from(">II", raw, i)
        v4.append((start, end, raw[i + 8:i + 10].decode()))
    raw6 = gzip.decompress((data / "ip6.cc.gz").read_bytes())
    v6: dict[int, list[tuple[int, int, str]]] = {}
    for i in range(0, len(raw6) - 18, 19):
        start, end, prefix = struct.unpack_from(">QQB", raw6, i)
        v6.setdefault(prefix, []).append((start, end, raw6[i + 17:i + 19].decode()))
    for rows in v6.values():
        rows.sort()
    _V4, _V6 = v4, v6


def _find(rows: list[tuple[int, int, str]], value: int) -> str:
    lo, hi = 0, len(rows) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, cc = rows[mid]
        if value < start:
            hi = mid - 1
        elif value > end:
            lo = mid + 1
        else:
            return cc
    return ""


def public_ip(host: str) -> str:
    """A global IP, or "" for a name, a private address, or something unreadable."""
    text = (host or "").strip().strip("[]")
    if text.lower().startswith("::ffff:"):
        text = text.split(":")[-1]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return ""
    if not ip.is_global:
        return ""
    return ip.compressed if ip.version == 6 else str(ip)


def country(host: str) -> str:
    """The two-letter country an address was allocated to, or ""."""
    text = public_ip(host)
    if not text:
        return ""
    _load()
    ip = ipaddress.ip_address(text)
    if ip.version == 4:
        assert _V4 is not None
        return _find(_V4, int(ip))
    assert _V6 is not None
    top = int(ip) >> 64
    for prefix in sorted(_V6, reverse=True):
        found = _find(_V6[prefix], top)
        if found:
            return found
    return ""


def place(host: str, salt: str = "") -> dict | None:
    """Where to draw `host`: country, name, and a lat/lon near that country's centre.

    None when the address is private or not in the tables. A known country with no
    centre still comes back, with lat and lon left empty, so the list can name it.
    """
    text = public_ip(host)
    cc = country(text) if text else ""
    if not cc:
        return None
    centre = CENTROIDS.get(cc)
    if centre is None:
        return {"country": cc, "name": cc, "lat": None, "lon": None}
    lat, lon, name = centre
    digest = hashlib.sha256(f"{salt}|{text}".encode()).digest()
    angle = int.from_bytes(digest[:2], "big") / 65535 * 2 * math.pi
    radius = 0.35 + int.from_bytes(digest[2:4], "big") / 65535 * 3.2
    lat2 = max(-55.0, min(72.0, lat + radius * math.sin(angle)))
    lon2 = lon + radius * math.cos(angle)
    if lon2 > 180:
        lon2 -= 360
    if lon2 < -180:
        lon2 += 360
    return {"country": cc, "name": name, "lat": round(lat2, 2), "lon": round(lon2, 2)}
