#!/usr/bin/env bash
# =============================================================================
# sq-engine.sh — the search engine from a RELEASE COPY, in podman on this workstation
# =============================================================================
# Every arm of the search-quality epic measures against an engine started here, never
# against indexhost, testbox or homeserver. CPU only. The container is the same shape as
# tools/engine-ab/arms-rc7.sh and ~/search-eval/arm.sh: the engine image with the API code
# under test mounted over /app and the release copy mounted read-only at /rel.
#
#   sq-engine.sh up <release-dir> [--port N] [--mem 4g] [--issue N] [--api DIR] [--name NAME] [--image IMG] [--news FILE] [--env K=V ...]
#   sq-engine.sh down [--name NAME | --port N]
#   sq-engine.sh status
#
#   --port N    host port (default 18484); the sidecar sq-eval.py reads is keyed by it
#   --mem SIZE  the container's memory cap (default 4g, the box budget; never uncapped). --cap4 = --mem 4g.
#               CPUs: SQ_CPUS (default 4; the owner's workstation caps of 2026-10-14 ask for at most 6, the sweep runs 3).
#   --issue N   the Gitea issue this engine runs for, written into the lock (default: SQ_ISSUE, else the
#               number in this checkout's branch name feat/sq-<N>-...)
#   --api DIR   the API code to run (default: this checkout's apps/search/serve/api)
#   --name      container suffix (default: the release dir's basename); container = sq-<name>
#   --image     engine image (default dbt-search-api:engine-rc4 — tantivy, onnxruntime, the models)
#   --news FILE a news.sqlite for the news box (default ~/search-eval/news.sqlite when present)
#   --env K=V   one more engine setting for an arm (repeatable; e.g. a constant under test). It comes after the
#               defaults, so it can replace one, and it is part of the build hash
#
# `up` writes ~/.local/share/logout-search/sq/engines/<port>.json: the release id, the API dir
# and a BUILD hash (sha256 of every /app *.py + the release manifest, + the --env settings and
# /app/ranker.json when --env is used; 12 hex). sq-eval.py and
# sq-candidates.py key their caches on that build, so a re-run after a code change re-asks
# only what the build hash says moved.
#
# ONE ENGINE ON THIS WORKSTATION AT A TIME (owner rule after 2026-10-12 20:34: four lanes each ran a 4 GB+
# engine on the 18 GB workstation and the kernel OOM-killed the desktop session and every lane with it).
#   - `up` takes an exclusive flock on ~/.cache/logout/sq-engine.lock and holds it for as long as the engine
#     is up; `down` releases it. If another lane holds it, `up` WAITS and prints who (the lock file names the
#     issue and the container). A holder whose engine died releases by itself.
#   - the lock is given back only when the engine's container is GONE, not when it stops answering: `down`
#     waits for the removal (SQ_GONE_WAIT seconds, default 30) and the holder keeps the flock until then.
#     With the lock in hand `up` still waits that long for every sq-* container to be gone and refuses,
#     naming them, if one is left (a dead engine nobody took down: `down --name` it).
#   - every engine is memory-capped (4g unless --mem), and `up` refuses when `free -m` shows under 6 GB available.
# So: `up`, measure, `down` promptly — other lanes are queued behind you.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SQ_DIR="${SQ_DIR:-$HOME/.local/share/logout-search/sq}"
ENGINES="$SQ_DIR/engines"
DEFAULT_IMAGE="${SQ_IMAGE:-dbt-search-api:engine-rc4}"
DEFAULT_PORT=18484
READY_WAIT="${SQ_READY_WAIT:-300}"
LOCK="${SQ_LOCK:-$HOME/.cache/logout/sq-engine.lock}"
GONE_WAIT="${SQ_GONE_WAIT:-30}"          # seconds to wait for a container to be gone before giving up
MIN_FREE_MB=6000

log() { printf '%s\n' "$*" >&2; }
die() { log "sq-engine: $*"; exit 1; }

build_hash() {  # build_hash API_DIR RELEASE_DIR [K=V ...] -> 12 hex over the API's *.py, the release manifest, the settings
    local api=$1 rel=$2
    shift 2
    {
        find "$api" -maxdepth 1 -name '*.py' -type f | LC_ALL=C sort | xargs cat
        cat "$rel/manifest.json"
        if (( $# )); then      # an arm with its own settings: they, and the ranker file one of them may point at, are the build
            printf '%s\n' "$@" | LC_ALL=C sort
            if [[ -f "$api/ranker.json" ]]; then cat "$api/ranker.json"; fi
        fi
    } | sha256sum | cut -c1-12
}

release_id() {  # the release's own name from its manifest, else the dir's basename
    python3 - "$1" <<'PY'
import json, os, sys
root = sys.argv[1]
m = json.load(open(os.path.join(root, "manifest.json")))
for k in ("release_id", "release", "id", "name"):
    v = m.get(k)
    if isinstance(v, str) and v:
        print(v); break
else:
    print(os.path.basename(root.rstrip("/")))
PY
}

hold_file() { printf '%s.hold.%s' "$LOCK" "$1"; }

# The lock outlives `up`: a detached holder takes the flock, writes who holds it into the lock file, and keeps
# it until `down` removes the hold file, the engine it was taken for stops, or `up` died before starting one —
# and in every case until that engine's container no longer exists (at most GONE_WAIT seconds): a container that
# is still stopping holds its port and its memory, and the next `up` is waiting on this flock.
lock_take() {  # lock_take CONTAINER ISSUE
    local ctr=$1 issue=$2 hold waited=0
    hold=$(hold_file "$ctr")
    mkdir -p "$(dirname "$LOCK")"
    : > "$hold"
    LOCK="$LOCK" HOLD="$hold" CTR="$ctr" ISSUE="$issue" GONE_WAIT="$GONE_WAIT" UP_PID=$$ setsid nohup bash -c '
        exec 9>>"$LOCK"
        flock -x 9
        [ -e "$HOLD" ] || exit 0
        who=$(printf "issue=#%s container=%s since=%s holder_pid=%s" "$ISSUE" "$CTR" "$(date +%Y-%m-%dT%H:%M:%S)" "$$")
        echo "$who" > "$LOCK"
        started=0
        while [ -e "$HOLD" ]; do
            # a waiter that opens the lock file for writing empties it (seen 2026-10-12): say again who holds it
            grep -q "container=$CTR " "$LOCK" 2>/dev/null || echo "$who" > "$LOCK"
            if [ "$(podman inspect -f "{{.State.Status}}" "$CTR" 2>/dev/null)" = running ]; then started=1
            elif [ "$started" = 1 ]; then break                      # the engine stopped or was killed
            elif ! kill -0 "$UP_PID" 2>/dev/null; then break         # `up` died before it started one
            fi
            sleep 2
        done
        t=0
        while podman container exists "$CTR" 2>/dev/null && [ "$t" -lt "$GONE_WAIT" ]; do sleep 1; t=$((t + 1)); done
        rm -f "$HOLD"
        : > "$LOCK"' >/dev/null 2>&1 &
    while ! grep -q "container=$ctr " "$LOCK" 2>/dev/null; do
        [[ -e "$hold" ]] || die "the lock holder for $ctr went away"
        if (( waited % 30 == 0 )); then
            log "waiting for the engine lock ($LOCK), held by: $(cat "$LOCK" 2>/dev/null || true)"
        fi
        sleep 1; waited=$((waited + 1))
    done
    log "engine lock taken: $(cat "$LOCK")"
}

lock_release() {  # lock_release CONTAINER
    local ctr=$1 t=0
    rm -f "$(hold_file "$ctr")"
    while grep -q "container=$ctr " "$LOCK" 2>/dev/null && (( t < 15 )); do sleep 1; t=$((t + 1)); done
}

sq_containers() {  # every sq-* container in any state, as "name (state)" lines
    podman ps -a --filter name='^sq-' --format '{{.Names}} ({{.State}})' 2>/dev/null || true
}

wait_gone() {  # wait_gone CONTAINER -> 0 once it no longer exists, 1 if it is still there after GONE_WAIT seconds
    local t=0
    while podman container exists "$1" 2>/dev/null; do
        (( t < GONE_WAIT )) || return 1
        sleep 1; t=$((t + 1))
    done
}

wait_no_engines() {  # with the lock held: no sq-* container may be left, in any state, before a new one starts
    local t=0 left
    while left=$(sq_containers); [[ -n "$left" ]]; do
        if (( t >= GONE_WAIT )); then
            die "still here after ${GONE_WAIT}s: $(echo "$left" | paste -sd, -). One engine at a time: not starting. Take it down first (sq-engine.sh down --name <name without sq->), or raise SQ_GONE_WAIT if it is only slow to stop."
        fi
        if (( t == 0 )); then log "waiting up to ${GONE_WAIT}s for the previous engine to be gone: $(echo "$left" | paste -sd, -)"; fi
        sleep 1; t=$((t + 1))
    done
}

default_issue() {
    local b
    b=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [[ "$b" =~ sq-([0-9]+) ]]; then echo "${BASH_REMATCH[1]}"; else echo "?"; fi
}

cmd_up() {
    local rel="" port=$DEFAULT_PORT mem=4g issue="${SQ_ISSUE:-}" api="$REPO/apps/search/serve/api" name="" image=$DEFAULT_IMAGE news="${SQ_NEWS:-$HOME/search-eval/news.sqlite}"
    local extra=() envargs=() kv
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port) port=$2; shift 2 ;;
            --cap4) mem=4g; shift ;;
            --mem) mem=$2; shift 2 ;;
            --issue) issue=${2#\#}; shift 2 ;;
            --api) api=$2; shift 2 ;;
            --name) name=$2; shift 2 ;;
            --image) image=$2; shift 2 ;;
            --news) news=$2; shift 2 ;;
            --env) [[ "$2" == *=* ]] || die "--env wants K=V"; extra+=("$2"); shift 2 ;;
            -*) die "unknown option $1" ;;
            *) [[ -z "$rel" ]] || die "one release dir only"; rel=$1; shift ;;
        esac
    done
    [[ -n "$rel" ]] || die "usage: sq-engine.sh up <release-dir> [--port N] [--mem 4g] [--issue N] [--api DIR] [--name NAME]"
    rel=$(cd "$rel" && pwd)
    # a release root is the folder with manifest.json; an install root points at it through `current`
    if [[ ! -f "$rel/manifest.json" && -e "$rel/current/manifest.json" ]]; then rel=$(cd "$rel/current" && pwd -P); fi
    [[ -f "$rel/manifest.json" ]] || die "$rel has no manifest.json: not a release copy"
    # the reranker folder: models/rerank-turbo from rc9p on, the older name before it
    local ce=models/rerank-turbo; [[ -d "$rel/$ce" ]] || ce=models/rerank-turbo-jevfree
    [[ -d "$rel/text/wiki" && -d "$rel/vectors/ivf" && -d "$rel/$ce" ]] \
        || die "$rel lacks text/wiki, vectors/ivf or models/rerank-turbo"
    api=$(cd "$api" && pwd)
    [[ -f "$api/search_api.py" ]] || die "$api has no search_api.py"
    [[ "$port" =~ ^[0-9]+$ ]] || die "--port must be a number"
    podman image exists "$image" || die "image $image is not here (podman images); pass --image"
    [[ -n "$name" ]] || name=$(basename "$rel")
    [[ "$mem" =~ ^[1-9][0-9]*[mg]$ ]] || die "--mem wants a size like 4g or 6000m (an engine is never uncapped)"
    [[ -n "$issue" ]] || issue=$(default_issue)
    local ctr="sq-$name" rid build avail
    rid=$(release_id "$rel")
    build=$(build_hash "$api" "$rel" "${extra[@]}")
    for kv in "${extra[@]}"; do envargs+=(-e "$kv"); done

    local newsargs=()
    if [[ -f "$news" ]]; then newsargs=(-e SEARCH_NEWS_BACKEND=sqlite -e SEARCH_NEWS_SQLITE=/news/news.sqlite -v "$news:/news/news.sqlite:ro"); fi
    podman rm -f "$ctr" >/dev/null 2>&1 || true
    lock_take "$ctr" "$issue"
    trap 'lock_release "$ctr"' EXIT                      # any failure below gives the lock back
    wait_no_engines
    avail=$(free -m | awk '/^Mem:/ {print $7}')
    (( avail >= MIN_FREE_MB )) || die "only ${avail} MB available (free -m); an engine needs ${MIN_FREE_MB}. Not starting."
    log "starting $ctr: release $rid at $rel, api $api, build $build, port $port, ${mem}/${SQ_CPUS:-4} cpus"
    podman run -d --name "$ctr" --security-opt label=disable --userns=keep-id \
        --memory "$mem" --cpus "${SQ_CPUS:-4}" \
        --label sq-build="$build" --label sq-release="$rid" --label sq-port="$port" \
        -e SEARCH_CE_DEVICE=cpu -e SEARCH_HOME="40.592,-105.871" -e SEARCH_REQUIRE_RERANKER=1 -e SEARCH_REQUIRE_KINDS_MODEL=1 \
        -e MALLOC_ARENA_MAX=2 -e SEARCH_SQLITE_CACHE_KB=8000 \
        -e SEARCH_POSTGRES=off -e SEARCH_LOOKUP_BACKEND=release -e SEARCH_RELEASE_DIR=/rel -e SEARCH_RELEASE_ID="$rid" \
        -e SEARCH_RANKER=/rel/ranker.json \
        -e SEARCH_WIKI_BACKEND=tantivy-release -e SEARCH_WIKI_INDEX=/rel/text/wiki \
        -e SEARCH_SEM_BACKEND=ivf -e SEARCH_SEM_IVF=/rel/vectors/ivf -e SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5 \
        -e SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5 -e SEARCH_CE_DIR="/rel/$ce" \
        -e SEARCH_WEB_BACKEND=tantivy-release \
        "${newsargs[@]}" "${envargs[@]}" \
        -v "$api:/app:ro" -v "$rel:/rel:ro" -p "127.0.0.1:$port:8090" "$image" >/dev/null

    mkdir -p "$ENGINES"
    python3 - "$ENGINES/$port.json" "$ctr" "$port" "$rel" "$rid" "$api" "$build" "$image" "$mem" "$issue" "${extra[@]}" <<'PY'
import json, sys, time
p, ctr, port, rel, rid, api, build, image, mem, issue, *env = sys.argv[1:]
json.dump({"env": env, "mem": mem, "issue": issue, "container": ctr, "port": int(port), "release": rel, "release_id": rid, "api": api, "build": build,
           "image": image, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, open(p, "w"), indent=1)
PY
    local t=0
    while (( t < READY_WAIT )); do
        if curl -s -m 20 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/readyz" 2>/dev/null | grep -q '^200$'; then
            log "ready: $ctr on http://127.0.0.1:$port (build $build) after ${t}s"
            trap - EXIT                                  # the engine is up: the holder keeps the lock until `down`
            echo "$build"
            return 0
        fi
        if ! podman container exists "$ctr" || [[ "$(podman inspect -f '{{.State.Status}}' "$ctr")" != running ]]; then
            podman logs --tail 20 "$ctr" >&2 || true
            die "$ctr exited while starting"
        fi
        sleep 5; t=$((t + 5))
    done
    podman logs --tail 20 "$ctr" >&2 || true
    die "$ctr not ready after ${READY_WAIT}s (SQ_READY_WAIT to raise)"
}

cmd_down() {
    local name="" port=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name) name=$2; shift 2 ;;
            --port) port=$2; shift 2 ;;
            *) die "usage: sq-engine.sh down [--name NAME | --port N]" ;;
        esac
    done
    if [[ -n "$port" && -z "$name" ]]; then
        [[ -f "$ENGINES/$port.json" ]] || die "no engine recorded on port $port"
        name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["container"][3:])' "$ENGINES/$port.json")
    fi
    [[ -n "$name" ]] || die "down: say which one: --name NAME or --port N (status lists them)"
    local ctr="sq-$name"
    if podman container exists "$ctr" 2>/dev/null; then
        podman rm -f "$ctr" >/dev/null 2>&1 || true
        # the lock stays held until the container is gone: the next `up` is queued on it and wants this port
        wait_gone "$ctr" || die "$ctr still exists ${GONE_WAIT}s after podman rm -f; the engine lock is NOT released by this call (podman ps -a; SQ_GONE_WAIT to wait longer)"
        log "removed $ctr"
    else
        log "$ctr was not running"
    fi
    lock_release "$ctr"
    for f in "$ENGINES"/*.json; do
        [[ -f "$f" ]] || continue
        if grep -q "\"container\": \"$ctr\"" "$f"; then rm -f "$f"; fi
    done
}

cmd_status() {
    echo "sq engines (podman, this workstation):"
    podman ps -a --filter name='^sq-' --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}'
    local f
    for f in "$ENGINES"/*.json; do
        [[ -f "$f" ]] || continue
        python3 - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  {d['container']}: port {d['port']} build {d['build']} release {d['release_id']} api {d['api']} "
      f"mem {d.get('mem')} issue #{d.get('issue')} since {d['started']}")
PY
    done
    echo "engine lock ($LOCK): $( [[ -s "$LOCK" ]] && cat "$LOCK" || echo free )"
}

case "${1:-}" in
    up) shift; cmd_up "$@" ;;
    down) shift; cmd_down "$@" ;;
    status) cmd_status ;;
    *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
