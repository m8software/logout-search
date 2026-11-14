"""A simulated monthly update of lookup.sqlite, applied in place (T4).

  python month.py DB PCT SCENARIO      edit DB in place: SCENARIO = rows | demand
      rows    PCT% of places (70% edited, 15% closed, 15% new; 1 in 5 edits renames) and
              PCT% of suggest rows (items re-scored, keys dropped / added, suggest_top and
              suggest_phrases rewritten), keeping every derived index (R*Tree, FTS5) in step
      demand  every suggest_items row's demand/score/pop moves a little (a full re-score)
  python month.py delta OLD NEW        changed 4 KiB pages (what a page-level sync sends) + zstd -19
  python month.py vacuum DB            VACUUM in place

Stable ids as export.py assigns them: an edited place keeps its id, a new one takes the
next free seq in its cell; a new typo key the next seq of its trigram count.
"""
import json, os, random, sqlite3, subprocess, sys, threading, time
import trgm

SEQ_BITS, NLON, CELL, TYPO_SEQ_BITS = 20, 720, 0.5, 23


def cell_of(lat, lon):
    return int((lat + 90) // CELL) * NLON + int((lon + 180) // CELL)


def month(path, pct, scenario):
    rnd = random.Random(f"{scenario}{pct}")
    db = sqlite3.connect(path)
    db.executescript("PRAGMA journal_mode = DELETE; PRAGMA secure_delete = OFF; PRAGMA cache_size = -200000;")
    t0 = time.time()
    st = {}
    if scenario == "demand":
        n = db.execute("UPDATE suggest_items SET demand = demand * (0.9 + 0.2 * ((id * 2654435761) % 1000) / 1000.0), "
                       "score = score * (0.95 + 0.1 * ((id * 40503) % 1000) / 1000.0)").rowcount
        db.commit()
        st["suggest_items_rescored"] = n
        print(json.dumps(st | {"s": round(time.time() - t0, 1)}))
        return
    f = pct / 100
    # ---- places
    ids = [r[0] for r in db.execute("SELECT id FROM places")]
    pick = rnd.sample(ids, int(len(ids) * f))
    n_edit, n_del = int(len(pick) * 0.70), int(len(pick) * 0.15)
    edit, dele, model = pick[:n_edit], pick[n_edit:n_edit + n_del], pick[n_edit + n_del:]
    renamed = 0
    for pid in edit:
        if rnd.random() < 0.2:
            name = db.execute("SELECT name FROM places WHERE id = ?", (pid,)).fetchone()[0] or ""
            new = (name + " " + rnd.choice(["Express", "Market", "Center", "II", "Plaza"])).strip()
            db.execute("UPDATE places SET name = ?, name_key = ?, hours = ? WHERE id = ?",
                       (new, "".join(ch for ch in trgm.pg_lower(new) if ch.isascii() and ch.isalnum()), "Mo-Su 07:00-22:00", pid))
            if name:
                db.execute("DELETE FROM place_names WHERE rowid = ?", (pid,))
            db.execute("INSERT INTO place_names (rowid, t) VALUES (?, ?)", (pid, trgm.padded(new)))
            renamed += 1
        else:
            db.execute("UPDATE places SET hours = ?, phone = ? WHERE id = ?",
                       (rnd.choice(["Mo-Fr 09:00-17:00", "Mo-Su 06:00-23:00", "24/7", "Mo-Sa 10:00-21:00"]),
                        f"+1 555 {rnd.randrange(1000000):07d}", pid))
    for pid in dele:
        db.execute("DELETE FROM places WHERE id = ?", (pid,))
        db.execute("DELETE FROM places_geo WHERE id = ?", (pid,))
        db.execute("DELETE FROM place_names WHERE rowid = ?", (pid,))
    nxt = {}
    for k, pid in enumerate(model):
        r = db.execute("SELECT osm, name, kind_id, brand, lat, lon, street, city, hours, website, phone, name_key, brand_key, "
                       "brand_lower FROM places WHERE id = ?", (pid,)).fetchone()
        lat, lon = r[4] + rnd.uniform(-0.01, 0.01), r[5] + rnd.uniform(-0.01, 0.01)
        cell = cell_of(lat, lon)
        if cell not in nxt:
            nxt[cell] = (db.execute("SELECT coalesce(max(id), ?) FROM places WHERE id BETWEEN ? AND ?",
                                    ((cell << SEQ_BITS) - 1, cell << SEQ_BITS, ((cell + 1) << SEQ_BITS) - 1)).fetchone()[0]) + 1
        nid = nxt[cell]; nxt[cell] += 1
        db.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (nid, f"wdc:new{scenario}{k:07d}", *r[1:4], lat, lon, *r[6:]))
        db.execute("INSERT INTO places_geo VALUES (?,?,?,?,?,?,?)", (nid, lat, lat, lon, lon, r[2], r[2]))
        if r[1]:
            db.execute("INSERT INTO place_names (rowid, t) VALUES (?, ?)", (nid, trgm.padded(r[1])))
    st["places"] = {"edited": len(edit), "renamed": renamed, "closed": len(dele), "new": len(model)}
    db.commit()
    # ---- suggest
    ids = [r[0] for r in db.execute("SELECT id FROM suggest_items")]
    for iid in rnd.sample(ids, int(len(ids) * f)):
        db.execute("UPDATE suggest_items SET demand = demand * ?, score = score * ?, pop = pop * ? WHERE id = ?",
                   (rnd.uniform(0.5, 2), rnd.uniform(0.8, 1.2), rnd.uniform(0.8, 1.2), iid))
    keys = db.execute("SELECT key, id, pos FROM suggest_keys").fetchall()
    drop = rnd.sample(keys, int(len(keys) * f / 2))
    for key, iid, pos in drop:
        db.execute("DELETE FROM suggest_keys WHERE key = ? AND id = ? AND pos = ?", (key, iid, pos))
    gone = [k for (k,) in db.execute("SELECT key FROM typo_keys WHERE key NOT IN (SELECT key FROM suggest_keys)")]
    for k in gone:
        kid = db.execute("SELECT kid FROM typo_keys WHERE key = ?", (k,)).fetchone()[0]
        db.execute("DELETE FROM typo_keys WHERE kid = ?", (kid,))
        db.execute("DELETE FROM typo_trgm WHERE rowid = ?", (kid,))
    added = 0
    for key, iid, pos in rnd.sample(keys, int(len(keys) * f / 2)):
        nk = key + rnd.choice(["s", "2026", "app", "hq", "tv"])
        if db.execute("INSERT OR IGNORE INTO suggest_keys VALUES (?, ?, ?, (SELECT kind FROM suggest_items WHERE id = ?), ?)",
                      (nk, iid, pos, iid, rnd.random())).rowcount:
            added += 1
            if not db.execute("SELECT 1 FROM typo_keys WHERE key = ?", (nk,)).fetchone():
                nt = len(trgm.trigrams(nk))
                kid = db.execute("SELECT coalesce(max(kid), ?) + 1 FROM typo_keys WHERE kid BETWEEN ? AND ?",
                                 ((nt << TYPO_SEQ_BITS) - 1, nt << TYPO_SEQ_BITS, ((nt + 1) << TYPO_SEQ_BITS) - 1)).fetchone()[0]
                db.execute("INSERT INTO typo_keys VALUES (?, ?)", (kid, nk))
                db.execute("INSERT INTO typo_trgm (rowid, t) VALUES (?, ?)", (kid, trgm.padded(nk)))
    tops = [r[0] for r in db.execute("SELECT prefix FROM suggest_top")]
    for p in rnd.sample(tops, int(len(tops) * f)):
        ids_, pos_ = db.execute("SELECT ids, pos FROM suggest_top WHERE prefix = ?", (p,)).fetchone()
        a, b = json.loads(ids_), json.loads(pos_)
        z = list(zip(a, b)); rnd.shuffle(z)
        db.execute("UPDATE suggest_top SET ids = ?, pos = ? WHERE prefix = ?",
                   (json.dumps([x for x, _ in z], separators=(",", ":")), json.dumps([y for _, y in z], separators=(",", ":")), p))
    phs = [r[0] for r in db.execute("SELECT prefix FROM suggest_phrases")]
    for p in rnd.sample(phs, int(len(phs) * f)):
        c = json.loads(db.execute("SELECT counts FROM suggest_phrases WHERE prefix = ?", (p,)).fetchone()[0])
        db.execute("UPDATE suggest_phrases SET counts = ? WHERE prefix = ?",
                   (json.dumps([round(x * rnd.uniform(0.7, 1.3), 1) for x in c], separators=(",", ":")), p))
    st["suggest"] = {"items_rescored": int(len(ids) * f), "keys_dropped": len(drop), "typo_keys_gone": len(gone),
                     "keys_added": added, "top_rewritten": int(len(tops) * f), "phrases_rewritten": int(len(phs) * f)}
    db.commit()
    # FTS5 segments are left as the inserts made them (automerge only); an explicit
    # ('merge', N) would rewrite more pages — not measured
    print(json.dumps(st | {"s": round(time.time() - t0, 1)}))


def delta(old, new, page=4096):
    changed = total = 0
    z = subprocess.Popen(["zstd", "-q", "-19", "-T2", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    zs = {"n": 0}

    def drain():
        while True:
            b = z.stdout.read(1 << 20)
            if not b:
                return
            zs["n"] += len(b)
    t = threading.Thread(target=drain); t.start()
    with open(old, "rb") as a, open(new, "rb") as b:
        while True:
            pb = b.read(page)
            if not pb:
                break
            pa = a.read(page)
            total += len(pb)
            if pa != pb:
                changed += len(pb); z.stdin.write(pb)
    z.stdin.close(); t.join(); z.wait()
    print(json.dumps({"old_mb": round(os.path.getsize(old) / 1e6), "new_mb": round(total / 1e6), "changed_pages_mb": round(changed / 1e6, 1),
                      "changed_pct": round(100 * changed / total, 2), "changed_zstd_mb": round(zs["n"] / 1e6, 1)}))


if __name__ == "__main__":
    if sys.argv[1] == "delta":
        delta(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "vacuum":
        c = sqlite3.connect(sys.argv[2]); c.execute("VACUUM"); c.close()
    else:
        month(sys.argv[1], float(sys.argv[2]), sys.argv[3])
