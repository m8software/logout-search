#!/usr/bin/env python3
"""regdiff.py OLD_REGISTRY NEW_REGISTRY [--unchanged TYPE,TYPE,...]

Per document type: kept / added / retired, and ids reused from the old release's tombstones (must be 0). With
--unchanged, the rows of those types must be identical between the two registries (ordered by doc_id, every column)
and the exit code says so: the registry stage's check for a release that may only move one kind of document
(rc9p: "nothing but pages moved"). Types: wiki site page place account youtube stock link. Read-only.
(The buildbox/rc*-regdiff.py reference copies, made a tool of the build pipeline.)"""
import hashlib
import json
import sqlite3
import sys

T = {1: "wiki", 2: "site", 3: "page", 4: "place", 5: "account", 6: "youtube", 7: "stock", 8: "link"}
BY_NAME = {v: k for k, v in T.items()}


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rows_hash(db: sqlite3.Connection, t: int) -> str:
    """sha256 over the rows of one type, streamed: fetchall() of the 10 M account rows as tuples is ~3 GB and was killed
    by the 4 GB stage limit (rc10, 2026-10-26); the hash only has to agree between the two files of one run."""
    h = hashlib.sha256()
    for row in db.execute("SELECT * FROM registry WHERE doc_id >> 28 = ? ORDER BY doc_id", (t,)):
        h.update(repr(row).encode())
        h.update(b"\n")
    return h.hexdigest()


def diff(old: str, new: str, unchanged: list[str]) -> tuple[dict, list[str]]:
    c = _ro(new)
    c.execute(f"ATTACH 'file:{old}?mode=ro' AS p")
    tot = dict(c.execute("SELECT doc_id >> 28, count(*) FROM registry GROUP BY 1"))
    add = dict(c.execute("SELECT doc_id >> 28, count(*) FROM registry WHERE doc_id NOT IN (SELECT doc_id FROM p.registry) GROUP BY 1"))
    ret = dict(c.execute("SELECT doc_id >> 28, count(*) FROM p.registry WHERE doc_id NOT IN (SELECT doc_id FROM registry) GROUP BY 1"))
    tomb = c.execute("SELECT count(*) FROM tombstones").fetchone()[0]
    reused = c.execute("SELECT count(*) FROM registry WHERE doc_id IN (SELECT doc_id FROM p.tombstones)").fetchone()[0]
    problems = []
    if reused:
        problems.append(f"{reused} ids reused from the old release's tombstones")
    same = {}
    for name in unchanged:
        t = BY_NAME.get(name)
        if t is None:
            problems.append(f"--unchanged {name!r}: not a type")
            continue
        h = [_rows_hash(db, t) for db in (_ro(old), _ro(new))]
        same[name] = h[0] == h[1]
        if not same[name]:
            problems.append(f"{name} rows differ from the previous release but were declared unchanged")
    out = {"stage": "regdiff", "types": {T[t]: {"total": tot.get(t, 0), "kept": tot.get(t, 0) - add.get(t, 0), "added": add.get(t, 0),
           "retired": ret.get(t, 0)} for t in T}, "tombstones_total": tomb, "ids_reused_from_old_tombstones": reused,
           "unchanged": same, "problems": problems}
    return out, problems


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 2:
        sys.exit(__doc__)
    unchanged = []
    if "--unchanged" in a:
        i = a.index("--unchanged")
        unchanged = [x for x in a[i + 1].split(",") if x]
        del a[i:i + 2]
    out, problems = diff(a[0], a[1], unchanged)
    print(json.dumps(out))
    sys.exit(1 if problems else 0)
