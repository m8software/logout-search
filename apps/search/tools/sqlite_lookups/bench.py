"""T4 bench: search_api's lookups on Postgres vs lookup.sqlite, same code path, same queries.

  python bench.py gen QSETS.json QUERY_FILES...      query sets (reads Postgres, read-only)
  python bench.py run pg   QSETS.json OUT.json       Postgres (the live serving DB)
  python bench.py run lite QSETS.json OUT.json DB [fts|tv TVDIR]
                                                     SQLite; the file's pages are evicted from the
                                                     page cache first, so pass 1 is cold
  python bench.py compare PG.json LITE.json          agreement + timings (markdown)

Both backends run master's search_api functions (place_exact, suggest, suggest_phrases,
correct_query, _rows): on "lite" its connection is lite.Lite, which answers each SQL
constant from SQLite. Each group runs twice in one process: pass 1 (first) and pass 2 (warm).
"""
import contextlib, json, os, random, re, statistics, sys, time
sys.path.insert(0, "/w")
import search_api as s

HOMES = {"testbox-home": (40.592, -105.871), "nyc": (40.758, -73.986), "la": (34.052, -118.244), "rural-sd": (44.368, -100.351)}


def read_queries(paths):
    out = []
    for p in paths:
        for line in open(p, encoding="utf-8"):
            if not line.strip() or line.startswith("#") or line.startswith("query\t"):
                continue
            q = line.rstrip("\n").split("\t")[0].strip()
            if q:
                out.append(q)
    return list(dict.fromkeys(out))


def mutate(txt, n, rnd):
    for _ in range(n):
        idx = [i for i, ch in enumerate(txt) if ch.isalpha()]
        if len(idx) < 3:
            break
        i = rnd.choice(idx[1:])
        op = rnd.randrange(4)
        if op == 0:
            txt = txt[:i] + txt[i + 1:]
        elif op == 1:
            txt = txt[:i] + rnd.choice("aeiourstnlc") + txt[i + 1:]
        elif op == 2:
            txt = txt[:i] + rnd.choice("aeiourstnlc") + txt[i:]
        elif i + 1 < len(txt):
            txt = txt[:i] + txt[i + 1] + txt[i] + txt[i + 2:]
    return txt


def gen(out, files):
    rnd = random.Random(20261001)
    Q = read_queries(files)
    print("real queries:", len(Q))
    with s._conn() as c:
        c.execute("SET statement_timeout = 0")
        qs = {"places_kinds": [], "places_name": [], "place_exact": [], "suggest": [], "typo": [], "raw": []}
        for h, (lat, lon) in HOMES.items():
            for k in s.PLACE_KINDS:
                qs["places_kinds"].append({"home": h, "key": k})
            d = 0.36
            near = [r[0] for r in c.execute(
                "SELECT DISTINCT lower(name) FROM places WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s AND name <> ''",
                (lat - d, lat + d, lon - d / 0.77, lon + d / 0.77))]
            near.sort()
            rnd.shuffle(near)
            near = [n for n in near if (s.place_query(n) or ("",))[0] == "name"][:40]
            names = near + [mutate(n, 1, rnd) for n in near[:20] if len(n) >= 6]
            if h == "testbox-home":
                names += [q for q in Q if (s.place_query(q) or ("",))[0] == "name"][:300]
            for n in dict.fromkeys(names):
                pq = s.place_query(n)
                if pq and pq[0] == "name":
                    qs["places_name"].append({"home": h, "name": pq[1]})
                    qs["place_exact"].append({"home": h, "q": n})
        chains = [r[0] for r in c.execute("SELECT lower(name) FROM places WHERE name <> '' GROUP BY 1 ORDER BY count(*) DESC LIMIT 25")]
        for h in HOMES:
            for n in chains:
                qs["places_name"].append({"home": h, "name": n})
                qs["place_exact"].append({"home": h, "q": n})
        # suggestions: prefixes of what people type
        pre = []
        for q in Q:
            for n in (3, 4, 5, 7, 10, 100):
                pre.append(q[:n])
        pre = list(dict.fromkeys(p for p in pre if len(s.tight(p)) >= s.SUGGEST_MIN))
        rnd.shuffle(pre)
        qs["suggest"] = pre[:2500]
        # typo correction: famous names with typos, the same names spelt right, real queries
        top = [r[0] for r in c.execute("SELECT name FROM suggest_items WHERE kind IN ('wiki', 'site') ORDER BY demand DESC LIMIT 4000")]
        top = [re.sub(r"\s*\(.*\)$", "", t) for t in top]
        top = [t for t in dict.fromkeys(top) if len(s.tight(t)) >= s.TYPO_MIN]
        rnd.shuffle(top)
        typos = [{"q": mutate(t, s.max_edits(s.tight(t)), rnd), "kind": "typo", "orig": t} for t in top[:300]]
        ctrl = [{"q": t, "kind": "control"} for t in top[300:450]]
        real = [{"q": q, "kind": "real"} for q in Q if s.typo_shape(q)][:600]
        qs["typo"] = typos + ctrl + real
        # exact lookups (cards, accounts, nav, kinds, stocks)
        raw = []
        titles = []
        for q in Q:
            nq = s.norm_key(q)
            keys = s.nav_keys(q)
            if len(nq) >= 2:
                raw.append(("WIKI_EXACT_SITE_SQL", {"nq": nq}))
                titles += [r[0] for r in c.execute(s.WIKI_EXACT_SITE_SQL, {"nq": nq})]
            if 3 <= len(nq) <= 30:
                raw.append(("FAMOUS_SQL", {"k": nq}))
                raw.append(("ORG_SQL", {"k": nq}))
                raw.append(("FACTS_SQL", {"keys": [nq], "facts": sorted({f for v in s.QUESTION_FACTS.values() for f in v})}))
            if keys:
                raw.append(("NAV_SITES_SQL", {"keys": keys}))
                raw.append(("NAV_SQL", {"keys": keys, "generic": s.GENERIC_TLD}))
                raw.append(("HOST_NAV_SQL", {"keys": [k.replace("-", "") for k in keys]}))
            acct = s.account_query(q)
            if acct:
                raw.append(("ACCOUNT_SQL", {"p": acct[0], "key": acct[1], "keys": s.the_keys(acct[1])}))
            yk = s.youtube_query(q)[0]
            if yk:
                raw.append(("YT_EXACT_SQL", {"key": yk}))
            sk = s.subreddit_query(q)[0]
            if sk:
                raw.append(("SUB_EXACT_SQL", {"key": sk}))
            st = s.stock_query(q)
            if st:
                raw.append(("STOCK_SQL", {"k": st, "cls": "#"}))
            kq = s.kind_query(q)
            if kq:
                raw.append(("KIND_KEYS_SQL", {"key": kq, "w": kq.split(), "min": s.KIND_MIN_MEMBERS}))
                kk = [r[0] for r in c.execute(s.KIND_KEYS_SQL, {"key": kq, "w": kq.split(), "min": s.KIND_MIN_MEMBERS})]
                if kk:
                    raw.append(("KIND_KEY", {"keys": kk, "k": s.KIND_TOP + 8, "exact": kq, "exact_w": s.KIND_EXACT_W}))
        for t in list(dict.fromkeys(titles))[:800]:
            raw += [("CARD_DESCR_SQL", {"t": t}), ("RELATED_SQL", {"t": t, "n": s.RELATED_N}), ("PERSON_SQL", {"t": t}),
                    ("PROFILES_SQL", {"t": t, "p": s.PROFILE_PLATFORMS})]
        tl = list(dict.fromkeys(titles))
        for i in range(0, min(len(tl), 600), 3):
            raw.append(("WIKI_DEMAND_SQL", {"t": tl[i:i + 3]}))
        for st in ("netflix", "apple", "berkshire hathaway", "tesla", "nvidia", "ford", "walmart", "costco"):
            raw.append(("STOCK_SQL", {"k": st, "cls": "#"}))
        for (h,) in c.execute("SELECT host FROM nav_sites ORDER BY md5(host) LIMIT 150"):
            raw.append(("DOMAIN_NAV_SQL", {"d": h}))
        qs["raw"] = [{"sql": a, "p": b} for a, b in raw]
    for k, v in qs.items():
        print(k, len(v))
    json.dump(qs, open(out, "w"))


# ------------------------------------------------------------------ run
LEAD_AT = {"WIKI_EXACT_SITE_SQL": 1, "RELATED_SQL": 1}


def norm(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    return v


def place_params(home):
    lat, lon = HOMES[home]
    dlat = s.PLACE_RADIUS_KM / 111.32
    return {"lat": lat, "lon": lon, "dlat": dlat, "dlon": dlat / max(0.1, s.math.cos(s.math.radians(lat))), "r": s.PLACE_RADIUS_KM}


def one(group, item):
    if group == "places_kinds":
        p = place_params(item["home"]); p["kinds"] = s.PLACE_KINDS[item["key"]]
        rows = s._rows(s.PLACES_SQL.format(match=s.PLACE_MATCH_KINDS), p)
        return [[r[0], round(float(r[8]), 6)] for r in rows]
    if group == "places_name":
        p = place_params(item["home"]); p["name"] = item["name"]
        rows = s._rows(s.PLACES_SQL.format(match=s.PLACE_MATCH_NAME), p, s.PLACE_NAME_SETUP)
        return [[r[0], round(float(r[8]), 6)] for r in rows]
    if group == "place_exact":
        s.home_point = lambda: HOMES[item["home"]]
        return norm(s.place_exact(item["q"]))
    if group == "suggest":
        items = s.suggest(item)
        return [[d["url"], d["title"]] for d in items] + [["phrase", p] for p in s.suggest_phrases(item, items)]
    if group == "typo":
        return s.correct_query(item["q"])
    if group == "raw":
        name, p = item["sql"], item["p"]
        with s._conn() as c:
            if name == "KIND_KEY":
                rows = c.execute(s.kind_key_sql(c), p).fetchall()
            else:
                rows = c.execute(getattr(s, name), p).fetchall()
        rows = [list(r) for r in rows]
        if name in LEAD_AT:
            for r in rows:
                r[LEAD_AT[name]] = None
        if name == "PROFILES_SQL":
            rows.sort(key=lambda r: r[0])
        return norm(rows)


def run(backend, qsets, out, db=None, typo="fts", tv=None):
    qs = json.load(open(qsets))
    if backend == "lite":
        import lite
        L = lite.Lite(db, typo=typo, tv_path=tv)
        s._conn = L.conn
        paths = [db] + ([os.path.join(tv, f) for f in os.listdir(tv)] if tv else [])
        for p in paths:
            if os.path.isfile(p):
                fd = os.open(p, os.O_RDONLY)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.close(fd)
    else:
        with s._conn() as c:
            c.execute("SELECT 1")
    s._tables = (s._NEVER, frozenset())
    res = {}
    only = os.environ.get("T4_ONLY", "")
    for group, items in qs.items():
        if only and group not in only.split(","):
            continue
        ms = [[], []]
        outs = []
        for ps in (0, 1):
            for it in items:
                t = time.perf_counter()
                try:
                    o = one(group, it)
                except Exception as e:
                    o = {"error": f"{type(e).__name__}: {e}"[:200]}
                ms[ps].append((time.perf_counter() - t) * 1000)
                if ps == 0:
                    outs.append(o)
        res[group] = {"items": items, "out": outs, "ms1": ms[0], "ms2": ms[1]}
        print(group, len(items), "p50 first/warm", round(statistics.median(ms[0]), 2), round(statistics.median(ms[1]), 2), flush=True)
    json.dump(res, open(out, "w"))


# ------------------------------------------------------------------ compare
def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def compare(pgf, litef, label="SQLite"):
    A, B = json.load(open(pgf)), json.load(open(litef))
    print(f"| lookup | n | same answer | ties only | differ | errors | PG p50 first/warm ms | PG p90 first/warm | {label} p50 first/warm | {label} p90 first/warm |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    details = {}
    groups = [g for g in A if g in B]
    for g in groups:
        a, b = A[g], B[g]
        if g == "raw":
            names = sorted({it["sql"] for it in a["items"]})
            for nm in names:
                idx = [i for i, it in enumerate(a["items"]) if it["sql"] == nm]
                row(f"raw {nm}", [a["out"][i] for i in idx], [b["out"][i] for i in idx], [a["ms1"][i] for i in idx],
                    [a["ms2"][i] for i in idx], [b["ms1"][i] for i in idx], [b["ms2"][i] for i in idx], details, g + ":" + nm,
                    [a["items"][i] for i in idx])
            continue
        row(g, a["out"], b["out"], a["ms1"], a["ms2"], b["ms1"], b["ms2"], details, g, a["items"])
    return details


def tie(g, x, y):
    """Same sort keys, different members: what Postgres's own LIMIT over equal keys does."""
    if g.startswith("places") and isinstance(x, list) and isinstance(y, list):
        return [d for _, d in x] == [d for _, d in y]
    if isinstance(x, list) and isinstance(y, list):
        return sorted(map(json.dumps, x)) == sorted(map(json.dumps, y))
    return False


def row(g, xa, xb, a1, a2, b1, b2, details, key, items):
    same = ties = errs = 0
    diffs = []
    for i, (x, y) in enumerate(zip(xa, xb)):
        if isinstance(y, dict) and "error" in y:
            errs += 1; diffs.append((items[i], x, y)); continue
        if x == y:
            same += 1
        elif tie(g, x, y):
            ties += 1
        else:
            diffs.append((items[i], x, y))
    n = len(xa)
    f = lambda v: f"{v:.1f}" if v >= 1 else f"{v:.2f}"
    print(f"| {g} | {n} | {same} ({100 * same / n:.1f}%) | {ties} | {len(diffs) - errs} | {errs} | "
          f"{f(statistics.median(a1))} / {f(statistics.median(a2))} | {f(pct(a1, .9))} / {f(pct(a2, .9))} | "
          f"{f(statistics.median(b1))} / {f(statistics.median(b2))} | {f(pct(b1, .9))} / {f(pct(b2, .9))} |")
    details[key] = diffs


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "gen":
        gen(sys.argv[2], sys.argv[3:])
    elif cmd == "run":
        be = sys.argv[2]
        if be == "pg":
            run("pg", sys.argv[3], sys.argv[4])
        else:
            run("lite", sys.argv[3], sys.argv[4], sys.argv[5], *(sys.argv[6:8]))
    elif cmd == "compare":
        d = compare(sys.argv[2], sys.argv[3], *(sys.argv[4:5]))
        if len(sys.argv) > 5:
            json.dump(d, open(sys.argv[5], "w"), indent=1, default=str)
