#!/usr/bin/env python3
"""curated-mklw.py CAND.jsonl SCORES.jsonl CUR.json OUT.json: input for curated-listwise.py (the whole-list pass): per name the engine's current first row, then the best by Qwen, 12 in all."""
import json, sys
cand, scores, cur, out = sys.argv[1:5]
Q = {(o := json.loads(l))["id"]: o["p"] for l in open(scores)}
F = {x["i"]: (x.get("cur") or [None])[0] for x in json.load(open(cur))}
norm = lambda u: u.split("#")[0].rstrip("/").replace("://www.", "://")
res = []
for l in open(cand):
    x = json.loads(l)
    sc = sorted(((Q.get(f"f{x['i']}-{k}", 0.0), r) for k, r in enumerate(x["cand"])), key=lambda z: -z[0])
    e = F.get(x["i"]); top, seen = [], set()
    for r in ([{"u": e["u"], "t": e["t"], "s": e["s"]}] if e else []) + [r for _, r in sc]:
        if norm(r["u"]) in seen: continue
        seen.add(norm(r["u"])); top.append({"u": r["u"], "t": r["t"][:110], "s": r.get("s", "")})
        if len(top) >= 12: break
    res.append({"i": x["i"], "q": x["q"], "cands": top})
json.dump(res, open(out, "w"), ensure_ascii=False); print(len(res), "names")
