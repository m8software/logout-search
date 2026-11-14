#!/usr/bin/env bash
# =============================================================================
# accept-testbox.sh — a search data release's acceptance on testbox in one command
# =============================================================================
# What was done by hand for rc10 and rc11 (memory: testbox-release-acceptance-recipe): an engine beside testbox's serving
# stack with the release read-only, a few queries by hand, the government set (tools/gov-eval) scored against earlier
# arms, the twelve open sets (test halves) against the previous final arm, the engine down, one report.
#
#   accept-testbox.sh run   --release-dir DIR --release-id ID --arm NAME --baseline NAME [--api DIR] [--port N]
#                       [--gov-baseline FILE.jsonl ...] [--env K=V ...] [--no-open] [--no-gov] [--pools QUERIES.txt]
#                       [--sem-pages IVF_DIR]
#   accept-testbox.sh fetch --arm NAME [--out DIR]     the report and the government answers back to the workstation
#   accept-testbox.sh status --arm NAME                the job's log tail on testbox
#
# --release-dir  the installed release on testbox (install.py install ... --dest /opt/logout-release/w/<rc>-base: its current/)
# --api          the engine code to test (default: this checkout's apps/search/serve/api), shipped to ~/accept/<arm>/api
# --baseline     an arm under $SQ_DIR on testbox (default ~/sq-sw473/sq), e.g. rc11final
# --gov-baseline earlier government answers already on testbox (e.g. ~/rc11acc/gov-rc11.jsonl), scored beside the new arm
# --pools        a workstation file of queries (one a line): each one's top 10 with the learned ranker and the reranker
#                off (rank=0&ce=0), into pools.jsonl, for ranker training rows (utility/ce-train/ranker_jf.py)
# --sem-pages    a workstation pages IVF folder (release/pages_vec.py + ivf_build.py, built for this release id): shipped
#                inside the engine's code folder and read with SEARCH_SEM_PAGES_IVF (meaning search over pages)
# ACCEPT_OWN=/tmp/own-a:/tmp/own-b: folders on testbox this caller changed itself in the last hour (its own test copies),
# which testbox's busy check would otherwise read as another session's work.
# The engine reads ~/accept/_news/news.sqlite (ACCEPT_NEWS): a snapshot of indexhost's news-store/news.sqlite (towns,
# weather), taken with sqlite's backup API.
#
# The job runs detached on testbox (setsid); `run` returns once it started. The engine's guard is up.sh's own: it refuses
# while another session's container is seen, while testbox is busy or while the serving search-api is unhealthy, and the
# engine gets 4 GB and 3 cpus. Pass rule for an open set: the 95 % interval includes 0 and the drop is at most
# 0.5 pt, or the gain is significant (sq-eval.py). After an accepted release the home engine on indexhost is rebuilt
# separately (search-rtc-dev is its own image), and the report says so.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARCH="$(cd "$HERE/.." && pwd)"
HOST="${ACCEPT_TST_HOST:-deploy}"
SETS="head-brand,site-nav,own-title,maker-product,wiki-redirect,account,typo-site,typo-wiki,place,place-town,typo-place,books"
SMOKE=("nj fishing license" "co jury duty" "2012 new york liberty" "in flames" "al gore" "dmv near me" "wells fargo"
       "weather in springfield" "craigsk" "virginia state tax refund status")

die() { echo "accept-testbox: $*" >&2; exit "${2:-1}"; }
remote() { timeout "${ACCEPT_SSH_TIMEOUT:-600}" ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }
safe_arm() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{1,40}$ ]] || die "arm must be lower-case letters, digits, - or _: $1"; }

cmd_run() {
    local rel="" rid="" arm="" base="" api="$SEARCH/serve/api" port=18481 open=1 gov=1 envs=() govb=() pools="" semp=""
    while (($#)); do
        case "$1" in
            --release-dir) rel=$2; shift 2 ;; --release-id) rid=$2; shift 2 ;; --arm) arm=$2; shift 2 ;;
            --baseline) base=$2; shift 2 ;; --api) api=$2; shift 2 ;; --port) port=$2; shift 2 ;;
            --gov-baseline) govb+=("$2"); shift 2 ;; --env) envs+=("$2"); shift 2 ;;
            --no-open) open=0; shift ;; --no-gov) gov=0; shift ;; --pools) pools=$2; shift 2 ;; --sem-pages) semp=$2; shift 2 ;;
            *) die "unknown argument $1" ;;
        esac
    done
    [[ -n "$rel" && -n "$rid" && -n "$arm" && -n "$base" ]] || die "run needs --release-dir, --release-id, --arm and --baseline"
    safe_arm "$arm"
    [[ -f "$api/search_api.py" ]] || die "no search_api.py in $api"
    [[ "$port" =~ ^[0-9]+$ ]] || die "bad port $port"
    remote true || die "server unreachable (ssh $HOST)"
    remote "test -f '$rel/manifest.json'" || die "no release at $HOST:$rel (manifest.json missing)"
    # shellcheck disable=SC2088  # the tilde expands on testbox
    remote "test ! -e ~/accept/$arm" || die "~/accept/$arm exists on testbox: an earlier run of this arm — fetch it, then move it aside"
    local stage; stage=$(mktemp -d); trap 'rm -rf -- "$stage"' RETURN
    mkdir -p "$stage/api" "$stage/tools/evalsets/open" "$stage/tools/gov-eval"
    cp "$api"/*.py "$stage/api/"; [[ -f "$api/ranker.json" ]] && cp "$api/ranker.json" "$stage/api/"
    cp "$SEARCH"/tools/{sq-eval.py,sq_common.py,eval-queries.py,compare-evals.py,facts-eval.py} "$stage/tools/"   # every tool sq-eval loads
    cp "$SEARCH"/tools/evalsets/open/*.tsv "$stage/tools/evalsets/open/"
    cp -r "$SEARCH/tools/evalsets/open/accept" "$stage/tools/evalsets/open/"
    [[ -d "$SEARCH/tools/evalsets/open/page" ]] && cp -r "$SEARCH/tools/evalsets/open/page" "$stage/tools/evalsets/open/"   # page-gold sidecars
    cp "$SEARCH"/tools/gov-eval/{run.py,gov-services.tsv,gov-home-points.tsv} "$stage/tools/gov-eval/"
    mkdir -p "$stage/release"                     # run.py reads ../../release/gov_states.py for the state of each site
    cp "$SEARCH"/release/gov_states.py "$stage/release/"
    [[ -f "$SEARCH/release/gov-state-domains.tsv" ]] && cp "$SEARCH/release/gov-state-domains.tsv" "$stage/release/"
    cp "$HERE"/sq-testbox/remote/*.sh "$stage/"
    if [[ -n "$semp" ]]; then
        [[ -f "$semp/ivf.json" ]] || die "no ivf.json in $semp"
        cp -r "$semp" "$stage/api/pages-ivf"; envs+=("SEARCH_SEM_PAGES_IVF=/app/pages-ivf")
    fi
    if [[ -n "$pools" ]]; then [[ -s "$pools" ]] || die "no queries in $pools"; cp "$pools" "$stage/pool-queries.txt"; fi
    local build; build="$arm-$(cat "$stage"/api/*.py | sha256sum | cut -c1-8)"
    {
        echo "REL=$(printf %q "$rel") RID=$(printf %q "$rid") ARM=$arm BASE=$(printf %q "$base") PORT=$port BUILD=$build"
        echo "OPEN=$open GOV=$gov SETS=$SETS"
        echo "OWN=$(printf %q "${ACCEPT_OWN:-}")"          # folders on testbox this caller changed itself (check.sh SQ_TST_OWN)
        # printf with no arguments still prints one empty word: an empty list is written as ()
        printf 'ENVS=('; ((${#envs[@]})) && printf '%q ' "${envs[@]}"; echo ')'
        printf 'GOVB=('; ((${#govb[@]})) && printf '%q ' "${govb[@]}"; echo ')'
        printf 'SMOKE=('; printf '%q ' "${SMOKE[@]}"; echo ')'
    } > "$stage/accept.env"
    cp "$HERE/accept-testbox.sh" "$stage/accept-testbox.sh"
    tar -C "$stage" -czf - . | remote "mkdir -p ~/accept/$arm && tar -C ~/accept/$arm -xzf - && chmod 700 ~/accept/$arm"
    # braces: only the job goes to the background; "cd && job &" backgrounds a subshell that holds ssh's output open
    remote "cd ~/accept/$arm && { setsid nohup bash accept-testbox.sh job </dev/null >job.log 2>&1 & } && echo started"
    echo "accept-testbox: $arm started on $HOST (build $build); follow: accept-testbox.sh status --arm $arm" >&2
}

# ---- on testbox ---------------------------------------------------------------------------------------------------------
cmd_job() {
    set +e
    cd "$(dirname "$0")" || exit 1
    # shellcheck disable=SC1091
    . ./accept.env
    local NAME="acc-$ARM" SQ=${SQ_DIR:-$HOME/sq-sw473/sq}
    log() { echo "$(date -u +%FT%TZ) $*"; }
    rep() { echo "$*" >> report.md; }
    : > report.md
    rep "# Acceptance: $ARM"; rep ""; rep "- release: \`$RID\` at \`$REL\`"; rep "- engine code: build \`$BUILD\`"
    rep "- baseline: \`$BASE\`; started $(date -u +%FT%TZ)"; rep ""
    # the engine's news file: weather, towns (a location's state), games. up.sh mounts ./news.sqlite; without a
    # real one docker makes an empty folder there and every town lookup answers nothing.
    local news=${ACCEPT_NEWS:-$HOME/accept/_news/news.sqlite}
    if [[ ! -s "$news" ]]; then rep "**No news file** at $news (a snapshot of indexhost's news-store/news.sqlite)"; log "NO-NEWS"; echo ACCEPT-DONE; return 1; fi
    cp "$news" news.sqlite
    # a snapshot of a live news file keeps its WAL journal mode, and a WAL file opened read-only (up.sh mounts it :ro)
    # needs a -shm beside it it cannot create: every news query then fails with "attempt to write a readonly
    # database" and the engine has no towns and no weather. The arm's own copy is switched to a rollback journal.
    python3 -c "import sqlite3; c = sqlite3.connect('news.sqlite'); c.execute('PRAGMA journal_mode=DELETE'); c.close()" \
        || { rep "**News file** could not be switched to a rollback journal"; log "NEWS-JOURNAL"; echo ACCEPT-DONE; return 1; }
    log "engine up"
    SQ_TST_OWN=${OWN:-} SQ_TST_IMAGE=${SQ_TST_IMAGE:-dbt-search-api:engine-rc4} bash up.sh "$NAME" "$PORT" "$REL" "$RID" \
        SEARCH_CE_DIR=/rel/models/rerank-turbo SEARCH_DROP_EVAL=1 "${ENVS[@]}" > up.log 2>&1
    if [[ $? -ne 0 ]]; then rep "**Engine did not start**: $(tail -3 up.log | tr '\n' ' ')"; log "UP-FAILED"; echo ACCEPT-DONE; return 1; fi
    rep "## By hand"; rep ""; rep '```'
    for q in "${SMOKE[@]}"; do
        echo "== $q"
        curl -s -m 120 -w '  ms %{time_total}\n' "http://127.0.0.1:$PORT/search?k=3&q=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$q")" \
          | python3 -c 'import sys,json
raw=sys.stdin.read(); body,_,t=raw.rpartition("}")
try:
    d=json.loads(body+"}")
except ValueError:
    print("  (no answer)"); sys.exit()
[print(" ", i+1, r.get("url"), "|", (r.get("title") or "")[:55]) for i,r in enumerate((d.get("results") or [])[:3])]
print(t.strip())'
    done >> report.md
    rep '```'; rep ""
    if [[ "$GOV" == 1 ]]; then
        log "government set"
        nice -n 10 python3 tools/gov-eval/run.py ask tools/gov-eval/gov-services.tsv "$ARM=http://127.0.0.1:$PORT" "gov-$ARM.jsonl" --workers 2 > gov-ask.log 2>&1
        rep "## Government set (tools/gov-eval, rule grader)"; rep ""; rep '```'
        python3 tools/gov-eval/run.py score tools/gov-eval/gov-services.tsv "$HOME/engine-gov/current-full.csv" "${GOVB[@]}" "gov-$ARM.jsonl" >> report.md 2>&1
        rep '```'; rep ""
        # the home wording: the same tasks without the state, asked from a point in it; the arm against itself
        # with the home-state rule dropped, since only an engine with the rule answers this wording differently
        log "government set, home wording"
        python3 tools/gov-eval/run.py home tools/gov-eval/gov-services.tsv tools/gov-eval/gov-home-points.tsv gov-home.tsv 2>> gov-ask.log
        nice -n 10 python3 tools/gov-eval/run.py ask gov-home.tsv "$ARM=http://127.0.0.1:$PORT" "govhome-$ARM.jsonl" --workers 2 >> gov-ask.log 2>&1
        nice -n 10 python3 tools/gov-eval/run.py ask gov-home.tsv "$ARM-nohome=http://127.0.0.1:$PORT&drop=govhome" "govhome-$ARM-nohome.jsonl" --workers 2 >> gov-ask.log 2>&1
        rep "## Government set, home wording (a point in the state, no state in the words; vs the arm with drop=govhome)"; rep ""; rep '```'
        python3 tools/gov-eval/run.py score gov-home.tsv "$HOME/engine-gov/current-full.csv" "govhome-$ARM-nohome.jsonl" "govhome-$ARM.jsonl" 2>&1 | sed -n '1,8p' >> report.md
        rep '```'; rep ""
    fi
    if [[ "$OPEN" == 1 ]]; then
        log "open sets vs $BASE"
        SQ_DIR=$SQ nice -n 10 python3 tools/sq-eval.py --arm "$ARM" --test --engine "http://127.0.0.1:$PORT" --sets "$SETS" \
            --workers 1 --build "$BUILD" --baseline "$BASE" > open.log 2>&1
        rep "## Open sets, test halves, vs $BASE (exit $?)"; rep ""; rep '```'; tail -22 open.log >> report.md; rep '```'; rep ""
    fi
    if [[ -s pool-queries.txt ]]; then
        log "pools ($(grep -c . pool-queries.txt) queries, rank=0&ce=0)"
        python3 - "$PORT" pool-queries.txt pools.jsonl <<'PYEOF' > pools.log 2>&1
import json, sys, urllib.parse, urllib.request
port, src, out = sys.argv[1:4]
with open(out, "w") as f:
    for q in (l.strip() for l in open(src) if l.strip()):
        url = f"http://127.0.0.1:{port}/search?k=10&rank=0&ce=0&q=" + urllib.parse.quote(q)
        try:
            d = json.load(urllib.request.urlopen(url, timeout=120))
            res = [{k: r.get(k) for k in ("url", "title", "content", "source", "score", "official", "popular")} for r in d.get("results") or []]
            f.write(json.dumps({"q": q, "results": res}) + "\n")
        except Exception as e:
            f.write(json.dumps({"q": q, "error": f"{type(e).__name__}: {e}"}) + "\n")
PYEOF
        rep "- pools: $(grep -c . pools.jsonl) of $(grep -c . pool-queries.txt) queries in pools.jsonl (rank=0&ce=0, top 10)"; rep ""
    fi
    bash down.sh "$NAME" "$PORT" >> up.log 2>&1; log "engine down"
    rep "## Next"; rep ""
    rep "- Clef's strict reading of rank 1: \`utility/search-gov-judge.py\` on gov-$ARM.jsonl (free tier; titles and URLs only)."
    rep "- Accepted: rebuild the home engine on indexhost (search-rtc-dev is its own image; memory home-engine-rebuild-on-release)."
    rep ""; rep "finished $(date -u +%FT%TZ)"
    echo ACCEPT-DONE
}

cmd_fetch() {
    local arm="" out=""
    while (($#)); do case "$1" in --arm) arm=$2; shift 2 ;; --out) out=$2; shift 2 ;; *) die "unknown argument $1" ;; esac; done
    safe_arm "$arm"; out=${out:-$HOME/data/accept/$arm}
    remote "grep -q ACCEPT-DONE ~/accept/$arm/job.log" || die "$arm has not finished (accept-testbox.sh status --arm $arm)"
    mkdir -p "$out"
    # only the files this arm has: a missing name made tar exit 2, and under pipefail fetch ended without a word
    remote "cd ~/accept/$arm && ls -d report.md job.log up.log gov-*.jsonl govhome-*.jsonl open.log pools.jsonl pools.log 2>/dev/null | tar -czf - -T -" \
        | tar -C "$out" -xzf -
    echo "$out/report.md"
}

cmd_status() {
    local arm=""; [[ "${1:-}" == --arm ]] && arm=$2; safe_arm "$arm"
    remote "tail -5 ~/accept/$arm/job.log; grep -c . ~/accept/$arm/gov-$arm.jsonl 2>/dev/null"
}

case "${1:-}" in
    run) shift; cmd_run "$@" ;;
    job) shift; cmd_job "$@" ;;
    fetch) shift; cmd_fetch "$@" ;;
    status) shift; cmd_status "$@" ;;
    *) sed -n '2,30p' "$0"; exit 2 ;;
esac
