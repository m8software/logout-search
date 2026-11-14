#!/usr/bin/env python3
"""gov-states.json: which US state a government site belongs to, as a file of the release.

    gov_states.py build CURRENT-FULL.csv OUT.json [STATE-DOMAINS.tsv]   from CISA's .gov registry, plus a state's
                                                                         own sites beyond .gov (gov-state-domains.tsv)
    gov_states.py check OUT.json                         the shape the engine reads; the problems, exit 1

Source: the .gov registry CISA publishes (https://github.com/cisagov/dotgov-data, current-full.csv; a work of the
United States government, public domain). Columns: Domain name, Domain type, Agency, Organization, City, State, ...
A row is kept when its type is a state, county, city or district government and its State is a two-letter code.
Federal, tribal and interstate domains are left out: they carry the state of their office, not of whom they serve
(usa.gov is everyone's). state.<xx>.us addresses need no row: the engine reads the state from the address.

A state's government also lives beyond .gov (step 2): its legislature and courts on .us or .org, its parks,
licence and benefit portals on contracted commercial names (ctparks.com, idmedicaid.com, myflorida.com). Those come
from gov-state-domains.tsv beside this file (state code, domain, what it is), a list kept by hand and checked
against Clef's reading of each site's name and address; the engine's state rule, the importer's page selection and
the government grader all read the merged file, so a state's portal is the state's page everywhere at once.

The engine (serve/api/gov_state.py load) reads only {"domains": {"nj.gov": "NJ", ...}}. The other keys say where
the file came from. Standard library only: release_check.py (shipped with the update tools) runs check() on a box.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys

SOURCE = ("CISA .gov registry, current-full.csv (https://github.com/cisagov/dotgov-data; United States government work, "
          "public domain): state, county, city and district government domains")
LEFT_OUT = ("Federal", "Tribal", "Interstate")
_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)*\.gov$")
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$")          # a listed non-.gov site: any registrable name
EXTRA_NAME = "gov-state-domains.tsv"
_STATE = re.compile(r"^[A-Z]{2}$")


def read_extra(path: str) -> dict[str, str]:
    """gov-state-domains.tsv: STATE<TAB>domain<TAB>what it is; # comments and blank lines skipped. Bad rows raise."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not _STATE.match(parts[0].strip()) or not _HOSTNAME.match(parts[1].strip().lower()):
                raise ValueError(f"{path}:{n}: expected STATE<TAB>domain<TAB>what, got {line!r}")
            out[parts[1].strip().lower()] = parts[0].strip()
    return out


_CITY_OF = re.compile(r"^(city|town|township|borough|village|municipality)\s+of\s+(the\s+)?")
_CITY_TYPE = re.compile(r"\s+(township|borough|village|town|city|municipality)$")
_CITY_DEPT = re.compile(r"\s+(police department|police dept|police|fire department|public library|library|government|"
                        r"water department|utilities|courts?|clerk)(\s+.*)?$")
_COUNTY_NAME = re.compile(r"(?:^|\b)county of\s+([a-z][a-z .'-]+?)(?:\s+(?:government|board|commission|clerk).*)?$|"
                          r"(?:^|\b(?:the|of)\s+)?([a-z][a-z .'-]+?)\s+county\b")
_STATE_NAMES = None


def _strip_state(name: str) -> str:
    """"Borough of Alpine New Jersey" -> "Borough of Alpine": the state's name at the end is no part of the town's."""
    global _STATE_NAMES
    if _STATE_NAMES is None:
        _STATE_NAMES = sorted({"new jersey", "new york", "new mexico", "new hampshire", "north carolina", "south carolina",
                               "north dakota", "south dakota", "west virginia", "rhode island", "alabama", "alaska",
                               "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
                               "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
                               "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
                               "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon", "pennsylvania",
                               "tennessee", "texas", "utah", "vermont", "virginia", "washington", "wisconsin", "wyoming"},
                              key=len, reverse=True)
    for st in _STATE_NAMES:
        if name.endswith(" " + st):
            rest = name[: -len(st) - 1].strip(" ,")
            return name if rest.endswith(" of") or not rest else rest      # "City of New York" keeps New York
    return name


def local_names(kind: str, org: str) -> list[str]:
    """local_name, and for "Jersey City" / "Alexandria Township" also the name with its type word: the Census towns
    the engine reads call a city "Jersey City" and a township "Alexandria"."""
    first = local_name(kind, org)
    if not first:
        return []
    o = _strip_state(" ".join((org or "").lower().replace(",", " ").split()))
    whole = re.sub(r"[^a-z0-9]", "", _CITY_DEPT.sub("", o)) if kind != "County" and not _CITY_OF.match(o) else ""
    return [first] + ([whole] if whole and whole != first else [])


def local_name(kind: str, org: str) -> str | None:
    """A city or county .gov's place name from its organisation: "City of Absecon" -> "absecon",
    "Borough of Alpine New Jersey" -> "alpine", "Alexandria Township" -> "alexandria", "County of Essex" -> "essex",
    "Office of the Bergen County Clerk" -> "bergen". None when nothing is left."""
    o = _strip_state(" ".join((org or "").lower().replace(",", " ").split()))
    if kind == "County":
        m = _COUNTY_NAME.search(o)
        name = (m.group(1) or m.group(2)) if m else ""
        name = re.sub(r"^(office of the|office of|the)\s+", "", name or "")
    else:
        # "City of Atlantic City": after "City of" the rest is the name; "Alexandria Township": the type word goes
        name = _CITY_DEPT.sub("", o)
        name = _CITY_OF.sub("", name) if _CITY_OF.match(name) else _CITY_TYPE.sub("", name)
    key = re.sub(r"[^a-z0-9]", "", name)
    return key or None


def build_local(csv_path: str) -> dict:
    """{"city": {"NJ:absecon": [domains]}, "county": {"NJ:atlantic": [domains]}} from the registry's City and County
    rows: what the engine asks for when a searcher's nearest town is Absecon, Atlantic County."""
    out: dict[str, dict[str, list[str]]] = {"city": {}, "county": {}}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            kind, st = row["Domain type"], (row["State"] or "").strip().upper()
            if kind not in ("City", "County") or not _STATE.match(st):
                continue
            d = row["Domain name"].strip().lower()
            if not _DOMAIN.match(d):
                continue
            for name in local_names(kind, row.get("Organization name") or ""):
                out[kind.lower()].setdefault(f"{st}:{name}", []).append(d)
    for m in out.values():
        for k in m:
            m[k] = sorted(set(m[k]))
    return out


def build(csv_path: str, extra_path: str | None = None) -> dict:
    """The file's content from the registry: {"source", "source_sha256", "rows_read", "domains": {domain: STATE}},
    plus the listed sites beyond .gov when `extra_path` is given ("extra", "extra_sha256", "extra_domains")."""
    domains, rows = {}, 0
    with open(csv_path, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for need in ("Domain name", "Domain type", "State"):
            if need not in (r.fieldnames or ()):
                raise ValueError(f"{csv_path}: no column {need!r} (columns: {r.fieldnames})")
        for row in r:
            rows += 1
            if row["Domain type"].startswith(LEFT_OUT) or not _STATE.match((row["State"] or "").strip().upper()):
                continue
            d = row["Domain name"].strip().lower()
            if _DOMAIN.match(d):
                domains[d] = row["State"].strip().upper()
    if not domains:
        raise ValueError(f"{csv_path}: no state, county, city or district domain with a state — not the .gov registry?")
    data = {"source": SOURCE, "source_sha256": hashlib.sha256(open(csv_path, "rb").read()).hexdigest(),
            "rows_read": rows}
    if extra_path:
        extra = read_extra(extra_path)
        clash = {d: (domains[d], s) for d, s in extra.items() if d in domains and domains[d] != s}
        if clash:
            raise ValueError(f"{extra_path}: {len(clash)} domain(s) the registry gives to another state, e.g. {next(iter(clash.items()))}")
        domains.update(extra)
        data.update({"extra": "gov-state-domains.tsv: a state's own sites beyond .gov (legislature, courts, parks, licence "
                              "and benefit portals), kept by hand",
                     "extra_sha256": hashlib.sha256(open(extra_path, "rb").read()).hexdigest(), "extra_domains": len(extra)})
    data["domains"] = dict(sorted(domains.items()))
    data["local"] = build_local(csv_path)
    return data


def write(csv_path: str, out: str, extra_path: str | None = None) -> dict:
    data = build(csv_path, extra_path)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    return data


def check(path: str) -> list[str]:
    """What is wrong with a gov-states.json, as the engine would read it: [] when nothing is."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [f"not readable as JSON: {e}"]
    if not isinstance(data, dict) or not isinstance(data.get("domains"), dict):
        return ["no \"domains\" object"]
    doms = data["domains"]
    errs = []
    if not doms:
        errs.append("\"domains\" is empty")
    bad_d = [d for d in doms if not isinstance(d, str) or not _HOSTNAME.match(d)]
    if bad_d:
        errs.append(f"{len(bad_d)} domain(s) not a lower-case host name, e.g. {bad_d[0]!r}")
    beyond = [d for d in doms if isinstance(d, str) and _HOSTNAME.match(d) and not _DOMAIN.match(d)]
    if beyond and not data.get("extra_sha256"):
        errs.append(f"{len(beyond)} domain(s) beyond .gov without an \"extra\" list named, e.g. {beyond[0]!r}")
    bad_s = [d for d, s in doms.items() if not isinstance(s, str) or not _STATE.match(s)]
    if bad_s:
        errs.append(f"{len(bad_s)} value(s) not a two-letter state code, e.g. {doms[bad_s[0]]!r} for {bad_s[0]!r}")
    if not isinstance(data.get("source"), str) or "public domain" not in data["source"]:
        errs.append("no \"source\" line naming the public-domain registry")
    return errs


def main(argv: list[str]) -> int:
    if len(argv) in (3, 4) and argv[0] == "build":
        data = write(argv[1], argv[2], argv[3] if len(argv) == 4 else None)
        print(json.dumps({"stage": "gov-states", "out": argv[2], "domains": len(data["domains"]), "rows_read": data["rows_read"],
                          "states": len(set(data["domains"].values())), "source_sha256": data["source_sha256"],
                          "extra_domains": data.get("extra_domains", 0)}))
        return 0
    if len(argv) == 2 and argv[0] == "check":
        errs = check(argv[1])
        print(json.dumps({"stage": "gov-states-check", "path": argv[1], "ok": not errs, "errors": errs}))
        return 1 if errs else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
