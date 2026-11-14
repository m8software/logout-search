#!/usr/bin/env python3
"""rc9p (2026-10-23): bring the already-built rc9p parts in line with the fixed builders, in place, before the assemble.

The 2026-10-23 audit of the dataset found what the parts said about themselves naming internal things: tombstone release
ids with a machine name in them, the kinds file pointing at a repo document and naming its build tool, the anchor-phrase
licence carrying audit codes, and Overture's own dataset names listed as providers. The builders are fixed at the source
(kinds_build.LICENCE / SOURCE, release_v1.ANCHOR_PHRASE_LICENCE, build.provider_names); this rewrites the same values into
the parts buildbox built, so they hold what a fresh build gives, then VACUUMs each file (sqlguard wants no free pages). Rows
other than these meta values and tombstone ids are untouched.

    rc9p-scrub.py PARTS_DIR        (PARTS_DIR = /opt/logout-release/w/rc9p-in; run with the release code on sys.path)
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REL = os.path.join(HERE, "..")
sys.path[:0] = [REL, os.path.join(REL, "..", "tools", "sqlite_lookups")]
import build as places_build  # noqa: E402
import kinds_build  # noqa: E402
import release_v1  # noqa: E402


def meta_set(db, **kv):
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(kv.items()))


def main(parts: str) -> int:
    out = {}
    p = os.path.join(parts, "rc9p", "registry.sqlite")
    db = sqlite3.connect(p)
    n = db.execute("UPDATE tombstones SET removed_release = replace(removed_release, '-testbox-', '-') "
                   "WHERE removed_release LIKE '%-testbox-%'").rowcount
    db.commit()
    out["registry tombstones renamed"] = n
    out["registry tombstone ids"] = db.execute("SELECT removed_release, count(*) FROM tombstones GROUP BY 1").fetchall()
    db.execute("VACUUM"); db.close()

    p = os.path.join(parts, "rc9p-web", "kinds.sqlite")
    db = sqlite3.connect(p)
    meta_set(db, licence=kinds_build.LICENCE, source=kinds_build.SOURCE)
    db.commit(); db.execute("VACUUM"); db.close()
    out["kinds"] = "licence, source"

    p = os.path.join(parts, "rc9p-web", "anchor-phrases.sqlite")
    db = sqlite3.connect(p)
    meta_set(db, licence=release_v1.ANCHOR_PHRASE_LICENCE)
    db.commit(); db.execute("VACUUM"); db.close()
    out["anchor-phrases"] = "licence"

    p = os.path.join(parts, "rc9p", "lookup", "places-overture.sqlite")
    db = sqlite3.connect(p)
    meta = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('providers', 'attribution')"))
    old = json.loads(meta["providers"])
    new = places_build.provider_names(old)
    if ", ".join(old) not in meta["attribution"]:
        sys.exit("places-overture: the attribution does not hold the provider list it was built with")
    meta_set(db, providers=json.dumps(new), attribution=meta["attribution"].replace(", ".join(old), ", ".join(new)))
    db.commit(); db.execute("VACUUM"); db.close()
    out["places-overture providers"] = [old, new]
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
