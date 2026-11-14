#!/usr/bin/env python3
"""Layer 1 of the build pipeline: raw sources, fetched once, never modified, named by their sha256.

    WORK/sources/NAME/<sha256[:16]>/data          the bytes as fetched
    WORK/sources/NAME/<sha256[:16]>/source.json   url, sha256, bytes, fetched (UTC), http headers worth keeping

A pipeline declares a source as [sources.NAME] url = ... sha256 = ... . The sha256 is the pin: a stage that
reads the source has that hash in its key, so a new fetch of the same URL that gives different bytes is a
different input and the stages downstream rebuild. `fetch` downloads, stores by hash and prints the pin line
to paste into the pipeline file; a pinned source whose fetch gives other bytes is reported, not swapped in.
A missing pin is a refusal at plan time, not a silent "latest".

Used by pipeline.py (`pipeline.py release.toml --work W sources fetch NAME`); standard library only.
URLs: http(s) and file:// (the latter for tests and for files copied by hand).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
import urllib.request

KEY_LEN = 16
UA = "logout-search-build/1 (+https://logout.cloud)"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(name: str, url: str, root: str, timeout: int = 600) -> dict:
    """Download url into root/name/<sha>/data (through a temp file; hashed while streaming). Returns the record.
    Fetching bytes already stored is a no-op that returns the existing record."""
    os.makedirs(os.path.join(root, name), exist_ok=True)
    tmp = os.path.join(root, name, f".fetch-{os.getpid()}.part")
    h = hashlib.sha256()
    headers = {}
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:   # noqa: S310 (url is from the pipeline file)
        for k in ("ETag", "Last-Modified", "Content-Type"):
            v = r.headers.get(k) if hasattr(r, "headers") else None
            if v:
                headers[k] = v
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
            n += len(chunk)
    sha = h.hexdigest()
    d = os.path.join(root, name, sha[:KEY_LEN])
    data = os.path.join(d, "data")
    if os.path.exists(data):
        os.unlink(tmp)
        return json.load(open(os.path.join(d, "source.json")))
    os.makedirs(d, exist_ok=True)
    rec = {"name": name, "url": url, "sha256": sha, "bytes": n, "fetched": _now(), "headers": headers}
    shutil.move(tmp, data)
    os.chmod(data, 0o444)
    json.dump(rec, open(os.path.join(d, "source.json"), "w"), indent=1, sort_keys=True)
    return rec


def stored(name: str, root: str) -> list[dict]:
    out = []
    base = os.path.join(root, name)
    if not os.path.isdir(base):
        return out
    for k in sorted(os.listdir(base)):
        p = os.path.join(base, k, "source.json")
        try:
            out.append(json.load(open(p)))
        except (OSError, ValueError):
            continue
    return out


def verify(name: str, sha: str, root: str) -> str | None:
    """None when the pinned bytes are stored and hash to the pin, else the problem."""
    data = os.path.join(root, name, sha[:KEY_LEN], "data")
    if not os.path.exists(data):
        return f"{name}: pinned {sha[:12]} is not stored (sources fetch {name})"
    h = hashlib.sha256()
    with open(data, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha:
        return f"{name}: stored bytes of {sha[:12]} hash to {h.hexdigest()[:12]}: the store is damaged, delete {os.path.dirname(data)} and fetch again"
    return None


def cli(p: dict, root: str, sub: str, names: list[str]) -> int:
    srcs = p["sources"]
    names = names or sorted(srcs)
    rc = 0
    for n in names:
        if n not in srcs:
            print(f"{n}: not declared under [sources]", file=sys.stderr)
            rc = 2
            continue
        s = srcs[n]
        if sub == "fetch":
            rec = fetch(n, s["url"], root)
            pin = s.get("sha256")
            if pin and pin != rec["sha256"]:
                print(f"{n}: fetched {rec['sha256'][:12]} ({rec['bytes']} bytes) but the pipeline pins {pin[:12]}: "
                      f"the source changed upstream. To move to it, set in the pipeline file:\n    sha256 = \"{rec['sha256']}\"")
                rc = 1
            elif pin:
                print(f"{n}: {rec['sha256'][:12]} ({rec['bytes']} bytes) matches the pin")
            else:
                print(f"{n}: fetched {rec['bytes']} bytes. Pin it in the pipeline file:\n    sha256 = \"{rec['sha256']}\"")
        elif sub == "list":
            pin = (s.get("sha256") or "")[:12] or "-"
            recs = stored(n, root)
            print(f"{n}: pin {pin}  url {s['url']}")
            for r in recs:
                mark = "*" if s.get("sha256") == r["sha256"] else " "
                print(f"  {mark} {r['sha256'][:12]}  {r['bytes']} bytes  fetched {r['fetched']}")
            if not recs:
                print("    (nothing stored)")
        elif sub == "verify":
            if not s.get("sha256"):
                print(f"{n}: no pin")
                rc = 1
                continue
            err = verify(n, s["sha256"], root)
            print(err or f"{n}: ok")
            if err:
                rc = 1
    return rc
