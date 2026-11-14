#!/usr/bin/env python3
"""Meaning search over web pages: the pages' 1-bit nomic codes, for ivf_build.py.

    pages_vec.py export REGISTRY OUT.tsv.gz       doc_id<TAB>text of every page the release ships, from the pages
                                                   store (SEARCH_PAGES_STORE) or else BRIDGE (PG_DSN)
    pages_vec.py bits SHARD_DIR RAW_PREFIX         embed-full.py shards (nomic-NNNN.ids.npy / .vecs.npy) ->
                                                   RAW_PREFIX.bits (N x 96) + RAW_PREFIX.ids.u64, what ivf_build reads
    then:  ivf_build.py RAW_PREFIX OUT/vectors/pages-ivf1000 RELEASE_ID --lists 1000

Only the codes ship (ADR-059: no readable page text leaves indexhost). The text is the page's title, h1, description and
the start of its body as the build database keeps them, read where the build reads them; the engine compares a
query's code with the pages' codes the way it does for Wikipedia (serve/api/sem_ivf.py, the same encoder and rule:
mean-pooled, unit length, bit i = component i >= 0, first component = most significant bit of byte 0). The rows are
exactly the pages release_v1's web input ships (WEB_INPUT_SQL["pages"]'s filter), with the registry's doc_id.
"""
from __future__ import annotations

import glob
import gzip
import os
import re
import sys
import time

TEXT_CHARS = 1500          # nomic reads 256 tokens a document (embed-full.py); about a thousand characters fill them

PAGES_TEXT_SQL = None      # built from release_v1's own filter in _sql(), so the two cannot drift


def _sql() -> str:
    import release_v1 as rv
    import docs_policy
    return ("SELECT DISTINCT ON (p.url COLLATE \"C\") p.url, coalesce(p.title, ''), coalesce(p.h1, ''), "
            "coalesce(p.description, ''), left(coalesce(p.body, ''), " + str(TEXT_CHARS) + ") "
            "FROM page_docs p WHERE p.error IS NULL AND " + rv.PAGE_WORDS_SQL.replace("body", "p.body") + " "
            "AND p.domain NOT IN (" + docs_policy.PG_DOMAINS_NOINDEX + ") "
            "ORDER BY p.url COLLATE \"C\", p.fetched_at DESC NULLS LAST, p.crawl COLLATE \"C\"")


def text_of(title: str, h1: str, desc: str, body: str) -> str:
    parts = [title]
    if h1 and h1.lower() not in title.lower():
        parts.append(h1)
    if desc:
        parts.append(desc)
    parts.append(body)
    parts = [p.strip().rstrip(".").strip() for p in parts]           # "Licences and rules." joins without "..":
    return re.sub(r"\s+", " ", ". ".join(p for p in parts if p)).strip()[:TEXT_CHARS]


def cmd_export(registry: str, out: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import release_v1 as rv
    t0 = time.time()
    ids = rv._registry_ids(registry, "page")
    n = kept = 0
    part = out + ".part"
    with gzip.open(part, "wt", encoding="utf-8", compresslevel=3) as f:
        import pages_store as ps
        store = ps.store_dir()                         # SEARCH_PAGES_STORE: the pages from files, no Postgres
        src = (((u, ti or "", h or "", de or "", (bo or "")[:TEXT_CHARS]) for u, ti, h, de, bo in
                ps.shipped_pages(store, ["url", "title", "h1", "description", "body"])) if store
               else rv.stream(rv.ex.PG_DSN, _sql()))
        for url, title, h1, desc, body in src:
            n += 1
            d = ids.get(url)
            if d is None:
                continue
            t = text_of(title, h1, desc, body)
            if t:
                f.write(f"{d}\t{t.replace(chr(9), ' ')}\n")
                kept += 1
    os.replace(part, out)
    print(f'{{"stage": "pages-vec-export", "rows": {n}, "kept": {kept}, "unknown_to_registry": {n - kept}, '
          f'"seconds": {time.time() - t0:.0f}}}', flush=True)


def codes(vecs):
    """N x 768 floats -> N x 96 bytes: bit set when the component is >= 0, first component = MSB (sem_ivf.code_of)."""
    import numpy as np
    return np.packbits(np.asarray(vecs) >= 0, axis=1, bitorder="big")


def cmd_bits(shard_dir: str, raw: str) -> None:
    import numpy as np
    shards = sorted(glob.glob(os.path.join(shard_dir, "*-[0-9][0-9][0-9][0-9].vecs.npy")))
    if not shards:
        sys.exit(f"pages-vec bits: no *-NNNN.vecs.npy shard in {shard_dir}")
    all_ids, n = [], 0
    with open(raw + ".bits.part", "wb") as fb:
        for s in shards:
            ids = np.load(s.replace(".vecs.npy", ".ids.npy"))
            v = np.load(s, mmap_mode="r")
            if v.shape != (len(ids), 768):
                sys.exit(f"pages-vec bits: {s} is {v.shape}, its ids {len(ids)}: want N x 768")
            codes(v).tofile(fb)
            all_ids.append(ids.astype(np.uint64))
            n += len(ids)
    ids = np.concatenate(all_ids)
    if len(np.unique(ids)) != len(ids):
        sys.exit("pages-vec bits: a doc_id appears twice across the shards")
    ids.tofile(raw + ".ids.u64.part")
    os.replace(raw + ".bits.part", raw + ".bits")
    os.replace(raw + ".ids.u64.part", raw + ".ids.u64")
    print(f'{{"stage": "pages-vec-bits", "rows": {n}, "shards": {len(shards)}}}', flush=True)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "export":
        cmd_export(argv[1], argv[2])
        return 0
    if len(argv) == 3 and argv[0] == "bits":
        cmd_bits(argv[1], argv[2])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
