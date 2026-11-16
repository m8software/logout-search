#!/usr/bin/env python3
"""The second download source: a data release's files as GitHub release assets (owner 2026-11-15).

    github_mirror.py stage  --kind release|latest|lists|addons --src DIR --out STAGE --key KEY.pem
    github_mirror.py upload --stage STAGE/<tag> [--repo m8software/logout-search] [--execute]

installer/install.py asks this mirror when Hugging Face cannot serve a request (see `Mirror` there for the layout it reads):

    kind      tag             --src holds                                     paths in the tag
    release   data-<release>  a hf_dataset.py stage folder                    everything but latest*.json, lists/, addons/
    latest    data-latest     latest.json(.sig), latest-beta.json(.sig)       as they are
    lists     data-lists      a lists_build.py --out folder (lists/...)       the paths under lists/
    addons    data-addons     a folder with addons/<name>/...                 the paths under addons/

GitHub release assets are flat and must be under 2 GiB, so `stage` names each path with "/" turned into "__", cuts a file of
more than SEGMENT bytes into consecutive slices (name.part001, ...) and writes `mirror-map.json` (path -> its assets and their
sizes) signed with the release key. The installer joins the slices and checks the result against the signed manifest's size and
sha256, so the mirror is as trusted as Hugging Face: not at all, except for what the signatures and checksums prove.

`stage` only writes STAGE/<tag>/ (small files are symlinks, big ones are cut copies: it needs the size of the biggest one free).
`upload` prints the `gh` commands and what they send; `--execute` runs them: creating the release (a PRE-release, never the
latest, or GitHub's "latest release" would stop being the program's: autoupdate reads it), sending every asset, the map last, and
removing assets of a mutable tag (latest, lists, addons) that its map no longer names. Nothing here runs by itself.

Limits (docs.github.com, About releases, 2026-11-15): up to 1000 assets per release; each file under 2 GiB; no limit on the total size
of a release or on bandwidth. Standard library only (`gh` for upload; `cryptography` or `openssl` for the signature).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "installer"))
import hf_dataset  # noqa: E402

SEGMENT = 1_900_000_000                  # under GitHub's 2 GiB (2,147,483,648) with room to spare
BATCH = 15                               # assets per `gh release upload`
REPO = "m8software/logout-search"
MUTABLE = ("data-latest", "data-lists", "data-addons")
NAME_OK = re.compile(r"[^A-Za-z0-9._+-]")


def flat(path: str) -> str:
    n = NAME_OK.sub("_", path.replace("/", "__"))
    return n if n[:1].isalnum() else "x" + n


def collect(kind: str, src: str):
    """-> (tag, {path in the tag: file on disk})."""
    files = {}
    if kind == "release":
        m = json.load(open(os.path.join(src, "manifest.json"), encoding="utf-8"))
        for d, _ds, fs in os.walk(src):
            for f in fs:
                rel = os.path.relpath(os.path.join(d, f), src).replace(os.sep, "/")
                if rel.startswith(("lists/", "addons/")) or rel in ("latest.json", "latest.json.sig", "latest-beta.json", "latest-beta.json.sig"):
                    continue
                files[rel] = os.path.join(d, f)
        return f"data-{m['release']}", files
    if kind == "latest":
        for n in ("latest.json", "latest.json.sig", "latest-beta.json", "latest-beta.json.sig"):
            if os.path.isfile(os.path.join(src, n)):
                files[n] = os.path.join(src, n)
        return "data-latest", files
    sub = {"lists": "lists", "addons": "addons"}[kind]
    root = os.path.join(src, sub)
    for d, _ds, fs in os.walk(root):
        for f in fs:
            files[os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")] = os.path.join(d, f)
    return f"data-{kind}", files


def split(path: str, dest_dir: str, name: str, size: int) -> list:
    out, n = [], 0
    with open(path, "rb") as f:
        while True:
            n += 1
            part = f"{name}.part{n:03d}"
            left, written = SEGMENT, 0
            with open(os.path.join(dest_dir, part), "wb") as o:
                while left:
                    b = f.read(min(left, 1 << 22))
                    if not b:
                        break
                    o.write(b)
                    left -= len(b)
                    written += len(b)
            if not written:
                os.remove(os.path.join(dest_dir, part))
                break
            out.append({"name": part, "bytes": written})
            if left:
                break
    return out


def stage(kind: str, src: str, out: str, key: str) -> dict:
    tag, files = collect(kind, src)
    if not files:
        raise SystemExit(f"{src}: no files for kind {kind!r}")
    d = os.path.join(out, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    m, seen, big = {}, {}, []
    for path in sorted(files):
        name = flat(path)
        if name in seen:
            raise SystemExit(f"{path!r} and {seen[name]!r} become the same asset name {name!r}")
        seen[name] = path
        size = os.path.getsize(files[path])
        if size <= SEGMENT:
            os.symlink(os.path.abspath(files[path]), os.path.join(d, name))
            m[path] = {"assets": [{"name": name, "bytes": size}]}
        else:
            m[path] = {"assets": split(files[path], d, name, size)}
            big.append((path, size, len(m[path]["assets"])))
    if len(m) + 2 > 1000 or sum(len(e["assets"]) for e in m.values()) + 2 > 1000:
        raise SystemExit(f"{tag}: more than 1000 assets: GitHub's limit per release")
    body = (json.dumps({"format": 1, "tag": tag, "files": m}, indent=1, sort_keys=True) + "\n").encode()
    open(os.path.join(d, "mirror-map.json"), "wb").write(body)
    open(os.path.join(d, "mirror-map.json.sig"), "wb").write(hf_dataset.sign(key, body))
    total = sum(a["bytes"] for e in m.values() for a in e["assets"])
    return {"tag": tag, "paths": len(m), "assets": sum(len(e["assets"]) for e in m.values()) + 2, "bytes": total,
            "cut": [{"path": p, "bytes": s, "slices": n} for p, s, n in big], "stage": d,
            "map_sha256": hashlib.sha256(body).hexdigest()}


def gh(args: list, execute: bool, log: list, capture: bool = False):
    log.append("gh " + " ".join(args))
    if not execute:
        return ""
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode and not capture:
        raise SystemExit(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[-400:]}")
    return r.stdout if not r.returncode else None


def upload(stage_dir: str, repo: str, execute: bool) -> dict:
    stage_dir = os.path.abspath(stage_dir)
    tag = os.path.basename(stage_dir)
    names = sorted(os.listdir(stage_dir))
    m = json.load(open(os.path.join(stage_dir, "mirror-map.json")))
    want = {a["name"] for e in m["files"].values() for a in e["assets"]} | {"mirror-map.json", "mirror-map.json.sig"}
    if want != set(names):
        raise SystemExit(f"{stage_dir} does not match its own mirror-map.json: stage it again")
    log = []
    exists = gh(["release", "view", tag, "--repo", repo, "--json", "assets", "-q", ".assets[].name"], execute, log, capture=True)
    if exists is None or not execute:
        gh(["release", "create", tag, "--repo", repo, "--title", tag, "--prerelease", "--latest=false",
            "--notes", "Files of one data release, a second download source for logout-search (installer/install.py reads them "
                       "when Hugging Face cannot serve a file). Not a program release."], execute, log)
        have = set()
    else:
        have = set(exists.split())
    maps = ["mirror-map.json", "mirror-map.json.sig"]
    todo = [n for n in names if n not in maps and (n not in have or n.endswith(".json") or n.endswith(".sig"))]
    sent = 0
    for i in range(0, len(todo), BATCH):
        gh(["release", "upload", tag, "--repo", repo, "--clobber"] + [os.path.join(stage_dir, n) for n in todo[i:i + BATCH]], execute, log)
        sent += sum(os.path.getsize(os.path.join(stage_dir, n)) for n in todo[i:i + BATCH])
    gh(["release", "upload", tag, "--repo", repo, "--clobber"] + [os.path.join(stage_dir, n) for n in maps], execute, log)   # the map last
    stale = sorted(have - want) if tag in MUTABLE else []
    for n in stale:
        gh(["release", "delete-asset", tag, n, "--repo", repo, "--yes"], execute, log)
    return {"tag": tag, "repo": repo, "execute": execute, "assets_to_send": len(todo) + 2, "bytes_to_send": sent,
            "stale_removed": stale, "commands": len(log), "log": log if not execute else log[-3:]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("stage")
    s.add_argument("--kind", required=True, choices=("release", "latest", "lists", "addons"))
    s.add_argument("--src", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--key", required=True, help="PEM ed25519 private key (the release key)")
    u = sp.add_parser("upload")
    u.add_argument("--stage", required=True, help="STAGE/<tag> made by `stage`")
    u.add_argument("--repo", default=REPO)
    u.add_argument("--execute", action="store_true", help="run the gh commands (default: print them)")
    a = ap.parse_args()
    r = stage(a.kind, a.src, a.out, a.key) if a.cmd == "stage" else upload(a.stage, a.repo, a.execute)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
