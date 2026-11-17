"""The matrix group: many kinds of search, asked several ways, with the timings recorded per request.
Types (tests/regress/data/matrix/<type>.tsv, built from the open eval sets by tools/build_matrix_data.py): brand, person,
place, howto, fact, misspell, product, gov, account, short, long, live, word.
Ways: cold (the first ask in this run), repeat (the same again), typing (every prefix length 2..10 of a sample, the form the
page sends while typing), parallel (4 at a time: each measured while three others run), and with a home position (`near=lat,lon`)
for the types where it matters (place near-me, weather, government tasks).
Record per request: type, mode, query, ok, card title, official link, first result, result count, cache state (from /storez
counters, serial modes only), engine ms (the engine's own `ms`), wall ms, per-stage times, expectation and whether it was met."""
import concurrent.futures as cf
import json
import os
import statistics
import time

from rg_http import facts, host, percentile

TYPES = ["brand", "person", "place", "howto", "fact", "misspell", "product", "gov", "account", "short", "long", "live", "word"]
MODES = ["cold", "repeat", "typing", "parallel"]
COLD_MAX_MS, REPEAT_MAX_MS = 2000, 100


def load_type(root, typ):
    out = []
    with open(os.path.join(root, "tests", "regress", "sets", "matrix", typ + ".tsv"), encoding="utf-8") as f:
        for ln in f:
            c = ln.rstrip("\n").split("\t") + ["", "", ""]
            if c[0].strip():
                out.append((c[0], c[1], c[2]))
    return out


def home_points(root):
    pts = {}
    with open(os.path.join(root, "tests", "regress", "sets", "matrix", "home-points.tsv"), encoding="utf-8") as f:
        for ln in f:
            c = ln.split("\t")
            if len(c) >= 4:
                pts[c[0]] = f"{c[2]},{c[3].strip()}"
    return pts


def _right(expect, d):
    if not expect or d is None:
        return None
    kind, _, val = expect.partition(":")
    alts = [v for v in val.split("|") if v]
    if kind == "host":
        res = d.get("results") or []
        h = host(res[0].get("url")) if res else ""
        return any(a in h for a in alts)
    if kind == "card":
        return any(a.lower() in ((d.get("card") or {}).get("title") or "").lower() for a in alts)
    return None


def _cache(z0, z1):
    if not z0 or not z1:
        return "n/a"
    for k, name in (("finished_hit", "fin"), ("hit", "hit"), ("prev_hit", "prev"), ("curated_hit", "cur"), ("miss", "miss")):
        if z1.get(k, 0) - z0.get(k, 0) > 0:
            return name
    return "none"


def one(t, typ, mode, q, expect, near=None, typing=False, prefix_len=0, serial=False):
    extra = {"near": near} if near else {}
    z0 = t.storez() if serial else None
    if typing:
        d, secs, err = t.instant(q, results=0, full=0, **extra)
    else:
        d, secs, err = t.instant(q, **extra)
    z1 = t.storez() if serial else None
    row = dict(type=typ, mode=mode, q=q, prefix_len=prefix_len, near=bool(near), ok=d is not None, err=err, wall_ms=round(secs * 1000), expect=expect)
    if d is not None:
        card, off, first = facts(d)
        res = d.get("results") or []
        row.update(card=card, official=off, first=(res[0].get("url") if res else ""), n_results=len(res), ms=d.get("ms"),
                   stages=d.get("stages"), right=_right(expect, d), cache=_cache(z0, z1) if serial else "n/a",
                   suggest=len(d.get("suggest") or []) if typing else None)
    return row


def run_type(t, root, typ, n=100, modes=MODES, workers=4, typing_sample=25, log=None):
    items = load_type(root, typ)
    pts = home_points(root)
    keys = sorted(pts)
    rows = []
    first, spare = items[:n], items[n:2 * n] or items[:n]

    def point(i, flags):
        for fl in flags.split():
            if fl.startswith("gov:") and fl[4:] in pts:
                return pts[fl[4:]]
        return pts[keys[i % len(keys)]]

    if "cold" in modes:
        for i, (q, ex, fl) in enumerate(first):
            rows.append(one(t, typ, "cold", q, ex, serial=True))
        for i, (q, ex, fl) in enumerate(first):
            if "near" in fl.split():
                rows.append(one(t, typ, "cold_near", q, ex, near=point(i, fl), serial=True))
    if "repeat" in modes:
        for i, (q, ex, fl) in enumerate(first):
            rows.append(one(t, typ, "repeat", q, ex, serial=True))
        for i, (q, ex, fl) in enumerate(first):
            if "near" in fl.split():
                rows.append(one(t, typ, "repeat_near", q, ex, near=point(i, fl), serial=True))
    if "typing" in modes:
        for q, ex, fl in first[:typing_sample]:
            for k in range(2, min(len(q), 10) + 1):
                rows.append(one(t, typ, "typing", q[:k], "", typing=True, prefix_len=k, serial=False))
    if "parallel" in modes and workers > 1:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, t, typ, "parallel", q, e, point(i, fl) if "near" in fl.split() else None) for i, (q, e, fl) in enumerate(spare)]
            rows += [f.result() for f in futs]
    return rows


def stats(v):
    v = [x for x in v if x is not None]
    if not v:
        return None
    return dict(n=len(v), p50=round(percentile(v, .5)), p90=round(percentile(v, .9)), p99=round(percentile(v, .99)), max=round(max(v)))


def summarize(rows):
    out = {}
    for typ in TYPES:
        rs = [r for r in rows if r["type"] == typ]
        if not rs:
            continue
        cold = [r for r in rs if r["mode"] == "cold"]
        o = dict(requests=len(rs), queries=len(cold), errors=sum(1 for r in rs if not r["ok"]))
        o["answered"] = round(100 * sum(1 for r in cold if r["ok"] and (r.get("n_results") or r.get("card") not in (None, "-"))) / max(1, len(cold)), 1)
        o["card_rate"] = round(100 * sum(1 for r in cold if r.get("card") not in (None, "-")) / max(1, len(cold)), 1)
        ex = [r for r in cold if r["expect"]]
        o["right_rate"] = round(100 * sum(1 for r in ex if r.get("right")) / len(ex), 1) if ex else None
        o["right_n"] = len(ex)
        for m in ("cold", "repeat", "typing", "parallel", "cold_near", "repeat_near"):
            ms = [r for r in rs if r["mode"] == m and r["ok"]]
            o[m] = dict(wall=stats([r["wall_ms"] for r in ms]), engine=stats([r.get("ms") for r in ms]))
        o["cold_over_2s"] = sum(1 for r in cold if (r.get("ms") or 0) > COLD_MAX_MS)
        o["repeat_over_100ms"] = sum(1 for r in rs if r["mode"] == "repeat" and (r.get("ms") or 0) > REPEAT_MAX_MS)
        o["cache"] = {}
        for r in cold:
            o["cache"][r.get("cache")] = o["cache"].get(r.get("cache"), 0) + 1
        out[typ] = o
    return out


def fmt(s):
    return f"{s['p50']}/{s['p90']}/{s['p99']}/{s['max']}" if s else "-"


def table(summary, name):
    L = [f"\n== matrix: {name} ==  (ms; p50/p90/p99/max of the engine's own time, wall time in the last columns' second line)",
         f"{'type':9s} {'n':>4s} {'ans%':>5s} {'card%':>6s} {'right%':>8s} {'err':>3s} | {'cold e p50/90/99/max':>24s} | {'repeat e':>20s} | {'typing e':>20s} | {'parallel e':>20s} | >2s >100ms"]
    for typ, o in summary.items():
        rt = f"{o['right_rate']}({o['right_n']})" if o["right_rate"] is not None else "-"
        L.append(f"{typ:9s} {o['queries']:4d} {o['answered']:5.1f} {o['card_rate']:6.1f} {rt:>8s} {o['errors']:3d} | {fmt(o['cold']['engine']):>24s} | {fmt(o['repeat']['engine']):>20s} | {fmt(o['typing']['engine']):>20s} | {fmt(o['parallel']['engine']):>20s} | {o['cold_over_2s']:3d} {o['repeat_over_100ms']:5d}")
        L.append(f"{'  wall':9s} {'':4s} {'':5s} {'':6s} {'':8s} {'':3s} | {fmt(o['cold']['wall']):>24s} | {fmt(o['repeat']['wall']):>20s} | {fmt(o['typing']['wall']):>20s} | {fmt(o['parallel']['wall']):>20s} |")
    return "\n".join(L)


def compare(rows_a, rows_b, name_a, name_b):
    """Per type: the same query answered identically (card, official link, first result, result count) and the time ratio."""
    def key(r):
        return (r["type"], r["q"], r["near"])
    A = {key(r): r for r in rows_a if r["mode"] in ("cold", "cold_near") and r["ok"]}
    B = {key(r): r for r in rows_b if r["mode"] in ("cold", "cold_near") and r["ok"]}
    out, diffs = {}, []
    for typ in TYPES:
        ks = [k for k in A if k[0] == typ and k in B]
        if not ks:
            continue
        same = 0
        for k in ks:
            a, b = A[k], B[k]
            fa, fb = (a["card"], a["official"], a["first"], a["n_results"]), (b["card"], b["official"], b["first"], b["n_results"])
            if fa == fb:
                same += 1
            else:
                diffs.append(dict(type=typ, q=k[1], near=k[2], a=fa, b=fb))
        ea = [A[k]["ms"] for k in ks if A[k].get("ms") is not None]
        eb = [B[k]["ms"] for k in ks if B[k].get("ms") is not None]
        wa, wb = [A[k]["wall_ms"] for k in ks], [B[k]["wall_ms"] for k in ks]
        pa, pb = percentile(ea, .5), percentile(eb, .5)
        out[typ] = dict(compared=len(ks), identical=same, identical_pct=round(100 * same / len(ks), 1),
                        engine_p50_a=round(pa), engine_p50_b=round(pb), engine_ratio=round(pa / pb, 2) if pb else None,
                        p90_a=round(percentile(ea, .9)), p90_b=round(percentile(eb, .9)),
                        wall_p50_a=round(percentile(wa, .5)), wall_p50_b=round(percentile(wb, .5)))
    return out, diffs


def compare_table(cmp, name_a, name_b):
    L = [f"\n== {name_a} vs {name_b}: the same searches, cold ==",
         f"{'type':9s} {'both':>5s} {'identical':>10s} {'%':>6s} | engine p50 A / B  ratio | engine p90 A / B | wall p50 A / B"]
    for typ, o in cmp.items():
        L.append(f"{typ:9s} {o['compared']:5d} {o['identical']:10d} {o['identical_pct']:6.1f} | {o['engine_p50_a']:6d} / {o['engine_p50_b']:<6d} {o['engine_ratio'] if o['engine_ratio'] is not None else '-':>5} | {o['p90_a']:6d} / {o['p90_b']:<6d} | {o['wall_p50_a']:6d} / {o['wall_p50_b']:<6d}")
    return "\n".join(L)
