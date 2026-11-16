#!/usr/bin/env python3
"""Pin and verify the inputs of a data-release build.

    pin_inputs.py pin    ROOT --out inputs.lock.json [--fast] [--subset GLOB ...] [--exclude GLOB ...]
                              [--sources sources.json] [--jobs N]
    pin_inputs.py verify ROOT --lock inputs.lock.json [--fast] [--subset GLOB ...] [--jobs N]

pin walks ROOT and records, per regular file, its size and sha256 (symlinks are recorded as links, never followed;
empty folders are not recorded). The lock holds no time, host or path of ROOT, so pinning the same bytes twice, on
two machines, after an rsync, gives the same file. `tree_sha256` is the sha256 of the sorted lines
"<path>\\0<size>\\0<sha256>\\n": one string to quote in an issue or a release note.

--fast records size and mtime_ns only (no sha256: a 100 GB tree in seconds). verify --fast compares size, and mtime_ns
where the lock has it; rsync -a and cp -p keep mtime, a plain cp does not. Use it to catch a half-copied tree, never as
the proof for a release: the full pin is.

--subset GLOB (repeatable; fnmatch on the path relative to ROOT, "*" also crosses "/") limits pin and verify to the
matching files, for a rehearsal on a few of them: files outside the subset are neither required nor reported as extra.

--sources FILE.json (pin only) is copied into the lock under "sources": where each input came from (url, dump date, the
sha256 the publisher gives), so the lock says how to fetch what the tree lacks.

verify prints one line per problem (MISSING, EXTRA, CHANGED, LINK) and a summary; exit 0 = identical, 1 = differences,
2 = bad arguments or unreadable input. Stdlib only, Python 3.11+. Reads only; writes nothing but --out.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

FORMAT = "inputs-lock/1"
CHUNK = 8 * 1024 * 1024


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def matches(rel: str, globs: list[str]) -> bool:
    return not globs or any(fnmatch.fnmatchcase(rel, g) for g in globs)


def walk(root: str, subset: list[str], exclude: list[str], skip_abs: str | None = None) -> dict[str, dict]:
    """relative posix path -> {"size", "mtime_ns"} or {"link"} for every file or symlink under root, sorted."""
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if skip_abs and os.path.abspath(full) == skip_abs:
                continue
            if not matches(rel, subset) or any(fnmatch.fnmatchcase(rel, g) for g in exclude):
                continue
            if os.path.islink(full):
                out[rel] = {"link": os.readlink(full)}
                continue
            st = os.stat(full)
            out[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return dict(sorted(out.items()))


def hash_all(root: str, entries: dict[str, dict], jobs: int) -> dict[str, str]:
    todo = [r for r, e in entries.items() if "link" not in e]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        sums = list(ex.map(lambda r: sha256_file(os.path.join(root, r)), todo))
    return dict(zip(todo, sums))


def tree_hash(files: dict[str, dict]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        e = files[rel]
        if "link" in e:
            h.update(f"{rel}\0link\0{e['link']}\n".encode())
        else:
            h.update(f"{rel}\0{e['size']}\0{e.get('sha256', '')}\n".encode())
    return h.hexdigest()


def cmd_pin(a: argparse.Namespace) -> int:
    root = a.root
    if not os.path.isdir(root):
        print(f"pin: {root} is not a folder", file=sys.stderr)
        return 2
    skip = os.path.abspath(a.out) if a.out != "-" else None
    entries = walk(root, a.subset, a.exclude, skip)
    if not entries:
        print(f"pin: no file under {root} matches (subset {a.subset or 'none'})", file=sys.stderr)
        return 2
    files: dict[str, dict] = {}
    if a.fast:
        for rel, e in entries.items():
            files[rel] = {"link": e["link"]} if "link" in e else {"size": e["size"], "mtime_ns": e["mtime_ns"]}
    else:
        sums = hash_all(root, entries, a.jobs)
        for rel, e in entries.items():
            files[rel] = {"link": e["link"]} if "link" in e else {"size": e["size"], "sha256": sums[rel]}
    lock: dict = {"format": FORMAT, "mode": "fast" if a.fast else "sha256", "subset": sorted(a.subset),
                  "file_count": len(files), "total_bytes": sum(e.get("size", 0) for e in files.values())}
    if not a.fast:
        lock["tree_sha256"] = tree_hash(files)
    if a.sources:
        try:
            with open(a.sources, encoding="utf-8") as f:
                lock["sources"] = json.load(f)
        except (OSError, ValueError) as ex:
            print(f"pin: cannot read --sources {a.sources}: {ex}", file=sys.stderr)
            return 2
    lock["files"] = files
    text = json.dumps(lock, indent=1, sort_keys=True) + "\n"
    if a.out == "-":
        sys.stdout.write(text)
    else:
        tmp = a.out + ".partial"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, a.out)
    print(f"pinned {len(files)} files, {lock['total_bytes']} bytes, mode {lock['mode']}"
          + (f", tree {lock['tree_sha256'][:16]}" if "tree_sha256" in lock else ""), file=sys.stderr)
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    root = a.root
    try:
        with open(a.lock, encoding="utf-8") as f:
            lock = json.load(f)
    except (OSError, ValueError) as ex:
        print(f"verify: cannot read lock {a.lock}: {ex}", file=sys.stderr)
        return 2
    if lock.get("format") != FORMAT or not isinstance(lock.get("files"), dict):
        print(f"verify: {a.lock} is not an {FORMAT} file", file=sys.stderr)
        return 2
    if not os.path.isdir(root):
        print(f"verify: {root} is not a folder", file=sys.stderr)
        return 2
    want = {r: e for r, e in lock["files"].items() if matches(r, a.subset)}
    have = walk(root, a.subset, [], os.path.abspath(a.lock))
    problems: list[str] = []
    for rel in sorted(want):
        if rel not in have:
            problems.append(f"MISSING {rel}")
    for rel in sorted(have):
        if rel not in want:
            problems.append(f"EXTRA   {rel}")
    both = [r for r in sorted(want) if r in have]
    full = [r for r in both if "link" not in want[r] and "sha256" in want[r] and not a.fast
            and have[r]["size"] == want[r]["size"] and "link" not in have[r]]
    sums = hash_all(root, {r: have[r] for r in full}, a.jobs)
    for rel in both:
        w, h = want[rel], have[rel]
        if "link" in w or "link" in h:
            if w.get("link") != h.get("link"):
                problems.append(f"LINK    {rel}: lock {w.get('link')!r}, found {h.get('link')!r}")
            continue
        if w["size"] != h["size"]:
            problems.append(f"CHANGED {rel}: size {w['size']} -> {h['size']}")
        elif rel in sums:
            if sums[rel] != w["sha256"]:
                problems.append(f"CHANGED {rel}: sha256 {w['sha256'][:16]} -> {sums[rel][:16]}")
        elif "mtime_ns" in w and w["mtime_ns"] != h["mtime_ns"]:
            problems.append(f"CHANGED {rel}: mtime_ns {w['mtime_ns']} -> {h['mtime_ns']}")
    if not a.subset and "tree_sha256" in lock and not a.fast and not problems:
        found = {r: ({"link": have[r]["link"]} if "link" in have[r] else {"size": have[r]["size"], "sha256": sums[r]})
                 for r in have}
        if tree_hash(found) != lock["tree_sha256"]:
            problems.append("TREE    tree_sha256 differs although every file matched (lock edited by hand?)")
    for p in problems:
        print(p)
    mode = "sha256" if sums else "size+mtime"
    print(f"verify {'FAILED' if problems else 'ok'}: {len(want)} in lock, {len(have)} found, "
          f"{len(problems)} problem(s), compared by {mode}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pin", help="write a lock for a tree")
    pp.add_argument("root")
    pp.add_argument("--out", required=True, help="lock file to write ('-' = stdout)")
    pp.add_argument("--fast", action="store_true", help="size+mtime only, no sha256")
    pp.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    pp.add_argument("--sources", metavar="FILE.json")
    vp = sub.add_parser("verify", help="compare a tree with a lock")
    vp.add_argument("root")
    vp.add_argument("--lock", required=True)
    vp.add_argument("--fast", action="store_true", help="size (and mtime where pinned) only")
    for s in (pp, vp):
        s.add_argument("--subset", action="append", default=[], metavar="GLOB", help="only paths matching GLOB")
        s.add_argument("--jobs", type=int, default=1, help="hashing threads (default 1: the build hosts are busy)")
    a = p.parse_args(argv)
    return cmd_pin(a) if a.cmd == "pin" else cmd_verify(a)


if __name__ == "__main__":
    sys.exit(main())
