"""Offline place search from OpenStreetMap (2026-09-24).

About one web search in six is for a local place (a shop, a restaurant, a gym). This loads the named places of a Geofabrik extract (New
Jersey by default) into `places`; search-api answers those queries with the nearest
matches to a town-level home point, so map searches no longer leave the LAN.

ODbL: fine for personal use; shipping it needs attribution + share-alike.
"""

from __future__ import annotations

import os
import re
import time
import urllib.request

import osmium

# Which region's map to load: Geofabrik paths, comma-separated ("europe/monaco"). No default: it is the
# region the box is in, and only whoever builds the index knows that.
EXTRACT = os.environ.get("OSM_EXTRACT", "")
DIR = "/osm"
USER_AGENT = "logout-search-import/1 (personal search index)"

# The keys whose values say what a place IS. First match wins (a pharmacy is a shop
# and an amenity in some data; amenity is the more specific one here).
KIND_KEYS = ("amenity", "shop", "leisure", "tourism", "healthcare", "office", "craft", "club", "sport")
# Amenities that are infrastructure, not somewhere you would search for by name.
SKIP = {("amenity", v) for v in ("parking", "parking_space", "bench", "waste_basket", "bicycle_parking",
                                 "vending_machine", "telephone", "post_box", "recycling", "toilets",
                                 "drinking_water", "shelter", "fountain", "hunting_stand", "waste_disposal",
                                 "parking_entrance", "grit_bin", "clock", "atm")}


def log(msg: str) -> None:
    print(f"[osmplaces] {msg}", flush=True)


def kind_of(tags) -> str | None:
    """OSM tags -> 'shop=supermarket', or None for something nobody searches for."""
    for k in KIND_KEYS:
        v = tags.get(k)
        if v and (k, v) not in SKIP:
            return f"{k}={v}"
    return None


_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def norm_date(v: str | None) -> str:
    """'2023-03-31' / '2023-03' / '2023' -> ISO day (missing parts = 01); anything else ''."""
    m = _DATE.match((v or "").strip())
    if not m or not (1990 <= int(m.group(1)) <= 2100):
        return ""
    return f"{m.group(1)}-{m.group(2) or '01'}-{m.group(3) or '01'}"


def hours_checked_of(tags) -> str:
    """When a mapper last confirmed the hours (2026-10-02): the newest of the check tags."""
    return max((norm_date(tags.get(k)) for k in ("check_date:opening_hours", "check_date", "survey:date")), default="")


def row_of(osm_type: str, osm_id: int, tags, lat: float, lon: float, edited: str = "") -> tuple | None:
    name = tags.get("name") or tags.get("brand")
    kind = kind_of(tags)
    if not name or not kind:
        return None
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    return (f"{osm_type}/{osm_id}", name, kind, tags.get("brand") or "", round(lat, 6), round(lon, 6),
            street, tags.get("addr:city") or "", tags.get("opening_hours") or "",
            tags.get("website") or tags.get("contact:website") or "", tags.get("phone") or tags.get("contact:phone") or "",
            hours_checked_of(tags) if tags.get("opening_hours") else "", edited if tags.get("opening_hours") else "")


def _day(o) -> str:
    """An element's last edit as YYYY-MM-DD ('' when the extract carries no timestamps)."""
    try:
        return o.timestamp.strftime("%Y-%m-%d") if o.timestamp.year > 1970 else ""
    except (AttributeError, ValueError):
        return ""


class _Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def node(self, n):
        if n.tags and n.location.valid():
            r = row_of("node", n.id, n.tags, n.location.lat, n.location.lon, _day(n))
            if r:
                self.rows.append(r)

    def way(self, w):
        if not w.tags or "name" not in w.tags and "brand" not in w.tags:
            return
        pts = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        if pts:
            r = row_of("way", w.id, w.tags, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts), _day(w))
            if r:
                self.rows.append(r)


# Several extracts ("europe/monaco,europe/andorra"): a place searched by name is found up to
# 200 km away (search_api.PLACE_EXACT_KM), which usually reaches the regions next to the box's
# own, so name those too. Their opening_hours are the hours on the card.
def extracts(spec: str | None = None) -> list[str]:
    found = [e.strip().strip("/") for e in (spec or EXTRACT).split(",") if e.strip()]
    if not found:
        raise SystemExit("places: no map region named — set OSM_EXTRACT or pass --extracts with "
                         "comma-separated Geofabrik paths, for example europe/monaco")
    return found


def download(extract: str) -> str:
    os.makedirs(DIR, exist_ok=True)
    dest = os.path.join(DIR, extract.replace("/", "_") + "-latest.osm.pbf")
    if not os.path.exists(dest):
        url = f"https://download.geofabrik.de/{extract}-latest.osm.pbf"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(dest + ".part", dest)
    return dest


COLS = "osm, name, kind, brand, lat, lon, street, city, hours, website, phone, hours_checked, edited"
# What older tables (and the Overture rows carried over) have — no dates.
COLS_BASE = "osm, name, kind, brand, lat, lon, street, city, hours, website, phone"


def cmd_places(conn, spec: str | None = None) -> None:
    """Build places_new from the extracts, carry the Overture rows (ov:*, 2026-10-02) over
    from places, and swap it in — one transaction, so a serve snapshot taken meanwhile
    sees the old table or the new one, never half of it."""
    t0 = time.time()
    rows: dict[str, tuple] = {}
    for ex in extracts(spec):
        path = download(ex)                     # one at a time: politeness, and memory
        h = _Handler()
        h.apply_file(path, locations=True)      # way centroids need node locations
        for r in h.rows:
            rows.setdefault(r[0], r)            # a place on a state border is in both extracts
        log(f"{len(h.rows):,} named places from {path} ({time.time() - t0:.0f}s)")
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS places_new")
        conn.execute("CREATE TABLE places_new (osm text PRIMARY KEY, name text, kind text, brand text, "
                     "lat double precision, lon double precision, street text, city text, hours text, "
                     "website text, phone text, hours_checked text NOT NULL DEFAULT '', edited text NOT NULL DEFAULT '')")
        with conn.cursor().copy(f"COPY places_new ({COLS}) FROM STDIN") as cp:
            for r in rows.values():
                cp.write_row(r)
        if conn.execute("SELECT to_regclass('places') IS NOT NULL").fetchone()[0]:
            # Overture's open places (utility/places/overture_import.py). Web Data Commons rows
            # (wdc:*) are research-only and are NOT carried over any more (2026-10-02).
            k = conn.execute(f"INSERT INTO places_new ({COLS_BASE}) SELECT {COLS_BASE} FROM places "
                             "WHERE osm LIKE 'ov:%' ON CONFLICT DO NOTHING").rowcount
            log(f"{k:,} Overture places carried over")
        conn.execute("CREATE INDEX places_new_name_trgm ON places_new USING gin (lower(name) gin_trgm_ops)")
        # exact names ("parx casino"): a trigram index answers equality in 40-600 ms, this in <1
        conn.execute("CREATE INDEX places_new_name_lower ON places_new (lower(name))")
        # "dunkin" / "home depot": letters-only names and brands (Dunkin', The Home Depot)
        conn.execute("CREATE INDEX places_new_name_key ON places_new ((regexp_replace(lower(name), '[^a-z0-9]', '', 'g')))")
        conn.execute("CREATE INDEX places_new_brand_key ON places_new ((regexp_replace(lower(brand), '[^a-z0-9]', '', 'g'))) "
                     "WHERE brand <> ''")
        conn.execute("CREATE INDEX places_new_kind ON places_new (kind)")
        conn.execute("CREATE INDEX places_new_latlon ON places_new (lat, lon)")
        conn.execute("DROP TABLE IF EXISTS places")
        conn.execute("ALTER TABLE places_new RENAME TO places")
        for old, new in (("places_new_pkey", "places_pkey"), ("places_new_name_trgm", "places_name_trgm"),
                         ("places_new_name_lower", "places_name_lower"), ("places_new_kind", "places_kind"),
                         ("places_new_latlon", "places_latlon"), ("places_new_name_key", "places_name_key"),
                         ("places_new_brand_key", "places_brand_key")):
            conn.execute(f"ALTER INDEX {old} RENAME TO {new}")
    n = conn.execute("SELECT count(*) FROM places").fetchone()[0]
    log(f"places ready: {n:,} in {(time.time() - t0) / 60:.1f} min")
