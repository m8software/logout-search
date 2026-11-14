#!/usr/bin/env bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9p step 7, run ON testbox: parts in /opt/logout-release/w/rc9p-in (buildbox stream + branch code) -> ONE release folder rc9p-acc and
# its publish folder rc9p-pub, UNSIGNED: the manifest is signed on the workstation with the release key, which never comes
# here for that (the sign/verify/check/sqlguard blocks of rc9's script run after the signature is copied back).
# rc9-in's files are hard-linked into the live rc9 / rc9f folders: anything taken from there is copied first (--reflink).
set -euo pipefail
ID=proto-20261023-rc9p; R=/opt/logout-release; P=$R/w/rc9p-in; OUT=$R/w/rc9p-acc
IMG=${IMG:-logout/search-api:kinds14}
PY=(docker run --rm --user 0 --entrypoint python --label dbtest=safe-to-remove -e PYTHONPATH=/r/w/pylib -e PYTHONDONTWRITEBYTECODE=1 -v "$R:/r" -w /r/w/rc9p-in/code/apps/search/release "$IMG")
# the meaning-search folder: Wikipedia ids are identical rc2 = rc9 = rc9p (step 1: type 1 rows identical), only its stamp changes
if [ ! -d "$P/ivf" ]; then
    cp -r --reflink=always "$R/w/proto-20261002-testbox/vectors/ivf2600" "$P/ivf"
    python3 - "$P/ivf/ivf.json" "$ID" <<'PYEOF'
import json, sys
p, rid = sys.argv[1:3]
m = json.load(open(p)); m.pop("restamped_from", None); m["release"] = rid
open(p, "w").write(json.dumps(m, indent=1, sort_keys=True) + "\n")
PYEOF
fi
( cd "$R/w/proto-20261002-testbox/vectors/ivf2600" && sha256sum codes.u8 ids.i32 offsets.i64 centroids.u8 ) > "$P/ivf-src.sha"; ( cd "$P/ivf" && sha256sum -c "$P/ivf-src.sha" )
BGE_SHA=828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35
echo "$BGE_SHA  $R/models/bge-small-en-v1.5/model.onnx" | sha256sum -c -
echo "ranker.json: $(sha256sum "$P/code/apps/search/serve/api/ranker.json")"
# the Wikipedia text index: rc9's folder (same articles and ids) with this release's stamp
if [ ! -d "$P/text/wiki" ]; then
    mkdir -p "$P/text"; cp -r --reflink=always "$R/w/rc9-in/text/wiki" "$P/text/wiki"
    python3 "$P/code/apps/search/release/buildbox/rc9-tvstamp.py" "$P/text/wiki" "$ID" wiki
fi
# facts.sqlite: rc9's file (keyed by wiki doc_id, identical in rc9p), restamped on a COPY (rc9-in's is hard-linked into rc9f-acc)
if [ ! -f "$P/rc9p/facts.sqlite" ]; then
    cp --reflink=always "$R/w/rc9-in/rc9/facts.sqlite" "$P/rc9p/facts.sqlite"
    # rc9p step 1 found the Wikipedia rows identical to rc9's (sha256 87fe8d7d033f986a...), which is what makes this valid
    python3 - "$P/rc9p/facts.sqlite" "$ID" <<'PYEOF'
import sqlite3, sys
p, rid = sys.argv[1:3]
db = sqlite3.connect(p)
db.execute("DELETE FROM meta WHERE key = 'restamped_from'")
db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("built_release", rid), ("restamp_reason",
    "the Wikipedia articles and their ids are unchanged since this file was built, and a rebuild from the same Wikidata "
    "fetch gives the same rows")])
db.commit()
db.execute("VACUUM")
assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0
PYEOF
fi
test "$(stat -c %h "$R/w/rc9-in/rc9/facts.sqlite")" -ge 2 && test "$(stat -c %i "$P/rc9p/facts.sqlite")" != "$(stat -c %i "$R/w/rc9-in/rc9/facts.sqlite")"
sudo -n rm -rf "$OUT" "$OUT.partial" "$R/w/rc9p-pub"
q=/r/w/rc9p-in
"${PY[@]}" assemble.py build --release-id "$ID" --out /r/w/rc9p-acc --publish /r/w/rc9p-pub --compress xz --mode link \
  --registry $q/rc9p/registry.sqlite --signals $q/rc9p/signals.bin --lookup $q/rc9p/lookup/lookup.sqlite \
  --places-osm $q/rc9p/lookup/places-osm.sqlite --places-overture $q/rc9p/lookup/places-overture.sqlite --hours-web $q/rc9p/lookup/hours-web.sqlite \
  --pictures $q/rc9p/pictures.sqlite --kinds $q/rc9p-web/kinds.sqlite --anchor-phrases $q/rc9p-web/anchor-phrases.sqlite --facts $q/rc9p/facts.sqlite \
  --typo $q/rc9p/lookup/text/typo --wiki $q/text/wiki --sites $q/rc9p-web/text/sites/sites --pages $q/rc9p-web/text/sites/pages --anchors $q/rc9p-web/text/sites/anchors \
  --ivf $q/ivf --nomic-dir /r/models/nomic-embed-text-v1.5 --rerank-dir /r/models/rerank-turbo-jevfree \
  --kinds-model-dir /r/models/bge-small-en-v1.5 --ranker $q/code/apps/search/serve/api/ranker.json \
  --stopwords $q/code/apps/search/release/english.stop --licence-text $q/code/apps/search/serve/api/licenses/Apache-2.0.txt \
  --licence-text $q/code/apps/search/serve/api/licenses/MIT-bge-small-en-v1.5.txt | tail -c 1500
echo; echo "== unsigned: sign manifest.json on the workstation, copy manifest.json.sig into rc9p-acc and rc9p-pub"
cmp "$OUT/manifest.json" "$R/w/rc9p-pub/manifest.json" && sha256sum "$OUT/manifest.json"
echo "== files"; ( cd "$OUT" && find . -type f | LC_ALL=C sort | xargs du -b ) | tail -n 120; du -sh "$OUT" "$R/w/rc9p-pub"
echo ASSEMBLE-DONE
