#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): the T4 A/B arms A and A2 are the Postgres lookup backend. Delete at phase 7.
# eval.sh — the T4 step 2 whole-engine A/B on testbox (after build.sh lookup-judged.sqlite).
#   A  postgres lookups      B  sqlite, with the legacy tables      C  sqlite, without them
#   A2 postgres again (does the same arm repeat itself?)
set -uo pipefail   # -e off on purpose: one failed arm is logged and the next one still runs
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
say "derive the file without legacy data: no kinds tables, junk rows from UT1 only (= export.py --open)"
rm -rf lookup-open.sqlite
cp lookup-judged.sqlite lookup-open.sqlite && rm -rf typo-rw-open && cp -r lookup-judged.typo-keys typo-rw-open
sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 1 --memory 1g -v ~/t4:/w -w /w --entrypoint python t4-lookups:1 -c "
import sqlite3
c = sqlite3.connect('/w/lookup-open.sqlite')
sz = dict(c.execute('SELECT name, sum(pgsize) FROM dbstat GROUP BY name'))
kinds = [t for (t,) in c.execute(\"SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'kind%'\")]
gone = sum(v for k, v in sz.items() if k.startswith('kind'))
print('dropping', kinds, f'{gone / 1e6:.0f} MB (with their indexes)')
for t in kinds: c.execute(f'DROP TABLE {t}')
n = c.execute(\"DELETE FROM site_junk WHERE source NOT IN ('ut1', 'jev+ut1')\").rowcount
c.commit()
print('site_junk: dropped', n, 'legacy rows; kept', c.execute('SELECT count(*) FROM site_junk').fetchone()[0])
"
arm() {  # arm NAME BACKEND FILE
    say "arm $1 ($2 $3)"
    ./arm.sh "$1" "$2" "$3" || return 1
    python3 run.py search search-queries.txt "res-$1-search.json"
    [ "$1" = A2 ] || python3 run.py instant instant-queries.txt "res-$1-instant.json"
    sudo -n docker logs t4-api 2>&1 | grep -oE "ms=[0-9]+ stages=.*" > "stages-$1.txt"
    sudo -n docker logs t4-api 2>&1 | grep -E "Traceback|Error|failed" | sort | uniq -c | head -20 > "errors-$1.txt"
    # shellcheck disable=SC2024  # the file is ours on purpose, not root's
    sudo -n docker stats --no-stream --format "{{.MemUsage}}" t4-api > "mem-$1.txt"
    sudo -n docker rm -f t4-api >/dev/null
}
arm A postgres lookup-judged.sqlite
arm B sqlite lookup-judged.sqlite
arm C sqlite lookup-open.sqlite
arm A2 postgres lookup-judged.sqlite
say EVAL-DONE
