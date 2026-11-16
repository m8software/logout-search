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
BIG = 64 << 20                    # on a metered connection a data update larger than this waits for an ordinary one


def apply_cmd() -> list:
    """How update_apply.py is run. A frozen launcher (installer/logout_search.py) has no interpreter and sets
    LOGOUT_UPDATE_APPLY_CMD to a JSON list that reaches update_apply.py's main."""
    j = os.environ.get("LOGOUT_UPDATE_APPLY_CMD", "")
    return json.loads(j) if j else [sys.executable, os.path.join(HERE, "update_apply.py")]


def latest_from(source: str, pub: bytes, channel: str = "stable") -> tuple:
    """(latest.json, a Source at the root that holds updates/). `hf:ORG/NAME` reads `main`; a URL or folder is that root.
    channel: beta reads latest-beta.json when it is published (installer/install.py read_latest_channel)."""
    m = inst.HF_RE.match(source)
    if m:
        if m.group(2):
            raise inst.Fatal("updates follow latest.json on main: give hf:ORG/NAME without @TAG")
        root = inst.hf_base(m.group(1), "main")
    else:
        root = source
    src = inst.Source(root)
    latest = inst.read_latest_channel(src, pub, channel)
    if m:   # the package is read from the release's tag, like the release itself: an upload never changes a tag
        src = inst.Source(inst.hf_base(m.group(1), latest["revision"]))
    return latest, src


def _root(source: str) -> str:
    """The folder or URL that holds the pointers (`hf:ORG/NAME` reads main)."""
    m = inst.HF_RE.match(source)
    return inst.hf_base(m.group(1), "main") if m else source


def hops_from(latest: dict, cur: str):
    """The updates that take `cur` to latest, in order: [{"from", "to", "package", "bytes"?}], or None when no unbroken line
    of published hops leads from `cur` to the latest release. One hop straight to the latest (`updates`) wins; else the daily
    chain (latest.json "chain", oldest first) is followed from `cur`."""
    if cur in latest["updates"]:
        return [{"from": cur, "to": latest["release"], "package": latest["updates"][cur]}]
    by_from = {}
    for h in latest.get("chain") or []:
        by_from.setdefault(h["from"], h)
    out, node = [], cur
    while node != latest["release"]:
        h = by_from.get(node)
        if h is None or len(out) >= len(latest.get("chain") or []):
            return None
        out.append(h)
        node = h["to"]
    return out or None


def plan(base: str, source: str, pub: bytes, channel: str = "stable") -> dict:
    cur = release_ptr.current_id(base)
    if cur is None:
        raise inst.Fatal(f"{base}/current is not a release: install one first (installer/install.py)")
    latest, _src = latest_from(source, pub, channel)
    if latest["release"] == cur:
        return {"current": cur, "latest": cur, "action": "up-to-date"}
    hops = hops_from(latest, cur)
    if hops:
        r = {"current": cur, "latest": latest["release"], "action": "update", "package": hops[0]["package"], "hops": hops}
        if all("bytes" in h for h in hops):
            r["bytes"] = sum(h["bytes"] for h in hops)
        return r
    if channel == "stable":
        try:                                             # on the beta release, which stable has not reached: wait, not "install again"
            if inst.read_latest_channel(inst.Source(_root(source)), pub, "beta")["release"] == cur:
                return {"current": cur, "latest": latest["release"], "action": "ahead",
                        "why": f"{cur} is the beta release; the stable channel has not reached it yet"}
        except inst.InstallError:
            pass
    return {"current": cur, "latest": latest["release"], "action": "full-install",
            "why": f"no update from {cur} is published (only from {sorted(latest['updates']) or 'none'}, chain from "
                   f"{sorted({h['from'] for h in latest.get('chain') or []}) or 'none'})"}


def fetch_hop(src, hop: dict, base: str, pub: bytes) -> tuple:
    """Download one hop's package into BASE/.download/update-<from>-<to>/ (resumable, each file's size and sha256 checked)."""
    dest = hop["package"]
    body = src.read_small(f"{dest}/package.json")
    if not inst.ed25519_verify(pub, body, src.read_small(f"{dest}/package.json.sig")):
        raise inst.Fatal(f"{dest}/package.json is not signed by the pinned key: refusing")
    pk = json.loads(body)
    if pk.get("format") != PACKAGE_FORMAT or pk.get("from_release") != hop["from"] or pk.get("to_release") != hop["to"]:
        raise inst.Fatal(f"{dest}/package.json is not the update {hop['from']} -> {hop['to']}")
    out = os.path.join(base, ".download", f"update-{hop['from']}-{hop['to']}")
    total = 0
    for f in pk["files"]:
        rel = inst.safe_rel(f["path"])
        part = os.path.join(out, *rel.split("/"))
        if os.path.isfile(part) and os.path.getsize(part) == f["bytes"] and inst.file_digest(part)[0] == f["sha256"]:
            continue                                     # already here from an earlier run
        inst.download(src, f"{dest}/{rel}", part + ".part", f["bytes"], f["sha256"], f"{dest}/{rel}")
        os.replace(part + ".part", part)
        total += f["bytes"]
    return out, len(pk["files"]), total


def fetch(base: str, source: str, pub: bytes, channel: str = "stable", metered: bool = False, big: int = BIG) -> dict:
    p = plan(base, source, pub, channel)
    if p["action"] != "update":
        return p
    if metered and p.get("bytes", 0) > big:
        return dict(p, action="waiting", why=f"{p['bytes'] >> 20} MiB of updates on a metered connection; they wait for an ordinary one")
    _latest, src = latest_from(source, pub, channel)
    folders, files, total = [], 0, 0
    for hop in p["hops"]:                                # every hop is downloaded and verified before any is applied
        out, n, b = fetch_hop(src, hop, base, pub)
        folders.append((hop, out))
        files += n
        total += b
    return dict(p, folder=folders[0][1], folders=[f for _h, f in folders], files=files, downloaded_bytes=total)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("check", "fetch"))
    ap.add_argument("--base", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--pubkey", required=True)
    ap.add_argument("--channel", choices=inst.CHANNELS, default="stable", help="beta follows latest-beta.json")
    ap.add_argument("--apply", action="store_true", help="fetch: run update_apply.py on each package in order, then remove the downloads")
    ap.add_argument("--metered", choices=("auto", "yes", "no"), default="no", help="fetch: on a metered connection an update of more than 64 MiB waits")
    a = ap.parse_args()
    base = os.path.abspath(a.base)
    metered = a.metered == "yes"
    if a.metered == "auto":
        try:
            import netcost
            metered = bool(netcost.metered())
        except Exception:  # noqa: BLE001 - not knowing says "not metered", as the lists update does
            metered = False
    try:
        pub = inst.load_pubkey(a.pubkey)
        r = plan(base, a.source, pub, a.channel) if a.cmd == "check" else fetch(base, a.source, pub, a.channel, metered)
    except inst.InstallError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if a.cmd == "fetch" and a.apply and r.get("folders"):
        if not os.path.isfile(a.pubkey):
            print("refused: --apply needs --pubkey as a PEM file (update_apply.py reads it)", file=sys.stderr)
            return 2
        done, followed = [], []
        for hop, folder in zip(r["hops"], r["folders"]):  # in order; each apply switches `current` atomically, so a failure leaves the last good release
            aff = inst.read_affected(os.path.join(folder, "affected.bin"))       # read before the folder goes
            rc = subprocess.run(apply_cmd() + ["apply", base, folder, "--pubkey", a.pubkey]).returncode
            if rc:
                print(f"refused: update_apply.py exited {rc} on {os.path.basename(folder)}; applied so far: {done or 'none'}; "
                      f"the download stays in {folder}", file=sys.stderr)
                return 2
            shutil.rmtree(folder, ignore_errors=True)
            done.append(os.path.basename(folder))
            followed.append(inst.lists_follow(base, hop["from"], hop["to"], aff))   # the stored lists and answers follow the release
        r["applied"] = True
        r["applied_hops"] = done
        r["answers_followed"] = followed
    print(json.dumps(r, indent=1))
    return 3 if r["action"] == "full-install" else 0                 # "ahead" (on the beta release, stable not there yet): 0


if __name__ == "__main__":
    sys.exit(main())
