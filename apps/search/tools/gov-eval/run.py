#!/usr/bin/env python3
"""Ask engines the government-services set and say whether their results help. DEV ONLY.

    run.py ask  QUERIES.tsv NAME=http://host:port OUT.jsonl [--workers 2]     /search?k=5 for every query
    run.py home QUERIES.tsv POINTS.tsv OUT.tsv                                the "home" wording: a named-state query
                                                                              without its state, asked from a point in it
    run.py score QUERIES.tsv DOTGOV.csv OUT.jsonl [OUT2.jsonl ...]            the table
    run.py states DOTGOV.csv OUT.json                                         gov-states.json for serve/api/gov_state.py

A result is graded by rule, no judge:
  helps      it is on the state government's own site, not a news/FAQ/contact/jobs page, and its title or address
             carries the task's words (the regexes were tuned against Clef 27B's verdicts on 1,368 results, 2026-10-28)
  right site it is on a government site of the query's state (a county's or a city's too), not the page
  no         anything else
A state's government sites: the .gov registry's domains whose State is that state (DOTGOV.csv is CISA's
current-full.csv, public domain) and state.<xx>.us. Blind spots, stated: a state service on a commercial
address (myflorida.com, njportal.com) counts as no; a federal page counts as no even where the federal
government is who does it. The same rule grades every arm, so the comparison holds; the level is a floor.
"""
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

TOPIC = {
    "drivers-license": r"driver|driving|\bdl\b|photo id|real.?id|\bid card|licen[cs]e.{0,20}(renew|extension|replace)",
    "vehicle-registration": r"vehicle|\bcar\b|auto|motor|plates?|\btags?\b|titl|registration.{0,10}renew|renew.{0,20}registration|\bdmv\b|\bmvd\b|\bmva\b|\brmv\b|\bbmv\b",
    "real-id": r"real.?id|travel id|photo id|\bid card|identification card|driver.?licen[cs]e|star card|gold star|enhanced",
    "voter-registration": r"\bvot(e|er|ing)\b|register to vote|election",
    "unemployment": r"unemploy|\bui\b|reemploy|jobless|apply.{0,20}benefits|benefits.{0,20}apply|file.{0,10}claim",
    "birth-certificate": r"birth cert|birth record|vital.?(record|statistic|certificate)|certificate.{0,20}birth|birth,? death",
    "business-registration": r"\bllc\b|business.{0,30}(regist|form|start|filing|entit)|start.{0,10}business|corporation|limited.liability|sunbiz|business services",
    "tax-refund": r"refund|where.?s my",
    "fishing-license": r"fish.{0,30}licen|licen.{0,30}fish|fishing (permit|regulations)|hunt.{0,10}(and|&) fish|angl|buy.{0,20}licen|freshwater|saltwater",
    "firearms-permit": r"firearm|handgun|pistol|concealed|\bgun\b|weapon|\bccw\b|\bfoid\b|\bltc\b|\bchl\b|\bcwp\b|carry permit",
    "food-assistance": r"\bsnap\b|food.?(stamp|assist|support|benefit)|nutrition assist|\bebt\b|apply.{0,20}benefits|benefits.{0,20}(apply|eligib)|\babe\b|\bcalfresh\b",
    "medicaid": r"medicaid|medical assistance|health.?care coverage|apply.{0,20}(services|benefits|coverage)|\bchip\b|\bmedi-?cal\b|\bhusky\b|\bmasshealth\b|\bbadgercare\b|\bsoonercare\b|\bahcccs\b",
    "nursing-license": r"nurs|licen[cs]e.{0,10}(verif|lookup|look up|search|renew)|verif.{0,20}licen|licensee (search|lookup)|\brn\b|\blpn\b",
    "state-parks": r"camp(ground|ing|site)|park.{0,20}reserv|reserv.{0,20}(park|camp|site)|state.?parks?|\bparks\b",
    "child-support": r"child.?support",
    "jury-duty": r"jur(y|or)",
    "marriage-license": r"marriage (licen|cert|applic)|get married|marry|marriage record",
    "notary": r"notar",
    "traffic-ticket": r"ticket|citation|traffic.{0,20}(pay|fine|court|violation)|pay.{0,20}fine|e-?pay|online payment",
    "unclaimed-property": r"unclaimed (property|funds|money)|missing money|abandoned property",
    # the held-out tasks (evalset.py --heldout), written before any model saw them
    "hunting-license": r"hunt|buy.{0,20}licen|licen.{0,20}(sales|buy|purchase)|game (licen|permit)",
    "title-transfer": r"titl|transfer.{0,20}(owner|vehicle)|vehicle.{0,20}transfer|motor vehicle|\bdmv\b|\bmvd\b|\bmva\b|\brmv\b|\bbmv\b",
    "income-tax-forms": r"income tax|tax forms?|forms?.{0,20}tax|individual.{0,20}(tax|income|forms?)|personal income",
    "boat-registration": r"boat|vessel|watercraft|\bpwc\b",
    "cdl": r"\bcdl\b|commercial.{0,10}(driv|licen|vehicle)|truck",
    "death-certificate": r"death (cert|record)|vital.?(record|statistic|certificate)|certificate.{0,20}death|birth,? death",
    "workers-comp": r"(workers.? ?comp|work comp).{0,40}(claim|forms?|file|filing|injur)|(claim|forms?|file|filing|injur|office of|division of).{0,40}(workers.? ?comp|work comp)|injured (worker|employee|at work)|workplace injur|report.{0,20}injur|\bdwc\b|industrial (insurance|accident)|file.{0,10}claim",
    "contractor-license": r"contractor|licen[cs]e.{0,10}(verif|lookup|look up|search)|verif.{0,20}licen|licensee (search|lookup)",
    "small-claims": r"small claims|civil (claims?|case)|court.{0,20}(self.?help|forms)|self.?help",
    "road-conditions": r"road (condition|report|closure|info)|road.?(and|&) (weather|travel)|traffic (condition|map|info|alert|camera)|\b511\b|travel (info|conditions?|alerts?|map)|travinfo|winter (driving|road)|highway (condition|info)|closures?",
}
TOPIC = {k: re.compile(v, re.I) for k, v in TOPIC.items()}
# A page of the state's site that is not where the task is done, whatever words it carries (Clef 27B rejected 190 of
# the 383 "helps" it was shown on 2026-10-28; the news items, FAQs, contact pages, job postings and announcements were
# most of them). Matched on the address path and the title.
_NOT_THE_PAGE = re.compile(r"(^|[/_.-])(news|newsroom|press|press-?releases?|releases?|media|blog|events?|calendar|meetings?|"
                           r"minutes|agendas?|announcements?|archives?|speeches|testimony|jobs?|careers?|job-?postings?|"
                           r"employment|contact(-?us)?|about(-?us)?|staff|directory|complaints?|fraud|records-?request|"
                           r"open-?records|training|partners?|providers?|employers?|statutes?|regulations?|laws?|legislat\w*|"
                           r"\d{4}([/_-]\d{1,2}){1,2})([/_.-]|$)|\b(faqs?|frequently asked|press release|news|contact us|"
                           r"careers?|job posting|training|complaint|fraud|employers?|partners?|providers?)\b", re.I)
_STATE_US = re.compile(r"(^|\.)state\.([a-z]{2})\.us$")


def _gov_states_mod():
    """release/gov_states.py: the one reading of the registry (the release ships what it builds)."""
    import importlib.util
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "release", "gov_states.py")
    spec = importlib.util.spec_from_file_location("gov_states", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def state_level(dotgov: str) -> set[str]:
    """The registry's state-government domains ("State or territory"): the state does these tasks, a county or a city
    page is its right site at most (Clef 27B, 2026-10-28: county clerks, tax collectors and courts were a third of the
    rule's false accepts; where a county does do the task the state's page still says so)."""
    with open(dotgov, encoding="utf-8", newline="") as f:
        return {r["Domain name"].strip().lower() for r in csv.DictReader(f) if r["Domain type"].startswith("State")}


def state_domains(dotgov: str) -> dict[str, str]:
    """registry domain -> state code, for domains that belong to a state, county, city or district there
    (not federal, not tribal: those carry the state of their office, not of whom they serve). The release's
    own reading (release/gov_states.py build), so the grader and the shipped file agree."""
    return _gov_states_mod().build(dotgov)["domains"]


def site_state(url: str, doms: dict[str, str]) -> str | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    m = _STATE_US.search(host)
    if m:
        return m.group(2).upper()
    parts = host.split(".")
    for i in range(len(parts) - 1):                       # dmv.ca.gov -> ca.gov
        s = doms.get(".".join(parts[i:]))
        if s:
            return s
    return None


def is_state_level(url: str, levels: set[str]) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if _STATE_US.search(host):
        return True
    parts = host.split(".")
    return any(".".join(parts[i:]) in levels for i in range(len(parts) - 1))


def grade(task: str, state: str, r: dict, doms: dict[str, str], levels: set[str] | None = None) -> int:
    """2 helps, 1 right site, 0 no. With `levels` (state_level) a county's or a city's page is 1 at most, and a page
    whose address or title says news, FAQ, contact, jobs or the like is 1 whatever words it carries."""
    url = r.get("url") or ""
    if site_state(url, doms) != state:
        return 0
    if levels is not None and not is_state_level(url, levels):
        return 1
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    title = r.get("title") or ""
    if levels is not None and (_NOT_THE_PAGE.search(path) or _NOT_THE_PAGE.search(title)):
        return 1
    return 2 if TOPIC[task].search(title + " " + path.replace("-", " ").replace("_", " ").replace("/", " ")) else 1


STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey",
    "NM": "new mexico", "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming",
}


def cmd_home(queries: str, points: str, out: str) -> None:
    """The named-state ("name") rows with the state's name taken out, each asked from its state's point: what a
    searcher whose location is set types. Rows whose wording does not carry the name are skipped."""
    names = STATE_NAMES
    pts = {p[0]: f"{p[2]},{p[3]}" for p in (l.rstrip("\n").split("\t") for l in open(points)) if not p[0].startswith("#")}
    n = skipped = 0
    with open(out, "w") as f:
        for row in (l.rstrip("\n").split("\t") for l in open(queries)):
            if len(row) < 5 or row[3] != "name" or row[1] not in pts or row[1] not in names:
                continue
            q = re.sub(r"\b" + re.escape(names[row[1]]) + r"\b", " ", row[4])
            q = re.sub(r"\s+(in|of|for)\s*$", "", " ".join(q.split()))
            if q == row[4] or not q:
                skipped += 1
                continue
            f.write("\t".join([row[0].rsplit("-name", 1)[0] + "-home", row[1], row[2], "home", q, pts[row[1]]]) + "\n")
            n += 1
    print(f"{n} home rows ({skipped} without the state's name, skipped) -> {out}", file=sys.stderr)


def cmd_ask(queries: str, arm: str, out: str, workers: int) -> None:
    name, api = arm.split("=", 1)
    api, _, extra = api.partition("&")                    # NAME=http://host:port&drop=govstate
    rows = [l.rstrip("\n").split("\t") for l in open(queries)]

    def one(row):
        p = {"q": row[4], "k": 5}
        if len(row) > 5 and row[5]:                       # a home-wording row carries its searcher's point
            p["near"] = row[5]
        url = api.rstrip("/") + "/search?" + urllib.parse.urlencode(p) + ("&" + extra if extra else "")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    res = json.load(resp).get("results") or []
                return {"id": row[0], "arm": name, "results": [{"url": x.get("url"), "title": x.get("title"), "source": x.get("source")} for x in res[:5]]}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:120]
        return {"id": row[0], "arm": name, "error": err}

    n = bad = 0
    with open(out, "w") as f, ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(one, rows):
            n += 1
            bad += "error" in res
            f.write(json.dumps(res) + "\n")
            if n % 250 == 0:
                print(f"{name}: {n}/{len(rows)} ({bad} errors)", file=sys.stderr, flush=True)
    print(f"{name}: {n} asked, {bad} errors -> {out}", file=sys.stderr)


def cmd_score(queries: str, dotgov: str, outs: list[str]) -> None:
    q = {r[0]: r for r in (l.rstrip("\n").split("\t") for l in open(queries))}
    doms, levels = state_domains(dotgov), state_level(dotgov)
    arms: dict[str, dict] = {}
    for path in outs:
        for l in open(path):
            r = json.loads(l)
            if "error" not in r:
                arms.setdefault(r["arm"], {})[r["id"]] = r["results"]
    common = sorted(set.intersection(*(set(a) for a in arms.values())))
    print(f"{len(common)} queries answered by every arm ({len(q)} in the set); arms: {', '.join(arms)}\n")

    def table(title: str, key) -> None:
        groups = defaultdict(list)
        for i in common:
            groups[key(q[i])].append(i)
        print(f"{title:24}" + "".join(f"{a + ' help@1':>16}{a + ' help@5':>16}{a + ' site@5':>16}" for a in arms))
        for g in sorted(groups):
            line = f"{g:24}"
            for a in arms:
                gr = [[grade(q[i][2], q[i][1], r, doms, levels) for r in arms[a][i]] for i in groups[g]]
                h1 = sum(1 for x in gr if x[:1] == [2]) / len(gr)
                h5 = sum(1 for x in gr if 2 in x) / len(gr)
                s5 = sum(1 for x in gr if any(v >= 1 for v in x)) / len(gr)
                line += f"{h1:16.1%}{h5:16.1%}{s5:16.1%}"
            print(line + f"   n={len(groups[g])}")
        print()

    table("all", lambda r: "all")
    table("by wording", lambda r: r[3])
    table("by task", lambda r: r[2])
    table("by state", lambda r: r[1])


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "ask":
        w = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 2
        cmd_ask(sys.argv[2], sys.argv[3], sys.argv[4], w)
    elif len(sys.argv) == 5 and sys.argv[1] == "home":
        cmd_home(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "states":
        d = _gov_states_mod().write(sys.argv[2], sys.argv[3])     # the same file a release ships (assemble.py --gov-states)
        print(f"{len(d['domains'])} domains -> {sys.argv[3]}", file=sys.stderr)
    elif len(sys.argv) >= 5 and sys.argv[1] == "score":
        cmd_score(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        sys.exit(__doc__)
