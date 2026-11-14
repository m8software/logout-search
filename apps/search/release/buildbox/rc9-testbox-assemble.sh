#!/usr/bin/env bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# rc9 step 7, run ON testbox: ssh testbox 'bash -s' < rc9-testbox-assemble.sh
# Parts in /opt/logout-release/w/rc9-in (buildbox stream + stamped indexes + kinds.sqlite + branch code) -> ONE signed (TEST key)
# release folder /opt/logout-release/w/rc9-acc, then the Tranco-free publish gate and sqlguard on every SQLite file.
set -euo pipefail
ID=proto-20261020-rc9; R=/opt/logout-release; P=$R/w/rc9-in; OUT=$R/w/rc9-acc
IMG=${IMG:-logout/search-api:kinds14}
PY=(docker run --rm --user 0 --entrypoint python --label dbtest=safe-to-remove -e PYTHONPATH=/r/w/pylib -e PYTHONDONTWRITEBYTECODE=1 -v "$R:/r" -w /r/w/rc9-in/code/apps/search/release "$IMG")
# the meaning-search folder: Wikipedia ids are identical rc2 = rc8 = rc9, so the 10-02 folder is valid; only its stamp changes
if [ ! -d "$P/ivf" ]; then
    cp -r --reflink=always "$R/w/proto-20261002-testbox/vectors/ivf2600" "$P/ivf"
    python3 - "$P/ivf/ivf.json" "$ID" <<'PYEOF'
import json, sys
p, rid = sys.argv[1:3]
m = json.load(open(p)); m["restamped_from"] = m["release"]; m["release"] = rid
open(p, "w").write(json.dumps(m, indent=1, sort_keys=True) + "\n")
PYEOF
fi
( cd "$R/w/proto-20261002-testbox/vectors/ivf2600" && sha256sum codes.u8 ids.i32 offsets.i64 centroids.u8 ) > "$P/ivf-src.sha"; ( cd "$P/ivf" && sha256sum -c "$P/ivf-src.sha" )
# rc9 W7: the kinds query model, unmodified from upstream (BAAI/bge-small-en-v1.5, MIT). Taken out of the engine image once and
# checked against the sha256 Hugging Face publishes for onnx/model.onnx (read 2026-10-07).
BGE_SHA=828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35
if [ ! -f "$R/models/bge-small-en-v1.5/model.onnx" ]; then
    c=$(docker create --label dbtest=safe-to-remove "$IMG"); sudo -n mkdir -p "$R/models"
    sudo -n docker cp "$c:/models/bge-small-en-v1.5" "$R/models/bge-small-en-v1.5"; docker rm "$c" >/dev/null
fi
echo "$BGE_SHA  $R/models/bge-small-en-v1.5/model.onnx" | sha256sum -c -
# the ranker weights are the repo's (fitted without Jev labels), never an image's: rc8's acceptance image held another file
echo "ranker.json: $(sha256sum "$P/code/apps/search/serve/api/ranker.json")"
# the Wikipedia text index: same articles and ids as rc8, so rc8's folder with this release's stamp
if [ ! -d "$P/text/wiki" ]; then
    mkdir -p "$P/text"; cp -r --reflink=always "$R/w/rc8-in/text/wiki" "$P/text/wiki"
    python3 "$P/code/apps/search/release/buildbox/rc9-tvstamp.py" "$P/text/wiki" "$ID" wiki
fi
sudo -n rm -rf "$OUT" "$OUT.partial"
q=/r/w/rc9-in
# --publish: what a download host serves (manifest + files/<path>[.xz]); xz so the installer needs nothing but Python
sudo -n rm -rf "$R/w/rc9-pub"
"${PY[@]}" assemble.py build --release-id "$ID" --out /r/w/rc9-acc --publish /r/w/rc9-pub --compress xz --mode link \
  --registry $q/rc9/registry.sqlite --signals $q/rc9/signals.bin --lookup $q/rc9/lookup/lookup.sqlite \
  --places-osm $q/rc9/lookup/places-osm.sqlite --places-overture $q/rc9/lookup/places-overture.sqlite --hours-web $q/rc9/lookup/hours-web.sqlite \
  --pictures $q/rc9/pictures.sqlite --kinds $q/rc9-web/kinds.sqlite --anchor-phrases $q/rc9-web/anchor-phrases.sqlite \
  --typo $q/rc9/lookup/text/typo --wiki $q/text/wiki --sites $q/rc9-web/text/sites/sites --pages $q/rc9-web/text/sites/pages --anchors $q/rc9-web/text/sites/anchors \
  --ivf $q/ivf --nomic-dir /r/models/nomic-embed-text-v1.5 --rerank-dir /r/models/rerank-turbo-jevfree \
  --kinds-model-dir /r/models/bge-small-en-v1.5 --ranker $q/code/apps/search/serve/api/ranker.json \
  --stopwords $q/code/apps/search/release/english.stop --licence-text $q/code/apps/search/serve/api/licenses/Apache-2.0.txt \
  --licence-text $q/code/apps/search/serve/api/licenses/MIT-bge-small-en-v1.5.txt | tail -c 1500
echo; echo "== sign (TEST key) + verify"
"${PY[@]}" assemble.py sign /r/w/rc9-acc /r/w/rc9-pub --key /r/w/testkey/test-ed25519.pem | tail -c 300; echo
"${PY[@]}" assemble.py verify /r/w/rc9-acc --pubkey /r/w/testkey/test-ed25519.pub | tail -c 600; echo
echo "== Tranco-free publish gate"
"${PY[@]}" release_v1.py check /r/w/rc9-acc /r/w/testkey/test-ed25519.pub --tranco-free | tail -c 1500; echo
echo "== sqlguard on every SQLite file"
mapfile -t SQLITES < <(cd "$OUT" && for f in *.sqlite; do echo "/r/w/rc9-acc/$f"; done)
"${PY[@]}" release_v1.py sqlguard "${SQLITES[@]}" | tail -c 1500; echo
echo "== publish folder (checked by installing from it: install.py install --source /opt/logout-release/w/rc9-pub ...)"; du -sh "$R/w/rc9-pub"
echo "== files"; ( cd "$OUT" && find . -type f | LC_ALL=C sort | xargs du -b ) ; sha256sum "$OUT/manifest.json"; du -sh "$OUT"
