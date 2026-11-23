#!/usr/bin/env python3
"""Build tides.json.gz from cached NOAA CO-OPS responses.

Inputs (all already fetched; this script does no network access):
  --stations  station list   (mdapi stations.json?type=tidepredictions)
  --offsets   the same list with offsets (stations.json?type=tidepredictions&expand=tidepredoffsets)
  --cache     folder holding harcon_<id>.json and datums_<id>.json for every harmonic station
  --exclude   optional [[id, name, reason], ...]: stations left out although their constants are usable
              (default tools/tide-exclude.json: the ones the all-station check against NOAA's own predictions failed).
              A left-out harmonic station that subordinate stations are computed from keeps its constants in the
              file under "reference_only": it is never offered as a station itself.
  --approximate  optional [[id, name, reason], ...]: stations kept although the same check found their times 6 to 13
              minutes from NOAA's (default tools/tide-approximate.json); the answer words their times as "about".
Output: a gzip JSON file read by tide_model.load() (default: serve/api/tides.json.gz, shipped with the engine).

The fetches, all from https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/ (no key; keep to 1 request a second: 2 a second got HTTP 403 on 2026-11-22):
  stations.json?type=tidepredictions                              -> --stations
  stations.json?type=tidepredictions&expand=tidepredoffsets       -> --offsets
  stations/<id>/harcon.json?units=english, stations/<id>/datums.json?units=english  for every type "R" station
                                                                  -> --cache/harcon_<id>.json, datums_<id>.json
The constants change rarely (a station is re-analysed every few years): rebuild once a year, then run
tests/unit/test-search-tide-model.sh.

Layout of the file:
  source, fetched, counts, excluded [[id, name, reason], ...], constituents [37 names], reference_only [ids],
  approximate [ids]
  harmonic    [[id, name, state, lat, lon, z0_ft, [amp0, phase0, amp1, phase1, ...37 pairs]], ...]
  subordinate [[id, name, state, lat, lon, reference_id, high_minutes, low_minutes,
                high_height, low_height, "R" ratio | "F" additive feet], ...]
Rounding: amplitude 3 decimals (feet), phase_GMT 1 decimal (degrees), Z0 3 decimals.  NOAA publishes exactly those
precisions, so the build reports the largest change rounding made (0 means lossless).
"""

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api"))
from tide_model import CONSTITUENT_NAMES  # noqa: E402


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def compact_number(value):
    return int(value) if float(value).is_integer() else value


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    parser.add_argument("--offsets", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--exclude", default=os.path.join(here, "tide-exclude.json"))
    parser.add_argument("--approximate", default=os.path.join(here, "tide-approximate.json"))
    parser.add_argument("--out", default=os.path.join(here, "..", "serve", "api", "tides.json.gz"))
    args = parser.parse_args()

    stations = {s["id"]: s for s in read_json(args.stations)["stations"]}
    offsets = {s["id"]: s["tidepredoffsets"] for s in read_json(args.offsets)["stations"] if s["type"] == "S"}
    known = set(CONSTITUENT_NAMES)
    left_out = {row[0]: row[2] for row in read_json(args.exclude)}
    excluded, harmonic, subordinate = [], [], []
    reasons = {}          # id -> reason, for stations that are not usable
    max_change = {"amplitude": 0.0, "phase": 0.0, "z0": 0.0}
    oldest = None

    for station_id, station in sorted(stations.items()):
        if station["type"] != "R":
            continue
        harcon_path = os.path.join(args.cache, f"harcon_{station_id}.json")
        datums_path = os.path.join(args.cache, f"datums_{station_id}.json")
        oldest_here = min(os.path.getmtime(harcon_path), os.path.getmtime(datums_path))
        oldest = oldest_here if oldest is None else min(oldest, oldest_here)
        constituents = (read_json(harcon_path) or {}).get("HarmonicConstituents") or []
        datums = {d["name"]: d["value"] for d in (read_json(datums_path) or {}).get("datums") or []}
        if not constituents:
            reasons[station_id] = "no harmonic constants"
        elif "MSL" not in datums or "MLLW" not in datums:
            reasons[station_id] = "no MSL/MLLW datum"
        elif len(constituents) > len(known) or any(c["name"] not in known for c in constituents):
            reasons[station_id] = f"{len(constituents)} constituents (more than the supported 37)"
        else:
            by_name = {c["name"]: c for c in constituents}
            flat = []
            for name in CONSTITUENT_NAMES:
                constituent = by_name.get(name)
                amplitude = constituent["amplitude"] if constituent else 0
                phase = constituent["phase_GMT"] if constituent else 0
                max_change["amplitude"] = max(max_change["amplitude"], abs(round(amplitude, 3) - amplitude))
                max_change["phase"] = max(max_change["phase"], abs(round(phase, 1) - phase))
                flat += [compact_number(round(amplitude, 3)), compact_number(round(phase, 1))]
            z0 = datums["MSL"] - datums["MLLW"]
            max_change["z0"] = max(max_change["z0"], abs(round(z0, 3) - z0))
            harmonic.append([station_id, station["name"], station["state"], round(station["lat"], 4),
                             round(station["lng"], 4), compact_number(round(z0, 3)), flat])

    usable = {row[0] for row in harmonic}
    needed = set()        # left-out harmonic stations that a kept subordinate station is computed from
    for station_id, station in sorted(stations.items()):
        if station["type"] != "S":
            continue
        reference = station.get("reference_id") or offsets.get(station_id, {}).get("refStationId")
        offset = offsets.get(station_id) or {}
        if "timeOffsetHighTide" not in offset:
            reasons[station_id] = "no offsets"
        elif reference not in stations:
            reasons[station_id] = f"reference {reference} not in station list"
        elif stations[reference]["type"] != "R":
            reasons[station_id] = f"reference {reference} is itself subordinate"
        elif station_id in left_out:
            reasons[station_id] = left_out[station_id]
        elif reference not in usable:
            reasons[station_id] = f"reference {reference} excluded ({reasons.get(reference)})"
        else:
            subordinate.append([station_id, station["name"], station["state"], round(station["lat"], 4),
                                round(station["lng"], 4), reference, offset["timeOffsetHighTide"],
                                offset["timeOffsetLowTide"], compact_number(offset["heightOffsetHighTide"]),
                                compact_number(offset["heightOffsetLowTide"]), offset["heightAdjustedType"]])
            if reference in left_out:
                needed.add(reference)
    for station_id in sorted(set(left_out) & usable):
        reasons[station_id] = left_out[station_id]
    harmonic = [row for row in harmonic if row[0] not in left_out or row[0] in needed]
    unknown = sorted(set(left_out) - set(stations))
    if unknown:
        sys.exit(f"--exclude names stations that are not in the station list: {unknown}")

    offered = ({row[0] for row in harmonic} - needed) | {row[0] for row in subordinate}
    approximate = sorted(row[0] for row in read_json(args.approximate))
    missing = sorted(set(approximate) - offered)
    if missing:
        sys.exit(f"--approximate names stations that are not offered: {missing}")
    for station_id, reason in sorted(reasons.items()):
        excluded.append([station_id, stations[station_id]["name"], reason])
    fetched = datetime.fromtimestamp(oldest, timezone.utc).strftime("%Y-%m-%d")
    data = {
        "source": "NOAA CO-OPS", "fetched": fetched,
        "counts": {"harmonic": len(harmonic) - len(needed), "subordinate": len(subordinate), "excluded": len(excluded),
                   "harmonic_in_list": sum(1 for s in stations.values() if s["type"] == "R"),
                   "subordinate_in_list": sum(1 for s in stations.values() if s["type"] == "S")},
        "excluded": excluded, "constituents": list(CONSTITUENT_NAMES), "harmonic": harmonic,
        "subordinate": subordinate, "reference_only": sorted(needed),
        "approximate": approximate,
    }
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    with gzip.GzipFile(args.out, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(text.encode("utf-8"))
    print(f"wrote {args.out}: {os.path.getsize(args.out)} bytes gzip, {len(text)} bytes JSON")
    print("counts", data["counts"], "largest rounding change", max_change)
    reason_counts = {}
    for _, _, reason in excluded:
        key = reason.split(" (")[0] if reason.startswith("reference") else reason
        key = "reference excluded or subordinate" if reason.startswith("reference") else key
        reason_counts[key] = reason_counts.get(key, 0) + 1
    print("excluded by reason", reason_counts)


if __name__ == "__main__":
    main()
