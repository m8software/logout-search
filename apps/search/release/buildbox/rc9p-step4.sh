#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9 step 4: pictures.sqlite from the card-images working file (no Postgres)
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
log "4. pictures-from"
eval $PY -v $R/pics:/pics:ro --memory 4g logout/search-api:1 /code/release/release_v1.py pictures-from /pics/card-images.sqlite /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p/pictures.sqlite $RELP
ls -l $OP/pictures.sqlite; sha256sum $OP/pictures.sqlite | tee $R/rc9p-step4.sha
log "STEP4-DONE"
