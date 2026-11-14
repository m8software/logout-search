#!/usr/bin/env python3
"""How-to topic normalisation and near-duplicate clustering. Stdlib only.

    topics.py key "How to Tie a Tie (with Pictures) - wikiHow"      -> ti ti
    topics.py normalise TITLES.tsv [--col 0] [--out keys.tsv]       title rows -> title TAB key
    topics.py cluster DEMAND.tsv [--threshold 0.6] [--out F]        key TAB score [TAB generator] rows
                                                                    -> canonical key TAB members TAB score

The same `key()` runs on the demand side (Common Crawl slugs, Stack Exchange titles) and, later, on the
query side of the box, so "how to tie a necktie", "How To Tie A Tie!" and "/how-to-tie-a-tie.html" are
one topic. Steps: lower-case, strip accents, keep the part after "how to / how do i / how can you",
drop a site suffix (" - wikiHow", " | Allrecipes"), drop the filler people put in titles ("step by
step", "(with pictures)", "easy", "in 5 steps"), map spelling variants (tyre->tire, necktie->tie), drop
stop words, stem (a light suffix stripper, the same on both sides — it does not need to be English-
correct, only consistent).

Clustering is string similarity: Jaccard over stem sets, or one set inside the other by at most one
stem. The issue asks for bge-small embeddings ("tie a tie" = "tie a necktie"); those need
sentence-transformers and a GPU (tools/embed.py on buildbox), so this file takes them as an OPTION:
`cluster ... --vecs PREFIX` reads PREFIX.ids.npy / PREFIX.vecs.npy written by embed.py over the keys
(id = row number) and adds cosine >= --cos (0.90) as a second reason to merge. Without --vecs the
string rule alone runs, on any machine, in seconds for 100k keys.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import defaultdict

STOP = frozenset("a an the of for to in on at with and or your my you i me it its into from by do does "
                 "can is are be this that these those up out off some any so they them their when which what"
                 .split())
# Spelling and synonym variants folded BEFORE stemming (one direction, lower-case words).
SYN = {"tyre": "tire", "tyres": "tire", "necktie": "tie", "neck tie": "tie", "colour": "color", "colours": "color",
       "favourite": "favorite", "organise": "organize", "organising": "organize", "programme": "program",
       "aluminium": "aluminum", "mobile": "phone", "cellphone": "phone", "cell phone": "phone",
       "pc": "computer", "laptop": "computer", "veggies": "vegetables", "fridge": "refrigerator",
       "loo": "toilet", "wc": "toilet", "jumpstart": "jump start", "jump-start": "jump start",
       "hoover": "vacuum", "iphone": "iphone", "mac": "mac", "macbook": "mac", "ipad": "ipad"}
_FILLER = re.compile(r"\b(step by step|with pictures?|easy|easily|quickly|fast|simple|the (?:right|easy|best) way|"
                     r"at home|for beginners|like a pro|properly|correctly|in \d+ (?:easy |simple )?(?:steps|minutes|ways)|"
                     r"\d+ (?:easy |simple )?ways|tutorial|guide|tips?|tricks?|ultimate|complete|beginner'?s?)\b")
_HOWTO = re.compile(r"\bhow (?:to|do (?:i|you|we)|can (?:i|you|we)|should (?:i|you)|does one|do you) (.+)", re.I)
_SITE_SUFFIX = re.compile(r"\s*[-|–—:·»]\s*[^-|–—:·»]{1,40}$")
_WORD = re.compile(r"[a-z0-9]+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def howto_phrase(title: str) -> str | None:
    """The part after 'how to' (or 'how do i' ...), or None when the title is not a how-to. A site suffix
    after ' - ' / ' | ' is dropped first when the title has one, then trailing punctuation."""
    t = strip_accents(title or "").replace("’", "'").strip()
    t = re.sub(r"(?<=\w)[-_](?=\w)", " ", t)          # a URL slug: how-to-tie-a-tie
    if not t:
        return None
    m = _HOWTO.search(t)
    if not m:
        return None
    rest = m.group(1)
    if re.search(r"\s[-|–—:·»]\s", rest):
        rest = _SITE_SUFFIX.sub("", rest)
    rest = re.sub(r"\(.*?\)|\[.*?\]", " ", rest)
    return rest.strip(" ?!.:,;'\"") or None


def stem(w: str) -> str:
    """A light, deterministic suffix stripper: the same on both sides, not Porter. bake/baking/baked ->
    bak, tie/tying/tied/ties -> ti, berries/berry -> berri, dresses/dress -> dress, bananas -> banana."""
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "i"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("es") and w[-3] in "sxzh" and len(w) > 4:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            if len(w) >= 3 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if w.endswith("y") and len(w) > 3:
        w = w[:-1] + "i"
    return w


def words(text: str) -> list[str]:
    """Lower-cased words with variants folded and filler dropped, stop words out, NOT stemmed."""
    t = strip_accents(text or "").lower().replace("'", "")
    for k, v in SYN.items():
        if " " in k and k in t:
            t = t.replace(k, v)
    t = _FILLER.sub(" ", t)
    out = []
    for w in _WORD.findall(t):
        w = SYN.get(w, w)
        for part in w.split():
            if part not in STOP:
                out.append(part)
    return out


def stems(text: str) -> list[str]:
    return [stem(w) for w in words(text)]


def key(text: str) -> str:
    """The topic key of a how-to title or a query: the stems after 'how to', space-joined. A text with no
    'how to' in it is keyed whole (the query side: 'tie a tie' asks the same as 'how to tie a tie')."""
    phrase = howto_phrase(text)
    return " ".join(stems(phrase if phrase is not None else text))


def slug_title(slug: str) -> str:
    """A URL slug from the Common Crawl index ('how-to-tie-a-tie-12345.html') -> words."""
    s = re.sub(r"\.(html?|php|aspx?|jsp|cfm)$", "", slug.lower())
    s = re.sub(r"[-_]\d{3,}$", "", s)             # a trailing numeric id
    return s.replace("-", " ").replace("_", " ").strip()


# --- clustering -------------------------------------------------------------------------------------

def similar(a: frozenset, b: frozenset, threshold: float) -> bool:
    if not a or not b:
        return False
    if a <= b or b <= a:
        return abs(len(a) - len(b)) <= 1
    return len(a & b) / len(a | b) >= threshold


def _cos_sim(vecs_prefix: str, cos: float, k: int = 5, block: int = 4096):
    """-> (similar(i, j), candidate pairs) over tools/embed.py output (ids = row numbers of the keys):
    each key's k nearest neighbours at cosine >= cos, found by blocked matrix products. Needs numpy."""
    import numpy as np                                   # only with --vecs; stdlib otherwise
    ids = np.load(vecs_prefix + ".ids.npy")
    vecs = np.load(vecs_prefix + ".vecs.npy").astype("float32")
    row = {int(i): n for n, i in enumerate(ids)}
    pairs = []
    for s in range(0, len(ids), block):
        sims = vecs[s:s + block] @ vecs.T
        for r in range(sims.shape[0]):
            sims[r, s + r] = -1.0                        # not itself
            for n in np.argpartition(-sims[r], k)[:k]:
                if sims[r, n] >= cos:
                    pairs.append((min(int(ids[s + r]), int(ids[n])), max(int(ids[s + r]), int(ids[n]))))

    def sim(i: int, j: int) -> bool:
        if i not in row or j not in row:
            return False
        return float(vecs[row[i]] @ vecs[row[j]]) >= cos
    return sim, sorted(set(pairs))


def cluster(items: list[tuple[str, float]], threshold: float = 0.6, max_df: int = 2000, extra_sim=None, extra_pairs=None):
    """[(key, score)] -> [(canonical key, [member keys], score sum)], largest first. Candidates for a pair
    share a stem seen in fewer than max_df keys (so 'make' and 'clean' do not pair everything); the
    canonical member is the one with the highest score. extra_sim(i, j) -> bool adds a second reason to
    merge, and extra_pairs (i, j) adds candidates that share no stem (the embedding neighbours)."""
    keys = [k for k, _ in items]
    sets = [frozenset(k.split()) for k in keys]
    parent = list(range(len(keys)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_stem: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(sets):
        for st in s:
            by_stem[st].append(i)
    def candidates():
        seen = set()
        for rows in by_stem.values():
            if len(rows) > max_df:
                continue
            for x in range(len(rows)):
                for y in range(x + 1, len(rows)):
                    if (rows[x], rows[y]) not in seen:
                        seen.add((rows[x], rows[y]))
                        yield rows[x], rows[y]
        for i, j in extra_pairs or ():
            if (i, j) not in seen and i < len(keys) and j < len(keys):
                seen.add((i, j))
                yield i, j

    for i, j in candidates():
        if similar(sets[i], sets[j], threshold) or (extra_sim is not None and extra_sim(i, j)):
            union(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(keys)):
        groups[find(i)].append(i)
    out = []
    for members in groups.values():
        members.sort(key=lambda i: (-items[i][1], keys[i]))
        out.append((keys[members[0]], [keys[i] for i in members], sum(items[i][1] for i in members)))
    out.sort(key=lambda c: (-c[2], c[0]))
    return out


# --- CLI --------------------------------------------------------------------------------------------

def _rows(path: str):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if row and not row[0].startswith("#"):
                yield row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("key")
    k.add_argument("text", nargs="+")
    n = sub.add_parser("normalise")
    n.add_argument("path")
    n.add_argument("--col", type=int, default=0)
    n.add_argument("--out", default="-")
    c = sub.add_parser("cluster")
    c.add_argument("path", help="key TAB score [TAB generator] (a header row starting with # is skipped)")
    c.add_argument("--threshold", type=float, default=0.6)
    c.add_argument("--vecs", help="tools/embed.py output prefix over the keys (row number = id)")
    c.add_argument("--cos", type=float, default=0.90)
    c.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    out = sys.stdout if a.out == "-" else open(a.out, "w", encoding="utf-8")
    if a.cmd == "key":
        print(key(" ".join(a.text)), file=out)
    elif a.cmd == "normalise":
        for row in _rows(a.path):
            if len(row) > a.col:
                print(f"{row[a.col]}\t{key(row[a.col])}", file=out)
    else:
        items = [(r[0], float(r[1]) if len(r) > 1 else 1.0) for r in _rows(a.path)]
        extra, pairs = _cos_sim(a.vecs, a.cos) if a.vecs else (None, None)
        for canon, members, score in cluster(items, a.threshold, extra_sim=extra, extra_pairs=pairs):
            print(f"{canon}\t{'|'.join(members)}\t{score:g}", file=out)
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
