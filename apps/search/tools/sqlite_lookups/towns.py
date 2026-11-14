#!/usr/bin/env python3
"""Where each town is, from the addresses of the places in it.

    towns.py FILE.sqlite        add the rows to a places file that has none (a dev copy: the file's checksum changes)

Under half of OpenStreetMap's places carry a city in their address (2026-10-03 release: 44%), so "sun spa cedar
mills" finds Sun Spa only if its own address says Cedar Mills. The places that do say it show where the town is:
for each normalised city name the places are grouped (a new group when a place is more than SEED_KM from the first
of every group so far, places taken in (lat, lon) order), and each group of MIN_PLACES or more becomes one row:
the median point, and the distance from it that holds PCT of the group, kept between R_MIN and R_MAX km.
A place with no city is then "in" the town when it lies inside such a circle (lookups_release.place_town).

Measured on the 2026-10-03 US files (20,000 places with a city: is the place inside a circle of its own town;
20,000 without: is it inside any): 91.5% and 86.5%, with 1.6 towns claiming a no-city place on average — a
neighbouring town's circle overlaps. PCT 0.95 / 2-15 km gave 95.7% and 90.5% with 2.1 towns; 0.75: 78.5%, 73.5%, 1.0.

The rows go in `meta` (key `town:<city key>`, value `lat,lon,r_km,n;…`), not in a table, so the file's schema is
unchanged and a monthly update carries them as row changes (as `pid_hwm:` does). Each file's rows come from that
file's own places only: one licence per file stays true (ODbL for OpenStreetMap, CDLA for Overture).
"""
import math, re, sqlite3, statistics, sys

SEED_KM, MIN_PLACES, PCT, R_MIN, R_MAX = 15.0, 3, 0.9, 1.5, 12.0
PREFIX = "town:"


def city_key(city: str) -> str:
    """search_api.PLACE_TOWN_SQL's key: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", city.lower())


def d_km(lat, lon, hlat, hlon):
    return 111.32 * math.sqrt(math.pow(lat - hlat, 2) + math.pow((lon - hlon) * math.cos(math.radians(hlat)), 2))


def clusters(rows) -> dict:
    """(city, lat, lon) rows -> {city key: [(lat, lon, r_km, n), …]}, the biggest group first."""
    pts: dict = {}
    for city, lat, lon in rows:
        k = city_key(city or "")
        if len(k) >= 3 and lat is not None and lon is not None:
            pts.setdefault(k, []).append((lat, lon))
    out = {}
    for k in sorted(pts):
        groups: list = []
        for p in sorted(pts[k]):
            g = next((g for g in groups if d_km(p[0], p[1], g[0][0], g[0][1]) <= SEED_KM), None)
            (g.append(p) if g is not None else groups.append([p]))
        cs = []
        for g in groups:
            if len(g) < MIN_PLACES:
                continue
            lat, lon = statistics.median(p[0] for p in g), statistics.median(p[1] for p in g)
            ds = sorted(d_km(p[0], p[1], lat, lon) for p in g)
            r = min(R_MAX, max(R_MIN, ds[min(len(ds) - 1, int(PCT * len(ds)))]))
            cs.append((round(lat, 5), round(lon, 5), round(r, 2), len(g)))
        if cs:
            out[k] = sorted(cs, key=lambda c: (-c[3], c[0], c[1]))
    return out


def meta_rows(cl: dict) -> list:
    return [(PREFIX + k, ";".join(f"{la:.5f},{lo:.5f},{r:.2f},{n}" for la, lo, r, n in cs)) for k, cs in sorted(cl.items())]


def parse(value: str) -> list:
    """A meta value -> [(lat, lon, r_km, n), …]."""
    return [(float(a), float(b), float(c), int(d)) for a, b, c, d in (x.split(",") for x in value.split(";") if x)]


def write(db: sqlite3.Connection, schema: str = "main") -> int:
    """Replace the town rows of an attached places file from its own places; -> rows written."""
    cl = clusters(db.execute(f"SELECT city, lat, lon FROM {schema}.places WHERE city <> ''"))
    db.execute(f"DELETE FROM {schema}.meta WHERE key GLOB '{PREFIX}*'")
    rows = meta_rows(cl)
    db.executemany(f"INSERT INTO {schema}.meta (key, value) VALUES (?, ?)", rows)
    return len(rows)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    db = sqlite3.connect(sys.argv[1])
    role = db.execute("SELECT value FROM meta WHERE key = 'role'").fetchone()
    if not role or not role[0].startswith("places-"):
        sys.exit(f"{sys.argv[1]}: role {role and role[0]!r} is not a places file — refusing")
    n = write(db)
    db.commit()
    print(f"{sys.argv[1]}: {n} towns")


if __name__ == "__main__":
    main()
