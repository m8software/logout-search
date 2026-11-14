#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): it benchmarks Postgres itself (a throwaway Postgres box). Delete at phase 7.
# =============================================================================
# box-bench.sh — the customer-box bench (docs/internal/SEARCH-BOX-PLAN.md) in its OWN
# Postgres instances on indexhost. Dev tooling; never shipped.
# =============================================================================
# Why its own instances: bench work in clones of the build database, inside the build
# cluster, broke pg_textsearch indexes of OTHER databases there (shared memory keyed by
# index OID; clones keep the OIDs) — 2026-09-26, cleared by a restart. Here every variant
# is its own ZFS clone dataset (nas/logout-bench-NAME: instant, copy-on-write, no
# downtime — per-file block cloning ran at ~2 GB/min, 2026-09-26) whose Postgres data
# directory is run by its own container, holding ONE database, restarted after every
# drop or rewrite. Nothing shares memory with the build database or another variant.
#
#   box-bench.sh source           CHECKPOINT + ZFS snapshot of the build cluster (no
#                                 downtime), cloned to bench `base`; keep only the build DB
#                                 and prepare the box baseline in it -> database `box`
#   box-bench.sh clone SRC DST    ZFS snapshot of bench SRC, cloned to bench DST (SRC stopped)
#   box-bench.sh up NAME          start search-bench-pg on bench/NAME
#   box-bench.sh sql NAME FILE    run FILE in `box` of bench/NAME (detached; waits; restarts)
#   box-bench.sh probe NAME       every BM25 index answers, or exit 1
#   box-bench.sh size NAME        served size (tables + indexes, catalog-based)
#   box-bench.sh api NAME         (re)start search-api-box on :8091 against bench/NAME
#   box-bench.sh down             stop the bench container and search-api-box
#   box-bench.sh rm NAME          destroy bench NAME (its dataset and snapshots)
#   box-bench.sh cleanup          destroy every bench dataset and the source snapshot
#   box-bench.sh dump NAME FILE   pg_dump -Fc (zstd) of bench NAME into snapshots/FILE + FILE.sha256
#                                 (restorable by serve/deploy.sh restore: extensions left out)
#   box-bench.sh upload FILE VER  put snapshots/FILE (+ .sha256) in Gitea's generic package
#                                 registry: owner/search-snapshot/VER/FILE
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARCH_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
JUMP="${SEARCH_JUMP_HOST:-user@jump-host}"
TARGET="${SEARCH_TARGET_HOST:-user@index-host}"
REMOTE_DIR="/mnt/nas/logout/search"
ROOT="${REMOTE_DIR}/bench"          # jobs/ only; data lives in the clone datasets
POOL_DS="nas/logout"                 # the dataset holding search/pgdata
BENCH_DS="nas/logout-bench"          # bench NAME = dataset ${BENCH_DS}-NAME
SNAP_DIR="${REMOTE_DIR}/snapshots"   # dumps land here (indexhost)
GITEA_API="http://127.0.0.1:3030/api/packages/owner/generic/search-snapshot"   # from indexhost
IMAGE="logout/search-postgres:18.6-pgts1.4.0"
PG="search-bench-pg"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15"

log() { echo "$(date -u +%FT%TZ) $*" >&2; }
die() { log "ERROR: $*"; exit 1; }
remote() {   # a bash script on stdin, as root on indexhost
    # shellcheck disable=SC2029
    ssh ${SSH_OPTS} "${JUMP}" "ssh ${SSH_OPTS} ${TARGET} 'sudo -n bash -s'"
}
name_ok() { [[ "$1" =~ ^[a-z0-9_-]+$ ]] || die "bad bench name '$1'"; }

cmd_down() {
    printf 'docker rm -f %s search-api-box >/dev/null 2>&1 || true\n' "$PG" | remote
}

# the Postgres data directory (parent of 18/docker) of bench NAME, on indexhost
datadir() { printf '/mnt/%s-%s/search/pgdata' "$BENCH_DS" "$1"; }

cmd_up() {
    local name="$1"; name_ok "$name"
    local dd; dd=$(datadir "$name")
    cat <<EOF | remote
set -euo pipefail
[ -d ${dd}/18/docker ] || { echo "no data directory ${dd}" >&2; exit 1; }
docker rm -f ${PG} >/dev/null 2>&1 || true
rm -f ${dd}/18/docker/postmaster.pid
mkdir -p ${ROOT}/jobs
docker run -d --name ${PG} --network search_default --shm-size 12g --memory 14g \\
    -v ${dd}:/var/lib/postgresql -v ${ROOT}/jobs:/jobs \\
    ${IMAGE} postgres -c shared_preload_libraries=pg_textsearch,pg_prewarm -c shared_buffers=1GB \\
    -c maintenance_work_mem=2GB -c max_parallel_maintenance_workers=4 -c max_wal_size=8GB >/dev/null
for i in \$(seq 1 120); do
    docker exec ${PG} pg_isready -U search -d postgres -h 127.0.0.1 >/dev/null 2>&1 && { echo "${PG} up on ${name}"; exit 0; }
    sleep 3
done
docker logs --tail 20 ${PG} >&2; exit 1
EOF
}

cmd_clone() {   # snapshot bench SRC (must be stopped), clone it to bench DST
    local src="$1" dst="$2"; name_ok "$src"; name_ok "$dst"
    local dd; dd=$(datadir "$src")
    cat <<EOF | remote
set -euo pipefail
if docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' ${PG} 2>/dev/null | grep -q "${dd} "; then
    echo "bench ${src} is running — stop it first" >&2; exit 1
fi
zfs list -H ${BENCH_DS}-${src} >/dev/null || { echo "no bench ${src}" >&2; exit 1; }
zfs list -H ${BENCH_DS}-${dst} >/dev/null 2>&1 && { echo "bench ${dst} exists — rm it first" >&2; exit 1; }
zfs destroy ${BENCH_DS}-${src}@to-${dst} 2>/dev/null || true
zfs snapshot ${BENCH_DS}-${src}@to-${dst}
zfs clone ${BENCH_DS}-${src}@to-${dst} ${BENCH_DS}-${dst}
[ -d $(datadir "$dst")/18/docker ] || { echo "clone not mounted at $(datadir "$dst")" >&2; exit 1; }
echo "bench ${src} -> ${dst} (zfs clone)"
EOF
}

cmd_rm() {
    local name="$1"; name_ok "$name"
    [[ "$name" != base ]] || die "refusing to destroy bench base (cleanup removes everything)"
    printf 'docker rm -f %s >/dev/null 2>&1 || true\nzfs destroy -r %s-%s\n' "$PG" "$BENCH_DS" "$name" | remote
}

cmd_cleanup() {   # every bench dataset, then the source snapshot of the build cluster
    cat <<EOF | remote
set -euo pipefail
docker rm -f ${PG} search-api-box >/dev/null 2>&1 || true
for ds in \$(zfs list -H -o name -t filesystem | grep "^${BENCH_DS}-" | sort -r); do
    zfs destroy -r "\$ds" && echo "destroyed \$ds"
done
zfs destroy ${POOL_DS}@bench-src 2>/dev/null && echo "destroyed ${POOL_DS}@bench-src" || true
EOF
}

# run SQL FILE in the bench database, detached inside the container, and wait for it
_run_sql() {
    local db="$1" file="$2"
    [[ -f "$file" ]] || die "no SQL file $file"
    local b64; b64=$(base64 -w0 < "$file")
    cat <<EOF | remote
set -euo pipefail
mkdir -p ${ROOT}/jobs
echo '${b64}' | base64 -d > ${ROOT}/jobs/job.sql
rm -f ${ROOT}/jobs/job.log
docker exec -d ${PG} sh -c 'psql -U search -d ${db} -v ON_ERROR_STOP=1 -q -f /jobs/job.sql > /jobs/job.log 2>&1; echo "EXIT \$?" >> /jobs/job.log'
EOF
    log "running $(basename "$file") in ${db} (detached)"
    local out
    while :; do
        sleep 30
        out=$(printf 'tail -n 3 %s/jobs/job.log 2>/dev/null; docker inspect -f "{{.State.Running}}" %s 2>/dev/null\n' "$ROOT" "$PG" | remote || true)
        grep -q '^EXIT ' <<<"$out" && break
        grep -qx false <<<"$out" && die "bench container stopped while $(basename "$file") ran"
    done
    grep -q '^EXIT 0$' <<<"$out" || { printf 'grep -v NOTICE %s/jobs/job.log | tail -20\n' "$ROOT" | remote >&2; die "$(basename "$file") failed"; }
    log "$(basename "$file") done"
}

cmd_sql() {   # sql NAME FILE: up, run, restart (fresh shared memory after the writes)
    local name="$1" file="$2"; name_ok "$name"
    cmd_up "$name" >/dev/null
    _run_sql box "$file"
    cmd_up "$name" >/dev/null
}

cmd_probe() {
    local name="$1"; name_ok "$name"
    cmd_up "$name" >/dev/null 2>&1 || true
    local out
    out=$(cat <<'EOF' | sed "s/@PG@/${PG}/g" | remote
P() { docker exec @PG@ psql -U search -d box -Atc "$1" 2>&1 | tail -1; }
for ix in $(P "SELECT string_agg(c.relname, ' ') FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE a.amname = 'bm25'"); do
    t=$(P "SELECT indrelid::regclass FROM pg_index WHERE indexrelid = '$ix'::regclass")
    col=$(P "SELECT pg_get_indexdef('$ix'::regclass)" | sed -E 's/.*USING bm25 \((.*)\) WITH.*/\1/')
    echo "$ix $(P "SET statement_timeout = 180000; SELECT count(*) FROM (SELECT 1 FROM $t ORDER BY $col <@> to_bm25query('new york', '$ix') LIMIT 5) x")"
done
echo "wiki_vec $(P "SET statement_timeout = 180000; SELECT count(*) FROM (SELECT id FROM wiki_vec ORDER BY v <=> (SELECT v FROM wiki_vec LIMIT 1) LIMIT 5) x")"
EOF
)
    echo "$out" | sed 's/^/  probe /' >&2
    [[ -n "$out" ]] && ! grep -qvE ' [0-9]+$' <<<"$out" || die "probe failed on bench ${name}"
    log "probe ok: bench ${name}"
}

cmd_size() {
    local name="$1"; name_ok "$name"
    cmd_up "$name" >/dev/null 2>&1 || true
    printf '%s\n' "docker exec ${PG} psql -U search -d box -Atc \"SELECT round(sum(pg_total_relation_size(c.oid)) / 1e9, 1) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm') AND c.relname <> 'wiki_pop'\"" | remote
}

cmd_api() {   # search-api-box (this checkout's code on the live image) against bench/NAME
    local name="$1"; name_ok "$name"
    cmd_up "$name" >/dev/null
    local payload; payload=$(tar -C "${SEARCH_DIR}/serve/api" --exclude=__pycache__ --exclude=./models -czf - . | base64 -w0)
    cat <<EOF | remote
set -euo pipefail
cd ${REMOTE_DIR}
rm -rf box/api && mkdir -p box/api
echo '${payload}' | base64 -d | tar -xzf - -C box/api
cmp -s box/api/Dockerfile serve/api/Dockerfile || { echo "serve/api/Dockerfile differs from the live one" >&2; exit 1; }
printf 'FROM logout/search-api:1\nCOPY search_api.py ranker.json warm.py ./\n' > box/api/Dockerfile.box
DOCKER_BUILDKIT=0 docker build -q -f box/api/Dockerfile.box -t logout/search-api:box box/api >/dev/null
env_get() { grep "^\$1=" .env | cut -d= -f2-; }
docker rm -f search-api-box >/dev/null 2>&1 || true
docker run -d --name search-api-box --network search_default --memory 1g \\
    -e PGHOST=${PG} -e PGUSER=search -e PGDATABASE=box -e "PGPASSWORD=\$(env_get POSTGRES_PASSWORD)" \\
    -e "SEARCH_API_ALLOW_FROM=\$(env_get SEARCH_API_ALLOW_FROM)" -e "SEARCH_HOME=\$(env_get SEARCH_HOME)" \\
    -e SEARCH_STATEMENT_TIMEOUT_MS=20000 -e SEARCH_NEWS_DB=box \\
    -p "\$(env_get SEARCH_API_BIND):8091:8090" --log-opt max-size=5m --log-opt max-file=2 logout/search-api:box >/dev/null
for i in \$(seq 1 40); do
    docker exec search-api-box python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/readyz', timeout=10)" 2>/dev/null \\
        && { echo "search-api-box on :8091 -> bench/${name}"; exit 0; }
    sleep 3
done
docker logs --tail 20 search-api-box >&2; exit 1
EOF
}

cmd_source() {
    [[ "$(printf 'hostname\n' | remote)" == truenas ]] || die "target is not indexhost"
    log "source: CHECKPOINT + snapshot of the build cluster, cloned to bench base (no downtime)"
    cat <<EOF | remote
set -euo pipefail
zfs list -H ${BENCH_DS}-base >/dev/null 2>&1 && { echo "bench base exists — cleanup first" >&2; exit 1; }
docker ps -q --filter name=search-job | grep -q . && { echo "a search-job container is running — try again later" >&2; exit 1; } || true
docker exec search-postgres psql -U search -d postgres -Atc "CHECKPOINT"
zfs destroy ${POOL_DS}@bench-src 2>/dev/null || true
zfs snapshot ${POOL_DS}@bench-src
zfs clone ${POOL_DS}@bench-src ${BENCH_DS}-base   # mounts at /mnt/nas/logout-bench-base (altroot /mnt)
[ -d /mnt/${BENCH_DS}-base/search/pgdata/18/docker ] || { echo "clone not mounted" >&2; exit 1; }
echo "bench base = clone of ${POOL_DS}@bench-src"
EOF
    cmd_up base >/dev/null
    log "source: keep only the build database"
    printf '%s\n' "docker exec ${PG} psql -U search -d postgres -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS serve WITH (FORCE)' -c 'DROP DATABASE IF EXISTS serve_box WITH (FORCE)' -c 'DROP DATABASE IF EXISTS news WITH (FORCE)' -c 'ALTER DATABASE search RENAME TO box'" | remote
    cmd_up base >/dev/null
    local prep; prep=$(mktemp)
    {
        cat "${SEARCH_DIR}/serve/box-profile.sql" "${SEARCH_DIR}/serve/serve-prep.sql" \
            "${SEARCH_DIR}/serve/suggest.sql" "${SEARCH_DIR}/serve/phrases.sql"
        cat <<'SQL'
-- The build-only tables go; the serving ones (serve-refresh's keep list, less subreddits,
-- plus wiki_pop for the prune profiles) stay.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
           AND tablename NOT IN ('docs', 'serve_sites', 'wiki_docs', 'wiki_aliases', 'wiki_sites',
                                 'places', 'page_docs', 'wiki_vec', 'host_nav', 'nav_sites', 'youtube_channels', 'wiki_links', 'wiki_next', 'wiki_facts', 'anchors', 'anchor_docs', 'site_meta', 'site_inlinks', 'card_images',
                                 'suggest_items', 'suggest_keys', 'suggest_top', 'suggest_phrases', 'wiki_pop') LOOP
    EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', t);
  END LOOP;
END $$;
SQL
        cat "${SEARCH_DIR}/serve/serving-indexes.sql"
        printf 'VACUUM (ANALYZE);\n'
    } > "$prep"
    _run_sql box "$prep"
    rm -f "$prep"
    cmd_down
    log "bench base ready (database box)"
}

# wait for a detached job that appends "EXIT n" to LOG (on indexhost); fail unless n = 0
_wait_log() {
    local logf="$1" what="$2" out
    while :; do
        sleep 30
        out=$(printf 'tail -n 3 %s 2>/dev/null; docker inspect -f "{{.State.Running}}" %s 2>/dev/null\n' "$logf" "$PG" | remote || true)
        grep -q '^EXIT ' <<<"$out" && break
        grep -qx false <<<"$out" && die "bench container stopped during ${what}"
    done
    grep -q '^EXIT 0$' <<<"$out" || { printf 'tail -n 20 %s\n' "$logf" | remote >&2; die "${what} failed"; }
}

cmd_dump() {
    local name="$1" file="$2"; name_ok "$name"
    [[ "$file" =~ ^[A-Za-z0-9._-]+\.dump$ ]] || die "dump file must be NAME.dump"
    cmd_up "$name" >/dev/null
    # ADR-059: no readable page text leaves indexhost. page_words() missing (box-profile.sql
    # never ran) fails the query, and any sampled body that is still prose counts.
    local left
    left=$( { printf '%s\n' "docker exec -i ${PG} psql -U search -d box -Atq -v ON_ERROR_STOP=1 <<'SQL'"
              cat "${SEARCH_DIR}/serve/no-page-text-check.sql"; printf 'SQL\n'; } | remote 2>&1 ) || true
    [[ "$left" == 0 ]] || die "bench ${name} still holds readable page text (check said: ${left:-nothing}) — rebuild it from box-bench.sh source (ADR-059)"
    log "dump: bench ${name} -> snapshots/${file}"
    cat <<EOF | remote
set -euo pipefail
mkdir -p ${ROOT}/jobs ${SNAP_DIR}
rm -f ${ROOT}/jobs/dump.log ${ROOT}/jobs/${file}
# Extensions left out: serve/deploy.sh restore creates pg_textsearch / vector / pg_trgm first.
docker exec -d ${PG} sh -c 'pg_dump -U search -d box -Fc -Z zstd:6 -T wiki_pop \\
    --exclude-extension=pg_textsearch --exclude-extension=vector --exclude-extension=pg_trgm \\
    --exclude-extension=pg_prewarm -f /jobs/${file} > /jobs/dump.log 2>&1; echo "EXIT \$?" >> /jobs/dump.log'
EOF
    _wait_log "${ROOT}/jobs/dump.log" "pg_dump of ${name}"
    cat <<EOF | remote
set -euo pipefail
mv ${ROOT}/jobs/${file} ${SNAP_DIR}/${file}
cd ${SNAP_DIR} && sha256sum ${file} > ${file}.sha256
echo "snapshots/${file}: \$(du -h --apparent-size ${file} | cut -f1), sha256 \$(cut -c1-16 ${file}.sha256)…"
EOF
}

cmd_upload() {   # the token is read here and travels on stdin — never on a command line
    local file="$1" ver="$2"
    [[ "$file" =~ ^[A-Za-z0-9._-]+$ && "$ver" =~ ^[A-Za-z0-9._-]+$ ]] || die "bad file or version"
    local tok
    tok=$(python3 -c "import yaml,os; c=yaml.safe_load(open(os.path.expanduser('~/.config/tea/config.yml'))); print([l for l in c['logins'] if l['name']=='gitea'][0]['token'])")
    [[ -n "$tok" ]] || die "no Gitea token in ~/.config/tea/config.yml"
    log "upload: snapshots/${file} -> search-snapshot/${ver}"
    { printf 'TOKEN_B64=%s\n' "$(printf 'user = "owner:%s"\n' "$tok" | base64 -w0)"; cat <<'EOF'; } | sed "s|@SNAP@|${SNAP_DIR}|g; s|@API@|${GITEA_API}|g; s|@FILE@|${file}|g; s|@VER@|${ver}|g" | remote
set -euo pipefail
cfg=$(mktemp); trap 'rm -f "$cfg"' EXIT; chmod 600 "$cfg"
echo "$TOKEN_B64" | base64 -d > "$cfg"
for f in @FILE@ @FILE@.sha256; do
    [ -f "@SNAP@/$f" ] || continue
    code=$(curl -s -K "$cfg" -o /dev/null -w '%{http_code}' --upload-file "@SNAP@/$f" "@API@/@VER@/$f")
    [ "$code" = 201 ] || { echo "upload of $f failed: HTTP $code" >&2; exit 1; }
    remote_size=$(curl -s -K "$cfg" -I "@API@/@VER@/$f" | awk 'tolower($1)=="content-length:" {print $2}' | tr -d '\r')
    local_size=$(stat -c %s "@SNAP@/$f")
    [ "$remote_size" = "$local_size" ] || { echo "$f: Gitea has $remote_size bytes, local $local_size" >&2; exit 1; }
    echo "uploaded $f ($local_size bytes, size verified)"
done
EOF
}

case "${1:-}" in
    source) cmd_source ;;
    clone)  cmd_clone "${2:?SRC}" "${3:?DST}" ;;
    up)     cmd_up "${2:?NAME}" ;;
    sql)    cmd_sql "${2:?NAME}" "${3:?FILE}" ;;
    probe)  cmd_probe "${2:?NAME}" ;;
    size)   cmd_size "${2:?NAME}" ;;
    api)    cmd_api "${2:?NAME}" ;;
    down)   cmd_down ;;
    rm)     cmd_rm "${2:?NAME}" ;;
    cleanup) cmd_cleanup ;;
    dump)   cmd_dump "${2:?NAME}" "${3:?FILE.dump}" ;;
    upload) cmd_upload "${2:?FILE}" "${3:?VERSION}" ;;
    *) sed -n '2,24p' "$0"; exit 1 ;;
esac
