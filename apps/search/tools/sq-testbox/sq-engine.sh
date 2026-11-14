#!/usr/bin/env bash
# =============================================================================
# sq-testbox/sq-engine.sh — sq-engine.sh's interface, the engine on testbox (the parity method)
# =============================================================================
# Put this directory first on PATH and tools/sq-sweep.py drives testbox instead of the workstation: this file stands in
# for sq-engine.sh, sq-eval.py beside it runs the eval ON testbox and brings the rows back, and podman beside it lists
# testbox's sq-* containers. The workstation runs no container. Everything goes through `ssh testbox`.
#
#   sq-engine.sh setup                  ship the API code, the eval tool and its sets to ~/$SQ_TST_DIR on testbox
#   sq-engine.sh up <release> --port N --name NAME [--issue N] [--env K=V ...]    -> prints the build hash
#   sq-engine.sh down --name NAME
#   sq-engine.sh teardown               remove ~/$SQ_TST_DIR on testbox (refused while an sq-sw473-* container exists)
#
# <release> is the workstation copy (for the build hash); testbox reads $SQ_TST_RELEASE (default
# /opt/logout-release/w/rc8-acc), whose manifest must have the same sha256. `up` exits 3 when another session is seen
# on testbox (remote/check.sh): the sweep stops and is not resumed until someone says so.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$TOOLS/../../.." && pwd)"
SQ_DIR="${SQ_DIR:-$HOME/.local/share/logout-search/sq}"
HOST="${SQ_TST_HOST:-deploy}"
RDIR="${SQ_TST_DIR:-sq-sw473}"
RREL="${SQ_TST_RELEASE:-/opt/logout-release/w/rc8-acc}"
NEWS="${SQ_NEWS:-$HOME/search-eval/news.sqlite}"
STATE="$SQ_DIR/sweeps/testbox-engines"

die() { echo "sq-testbox: $*" >&2; exit "${2:-1}"; }
remote() { timeout "${SQ_TST_SSH_TIMEOUT:-1800}" ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

build_hash() {  # sq-engine.sh's own function, read from it so the two cannot drift
    eval "$(sed -n '/^build_hash() {/,/^}/p' "$TOOLS/sq-engine.sh")"
    build_hash "$@"
}

cmd_setup() {
    remote true || die "server unreachable (ssh $HOST)"
    remote "test ! -e ~/$RDIR" || die "~/$RDIR already exists on testbox: someone's earlier run, or ours not torn down — look before removing it"
    local lsha rsha
    lsha=$(sha256sum "${SQ_TST_LOCAL_RELEASE:-$HOME/search-eval/rc8-acc}/manifest.json" | cut -d' ' -f1)
    rsha=$(remote "sha256sum $RREL/manifest.json" | cut -d' ' -f1)
    [[ "$lsha" == "$rsha" ]] || die "testbox's release manifest ($rsha) is not the workstation's ($lsha)"
    local stage; stage=$(mktemp -d); trap 'rm -rf "$stage"' RETURN
    mkdir -p "$stage/api" "$stage/tools/evalsets/open" "$stage/sq"
    cp "$REPO"/apps/search/serve/api/*.py "$stage/api/"
    [[ -f "$REPO/apps/search/serve/api/ranker.json" ]] && cp "$REPO/apps/search/serve/api/ranker.json" "$stage/api/"
    cp "$TOOLS"/{sq-eval.py,sq_common.py,eval-queries.py,compare-evals.py} "$stage/tools/"
    cp "$TOOLS"/evalsets/open/*.tsv "$stage/tools/evalsets/open/"
    cp -r "$TOOLS/evalsets/open/accept" "$stage/tools/evalsets/open/"
    local base; base=$(python3 -c "import sys; sys.path.insert(0, '$TOOLS'); import sq_common as C; print(C.BASELINE)")
    [[ -d "$SQ_DIR/$base" ]] && cp -r "$SQ_DIR/$base" "$stage/sq/"
    cp "$NEWS" "$stage/news.sqlite"
    cp "$HERE"/remote/*.sh "$stage/"
    tar -C "$stage" -czf - . | remote "mkdir -p ~/$RDIR && tar -C ~/$RDIR -xzf - && chmod 700 ~/$RDIR"
    echo "shipped to $HOST:~/$RDIR" >&2
}

cmd_up() {
    local rel="" port=18473 name="" extra=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port) port=$2; shift 2 ;;
            --name) name=$2; shift 2 ;;
            --issue|--mem|--api|--image|--news) shift 2 ;;
            --cap4) shift ;;
            --env) [[ "$2" == *=* ]] || die "--env wants K=V"; extra+=("$2"); shift 2 ;;
            -*) die "unknown option $1" ;;
            *) rel=$1; shift ;;
        esac
    done
    [[ -n "$rel" && -n "$name" ]] || die "usage: sq-engine.sh up <release> --port N --name NAME [--env K=V ...]"
    rel=$(cd "$rel" && pwd)
    local build rid args="" kv
    build=$(build_hash "$REPO/apps/search/serve/api" "$rel" "${extra[@]}")
    rid=$(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(next((m[k] for k in ('release_id','release','id','name') if isinstance(m.get(k), str) and m[k]), ''))" "$rel/manifest.json")
    for kv in "${extra[@]}"; do args+=" $(printf '%q' "$kv")"; done
    mkdir -p "$STATE"
    printf 'NAME=%s\nBUILD=%s\nPORT=%s\n' "sq-$name" "$build" "$port" > "$STATE/$port.env"
    remote "bash ~/$RDIR/up.sh $(printf '%q' "sq-$name") $port $RREL $(printf '%q' "$rid")$args" >&2 || exit $?
    echo "$build"
}

cmd_down() {
    local name=""
    while [[ $# -gt 0 ]]; do case "$1" in --name) name=$2; shift 2 ;; *) shift ;; esac; done
    [[ -n "$name" ]] || die "down: --name NAME"
    local f port=18473
    for f in "$STATE"/*.env; do [[ -f "$f" ]] && grep -qx "NAME=sq-$name" "$f" && port=$(sed -n 's/^PORT=//p' "$f"); done
    remote "bash ~/$RDIR/down.sh $(printf '%q' "sq-$name") $port" >&2
}

cmd_teardown() {
    local left; left=$(remote "docker ps -a --filter name=^sq-sw473- --format '{{.Names}}'")
    [[ -z "$left" ]] || die "not removing ~/$RDIR: $left still exists"
    remote "rm -rf ~/$RDIR"
    remote "test ! -e ~/$RDIR" && echo "removed $HOST:~/$RDIR" >&2
}

case "${1:-}" in
    setup) shift; cmd_setup ;;
    up) shift; cmd_up "$@" ;;
    down) shift; cmd_down "$@" ;;
    teardown) shift; cmd_teardown ;;
    *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
