#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9p step 1: stage from the Postgres copy (rc9's tables + se_authors, rc9p-load-se.sh), then the registry --previous rc9.
# No 1.0: the copy already holds rc9's suggest_own output and its inputs did not change. Stops if anything but pages moved.
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
test -s $R/rc5in/own_rank_G.tsv || { echo "no $R/rc5in/own_rank_G.tsv"; exit 1; }
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
mkdir -p $OP/lookup
IN=$R/rc5in
for f in ut1-unsafe.tsv.gz blocked.tsv; do test -s $IN/$f || { echo "no $IN/$f"; exit 1; }; done
log "1.1 stage from Postgres with own_rank, the block lists and UT1's file (build.stage only)"
eval $PY --network container:buildbox-pg -e PGHOST=127.0.0.1 -e PGDATABASE=search -e PGUSER=search --memory 3g -w /code/tools/sqlite_lookups logout/search-api:1 -c '"import build, psycopg; pg = psycopg.connect(); pg.execute(\"SET statement_timeout = 0\"); build.stage(pg, \"/w/'$REL2'/rc9p/lookup/stage.sqlite\", \"'$OWN'\", ut1_unsafe=\"/in/ut1-unsafe.tsv.gz\", blocked=\"/in/blocked.tsv\").close()"' || { pg_down; exit 1; }
ls -l $OP/lookup/stage.sqlite
python3 - $OP/lookup/stage.sqlite <<'EOF'
import sqlite3, sys
s = sqlite3.connect(sys.argv[1])
n = s.execute("SELECT count(*) FROM s_se_authors").fetchone()[0]
print("stage s_se_authors", n, "| pages_capture_out", dict(s.execute("SELECT key, value FROM s_policy")).get("pages_capture_out"))
sys.exit(0 if n == 3124280 else "s_se_authors is not 3124280")
EOF
pg_down                                          # step 2's lookup needs 6g of WSL's 7; step 3 starts Postgres again
log "1.2 registry rc9p --previous rc9 --from-stage"
eval $PY --memory 4g -e RELEASE_ID=$RELP logout/search-api:1 /code/release/release_v1.py registry /w/$REL2/rc9p/registry.sqlite $RELP --previous /w/$REL2/rc9/registry.sqlite --from-stage /w/$REL2/rc9p/lookup/stage.sqlite --places-dir /places
sha256sum $OP/registry.sqlite
python3 /mnt/c/logout-wsltest/rc9-regdiff.py $O9/registry.sqlite $OP/registry.sqlite | tee $R/rc9p-step1.json
python3 - $O9/registry.sqlite $OP/registry.sqlite <<'EOF'
import hashlib, sqlite3, sys
a, b = (sqlite3.connect(p) for p in sys.argv[1:3])
au = b.execute("SELECT count(*), count(DISTINCT doc_id), sum((doc_id >> 28) <> 3) FROM authors").fetchone()
print("authors rows, pages, not-a-page", au)
bad = au[2] != 0
for t in (1, 2, 4, 5, 6, 7, 8):
    h = [hashlib.sha256(repr(db.execute("SELECT * FROM registry WHERE doc_id >> 28 = ? ORDER BY doc_id", (t,)).fetchall()).encode()).hexdigest() for db in (a, b)]
    print(f"type {t} rows identical: {h[0] == h[1]}  sha256 {h[1][:16]}")
    bad |= h[0] != h[1]
sys.exit("STOP: something other than pages moved, or an author is not on a page" if bad else 0)
EOF
log "STEP1-DONE (Postgres down)"
