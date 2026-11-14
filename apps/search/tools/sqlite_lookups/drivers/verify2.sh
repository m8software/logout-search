#!/usr/bin/env bash
set -uo pipefail   # -e off on purpose: a failed step is logged (Traceback in the log) and the run goes on to the next measurement
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
R() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 -v ~/t4:/w -w /w --entrypoint "$@"; }
say "typo-tv directory, zstd (full re-ship bound)"
R sh --memory 2g t4-lookups:1 -c "tar -C /w -cf - typo-tv | zstd -q -19 -T2 | wc -c"   # zstd is not on the testbox host
cp lookup.sqlite lookup-m.sqlite
R python --memory 2g t4-lookups:1 month.py /w/lookup-m.sqlite 2 rows
cp lookup.sqlite lookup-a.sqlite
R python --memory 2g t4-lookups:1 apply_check.py /w/lookup-a.sqlite /w/month.sql
say "row check"
R python --memory 2g t4-lookups:1 rowcheck.py
say "answers incl. place_exact and typo (fts) on the applied copy vs the month file"
cat > qsub.py <<"PY"
import json; q=json.load(open("/w/qsets.json")); json.dump({"place_exact": q["place_exact"], "typo": q["typo"][:300]}, open("/w/qsets-sub.json","w"))
PY
R python --memory 2g t4-lookups:1 qsub.py
R python --memory 2g t4-lookups:1 bench.py run lite /w/qsets-sub.json /w/res-sub-m.json /w/lookup-m.sqlite fts
R python --memory 2g t4-lookups:1 bench.py run lite /w/qsets-sub.json /w/res-sub-a.json /w/lookup-a.sqlite fts
R python --memory 2g t4-lookups:1 bench.py compare /w/res-sub-m.json /w/res-sub-a.json applied
rm -f lookup-a.sqlite lookup-m.sqlite
say VERIFY3-DONE
