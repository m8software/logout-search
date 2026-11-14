#!/usr/bin/env bash
set -uo pipefail   # -e off on purpose: a failed step is logged (Traceback in the log) and the run goes on to the next measurement
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
R() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 --network search-serve_search-serve-net --env-file pg.env -v ~/t4:/w -w /w --entrypoint "$@"; }
say "LITE run (fts build, all but typo; 2 GB cap)"
R python -e T4_ONLY=places_kinds,places_name,place_exact,suggest,raw --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite2.json /w/lookup.sqlite fts
say "tantivy typo index"; R python --memory 2g t4-lookups:1 tv_typo.py /w/lookup.sqlite /w/typo-tv
say "LITE tv typo run (2 GB cap)"
R python -e T4_ONLY=typo --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite2-tv.json /w/lookup.sqlite tv /w/typo-tv
say "FLOOR: second build of the same data"
./build.sh lookup-b.sqlite > build-b.log 2>&1; tail -1 build-b.log
R python --memory 2g t4-lookups:1 month.py delta /w/lookup.sqlite /w/lookup-b.sqlite
rm -f lookup-b.sqlite
say "MONTH rows 2% (in place, no VACUUM)"
cp lookup.sqlite lookup-m.sqlite
R python --memory 2g t4-lookups:1 month.py /w/lookup-m.sqlite 2 rows
R python --memory 2g t4-lookups:1 month.py delta /w/lookup.sqlite /w/lookup-m.sqlite
say "changeset (sqldiff --changeset)"
R sh --memory 2g t4-lookups:1 -c "time timeout 3600 sqldiff --changeset /w/cs-m.bin /w/lookup.sqlite /w/lookup-m.sqlite; ls -l /w/cs-m.bin; zstd -q -19 -T2 -f /w/cs-m.bin -o /w/cs-m.bin.zst; ls -l /w/cs-m.bin.zst"
say "changeset of base tables only (indexes re-derived on the client)"
R sh --memory 2g t4-lookups:1 -c "for t in places suggest_items suggest_keys suggest_top suggest_phrases typo_keys; do timeout 1800 sqldiff --changeset /w/cs-\$t.bin --table \$t /w/lookup.sqlite /w/lookup-m.sqlite; done; cat /w/cs-places.bin /w/cs-suggest_*.bin /w/cs-typo_keys.bin > /w/cs-base.bin; ls -l /w/cs-base.bin; zstd -q -19 -T2 -f /w/cs-base.bin -o /w/cs-base.bin.zst; ls -l /w/cs-base.bin.zst"
say "LITE run after the month (no VACUUM)"
R python -e T4_ONLY=places_kinds,places_name,suggest --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite-m.json /w/lookup-m.sqlite fts
say "MONTH + VACUUM"
R python --memory 2g t4-lookups:1 month.py vacuum /w/lookup-m.sqlite
R python --memory 2g t4-lookups:1 month.py delta /w/lookup.sqlite /w/lookup-m.sqlite
rm -f lookup-m.sqlite
say "DEMAND re-score (every suggest_items row)"
cp lookup.sqlite lookup-d.sqlite
R python --memory 2g t4-lookups:1 month.py /w/lookup-d.sqlite 0 demand
R python --memory 2g t4-lookups:1 month.py delta /w/lookup.sqlite /w/lookup-d.sqlite
rm -f lookup-d.sqlite
say NEXT-DONE
