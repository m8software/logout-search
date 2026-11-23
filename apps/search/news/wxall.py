"""Weather for any US town, kept on the box (2026-09-25).

weather.py answers for home from api.weather.gov. This keeps the WHOLE country instead,
from NOAA bulk files, so "weather in chicago" is a local table read like everything else:

  NBM station bulletins (National Blend of Models, NWS; public domain), from NOAA's open
  data bucket on AWS (noaa-nbm-grib2-pds): blend_nbhtx (25 hourly columns, every station,
  ~28 MB) every hour (a new run is published hourly; downloaded only when there is one), blend_nbetx (8 days in 12-h steps, ~21 MB) every 6 h.
  METARs (aviationweather.gov, one file for every station, ~250 KB): now, every 10 min.
  Alerts (api.weather.gov/alerts/active, the whole country in one request): every 5 min.
  Towns (Census Gazetteer places + county subdivisions, population estimates; USGS GNIS
  populated places for the unincorporated ones the Census does not list): weekly.
  Radar (only where WX_RADAR_DIR is set): each NWS radar's newest picture
  (radar.weather.gov/ridge/standard/<station>_0.gif, public domain, ~90 KB), asked every
  10 minutes with "only if newer"; the last 10 per station are kept as files and the page
  plays them as a loop from our own server.
  Airport status (FAA, US government work): every delay, ground stop and closure
  in the country in one small XML file (nasstatus.faa.gov, ~2 KB), every 5 min. The server
  sends no Last-Modified, so it is fetched each time and dated by its own Update_Time.
  Airports (OurAirports airports.csv, public domain, ~13 MB): weekly; the US airports with
  a 3-letter code are kept (about 1,070).
  On-time record (Bureau of Transportation Statistics, US government work): the
  month's file of every scheduled flight of the large US airlines (transtats.bts.gov/PREZIP,
  ~33 MB zipped, 630,000 flights, published about two months late). The site's file list
  (~160 KB) is read weekly and a file is downloaded only when it is a newer month. Kept:
  one row per airline, flight number and route (how often it flew, on time, cancelled, its
  usual times). Tail numbers and single flights are not kept.

Nothing here knows where home is, and a search never causes a request.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import email.utils
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import newsdb  # noqa: E402  (Postgres or the NEWS_SQLITE file)

USER_AGENT = "logout-weather/1 (personal weather on a home server; logout.cloud)"
TIMEOUT_S = 120
NBM = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
METARS = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
ALERTS = "https://api.weather.gov/alerts/active?status=actual"
GAZ = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
POP = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv"
# USGS Geographic Names (public domain): every named community, incorporated or not (~46 MB zip)
GNIS = "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/DomesticNames/DomesticNames_National_Text.zip"
GNIS_KIND = "populated place"
NAS = "https://nasstatus.faa.gov/api/airport-status-information"
AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORTS_MIN = 500            # fewer US airports than this: the file's layout changed, keep the old list
NAS_MAX_BYTES = 2 * 1024 * 1024
ONTIME_INDEX = "https://transtats.bts.gov/PREZIP/"
# the site renamed these files once ("(Beginning_January_2018)" lost its brackets), so the name is read from its own list
ONTIME_FILE = re.compile(r'"(/PREZIP/On_Time_Marketing_Carrier_On_Time_Performance_[A-Za-z0-9_()]*?_(20\d\d)_(\d{1,2})\.zip)"')
ONTIME_MIN_FLIGHTS = 100_000  # a month has about 600,000; fewer than this is not a month's file
EVERY = {"alerts": 300, "metars": 600, "nbh": 3600, "nbe": 6 * 3600, "towns": 7 * 86400, "radar": 600,
         "nas": 300, "airports": 7 * 86400, "ontime": 7 * 86400}
# A home computer (installer/logout_search.py's collectors, homesettings.py's Data use) sets how often each part is
# fetched: WX_EVERY_<PART>_S in seconds; 0 = never (the Light setting skips the 28 MB hourly bulletin and keeps the
# 8-day one). Anything else unparseable keeps the default.
for _k in EVERY:
    _v = os.environ.get(f"WX_EVERY_{_k.upper()}_S", "")
    if _v.isdigit():
        EVERY[_k] = int(_v) if int(_v) > 0 else float("inf")
# Radar pictures. Unset = no radar: this file also runs on home boxes, which must not each pull 2 GB a day.
RADAR_DIR = os.environ.get("WX_RADAR_DIR", "")
RADAR = "https://radar.weather.gov/ridge/standard"
RADAR_STATIONS = "https://api.weather.gov/radar/stations?stationType=WSR-88D"
RADAR_STATIONS_EVERY_S = 7 * 86400
RADAR_KEEP = 10               # frames in a station's loop (100 minutes at one a round)
RADAR_SPARE = 3               # older files left on disk for a page still playing the previous list
RADAR_MAX_BYTES = 2 * 1024 * 1024
RADAR_TIMEOUT_S = 20          # one silent station must not hold up alerts and METARs
RADAR_GAP_S = 0.2             # between stations: a round is about a minute, never a burst
RADAR_ID = re.compile(r"^[A-Z]{4}$")
# on an install (WX_RADAR_DEMAND=1), fetch stations.json (~20 KB/week) so the engine knows every station,
# but fetch GIF frames ONLY for the home station and stations queried recently (~13 MB/day max per active station).
RADAR_DEMAND = os.environ.get("WX_RADAR_DEMAND", "") == "1"
RADAR_INTEREST_TTL_S = int(os.environ.get("WX_RADAR_INTEREST_TTL_S", str(24 * 3600)))
RADAR_DEMAND_MAX = int(os.environ.get("WX_RADAR_DEMAND_MAX", "8"))
RADAR_MAX_KM = 230
TICK_S = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_towns (key text, name text, state text, lat double precision,
    lon double precision, pop integer, counties text[], tz text);
ALTER TABLE wx_towns ADD COLUMN IF NOT EXISTS kind text;      -- township, city, borough, CDP …
ALTER TABLE wx_towns ADD COLUMN IF NOT EXISTS county text;    -- "Marlow County": article titles
CREATE INDEX IF NOT EXISTS wx_towns_key ON wx_towns (key);
CREATE TABLE IF NOT EXISTS wx_stations (id text PRIMARY KEY, lat double precision, lon double precision);
CREATE TABLE IF NOT EXISTS wx_obs (id text PRIMARY KEY, obs jsonb, at timestamptz);
CREATE TABLE IF NOT EXISTS wx_fc (id text PRIMARY KEY, hours jsonb, days jsonb, cycle timestamptz, days_cycle timestamptz);
CREATE TABLE IF NOT EXISTS wx_alerts (id text PRIMARY KEY, event text, severity text, headline text,
    ends text, same text[], poly jsonb);
"""
# The airport and flight tables exist only in the news file (schema-sqlite.sql): Postgres is retired, and on a
# host that still writes to it these parts are never fetched.
SQLITE_ONLY = ("airports", "nas", "ontime")


def log(msg: str) -> None:
    print(f"[wxall] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def fetch(url: str, accept: str = "*/*") -> bytes:
    # gzip (2026-11-14): the national alerts file is 2.0 MB plain and 131 KB gzipped, and is asked for every 5 minutes:
    # ~580 MB a day plain, ~38 MB gzipped. A server that sends it plain anyway is read as before.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(200 * 1024 * 1024)
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
        return body


def c_to_f(c) -> int | None:
    return None if c is None else round(float(c) * 9 / 5 + 32)


# ---------------------------------------------------------------------------
# NBM station bulletins
# ---------------------------------------------------------------------------
# The version is optional: NOAA dropped it from the header on 2026-11-03 ("NBM V4.2 NBH GUIDANCE"
# became "NBM  NBH GUIDANCE"), and a pattern that required it read 0 stations for a week.
_HEAD = re.compile(r"^\s*(\S+)\s+NBM(?:\s+V[\d.]+)?\s+(NBH|NBE) GUIDANCE\s+(\d+)/(\d+)/(\d+)\s+(\d{2})(\d{2}) UTC")


def _num(s: str) -> int | None:
    s = s.strip().strip("|").strip()
    try:
        return int(s)
    except ValueError:
        return None


def parse_nbm(text: str, keep: tuple | None = None) -> dict[str, dict]:
    """A blend_nbhtx / blend_nbetx bulletin -> {station: {"cycle", "valid": [UTC times],
    "rows": {label: [int | None per column]}}}. Values are right-aligned in 3-character
    fields under the UTC (hourly) or FHR (extended) line; '|' separates days."""
    out: dict[str, dict] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        sid, kind = m.group(1), m.group(2)
        cycle = dt.datetime(int(m.group(5)), int(m.group(3)), int(m.group(4)), int(m.group(6)), tzinfo=dt.timezone.utc)
        block = []
        i += 1
        while i < len(lines) and not _HEAD.match(lines[i]):
            block.append(lines[i])
            i += 1
        ref = next((ln for ln in block if ln[:5].strip() == ("UTC" if kind == "NBH" else "FHR")), None)
        if ref is None:
            continue
        spans = [mm.end() for mm in re.finditer(r"\d+", ref) if mm.start() >= 5]
        if kind == "NBH":
            valid = [cycle + dt.timedelta(hours=k + 1) for k in range(len(spans))]
        else:
            valid = [cycle + dt.timedelta(hours=int(ref[e - 3:e])) for e in spans]
        rows = {}
        for ln in block:
            label = ln[:5].strip()
            if len(label) == 3 and label.isalnum() and label.isupper() and (keep is None or label in keep):
                rows[label] = [_num(ln[e - 3:e]) if len(ln) >= e else None for e in spans]
        out[sid] = {"cycle": cycle, "valid": valid, "rows": rows}
    return out


NBM_MIN_STATIONS = 1000       # a real bulletin carries about 9,500
KEEP = {"nbhtx": ("TMP", "P01", "SKY", "T01", "PSN", "WSP", "GST"), "nbetx": ("TXN", "P12", "SKY", "T12", "PSN")}


def compact(st: dict, labels: tuple) -> dict:
    """What search-api needs of one station's bulletin: the valid times and a few rows.
    Days and hours are built at question time, in the asked-about town's zone."""
    return {"valid": [v.isoformat() for v in st["valid"]],
            "rows": {lab: st["rows"][lab] for lab in labels if lab in st["rows"]}}


# ---------------------------------------------------------------------------
# METARs: now
# ---------------------------------------------------------------------------
_WX = [("TS", "Thunderstorm"), ("FZRA", "Freezing rain"), ("SN", "Snow"), ("RA", "Rain"), ("DZ", "Drizzle"),
       ("FG", "Fog"), ("BR", "Mist"), ("HZ", "Haze"), ("FU", "Smoke")]
_SKY = {"CLR": "Clear", "SKC": "Clear", "CAVOK": "Clear", "FEW": "Mostly clear", "SCT": "Partly cloudy",
        "BKN": "Mostly cloudy", "OVC": "Overcast", "OVX": "Obscured"}


def metar_words(wx: str, covers: list[str]) -> str:
    for code, word in _WX:
        if code in (wx or ""):
            return ("Light " + word.lower() if wx.startswith("-") and code not in ("FG", "BR", "HZ", "FU")
                    else "Heavy " + word.lower() if wx.startswith("+") else word)
    order = ["CLR", "SKC", "CAVOK", "FEW", "SCT", "BKN", "OVC", "OVX"]
    worst = max((c for c in covers if c in order), key=order.index, default="")
    return _SKY.get(worst, "")


def humidity(t_c, d_c) -> int | None:
    if t_c is None or d_c is None:
        return None
    a, b = 17.625, 243.04
    return round(100 * math.exp(a * d_c / (b + d_c)) / math.exp(a * t_c / (b + t_c)))


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_metars(text: str) -> dict[str, dict]:
    """aviationweather.gov's metars.cache.csv -> {station: {lat, lon, now}} (the latest
    report per station; the file repeats sky_cover columns, read by position)."""
    rd = csv.reader(io.StringIO(text))
    head = next(rd, [])
    col = {h: i for i, h in reversed(list(enumerate(head)))}          # first occurrence
    sky_cols = [i for i, h in enumerate(head) if h == "sky_cover"]
    out: dict[str, dict] = {}
    for row in rd:
        if len(row) < len(head) - 5:
            continue
        g = lambda name: row[col[name]] if col.get(name) is not None and col[name] < len(row) else ""
        sid, lat, lon = g("station_id"), _f(g("latitude")), _f(g("longitude"))
        if not sid or lat is None or lon is None:
            continue
        t, d = _f(g("temp_c")), _f(g("dewpoint_c"))
        kt, gust = _f(g("wind_speed_kt")), _f(g("wind_gust_kt"))
        at = g("observation_time")
        if sid in out and out[sid]["now"]["at"] >= at:
            continue
        out[sid] = {"lat": lat, "lon": lon, "now": {
            "temp_f": c_to_f(t), "feels_f": None, "humidity": humidity(t, d),
            "wind_mph": None if kt is None else round(kt * 1.15078),
            "gust_mph": None if gust is None else round(gust * 1.15078),
            "text": metar_words(g("wx_string"), [row[i] for i in sky_cols if i < len(row)]), "at": at}}
    return out


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def parse_alerts(data: dict) -> list[dict]:
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
            continue
        geom = f.get("geometry") or {}
        poly = geom.get("coordinates") if geom.get("type") == "Polygon" else None
        out.append({"id": p.get("id") or f.get("id") or "", "event": p.get("event") or "",
                    "severity": p.get("severity") or "Unknown", "headline": p.get("headline") or "",
                    "ends": p.get("ends") or p.get("expires"),
                    "same": [s[1:] if len(s) == 6 else s for s in (p.get("geocode") or {}).get("SAME") or []],
                    "poly": poly})
    return [a for a in out if a["id"] and a["event"]]


def in_polygon(lat: float, lon: float, rings: list) -> bool:
    """Ray casting on a GeoJSON Polygon ([lon, lat] pairs; holes ignored)."""
    ring = rings[0] if rings else []
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Towns
# ---------------------------------------------------------------------------
_SUFFIX = re.compile(r"\s+(?:\(balance\)|city and borough|consolidated government(?: \(balance\))?|"
                     r"metropolitan government(?: \(balance\))?|unified government(?: \(balance\))?|"
                     r"urban county|charter township|city|town|village|borough|CDP|township|municipality|"
                     r"plantation|comunidad|zona urbana|gore|grant|location|purchase|CCD)$")   # case matters: "Carson City"


def town_kind(raw: str) -> str:
    """"Marsh township" -> "township", "Chicago city" -> "city" (what Wikipedia titles follow)."""
    m = _SUFFIX.search(re.sub(r"\s*\(balance\)$", "", raw.strip()))
    return m.group(0).strip().lower() if m else ""


def town_name(raw: str) -> str:
    n = re.sub(r"^Urban ", "", raw.strip())             # "Urban Honolulu CDP"
    n = re.sub(r"\s*\(balance\)$", "", n)                # "Nashville-Davidson metropolitan government (balance)"
    return _SUFFIX.sub("", n, count=1).strip()          # once: "Ocean City city" is Ocean City


STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver", "PR": "America/Puerto_Rico",
}


def town_tz(state: str, lat: float, lon: float) -> str:
    """The state's zone, with the split states cut by longitude (close enough for day names
    and hours; a town on the line may be an hour off)."""
    if state == "FL" and lon < -85.0:
        return "America/Chicago"
    if state == "TX" and lon < -104.9:
        return "America/Denver"
    if state == "TN" and lon > -85.3:
        return "America/New_York"
    if state == "KY" and lon < -86.0:
        return "America/Chicago"
    if state == "IN" and lon < -86.7 and (lat > 41.0 or lat < 38.4):
        return "America/Chicago"
    if state == "MI" and lon < -87.6 and lat > 45.5:
        return "America/Menominee"
    if state in ("ND", "SD", "NE", "KS") and lon < {"ND": -101.5, "SD": -100.5, "NE": -101.3, "KS": -101.3}[state]:
        return "America/Denver"
    if state == "ID" and lat > 45.5:
        return "America/Los_Angeles"
    if state == "OR" and lon > -117.9 and lat < 44.5:
        return "America/Boise"
    return STATE_TZ.get(state, "America/New_York")


def parse_towns(places_tsv: str, cousubs_tsv: str, pop_csv: str, counties_tsv: str = "") -> list[tuple]:
    """Gazetteer places and county subdivisions (New Jersey's and Pennsylvania's townships
    are not places) with the Census population estimate, county FIPS codes and the first
    county's name -> (key, name, state, lat, lon, pop, counties, tz, kind, county) rows."""
    pop_place, pop_cousub, counties = {}, {}, {}
    county_name = {}
    rd = csv.reader(io.StringIO(counties_tsv), delimiter="\t")
    head = [h.strip() for h in next(rd, [])]
    if "GEOID" in head and "NAME" in head:
        for p in rd:
            if len(p) > max(head.index("GEOID"), head.index("NAME")):
                county_name[p[head.index("GEOID")].strip()] = p[head.index("NAME")].strip()
    for r in csv.DictReader(io.StringIO(pop_csv)):
        try:
            p = int(r["POPESTIMATE2024"])
        except (KeyError, ValueError):
            continue
        if r["SUMLEV"] == "162":
            pop_place[r["STATE"] + r["PLACE"]] = p
        elif r["SUMLEV"] == "157":
            counties.setdefault(r["STATE"] + r["PLACE"], []).append(r["STATE"] + r["COUNTY"])
        elif r["SUMLEV"] == "061":
            pop_cousub[r["STATE"] + r["COUNTY"] + r["COUSUB"]] = p
    rows = []
    for text, is_place in ((places_tsv, True), (cousubs_tsv, False)):
        rd = csv.reader(io.StringIO(text), delimiter="\t")
        head = [h.strip() for h in next(rd, [])]
        ix = {h: i for i, h in enumerate(head)}
        for p in rd:
            p = [x.strip() for x in p]
            if len(p) < len(head):
                continue
            geoid, raw = p[ix["GEOID"]], p[ix["NAME"]]
            # a county subdivision is a town only when it governs (A/B/C): not a statistical CCD
            if not is_place and p[ix["FUNCSTAT"]] not in ("A", "B", "C"):
                continue
            lat, lon = _f(p[ix["INTPTLAT"]]), _f(p[ix["INTPTLONG"]])
            name, state = town_name(raw), p[ix["USPS"]]
            if lat is None or lon is None or not name:
                continue
            if is_place:
                pop, cty = pop_place.get(geoid, 0), counties.get(geoid, [])
            else:
                pop, cty = pop_cousub.get(geoid, 0), [geoid[:5]]
            cty = sorted(set(cty))
            rows.append((name.lower(), name, state, lat, lon, pop, cty, town_tz(state, lat, lon),
                         town_kind(raw), county_name.get(cty[0], "") if cty else ""))
    return rows


def parse_gnis(lines, towns: list[tuple], places_tsv: str, counties_tsv: str = "") -> list[tuple]:
    """GNIS populated places the Census rows do not already name in that state ("Bayhollow, NJ" is
    a community inside Example Township: no place, no county subdivision) -> rows like
    parse_towns's, population 0 (none is published), kind GNIS_KIND."""
    usps, county_name = {}, {}
    for text, to, val in ((places_tsv, usps, "USPS"), (counties_tsv, county_name, "NAME")):
        rd = csv.reader(io.StringIO(text), delimiter="\t")
        head = [h.strip() for h in next(rd, [])]
        if "GEOID" in head and val in head:
            for p in rd:
                if len(p) > max(head.index("GEOID"), head.index(val)):
                    g = p[head.index("GEOID")].strip()
                    to[g if val == "NAME" else g[:2]] = p[head.index(val)].strip()
    have = {(r[0], r[2]) for r in towns}
    rd = csv.reader(lines, delimiter="|", quoting=csv.QUOTE_NONE)
    ix = {h.strip().lstrip("\ufeff"): i for i, h in enumerate(next(rd, []))}
    rows = []
    if not {"feature_name", "feature_class", "state_numeric", "county_numeric", "prim_lat_dec", "prim_long_dec"} <= set(ix):
        return rows
    for p in rd:
        if len(p) < len(ix) or p[ix["feature_class"]] != "Populated Place":
            continue
        name, state = p[ix["feature_name"]].strip(), usps.get(p[ix["state_numeric"]])
        lat, lon = _f(p[ix["prim_lat_dec"]]), _f(p[ix["prim_long_dec"]])
        if not name or not state or "(historical)" in name or not lat or not lon or (name.lower(), state) in have:
            continue
        cty = p[ix["state_numeric"]] + p[ix["county_numeric"]]
        rows.append((name.lower(), name, state, lat, lon, 0, [cty], town_tz(state, lat, lon), GNIS_KIND,
                     county_name.get(cty, "")))
    return rows


def _unzip_lines(blob: bytes):
    """The lines of the archive's text file, read as they are wanted (GNIS is 150 MB unpacked)."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    with z.open(next(n for n in z.namelist() if n.endswith(".txt"))) as f:
        yield from io.TextIOWrapper(f, "utf-8", "replace", newline="")


def _unzip_first(blob: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(blob))
    return z.read(z.namelist()[0]).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Radar pictures
# ---------------------------------------------------------------------------
def parse_radar_stations(data: dict) -> list[dict]:
    """api.weather.gov/radar/stations -> [{"id", "name", "lat", "lon"}] (GeoJSON points are [lon, lat])."""
    out = []
    for f in data.get("features") or []:
        p, xy = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        sid = p.get("id") or ""
        if RADAR_ID.match(sid) and len(xy) >= 2 and _f(xy[0]) is not None and _f(xy[1]) is not None:
            out.append({"id": sid, "name": str(p.get("name") or "")[:40], "lat": round(float(xy[1]), 4), "lon": round(float(xy[0]), 4)})
    return sorted(out, key=lambda s: s["id"])


def fetch_newer(url: str, since: float | None) -> tuple[bytes, float] | None:
    """-> (body, its Last-Modified as epoch seconds), or None when the server has nothing newer than `since`."""
    head = {"User-Agent": USER_AGENT, "Accept": "image/gif"}
    if since:
        head["If-Modified-Since"] = email.utils.formatdate(since, usegmt=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=head), timeout=RADAR_TIMEOUT_S) as r:
            body = r.read(RADAR_MAX_BYTES + 1)
            lm = r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise
    if len(body) > RADAR_MAX_BYTES or body[:4] != b"GIF8":
        raise ValueError(f"not a radar picture ({len(body)} bytes, starts {body[:4]!r})")
    at = email.utils.parsedate_to_datetime(lm).timestamp() if lm else time.time()
    return body, at


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)                               # a reader sees the old file or the new one, never half


def radar_frames(folder: str) -> list[int]:
    """The frame times (epoch seconds, oldest first) a station's folder holds."""
    try:
        return sorted(int(n[:-4]) for n in os.listdir(folder) if n.endswith(".gif") and n[:-4].isdigit())
    except OSError:
        return []


def radar_keep(root: str, st: dict, body: bytes | None, at: float) -> bool:
    """Store one new frame (None = nothing new), drop the old ones, rewrite the station's list.
    -> whether a frame was added. Files are <root>/<ID>/<epoch>.gif; the list is <root>/<ID>.json."""
    folder = os.path.join(root, st["id"])
    os.makedirs(folder, exist_ok=True)
    have = radar_frames(folder)
    added = False
    if body is not None and int(at) not in have:
        _write(os.path.join(folder, f"{int(at)}.gif"), body)
        have = sorted(have + [int(at)])
        added = True
    for old in have[:-(RADAR_KEEP + RADAR_SPARE)]:
        try:
            os.remove(os.path.join(folder, f"{old}.gif"))
        except OSError:
            pass
    if added or not os.path.exists(folder + ".json"):
        _write(folder + ".json", json.dumps({"id": st["id"], "name": st["name"], "lat": st["lat"], "lon": st["lon"],
                                             "frames": have[-RADAR_KEEP:]}, separators=(",", ":")).encode())
    return added


def _parse_latlon(raw: str | None) -> tuple[float, float] | None:
    if not raw or raw.count(",") != 1:
        return None
    try:
        lat, lon = (float(x.strip()) for x in raw.split(",", 1))
    except ValueError:
        return None
    return (lat, lon) if (-90 <= lat <= 90 and -180 <= lon <= 180) else None


def nearest_radar_station(lat: float, lon: float, stations: list[dict], max_km: float = RADAR_MAX_KM) -> dict | None:
    """Nearest valid radar station within max_km, or None."""
    best, best_km = None, max_km
    for s in stations:
        sid = str(s.get("id") or "")
        if not RADAR_ID.fullmatch(sid):
            continue
        try:
            km = 111.2 * math.hypot(s["lat"] - lat, (s["lon"] - lon) * math.cos(math.radians(lat)))
        except (KeyError, TypeError):
            continue
        if km <= best_km:
            best, best_km = s, km
    return best


def read_radar_interest(root: str, now: float | None = None, ttl_s: int | None = None) -> list[str]:
    """Station ids queried recently by searches, most recent first (capped at RADAR_DEMAND_MAX)."""
    t = time.time() if now is None else now
    ttl = RADAR_INTEREST_TTL_S if ttl_s is None else ttl_s
    try:
        with open(os.path.join(root, "interest.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    entries = raw.get("stations") if isinstance(raw, dict) and isinstance(raw.get("stations"), dict) else raw
    if not isinstance(entries, dict):
        return []
    keep = []
    for sid, ts in entries.items():
        if isinstance(sid, str) and RADAR_ID.fullmatch(sid) and isinstance(ts, (int, float)) and not isinstance(ts, bool):
            if t - ts <= ttl:
                keep.append((float(ts), sid))
    keep.sort(reverse=True)
    return [sid for _, sid in keep[:RADAR_DEMAND_MAX]]


def radar_wanted_stations(root: str, stations: list[dict], now: float | None = None,
                          home: tuple[float, float] | None = None) -> list[dict]:
    """Stations this collector fetches frames for: all on public, home + recently queried areas on installs."""
    if not RADAR_DEMAND:
        return stations
    by_id = {s["id"]: s for s in stations if RADAR_ID.fullmatch(str(s.get("id") or ""))}
    wanted: list[str] = []
    pt = home if home is not None else _parse_latlon(os.environ.get("WX_RADAR_HOME") or os.environ.get("SEARCH_HOME", ""))
    if pt:
        hs = nearest_radar_station(pt[0], pt[1], stations)
        if hs:
            wanted.append(hs["id"])
    for sid in read_radar_interest(root, now=now):
        if sid in by_id and sid not in wanted:
            wanted.append(sid)
    return [by_id[sid] for sid in wanted if sid in by_id]


def load_radar(_conn, state: dict) -> None:
    """One round: the station list when it is a week old, then each station's newest frame if it is newer
    than the one we hold. A station that fails is skipped (and counted); the round goes on."""
    os.makedirs(RADAR_DIR, exist_ok=True)
    listing = os.path.join(RADAR_DIR, "stations.json")
    if not state.get("stations"):
        try:
            state["stations"] = json.load(open(listing, encoding="utf-8"))["stations"]
            state["listed"] = os.path.getmtime(listing)
        except (OSError, ValueError, KeyError):
            pass
    if not state.get("stations") or time.time() - state.get("listed", 0) > RADAR_STATIONS_EVERY_S:
        got = parse_radar_stations(json.loads(fetch(RADAR_STATIONS, "application/geo+json")))
        if len(got) < 100:                              # the country has about 160; a short list is a bad answer
            raise ValueError(f"radar station list: read {len(got)}, expected about 160")
        state["stations"], state["listed"] = got, time.time()
        _write(listing, json.dumps({"stations": got}, separators=(",", ":")).encode())
        log(f"radar: {len(got)} stations listed")
    targets = radar_wanted_stations(RADAR_DIR, state["stations"])
    new = failed = size = 0
    fetched = state.setdefault("fetched_ids", set())
    for st in targets:
        have = radar_frames(os.path.join(RADAR_DIR, st["id"]))
        try:
            got = fetch_newer(f"{RADAR}/{st['id']}_0.gif", have[-1] if have else None)
            if radar_keep(RADAR_DIR, st, got[0] if got else None, got[1] if got else 0):
                new, size = new + 1, size + len(got[0])
            fetched.add(st["id"])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            failed += 1
            state["why"] = f"{st['id']}: {type(e).__name__}: {str(e)[:80]}"
        time.sleep(RADAR_GAP_S)
    log(f"radar: {new} new frames of {len(targets)} stations ({size / 1e6:.1f} MB)"
        + (f", {failed} failed (last: {state.get('why')})" if failed else ""))


# ---------------------------------------------------------------------------
# The latest bulletin
# ---------------------------------------------------------------------------
def latest_cycle(product: str, now: dt.datetime) -> tuple[str, dt.datetime] | None:
    """-> (url, cycle) of the newest blend_<product>.tHHz in NOAA's bucket. A day holds
    thousands of gridded files (a listing stops at 1,000), so: the day's hour folders
    first, then each hour's text folder, newest first, today or yesterday."""
    for back in (0, 1):
        day = (now - dt.timedelta(days=back)).strftime("%Y%m%d")
        hours = re.findall(rf"<Prefix>blend\.{day}/(\d\d)/</Prefix>",
                           fetch(f"{NBM}/?list-type=2&delimiter=/&prefix=blend.{day}/").decode())
        for hh in sorted(hours, reverse=True)[:6]:
            key = f"blend.{day}/{hh}/text/blend_{product}.t{hh}z"
            if f"<Key>{key}</Key>" in fetch(f"{NBM}/?list-type=2&prefix={key}").decode():
                return f"{NBM}/{key}", dt.datetime.strptime(day + hh, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    return None


# ---------------------------------------------------------------------------
# Airports and their status
# ---------------------------------------------------------------------------
AIRPORT_COUNTRIES = frozenset(("US", "PR", "GU", "VI", "AS", "MP"))
AIRPORT_KINDS = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
_AIRPORT_CODE = re.compile(r"^[A-Z][A-Z0-9]{2}$")
_ICAO = re.compile(r"^[A-Z]{4}$")


def parse_airports(text: str) -> list[tuple]:
    """OurAirports airports.csv -> (code, icao, name, city, state, lat, lon, kind, sched) for US airports with a
    3-letter code: every large and medium one, and a small one only with scheduled flights. The code is the
    one the FAA's status file uses (IATA, else the FAA's own); the METAR station is the ICAO code as listed
    (PANC, PHNL, TJSJ: never "K" + code)."""
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error as e:
        raise ValueError(f"airports.csv unreadable: {e}") from e
    best: dict[str, tuple] = {}
    for r in rows:
        kind = r.get("type") or ""
        if r.get("iso_country") not in AIRPORT_COUNTRIES or kind not in AIRPORT_KINDS:
            continue
        sched = 1 if r.get("scheduled_service") == "yes" else 0
        if kind == "small_airport" and not sched:
            continue
        code = (r.get("iata_code") or r.get("local_code") or "").strip().upper()
        lat, lon = _f(r.get("latitude_deg") or ""), _f(r.get("longitude_deg") or "")
        if not _AIRPORT_CODE.match(code) or not (r.get("name") or "").strip() or lat is None or lon is None:
            continue
        icao = next((c for c in ((r.get("icao_code") or "").strip().upper(), (r.get("gps_code") or "").strip().upper())
                     if _ICAO.match(c)), "")
        region = (r.get("iso_region") or "").split("-")
        state = region[1] if len(region) > 1 and r["iso_country"] == "US" else r["iso_country"]
        row = (code, icao, r["name"].strip()[:120], (r.get("municipality") or "").strip()[:80], state, lat, lon, kind, sched)
        old = best.get(code)
        if old is None or (AIRPORT_KINDS[kind], -sched) < (AIRPORT_KINDS[old[7]], -old[8]):
            best[code] = row
    if len(best) < AIRPORTS_MIN:
        raise ValueError(f"airports.csv: read {len(best)} US airports, expected over {AIRPORTS_MIN} — has the file's layout changed?")
    return sorted(best.values())


_HOURS = re.compile(r"(\d+)\s*hours?")
_MINS = re.compile(r"(\d+)\s*minutes?")


def nas_minutes(text: str | None) -> int | None:
    """"1 hour and 15 minutes" -> 75; "33 minutes" -> 33; anything else -> None."""
    h, m = _HOURS.search(text or ""), _MINS.search(text or "")
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


_REASON = {"vol": "volume", "wx": "weather", "rwy": "runway", "eq": "equipment", "equip": "equipment", "staff": "staffing",
           "tm initiatives": "traffic management", "tm": "traffic management", "mit": "", "stop": ""}


def nas_reason(raw: str | None) -> str:
    """The FAA's codes as words: "VOL:Multi-taxi" -> "volume (multi-taxi)"; "TM Initiatives:MIT:VOL" ->
    "traffic management (volume)"; plain text ("low visibility") stays."""
    parts = []
    for p in (raw or "").split(":"):
        p = " ".join(p.split()).lower()
        p = _REASON.get(p, p)
        if p and p not in parts:
            parts.append(p)
    if not parts:
        return ""
    return (parts[0] + (f" ({', '.join(parts[1:])})" if parts[1:] else ""))[:80]


# A NOTAM that closes the whole airport: "AP CLSD" followed by nothing, or by its dates. "AP CLSD TO NON SKED
# TRANSIENT GA ACFT EXC PPR" (LAX, SAN and LAS carried one for months in 2026) closes it to a few private
# aircraft; calling that "closed" would be false, so it is passed on as a notice with its own words.
_FULL_CLOSURE = re.compile(r"\bAP CLSD(?:\s+(?:DLY\s+)?\d{4,10}-\d{4,10})?\s*$")


def nas_closed(notam: str) -> bool:
    return bool(_FULL_CLOSURE.search(" ".join((notam or "").upper().split())))


def parse_nas(body: bytes) -> tuple[float, dict[str, list[dict]]]:
    """The FAA's airport status XML -> (its Update_Time as Unix seconds, {airport code: [item, ...]}).
    Raises ValueError when the file is not the layout this was written for, so the old rows are kept and
    the health check sees them age instead of the card saying "no delays"."""
    if len(body) > NAS_MAX_BYTES or b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise ValueError(f"airport status: {len(body)} bytes, or a document type declaration — not read")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"airport status is not XML: {e}") from e
    if root.tag != "AIRPORT_STATUS_INFORMATION":
        raise ValueError(f"airport status: root element {root.tag[:40]!r} — has the layout changed?")
    try:
        updated = dt.datetime.strptime((root.findtext("Update_Time") or "").strip(), "%a %b %d %H:%M:%S %Y GMT") \
            .replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError as e:
        raise ValueError(f"airport status: Update_Time {root.findtext('Update_Time')!r} unreadable") from e
    out: dict[str, list[dict]] = {}
    known = unread = 0
    t = lambda el, tag: " ".join((el.findtext(tag) or "").split())
    for dtype in root.findall("Delay_type"):
        name = t(dtype, "Name").lower()
        entries = [el for el in dtype.iter() if el.find("ARPT") is not None]
        for el in entries:
            code = t(el, "ARPT").upper()
            if not _AIRPORT_CODE.match(code):
                unread += 1
                continue
            items = []
            if name.startswith("ground delay"):
                items.append({"kind": "ground_delay", "reason": nas_reason(t(el, "Reason")),
                              "avg": nas_minutes(t(el, "Avg")), "max": nas_minutes(t(el, "Max"))})
            elif name.startswith("ground stop"):
                items.append({"kind": "ground_stop", "reason": nas_reason(t(el, "Reason")), "until": t(el, "End_Time")[:40]})
            elif "arrival/departure" in name:
                for ad in el.findall("Arrival_Departure"):
                    kind = (ad.get("Type") or "").strip().lower()
                    if kind in ("arrival", "departure"):
                        items.append({"kind": kind, "reason": nas_reason(t(el, "Reason")), "min": nas_minutes(t(ad, "Min")),
                                      "max": nas_minutes(t(ad, "Max")), "trend": t(ad, "Trend").lower()[:12]})
            elif "closure" in name:
                notam = t(el, "Reason")
                when = {"start": t(el, "Start").rstrip(".")[:40], "reopen": t(el, "Reopen").rstrip(".")[:40]}
                items.append({"kind": "closure", **when} if nas_closed(notam) else {"kind": "notice", "text": notam[:300], **when})
            if items:
                known += 1
                out.setdefault(code, []).extend(items)
            else:
                unread += 1
    if unread and not known:
        raise ValueError(f"airport status: {unread} entries and none readable — has the layout changed?")
    return updated, out


def ontime_latest(index_html: str) -> tuple[int, int, str] | None:
    """The site's file list -> (year, month, path) of the newest month's file, or None."""
    found = [(int(y), int(m), path) for path, y, m in ONTIME_FILE.findall(index_html) if 1 <= int(m) <= 12]
    return max(found) if found else None


ONTIME_COLUMNS = ("Marketing_Airline_Network", "Flight_Number_Marketing_Airline", "Origin", "Dest", "CRSDepTime", "CRSArrTime",
                  "ArrDel15", "Cancelled")


def _hhmm(v: str) -> int | None:
    return int(v) if v.isdigit() and int(v) % 100 < 60 and int(v) <= 2400 else None


def parse_ontime(lines) -> list[tuple]:
    """The month's flights (CSV lines) -> one row per airline, flight number and route:
    (carrier, num, origin, dest, scheduled, completed, on time, cancelled, usual departure, usual arrival).
    On time is the government's own measure: at the gate less than 15 minutes late. The usual times are the
    most common scheduled ones (local, HHMM). Nothing about a single flight or aircraft is kept."""
    try:
        rd = csv.reader(lines)
        head = [h.strip() for h in next(rd, [])]
        col = {h: i for i, h in enumerate(head)}
        if any(c not in col for c in ONTIME_COLUMNS):
            raise ValueError(f"on-time file: columns {[c for c in ONTIME_COLUMNS if c not in col]} missing — has the layout changed?")
        ix = [col[c] for c in ONTIME_COLUMNS]
        top = max(ix)
        agg: dict[tuple, list] = {}
        flights = 0
        for r in rd:
            if len(r) <= top:
                continue
            carrier, num, origin, dest, dep, arr, late, cancelled = (r[i].strip() for i in ix)
            if not (carrier and num.isdigit() and len(origin) == 3 and len(dest) == 3):
                continue
            flights += 1
            a = agg.setdefault((carrier.upper(), int(num), origin.upper(), dest.upper()), [0, 0, 0, 0, {}, {}])
            a[0] += 1
            if cancelled.startswith("1"):
                a[3] += 1
            elif late:                                   # empty for a diverted flight: neither on time nor late
                a[1] += 1
                a[2] += 1 if late.startswith("0") else 0
            for slot, v in ((4, _hhmm(dep)), (5, _hhmm(arr))):
                if v is not None:
                    a[slot][v] = a[slot].get(v, 0) + 1
    except csv.Error as e:
        raise ValueError(f"on-time file unreadable: {e}") from e
    if flights < ONTIME_MIN_FLIGHTS:
        raise ValueError(f"on-time file: {flights:,} flights, expected over {ONTIME_MIN_FLIGHTS:,} — has the layout changed?")
    usual = lambda times: max(times.items(), key=lambda kv: (kv[1], -kv[0]))[0] if times else None
    return [(k[0], k[1], k[2], k[3], a[0], a[1], a[2], a[3], usual(a[4]), usual(a[5])) for k, a in sorted(agg.items())]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def load_towns(conn) -> None:
    places = _unzip_first(fetch(GAZ + "2024_Gaz_place_national.zip"))
    cousubs = _unzip_first(fetch(GAZ + "2024_Gaz_cousubs_national.zip"))
    pops = fetch(POP).decode("latin-1")
    counties = _unzip_first(fetch(GAZ + "2024_Gaz_counties_national.zip"))
    rows = parse_towns(places, cousubs, pops, counties)
    census, gnis_err = len(rows), None
    try:                                                # without GNIS the Census towns still load
        rows += parse_gnis(_unzip_lines(fetch(GNIS)), rows, places, counties)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, StopIteration, zipfile.BadZipFile) as e:
        gnis_err = e
    with conn.transaction():
        conn.execute("DELETE FROM wx_towns")
        if newsdb.is_sqlite():
            conn.executemany("INSERT INTO wx_towns (key, name, state, lat, lon, pop, counties, tz, kind, county) "
                             "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
        else:
            with conn.conn.cursor().copy("COPY wx_towns (key, name, state, lat, lon, pop, counties, tz, kind, county) "
                                         "FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
    log(f"towns: {len(rows):,} ({len(rows) - census:,} from GNIS)")
    if gnis_err is not None:
        raise gnis_err                                  # the loop retries in 10 minutes


def load_metars(conn) -> None:
    got = parse_metars(gzip.decompress(fetch(METARS)).decode("utf-8", "replace"))
    at = newsdb.now()
    with conn.transaction():
        conn.executemany("INSERT INTO wx_stations (id, lat, lon) VALUES (%s, %s, %s) "
                         "ON CONFLICT (id) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon",
                         [(k, v["lat"], v["lon"]) for k, v in got.items()])
        conn.executemany("INSERT INTO wx_obs (id, obs, at) VALUES (%s, %s, %s) "
                         "ON CONFLICT (id) DO UPDATE SET obs = EXCLUDED.obs, at = EXCLUDED.at",
                         [(k, json.dumps(v["now"]), at) for k, v in got.items()])
    log(f"metars: {len(got):,} stations")


def load_nbm(conn, product: str, seen: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    found = latest_cycle(product, now)
    if not found or seen.get(product) == found[1]:
        return
    url, cycle = found
    parsed = parse_nbm(fetch(url).decode("ascii", "replace"), KEEP[product])     # only what is stored: memory
    if len(parsed) < NBM_MIN_STATIONS:
        # a bulletin that downloads but does not parse is a failure (retried, logged as one), never a
        # quiet "0 stations" that leaves last week's forecast in place
        raise ValueError(f"{product} {cycle:%Y-%m-%d %H}Z: read {len(parsed)} stations, expected over {NBM_MIN_STATIONS:,} "
                         "— has the bulletin's layout changed?")
    col, at = ("hours", "cycle") if product == "nbhtx" else ("days", "days_cycle")
    with conn.transaction():
        conn.executemany(f"INSERT INTO wx_fc (id, {col}, {at}) VALUES (%s, %s, %s) ON CONFLICT (id) "
                         f"DO UPDATE SET {col} = EXCLUDED.{col}, {at} = EXCLUDED.{at}",
                         [(sid, json.dumps(compact(st, KEEP[product])), cycle) for sid, st in parsed.items()])
    seen[product] = cycle
    log(f"{product} {cycle:%Y-%m-%d %H}Z: {len(parsed):,} stations")


def load_airports(conn) -> None:
    rows = parse_airports(fetch(AIRPORTS).decode("utf-8", "replace"))
    with conn.transaction():
        conn.execute("DELETE FROM airports")
        conn.executemany("INSERT INTO airports (code, icao, name, city, state, lat, lon, kind, sched) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
    log(f"airports: {len(rows):,}")


def load_nas(conn) -> None:
    updated, got = parse_nas(fetch(NAS, "application/xml"))
    rows = [(code, n, json.dumps(item)) for code, items in sorted(got.items()) for n, item in enumerate(items)]
    with conn.transaction():
        conn.execute("DELETE FROM airport_status")
        conn.executemany("INSERT INTO airport_status (code, n, item) VALUES (%s, %s, %s)", rows)
        conn.execute("INSERT INTO airport_feed (id, updated, fetched) VALUES (1, %s, %s) "
                     "ON CONFLICT (id) DO UPDATE SET updated = EXCLUDED.updated, fetched = EXCLUDED.fetched",
                     (updated, time.time()))
    log(f"airport status: {len(got):,} airports, {len(rows):,} items, FAA time {dt.datetime.fromtimestamp(updated, dt.timezone.utc):%H:%M}Z")


def load_ontime(conn) -> None:
    newest = ontime_latest(fetch(ONTIME_INDEX, "text/html").decode("utf-8", "replace"))
    if newest is None:
        raise ValueError("on-time file list: no monthly file named in it — has the page changed?")
    year, month, path = newest
    have = conn.execute("SELECT year, month FROM flight_ontime_feed WHERE id = 1").fetchone()
    if have and (have[0], have[1]) >= (year, month):
        log(f"on-time record: {have[0]}-{have[1]:02d} is the newest")
        return
    with zipfile.ZipFile(io.BytesIO(fetch(urllib.parse.urljoin(ONTIME_INDEX, urllib.parse.quote(path))))) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if name is None:
            raise ValueError("on-time file: no CSV in the zip")
        with z.open(name) as f:
            rows = parse_ontime(io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline=""))
    with conn.transaction():
        conn.execute("DELETE FROM flight_ontime")
        conn.executemany("INSERT INTO flight_ontime (carrier, num, origin, dest, n, done, ontime, cancelled, dep, arr) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
        conn.execute("INSERT INTO flight_ontime_feed (id, year, month, fetched) VALUES (1, %s, %s, %s) "
                     "ON CONFLICT (id) DO UPDATE SET year = EXCLUDED.year, month = EXCLUDED.month, fetched = EXCLUDED.fetched",
                     (year, month, time.time()))
    log(f"on-time record: {year}-{month:02d}, {len(rows):,} flight-number routes")


def load_alerts(conn) -> None:
    alerts = parse_alerts(json.loads(fetch(ALERTS, "application/geo+json")))
    with conn.transaction():
        conn.execute("DELETE FROM wx_alerts")
        conn.executemany("INSERT INTO wx_alerts (id, event, severity, headline, ends, same, poly) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                         [(a["id"], a["event"], a["severity"], a["headline"], a["ends"], a["same"],
                           json.dumps(a["poly"]) if a["poly"] else None) for a in alerts])
    log(f"alerts: {len(alerts):,}")


def run(once: bool = False) -> None:
    due = {k: (float("inf") if EVERY[k] == float("inf") else 0.0) for k in EVERY}   # a part set to never is never fetched
    seen: dict = {}
    radar: dict = {}
    if not RADAR_DIR:
        due["radar"] = float("inf")
    if newsdb.is_sqlite():
        newsdb.ensure_sqlite_schema()
    with newsdb.connect() as conn:
        if not newsdb.is_sqlite():
            conn.execute(SCHEMA)
        if (conn.execute("SELECT count(*) FROM wx_towns WHERE county IS NOT NULL").fetchone()[0] == 0
                or conn.execute("SELECT count(*) FROM wx_towns WHERE kind = %s", (GNIS_KIND,)).fetchone()[0] == 0):
            due["towns"] = 0.0
        else:
            due["towns"] = time.time() + EVERY["towns"]
        if not newsdb.is_sqlite():
            due.update({k: float("inf") for k in SQLITE_ONLY})
        elif EVERY["airports"] != float("inf") and conn.execute("SELECT count(*) FROM airports").fetchone()[0] > 0:
            due["airports"] = time.time() + EVERY["airports"]      # a restart does not download the list again
        while True:
            if RADAR_DIR and RADAR_DEMAND and EVERY["radar"] != float("inf"):
                if any(sid not in radar.get("fetched_ids", ()) for sid in read_radar_interest(RADAR_DIR)):
                    due["radar"] = 0.0
            for part, step in (("towns", load_towns), ("metars", load_metars), ("alerts", load_alerts),
                               ("nbh", lambda c: load_nbm(c, "nbhtx", seen)), ("nbe", lambda c: load_nbm(c, "nbetx", seen)),
                               ("radar", lambda c: load_radar(c, radar)), ("airports", load_airports), ("nas", load_nas),
                               ("ontime", load_ontime)):
                if time.time() < due[part]:
                    continue
                try:
                    step(conn)
                    due[part] = time.time() + EVERY[part]
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, zipfile.BadZipFile,
                        newsdb.sqlite3.OperationalError) as e:     # the poller held the file's write lock too long
                    log(f"{part} failed ({type(e).__name__}: {str(e)[:120]}) — retry in 10 min")
                    due[part] = time.time() + 600
            if once:
                return
            # on an install a search for a new area waits for its radar: look for a station newly asked for every 5 s, not
            # once a minute (the page asks again for about a minute and a half, owner 2026-11-18)
            for _ in range(max(1, TICK_S // 5)):
                time.sleep(min(5, TICK_S))
                if RADAR_DIR and RADAR_DEMAND and any(sid not in radar.get("fetched_ids", ()) for sid in read_radar_interest(RADAR_DIR)):
                    break


if __name__ == "__main__":
    run(once="--once" in sys.argv)
