"""What a stored answer drew on, as 64-bit tokens (phase 4, 2026-11-15).

A release delta (twin/release_delta.py) turns the rows that changed between two releases into the same kind of token;
an answer whose tokens meet them may be different under the new release, every other answer is the same by construction
(and the exactness gate samples that claim). Computed from the stored body, so answers stored before this existed need no
rebuild. Tokens, not ids: the answer holds urls and titles, the release's tables hold doc ids, keys and aliases, and a
hash of the normalised text is the one thing both sides share.

Query side: the query and its 1-4 word n-grams, because a row keyed by the query's words (a new alias, anchor, card, name)
changes the answer without ever being cited in it.
"""
from __future__ import annotations

import hashlib
import json
import re

_URL = re.compile(r"^https?://", re.I)
BIG_INT = 100000                  # smaller integers are scores, counts, flags: never a doc, place or entity id
STR_MIN, STR_MAX = 2, 300         # a longer string is page text (a result's content), not a key
SKIP_KEYS = frozenset(("content", "score", "qwen", "ce", "lr", "pos0", "source", "kind", "intent", "curated", "flags"))   # labels, not documents


def tok(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "replace"), digest_size=8).digest(), "big")


def norm(s: str) -> str:
    s = " ".join(s.lower().split())
    if _URL.match(s):
        s = s.split("://", 1)[1]
        if s.startswith("www."):
            s = s[4:]
        s = s.rstrip("/")
    return s


def add_str(out: set, s: str) -> None:
    n = norm(s)
    if STR_MIN <= len(n) <= STR_MAX:
        out.add(tok(n))


def add_int(out: set, v: int) -> None:
    if isinstance(v, int) and not isinstance(v, bool) and v >= BIG_INT:
        out.add(tok("#%d" % v))


def _walk(out: set, x) -> None:
    if isinstance(x, str):
        add_str(out, x)
    elif isinstance(x, int):
        add_int(out, x)
    elif isinstance(x, dict):
        for k, v in x.items():
            if k not in SKIP_KEYS:
                _walk(out, v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _walk(out, v)


def query_tokens(q: str, n: int = 4) -> set:
    w = norm(q).split()
    out = {tok(" ".join(w))} if w else set()
    for size in range(1, min(n, len(w)) + 1):
        for i in range(len(w) - size + 1):
            out.add(tok(" ".join(w[i:i + size])))
    return out


def answer_tokens(key: str, body: dict) -> set:
    """Tokens of the stored answer for `key` ("<name>" or "<name>\\x1ffull")."""
    out = query_tokens(key.split("\x1f")[0])
    for f in ("results", "card", "place", "kinds", "local"):
        _walk(out, body.get(f))
    return out


def cand_summary(calls) -> tuple[str, bytes, str]:
    """calls: [(args, rows)] of every base search the answer ran (args = [q, k, semantic, learned, correct, ce], rows = what it
    returned). Returns (hash of the ordered (url, score) of every call's rows, the url tokens packed 6 bytes each, the args as
    JSON for the replay). The hash is the retrieval comparison: the same set in another order or with another score is a
    different answer. Calls are combined in sorted order: parallel searches of one request finish in any order."""
    parts, toks, argl = [], set(), []
    for args, rows in calls:
        h = hashlib.blake2b(digest_size=8)
        for r in rows:
            u = r.get("url") or ""
            h.update(("%s|%s\n" % (norm(u), r.get("score"))).encode("utf-8", "replace"))
            if u:
                toks.add(tok(norm(u)))
        parts.append(h.hexdigest())
        argl.append(list(args))
    argl.sort(key=lambda a: json.dumps(a))
    return (hashlib.blake2b("".join(sorted(parts)).encode(), digest_size=8).hexdigest(),
            b"".join(t.to_bytes(8, "big")[2:] for t in sorted(toks)), json.dumps(argl, separators=(",", ":")))
