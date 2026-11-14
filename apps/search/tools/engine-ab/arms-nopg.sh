#!/usr/bin/env bash
# No-Postgres arms on testbox (2026-10-05). Run ON testbox: ssh testbox 'bash -s N' < arms-nopg.sh   (N = n | c | d)
#   n  smoke: release lookups + wiki + IVF, SEARCH_POSTGRES=off, web text still Postgres-backed (so it must miss)
#   c  arm C: n + SEARCH_WEB_BACKEND=tantivy-release (rootC), anchor-phrases, news sqlite (empty, from schema-sqlite.sql)
#   d  arm D: c + kinds.sqlite (rootD), kinds ON
# No PG* variables are given to the container, so no database can be reached. Throwaway only.
set -euo pipefail
arm="${1:?n|c|d}"
D=${EE_DIR:-$HOME/engine-rc3}
IMG=${EE_IMAGE:-dbt-search-api:engine-rc3}
case "$arm" in n) root=root port=18195;; c) root=rootC port=18196;; d) root=rootD port=18197;; *) exit 2;; esac
env_args=(-e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel
 -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki
 -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/vec/ivf2600)
if [ "$arm" != n ]; then
    env_args+=(-e SEARCH_WEB_BACKEND=tantivy-release -e SEARCH_NEWS_BACKEND=sqlite -e SEARCH_NEWS_SQLITE=/news/news.sqlite)
fi
docker rm -f "ee-$arm" >/dev/null 2>&1 || true
docker run -d --name "ee-$arm" --label dbtest=safe-to-remove --cpus 2 --memory "${MEM:-4g}" \
 -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" \
 -v "$D/$root:/rel" -v "$D/news.sqlite:/news/news.sqlite:ro" \
 -v /opt/logout-release/w/proto-20261002-testbox/vectors:/vec:ro \
 -v /opt/logout-release/models/nomic-embed-text-v1.5:/models/nomic-embed-text-v1.5:ro \
 -v /opt/logout-release/models/rerank-turbo-jevfree:/models/rerank-turbo-jevfree:ro \
 -p "127.0.0.1:$port:8090" "${env_args[@]}" "$IMG" >/dev/null
sleep 40
docker ps --filter "name=ee-$arm" --format '{{.Names}} {{.Status}}'
docker logs --tail 12 "ee-$arm" 2>&1 | cut -c1-220
