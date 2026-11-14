"""Apply a month as a base-table SQL diff, then re-derive the indexes the client keeps (T4).

  python apply_check.py OLD_COPY DIFF.sql       OLD_COPY is edited in place

DIFF.sql = `sqldiff --table T OLD NEW` for the base tables (places, suggest_*, typo_keys).
The R*Tree and the two FTS5 trigram indexes are not shipped: they are re-derived here from
the rows the diff touched (places by id, typo keys by kid), which is what an updater on the
user's machine would do. Prints rows touched and seconds.
"""
import re, sqlite3, sys, time
import trgm

db_path, diff = sys.argv[1], sys.argv[2]
db = sqlite3.connect(db_path)
db.executescript("PRAGMA journal_mode = DELETE; PRAGMA secure_delete = OFF; PRAGMA cache_size = -200000;")
t0 = time.time()
sql = open(diff, encoding="utf-8").read()
# ids the diff touches, before applying (deletes need the old row's presence)
place_ids = {int(x) for x in re.findall(r"(?:INSERT INTO places\(id,[^)]*\) VALUES\(|UPDATE places SET .*? WHERE id=|DELETE FROM places WHERE id=)(\d+)", sql)}
kids = {int(x) for x in re.findall(r"(?:INSERT INTO typo_keys\(kid,key\) VALUES\(|DELETE FROM typo_keys WHERE kid=)(\d+)", sql)}
db.executescript("BEGIN;" + sql + "COMMIT;")
t1 = time.time()
for pid in place_ids:
    db.execute("DELETE FROM places_geo WHERE id = ?", (pid,))
    db.execute("DELETE FROM place_names WHERE rowid = ?", (pid,))
    r = db.execute("SELECT name, lat, lon, kind_id FROM places WHERE id = ?", (pid,)).fetchone()
    if r:
        if r[1] is not None:
            db.execute("INSERT INTO places_geo VALUES (?,?,?,?,?,?,?)", (pid, r[1], r[1], r[2], r[2], r[3], r[3]))
        if r[0]:
            db.execute("INSERT INTO place_names (rowid, t) VALUES (?, ?)", (pid, trgm.padded(r[0])))
for kid in kids:
    db.execute("DELETE FROM typo_trgm WHERE rowid = ?", (kid,))
    r = db.execute("SELECT key FROM typo_keys WHERE kid = ?", (kid,)).fetchone()
    if r:
        db.execute("INSERT INTO typo_trgm (rowid, t) VALUES (?, ?)", (kid, trgm.padded(r[0])))
db.commit()
print(f"applied SQL diff in {t1 - t0:.0f} s; re-derived {len(place_ids)} places and {len(kids)} typo keys in {time.time() - t1:.0f} s")
print("integrity:", db.execute("PRAGMA quick_check").fetchone()[0])
