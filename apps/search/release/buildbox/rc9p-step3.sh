#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9 step 3: signals (own rank, inlinks off), then the web inputs that still read the Postgres copy (bridge), then Postgres down
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
trap pg_down EXIT
log "3.1 signals rc9"
eval $PY $PGENV --memory 4g -e RELEASE_ID=$RELP logout/search-api:1 /code/release/release_v1.py signals /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p/signals.bin $RELP --own-rank $OWN
ls -l $OP/signals.bin; sha256sum $OP/signals.bin
eval $PY --memory 2g logout/search-api:1 /code/release/release_v1.py sigdump /w/$REL2/rc9p/signals.bin > $OP/signals.header.json
head -c 600 $OP/signals.header.json; echo
log "3.2 web-input"
eval $PY $PGENV --memory 2500m -e RELEASE_ID=$RELP logout/search-api:1 /code/release/release_v1.py web-input /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p-web/webinput $RELP
log "3.3 anchor-phrases (twice: same bytes?)"
for o in anchor-phrases.sqlite anchor-phrases.rerun.sqlite; do
  eval $PY $PGENV --memory 2500m -e RELEASE_ID=$RELP logout/search-api:1 /code/release/release_v1.py anchor-phrases /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p-web/$o $RELP
done
sha256sum $OPW/anchor-phrases.sqlite $OPW/anchor-phrases.rerun.sqlite; rm -f $OPW/anchor-phrases.rerun.sqlite
for f in $OPW/anchor-phrases.sqlite $OPW/webinput/*.zst; do echo "$(basename $f) raw=$(stat -c %s $f) sha256=$(sha256sum $f | cut -d' ' -f1)"; done | tee $R/rc9p-step3.sizes
for k in sites pages anchors; do echo "$k rows=$(zstd -dc $OPW/webinput/$k.rows.tsv.zst | wc -l)"; done | tee -a $R/rc9p-step3.sizes
log "STEP3-DONE"
