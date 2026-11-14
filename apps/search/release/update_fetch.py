#!/usr/bin/env python3
"""Fetch this month's update from where releases are published, then apply it (SEARCH-HOME-ENGINE-PLAN.md §5).

    update_fetch.py check --base BASE --source hf:ORG/NAME|URL|DIR --pubkey KEY
    update_fetch.py fetch --base BASE --source ... --pubkey KEY [--apply]

`check` reads the signed latest.json (hf_dataset.py writes it) and says one of: up to date, an update from the release this
machine is on, or no update path (the machine is on a release the publisher no longer ships an update from: install the new
release with installer/install.py). Exit 0, 0, 3.

`fetch` downloads updates/<this release>/ into BASE/.download/update-<from>-<to>/: package.json first, its signature checked
against the pinned key, then every file it lists, resumable (HTTP Range), each size and sha256 compared before anything else
reads it. `--apply` then runs update_apply.py on the folder (it checks update.json's signature and every resulting file against
the signed manifest, and swaps `current` atomically); KEY must then be a PEM file, which update_apply.py reads.

Standard library only (the download code is the installer's). Exit 2 on any refusal: nothing under BASE/releases is touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "installer"))
import install as inst  # noqa: E402
import release_ptr  # noqa: E402

PACKAGE_FORMAT = 1


def apply_cmd() -> list:
    """How update_apply.py is run. A frozen launcher (installer/logout_search.py) has no interpreter and sets
    LOGOUT_UPDATE_APPLY_CMD to a JSON list that reaches update_apply.py's main."""
    j = os.environ.get("LOGOUT_UPDATE_APPLY_CMD", "")
    return json.loads(j) if j else [sys.executable, os.path.join(HERE, "update_apply.py")]


def latest_from(source: str, pub: bytes) -> tuple:
    """(latest.json, a Source at the root that holds updates/). `hf:ORG/NAME` reads `main`; a URL or folder is that root."""
    m = inst.HF_RE.match(source)
    if m:
        if m.group(2):
            raise inst.Fatal("updates follow latest.json on main: give hf:ORG/NAME without @TAG")
        root = inst.hf_base(m.group(1), "main")
    else:
        root = source
    src = inst.Source(root)
    latest = inst.read_latest(src, pub)
    if m:   # the package is read from the release's tag, like the release itself: an upload never changes a tag
        src = inst.Source(inst.hf_base(m.group(1), latest["revision"]))
    return latest, src


def plan(base: str, source: str, pub: bytes) -> dict:
    cur = release_ptr.current_id(base)
    if cur is None:
        raise inst.Fatal(f"{base}/current is not a release: install one first (installer/install.py)")
    latest, _src = latest_from(source, pub)
    if latest["release"] == cur:
        return {"current": cur, "latest": cur, "action": "up-to-date"}
    if cur in latest["updates"]:
        return {"current": cur, "latest": latest["release"], "action": "update", "package": latest["updates"][cur]}
    return {"current": cur, "latest": latest["release"], "action": "full-install",
            "why": f"no update from {cur} is published (only from {sorted(latest['updates']) or 'none'})"}


def fetch(base: str, source: str, pub: bytes) -> dict:
    p = plan(base, source, pub)
    if p["action"] != "update":
        return p
    _latest, src = latest_from(source, pub)
    dest = p["package"]
    body = src.read_small(f"{dest}/package.json")
    if not inst.ed25519_verify(pub, body, src.read_small(f"{dest}/package.json.sig")):
        raise inst.Fatal(f"{dest}/package.json is not signed by the pinned key: refusing")
    pk = json.loads(body)
    if pk.get("format") != PACKAGE_FORMAT or pk.get("from_release") != p["current"] or pk.get("to_release") != p["latest"]:
        raise inst.Fatal(f"{dest}/package.json is not the update {p['current']} -> {p['latest']}")
    out = os.path.join(base, ".download", f"update-{p['current']}-{p['latest']}")
    total = 0
    for f in pk["files"]:
        rel = inst.safe_rel(f["path"])
        part = os.path.join(out, *rel.split("/"))
        if os.path.isfile(part) and os.path.getsize(part) == f["bytes"] and inst.file_digest(part)[0] == f["sha256"]:
            continue                                     # already here from an earlier run
        inst.download(src, f"{dest}/{rel}", part + ".part", f["bytes"], f["sha256"], f"{dest}/{rel}")
        os.replace(part + ".part", part)
        total += f["bytes"]
    return dict(p, folder=out, files=len(pk["files"]), downloaded_bytes=total)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("check", "fetch"))
    ap.add_argument("--base", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--pubkey", required=True)
    ap.add_argument("--apply", action="store_true", help="fetch: run update_apply.py on the package, then remove the download")
    a = ap.parse_args()
    base = os.path.abspath(a.base)
    try:
        pub = inst.load_pubkey(a.pubkey)
        r = plan(base, a.source, pub) if a.cmd == "check" else fetch(base, a.source, pub)
    except inst.InstallError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if a.cmd == "fetch" and a.apply and r.get("folder"):
        if not os.path.isfile(a.pubkey):
            print("refused: --apply needs --pubkey as a PEM file (update_apply.py reads it)", file=sys.stderr)
            return 2
        rc = subprocess.run(apply_cmd() + ["apply", base, r["folder"], "--pubkey", a.pubkey]).returncode
        if rc:
            print(f"refused: update_apply.py exited {rc}; the download stays in {r['folder']}", file=sys.stderr)
            return 2
        shutil.rmtree(r["folder"], ignore_errors=True)
        r["applied"] = True
    print(json.dumps(r, indent=1))
    return 3 if r["action"] == "full-install" else 0


if __name__ == "__main__":
    sys.exit(main())
