#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit test — an overlay whose release is gone is removed
################################################################################
# tv_open.make_overlay gives a read-only index a writable folder of links. On Windows the links are hard links, so
# after an update pruned the old release its overlay alone kept the old index files on disk (about 2.4 GB per
# release on an install). The next open now removes every overlay whose entries all point at nothing. Hard links are
# forced here by refusing symlinks, the way Windows without developer mode does. No tantivy needed.
# Usage: bash tests/unit/test-search-tv-overlay-sweep.sh
################################################################################
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$REPO" TMP="$TMP" exec python3 -I - <<'PY'
import os, shutil, sys
T = os.environ["TMP"]
sys.path.insert(0, os.path.join(os.environ["REPO"], "apps/search/serve/api"))
os.environ["LOGOUT_TV_OVERLAY_DIR"] = os.path.join(T, "ov")
import tv_open as V
fails = total = 0
def check(name, ok, detail=""):
    global fails, total
    total += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += not ok
def release(name, size=1000):
    d = os.path.join(T, "releases", name, "text")
    os.makedirs(d)
    for f in ("a.idx", "a.term", "meta.json"):
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(b"x" * size)
    return d
real_symlink = os.symlink
def refuse(*a, **k):
    raise OSError("symlinks refused")
for mode in ("hard links (Windows)", "symlinks"):
    shutil.rmtree(T + "/releases", ignore_errors=True); shutil.rmtree(T + "/ov", ignore_errors=True)
    os.symlink = refuse if mode.startswith("hard") else real_symlink
    a = release("rc15"); ova = V.make_overlay(a)
    check(f"{mode}: the overlay links every index file", sorted(x for x in os.listdir(ova) if x not in V.LOCKS) == ["a.idx", "a.term", "meta.json"])
    if mode.startswith("hard"):
        check(f"{mode}: its files are the release's (2 links each)", os.stat(os.path.join(ova, "a.idx")).st_nlink == 2)
    b = release("rc16"); ovb = V.make_overlay(b)
    check(f"{mode}: opening a second release while the first is there keeps both", os.path.isdir(ova) and os.path.isdir(ovb))
    shutil.rmtree(os.path.dirname(a))                       # the update prunes the old release
    V.make_overlay(b)
    check(f"{mode}: the next open removes the pruned release's overlay", not os.path.exists(ova))
    check(f"{mode}: and keeps the current one", os.path.isdir(ovb) and os.path.exists(os.path.join(ovb, "a.idx")))
    other = os.path.join(T, "ov", "not-ours"); os.makedirs(other, exist_ok=True)
    check(f"{mode}: an empty folder is left alone", V.sweep(os.path.join(T, "ov"), keep=ovb) == [] and os.path.isdir(other))
    os.rmdir(other)
os.symlink = real_symlink
check("sweep of a folder that does not exist returns nothing", V.sweep(os.path.join(T, "nowhere")) == [])
print(f"\n{total - fails}/{total} passed"); sys.exit(1 if fails else 0)
PY
