#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# rc9 step 2: lookup builder on rc9's own-rank stage, --previous rc8 lookup (stable ids), typo index as a monthly delta
set -eo pipefail
. /mnt/c/logout-wsltest/rc9-common.sh
L6=$O9/lookup; test -s $O9/registry.sqlite && test -s $L6/stage.sqlite || { echo "registry or stage missing"; exit 1; }
rm -rf $L6/lookup.sqlite $L6/places-*.sqlite $L6/hours-web.sqlite $L6/text
log "2.1 lookup builder"
eval $PY --name buildbox-build8 --memory 6g -w /code/tools/sqlite_lookups logout/search-api:1 /code/tools/sqlite_lookups/build.py /w/$REL2/rc9/lookup --registry /w/$REL2/rc9/registry.sqlite --release-id $REL9 --places-dir /places --stage /w/$REL2/rc9/lookup/stage.sqlite --previous /w/$REL2/rc8/lookup
log "BUILD-DONE"
log "2.2 typo: monthly delta on rc8's index"
mv $L6/text/typo $L6/text/typo-fresh
eval $PY --memory 4g logout/search-api:1 /code/release/typo_month.py /w/$REL2/rc8/lookup/text/typo /w/$REL2/rc8/lookup/lookup.sqlite /w/$REL2/rc9/lookup/lookup.sqlite /w/$REL2/rc9/lookup/text/typo $REL9
du -sb $O8/lookup/text/typo $L6/text/typo $L6/text/typo-fresh
log "2.3 sqlguard (stage.sqlite excluded)"
set +e
eval $PY --memory 3g logout/search-api:1 /code/release/release_v1.py sqlguard /w/$REL2/rc9/registry.sqlite /w/$REL2/rc9/lookup/lookup.sqlite /w/$REL2/rc9/lookup/places-osm.sqlite /w/$REL2/rc9/lookup/places-overture.sqlite /w/$REL2/rc9/lookup/hours-web.sqlite > $O9/sqlguard.json 2>&1; echo "sqlguard rc=$?"
head -c 2500 $O9/sqlguard.json; echo
set -e
for f in $O9/registry.sqlite $L6/lookup.sqlite $L6/places-osm.sqlite $L6/places-overture.sqlite $L6/hours-web.sqlite; do
  echo "$(basename $f) raw=$(stat -c %s $f) zstd12=$(zstd -T0 -12 -c "$f" 2>/dev/null | wc -c) sha256=$(sha256sum $f | cut -d' ' -f1)"
done | tee $R/rc9-step2.sizes
log "STEP2-DONE"
