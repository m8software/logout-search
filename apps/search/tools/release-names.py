#!/usr/bin/env python3
"""Name samples for the rc16 proof (owner 2026-11-16): articles whose lead CHANGED and articles that did not.

    release-names.py CHANGED.tsv.gz DELETED.tsv RC15_DIR OUT_DIR [--n 300] [--seed 1]

Reads wikidelta's combined change set and the baseline release (registry + lookup cards). A name is a Wikipedia title that
is an exact, popular card in rc15 (its card carries a pageview field `p`), so a title search should answer with that article's
card. Writes OUT_DIR/changed.tsv (page_id, title, old lead[:300], new lead[:300]: leads differ), OUT_DIR/unchanged.tsv
(page_id, title, lead[:300]: not in the change set at all) and OUT_DIR/head200.txt (200 head names from the open eval sets).
"""
import argparse, glob, gzip, json, os, random, sqlite3, sys, zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "importer"))
import wikidelta as D  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("changed"); ap.add_argument("deleted"); ap.add_argument("rc15"); ap.add_argument("out")
    ap.add_argument("--n", type=int, default=300); ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rnd = random.Random(a.seed)
    reg = sqlite3.connect(f"file:{a.rc15}/registry.sqlite?mode=ro", uri=True)
    lk = sqlite3.connect(f"file:{a.rc15}/lookup.sqlite?mode=ro", uri=True)
    gone = {int(l.split("\t")[0]) for l in open(a.deleted) if l.strip()}
    rows = []
    with gzip.open(a.changed, "rt", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if p[0] == "change":
                rows.append((int(p[1]), D.unescape(p[3]) or "", D.unescape(p[5]) or ""))
    touched = {r[0] for r in rows} | gone
    rnd.shuffle(rows)

    ids = {ext: (doc, title) for doc, ext, title in reg.execute(
        "SELECT doc_id, ext, title FROM registry WHERE doc_id >= 268435456 AND doc_id < 536870912")}   # no index on ext: one scan

    def card(pid):
        r = ids.get(pid)
        if not r:
            return None
        c = lk.execute("SELECT c FROM cards WHERE doc_id = ?", (r[0],)).fetchone()
        return json.loads(zlib.decompress(c[0])) if c else None
    ch = []
    for pid, title, lead in rows[:60000]:
        c = card(pid)
        if c and c.get("p") and (c.get("l") or "")[:300] != lead[:300] and "(" not in title[-1:] and len(title) > 3:
            ch.append((pid, title, (c.get("l") or "")[:300], lead[:300]))
            if len(ch) >= a.n:
                break
    pool = sorted(ids)
    un = []
    tried = 0
    while len(un) < a.n and tried < 200000:
        tried += 1
        pid = rnd.choice(pool)
        if pid in touched:
            continue
        r = ids.get(rnd.choice(pool)) if False else ids.get(pid)
        if not r:
            continue
        c = lk.execute("SELECT c FROM cards WHERE doc_id = ?", (r[0],)).fetchone()
        c = json.loads(zlib.decompress(c[0])) if c else None
        if c and c.get("p") and len(r[1]) > 3:
            un.append((pid, r[1], (c.get("l") or "")[:300]))
    with open(os.path.join(a.out, "changed.tsv"), "w", encoding="utf-8") as f:
        for r in ch:
            f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in r) + "\n")
    with open(os.path.join(a.out, "unchanged.tsv"), "w", encoding="utf-8") as f:
        for r in un:
            f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in r) + "\n")
    names = []
    for fn in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "evalsets", "open", "*.tsv"))):
        for line in open(fn, encoding="utf-8", errors="replace"):
            q = line.split("\t")[0].strip()
            if q and not q.startswith("#") and len(q) < 60:
                names.append(q)
    rnd.shuffle(names)
    seen, head = set(), []
    for q in names:
        if q.lower() not in seen:
            seen.add(q.lower()); head.append(q)
        if len(head) >= 200:
            break
    open(os.path.join(a.out, "head200.txt"), "w", encoding="utf-8").write("\n".join(head) + "\n")
    print(json.dumps({"changed": len(ch), "unchanged": len(un), "head": len(head)}))


if __name__ == "__main__":
    main()
