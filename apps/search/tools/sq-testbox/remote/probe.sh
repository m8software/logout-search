#!/usr/bin/env bash
# One real (uncached) search on testbox's SERVING engine (the parity probe): "public library", its letter case taken
# from 13 bits that were never used before in this directory (probe.used), because the serving engine caches a repeated
# query for 6 h and would answer in about 1 ms (2026-10-14: a baseline of cached answers made the guard's limit 3.5 ms
# and it removed the control's engine at the first real probe). Prints: http_code seconds. Every probe is logged to
# probe.log (time, spelling, code, seconds) and comes home with the arm's logs.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
q="public library"; tries=0
base=${SQ_TST_PROBE_BASE:-$(( (RANDOM * 32768 + RANDOM + $(date +%N | sed 's/^0*//; s/^$/0/') + $$) % 8192 ))}
while :; do
    n=$(( (base + tries) % 8192 ))
    out=""
    for ((i = 0; i < ${#q}; i++)); do c=${q:$i:1}; if (( i > 0 && (n >> (i - 1)) & 1 )); then c=${c^^}; fi; out+=$c; done
    tries=$((tries + 1))
    grep -qxF -- "$out" probe.used 2>/dev/null || break
    (( tries >= 8192 )) && break
done
echo "$out" >> probe.used
ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "search-serve_search-serve-net").IPAddress}}' search-api 2>/dev/null)
r=$(curl -s -m 10 -o /dev/null -w '%{http_code} %{time_total}' "http://$ip:8090/search?k=5&q=${out// /+}" 2>/dev/null) || r="000 10"
echo "$(date +%T) $out $r" >> probe.log
echo "$r"
