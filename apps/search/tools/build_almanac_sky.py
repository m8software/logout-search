#!/usr/bin/env python3
"""Build almanac_sky.json.gz (phase 3): the eclipse table and the meteor-shower calendars the almanac answers from.

Sources, fetched here at build time into --work (nothing is fetched at run time; a file already in --work is reused):
  Eclipses  NASA GSFC, Five Millennium Catalogs of Solar and Lunar Eclipses (Espenak and Meeus), eclipse.gsfc.nasa.gov
            SEcat5/SE2001-2100.html, LEcat5/LE2001-2100.html  (date, time of greatest eclipse in TD, delta T, type, gamma,
            magnitude, durations, where the greatest eclipse is) and the decade pages SEdecade/SEdecadeYYYY.html,
            LEdecade/LEdecadeYYYY.html (where NASA says each eclipse is seen, and the countries of a central path). NASA grants the data for reuse with
            the credit "Eclipse Predictions by Fred Espenak (NASA's GSFC)".
  Showers   International Meteor Organization, Meteor Shower Calendar YYYY, Table 5 "Working List of Visual Meteor
            Showers" (imo.net/files/meteor-shower/calYYYY.pdf, read with pdftotext). Only the facts are kept: names,
            activity dates, the date of the maximum, the solar longitude, radiant, speed, ZHR. None of the text.
Output (default serve/api/almanac_sky.json.gz): {"eclipses": [...], "showers": {"2027": [...], ...}, "source": {...}}.
Rebuild every December (the IMO publishes the next year's calendar then; a year with no calendar has no shower dates),
then run tests/unit/test-search-almanac-sky.sh.

Usage: build_almanac_sky.py --work DIR [--first 2024 --last 2050] [--years 2026 2027] [--out FILE]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

NASA = "https://eclipse.gsfc.nasa.gov/"
IMO = "https://www.imo.net/files/meteor-shower/"
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
UA = "Mozilla/5.0 (compatible; almanac-table-build; +build-time only)"


def fetch(url: str, path: str) -> str:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=90) as r, open(path, "wb") as f:
            f.write(r.read())
        time.sleep(1.0)
    return path


def text_of(path: str) -> str:
    raw = open(path, encoding="utf-8", errors="replace").read()
    return html.unescape(re.sub(r"<[^>]*>", "", raw))


# ---------------------------------------------------------------------------------------------- catalogs
_DATE = r"(?P<y>\d{4}) (?P<mon>[A-Z][a-z]{2}) (?P<d>\d\d)\s+(?P<td>\d\d:\d\d:\d\d)\s+(?P<dtt>-?\d+)"
SOLAR_ROW = re.compile(r"^\s*(?P<cat>\d{5})\s+" + _DATE + r"\s+(?P<lun>-?\d+)\s+(?P<saros>\d+)\s+(?P<type>[TAHP][a-z0-9+\-]?)\s+\S\S\s+"
                       r"(?P<gamma>-?\d+\.\d+)\s+(?P<mag>\d+\.\d+)\s+(?P<lat>\d+[NS])\s+(?P<lon>\d+[EW])\s+(?P<alt>\d+)"
                       r"(?:\s+(?P<width>\d+|-)\s+(?P<dur>\d\dm\d\ds))?\s*$")
LUNAR_ROW = re.compile(r"^\s*(?P<cat>\d{5})\s+" + _DATE + r"\s+(?P<lun>-?\d+)\s+(?P<saros>\d+)\s+(?P<type>[NPT][a-z+\-]?)\s+\S\S\s+"
                       r"(?P<gamma>-?\d+\.\d+)\s+(?P<pen>-?\d+\.\d+)\s+(?P<um>-?\d+\.\d+)\s+(?P<penm>\d+\.\d)(?:\s+(?P<parm>\d+\.\d|-))?(?:\s+(?P<totm>\d+\.\d|-))?\s+"
                       r"(?P<lat>\d+[NS])\s+(?P<lon>\d+[EW])\s*$")


def catalog(path: str, solar: bool) -> dict[str, dict]:
    out = {}
    rx = SOLAR_ROW if solar else LUNAR_ROW
    for line in text_of(path).splitlines():
        m = rx.match(line)
        if not m:
            continue
        g = m.groupdict()
        day = f"{g['y']}-{MONTHS[g['mon']]:02d}-{g['d']}"
        row = {"k": "S" if solar else "L", "d": day, "td": g["td"], "dt": int(g["dtt"]), "t": g["type"][0], "saros": int(g["saros"]),
               "gamma": float(g["gamma"]), "mag": float(g["mag"] if solar else g["um"]), "at": f"{g['lat']} {g['lon']}"}
        if solar:
            if g["width"] and g["width"] != "-":
                row["width"] = int(g["width"])
            if g["dur"]:
                row["dur"] = g["dur"]
        else:
            row.update(pen=float(g["pen"]), pdur=float(g["penm"]))
            if g["parm"] and g["parm"] != "-":
                row["par"] = float(g["parm"])
            if g["totm"] and g["totm"] != "-":
                row["tot"] = float(g["totm"])
        out[day] = row
    return out


def decade(path: str) -> dict[str, tuple[str, str]]:
    """{date: (TD time, region text)} from a NASA decade page (one table row per eclipse)."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S | re.I):
        cells = [html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", c))).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        m = re.match(r"^(\d{4}) ([A-Z][a-z]{2}) (\d\d)$", cells[0]) if cells else None
        if not m or len(cells) < 6:
            continue
        out[f"{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"] = (cells[1], cells[-1])
    return out


def region(text: str) -> tuple[str, str]:
    """(where the eclipse is seen, the countries or regions of the central path) as NASA writes them: its short hand
    ("n N. America, w Africa") is kept, not expanded, so what the box says is what NASA's page says. The path is the
    bracketed part of a total, annular or hybrid eclipse's row."""
    m = re.search(r"\[[^:\]]*:\s*([^\]]*)\]", text)
    main = re.sub(r"\[[^\]]*\]", "", text)
    clean = lambda t: re.sub(r"\s+", " ", re.sub(r"(,\s*)+", ", ", t)).strip(" ,")
    return clean(main), clean(m.group(1)) if m else ""


def _secs(t: str) -> int:
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s


def eclipses(work: str, first: int, last: int) -> list[dict]:
    se = catalog(fetch(NASA + "SEcat5/SE2001-2100.html", f"{work}/se/SE2001-2100.html"), True)
    le = catalog(fetch(NASA + "LEcat5/LE2001-2100.html", f"{work}/le/LE2001-2100.html"), False)
    reg = {}
    for y in sorted({(y // 10) * 10 + 1 for y in range(first, last + 1)}):
        for kind, folder in (("S", "SEdecade/SEdecade"), ("L", "LEdecade/LEdecade")):
            for d, (td, text) in decade(fetch(f"{NASA}{folder}{y}.html", f"{work}/dec/{folder.split('/')[1]}{y}.html")).items():
                reg[(kind, d)] = (td, text)
    out = []
    for cat in (se, le):
        for day, row in sorted(cat.items()):
            if not first <= int(day[:4]) <= last:
                continue
            td, text = reg.get((row["k"], day), (None, None))
            if text is None or td is None:
                sys.exit(f"no decade-page row for {row['k']} {day}")
            if (_secs(td) - _secs(row["td"]) + 43200) % 86400 - 43200 not in (-1, 0, 1):
                sys.exit(f"catalog and decade page disagree on the time of {row['k']} {day}: {row['td']} vs {td}")
            row["region"], path = region(text)
            if path:
                row["path"] = path
            out.append(row)
    out.sort(key=lambda r: (r["d"], r["td"]))
    return out


# ---------------------------------------------------------------------------------------------- IMO working list
_LAM = re.compile(r"(\d+)\s*◦(?:\.\s*(\d+))?")
_ROW = re.compile(r"^\s*(?P<name>\S.*?)\s*\((?P<num>\d{3}) (?P<code>[A-Z]{3})\)\s+(?P<a0>[A-Z][a-z]{2} \d\d)\s*[–-]\s*(?P<a1>[A-Z][a-z]{2} \d\d)\s+"
                  r"\(?(?P<max>[A-Z][a-z]{2} \d\d)\)?\s+(?P<lam>\S.*?)\s+(?P<ra>\d+)\s*◦\s+(?P<dec>[+−-]\s*\d+)\s*◦\s+(?P<v>\d+)\s+(?P<r>\d\.\d)\s+(?P<zhr>\S+)\s*$")
_NAMES = {"Dayt.": "Daytime", "S.": "Southern", "N.": "Northern", "Sep.": "September", "Oct.": "October", "Nov.": "November",
          "Camelopard.": "Camelopardalids"}
_GREEK = {"α": "Alpha", "γ": "Gamma", "δ": "Delta", "ε": "Epsilon", "ζ": "Zeta", "η": "Eta", "ι": "Iota", "κ": "Kappa", "π": "Pi", "σ": "Sigma"}


def pretty(name: str) -> str:
    """The IMO's name with its Greek letter spelled ("η-Aquariids" -> "Eta Aquariids"): a search says "eta aquariids"."""
    for g, w in _GREEK.items():
        name = re.sub(rf"{g}-(?=[A-Z])", w + " ", name)
    return " ".join(_NAMES.get(w, w) for w in name.split())


def showers(work: str, year: int) -> list[dict]:
    pdf = fetch(f"{IMO}cal{year}.pdf", f"{work}/imo/cal{year}.pdf")
    txt = subprocess.run(["pdftotext", "-layout", pdf, "-"], check=True, capture_output=True, text=True).stdout
    start = txt.index("Table 5. Working List")
    end = txt.index("Table 6 (next page)", start)
    out = []
    for line in txt[start:end].splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        g = m.groupdict()
        lam = _LAM.match(g["lam"].strip("() "))
        name = pretty(g["name"])
        mx = g["max"]
        out.append({"code": g["code"], "num": g["num"], "name": name, "from": _md(g["a0"]), "to": _md(g["a1"]), "max": _md(mx),
                    "lam": float(lam.group(1) + "." + (lam.group(2) or "0")) if lam else None,
                    "ra": int(g["ra"]), "dec": int(g["dec"].replace("−", "-").replace(" ", "")), "v": int(g["v"]), "r": float(g["r"]),
                    "zhr": g["zhr"], "ref_only": "(" + mx in line})
    if len(out) < 35:
        sys.exit(f"IMO {year}: only {len(out)} showers parsed from Table 5")
    return out


def _md(s: str) -> str:
    return f"{MONTHS[s[:3]]:02d}-{s[4:6]}"


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="download folder (kept between runs)")
    ap.add_argument("--first", type=int, default=2024)
    ap.add_argument("--last", type=int, default=2050)
    ap.add_argument("--years", type=int, nargs="+", default=[2026, 2027])
    ap.add_argument("--out", default=os.path.join(here, "..", "serve", "api", "almanac_sky.json.gz"))
    a = ap.parse_args()
    ecl = eclipses(a.work, a.first, a.last)
    sh = {str(y): showers(a.work, y) for y in a.years}
    data = {"source": {"eclipses": "NASA GSFC Five Millennium Catalogs of Solar and Lunar Eclipses (Espenak and Meeus)",
                       "showers": "International Meteor Organization Meteor Shower Calendar, Working List of Visual Meteor Showers"},
            "eclipses": ecl, "showers": sh}
    with gzip.GzipFile(a.out, "wb", compresslevel=9, mtime=0) as f:
        f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    print(f"wrote {a.out}: {sum(r['k'] == 'S' for r in ecl)} solar and {sum(r['k'] == 'L' for r in ecl)} lunar eclipses {a.first}-{a.last}, "
          f"showers {', '.join(f'{y}: {len(v)}' for y, v in sh.items())}, {os.path.getsize(a.out)} bytes gzip")


if __name__ == "__main__":
    main()
