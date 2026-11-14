#!/usr/bin/env python3
"""maker-sites.tsv: one row per official host of a maker or brand, for the sitemap harvest.

    maker_sites.py --wd2 DIR --sites brand-sites.tsv --db products-cc0.sqlite --harvest harvest-targets.tsv --out maker-sites.tsv [--links links-new.tsv]

Columns: host, site_url, maker_name, wikidata_id, source, category, in_earlier_harvest
  source: "wikidata P856" (a manufacturer/brand/developer target with a website), "brand-sites" (the release's list), "table"
          (a maker_site already in products-cc0.sqlite), or "link:<rule>" (a link added by link_makers.py).
  category: the most common product category of the table's rows that carry the host, else the maker's Wikidata class.
  in_earlier_harvest: host (same host in the earlier sitemap harvest), domain (same registered domain, other host), no.
One row per host (www. dropped); the first source in the order above names the row.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from build_cc0 import lit, rank_site, mkey  # noqa: E402

TWO = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "ne.jp", "or.jp", "com.br", "co.za", "com.cn",
       "com.mx", "com.tr", "co.in", "co.kr", "com.sg", "com.hk", "com.tw", "com.ar", "co.id", "com.my", "com.ph", "com.vn", "com.ua"}


def host_of(url: str) -> str:
    h = urlparse(url if "//" in url else "//" + url).netloc.lower().split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def domain_of(host: str) -> str:
    p = host.split(".")
    return ".".join(p[-3:]) if len(p) >= 3 and ".".join(p[-2:]) in TWO else ".".join(p[-2:])


def main() -> None:
    ap = argparse.ArgumentParser()
    for n in ("wd2", "sites", "db", "harvest", "out"):
        ap.add_argument("--" + n, required=True)
    ap.add_argument("--links")
    a = ap.parse_args()
    hh = {host_of(l.split("\t")[0].strip()) for l in open(a.harvest) if l.strip()}
    hd = {domain_of(h) for h in hh}
    cats = defaultdict(Counter)
    db = sqlite3.connect(a.db)
    tab = {}
    for maker, site, cat in db.execute("SELECT maker, maker_site, category FROM products WHERE maker_site<>''"):
        h = host_of(site)
        cats[h][cat] += 1
        tab.setdefault(h, (site, maker))
    rows = {}

    def put(site, maker, qid, src, klass=""):
        h = host_of(site)
        if not h or "." not in h or h in rows:
            return
        rows[h] = [h, site, maker, qid, src, klass]

    # wikidata makers: several sites per maker -> the best-ranked one first
    best = {}
    for line in Path(a.wd2, "makers.tsv").read_text(encoding="utf-8").split("\n"):
        p = line.split("\t")
        if len(p) < 4 or "entity/Q" not in p[0]:
            continue
        q, label, site, kl = lit(p[0]).rsplit("/", 1)[1], lit(p[1]), lit(p[2]), lit(p[3])
        best.setdefault(q, []).append((rank_site(mkey(label), site), site, label, kl))
    for q, lst in best.items():
        lst.sort()
        for _, site, label, kl in lst:
            put(site, label, q, "wikidata P856", kl)
    n_wd = len(rows)
    for line in open(a.sites, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 3:
            put(p[1], p[2], "", "brand-sites")
    for h, (site, maker) in tab.items():
        put(site, maker, "", "table")
    if a.links:
        for line in open(a.links, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[0] != "maker":
                put(p[1], p[0], p[2], "link:" + p[3], "")
    out = Path(a.out)
    new = 0
    with open(out, "w", encoding="utf-8") as f:
        f.write("host\tsite_url\tmaker_name\twikidata_id\tsource\tcategory\tin_earlier_harvest\n")
        for h, r in rows.items():
            tc = cats.get(h)
            cat = tc.most_common(1)[0][0] if tc else r[5]
            st = "host" if h in hh else ("domain" if domain_of(h) in hd else "no")
            new += st == "no"
            f.write("\t".join([r[0], r[1], r[2], r[3], r[4], cat, st]) + "\n")
    print(f"hosts {len(rows):,} (wikidata makers {n_wd:,}); not in earlier harvest (host or domain): {new:,}; earlier harvest hosts {len(hh):,}")


if __name__ == "__main__":
    main()
