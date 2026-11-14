"""rootB2 = rootB1 with the exact-name rows (names kind 4 nav host, kind 5 typed domain) of every registry site that is
NOT in own_rank.tsv deleted: the sites the Tranco list alone put in nav_sites no longer answer by name (eval only).
Hosts own_rank adds that the registry lacks are not added (that needs a registry rebuild); typo/label indexes unchanged."""
import sqlite3, sys
own_tsv, reg, lookup = sys.argv[1:4]
own = set(l.split('\t', 1)[0] for l in open(own_tsv))
db = sqlite3.connect(f"file:{reg}?mode=ro", uri=True)
rm = [(did,) for did, host in db.execute("SELECT doc_id, path FROM registry WHERE doc_id >> 28 = 2") if host not in own]
print('registry sites', db.execute("SELECT count(*) FROM registry WHERE doc_id >> 28 = 2").fetchone()[0], 'not in own_rank', len(rm), flush=True)
lk = sqlite3.connect(lookup)
lk.execute("PRAGMA temp_store = FILE")
lk.execute("CREATE TEMP TABLE rm (doc_id INTEGER PRIMARY KEY)")
lk.executemany("INSERT INTO rm VALUES (?)", rm)
n0 = lk.execute("SELECT count(*) FROM names WHERE kind IN (4, 5)").fetchone()[0]
lk.execute("DELETE FROM names WHERE kind IN (4, 5) AND doc_id IN (SELECT doc_id FROM rm)")
lk.commit()
n1 = lk.execute("SELECT count(*) FROM names WHERE kind IN (4, 5)").fetchone()[0]
print('names kind 4,5 rows', n0, '->', n1, flush=True)
