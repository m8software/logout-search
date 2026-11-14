#!/usr/bin/env bash
# Rule 5 properly: popularity order vs input order at one segment, corrected harness; A rebuilt (determinism across runs).
set -uo pipefail
cd ~/t6 || exit 1; exec >> main6.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -v /opt/logout-release/w/proto-20261002-testbox:/r:ro -w /w "$@"; }
IMG=dbt-tantivy:1; TS="./tantivy-structure"; S="--stop /d/english.stop"
B="$S --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
mkdir -p tmp
run --cpus 2 --memory 4g $IMG python pop_order.py signals /r/registry.sqlite /r/signals.bin /w/ranks-real.tsv; check $? ranks
say "exports"
run --cpus 2 --memory 4g $IMG sh -c 'set -eo pipefail; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | LC_ALL=C sort -t "	" -k1,1 -S 1200M --compress-program=zstd -T /w/tmp | cut -f2- | zstd -q -3 -T2 -o wiki_sorted_real.tsv.zst'; check $? sort
run --cpus 2 --memory 4g $IMG sh -c 'set -eo pipefail; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | cut -f2- | zstd -q -3 -T2 -o wiki_unsorted_doc.tsv.zst'; check $? unsorted
say "build A (popularity order)"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted_real.tsv.zst | $TS build /w/A $B"; check $? A
run $IMG sh -c 'python canon.py canon /w/A >/dev/null && cd /w/A && sha256sum * .managed.json | cut -c1-16,65-'
say "build C (input order)"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_unsorted_doc.tsv.zst | $TS build /w/C $B"; check $? C
run $IMG python canon.py canon /w/C > /dev/null
run $IMG du -sb /w/A /w/C
b6() { local tag=$1 v=$2 mem=$3; shift 3
  local l; l=$(cut -d' ' -f1 /proc/loadavg)
  local o; o=$(run --cpus 2 --memory $mem $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --out /w/o6.tsv); local rc=$?
  echo "{\"tag\":\"$tag\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o6.tsv),\"r\":${o:-null}}" >> bench6.jsonl; }
for round in 1 2 3; do for mem in 2g 1g; do
  for v in A C; do
    b6 wand $v $mem --index /w/$v; b6 pylike $v $mem --index /w/$v --py-like
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG python tv_store.py query /w/$v /d/english.stop /d/queries.txt fast:doc_id /w/o6p.tsv 1); rc=$?
    echo "{\"tag\":\"py\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o6p.tsv),\"r\":${o:-null}}" >> bench6.jsonl
  done; say "round $round $mem"
done; done
for v in A C; do run --cpus 2 --memory 3g $IMG $TS bench --index /w/$v $S --queries /d/queries.txt --repeats 1 --no-evict --out /w/p6-$v.tsv >/dev/null; echo "$v $(run $IMG python score.py overlap /d/pg-ids.tsv /w/p6-$v.tsv /w/ranks-real.tsv)"; done
say DONE6
