#!/usr/bin/env bash
set -uo pipefail   # -e off on purpose: a failed step is logged (Traceback in the log) and the run goes on to the next measurement
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
R() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 --network search-serve_search-serve-net --env-file pg.env -v ~/t4:/w -w /w --entrypoint python "$@"; }
say "tantivy typo index"; R --memory 2g t4-lookups:1 tv_typo.py /w/lookup.sqlite /w/typo-tv
say "PG run"; R --memory 2g t4-lookups:1 bench.py run pg /w/qsets.json /w/res-pg.json
say "LITE fts run (2 GB cap)"; R --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite.json /w/lookup.sqlite fts
say "LITE tv typo run (2 GB cap)"; T4_ONLY=typo R -e T4_ONLY=typo --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite-tv.json /w/lookup.sqlite tv /w/typo-tv
say BENCH-DONE
