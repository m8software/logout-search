#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): it samples testbox's Postgres for a finished spike. Delete at phase 7.
# T3b on testbox: low-memory meaning search over the nomic 1-bit codes already in ~/t3/nomic
# (no new reads of the live Postgres). Run detached:
#   nohup bash ~/t3/bitivf-testbox.sh > ~/t3/t3b.log 2>&1 &
# Throwaway containers (label dbtest), 2 cores, named t3b-* so they can be removed by name.
set -uo pipefail
T=$HOME/t3
D=/w/nomic
IMG=t3b:1
say() { echo "== $(date -u +%T) $*"; }
py() {   # py NAME MEM args...
  local name=$1 mem=$2; shift 2
  sudo -n docker run --rm --name "t3b-$name" --label "dbtest=safe to remove" --cpus "${CPUS:-2}" --memory "$mem" \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -e NUMBA_CACHE_DIR=/tmp -v "$T":/w -w /w "$IMG" python bitivf_spike.py "$@"
  local rc=$?; [ $rc -eq 0 ] || say "FAILED rc=$rc ($name, $mem; 137 = OOM-killed)"; return 0
}
zst() { sudo -n docker run --rm --label "dbtest=safe to remove" --cpus 2 -v "$T":/w dbt-tantivy:1 zstd -19 -T2 -c "/w/${1#"$T"/}" | wc -c; }

sudo -n docker image inspect "$IMG" >/dev/null 2>&1 || {
  printf 'FROM t3-usearch:1\nRUN pip install -q numba==0.68.0 faiss-cpu==1.15.1\n' |
    sudo -n docker build -q --label "dbtest=safe to remove" -t "$IMG" -
}

# 4-core phase (the target box has 4 cores): only at a quiet moment, since testbox is shared.
# Waits up to 2 h for a 1-minute load average under QUIET (default 1.0), then runs at --cpus 4.
if [ "${1:-}" = t4 ]; then
  for _ in $(seq 240); do
    awk -v q="${QUIET:-1.0}" '{exit !($1 < q)}' /proc/loadavg && break
    sleep 30
  done
  export CPUS=4
  mem=1536m
  say "4-core flat under $mem; load: $(cut -d' ' -f1-3 /proc/loadavg)"
  py flat4-$mem $mem flat "$D" "flat4c_$mem" --threads 1,2,4
  for L in 2600 8192; do
    for nt in 1 2 4; do
      say "4-core ivf$L threads $nt under $mem; load: $(cut -d' ' -f1-3 /proc/loadavg)"
      py "ivf$L-t$nt" $mem ivf "$D" "$L" "ivf${L}_t${nt}_$mem" --probes 100,200,400 --threads "$nt"
    done
  done
  say "4-core phase done; load: $(cut -d' ' -f1-3 /proc/loadavg)"
  tags=""
  for nt in 1 2 4; do
    tags="$tags flat4c_${mem}_t${nt}_s2"
    for L in 2600 8192; do for p in 100 200 400; do tags="$tags ivf${L}_t${nt}_${mem}_p${p}_s2"; done; done
  done
  CPUS=2 sudo -n docker run --rm --name t3b-recall4 --label "dbtest=safe to remove" --cpus 2 --memory 4g \
    --user "$(id -u):$(id -g)" -v "$T":/w -w /w "$IMG" python usearch_spike.py recall "$D" t3b_t4 $tags
  say "T4 DONE"
  exit 0
fi

for L in 2600 8192; do
  say "build ivf$L"
  [ -f "$T/nomic/ivf$L/build.json" ] || py "build$L" 4g build "$D" "$L"
done

for mem in 2g 1536m 1g; do
  for L in 2600 8192; do
    say "ivf$L under $mem; load: $(uptime)"
    py "ivf$L-$mem" "$mem" ivf "$D" "$L" "ivf${L}_$mem"
  done
  say "flat (numba) under $mem; load: $(uptime)"
  py "flat-$mem" "$mem" flat "$D" "flat_$mem" --threads 1,2
  say "faiss flat under $mem; load: $(uptime)"
  py "faiss-$mem" "$mem" faissflat "$D" "faiss_$mem" --threads 1,2 --limit 100
done

say "real-box mimic: nomic CPU encoder in the same process, 3g; load: $(uptime)"
py enc-ivf 3g ivf "$D" 8192 "enc_ivf8192_3g" --probes 100,200 --encoder /w/search_api.py
py enc-flat 3g flat "$D" "enc_flat_3g" --threads 2 --encoder /w/search_api.py

for pct in 2 10; do
  say "month $pct% for ivf8192"
  py "delta$pct" 2g delta "$D" 8192 "$pct"
  for f in codes.u8 ids.i64 offsets.npy tombstones.u32; do
    printf 'delta %s%% %s raw=%s zstd19=%s\n' "$pct" "$f" "$(stat -c %s "$T/nomic/ivf8192/delta_$pct/$f")" "$(zst "$T/nomic/ivf8192/delta_$pct/$f")"
  done
  for mem in 1g 2g; do
    py "m$pct-$mem" "$mem" ivf "$D" 8192 "m${pct}_ivf8192_$mem" --probes 100,200,400 --delta "$pct"
  done
done

say "recall"
tags=""
for mem in 2g 1536m 1g; do
  for L in 2600 8192; do for p in 25 50 100 200 400; do tags="$tags ivf${L}_${mem}_p${p}_s2"; done; done
  tags="$tags flat_${mem}_t1_s2 flat_${mem}_t2_s2"
done
tags="$tags faiss_2g_t1_s2 enc_ivf8192_3g_p100_s2 enc_ivf8192_3g_p200_s2 enc_flat_3g_t2_s2"
existing=""
for t in $tags; do [ -f "$T/nomic/res_$t.npy" ] && existing="$existing $t"; done
sudo -n docker run --rm --name t3b-recall --label "dbtest=safe to remove" --cpus 2 --memory 4g --user "$(id -u):$(id -g)" \
  -v "$T":/w -w /w "$IMG" python usearch_spike.py recall "$D" t3b $existing
for pct in 2 10; do
  mt=""
  for mem in 1g 2g; do for p in 100 200 400; do mt="$mt m${pct}_ivf8192_${mem}_p${p}_s2"; done; done
  sudo -n docker run --rm --name "t3b-recall$pct" --label "dbtest=safe to remove" --cpus 2 --memory 4g --user "$(id -u):$(id -g)" \
    -v "$T":/w -w /w "$IMG" python usearch_spike.py recall "$D" "t3b_m$pct" $mt \
    --exact-ids "$D/delta_$pct/exact_ids.npy" --exact-sim "$D/delta_$pct/exact_sim.npy" --delta-dir "$D/delta_$pct"
done
say "T3B DONE"
