# shellcheck shell=bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# sourced by rc9p-*.sh. Reference copy: hard-coded buildbox paths.
R=/data/release; REL2=proto-20261002-testbox; REL9=proto-20261020-rc9; RELP=proto-20261023-rc9p; PLACES=${PLACES:-places-2026-10-23-us}
W=$R/w; B=$W/$REL2; O9=$B/rc9; OP=$B/rc9p; OPW=$B/rc9p-web; C=$R/code-rc9p/apps/search; OWN=/in/own_rank_G.tsv
log(){ echo "$(date -u +%FT%TZ) $*"; }
mkdir -p $OP $OPW $W/tmp
# no CPU cap (owner, 2026-10-06: run as fast as buildbox allows); still one heavy job at a time (7 GB of RAM)
PY="docker run --rm --user 0 --entrypoint python -e PYTHONPATH=/w/pylib -e SQLITE_TMPDIR=/w/tmp -e TMPDIR=/w/tmp -v /usr/bin/zstd:/usr/bin/zstd:ro -v /usr/lib/x86_64-linux-gnu/libzstd.so.1:/usr/lib/x86_64-linux-gnu/libzstd.so.1:ro -e PHRASES_SQL=/code/serve/phrases.sql -e NO_PAGE_TEXT_CHECK=/code/serve/no-page-text-check.sql -v $C:/code:ro -v $W:/w -v $R/rc5in:/in:ro -v $R/$PLACES:/places:ro"
PGENV="--network container:buildbox-pg -e PG_DSN=host=127.0.0.1\ dbname=search\ user=search -e PG_SEARCH_DSN=host=127.0.0.1\ dbname=search\ user=search"
pg_up(){
  docker rm -f buildbox-pg >/dev/null 2>&1 || true
  docker network inspect lrnet >/dev/null 2>&1 || docker network create lrnet >/dev/null
  rm -f $R/pg/18/docker/postmaster.pid; chown -R 999:999 $R/pg
  docker run -d --name buildbox-pg --network lrnet --memory 3g --shm-size 1g -v $R/pg:/var/lib/postgresql \
    logout/search-postgres:18.6-pgts1.4.0 postgres -c shared_preload_libraries=pg_textsearch -c shared_buffers=512MB \
    -c autovacuum=off -c max_wal_size=8GB -c work_mem=128MB -c max_parallel_workers_per_gather=4 >/dev/null
  until docker exec buildbox-pg pg_isready -U search -d search -h 127.0.0.1 >/dev/null 2>&1; do sleep 3; done; log "PG-UP (bridge)"
}
pg_down(){ docker stop -t 120 buildbox-pg >/dev/null 2>&1 || true; docker rm -f buildbox-pg >/dev/null 2>&1 || true; log "PG-DOWN"; }
