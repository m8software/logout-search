"""T4 eval runner (testbox host, stdlib only): ask the arm's search-api, keep every answer.

  python3 run.py search QUERIES.txt OUT.json     /search?k=10 for each query
  python3 run.py instant QUERIES.txt OUT.json    /instant (suggestions, phrases, card, place)
One query at a time; per query the JSON answer (or the error) and the wall time in ms.
"""
import json, sys, time, urllib.parse, urllib.request

API = "http://127.0.0.1:18097"
mode, qf, out = sys.argv[1:4]
qs = [l.rstrip("\n") for l in open(qf, encoding="utf-8") if l.strip()]
res = []
for q in qs:
    url = API + ("/search?k=10&q=" if mode == "search" else "/instant?q=") + urllib.parse.quote(q)
    t = time.perf_counter()
    try:
        d = json.load(urllib.request.urlopen(url, timeout=60))
    except Exception as e:
        d = {"error": type(e).__name__}
    res.append({"q": q, "ms": round((time.perf_counter() - t) * 1000, 1), "d": d})
json.dump(res, open(out, "w"))
print(mode, qf, len(res), "errors", sum(1 for r in res if "error" in r["d"]))
