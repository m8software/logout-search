# shellcheck shell=bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# sourced by rc9-*.sh (rc9 = rc8 rebuilt after the homepage-language repair of the build database, 2026-10-08). Reference copy: hard-coded buildbox paths.
R=/data/release; REL2=proto-20261002-testbox; REL8=proto-20261002-testbox-rc8; REL9=proto-20261020-rc9
W=$R/w; B=$W/$REL2; O8=$B/rc8; O9=$B/rc9; O9W=$B/rc9-web; C=$R/code-rc9/apps/search; OWN=/in/own_rank_G.tsv
log(){ echo "$(date -u +%FT%TZ) $*"; }
mkdir -p $O9 $O9W $W/tmp
# no CPU cap (owner, 2026-10-06: run as fast as buildbox allows); still one heavy job at a time (7 GB of RAM)
PY="docker run --rm --user 0 --entrypoint python -e PYTHONPATH=/w/pylib -e SQLITE_TMPDIR=/w/tmp -e TMPDIR=/w/tmp -v /usr/bin/zstd:/usr/bin/zstd:ro -v /usr/lib/x86_64-linux-gnu/libzstd.so.1:/usr/lib/x86_64-linux-gnu/libzstd.so.1:ro -e PHRASES_SQL=/code/serve/phrases.sql -e NO_PAGE_TEXT_CHECK=/code/serve/no-page-text-check.sql -v $C:/code:ro -v $W:/w -v $R/rc5in:/in:ro -v $R/places-2026-10-10-us:/places:ro"
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
