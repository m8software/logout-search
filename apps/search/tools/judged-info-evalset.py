#!/usr/bin/env python3
"""The judged informational set: ~1,000 informational queries from open sources, written by FIXED
TEMPLATES — no LLM writes a query, no browsing history (ADR-064/065). Clef 27B judges the engine's
answers once (judged-info-judge.py) and the verdicts are frozen beside the set.

    tools/judged-info-evalset.py [--out tools/evalsets/open] [--per 90] [--pool 300] [--month 2026-09]
                                 [--gov PATH] [--probe]

Where the queries come from:

  Wikidata (CC0) through QLever, Wikipedia pageviews (CC0) through the Wikimedia REST API — the head-brand set's
  helpers. For each template a class of items with an English article and the property the question asks for
  is pulled ordered by Wikidata sitelinks (--pool most-linked); their --month pageviews are fetched and the --per
  most viewed make the rows, so the set leans to the head like head-brand does. The article TITLE is the slot:

    wrote      literary works with an author (P50)               "who wrote <title>"
    capital    sovereign states with a capital (P36)             "capital of <title>"
    founded    organisations with an inception (P571)           "when was <title> founded"
    does       the same organisations, the other half by entity  "what does <title> do"
    directed   films with a director (P57)                       "who directed <title>"
    what-is    disciplines, diseases, compounds, quantities, concepts, ideologies, religions, languages
                                                                 "what is <title>"
    how-does   processes, phenomena, algorithms, technologies    "how does <title> work"

  A title with a "(...)" qualifier is dropped (the bare name is ambiguous by construction); so is one over six
  words, and a name two pooled articles share. No answer is stored: the judge decides what is right.

  descriptive   evalsets/descriptive-sites.txt — hand-written "find me a site of this kind" queries (2026-09-28)
  control       evalsets/descriptive-controls.txt — hand-written "what is / how does" queries about kinds of sites
  gov           the government task list (apps/search/tools/gov-eval/gov-services.tsv, branch feat/search-gov-pages:
                20 tasks x 50 states x 2 forms by fixed templates); --gov PATH, --gov-n rows sampled by md5

Split: open-evalsets.py's rule (md5, seed 402) on the ENTITY — the normalised article title for the Wikidata
templates, so an entity is on the same side as in every other open set; "gov:<state>-<task>" for gov rows. The two
hand-written lists keep their own file split (odd line dev, even line test) so a query tuned on there never becomes
test here. Output: judged-info.tsv — query, category (judged), expected (-), set, stratum (template:..., views-rank:...),
split, entity, source (the Wikidata item, or file:line) — and ambiguous-judged-info.tsv for the names dropped as shared.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("head_brand", TOOLS / "head-brand-evalset.py")
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)                               # qlever(), pageviews(), split_of(), norm_key(), plain_title()

SEED = "402"
SET = "judged-info"
PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
"""
# template -> (phrasing, the P31 classes (VALUES; no subclass walk: the walk pulls in states and people), the property the
# question asks for). Classes read on wikidata.org 2026-10-12.
TEMPLATES: dict[str, tuple[str, list[str], str]] = {
    "wrote": ("who wrote {t}", ["Q7725634", "Q571", "Q8261", "Q25379", "Q49084", "Q5185279", "Q1318295", "Q12106333"], "P50"),
    "capital": ("capital of {t}", ["Q6256", "Q3624078"], "P36"),
    "org": ("", ["Q4830453", "Q6881511", "Q783794", "Q3918", "Q163740", "Q484652", "Q7278", "Q476028", "Q327333",
                 "Q22687", "Q46970", "Q18127", "Q11032", "Q1137109", "Q2659904", "Q891723", "Q2085381",
                 "Q15911314", "Q1616075", "Q157031", "Q31855", "Q875538", "Q7210356", "Q1058914"], "P571"),
    "directed": ("who directed {t}", ["Q11424", "Q24869", "Q202866"], "P57"),
    "what-is": ("what is {t}", ["Q11862829", "Q2465832", "Q12136", "Q11173", "Q107715", "Q24034552", "Q29028649",
                                "Q33104279", "Q2135465", "Q12909644", "Q9174", "Q9143", "Q11344", "Q1047113",
                                "Q7748", "Q17444909", "Q16889133", "Q1799072", "Q3505845", "Q211606"], ""),
    "how-does": ("how does {t} work", ["Q2996394", "Q8366", "Q11016", "Q17320256", "Q2281940", "Q1322005",
                                       "Q2695280", "Q11019", "Q1183543", "Q21198", "Q2424752", "Q17747846",
                                       "Q4915012", "Q39546", "Q1183649"], ""),
}
# the org pool splits in two by the entity's own md5: "founded" one half, "does" the other
ORG_TEMPLATES = {"founded": "when was {t} founded", "does": "what does {t} do"}
MAX_WORDS = 6


def log(msg: str) -> None:
    print(f"[judged-info] {msg}", file=sys.stderr, flush=True)


def sparql(classes: list[str], prop: str, pool: int) -> str:
    vals = " ".join(f"wd:{c}" for c in classes)
    need = f"?item wdt:{prop} ?v ." if prop else ""
    return (PREFIXES + "SELECT ?title ?item ?links WHERE {\n"
            f"  VALUES ?cls {{ {vals} }}\n  ?item wdt:P31 ?cls ; wikibase:sitelinks ?links .\n  {need}\n"
            "  ?a schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title .\n"
            f"}} ORDER BY DESC(?links) LIMIT {pool}")


def parse(lines) -> list[tuple[str, str, int]]:
    """QLever TSV (title, item, links) -> [(title, QID, sitelinks)], one row per item."""
    out, seen = [], set()
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 3 or f[0].startswith("?"):
            continue
        title = f[0]
        if title.startswith('"'):
            title = title.rsplit('"', 1)[0][1:].replace('\\"', '"')
        qid = f[1].strip("<>").rsplit("/", 1)[-1]
        try:
            links = int(f[2].strip('"').split("^^")[0].strip('"') or 0)
        except ValueError:
            links = 0
        if qid in seen:
            continue
        seen.add(qid)
        out.append((title, qid, links))
    return out


def usable(title: str) -> bool:
    """A title the template can hold: no "(...)" qualifier, at most MAX_WORDS words, letters in it."""
    if title != hb.plain_title(title) or len(title.split()) > MAX_WORDS:
        return False
    return bool(re.search(r"[A-Za-z]", title)) and not re.search(r"[#|\t]", title)


def half(entity: str) -> int:
    """Which org template an entity goes to (0 founded, 1 does): its own md5, apart from the split's."""
    return int(hashlib.md5(f"{SEED}:org-template:{entity}".encode()).hexdigest()[:8], 16) % 2


def row(query: str, template: str, stratum: list[str], split: str, entity: str, source: str) -> dict:
    return {"query": query, "category": "judged", "expected": "-", "set": SET,
            "stratum": ",".join([f"template:{template}"] + stratum), "split": split, "entity": entity, "source": source}


def wikidata_rows(a, cache: Path) -> tuple[list[dict], list[tuple[str, str, int]]]:
    pools: dict[str, list[tuple[str, str, int]]] = {}
    for name, (_phr, classes, prop) in TEMPLATES.items():
        pool = a.pool * 2 if name in ("org", "directed") else a.pool      # org feeds two templates; most film titles carry "(film)"
        pools[name] = [r for r in parse(hb.qlever(sparql(classes, prop, pool), cache / f"judged-info-{name}-{pool}.tsv")) if usable(r[0])]
        log(f"{name}: {len(pools[name])} usable of the {pool} most-linked")
    if a.probe:
        for name, rows in pools.items():
            print(f"== {name}")
            for t, q, l in rows[:12]:
                print(f"  {l:4d} {q:12} {t}")
        return [], []
    titles = sorted({t for rows in pools.values() for t, _q, _l in rows})
    views = hb.pageviews(titles, a.month, cache / f"judged-info-pageviews-{a.month}.tsv")
    # a bare name two pooled articles share (across templates too) is ambiguous: dropped
    owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, rows in pools.items():
        for t, q, _l in rows:
            owners[hb.norm_key(t)].append((name, q))
    ambiguous = [(k, "/".join(sorted({n for n, _ in v})), len(v)) for k, v in owners.items() if len(v) > 1]
    out = []
    for name, rows in pools.items():
        rows = [r for r in rows if len(owners[hb.norm_key(r[0])]) == 1]
        head = sorted(rows, key=lambda r: (-views.get(r[0], 0), r[0].encode("utf-8")))
        if name == "org":
            for tmpl_i, (tmpl, phr) in enumerate(ORG_TEMPLATES.items()):
                mine = [r for r in head if half(hb.norm_key(r[0])) == tmpl_i][: a.per]
                out += [row(phr.format(t=t.lower()), tmpl, [f"views-rank:{band(i)}"], hb.split_of(hb.norm_key(t)), hb.norm_key(t), q)
                        for i, (t, q, _l) in enumerate(mine)]
        else:
            phr = TEMPLATES[name][0]
            out += [row(phr.format(t=t.lower()), name, [f"views-rank:{band(i)}"], hb.split_of(hb.norm_key(t)), hb.norm_key(t), q)
                    for i, (t, q, _l) in enumerate(head[: a.per])]
    return out, sorted(ambiguous)


def band(i: int) -> str:
    return "1-30" if i < 30 else "31-60" if i < 60 else "61+"


def hand_rows(path: Path, template: str) -> list[dict]:
    """A hand-written list: odd lines dev, even lines test (the file's own rule), comments skipped."""
    out, n = [], 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        q = " ".join(line.split()).lower()
        if not q or q.startswith("#"):
            continue
        n += 1
        out.append(row(q, template, [], "dev" if n % 2 else "test", hb.norm_key(q), f"{path.name}:{lineno}"))
    return out


def gov_rows(path: Path, n: int) -> list[dict]:
    """n rows of the government task list, picked by md5 so a rerun picks the same; entity gov:<state>-<task>."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        f = line.rstrip("\n").split("\t")
        if len(f) == 5 and f[0] and not f[0].startswith("#") and f[0] != "id":
            rid, state, task, form, q = f
            rows.append((hashlib.md5(f"{SEED}:gov:{rid}".encode()).hexdigest(), rid, state, task, form, q))
    rows.sort()
    out = []
    for _h, rid, state, task, form, q in rows[:n]:
        ent = f"gov:{state}-{task}"
        out.append(row(" ".join(q.split()).lower(), "gov", [f"form:{form}"], hb.split_of(ent), ent, f"{path.name}:{rid}"))
    return out


def write(path: Path, rows: list[dict]) -> None:
    cols = ["query", "category", "expected", "set", "stratum", "split", "entity", "source"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(r[c] for c in cols) + "\n")


def write_exclude(path: Path, rows: list[dict]) -> int:
    """Every query of the set in exclude.tsv's two forms (sq-eval.py --write-exclude — form=query is the query
    lower-cased and single-spaced, form=norm the engine's exact-tier key, letters and digits), source=judged-info.
    Both halves, all marked split=test as exclude.tsv asks: the whole set is judged and frozen, so no head list, training
    pool or precompute may hold any of it."""
    out = set()
    for r in rows:
        q = " ".join(r["query"].lower().split())
        out.add((q, "query", SET, "test", "judged-info"))            # the convention: source=judged-<name>, split=test
        out.add((hb.norm_key(q), "norm", SET, "test", "judged-info"))
    out = {x for x in out if x[0]}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Queries of the judged informational set, in exclude.tsv's shape: no head list, training\n"
                "# pool or precompute may contain them. key = the query lower-cased and single-spaced (form=query) or the\n"
                "# engine's exact-tier key, letters and digits only (form=norm). Both halves are here: the set is judged once\n"
                "# and frozen. Append these rows to evalsets/open/exclude.tsv (sq-eval.py --write-exclude keeps judged rows).\n"
                "key\tform\tset\tsplit\tsource\n")
        for x in sorted(out):
            f.write("\t".join(x) + "\n")
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(TOOLS / "evalsets" / "open"))
    ap.add_argument("--per", type=int, default=90, help="rows per Wikidata template (the most viewed of the pool)")
    ap.add_argument("--pool", type=int, default=300, help="most-linked candidates per template whose pageviews are fetched")
    ap.add_argument("--month", default="2026-09", help="pageview month, YYYY-MM")
    ap.add_argument("--gov", help="gov-services.tsv (id, state, task, form, query); absent = no gov rows")
    ap.add_argument("--gov-n", type=int, default=70, help="gov rows sampled by md5")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "logout"), help="QLever and pageview answers (delete to refresh)")
    ap.add_argument("--probe", action="store_true", help="print the 12 most-linked usable titles per template and stop (no pageviews)")
    a = ap.parse_args()
    cache = Path(a.cache)
    rows, ambiguous = wikidata_rows(a, cache)
    if a.probe:
        return
    rows += hand_rows(TOOLS / "evalsets" / "descriptive-sites.txt", "descriptive")
    rows += hand_rows(TOOLS / "evalsets" / "descriptive-controls.txt", "control")
    if a.gov:
        rows += gov_rows(Path(a.gov), a.gov_n)
    else:
        log("no --gov PATH: no government rows")
    seen: dict[str, dict] = {}
    for r in rows:                                          # one row per query; the first template keeps it
        seen.setdefault(hb.norm_key(r["query"]), r)
    rows = list(seen.values())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write(out / f"{SET}.tsv", rows)
    n_ex = write_exclude(out / f"{SET}.exclude.tsv", rows)
    with open(out / f"ambiguous-{SET}.tsv", "w", encoding="utf-8") as f:
        f.write("key\ttemplates\tarticles\n")
        for k, t, n in ambiguous:
            f.write(f"{k}\t{t}\t{n}\n")
    per_t: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        per_t[r["stratum"].split(",")[0][9:]].append(r["split"])
    for t, splits in per_t.items():
        log(f"{t:12} {len(splits):4d}  dev {splits.count('dev'):3d} / test {splits.count('test'):3d}")
    log(f"{len(rows)} queries -> {out / (SET + '.tsv')}; {len(ambiguous)} shared names -> ambiguous-{SET}.tsv; "
        f"{n_ex} exclusion keys -> {SET}.exclude.tsv")


if __name__ == "__main__":
    main()
