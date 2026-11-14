#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# rc9 step 1: stage the universe once from the Postgres copy (bridge) with the NEW own rank (step 0's own_rank_G.tsv),
# then the registry from that stage, --previous rc8. Never `registry --own-rank` straight from Postgres: it adds 343,621
# `page` rows (rc8, 2026-10-06). --stage and --own-rank exclude each other, so rc8's stage cannot be reused.
set -eo pipefail
. /mnt/c/logout-wsltest/rc9-common.sh
test -s $R/rc5in/own_rank_G.tsv || { echo "no $R/rc5in/own_rank_G.tsv: run rc9-step0.sh first"; exit 1; }
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
mkdir -p $O9/lookup
IN=$R/rc5in
for f in wiki_search_refs.tsv.zst entities.tsv.zst ut1-unsafe.tsv.gz blocked.tsv; do test -s $IN/$f || { echo "no $IN/$f (README: rc9 inputs)"; exit 1; }; done
log "1.0 suggestions from our own rank (release/suggest_own.py: rewrites suggest_items / _keys / _top in this COPY)"
eval $PY --network container:buildbox-pg -e PGHOST=127.0.0.1 -e PGDATABASE=search -e PGUSER=search --memory 3g logout/search-api:1 /code/release/suggest_own.py --own-rank $OWN --blocked /in/blocked.tsv --refs /in/wiki_search_refs.tsv.zst --entities /in/entities.tsv.zst --suggest-sql /code/serve/suggest.sql || { pg_down; exit 1; }
log "1.1 stage from Postgres with own_rank, the block lists and UT1's file (build.stage only)"
eval $PY --network container:buildbox-pg -e PGHOST=127.0.0.1 -e PGDATABASE=search -e PGUSER=search --memory 3g -w /code/tools/sqlite_lookups logout/search-api:1 -c '"import build, psycopg; pg = psycopg.connect(); pg.execute(\"SET statement_timeout = 0\"); build.stage(pg, \"/w/'$REL2'/rc9/lookup/stage.sqlite\", \"'$OWN'\", ut1_unsafe=\"/in/ut1-unsafe.tsv.gz\", blocked=\"/in/blocked.tsv\").close()"' || { pg_down; exit 1; }
ls -l $O9/lookup/stage.sqlite
log "1.2 registry rc9 --previous rc8 --from-stage"
eval $PY --memory 4g -e RELEASE_ID=$REL9 logout/search-api:1 /code/release/release_v1.py registry /w/$REL2/rc9/registry.sqlite $REL9 --previous /w/$REL2/rc8/registry.sqlite --from-stage /w/$REL2/rc9/lookup/stage.sqlite --places-dir /places
sha256sum $O9/registry.sqlite
python3 /mnt/c/logout-wsltest/rc9-regdiff.py $O8/registry.sqlite $O9/registry.sqlite | tee $R/rc9-step1.json
log "STEP1-DONE (Postgres left up)"
