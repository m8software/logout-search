#!/usr/bin/env python3
"""Link a maker / labeler / brand-owner / brand name to an official site through Wikidata (CC0) only. No domain is ever guessed from a
name and no company site is fetched. Rules, in order (each rule's precision is sampled by hand before it is kept):

  A   the maker's normalised name equals the label or an alias of a Wikidata business (instance of business or a subclass) that has a
      website P856; ambiguous names (two businesses, two registered domains) are skipped.
  B1  the brand name (FDC brand, an NDC proprietary name, an Open Food Facts brand) equals a Wikidata brand (instance of brand, or the
      target of a brand property P1716) that has its own P856.
  B2  the same brand has no P856 but its owner P127, parent P749 or manufacturer P176 has one.
  C   the maker matches a Wikidata business with no P856 whose parent P749 or owner P127 has one.

Normalisation: accents folded, "&" -> "and", punctuation dropped, company suffixes (Inc, LLC, Co, Corp, Ltd, The, USA, ...) dropped;
for the maker also trailing generic words (Sales, Brands, Foods, Operations, North America, ...) dropped one at a time.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from build_cc0 import GENERIC, SUFFIX, fold, lit, mtokens, rank_site
from maker_sites import domain_of, host_of

MIN_KEY = 3


def nkey(s: str) -> str:
    return "".join(mtokens(s))


class Resolver:
    def __init__(self, wd2: str):
        d = Path(wd2)
        self.org: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))     # key -> domain -> {(qid, site, label)}
        self.brand_own: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        self.brand_up: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        self.nosite: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        psite: dict[str, str] = {}
        for p in self._rows(d / "parent-sites.tsv", 3):
            q = lit(p[0]).rsplit("/", 1)[-1]
            s = lit(p[2])
            if q not in psite or rank_site(nkey(lit(p[1])), s) < rank_site(nkey(lit(p[1])), psite[q]):
                psite[q] = s
        self.psite = psite
        for p in self._rows(d / "orgs.tsv", 5):
            q, label, aliases, site = lit(p[0]).rsplit("/", 1)[-1], lit(p[1]), lit(p[2]), lit(p[3])
            for n in [label] + aliases.split("|"):
                k = nkey(n)
                if len(k) >= MIN_KEY and site:
                    self.org[k][domain_of(host_of(site))].add((q, site, label))
        for p in self._rows(d / "brands.tsv", 7):
            q, label, aliases, site = lit(p[0]).rsplit("/", 1)[-1], lit(p[1]), lit(p[2]), lit(p[3])
            ups = [lit(x).rsplit("/", 1)[-1] for x in p[4:7] if x.strip()]
            for n in [label] + aliases.split("|"):
                k = nkey(n)
                if len(k) < MIN_KEY:
                    continue
                if site:
                    self.brand_own[k][domain_of(host_of(site))].add((q, site, label))
                else:
                    for u in ups:
                        if u in psite:
                            self.brand_up[k][domain_of(host_of(psite[u]))].add((q, psite[u], label + " <- " + u))
        f = d / "orgs-nosite.tsv"
        if f.exists():
            for p in self._rows(f, 5):
                q, label, aliases, par, site = lit(p[0]).rsplit("/", 1)[-1], lit(p[1]), lit(p[2]), lit(p[3]), lit(p[4])
                for n in [label] + aliases.split("|"):
                    k = nkey(n)
                    if len(k) >= MIN_KEY and site:
                        self.nosite[k][domain_of(host_of(site))].add((q, site, label + " <- " + par.rsplit("/", 1)[-1]))

    @staticmethod
    def _rows(path: Path, n: int):
        for line in path.read_text(encoding="utf-8").split("\n"):
            p = line.split("\t")
            if len(p) >= n and "entity/Q" in p[0]:
                yield p

    @staticmethod
    def _pick(table, key):
        dom = table.get(key)
        if not dom or len(dom) != 1:         # none, or ambiguous between registered domains
            return None
        items = sorted(next(iter(dom.values())))
        q, site, label = items[0]
        best = min((i[1] for i in items), key=lambda s: rank_site(key, s))
        return best, q, label

    def candidates(self, maker: str, brand: str = "") -> list[tuple]:
        """Every rule hit in rule order: (site, rule, wikidata id, matched label, {ids whose classes and industries decide the sector})."""
        out = []
        hit = self.resolve(maker, brand, _all=out)
        return out

    def resolve(self, maker: str, brand: str = "", _all: list | None = None) -> tuple[str, str, str, str] | None:
        """(site, rule, wikidata id, matched label) or None."""
        toks = mtokens(maker)
        cands = []
        while toks:
            cands.append("".join(toks))
            if len(toks) > 1 and toks[-1] in GENERIC:
                toks = toks[:-1]
            else:
                break
        def emit(r, rule):
            ids = {r[1]} | set(re.findall(r"Q\d+", r[2].split("<-")[-1] if "<-" in r[2] else ""))
            hit = (r[0], rule, r[1], r[2], ids)
            if _all is None:
                return hit[:4]
            _all.append(hit)
            return None

        for k in cands:
            r = self._pick(self.org, k)
            if r:
                x = emit(r, "A")
                if x:
                    return x
        bks = [nkey(brand)] if brand else []
        for k in bks:
            r = self._pick(self.brand_own, k)
            if r:
                x = emit(r, "B1")
                if x:
                    return x
            r = self._pick(self.brand_up, k)
            if r:
                x = emit(r, "B2")
                if x:
                    return x
        for k in cands:
            r = self._pick(self.nosite, k)
            if r:
                x = emit(r, "C")
                if x:
                    return x
        return None
