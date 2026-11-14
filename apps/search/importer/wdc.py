"""Web Data Commons schema.org extracts (businesses recipes events;
2026-09-24). WDC publishes, per schema.org class, every Common Crawl page that
carries that class, as gzip N-Quads grouped by page:
https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/<Class>/part_N.gz
(LocalBusiness 176 parts / 23.2 GB, Restaurant 11 / 1.6 GB, Recipe 21 / 3.8 GB,
Event 133 / 20.8 GB). Read on AWS, one part at a time — a university server.

    wdc.py businesses --class LocalBusiness --parts 176 --out biz.tsv.gz
        US businesses with a map point: id, name, type, lat, lon, street, city, region,
        postal, phone, website, page
    search_import.py wdc-places --file biz.tsv.gz [--zips 2024_Gaz_zcta_national.txt]
        -> places rows 'wdc:<id>'; address-only rows go to their ZIP's centre
"""

from __future__ import annotations

import gzip
import hashlib
import html
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific"
USER_AGENT = "logout-search-import/1 (personal search index; one file at a time)"
S = "http://schema.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# subject, predicate, object (IRI | blank node | literal with lang/datatype), graph
_QUAD = re.compile(r'^(<[^>]*>|_:\S+) <([^>]*)> (<[^>]*>|_:\S+|"(?:[^"\\]|\\.)*"(?:@[\w-]+|\^\^<[^>]*>)?) <([^>]*)>\s*\.\s*$')
_ESC = re.compile(r'\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)')


def log(msg: str) -> None:
    print(f"[wdc] {msg}", flush=True)


def _unescape(m):
    e = m.group(1)
    if e[0] in "uU":
        try:
            return chr(int(e[1:], 16))
        except ValueError:
            return ""
    return {"n": "\n", "t": "\t", "r": " ", '"': '"', "\\": "\\"}.get(e, e)


def term(t: str) -> str:
    """'<http://x>' -> 'http://x'; '"Café"@en' -> 'Café'; '"4.8E1"^^<…double>' -> '4.8E1'."""
    if t.startswith("<"):
        return t[1:-1]
    if t.startswith('"'):
        end = t.rfind('"')
        return _ESC.sub(_unescape, t[1:end])
    return t                                          # blank node id


def pages(lines):
    """N-Quad lines (grouped by page, as WDC writes them) -> (page URL, {subject:
    {predicate: [values]}}), one page at a time."""
    cur, subj = None, {}
    for line in lines:
        m = _QUAD.match(line)
        if not m:
            continue
        s, p, o, g = m.groups()
        if g != cur:
            if cur is not None:
                yield cur, subj
            cur, subj = g, {}
        subj.setdefault(s, {}).setdefault(p, []).append(term(o) if not o.startswith("_:") else o)
    if cur is not None:
        yield cur, subj


def _first(props: dict, key: str) -> str:
    v = props.get(S + key) or props.get("https://schema.org/" + key) or []
    return v[0].strip() if v else ""


def _all(props: dict, key: str) -> list[str]:
    return [v.strip() for v in (props.get(S + key) or []) + (props.get("https://schema.org/" + key) or []) if v.strip()]


# Opening hours, in the OSM opening_hours shape the start page reads ("Mo-Fr 09:00-17:00;
# Sa 10:00-14:00"): schema.org openingHours is already that shape (or free text, kept as
# written); openingHoursSpecification nodes (dayOfWeek, opens, closes) are turned into it.
_DAY2 = {"monday": "Mo", "tuesday": "Tu", "wednesday": "We", "thursday": "Th", "friday": "Fr",
         "saturday": "Sa", "sunday": "Su"}
_WEEK = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})")


def _hhmm(v: str) -> str:
    m = _HHMM.match(v.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def hours_of(props: dict, subj: dict) -> str:
    plain = [v for v in _all(props, "openingHours") if not v.startswith("_:")]   # a node id, not hours
    if plain:
        return "; ".join(dict.fromkeys(plain))[:200]
    per_day: dict[str, list[str]] = {}
    for sid in _all(props, "openingHoursSpecification"):
        spec = subj.get(sid, {})
        opens, closes = _hhmm(_first(spec, "opens")), _hhmm(_first(spec, "closes"))
        if not opens or not closes or _all(spec, "validFrom"):   # seasonal / one-off: not the week
            continue
        for d in _all(spec, "dayOfWeek"):
            day = _DAY2.get(d.rstrip("/").rsplit("/", 1)[-1].lower())
            if day and f"{opens}-{closes}" not in per_day.setdefault(day, []):
                per_day[day].append(f"{opens}-{closes}")
    if not per_day:
        return ""
    rules, i = [], 0                              # runs of days with the same hours: "Mo-Fr 09:00-17:00"
    while i < 7:
        d = _WEEK[i]
        if d not in per_day:
            i += 1
            continue
        j = i
        while j + 1 < 7 and per_day.get(_WEEK[j + 1]) == per_day[d]:
            j += 1
        days = d if j == i else f"{d}-{_WEEK[j]}"
        rules.append(f"{days} {','.join(per_day[d])}")
        i = j + 1
    return "; ".join(rules)[:200]


def _types(props: dict) -> list[str]:
    return [t.rsplit("/", 1)[-1] for t in props.get(RDF_TYPE, [])]


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Contiguous US + Alaska + Hawaii, generously — the country field is often missing.
def in_us(lat: float, lon: float) -> bool:
    return ((24.0 <= lat <= 49.6 and -125.0 <= lon <= -66.5) or (51.0 <= lat <= 71.5 and -170.0 <= lon <= -129.9)
            or (18.8 <= lat <= 22.4 and -160.5 <= lon <= -154.6))


_US_COUNTRY = re.compile(r"^(us|usa|u\.s\.a?\.?|united states( of america)?)$", re.I)


_STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
           "CT": "connecticut", "DE": "delaware", "DC": "district of columbia", "FL": "florida", "GA": "georgia",
           "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
           "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
           "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
           "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
           "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
           "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
           "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
           "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming"}
_STATE_NAMES = set(_STATES.values())
_ZIP = re.compile(r"^\d{5}(-\d{4})?$")


def us_state(region: str) -> bool:
    r = region.strip().rstrip(".")
    return r.upper() in _STATES or r.lower() in _STATE_NAMES


def businesses(page: str, subj: dict, cls: str) -> list[tuple]:
    """One page -> its US businesses of class `cls` (or a known subtype) that have a name
    and either a map point or a US postal address (state + ZIP; lat/lon left blank, for
    a ZIP-centroid fill at load time)."""
    out = []
    for sid, props in subj.items():
        types = _types(props)
        if not types or not any(t == cls or t in SUBTYPES for t in types):
            continue
        name = _first(props, "name")
        if not (2 <= len(name) <= 120):
            continue
        lat, lon = _num(_first(props, "latitude")), _num(_first(props, "longitude"))
        geo = subj.get(_first(props, "geo"), {})
        if (lat is None or lon is None) and geo:
            lat, lon = _num(_first(geo, "latitude")), _num(_first(geo, "longitude"))
        addr = subj.get(_first(props, "address"), {})
        country = _first(addr, "addressCountry")
        if country in subj:                           # addressCountry -> a Country node
            country = _first(subj[country], "name")
        if country and not _US_COUNTRY.match(country):
            continue
        region, postal = _first(addr, "addressRegion"), _first(addr, "postalCode")
        # The box alone takes in Montreal and Toronto: some US evidence in the address too.
        us = bool(country) or us_state(region) or (not region and bool(_ZIP.match(postal)))
        if not us:
            continue
        if lat is not None and lon is not None:
            if not in_us(lat, lon):
                continue
            key = f"{name.lower()}|{lat:.4f}|{lon:.4f}"
            lat, lon = round(lat, 6), round(lon, 6)
        elif us_state(region) and _ZIP.match(postal) and _first(addr, "streetAddress"):
            key = f"{name.lower()}|{postal[:5]}|{_first(addr, 'streetAddress').lower()}"
            lat = lon = ""
        else:
            continue
        url = _first(props, "url")
        website = url if url.startswith("http") else page
        typ = next((t for t in types if t != cls and t in SUBTYPES), cls)
        bid = hashlib.sha1(key.encode()).hexdigest()[:16]
        out.append((bid, name, typ, lat, lon, _first(addr, "streetAddress")[:120],
                    _first(addr, "addressLocality")[:80], region[:40], postal[:12],
                    _first(props, "telephone")[:40], website[:300], page[:300], hours_of(props, subj)))
    return out


# schema.org LocalBusiness subtypes worth recognising, mapped to the OSM tags the places
# tier searches by (search-api PLACE_KINDS). Anything else keeps 'schema=<Type>'.
KIND = {"Restaurant": "amenity=restaurant", "FastFoodRestaurant": "amenity=fast_food", "CafeOrCoffeeShop": "amenity=cafe",
        "BarOrPub": "amenity=bar", "Bakery": "shop=bakery", "Dentist": "amenity=dentist", "Pharmacy": "amenity=pharmacy",
        "GasStation": "amenity=fuel", "GroceryStore": "shop=supermarket", "HardwareStore": "shop=hardware",
        "ExerciseGym": "leisure=fitness_centre", "HealthClub": "leisure=fitness_centre", "AutoRepair": "shop=car_repair",
        "HairSalon": "shop=hairdresser", "BeautySalon": "shop=beauty", "Hotel": "tourism=hotel", "Motel": "tourism=motel",
        "Library": "amenity=library", "Bank": "amenity=bank", "Physician": "amenity=doctors", "Hospital": "amenity=hospital",
        "VeterinaryCare": "amenity=veterinary", "PetStore": "shop=pet", "BookStore": "shop=books", "ClothingStore": "shop=clothes",
        "ElectronicsStore": "shop=electronics", "FurnitureStore": "shop=furniture", "LiquorStore": "shop=alcohol",
        "Florist": "shop=florist", "MovieTheater": "amenity=cinema", "Store": "shop=yes", "AutoDealer": "shop=car",
        "ChildCare": "amenity=childcare", "Plumber": "craft=plumber", "Electrician": "craft=electrician"}
SUBTYPES = frozenset(KIND) | {"LocalBusiness"}


def kind_of(typ: str) -> str:
    return KIND.get(typ, f"schema={typ}")


def _stream(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, gzip.GzipFile(fileobj=r) as gz:
                for raw in gz:
                    yield raw.decode("utf-8", "replace")
            return
        except OSError as e:                          # a dropped connection: wait, retry the part
            if attempt == 3:
                raise
            log(f"{url}: {type(e).__name__}: {e} — retrying in {30 * (attempt + 1)} s")
            time.sleep(30 * (attempt + 1))


def cmd_businesses(cls: str, parts: int, out: str) -> None:
    t0 = time.time()
    seen, n_pages, failed = set(), 0, []
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for i in range(parts):
            url = f"{BASE}/{cls}/part_{i}.gz"
            got = 0
            try:
                for page, subj in pages(_stream(url)):
                    n_pages += 1
                    for row in businesses(page, subj, cls):
                        if row[0] not in seen:
                            seen.add(row[0])
                            f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ").replace("\x00", "")
                                               for x in row) + "\n")
                            got += 1
            except OSError as e:                      # four tries failed: skip the part, say so
                failed.append(i)
                log(f"{cls} part {i}: given up ({type(e).__name__}: {e}) — skipped")
            log(f"{cls} part {i + 1}/{parts}: +{got:,} US businesses ({len(seen):,} total, {n_pages:,} pages), "
                f"{(time.time() - t0) / 60:.1f} min")
    log(f"{cls}: {len(seen):,} US businesses from {n_pages:,} pages"
        + (f"; parts skipped: {failed}" if failed else ""))


def read_zips(path: str) -> dict[str, tuple[float, float]]:
    """Census Gazetteer ZCTA file (GEOID … INTPTLAT INTPTLONG, tab separated) -> ZIP ->
    (lat, lon) of its interior point."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        head = [h.strip() for h in f.readline().split("\t")]
        z, la, lo = head.index("GEOID"), head.index("INTPTLAT"), head.index("INTPTLONG")
        for line in f:
            p = [x.strip() for x in line.split("\t")]
            if len(p) > max(z, la, lo) and _num(p[la]) is not None and _num(p[lo]) is not None:
                out[p[z]] = (float(p[la]), float(p[lo]))
    return out


def own_site(url: str, name: str) -> bool:
    """Is this the business's OWN site? 82% of the WDC 'websites' are directory and
    delivery pages listing it (ubereats.com, 101dentist.com, a city business directory —
    measured 2026-09-24 on the 4.2M US rows); linking those is linking SEO spam. Own =
    the domain's name label is in the business name or the name in it ("Jimmy John's"
    -> jimmyjohns.com, "Argos Farm" -> argosfarm.com)."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower() if url.startswith("http") else ""
    except ValueError:                          # "http://[broken" — pages carry anything
        return False
    parts = host.split(".")
    if len(parts) < 2:
        return False
    lab = re.sub(r"[^a-z0-9]", "", parts[-2])
    t = re.sub(r"[^a-z0-9]", "", name.lower())
    return len(lab) >= 3 and bool(t) and (lab in t or t in lab or (len(t) >= 5 and t[:5] == lab[:5]))


def map_pin(lat: float, lon: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}"


def listing_hours(h: str) -> str:
    """schema.org's convention (as Google documents it), not OSM's: 00:00-00:00 is closed
    all day, 00:00-23:59 open all day. Put in the OSM shape the page reads."""
    h = re.sub(r"(?<![\d:])00:00-00:00(?![\d:])", "off", h)
    return re.sub(r"(?<![\d:])00:00-23:59(?![\d:])", "00:00-24:00", h)


def place_row(p: list[str], zips: dict) -> list | None:
    """One biz.tsv row -> the wdc temp-table row, placing an address-only business at
    its ZIP's centre; None when it cannot be put on the map."""
    if len(p) == 12:                            # extracts from before 2026-09-25 carry no hours
        p = p + [""]
    if len(p) != 13:
        return None
    p = [html.unescape(x.replace("\x00", "")) for x in p]   # NUL: Postgres refuses it; "&amp;" in names
    lat, lon = _num(p[3]), _num(p[4])
    if lat is None or lon is None:
        ll = zips.get(p[8][:5])
        if not ll:
            return None
        lat, lon = ll
    site = p[10] if own_site(p[10], p[1]) else map_pin(lat, lon)
    return [p[0], p[1], p[2], lat, lon, *p[5:10], site, p[11], listing_hours(p[12])[:200]]


def cmd_load_places(conn, path: str, zips_path: str | None = None) -> None:
    """biz.tsv.gz -> places rows 'wdc:<id>', replacing earlier WDC rows. A business that
    OpenStreetMap already has (similar name within ~150 m) is left to OSM."""
    t0 = time.time()
    zips = read_zips(zips_path) if zips_path else {}
    conn.execute("CREATE TEMP TABLE wdc (id text, name text, typ text, lat double precision, lon double precision, "
                 "street text, city text, region text, postal text, phone text, website text, page text, hours text)")
    n = skipped = 0
    with conn.cursor().copy("COPY wdc FROM STDIN") as cp:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                row = place_row(line.rstrip("\n").split("\t"), zips)
                if row is None:
                    skipped += 1
                    continue
                cp.write_row(row)
                n += 1
    kinds = " ".join(f"WHEN '{t}' THEN '{v}'" for t, v in KIND.items())
    # The duplicate check probes a small copy of the OpenStreetMap places only: probing
    # `places` itself after the DELETE below walked 4.2M dead WDC rows per dense area
    # (a reload ran 38 min before it was cancelled; the first load, into an empty set,
    # took 3.8).
    conn.execute("CREATE TEMP TABLE osm_places AS SELECT name, lat, lon FROM places WHERE osm NOT LIKE 'wdc:%'")
    conn.execute("CREATE INDEX ON osm_places (lat, lon)")
    conn.execute("ANALYZE osm_places")
    with conn.transaction():
        conn.execute("DELETE FROM places WHERE osm LIKE 'wdc:%'")
        k = conn.execute(f"""
            INSERT INTO places (osm, name, kind, brand, lat, lon, street, city, hours, website, phone)
            SELECT DISTINCT ON (w.id) 'wdc:' || w.id, w.name, CASE w.typ {kinds} ELSE 'schema=' || w.typ END,
                   '', w.lat, w.lon, w.street, concat_ws(', ', nullif(w.city, ''), nullif(w.region, '')), w.hours,
                   w.website, w.phone
            FROM wdc w
            WHERE NOT EXISTS (
                SELECT 1 FROM osm_places p
                WHERE p.lat BETWEEN w.lat - 0.0015 AND w.lat + 0.0015
                  AND p.lon BETWEEN w.lon - 0.002 AND w.lon + 0.002
                  AND similarity(lower(p.name), lower(w.name)) >= 0.5)
            ON CONFLICT (osm) DO NOTHING""").rowcount
        # A business OpenStreetMap already has is left to OSM — but when OSM has no hours
        # for it, the listing's hours fill the gap (Parx Casino, Bensalem: none in OSM).
        conn.execute("CREATE INDEX ON wdc (lat, lon)")
        filled = conn.execute("""
            UPDATE places p SET hours = w.hours
            FROM wdc w
            WHERE p.osm NOT LIKE 'wdc:%' AND p.hours = '' AND w.hours <> ''
              AND w.lat BETWEEN p.lat - 0.0015 AND p.lat + 0.0015
              AND w.lon BETWEEN p.lon - 0.002 AND p.lon + 0.002
              AND similarity(lower(p.name), lower(w.name)) >= 0.5""").rowcount
        log(f"{filled:,} OpenStreetMap places got their hours from a business listing")
    conn.execute("VACUUM ANALYZE places")        # the replaced WDC rows are 4M dead tuples otherwise
    log(f"{n:,} WDC businesses read ({skipped:,} with no map point or ZIP centre skipped), {k:,} added to places "
        f"(the rest are already in OpenStreetMap) in {(time.time() - t0) / 60:.1f} min")


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("businesses")
    b.add_argument("--class", dest="cls", required=True); b.add_argument("--parts", type=int, required=True)
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    cmd_businesses(a.cls, a.parts, a.out)


if __name__ == "__main__":
    main(sys.argv[1:])
