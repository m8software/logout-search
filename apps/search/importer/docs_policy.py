"""Which homepage captures a public release may use, and how much of a site's own description it shows.

G3 D4c (docs/legal/SEARCH-DATA-LICENSES.md) and the 2026-10-09 audit (docs/internal/audits/2026-10-09-silo-licensing-audit.md):
a description is the site's own sentence. It ships the way a search engine shows it — short, beside its link, and not
at all where the site said no — or it does not ship.

  capture      crawl LIKE 'wayback%'            never used (the Internet Archive grants no reuse)
               robots has noindex / none        never used (the site asked not to be listed from this page)
  description  robots has nosnippet / max-snippet:0 / tdm-reservation     not shown
               robots has max-snippet:N         cut to N (and to DESCR_MAX)
               robots is NULL (never read)      not shown, unless unchecked="keep" (builds that are not published)
               otherwise                        cut to DESCR_MAX characters at a word, with an ellipsis

`robots` is what importer/ccfetch.py stored for the capture (robots_field): '' = read, nothing said; NULL = not read.
Standard library only (release_check.py and the builders both import it).
"""
import re

DESCR_MAX = 160
PUBLISH_POLICY = f"max={DESCR_MAX};unchecked=drop;wayback=drop;noindex=drop"      # what a published lookup file's meta must say

# Postgres, over a docs row aliased {d}: the capture may be used at all. NULL robots = not read yet: usable (a title, an
# address and index words), only its description waits for the check.
PG_CAPTURE_OK = ("(left({d}.crawl, 7) <> 'wayback' AND ({d}.robots IS NULL OR {d}.robots !~ '(^|,)(noindex|none)(,|$)'))")
# Postgres: the domains whose LATEST fetched capture may not be used. A statement that reads docs adds
# `AND domain NOT IN (PG_DOMAINS_OUT)`: an older capture never stands in for a newer one that said no.
PG_DOMAINS_OUT = ("SELECT l.domain FROM (SELECT DISTINCT ON (domain COLLATE \"C\") domain, crawl, robots FROM docs "
                  "WHERE error IS NULL ORDER BY domain COLLATE \"C\", fetched_at DESC NULLS LAST, crawl COLLATE \"C\") l "
                  "WHERE NOT " + PG_CAPTURE_OK.format(d="l"))
# The domains whose latest capture SAID noindex / none: what an inner page follows (audit 2026-10-22 G4). A homepage
# known only from Wayback says nothing about pages from Common Crawl or an open dump (stackexchange.com's homepage is a
# Wayback capture; its 330k questions come from Stack Exchange's own CC BY-SA dump): the Archive's terms bind what was
# read from the Archive, and only the site's own statement binds the rest.
PG_DOMAINS_NOINDEX = ("SELECT l.domain FROM (SELECT DISTINCT ON (domain COLLATE \"C\") domain, robots FROM docs "
                      "WHERE error IS NULL ORDER BY domain COLLATE \"C\", fetched_at DESC NULLS LAST, crawl COLLATE \"C\") l "
                      "WHERE l.robots ~ '(^|,)(noindex|none)(,|$)'")

_VALUED = ("max-snippet", "max-image-preview", "max-video-preview", "unavailable_after")
_KEPT = ("noindex", "none", "nosnippet", "noarchive", "noai", "noimageai", "tdm-reservation")


def robots_field(values) -> str:
    """The stored form of every robots statement a capture made: `<meta name=robots|googlebot|bingbot|...>` contents and
    X-Robots-Tag header values (a `botname:` prefix is dropped: a no said to one search engine is a no). Sorted, unique,
    comma-joined; '' when nothing restrictive was said. Only what a release acts on is kept."""
    out = set()
    for v in values:
        for tok in (v or "").lower().split(","):
            tok = tok.strip()
            left, _, right = tok.partition(":")
            if right and left.strip() not in _VALUED:        # "googlebot: noindex", "bingbot: max-snippet:0"
                tok = right.strip()
                left, _, right = tok.partition(":")
            left, right = left.strip(), right.strip()
            if left == "max-snippet":
                if re.fullmatch(r"-?\d+", right):
                    out.add(f"max-snippet:{int(right)}")
            elif not right and tok in _KEPT:
                out.add(tok)
    return ",".join(sorted(out))


def said_noindex(robots) -> bool:
    """The capture's own robots statements asked not to be listed (noindex / none)."""
    return bool(set((robots or "").split(",")) & {"noindex", "none"})


def capture_ok(crawl, robots) -> bool:
    if (crawl or "").startswith("wayback"):
        return False
    return not said_noindex(robots)


def cut(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    head = text[:n - 1]
    sp = head.rfind(" ")
    if sp >= n // 2:
        head = head[:sp]
    return head.rstrip(" ,;:-–—") + "…"


def shown_description(text, crawl, robots, unchecked: str = "drop"):
    """The description a release shows for this capture, or None."""
    if not text or not capture_ok(crawl, robots):
        return None
    if robots is None:
        return cut(text, DESCR_MAX) if unchecked == "keep" else None
    d = set(robots.split(","))
    if d & {"nosnippet", "tdm-reservation"}:
        return None
    limit = DESCR_MAX
    for x in d:
        if x.startswith("max-snippet:"):
            n = int(x.split(":", 1)[1])
            if n == 0:
                return None
            if n > 0:
                limit = min(limit, n)
    return cut(text, limit) or None
