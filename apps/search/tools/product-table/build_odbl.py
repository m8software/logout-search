#!/usr/bin/env python3
"""Build the ODbL product table from the Open Food Facts family (food, beauty, pet food, products): same shape as the CC0 table.

    build_odbl.py --off DIR --cc0 products-cc0.sqlite --sites brand-sites.tsv --out products-off.sqlite

Licence: the Open Food Facts database is ODbL 1.0, its contents DbCL 1.0 (world.openfoodfacts.org/terms-of-use). This file is a
Derivative Database of it, so it ships as its own ODbL file with "Open Food Facts contributors" credit and the ODbL text. No image
is read. Nothing from this file is ever copied into the CC0 file; the CC0 file is only READ here, for the barcodes it already
holds, and those barcodes are left out of this one (the CC0 row wins). Rows are kept only with a brand AND a product name.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_cc0 import key, mkey, site_for, title  # noqa: E402

csv.field_size_limit(1 << 28)
FILES = [("openfoodfacts", "food"), ("openbeautyfacts", "beauty"), ("openproductsfacts", "products"), ("openpetfoodfacts", "pet food")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", required=True), ap.add_argument("--cc0", required=True), ap.add_argument("--sites", required=True)
    ap.add_argument("--out", required=True), ap.add_argument("--skip-food", action="store_true", help="test run without the big file")
    a = ap.parse_args()
    cc0 = sqlite3.connect(a.cc0)
    have = set()
    for (ids,) in cc0.execute("SELECT ids FROM products WHERE ids LIKE '%gtin:%'"):
        have.update(m.lstrip("0") for m in re.findall(r"gtin:(\d+)", ids))
    out = Path(a.out)
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, maker TEXT, "
                     "maker_site TEXT, category TEXT, ids TEXT, source TEXT, url TEXT);")
    sites = {}
    with open(a.sites, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3:
                for k in (mkey(p[2]), mkey(p[0])):
                    if k and k not in sites:
                        sites[k] = p[1]
    seen, stats = set(), {}
    for fam, label in FILES:
        if not (Path(a.off) / f"{fam}.csv.gz").exists() or (fam == "openfoodfacts" and a.skip_food):
            print("skipped", fam, file=sys.stderr)
            continue
        st = stats.setdefault(label, {"rows_read": 0, "no_brand_or_name": 0, "in_cc0_by_gtin": 0, "dup": 0, "kept": 0, "with_site": 0})
        with gzip.open(Path(a.off) / f"{fam}.csv.gz", "rt", encoding="utf-8", errors="replace", newline="") as fh:
            rd = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
            for r in rd:
                st["rows_read"] += 1
                name = " ".join((r.get("product_name") or "").split())
                raw = (r.get("brands") or r.get("brands_en") or r.get("brand_owner") or "").split(",")
                # the export carries brand slugs ("lu-biscuits"); make them words, and drop the placeholders
                brands = [b.strip().replace("-", " ") if " " not in b.strip() else b.strip() for b in raw
                          if b.strip() and b.strip().lower() not in ("null", "unknown", "none", "xx", "n a", "n/a")]
                if not name or not brands:
                    st["no_brand_or_name"] += 1
                    continue
                code = (r.get("code") or "").strip()
                if code.lstrip("0") in have:
                    st["in_cc0_by_gtin"] += 1
                    continue
                maker = brands[0].title() if brands[0] == brands[0].lower() else title(brands[0])
                k = (mkey(maker), key(name), code.lstrip("0"))
                if k in seen or (mkey(maker), key(name)) in seen:
                    st["dup"] += 1
                    continue
                seen.add(k)
                seen.add(k[:2])
                cats = [c.strip() for c in (r.get("categories_en") or r.get("categories") or "").split(",") if c.strip()]
                ids = [f"gtin:{code}"] if code else []
                ids += [f"brand:{title(b)}" for b in brands[1:3]]
                if r.get("quantity"):
                    ids.append("qty:" + r["quantity"].strip()[:30])
                site = ""
                for b in brands[:2]:
                    site = site or site_for(b, sites)
                db.execute("INSERT INTO products(name,maker,maker_site,category,ids,source,url) VALUES(?,?,?,?,?,?,?)",
                           (name[:200], maker[:120], site, (cats[-1] if cats else label)[:80], " ".join(ids)[:300], "off:" + label,
                            (r.get("url") or "")[:120]))
                st["kept"] += 1
                st["with_site"] += bool(site)
        print(label, st, flush=True)
    db.execute("CREATE VIRTUAL TABLE products_fts USING fts5(name, maker, ids, content='products', content_rowid='id', "
               "tokenize=\"unicode61 remove_diacritics 2\")")
    db.execute("INSERT INTO products_fts(rowid, name, maker, ids) SELECT id, name, maker, ids FROM products")
    db.execute("INSERT INTO products_fts(products_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    json.dump(stats, open(str(out) + ".stats.json", "w"), indent=1)


if __name__ == "__main__":
    main()
