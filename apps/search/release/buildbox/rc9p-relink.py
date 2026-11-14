#!/usr/bin/env python3
"""rc9p (2026-10-23): turn the self-tested rc9f -> rc9p package's links from an old folder into blobs, as
update_pkg.py --no-link-from would have built them, then run update_pkg's own self-test on the result.

The package was built before --no-link-from existed; rebuilding it costs about 3 hours on testbox (the row diffs), this
about 40 minutes (the self-test). Run in the release folder (update_common / update_pkg on the path), unsigned:
update.json is signed on the workstation afterwards.

    rc9p-relink.py OLD_REL NEW_REL PKG OLD_PREFIX"""
import json, os, sys
sys.path.insert(0, os.getcwd())
import update_common as uc
import update_pkg
old, new, pkg, prefix = sys.argv[1:5]
upd = json.load(open(os.path.join(pkg, "update.json")))
n = 0
for i, o in enumerate(upd["files"]):
    if o["op"] == "link" and o["from_path"].startswith(prefix):
        z = os.path.join(pkg, "blobs", o["sha256"] + ".zst")
        zb = uc.zst_compress(os.path.join(new, o["path"]), z, 19)
        o = {k: v for k, v in o.items() if k != "from_path"}
        o.update(op="blob", blob=f"blobs/{o['sha256']}.zst", zbytes=zb)
        upd["files"][i] = o
        n += 1
print("relinked", n)
open(os.path.join(pkg, "update.json"), "wb").write(json.dumps(upd, indent=1, sort_keys=True).encode())
mn, _ = update_pkg.load_manifest(new)
update_pkg.selftest_apply(old, new, pkg, mn)
print(json.dumps(update_pkg.info(pkg), sort_keys=True))
print("RELINK-DONE")
