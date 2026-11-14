#!/usr/bin/env bash
set -uo pipefail   # -e off on purpose: a failed step is logged (Traceback in the log) and the run goes on to the next measurement
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
R() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 -v ~/t4:/w -w /w --entrypoint "$@"; }
mv -f month.py.fixed month.py
say "MONTH rows 2% again (same seed) -> lookup-m.sqlite"
cp lookup.sqlite lookup-m.sqlite
R python --memory 2g t4-lookups:1 month.py /w/lookup-m.sqlite 2 rows
say "full changeset incl. FTS5/R*Tree shadow tables"
R sh --memory 2g t4-lookups:1 -c "s=\$(date +%s); timeout 3600 sqldiff --changeset /w/cs-m.bin /w/lookup.sqlite /w/lookup-m.sqlite; echo rc=\$? secs=\$((\$(date +%s)-s)); ls -l /w/cs-m.bin; zstd -q -19 -T2 -f /w/cs-m.bin -o /w/cs-m.bin.zst; ls -l /w/cs-m.bin.zst"
say "base-table SQL diff, applied to a copy, indexes re-derived"
R sh --memory 2g t4-lookups:1 -c "for t in places suggest_items suggest_keys suggest_top suggest_phrases typo_keys; do sqldiff --table \$t /w/lookup.sqlite /w/lookup-m.sqlite; done > /w/month.sql; ls -l /w/month.sql; zstd -q -19 -T2 -f /w/month.sql -o /w/month.sql.zst; ls -l /w/month.sql.zst"
cp lookup.sqlite lookup-a.sqlite
R python --memory 2g t4-lookups:1 apply_check.py /w/lookup-a.sqlite /w/month.sql
R sh --memory 2g t4-lookups:1 -c "for t in places suggest_items suggest_keys suggest_top suggest_phrases typo_keys; do sqldiff --table \$t /w/lookup-m.sqlite /w/lookup-a.sqlite; done | wc -l"
say "answers on the applied copy vs the month file"
R python -e T4_ONLY=places_kinds,places_name,suggest --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite-a.json /w/lookup-a.sqlite fts
R python --memory 2g t4-lookups:1 bench.py compare /w/res-lite-m.json /w/res-lite-a.json applied
rm -f lookup-a.sqlite lookup-m.sqlite
say VERIFY-DONE
say "places_name with the prefix filter (fresh file)"
R python -e T4_ONLY=places_name -e T4_PLACE_PREFIX=1 --memory 2g t4-lookups:1 bench.py run lite /w/qsets.json /w/res-lite-prefix.json /w/lookup.sqlite fts
say VERIFY2-DONE
