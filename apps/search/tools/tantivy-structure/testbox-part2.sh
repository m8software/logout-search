#!/usr/bin/env bash
# Tantivy structure, part 2 (testbox): accuracy on the open sets, segment count, en_pg2, real popularity.
set -uo pipefail
cd ~/t6 || exit 1; exec >> main2.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -v /opt/logout-release/w/proto-20261002-testbox:/r:ro -w /w "$@"; }
IMG=dbt-tantivy:1
TS="./tantivy-structure"
S="--stop /d/english.stop"
B="$S --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
bench1() { # tag variant mem args...
  local tag=$1 v=$2 mem=$3; shift 3
  local l; l=$(cut -d' ' -f1 /proc/loadavg)
  local o; o=$(run --cpus 2 --memory $mem $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --out /w/o2-$v.tsv); local rc=$?
  echo "{\"tag\":\"$tag\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o2-$v.tsv 2>/dev/null || echo 0),\"r\":${o:-null}}" >> bench2.jsonl
}
acc() { # variant col args... : accuracy on the open sets + overlap vs Postgres
  local v=$1 col=$2; shift 2
  run --cpus 2 --memory 3g $IMG $TS bench "$@" $S --queries /w/evs/eval-queries.txt --repeats 1 --no-evict --out /w/e2-$v.tsv > /dev/null; local rc=$?
  run --cpus 2 --memory 3g $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --no-evict --out /w/o2-$v.tsv > /dev/null
  local ov; ov=$(run $IMG sh -c "python score.py overlap /d/pg-ids.tsv /w/o2-$v.tsv $([ $col = doc ] && echo /w/ranks-real.tsv)")
  local h1; h1=$(run $IMG python score.py hits /w/e2-$v.tsv /w/evs/eval-wiki-redirect.tsv $col)
  local h2; h2=$(run $IMG python score.py hits /w/e2-$v.tsv /w/evs/eval-typo-wiki.tsv $col)
  echo "{\"variant\":\"$v\",\"rc\":$rc,\"pg\":$ov,\"redirect\":$h1,\"typo\":$h2}" >> acc2.jsonl
  say "acc $v done"
}

say "A: accuracy of the existing page_id indexes on the open sets"
acc r1 page --index /w/r1
acc r2a page --index /w/r2a
acc hot-only page --index /w/hot --cold /w/cold --mode hotcold --rule hot
acc hc-always page --index /w/hot --cold /w/cold --mode hotcold --rule always --stats global
acc hc-alpha0.9 page --index /w/hot --cold /w/cold --mode hotcold --rule alpha:0.9 --stats global
acc hc-local page --index /w/hot --cold /w/cold --mode hotcold --rule always --stats local
sudo -n docker run --rm -v ~/t6:/w alpine sh -c 'rm -rf /w/r2u /w/r1 /w/r2a /w/x.tsv'
for v in hot cold; do :; done

say "B0: sorted + unsorted exports with the registry doc_id"
run --cpus 2 --memory 4g $IMG sh -c 'set -e; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | LC_ALL=C sort -t "	" -k1,1 -S 1200M --compress-program=zstd -T /w/tmp | cut -f2- | zstd -q -3 -T2 -o wiki_sorted_real.tsv.zst'; check $? sort
run --cpus 2 --memory 4g $IMG sh -c 'set -e; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | cut -f2- | zstd -q -3 -T2 -o wiki_unsorted_doc.tsv.zst'; check $? unsorted
run $IMG sh -c 'zstd -dc /w/wiki_sorted_real.tsv.zst | head -3 | cut -f1,2; zstd -dc /w/wiki_unsorted_doc.tsv.zst | head -2 | cut -f1,2'

say "B1: A = en_pg, popularity order (real signals), registry doc_id, parts kept"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted_real.tsv.zst | $TS build /w/A $B --keep-parts"; check $? A
say "B2: B = en_pg2, same order"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted_real.tsv.zst | $TS build /w/B $B --analyzer en_pg2"; check $? B
say "B3: C = en_pg, input order (no popularity sort), parts kept"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_unsorted_doc.tsv.zst | $TS build /w/C $B --keep-parts"; check $? C
for g in 46 12 6 3; do say "regroup A -> A$g"; run --cpus 2 --memory 4g $IMG $TS regroup /w/A.parts /w/A$g --groups $g $S; check $? A$g; done
say "regroup C -> C6"; run --cpus 2 --memory 4g $IMG $TS regroup /w/C.parts /w/C6 --groups 6 $S; check $? C6
sudo -n docker run --rm -v ~/t6:/w alpine sh -c 'rm -rf /w/A.parts /w/C.parts'
say "SPACE"; for d in A B C6; do run $IMG sh -c "echo $d; $TS space --index /w/$d $S $([ $d = B ] && echo --analyzer en_pg2)"; done; du -sb A B A3 A6 A12 A46 C6 v1c | tr '\t' ' '

say "C: accuracy of the new indexes (doc_id)"
acc A doc --index /w/A
acc B doc --index /w/B --analyzer en_pg2
acc A6 doc --index /w/A6
acc C6 doc --index /w/C6

say "D: speed, interleaved, 3 rounds x 2 caps"
for round in 1 2 3; do for mem in 2g 1g; do
  bench1 rust v1c $mem --index /w/v1c --id-field page_id
  bench1 rust A $mem --index /w/A
  bench1 rust B $mem --index /w/B --analyzer en_pg2
  for g in 3 6 12 46; do bench1 rust A$g $mem --index /w/A$g; done
  bench1 rust C6 $mem --index /w/C6
  say "bench round $round $mem"
done; done
say "DONE2"
