#!/usr/bin/env bash
# The Postgres bridge of the build pipeline (release.toml [bridges.postgres]): a throwaway server over a COPY of the
# build database's data folder, started before a bridge stage and stopped after it. It exists so stages that still read
# tables (stage-sqlite, signals, web-input, anchor-phrases) run through the pipeline while they are ported; the
# pipeline's `status` counts how many are left. Nothing here is the system of record: the copy is replaceable.
#   bridge-postgres.sh up | down
#   BRIDGE_PG_DIR    the data folder (default /data/release/pg; buildbox's copy of indexhost's tables). Must hold 18/docker
#                    (a Postgres 18 data folder), or nothing is touched.
#   BRIDGE_PG_IMAGE  the server image (default logout/search-postgres:18.6-pgts1.4.0)
#   BRIDGE_PG_NAME   the container name (default build-pg; stages join its network with --network container:NAME)
#   DOCKER           docker | podman | "sudo -n docker"
set -euo pipefail
DIR="${BRIDGE_PG_DIR:-/data/release/pg}"; IMG="${BRIDGE_PG_IMAGE:-logout/search-postgres:18.6-pgts1.4.0}"
NAME="${BRIDGE_PG_NAME:-build-pg}"; D="${DOCKER:-docker}"
log() { echo "$(date -u +%FT%TZ) bridge postgres: $*"; }
case "${1:-}" in
  up)
    [ -n "$DIR" ] && [ "${DIR:0:1}" = "/" ] && [ -d "$DIR/18/docker" ] && [ -f "$DIR/18/docker/PG_VERSION" ] \
      || { log "refusing: $DIR is not a Postgres 18 data folder (no 18/docker/PG_VERSION)"; exit 2; }
    $D rm -f "$NAME" >/dev/null 2>&1 || true
    rm -f "$DIR/18/docker/postmaster.pid"
    # the server runs as uid 999 in the image; only the data folder's ownership is touched, nothing above it
    chown -R 999:999 "$DIR/18" 2>/dev/null || true
    $D run -d --name "$NAME" --memory 3g --shm-size 1g -v "$DIR:/var/lib/postgresql" "$IMG" \
      postgres -c shared_preload_libraries=pg_textsearch -c shared_buffers=512MB -c autovacuum=off \
      -c max_wal_size=8GB -c work_mem=128MB -c max_parallel_workers_per_gather=4 >/dev/null
    for _ in $(seq 1 100); do
      $D exec "$NAME" pg_isready -U search -d search -h 127.0.0.1 >/dev/null 2>&1 && { log "up ($NAME)"; exit 0; }
      sleep 3
    done
    log "did not become ready"; $D logs --tail 20 "$NAME" || true; exit 1 ;;
  down)
    $D stop -t 120 "$NAME" >/dev/null 2>&1 || true
    $D rm -f "$NAME" >/dev/null 2>&1 || true
    log "down" ;;
  *) echo "usage: bridge-postgres.sh up|down" >&2; exit 2 ;;
esac
