#!/usr/bin/env python3
# rc9 (2026-10-08): the rc8 script of the same name with rc8 -> rc9 and rc4 -> rc8 (rc8 is --previous). Inputs stay in rc5in.
"""rc9-regdiff.py OLD_REGISTRY NEW_REGISTRY: per type, documents kept / added / retired, and ids reused (must be 0)."""
import json, sqlite3, sys
old, new = sys.argv[1:3]
c = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
c.execute(f"ATTACH 'file:{old}?mode=ro' AS p")
T = {1: "wiki", 2: "site", 3: "page", 4: "place", 5: "account", 6: "youtube", 7: "stock", 8: "link"}
tot = dict(c.execute("SELECT doc_id >> 28, count(*) FROM registry GROUP BY 1"))
add = dict(c.execute("SELECT doc_id >> 28, count(*) FROM registry WHERE doc_id NOT IN (SELECT doc_id FROM p.registry) GROUP BY 1"))
ret = dict(c.execute("SELECT doc_id >> 28, count(*) FROM p.registry WHERE doc_id NOT IN (SELECT doc_id FROM registry) GROUP BY 1"))
tomb = c.execute("SELECT count(*) FROM tombstones").fetchone()[0]
reused = c.execute("SELECT count(*) FROM registry WHERE doc_id IN (SELECT doc_id FROM p.tombstones)").fetchone()[0]
print(json.dumps({"stage": "regdiff", "types": {T[t]: {"total": tot.get(t, 0), "kept": tot.get(t, 0) - add.get(t, 0), "added": add.get(t, 0),
      "retired": ret.get(t, 0)} for t in T}, "tombstones_total": tomb, "ids_reused_from_old_tombstones": reused}))
