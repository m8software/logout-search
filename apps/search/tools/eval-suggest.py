#!/usr/bin/env python3
"""Typing-suggestion eval (owner 2026-11-15: "our autocomplete is really bad"): ~100 prefixes with the suggestion that must come first.

  eval-suggest.py --standin [--api DIR]   the engine's own rank_suggestions on a stand-in catalogue (evalsets/suggest/standin.json),
                                          no database; --api points at another engine checkout (before/after)
  eval-suggest.py --live http://host:port  ask a running engine: /instant?q=<prefix>&results=0, in the order the page shows
Prints first-suggestion hits, top-3 hits and the wording problems (page titles, infrastructure hosts, one entity twice, obscure rows
shown beside popular ones). Exit 1 under --min-first. The stand-in numbers are made up; only a live engine on the release says how
the real ranking does (run it on staging)."""
import argparse, json, os, re, sys, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "evalsets", "suggest", "standin.json")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def page_order(resp, old=False):
    """What the page shows, in order: the lead suggestion(s), the searched phrases, the rest (old page: phrases first, always)."""
    ents = resp.get("suggest") or []
    ph = [{"phrase": p} for p in resp.get("phrases") or []]
    lead = 0 if old else min(int(resp.get("suggest_lead") or 0), len(ents))
    return ents[:lead] + ph + ents[lead:]


def problems(items, catalog_demand=None):
    out = {"title_page": 0, "infra": 0, "dup": 0, "obscure": 0}
    seen = set()
    for it in items:
        t = it.get("title") or it.get("phrase") or ""
        if it.get("phrase") is None and (":" in t or len(t) > 40 or "," in t):
            out["title_page"] += 1
        if re.search(r"(apis?|gstatic|amazonaws|cdn\w*)\.(com|net)$", t.lower()):
            out["infra"] += 1
        k = norm(re.sub(r"\s*\(.*\)$", "", t))
        k = re.sub(r"(search|inc|corporation|company)$", "", k)
        if it.get("phrase") is None and k in seen:
            out["dup"] += 1
        seen.add(k)
        if catalog_demand is not None and it.get("phrase") is None and catalog_demand.get(it.get("url"), 1e9) < 100:
            out["obscure"] += 1
    return out


def standin(api_dir):
    sys.path.insert(0, api_dir)
    for m in ("psycopg",):
        try:
            __import__(m)
        except ImportError:
            import types
            mod = types.ModuleType(m); mod.Error = Exception; sys.modules[m] = mod
    import search_api as s
    d = json.load(open(DATA))
    ents = d["entities"]
    demand = {e["url"]: e["demand"] for e in ents}
    old = not hasattr(s, "suggest_name")                      # the engine before the ranking layer
    res = []
    for case in d["prefixes"]:
        t = s.tight(case["prefix"])
        rows = []
        for e in ents:
            nm = s.tight(e["name"]); words = [s.tight(w) for w in e["name"].split()]
            lab = s.tight((e["domain"] or "").split(".")[0]) if e["domain"] else ""
            pos = 0 if (nm.startswith(t) or (lab and lab.startswith(t))) else 1 if any(w.startswith(t) for w in words[1:]) else None
            if pos is None:
                continue
            rows.append((e["id"], e["kind"], e["name"], e["url"], e["domain"], e["wiki"], 0.5, 0.0, float(e["demand"]), None, pos))
        by = {}
        for r in rows:
            by.setdefault(r[1], []).append(r)
        rows = [r for v in by.values() for r in sorted(v, key=lambda r: -(r[8] or 0) * (0.03 if r[10] else 1))[:12]]
        kw = {} if old else {"typed": case["prefix"]}
        sug = s.rank_suggestions(rows, set(), **kw)
        resp = {"suggest": sug, "phrases": d["phrases"].get(case["prefix"], [])[:3 if old or not (sug and sug[0].get("lead")) else s.PHRASE_N_LEAD],
                "suggest_lead": 1 if sug and sug[0].get("lead") else 0}
        res.append((case, resp))
    return res, demand, old


def live(base):
    d = json.load(open(DATA))
    res = []
    for case in d["prefixes"]:
        u = f"{base}/instant?" + urllib.parse.urlencode({"q": case["prefix"], "results": "0"})
        try:
            resp = json.load(urllib.request.urlopen(u, timeout=20))
        except Exception as e:                              # noqa: BLE001
            resp = {"suggest": [], "phrases": [], "error": type(e).__name__}
        res.append((case, resp))
    return res, None, False


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--standin", action="store_true")
    g.add_argument("--live", metavar="URL")
    ap.add_argument("--api", default=os.path.join(HERE, "..", "serve", "api"))
    ap.add_argument("--min-first", type=float, default=0.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    res, demand, old = standin(os.path.abspath(a.api)) if a.standin else live(a.live.rstrip("/"))
    first = top3 = 0
    tot = {"title_page": 0, "infra": 0, "dup": 0, "obscure": 0}
    for case, resp in res:
        order = page_order(resp, old)
        titles = [norm(x.get("title") or x.get("phrase") or "") for x in order]
        want = {norm(x) for x in case["expect"].split("|")}
        first += bool(titles) and titles[0] in want
        top3 += bool(want & set(titles[:3]))
        for k, v in problems(order, demand).items():
            tot[k] += v
        if a.verbose and not (titles and titles[0] in want):
            print(f"  {case['prefix']!r}: want {case['expect']!r}, page shows {[x.get('title') or x.get('phrase') for x in order[:4]]}")
    n = len(res)
    print(f"prefixes {n}: first suggestion right {first} ({first / n:.0%}), in the first three {top3} ({top3 / n:.0%}); "
          f"page-title names {tot['title_page']}, infrastructure hosts {tot['infra']}, one entity twice {tot['dup']}, obscure rows {tot['obscure']}")
    return 1 if first / n < a.min_first else 0


if __name__ == "__main__":
    sys.exit(main())
