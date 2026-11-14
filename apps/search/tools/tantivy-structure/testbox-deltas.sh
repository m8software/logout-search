#!/usr/bin/env bash
# canon.py == T5 tv_normalize (+payload) at 300k; then the monthly-delta layout cost at 1 GB (one base segment + 1 and 2 month deltas).
# No `set -e`: each step is checked with `check` so a failure is logged with its name before exiting.
set -uo pipefail
cd ~/t6 || exit 1; exec >> main7.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -v /opt/logout-release/w/proto-20261002-testbox:/r:ro -w /w "$@"; }
IMG=dbt-tantivy:1; TS="./tantivy-structure"; S="--stop /d/english.stop"
B="$S --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
mkdir -p tmp
run --cpus 2 --memory 4g $IMG python pop_order.py signals /r/registry.sqlite /r/signals.bin /w/ranks-real.tsv; check $? ranks
say "exports (input order, registry doc_id)"
run --cpus 2 --memory 4g $IMG sh -c 'set -eo pipefail; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | cut -f2- | zstd -q -3 -T2 -o wiki_unsorted_doc.tsv.zst'; check $? export
# head closes the pipe early (SIGPIPE, exit 141 under pipefail), so this one runs without pipefail and the count is checked
run --cpus 2 --memory 4g $IMG sh -c 'zstd -dc wiki_unsorted_doc.tsv.zst | head -300000 | zstd -q -o sub300k.tsv.zst; test $(zstd -dc sub300k.tsv.zst | wc -l) = 300000'; check $? subset
say "alignment test at 300k"
for k in a b; do run --cpus 2 --memory 3g $IMG sh -c "set -o pipefail; zstd -dc /w/sub300k.tsv.zst | $TS build /w/s-$k $S --chunk 50000 --heap-mb 400 --jobs 2 > /dev/null"; check $? s-$k; done
run $IMG sh -c 'cp -a /w/s-a /w/s-a2 && cp -a /w/s-a /w/s-a3 && python canon.py canon /w/s-a > /dev/null && python canon.py strip /w/s-a2 && python canon.py ref /w/s-a2 && echo "canon(a) vs strip+T5ref(a):" && python tv_store.py same /w/s-a /w/s-a2 | grep -E "identical|only_in" && python canon.py canon /w/s-b >/dev/null && echo "canon(a) vs canon(b):" && python tv_store.py same /w/s-a /w/s-b | grep -E "identical|only_in"; rm -rf /w/s-a2 /w/s-a3'
say "build U (one segment, input order)"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_unsorted_doc.tsv.zst | $TS build /w/U $B"; check $? U
run $IMG python canon.py canon /w/U > /dev/null
say "months"
run $IMG sh -c 'cp -a /w/U /w/U1'
run --cpus 2 --memory 6g $IMG python tv_store.py month /w/U1 /d/english.stop /d/wiki_docs.tsv.zst 50 0 g1walmarktwo; check $? m1
run $IMG sh -c 'cp -a /w/U1 /w/U2'
run --cpus 2 --memory 6g $IMG python tv_store.py month /w/U2 /d/english.stop /d/wiki_docs.tsv.zst 50 1 g1walmarkthree; check $? m2
run $IMG sh -c 'cd /d && python dbt_tantivy.py delta /w/U /w/U1; python dbt_tantivy.py delta /w/U1 /w/U2; du -sb /w/U /w/U1 /w/U2'
b7() { local tag=$1 v=$2 mem=$3; shift 3
  local l; l=$(cut -d' ' -f1 /proc/loadavg)
  local o; o=$(run --cpus 2 --memory $mem $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --out /w/o7.tsv); local rc=$?
  echo "{\"tag\":\"$tag\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o7.tsv),\"r\":${o:-null}}" >> bench7.jsonl; }
for round in 1 2; do for mem in 2g 1g; do
  for v in U U1 U2; do
    b7 wand $v $mem --index /w/$v
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG python tv_store.py query /w/$v /d/english.stop /d/queries.txt fast:doc_id /w/o7p.tsv 1); rc=$?
    echo "{\"tag\":\"py\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o7p.tsv),\"r\":${o:-null}}" >> bench7.jsonl
  done; say "round $round $mem"
done; done
say DONE7
