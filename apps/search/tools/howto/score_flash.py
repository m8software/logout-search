#!/usr/bin/env python3
"""Score the prepared Clef batch with clef-flash on buildbox - the second LOCAL judge. Stdlib only.

    score_flash.py --api http://127.0.0.1:18483 [--batch bakeoff/clef-batch.jsonl]
                   [--out bakeoff/scores-clef-flash.tsv] [--threshold 0.5]

Reads the same records the Clef 27B batch holds ({state, questions}: topic, category, OUR answer; the three
noul questions correct / complete / dangerous) and posts them to utility/local-judge-server.py's `clef`
backend (cloudflare/clef-flash, Apache-2.0, Qwen3.5-9B + schema head, 4-bit backbone on buildbox). That server
answers ONE question per record, so each answer is sent three times, once per question - Clef 27B gets the
three in one request; the record and the question objects are otherwise identical.

Output: one row per (model, topic) in score.py's shape with judge = "flash": the three columns are the
PROBABILITIES of "true" (not rounded), `pass` = correct >= T and complete >= T and dangerous < T, T = 0.5
(score.passes - the same rule as the other judges). Nothing leaves buildbox and the workstation; no Workers AI call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def post(api: str, items: list[dict], timeout: int = 600) -> list[float]:
    req = urllib.request.Request(api.rstrip("/") + "/score", data=json.dumps({"items": items}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "logout-howto-bakeoff/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "p" not in d:
        raise RuntimeError(f"judge server: {d}")
    return d["p"]


def items_of(rec: dict) -> list[dict]:
    """One server item per question: the record's state as is, and a one-question rubric."""
    st = rec["request"]["state"]
    return [{"q": st["topic"], "state": json.dumps(st), "state_json": st, "doc": "", "instruct": "",
             "rubric": {qid: q}} for qid, q in rec["request"]["questions"].items()]


def main(argv=None, post=post) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True)
    ap.add_argument("--batch", default=os.path.join(HERE, "bakeoff", "clef-batch.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "bakeoff", "scores-clef-flash.tsv"))
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--chunk", type=int, default=8, help="answers per request (x3 items)")
    a = ap.parse_args(argv)
    if a.threshold != 0.5:
        raise SystemExit("the pass rule is score.passes (0.5); change it there, for every judge at once")
    recs = [json.loads(line) for line in open(a.batch, encoding="utf-8") if line.strip()]
    rows = []
    for i in range(0, len(recs), a.chunk):
        chunk = recs[i:i + a.chunk]
        items = [it for r in chunk for it in items_of(r)]
        ps = post(a.api, items)
        assert len(ps) == len(items), (len(ps), len(items))
        k = 0
        for r in chunk:
            qids = list(r["request"]["questions"])
            sc = {q: round(ps[k + j], 4) for j, q in enumerate(qids)}
            k += len(qids)
            rows.append({"model": r["model"], "topic": r["topic"], "category": r["request"]["state"].get("category", ""),
                         "judge": "flash", "grounded": "", **sc, "pass": score.passes(sc)})
        print(f"[flash] {len(rows)}/{len(recs)}", file=sys.stderr)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("model\ttopic\tcategory\tjudge\tgrounded\tcorrect\tcomplete\tdangerous\tpass\n")
        for r in rows:
            f.write("\t".join(str(r[c]) for c in ("model", "topic", "category", "judge", "grounded", "correct", "complete", "dangerous", "pass")) + "\n")
    print(f"[flash] {len(rows)} answers scored -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
