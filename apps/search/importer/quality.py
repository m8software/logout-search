"""Junk homepages out of the index (2026-09-24).

Some fetched captures are not a site's real homepage: parked "this domain is for
sale" pages, "coming soon", server default pages, suspended accounts, error or
bot-check pages, empty shells, and non-English captures. Measured on 322,299
homepages: ~30.6k mostly non-Latin text, ~9.8k empty, ~1k server default, ~0.7k
error, ~0.4k parked, ~0.3k coming soon.

`search_import.py quality` classifies every parsed doc/page and marks junk as
`error = 'junk: <reason>'` (nothing is deleted; serving already skips error rows).
Popular sites (Tranco top POPULAR, or in the browser history) are kept even when
empty or in another script: the URL is still the right navigation target
(microsoft.com was captured in Japanese); only their foreign title is replaced.

A language version of a site ("thunderbird" led with "Thunderbird — Gelen Kutunu Özgür Bırak"):
when a site's "/" redirects to a language path, the scan keeps one of those paths, whichever is newest
(thunderbird.net/tr/, firefox.com/es-CL/, nodejs.org/tr/, playstation.com/pt-pt/; 4,666 homepages that day). The
script rule above cannot see Turkish or Spanish. language_version reads it from the page itself: the path is a
locale and the html lang names the same one. Such a row is marked junk when another crawl holds an English-first
capture of the domain (ccfetch then takes that one); otherwise its foreign title and description are cleared and
the domain is shown.
"""

from __future__ import annotations

import re
import time

POPULAR = 10_000

_PARKED = re.compile(r"(buy this domain|this domain (is|may be) for sale|domain is for sale|make an offer on this "
                     r"domain|hugedomains|sedoparking|dan\.com|afternic|parked free|domain parking)")
_PARKED_T = re.compile(r"(domain|this domain).*(for sale)|\b[a-z0-9-]+\.[a-z]{2,}\b.*\bis for sale")
_SOON = re.compile(r"(^|\s)(coming soon|under construction|website coming soon|launching soon)(\s|$|!|\.)")
_DEFAULT = re.compile(r"^(index of /|welcome to nginx|apache2 .*default page|it works!?$|iis windows server|"
                      r"test page for|default web site page$|welcome to your new site)")
_SUSPENDED = re.compile(r"(account suspended|website expired|domain expired|site suspended|this site is currently unavailable)")
_ERROR = re.compile(r"^((403|404|410|500|502|503)\b|forbidden$|not found$|access denied$|page not found|"
                    r"just a moment\.\.\.|attention required!)|(page not found|domain not found)$")


_LOCALE_ROOT = re.compile(r"^https?://[^/]+/([a-z]{2,3})(?:[-_]([a-z]{2,4}))?/?$", re.I)


# English as pages declare it: "en", and the country codes some sites write where a language belongs
# (e-gothiczone.com/gb/ declares lang="gb"). Not "uk": that is Ukrainian.
_ENGLISH_LANG = frozenset(("en", "gb", "us", "au", "nz"))


def language_version(url: str | None, lang: str | None) -> bool:
    """The capture is one language version of a site, not its front page: a one-segment locale path whose page
    declares that same non-English language or country (/tr/ + "tr", /ca-fr + "fr-CA", /cn/ + "zh-CN"). A path that
    only looks like a locale is left alone (lequipe.fr/tv/ declares "fr")."""
    parts = set((lang or "").strip().lower().replace("_", "-").split("-")) - {""}
    m = _LOCALE_ROOT.match(url or "")
    if not m or not parts or parts & _ENGLISH_LANG:
        return False
    return bool({x.lower() for x in m.groups() if x} & parts)


# an English-first capture of the same domain in a crawl not yet tried: what ccfetch.FETCH_SQL would take next
_ENGLISH_ELSEWHERE = ("EXISTS (SELECT 1 FROM cc_captures e WHERE e.domain = d.domain AND e.crawl <> d.crawl "
                      "AND e.languages LIKE 'eng%%' AND (e.host IN (e.domain, 'www.' || e.domain) OR e.kind = 'subroot') "
                      "AND NOT EXISTS (SELECT 1 FROM docs x WHERE x.domain = e.domain AND x.crawl = e.crawl))")


def ascii_share(text: str) -> float:
    return sum(1 for ch in text if ord(ch) < 128) / len(text) if text else 1.0


def classify(title: str | None, body: str | None, popular: bool) -> tuple[str | None, str | None]:
    """-> (junk reason or None, replacement title or None)."""
    t = (title or "").strip().lower()
    b = (body or "")[:2000]
    bl = b[:600].lower()
    if _PARKED_T.search(t) or _PARKED.search(bl):
        return "parked / for sale", None
    if _SOON.search(t):
        return "coming soon", None
    if _DEFAULT.search(t):
        return "server default page", None
    if _SUSPENDED.search(t):
        return "suspended / expired", None
    if _ERROR.search(t):
        return "error / bot-check page", None
    if not t and len(b.strip()) < 40:
        return (None, None) if popular else ("empty", None)
    if len(b) > 200 and ascii_share(b) < 0.6:
        return (None, "") if popular else ("non-English page", None)   # "" = show the domain instead
    return None, None


def cmd_quality(conn) -> None:
    t0 = time.time()
    counts: dict[str, int] = {}
    for table, key in (("docs", "d.domain, d.crawl"), ("page_docs", "d.url")):
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            continue
        home = table == "docs" and conn.execute("SELECT to_regclass('cc_captures')").fetchone()[0] is not None
        extra = f", d.url, d.lang, {_ENGLISH_ELSEWHERE}" if home else ""
        rows = conn.execute(
            f"SELECT {key}, d.title, left(d.body, 2000), "
            f"(coalesce(s.tranco_rank, 99999999) <= %s OR coalesce(s.history_visits, 0) > 0){extra} "
            f"FROM {table} d LEFT JOIN sites s ON s.domain = d.domain WHERE d.error IS NULL",
            (POPULAR,)).fetchall()
        junk, retitle, undescribe = [], [], []
        nk = 2 if table == "docs" else 1
        for r in rows:
            reason, new_title = classify(r[nk], r[nk + 1], r[nk + 2])
            if not reason and home and language_version(r[nk + 3], r[nk + 4]):
                if r[nk + 5]:
                    reason = "language version, English capture in another crawl"
                else:
                    undescribe.append(r[:nk])
                    counts["docs: language version, foreign title and description cleared"] = \
                        counts.get("docs: language version, foreign title and description cleared", 0) + 1
                    continue
            if reason:
                junk.append((*r[:nk], "junk: " + reason))
                counts[f"{table}: {reason}"] = counts.get(f"{table}: {reason}", 0) + 1
            elif new_title is not None:
                retitle.append(r[:nk])
                counts[f"{table}: foreign title cleared (popular)"] = counts.get(f"{table}: foreign title cleared (popular)", 0) + 1
        where = "domain = %s AND crawl = %s" if table == "docs" else "url = %s"
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(f"UPDATE {table} SET error = %s WHERE {where}", [(j[-1], *j[:-1]) for j in junk])
                cur.executemany(f"UPDATE {table} SET title = NULL WHERE {where}", retitle)
                cur.executemany(f"UPDATE {table} SET title = NULL, description = NULL WHERE {where}", undescribe)
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"[quality] {k}: {v:,}", flush=True)
    print(f"[quality] done in {(time.time() - t0) / 60:.1f} min", flush=True)
