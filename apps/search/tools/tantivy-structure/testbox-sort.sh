#!/usr/bin/env bash
# Popularity order for the export (ranks + external sort with zstd-compressed temp files).
set -euo pipefail
cd ~/t6
exec >> sort.log 2>&1
echo "== $(date -u +%T) ranks; load $(cut -d' ' -f1-3 /proc/loadavg)"
sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 --memory 4g -v ~/t6:/w -v ~/dbtest:/d:ro -w /w dbt-tantivy:1 \
  sh -c 'python pop_order.py ranks /d/wiki_docs.tsv.zst pop-suggest.tsv ranks.tsv'
echo "== $(date -u +%T) sorted export"
sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 --memory 4g -v ~/t6:/w -v ~/dbtest:/d:ro -w /w dbt-tantivy:1 \
  sh -c 'set -e; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks.tsv | LC_ALL=C sort -t "	" -k1,1 -S 1200M --compress-program=zstd -T /w/tmp | cut -f2- | zstd -q -3 -T2 -o wiki_sorted.tsv.zst; zstd -dc wiki_sorted.tsv.zst | wc -l'
echo "== $(date -u +%T) SORT-DONE"
