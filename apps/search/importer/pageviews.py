"""How much each English Wikipedia article is read (2026-09-24).

wiki_pop.sitelinks (how many language editions have the article) favours what is
famous worldwide over what is read now in English ("mon" -> Mongolia, not Monday).
Wikimedia's public monthly pageview dump — one archive download, no live fetching —
adds the month's views per article.

The dump: pageview_complete/monthly/YYYY/YYYY-MM/pageviews-YYYYMM-user.bz2 (~5 GB for
2026-08), lines "wiki title page_id access monthly_total hourly…", sorted by wiki; only
en.wikipedia lines are kept, the access methods (desktop / mobile-web / mobile-app)
summed per title. User agents only — the "-user" file excludes known bots.

    search_import.py pageviews [--month 2026-08]   -> wiki_pop.pageviews
"""

from __future__ import annotations

import bz2
import time
import urllib.request

URL = "https://dumps.wikimedia.org/other/pageview_complete/monthly/{y}/{y}-{m}/pageviews-{y}{m}-user.bz2"
USER_AGENT = "logout-search-import/1 (personal search index)"
WIKI = "en.wikipedia "


def log(msg: str) -> None:
    print(f"[pageviews] {msg}", flush=True)


def parse_line(line: str) -> tuple[str, int] | None:
    """'en.wikipedia Mona_Lisa 70889 desktop 123456 A1B2…' -> ('Mona Lisa', 123456);
    None for other wikis and malformed lines."""
    if not line.startswith(WIKI):
        return None
    p = line.split(" ")
    if len(p) < 5 or not p[4].isdigit() or p[1] in ("", "-"):
        return None
    return p[1].replace("_", " "), int(p[4])


def past_english(line: str) -> bool:
    """The dump is sorted by wiki code; once a line's code sorts after en.wikipedia
    (en.wikiquote, eo.…), no English Wikipedia line can follow."""
    return line.split(" ", 1)[0] > WIKI.strip()


def lines_of(stream, chunk: int = 1 << 20):
    """Decompress a (multi-stream) bz2 byte stream into text lines, one chunk at a time."""
    dec = bz2.BZ2Decompressor()
    tail = b""
    while True:
        block = stream.read(chunk)
        if not block:
            break
        while block:
            out = dec.decompress(block)
            block = b""
            if dec.eof:                       # the dump is several concatenated bz2 streams
                block = dec.unused_data
                dec = bz2.BZ2Decompressor()
            buf = tail + out
            parts = buf.split(b"\n")
            tail = parts.pop()
            for p in parts:
                yield p.decode("utf-8", "replace")
    if tail:
        yield tail.decode("utf-8", "replace")


def cmd_pageviews(conn, month: str = "2026-08") -> None:
    y, m = month.split("-")
    url = URL.format(y=y, m=m)
    t0 = time.time()
    views: dict[str, int] = {}
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        seen_en = False
        for line in lines_of(r):
            n += 1
            got = parse_line(line)
            if got:
                seen_en = True
                views[got[0]] = views.get(got[0], 0) + got[1]
            elif seen_en and past_english(line):
                break                          # sorted by wiki: English Wikipedia is over
            if n % 20_000_000 == 0:
                log(f"{n:,} lines, {len(views):,} English articles, {(time.time() - t0) / 60:.1f} min")
    log(f"{len(views):,} English Wikipedia titles with views in {month} ({n:,} lines read)")
    conn.execute("CREATE TEMP TABLE pv (title text, views bigint)")
    with conn.cursor().copy("COPY pv (title, views) FROM STDIN") as cp:
        for title, v in views.items():
            cp.write_row((title, v))
    with conn.transaction():
        conn.execute("ALTER TABLE wiki_pop ADD COLUMN IF NOT EXISTS pageviews bigint")
        conn.execute("UPDATE wiki_pop SET pageviews = NULL")
        k = conn.execute("UPDATE wiki_pop p SET pageviews = pv.views FROM wiki_docs w JOIN pv ON pv.title = w.title "
                         "WHERE w.page_id = p.page_id").rowcount
    log(f"{k:,} articles in wiki_pop got {month}'s views in {(time.time() - t0) / 60:.1f} min")
