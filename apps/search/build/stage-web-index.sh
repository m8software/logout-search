#!/usr/bin/env bash
# One web text index (sites | pages | anchors) as a pipeline stage: tantivy-structure/local-build.sh over web-input's
# rows, in popularity order, analyzer en_pg2, then the release stamp. Runs on the host (local-build.sh runs docker itself).
#   stage-web-index.sh OUT KIND REGISTRY.sqlite KIND.rows.tsv.zst HOSTRANKS.tsv STOPWORDS RELEASE_ID
# Output: OUT/KIND (the canonical index folder with release.json). OUT must be the stage's fresh output folder
# (outdir.sh: WORK/stages/NAME/KEY.partial); the scratch folder OUT/tw is made and removed inside it, nothing else is removed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/outdir.sh"
[ $# -eq 7 ] || { echo "usage: stage-web-index.sh OUT KIND REGISTRY ROWS HOSTRANKS STOPWORDS RELEASE_ID" >&2; exit 2; }
OUT=$1; KIND=$2; REG=$3; ROWS=$4; RANKS=$5; STOP=$6; RID=$7
require_outdir "$OUT"
TS="$HERE/../tools/tantivy-structure"; IMG="${IMG:-logout-tantivy-build:4}"; D="${DOCKER:-docker}"
case "$KIND" in sites|pages|anchors) ;; *) echo "KIND must be sites, pages or anchors" >&2; exit 2 ;; esac
[ -e "$OUT/$KIND" ] && { echo "refusing: $OUT/$KIND exists already" >&2; exit 2; }
for f in "$REG" "$ROWS" "$RANKS" "$STOP"; do [ -s "$f" ] || { echo "missing input $f" >&2; exit 2; }; done
$D image inspect "$IMG" >/dev/null 2>&1 || $D build -q -t "$IMG" "$TS"
TW="$OUT/tw"; mkdir -p "$TW/in" "$TW/out"
cp "$STOP" "$TW/in/english.stop"
ln "$REG" "$TW/in/registry.sqlite" 2>/dev/null || cp "$REG" "$TW/in/registry.sqlite"
cp "$RANKS" "$TW/in/hostranks.tsv"
ln "$ROWS" "$TW/in/$KIND.rows.tsv.zst" 2>/dev/null || cp "$ROWS" "$TW/in/$KIND.rows.tsv.zst"
KIND=$KIND ANALYZER=en_pg2 IMG=$IMG DOCKER="$D" DOCKER_OPTS="${DOCKER_OPTS:---memory 5g}" bash "$TS/local-build.sh" "$TW" 2>&1 | tail -15
python3 "$HERE/tvstamp.py" "$TW/out/$KIND" "$RID" "$KIND" "$TW/in/english.stop"
mv "$TW/out/$KIND" "$OUT/$KIND"
rm -rf "$OUT/tw"                                   # inside the checked output folder only
python3 - "$OUT/$KIND/meta.json" "$KIND" <<'PY'
import json, sys
m = json.load(open(sys.argv[1])); print(sys.argv[2], "docs", sum(s["max_doc"] for s in m["segments"]), "segments", len(m["segments"]))
PY
