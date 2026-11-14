#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): it samples testbox's Postgres for a finished spike. Delete at phase 7.
# T3 usearch spike on testbox (docs/internal/audits/2026-10-01-usearch-spike.md). Run detached:
#   nohup bash ~/t3/usearch-spike-testbox.sh nomic|bge >> ~/t3/SPACE.log 2>&1 &
# Throwaway containers only (label dbtest), 2 cores; search runs under --memory 2g.
# The live search stack is only read (COPY / SELECT, done before this script).
set -euo pipefail
SPACE=${1:?nomic or bge}
T=$HOME/t3
D=/w/$SPACE
IMG=t3-usearch:1
say() { echo "== $(date -u +%T) $*"; }
run() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$T":/w -w /w "$@"; }
# zstd lives in the earlier Tantivy spike image, not on the host
zst() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 -v "$T":/w dbt-tantivy:1 zstd -19 -T2 -c "/w/${1#"$T"/}" | wc -c; }
py() { local mem=$1; shift; run --memory "$mem" "$IMG" python usearch_spike.py "$@"; }

# pgvector reference (read-only on the live, 2 GB-capped Postgres): top-10 ids, then the
# served sem SQL and an id-only variant, each timed twice (\timing).
if [ "${2:-}" = pg ]; then
  cd "$T/$SPACE"
  P=(sudo -n docker exec -i search-serve-postgres psql -U search -d search -qAt)
  "${P[@]}" < pg_ids.sql | grep -v '^SET' > pg_ids.tsv
  say "pg ids: $(wc -l < pg_ids.tsv) queries"
  for f in served ids; do
    for p in 1 2; do
      "${P[@]}" < "pg_time_$f.sql" 2>&1 | grep -o 'Time: [0-9.]*' | awk '{print $2}' > "pgt_${f}_p$p.txt"
      say "pg $f pass $p: n=$(wc -l < "pgt_${f}_p$p.txt") $(sort -n "pgt_${f}_p$p.txt" | awk '{a[NR]=$1} END{print "p50=" a[int(NR*0.5)+1], "p90=" a[int(NR*0.9)+1], "p99=" a[int(NR*0.99)+1]}')"
    done
  done
  exit 0
fi

# Second pass (after the first run): the wider expansion sweep at 2g, then tighter caps (the
# engine shares its memory with the query encoder and Tantivy), full load at 3g, recall.
if [ "${2:-}" = sweep ]; then
  K=${3:?index kind}
  say "load: $(uptime)"
  py 2g bench "$D" "$D/idx_$K.usearch" view "${K}_view" --exp 40,64,128,256,512,1024,2048 --passes 2
  # Below the index size the mmap thrashes (2026-10-01: 1g ran 16 min / 304 GB of reads without
  # finishing 60 queries), so tighter caps get a 20-minute timeout and fewer queries.
  say "load: $(uptime)"
  run --memory 1536m "$IMG" timeout 1200 python usearch_spike.py bench "$D" "$D/idx_$K.usearch" view "${K}_v1536m" \
    --exp 256 --passes 2 --limit 60 || say "view 1536m FAILED rc=$?"
  say "load: $(uptime)"
  run --memory 1g "$IMG" timeout 1200 python usearch_spike.py bench "$D" "$D/idx_$K.usearch" view "${K}_v1g" \
    --exp 256 --passes 1 --limit 5 || say "view 1g FAILED rc=$?"
  py 3g bench "$D" "$D/idx_$K.usearch" load "${K}_load3g" --exp 256,1024 --passes 2 || say "load 3g FAILED rc=$?"
  tags=""
  for e in 40 64 128 256 512 1024 2048; do tags="$tags ${K}_view_e${e}_p2"; done
  py 4g recall "$D" sweep $tags "${K}_v1536m_e256_p2"
  say "SWEEP DONE"
  exit 0
fi

if [ "$SPACE" = nomic ]; then KINDS=${KINDS:-b1}; else KINDS=${KINDS:-i8 f16}; fi
# QMEM: cap for the full-set (recall) benches. bge indexes are larger than 2 GB, so their recall
# runs at 6g and the 2 GB speed run uses a prefix of the queries (it thrashes: see the audit).
if [ "$SPACE" = nomic ]; then QMEM=${QMEM:-2g}; else QMEM=${QMEM:-6g}; fi

if [ "${MAIN:-1}" = 1 ]; then
say "exact truth"
[ -f "$T/$SPACE/exact_ids.npy" ] || py 4g exact "$D"

for k in $KINDS; do
  say "build $k"
  [ -f "$T/$SPACE/idx_$k.usearch" ] || py 9g build "$D" "$k"
  say "bench $k view (mmap) under $QMEM; load: $(uptime)"
  py "$QMEM" bench "$D" "$D/idx_$k.usearch" view "${k}_view" --exp 40,64,128,256,512 --passes 2 || say "bench $k view FAILED rc=$?"
  if [ "$QMEM" != 2g ]; then
    say "bench $k view (mmap) under 2g, 30 queries; load: $(uptime)"
    run --memory 2g "$IMG" timeout 1200 python usearch_spike.py bench "$D" "$D/idx_$k.usearch" view "${k}_v2g" --exp 40,128 --passes 2 --limit 30 \
      || say "bench $k view 2g FAILED rc=$? (124 = 20 min timeout)"
  fi
  say "bench $k load under 2g"
  py 2g bench "$D" "$D/idx_$k.usearch" load "${k}_load" --exp 40,64,128,256 --passes 2 || say "bench $k load FAILED rc=$? (137 = OOM-killed)"
done

if [ "$SPACE" = nomic ] && [ "${FLAT:-0}" = 1 ]; then   # 1.17 s/query on testbox (2026-10-01): off by default
  say "flat exact scan under 2g (1 thread, then 2)"
  py 2g flat "$D" flat_t1 --passes 3 --threads 1 || say "flat t1 FAILED rc=$?"
  py 2g flat "$D" flat_t2 --passes 2 --threads 2 || say "flat t2 FAILED rc=$?"
fi

say "recall"
tags=""
for k in $KINDS; do
  for e in 40 64 128 256 512; do tags="$tags ${k}_view_e${e}_p2"; done
done
[ -f "$T/$SPACE/res_flat_t1_p2.npy" ] && tags="$tags flat_t1_p2"
py 4g recall "$D" main $tags

fi   # MAIN

K=${KINDS%% *}
for pct in ${MONTHS:-2 10}; do
  say "month $pct%: delta index ($K)"
  py 4g delta "$D" "$pct" "$pct" --kind "$K"
  py 4g exact_delta "$D" "$pct"
  say "month $pct%: search main (mmap) + delta under $QMEM"
  py "$QMEM" bench "$D" "$D/idx_$K.usearch" view "m${pct}_${K}" --exp 64,128,256 --passes 2 \
    --delta "$D/delta_$pct/delta_$K.usearch" --tomb "$D/delta_$pct/tombstones.npy"
  say "month $pct%: in-place remove+add"
  py 9g inplace "$D" "$pct" --kind "$K" || say "inplace FAILED rc=$?"
  py "$QMEM" bench "$D" "$D/delta_$pct/inplace_$K.usearch" view "ip${pct}_${K}" --exp 64,128,256 --passes 2 || true
  py 4g recall "$D" "m$pct" "m${pct}_${K}_e64_p2" "m${pct}_${K}_e128_p2" "m${pct}_${K}_e256_p2" \
    "ip${pct}_${K}_e64_p2" "ip${pct}_${K}_e128_p2" "ip${pct}_${K}_e256_p2" \
    --exact-ids "$D/delta_$pct/exact_ids.npy" --exact-sim "$D/delta_$pct/exact_sim.npy" --delta-dir "$D/delta_$pct"
  say "month $pct%: download sizes (zstd -19)"
  for f in "delta_$K.usearch" tombstones.u32; do
    printf '%s raw=%s zstd19=%s\n' "$f" "$(stat -c %s "$T/$SPACE/delta_$pct/$f")" "$(zst "$T/$SPACE/delta_$pct/$f")"
  done
  if [ "${RSYNC:-1}" = 1 ] && [ -f "$T/$SPACE/delta_$pct/inplace_$K.usearch" ]; then
    rm -f "$T/$SPACE/delta_$pct/rsync.batch"*
    mkdir -p "$T/$SPACE/delta_$pct/rs" && cp "$T/$SPACE/idx_$K.usearch" "$T/$SPACE/delta_$pct/rs/idx.usearch"
    rsync --no-whole-file --only-write-batch="$T/$SPACE/delta_$pct/rsync.batch" \
      "$T/$SPACE/delta_$pct/inplace_$K.usearch" "$T/$SPACE/delta_$pct/rs/idx.usearch" >/dev/null
    printf 'in-place rsync batch=%s zstd19=%s\n' "$(stat -c %s "$T/$SPACE/delta_$pct/rsync.batch")" \
      "$(zst "$T/$SPACE/delta_$pct/rsync.batch")"
    rm -rf "$T/$SPACE/delta_$pct/rs" "$T/$SPACE/delta_$pct/rsync.batch"*
  fi
  rm -f "$T/$SPACE/delta_$pct/inplace_$K.usearch"
done
printf 'main index %s raw=%s\n' "idx_$K.usearch" "$(stat -c %s "$T/$SPACE/idx_$K.usearch")"
say "DONE $SPACE"
