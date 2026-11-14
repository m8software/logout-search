#!/usr/bin/env bash
# A/B arms for the own site rank (G3 D2). Run ON testbox: ssh testbox bash -s -- A|B1|B2 < arms.sh
# Throwaway containers only: --cpus 2, label dbtest=safe-to-remove, the serving database read-only.
# Needs ~/own-rank-eval/{rootA,rootB1..rootB4,api}: release roots (reflink copies of rc2-acc; they differ only in
# signals.bin; rootB2 and rootB4 also in lookup.sqlite's names) and this checkout's apps/search/serve/api/*.py + ranker.json.
# Image dbt-search-api:t2c carries tantivy 0.26.2; the api folder is mounted over /app. Both arms are the SAME
# engine on the release lookups (SEARCH_LOOKUP_BACKEND=release); wiki text, sites/pages/anchors text and meaning
# search stay Postgres in both (no release text index exists on testbox), so their popularity factor is the serving
# database's Tranco rank in A and in B. A = signals.bin of rc2-acc (Tranco); B1 = recipe v1's own rank in its place;
# B2 = B1 + the exact-name rows of every site that is not in own_rank.tsv deleted (mk_b2.py); B3 = recipe v2 (pv/sl only
# from organisation articles) in B1's place; B4 = B3 + B2's name rows; B5 = recipe v3 (+ official_key front-page rule) rank only; B6 = B5 + B2's name rows; v4 = v3 + official hosts under a shared domain ranked by their own CC host rank (B7), v5org / v5all = + they inherit the parent's rank (organisation articles / every article; B8, B9); B10 = the dev-chosen recipe + names.
# Kinds are switched off by the client (&off=kind_docs,...). Ports: A 18091, B1 18092, B2 18093, B3 18094, B4 18095, B5 18096, B6 18097.
set -euo pipefail
arm=${1:?A|B1..B14}
D=$HOME/own-rank-eval
case $arm in A) port=18091;; B1) port=18092;; B2) port=18093;; B3) port=18094;; B4) port=18095;; B5) port=18096;; B6) port=18097;; B7) port=18098;; B8) port=18099;; B9) port=18100;; B10) port=18101;; B11) port=18102;; B12) port=18103;; B13) port=18104;; B14) port=18105;; B15) port=18106;; *) echo "arm?"; exit 2;; esac
# a variant of an arm: NAME=B3cr2500 PORT=18096 CR=2500 ... arms.sh B3  (same root, another container, SEARCH_CONFIDENT_RANK=CR)
name=${NAME:-$arm}; port=${PORT:-$port}; cr=()
[ -z "${CR:-}" ] || cr=(-e "SEARCH_CONFIDENT_RANK=$CR")
PGPW=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' search-api | sed -n 's/^PGPASSWORD=//p')
docker rm -f own-rank-$name >/dev/null 2>&1 || true
docker run -d --name own-rank-$name --label dbtest=safe-to-remove --cpus 2 --memory 4g \
 --network search-serve_search-serve-net -p 127.0.0.1:$port:8090 \
 -e PGHOST=search-serve-postgres -e PGUSER=search -e PGDATABASE=search -e "PGPASSWORD=$PGPW" \
 -e PGOPTIONS='-c default_transaction_read_only=on' \
 -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_STATEMENT_TIMEOUT_MS=30000 \
 -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_ALLOW_MIXED_WEB=1 "${cr[@]}" \
 -v /opt/logout-release/models/rerank-turbo-jevfree:/models/rerank-turbo-jevfree:ro \
 -v "$D/root$arm":/rel -v "$D/api":/app:ro \
 dbt-search-api:t2c >/dev/null
for _ in $(seq 1 90); do
  docker ps -q --filter name=own-rank-$name --filter status=running | grep -q . || { docker logs own-rank-$name 2>&1 | tail -12; exit 1; }
  curl -fsS -m 3 http://127.0.0.1:$port/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS -m 5 http://127.0.0.1:$port/readyz >/dev/null && echo "own-rank-$name up on 127.0.0.1:$port"
docker logs own-rank-$name 2>&1 | grep -E "lookups:|Traceback|Error" | head -5 || true
