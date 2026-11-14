#!/usr/bin/env python3
"""The release stamp of a Tantivy index folder (release format v1, rule 8): release.json beside meta.json.

    tvstamp.py DIR RELEASE_ID ROLE [STOPFILE]

A folder that already has one (an index reused from the previous release: text/wiki, whose Wikipedia ids did not
change) keeps every field and gets the new release id. A new folder (tantivy-structure/local-build.sh output) gets
the fields assemble.py and release_check read: analyzer from logout-analyzer.txt, the stop list's sha256.
(The buildbox/rc*-tvstamp.py reference copies, made a tool of the build pipeline.) Writes one file, removes nothing."""
import hashlib
import json
import os
import sys


def stamp(d: str, rid: str, role: str, stop: str | None) -> dict:
    if not os.path.isfile(os.path.join(d, "meta.json")):
        sys.exit(f"{d}: not a Tantivy index folder (no meta.json)")
    p = os.path.join(d, "release.json")
    if os.path.exists(p):
        side = json.load(open(p))
        if side.get("role") != role:
            sys.exit(f"{d}: its stamp says role {side.get('role')!r}, not {role!r}: refusing")
    else:
        if not stop:
            sys.exit(f"{d}: a new index needs the stop list it was built with")
        side = {"analyzer": open(os.path.join(d, "logout-analyzer.txt")).read().strip(), "format": 1, "role": role,
                "schema": "store-nothing", "stopwords_sha256": hashlib.sha256(open(stop, "rb").read()).hexdigest(), "tantivy": "0.26.2"}
    side["built_release"] = rid
    open(p, "w").write(json.dumps(side, sort_keys=True) + "\n")
    return side


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    print(json.dumps(stamp(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None), sort_keys=True))
