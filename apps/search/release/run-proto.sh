#!/usr/bin/env bash
# =============================================================================
# run-proto.sh — the T5 release-export PROTOTYPE on indexhost (dev tooling; never shipped).
# docs/internal/SEARCH-HOME-ENGINE-PLAN.md gate T5, ADR-063. Runs export.py against a
# throwaway Postgres on a ZFS CLONE of nas/logout, so the live search database is never
# read: big reads of a second copy evict live `serve` from the shared ARC
# (runbook/memory: lesson-indexhost-bench-evicts-live-search). The clone gets
# primarycache=metadata at creation, so neither its reads nor the export's writes land in
# the ARC data cache.
#
#   run-proto.sh start [RELEASE_ID]   stage export.py on indexhost, launch the job (systemd-run)
#   run-proto.sh status               tail the job log and its results
#   run-proto.sh stop                 stop the job and remove its containers (clone kept)
#
# Leaves behind: dataset nas/logout-release-proto (+ snapshot nas/logout@release-proto-*),
# reusable; the release under /mnt/nas/logout-release-proto/release-proto/<RELEASE_ID>;
# a TEST ed25519 key under .../release-proto/testkey (never the production manifest key).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARCH_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
JUMP="${SEARCH_JUMP_HOST:-user@jump-host}"
TARGET="${SEARCH_TARGET_HOST:-user@index-host}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15"
UNIT="release-proto"
DS="nas/logout-release-proto"
W="/mnt/${DS}/release-proto"

log() { echo "$(date -u +%FT%TZ) $*" >&2; }
die() { log "ERROR: $*"; exit 1; }
remote() {   # a bash script on stdin, as root on indexhost (/tmp is noexec: bash -s, not ./x)
    # shellcheck disable=SC2029
    ssh ${SSH_OPTS} "${JUMP}" "ssh ${SSH_OPTS} ${TARGET} 'sudo -n bash -s'"
}

# The job itself (runs on indexhost as root, detached under systemd-run).
job() {
    cat <<'JOB'
#!/usr/bin/env bash
set -euo pipefail
REL_ID="$1"
SRC_DS=nas/logout
DS=nas/logout-release-proto
SNAP="${SRC_DS}@release-proto-${REL_ID#proto-}"
MNT="/mnt/${DS}"
W="${MNT}/release-proto"
REL="${W}/${REL_ID}"
CODE=/root/release-proto/code
RES="${W}/results-${REL_ID}.jsonl"
IMAGE_PG="logout/search-postgres:18.6-pgts1.4.0"
IMAGE_PY="logout/search-api:1"
PG=release-proto-pg
NET=release-proto-net
log() { echo "$(date -u +%FT%TZ) $*"; }

[ "$(hostname)" = truenas ] || { log "not indexhost"; exit 1; }
if docker ps --format '{{.Names}}' | grep -Eq '^(search-job|search-boxbuild|search-bench-pg)'; then
    log "another search build/bench container is running — try later"; exit 1
fi

arc() { awk '$1=="size"{printf "%.1f", $3/2^30}' /proc/spl/kstat/zfs/arcstats; }
# Live search latency, probed from inside search-api (its allow list refuses the host).
# Wall time of /search for the fixed latency query set; a READ of the live API only.
probe() {
    local tag="$1" out
    PROBE_K=$(( ${PROBE_K:-11} + 1 )); [ "$PROBE_K" -le 20 ] || PROBE_K=12   # a fresh k per probe (cache keys on k; MAX_K 20)
    out=$(docker exec search-api python -c '
import base64, json, sys, time, urllib.parse, urllib.request
qs = base64.b64decode(sys.argv[1]).decode().split("\n"); k = sys.argv[2]; salt = int(time.time()) % 9973
# a letter-case pattern unique to this probe: the result cache keys on the raw query, so no probe
# is ever answered from it (search treats the cases alike)
qs = ["".join(c.upper() if (i * 7 + salt) % 5 == 0 else c for i, c in enumerate(q)) for q in qs]
ms = []
for q in qs:
    t = time.monotonic()
    try:
        urllib.request.urlopen("http://127.0.0.1:8090/search?k=" + k + "&q=" + urllib.parse.quote(q), timeout=10).read()
    except Exception:
        pass
    ms.append((time.monotonic() - t) * 1000)
ms.sort(); n = len(ms)
print(json.dumps({"k": int(k), "n": n, "p50_ms": round(ms[n // 2]), "p90_ms": round(ms[int(n * .9)]), "max_ms": round(ms[-1])}))
' "$(grep -v '^#' "${CODE}/latency-queries.txt" | grep . | base64 -w0)" "$PROBE_K" 2>/dev/null || echo '{"error": "probe failed"}')
    echo "{\"stage\": \"live-probe\", \"at\": \"${tag}\", \"arc_gb\": $(arc), \"io_some_avg60\": $(awk -F'[= ]' '/^some/{print $5}' /proc/pressure/io), \"live\": ${out}}" | tee -a "$RES"
}

log "release ${REL_ID}: start (results -> ${RES})"

# 1. the clone (instant, copy-on-write; data cache off from birth)
if ! zfs list -H "$DS" >/dev/null 2>&1; then
    zfs snapshot "$SNAP"
    zfs clone -o primarycache=metadata -o secondarycache=none -o atime=off "$SNAP" "$DS"
    log "cloned ${SNAP} -> ${DS}"
fi
[ "$(zfs get -H -o value primarycache "$DS")" = metadata ] || { log "clone primarycache is not metadata"; exit 1; }
mkdir -p "$W" "$W/tmp"
echo "{\"stage\": \"run\", \"stages\": \"${STAGES:-all}\", \"at\": \"$(date -u +%FT%TZ)\"}" >> "$RES"
probe before

# 2. a throwaway Postgres on the clone's data directory (crash recovery replays the WAL
#    into the clone only). autovacuum off: it would otherwise walk the whole clone.
docker rm -f "$PG" >/dev/null 2>&1 || true
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
rm -f "${MNT}/search/pgdata/18/docker/postmaster.pid"
docker run -d --name "$PG" --network "$NET" --cpus 2 --memory 2g --shm-size 1g \
    -v "${MNT}/search/pgdata:/var/lib/postgresql" "$IMAGE_PG" \
    postgres -c shared_preload_libraries=pg_textsearch -c shared_buffers=256MB -c autovacuum=off \
    -c max_wal_size=8GB -c work_mem=64MB -c max_parallel_workers_per_gather=0 >/dev/null
t0=$(date +%s)
until docker exec "$PG" pg_isready -U search -d serve -h 127.0.0.1 >/dev/null 2>&1; do
    sleep 5
    [ "$(docker inspect -f '{{.State.Running}}' "$PG")" = true ] || { docker logs --tail 30 "$PG"; exit 1; }
    [ $(( $(date +%s) - t0 )) -lt 3600 ] || { log "Postgres not ready after 1 h"; exit 1; }
done
echo "{\"stage\": \"pg-up\", \"seconds\": $(( $(date +%s) - t0 ))}" | tee -a "$RES"

# 3. Python deps on the clone (tantivy 0.26.2 = the T1 parity version)
PGPASSWORD=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' search-api | sed -n 's/^PGPASSWORD=//p')
export PGPASSWORD
[ -n "$PGPASSWORD" ] || { log "no PGPASSWORD in search-api's environment"; exit 1; }
if [ ! -d "${W}/pylib/tantivy" ]; then
    docker run --rm --user 0 --entrypoint pip -v "${W}:/w" "$IMAGE_PY" \
        install -q --no-cache-dir --target /w/pylib tantivy==0.26.2 zstandard cryptography
fi

PYNET="$NET"
py() {   # one export stage in its own capped container; its JSON line goes to RES
    docker run --rm --name release-proto-x --user 0 --network "$PYNET" --cpus 4 --memory 4g \
        --entrypoint python -e PYTHONPATH=/w/pylib -e PGPASSWORD -e TANTIVY_HEAP="${TANTIVY_HEAP:-2000000000}" \
        -e NO_PAGE_TEXT_CHECK=/code/no-page-text-check.sql -e PHRASES_SQL=/code/phrases.sql \
        -e PG_DSN="host=${PG} dbname=${PYDB:-serve} user=search" -e PG_SEARCH_DSN="host=${PG} dbname=search user=search" \
        -e SQLITE_TMPDIR=/w/tmp -e LOOKUP_ONLY \
        -e RELEASE_ID="$REL_ID" -v "${W}:/w" -v "${CODE}:/code:ro" "$IMAGE_PY" "/code/${PYSCRIPT:-export.py}" "$@" | tee -a "$RES"
}
v1() { PYSCRIPT=release_v1.py py "$@"; }   # release format v1 parts (registry, signals, pictures, stamps)

STAGES="${STAGES:-profile guard wiki sites vectors lookup catalog}"
for s in $STAGES; do
    log "stage ${s}"
    case "$s" in
        profile) py profile /code/release-profile.sql ;;
        guard)   py guard ;;
        wiki)    rm -rf "${REL}/text/wiki";  py wiki  "/w/${REL_ID}/text/wiki" /code/english.stop ;;
        sites)   rm -rf "${REL}/text/sites"; py sites "/w/${REL_ID}/text/sites" /code/english.stop ;;
        vectors) rm -rf "${REL}/vectors";    py vectors "/w/${REL_ID}/vectors/raw" ;;
        lookup)  rm -rf "${REL}"/lookup*.sqlite "${REL}/card-images.sqlite" "${REL}/_research"
                 py lookup "/w/${REL_ID}" ;;
        removed) py removed "/w/removed-${REL_ID}.json" ;;
        # ---- release format v1 (docs/internal/SEARCH-RELEASE-FORMAT.md)
        registry) v1 registry "/w/${REL_ID}/registry.sqlite" "$REL_ID" ;;
        signals)  v1 signals "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/signals.bin" "$REL_ID" ;;
        pictures) rm -f "${REL}/card-images.sqlite"
                  v1 pictures "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/pictures.sqlite" "$REL_ID" ;;
        # web index inputs: sites/pages/anchors .rows.tsv.zst for tools/tantivy-structure/local-build.sh, and
        # the exact_anchor table anchor-phrases.sqlite; both need registry.sqlite from the registry stage
        webinput) v1 web-input "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/web-input" "$REL_ID" ;;
        anchorphrases) v1 anchor-phrases "/w/${REL_ID}/registry.sqlite" "/w/${REL_ID}/anchor-phrases.sqlite" "$REL_ID" ;;
        tvstamp)  for d in text/wiki text/sites/sites text/sites/pages text/sites/anchors; do
                      v1 tvnormalize "/w/${REL_ID}/${d}" "$REL_ID" "${d#text/}" /code/english.stop
                  done ;;
        repro)    # build the deterministic parts a second time and compare bytes
                  rm -rf "${W}/repro" && mkdir -p "${W}/repro"
                  v1 registry /w/repro/registry.sqlite "$REL_ID"
                  v1 signals /w/repro/registry.sqlite /w/repro/signals.bin "$REL_ID"
                  v1 pictures /w/repro/registry.sqlite /w/repro/pictures.sqlite "$REL_ID"
                  # lookup-wiki (10 GB, ~50 min of reads) is left out of the repeat while indexhost is shared
                  LOOKUP_ONLY=lookup-sites.sqlite,lookup-places-osm.sqlite,lookup-phrases.sqlite,lookup-unsafe-ut1.sqlite \
                      RELEASE_ID="$REL_ID" py lookup /w/repro
                  v1 tvrepro /code/english.stop /w/repro
                  v1 sha "/w/${REL_ID}/registry.sqlite" /w/repro/registry.sqlite "/w/${REL_ID}/signals.bin" \
                      /w/repro/signals.bin "/w/${REL_ID}/pictures.sqlite" /w/repro/pictures.sqlite \
                      "/w/${REL_ID}/lookup-sites.sqlite" \
                      /w/repro/lookup-sites.sqlite "/w/${REL_ID}/lookup-places-osm.sqlite" /w/repro/lookup-places-osm.sqlite \
                      "/w/${REL_ID}/lookup-phrases.sqlite" /w/repro/lookup-phrases.sqlite \
                      "/w/${REL_ID}/lookup-unsafe-ut1.sqlite" /w/repro/lookup-unsafe-ut1.sqlite ;;
        idhash)   v1 idhash "/w/${REL_ID}/registry.sqlite" ;;
        storemap) v1 storemap "/w/${REL_ID}" ;;
        # ---- monthly delta simulation: snapshot first, ROLLBACK after (MONTH_ROLLBACK=1)
        snap)     zfs list -H -t snapshot "${DS}@pre-month" >/dev/null 2>&1 && { log "snapshot exists"; exit 1; }
                  zfs snapshot "${DS}@pre-month" && log "snapshot ${DS}@pre-month" ;;
        monthtext)
                  py profile /code/month-text.sql
                  rm -rf "${W}/month" && mkdir -p "${W}/month"
                  v1 registry /w/month/registry.sqlite "${REL_ID}-m1" --previous "/w/${REL_ID}/registry.sqlite"
                  v1 signals /w/month/registry.sqlite /w/month/signals.bin "${REL_ID}-m1"
                  RELEASE_ID="${REL_ID}-m1" py lookup /w/month
                  cp -a "${REL}/text/wiki" "${W}/month/wiki"
                  v1 tvmonth /w/month/wiki /code/english.stop
                  v1 rowdiff "/w/${REL_ID}/registry.sqlite" /w/month/registry.sqlite
                  v1 rowdiff "/w/${REL_ID}/lookup-wiki.sqlite" /w/month/lookup-wiki.sqlite
                  v1 rowdiff "/w/${REL_ID}/lookup-sites.sqlite" /w/month/lookup-sites.sqlite ;;
        monthsig)
                  py profile /code/month-signals.sql
                  PYDB=search py profile /code/month-signals-search.sql
                  rm -rf "${W}/month2" && mkdir -p "${W}/month2"
                  v1 signals /w/month/registry.sqlite /w/month2/signals.bin "${REL_ID}-m2"
                  RELEASE_ID="${REL_ID}-m2" py lookup /w/month2
                  v1 rowdiff /w/month/lookup-sites.sqlite /w/month2/lookup-sites.sqlite
                  v1 rowdiff /w/month/lookup-wiki.sqlite /w/month2/lookup-wiki.sqlite
                  v1 sha /w/month/signals.bin /w/month2/signals.bin ;;
        sqlguard) mapfile -t dbs < <(cd "$W" && ls "${REL_ID}"/*.sqlite | sed 's|^|/w/|')
                  v1 sqlguard "${dbs[@]}" ;;
        catalog) py catalog "/w/catalog-${REL_ID}.json" ;;
    esac
    probe "after-${s}"
done

# 4. the throwaway Postgres goes; the clone stays for reuse
docker stop -t 120 "$PG" >/dev/null; docker rm -f "$PG" >/dev/null
docker network rm "$NET" >/dev/null 2>&1 || true
PYNET=none
if [ "${MONTH_ROLLBACK:-0}" = 1 ]; then
    # the month runs changed the clone and wrote into it: keep the results, then roll it back
    py zstd /w/month "/w/zstd-month-${REL_ID}.json" || true
    cp "$RES" "/root/release-proto/results-${REL_ID}-month.jsonl"
    cp "${W}/zstd-month-${REL_ID}.json" /root/release-proto/ 2>/dev/null || true
    zfs rollback -r "${DS}@pre-month" && zfs destroy "${DS}@pre-month" && log "clone rolled back to pre-month"
    exit 0
fi

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
probe end
log "release ${REL_ID}: DONE"
JOB
}

cmd_start() {
    local rel="${1:-proto-$(date +%Y%m%d)}"
    [[ "$rel" =~ ^proto-[0-9]{8}[a-z0-9-]*$ ]] || die "release id must look like proto-YYYYMMDD[-x]"
    local b_m1 b_m2 b_m3 b_export b_v1 b_ri b_stop b_q b_chk b_prof b_phr b_job
    b_export=$(base64 -w0 < "${SCRIPT_DIR}/export.py")
    b_v1=$(base64 -w0 < "${SCRIPT_DIR}/release_v1.py")
    b_ri=$(base64 -w0 < "${SCRIPT_DIR}/registry_idents.py")
    b_stop=$(base64 -w0 < "${SCRIPT_DIR}/english.stop")
    b_q=$(base64 -w0 < "${SEARCH_DIR}/tools/latency-queries.txt")
    b_chk=$(base64 -w0 < "${SEARCH_DIR}/serve/no-page-text-check.sql")
    b_prof=$(base64 -w0 < "${SCRIPT_DIR}/release-profile.sql")
    b_phr=$(base64 -w0 < "${SEARCH_DIR}/serve/phrases.sql")
    b_m1=$(base64 -w0 < "${SCRIPT_DIR}/month-text.sql")
    b_m2=$(base64 -w0 < "${SCRIPT_DIR}/month-signals.sql")
    b_m3=$(base64 -w0 < "${SCRIPT_DIR}/month-signals-search.sql")
    b_job=$(job | base64 -w0)
    log "staging export.py and launching unit ${UNIT} for ${rel}"
    cat <<EOF | remote
set -euo pipefail
systemctl is-active --quiet ${UNIT} && { echo "${UNIT} is already running" >&2; exit 1; }
mkdir -p /root/release-proto/code
echo '${b_export}' | base64 -d > /root/release-proto/code/export.py
echo '${b_v1}' | base64 -d > /root/release-proto/code/release_v1.py
echo '${b_ri}' | base64 -d > /root/release-proto/code/registry_idents.py
echo '${b_stop}' | base64 -d > /root/release-proto/code/english.stop
echo '${b_q}' | base64 -d > /root/release-proto/code/latency-queries.txt
echo '${b_chk}' | base64 -d > /root/release-proto/code/no-page-text-check.sql
echo '${b_prof}' | base64 -d > /root/release-proto/code/release-profile.sql
echo '${b_phr}' | base64 -d > /root/release-proto/code/phrases.sql
echo '${b_m1}' | base64 -d > /root/release-proto/code/month-text.sql
echo '${b_m2}' | base64 -d > /root/release-proto/code/month-signals.sql
echo '${b_m3}' | base64 -d > /root/release-proto/code/month-signals-search.sql
echo '${b_job}' | base64 -d > /root/release-proto/job.sh
systemctl reset-failed ${UNIT} 2>/dev/null || true
systemd-run --unit ${UNIT} --collect --setenv=STAGES="${STAGES:-profile guard wiki sites vectors lookup catalog}" \
    --setenv=TANTIVY_HEAP="${TANTIVY_HEAP:-2000000000}" \
    --setenv=SKIP_ZSTD="${SKIP_ZSTD:-0}" --setenv=MONTH_ROLLBACK="${MONTH_ROLLBACK:-0}" \
    bash -c 'bash /root/release-proto/job.sh ${rel} > /root/release-proto/job-${rel}.log 2>&1'
echo "launched ${UNIT}: log /root/release-proto/job-${rel}.log"
EOF
}

cmd_status() {
    cat <<EOF | remote
systemctl is-active ${UNIT} 2>/dev/null || true
f=\$(ls -t /root/release-proto/job-*.log 2>/dev/null | head -1); [ -n "\$f" ] && tail -n 25 "\$f"
EOF
}

cmd_stop() {
    cat <<EOF | remote
systemctl stop ${UNIT} 2>/dev/null || true
docker stop -t 120 release-proto-pg >/dev/null 2>&1 || true
docker rm -f release-proto-x release-proto-pg >/dev/null 2>&1 || true
docker network rm release-proto-net >/dev/null 2>&1 || true
echo "stopped; dataset ${DS} kept"
EOF
}

case "${1:-}" in
    start)  shift; cmd_start "$@" ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    *) echo "usage: $0 start [RELEASE_ID] | status | stop" >&2; exit 2 ;;
esac
