#!/usr/bin/env python3
"""Maker-product eval set ("keep" showed no Google Keep): a maker's product or service asked by its
bare word, without the maker. No Tranco, no Jev, no database: Wikidata (CC0) through QLever and Wikipedia pageviews
(CC0) through the Wikimedia REST API, as head-brand-evalset.py (whose helpers this reads).

    tools/maker-product-evalset.py [--out tools/evalsets/open] [--n 400] [--month 2026-09]

head-brand cannot hold these rows: its title must name the host, and "Google Keep" does not name keep.google.com.

A row is an English Wikipedia article with an official website (P856) and a maker that has an article of its own
(developer P178, owner P127, parent P749, manufacturer P176, publisher P123, operator P137, creator P170), where

  title     is "<maker> <rest>": the maker's article title (company suffixes dropped), then the query
  site      is named by the query: the query is a label of the host, a path segment, or follows the maker in a
            label (keep.google.com, google.com/maps, tesla.com/modely, redbullracing.com)
  head      the --n candidates with the most Wikidata sitelinks; their pageviews of --month give the strata

Query: the rest, lowercased. expected: the host of every such site of the item, "|"-joined: a result on that host
is a hit, so a row whose site is a path (google.com/maps) is also hit by the maker's front page. LIMIT: many words
are a common word first ("canal", "news", "air"); the product is ONE right answer there. Read hit@5 and hit@10,
not hit@1, and read the strata apart. Two products of one word ("maps": Google's and Apple's) are two rows.

Split and columns are open-evalsets.py's (seed 402, by normalised article title).
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import urllib.parse
from pathlib import Path

_spec = importlib.util.spec_from_file_location("head_brand", Path(__file__).with_name("head-brand-evalset.py"))
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)

MAKER_PROPS = "P178 P127 P749 P176 P123 P137 P170".split()
CANDIDATE_QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?article ?site ?sl ?makerArticle WHERE {
  ?item wdt:P856 ?site ; wikibase:sitelinks ?sl .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  %s
  ?makerArticle schema:about ?maker ; schema:isPartOf <https://en.wikipedia.org/> .
}""" % " UNION ".join("{ ?item wdt:%s ?maker }" % p for p in MAKER_PROPS)
_SUFFIX = re.compile(r",?\s+(inc\.?|llc|corporation|corp\.?|ltd\.?|company|co\.|group|plc|s\.a\.|ag|gmbh|platforms|holdings)$", re.I)
_REST = re.compile(r"[a-z0-9+&' .-]{2,30}")
COLS = hb.COLS


def title_of(iri: str) -> str:
    return urllib.parse.unquote(iri.strip("<>").rsplit("/wiki/", 1)[-1]).replace("_", " ")


def maker_names(maker_title: str) -> list[str]:
    """The maker's names, longest first: "Apple Inc." -> ["Apple Inc.", "Apple"]."""
    m = hb.plain_title(maker_title)
    names = [m]
    while _SUFFIX.search(m):
        m = _SUFFIX.sub("", m)
        names.append(m)
    return sorted(set(names), key=len, reverse=True)


def rest_of(title: str, maker_title: str) -> tuple[str, str] | None:
    """(maker, query) when the title is "<maker> <rest>", else None. A rest of digits only is a version, not a name."""
    plain = hb.plain_title(title)
    for n in maker_names(maker_title):
        if plain.lower().startswith(n.lower() + " ") and len(plain) > len(n) + 1:
            rest = plain[len(n) + 1:].strip().lower()
            if _REST.fullmatch(rest) and not re.fullmatch(r"[0-9. ]+", rest) and len(hb.norm_key(rest)) >= 3:
                return n, rest
            return None
    return None


def site_names(url: str, maker: str, query: str) -> str | None:
    """The host when the site is named by the query (see the module text), else None."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if "." not in host:
        return None
    k = hb.norm_key(query)
    labels = [hb.norm_key(x) for x in host.split(".")[:-1]]
    segs = [hb.norm_key(x) for x in u.path.split("/") if x]
    return host if k in labels or k in segs or (hb.norm_key(maker) + k) in labels else None


def candidates(lines) -> list[tuple[str, str, str, int]]:
    """QLever TSV (article, site, sitelinks, maker article) -> [(title, query, hosts, sitelinks)], most linked first."""
    per: dict[str, dict] = {}
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 4 or f[0].startswith("?") or "/wiki/" not in f[0] or "/wiki/" not in f[3]:
            continue
        title = title_of(f[0])
        got = rest_of(title, title_of(f[3]))
        if not got:
            continue
        host = site_names(f[1].strip("<>"), *got)
        if not host:
            continue
        try:
            sl = int(re.sub(r"[^0-9]", "", f[2].split("^^")[0]) or 0)
        except ValueError:
            sl = 0
        e = per.setdefault(title, {"q": got[1], "sl": 0, "hosts": set()})
        e["sl"] = max(e["sl"], sl)
        e["hosts"].add(host)
    rows = [(t, e["q"], "|".join(sorted(e["hosts"], key=lambda h: h.encode("utf-8"))), e["sl"]) for t, e in per.items()]
    rows.sort(key=lambda r: (-r[3], r[0].encode("utf-8")))
    return rows


def build(rows, views: dict[str, int]) -> list[dict]:
    head = sorted(rows, key=lambda r: (-views.get(r[0], 0), r[0].encode("utf-8")))
    out = []
    for i, (title, q, hosts, _sl) in enumerate(head):
        band = "1-100" if i < 100 else "101+"
        kind = "subdomain" if any(h.count(".") >= 2 and hb.norm_key(q) in [hb.norm_key(x) for x in h.split(".")[:-2]]
                                  for h in hosts.split("|")) else "other"
        ent = hb.norm_key(title)
        out.append({"query": q, "category": "site", "expected": hosts, "set": "maker-product",
                    "stratum": f"views-rank:{band},site:{kind}", "split": hb.split_of(ent), "entity": ent})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "evalsets" / "open"))
    ap.add_argument("--n", type=int, default=400, help="rows: the most-linked candidates")
    ap.add_argument("--month", default="2026-09", help="pageview month, YYYY-MM")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "logout"), help="QLever and pageview answers (delete to refresh)")
    a = ap.parse_args()
    cache, out = Path(a.cache), Path(a.out)
    lines = hb.qlever(CANDIDATE_QUERY, cache / "maker-product-candidates.tsv")
    if lines and lines[0].lstrip().startswith("{"):
        (cache / "maker-product-candidates.tsv").unlink()
        sys.exit("QLever answered with an error, not rows - try later")
    rows = candidates(lines)
    hb.log(f"{len(rows):,} products whose site is named by the bare word")
    pool = rows[:a.n]
    views = hb.pageviews([r[0] for r in pool], a.month, cache / f"maker-product-pageviews-{a.month}.tsv", workers=2)
    res = build(pool, views)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "maker-product.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in res:
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    hb.log(f"wrote {len(res):,} rows: {sum(r['split'] == 'dev' for r in res)} dev, {sum(r['split'] == 'test' for r in res)} test")


if __name__ == "__main__":
    main()
