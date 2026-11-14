#!/usr/bin/env bash
# Segment layout x collector (WAND vs tantivy-py's Count+TopDocs) on testbox; rebuilds A of main2.sh.
set -uo pipefail
cd ~/t6 || exit 1; exec >> main4.log 2>&1
say() { echo "== $(date -u +%T) $*; load $(cut -d' ' -f1-3 /proc/loadavg)"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t6:/w -v ~/dbtest:/d:ro -v /opt/logout-release/w/proto-20261002-testbox:/r:ro -w /w "$@"; }
IMG=dbt-tantivy:1; TS="./tantivy-structure"; S="--stop /d/english.stop"
B="$S --chunk 150000 --heap-mb 900 --jobs 2"
check() { [ "$1" = 0 ] || { say "FAILED ($2) exit=$1"; exit 1; }; }
say "ranks from signals.bin"
run --cpus 2 --memory 4g $IMG python pop_order.py signals /r/registry.sqlite /r/signals.bin /w/ranks-real.tsv; check $? ranks
rm -f wiki_sorted_real.tsv.zst
say "export in popularity order"
run --cpus 2 --memory 4g $IMG sh -c 'set -eo pipefail; mkdir -p tmp; python pop_order.py prefix /d/wiki_docs.tsv.zst ranks-real.tsv --doc-id | LC_ALL=C sort -t "	" -k1,1 -S 1200M --compress-program=zstd -T /w/tmp | cut -f2- | zstd -q -3 -T2 -o wiki_sorted_real.tsv.zst; test $(zstd -dc wiki_sorted_real.tsv.zst | head -1000 | wc -l) = 1000'; check $? sort
say "build A"
run --cpus 2 --memory 6g $IMG sh -c "set -o pipefail; zstd -dc /w/wiki_sorted_real.tsv.zst | $TS build /w/A $B --keep-parts"; check $? A
run $IMG sh -c 'cd /w/A && sha256sum * | cut -c1-16,65- ; cd /w && python canon.py canon /w/A >/dev/null && cd /w/A && sha256sum * .managed.json | cut -c1-16,65-'
for g in 3 6; do say "regroup A$g"; run --cpus 2 --memory 4g $IMG $TS regroup /w/A.parts /w/A$g --groups $g $S; check $? A$g; done
say "regroup A2 (from A3)"; run --cpus 2 --memory 4g $IMG $TS regroup /w/A3 /w/A2 --groups 2 $S; check $? A2
sudo -n docker run --rm -v ~/t6:/w alpine rm -rf /w/A.parts
sudo -n cp -a ~/dbtest/v1 ~/t6/v1c && sudo -n chown -R m8 ~/t6/v1c
for round in 1 2; do for mem in 2g 1g; do
  for v in v1c A A2 A3 A6; do
    case $v in v1c) ia="--index /w/v1c --id-field page_id"; py="/w/v1c /d/english.stop /d/queries.txt stored:page_id";; *) ia="--index /w/$v"; py="/w/$v /d/english.stop /d/queries.txt fast:doc_id";; esac
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG $TS bench $ia --py-like $S --queries /d/queries.txt --repeats 1 --out /w/o4.tsv); rc=$?
    echo "{\"tag\":\"rust-pylike\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o4.tsv),\"r\":${o:-null}}" >> bench4.jsonl
    l=$(cut -d' ' -f1 /proc/loadavg)
    o=$(run --cpus 2 --memory $mem $IMG python tv_store.py query $py /w/o4p.tsv 1); rc=$?
    echo "{\"tag\":\"py\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o4p.tsv),\"r\":${o:-null}}" >> bench4.jsonl
    if [ $v = A ] || [ $v = A3 ] || [ $v = A6 ]; then
      l=$(cut -d' ' -f1 /proc/loadavg)
      o=$(run --cpus 2 --memory $mem $IMG $TS bench $ia $S --queries /d/queries.txt --repeats 1 --out /w/o4.tsv); rc=$?
      echo "{\"tag\":\"rust\",\"variant\":\"$v\",\"mem\":\"$mem\",\"load1\":$l,\"rc\":$rc,\"lines\":$(wc -l < o4.tsv),\"r\":${o:-null}}" >> bench4.jsonl
    fi
  done; say "round $round $mem"
done; done
say "accuracy of A3 in the shipped path (py) vs wand"
run $IMG sh -c 'python score.py overlap /d/pg-ids.tsv /w/o4p.tsv'
say DONE4
