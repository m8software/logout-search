#!/usr/bin/env bash
# requires: cmd:python3 cmd:openssl
################################################################################
# Unit tests for the Hugging Face dataset a home engine installs and updates from (SEARCH-HOME-ENGINE-PLAN.md §5):
# apps/search/release/hf_dataset.py (stage, check), installer/install.py's `hf:` source and
# apps/search/release/update_fetch.py. A SYNTHETIC publish folder (not a real release: the engine's release check is
# test-search-install.sh's) and update package, signed with a throwaway openssl ed25519 key; the dataset is served by
# tests/unit/lib/search_install_server.py (Range, request log) in Hugging Face's URL layout
# (datasets/ORG/NAME/resolve/<rev>/...), so nothing here reaches the network.
#   1. stage: the release and the package as published, readable licence texts, a LICENSE line per file, a card, a
#      signed latest.json and package.json; it refuses an incomplete publish folder and a package for another release
#   2. check: passes the staged folder; refuses a changed file, an unlisted file and a wrong key
#   3. the installer's hf: source: latest.json read and verified, the release read from its TAG, a forged latest.json and
#      a manifest latest.json does not name refused
#   4. update_fetch: up to date / update / full-install decided from latest.json; the package downloaded from the tag,
#      every file checked, a dropped connection resumed, a tampered file refused
#   5. upload order, with a stand-in huggingface_hub
#   6. counts and card: the card's numbers are read from the release's SQLite files, and `card` re-renders the card alone
# Not covered: the upload itself (needs a Hugging Face account and huggingface_hub), applying the package
# (test-search-update-package.sh).
#
# Usage: bash tests/unit/test-search-hf-dataset.sh
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
REL="$REPO_ROOT/apps/search/release"
export PYTHONDONTWRITEBYTECODE=1

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
finish() { echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]; }
refused() {     # refused "<description>" "<text the refusal must contain>" <command...>
    local d="$1" want="$2"; shift 2
    local o rc; o=$("$@" 2>&1); rc=$?
    if [[ $rc -eq 2 ]] && grep -qF -- "$want" <<<"$o"; then ok "$d"; else bad "$d" "rc=$rc want text: $want" "$(head -c 600 <<<"$o")"; fi
}
command -v openssl >/dev/null || { echo "  SKIP: no openssl"; exit 0; }

tmp=$(mktemp -d); SERVERS=()
cleanup() { for p in "${SERVERS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null; done; rm -rf "$tmp"; }
trap cleanup EXIT
HF=(python3 "$REL/hf_dataset.py")
UF=(python3 "$REL/update_fetch.py")

# ---- a synthetic publish folder (fx-1 -> fx-2) and an update package, signed with key A
openssl genpkey -algorithm ed25519 -out "$tmp/a.pem" 2>/dev/null && openssl pkey -in "$tmp/a.pem" -pubout -out "$tmp/a.pub" 2>/dev/null
openssl genpkey -algorithm ed25519 -out "$tmp/b.pem" 2>/dev/null && openssl pkey -in "$tmp/b.pem" -pubout -out "$tmp/b.pub" 2>/dev/null
python3 - "$tmp" <<'PY' || { bad "fixture"; finish; exit 1; }
import hashlib, json, lzma, os, subprocess, sys
t = sys.argv[1]
def sign(body, out):
    open(f"{t}/msg", "wb").write(body)                   # ed25519 signs in one shot: openssl needs a file
    subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", f"{t}/a.pem", "-in", f"{t}/msg", "-out", out], check=True)
def publish(rid, content):
    pub = f"{t}/pub-{rid}"
    files, transport = [], {}
    for path, data, lic, enc in content:
        os.makedirs(os.path.dirname(f"{pub}/files/{path}"), exist_ok=True)
        files.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "licence": lic})
        if enc:
            z = lzma.compress(data, format=lzma.FORMAT_XZ)
            open(f"{pub}/files/{path}.xz", "wb").write(z)
            transport[path] = {"encoding": "xz", "bytes": len(z), "sha256": hashlib.sha256(z).hexdigest()}
        else:
            open(f"{pub}/files/{path}", "wb").write(data)
    lics = {}
    for f in files:
        lics.setdefault(f["licence"], []).append(f["path"])
    m = {"release": rid, "format": 1, "from_release": None, "files": files, "licences": lics, "total_bytes": sum(f["bytes"] for f in files),
         "transport": {"compress": "xz", "files": transport}, "engine": {}}
    raw = json.dumps(m, indent=1, sort_keys=True).encode()
    open(f"{pub}/manifest.json", "wb").write(raw)
    sign(raw, f"{pub}/manifest.json.sig")
    return raw
base = [("LICENSES/NOTICE.txt", b"Wikipedia: CC BY-SA 4.0 ...\n", "licence texts and notice written by the assembler", True),
        ("LICENSES/CC0-1.0.txt", b"CC0 text\n", "licence texts and notice written by the assembler", True)]
r1 = publish("fx-1", base + [("lookup.sqlite", b"L" * 5000, "CC-BY-SA-4.0 (Wikipedia)", True), ("signals.bin", os.urandom(3000), "CC0-1.0", False)])
r2 = publish("fx-2", base + [("lookup.sqlite", b"M" * 5000, "CC-BY-SA-4.0 (Wikipedia)", True), ("signals.bin", os.urandom(3000), "CC0-1.0", False),
                             ("places-osm.sqlite", b"P" * 7000, "ODbL-1.0 (OpenStreetMap contributors)", True)])
def package(frm_raw, to_raw, frm, to, out):
    os.makedirs(f"{out}/blobs")
    open(f"{out}/blobs/aa.zst", "wb").write(os.urandom(200_000))           # big enough to drop and resume
    open(f"{out}/tombstones.json", "wb").write(b'{"count":0}')
    body = json.dumps({"format": 1, "from_release": frm, "to_release": to, "from_manifest_sha256": hashlib.sha256(frm_raw).hexdigest(),
                       "to_manifest_sha256": hashlib.sha256(to_raw).hexdigest(), "files": []}, sort_keys=True).encode()
    open(f"{out}/update.json", "wb").write(body)
    sign(body, f"{out}/update.json.sig")
package(r1, r2, "fx-1", "fx-2", f"{t}/pkg-1-2")
package(r1, r1, "fx-0", "fx-1", f"{t}/pkg-0-1")        # an update INTO fx-1: refused when staging fx-2
PY
ok "fixture: publish folders fx-1 and fx-2, an update package fx-1 -> fx-2, signed with a throwaway key"

echo ""; echo "=== 1. stage ==="
ST="$tmp/stage"
out=$("${HF[@]}" stage "$tmp/pub-fx-2" "$ST" --repo test-org/logout-search --sign "$tmp/a.pem" --update "$tmp/pkg-1-2" --test-key 2>&1)
[[ $? -eq 0 ]] && ok "stage exits 0" || bad "stage failed" "$out"
for p in manifest.json manifest.json.sig files/lookup.sqlite.xz files/signals.bin files/places-osm.sqlite.xz updates/fx-1/update.json \
         updates/fx-1/blobs/aa.zst updates/fx-1/package.json updates/fx-1/package.json.sig LICENSES/NOTICE.txt LICENSE README.md latest.json latest.json.sig; do
    [[ -f "$ST/$p" ]] || { bad "staged folder lacks $p"; continue; }
done
ok "the staged folder holds the release, the package, the licences, the card and latest.json"
cmp -s "$ST/files/lookup.sqlite.xz" "$tmp/pub-fx-2/files/lookup.sqlite.xz" && cmp -s "$ST/manifest.json" "$tmp/pub-fx-2/manifest.json" \
    && ok "the release is staged byte for byte as published" || bad "staged release differs from the publish folder"
grep -qx "Wikipedia: CC BY-SA 4.0 ..." "$ST/LICENSES/NOTICE.txt" && ok "LICENSES/NOTICE.txt is readable (decoded from its .xz)" || bad "NOTICE not readable" "$(head -c 200 "$ST/LICENSES/NOTICE.txt")"
grep -q "^files/places-osm.sqlite.xz$" "$ST/LICENSE" && grep -q "ODbL-1.0" <<<"$(grep -A1 "^files/places-osm.sqlite.xz$" "$ST/LICENSE")" \
    && ok "LICENSE names each stored file's licence (files/places-osm.sqlite.xz: ODbL)" || bad "LICENSE line missing" "$(head -20 "$ST/LICENSE")"
grep -q "^license: other$" "$ST/README.md" && grep -q "^license_link: LICENSE$" "$ST/README.md" && grep -q "TEST key" "$ST/README.md" \
    && grep -q "hf:test-org/logout-search" "$ST/README.md" && ok "the card: license other -> LICENSE, the install line, the test-key warning" \
    || bad "card header/body wrong" "$(head -20 "$ST/README.md")"
python3 -c "import json,sys; d=json.load(open('$ST/latest.json')); sys.exit(0 if d['release']=='fx-2' and d['revision']=='fx-2' and d['updates']=={'fx-1':'updates/fx-1'} else 1)" \
    && ok "latest.json: release fx-2 at tag fx-2, update from fx-1" || bad "latest.json wrong" "$(cat "$ST/latest.json")"
refused "stage refuses a publish folder with a missing file" "not a complete publish folder" \
    bash -c "cp -r '$tmp/pub-fx-2' '$tmp/pub-broken' && rm '$tmp/pub-broken/files/signals.bin' && ${HF[*]} stage '$tmp/pub-broken' '$tmp/st-b' --repo a/b --sign '$tmp/a.pem'"
refused "stage refuses a package that updates to another release" "updates to 'fx-1'" \
    "${HF[@]}" stage "$tmp/pub-fx-2" "$tmp/st-c" --repo a/b --sign "$tmp/a.pem" --update "$tmp/pkg-0-1"
refused "stage refuses an existing output folder" "exists" "${HF[@]}" stage "$tmp/pub-fx-2" "$ST" --repo a/b --sign "$tmp/a.pem"

echo ""; echo "=== 2. check ==="
out=$("${HF[@]}" check "$ST" --pubkey "$tmp/a.pub" 2>&1) && grep -q '"ok": true' <<<"$out" && ok "check passes the staged folder" || bad "check failed" "$out"
refused "check refuses another key" "not signed by the pinned key" "${HF[@]}" check "$ST" --pubkey "$tmp/b.pub"
cp -r "$ST" "$tmp/st-x"; printf 'X' | dd of="$tmp/st-x/updates/fx-1/blobs/aa.zst" bs=1 seek=10 conv=notrunc 2>/dev/null
refused "check refuses a changed package file" "wrong sha256" "${HF[@]}" check "$tmp/st-x" --pubkey "$tmp/a.pub"
cp -r "$ST" "$tmp/st-y"; echo extra > "$tmp/st-y/files/stray.bin"
refused "check refuses a file nothing lists" "nothing lists" "${HF[@]}" check "$tmp/st-y" --pubkey "$tmp/a.pub"

# ---- the dataset as Hugging Face serves it: main = fx-2 + latest.json; tag fx-2 = the same files without latest.json
HFR="$tmp/hf"; DS="$HFR/datasets/test-org/logout-search/resolve"
mkdir -p "$DS"; cp -r "$ST" "$DS/main"; cp -r "$ST" "$DS/fx-2"; rm "$DS/fx-2/latest.json" "$DS/fx-2/latest.json.sig"
python3 "$SCRIPT_DIR_T/lib/search_install_server.py" "$HFR" "$tmp/port" "$tmp/req.log" --drop "datasets/test-org/logout-search/resolve/fx-2/updates/fx-1/blobs/aa.zst:65536" &
SERVERS+=("$!"); for _ in $(seq 1 50); do [[ -s "$tmp/port" ]] && break; sleep 0.1; done
LOGOUT_HF_ENDPOINT="http://127.0.0.1:$(cat "$tmp/port")"; export LOGOUT_HF_ENDPOINT

echo ""; echo "=== 3. the installer's hf: source ==="
out=$(python3 -S -E - "$REPO_ROOT" "$tmp/a.pub" <<'PY' 2>&1
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "apps", "search", "installer"))
import install as I
pub = I.load_pubkey(sys.argv[2])
where, latest = I.resolve_source("hf:test-org/logout-search", pub)
print("where", where.split("/datasets/")[1]); print("latest", latest["release"])
where, latest = I.resolve_source("hf:test-org/logout-search@fx-2", pub)
print("pinned", where.split("/datasets/")[1], latest)
for bad_arg in ("hf:nope", "hf:a/b@../x"):
    try:
        I.resolve_source(bad_arg, pub); print("accepted", bad_arg)
    except I.InstallError as e:
        print("refused", bad_arg)
PY
)
grep -q "^where test-org/logout-search/resolve/fx-2$" <<<"$out" && grep -q "^latest fx-2$" <<<"$out" \
    && ok "hf:ORG/NAME reads latest.json on main and installs from the tag it names" || bad "resolve wrong" "$out"
grep -q "^pinned test-org/logout-search/resolve/fx-2 None$" <<<"$out" && ok "hf:ORG/NAME@TAG pins the tag without reading latest.json" || bad "pinned wrong" "$out"
grep -q "^refused hf:nope$" <<<"$out" && grep -q "^refused hf:a/b@../x$" <<<"$out" && ok "a malformed hf: source is refused" || bad "bad hf: accepted" "$out"
grep -q "GET datasets/test-org/logout-search/resolve/main/latest.json " "$tmp/req.log" && ok "latest.json was fetched over HTTP from main" || bad "no latest.json request" "$(cat "$tmp/req.log")"
cp "$DS/main/latest.json" "$tmp/latest.bak"; sed -i 's/fx-2/fx-9/' "$DS/main/latest.json"
out=$(python3 -S -E -c "import sys; sys.path.insert(0,'$REPO_ROOT/apps/search/installer'); import install as I
try: I.resolve_source('hf:test-org/logout-search', I.load_pubkey('$tmp/a.pub')); print('accepted')
except I.InstallError as e: print('refused', e)" 2>&1)
grep -q "refused.*not signed by the pinned key" <<<"$out" && ok "a latest.json changed after signing is refused" || bad "forged latest.json accepted" "$out"
cp "$tmp/latest.bak" "$DS/main/latest.json"
out=$(python3 -S -E "$REPO_ROOT/apps/search/installer/install.py" install --source hf:test-org/logout-search --pubkey "$tmp/a.pub" --dest "$tmp/inst" 2>&1)
# the synthetic files are not an engine release: the install gets past the signature and manifest steps and stops at the release check
grep -q "manifest under tag" <<<"$out" && bad "the tag's manifest was refused" "$out" || ok "install reads the tag's manifest and accepts it as the one latest.json names"
cp "$DS/fx-2/manifest.json" "$tmp/m.bak"; python3 - "$DS/fx-2/manifest.json" <<'PY'
import json, sys; m = json.load(open(sys.argv[1])); m["total_bytes"] += 1; open(sys.argv[1], "w").write(json.dumps(m))
PY
openssl pkeyutl -sign -rawin -inkey "$tmp/a.pem" -in "$DS/fx-2/manifest.json" -out "$DS/fx-2/manifest.json.sig.new" 2>/dev/null
cp "$DS/fx-2/manifest.json.sig" "$tmp/sig.bak"; mv "$DS/fx-2/manifest.json.sig.new" "$DS/fx-2/manifest.json.sig"
out=$(python3 -S -E "$REPO_ROOT/apps/search/installer/install.py" install --source hf:test-org/logout-search --pubkey "$tmp/a.pub" --dest "$tmp/inst2" 2>&1); rc=$?
[[ $rc -eq 2 ]] && grep -q "is not the one latest.json names" <<<"$out" && ok "a validly signed manifest that latest.json does not name is refused" || bad "manifest/latest mismatch accepted (rc=$rc)" "$out"
cp "$tmp/m.bak" "$DS/fx-2/manifest.json"; cp "$tmp/sig.bak" "$DS/fx-2/manifest.json.sig"

echo ""; echo "=== 4. update_fetch ==="
mk_base() { mkdir -p "$1/releases/$2"; ln -sfn "releases/$2" "$1/current"; }
mk_base "$tmp/u2" fx-2
out=$("${UF[@]}" check --base "$tmp/u2" --source hf:test-org/logout-search --pubkey "$tmp/a.pub" 2>&1); rc=$?
[[ $rc -eq 0 ]] && grep -q '"action": "up-to-date"' <<<"$out" && ok "on fx-2: up to date" || bad "fx-2 not up to date (rc=$rc)" "$out"
mk_base "$tmp/u0" fx-0
out=$("${UF[@]}" check --base "$tmp/u0" --source hf:test-org/logout-search --pubkey "$tmp/a.pub" 2>&1); rc=$?
[[ $rc -eq 3 ]] && grep -q '"action": "full-install"' <<<"$out" && ok "on fx-0 (no update published from it): full install, exit 3" || bad "fx-0 not full-install (rc=$rc)" "$out"
mk_base "$tmp/u1" fx-1
: > "$tmp/req.log"
out=$("${UF[@]}" fetch --base "$tmp/u1" --source hf:test-org/logout-search --pubkey "$tmp/a.pub" 2>&1); rc=$?
D="$tmp/u1/.download/update-fx-1-fx-2"
[[ $rc -eq 0 ]] && grep -q '"action": "update"' <<<"$out" && ok "on fx-1: the update is fetched" || bad "fetch failed (rc=$rc)" "$out"
diff -r "$D" "$tmp/pkg-1-2" >/dev/null && ok "the downloaded folder is the package, byte for byte" || bad "download differs" "$(diff -rq "$D" "$tmp/pkg-1-2" | head -3)"
grep -q "resolve/fx-2/updates/fx-1/blobs/aa.zst 65536 " "$tmp/req.log" && ok "the dropped connection was resumed from byte 65536 (Range), from the tag" || bad "no resume" "$(grep aa.zst "$tmp/req.log")"
! grep -q "resolve/main/updates" "$tmp/req.log" && ok "package files come from the release's tag, never from main" || bad "package read from main" "$(grep updates "$tmp/req.log" | head -3)"
: > "$tmp/req.log"
out=$("${UF[@]}" fetch --base "$tmp/u1" --source hf:test-org/logout-search --pubkey "$tmp/a.pub" 2>&1)
! grep -q "blobs/aa.zst" "$tmp/req.log" && grep -q '"downloaded_bytes": 0' <<<"$out" && ok "a second run downloads nothing that is already there and verified" || bad "re-downloaded" "$out"
printf 'X' | dd of="$DS/fx-2/updates/fx-1/tombstones.json" bs=1 seek=2 conv=notrunc 2>/dev/null
mk_base "$tmp/u1b" fx-1
refused "a package file that differs from package.json is refused" "tampered" "${UF[@]}" fetch --base "$tmp/u1b" --source hf:test-org/logout-search --pubkey "$tmp/a.pub"
[[ ! -e "$tmp/u1b/.download/update-fx-1-fx-2/tombstones.json" ]] && ok "the bad file is not kept" || bad "bad file kept"
refused "another key is refused before anything is downloaded" "not signed by the pinned key" "${UF[@]}" check --base "$tmp/u1" --source hf:test-org/logout-search --pubkey "$tmp/b.pub"
refused "updates refuse a pinned @TAG source" "without @TAG" "${UF[@]}" check --base "$tmp/u1" --source hf:test-org/logout-search@fx-2 --pubkey "$tmp/a.pub"

echo ""; echo "=== 5. upload order (a stand-in huggingface_hub records the calls) ==="
mkdir -p "$tmp/stub/huggingface_hub"; echo secret-token > "$tmp/token"
cat > "$tmp/stub/huggingface_hub/__init__.py" <<'PY'
import json, os
LOG = os.environ["HF_STUB_LOG"]
def rec(*a):
    open(LOG, "a").write(json.dumps(a) + "\n")
class CommitOperationAdd:
    def __init__(self, path_in_repo, path_or_fileobj): self.path_in_repo = path_in_repo
class HfApi:
    def __init__(self, token): rec("init", token == "secret-token")
    def create_repo(self, repo, repo_type, private, exist_ok): rec("create_repo", repo, repo_type, private)
    def list_repo_files(self, repo, repo_type): return ["files/old.bin", "latest.json", "latest.json.sig", ".gitattributes", "README.md",
                                                 "addons/se-howto/manifest.json", "addons/se-howto/files/se-howto.sqlite.xz", "lists/manifest.json"]
    def upload_folder(self, repo_id, folder_path, repo_type, ignore_patterns, delete_patterns, commit_message): rec("upload_folder", ignore_patterns, delete_patterns)
    def hf_hub_download(self, repo, filename, repo_type):
        p = os.environ["HF_STUB_LOG"] + ".ga"
        open(p, "w").write(open(os.environ["HF_STUB_GA"]).read() if os.environ.get("HF_STUB_GA") else "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        return p
    def create_tag(self, repo, tag, revision, repo_type, tag_message): rec("create_tag", tag, revision)
    def delete_tag(self, repo, tag, repo_type): rec("delete_tag", tag)
    def list_repo_refs(self, repo, repo_type):
        T = type("T", (), {})
        t = T(); t.name = "fx-2"
        r = T(); r.tags = [t] if os.environ.get("HF_STUB_TAGGED") else []
        return r
    def create_commit(self, repo, repo_type, commit_message, operations):
        rec("create_commit", [o.path_in_repo for o in operations])
        c = type("C", (), {})(); c.oid = "c0ffee"
        return c
PY
out=$(HF_STUB_LOG="$tmp/calls" PYTHONPATH="$tmp/stub" "${HF[@]}" upload "$ST" --repo test-org/logout-search --token-file "$tmp/token" 2>&1); rc=$?
[[ $rc -eq 0 ]] || bad "upload with the stand-in failed (rc=$rc)" "$out"
order=$(python3 -c "import json; print(' '.join(json.loads(l)[0] for l in open('$tmp/calls')))")
[[ "$order" == "init create_repo create_commit upload_folder create_commit create_tag" ]] && ok "create (private) -> binary rules -> upload -> latest.json -> tag, in that order" || bad "call order" "$order"
grep -m1 '"create_commit"' "$tmp/calls" | grep -q '\[".gitattributes"\]' && ok "missing binary rules (.fieldnorm, refused by the Hub 2026-11-23) go up in .gitattributes before the files" || bad "no gitattributes commit" "$(cat "$tmp/calls")"
python3 -c "import sys; sys.path.insert(0, '$(dirname "${HF[-1]}")'); import hf_dataset as h; print(h.gitattributes_with_binaries(''), end='')" > "$tmp/ga-full"
grep -q '"create_repo", "test-org/logout-search", "dataset", true' "$tmp/calls" && ok "the repository is created private unless --public" || bad "not private" "$(cat "$tmp/calls")"
grep -q '"upload_folder", \["latest.json", "latest.json.sig"\], \["files/old.bin"\]' "$tmp/calls" \
    && ok "the release goes up without latest.json, and the file it no longer has goes in the same commit" || bad "upload args" "$(grep upload_folder "$tmp/calls")"
! grep -qE '"upload_folder".*addons/' "$tmp/calls" \
    && ok "a release upload never removes the add-ons published beside it (addons/ is not part of a release)" || bad "add-ons removed by a release upload" "$(grep upload_folder "$tmp/calls")"
! grep -qE '"upload_folder".*lists/' "$tmp/calls" \
    && ok "nor the daily lists (lists/ is published by its own job)" || bad "lists removed by a release upload" "$(grep upload_folder "$tmp/calls")"
grep -q '"create_tag", "fx-2", "c0ffee"' "$tmp/calls" && grep -q '"create_commit", \["latest.json", "latest.json.sig"\]' "$tmp/calls" \
    && ok "the tag is the release id, on the latest.json commit (so the tag holds this release's latest.json)" || bad "tag/latest" "$(cat "$tmp/calls")"
: > "$tmp/calls"
HF_STUB_GA="$tmp/ga-full" HF_STUB_TAGGED=1 HF_STUB_LOG="$tmp/calls" PYTHONPATH="$tmp/stub" "${HF[@]}" upload "$ST" --repo test-org/logout-search --token-file "$tmp/token" >/dev/null 2>&1 || bad "re-upload with the stand-in failed"
order=$(python3 -c "import json; print(' '.join(json.loads(l)[0] for l in open('$tmp/calls')))")
[[ "$order" == "init create_repo upload_folder create_commit delete_tag create_tag" ]] && ok "a re-upload of the same release moves its tag to the new latest.json commit" || bad "re-upload tag" "$order"
! grep -q "secret-token" <<<"$out" && ok "the token is never printed" || bad "token printed" "$out"

echo ""; echo "=== 6. counts and the card (numbers come from the release's files, never typed in) ==="
mkdir -p "$tmp/rel"
python3 - "$tmp/rel" <<'PY'
import sqlite3, sys, json
d = sys.argv[1]
r = sqlite3.connect(d + "/registry.sqlite")
r.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
r.execute("CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, title TEXT)")
r.execute("CREATE TABLE platforms (id INTEGER PRIMARY KEY, name TEXT)")
r.executemany("INSERT INTO meta VALUES (?, ?)", [("type_shift", "28"), ("types", json.dumps({"wiki": 1, "site": 2, "page": 3, "place": 4}))])
rows = [(1 << 28) + i for i in range(5)] + [(2 << 28) + i for i in range(3)] + [(4 << 28) + i for i in range(7)]
r.executemany("INSERT INTO registry VALUES (?, 'x')", [(x,) for x in rows])
r.executemany("INSERT INTO platforms VALUES (?, ?)", [(1, "a"), (2, "b")])
r.commit(); r.close()
p = sqlite3.connect(d + "/places-osm.sqlite")
p.execute("CREATE TABLE places (pid INTEGER PRIMARY KEY, name TEXT)")
p.executemany("INSERT INTO places VALUES (?, 'p')", [(i,) for i in range(7)])
p.commit(); p.close()
PY
norm() { python3 -c "import json,sys; print(json.dumps(json.loads(sys.argv[1]), sort_keys=True))" "$1"; }
out=$("${HF[@]}" counts "$tmp/rel" 2>&1); rc=$?
want='{"wiki": 5, "site": 3, "page": 0, "place": 7, "documents": 15, "platforms": 2, "places_osm": 7}'
if [[ $rc -eq 0 && "$(norm "$out")" == "$(norm "$want")" ]]; then
    ok "counts: documents by registry type (from meta.types), platforms, places; a file the release lacks gives no number"
else bad "counts" "rc=$rc" "$out"; fi
echo "$out" > "$tmp/counts.json"
PUBHEX=$(openssl pkey -in "$tmp/a.pem" -pubout -outform DER 2>/dev/null | tail -c 32 | od -An -tx1 | tr -d ' \n')
with=$("${HF[@]}" card "$tmp/pub-fx-2" --repo test-org/logout-search --pubkey "$PUBHEX" --latest "$ST/latest.json" --counts "$tmp/counts.json" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "^| Part | Count | Source" <<<"$with" && grep -q "^size_categories:" <<<"$with" && grep -q "n<1K" <<<"$with" \
    && grep -q "updates/fx-1/" <<<"$with"; then
    ok "card --counts: a Count column, size_categories from the document count, the updates latest.json lists"
else bad "card with counts" "rc=$rc" "$(head -40 <<<"$with")"; fi
without=$("${HF[@]}" card "$tmp/pub-fx-2" --repo test-org/logout-search --pubkey "$PUBHEX" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "^| Part | Source | Licence" <<<"$without" && ! grep -q "size_categories" <<<"$without" && ! grep -q "Count" <<<"$without"; then
    ok "card without counts: no Count column and no size category, nothing invented"
else bad "card without counts" "rc=$rc" "$(head -30 <<<"$without")"; fi
if grep -q "Places in the United States" <<<"$without" && ! grep -q "| Pictures" <<<"$without"; then
    ok "the parts table names only what the release has (places yes, pictures no)"
else bad "parts table" "$(grep '^|' <<<"$without")"; fi
refused "card refuses a pubkey that is not 64 hex characters" "64 hex" "${HF[@]}" card "$tmp/pub-fx-2" --repo test-org/logout-search --pubkey abc
mkdir -p "$tmp/addons/se-howto" "$tmp/addons/notes"
python3 - "$tmp/addons/se-howto/manifest.json" <<'PY'
import json, sys
json.dump({"format": 1, "kind": "addon", "name": "se-howto", "version": "1", "title": "Stack Exchange how-to index",
           "description": "Questions with a short answer excerpt.", "fits_releases": ["fx-2"], "total_bytes": 481415168,
           "files": [{"path": "se-howto.sqlite", "bytes": 481415168, "sha256": "0" * 64}],
           "transport": {"files": {"se-howto.sqlite": {"bytes": 177887236, "encoding": "xz", "sha256": "1" * 64}}},
           "licence": {"name": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}, "notice": "credit"}, open(sys.argv[1], "w"))
PY
withad=$("${HF[@]}" card "$tmp/pub-fx-2" --repo test-org/logout-search --pubkey "$PUBHEX" --addons "$tmp/addons" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "^## Add-ons" <<<"$withad" && grep -q '^| `se-howto` | Questions with a short answer excerpt. | 177.9 MB | 481.4 MB | fx-2 | CC BY-SA 4.0 |$' <<<"$withad" \
    && grep -q "install.py addon install se-howto --source hf:test-org/logout-search --pubkey ed25519:$PUBHEX --dest <folder>" <<<"$withad" \
    && [[ $(grep -n "^## Add-ons" <<<"$withad" | cut -d: -f1) -lt $(grep -n "^## Releases" <<<"$withad" | cut -d: -f1) ]] && ! grep -q "notes" <<<"$withad"; then
    ok "card --addons: an Add-ons section from the signed manifests (sizes read, not typed), its install line, before Releases"
else bad "card with add-ons" "rc=$rc" "$(sed -n '/## Install/,/## Releases/p' <<<"$withad")"; fi
! grep -q "^## Add-ons" <<<"$without" && ok "card without --addons has no Add-ons section" || bad "Add-ons section without --addons"
out=$("${HF[@]}" stage "$tmp/pub-fx-2" "$tmp/stage-ad" --repo test-org/logout-search --sign "$tmp/a.pem" --addons "$tmp/addons" 2>&1); rc=$?
[[ $rc -eq 0 ]] && grep -q "^## Add-ons" "$tmp/stage-ad/README.md" && [[ ! -e "$tmp/stage-ad/addons" ]] \
    && ok "stage --addons: the next release's card keeps the Add-ons section (the add-ons themselves are not staged)" || bad "stage --addons" "rc=$rc" "$out"
if cmp -s <("${HF[@]}" card "$tmp/pub-fx-2" --repo test-org/logout-search --pubkey "$PUBHEX" --latest "$ST/latest.json" --test-key) "$ST/README.md"; then
    ok "card reproduces the staged README byte for byte (a wording change needs no new release)"
else bad "card != staged README"; fi

echo ""; echo "=== 6. the chain of daily updates (stage --prev), and how an install walks it ==="
ST1="$tmp/stage-c1"; ST2="$tmp/stage-c2"
"${HF[@]}" stage "$tmp/pub-fx-1" "$ST1" --repo test-org/logout-search --sign "$tmp/a.pem" --update "$tmp/pkg-0-1" --test-key >/dev/null 2>&1 \
    && "${HF[@]}" stage "$tmp/pub-fx-2" "$ST2" --repo test-org/logout-search --sign "$tmp/a.pem" --update "$tmp/pkg-1-2" --prev "$ST1" --test-key >/dev/null 2>&1 \
    && ok "stage --prev: fx-2 staged on top of fx-1's folder" || bad "chain stage failed"
out=$(python3 - "$ST2" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1] + "/latest.json"))
print("chain", [(h["from"], h["to"], h["package"]) for h in d["chain"]])
print("updates", sorted(d["updates"]))
print("old hop kept", os.path.isfile(sys.argv[1] + "/updates/fx-0/package.json") and os.path.isfile(sys.argv[1] + "/updates/fx-0/update.json"))
PY
)
grep -qx "chain \[('fx-0', 'fx-1', 'updates/fx-0'), ('fx-1', 'fx-2', 'updates/fx-1')\]" <<<"$out" && ok "latest.json's chain lists both hops, oldest first, ending at the release" || bad "chain" "$out"
grep -qx "updates \['fx-1'\]" <<<"$out" && ok "updates (the one-step map) still holds only the hop from the previous release" || bad "updates map" "$out"
grep -qx "old hop kept True" <<<"$out" && ok "the earlier hop's folder is in the new stage (upload keeps it: it is not stale)" || bad "old hop missing" "$out"
out=$("${HF[@]}" check "$ST2" --pubkey "$tmp/a.pub" 2>&1) && grep -q '"ok": true' <<<"$out" && ok "check passes a stage with a chain (every hop's signature and files)" || bad "check chain" "$out"
cp -r "$ST2" "$tmp/st-cx"; printf 'X' | dd of="$tmp/st-cx/updates/fx-0/blobs/aa.zst" bs=1 seek=10 conv=notrunc 2>/dev/null
refused "check refuses a changed file in an EARLIER hop" "wrong sha256" "${HF[@]}" check "$tmp/st-cx" --pubkey "$tmp/a.pub"
out=$("${HF[@]}" stage "$tmp/pub-fx-2" "$tmp/stage-c3" --repo test-org/logout-search --sign "$tmp/a.pem" --update "$tmp/pkg-1-2" --prev "$ST1" --chain-max 1 --test-key 2>&1) \
    && [[ "$(python3 -c "import json,sys; d=json.load(open(sys.argv[1]+'/latest.json')); print(len(d['chain']))" "$tmp/stage-c3")" == 1 ]] && [[ ! -e "$tmp/stage-c3/updates/fx-0" ]] \
    && ok "--chain-max 1 keeps only the newest hop and drops the older folder" || bad "chain-max" "$out"
out=$(REL="$REL" python3 - "$ST2" <<'PY' 2>&1
import json, os, sys
sys.path.insert(0, os.environ["REL"])
import update_fetch as uf
d = json.load(open(sys.argv[1] + "/latest.json"))
f = lambda c: [(h["from"], h["to"]) for h in (uf.hops_from(d, c) or [])]
print("from fx-0", f("fx-0")); print("from fx-1", f("fx-1")); print("from zz", f("zz")); print("from fx-2", f("fx-2"))
PY
)
grep -qx "from fx-0 \[('fx-0', 'fx-1'), ('fx-1', 'fx-2')\]" <<<"$out" && grep -qx "from fx-1 \[('fx-1', 'fx-2')\]" <<<"$out" && grep -qx "from zz \[\]" <<<"$out" \
    && ok "an install on fx-0 walks two hops, on fx-1 one, on an unknown release none (full install)" || bad "hops_from" "$out"
out=$(REL="$REL" python3 - "$ST2" <<'PY' 2>&1
import json, os, sys
sys.path.insert(0, os.environ["REL"])
import update_fetch as uf
d = json.load(open(sys.argv[1] + "/latest.json"))
d["chain"] = d["chain"][:1]; d["updates"] = {}
print("short", uf.hops_from(d, "fx-0"))
uf.plan = lambda *a, **k: {"action": "update", "current": "fx-0", "latest": "fx-2", "bytes": 100 << 20, "hops": [{"from": "fx-0", "to": "fx-1", "package": "x"}]}
r = uf.fetch("/nonexistent", "x", b"", metered=True)
print("metered", r["action"])
PY
)
grep -qx "short None" <<<"$out" && grep -qx "metered waiting" <<<"$out" && ok "a chain that stops short is no path; a metered connection leaves a big update waiting (nothing fetched)" || bad "short chain / metered" "$out"

echo ""; echo "=== 7. data-update-publish.sh (the publisher's one command): its guards ==="
DUP="$REPO_ROOT/apps/search/release/data-update-publish.sh"
out=$(env -u KEY bash "$DUP" "$tmp/pub-fx-1" "$tmp/pub-fx-2" "$tmp/pub-fx-2" "" "$tmp/dup-out" 2>&1); rc=$?
[[ $rc -ne 0 ]] && grep -q "KEY must name the release key" <<<"$out" && ok "no KEY: refused, nothing built" || bad "no KEY" "rc=$rc $out"
mkdir -p "$tmp/dup-exists"
out=$(KEY="$tmp/a.pem" bash "$DUP" "$tmp/pub-fx-1" "$tmp/pub-fx-2" "$tmp/pub-fx-2" "" "$tmp/dup-exists" 2>&1); rc=$?
[[ $rc -ne 0 ]] && grep -q "exists" <<<"$out" && ok "an existing OUT is refused (a run never builds on another)" || bad "existing OUT" "rc=$rc $out"
out=$(KEY="$tmp/a.pem" bash "$DUP" "$tmp/pub-fx-1" "$tmp/pub-fx-2" "$tmp/pub-fx-2" "$tmp/nope" "$tmp/dup-new" 2>&1); rc=$?
[[ $rc -ne 0 ]] && grep -q "PREV_STAGE has no latest.json" <<<"$out" && ok "a PREV_STAGE without latest.json is refused" || bad "bad PREV" "rc=$rc $out"
! grep -q "BEGIN" <<<"$out" && ok "the key is never printed" || bad "key text in the output"

finish
