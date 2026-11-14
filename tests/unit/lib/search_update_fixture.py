#!/usr/bin/env python3
"""A SYNTHETIC home-engine release, in the real formats, for the update-package tests and measurements.

    search_update_fixture.py build OUT_BASE [--wiki 3000] [--frac 0.02] [--months 1] [--seed 1] [--tv-docs 0]

Builds `OUT_BASE/r1` (release `fx-1`) and `OUT_BASE/r2` ... (`fx-2`, ...): each month evolves the
synthetic world (a share `--frac` of every kind of thing is removed, changed or new, plus one place
whose Overture GERS id changes) and builds the next release the way the real monthly build does:

  registry.sqlite     the REAL apps/search/release/release_v1.py `registry --from-stage [--previous]`
  lookup.sqlite, places-osm / places-overture / hours-web .sqlite, text/typo
                      the REAL apps/search/tools/sqlite_lookups/build.py (--stage --registry --places-dir)
  signals.bin         a faithful LOSIGNL1 writer (the real one needs Postgres): per-type arrays, the
                      dtypes of the format doc; every value moves each month (replaced whole)
  pictures.sqlite     the release_v1.py `pictures` schema and stamp, synthetic JPEG-like bytes (the real builder needs Postgres): ~40% of
                      the articles have a picture, a few change it every month, the rows of removed articles go
  text/typo           month 1: build.py's one sorted segment; later months: typo_month.py (a copy of last month's index + the
                      keys that left as deletes + the keys that arrived as a new segment)
  text/wiki           a Tantivy index (doc_id + title/body, nothing stored); month N+1 = a COPY of N, delete
                      by doc_id + re-add + one commit (release_v1.cmd_tvmonth's pattern), then tv_normalize
  vectors/ivf         the sem_ivf.py folder: month 1 = assign to fixed centres, month N+1 = update_common.ivf_merge
  manifest.json       format, release, engine stamps, per-file bytes + sha256 + licence tag; no timestamp

Nothing here is real data: ids, names, text and numbers are generated from `--seed`. Everything is
deterministic (same arguments, same bytes).
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
REL = os.path.join(REPO, "apps", "search", "release")
TOOLS = os.path.join(REPO, "apps", "search", "tools", "sqlite_lookups")
sys.path[:0] = [REL, TOOLS]

import numpy as np  # noqa: E402

import update_common as uc  # noqa: E402

try:
    import psycopg  # noqa: F401
except ImportError:                        # the release code imports it at module level; no database is used here
    m = types.ModuleType("psycopg")
    m.Error = Exception
    sys.modules["psycopg"] = m
import release_v1 as rv  # noqa: E402
import registry_idents as ri  # noqa: E402
import typo_month  # noqa: E402

VOCAB = [f"w{i:03d}" + s for i in range(300) for s in ("a", "b")]
PLATFORMS = ["x", "instagram", "github"]


# ------------------------------------------------------------------ the synthetic world

def world(n_wiki: int, seed: int) -> dict:
    rnd = random.Random(seed)
    U = {"seed": seed, "month": 1, "next_pid": 5, "next_item": 1, "wiki": {}, "site": {}, "acct": {}, "yt": {}, "stock": {},
         "osm": {}, "ov": {}, "hw": {}, "item": {}}
    for _ in range(n_wiki):
        add_wiki(U, rnd)
    for _ in range(max(8, n_wiki // 4)):
        add_site(U, rnd)
    for pid in sorted(U["wiki"])[:max(4, n_wiki // 10)]:
        add_acct(U, rnd, pid)
    for _ in range(max(3, n_wiki // 40)):
        add_yt(U, rnd)
    for _ in range(max(4, n_wiki // 80)):
        add_stock(U, rnd)
    U["pic"] = {}
    for pid in sorted(U["wiki"]):
        set_pic(U, pid)
    for _ in range(max(10, n_wiki // 2)):
        add_osm(U, rnd)
    for _ in range(max(6, n_wiki // 4)):
        add_ov(U, rnd)
    return U


def words(rnd, n):
    return " ".join(rnd.choice(VOCAB) for _ in range(n))


def has_pic(pid):
    return hashlib.sha256(f"pic|{pid}".encode()).digest()[0] < 102          # 40% of the articles


def set_pic(U, pid):
    if has_pic(pid):
        U["pic"][pid] = 0


def add_item(U, kind, name, url, domain, wiki):
    i = U["next_item"]
    U["next_item"] += 1
    U["item"][i] = {"kind": kind, "name": name, "url": url, "domain": domain, "wiki": wiki}
    return i


def add_wiki(U, rnd):
    pid = U["next_pid"]
    U["next_pid"] += 3
    title = f"Topic{pid} {rnd.choice(VOCAB)}"
    U["wiki"][pid] = {"title": title, "lead": words(rnd, 30), "gen": 0, "tranco": rnd.choice([None, None, None, rnd.randrange(100, 90000)])}
    if rnd.random() < 0.3:
        add_item(U, "wiki", title, f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}", None, title)


def add_site(U, rnd):
    n = len(U["site"]) + U.get("site_seq", 0)
    U["site_seq"] = U.get("site_seq", 0) + 1
    dom = f"site{n}-{rnd.randrange(10**6)}.example"
    U["site"][dom] = {"title": f"Site {n} {rnd.choice(VOCAB)}", "descr": words(rnd, 8), "tranco": rnd.randrange(1, 10**6), "cc": rnd.randrange(1, 10**6)}
    add_item(U, "site", U["site"][dom]["title"], f"https://{dom}/", dom, None)


def add_acct(U, rnd, pid):
    q = f"Q{pid}"
    pl = rnd.choice(PLATFORMS)
    h = f"h{pid}{rnd.choice('abcdef')}"
    U["acct"][(pl, h, q)] = {"wiki": pid, "sitelinks": rnd.randrange(1, 90)}


def add_yt(U, rnd):
    n = U["next_pid"]
    U["next_pid"] += 3
    U["yt"][f"UC{n:08d}"] = {"name": f"Channel {n} {rnd.choice(VOCAB)}", "followers": rnd.randrange(10**3, 10**7), "sitelinks": rnd.randrange(0, 40)}


def add_stock(U, rnd):
    n = U["next_pid"]
    U["next_pid"] += 3
    U["stock"][f"TK{n}"] = {"name": f"Corp {n}", "cik": 1000 + n, "sitelinks": rnd.randrange(0, 60)}


def place_row(rnd, name, pid):
    lat, lon = 39.0 + rnd.random() * 2, -106.716 + rnd.random() * 2
    return {"name": name, "kind": rnd.choice(["shop=wholesale", "amenity=restaurant", "leisure=fitness_centre"]), "brand": "",
            "lat": round(lat, 5), "lon": round(lon, 5), "street": f"{rnd.randrange(1, 999)} Main St", "city": "Twin Falls",
            "hours": rnd.choice(["Mo-Su 10-20", "Mo-Fr 09:00-17:00", "24/7"]), "website": f"https://p{pid}.example/", "phone": ""}


def add_osm(U, rnd):
    n = U["next_pid"]
    U["next_pid"] += 3
    pid = f"node/{n}"
    U["osm"][pid] = place_row(rnd, f"Osm Place {n} {rnd.choice(VOCAB)}", n)
    if rnd.random() < 0.3:
        U["hw"][pid] = {"hours": "Mo-Su 08-22", "crawled": "2026-09-01", "url": f"https://p{n}.example/hours", "name": U["osm"][pid]["name"]}


def add_ov(U, rnd, name=None, row=None):
    n = U["next_pid"]
    U["next_pid"] += 3
    pid = "ov:%08x-0000-4000-8000-%012x" % (rnd.randrange(2**32), n)
    U["ov"][pid] = row or place_row(rnd, name or f"Ov Place {n} {rnd.choice(VOCAB)}", n)
    if rnd.random() < 0.3:
        U["hw"][pid] = {"hours": "Mo-Sa 09-18", "crawled": "2026-09-02", "url": f"https://q{n}.example/hours", "name": U["ov"][pid]["name"]}
    return pid


def evolve(U: dict, frac: float, seed: int) -> dict:
    """The next month: per kind of thing, frac of the things are touched; a third gone, a third changed, a third new."""
    import copy
    V = copy.deepcopy(U)
    V["month"] = U["month"] + 1
    rnd = random.Random(seed * 1000 + V["month"])

    def pick(d):
        keys = sorted(d)
        k = max(3, round(len(keys) * frac))
        s = rnd.sample(keys, min(k, len(keys)))
        t = len(s) // 3
        return s[:t], s[t:2 * t], len(s) - 2 * t           # removed, changed, number new

    rm, ch, nw = pick(V["wiki"])
    for pid in rm:
        t = V["wiki"].pop(pid)["title"]
        for i in [i for i, it in V["item"].items() if it["wiki"] == t]:
            del V["item"][i]
        for k in [k for k, a in V["acct"].items() if a["wiki"] == pid]:
            del V["acct"][k]
    for pid in ch:
        w = V["wiki"][pid]
        w["lead"] = words(rnd, 30)
        w["gen"] += 1
    for _ in range(nw):
        add_wiki(V, rnd)
    for pid in list(V["pic"]):                             # pictures: rows of removed articles go, new articles get theirs,
        if pid not in V["wiki"]:                           # ~30% of the changed articles get a new picture (a hash, not rnd:
            del V["pic"][pid]                              # the rest of the world must not move)
    for pid in sorted(V["wiki"]):
        if pid not in V["pic"]:
            set_pic(V, pid)
    for pid in ch:
        if pid in V["pic"] and hashlib.sha256(f"newpic|{pid}|{V['month']}".encode()).digest()[0] < 77:
            V["pic"][pid] += 1
    rm, ch, nw = pick(V["site"])
    for d in rm:
        del V["site"][d]
        for i in [i for i, it in V["item"].items() if it["domain"] == d]:
            del V["item"][i]
    for d in ch:
        V["site"][d]["descr"] = words(rnd, 8)
    for _ in range(nw):
        add_site(V, rnd)
    for pid in sorted(V["wiki"])[-max(2, nw):]:
        if rnd.random() < 0.5:
            add_acct(V, rnd, pid)
    rm, ch, nw = pick(V["yt"])
    for c in rm:
        del V["yt"][c]
    for c in ch:
        V["yt"][c]["followers"] += 1000
    for _ in range(nw):
        add_yt(V, rnd)
    rm, ch, nw = pick(V["osm"])
    for p in rm:
        del V["osm"][p]
        V["hw"].pop(p, None)
    for p in ch:
        V["osm"][p]["hours"] = "Mo-Su 06-23"
    for _ in range(nw):
        add_osm(V, rnd)
    rm, ch, nw = pick(V["ov"])
    for p in rm:
        del V["ov"][p]
        V["hw"].pop(p, None)
    half = max(1, len(ch) // 2)
    for p in ch[:half]:                                    # a changed GERS id: the same place under a NEW id
        row = V["ov"].pop(p)
        hw = V["hw"].pop(p, None)
        np_ = add_ov(V, rnd, row=row)
        if hw:
            V["hw"][np_] = hw
    for p in ch[half:]:
        V["ov"][p]["hours"] = "Mo-Su 06-23"
    for _ in range(nw):
        add_ov(V, rnd)
    return V


# ------------------------------------------------------------------ stage + places release for the real builders

def norm(s):
    return "".join(c for c in s.lower() if c.isascii() and c.isalnum())


def write_stage(U, path, release):
    import build as b
    if os.path.exists(path):
        os.remove(path)
    st = sqlite3.connect(path)
    for t, (cols, _sql) in b.STAGE.items():
        st.execute(f"CREATE TABLE {t} ({cols})")

    def I(t, rows):
        rows = list(rows)
        if rows:
            st.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(rows[0]))})", rows)

    titles = sorted(w["title"] for w in U["wiki"].values())
    tord = {t: i + 1 for i, t in enumerate(titles)}
    I("s_wiki", [(p, w["title"], tord[w["title"]], w["lead"]) for p, w in sorted(U["wiki"].items())])
    I("s_alias", [(norm(w["title"]), p) for p, w in sorted(U["wiki"].items())])
    I("s_wsites", [(p, f"https://www.wsite{p}.example/", f"wsite{p}.example", "infobox", w["tranco"])
                   for p, w in sorted(U["wiki"].items()) if w["tranco"]])
    I("s_docs", [(d, f"https://{d}/", s["title"], s["descr"]) for d, s in sorted(U["site"].items())])
    I("s_serve", [(d, s["tranco"], s["cc"]) for d, s in sorted(U["site"].items())])
    I("s_nav", [(d, norm(d.split(".")[0]), s["tranco"], d) for d, s in sorted(U["site"].items())])
    I("s_own", [(d, d, s["tranco"], s["cc"]) for d, s in sorted(U["site"].items())])
    vals = sorted({h for (pl, h, q), a in U["acct"].items() if a["wiki"] in U["wiki"]})
    vord = {h: i + 1 for i, h in enumerate(vals)}                       # dense_rank() over value, as the real staging does
    I("s_links", [(pl, q, h, f"https://{pl}.example/{h}", U["wiki"][a["wiki"]]["title"], "fixture account", U["wiki"][a["wiki"]]["title"],
                   a["sitelinks"], norm(h), None, None, None, vord[h]) for (pl, h, q), a in sorted(U["acct"].items()) if a["wiki"] in U["wiki"]])
    I("s_yt", [(c, norm(y["name"]), y["name"], "videos", None, y["followers"], y["sitelinks"], norm(y["name"]), None, None, None)
               for c, y in sorted(U["yt"].items())])
    I("s_stock", [(t, "Nasdaq", s["name"], s["cik"], None, s["name"], None, s["sitelinks"], None, 0, i + 1)
                  for i, (t, s) in enumerate(sorted(U["stock"].items()))])
    I("s_stock_keys", [(t.lower(), t) for t in sorted(U["stock"])])
    items = sorted(U["item"].items())
    I("s_items", [(i, it["kind"], it["name"], it["url"], it["domain"], it["wiki"], 1.0 + (i % 7), 2.0 + (i % 5), 100.0 + (i % 11), None)
                  for i, it in items])
    I("s_keys", [(norm(it["name"]), i, 0) for i, it in items if norm(it["name"])])
    tops = {}
    for i, it in items:
        k = norm(it["name"])[:3]
        if k:
            tops.setdefault(k, []).append(i)
    I("s_top", [(k, json.dumps(v[:5]), json.dumps([0] * len(v[:5]))) for k, v in sorted(tops.items())])
    I("s_phrases", [(k, json.dumps([k + " hours"]), json.dumps([500.0])) for k in sorted(tops)[:50]])
    if U.get("suggest_meta", True):      # what release/suggest_own.py leaves in the database (rc5 W2); False = a build without it
        I("s_sugmeta", [("history", "none"), ("own_rank_sha256", "fixture"), ("rank_source", "own_rank"), ("site_cut", "100000")])
    st.commit()
    st.close()


def zst(path, text):
    open(path, "wb").write(subprocess.run(["zstd", "-q", "-19", "-c"], input=text.encode(), capture_output=True, check=True).stdout)


def write_places_release(U, d, release):
    os.makedirs(d, exist_ok=True)

    def cols(p, r):
        return (p, r["name"], r["kind"], r["brand"], str(r["lat"]), str(r["lon"]), r["street"], r["city"], r["hours"], r["website"], r["phone"], "", "")

    def tsv(rows):
        return "".join("\t".join(r) + "\n" for r in rows)
    zst(f"{d}/places-osm.tsv.zst", tsv(cols(p, r) for p, r in sorted(U["osm"].items())))
    zst(f"{d}/places-overture.tsv.zst", tsv(cols(p, r) for p, r in sorted(U["ov"].items())))
    zst(f"{d}/hours_web.tsv.zst", tsv((p, h["hours"], h["crawled"], h["url"], h["name"]) for p, h in sorted(U["hw"].items())))

    def sha(n):
        return hashlib.sha256(open(f"{d}/{n}", "rb").read()).hexdigest()
    man = {"release": f"places-{release}",
           "columns": {"places": ["osm", "name", "kind", "brand", "lat", "lon", "street", "city", "hours", "website", "phone", "hours_checked", "edited"],
                       "hours_web": ["osm", "hours", "crawled", "url", "name"]},
           "sources": {"osm": {"rows": len(U["osm"])}, "overture": {"release": "fixture"}, "hours": {"crawl": "fixture"}},
           "files": {"places-osm.tsv.zst": {"sha256": sha("places-osm.tsv.zst"), "bytes": os.path.getsize(f"{d}/places-osm.tsv.zst"), "rows": len(U["osm"]), "licence": "ODbL-1.0"},
                     "places-overture.tsv.zst": {"sha256": sha("places-overture.tsv.zst"), "bytes": os.path.getsize(f"{d}/places-overture.tsv.zst"), "rows": len(U["ov"]), "licence": "CDLA-Permissive-2.0"},
                     "hours_web.tsv.zst": {"sha256": sha("hours_web.tsv.zst"), "bytes": os.path.getsize(f"{d}/hours_web.tsv.zst"), "rows": len(U["hw"]), "licence": "facts"}}}
    json.dump(man, open(f"{d}/manifest.json", "w"), sort_keys=True)


def pic_bytes(pid, gen):
    """Deterministic incompressible bytes of a thumbnail's size (2-4 KB here; the real ones are ~15 KB JPEGs)."""
    n = 2000 + int.from_bytes(hashlib.sha256(f"len|{pid}|{gen}".encode()).digest()[:2], "little") % 2000
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(f"img|{pid}|{gen}|{i}".encode()).digest()
        i += 1
    return b"\xff\xd8" + out[:n]


def write_pictures(U, registry, out, release):
    """pictures.sqlite as release_v1.cmd_pictures writes it (same table, same stamp), from the synthetic world."""
    ids = wiki_doc_ids(registry)
    db = rv.new_db(out)
    db.execute("CREATE TABLE pictures (doc_id INTEGER PRIMARY KEY, file TEXT, img BLOB, w INTEGER, h INTEGER, "
               "author TEXT, licence TEXT, licence_url TEXT)")
    for pid in sorted(U["pic"], key=lambda p: ids[p]):
        g = U["pic"][pid]
        db.execute("INSERT INTO pictures VALUES (?,?,?,?,?,?,?,?)", (ids[pid], f"File:Topic{pid}-{g}.jpg", pic_bytes(pid, g), 250, 180,
                                                                     f"Author {pid % 17}", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"))
    rv.stamp(db, "pictures", release, {"licence": "per row (Wikimedia Commons)"})
    rv.finish(db)


# ------------------------------------------------------------------ signals.bin (LOSIGNL1)

SIGNAL_FIELDS = {   # type: [(field, dtype)] — the format doc's table
    "wiki": [("pageviews", "<u4"), ("sitelinks", "<u2"), ("site_rank", "<u4"), ("suggest_pop", "<f4"), ("suggest_score", "<f4"), ("suggest_demand", "<f4")],
    "site": [("own_rank", "<u4"), ("cc_hc_pos", "<u4"), ("nav_rank", "<u4"), ("suggest_pop", "<f4"), ("suggest_score", "<f4"), ("suggest_demand", "<f4")],
    "link": [("nav_rank", "<u4"), ("suggest_pop", "<f4"), ("suggest_score", "<f4"), ("suggest_demand", "<f4")],
    "account": [("sitelinks", "<u2")],
    "youtube": [("followers", "<u8"), ("sitelinks", "<u2")],
    "stock": [("sitelinks", "<u2")],
}


LEGACY_HASH_NAME = {"own_rank": "tranco_rank"}      # the values (and so every golden built on them) predate the rename


def write_signals(registry, out, release, month):
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    ident = {}
    for did, typ, idt in db.execute(f"SELECT doc_id, type, ident FROM ({ri.ident_select()}) ORDER BY doc_id"):
        ident[did] = idt
    size = {}
    for did in ident:
        t = did >> ri.TYPE_SHIFT
        size[t] = max(size.get(t, 0), (did & ri.SEQ_MASK) + 1)
    fields, arrays, off = [], [], 0
    for tname, fl in SIGNAL_FIELDS.items():
        t = ri.TYPES[tname]
        for fname, dt in fl:
            a = np.zeros(size.get(t, 0), dtype=dt)
            for did, idt in ident.items():
                if did >> ri.TYPE_SHIFT != t:
                    continue
                h = int.from_bytes(hashlib.sha256(f"{idt}|{LEGACY_HASH_NAME.get(fname, fname)}|{month}".encode()).digest()[:4], "little")
                a[did & ri.SEQ_MASK] = (h % min(60000, np.iinfo(dt).max) + 1) if dt[1] != "f" else (h % 1000) / 10 + 0.5
            fields.append({"name": f"{tname}.{fname}", "type": tname, "dtype": dt, "count": len(a), "offset": off,
                           "source": "fixture", "licence": "CC0", "note": ""})
            arrays.append(a)
            off += a.nbytes
    head = json.dumps({"format": 1, "built_release": release, "type_shift": ri.TYPE_SHIFT, "fields": fields}, sort_keys=True).encode()
    with open(out, "wb") as f:
        f.write(b"LOSIGNL1" + struct.pack("<I", len(head)) + head)
        for a in arrays:
            f.write(a.tobytes())


# ------------------------------------------------------------------ text/wiki (Tantivy) and vectors/ivf

def wiki_doc_ids(registry):
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    return {ext: did for did, ext in db.execute("SELECT doc_id, ext FROM registry WHERE doc_id >> 28 = 1")}


def tv_schema():
    import tantivy
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("doc_id", stored=False, indexed=True, fast=True)      # u64, as the real index (the Rust builder)
    sb.add_text_field("title", stored=False, tokenizer_name="en_stem", index_option="position")
    sb.add_text_field("body", stored=False, tokenizer_name="en_stem", index_option="freq")
    return sb.build()


def tv_doc(did, title, lead):
    import tantivy
    d = tantivy.Document()
    d.add_unsigned("doc_id", did)
    d.add_text("title", title)
    d.add_text("body", f"{title} {lead}")
    return d


def build_wiki_index(U, prev_U, registry, prev_dir, out, release):
    """Month 1: one commit of every article. Later: a copy of last month's index, delete + re-add what changed, one commit."""
    import tantivy
    ids = wiki_doc_ids(registry)
    if prev_dir is None:
        os.makedirs(out)
        idx = tantivy.Index(tv_schema(), path=out)
        w = idx.writer(heap_size=100_000_000, num_threads=1)
        for pid, a in sorted(U["wiki"].items()):
            w.add_document(tv_doc(ids[pid], a["title"], a["lead"]))
    else:
        shutil.copytree(prev_dir, out)
        for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
            if os.path.exists(os.path.join(out, f)):
                os.remove(os.path.join(out, f))
        idx = tantivy.Index.open(out)
        w = idx.writer(heap_size=100_000_000, num_threads=1)
        old_ids = wiki_doc_ids(os.path.normpath(os.path.join(prev_dir, "..", "..", "registry.sqlite")))
        gone = [p for p in prev_U["wiki"] if p not in U["wiki"]]
        changed = [p for p, a in U["wiki"].items() if p in prev_U["wiki"] and prev_U["wiki"][p] != a]
        new = [p for p in U["wiki"] if p not in prev_U["wiki"]]
        uc.tv_delete_doc_ids(w, idx.schema, [old_ids[p] for p in sorted(gone + changed)])
        for p in sorted(changed + new):
            w.add_document(tv_doc(ids[p], U["wiki"][p]["title"], U["wiki"][p]["lead"]))
    w.commit()
    w.wait_merging_threads()
    del w, idx
    rv.tv_normalize(out, release, "wiki", None, {"analyzer": "en_stem"})
    uc.tv_sort_segments(out)


IVF_LISTS = 16


def code_of(pid, gen):
    rnd = np.random.default_rng(pid * 1000 + gen)
    proto = np.random.default_rng(pid % IVF_LISTS + 777).integers(0, 2, 768, dtype=np.uint8)
    flip = (rnd.random(768) < 0.15).astype(np.uint8)
    return np.packbits(proto ^ flip)


def build_ivf(U, prev_U, prev_dir, out, release):
    os.makedirs(out)
    if prev_dir is None:
        pids = sorted(U["wiki"])
        codes = np.stack([code_of(p, U["wiki"][p]["gen"]) for p in pids])
        cent = np.stack([np.packbits(np.random.default_rng(k + 777).integers(0, 2, 768, dtype=np.uint8)) for k in range(IVF_LISTS)])
        uc.ivf_write_fresh(out, cent, np.array(pids, np.int32), codes, release)
        return
    old_ids = {p for p in prev_U["wiki"]}
    removed = sorted(p for p in prev_U["wiki"] if p not in U["wiki"] or prev_U["wiki"][p] != U["wiki"][p])
    added = sorted(p for p in U["wiki"] if p not in old_ids or prev_U["wiki"][p] != U["wiki"][p])
    ncodes = np.stack([code_of(p, U["wiki"][p]["gen"]) for p in added]) if added else np.zeros((0, uc.DIM_BYTES), np.uint8)
    uc.ivf_merge(prev_dir, out, np.array(removed, np.int32), np.array(added, np.int32), ncodes, release)


# ------------------------------------------------------------------ one release

LICENCE = {"pictures.sqlite": "per row (Wikimedia Commons, fixture)", "registry.sqlite": "CC-BY-SA-4.0 + CC0-1.0 (fixture)", "places-osm.sqlite": "ODbL-1.0", "places-overture.sqlite": "CDLA-Permissive-2.0",
           "hours-web.sqlite": "facts", "signals.bin": "CC0 (fixture)", "lookup.sqlite": "CC0-1.0 (fixture)"}


def licence_of(p):
    return LICENCE.get(p, "fixture (synthetic, no real data)")


def write_manifest(rel, release):
    files = []
    for d, _, fs in os.walk(rel):
        for f in sorted(fs):
            p = os.path.join(d, f)
            r = os.path.relpath(p, rel)
            if r in ("manifest.json", "manifest.json.sig") or f.endswith(".lock"):
                continue
            files.append({"path": r, "bytes": os.path.getsize(p), "sha256": uc.sha256_file(p), "licence": licence_of(r)})
    files.sort(key=lambda x: x["path"])
    import tantivy  # noqa: F401
    m = {"format": rv.FORMAT_VERSION, "release": release, "fixture": True,
         "engine": {"tantivy": rv.ex.TANTIVY_VERSION, "analyzer": {"name": "en_stem"},
                    "registry": {"doc_id": "type << 28 | seq", "layout": ri.LAYOUT},
                    "vectors": {"ivf": uc.IVF_FORMAT}},
         "files": files, "total_bytes": sum(f["bytes"] for f in files)}
    open(os.path.join(rel, "manifest.json"), "wb").write(json.dumps(m, indent=1, sort_keys=True).encode())


def build_release(U, prev_U, prev_dir, base, name, release, tmp, stable=False):
    rel = os.path.join(base, name)
    if os.path.exists(rel):
        shutil.rmtree(rel)
    os.makedirs(rel)
    stage = os.path.join(tmp, f"{name}-stage.sqlite")
    pdir = os.path.join(tmp, f"{name}-places")
    write_stage(U, stage, release)
    write_places_release(U, pdir, release)
    reg = os.path.join(rel, "registry.sqlite")
    prev_reg = os.path.join(prev_dir, "registry.sqlite") if prev_dir else None
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rv.cmd_registry(reg, release, prev_reg, stage, None, pdir)
    write_signals(reg, os.path.join(rel, "signals.bin"), release, U["month"])
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "build.py"), rel, "--stage", stage, "--registry", reg, "--places-dir", pdir,
                        "--release-id", release] + (["--previous", prev_dir] if stable and prev_dir else []),
                       capture_output=True, text=True)
    if "BUILD-OK" not in r.stdout:
        sys.exit(f"build.py failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    typo = os.path.join(rel, "text", "typo")
    if prev_dir is None:
        rv.tv_normalize(typo, release, "typo")
    else:                                                  # months 2+: a delta on last month's index, not build.py's fresh one
        shutil.rmtree(typo)
        r = typo_month.month(os.path.join(prev_dir, "text", "typo"), os.path.join(prev_dir, "lookup.sqlite"), os.path.join(rel, "lookup.sqlite"),
                             typo, release)
        assert r["mode"] == "delta", r
    write_pictures(U, reg, os.path.join(rel, "pictures.sqlite"), release)
    build_wiki_index(U, prev_U, reg, os.path.join(prev_dir, "text", "wiki") if prev_dir else None, os.path.join(rel, "text", "wiki"), release)
    build_ivf(U, prev_U, os.path.join(prev_dir, "vectors", "ivf") if prev_dir else None, os.path.join(rel, "vectors", "ivf"), release)
    write_manifest(rel, release)
    return rel


def build_chain(base, wiki=3000, frac=0.02, months=1, seed=1, stable=False):
    os.makedirs(base, exist_ok=True)
    tmp = os.path.join(base, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    U = world(wiki, seed)
    prev_U, prev = None, None
    out = []
    for m in range(1, months + 2):
        rel = build_release(U, prev_U, prev, base, f"r{m}", f"fx-{m}", tmp, stable)
        out.append(rel)
        prev_U, prev = U, rel
        U = evolve(U, frac, seed)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("base")
    ap.add_argument("--wiki", type=int, default=3000)
    ap.add_argument("--frac", type=float, default=0.02)
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--stable", action="store_true", help="build.py --previous: ids stable across months")
    a = ap.parse_args()
    for r in build_chain(a.base, a.wiki, a.frac, a.months, a.seed, a.stable):
        print(r)
