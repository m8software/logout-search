#!/usr/bin/env python3
"""Reuse a part of the previous release unchanged, under the new release id (the rc9p recipe for text/wiki, the IVF folder
and facts.sqlite: the Wikipedia ids did not move, so the bytes are the same and only the stamp changes).

    restamp.py tantivy SRC_DIR OUT_DIR RELEASE_ID ROLE      copy of a Tantivy index folder, release.json restamped
    restamp.py ivf     SRC_DIR OUT_DIR RELEASE_ID           copy of an IVF folder, ivf.json "release" restamped
    restamp.py sqlite  SRC.sqlite OUT.sqlite RELEASE_ID REASON   copy, meta built_release + restamp_reason, VACUUM

The destination must not exist (the runner gives every stage a fresh folder): this tool creates, it never removes.
Copies use reflinks when the file system has them (cp --reflink=auto). The stage that calls this declares the previous
release's folder as a file: input, so a different previous release is a different key."""
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tvstamp  # noqa: E402


def fresh(dst: str) -> None:
    if os.path.lexists(dst):
        sys.exit(f"refusing: {dst} exists already (the output folder of a stage starts empty)")
    if not os.path.isdir(os.path.dirname(os.path.abspath(dst))):
        sys.exit(f"refusing: the parent of {dst} does not exist")


def copy_tree(src: str, dst: str) -> None:
    fresh(dst)
    subprocess.run(["cp", "-r", "--reflink=auto", src, dst], check=True)


def main(a: list[str]) -> None:
    what = a[0]
    if what == "tantivy":
        src, out, rid, role = a[1:5]
        copy_tree(src, out)
        print(json.dumps(tvstamp.stamp(out, rid, role, None), sort_keys=True))
    elif what == "ivf":
        src, out, rid = a[1:4]
        copy_tree(src, out)
        p = os.path.join(out, "ivf.json")
        m = json.load(open(p))
        m.pop("restamped_from", None)
        m["release"] = rid
        open(p, "w").write(json.dumps(m, indent=1, sort_keys=True) + "\n")
        print(json.dumps({"ivf": out, "release": rid}))
    elif what == "sqlite":
        src, out, rid, reason = a[1:5]
        fresh(out)
        subprocess.run(["cp", "--reflink=auto", src, out], check=True)
        db = sqlite3.connect(out)
        db.execute("DELETE FROM meta WHERE key = 'restamped_from'")
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("built_release", rid), ("restamp_reason", reason)])
        db.commit()
        db.execute("VACUUM")
        if db.execute("PRAGMA freelist_count").fetchone()[0] != 0:
            sys.exit(f"{out}: freelist not empty after VACUUM")
        print(json.dumps({"sqlite": out, "built_release": rid}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(sys.argv[1:])
