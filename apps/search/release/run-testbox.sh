#!/usr/bin/env bash
# =============================================================================
# run-testbox.sh — the release export on testbox while indexhost is down (dev tooling; never shipped).
# Same stages as run-proto.sh, different source: an ONLINE BASE BACKUP of testbox's serving
# database (search-serve-postgres, db `search`) becomes the throwaway copy, because the
# profile stage rewrites its source (trims leads, drops build-only tables). The live
# database is only read, by pg_basebackup; nothing is written to it.
#
# testbox keeps one database (`search`) where indexhost had two (`serve` + `search`), and it has no
# wiki_pop or card_images. wiki_pop is rebuilt on the copy from the same open sources the
# importer always used: Wikidata sitelinks (QLever) + the Wikimedia monthly pageview dump.
# The public site's database is newer for card pictures and stocks: `pull-public` copies
# card_images, stock_tickers and stock_keys from it into the copy (pg_dump -t, read-only there).
#
#   run-testbox.sh start [RELEASE_ID]   stage code on testbox, launch the job (systemd-run)
#   run-testbox.sh pull-public          copy card_images + stocks from the public VPS into the copy
#                                   (the copy's Postgres must be up, i.e. during a run)
#   run-testbox.sh status               tail the job log and results
#   run-testbox.sh stop                 stop the job and remove its containers (copy kept)
#   run-testbox.sh clean                remove the copy and the release folder too
#
# Leaves behind on testbox: /opt/logout-release/{pg,w,code}; the release under
# /opt/logout-release/w/<RELEASE_ID>; a TEST ed25519 key under /opt/logout-release/w/testkey.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARCH_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT="release-testbox"
ROOT="/opt/logout-release"

log() { echo "$(date -u +%FT%TZ) $*" >&2; }
die() { log "ERROR: $*"; exit 1; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 deploy 'sudo -n bash -s'; }

job() {
    cat <<'JOB'
#!/usr/bin/env bash
set -euo pipefail
REL_ID="$1"
ROOT=/opt/logout-release
PGD="${ROOT}/pg"
W="${ROOT}/w"
REL="${W}/${REL_ID}"
CODE="${ROOT}/code"
RES="${W}/results-${REL_ID}.jsonl"
IMAGE_PG="logout/search-postgres:18.6-pgts1.4.0"
IMAGE_PY="logout/search-api:1"
PG=release-proto-pg          # the export's guards only allow the profile against this name
NET=release-proto-net
PV_MONTH="${PV_MONTH:-2026-08}"
log() { echo "$(date -u +%FT%TZ) $*"; }

[ -d /opt/logout ] && [ "$(hostname)" != truenas ] || { log "not testbox"; exit 1; }
mkdir -p "$W" "$W/tmp"
echo "{\"stage\": \"run\", \"host\": \"testbox\", \"stages\": \"${STAGES:-all}\", \"at\": \"$(date -u +%FT%TZ)\"}" >> "$RES"

PGPASSWORD=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' search-api | sed -n 's/^PGPASSWORD=//p')
export PGPASSWORD
[ -n "$PGPASSWORD" ] || { log "no PGPASSWORD in search-api's environment"; exit 1; }

# 1. the copy: an online base backup of the live serving database (read-only for the source)
if [ ! -f "${PGD}/18/docker/PG_VERSION" ]; then
    rm -rf "$PGD"; mkdir -p "${PGD}/18"; chown 999:999 "$PGD" "${PGD}/18"
    t0=$(date +%s)
    docker run --rm --user 999 --network container:search-serve-postgres -e PGPASSWORD \
        -v "${PGD}:/var/lib/postgresql" "$IMAGE_PG" \
        pg_basebackup -h 127.0.0.1 -U search -D /var/lib/postgresql/18/docker -X stream -c fast --max-rate=200M
    echo "{\"stage\": \"basebackup\", \"seconds\": $(( $(date +%s) - t0 )), \"bytes\": $(du -sb "$PGD" | cut -f1)}" | tee -a "$RES"
fi

# 2. a throwaway Postgres on the copy. autovacuum off: it would otherwise walk the whole copy.
docker rm -f "$PG" >/dev/null 2>&1 || true
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
rm -f "${PGD}/18/docker/postmaster.pid"
docker run -d --name "$PG" --label dbtest=safe-to-remove --network "$NET" --cpus 2 --memory 2g --shm-size 1g \
    -v "${PGD}:/var/lib/postgresql" "$IMAGE_PG" \
    postgres -c shared_preload_libraries=pg_textsearch -c shared_buffers=256MB -c autovacuum=off \
    -c max_wal_size=8GB -c work_mem=64MB -c max_parallel_workers_per_gather=0 >/dev/null
t0=$(date +%s)
until docker exec "$PG" pg_isready -U search -d search -h 127.0.0.1 >/dev/null 2>&1; do
    sleep 5
    [ "$(docker inspect -f '{{.State.Running}}' "$PG")" = true ] || { docker logs --tail 30 "$PG"; exit 1; }
    [ $(( $(date +%s) - t0 )) -lt 3600 ] || { log "Postgres not ready after 1 h"; exit 1; }
done
echo "{\"stage\": \"pg-up\", \"seconds\": $(( $(date +%s) - t0 ))}" | tee -a "$RES"

# 3. Python deps: tantivy 0.26.2 (= the T1 parity version) for the export; the two pure-Python
#    modules the popularity importer pulls in (pinned as in importer/requirements.txt), kept apart
if [ ! -d "${W}/pylib/tantivy" ]; then
    docker run --rm --user 0 --entrypoint pip -v "${W}:/w" "$IMAGE_PY" \
        install -q --no-cache-dir --target /w/pylib tantivy==0.26.2 zstandard cryptography
fi
if [ ! -d "${W}/implib/mwparserfromhell" ]; then
    docker run --rm --user 0 --entrypoint pip -v "${W}:/w" "$IMAGE_PY" \
        install -q --no-cache-dir --target /w/implib mwparserfromhell==0.7.2 tldextract==5.3.2
fi

py() {   # one export stage in its own capped container; its JSON line goes to RES
    docker run --rm --name release-proto-x --label dbtest=safe-to-remove --user 0 --network "$NET" \
        --cpus "${PY_CPUS:-2}" --memory 4g \
        --entrypoint python -e PYTHONPATH=/w/pylib -e PGPASSWORD -e TANTIVY_HEAP="${TANTIVY_HEAP:-2000000000}" \
        -e NO_PAGE_TEXT_CHECK=/code/no-page-text-check.sql -e PHRASES_SQL=/code/phrases.sql \
        -e PG_DSN="host=${PG} dbname=search user=search" -e PG_SEARCH_DSN="host=${PG} dbname=search user=search" \
        -e SQLITE_TMPDIR=/w/tmp -e LOOKUP_ONLY \
        -e RELEASE_ID="$REL_ID" -v "${W}:/w" -v "${CODE}:/code:ro" "$IMAGE_PY" "/code/${PYSCRIPT:-export.py}" "$@" | tee -a "$RES"
}
v1() { PYSCRIPT=release_v1.py py "$@"; }
pysh() {  # a bash pipeline in the export container (python deps, the registry, the Rust builder binary)
    docker run --rm --name release-proto-x --label dbtest=safe-to-remove --user 0 --network "$NET" \
        --cpus "${PY_CPUS:-2}" --memory 4g --entrypoint bash \
        -e PYTHONPATH=/w/pylib -e PGPASSWORD \
        -e NO_PAGE_TEXT_CHECK=/code/no-page-text-check.sql -e PHRASES_SQL=/code/phrases.sql \
        -e PG_DSN="host=${PG} dbname=search user=search" -e PG_SEARCH_DSN="host=${PG} dbname=search user=search" \
        -e RELEASE_ID="$REL_ID" -v "${W}:/w" -v "${CODE}:/code:ro" "$IMAGE_PY" -c "set -euo pipefail; $1" | tee -a "$RES"
}
wikibin() {  # the Rust index builder (tantivy =0.26.2, Cargo.lock committed), built once into /w/bin
    [ -x "${W}/bin/tantivy-structure" ] && return 0
    mkdir -p "${W}/bin"
    docker run --rm --cpus 2 --memory 4g -v "${CODE}/tantivy-structure:/src:ro" -v "${W}/bin:/out" rust:1.90-bookworm \
        sh -c 'set -e; cp -r /src /build; cd /build; cargo build --release --locked; cp target/release/tantivy-structure /out/'
}
imp() {  # one importer function, unchanged, against the copy (standard PG* variables). Called
         # directly: search_import.py's migrate() needs the BUILD schema, which a serving copy lacks.
    docker run --rm --name release-proto-imp --label dbtest=safe-to-remove --user 0 --network "$NET" \
        --cpus 1 --memory "${IMP_MEM:-6g}" --entrypoint python -w /code/importer \
        -e PYTHONPATH=/w/implib:/code/importer -e PGPASSWORD -e PGHOST="$PG" -e PGDATABASE=search -e PGUSER=search \
        -e TLDEXTRACT_CACHE=/w/tldcache -v "${W}:/w" -v "${CODE}:/code:ro" "$IMAGE_PY" \
        -c "import psycopg, sys; m = __import__(sys.argv[1]); c = psycopg.connect(autocommit=True); getattr(m, sys.argv[2])(c, *sys.argv[3:])" "$@"
}

STAGES="${STAGES:-sitelinks pageviews profile guard registry idhash wiki sites vectors lookup signals pictures tvstamp sqlguard catalog}"
for s in $STAGES; do
    log "stage ${s}"
    t0=$(date +%s)
    case "$s" in
        sitelinks) imp wikipop cmd_wiki_pop ;;
        pageviews) # holds every English title of the month in memory: 3 GB was OOM-killed (2026-10-02)
                 imp pageviews cmd_pageviews "$PV_MONTH"
                 docker exec "$PG" psql -U search -d search -Atc \
                     "SELECT json_build_object('stage','pageviews','rows',count(*),'with_views',count(pageviews),'sitelinks_10plus',count(*) FILTER (WHERE sitelinks >= 10)) FROM wiki_pop" \
                     | tee -a "$RES" ;;
        profile) py profile /code/release-profile.sql ;;
        guard)   py guard ;;
        # text/wiki: the Rust builder, store nothing (doc_id = the registry's), so registry runs first.
        # wiki-py is the old Python writer (stores page_id, title and a snippet), kept working.
        wiki)    [ -f "${REL}/registry.sqlite" ] || { log "stage wiki needs ${REL}/registry.sqlite: run the registry stage first"; exit 1; }
                 rm -rf "${REL}/text/wiki" "${REL}/text/wiki.parts"; wikibin
                 pysh "python /code/release_v1.py wikiinput /w/${REL_ID}/registry.sqlite | /w/bin/tantivy-structure build /w/${REL_ID}/text/wiki --stop /code/english.stop --jobs 2 --chunk 150000 --heap-mb 900"
                 v1 tvnormalize "/w/${REL_ID}/text/wiki" "$REL_ID" wiki /code/english.stop schema=store-nothing builder=tantivy-structure ;;
        wiki-py) rm -rf "${REL}/text/wiki";  py wiki  "/w/${REL_ID}/text/wiki" /code/english.stop ;;
        sites)   rm -rf "${REL}/text/sites"; py sites "/w/${REL_ID}/text/sites" /code/english.stop ;;
        vectors) rm -rf "${REL}/vectors";    py vectors "/w/${REL_ID}/vectors/raw" ;;
        lookup)  rm -rf "${REL}"/lookup*.sqlite "${REL}/card-images.sqlite" "${REL}/_research"
                 py lookup "/w/${REL_ID}" ;;
        lookup2) # T4's builder (tools/sqlite_lookups/build.py) with T5's registry: the single lookup builder
                 rm -rf "${REL}/lookup2"
                 docker run --rm --name release-proto-x --label dbtest=safe-to-remove --user 0 --network "$NET" \
                     --cpus "${PY_CPUS:-2}" --memory 4g --entrypoint python -e PYTHONPATH=/w/pylib -e PGHOST="$PG" \
                     -e PGDATABASE=search -e PGUSER=search -e PGPASSWORD -e SQLITE_TMPDIR=/w/tmp \
                     -v "${W}:/w" -v "${CODE}/sqlite_lookups:/code:ro" -w /code "$IMAGE_PY" /code/build.py \
                     "/w/${REL_ID}/lookup2" --registry "/w/${REL_ID}/registry.sqlite" --release-id "$REL_ID" | tee -a "$RES" ;;
        registry) v1 registry "/w/${REL_ID}/registry.sqlite" "$REL_ID" ${PREV_REGISTRY:+--previous "$PREV_REGISTRY"} ;;
        idhash)   v1 idhash "/w/${REL_ID}/registry.sqlite" ;;
        signals)  v1 signals "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/signals.bin" "$REL_ID" ;;
        pictures) rm -f "${REL}/card-images.sqlite"
                  v1 pictures "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/pictures.sqlite" "$REL_ID" ;;
        tvstamp)  for d in text/wiki text/sites/sites text/sites/pages text/sites/anchors; do
                      v1 tvnormalize "/w/${REL_ID}/${d}" "$REL_ID" "${d#text/}" /code/english.stop
                  done ;;
        sqlguard) mapfile -t dbs < <(cd "$W" && ls "${REL_ID}"/*.sqlite | sed 's|^|/w/|')
                  v1 sqlguard "${dbs[@]}" ;;
        catalog) py catalog "/w/catalog-${REL_ID}.json" ;;
        *)       log "unknown stage ${s}"; exit 1 ;;
    esac
    echo "{\"stage\": \"done-${s}\", \"seconds\": $(( $(date +%s) - t0 )), \"load\": \"$(cut -d' ' -f1-3 /proc/loadavg)\"}" >> "$RES"
done

# 4. the throwaway Postgres goes; the copy stays for reuse
docker stop -t 120 "$PG" >/dev/null; docker rm -f "$PG" >/dev/null
docker network rm "$NET" >/dev/null 2>&1 || true
NET=none

[ "${NO_MANIFEST:-0}" = 1 ] && { log "stages done (NO_MANIFEST=1)"; exit 0; }
# 5. manifest signed with a TEST key (generated here; never the production manifest key)
mkdir -p "${W}/testkey"
if [ ! -f "${W}/testkey/test-ed25519.pem" ]; then
    openssl genpkey -algorithm ed25519 -out "${W}/testkey/test-ed25519.pem"
    openssl pkey -in "${W}/testkey/test-ed25519.pem" -pubout -out "${W}/testkey/test-ed25519.pub"
    chmod 600 "${W}/testkey/test-ed25519.pem"
fi
cp "${CODE}/english.stop" "${REL}/english.stop"
py manifest "/w/${REL_ID}" "$REL_ID" /code/english.stop /w/testkey/test-ed25519.pem
py verify "/w/${REL_ID}" /w/testkey/test-ed25519.pub
v1 check "/w/${REL_ID}" /w/testkey/test-ed25519.pub
[ "${SKIP_ZSTD:-0}" = 1 ] || py zstd "/w/${REL_ID}" "/w/zstd-${REL_ID}.json"
du -sb "${REL}" "${REL}"/* "${REL}"/text/* | tee -a "${W}/du-${REL_ID}.txt"
log "release ${REL_ID}: DONE"
JOB
}

cmd_start() {
    local rel="${1:-proto-$(date +%Y%m%d)-testbox}"
    [[ "$rel" =~ ^proto-[0-9]{8}[a-z0-9-]*$ ]] || die "release id must look like proto-YYYYMMDD[-x]"
    log "staging code on testbox and launching unit ${UNIT} for ${rel}"
    # code: the release scripts + the importer + the two SQL files the export reads
    tar -C "${SEARCH_DIR}" -czf - release/export.py release/release_v1.py release/registry_idents.py release/english.stop \
        release/release-profile.sql serve/no-page-text-check.sql serve/phrases.sql importer tools/sqlite_lookups \
        tools/tantivy-structure/Cargo.toml tools/tantivy-structure/Cargo.lock tools/tantivy-structure/src/main.rs \
        | ssh -o BatchMode=yes deploy "sudo -n bash -c 'rm -rf ${ROOT}/code.new && mkdir -p ${ROOT}/code.new && tar -C ${ROOT}/code.new -xzf -'"
    cat <<EOF | remote
set -euo pipefail
systemctl is-active --quiet ${UNIT} && { echo "${UNIT} is already running" >&2; exit 1; }
rm -rf ${ROOT}/code && mkdir -p ${ROOT}/code
cp ${ROOT}/code.new/release/* ${ROOT}/code/
cp ${ROOT}/code.new/serve/no-page-text-check.sql ${ROOT}/code.new/serve/phrases.sql ${ROOT}/code/
cp -r ${ROOT}/code.new/importer ${ROOT}/code/importer
cp -r ${ROOT}/code.new/tools/tantivy-structure ${ROOT}/code/tantivy-structure
cp -r ${ROOT}/code.new/tools/sqlite_lookups ${ROOT}/code/sqlite_lookups
rm -rf ${ROOT}/code.new
echo '$(job | base64 -w0)' | base64 -d > ${ROOT}/job.sh
systemctl reset-failed ${UNIT} 2>/dev/null || true
systemd-run --unit ${UNIT} --collect --property=StandardOutput=append:${ROOT}/job.log \
    --property=StandardError=append:${ROOT}/job.log \
    --setenv=STAGES="${STAGES:-}" --setenv=NO_MANIFEST="${NO_MANIFEST:-0}" --setenv=PV_MONTH="${PV_MONTH:-2026-08}" --setenv=PREV_REGISTRY="${PREV_REGISTRY:-}" \
    bash ${ROOT}/job.sh "${rel}"
echo "launched ${UNIT} (${rel}); log ${ROOT}/job.log"
EOF
}

cmd_pull_public() {   # newer tables from the public site's database into the copy
    local pub pw
    pub="${SEARCH_PUBLIC_SSH:-$(cat ~/.config/logout/search-public-ssh)}"
    pw=$(ssh -o BatchMode=yes deploy "sudo -n docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' search-api" \
        | sed -n 's/^PGPASSWORD=//p')
    [ -n "$pw" ] || die "no PGPASSWORD from testbox's search-api"
    for t in "stock_tickers stock_keys" card_images; do
        log "copying ${t} from ${pub}"
        # shellcheck disable=SC2086,SC2046
        ssh -o BatchMode=yes "$pub" "sudo -n docker exec search-public-pg pg_dump -U search -d search -Fc -Z0 $(printf -- '-t %s ' $t)" \
            | ssh -o BatchMode=yes deploy "sudo -n docker exec -i -e PGPASSWORD=${pw} release-proto-pg pg_restore -U search -d search --clean --if-exists --no-owner --single-transaction"
    done
}

cmd_status() {
    cat <<EOF | remote
systemctl is-active ${UNIT} || true
tail -n 25 ${ROOT}/job.log 2>/dev/null || true
echo ---; tail -n 15 ${ROOT}/w/results-*.jsonl 2>/dev/null || true
docker ps --filter name=release-proto --format '{{.Names}}\t{{.Status}}'
EOF
}

cmd_stop() {
    cat <<EOF | remote
systemctl stop ${UNIT} 2>/dev/null || true
docker rm -f release-proto-x release-proto-imp release-proto-pg >/dev/null 2>&1 || true
docker network rm release-proto-net >/dev/null 2>&1 || true
echo stopped
EOF
}

cmd_clean() {
    cmd_stop
    echo "rm -rf ${ROOT}/pg ${ROOT}/w; echo cleaned" | remote
}

case "${1:-}" in
    start)  shift; cmd_start "$@" ;;
    pull-public) cmd_pull_public ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    clean)  cmd_clean ;;
    *)      echo "usage: $0 {start [RELEASE_ID]|pull-public|status|stop|clean}" >&2; exit 1 ;;
esac
