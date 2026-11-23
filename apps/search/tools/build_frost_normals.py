#!/usr/bin/env python3
"""Build frost_normals.tsv.gz (phase 3): when the last spring and first fall freeze come, for the weather stations
of the United States, from NOAA's 1991-2020 U.S. Climate Normals.

Source (public domain, NOAA National Centers for Environmental Information):
  https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/archive/
  us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz   (about 54 MB)
Fetched here into --work when it is not there already; nothing is fetched at run time.

NOAA's meaning (Normals Calculation Methodology, "Frost-Freeze Probability Dates"): ANN-TMIN-PRBLST-T32FPnn is the date
by which there is an nn percent chance that the last spring freeze (a low of 32 F or colder) is still to come; ANN-TMIN-
PRBFST-T32FPnn is the date by which there is an nn percent chance that the first fall freeze has come. Dates are
calendar days (no year). A station is kept when its 32 F dates at 50 percent are present and their completeness flag is
S (24 or more years of data) or R (10 or more, gaps filled from neighbours); P and E stations, and the special values
-9999 (too cold to compute, year-round frost risk, or too warm for a freeze), are left out.

Output, one station a line, tab separated:
  id, name, state, lat, lon, elevation (metres), then the dates MM-DD (empty when NOAA has none for that cell):
  last spring freeze at 10, 50, 90 percent for 32 F; first fall freeze at 10, 50, 90 percent for 32 F;
  last and first at 50 percent for 28 F; growing season length in days at 50 percent for 32 F.
Usage: build_frost_normals.py --work DIR [--out FILE]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import io
import os
import re
import tarfile
import urllib.request

URL = ("https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/archive/"
       "us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz")
OK_FLAGS = ("S", "R")
CELLS = [("PRBLST", 32, 10), ("PRBLST", 32, 50), ("PRBLST", 32, 90), ("PRBFST", 32, 10), ("PRBFST", 32, 50), ("PRBFST", 32, 90),
         ("PRBLST", 28, 50), ("PRBFST", 28, 50)]


def cell(row: dict, kind: str, temp: int, prob: int) -> str:
    key = f"ANN-TMIN-{kind}-T{temp}FP{prob}"
    v = (row.get(key) or "").strip()
    if not re.fullmatch(r"\d\d/\d\d", v) or (row.get("meas_flag_" + key) or "").strip() or (row.get("comp_flag_" + key) or "").strip() not in OK_FLAGS:
        return ""
    return v.replace("/", "-")


def season_day(mmdd: str) -> int:
    """Days since August 1: NOAA counts the cold season from August 1, so a warm station's last freeze can come in
    December or January and its first in January."""
    month, day = int(mmdd[:2]), int(mmdd[3:5])
    return (datetime.date(2001, month, day).timetuple().tm_yday - 213) % 365


def station(row: dict):
    cells = [cell(row, *c) for c in CELLS]
    if not cells[1] or not cells[4]:
        return None
    last, first = cells[0:3], cells[3:6]
    days = lambda c: [season_day(x) for x in c if x]
    # the dates must be in the order NOAA's definitions give: the last freeze's 90, 50, 10 percent dates rise, so do the
    # first freeze's 10, 50, 90, and the first freeze of the season comes before its last
    if days([last[2], last[1], last[0]]) != sorted(days([last[2], last[1], last[0]])) or days(first) != sorted(days(first)) or season_day(cells[4]) >= season_day(cells[1]):
        return None
    name = (row["NAME"] or "").strip()
    m = re.fullmatch(r"(.+?),\s*([A-Z]{2}) US", name)
    if not m:
        return None
    gsl = (row.get("ANN-TMIN-PRBGSL-T32FP50") or "").strip()
    flag = (row.get("meas_flag_ANN-TMIN-PRBGSL-T32FP50") or "").strip() or (row.get("comp_flag_ANN-TMIN-PRBGSL-T32FP50") or "").strip() not in OK_FLAGS
    days = str(int(float(gsl))) if re.fullmatch(r"\d+(\.\d+)?", gsl) and not flag else ""
    return [row["STATION"].strip(), m.group(1).strip().title(), m.group(2), f"{float(row['LATITUDE']):.4f}", f"{float(row['LONGITUDE']):.4f}",
            f"{float(row['ELEVATION']):.0f}"] + cells + [days]


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", default=os.path.join(here, "..", "serve", "api", "frost_normals.tsv.gz"))
    a = ap.parse_args()
    tar = os.path.join(a.work, os.path.basename(URL))
    if not os.path.exists(tar):
        os.makedirs(a.work, exist_ok=True)
        req = urllib.request.Request(URL, headers={"User-Agent": "almanac-table-build"})
        with urllib.request.urlopen(req, timeout=600) as r, open(tar, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    rows, seen = [], 0
    with tarfile.open(tar, "r:gz") as t:
        for member in t:
            if not member.isfile() or not member.name.endswith(".csv"):
                continue
            seen += 1
            data = t.extractfile(member).read().decode("utf-8", "replace")
            for row in csv.DictReader(io.StringIO(data)):
                s = station(row)
                if s:
                    rows.append(s)
    rows.sort(key=lambda r: r[0])
    text = "".join("\t".join(r) + "\n" for r in rows)
    with gzip.GzipFile(a.out, "wb", compresslevel=9, mtime=0) as f:
        f.write(text.encode("utf-8"))
    print(f"wrote {a.out}: {len(rows)} stations of {seen} files, {os.path.getsize(a.out)} bytes gzip")


if __name__ == "__main__":
    main()
