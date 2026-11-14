#!/usr/bin/env python3
"""Score the `facts` open set against a running engine, and prove the result list did not move.

    tools/facts-eval.py answered SET.tsv --url http://127.0.0.1:18481 [--split dev|test] [--details out.tsv]
        ANSWERED = the /instant answer box is present, names the expected article (url) and its value matches the
        expected one (dates by ISO prefix at the stored precision, amounts within 0.5%, labels case-insensitive).
        Also reports CARD (an answer box of any value for the right article) and per-property rows.
    tools/facts-eval.py rescore DETAILS.tsv [--set SET.tsv --split dev|test]     the same counts from a --details file
    tools/facts-eval.py dump URL OUT.jsonl SET.tsv [SET2.tsv ...] [--n 500] [--split dev]
    tools/facts-eval.py diff A.jsonl B.jsonl
        the same N queries (md5-sampled, seed 402) against one engine at a time (the workstation runs ONE engine:
        dump with SEARCH_FACTS=1, restart with 0, dump again); diff says how many result lists differ (urls and
        titles of `results`) and exits 1 if any does — the "nothing else may move" check.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter


def instant(url: str, q: str, timeout: int = 120, results: bool = True) -> dict:
    """/instant as the page asks it for a submitted search; results=False asks for the suggestions and the answer box
    only (results=0: the engine skips the search, the answer stage is the same)."""
    with urllib.request.urlopen(f"{url}/instant?{'full=1' if results else 'results=0'}&q=" + urllib.parse.quote(q), timeout=timeout) as r:
        return json.load(r)


def read_set(path: str, split: str | None) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return [r for r in rows if not split or r["split"] == split]


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def _numbers(text: str) -> list[tuple[float, bool]]:
    """Every number a card's text says: '545,796', '8,849 m (29,032 ft)', '$130.5 billion' (-> 130.5e9)."""
    out = []
    for m in re.finditer(r"(-?\d[\d,]*(?:\.\d+)?)(?:\s*(trillion|billion|million))?", text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.append((v * {"trillion": 1e12, "billion": 1e9, "million": 1e6}.get(m.group(2), 1), bool(m.group(2))))
    return out


def value_matches(expected: str, card: dict) -> bool:
    """expected 'value[ unit]' against the card. A facts card carries the raw value; the older answer box only its
    text, so the text is read too: a date as the card would print it at day, month or year precision; an amount as
    any number in the text within 0.1% (1% when the text rounds to millions or billions; a printed rounding of a decimal), also after the unit conversions the box
    makes (cm -> m, ft -> m, mi2 -> km2); a label as a substring."""
    exp = expected.strip()
    got = str(card.get("value") or "")
    ans = str(card.get("answer") or "")
    m = re.match(r"^(-?)(\d{1,6})-(\d\d)-(\d\d)$", exp)
    if m:
        if got:
            return got == exp
        y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
        ys = f"{y} BC" if m.group(1) else str(y)
        forms = [f"{_MONTHS[mo - 1]} {d}, {ys}", f"{_MONTHS[mo - 1]} {ys}"] if 1 <= mo <= 12 else []
        return any(f in ans for f in forms) or re.fullmatch(rf"\D*{re.escape(ys)}\D*", ans) is not None
    m = re.match(r"^(-?[\d.eE+-]+)(?: (\S+))?$", exp)
    if m:
        try:
            e = float(m.group(1))
        except ValueError:
            return exp.lower() in ans.lower()
        if got:
            try:
                return abs(e - float(got)) <= abs(e) * 0.005 + 1e-9
            except ValueError:
                return False
        conv = {"cm": 0.01, "ft": 0.3048, "in": 0.0254, "km": 1000.0, "mi": 1609.344, "mi2": 2.589988, "ha": 0.01, "m2": 1e-6}.get(m.group(2) or "", 1.0)
        want = {e, e * conv, e * conv / 1000}
        return any(abs(w - n) <= abs(w) * (0.01 if rounded else 0.001) + (0.006 if abs(w) < 10 else 0.51 if "." in m.group(1) else 1e-9)
                   for w in want for n, rounded in _numbers(ans))
    return exp.lower() == got.lower() or exp.lower() in ans.lower()


def cmd_rescore(a) -> None:
    """Score a --details file again (no engine): the same counts as `answered`, from the stored cards."""
    n = ans = card = 0
    with open(a.details, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if a.split and a.splits.get(r["query"]) != a.split:
                continue
            n += 1
            ok = r["card"] == "1"
            card += ok
            ans += ok and value_matches(r["expected"].partition("|")[2], json.loads(r["got"]))
    print(f"facts{' ' + a.split if a.split else ''}: n={n} ANSWERED={ans} ({100 * ans / max(n, 1):.1f}%) CARD={card} ({100 * card / max(n, 1):.1f}%)")


def cmd_answered(a) -> None:
    rows = read_set(a.set, a.split)
    n = ans = card = 0
    per: dict[str, list[int]] = {}
    det = open(a.details, "w", encoding="utf-8") if a.details else None
    if det:
        det.write("query\texpected\tanswered\tcard\tgot\n")
    for r in rows:
        title, _, value = r["expected"].partition("|")
        want_url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        try:
            d = instant(a.url, r["query"], results=False)
        except Exception as e:                              # a timeout is a miss, not a crash
            print(f"{r['query']!r}: {type(e).__name__}", file=sys.stderr)
            d = {}
        c = d.get("answer") or {}
        right_article = bool(c) and c.get("url") == want_url and c.get("answer")
        hit = bool(right_article and value_matches(value, c))
        prop = r["stratum"].split(",")[0]
        per.setdefault(prop, [0, 0, 0])
        per[prop][0] += 1
        per[prop][1] += hit
        per[prop][2] += bool(right_article)
        n += 1
        ans += hit
        card += bool(right_article)
        if det:
            det.write(f"{r['query']}\t{r['expected']}\t{int(hit)}\t{int(bool(right_article))}\t{json.dumps({k: c.get(k) for k in ('title', 'answer', 'value', 'label')}, ensure_ascii=False)}\n")
    if det:
        det.close()
    print(f"facts{' ' + a.split if a.split else ''}: n={n} ANSWERED={ans} ({100 * ans / max(n, 1):.1f}%) CARD={card} ({100 * card / max(n, 1):.1f}%)")
    for prop, (k, h_, c_) in sorted(per.items(), key=lambda x: -x[1][0]):
        print(f"  {prop:<32} n={k:<4} answered={h_:<4} card={c_}")


def sample(sets: list[str], split: str | None, n: int) -> list[str]:
    qs = []
    for p in sets:
        qs += [r["query"] for r in read_set(p, split)]
    return sorted(set(qs), key=lambda q: hashlib.md5(("402:lists:" + q).encode()).hexdigest())[:n]


def cmd_dump(a) -> None:
    """query -> the url sequence of `results` and whether an answer box came back, as JSON lines."""
    qs = sample(a.sets, a.split, a.n)
    fail = 0
    with open(a.out, "w", encoding="utf-8") as o:
        for q in qs:
            try:
                d = instant(a.url, q)
            except Exception as e:
                fail += 1
                print(f"{q!r}: {type(e).__name__}", file=sys.stderr)
                continue
            ans = d.get("answer") or {}
            o.write(json.dumps({"q": q, "urls": [r.get("url") for r in d.get("results", [])], "titles": [r.get("title") for r in d.get("results", [])],
                                "answer": bool(ans.get("answer")), "kind": ans.get("kind") or ans.get("question")}, ensure_ascii=False) + "\n")
    print(f"dump: {len(qs) - fail} of {len(qs)} queries -> {a.out} (failed {fail})")


def cmd_diff(a) -> None:
    """Two dumps (one engine at a time on this workstation): how many result lists differ."""
    def load(p):
        return {r["q"]: r for r in map(json.loads, open(p, encoding="utf-8"))}
    x, y = load(a.a), load(a.b)
    both = sorted(set(x) & set(y))
    differ = [q for q in both if (x[q]["urls"], x[q]["titles"]) != (y[q]["urls"], y[q]["titles"])]
    print(f"lists: n={len(both)} same={len(both) - len(differ)} differ={len(differ)} only_a={len(set(x) - set(y))} only_b={len(set(y) - set(x))}")
    print(f"answer boxes: A {sum(x[q]['answer'] for q in both)} B {sum(y[q]['answer'] for q in both)}; "
          f"A kinds {dict(Counter(str(x[q]['kind']) for q in both if x[q]['answer']))} B kinds {dict(Counter(str(y[q]['kind']) for q in both if y[q]['answer']))}")
    for q in differ[:10]:
        print(f"  {q!r}\n    A {x[q]['urls'][:3]}\n    B {y[q]['urls'][:3]}")
    sys.exit(1 if differ else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("answered")
    s.add_argument("set")
    s.add_argument("--url", required=True)
    s.add_argument("--split", default=None)
    s.add_argument("--details", default=None)
    s.set_defaults(fn=cmd_answered)
    s = sub.add_parser("rescore")
    s.add_argument("details")
    s.add_argument("--set", default=None, help="the set file, to score one --split")
    s.add_argument("--split", default=None)
    s.set_defaults(fn=cmd_rescore)
    s = sub.add_parser("dump")
    s.add_argument("url")
    s.add_argument("out")
    s.add_argument("sets", nargs="+")
    s.add_argument("--n", type=int, default=500)
    s.add_argument("--split", default="dev")
    s.set_defaults(fn=cmd_dump)
    s = sub.add_parser("diff")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_diff)
    a = ap.parse_args()
    if a.cmd == "rescore":
        a.splits = {r["query"]: r["split"] for r in read_set(a.set, None)} if a.set else {}
    a.fn(a)


if __name__ == "__main__":
    main()
