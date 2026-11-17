#!/usr/bin/env bash
set -euo pipefail
# A scratch search engine on testbox for proving a release folder (rc16, 2026-11-16). Run ON testbox (ssh testbox), as m8.
#   scratch-engine-testbox.sh up NAME PORT RELEASE_DIR RELEASE_ID     start (2 cpus, 4 GB, localhost only, release read-only)
#   scratch-engine-testbox.sh down NAME                                 stop and remove that container (only names starting wfs-)
# It never touches the serving stack: its own container (wfs-*), its own localhost port, Postgres off, no stored answers,
# no curated lists, no news: only the release folder answers, which is what a release proof needs. The other agent's package
# build also uses testbox (2 cpus), so one engine at a time keeps to 2 cpus here; run rc15 and rc16 one after the other.
cmd=${1:?up|down}; name=${2:?name}
[[ $name == wfs-* ]] || { echo "name must start with wfs-" >&2; exit 2; }
case $cmd in
down) docker rm -f "$name" >/dev/null 2>&1 && echo "removed $name" || echo "no $name"; exit 0 ;;
up) port=${3:?port}; rel=${4:?release dir}; rid=${5:?release id}
  [[ -f $rel/manifest.json ]] || { echo "$rel has no manifest.json" >&2; exit 2; }
  listening=$(ss -ltn "sport = :$port" 2>/dev/null || true)
  [[ $listening == *LISTEN* ]] && { echo "port $port is taken" >&2; exit 2; }
  docker rm -f "$name" >/dev/null 2>&1 || true
  appmount=(); [[ -n ${SCRATCH_APP:-} ]] && appmount=(-v "$SCRATCH_APP:/app:ro")      # engine code: the image's /app with master's files over it
  docker run -d --name "$name" --memory 4g --memory-swap 4g --cpus 2 \
    -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_REQUIRE_RERANKER=1 -e SEARCH_REQUIRE_KINDS_MODEL=1 \
    -e MALLOC_ARENA_MAX=2 -e SEARCH_SQLITE_CACHE_KB=8000 \
    -e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_RELEASE_ID="$rid" \
    -e SEARCH_RANKER=/rel/ranker.json \
    -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki -e SEARCH_WEB_BACKEND=tantivy-release \
    -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/rel/vectors/ivf -e SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5 \
    -e SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5 -e SEARCH_CE_DIR=/rel/models/rerank-turbo \
    -e SEARCH_API_ALLOW_FROM=0.0.0.0/0 -e SEARCH_CE_GOV_DIR=/rel/models/rerank-turbo \
    -e SEARCH_BREADTH=JDLF -e SEARCH_BREADTH_FLOOR=0 -e SEARCH_SCORE_ALL=1 -e SEARCH_SCORE_ALL_TOP=10 \
    "${appmount[@]}" -v "$rel:/rel:ro" -p "127.0.0.1:$port:8090" "${SCRATCH_IMAGE:-logout/search-api:public}" >/dev/null
  t=0
  until [[ $(curl -s -m 20 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/readyz" 2>/dev/null || true) == 200 ]]; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]] && (( t < 600 )) || { docker logs --tail 20 "$name" >&2; docker rm -f "$name" >/dev/null 2>&1; echo "$name did not become ready" >&2; exit 1; }
    sleep 5; t=$((t + 5))
  done
  echo "ready after ${t}s: $name on 127.0.0.1:$port" ;;
*) echo "up|down" >&2; exit 2 ;;
esac
