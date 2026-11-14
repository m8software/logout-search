#!/usr/bin/env bash
################################################################################
# Unit tests for the release assembler (docs/internal/SEARCH-INSTALLER.md):
# apps/search/release/assemble.py puts the parts of a release into ONE folder in the layout the
# engine reads, with manifest.json (sizes, sha256, licence per file, transport encodings), signs
# it with a TEST key, and runs release_check on the result. Everything runs on a SYNTHETIC
# release (tests/unit/lib/search_update_fixture.py + search_assemble_parts.py): the real
# registry/lookup builders, a Tantivy index, an IVF folder. Nothing here is real data.
#
# Needs a python with tantivy + numpy + cryptography, else SKIP.
# Usage: bash tests/unit/test-search-assemble.sh      (PY=<python with tantivy + numpy + cryptography>)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
finish() { echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]; }
# check "<description>" <command...>: PASS when the command exits 0 (its output is shown on FAIL)
check() { local d="$1"; shift; local o; if o=$("$@" 2>&1); then ok "$d"; else bad "$d" "$(head -c 1500 <<<"$o")"; fi; }
# refused "<description>" "<text the refusal must contain>" <command...>: exit 2 and the message
refused() {
    local d="$1" want="$2"; shift 2
    local o rc; o=$("$@" 2>&1); rc=$?
    if [[ $rc -eq 2 ]] && grep -qF -- "$want" <<<"$o"; then ok "$d"; else bad "$d" "rc=$rc want text: $want" "$(head -c 800 <<<"$o")"; fi
}

if ! "$PY" -c "import tantivy, numpy, cryptography" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy + numpy + cryptography"
    exit 0
fi

tmp=$(mktemp -d); trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
# shellcheck source=lib/search_assemble_setup.sh
source "$SCRIPT_DIR_T/lib/search_assemble_setup.sh"

echo ""; echo "=== assemble fx-1 from its parts ==="
if ! asm_setup; then bad "the synthetic release could not be built, assembled and signed"; finish; exit 1; fi
ok "fixture built; fx-1 and fx-2 assembled (--publish) and signed with a TEST key"

py() { "$PY" - "$@"; }
out=$(py "$REL1" "$PUB1" <<'PYEOF'
import json, os, sys
rel, pub = sys.argv[1:3]
want = ["registry.sqlite", "signals.bin", "lookup.sqlite", "places-osm.sqlite", "places-overture.sqlite", "hours-web.sqlite", "pictures.sqlite",
        "text/typo/meta.json", "text/typo/release.json", "text/wiki/meta.json", "text/sites/sites/meta.json", "text/sites/pages/meta.json",
        "text/sites/anchors/meta.json", "vectors/ivf/ivf.json", "vectors/ivf/codes.u8", "vectors/ivf/ids.i32", "vectors/ivf/offsets.i64",
        "vectors/ivf/centroids.u8", "models/nomic-embed-text-v1.5/model.onnx", "models/nomic-embed-text-v1.5/tokenizer.json",
        "english.stop", "LICENSES/NOTICE.txt", "LICENSES/Apache-2.0.txt", "manifest.json", "manifest.json.sig"]
print("layout", [w for w in want if not os.path.exists(os.path.join(rel, w))])
m = json.load(open(rel + "/manifest.json"))
print("release", m["release"], "from", m["from_release"], "format", m["format"])
paths = [f["path"] for f in m["files"]]
print("sorted and unique", paths == sorted(set(paths)))
print("no lock files", not any(p.endswith(".lock") for p in paths))
print("total", m["total_bytes"] == sum(f["bytes"] for f in m["files"]))
print("every file tagged", all(f["licence"] for f in m["files"]))
lic = m["licences"]
print("licences list every file once", sorted(p for v in lic.values() for p in v) == paths)
one = {k: v for k, v in lic.items() if len(v) == 1 and v[0].endswith(".sqlite")}
print("one sqlite per licence", sorted(v[0] for v in one.values()) == ["hours-web.sqlite", "lookup.sqlite", "pictures.sqlite", "places-osm.sqlite", "places-overture.sqlite", "registry.sqlite"])
print("osm/overture/hours licences", lic and [next(k for k, v in lic.items() if p in v)[:12] for p in ("places-osm.sqlite", "places-overture.sqlite", "hours-web.sqlite")])
notice = open(rel + "/LICENSES/NOTICE.txt").read()
print("notice names every file", all(p in notice for p in paths if not p.startswith("LICENSES/")), all(k in notice for k in lic))
print("engine stamps", m["engine"]["tantivy"], m["engine"]["registry"]["layout"], m["engine"]["vectors"]["ivf"], m["engine"]["analyzer"]["stopwords"]["words"])
pm = json.load(open(pub + "/manifest.json"))
print("published manifest is the release's", open(pub + "/manifest.json", "rb").read() == open(rel + "/manifest.json", "rb").read())
t = pm["transport"]["files"]
print("transport", "model.onnx stored as is:", "models/nomic-embed-text-v1.5/model.onnx" not in t, "something zstd:", any(x["encoding"] == "zstd" for x in t.values()))
rr = "models/rerank-turbo"
print("reranker files in the release", all(os.path.isfile(os.path.join(rel, rr, n)) for n in ("model_int8.onnx", "tokenizer.json")))
print("reranker manifest stanza", m["engine"].get("reranker"))
print("reranker files hashed", all(f["sha256"] and f["bytes"] > 0 for f in m["files"] if f["path"].startswith(rr + "/")), len([p for p in paths if p.startswith(rr + "/")]))
print("reranker licence", next((k for k, v in lic.items() if rr + "/model_int8.onnx" in v), "")[:10])
print("notice modification", "modified version of jinaai/jina-reranker-v1-turbo-en" in notice)
print("notice credits Stack Exchange", "Stack Exchange, CC BY-SA 2.5/3.0/4.0" in notice, "archive.org/details/stackexchange" in notice)
print("reranker stored as is", rr + "/model_int8.onnx" not in t)
print("published files exist", all(os.path.exists(os.path.join(pub, "files", p + ({"xz": ".xz", "zstd": ".zst"}[t[p]["encoding"]] if p in t else ""))) for p in paths))
PYEOF
)
chk() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1500 <<<"$out")"; fi; }
chk "layout []" "the folder has the layout the engine reads (SQLite files, signals, text/*, vectors/ivf, models/nomic, LICENSES)"
chk "reranker files in the release True" "the release holds models/rerank-turbo/{model_int8.onnx,tokenizer.json} (the real file names)"
chk "reranker manifest stanza {'cpu': 'models/rerank-turbo'}" "manifest.engine.reranker names the folder the installer points SEARCH_CE_DIR at"
chk "reranker files hashed True 2" "the reranker files are listed with size and sha256"
chk "reranker licence Apache-2.0" "the reranker carries an Apache-2.0 licence tag"
chk "notice modification True" "NOTICE states the reranker is a modified version of the Apache-2.0 base model"
chk "notice credits Stack Exchange True True" "NOTICE carries the Stack Exchange line the licence decision requires (CC BY-SA 2.5/3.0/4.0, official item)"
chk "reranker stored as is True" "the incompressible int8 reranker is stored as is by --publish"
chk "release fx-1 from None format 1" "manifest: release id, from_release null (a full release), format 1"
chk "sorted and unique True" "manifest file list is sorted and has no duplicates"
chk "no lock files True" "no Tantivy lock file is listed or shipped"
chk "total True" "manifest total_bytes is the sum of the file sizes"
chk "every file tagged True" "every file has a licence tag"
chk "licences list every file once True" "the per-licence file lists cover every file exactly once"
chk "one sqlite per licence True" "one SQLite file per licence (registry, lookup, osm, overture, hours, pictures)"
chk "notice names every file True True" "LICENSES/NOTICE.txt names every file and every licence"
chk "published manifest is the release's True" "the published manifest.json is byte for byte the release's"
chk "published files exist True" "every file is on the download side (as is, .zst or .xz)"
if grep -q "^transport model.onnx stored as is: True something zstd: True" <<<"$out"; then ok "transport: the incompressible model is stored as is, compressible files as zstd (the default since 2026-11-14)"; else bad "transport encodings" "$out"; fi
if grep -q "^engine stamps 0.26.2 3 logout-sem-ivf/1 3$" <<<"$out"; then ok "manifest carries the engine stamps (Tantivy, registry layout, IVF format, stop list)"; else bad "engine stamps" "$(grep '^engine' <<<"$out")"; fi

echo ""; echo "=== check, sign, verify ==="
out=$("${ASM[@]}" verify "$REL1" --pubkey "$KEYA.pub" 2>&1) && grep -q '"ok": true' <<<"$out" && ok "verify: engine checks + every sha256 + signature pass" || bad "verify on a fresh release" "$out"
out=$("${ASM[@]}" verify "$REL1" --pubkey "$KEYB.pub" 2>&1); [[ $? -ne 0 ]] && grep -q "signature invalid" <<<"$out" && ok "verify refuses a signature made by another key" || bad "wrong key accepted" "$out"
check "release_check.check_release(hashes=True) on the folder returns no problem" "$PY" -c "
import sys; sys.path.insert(0, '$REL'); import release_check as rc
e = rc.check_release('$REL1', pubkey='$KEYA.pub', hashes=True); sys.exit(1 if e else 0)"
check "the signature is a raw ed25519 signature (64 bytes) over manifest.json that the 'cryptography' package verifies" "$PY" -c "
from cryptography.hazmat.primitives import serialization
k = serialization.load_pem_public_key(open('$KEYA.pub','rb').read())
k.verify(open('$REL1/manifest.json.sig','rb').read(), open('$REL1/manifest.json','rb').read())"
refused "test-keygen refuses a path that does not say test (no production key is ever made here)" "name says 'test'" "${ASM[@]}" test-keygen "$tmp/prod-key"
[[ ! -e "$tmp/prod-key.key" ]] && ok "no key file was written for it" || bad "a key was written"
refused "sign refuses a key that is not ed25519" "not an ed25519 key" "$PY" -c "
import sys; sys.argv=['x']; sys.path.insert(0,'$REL'); import assemble
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
k = rsa.generate_private_key(65537, 2048)
open('$tmp/rsa.pem','wb').write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
try: assemble.sign(['$REL1'], '$tmp/rsa.pem')
except assemble.AssembleError as e: print('REFUSED:', e); sys.exit(2)"

echo ""; echo "=== hard links, move, copy ==="
out=$(py "$P1" "$REL1" <<'PYEOF'
import os, sys
parts, rel = sys.argv[1:3]
a, b = os.stat(parts + "/registry.sqlite"), os.stat(rel + "/registry.sqlite")
print("registry linked", a.st_ino == b.st_ino, b.st_nlink >= 3)
a, b = os.stat(parts + "/ivf/codes.u8"), os.stat(rel + "/vectors/ivf/codes.u8")
print("ivf linked", a.st_ino == b.st_ino)
a, b = os.stat(parts + "/wiki/meta.json"), os.stat(rel + "/text/wiki/meta.json")
print("tantivy linked", a.st_ino == b.st_ino)
a, b = os.stat(parts + "/rerank/model_int8.onnx"), os.stat(rel + "/models/rerank-turbo/model_int8.onnx")
print("rerank linked", a.st_ino == b.st_ino)
PYEOF
)
chk "registry linked True True" "default mode hard-links the SQLite files from the parts (no second copy)"
chk "rerank linked True" "the reranker model is hard-linked like the nomic model"
chk "ivf linked True" "the IVF folder is hard-linked"
chk "tantivy linked True" "Tantivy files are hard-linked"
mapfile -t M < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pm" "$R1" fx-1)
if "${ASM[@]}" build --release-id fx-1 --out "$tmp/relmove" --publish "$tmp/pubmove" --licence-text "$LIC" --mode move "${M[@]}" >/dev/null 2>&1 && [[ ! -e "$tmp/pm/registry.sqlite" && -f "$tmp/relmove/registry.sqlite" ]]; then ok "--mode move renames the parts into the release (the part is gone)"; else bad "--mode move"; fi
mapfile -t C < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pc" "$R1" fx-1)
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relcopy" --publish "$tmp/pubcopy" --licence-text "$LIC" --mode copy "${C[@]}" >/dev/null 2>&1
[[ "$(stat -c %i "$tmp/pc/registry.sqlite")" != "$(stat -c %i "$tmp/relcopy/registry.sqlite")" ]] && ok "--mode copy makes real copies (separate inodes)" || bad "--mode copy shares an inode"
cmp -s "$tmp/relmove/manifest.json" "$REL1/manifest.json" && cmp -s "$tmp/relcopy/manifest.json" "$REL1/manifest.json" && ok "link, move and copy give the same manifest.json bytes" || bad "manifest differs between modes"

echo ""; echo "=== deterministic ==="
mapfile -t D < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pd" "$R1" fx-1)
"${ASM[@]}" build --release-id fx-1 --out "$tmp/reld" --publish "$tmp/pubd" --licence-text "$LIC" "${D[@]}" >/dev/null 2>&1
tree_sha() { (cd "$1" && find . -type f ! -name 'manifest.json.sig' | LC_ALL=C sort | xargs sha256sum) | sha256sum | cut -d' ' -f1; }
cmp -s "$tmp/reld/manifest.json" "$REL1/manifest.json" && ok "assembling the same parts twice gives byte-identical manifest.json" || bad "manifest.json differs between two builds"
[[ "$(tree_sha "$tmp/pubd")" == "$(tree_sha "$PUB1")" ]] && ok "the published tree (xz payloads included) is byte-identical between two builds" || bad "published trees differ"
"${ASM[@]}" sign "$tmp/reld" --key "$KEYA.key" >/dev/null; cmp -s "$tmp/reld/manifest.json.sig" "$REL1/manifest.json.sig" && ok "the signature is deterministic too (ed25519)" || bad "signature differs"
mapfile -t D1 < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pj1" "$R1" fx-1)
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relj1" --publish "$tmp/pubj1" --jobs 1 --licence-text "$LIC" "${D1[@]}" >/dev/null 2>&1
mapfile -t D4 < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pj4" "$R1" fx-1)
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relj4" --publish "$tmp/pubj4" --jobs 4 --licence-text "$LIC" "${D4[@]}" >/dev/null 2>&1
[[ -s "$tmp/pubj1/manifest.json" && "$(tree_sha "$tmp/pubj1")" == "$(tree_sha "$tmp/pubj4")" && "$(tree_sha "$tmp/pubj4")" == "$(tree_sha "$PUB1")" ]] \
    && ok "--jobs 1 and --jobs 4 publish byte-identical trees" || bad "the published tree depends on --jobs"
mapfile -t DR < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pr" "$R1" fx-1)
reuse_err=$("${ASM[@]}" build --release-id fx-1 --out "$tmp/relr" --publish "$tmp/pubr" --reuse-publish "$PUB1" --licence-text "$LIC" "${DR[@]}" 2>&1 >/dev/null)
[[ "$(tree_sha "$tmp/pubr")" == "$(tree_sha "$PUB1")" && "$reuse_err" == *'"reused"'* && "$reuse_err" != *'"encoded"'* ]] \
    && ok "--reuse-publish takes every unchanged file's stored bytes from the previous publish: the same tree, nothing encoded" \
    || bad "--reuse-publish" "$reuse_err"

echo ""; echo "=== zstd and no compression ==="
if command -v zstd >/dev/null; then
    "${ASM[@]}" build --release-id fx-1 --out "$tmp/relz" --publish "$tmp/pubz" --compress zstd --licence-text "$LIC" "${D[@]}" >/dev/null 2>&1
    out=$("$PY" -c "
import json; m = json.load(open('$tmp/pubz/manifest.json')); print(sorted({t['encoding'] for t in m['transport']['files'].values()}))")
    [[ "$out" == "['zstd']" ]] && ok "--compress zstd stores files as .zst" || bad "zstd encodings" "$out"
fi
"${ASM[@]}" build --release-id fx-1 --out "$tmp/reln" --publish "$tmp/pubn" --compress none --licence-text "$LIC" "${D[@]}" >/dev/null 2>&1
out=$("$PY" -c "
import json; m = json.load(open('$tmp/pubn/manifest.json')); print(m['transport']['files'])")
[[ "$out" == "{}" ]] && ok "--compress none lists no transport encodings" || bad "none still compresses" "$out"

echo ""; echo "=== gov-states.json: whose state a government site is ==="
echo '{"source": "CISA .gov registry (public domain)", "domains": {"nj.gov": "NJ", "ca.gov": "CA"}}' > "$tmp/gov-states.json"
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relg" --publish "$tmp/pubg" --licence-text "$LIC" --gov-states "$tmp/gov-states.json" "${D[@]}" >/dev/null 2>&1
out=$("$PY" -c "
import json; m = json.load(open('$tmp/relg/manifest.json')); f = {x['path']: x for x in m['files']}
print('gov-states.json' in f, f.get('gov-states.json', {}).get('licence', '')[:34], f.get('gov-states.json', {}).get('bytes') == $(stat -c %s "$tmp/gov-states.json"))
n = open('$tmp/relg/LICENSES/NOTICE.txt').read(); print('CISA .gov registry' in n, 'gov-states.json' in n)")
[[ "$out" == $'True public domain (CISA .gov registry: True\nTrue True' ]] && ok "--gov-states ships gov-states.json with its public-domain licence tag, listed in the manifest and credited in NOTICE.txt" || bad "--gov-states" "$out"
out=$("$PY" -c "
import json, sys; sys.path.insert(0, '$REL'); import release_check as rc; print(rc.check_release('$tmp/relg'))")
[[ "$out" == "[]" ]] && ok "release_check passes a release that ships gov-states.json" || bad "release_check on gov-states" "$out"

echo ""; echo "=== vectors/pages-ivf: meaning search over pages ships in the release ==="
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relpv" --publish "$tmp/pubpv" --licence-text "$LIC" --pages-ivf "$REL1/vectors/ivf" "${D[@]}" >/dev/null 2>&1
out=$("$PY" -c "
import json, sys; sys.path.insert(0, '$REL'); import release_check as rc
m = json.load(open('$tmp/relpv/manifest.json')); f = {x['path']: x for x in m['files']}
print('vectors/pages-ivf/ivf.json' in f, f.get('vectors/pages-ivf/codes.u8', {}).get('licence', '')[:12], rc.check_release('$tmp/relpv'))")
[[ "$out" == "True CC-BY-SA-4.0 []" ]] && ok "--pages-ivf ships vectors/pages-ivf, tagged, and release_check passes" || bad "--pages-ivf" "$out"
cp -r "$REL1/vectors/ivf" "$tmp/pv-other"; sed -i 's/"release": *"fx-1"/"release": "fx-2"/' "$tmp/pv-other/ivf.json"
refused "a pages folder built for another release is refused" "built for release" "${ASM[@]}" build --release-id fx-1 --out "$tmp/relpv2" --licence-text "$LIC" --pages-ivf "$tmp/pv-other" "${D[@]}"
[[ ! -e "$tmp/reln/gov-states.json" ]] && ok "a release built without --gov-states has no gov-states.json (the engine's state rule stays off)" || bad "gov-states.json appeared without --gov-states"
echo '{"domains": {}}' > "$tmp/gov-bad.json"
refused "a gov-states.json of the wrong shape is refused" "\"domains\" is empty" "${ASM[@]}" build --release-id fx-1 --out "$tmp/xg" --gov-states "$tmp/gov-bad.json" "${D[@]}"
[[ ! -e "$tmp/xg" && ! -e "$tmp/xg.partial" ]] && ok "the refused build left nothing behind" || bad "refused gov-states build left files"

echo ""; echo "=== refusals leave nothing behind ==="
nothing() { [[ ! -e "$1" && ! -e "$1.partial" && ! -e "$2" && ! -e "$2.partial" ]]; }
# a part the stamps say is for another release
mapfile -t W < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pw" "$R1" fx-1)
refused "a release id the parts were not built for is refused (every stamp names its release)" "registry.sqlite: built for release 'fx-1', this release is 'fx-9'" \
    "${ASM[@]}" build --release-id fx-9 --out "$tmp/x1" "${W[@]}"
repl() { local a=(); for x in "${W[@]}"; do a+=("$x"); done; local k="$1" v="$2"; local o=(); local i=0; while [[ $i -lt ${#a[@]} ]]; do if [[ "${a[$i]}" == "--$k" ]]; then o+=("--$k" "$v"); i=$((i+2)); else o+=("${a[$i]}"); i=$((i+1)); fi; done; printf '%s\n' "${o[@]}"; }
mapfile -t W2 < <("$PY" "$LIBD/search_assemble_parts.py" "$tmp/pw2" "$R2" fx-2)
mapfile -t X < <(repl signals "$P2/signals.bin")
refused "signals.bin of another release is refused" "signals.bin: built for release 'fx-2'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x2" "${X[@]}"
mapfile -t X < <(repl ivf "$P2/ivf")
refused "an IVF folder of another release is refused" "built for release 'fx-2'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x3" "${X[@]}"
mapfile -t X < <(repl registry "$P2/registry.sqlite")
refused "a registry of another release is refused" "built for release 'fx-2'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x4" "${X[@]}"
mapfile -t X < <(repl wiki "$P2/wiki")
refused "a Tantivy index of another release is refused" "built for release 'fx-2'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x5" "${X[@]}"
# bad stamps made on copies
cp "$P1/registry.sqlite" "$tmp/badapp.sqlite"; chmod u+w "$tmp/badapp.sqlite"; sqlite3 "$tmp/badapp.sqlite" "PRAGMA application_id = 5" 2>/dev/null || "$PY" -c "
import sqlite3; c = sqlite3.connect('$tmp/badapp.sqlite'); c.execute('PRAGMA application_id = 5'); c.commit()"
mapfile -t X < <(repl registry "$tmp/badapp.sqlite")
refused "a SQLite file with a wrong application_id is refused" "stamp is application_id" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x6" "${X[@]}"
cp "$P1/lookup.sqlite" "$tmp/badrole.sqlite"; chmod u+w "$tmp/badrole.sqlite"
"$PY" -c "
import sqlite3; c = sqlite3.connect('$tmp/badrole.sqlite'); c.execute(\"UPDATE meta SET value='registry' WHERE key='role'\"); c.commit()"
mapfile -t X < <(repl lookup "$tmp/badrole.sqlite")
refused "a SQLite file with the wrong role is refused" "stamp says role 'registry', this part must be 'lookup'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x7" "${X[@]}"
cp -r "$P1/typo" "$tmp/typo-nosidecar"; rm -f "$tmp/typo-nosidecar/release.json"
mapfile -t X < <(repl typo "$tmp/typo-nosidecar")
refused "a Tantivy folder without its release.json sidecar is refused" "no release.json sidecar (the release stamp" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x8" "${X[@]}"
cp -r "$P1/typo" "$tmp/typo-old"; rm -f "$tmp/typo-old/release.json"
"$PY" -c "
import json; s = json.load(open('$P1/typo/release.json')); s['tantivy'] = '0.22.0'; json.dump(s, open('$tmp/typo-old/release.json','w'))"
mapfile -t X < <(repl typo "$tmp/typo-old")
refused "a Tantivy index written by another version is refused" "the engine reads 0.26." "${ASM[@]}" build --release-id fx-1 --out "$tmp/x9" "${X[@]}"
cp -r "$P1/typo" "$tmp/typo-stop"; rm -f "$tmp/typo-stop/release.json"
"$PY" -c "
import json; s = json.load(open('$P1/typo/release.json')); s['stopwords_sha256'] = '0'*64; json.dump(s, open('$tmp/typo-stop/release.json','w'))"
mapfile -t X < <(repl typo "$tmp/typo-stop")
refused "an index built with a different stop list is refused (release_check on the result)" "stop list differs" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x10" "${X[@]}"
nothing "$tmp/x10" "$tmp/x10p" && ok "...and nothing is left at --out" || bad "partial output left at x10"
# missing required part
mapfile -t X < <(printf '%s\n' "${W[@]}" | awk 'BEGIN{skip=0} skip{skip=0; next} $0=="--wiki"{skip=1; next} {print}')
refused "a missing required part (--wiki) is refused" "missing required part --wiki" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x11" "${X[@]}"
mapfile -t X < <(printf '%s\n' "${W[@]}" | awk 'BEGIN{skip=0} skip{skip=0; next} $0=="--pages"{skip=1; next} {print}')
refused "the web indexes come together (sites without pages is refused)" "come together" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x12" "${X[@]}"
mkdir "$tmp/nonmodel"; echo x > "$tmp/nonmodel/tokenizer.json"
mapfile -t X < <(repl nomic-dir "$tmp/nonmodel")
refused "a model folder without model.onnx is refused" "model.onnx is missing" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x13" "${X[@]}"
mapfile -t NR < <(printf '%s\n' "${W[@]}" | awk 'BEGIN{skip=0} skip{skip=0; next} $0=="--rerank-dir"{skip=1; next} {print}')
refused "the reranker is required: a build with neither --rerank-dir nor --no-reranker is refused" "missing required part --rerank-dir" \
    "${ASM[@]}" build --release-id fx-1 --out "$tmp/x15" "${NR[@]}"
nothing "$tmp/x15" "$tmp/x15p" && ok "...and nothing is left at --out" || bad "partial output left at x15"
refused "--rerank-dir together with --no-reranker is refused" "pick one" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x16" --no-reranker "${W[@]}"
mkdir "$tmp/norr"; echo x > "$tmp/norr/model_int8.onnx"
mapfile -t X < <(repl rerank-dir "$tmp/norr")
refused "a reranker folder without tokenizer.json is refused" "tokenizer.json is missing" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x17" "${X[@]}"
mkdir "$tmp/norr2"; echo x > "$tmp/norr2/tokenizer.json"
mapfile -t X < <(repl rerank-dir "$tmp/norr2")
refused "a reranker folder without a model file is refused" "model_int8.onnx (or model.onnx) is missing" "${ASM[@]}" build --release-id fx-1 --out "$tmp/x18" "${X[@]}"
refused "an --out that already holds files is refused (two releases are never mixed)" "not empty" "${ASM[@]}" build --release-id fx-1 --out "$REL1" "${W[@]}"
refused "a release id with a slash is refused" "no slashes" "${ASM[@]}" build --release-id ../fx --out "$tmp/x14" "${W[@]}"
refused "a file with no licence tag is refused" "no licence tag for surprise.bin" "$PY" -c "
import sys; sys.path.insert(0,'$REL'); import assemble
try: assemble.licence_of('surprise.bin')
except assemble.AssembleError as e: print('REFUSED:', e); sys.exit(2)"

echo ""; echo "=== --no-reranker (test fixtures) and release_check on the reranker ==="
if "${ASM[@]}" build --release-id fx-1 --out "$tmp/relnr" --no-reranker "${NR[@]}" >/dev/null 2>&1; then
    out=$("$PY" -c "
import json, os; m = json.load(open('$tmp/relnr/manifest.json'))
print('reranker' in m['engine'], os.path.exists('$tmp/relnr/models/rerank-turbo'))")
    [[ "$out" == "False False" ]] && ok "--no-reranker builds a release with no reranker folder and no engine.reranker stanza" || bad "--no-reranker still shipped one" "$out"
else bad "--no-reranker build failed"; fi
out=$("${ASM[@]}" verify "$tmp/relnr" 2>&1) && grep -q '"ok": true' <<<"$out" && ok "a release without the reranker still passes verify (old releases in the field apply)" || bad "reranker-less release fails verify" "$out"
cp -r --reflink=auto "$REL1" "$tmp/relrc" 2>/dev/null; chmod -R u+w "$tmp/relrc"
"$PY" - "$tmp/relrc" <<'PYEOF'
import json, sys
p = sys.argv[1] + "/manifest.json"; m = json.load(open(p))
m["files"] = [f for f in m["files"] if f["path"] != "models/rerank-turbo/tokenizer.json"]
json.dump(m, open(p, "w"))
PYEOF
out=$("${ASM[@]}" verify "$tmp/relrc" 2>&1); [[ $? -ne 0 ]] && grep -q "declares a reranker but does not list it" <<<"$out" && ok "release_check refuses a manifest that declares the reranker but does not list its tokenizer" || bad "declared-but-unlisted reranker accepted" "$out"
"$PY" - "$tmp/relrc" <<'PYEOF'
import json, sys
p = sys.argv[1] + "/manifest.json"; m = json.load(open(p))
m["files"].append({"path": "models/rerank-turbo/tokenizer.json", "bytes": 0, "sha256": "", "licence": "x"})   # back, so only the model file is the problem
m["files"] = [f for f in m["files"] if f["path"] != "models/rerank-turbo/model_int8.onnx"]
json.dump(m, open(p, "w"))
PYEOF
out=$("${ASM[@]}" verify "$tmp/relrc" 2>&1); [[ $? -ne 0 ]] && grep -q "lists no model file" <<<"$out" && ok "release_check refuses a manifest that declares the reranker but lists no model file" || bad "declared reranker without a model file accepted" "$out"
"$PY" - "$tmp/relrc" <<'PYEOF'
import json, sys
p = sys.argv[1] + "/manifest.json"; m = json.load(open(p))
m["engine"]["reranker"] = {"cpu": "../../etc"}
json.dump(m, open(p, "w"))
PYEOF
out=$("${ASM[@]}" verify "$tmp/relrc" 2>&1); [[ $? -ne 0 ]] && grep -q "does not name a models/<dir> folder" <<<"$out" && ok "release_check refuses a reranker path outside models/" || bad "bad reranker path accepted" "$out"

echo ""; echo "=== verify catches damage ==="
"${ASM[@]}" build --release-id fx-1 --out "$tmp/relt" --publish "$tmp/pubt" --licence-text "$LIC" --mode copy "${D[@]}" >/dev/null 2>&1
"${ASM[@]}" sign "$tmp/relt" --key "$KEYA.key" >/dev/null
out=$("${ASM[@]}" verify "$tmp/relt" --pubkey "$KEYA.pub" 2>&1) && ok "a copy-mode release verifies" || bad "copy release does not verify" "$out"
chmod u+w "$tmp/relt/lookup.sqlite"; "$PY" -c "
f = open('$tmp/relt/lookup.sqlite', 'r+b'); f.seek(5000); b = f.read(1); f.seek(5000); f.write(bytes([b[0] ^ 1]))"
out=$("${ASM[@]}" verify "$tmp/relt" --pubkey "$KEYA.pub" 2>&1); [[ $? -ne 0 ]] && grep -q "lookup.sqlite: sha256 differs" <<<"$out" && ok "one changed byte in lookup.sqlite is found (sha256)" || bad "a tampered file passed verify" "$out"
echo stray > "$tmp/rel2/stray.txt"
out=$("${ASM[@]}" verify "$tmp/rel2" 2>&1); [[ $? -ne 0 ]] && grep -q "stray.txt: in the folder but not in the manifest" <<<"$out" && ok "a file the manifest does not list is found" || bad "stray file passed verify" "$out"
rm -f "$tmp/rel2/stray.txt"
mkdir "$tmp/vdir" && cp -r "$tmp/relt/." "$tmp/vdir/" 2>/dev/null; chmod -R u+w "$tmp/vdir"
"$PY" - "$tmp/vdir/manifest.json" <<'PYEOF'
import json, sys
p = sys.argv[1]
m = json.load(open(p))
m["from_release"] = "fx-0"
open(p, "wb").write(json.dumps(m, indent=1, sort_keys=True).encode())
PYEOF
out=$("${ASM[@]}" verify "$tmp/vdir" 2>&1); grep -q "from_release is 'fx-0'" <<<"$out" && ok "verify refuses a manifest that names a predecessor (a full release has none)" || bad "from_release not checked by verify" "$out"
"$PY" - "$tmp/vdir/manifest.json" <<'PYEOF'
import json, sys
p = sys.argv[1]
m = json.load(open(p))
m["from_release"] = None
m["files"][0]["licence"] = ""
open(p, "wb").write(json.dumps(m, indent=1, sort_keys=True).encode())
PYEOF
out=$("${ASM[@]}" verify "$tmp/vdir" 2>&1); grep -q "no licence tag" <<<"$out" && ok "verify refuses a file without a licence tag" || bad "licence tag not checked by verify" "$out"
printf ' ' >> "$tmp/relcopy/manifest.json"
out=$("${ASM[@]}" verify "$tmp/relcopy" --pubkey "$KEYA.pub" 2>&1); [[ $? -ne 0 ]] && ok "a changed manifest.json fails (no valid signature)" || bad "changed manifest accepted" "$out"

echo ""; echo "=== the engine opens it ==="
READER=("$PY" "$LIBD/search_update_reader.py")
"${READER[@]}" "$REL1" > "$tmp/read1.out" 2>&1
if grep -q "^opened release fx-1" "$tmp/read1.out" && grep -q "exact-search ok True" "$tmp/read1.out"; then ok "ReleaseLookups opens the assembled release, answers lookups, and IvfIndex searches its meaning index"; else bad "engine could not open the assembled release" "$(tail -8 "$tmp/read1.out")"; fi
out=$("$PY" - "$REL1" "$REPO_ROOT" <<'PYEOF' 2>&1
import os, sys
rel, repo = sys.argv[1:3]
api = os.path.join(repo, "apps", "search", "serve", "api")
sys.path.insert(0, api)
import tantivy, tv_open
for d in ("text/typo", "text/wiki", "text/sites/sites", "text/sites/pages", "text/sites/anchors"):
    idx = tv_open.open_index(tantivy, os.path.join(rel, d))
    print("open", d, idx.searcher().num_docs > 0)
PYEOF
)
[[ "$(grep -c '^open .* True$' <<<"$out")" == 5 ]] && ok "all five Tantivy folders (typo, wiki, sites, pages, anchors) open through the engine's read-only helper" || bad "a Tantivy folder did not open" "$out"
echo ""; echo "=== the stop-list stamp of each Tantivy folder (rc4) ==="
# real data: rc4's typo index has no stopwords_sha256 (raw tokenizer, no stop list) and the assembler refused the whole release
out=$("$PY" - "$REL1" <<'PYEOF' 2>&1
import json, sys
rel = sys.argv[1]
print("typo stamp", "stopwords_sha256" in json.load(open(rel + "/text/typo/release.json")), "wiki stamp", "stopwords_sha256" in json.load(open(rel + "/text/wiki/release.json")))
PYEOF
)
grep -qxF "typo stamp False wiki stamp True" <<<"$out" && ok "the fixture is like real data: text/typo carries no stop-list stamp, text/wiki does (and fx-1 assembled)" || bad "fixture stop-list stamps" "$out"
stopcase() { # name role-folder python-edit-of-the-sidecar
    rm -rf "$tmp/stopc"; cp -r "$REL1" "$tmp/stopc"; chmod -R u+w "$tmp/stopc"
    "$PY" - "$tmp/stopc/$1/release.json" "$2" <<'PYEOF'
import json, os, sys
p, edit = sys.argv[1:3]
side = json.load(open(p)); os.unlink(p)
if edit == "drop": side.pop("stopwords_sha256", None)
else: side["stopwords_sha256"] = "0" * 64
open(p, "w").write(json.dumps(side, sort_keys=True) + "\n")
PYEOF
    "$PY" "$REL/release_v1.py" check "$tmp/stopc" 2>&1
}
out=$(stopcase text/wiki drop); grep -q "text/wiki: stop list differs" <<<"$out" && ok "a wiki index with no stop-list stamp is refused" || bad "wiki index without a stop-list stamp accepted" "$out"
out=$(stopcase text/sites/sites wrong); grep -q "text/sites/sites: stop list differs" <<<"$out" && ok "a sites index built with another stop list is refused" || bad "sites index with another stop list accepted" "$out"
out=$(stopcase text/typo wrong); grep -q "text/typo: stop list differs" <<<"$out" && ok "a typo index that does carry a stamp must still match" || bad "typo index with a wrong stamp accepted" "$out"

echo ""; echo "=== the optional kinds and anchor-phrases files (rc4) ==="
# a release file is accepted by its stamp: application_id, format, role, built_release (the builders' own stamp)
stamped() { "$PY" - "$1" "$2" "$3" <<'PYEOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("PRAGMA application_id = 0x6C6F7331; PRAGMA user_version = 1; CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE t (x)")
db.executemany("INSERT INTO meta VALUES (?,?)", [("format", "1"), ("role", sys.argv[2]), ("built_release", sys.argv[3])])
db.commit()
PYEOF
}
stamped "$tmp/kinds.sqlite" kinds fx-1; stamped "$tmp/ap.sqlite" anchor-phrases fx-1; stamped "$tmp/kinds-other.sqlite" kinds fx-2; stamped "$tmp/notkinds.sqlite" lookup fx-1
RELK="$tmp/relk"
KM="$(dirname "$(printf '%s\n' "${ARGS1[@]}" | grep -A1 -x -- --ranker | tail -1)")/kindsmodel"
refused "--kinds without the model that embeds the query is refused" "--kinds without --kinds-model-dir" "${ASM[@]}" build --release-id fx-1 --out "$tmp/relk0" --licence-text "$LIC" "${ARGS1[@]}" --kinds "$tmp/kinds.sqlite"
mapfile -t NORK < <(printf '%s\n' "${ARGS1[@]}" | awk 'skip {skip=0; next} $0 == "--ranker" {skip=1; next} {print}')
refused "a release without the ranker weights is refused" "missing required part --ranker" "${ASM[@]}" build --release-id fx-1 --out "$tmp/relk00" --licence-text "$LIC" "${NORK[@]}"
echo '{"weights": [1]}' > "$tmp/badranker.json"
refused "a file that is not a ranker.json is refused as --ranker" "not a ranker.json" "${ASM[@]}" build --release-id fx-1 --out "$tmp/relk01" --licence-text "$LIC" "${NORK[@]}" --ranker "$tmp/badranker.json"
if "${ASM[@]}" build --release-id fx-1 --out "$RELK" --licence-text "$LIC" "${ARGS1[@]}" --kinds "$tmp/kinds.sqlite" --kinds-model-dir "$KM" --anchor-phrases "$tmp/ap.sqlite" >"$tmp/relk.out" 2>&1; then
    ok "a release assembles with --kinds and --anchor-phrases"
else
    bad "assemble with --kinds and --anchor-phrases failed" "$(tail -c 800 "$tmp/relk.out")"
fi
out=$("$PY" - "$RELK" <<'PYEOF' 2>&1
import json, os, sys
rel = sys.argv[1]
m = json.load(open(rel + "/manifest.json"))
lic = m["licences"]
import hashlib
e = m["engine"]
print("kinds model", e.get("kinds_model"), os.path.isfile(rel + "/models/bge-small-en-v1.5/model.onnx"),
      [k[:4] for k, v in lic.items() if "models/bge-small-en-v1.5/model.onnx" in v])
print("ranker", e["ranker"]["file"], e["ranker"]["trained_on"], e["ranker"]["sha256"] == hashlib.sha256(open(rel + "/ranker.json", "rb").read()).hexdigest(),
      [k[:7] for k, v in lic.items() if "ranker.json" in v])
for f in ("kinds.sqlite", "anchor-phrases.sqlite"):
    own = [k for k, v in lic.items() if f in v]
    print(f, "in folder", os.path.isfile(os.path.join(rel, f)), "listed", f in [x["path"] for x in m["files"]], "own licence", len(own) == 1 and lic[own[0]] == [f], own[0][:12] if own else "")
PYEOF
)
grep -qxF "kinds model models/bge-small-en-v1.5 True ['MIT ']" <<<"$out" && ok "the kinds model is in the release, named in the manifest, under its MIT tag" || bad "kinds model placement" "$out"
grep -qxF "ranker ranker.json 7 True ['CC0-1.0']" <<<"$out" && ok "the ranker weights are in the release; the manifest records their sha256 and training size" || bad "ranker placement" "$out"
envk=$("$PY" - "$REPO_ROOT/apps/search/installer" "$RELK" "$tmp" <<'PYEOF' 2>&1
import os, sys
sys.path.insert(0, sys.argv[1])
import install
base = os.path.join(sys.argv[3], "envbase"); os.makedirs(base, exist_ok=True)
if not os.path.lexists(os.path.join(base, "current")):
    os.symlink(sys.argv[2], os.path.join(base, "current"))
env = install.engine_env(base)
print("env", env.get("SEARCH_MODEL_DIR", "").endswith("current/models/bge-small-en-v1.5"), env.get("SEARCH_REQUIRE_KINDS_MODEL"), env.get("SEARCH_RANKER", "").endswith("current/ranker.json"))
os.rename(sys.argv[2] + "/ranker.json", sys.argv[2] + "/ranker.gone")
try:
    install.engine_env(base); print("missing ranker accepted")
except install.InstallError as e:
    print("missing ranker refused", "ranker" in str(e))
os.rename(sys.argv[2] + "/ranker.gone", sys.argv[2] + "/ranker.json")
PYEOF
)
grep -qxF "env True 1 True" <<<"$envk" && ok "the installer points the engine at the release's kinds model (and requires it) and at its ranker weights" || bad "installer environment" "$envk"
grep -qxF "missing ranker refused True" <<<"$envk" && ok "the installer refuses a release that names ranker weights it does not hold" || bad "a missing ranker file is not refused" "$envk"
grep -qxF "kinds.sqlite in folder True listed True own licence True CC-BY-SA-4.0" <<<"$out" && ok "kinds.sqlite is in the folder, in the manifest, alone under its CC-BY-SA licence tag" || bad "kinds.sqlite placement or licence" "$out"
grep -qxF "anchor-phrases.sqlite in folder True listed True own licence True CC0-1.0 (M8 " <<<"$out" && ok "anchor-phrases.sqlite is in the folder, in the manifest, alone under its own licence tag" || bad "anchor-phrases.sqlite placement or licence" "$out"
out=$("${ASM[@]}" verify "$RELK" 2>&1) && grep -q '"ok": true' <<<"$out" && ok "verify passes on a release that carries kinds and anchor-phrases" || bad "verify with kinds and anchor-phrases" "$out"
refused "a kinds file built for another release is refused" "built for release 'fx-2'" "${ASM[@]}" build --no-kinds-model --release-id fx-1 --out "$tmp/relk2" --licence-text "$LIC" "${ARGS1[@]}" --kinds "$tmp/kinds-other.sqlite"
refused "a file with another role is refused as --kinds" "this part must be 'kinds'" "${ASM[@]}" build --no-kinds-model --release-id fx-1 --out "$tmp/relk3" --licence-text "$LIC" "${ARGS1[@]}" --kinds "$tmp/notkinds.sqlite"
refused "a file with another role is refused as --anchor-phrases" "this part must be 'anchor-phrases'" "${ASM[@]}" build --release-id fx-1 --out "$tmp/relk4" --licence-text "$LIC" "${ARGS1[@]}" --anchor-phrases "$tmp/kinds.sqlite"

out=$("$PY" - "$REPO_ROOT" "$REL" <<'PYEOF' 2>&1
import ast, os, sys
repo, rel = sys.argv[1:3]
sys.path.insert(0, rel)
import assemble, update_common
src = open(os.path.join(repo, "apps/search/serve/api/sem_ivf.py")).read()
fmt = [n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "FORMAT"][0]
print("ivf format agrees", assemble.IVF_FORMAT == update_common.IVF_FORMAT == fmt)
PYEOF
)
grep -q "ivf format agrees True" <<<"$out" && ok "the IVF format string agrees between assemble.py, update_common.py and sem_ivf.py" || bad "IVF format drift" "$out"
finish
