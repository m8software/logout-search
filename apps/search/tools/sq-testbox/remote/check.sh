#!/usr/bin/env bash
# Is anyone else working on testbox? Exit 0 when not; else exit 3 and say what was seen. Read-only.
# Run from a file so its patterns are not on its own command line.
set -uo pipefail
R=$(cd "$(dirname "$0")" && pwd)
seen=""
c=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E 'rc9|eval|arm|parity|accept|acc-|^sq-|bench|^ev-' | grep -v '^sq-sw473-' || true)
[[ -n "$c" ]] && seen+="containers: $(echo "$c" | paste -sd, -); "
p=$(ps -eo pid=,args= | grep -E 'sq-eval|eval-queries|sq-candidates|master-deploy|deploy-to-server|apps/[^ ]+/deploy\.sh|rc9|docker build|docker compose' \
    | grep -v -e grep -e sq-sw473 -e check.sh || true)
[[ -n "$p" ]] && seen+="processes: $(echo "$p" | head -3 | cut -c1-120 | paste -sd'|' -); "
# SQ_TST_OWN: colon-separated folders the caller itself changed (its own test copies), named on purpose
own=(); IFS=: read -r -a _own <<< "${SQ_TST_OWN:-}"; for o in "${_own[@]}"; do [[ -n "$o" ]] && own+=(! -path "$o" ! -path "$o/*"); done
f=$( { find "${SQ_TST_RELEASES:-/opt/logout-release/w}" -mindepth 1 -maxdepth 2 -newermt '-60 minutes' "${own[@]}" 2>/dev/null   # a release the caller copied in is its own too
       find "$HOME" -mindepth 1 -maxdepth 2 -newermt '-60 minutes' ! -path "$R" ! -path "$R/*" ! -path "$HOME/.*" \
           ! -path "$HOME/accept" ! -path "$HOME/accept/*" ! -path "$HOME/sq-sw473/sq" "${own[@]}" 2>/dev/null   # accept-testbox.sh runs (one live shows as an acc-* container) and the eval results folder
       find /var/log/logout-deploy -mindepth 1 -maxdepth 2 -newermt '-60 minutes' 2>/dev/null; } | head -3)
[[ -n "$f" ]] && seen+="files changed in the last hour: $(echo "$f" | paste -sd, -); "
if [[ -n "$seen" ]]; then echo "another session on testbox: $seen"; exit 3; fi
exit 0
