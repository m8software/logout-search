#!/usr/bin/env python3
"""Build the CC0 / public-domain product table: products(name, maker, maker_site, category, ids, source, url) + FTS5.

    build_cc0.py --wd DIR --fdc DIR --ndc FILE.zip --es DIR --epa FILE.zip --sites brand-sites.tsv --out products-cc0.sqlite

Sources (all CC0 or US public domain; see SOURCES): Wikidata, USDA FoodData Central Branded Foods, openFDA drug NDC,
ENERGY STAR Model Index, EPA fueleconomy.gov. Dedupe: within a source on (maker, name) and GTIN; across sources the first
of wd, es, epa, ndc, fdc wins (a row with the product's own site beats a bare label row). Nothing here is read from an ODbL source.
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import re
import sqlite3
import sys
import unicodedata
import zipfile
from pathlib import Path

csv.field_size_limit(1 << 28)
SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|co|company|corp|corporation|plc|gmbh|sa|ag|lp|llp|the|usa|us|na|of|and)\b")
PRIORITY = ["wd", "es", "epa", "ndc", "fdc"]


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(s).lower().replace("&", " and "))


def mkey(s: str) -> str:
    """Maker key: company suffixes and punctuation dropped ("GENERAL MILLS, INC." -> generalmills)."""
    t = re.sub(r"[^a-z0-9 ]+", " ", fold(s).lower().replace("&", " and "))
    return re.sub(r"\s+", "", SUFFIX.sub(" ", t))


GENERIC = set("sales brands brand foods food operations operation holdings holding group manufacturing specialty international worldwide "
              "distribution consumer products healthcare health pharma pharmaceuticals pharmaceutical laboratories labs industries enterprises "
              "americas america north global services kitchens beverages beverage nutrition electronics appliances appliance technologies technology tools power".split())


def rank_site(mk: str, url: str):
    """Which of a maker's several official sites to keep: one whose host carries the maker's name, then the shortest (the front page, not a locale path)."""
    host = re.sub(r"[^a-z0-9]", "", url.split("//", 1)[-1].split("/", 1)[0].lower())
    return (0 if mk[:5] and mk[:5] in host else 1, len(url), url)


def mtokens(s: str) -> list[str]:
    t = re.sub(r"[^a-z0-9 ]+", " ", fold(s).lower().replace("&", " and "))
    return SUFFIX.sub(" ", t).split()


def site_for(maker: str, sites: dict) -> str:
    """Official site of a maker: exact key, then with trailing generic words dropped ("General Mills Sales" -> General Mills)."""
    toks = mtokens(maker)
    while toks:
        if "".join(toks) in sites:
            return sites["".join(toks)]
        if len(toks) > 1 and toks[-1] in GENERIC:
            toks.pop()
        else:
            break
    return ""


def title(s: str) -> str:
    s = " ".join(s.split())
    return s.title() if s and s.upper() == s and len(s) > 3 else s


def lit(v: str) -> str:
    v = v.strip()
    if v.startswith("<"):
        return v[1:-1]
    if v.startswith('"'):
        v = v[1:v.rfind('"')]
    return v.replace('\\"', '"').replace("\\\\", "\\")


class Sink:
    def __init__(self, db: sqlite3.Connection):
        self.db, self.seen, self.gt, self.n = db, set(), set(), {}

    def add(self, src, name, maker, category, ids, url, gtins=(), site=""):
        name = " ".join(name.split())[:200]
        if not name:
            return False
        k = (mkey(maker), key(name))
        if k in self.seen or any(g in self.gt for g in gtins):
            return False
        self.seen.add(k)
        self.gt.update(gtins)
        self.db.execute("INSERT INTO products(name,maker,maker_site,category,ids,source,url) VALUES(?,?,?,?,?,?,?)",
                        (name, " ".join(maker.split())[:120], site, category, ids[:300], src, url))
        self.n[src] = self.n.get(src, 0) + 1
        return True


def wikidata(sink: Sink, wd: Path, makersite: dict):
    rows = {}
    drop = set()
    for f in wd.glob("_exclude_*.tsv"):
        drop.update(lit(l).rsplit("/", 1)[-1] for l in f.read_text().split("\n") if l.strip())
    for f in sorted(wd.glob("[a-z]*.tsv")):
        cls = f.stem
        for line in f.read_text(encoding="utf-8").split("\n"):
            p = line.split("\t")
            if len(p) < 8 or "wikidata.org/entity/Q" not in p[0]:
                continue
            q = lit(p[0]).rsplit("/", 1)[1]
            label = lit(p[1])
            if not label or label == q or q in drop:
                continue
            mk, msite, gtin, site, desc = lit(p[3]), lit(p[4]), lit(p[5]), lit(p[6]), lit(p[7])
            r = rows.setdefault(q, {"label": label, "alias": set(), "mk": mk, "msite": msite, "gtin": set(), "site": site, "desc": desc, "cls": cls})
            r["alias"].update(a for a in lit(p[2]).split("|") if a and a != label)
            if gtin:
                r["gtin"].add(gtin)
            if mk and not r["mk"]:
                r["mk"] = mk
            if msite and not r["msite"]:
                r["msite"] = msite
            r["site"] = r["site"] or site
            r["desc"] = r["desc"] or desc
    for q, r in rows.items():
        if r["mk"] and r["msite"]:
            mk_ = mkey(r["mk"])
            cur = makersite.get(mk_)
            if cur is None or rank_site(mk_, r["msite"]) < rank_site(mk_, cur):
                makersite[mk_] = r["msite"]
    for q, r in rows.items():
        ids = [f"wd:{q}"] + [f"gtin:{g}" for g in sorted(r["gtin"])[:3]] + [f"alias:{a}" for a in sorted(r["alias"])[:6]]
        name = r["label"]
        # a model label that does not carry its maker ("Camry") is searched with the maker in its own column
        sink.add("wd", name, r["mk"], r["cls"], " ".join(ids), r["site"] or r["desc"], gtins=[g for g in r["gtin"]],
                 site=makersite.get(mkey(r["mk"]), r["msite"]))


def energystar(sink: Sink, es: Path):
    groups = {}
    for f in sorted(es.glob("es-*.csv")):
        with open(f, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                brand = r.get("brand_name", "").strip() or r.get("energy_star_partner", "").strip()
                mn = r.get("model_name", "").strip()
                num = r.get("model_number", "").strip()
                name = mn if mn and key(mn) != key(num) else (mn or num)
                if not brand or not name:
                    continue
                g = groups.setdefault((mkey(brand), key(name), r["product_category"]), [brand, name, r["product_category"], []])
                if num and len(g[3]) < 6 and num not in g[3]:
                    g[3].append(num)
    for brand, name, cat, nums in groups.values():
        sink.add("es", name, title(brand), cat, " ".join(f"model:{n}" for n in nums), "")


def epa(sink: Sink, zp: Path):
    z = zipfile.ZipFile(zp)
    with z.open(z.namelist()[0]) as fh:
        years = {}
        for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")):
            k = (r["make"], r["model"])
            y = int(r["year"])
            lo, hi, cls = years.get(k, (9999, 0, r.get("VClass", "")))
            years[k] = (min(lo, y), max(hi, y), cls or r.get("VClass", ""))
    for (make, model), (lo, hi, cls) in years.items():
        sink.add("epa", f"{make} {model}", make, "car" if not cls else f"car: {cls}", f"years:{lo}-{hi}", "")


def ndc(sink: Sink, zp: Path):
    z = zipfile.ZipFile(zp)
    d = json.load(z.open(z.namelist()[0]))["results"]
    counts = {}
    groups = {}
    for r in d:
        pt = r.get("product_type", "")
        counts[pt] = counts.get(pt, 0) + 1
        if pt not in ("HUMAN PRESCRIPTION DRUG", "HUMAN OTC DRUG"):
            continue
        brand = r.get("brand_name") or r.get("generic_name") or ""
        lab = r.get("labeler_name", "")
        g = groups.setdefault((mkey(lab), key(brand)), [brand, lab, r.get("generic_name", ""), pt, r.get("dosage_form", ""), []])
        if len(g[5]) < 4:
            g[5].append(r.get("product_ndc", ""))
    print("ndc product types:", counts, file=sys.stderr)
    for brand, lab, gen, pt, form, ndcs in groups.values():
        cat = "drug: OTC" if "OTC" in pt else "drug: prescription"
        ids = " ".join([f"ndc:{n}" for n in ndcs] + ([f"generic:{gen}"] if gen and key(gen) != key(brand) else []) + ([f"form:{form}"] if form else []))
        sink.add("ndc", title(brand), title(lab), cat, ids, "")
    return counts


def fdc(sink: Sink, d: Path):
    desc = {}
    with open(next(d.glob("*/food.csv")), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            desc[r["fdc_id"]] = r["description"]
    with open(next(d.glob("*/branded_food.csv")), encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            name = title(desc.get(r["fdc_id"], ""))
            owner = title(r["brand_owner"]) or title(r["brand_name"])
            gt = r["gtin_upc"].lstrip("0") and r["gtin_upc"]
            ids = []
            if r["brand_name"] and key(r["brand_name"]) not in key(name):
                ids.append("brand:" + title(r["brand_name"]))
            if gt:
                ids.append("gtin:" + gt)
            sink.add("fdc", name, owner, r["branded_food_category"] or "food", " ".join(ids), "", gtins=[gt] if gt else [])
            # brand name remembered for the site lookup
            if r["brand_name"] and owner:
                BRANDOWNER[mkey(r["brand_name"])] = mkey(owner)


BRANDOWNER: dict[str, str] = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wd"), ap.add_argument("--fdc"), ap.add_argument("--ndc"), ap.add_argument("--es"), ap.add_argument("--epa")
    ap.add_argument("--sites"), ap.add_argument("--out", required=True), ap.add_argument("--wd2", help="Wikidata org files for link_makers.py"), ap.add_argument("--links-out")
    a = ap.parse_args()
    out = Path(a.out)
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, maker TEXT, "
                     "maker_site TEXT, category TEXT, ids TEXT, source TEXT, url TEXT);")
    sink, makersite = Sink(db), {}
    # order of insertion = priority: wd, es, epa, ndc, fdc
    wikidata(sink, Path(a.wd), makersite)
    print("wd", sink.n.get("wd"), flush=True)
    energystar(sink, Path(a.es)); print("es", sink.n.get("es"), flush=True)
    epa(sink, Path(a.epa)); print("epa", sink.n.get("epa"), flush=True)
    counts = ndc(sink, Path(a.ndc)); print("ndc", sink.n.get("ndc"), flush=True)
    fdc(sink, Path(a.fdc)); print("fdc", sink.n.get("fdc"), flush=True)
    # maker -> official site: Wikidata first, then the release's brand-sites (brand key and display name)
    sites = dict(makersite)
    with open(a.sites, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3:
                for k in (mkey(p[2]), mkey(p[0])):
                    if k and k not in sites:
                        sites[k] = p[1]
    stats = {}
    resolver = None
    links = {}
    if a.wd2:
        sys.path.insert(0, str(Path(__file__).parent))
        from link_makers import Resolver
        resolver = Resolver(a.wd2)
    cur = db.cursor()
    for rid, maker, site, src, ids, name in db.execute("SELECT id, maker, maker_site, source, ids, name FROM products").fetchall():
        if not site:
            site = site_for(maker, sites)
            if not site and ids.startswith("brand:"):          # FDC: the brand's own entry when the owner has none
                site = site_for(ids[6:].split(" gtin:")[0], sites)
            if not site and resolver:
                brand = ids[6:].split(" gtin:")[0] if ids.startswith("brand:") else (name if src == "ndc" else maker)
                hit = resolver.resolve(maker, brand)
                if hit:
                    site = hit[0]
                    lk = links.setdefault((maker, brand if hit[1].startswith("B") else "", hit[1]), [hit, src, name, 0])
                    lk[3] += 1
            if site:
                cur.execute("UPDATE products SET maker_site=? WHERE id=?", (site, rid))
        s = stats.setdefault(src, [0, 0])
        s[0] += 1
        s[1] += bool(site)
    if a.links_out:
        with open(a.links_out, "w", encoding="utf-8") as lf:
            lf.write("maker\tsite_url\twikidata_id\trule\tbrand\tmatched_label\tsource\texample_name\tn_rows\n")
            for (maker, brand, rule), (hit, src, name, n) in sorted(links.items()):
                lf.write("\t".join([maker, hit[0], hit[2], rule, brand, hit[3], src, name, str(n)]) + "\n")
    db.execute("CREATE VIRTUAL TABLE products_fts USING fts5(name, maker, ids, content='products', content_rowid='id', "
               "tokenize=\"unicode61 remove_diacritics 2\")")
    db.execute("INSERT INTO products_fts(rowid, name, maker, ids) SELECT id, name, maker, ids FROM products")
    db.execute("INSERT INTO products_fts(products_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    json.dump({"rows": sink.n, "site_share": {k: v for k, v in stats.items()}, "ndc_types": counts}, open(str(out) + ".stats.json", "w"), indent=1)
    print(json.dumps({"rows": sink.n, "with_site": stats}))


if __name__ == "__main__":
    main()
