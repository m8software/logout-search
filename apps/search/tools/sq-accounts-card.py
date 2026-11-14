#!/usr/bin/env python3
"""A/B of SEARCH_ACCOUNTS_IN_CARD (option 3) on one engine: every dev-half query of the named open sets is
asked three ways through /instant: nocap (?pcap=off, every account in the list: the 48% of the issue), cap2 (the shipped cap of
two) and card (?aic=1: the card carries them, the list keeps sites and the article). Dev halves only; there is no --test.

    tools/sq-accounts-card.py --engine http://127.0.0.1:18651 --out DIR [--sets head-brand,account,...] [--workers 3] [--limit N]

Per set and arm it reports, on the list as /instant returns it (first ten results):
  hit@1, hit@5            the open-set rule (accept keys), on the list alone
  hit@5 +card             the same, counting an expected answer the card offers as one of its links
  acct slots              share of the top-5 slots that are accounts (source == account)
  3+ accts                queries with three or more accounts in results 2-5
  cards, links            queries answered with a card, and the mean number of links on it
and, paired, how many queries changed: list hit@1 / hit@5 lost and gained, and the same with the card counted.
Answers are kept in OUT/<set>.<arm>.jsonl so a rerun asks only what is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

eq = C.load_tool("eval_queries", C.TOOLS / "eval-queries.py")
DEFAULT_SETS = "head-brand,account,own-title,site-nav,wiki-redirect"


# the three layouts: every account in the list (the 48% of the issue), the shipped cap of two, accounts in the card
ARMS = {"nocap": "&aic=0&pcap=off", "cap2": "&aic=0", "card": "&aic=1"}


def ask(api: str, q: str, arm: str) -> dict:
    try:
        with urllib.request.urlopen(f"{api}/instant?full=1{ARMS[arm]}&q={urllib.parse.quote(q)}", timeout=120) as r:
            d = json.load(r)
    except Exception as e:                                   # a miss, counted, never a crash
        return {"q": q, "error": type(e).__name__}
    res = [{"url": x.get("url"), "title": x.get("title"), "source": x.get("source"), "platform": x.get("platform")}
           for x in (d.get("results") or [])[:10]]
    card = d.get("card") or {}
    return {"q": q, "results": res, "links": [l.get("url") for l in (card.get("links") or [])], "card": bool(card)}


def answers(api: str, path: Path, queries: list[str], arm: str, workers: int) -> dict:
    have: dict = {}
    if path.exists():
        for line in open(path, encoding="utf-8"):
            if line.strip():
                o = json.loads(line)
                if "error" not in o:
                    have[o["q"]] = o
    todo = [q for q in dict.fromkeys(queries) if q not in have]
    if todo:
        print(f"  asking {len(todo)} queries, arm {arm}", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(max(1, workers)) as ex:
            got = list(ex.map(lambda q: ask(api, q, arm), todo))
        with open(path, "a", encoding="utf-8") as f:
            for o in got:
                if "error" not in o:
                    f.write(json.dumps(o) + "\n")
        errs = [o for o in got if "error" in o]
        if errs:
            print(f"  {len(errs)} queries failed ({errs[0]['error']}): scored as misses", file=sys.stderr)
        for o in got:
            have[o["q"]] = o
    return have


def rank(r: dict, o: dict, with_card: bool) -> int | None:
    """1-based place of the expected answer in the list (or None); with_card: a card link counts as place 1."""
    res = o.get("results") or []
    hit = next((i + 1 for i, x in enumerate(res) if C.row_hit(eq, r, x)), None)
    if with_card and hit is None and any(C.row_hit(eq, r, {"url": u}) for u in (o.get("links") or [])):
        return 1
    return hit


def stats(rows: list[dict], got: dict) -> dict:
    n = len(rows)
    out = {"n": n, "h1": 0, "h5": 0, "h5c": 0, "slots": 0, "acct": 0, "three": 0, "cards": 0, "links": 0}
    for r in rows:
        o = got.get(r["query"]) or {}
        res = (o.get("results") or [])[:5]
        k = rank(r, o, False)
        kc = rank(r, o, True)
        out["h1"] += k == 1
        out["h5"] += bool(k and k <= 5)
        out["h5c"] += bool(kc and kc <= 5)
        out["slots"] += len(res)
        out["acct"] += sum(x.get("source") == "account" for x in res)
        out["three"] += sum(x.get("source") == "account" for x in res[1:5]) >= 3
        out["cards"] += bool(o.get("card"))
        out["links"] += len(o.get("links") or [])
    return out


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}" if b else "-"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sets", default=DEFAULT_SETS)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    api = a.engine.rstrip("/")
    lines = ["| set | arm | n | hit@1 | hit@5 | hit@5 +card | acct slots % | 3+ accts | cards | links/card |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    flips = ["| set | list hit@1 lost / gained | list hit@5 lost / gained | hit@5 +card lost / gained |", "|---|---|---|---|"]
    keys = ("n", "h1", "h5", "h5c", "slots", "acct", "three", "cards", "links")
    tot = {arm: {k: 0 for k in keys} for arm in ARMS}
    tf = [0] * 6
    for name in [s for s in a.sets.split(",") if s]:
        rows = C.load_set(name, "dev", a.limit)
        qs = [r["query"] for r in rows]
        got = {arm: answers(api, out / f"{name}.{arm}.jsonl", qs, arm, a.workers) for arm in ARMS}
        st = {arm: stats(rows, got[arm]) for arm in ARMS}

        def line(label, arm, s):
            lk = f"{s['links'] / s['cards']:.1f}" if s["cards"] else "-"
            return (f"| {label} | {arm} | {s['n']} | {pct(s['h1'], s['n'])} | {pct(s['h5'], s['n'])} | {pct(s['h5c'], s['n'])} | "
                    f"{pct(s['acct'], s['slots'])} | {s['three']} | {s['cards']} | {lk} |")
        for arm in ARMS:
            lines.append(line(name, arm, st[arm]))
            for k in keys:
                tot[arm][k] += st[arm][k]
        f = [0] * 6
        for r in rows:
            o0, o1 = got["cap2"].get(r["query"]) or {}, got["card"].get(r["query"]) or {}
            for j, (wc, th) in enumerate(((False, 1), (False, 5), (True, 5))):
                a0, a1 = rank(r, o0, wc), rank(r, o1, wc)
                h0, h1 = bool(a0 and a0 <= th), bool(a1 and a1 <= th)
                f[2 * j] += h0 and not h1
                f[2 * j + 1] += h1 and not h0
        flips.append(f"| {name} | {f[0]} / {f[1]} | {f[2]} / {f[3]} | {f[4]} / {f[5]} |")
        tf = [x + y for x, y in zip(tf, f)]
    for arm in ARMS:
        s = tot[arm]
        lk = f"{s['links'] / s['cards']:.1f}" if s["cards"] else "-"
        lines.append(f"| **all** | {arm} | {s['n']} | {pct(s['h1'], s['n'])} | {pct(s['h5'], s['n'])} | {pct(s['h5c'], s['n'])} | "
                     f"{pct(s['acct'], s['slots'])} | {s['three']} | {s['cards']} | {lk} |")
    flips.append(f"| **all** | {tf[0]} / {tf[1]} | {tf[2]} / {tf[3]} | {tf[4]} / {tf[5]} |")
    text = "\n".join(lines) + "\n\nPaired changes, cap2 -> card (lost = answered before, not after):\n\n" + "\n".join(flips) + "\n"
    (out / "table.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
