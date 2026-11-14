#!/usr/bin/env python3
"""Score local search against labelled real queries.

    tools/eval-queries.py LABELS.tsv [--host user@jump-host] [--details OUT.tsv]

LABELS.tsv: query <TAB> category <TAB> expected
  category  site | wiki | shop | howto | local | fresh | junk
  expected  for site: registrable domain(s), '|'-separated (costco.com)
            for wiki: Wikipedia title(s), '|'-separated (Robert Loggia)
            otherwise '-'

Open-data sets (tools/evalsets/open/*.tsv, written by tools/open-evalsets.py) are
read when the file starts with the header "query<TAB>category<TAB>expected<TAB>set…":
  category  account  expected: account URL(s) without scheme/www (instagram.com/cntower)
            place    expected: osm-id@lat,lon@name (a listing twin within 150 m counts)
  --split dev|test keeps one half; the report adds hit@1/hit@5 per set and per stratum.
They are public data and live in git; --direct URL asks search-api over HTTP (a tunnel
to a throwaway container) instead of through searxng-a.

The label file holds real searches — keep it OUT of git (session scratch or the
build database only). Queries go over SSH to the serving box and are asked of
search-api from inside the SearXNG network, exactly the path SearXNG uses; the
API itself never logs query text.

Reports hit@1 / hit@5 for site and wiki queries, and the top local score per
category — the numbers the confidence gate will be fitted on: queries
local search should NOT answer (shop, howto, local, fresh, junk) must score
low, or the gate would keep them from the web.
"""
import argparse
import base64
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import statistics
from collections import Counter
import subprocess
import sys
from urllib.parse import unquote, urlsplit

REMOTE = r'''
import json, sys, urllib.parse, urllib.request
out = []
req = json.load(sys.stdin)
for q in req["queries"]:
    try:
        d = json.load(urllib.request.urlopen(
            req["api"] + "/search?k=10" + req.get("extra", "") + "&q=" + urllib.parse.quote(q), timeout=10))
        out.append({"results": d.get("results", []), "confident": bool(d.get("confident")),
                    "intent": d.get("intent", "")})
    except Exception as e:
        out.append({"error": type(e).__name__})
json.dump(out, sys.stdout)
'''


def ask(host: str, api: str, queries: list[str], extra: str = "") -> list:
    # base64 so no quote in the script has to survive ssh + docker exec
    b64 = base64.b64encode(REMOTE.encode()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", host,
           "docker exec -i searxng-a /usr/local/searxng/.venv/bin/python -c "
           f"\"import base64; exec(base64.b64decode('{b64}'))\""]
    # stdin carries the queries so they never appear on a command line (ps, shell history)
    r = subprocess.run(cmd, input=json.dumps({"api": api, "queries": queries, "extra": extra}),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def ask_direct(api: str, queries: list[str], extra: str = "", workers: int = 2) -> list:
    """search-api over HTTP from here (e.g. ssh -L to a throwaway container)."""
    def one(q):
        try:
            with urllib.request.urlopen(api + "/search?k=10" + extra + "&q=" + urllib.parse.quote(q), timeout=60) as r:
                d = json.load(r)
            return {"results": d.get("results", []), "confident": bool(d.get("confident")), "intent": d.get("intent", "")}
        except Exception as e:
            return {"error": type(e).__name__}
    with ThreadPoolExecutor(max(1, workers)) as ex:
        return list(ex.map(one, queries))


PRESENT = r'''
import json, sys, subprocess
want = json.load(sys.stdin)
def q(sql):
    r = subprocess.run(["docker", "exec", "-i", want["container"], "psql", "-U", "search", "-d", want["db"],
                        "-Atc", sql], capture_output=True, text=True)
    return set(r.stdout.split())
lit = lambda xs: ",".join("'" + x.replace("'", "''") + "'" for x in xs) or "''"
doms = q("SELECT domain FROM docs WHERE error IS NULL AND domain IN (" + lit(want["domains"]) + ")")
wiki = q("SELECT norm FROM wiki_aliases WHERE norm IN (" + lit(want["norms"]) + ")")
json.dump({"domains": sorted(doms), "norms": sorted(wiki)}, sys.stdout)
'''


# Release path (SEARCH-POSTGRES-EXIT.md phase 3): the same two answers from a release folder on the box,
# stdlib sqlite3 only. names kind 5 = domain (N_DOMAIN) (a site with a crawled homepage: sites.home), kind 2 = alias norm (N_ALIAS).
PRESENT_RELEASE = r'''
import json, os, sqlite3
want = json.load(sys.stdin)
root = want["release"].rstrip("/")
if not os.path.isfile(root + "/manifest.json") and os.path.islink(root + "/current"):
    root = os.path.realpath(root + "/current")
c = sqlite3.connect("file:" + root + "/lookup.sqlite?mode=ro&immutable=1", uri=True)
doms = {d for d in want["domains"]
        if c.execute("SELECT 1 FROM names n JOIN sites s ON s.doc_id = n.doc_id WHERE n.name = ? AND n.kind = 5 AND s.home", (d,)).fetchone()}
wiki = {n for n in want["norms"] if c.execute("SELECT 1 FROM names WHERE name = ? AND kind = 2", (n,)).fetchone()}
json.dump({"domains": sorted(doms), "norms": sorted(wiki)}, sys.stdout)
'''


def present_release(host: str, via: str, release: str, domains: list[str], norms: list[str]) -> tuple[set, set]:
    """present() from a release folder on `host` (after the optional --pg-via hop) instead of psql."""
    want = json.dumps({"domains": domains, "norms": norms, "release": release})
    script = "import sys\n" + PRESENT_RELEASE.replace("want = json.load(sys.stdin)", "want = json.loads(" + repr(want) + ")")
    cmd = ["ssh", "-o", "BatchMode=yes", host, f"{via} python3 -".strip()]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return set(d["domains"]), set(d["norms"])


def present(host: str, via: str, container: str, db: str,
            domains: list[str], norms: list[str]) -> tuple[set, set]:
    """Which expected answers are actually in the served index (separates ranking from coverage)."""
    # The script travels on stdin with its input baked in, so nothing has to survive
    # the quoting of two ssh hops (--pg-via, e.g. "ssh user@index-host sudo -n").
    want = json.dumps({"domains": domains, "norms": norms, "container": container, "db": db})
    script = PRESENT.replace("want = json.load(sys.stdin)",
                             "want = json.loads(" + repr(want) + ")")
    cmd = ["ssh", "-o", "BatchMode=yes", host, f"{via} python3 -".strip()]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return set(d["domains"]), set(d["norms"])


def norm_key(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def norm_url(u: str) -> str:
    """scheme, www. and a trailing slash dropped, lowercase: instagram.com/cntower"""
    p = urlsplit(u if "://" in u else "https://" + u)
    host = (p.hostname or "").lower()
    return ((host[4:] if host.startswith("www.") else host) + p.path.rstrip("/")).lower()


def place_hit(exp: str, result: dict) -> bool:
    """osm-id@lat,lon@name: the OSM feature itself, or a row of the same name within 150 m
    (the same business from a listing, which the engine may show instead)."""
    osm, point, name = exp.split("@", 2)
    if result.get("url", "").endswith("/" + osm):
        return True
    if result.get("lat") is None:
        return False
    la, lo = map(float, point.split(","))
    km = 111.32 * math.hypot(result["lat"] - la, (result["lon"] - lo) * math.cos(math.radians(la)))
    return km <= 0.15 and norm_key(result.get("title", "").split(" \u2014 ")[0]) == norm_key(name)


def hit(cat: str, expected: list[str], result: dict) -> bool:
    if cat == "account":
        return norm_url(result.get("url", "")) in expected
    if cat == "place":
        return result.get("source") == "place" and any(place_hit(e, result) for e in expected)
    if cat in ("site", "click"):
        h = domain_of(result.get("url", ""))
        return any(h == d or h.endswith("." + d) for d in expected)
    if cat == "wiki":
        if result.get("source") != "wikipedia":
            return False
        title = unquote(result["url"].rsplit("/wiki/", 1)[-1]).replace("_", " ")
        return any(title.lower() == e.lower() for e in expected)
    return False


def accept_hit(cat: str, tokens: list[str], result: dict) -> bool:
    """Accepted alternatives of an open-set row (evalsets/open/accept/, built by sq-accept-keys.py from
    Wikidata P856 and, for place rows, by sq-accept-places.py from the OSM website tag): the entity's own official
    site is a right answer too. site:<host> = a result on that host or a subdomain of it; url:<host/path> = a
    result at that URL or under it (scheme, www., case and a trailing slash do not count). An article is never
    matched here: for a wiki row only its expected title is the article."""
    if cat not in ("wiki", "site", "place") or (cat == "wiki" and result.get("source") == "wikipedia"):
        return False
    url = result.get("url", "")
    h = domain_of(url)
    h = h[4:] if h.startswith("www.") else h
    if not h:
        return False
    for t in tokens:
        kind, _, v = t.partition(":")
        if kind == "site":
            if h == v or h.endswith("." + v):
                return True
        elif kind == "url":
            u = norm_url(url)
            if u == v or u.startswith(v + "/"):
                return True
    return False


SCORED = ("site", "click", "wiki", "account", "place")


def by_group(meta: list, details: list) -> None:
    """Open-data sets: hit@1 / hit@5 per set, then per value of each stratum facet."""
    groups: dict[tuple, list] = {}
    for (st, strat), d in zip(meta, details):
        r = d[2]
        groups.setdefault((st, ""), []).append(r)
        for facet in filter(None, strat.split(",")):
            groups.setdefault((st, facet), []).append(r)
    print(f"\n{'set':<14} {'stratum':<28} {'n':>5} {'hit@1':>7} {'hit@5':>7}")
    for (st, facet), ranks in sorted(groups.items()):
        n = len(ranks)
        h1 = sum(r == 1 for r in ranks) / n
        h5 = sum(r != "-" and r <= 5 for r in ranks) / n
        print(f"{st:<14} {facet or '(all)':<28} {n:>5} {h1:>7.1%} {h5:>7.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels")
    ap.add_argument("--host", default="user@jump-host")
    ap.add_argument("--details", help="write per-query results here (keep out of git)")
    ap.add_argument("--api", default="http://search-api:8090",
                    help="search-api as seen from searxng-a (indexhost: http://index-host:8090)")
    ap.add_argument("--pg-via", default="", help="extra hop to the index host, e.g. 'ssh user@index-host sudo -n'")
    ap.add_argument("--pg-container", default="search-serve-postgres")
    ap.add_argument("--pg-db", default="search")
    ap.add_argument("--release-dir", default="", help="release folder ON --host (after --pg-via) for the is-the-answer-indexed "
                    "check, instead of Postgres. Without it the check uses the LEGACY Postgres path (--pg-*), deleted at phase 7")
    ap.add_argument("--no-semantic", action="store_true", help="ask search-api to skip the semantic tier (A/B)")
    ap.add_argument("--no-learned", action="store_true", help="ask search-api to skip the learned ranker (A/B)")
    ap.add_argument("--d2q", action="store_true", help="ask search-api to add the model-written query list (A/B)")
    ap.add_argument("--no-anchors", action="store_true", help="ask search-api to skip the anchor-text list (A/B)")
    ap.add_argument("--off", default="", help="search as if these tables were not served, e.g. page_docs,wiki_links "
                                              "(what leaving them off a customer box would cost)")
    ap.add_argument("--body", choices=("full", "bow", "none"), default="full",
                    help="page-text index for the site and page lists: full, bow (sorted words), none ( A/B)")
    ap.add_argument("--direct", metavar="URL", help="ask search-api at URL over HTTP from here (e.g. "
                    "http://127.0.0.1:18090 through ssh -L) instead of through searxng-a on --host")
    ap.add_argument("--workers", type=int, default=2, help="--direct: queries in flight")
    ap.add_argument("--extra", default="", help="more query-string switches, e.g. '&drop=intent&ce=0' (A/B)")
    ap.add_argument("--split", choices=("dev", "test"), help="open-data sets: score one half only")
    ap.add_argument("--no-present", action="store_true", help="skip the is-the-answer-indexed check")
    a = ap.parse_args()

    rows, meta = [], []
    with open(a.labels, encoding="utf-8") as f:
        first = f.readline()
        cols = first.rstrip("\n").split("\t")
        opened = cols[:4] == ["query", "category", "expected", "set"]
        if not opened:
            f.seek(0)
        for line in f:
            vals = line.rstrip("\n").split("\t")
            if opened:
                r = dict(zip(cols, vals))
                if a.split and r.get("split") != a.split:
                    continue
                q, cat, exp = r["query"], r["category"], r["expected"]
                meta.append((r["set"], r.get("stratum", "")))
            else:
                q, cat, exp = vals
            rows.append((q, cat, [] if exp == "-" else exp.split("|")))
    extra = ("&sem=0" if a.no_semantic else "") + ("&rank=0" if a.no_learned else "") + ("&anc=0" if a.no_anchors else "")
    extra += ("&off=" + a.off) if a.off else ""
    extra += ("&d2q=1" if a.d2q else "") + ("" if a.body == "full" else "&body=" + a.body)
    extra += a.extra
    if a.direct:
        results = ask_direct(a.direct.rstrip("/"), [r[0] for r in rows], extra, a.workers)
    else:
        results = ask(a.host, a.api, [r[0] for r in rows], extra)

    doms = sorted({d for q, c, e in rows if c in ("site", "click") for d in e})
    norms = sorted({norm_key(t) for q, c, e in rows if c == "wiki" for t in e})
    if a.no_present:
        have_dom, have_norm = set(doms), set(norms)
    elif a.release_dir:
        have_dom, have_norm = present_release(a.host, a.pg_via, a.release_dir, doms, norms)
    else:
        print("eval-queries: is-the-answer-indexed check uses LEGACY Postgres (pass --release-dir); removed at phase 7", file=sys.stderr)
        have_dom, have_norm = present(a.host, a.pg_via, a.pg_container, a.pg_db, doms, norms)

    by = {}
    details = []
    gate = {"fired": 0, "right": 0, "answerable": 0}
    intents: dict[str, Counter] = {}
    for (q, cat, exp), res in zip(rows, results):
        if "error" in res:
            print(f"search-api error on a query: {res['error']}", file=sys.stderr)
            res = {"results": [], "confident": False}
        conf, intent, res = res["confident"], res.get("intent", ""), res["results"]
        intents.setdefault(cat, Counter())[intent] += 1
        rank = next((i + 1 for i, r in enumerate(res) if hit(cat, exp, r)), None)
        top = res[0]["score"] if res else 0.0
        indexed = (cat in ("site", "click") and any(d in have_dom for d in exp)) or \
                  (cat == "wiki" and any(norm_key(t) in have_norm for t in exp)) or \
                  cat in ("account", "place")         # open sets: built from the served tables
        b = by.setdefault(cat, {"n": 0, "h1": 0, "h5": 0, "tops": [], "ni": 0, "i1": 0})
        b["n"] += 1
        b["h1"] += rank == 1
        b["h5"] += rank is not None and rank <= 5
        b["tops"].append(top)
        b["ni"] += indexed
        b["i1"] += indexed and rank == 1
        # Gate: a confident answer is right only when it is THE expected site/article.
        if conf:
            gate["fired"] += 1
            gate["right"] += cat in SCORED and rank == 1
        gate["answerable"] += cat in SCORED and rank == 1
        details.append((q, cat, rank or "-", f"{top:.1f}", res[0]["url"] if res else "", int(conf)))

    print("Local search vs real queries")
    print("hit@k = the expected site / Wikipedia article is in the top k local results;")
    print("top score = the best local result's score (for the gate: should be LOW where local can't answer)\n")
    print("indexed hit@1 = hit@1 counting only queries whose expected answer IS in the index (ranking quality)\n")
    print(f"{'category':<8} {'queries':>7} {'hit@1':>12} {'hit@5':>12} {'indexed hit@1':>14} {'top score median':>17} {'p90':>6}")
    order = ["site", "click", "wiki", "account", "place", "shop", "howto", "local", "fresh", "junk"]
    for cat in sorted(by, key=lambda c: order.index(c) if c in order else 99):
        b = by[cat]
        scored = cat in SCORED
        h1 = f"{b['h1']}/{b['n']} ({b['h1'] / b['n']:.0%})" if scored else "-"
        h5 = f"{b['h5']}/{b['n']} ({b['h5'] / b['n']:.0%})" if scored else "-"
        tops = sorted(b["tops"])
        p90 = tops[int(0.9 * (len(tops) - 1))]
        ih = f"{b['i1']}/{b['ni']}" if scored else "-"
        print(f"{cat:<8} {b['n']:>7} {h1:>12} {h5:>12} {ih:>14} {statistics.median(tops):>17.1f} {p90:>6.1f}")
    print(f"\nconfident (local alone would answer, web skipped): fired on {gate['fired']} of {len(rows)} queries, "
          f"right {gate['right']}/{gate['fired']}; it caught {gate['right']} of the {gate['answerable']} "
          f"queries local search answered correctly at rank 1")
    if meta:
        by_group(meta, details)
    cols = ["nav", "local", "fresh", "shop", "howto", "other"]
    print("\nintent (search-api's guess from the wording) per hand-labelled category — "
          "each cell: how many of that row's queries got that intent")
    print(f"{'category':<8} " + " ".join(f"{c:>6}" for c in cols))
    for cat in sorted(intents, key=lambda c: order.index(c) if c in order else 99):
        print(f"{cat:<8} " + " ".join(f"{intents[cat].get(c, 0):>6}" for c in cols))
    if a.details:
        with open(a.details, "w", encoding="utf-8") as f:
            f.write("query\tcategory\trank\ttop_score\ttop_url\tconfident" + ("\tset\tstratum" if meta else "") + "\n")
            for i, d in enumerate(details):
                f.write("\t".join(map(str, d + (meta[i] if meta else ()))) + "\n")


if __name__ == "__main__":
    main()
