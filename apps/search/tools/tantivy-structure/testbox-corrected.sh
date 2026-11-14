#!/usr/bin/env bash
# Corrected Rust harness (id columns opened once per searcher): layout x collector, then hot/cold with real popularity.
set -uo pipefail
cd ~/t6 || exit 1; exec >> main5.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -v /opt/logout-release/w/proto-20261002-testbox:/r:ro -w /w "$@"; }
IMG=dbt-tantivy:1; TS="./tantivy-structure"; S="--stop /d/english.stop"
B="$S --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
b5() { # tag variant mem args...
  local tag=$1 v=$2 mem=$3; shift 3
  local l; l=$(cut -d' ' -f1 /proc/loadavg)
  local o; o=$(run --cpus 2 --memory $mem $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --out /w/o5.tsv); local rc=$?
  echo "{\"tag\":\"$tag\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o5.tsv),\"r\":${o:-null}}" >> bench5.jsonl
}
acc() { local v=$1; shift
  run --cpus 2 --memory 3g $IMG $TS bench "$@" $S --queries /w/evs/eval-queries.txt --repeats 1 --no-evict --out /w/e5-$v.tsv > /dev/null
  run --cpus 2 --memory 3g $IMG $TS bench "$@" $S --queries /d/queries.txt --repeats 1 --no-evict --out /w/o5-$v.tsv > /dev/null
  local ov h1 h2
  ov=$(run $IMG python score.py overlap /d/pg-ids.tsv /w/o5-$v.tsv /w/ranks-real.tsv)
  h1=$(run $IMG python score.py hits /w/e5-$v.tsv /w/evs/eval-wiki-redirect.tsv doc)
  h2=$(run $IMG python score.py hits /w/e5-$v.tsv /w/evs/eval-typo-wiki.tsv doc)
  echo "{\"variant\":\"$v\",\"pg\":$ov,\"redirect\":$h1,\"typo\":$h2}" >> acc5.jsonl; say "acc $v"
}
say "1: layout x collector"
for round in 1 2; do for mem in 2g 1g; do
  for v in A A2 A3 A6; do b5 wand $v $mem --index /w/$v; b5 pylike $v $mem --index /w/$v --py-like; done
  b5 wand v1c $mem --index /w/v1c --id-field page_id; b5 pylike v1c $mem --index /w/v1c --id-field page_id --py-like
  say "layout round $round $mem"
done; done
say "2: search-only reference (no id readback), 2g warm"
for v in A A3 A6; do b5 wand-noids $v 2g --index /w/$v --no-ids; b5 pylike-noids $v 2g --index /w/$v --py-like --no-ids; done
say "3: build hot/cold H/K (real pop, doc_id), 10% hot"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted_real.tsv.zst | $TS build /w/H --cold-out /w/K --split-at 679041 $B"; check $? HK
for d in H K; do run $IMG python canon.py canon /w/$d > /dev/null; done
run $IMG sh -c "for d in H K; do echo \$d; $TS space --index /w/\$d $S; done; du -sb /w/H /w/K" 
say "4: hot/cold speed"
for round in 1 2; do for mem in 2g 1g; do
  b5 wand A $mem --index /w/A
  b5 hot-only H $mem --index /w/H --cold /w/K --mode hotcold --rule hot
  b5 hc-always H $mem --index /w/H --cold /w/K --mode hotcold --rule always --stats global
  b5 hc-alpha0.9 H $mem --index /w/H --cold /w/K --mode hotcold --rule alpha:0.9 --stats global
  b5 hc-local H $mem --index /w/H --cold /w/K --mode hotcold --rule always --stats local
  say "hotcold round $round $mem"
done; done
say "5: hot/cold dumps + accuracy"
for st in global local; do run --cpus 2 --memory 4g $IMG $TS bench --index /w/H --cold /w/K --mode hotcold --rule hot --stats $st $S --queries /d/queries.txt --repeats 1 --no-evict --out /w/d5-$st.tsv --dump /w/d5-$st.jsonl > /dev/null; done
acc A --index /w/A
acc hot-only --index /w/H --cold /w/K --mode hotcold --rule hot
acc hc-always --index /w/H --cold /w/K --mode hotcold --rule always --stats global
acc hc-alpha0.9 --index /w/H --cold /w/K --mode hotcold --rule alpha:0.9 --stats global
say DONE5
