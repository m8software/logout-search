#!/usr/bin/env bash
# Runs every set against both arms through ssh -L 18193 (A) / 18194 (B) to testbox. Kinds off in both arms.
# Hand/click label files are private (never commit their details files).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
S=${S:-/tmp/engine-ab}; mkdir -p "$S"
L=$HOME/.local/share/logout-search
O=apps/search/tools/evalsets/open
# kinds off by default; KINDS_ON=1 asks without &off (arm D, whose root holds kinds.sqlite)
OFF='&off=kind_docs,kind_members,kind_keys,kind_vecs'
if [ "${KINDS_ON:-0}" = 1 ]; then OFF=''; fi
run() { # name file split arm port
  local split=() rc=0
  if [ -n "$3" ]; then split=(--split "$3"); fi
  python3 apps/search/tools/eval-queries.py "$2" "${split[@]}" --direct "http://127.0.0.1:$5" --no-present --workers 2 --extra "$OFF" --details "$S/d-$1-$4.tsv" > "$S/o-$1-$4.txt" 2>&1 || rc=$?
  echo "$(date +%T) done $1 $4 rc=$rc"
}
# ARMS="A B" ports A=18193 B=18194 C=18196 D=18197; HAND_ONLY=1 runs the three hand/click sets only
for arm in ${ARMS:-A B}; do
  case $arm in A) port=18193;; B) port=18194;; C) port=18196;; D) port=18197;; *) exit 2;; esac
  run v3 $L/eval_labels_v3.tsv "" $arm $port
  run desc $L/eval_labels_descriptive.tsv "" $arm $port
  run click $L/auto_labels.tsv "" $arm $port
  if [ "${HAND_ONLY:-0}" = 1 ]; then continue; fi
  # head-brand: well-known sites asked by name; report its name:only and name:shared strata apart.
  # SPLIT=dev for the popularity-line sweep (never fit on the test half), default test for acceptance.
  for s in wiki-redirect site-nav account place place-town typo-wiki typo-site typo-place head-brand; do run $s $O/$s.tsv "${SPLIT:-test}" $arm $port; done
done
echo ALLDONE
