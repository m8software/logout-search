#!/usr/bin/env python3
"""The how-to deny/allow list: categories.tsv loaded and applied to a topic. Stdlib only.

    categories.py "how to jump a car"                 -> allow  vehicle-trivial  (jump a car)
    categories.py --file topics.tsv [--col 0]         -> topic TAB verdict TAB category TAB rule

Verdicts: allow (may be generated), deny (never), health (MedlinePlus topic, never generated), unknown
(no rule matched, or an `other` row did — a topic outside the allowed categories that is not on the deny
list, e.g. business or marketing: not generated either — the later kinds-model pass or a person files it). The rules
are keyword and phrase lists; a pattern matches as a contiguous run of stems inside the topic key
(topics.key), so one rule covers "tie / tying / tied". Precedence: allow! exceptions, then deny, then
health, then other, then allow — a topic that trips any deny row is denied however many allow rows it also hits.

Using the kinds model later: the engine's kinds classifier (apps/search/importer/kinds.py, bge-small
nearest-kinds) can label the "unknown" remainder by nearest allowed/denied EXAMPLES, but it may only
move a topic INTO allow when no deny stem is present; a deny here is final. That pass is not written
yet; this file is the checked-in, tested gate it would sit behind.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import topics  # noqa: E402

DEFAULT_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "categories.tsv")
VERDICTS = ("allow!", "deny", "health", "other", "allow")
RETURNED = {"allow!": "allow", "other": "unknown"}     # what classify() calls a verdict it matched
ARTICLES = frozenset("a an the of to for in on with and or your my".split())
ALLOWED = ("cooking", "crafts", "household", "software", "gardening", "travel", "school", "vehicle-trivial")
UNGROUNDED_OK = frozenset({"cooking", "crafts", "household", "software"})


class Rules:
    """categories.tsv as (verdict, category, pattern stems) rows, in file order within a verdict."""

    def __init__(self, rows: list[tuple[str, str, str]]):
        self.rows = []
        for verdict, category, pattern in rows:
            if verdict not in VERDICTS:
                raise ValueError(f"categories.tsv: unknown verdict {verdict!r} on {pattern!r}")
            pat = tuple(topics.stems(pattern))
            if not pat:
                raise ValueError(f"categories.tsv: pattern {pattern!r} has no words after normalisation")
            # A pattern word that is a stop word silently widens the rule ("stitches out" -> "stitch"): refuse
            # any lost word that is not a plain article/preposition.
            lost = [w for w in topics._WORD.findall(pattern.lower()) if w in topics.STOP and w not in ARTICLES]
            if lost:
                raise ValueError(f"categories.tsv: pattern {pattern!r} loses {lost} to the stop list — reword it")
            # "get to" is one stem (get) once "to" is dropped, and then allows every "how to get ..." — 2,253
            # false travel allows on the first real list. A pattern written as several words must keep two.
            if len(pat) < 2 and len(topics._WORD.findall(pattern.lower())) > 1:
                raise ValueError(f"categories.tsv: pattern {pattern!r} is the single stem {pat[0]!r} after the stop "
                                 f"list — it would match far more than it says; write that stem or a longer phrase")
            self.rows.append((verdict, category, pat, pattern))
        self.by_verdict = {v: [r for r in self.rows if r[0] == v] for v in VERDICTS}

    @classmethod
    def load(cls, path: str = DEFAULT_TSV) -> "Rules":
        rows = []
        with open(path, encoding="utf-8", newline="") as f:
            for n, row in enumerate(csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE), 1):
                if not row or row[0].startswith("#") or not row[0].strip():
                    continue
                if len(row) != 3:
                    raise ValueError(f"{path}:{n}: expected verdict<TAB>category<TAB>pattern, got {row!r}")
                rows.append((row[0].strip(), row[1].strip(), row[2].strip()))
        return cls(rows)

    @staticmethod
    def _contains(hay: list[str], needle: tuple[str, ...]) -> bool:
        n = len(needle)
        return any(hay[i:i + n] == list(needle) for i in range(len(hay) - n + 1))

    def classify(self, topic: str) -> tuple[str, str, str]:
        """-> (verdict, category, rule): verdict in allow / deny / health / unknown."""
        key = topics.key(topic).split()
        if not key:
            return ("unknown", "", "")
        for verdict in VERDICTS:
            for _v, category, pat, raw in self.by_verdict[verdict]:
                if self._contains(key, pat):
                    return (RETURNED.get(verdict, verdict), category, raw)
        return ("unknown", "", "")

    def generation_mode(self, topic: str, has_source: bool) -> str:
        """What the generator may do with a topic: 'grounded', 'ungrounded', 'skip' (allowed category but
        no source and ungrounded text is not permitted there), 'health' or 'deny'."""
        verdict, category, _ = self.classify(topic)
        if verdict != "allow":
            return verdict if verdict in ("health", "deny") else "deny"
        if has_source:
            return "grounded"
        return "ungrounded" if category in UNGROUNDED_OK else "skip"


_RULES: Rules | None = None


def classify(topic: str, path: str = DEFAULT_TSV) -> tuple[str, str, str]:
    global _RULES
    if _RULES is None or path != DEFAULT_TSV:
        rules = Rules.load(path)
        if path == DEFAULT_TSV:
            _RULES = rules
        return rules.classify(topic)
    return _RULES.classify(topic)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("topic", nargs="*")
    ap.add_argument("--file", help="TSV of topics; --col picks the column")
    ap.add_argument("--col", type=int, default=0)
    ap.add_argument("--rules", default=DEFAULT_TSV)
    a = ap.parse_args(argv)
    rules = Rules.load(a.rules)
    if a.file:
        with open(a.file, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                if row and not row[0].startswith("#") and len(row) > a.col:
                    v, c, r = rules.classify(row[a.col])
                    print(f"{row[a.col]}\t{v}\t{c}\t{r}")
    elif a.topic:
        v, c, r = rules.classify(" ".join(a.topic))
        print(f"{v}\t{c}\t({r})" if r else v)
    else:
        ap.error("a topic or --file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
