#!/usr/bin/env bash
# Runs every set against the given arms (default: A B1), one arm after the other (testbox is shared: one container busy at
# a time), through ssh -L ports (A 18091, B1 18092, B2 18093, B3 18094, B4 18095) to the containers arms.sh started.
# Kinds off in every arm.
#   S=/tmp/own-rank-ab bash apps/search/tools/own-rank-ab/run.sh [ARM...]
#   SPLIT=dev SETS='wiki-redirect' ...   the dev half of the named open sets (details files d-<set>.dev-<arm>.tsv), no hand sets
# Hand/click label files are private (~/.local/share/logout-search); never commit their details files.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
S=${S:-/tmp/own-rank-ab}; mkdir -p "$S"
L=$HOME/.local/share/logout-search
O=apps/search/tools/evalsets/open
OFF='&off=kind_docs,kind_members,kind_keys,kind_vecs'
SPLIT=${SPLIT:-test}; SFX=""; [ "$SPLIT" = test ] || SFX=".$SPLIT"
SETS=${SETS:-site-nav typo-site wiki-redirect typo-wiki account place place-town typo-place}
tunnel() { # port: (re)open the ssh -L to the container when it is not answering (it dies on a long idle or a network blip)
  curl -fsS -m 5 "http://127.0.0.1:$1/readyz" >/dev/null 2>&1 && return 0
  ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -L "$1:127.0.0.1:$1" deploy 2>/dev/null || true
  sleep 2; curl -fsS -m 5 "http://127.0.0.1:$1/readyz" >/dev/null 2>&1
}
run() { # name file split arm port; a set whose answers are >2% empty (the tunnel or the engine died) is run again, up to 3 times
  local split=() rc=0 try empty n
  if [ -n "$3" ]; then split=(--split "$3"); fi
  for try in 1 2 3; do
    tunnel "$5" || echo "$(date +%T) tunnel to $5 is down (try $try)"
    rc=0
    python3 apps/search/tools/eval-queries.py "$2" "${split[@]}" --direct "http://127.0.0.1:$5" --no-present --workers ${WORKERS:-1} --extra "$OFF" --details "$S/d-$1$SFX-$4.tsv" > "$S/o-$1$SFX-$4.txt" 2>&1 || rc=$?
    n=$(($(wc -l < "$S/d-$1$SFX-$4.tsv") - 1)); empty=$(awk -F'\t' 'NR > 1 && $5 == ""' "$S/d-$1$SFX-$4.tsv" | wc -l)
    [ $((empty * 50)) -le "$n" ] && break
    echo "$(date +%T) $1$SFX $4: $empty of $n answers empty, again"
  done
  echo "$(date +%T) done $1$SFX $4 rc=$rc empty=$empty"
}
arms=("$@"); [ ${#arms[@]} -gt 0 ] || arms=(A B1)
for arm in "${arms[@]}"; do
  case $arm in A) port=18091;; B1) port=18092;; B2) port=18093;; B3) port=18094;; B4) port=18095;; B5) port=18096;; B6) port=18097;; B7) port=18098;; B8) port=18099;; B9) port=18100;; B10) port=18101;; B11) port=18102;; B12) port=18103;; B13) port=18104;; B14) port=18105;; B15) port=18106;; *) echo "arm?"; exit 2;; esac
  for s in $SETS; do run "$s" "$O/$s.tsv" "$SPLIT" "$arm" "$port"; done
  if [ "$SPLIT" = test ]; then
    run v3 "$L/eval_labels_v3.tsv" "" "$arm" "$port"
    run desc "$L/eval_labels_descriptive.tsv" "" "$arm" "$port"
    run click "$L/auto_labels.tsv" "" "$arm" "$port"
  fi
done
echo ALLDONE
