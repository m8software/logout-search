#!/usr/bin/env python3
"""maker-plus-product: "maker + product" as people type it -> the maker's own site ("toyota camry", "samsung galaxy
s24", "bosch dishwasher", "dyson v15"). The owner's maker-product. No Tranco, no visitor searches, no network, no
engine, no LLM: the release's facts.sqlite / lookup.sqlite / signals.bin (Wikipedia pageviews, CC0), the release's
brand-sites.tsv, the list of official sites the 2026-11-05 fetch read (j575 fetched.jsonl), and the sitemap table.

    tools/maker-plus-product-evalset.py --release DIR --brand-sites TSV --fetched JSONL [--pages site-pages.sqlite]
                                        [--out evalsets/open] [--n 400]

How the names were chosen (nothing by hand but the fixed word lists below):
  makers   Wikipedia articles the release's facts name as the maker of another article: manufacturer P176, developer
           P178, publisher P123 (facts.sqlite pid 58 / 10 / 52, with the maker's article as vdoc). A maker counts when
           brand-sites.tsv holds its name (the maker's own site) and that site is in the fetched list of official sites
           (the harvest list WITHOUT the 251 hosts that were added for the old eval's gold: no row's site is there only
           because an eval needed it).
  category the maker's industry fact (P452) mapped by the patterns in CATS: cars, phones, software, appliances (consumer
           electronics and household), tools, food, toys, clothing.
  named    the product's article title, the maker's name taken off the front, query = "<maker> <product>" lower-cased
           (a brand name as brand-sites writes it, else the article title without "Inc." / "Corporation" ...). The
           products with the most Wikipedia pageviews of each category, at most MAXMAKER per maker, none with a dissolved
           or end-time date in the facts, none whose article is not among the release's names (the engine could not
           know it either).
  typed    Wikidata names few products of toys, tools, food, clothing and household appliances, so those categories are
           filled with the maker's name and a product-type word ("bosch dishwasher", "nike shoes"): the makers are the
           names in SEEDS (mine, one list per category), each kept only if it has a site in the fetched list and an entry
           in the release's names; the product-type word after the colon is the one I judged the maker best known for (one word each, no hash).
           The list is mine and says so; nothing in it comes from a search log. (Makers picked by industry fact gave
           "oppo microwave": the fact says consumer electronics, not what the maker sells.)
  gold     the maker's site: its host, "|"-joined with nothing else. A result on that host or a subdomain is a hit.
  page     (written only for the pages I read and found to be the product's own page: --page-checked)
           a sample (--page-sample, 60) of the rows with a page in site-pages.sqlite on the maker's host holding every
           product word in its address or title; the chosen page is the shallowest then shortest of them, written to
           page/maker-plus-product.page.tsv and read by sq-eval as page@1 / page@5. Rows with a page are picked by the md5 of the query;
           the file `page/maker-plus-product.candidates.tsv` lists every candidate seen so each can be read by hand.
  split    open-evalsets.split_of on the entity (the maker and product keys), as every open set.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SEED = "402"
TYPE_SHIFT = 28
SET = "maker-plus-product"
COLS = ("query", "category", "expected", "set", "stratum", "split", "entity")
MAXMAKER = 4
nk = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())      # noqa: E731

# category -> (maker industry pattern, row quota, named quota). The rest of the quota is typed rows.
CATS = {
    "cars": (r"automotive|automobile|motorcycle|trade in cars|vehicle construction", 70, 70),
    "phones": (r"mobile phone", 55, 55),
    "software": (r"software|information technology|internet|web service", 70, 70),
    "appliances": (r"consumer electronics|home appliance|major appliance|household appliance|kitchenware", 55, 30),
    "tools": (r"power tool|hand tool|machine tool|toolmaking|\btools?\b", 35, 0),
    "food": (r"food industry|confection|snack|chocolate|candy|bakery|dairy|food processing|beverage|coffee", 40, 0),
    "toys": (r"\btoy|games and toys|\bdoll", 30, 0),
    "clothing": (r"clothing|apparel|fashion|footwear|sportswear", 45, 0),
}
# Makers named by me for the categories the release's facts hold almost nothing for (no more than their names: each must
# still resolve to an official site in the fetched list and to an article with pageviews in the release, or it is skipped).
SEEDS = {
    "appliances": "Bosch:dishwasher|Dyson:vacuum cleaner|Whirlpool:washing machine|Miele:dishwasher|KitchenAid:stand mixer|Siemens:refrigerator|"
                  "Electrolux:vacuum cleaner|LG:refrigerator|Philips:air fryer|Panasonic:microwave|Haier:refrigerator|Beko:washing machine|"
                  "Maytag:dryer|Smeg:kettle|Vitamix:blender|Cuisinart:food processor|Breville:toaster|Hoover:vacuum cleaner|Shark:vacuum cleaner|"
                  "iRobot:robot vacuum|Keurig:coffee maker|Nespresso:coffee machine|De'Longhi:espresso machine|Sharp:microwave|Hisense:television|"
                  "Frigidaire:refrigerator|Hamilton Beach:toaster|Tefal:frying pan|Kenwood:food processor|Braun:electric shaver",
    "tools": "DeWalt:cordless drill|Makita:impact driver|Milwaukee:circular saw|Ryobi:cordless drill|Black & Decker:cordless drill|Stanley:tape measure|"
             "Craftsman:tool set|Hilti:rotary hammer|Festool:track saw|Husqvarna:chainsaw|Stihl:chainsaw|Snap-on:wrench set|Metabo:angle grinder|"
             "Dremel:rotary tool|Ridgid:pipe wrench|Skil:circular saw|Fiskars:axe|Leatherman:multitool|Gerber:knife|Wera:screwdriver set|"
             "Knipex:pliers|Irwin:clamps|Husky:tool chest|Worx:leaf blower|Klein Tools:wire stripper|Kobalt:tool set",
    "toys": "Lego:building sets|Mattel:dolls|Hasbro:board games|Fisher-Price:baby toys|Playmobil:play sets|Nerf:blasters|Bandai:action figures|"
            "Funko:pop figures|Spin Master:toys|VTech:learning toys|Ravensburger:puzzles|Tomy:toy trains|Hornby:model trains|Crayola:crayons|"
            "Hape:wooden toys|MGA Entertainment:dolls|Lionel:model trains|Playskool:toys|Little Tikes:playhouse|Schleich:animal figures",
    "food": "Nestlé:chocolate|Kellogg's:cereal|Mondelez:biscuits|Hershey:chocolate bars|Mars:chocolate|Ferrero:chocolates|Cadbury:chocolate|"
            "Kraft Heinz:ketchup|Heinz:baked beans|Campbell:soup|General Mills:cereal|PepsiCo:soda|Coca-Cola:soda|Danone:yogurt|Lindt:chocolate|"
            "Haribo:gummy bears|Barilla:pasta|Tyson:chicken|Pringles:chips|Doritos:chips|Oreo:cookies|Nutella:spread|Starbucks:coffee beans|"
            "Quaker:oats|Hormel:bacon|Oscar Mayer:hot dogs",
    "clothing": "Nike:running shoes|Adidas:sneakers|Puma:sneakers|Levi's:jeans|H&M:dresses|Uniqlo:jackets|Gap:hoodies|Under Armour:shirts|"
                "Reebok:sneakers|New Balance:running shoes|Converse:sneakers|Vans:shoes|Timberland:boots|The North Face:jackets|Patagonia:fleece|"
                "Columbia:jackets|Lacoste:polo shirts|Ralph Lauren:polo shirts|Tommy Hilfiger:jackets|Calvin Klein:underwear|Carhartt:work jackets|"
                "Wrangler:jeans|Hanes:t-shirts|Fila:sneakers|Asics:running shoes|Skechers:sneakers|Dr. Martens:boots|Ugg:boots|Crocs:clogs|Zara:dresses",
}
_PHONE = re.compile(r"^(galaxy|iphone|pixel|lumia|xperia|nexus|redmi|poco|oneplus|mate|find x|moto|nord)\b", re.I)
_SUFFIX = re.compile(r",?\s+(inc\.?|llc|corporation|corp\.?|ltd\.?|limited|company|co\.|group|plc|s\.a\.|ag|gmbh|holdings|electronics)$", re.I)
_NOT_A_PRODUCT = re.compile(r"\b(class \d+|engine|locomotive|train|railway|tram|microprocessor|processor|chip|cpu|ship|film|album|comics|"
                            r"novel|episode|season|prototype|concept|racing|team|championship|minigun|carrier|submarine|destroyer|missile|rifle|tank|cruiser)\b", re.I)
_BAD_HOST = re.compile(r"(wikipedia|facebook|twitter|instagram|youtube|linkedin)\.")


def short_name(name: str) -> str:
    """"Audi AG" -> "Audi", "Apple Inc." -> "Apple": company suffixes off the end, repeatedly."""
    while True:
        n = _SUFFIX.sub("", name).strip()
        if n == name or not n:
            return n or name
        name = n


def md5i(*parts) -> int:
    return int(hashlib.md5((SEED + ":" + ":".join(map(str, parts))).encode()).hexdigest()[:8], 16)


def split_of(entity: str) -> str:
    return "test" if md5i("split", entity) % 2 else "dev"


def pageviews(signals: Path):
    with open(signals, "rb") as f:
        head = f.read(12)
        n = struct.unpack("<I", head[8:12])[0]
        hd = json.loads(f.read(n))
        fld = next(x for x in hd["fields"] if x["name"] == "wiki.pageviews")
        f.seek(12 + n + fld["offset"])
        return memoryview(f.read(fld["count"] * 4)).cast("I")


def views_of(pv, doc: int) -> int:
    seq = doc & ((1 << TYPE_SHIFT) - 1)
    return int(pv[seq]) if seq < len(pv) else 0


def host_of(url: str) -> str:
    h = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def pages_by_host(path: Path) -> dict[str, list[tuple[str, str]]]:
    """{host: [(path, title)]} from site-pages.sqlite (the kept subset the engine arm would hold)."""
    out: dict[str, list] = collections.defaultdict(list)
    if path and path.exists():
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for host, p, title in db.execute("SELECT host, path, coalesce(title, '') FROM site_pages"):
            out[host].append((p, title))
    return out


def find_pages(pages: dict, gold: str, words: list[str]) -> list[tuple[str, str, str]]:
    """Pages of the gold host (or a subdomain) holding every word in the address or the title -> (host, path, title)."""
    hits = []
    for h in (h for h in pages if h == gold or h.endswith("." + gold)):
        for p, t in pages[h]:
            toks = set(re.findall(r"[a-z0-9]+", (re.sub(r"%[0-9a-f]{2}", " ", p) + " " + t).lower()))
            if all(w in toks for w in words):
                hits.append((h, p, t))
    return sorted(hits, key=lambda x: (x[1].count("/"), len(x[1])))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--release", required=True)
    ap.add_argument("--brand-sites", required=True)
    ap.add_argument("--fetched", required=True)
    ap.add_argument("--pages", default="")
    ap.add_argument("--out", default=str(HERE / "evalsets/open"))
    ap.add_argument("--page-sample", type=int, default=60)
    ap.add_argument("--page-checked", default="", help="query<TAB>ok|no<TAB>chosen: my reading of the sampled pages; only ok keeps a page")
    a = ap.parse_args()
    rel, out = Path(a.release), Path(a.out)
    fx = sqlite3.connect(f"file:{rel / 'facts.sqlite'}?mode=ro&immutable=1", uri=True)
    lk = sqlite3.connect(f"file:{rel / 'lookup.sqlite'}?mode=ro&immutable=1", uri=True)
    pv = pageviews(rel / "signals.bin")
    brand = {}
    for ln in open(a.brand_sites, encoding="utf-8"):
        p = ln.rstrip("\n").split("\t")
        if len(p) >= 2 and not _BAD_HOST.search(p[1]):
            brand[p[0]] = (host_of(p[1]), p[2] if len(p) > 2 else "")
    fetched = set()
    for ln in open(a.fetched, encoding="utf-8"):
        fetched.add(host_of(json.loads(ln)["site"]))

    by_label: dict[str, list[str]] = collections.defaultdict(list)
    for h in fetched:
        by_label[h.split(".")[0]].append(h)

    def maker_site(name: str):
        """brand-sites.tsv first; else the fetched official site whose first label is the maker's name: label.com, then
        label.org, then the shortest ("nike" -> nike.com). Never a host that is not in the fetched list."""
        for cand in (name, _SUFFIX.sub("", name)):
            b = brand.get(nk(cand))
            if b and b[0] in fetched:
                return b[0], short_name(b[1] or name)
        short = short_name(name)
        hs = by_label.get(nk(short), [])
        if hs:
            pick = next((h for h in (nk(short) + ".com", nk(short) + ".org") if h in hs), min(hs, key=lambda h: (len(h), h)))
            return pick, short
        return None

    def gold_hosts(host: str, short: str) -> str:
        """The site, plus the maker's other fetched official sites named exactly like it on .com ("google" -> about.google
        and google.com): any of them is the maker's own site."""
        alts = [h for h in sorted(by_label.get(nk(short), [])) if h != host and h.endswith(".com")][:2]
        return "|".join([host] + alts)

    # makers: article doc -> (name, host, short name, industries)
    makers: dict[int, tuple] = {}
    prod: dict[int, list] = collections.defaultdict(list)
    label: dict[int, str] = {}
    for d, pid, v, vd in fx.execute("SELECT doc_id, pid, v, vdoc FROM facts WHERE pid IN (58, 10, 52) AND doc_id >> ? = 1 AND vdoc IS NOT NULL", (TYPE_SHIFT,)):
        prod[vd].append(d)
        label[vd] = v
    for vd in set(prod) | {d for (d,) in fx.execute("SELECT DISTINCT doc_id FROM facts WHERE pid = 32 AND doc_id >> ? = 1", (TYPE_SHIFT,))}:
        name = label.get(vd)
        if not name:
            row = lk.execute("SELECT name FROM suggest_items WHERE doc_id = ? LIMIT 1", (vd,)).fetchone()
            name = row[0] if row else None
        ms = maker_site(name) if name else None
        if ms:
            ind = [v for (v,) in fx.execute("SELECT v FROM facts WHERE doc_id = ? AND pid = 32", (vd,))]
            makers[vd] = (name, ms[0], ms[1], ind)
    print(f"makers with a brand site in the fetched list: {len(makers)}; with products: {sum(1 for m in makers if m in prod)}", file=sys.stderr)

    def cat_of(ind: list[str]) -> str | None:
        for c, (pat, _q, _n) in CATS.items():
            if any(re.search(pat, i, re.I) for i in ind):
                return c
        return None

    name_of = {}
    rows, seen = [], set()
    for cat, (_pat, quota, nquota) in CATS.items():
        pool = []
        for vd, (mname, host, short, ind) in makers.items():
            mcat = cat_of(ind)
            if cat == "phones" and mcat not in ("phones", "appliances", "software"):
                continue
            if mcat != cat and cat != "phones":
                continue
            for d in prod.get(vd, []):
                if fx.execute("SELECT 1 FROM facts WHERE doc_id = ? AND pid IN (95, 97) LIMIT 1", (d,)).fetchone():
                    continue
                r = lk.execute("SELECT name FROM suggest_items WHERE doc_id = ? LIMIT 1", (d,)).fetchone()
                if not r:
                    continue
                title = re.sub(r"\s*\(.*\)$", "", r[0])
                rest = re.sub(r"^" + re.escape(short) + r"\b[\s-]*", "", title, flags=re.I)
                rest = re.sub(r"^" + re.escape(short_name(mname)) + r"\b[\s-]*", "", rest, flags=re.I).strip()
                if not rest or nk(rest) == nk(short) or len(rest) > 40 or len(rest.split()) > 3 or _NOT_A_PRODUCT.search(rest):
                    continue
                if (cat == "phones") != bool(_PHONE.match(rest)) and not (cat != "phones" and mcat == cat and not _PHONE.match(rest)):
                    continue
                pool.append((views_of(pv, d), vd, host, f"{short} {rest}".lower(), nk(title), d, short))
        pool.sort(key=lambda t: -t[0])
        per, n = collections.Counter(), 0
        for views, vd, host, q, ent, d, short in pool:
            if n >= nquota or per[vd] >= MAXMAKER or q in seen or views == 0:
                continue
            seen.add(q); per[vd] += 1; n += 1
            rows.append(dict(query=q, category="site", expected=gold_hosts(host, short), set=SET, stratum=f"cat:{cat},kind:named",
                             split=split_of(ent), entity=ent, page=""))
        # typed rows: the category's seeds and fact-named makers, best-viewed first
        if cat in SEEDS and quota > n:
            ms = {}
            typed = {}
            for entry in SEEDS.get(cat, "").split("|"):
                nm, _, tword = entry.partition(":")
                typed[nk(nm)] = tword
                site = maker_site(nm)
                docs = [d for (d,) in lk.execute("SELECT DISTINCT i.doc_id FROM suggest_keys k JOIN suggest_items i ON i.doc_id = k.doc_id "
                                                 "WHERE k.key = ? AND k.pos = 0", (nk(nm),))]
                if docs and site:      # an article or a site entry by that name exists in the release; views when it is an article
                    d = max(docs, key=lambda d: views_of(pv, d) if d >> TYPE_SHIFT == 1 else 0)
                    v = views_of(pv, d) if d >> TYPE_SHIFT == 1 else 0
                    ms.setdefault(d, (max(v, 1), (nm, site[0], nm, [])))
            k = 0
            for views, (mname, host, short, _ind) in sorted(ms.values(), key=lambda t: -t[0]):
                if k >= quota - n or views == 0:
                    break
                t = typed[nk(short)]
                q = f"{short} {t}".lower()
                if q in seen:
                    continue
                seen.add(q); k += 1
                rows.append(dict(query=q, category="site", expected=gold_hosts(host, nk(short)), set=SET, stratum=f"cat:{cat},kind:typed",
                                 split=split_of(nk(short) + nk(t)), entity=nk(short) + nk(t), page=""))
    # page-level sample
    pages = pages_by_host(Path(a.pages)) if a.pages else {}
    cand = []
    for r in rows:
        maker_words = re.findall(r"[a-z0-9]+", r["query"])
        short_words = set(re.findall(r"[a-z0-9]+", r["query"]))
        host = r["expected"].split("|")[0]
        # product words: the query minus the words of the host's first label ("toyota camry" on toyota.com -> camry)
        label = host.split(".")[0]
        pw = [w for w in maker_words if w not in label and label not in w]
        if not pw:
            continue
        hits = find_pages(pages, host, pw)
        r["_pw"], r["_hits"] = pw, hits
        if hits:
            cand.append(r)
    cand.sort(key=lambda r: md5i("page", r["query"]))
    pick = {id(r) for r in cand[:a.page_sample]}
    checked = {}
    if a.page_checked and Path(a.page_checked).exists():
        for ln in list(open(a.page_checked, encoding="utf-8"))[1:]:
            q, verdict, chosen = ln.rstrip("\n").split("\t")[:3]
            checked[q] = (verdict, chosen)
    for r in cand:
        h, p, t = r["_hits"][0]
        if id(r) in pick and (not checked or checked.get(r["query"], ("", ""))[0] == "ok"):
            r["page"] = f"{h}{p}"
    (out / "page").mkdir(exist_ok=True)
    with open(out / f"{SET}.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in rows:
            f.write("\t".join(r[c] for c in COLS) + "\n")
    with open(out / f"page/{SET}.page.tsv", "w", encoding="utf-8") as f:
        f.write("query\tentity\tpage\n")
        for r in rows:
            if r["page"]:
                f.write(f"{r['query']}\t{r['entity']}\t{r['page']}\n")
    with open(out / f"page/{SET}.candidates.tsv", "w", encoding="utf-8") as f:
        f.write("query\tpicked\tn_pages\tchosen\tothers(first 3)\ttitle_of_chosen\n")
        for r in cand:
            f.write("\t".join([r["query"], "1" if r["page"] else "0", str(len(r["_hits"])), f"{r['_hits'][0][0]}{r['_hits'][0][1]}",
                               " ".join(f"{h}{p}" for h, p, _t in r["_hits"][1:4]), r["_hits"][0][2]]) + "\n")
    c = collections.Counter((r["stratum"].split(",")[0], r["split"]) for r in rows)
    print(f"rows {len(rows)}; per category/split {sorted(c.items())}")
    print(f"with a page in the sitemap table: {len(cand)} rows; page gold written for {sum(bool(r['page']) for r in rows)}")


if __name__ == "__main__":
    main()
