#!/usr/bin/env bash
# Three engines on testbox for the experiment: rc8 as it is (base), rc8 + the small budget's government pages,
# rc8 + the medium budget's. Same engine code and settings in all three (tools/engine-ab/arms-rc7.sh's release-only
# arm, with indexhost's models). Run ON testbox:  ssh testbox 'bash -s ARM' < arms.sh   (ARM = setup | overlay | up | state | down)
#   setup    image from ~/engine-gov/api, three reflink copies of rc8-acc (the copies carry the .gov-overlay marker)
#   overlay  ~/engine-gov/gov-pages.jsonl.gz into the small and medium copies (overlay.py)
#   up       base :18201, small :18202, medium :18203 on 127.0.0.1
set -euo pipefail
arm="${1:?setup|overlay|up|state|down}"
D=$HOME/engine-gov; IMG=dbt-search-api:engine-gov; SRC=/opt/logout-release/w/rc8-acc
ARMS=(base small medium)
case "$arm" in
setup)
    test -d "$D/api" && test -d "$SRC"
    printf 'FROM dbt-search-api:engine-rc8\nUSER 0\nCOPY api/*.py /app/\nUSER 1000:1000\n' > "$D/Dockerfile"
    docker build -q -t "$IMG" "$D"
    rm -f "$D/news.sqlite"; python3 -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).executescript(open(sys.argv[2]).read())" "$D/news.sqlite" "$D/schema-sqlite.sql"
    for a in "${ARMS[@]}"; do
        rm -rf "${D:?}/root-$a"; cp -r --reflink=always "$SRC" "$D/root-$a"; chmod -R u+w "$D/root-$a"; touch "$D/root-$a/.gov-overlay"
    done
    du -sh "$D"; df -h "$D" | tail -1;;
overlay)
    test -s "$D/gov-pages.jsonl.gz"
    for a in small medium; do
        docker run --rm --user "$(id -u):$(id -g)" -v "$D/root-$a:/rel" -v "$D/gov-pages.jsonl.gz:/in/pages.jsonl.gz:ro" -v "$D/overlay.py:/app/gov_overlay.py:ro" \
            --entrypoint python "$IMG" /app/gov_overlay.py /rel /in/pages.jsonl.gz --arm "$a"
    done;;
up)
    i=0
    for a in "${ARMS[@]}"; do
      i=$((i + 1)); port=$((18200 + i))
      docker rm -f "gov-$a" >/dev/null 2>&1 || true
      docker run -d --name "gov-$a" --label dbtest=safe-to-remove --memory "${MEM:-4g}" \
       -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_REQUIRE_RERANKER=1 \
       -e MALLOC_ARENA_MAX=2 -e SEARCH_SQLITE_CACHE_KB=8000 \
       -e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_RANKER=/rel/ranker.json \
       -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki \
       -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/rel/vectors/ivf -e SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5 \
       -e SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5 -e SEARCH_CE_DIR=/rel/models/rerank-turbo-jevfree \
       -e SEARCH_WEB_BACKEND=tantivy-release -e SEARCH_NEWS_BACKEND=sqlite -e SEARCH_NEWS_SQLITE=/news/news.sqlite \
       -v "$D/root-$a:/rel" -v "$D/news.sqlite:/news/news.sqlite:ro" -p "127.0.0.1:$port:8090" "$IMG" >/dev/null
    done
    sleep "${WARM:-90}"
    i=0
    for a in "${ARMS[@]}"; do
      i=$((i + 1)); docker ps --filter "name=gov-$a" --format '{{.Names}} {{.Status}}'; docker logs --tail 4 "gov-$a" 2>&1 | cut -c1-200
      curl -s -m 20 -o /dev/null -w "readyz $a: %{http_code}\n" "http://127.0.0.1:$((18200 + i))/readyz"
    done;;
state)
    # The small copy again with the state rule (serve/api/gov_state.py), :18204. One engine answers both ways:
    # /search?drop=govstate is the rule off. Needs ~/engine-gov/api-state (the branch's api/*.py) and gov-states.json.
    test -s "$D/api-state/gov_state.py" && test -s "$D/gov-states.json"
    printf 'FROM dbt-search-api:engine-rc8\nUSER 0\nCOPY api-state/*.py /app/\nUSER 1000:1000\n' > "$D/Dockerfile.state"
    docker build -q -t "$IMG-state" -f "$D/Dockerfile.state" "$D"
    docker rm -f gov-state >/dev/null 2>&1 || true
    docker run -d --name gov-state --label dbtest=safe-to-remove --memory "${MEM:-4g}" \
       -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_REQUIRE_RERANKER=1 \
       -e MALLOC_ARENA_MAX=2 -e SEARCH_SQLITE_CACHE_KB=8000 -e SEARCH_DROP_EVAL=1 -e SEARCH_GOV_STATES=/gov-states.json \
       -e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_RANKER=/rel/ranker.json \
       -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki \
       -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/rel/vectors/ivf -e SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5 \
       -e SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5 -e SEARCH_CE_DIR=/rel/models/rerank-turbo-jevfree \
       -e SEARCH_WEB_BACKEND=tantivy-release -e SEARCH_NEWS_BACKEND=sqlite -e SEARCH_NEWS_SQLITE=/news/news.sqlite \
       -v "$D/root-small:/rel" -v "$D/news.sqlite:/news/news.sqlite:ro" -v "$D/gov-states.json:/gov-states.json:ro" \
       -p "127.0.0.1:18204:8090" "$IMG-state" >/dev/null
    sleep "${WARM:-90}"
    docker ps --filter name=gov-state --format '{{.Names}} {{.Status}}'; docker logs --tail 6 gov-state 2>&1 | cut -c1-200
    curl -s -m 20 -o /dev/null -w "readyz state: %{http_code}\n" "http://127.0.0.1:18204/readyz";;
down) for a in "${ARMS[@]}" state; do docker rm -f "gov-$a" >/dev/null 2>&1 || true; done;;
*) exit 2;;
esac
