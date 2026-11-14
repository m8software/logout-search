#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# rc9 step 3: signals (own rank, inlinks off), then the web inputs that still read the Postgres copy (bridge), then Postgres down
set -eo pipefail
. /mnt/c/logout-wsltest/rc9-common.sh
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
trap pg_down EXIT
log "3.1 signals rc9"
eval $PY $PGENV --memory 4g -e RELEASE_ID=$REL9 logout/search-api:1 /code/release/release_v1.py signals /w/$REL2/rc9/registry.sqlite /w/$REL2/rc9/signals.bin $REL9 --own-rank $OWN
ls -l $O9/signals.bin; sha256sum $O9/signals.bin
eval $PY --memory 2g logout/search-api:1 /code/release/release_v1.py sigdump /w/$REL2/rc9/signals.bin > $O9/signals.header.json
head -c 600 $O9/signals.header.json; echo
log "3.2 web-input"
eval $PY $PGENV --memory 2500m -e RELEASE_ID=$REL9 logout/search-api:1 /code/release/release_v1.py web-input /w/$REL2/rc9/registry.sqlite /w/$REL2/rc9-web/webinput $REL9
log "3.3 anchor-phrases (twice: same bytes?)"
for o in anchor-phrases.sqlite anchor-phrases.rerun.sqlite; do
  eval $PY $PGENV --memory 2500m -e RELEASE_ID=$REL9 logout/search-api:1 /code/release/release_v1.py anchor-phrases /w/$REL2/rc9/registry.sqlite /w/$REL2/rc9-web/$o $REL9
done
sha256sum $O9W/anchor-phrases.sqlite $O9W/anchor-phrases.rerun.sqlite; rm -f $O9W/anchor-phrases.rerun.sqlite
for f in $O9W/anchor-phrases.sqlite $O9W/webinput/*.zst; do echo "$(basename $f) raw=$(stat -c %s $f) sha256=$(sha256sum $f | cut -d' ' -f1)"; done | tee $R/rc9-step3.sizes
for k in sites pages anchors; do echo "$k rows=$(zstd -dc $O9W/webinput/$k.rows.tsv.zst | wc -l)"; done | tee -a $R/rc9-step3.sizes
log "STEP3-DONE"
