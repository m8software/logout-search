#!/usr/bin/env python3
"""Browser history -> eval labels for eval-queries.py: query<TAB>category<TAB>expected.

A web search followed by a click is a label someone already wrote: the query, and
the site they actually went to. Chromium-family histories only (Brave, Chrome,
Chromium; buildbox's copied History file). Nothing leaves this machine — the output is
a file you keep OUT of git (it is your search history).

    tools/history-labels.py HISTORY [HISTORY…] > labels.tsv
    tools/history-labels.py --local > labels.tsv        # this workstation's browsers

Click = the first visit whose referrer (from_visit) is the search page, or else the
next link-click visit within CLICK_WINDOW_S of it (SearXNG opens results with no
referrer). A query is kept when one destination got most of its clicks.
    category site   expected = the clicked domain, and the query names it (a bank's name)
    category click  expected = the clicked domain, a content click (a person and a topic)
    category wiki   expected = the Wikipedia article title
Clicks into our own boxes (*.logout.cloud) are not labels.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import tldextract

CLICK_WINDOW_S = 90
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Web search result pages: host -> query parameter. In-site searches (YouTube,
# Amazon) are not web searches and are skipped.
ENGINES = {"www.google.com": "q", "google.com": "q", "www.bing.com": "q", "bing.com": "q",
           "duckduckgo.com": "q", "html.duckduckgo.com": "q", "search.brave.com": "q",
           "www.startpage.com": "query", "www.qwant.com": "q", "www.mojeek.com": "q"}
SEARXNG_SUFFIX = ".logout.cloud"       # search.<prefix>.logout.cloud/search?q= and /local/search?q=
LINK = 0                               # Chromium core transition: clicked a link

LOCAL_GLOBS = [".config/BraveSoftware/Brave-Browser/*/History", ".config/google-chrome/*/History",
               ".config/chromium/*/History", "snap/chromium/common/chromium/*/History"]


def search_query(url: str) -> str | None:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if host in ENGINES and u.path.rstrip("/") in ("", "/search", "/html"):
        param = ENGINES[host]
    elif host.startswith("search.") and host.endswith(SEARXNG_SUFFIX) and u.path.endswith("/search"):
        param = "q"
    else:
        return None
    q = (parse_qs(u.query).get(param) or [""])[0].strip()
    return " ".join(q.lower().split()) or None


def destination(url: str) -> tuple[str, str] | None:
    """Clicked URL -> (category, expected), or None for search pages and junk."""
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if not host or u.scheme not in ("http", "https") or search_query(url) is not None:
        return None
    if host.endswith(".wikipedia.org") and u.path.startswith("/wiki/"):
        return "wiki", unquote(u.path[6:]).replace("_", " ")
    r = _extract(host)
    if not r.domain or not r.suffix:
        return None
    dom = f"{r.domain}.{r.suffix}"
    if dom == "logout.cloud":
        return None
    if host in ENGINES or dom in ("google.com", "bing.com", "duckduckgo.com", "brave.com") and host != dom:
        return None
    return "site", dom


def names_site(q: str, domain: str) -> bool:
    """Does the query name the domain? "wells fargo" / wellsfargo.com, "youtrube" is not."""
    key = "".join(ch for ch in q if ch.isalnum())
    label = domain.split(".")[0].replace("-", "")
    return len(label) >= 2 and (label in key or key in label)


def clicks(path: Path) -> list[tuple[str, tuple[str, str]]]:
    with tempfile.TemporaryDirectory() as d:        # locked while the browser runs: copy first
        tmp = Path(d) / "History"
        shutil.copy(path, tmp)
        db = sqlite3.connect(tmp)
        rows = db.execute("SELECT v.id, v.visit_time, v.from_visit, v.transition & 255, u.url "
                          "FROM visits v JOIN urls u ON u.id = v.url ORDER BY v.visit_time").fetchall()
        db.close()
    children = defaultdict(list)
    for r in rows:
        if r[2]:
            children[r[2]].append(r)
    out = []
    for i, (vid, t, _frm, _tr, url) in enumerate(rows):
        q = search_query(url)
        if not q:
            continue
        dest = None
        for kid in sorted(children.get(vid, []), key=lambda k: k[1]):
            dest = destination(kid[4])
            if dest:
                break
        if dest is None:                              # no referrer: next link click, soon after
            for nxt in rows[i + 1:]:
                if (nxt[1] - t) / 1e6 > CLICK_WINDOW_S or search_query(nxt[4]):
                    break
                if nxt[3] == LINK:
                    dest = destination(nxt[4])
                    if dest:
                        break
        if dest:
            out.append((q, dest))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("histories", nargs="*", type=Path)
    ap.add_argument("--local", action="store_true", help="also read this workstation's browsers")
    ap.add_argument("--min-share", type=float, default=0.6, help="keep a query when one destination has this share")
    a = ap.parse_args()
    paths = list(a.histories)
    if a.local:
        for g in LOCAL_GLOBS:
            paths += sorted(Path.home().glob(g))
    per_q: dict[str, Counter] = defaultdict(Counter)
    for p in paths:
        for q, dest in clicks(p):
            per_q[q][dest] += 1
    kept = 0
    for q, c in sorted(per_q.items()):
        (cat, exp), n = c.most_common(1)[0]
        if cat == "site" and not names_site(q, exp):
            cat = "click"
        if n / sum(c.values()) >= a.min_share:
            print(f"{q}\t{cat}\t{exp}")
            kept += 1
    print(f"{len(paths)} histories, {len(per_q)} queries with a click, {kept} labelled", file=sys.stderr)


if __name__ == "__main__":
    main()
