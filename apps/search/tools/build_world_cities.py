#!/usr/bin/env python3
"""Build world_cities.tsv.gz from GeoNames: the cities outside the United States that a sun or moon question
may name ("sunset in london"). US towns come from the weather card's own town table and are left out here.

Inputs (https://download.geonames.org/export/dump/, Creative Commons Attribution 4.0; this script fetches nothing):
  --cities    cities15000.zip        every populated place of 15,000 people or more
  --countries countryInfo.txt        ISO code -> country name
  --admin1    admin1CodesASCII.txt   "GB.ENG" -> "England"
Output (default serve/api/world_cities.tsv.gz, shipped with the engine), one city a line, biggest first:
  name <tab> ascii name <tab> country code <tab> country <tab> first-level division <tab> lat <tab> lon
  <tab> population <tab> IANA time zone
Changed from GeoNames' file: US rows, places with no time zone and all other columns are dropped; positions are
rounded to 4 decimals. Rebuild when wanted (cities do not move); then run tests/unit/test-search-world-places.sh.
"""

import argparse
import gzip
import io
import os
import zipfile


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument("--cities", required=True)
    parser.add_argument("--countries", required=True)
    parser.add_argument("--admin1", required=True)
    parser.add_argument("--out", default=os.path.join(here, "..", "serve", "api", "world_cities.tsv.gz"))
    args = parser.parse_args()
    countries = {}
    with open(args.countries, encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("#"):
                cells = line.rstrip("\n").split("\t")
                countries[cells[0]] = cells[4]
    admin1 = {}
    with open(args.admin1, encoding="utf-8") as handle:
        for line in handle:
            cells = line.rstrip("\n").split("\t")
            admin1[cells[0]] = cells[2]
    rows = []
    with zipfile.ZipFile(args.cities) as archive, archive.open("cities15000.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            c = line.rstrip("\n").split("\t")
            code, zone = c[8], c[17]
            if code == "US" or not zone or code not in countries:
                continue
            rows.append((int(c[14] or 0), c[1], c[2], code, countries[code], admin1.get(f"{code}.{c[10]}", ""),
                         f"{float(c[4]):.4f}", f"{float(c[5]):.4f}", zone))
    rows.sort(key=lambda r: (-r[0], r[2], r[3]))
    text = "".join("\t".join((r[1], r[2], r[3], r[4], r[5], r[6], r[7], str(r[0]), r[8])) + "\n" for r in rows)
    with gzip.GzipFile(args.out, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(text.encode("utf-8"))
    print(f"wrote {args.out}: {len(rows)} cities, {os.path.getsize(args.out)} bytes gzip, "
          f"{len({r[3] for r in rows})} countries, {len({r[8] for r in rows})} time zones")


if __name__ == "__main__":
    main()
