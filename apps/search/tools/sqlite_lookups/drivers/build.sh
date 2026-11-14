#!/usr/bin/env bash
set -euo pipefail
cd ~/t4 || exit 1
OUT=${1:-lookup.sqlite}; GRP=${2:-places,suggest,wiki,nav,kinds,misc}
rm -rf "$OUT" "${OUT%.*}.typo-keys"
sudo -n docker run --rm --label "dbtest=safe to remove" --name t4-build-$$ --cpus 2 --memory 4g \
  --network search-serve_search-serve-net --env-file pg.env -v ~/t4:/w -w /w --entrypoint python t4-lookups:1 \
  export.py "/w/$OUT" "$GRP"
sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t4:/w --entrypoint sh t4-lookups:1 -c "chmod -R a+rX /w/$OUT /w/${OUT%.*}.typo-keys"   # the eval container runs as uid 1000
echo BUILD-DONE
