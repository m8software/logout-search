#!/usr/bin/env bash
# Tantivy structure (release format rules 3, 5, 7) on testbox: builds, interleaved bench, months.
set -uo pipefail
cd ~/t6 || exit 1; exec >> main.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -w /w "$@"; }
IMG=dbt-tantivy:1
B="--stop /d/english.stop --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
PHASE=${1:-all}

if [ "$PHASE" = all ] || [ "$PHASE" = build ]; then
say "BUILD r1: python, store nothing, unsorted (Phase-1 code, 2 threads)"
run --cpus 2 --memory 6g $IMG python tv_store.py build /d/wiki_docs.tsv.zst /w/r1 /d/english.stop 2; check $? r1
say "BUILD r2u: rust, store nothing, unsorted"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /d/wiki_docs.tsv.zst | ./tantivy-structure build /w/r2u $B"; check $? r2u
for k in a b; do
  say "BUILD r2$k: rust, store nothing, popularity order"
  run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted.tsv.zst | ./tantivy-structure build /w/r2$k $B"; check $? r2$k
done
say "SAME r2a r2b raw"; run $IMG python tv_store.py same /w/r2a /w/r2b
say "CANON"; for d in r2u r2a r2b; do run $IMG python canon.py canon /w/$d; done
say "SAME r2a r2b after canon"; run $IMG python tv_store.py same /w/r2a /w/r2b
run $IMG sh -c 'cd /w/r2a && sha256sum * .managed.json'
run $IMG sh -c 'rm -rf /w/r2b'
say "BUILD hot/cold: rust, popularity order, top 679,041 (10%) = hot"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted.tsv.zst | ./tantivy-structure build /w/hot --cold-out /w/cold --split-at 679041 $B"; check $? hotcold
for d in hot cold; do run $IMG python canon.py canon /w/$d; done
say "SPACE"; for d in /d/v1 /w/r1 /w/r2u /w/r2a /w/hot /w/cold; do run $IMG sh -c "echo $d; ./tantivy-structure space --index $d --stop /d/english.stop"; done
say "DFS gate r1 vs r2a"
run $IMG sh -c './tantivy-structure dfs --index /w/r1 --stop /d/english.stop --queries /d/queries.txt > dfs-r1.tsv; ./tantivy-structure dfs --index /w/r2a --stop /d/english.stop --queries /d/queries.txt > dfs-r2a.tsv; wc -l dfs-r1.tsv; diff dfs-r1.tsv dfs-r2a.tsv | head -20; echo diff_lines=$(diff dfs-r1.tsv dfs-r2a.tsv | grep -c "^<")'
say "BUILD-DONE"
fi

if [ "$PHASE" = all ] || [ "$PHASE" = bench ]; then
Q="--stop /d/english.stop --queries /d/queries.txt --repeats 1"
for round in 1 2 3; do for mem in 2g 1g; do
  for v in v1 r1 r2u r2a hot hcg hcl hca1; do
    case $v in
      v1) a="--index /d/v1 --id-field page_id";; r1|r2u|r2a) a="--index /w/$v";;
      hot) a="--index /w/hot --cold /w/cold --mode hotcold --rule hot";;
      hcg) a="--index /w/hot --cold /w/cold --mode hotcold --rule always --stats global";;
      hcl) a="--index /w/hot --cold /w/cold --mode hotcold --rule always --stats local";;
      hca1) a="--index /w/hot --cold /w/cold --mode hotcold --rule alpha:1 --stats global";;
    esac
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG ./tantivy-structure bench $a $Q --out /w/out-$v.tsv); rc=$?
    echo "{\"tag\":\"rust\",\"variant\":\"$v\",\"mem\":\"$mem\",\"round\":$round,\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < out-$v.tsv),\"r\":${o:-null}}" >> bench.jsonl
  done
  for v in v1 r1 r2a; do
    case $v in v1) a="/d/v1 /d/english.stop /d/queries.txt stored:page_id";; *) a="/w/$v /d/english.stop /d/queries.txt fast:doc_id";; esac
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG python tv_store.py query $a /w/py-$v.tsv 1); rc=$?
    echo "{\"tag\":\"py\",\"variant\":\"$v\",\"mem\":\"$mem\",\"round\":$round,\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < py-$v.tsv),\"r\":${o:-null}}" >> bench.jsonl
  done
  say "bench round $round mem $mem done"
done; done
say "DUMPS (untimed)"
for st in global local; do run --cpus 2 --memory 4g $IMG ./tantivy-structure bench --index /w/hot --cold /w/cold --mode hotcold --rule hot --stats $st $Q --no-evict --out /w/dump-$st.tsv --dump /w/dump-$st.jsonl > /dev/null; echo "dump $st rc=$? $(wc -l < dump-$st.jsonl)"; done
say "OVERLAP vs Postgres"
for f in out-*.tsv py-*.tsv; do echo "$f $(run $IMG sh -c "cd /d && python dbt_tv2.py overlap /d/pg-ids.tsv /w/$f")"; done
say "BENCH-DONE"
fi

if [ "$PHASE" = all ] || [ "$PHASE" = months ]; then
say "MONTH 2% r1 (python index, build side 6 GB)"
run $IMG sh -c 'rm -rf /w/r1m && cp -a /w/r1 /w/r1m'
run --cpus 2 --memory 6g $IMG python tv_store.py month /w/r1m /d/english.stop /d/wiki_docs.tsv.zst 50 0 g1walmarktwo; check $? month-r1
run $IMG sh -c 'cd /d && python dbt_tantivy.py delta /w/r1 /w/r1m'; run $IMG rm -rf /w/r1m
say "MONTH 2% r2a (rust, popularity order)"
run $IMG sh -c 'rm -rf /w/r2m && cp -a /w/r2a /w/r2m'
run --cpus 2 --memory 6g $IMG python tv_store.py month /w/r2m /d/english.stop /d/wiki_docs.tsv.zst 50 0 g1walmarktwo; check $? month-r2
run $IMG sh -c 'cd /d && python dbt_tantivy.py delta /w/r2a /w/r2m'
say "QUERY r2a after the month (2g)"
run --cpus 2 --memory 2g $IMG ./tantivy-structure bench --index /w/r2m --stop /d/english.stop --queries /d/queries.txt --repeats 3 --out /w/out-r2m.tsv | cut -c1-400
run $IMG sh -c "cd /d && python dbt_tv2.py overlap /d/pg-ids.tsv /w/out-r2m.tsv"; run $IMG rm -rf /w/r2m
say "MONTH 2% hot/cold (routed by rank)"
run $IMG sh -c 'rm -rf /w/hotm /w/coldm && cp -a /w/hot /w/hotm && cp -a /w/cold /w/coldm'
run --cpus 2 --memory 6g $IMG python tv_store.py month-split /w/hotm /w/coldm /d/english.stop /d/wiki_docs.tsv.zst 50 0 g1walmarktwo /w/ranks.tsv 679041; check $? month-hc
run $IMG sh -c 'cd /d && python dbt_tantivy.py delta /w/hot /w/hotm; python dbt_tantivy.py delta /w/cold /w/coldm'; run $IMG rm -rf /w/hotm /w/coldm
say "MONTHS-DONE"
fi
