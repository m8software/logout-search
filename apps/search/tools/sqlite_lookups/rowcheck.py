"""T4 (drivers/verify2.sh): every base-table row that differs between the month file and the applied copy: is it only REALs, tiny?"""
import sqlite3, sys
m = sqlite3.connect("file:/w/lookup-m.sqlite?mode=ro", uri=True)
m.execute("ATTACH 'file:/w/lookup-a.sqlite?mode=ro' AS a")
specs = {"places": "id", "suggest_items": "id", "suggest_keys": "key, id, pos", "suggest_top": "prefix",
         "suggest_phrases": "prefix", "typo_keys": "kid"}
for t, pk in specs.items():
    cols = [r[1] for r in m.execute(f"PRAGMA table_info({t})")]
    k = [c.strip() for c in pk.split(",")]
    on = " AND ".join(f"x.{c} = y.{c}" for c in k)
    only = m.execute(f"SELECT (SELECT count(*) FROM main.{t} x WHERE NOT EXISTS (SELECT 1 FROM a.{t} y WHERE {on})), "
                     f"(SELECT count(*) FROM a.{t} y WHERE NOT EXISTS (SELECT 1 FROM main.{t} x WHERE {on}))").fetchone()
    diff = m.execute(f"SELECT {', '.join('x.' + c for c in cols)}, {', '.join('y.' + c for c in cols)} FROM main.{t} x JOIN a.{t} y ON {on} "
                     f"WHERE " + " OR ".join(f"x.{c} IS NOT y.{c}" for c in cols)).fetchall()
    n = len(cols); nonreal = 0; maxrel = 0.0
    for r in diff:
        for i in range(n):
            u, v = r[i], r[n + i]
            if u == v:
                continue
            if isinstance(u, float) and isinstance(v, float):
                maxrel = max(maxrel, abs(u - v) / max(abs(u), abs(v), 1e-300))
            else:
                nonreal += 1
    print(f"{t}: rows only in month/applied {only}, rows differing {len(diff)}, non-REAL field diffs {nonreal}, max rel REAL diff {maxrel:.2e}")
