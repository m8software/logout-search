#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# rc9 step 4: pictures.sqlite from the card-images working file (no Postgres)
set -eo pipefail
. /mnt/c/logout-wsltest/rc9-common.sh
log "4. pictures-from"
eval $PY -v $R/pics:/pics:ro --memory 4g logout/search-api:1 /code/release/release_v1.py pictures-from /pics/card-images.sqlite /w/$REL2/rc9/registry.sqlite /w/$REL2/rc9/pictures.sqlite $REL9
ls -l $O9/pictures.sqlite; sha256sum $O9/pictures.sqlite | tee $R/rc9-step4.sha
log "STEP4-DONE"
