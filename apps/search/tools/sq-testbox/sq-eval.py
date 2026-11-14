#!/usr/bin/env bash
# sq-testbox/sq-eval.py — sq-eval.py's interface for sq-sweep.py, run ON testbox. Named .py because the sweep
# calls the tool by that name; it is a shell script. Takes --arm, --engine, --sets, --baseline; asks testbox's engine with
# 1 query in flight under testbox's copy of tools/sq-eval.py (dev halves: --test is refused here as well), then copies the
# arm's rows back to the workstation's SQ_DIR and the build's cached answers to SQ_DIR/cache-testbox/. Fails when the arm's guard fired.
set -euo pipefail
SQ_DIR="${SQ_DIR:-$HOME/.local/share/logout-search/sq}"
HOST="${SQ_TST_HOST:-deploy}"
RDIR="${SQ_TST_DIR:-sq-sw473}"
remote() { timeout "${SQ_TST_SSH_TIMEOUT:-21600}" ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }
arm="" engine="" sets="" baseline=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --arm) arm=$2; shift 2 ;;
        --engine) engine=$2; shift 2 ;;
        --sets) sets=$2; shift 2 ;;
        --baseline) baseline=$2; shift 2 ;;
        --test|--both|--force|--judged) echo "sq-testbox: $1 is never run from the sweep" >&2; exit 2 ;;
        *) echo "sq-testbox: unknown option $1" >&2; exit 2 ;;
    esac
done
[[ -n "$arm" && -n "$engine" && -n "$sets" ]] || { echo "sq-testbox: --arm, --engine and --sets are required" >&2; exit 2; }
port=${engine##*:}; port=${port%%/*}
env="$SQ_DIR/sweeps/testbox-engines/$port.env"
[[ -f "$env" ]] || { echo "sq-testbox: no engine recorded on port $port ($env)" >&2; exit 1; }
name=$(sed -n 's/^NAME=//p' "$env"); build=$(sed -n 's/^BUILD=//p' "$env")
q() { printf '%q' "$1"; }
cmd="cd ~/$RDIR && SQ_DIR=~/$RDIR/sq nice -n 10 python3 tools/sq-eval.py --arm $(q "$arm") --engine http://127.0.0.1:$port"
cmd+=" --sets $(q "$sets") --workers 1 --build $(q "$build")"
[[ -n "$baseline" ]] && cmd+=" --baseline $(q "$baseline")"
set +e
remote "$cmd"
rc=$?
set -e
g=$(remote "cat ~/$RDIR/guard.$(q "$name").txt 2>/dev/null" || true)
# the engine's guard rows, the serving probes and the engine's last log lines come home, whatever happened: a guard
# that fired once and left nothing to read cost a night (2026-10-14)
mkdir -p "$SQ_DIR/sweeps/testbox-logs/$arm"
remote "cd ~/$RDIR && tar -czf - mon.$(q "$name").csv guard.$(q "$name").txt baseline.$(q "$name").csv baseline.$(q "$name").median probe.log engine.$(q "$name").log 2>/dev/null" \
    | tar -C "$SQ_DIR/sweeps/testbox-logs/$arm" -xzf - 2>/dev/null || true
mkdir -p "$SQ_DIR"
# the rows under the arm's name; the cached answers under cache-testbox/, so the workstation's own cache of the same build
# (its control has the same settings) is never overwritten. --same-answers reads them as build ../cache-testbox/<build>.
mkdir -p "$SQ_DIR/cache-testbox"
remote "cd ~/$RDIR/sq && tar -czf - $(q "$arm") 2>/dev/null" | tar -C "$SQ_DIR" -xzf - || true
remote "cd ~/$RDIR/sq/cache && tar -czf - $(q "$build") 2>/dev/null" | tar -C "$SQ_DIR/cache-testbox" -xzf - || true
[[ -z "$g" ]] || { echo "sq-testbox: the guard removed the engine: $g" >&2; exit 1; }
exit "$rc"
