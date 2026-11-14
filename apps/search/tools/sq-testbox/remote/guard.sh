#!/usr/bin/env bash
# The guard of the parity run, for one sweep engine: a row every 30 s to mon.<name>.csv; the engine is removed at
# once (and guard.<name>.txt written) on the first failure of (a) serving search-api healthy with the same id and
# start time, (b) a real search on it answering 200 in under 3x the median measured before the engine started,
# (c) available memory above 4 GB, (d) load above 12 for three minutes. Ends when the engine is gone or after 4 h.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
c=$1; out=mon.$c.csv; hi=0; end=$(( $(date +%s) + 14400 ))
med=$(cat "baseline.$c.median"); want=$(cat "serving.$c.id"); lim=$(awk -v m="$med" 'BEGIN{printf "%.4f", 3*m}')
echo "time,load1,avail_mb,swap_used_mb,serving_health,probe_code,probe_s,probe_limit_s" > "$out"
while [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" == true ]] && (( $(date +%s) < end )); do
    read -r l1 _ < /proc/loadavg
    avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    sw=$(awk '/^SwapTotal:/{t=$2}/^SwapFree:/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)
    sh=$(docker inspect -f '{{.State.Health.Status}}' search-api 2>/dev/null)
    now=$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' search-api 2>/dev/null)
    read -r pc pt < <(bash probe.sh)
    echo "$(date +%T),$l1,$avail,$sw,$sh,$pc,$pt,$lim" >> "$out"
    if awk -v l="$l1" 'BEGIN{exit !(l>12)}'; then hi=$((hi + 1)); else hi=0; fi
    why=""
    [[ "$sh" != healthy ]] && why="(a) serving search-api health=$sh"
    [[ "$now" != "$want" ]] && why="(a) serving search-api id/start changed: $now"
    [[ "$pc" != 200 ]] && why="(b) serving probe answered $pc"
    awk -v t="$pt" -v l="$lim" 'BEGIN{exit !(t>=l)}' && why="(b) serving probe took ${pt}s, limit ${lim}s (3x the median ${med}s)"
    (( ${avail:-99999} <= ${SQ_TST_GUARD_AVAIL_MB:-4000} )) && why="(c) available memory ${avail} MB"
    (( hi >= 6 )) && why="(d) load1 above 12 for 3 minutes"
    if [[ -n "$why" ]]; then echo "GUARD $(date +%T): $why -> removing $c" >> "guard.$c.txt"; docker logs --tail 300 "$c" > "engine.$c.log" 2>&1; docker rm -f "$c" >/dev/null 2>&1; break; fi
    sleep "${SQ_TST_GUARD_EVERY:-28}"
done
