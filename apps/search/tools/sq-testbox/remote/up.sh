#!/usr/bin/env bash
# up.sh NAME PORT RELEASE_DIR RELEASE_ID [K=V ...] — one sweep engine beside testbox's serving stack (the parity
# shape): its own container and localhost port, 4 GB / 3 cpus, testbox's engine image, the API code from this directory,
# the release read-only, Postgres off. Waits until no one else works on testbox is confirmed, memory and load are calm,
# measures the serving engine's search for 70 s, starts the engine, waits for /readyz, starts the guard.
set -uo pipefail
R=$(cd "$(dirname "$0")" && pwd); cd "$R" || exit 1
name=$1 port=$2 rel=$3 rid=$4; shift 4
die() { echo "testbox up: $1" >&2; exit "${2:-1}"; }
why=$(bash check.sh) || die "$why — not starting (the sweep pauses)" 3
t=0
while :; do
    read -r l1 _ < /proc/loadavg; avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    awk -v l="$l1" -v m="${SQ_TST_MAX_LOAD:-3}" 'BEGIN{exit !(l<=m)}' && (( avail >= ${SQ_TST_MIN_AVAIL_MB:-6000} )) && break
    (( t >= ${SQ_TST_CALM_WAIT:-600} )) && die "testbox not calm after ${t}s: load1 $l1, available $avail MB"
    sleep 15; t=$((t + 15))
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' search-api 2>/dev/null)" == healthy ]] || die "serving search-api is not healthy"
docker container inspect "$name" >/dev/null 2>&1 && die "$name already exists"
ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN && die "port $port is taken on testbox"
: > "baseline.$name.csv"
# 7 probes of never-used spellings; the median of a serving search that is not answered from its cache is 100 ms or more
# (parity run: 193 ms), so under SQ_TST_MIN_BASELINE_S (20 ms) the probes were cached, whatever the cause: no engine.
ok=0
for round in 1 2 3; do
    : > "baseline.$name.csv"
    for i in 1 2 3 4 5 6 7; do read -r code s < <(bash probe.sh); echo "$code,$s" >> "baseline.$name.csv"; (( i < 7 )) && sleep "${SQ_TST_BASELINE_EVERY:-10}"; done
    grep -c '^200,' "baseline.$name.csv" | grep -qx 7 || die "the serving probe failed before the engine started: $(paste -sd' ' "baseline.$name.csv")"
    cut -d, -f2 "baseline.$name.csv" | sort -n | sed -n 4p > "baseline.$name.median"
    if awk -v m="$(cat "baseline.$name.median")" -v f="${SQ_TST_MIN_BASELINE_S:-0.02}" 'BEGIN{exit !(m >= f)}'; then ok=1; break; fi
done
(( ok )) || die "the serving engine answered the baseline probes from its cache three times (median $(cat "baseline.$name.median") s): the guard's limit would be meaningless — not starting"
docker inspect -f '{{.Id}} {{.State.StartedAt}}' search-api > "serving.$name.id"
envargs=(); for kv in "$@"; do envargs+=(-e "$kv"); done
docker run -d --name "$name" --memory 4g --memory-swap 4g --cpus 3 \
    -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_REQUIRE_RERANKER=1 -e SEARCH_REQUIRE_KINDS_MODEL=1 \
    -e MALLOC_ARENA_MAX=2 -e SEARCH_SQLITE_CACHE_KB=8000 \
    -e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_RELEASE_ID="$rid" \
    -e SEARCH_RANKER=/rel/ranker.json \
    -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki \
    -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/rel/vectors/ivf -e SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5 \
    -e SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5 -e SEARCH_CE_DIR=/rel/models/rerank-turbo-jevfree \
    -e SEARCH_WEB_BACKEND=tantivy-release \
    -e SEARCH_NEWS_BACKEND=sqlite -e SEARCH_NEWS_SQLITE=/news/news.sqlite -v "$R/news.sqlite:/news/news.sqlite:ro" \
    "${envargs[@]}" -v "$R/api:/app:ro" -v "$rel:/rel:ro" -p "127.0.0.1:$port:8090" "${SQ_TST_IMAGE:-dbt-search-api:engine-rc4}" >/dev/null \
    || die "docker run failed"
t=0
until curl -s -m 20 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/readyz" 2>/dev/null | grep -q '^200$'; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != true ]] || (( t >= 300 )); then
        docker logs --tail 15 "$name" >&2 2>&1; docker rm -f "$name" >/dev/null 2>&1; die "$name did not become ready"
    fi
    sleep 5; t=$((t + 5))
done
rm -f "guard.$name.txt"
setsid nohup bash guard.sh "$name" > /dev/null 2>&1 < /dev/null &
echo $! > "guard.$name.pid"
echo "ready after ${t}s"
