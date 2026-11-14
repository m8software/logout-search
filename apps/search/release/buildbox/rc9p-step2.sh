#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9p step 2: lookup builder on rc9p's stage, --previous rc9 lookup (stable ids), typo index as a monthly delta
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
L6=$OP/lookup; test -s $OP/registry.sqlite && test -s $L6/stage.sqlite || { echo "registry or stage missing"; exit 1; }
rm -rf $L6/lookup.sqlite $L6/places-*.sqlite $L6/hours-web.sqlite $L6/text
log "2.1 lookup builder"
eval $PY --name buildbox-build8 --memory 6g -w /code/tools/sqlite_lookups logout/search-api:1 /code/tools/sqlite_lookups/build.py /w/$REL2/rc9p/lookup --registry /w/$REL2/rc9p/registry.sqlite --release-id $RELP --places-dir /places --stage /w/$REL2/rc9p/lookup/stage.sqlite --previous /w/$REL2/rc9/lookup
log "BUILD-DONE"
log "2.2 typo: monthly delta on rc9's index"
mv $L6/text/typo $L6/text/typo-fresh
eval $PY --memory 4g logout/search-api:1 /code/release/typo_month.py /w/$REL2/rc9/lookup/text/typo /w/$REL2/rc9/lookup/lookup.sqlite /w/$REL2/rc9p/lookup/lookup.sqlite /w/$REL2/rc9p/lookup/text/typo $RELP
du -sb $O9/lookup/text/typo $L6/text/typo $L6/text/typo-fresh
log "2.3 sqlguard (stage.sqlite excluded)"
set +e
eval $PY --memory 3g logout/search-api:1 /code/release/release_v1.py sqlguard /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p/lookup/lookup.sqlite /w/$REL2/rc9p/lookup/places-osm.sqlite /w/$REL2/rc9p/lookup/places-overture.sqlite /w/$REL2/rc9p/lookup/hours-web.sqlite > $OP/sqlguard.json 2>&1; echo "sqlguard rc=$?"
head -c 2500 $OP/sqlguard.json; echo
set -e
for f in $OP/registry.sqlite $L6/lookup.sqlite $L6/places-osm.sqlite $L6/places-overture.sqlite $L6/hours-web.sqlite; do
  echo "$(basename $f) raw=$(stat -c %s $f) zstd12=$(zstd -T0 -12 -c "$f" 2>/dev/null | wc -c) sha256=$(sha256sum $f | cut -d' ' -f1)"
done | tee $R/rc9p-step2.sizes
log "STEP2-DONE"
