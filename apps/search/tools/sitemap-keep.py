#!/usr/bin/env python3
"""Which URLs of a site's sitemap to keep, and the SQLite table of them (2026-11-10).

    sitemap-keep.py eval  HARVEST.jsonl REGISTRY.sqlite SITENAV.jsonl SITESLOT.tsv [--targets TARGETS.tsv --n-card N] [--workers 6]
    sitemap-keep.py build HARVEST.jsonl OUT.sqlite [--rule K3] [--targets TARGETS.tsv --n-card N]

eval: runs every keep rule over the harvest (sitemap-harvest.py output) and measures, per rule, how many rows and bytes
it keeps and how many gold pages of the eval sets survive. Rules are chosen on the DEV side (the site-slot dev half and
half of the maker-product rows, picked by md5 of the query) and reported on the held-out side (the site-slot test half
and the other maker-product half); no rule is tuned on a held-out row. --targets/--n-card: the harvest list was the
card sites (the first N lines) plus gold-answer hosts added after them; with them given, every number is also printed
with those added hosts removed from the harvest side, which a real harvest would not know.

build: the chosen rule's rows as site_pages(host, path, lastmod, title) with an FTS5 table over address words and title,
and a small hosts table. Stdlib only (sqlite3 needs FTS5, which Python's build has).

Gold: site-slot rows expect a page address (exact match after dropping scheme, www., query and trailing slash).
maker-product rows expect only a SITE, so the measure there is a proxy: some kept page of the site has every product word
(the query minus the maker's name) in its address or title.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import multiprocessing
import os
import re
import sqlite3
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SECOND = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go"}
# What a person types after a name to reach a part of the site (login, returns, support, pricing, ...): the single words
# of the slot vocabulary in serve/api/site_slots.py (feat/intent-slots), plus the product and listing words a path uses.
SLOT_PATH = frozenset("""login signin account signup register contact support help faq returns shipping delivery tracking
pricing plans prices careers jobs docs documentation developers api download downloads app locations stores hours
investors press newsroom cancel billing payments gift giftcards menu order reservations booking tickets events schedule
shop store products product about team leadership news blog privacy terms cookies legal accessibility sitemap
donate volunteer rewards deals franchise partners admissions directions feedback security status""".split())
PRODUCT_SEG = frozenset("""product products p pd pdp dp ip item items sku skus catalog catalogue shop model models goods prod
buy detail collections t store""".split())
FILE_PROD = frozenset("product products prod item items catalog catalogue shop sku skus model models goods pdp".split())
_PROD_TAIL = re.compile(r"[-_/]p[-_]?\d{3,}/?$|/(?:dp|ip)/[a-z0-9]{6,}/?$")
_LOCALE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2,4})?$")
_ENGLISH = {"en", "en-us", "en_us", "en-gb", "en_gb", "en-au", "en-ca", "us", "uk"}
_JUNK_SEG = frozenset(("tag", "tags", "category", "categories", "author", "authors", "feed", "feeds", "wp-json", "wp-content",
                       "search", "cart", "checkout", "basket", "compare", "wishlist", "archive", "archives"))
_PAGINATED = re.compile(r"/(?:page|p|pg)/?\d+/?$|[-_/]page[-_]?\d+/?$|/\d{1,3}/?$")
_DATED_ARCHIVE = re.compile(r"/(?:19|20)\d\d(?:/(?:0?[1-9]|1[0-2])(?:/(?:0?[1-9]|[12]\d|3[01]))?)?/?$")
_WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset("the a an of for and in on to with by at from official site website online buy shop store new best how".split())


def regdom(h: str) -> str:
    p = h.lower().split(".")
    if len(p) >= 3 and p[-2] in SECOND and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def usplit(u: str):
    try:
        return urllib.parse.urlsplit(u)
    except ValueError:
        return urllib.parse.urlsplit("http://invalid/")


def norm(u: str) -> str:
    """host/path, lower case, no scheme, no www., no query, no trailing slash."""
    u = u.strip()
    if "://" not in u:
        u = "http://" + u
    p = usplit(u)
    h = (p.hostname or "").lower()
    h = h[4:] if h.startswith("www.") else h
    return h + urllib.parse.unquote(p.path).lower().rstrip("/")


def toks(s: str) -> list[str]:
    return _WORD.findall(urllib.parse.unquote(s).lower())


def stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def address_words(path: str) -> list[str]:
    """The tokens of a path as indexed: letters and digits, so a model number (wh-1000xm5) stays; hashes and long numbers go."""
    out = []
    for t in _WORD.findall(urllib.parse.unquote(path).lower()):
        if len(t) < 2 or len(t) > 30:
            continue
        if t.isdigit() and len(t) > 4:
            continue
        if len(t) >= 12 and re.fullmatch(r"[0-9a-f]+", t):
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------------------------- features and rules
class U:
    """One sitemap URL with what the rules read."""
    __slots__ = ("url", "lastmod", "title", "path", "depth", "junk", "locale", "key", "lw", "slot", "year", "words", "dkey",
                 "file", "prod")

    def __init__(self, url: str, lastmod: str, title: str, file: str = ""):
        self.url, self.lastmod, self.title = url, lastmod or "", title or ""
        self.file = file
        p = usplit(url)
        path = urllib.parse.unquote(p.path or "/").lower()
        segs = [s for s in path.split("/") if s]
        self.locale = None
        if segs and _LOCALE.match(segs[0]) and len(segs) > 1:
            self.locale = segs[0]
            segs = segs[1:]
        self.path = "/" + "/".join(segs)
        self.depth = len(segs)
        self.junk = bool(p.query) or any(s in _JUNK_SEG for s in segs) or bool(_PAGINATED.search(path)) or bool(_DATED_ARCHIVE.search(path))
        self.words = address_words("/".join(segs))
        last = address_words(segs[-1]) if segs else []
        self.lw = len([w for w in last if w.isalpha()])
        self.slot = any(w in SLOT_PATH for w in self.words)
        self.year = int(self.lastmod[:4]) if self.lastmod[:4].isdigit() else 0
        self.key = self.path
        self.dkey = re.sub(r"\d+", "#", self.path)
        # a product's address shape: a product-ish segment above a slug (/products/air-max-90, /p/<slug>, /dp/<id>), or a
        # "-p-123" / "/p123" tail; a sitemap file named product(s) / item(s) / catalog counts too (see FILE_PROD)
        self.prod = (any(sg in PRODUCT_SEG for sg in segs[:-1]) and bool(segs)) or bool(_PROD_TAIL.search(path))


def urls_of(r: dict) -> list:
    """The record's URLs as U, each with the name of the sitemap file it came from. The harvest stores the URLs in the order
    its files were read, and each file's count, so the file of every URL follows."""
    us, files = r["urls"], [f for f in r.get("sitemap_files", []) if f.get("status") == "ok" and f.get("urls")]
    out, i = [], 0
    for f in files:
        name = f["url"].rsplit("/", 1)[-1].split("?")[0].lower()
        for u in us[i:i + f["urls"]]:
            out.append(U(u[0], u[1], u[2], name))
        i += f["urls"]
    for u in us[i:]:
        out.append(U(u[0], u[1], u[2]))
    return out


def rule_baseline(us: list[U]) -> list[U]:
    """The first stand-in (pick200 of the first check): no junk addresses, newest lastmod, then shallow, 200."""
    c = [u for u in us if not u.junk]
    c.sort(key=lambda u: (u.lastmod or "", u.lw), reverse=True)
    c.sort(key=lambda u: u.depth)
    return c[:200]


def _clean(us: list[U]) -> list[U]:
    """No junk address; one locale's copy of a page (an English one first); at most 3 per number-normalised path."""
    pref = {}
    for u in us:
        if u.junk:
            continue
        cur = pref.get(u.key)
        rank = 0 if u.locale is None or u.locale.lower() in _ENGLISH else 1
        if cur is None or rank < cur[0]:
            pref[u.key] = (rank, u)
    seen = collections.Counter()
    out = []
    for _rank, u in pref.values():
        seen[u.dkey] += 1
        if seen[u.dkey] <= 3:
            out.append(u)
    return out


def budget_flat(n: int) -> int:
    return 200


def budget_scaled(n: int) -> int:
    return min(750, max(200, int(0.15 * n)))


def make_rule(score, budget):
    def rule(us: list[U]) -> list[U]:
        c = _clean(us)
        c.sort(key=score, reverse=True)
        return c[:budget(len(us))]
    return rule


def s_shallow(u: U):
    return (-u.depth, min(u.lw, 4))


def s_mixed(u: U):
    return 3 * bool(u.title) + 2 * u.slot + min(u.lw, 4) + (1 if u.year >= 2025 else 0) - 0.7 * u.depth


def s_wordy(u: U):
    return 2 * bool(u.title) + 2 * u.slot + 2 * min(u.lw, 5) - 0.3 * u.depth + (0.5 if u.year >= 2025 else 0)


def budget_small(n: int) -> int:
    return min(400, max(150, int(0.1 * n)))


_CTX = {"maker": False}               # set per site before a rule runs: is this site a maker's (it gets the larger budget)


def budget_product(n: int) -> int:
    """A maker's site keeps up to 2,000 of what was read (40% of it, at least 200); any other site as K6 does (150 to 400)."""
    return min(2000, max(200, int(0.4 * n))) if _CTX["maker"] else budget_small(n)


def budget_product_small(n: int) -> int:
    """The smaller variant: a maker's site keeps up to 500 (10% of what was read, at least 100); any other site as K6."""
    return min(500, max(100, int(0.1 * n))) if _CTX["maker"] else budget_small(n)


def s_product(u: U):
    """Product pages first: a product-shaped address, a sitemap file named for products, a title, a slug of words; the
    shallow and the fresh a little."""
    fp = any(w in FILE_PROD for w in re.split(r"[^a-z0-9]+", u.file))
    return 4 * u.prod + 3 * fp + 2 * bool(u.title) + min(u.lw, 5) - 0.2 * u.depth + (0.5 if u.year >= 2025 else 0)


def make_quota(budget, share_sections: float = 0.35, score=None):
    """Two kinds of page people ask a site for: its sections (shallow: /returns, /about/team, /privacy-policy) and its
    things (a product, a model, an article: a longer address of words). Part of the budget goes to each, so neither
    crowds out the other: sections = depth <= 2, slot words first, then shallow, then newest; things = the rest by the
    wordiness of the last segment, a title, a slot word, shallow."""
    def rule(us: list[U]) -> list[U]:
        c = _clean(us)
        b = budget(len(us))
        nsec = int(b * share_sections)
        sec = sorted((u for u in c if u.depth <= 2), key=lambda u: (u.slot, -u.depth, u.lastmod or ""), reverse=True)[:nsec]
        taken = {id(u) for u in sec}
        rest = sorted((u for u in c if id(u) not in taken), key=score or s_wordy, reverse=True)[:b - len(sec)]
        return sec + rest
    return rule


RULES = {
    "K0": rule_baseline,
    "K1": make_rule(s_shallow, budget_flat),            # shallow paths first, 200
    "K2": make_rule(s_mixed, budget_flat),              # titles, slot words, wordy last segment, shallow; 200
    "K3": make_rule(s_mixed, budget_scaled),            # K2 with a budget that grows with the site (200 to 750)
    "K4": make_rule(s_wordy, budget_scaled),            # words of the last segment first (product-like pages), scaled
    "K5": make_quota(budget_flat),                      # 35% sections + 65% things, 200
    "K6": make_quota(budget_small),                     # the same with a budget of 150 to 400 by site size
    "K7": make_quota(budget_scaled),                    # the same with 200 to 750
    "K8": make_quota(budget_product, 0.10, s_product),  # product pages first; 10% sections; makers up to 2,000, others 150-400
    "K9": make_quota(budget_product_small, 0.10, s_product),   # the same, makers up to 500
}


def row_bytes(u: U) -> int:
    return len(u.path) + len(u.title) + 10 + 16          # path, title, lastmod, row overhead (a rough on-disk figure)


# ---------------------------------------------------------------------------------------------- gold sets
def load_gold(slot_tsv: str):
    mp = [ln.split("\t") for ln in open(os.path.join(HERE, "evalsets", "open", "maker-product.tsv"), encoding="utf-8").read().splitlines()[1:]]
    slot = [ln.split("\t") for ln in open(slot_tsv, encoding="utf-8").read().splitlines()[1:]]
    sets = {"slot_dev": [], "slot_test": [], "maker_dev": [], "maker_held": []}
    for r in slot:
        g = r[2]
        if "/" not in g.strip("/"):
            continue                                       # a bare site is its home page: not a page that needs a sitemap
        sets["slot_dev" if r[5] == "dev" else "slot_test"].append(("slot", r[0], g, norm(g)))
    for r in mp:
        q, dom = r[0], regdom(r[2].split("/")[0])
        label = dom.split(".")[0]
        words = [t for t in toks(q) if t not in STOP and not (len(t) >= 3 and (t in label or label in t))]
        if not words:
            continue
        half = "maker_dev" if int(hashlib.md5(q.encode()).hexdigest(), 16) % 2 == 0 else "maker_held"
        sets[half].append(("maker", q, dom, words))
    return sets


def row_hit(row, kept_by_dom: dict) -> bool:
    if row[0] == "slot":
        dom = regdom(row[2].split("/")[0])
        return any(norm(u[0]) == row[3] for u in kept_by_dom.get(dom, ()))
    dom, need = row[2], {stem(w) for w in row[3]}
    for u in kept_by_dom.get(dom, ()):
        have = {stem(t) for t in toks(urllib.parse.urlsplit(u[0]).path)} | {stem(t) for t in toks(u[1])}
        if need <= have:
            return True
    return False


# ---------------------------------------------------------------------------------------------- eval
_GOLD_DOMS: set = set()
_MAKERS: set = set()
_NAMES: set = set()


def _init(gold_doms, makers=frozenset(), names=frozenset()):
    global _GOLD_DOMS, _MAKERS, _NAMES
    _GOLD_DOMS, _MAKERS, _NAMES = gold_doms, set(makers), set(names)


def load_makers(path: str | None) -> set:
    """Registered domains of the sites that get the maker budget: a file of hosts or URLs, one per line (first column)."""
    out = set()
    for ln in open(path, encoding="utf-8") if path else ():
        f = ln.split("\t")[0].strip()
        if f and not f.startswith("#"):
            out.add(regdom((usplit(f if "://" in f else "http://" + f).hostname or "")))
    return out


def work(line: str):
    r = json.loads(line)
    if not r.get("has_sitemap"):
        return r["target"], None
    us = urls_of(r)
    stats, gold = {}, {}
    site_dom = regdom((usplit(r["target"]).hostname or ""))
    _CTX["maker"] = site_dom in _MAKERS
    for name, rule in RULES.items():
        if _NAMES and name not in _NAMES:
            continue
        kept = rule(us)
        stats[name] = (len(kept), sum(row_bytes(u) for u in kept))
        if site_dom in _GOLD_DOMS or any(regdom(usplit(u.url).hostname or "") in _GOLD_DOMS for u in kept[:50]):
            gold[name] = [(u.url, u.title) for u in kept if regdom(usplit(u.url).hostname or "") in _GOLD_DOMS]
    return r["target"], (len(us), stats, gold)


def load_release(reg_path: str, nav_path: str, gold_doms: set) -> dict:
    """regdom -> [(url, title)]: the registry's page and site rows, and the card's site-nav links, for the gold domains."""
    out = collections.defaultdict(list)
    db = sqlite3.connect(f"file:{reg_path}?mode=ro&immutable=1", uri=True)
    for origin, path, title in db.execute("select h.origin, r.path, r.title from registry r join hosts h on h.id = r.host_id"):
        d = regdom(usplit(origin).hostname or "")
        if d in gold_doms:
            out[d].append((origin + (path or ""), title or ""))
    s0 = 2 << 28
    for _doc, path, title, alt in db.execute("select doc_id, path, title, alt from registry where doc_id >= ? and doc_id < ?", (s0, s0 + (1 << 28))):
        if path and regdom(path) in gold_doms:
            out[regdom(path)].append((alt or "http://" + path, title or ""))
    for ln in open(nav_path, encoding="utf-8"):
        r = json.loads(ln)
        d = regdom(r["site"])
        if d in gold_doms:
            for l in r.get("links", []):
                out[d].append((l["url"], l.get("text", "")))
    return out


def cmd_eval(a):
    sets = load_gold(a.slotsv)
    gold_doms = set()
    for rows in sets.values():
        for row in rows:
            gold_doms.add(regdom(row[2].split("/")[0]) if row[0] == "slot" else row[2])
    extra = set()
    if a.targets and a.n_card:
        extra = {ln.strip() for i, ln in enumerate(open(a.targets, encoding="utf-8")) if i >= a.n_card and ln.strip()}
    t0 = time.time()
    per = {}                      # target -> (n_urls, stats, gold)
    with multiprocessing.Pool(a.workers, _init, (gold_doms, load_makers(a.makers))) as pool, open(a.harvest, encoding="utf-8") as f:
        for tgt, res in pool.imap_unordered(work, f, chunksize=4):
            per[tgt] = res
    print(f"rules run over {len(per)} sites in {time.time() - t0:.0f} s", file=sys.stderr)
    rel = load_release(a.registry, a.sitenav, gold_doms)
    for label, drop in (("all harvest hosts", set()), (f"{len(extra)} added gold hosts removed", extra)):
        if label != "all harvest hosts" and not extra:
            continue
        kept = {k: collections.defaultdict(list) for k in RULES}
        rows = {k: 0 for k in RULES}
        byts = {k: 0 for k in RULES}
        nsite = 0
        for tgt, res in per.items():
            if res is None or tgt in drop:
                continue
            nsite += 1
            for k in RULES:
                rows[k] += res[1][k][0]
                byts[k] += res[1][k][1]
                for u in res[2].get(k, ()):
                    kept[k][regdom(usplit(u[0]).hostname or "")].append(u)
        print(f"\n### Keep rules, {label} ({nsite} sites with a sitemap)\n")
        print("| rule | rows | est. bytes (MB) | slot dev | maker dev | slot test (held out) | maker held out | union with release: slot test / maker held |")
        print("|---|---|---|---|---|---|---|---|")
        for name in list(RULES):
            cells = []
            for s in ("slot_dev", "maker_dev", "slot_test", "maker_held"):
                rs = sets[s]
                h = sum(1 for r in rs if row_hit(r, kept[name]))
                cells.append(f"{100 * h / len(rs):.1f}% ({h}/{len(rs)})")
            un = []
            for s in ("slot_test", "maker_held"):
                rs = sets[s]
                h = sum(1 for r in rs if row_hit(r, kept[name]) or row_hit(r, rel))
                un.append(f"{100 * h / len(rs):.1f}%")
            print(f"| {name} | {rows[name]:,} | {byts[name] / 1e6:,.0f} | " + " | ".join(cells) + f" | {' / '.join(un)} |")
        base = [f"{100 * sum(1 for r in sets[s] if row_hit(r, rel)) / len(sets[s]):.1f}%" for s in ("slot_dev", "maker_dev", "slot_test", "maker_held")]
        print(f"| release alone | - | - | {' | '.join(base)} | - |")
    print("\nrows per set: " + ", ".join(f"{k} {len(v)}" for k, v in sets.items()))


def cmd_evalmaker(a):
    """The maker-plus-product set (feat/maker-product): site gold judged by the product-words proxy, and the page-gold rows
    (page/maker-plus-product.page.tsv) judged by an exact address; dev half, by category, release alone against release
    plus the harvest(s) under the named rules."""
    names = [x for x in a.rules.split(",") if x]
    base = os.path.dirname(a.set)
    cat_of, split_of, rows = {}, {}, []
    for ln in open(a.set, encoding="utf-8").read().splitlines()[1:]:
        f = ln.split("\t")
        q, exp, strat, split = f[0], f[2], f[4], f[5]
        cat = next((x[4:] for x in strat.split(",") if x.startswith("cat:")), "?")
        cat_of[q], split_of[q] = cat, split
        host = exp.split("/")[0].lower()
        htoks = set(toks(host))
        label = regdom(host).split(".")[0]
        words = [t for t in toks(q) if t not in STOP and t not in htoks and not (len(t) >= 3 and (t in label or label in t))]
        if words and split == a.split:
            rows.append(("maker", q, regdom(host), words, cat))
    pages = []
    for ln in open(os.path.join(base, "page", "maker-plus-product.page.tsv"), encoding="utf-8").read().splitlines()[1:]:
        q, _ent, pg = ln.split("\t")[:3]
        if split_of.get(q) == a.split:
            pages.append(("slot", q, pg, norm(pg), cat_of.get(q, "?")))
    gold_doms = {r[2] for r in rows} | {regdom(r[2].split("/")[0]) for r in pages}
    drop = set()
    if a.targets and a.n_card:
        drop = {ln.strip() for i, ln in enumerate(open(a.targets, encoding="utf-8")) if i >= a.n_card and ln.strip()}
    per, seen = {}, set()
    for path in reversed(a.harvest):                     # the last file first: its sites win
        mine = set()
        with multiprocessing.Pool(a.workers, _init, (gold_doms, load_makers(a.makers), names)) as pool, open(path, encoding="utf-8") as f:
            for tgt, res in pool.imap_unordered(work, f, chunksize=4):
                mine.add(tgt)
                if tgt not in seen and tgt not in drop:
                    per[tgt] = res
        seen |= mine
    rel = load_release(a.registry, a.sitenav, gold_doms)
    kept = {k: collections.defaultdict(list) for k in names}
    nrows = {k: 0 for k in names}
    for tgt, res in per.items():
        if res is None:
            continue
        for k in names:
            nrows[k] += res[1][k][0]
            for u in res[2].get(k, ()):
                kept[k][regdom(usplit(u[0]).hostname or "")].append(u)
    print(f"\n### {a.set.rsplit('/', 1)[-1]} {a.split} half: {len(rows)} site rows with product words left, {len(pages)} page-gold rows"
          f" (harvest files: {len(a.harvest)}; {'the added gold hosts removed' if drop else 'all hosts'})\n")
    cats = sorted({r[4] for r in rows})
    for kind, rs in (("site rows (product words in a page of the gold site)", rows), ("page-gold rows (exact address)", pages)):
        print(f"**{kind}**\n")
        print("| category | rows | release alone | " + " | ".join(f"{k} harvest / union" for k in names) + " |")
        print("|---|---|---|" + "---|" * len(names))
        for cat in cats + ["ALL"]:
            sel = [r for r in rs if cat == "ALL" or r[4] == cat]
            if not sel:
                continue
            n = len(sel)
            relh = sum(1 for r in sel if row_hit(r, rel))
            cells = []
            for k in names:
                h = sum(1 for r in sel if row_hit(r, kept[k]))
                u = sum(1 for r in sel if row_hit(r, kept[k]) or row_hit(r, rel))
                cells.append(f"{100 * h / n:.0f}% / {100 * u / n:.0f}%")
            print(f"| {cat} | {n} | {100 * relh / n:.0f}% | " + " | ".join(cells) + " |")
        print()
    print("rows kept: " + ", ".join(f"{k} {nrows[k]:,}" for k in names))


# ---------------------------------------------------------------------------------------------- build
SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;
PRAGMA page_size = 4096;
PRAGMA cache_size = -1000000;
PRAGMA locking_mode = EXCLUSIVE;
PRAGMA temp_store = MEMORY;
CREATE TABLE site_pages (id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL, path TEXT NOT NULL, lastmod INTEGER, title TEXT);
CREATE TABLE site_hosts (host_id INTEGER PRIMARY KEY, host TEXT NOT NULL UNIQUE, pages INTEGER NOT NULL);
CREATE VIRTUAL TABLE site_pages_fts USING fts5(site, words, title, content='', detail=column, columnsize=0, tokenize="unicode61 remove_diacritics 2");
"""


def host_of_url(u: str) -> str:
    h = (usplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def targets_of(path: str) -> set:
    out = set()
    with open(path, encoding="utf-8") as f:
        for ln in f:
            k = ln.find('"target": "')            # cheap: the target is in every record
            if k >= 0:
                out.add(ln[k + 11:ln.index('"', k + 11)])
    return out


def cmd_build(a):
    rule = RULES[a.rule]
    drop = set()
    if a.targets and a.n_card:
        drop = {ln.strip() for i, ln in enumerate(open(a.targets, encoding="utf-8")) if i >= a.n_card and ln.strip()}
    makers = load_makers(a.makers)
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript(SCHEMA)
    t0 = time.time()
    hosts: dict[str, int] = {}
    per_host: collections.Counter = collections.Counter()
    rows = 0
    pending = []
    later: set = set()
    order = list(reversed(a.harvest))                # the last file first: its sites win
    kept_stats = collections.Counter()
    for path in order:
        mine = set()
        with open(path, encoding="utf-8") as f:
            for ln in f:
                r = json.loads(ln)
                mine.add(r["target"])
                if r["target"] in later or not r.get("has_sitemap") or r["target"] in drop:
                    continue
                _CTX["maker"] = regdom(usplit(r["target"]).hostname or "") in makers
                kept = rule(urls_of(r))
                kept_stats["maker_sites" if _CTX["maker"] else "other_sites"] += 1
                kept_stats["maker_rows" if _CTX["maker"] else "other_rows"] += len(kept)
                for u in kept:
                    host = host_of_url(u.url)
                    if not host:
                        continue
                    n = hosts.setdefault(host, len(hosts) + 1)
                    per_host[n] += 1
                    ym = int(u.lastmod[:4]) * 100 + int(u.lastmod[5:7]) if u.lastmod[:4].isdigit() and u.lastmod[5:7].isdigit() else 0
                    pending.append((n, usplit(u.url).path or "/", ym, u.title))
                if len(pending) > 400000:                  # big batches: fewer, larger FTS segments, less merging
                    rows += _flush(db, pending)
                    pending = []
                    print(f"  {rows:,} rows, {time.time() - t0:.0f} s", file=sys.stderr, flush=True)
        later |= mine
    rows += _flush(db, pending)
    db.executemany("INSERT INTO site_hosts(host_id, host, pages) VALUES (?,?,?)", [(n, h, per_host[n]) for h, n in hosts.items()])
    db.execute("INSERT INTO site_pages_fts(site_pages_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"{a.rule}: {rows:,} rows, {len(hosts):,} hosts, {os.path.getsize(a.out) / 1e6:,.1f} MB, {time.time() - t0:.0f} s; {dict(kept_stats)}")


_NEXT_ID = [1]


def _flush(db, pending) -> int:
    base = _NEXT_ID[0]
    db.executemany("INSERT INTO site_pages(id, host_id, path, lastmod, title) VALUES (?,?,?,?,?)",
                   [(base + i, n, p, lm, t or None) for i, (n, p, lm, t) in enumerate(pending)])
    db.executemany("INSERT INTO site_pages_fts(rowid, site, words, title) VALUES (?,?,?,?)",
                   [(base + i, f"s{n}", " ".join(address_words(p)), t) for i, (n, p, _lm, t) in enumerate(pending)])
    db.commit()
    _NEXT_ID[0] += len(pending)
    return len(pending)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("eval")
    p.add_argument("harvest"); p.add_argument("registry"); p.add_argument("sitenav"); p.add_argument("slotsv")
    p.add_argument("--targets"); p.add_argument("--n-card", type=int, default=0); p.add_argument("--workers", type=int, default=6)
    p.add_argument("--makers", help="hosts that get the maker budget (K8)")
    p = sub.add_parser("evalmaker")
    p.add_argument("set"); p.add_argument("registry"); p.add_argument("sitenav")
    p.add_argument("--harvest", action="append", required=True); p.add_argument("--rules", default="K6,K8")
    p.add_argument("--split", default="dev"); p.add_argument("--makers"); p.add_argument("--workers", type=int, default=6)
    p.add_argument("--targets"); p.add_argument("--n-card", type=int, default=0)
    p = sub.add_parser("build")
    p.add_argument("out"); p.add_argument("--harvest", action="append", required=True, help="repeat: later files win per site")
    p.add_argument("--rule", default="K6")
    p.add_argument("--targets"); p.add_argument("--n-card", type=int, default=0)
    p.add_argument("--makers", help="hosts that get the maker budget (K8)")
    a = ap.parse_args()
    {"eval": cmd_eval, "build": cmd_build, "evalmaker": cmd_evalmaker}[a.cmd](a)


if __name__ == "__main__":
    main()
