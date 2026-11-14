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
import urllib.request
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
EVERY = {"alerts": 300, "metars": 600, "nbh": 3600, "nbe": 6 * 3600, "towns": 7 * 86400, "radar": 600}
# A home computer (installer/logout_search.py's collectors) fetches the hourly bulletin (~28 MB) every 3 hours instead of
# every hour: about 310 MB a day instead of 750. WX_EVERY_<PART>_S sets any part's interval in seconds.
for _k in EVERY:
    _v = os.environ.get(f"WX_EVERY_{_k.upper()}_S", "")
    if _v.isdigit() and int(_v) > 0:
        EVERY[_k] = int(_v)
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


def log(msg: str) -> None:
    print(f"[wxall] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def fetch(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read(200 * 1024 * 1024)


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
    new = failed = size = 0
    for st in state["stations"]:
        have = radar_frames(os.path.join(RADAR_DIR, st["id"]))
        try:
            got = fetch_newer(f"{RADAR}/{st['id']}_0.gif", have[-1] if have else None)
            if radar_keep(RADAR_DIR, st, got[0] if got else None, got[1] if got else 0):
                new, size = new + 1, size + len(got[0])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            failed += 1
            state["why"] = f"{st['id']}: {type(e).__name__}: {str(e)[:80]}"
        time.sleep(RADAR_GAP_S)
    log(f"radar: {new} new frames of {len(state['stations'])} stations ({size / 1e6:.1f} MB)"
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
    due = {k: 0.0 for k in EVERY}
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
        while True:
            for part, step in (("towns", load_towns), ("metars", load_metars), ("alerts", load_alerts),
                               ("nbh", lambda c: load_nbm(c, "nbhtx", seen)), ("nbe", lambda c: load_nbm(c, "nbetx", seen)),
                               ("radar", lambda c: load_radar(c, radar))):
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
            time.sleep(TICK_S)


if __name__ == "__main__":
    run(once="--once" in sys.argv)
