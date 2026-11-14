#!/usr/bin/env python3
"""Offline kill-rule check (2026-11-10): how much of the gold pages does the sitemap harvest hold, against what the release
already holds? Usage: sitemap-coverage-check.py HARVEST.jsonl REGISTRY.sqlite SITENAV.jsonl [--cap200]"""
import collections, json, re, sqlite3, subprocess, sys, urllib.parse

import os
W = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
harvest_p, reg_p, nav_p = sys.argv[1:4]
CAP200 = "--cap200" in sys.argv
def usplit(u):
    try:
        return urllib.parse.urlsplit(u)
    except ValueError:
        return urllib.parse.urlsplit("http://invalid/")


SECOND = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go"}


def regdom(h):
    p = h.lower().split(".")
    if len(p) >= 3 and p[-2] in SECOND and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def norm(u):
    """host/path, lower case, no scheme, no www., no query, no trailing slash."""
    u = u.strip()
    if "://" not in u:
        u = "http://" + u
    p = usplit(u)
    h = (p.hostname or "").lower()
    h = h[4:] if h.startswith("www.") else h
    path = urllib.parse.unquote(p.path).lower().rstrip("/")
    return h + path


def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", urllib.parse.unquote(s).lower()) if t]


def stem(t):
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


STOP = set("the a an of for and in on to with by at from official site website online buy shop store new best how".split())

# ---- gold
mp = [ln.split("\t") for ln in open(f"{W}/apps/search/tools/evalsets/open/maker-product.tsv").read().splitlines()[1:]]
slot = [ln.split("\t") for ln in subprocess.run(["git", "-C", W, "show", "feat/intent-slots:apps/search/tools/evalsets/open/site-slot.tsv"],
                                                  capture_output=True, text=True).stdout.splitlines()[1:]]
slot = [r for r in slot if r[5] == "test"]
gold_doms = {regdom(r[2].split("/")[0]) for r in mp} | {regdom(r[2].split("/")[0]) for r in slot}
slot_norm = {r[2]: norm(r[2]) for r in slot}
slot_hosts = {n.split("/")[0] for n in slot_norm.values()}

ORIG = None
if "--orig" in sys.argv:        # only rows whose site was already in the card-site list (the gold hosts added to the harvest are left out)
    ORIG = {regdom(json.loads(ln)["site"]) for ln in open(sys.argv[sys.argv.index("--orig") + 1])}
    mp = [r for r in mp if regdom(r[2].split("/")[0]) in ORIG]
    slot = [r for r in slot if regdom(r[2].split("/")[0]) in ORIG]
    gold_doms = {regdom(r[2].split("/")[0]) for r in mp} | {regdom(r[2].split("/")[0]) for r in slot}
    slot_norm = {r[2]: norm(r[2]) for r in slot}

# ---- harvest: per site its URL set (cap 5000 as read; optionally the first 200 by a simple choice)
H_urls = collections.defaultdict(list)      # regdom -> [(url, title)]
n_sites = n_sm = n_blocked = 0
for ln in open(harvest_p):
    r = json.loads(ln)
    n_sites += 1
    n_sm += bool(r.get("has_sitemap"))
    for u in r.get("urls", []):
        d = regdom(usplit(u[0]).hostname or "")
        if d in gold_doms:
            H_urls[d].append((u[0], u[1], u[2]))


def pick200(us):
    """a plausible per-site keeper: newest lastmod first, then shallow paths, then wordier addresses, no tag/page/query addresses."""
    bad = re.compile(r"/(tag|tags|page|category|author|feed|wp-json)/|[?&](utm_|page=|p=)|/page/\d", re.I)
    c = [u for u in us if not bad.search(u[0])]
    c.sort(key=lambda u: (u[1] or "", -len(re.findall(r"[a-z]{3,}", u[0].lower()))), reverse=True)
    c.sort(key=lambda u: usplit(u[0]).path.count("/"))
    return c[:200]


if CAP200:
    H_urls = {d: pick200(v) for d, v in H_urls.items()}

# ---- release: registry pages (host_id rows) + site rows + sitenav links, only for gold domains
R_urls = collections.defaultdict(list)      # regdom -> [(url, title)]
db = sqlite3.connect(f"file:{reg_p}?mode=ro&immutable=1", uri=True)
for origin, path, title in db.execute("select h.origin, r.path, r.title from registry r join hosts h on h.id = r.host_id"):
    d = regdom(usplit(origin).hostname or "")
    if d in gold_doms:
        R_urls[d].append((origin + (path or ""), "", title or ""))
SITE0 = 2 << 28
for doc, path, title, alt in db.execute("select doc_id, path, title, alt from registry where doc_id >= ? and doc_id < ?", (SITE0, SITE0 + (1 << 28))):
    if path and regdom(path) in gold_doms:
        R_urls[regdom(path)].append((alt or "http://" + path, "", title or ""))
nav_links = 0
for ln in open(nav_p):
    r = json.loads(ln)
    d = regdom(r["site"])
    if d in gold_doms:
        for l in r.get("links", []):
            R_urls[d].append((l["url"], "", l.get("text", "")))
            nav_links += 1

# ---- B: site-slot test rows, gold = a page address
_ns = {}
def held(table, g):
    d = regdom(g.split("/")[0])
    k = (id(table), d)
    if k not in _ns:
        _ns[k] = {norm(u[0]) for u in table.get(d, [])}
    return slot_norm_cache(g) in _ns[k]


_sn = {}
def slot_norm_cache(g):
    if g not in _sn:
        _sn[g] = norm(g)
    return _sn[g]


rows = []
for r in slot:
    g = r[2]
    has_path = "/" in g.strip("/")
    rows.append((g, has_path, held(R_urls, g), held(H_urls, g)))


def share(sel):
    n = len(sel)
    rel = sum(1 for x in sel if x[2]); har = sum(1 for x in sel if x[3]); uni = sum(1 for x in sel if x[2] or x[3])
    return n, 100 * rel / n, 100 * har / n, 100 * uni / n, 100 * (uni - rel) / n


print(f"harvest: {n_sites} sites, {n_sm} with a sitemap; gold-domain URLs in harvest {sum(len(v) for v in H_urls.values()):,}; "
      f"release URLs for gold domains {sum(len(v) for v in R_urls.values()):,} (sitenav links {nav_links:,}); cap200={CAP200}")
print("\n| set | rows | release holds | harvest holds | union | added by harvest (points) |\n|---|---|---|---|---|---|")
for name, sel in (("site-slot test, all 522", rows), ("site-slot test, gold has a path", [x for x in rows if x[1]]),
                  ("site-slot test, gold is a bare site", [x for x in rows if not x[1]])):
    n, a, b, c, d = share(sel)
    print(f"| {name} | {n} | {a:.1f}% | {b:.1f}% | {c:.1f}% | {d:+.1f} |")
# only rows whose site was in the harvest at all
inh = [x for x in rows if x[1] and regdom(x[0].split('/')[0]) in H_urls]
if inh:
    n, a, b, c, d = share(inh)
    print(f"| site-slot test, gold has a path and its site returned URLs | {n} | {a:.1f}% | {b:.1f}% | {c:.1f}% | {d:+.1f} |")

# ---- A: maker-product, gold has no page; proxy = a page of the gold site whose address or title holds all the product words
def covers(table, dom, words):
    need = {stem(w) for w in words}
    for u in table.get(dom, []):
        have = {stem(t) for t in toks(usplit(u[0]).path)} | {stem(t) for t in toks(u[2])}
        if need <= have:
            return True
    return False


mrows = []
for r in mp:
    q, dom = r[0], regdom(r[2].split("/")[0])
    label = dom.split(".")[0]
    words = [t for t in toks(q) if t not in STOP and not (len(t) >= 3 and (t in label or label in t))]
    if not words:
        continue
    mrows.append((q, dom, words, covers(R_urls, dom, words), covers(H_urls, dom, words)))
n = len(mrows)
rel = sum(1 for x in mrows if x[3]); har = sum(1 for x in mrows if x[4]); uni = sum(1 for x in mrows if x[3] or x[4])
print(f"\nmaker-product: {len(mp)} rows, {n} with product words left after removing the maker's name")
print("\n| set | rows | release holds a page with the words | harvest does | union | added (points) |\n|---|---|---|---|---|---|")
print(f"| maker-product (proxy: a page of the gold site with all product words in address or title) | {n} | {100*rel/n:.1f}% | {100*har/n:.1f}% | {100*uni/n:.1f}% | {100*(uni-rel)/n:+.1f} |")
dom_in = [x for x in mrows if x[1] in H_urls]
if dom_in:
    n2 = len(dom_in); rel = sum(1 for x in dom_in if x[3]); har = sum(1 for x in dom_in if x[4]); uni = sum(1 for x in dom_in if x[3] or x[4])
    print(f"| ... only rows whose maker site returned URLs | {n2} | {100*rel/n2:.1f}% | {100*har/n2:.1f}% | {100*uni/n2:.1f}% | {100*(uni-rel)/n2:+.1f} |")
if "--examples" in sys.argv:
    import random
    random.Random(1).shuffle(mrows)
    print("\nmaker-product rows the harvest covers and the release does not:")
    for q, dom, words, a, b in [x for x in mrows if x[4] and not x[3]][:8]:
        u = next(u for u in H_urls[dom] if {stem(w) for w in words} <= ({stem(t) for t in toks(usplit(u[0]).path)} | {stem(t) for t in toks(u[2])}))
        print(f"- {q!r} ({dom}): {u[0]}")
    print("\nsite-slot rows the harvest holds and the release does not:")
    for x in [x for x in rows if x[3] and not x[2]][:8]:
        print("-", x[0])
