#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): arm A IS the Postgres engine; the A/B measures Postgres against the release engine. Delete at phase 7.
# Whole-engine A/B arms on testbox (2026-10-04). Run ON testbox: ssh testbox bash -s < arms.sh
# Needs ~/engine-eval/root (reflink copies of rc2-acc files + text/typo + text/wiki), image
# dbt-search-api:engine-eval (logout/search-api:kinds14 + tantivy 0.26.2 + this checkout's api/*.py).
# A = Postgres engine; B = release engine (4g: at 2g B was OOM-killed after ~2,200 queries). Throwaway only.
set -euo pipefail
PGPW=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' search-api | sed -n 's/^PGPASSWORD=//p')
D=${EE_DIR:-$HOME/engine-eval}
IMG=${EE_IMAGE:-dbt-search-api:engine-eval}
COMMON=(--label dbtest=safe-to-remove --cpus 2 --memory 2g --network search-serve_search-serve-net
 -e PGHOST=search-serve-postgres -e PGUSER=search -e PGDATABASE=search -e "PGPASSWORD=$PGPW"
 -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_STATEMENT_TIMEOUT_MS=30000
 -v /opt/logout-release/models/rerank-turbo-jevfree:/models/rerank-turbo-jevfree:ro)
docker rm -f ee-a ee-b >/dev/null 2>&1 || true
docker run -d --name ee-a "${COMMON[@]}" -p 127.0.0.1:18193:8090 "$IMG" >/dev/null
docker run -d --name ee-b "${COMMON[@]}" --memory 4g -p 127.0.0.1:18194:8090 \
 -v $D/root:/rel -v /opt/logout-release/w/proto-20261002-testbox/vectors:/vec:ro -v /opt/logout-release/models/nomic-embed-text-v1.5:/models/nomic-embed-text-v1.5:ro \
 -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_ALLOW_MIXED_WEB=1 \
 -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki \
 -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/vec/ivf2600 \
 "$IMG" >/dev/null
sleep 45; docker ps --filter label=dbtest=safe-to-remove --format '{{.Names}} {{.Status}}'; docker logs --tail 15 ee-a; echo ----; docker logs --tail 15 ee-b
