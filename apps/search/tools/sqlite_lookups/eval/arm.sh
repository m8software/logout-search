#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): the T4 A/B arms include a Postgres lookup backend. Delete at phase 7.
# arm.sh NAME BACKEND [FILE]  — (re)start the throwaway search-api for one T4 eval arm on 127.0.0.1:18097.
#   BACKEND postgres | sqlite | release;  FILE = lookup file name in ~/t4 (sqlite) or release folder in ~/t4 (release)
set -euo pipefail
name=$1 backend=$2 file=${3:-lookup-jev.sqlite}
# Tantivy takes a lock file in the index directory even to read: the typo index is mounted
# from a writable copy owned by uid 1000 (typo-rw-<tag>), the lookup file read-only.
tag=${file#lookup-}; tag=${tag%.sqlite}
cd ~/t4 || exit 1
sudo -n docker rm -f t4-api >/dev/null 2>&1 || true
# the live container's database env (PG*, SEARCH_HOME), never printed
sudo -n docker inspect search-api --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(PG[A-Z]+|SEARCH_HOME)=' > env.live; chmod 600 env.live
if [ "$backend" = release ]; then
  rm -rf ~/t4/typo-rw-"$file" && cp -r ~/t4/"$file"/text/typo ~/t4/typo-rw-"$file"
  mounts=(-e SEARCH_RELEASE_DIR=/data/release -v ~/t4/"$file":/data/release:ro -v ~/t4/typo-rw-"$file":/data/release/text/typo)
else
  mounts=(-e SEARCH_LOOKUP_DB="/data/lookup/$file" -e SEARCH_TYPO_INDEX=/data/typo -v ~/t4:/data/lookup:ro -v ~/t4/typo-rw-"$tag":/data/typo)
fi
sudo -n docker run -d --name t4-api --label "dbtest=safe to remove" --cpus 2 --memory 2g \
  --network search-serve_search-serve-net -p 127.0.0.1:18097:8090 \
  --env-file env.live -e SEARCH_STATEMENT_TIMEOUT_MS=10000 -e SEARCH_LOOKUP_BACKEND="$backend" \
  "${mounts[@]}" t4-api:2 >/dev/null
for _ in $(seq 1 120); do
  sudo -n docker ps -q --filter name=t4-api --filter status=running | grep -q . || { sudo -n docker logs t4-api 2>&1 | tail -8; exit 1; }
  if curl -fsS -m 3 http://127.0.0.1:18097/readyz >/dev/null 2>&1; then break; fi; sleep 2
done
curl -fsS -m 5 http://127.0.0.1:18097/readyz >/dev/null
echo "arm $name ($backend $file) up"; sudo -n docker logs t4-api 2>&1 | grep -E "lookups:|kind vectors|Error|Traceback" | head -5 || true
