"""The local news box's poller ("typing in iran would poll news rss feeds
for the last day or so … feeds could be kept local and refreshed every 1-30 minutes").

Reads the approved list (feeds.tsv), polls each feed on its own schedule with conditional
GETs, and keeps the last NEWS_KEEP_DAYS of items — headline, link, a short summary, time,
source — in the `news` database, which search-api reads. Article text is never stored.

Politeness, because this runs from a home connection:
  - one request at a time per host, NEWS_WORKERS hosts at once
  - If-None-Match / If-Modified-Since on every poll; a 304 costs the publisher nothing
  - each feed's own interval, stretched (up to 4x) while it has nothing new
  - 429/503 honour Retry-After (else double the wait); failures back off exponentially,
    and a feed that keeps failing is disabled with the reason logged — never hammered
  - news sitemaps (kind news-sitemap / sitemap-discover) are crawler files: robots.txt is
    checked first and a disallowed one is skipped
  - responses over NEWS_MAX_BYTES are cut off; XML is parsed with entity expansion off

    python poller.py            # run forever
    python poller.py --once     # one pass over every due feed, then exit (tests, first run)

The same loop keeps the ECB's daily euro reference rates (fx.py): one 2 KB file, looked at
four times a day, into the news file's fx_rates table (NEWS_SQLITE only; NEWS_FX=0 turns it off).

The user's own feeds (search.logout.cloud ⚙ -> "My news feeds"): NEWS_SETTINGS names the
engine's settings.json; its "feeds" ([{name, url}]) join the list as section "mine" and are
re-read every minute, so an edit takes effect without a restart. NEWS_FEEDS="" leaves the
approved feeds.tsv out entirely (customer engines: that list is personal-only until the
licensing review) — the user's own feeds are then the whole list. Same politeness rules.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import html
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

import fx  # noqa: E402  (the ECB's daily reference rates)
import newsdb  # noqa: E402  (Postgres or the NEWS_SQLITE file)

# An honest User-Agent: a box is one household's reader; the public site (ADR-062) says what it is
USER_AGENT = os.environ.get("NEWS_USER_AGENT") or "logout-news/1 (personal news reader; one household)"
# SEC EDGAR (2026-09-30): SEC asks every automated reader to name a contact in its User-Agent.
# One shared contact would make every box look like one requester, and it would be ours, not the
# owner's, so it is per box and opt-in: the owner's own contact (NEWS_SEC_CONTACT, or
# "sec_contact" in the engine's settings.json). Unset = no SEC feeds at all.
SEC_FEEDS = [
    {"section": "business", "name": "SEC EDGAR — latest 8-K filings", "lean": "-", "kind": "official", "poll": 5,
     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
            "&start=0&count=100&output=atom"},
]
_SEC_HOSTS = ("sec.gov", "www.sec.gov")
_FEEDS_ENV = os.environ.get("NEWS_FEEDS", str(Path(__file__).with_name("feeds.tsv")))
FEEDS_FILE = Path(_FEEDS_ENV) if _FEEDS_ENV else None          # "" = no approved list
# The bulk tier (2026-09-30, ADR-060): feeds found from Wikidata and agencies' own RSS pages and
# live-checked once, kept apart from the hand-approved list. Read after it (its URLs win) and
# only when that list is on: NEWS_FEEDS="" (customer engines) leaves both out.
_MORE_ENV = os.environ.get("NEWS_FEEDS_MORE", str(Path(__file__).with_name("feeds-more.tsv")))
MORE_FILE = Path(_MORE_ENV) if _MORE_ENV else None
SETTINGS_FILE = os.environ.get("NEWS_SETTINGS", "")            # the engine's settings.json
USER_POLL_MIN = 30                                             # the user's own feeds
# The user's own feeds (section "mine") are theirs to keep (owner 2026-11-13: "their own rss feeds should go back as
# far as they can"): never pruned, no age limit on what is stored, and on the first poll the older pages a feed
# offers are read too (RFC 5005 rel="next", WordPress ?paged=N) — at most BACKFILL_PAGES, BACKFILL_GAP_S apart.
BACKFILL_PAGES = int(os.environ.get("NEWS_BACKFILL_PAGES", "40"))
BACKFILL_GAP_S = float(os.environ.get("NEWS_BACKFILL_GAP_S", "2"))
# The address a person pastes is often the site, not its feed: the page's own <link rel="alternate"> names the feed;
# without one, the usual places are tried once, in this order.
FEED_GUESSES = ("feed", "rss", "feed.xml", "rss.xml", "atom.xml", "index.xml")
NO_FEED = "No feed found at this address"
SCHEMA = Path(__file__).with_name("schema.sql")
WORKERS = int(os.environ.get("NEWS_WORKERS", "4"))
TIMEOUT_S = 20
MAX_BYTES = int(os.environ.get("NEWS_MAX_BYTES", str(5 * 1024 * 1024)))
KEEP_DAYS = int(os.environ.get("NEWS_KEEP_DAYS", "7"))
TICK_S = 15
MIN_POLL_MIN = 2              # never more often than this, whatever the list says
MAX_STRETCH = 4.0             # an idle feed is polled at most 4x less often than listed
MAX_BACKOFF_MIN = 360
DISABLE_AFTER_4XX = 3         # 404/410/403 in a row
DISABLE_AFTER_FAILS = 12      # any failure in a row
SUMMARY_CHARS = 300
ROBOTS_TTL_S = 86400


def log(msg: str) -> None:
    print(f"[news] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
KINDS = {"outlet", "official", "aggregator", "newsletter", "bluesky", "mastodon", "news-sitemap", "sitemap-discover"}


_user_feeds_read = True      # did the last read_user_feeds read the file? False = sync_feeds keeps what it has


def read_user_feeds(path: str = SETTINGS_FILE) -> list[dict]:
    """The engine's settings.json "feeds" -> list entries (section "mine"). A missing or broken
    file is an empty list, never an error: the user may not have added any. It is also not proof that they
    removed their feeds (a mount that is away for a moment reads the same): _user_feeds_read says which."""
    import json
    global _user_feeds_read
    _user_feeds_read = True
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            feeds = json.load(f).get("feeds") or []
    except (OSError, ValueError, AttributeError):
        _user_feeds_read = False
        return []
    out = []
    for f in feeds[:100]:
        url = str((f or {}).get("url") or "") if isinstance(f, dict) else ""
        if re.match(r"^https?://[^\s/]+", url):
            name = str(f.get("name") or "").strip()[:60] or urllib.parse.urlsplit(url).hostname or url
            out.append({"section": "mine", "name": name, "lean": "-", "kind": "outlet", "poll": USER_POLL_MIN, "url": url})
    return out


def sec_contact(path: str | None = None) -> str:
    """The owner's contact for SEC ("Jane Doe jane@example.com"), or "" — one line, an email in it."""
    import json
    path = SETTINGS_FILE if path is None else path
    c = os.environ.get("NEWS_SEC_CONTACT", "")
    if not c and path:
        try:
            with open(path, encoding="utf-8") as f:
                c = str(json.load(f).get("sec_contact") or "")
        except (OSError, ValueError, AttributeError):
            c = ""
    c = " ".join(c.split())[:100]
    return c if re.search(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", c, re.I) else ""


def user_agent_for(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    c = sec_contact() if host in _SEC_HOSTS else ""
    return f"logout-news/1 ({c})" if c else USER_AGENT


def full_list() -> list[dict]:
    """The approved list (unless NEWS_FEEDS="") plus the user's own; first listing of a URL wins."""
    out, seen = [], set()
    more = read_list(MORE_FILE) if FEEDS_FILE and MORE_FILE and MORE_FILE.exists() else []
    sec = SEC_FEEDS if sec_contact() else []
    for f in (read_list() if FEEDS_FILE else []) + more + sec + read_user_feeds():
        if f["url"] not in seen:
            seen.add(f["url"])
            out.append(f)
    return out


def read_list(path: Path | None = None) -> list[dict]:
    """feeds.tsv -> [{section, name, lean, kind, poll, url}]; comments, blanks and malformed
    lines are skipped (and a malformed one is logged)."""
    path = path or FEEDS_FILE
    out, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) != 6 or p[3] not in KINDS or not p[4].isdigit() or not re.match(r"^https?://[^\s/]+", p[5]):
            log(f"{path.name} line {n} skipped (malformed): {line[:80]!r}")
            continue
        if p[5] in seen:
            continue
        seen.add(p[5])
        out.append({"section": p[0], "name": p[1], "lean": p[2], "kind": p[3],
                    "poll": max(MIN_POLL_MIN, int(p[4])), "url": p[5]})
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def plain(text: str | None, limit: int = SUMMARY_CHARS) -> str:
    """HTML fragment -> plain text, collapsed, cut at a word boundary."""
    t = _WS.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _when(struct) -> dt.datetime | None:
    if not struct:
        return None
    try:
        return dt.datetime(*struct[:6], tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_feed(body: bytes, kind: str) -> list[dict]:
    """RSS / Atom / RDF / JSON Feed -> items {guid, url, title, summary, author, published}.
    Social posts have no title: their text becomes the title (first 140 characters)."""
    import feedparser
    d = feedparser.parse(body)
    out = []
    for e in d.entries:
        url = (e.get("link") or "").strip()
        if not url.startswith("http"):
            continue
        text = plain(e.get("summary") or (e.get("content") or [{}])[0].get("value", ""))
        title = plain(e.get("title"), 200)
        if kind in ("bluesky", "mastodon") or not title:
            title = plain(text, 140)
        if not title:
            continue
        if title.startswith("8-K ") and "Item 2.02" in text:     # SEC: "Results of Operations" = earnings
            text = ("Earnings release (quarterly results). " + text)[:SUMMARY_CHARS]
        out.append({"guid": (e.get("id") or url)[:500], "url": url[:1000], "title": title,
                    "summary": "" if title == text else text,
                    "author": plain(e.get("author"), 120) or None,
                    "published": _when(e.get("published_parsed") or e.get("updated_parsed"))})
    return out


_NS_NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"
_NS_SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def parse_news_sitemap(body: bytes) -> tuple[list[dict], list[str]]:
    """A Google News sitemap -> (items, child sitemaps). An index file lists child sitemaps
    instead of articles; the caller polls the first news one."""
    from defusedxml import ElementTree as ET
    root = ET.fromstring(body)
    children = [el.text.strip() for el in root.iter(f"{_NS_SM}loc")
                if root.tag == f"{_NS_SM}sitemapindex" and el.text]
    items = []
    for u in root.iter(f"{_NS_SM}url"):
        loc = u.findtext(f"{_NS_SM}loc") or ""
        news = u.find(f"{_NS_NEWS}news")
        if not loc.startswith("http") or news is None:
            continue
        title = plain(news.findtext(f"{_NS_NEWS}title"), 200)
        if not title:
            continue
        items.append({"guid": loc[:500], "url": loc[:1000], "title": title, "summary": "",
                      "author": None, "published": _iso(news.findtext(f"{_NS_NEWS}publication_date"))})
    return items, children


def news_sitemaps_in_robots(text: str) -> list[str]:
    """robots.txt -> its Sitemap: lines that look like news sitemaps."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*sitemap:\s*(\S+)", line, re.I)
        # "news" in the PATH: apnews.com/sitemap.xml is its everything-sitemap
        if m and "news" in urllib.parse.urlsplit(m.group(1)).path.lower():
            out.append(m.group(1))
    return out


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def next_interval(base_min: float, idle_polls: int) -> float:
    """Minutes until the next poll: the listed interval, stretched 1.5x per idle poll after
    the second, at most MAX_STRETCH x."""
    stretch = min(MAX_STRETCH, 1.5 ** max(0, idle_polls - 2))
    return max(MIN_POLL_MIN, base_min * stretch)


def backoff(base_min: float, fails: int, retry_after_s: float | None = None) -> float:
    """Minutes to wait after a failure: Retry-After when the server gave one, else the listed
    interval doubled per failure in a row, capped."""
    if retry_after_s is not None:
        return min(MAX_BACKOFF_MIN, max(base_min, retry_after_s / 60))
    return min(MAX_BACKOFF_MIN, base_min * (2 ** max(0, fails)))


def retry_after(value: str | None) -> float | None:
    """Retry-After in seconds (delta or HTTP date)."""
    if not value:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def keep_item(it: dict, now: dt.datetime, keep_days: int | None = KEEP_DAYS) -> bool:
    """Too old, or dated in the future (clock or timezone bugs), is not news. keep_days None (the user's own
    feeds): no item is too old."""
    p = it.get("published")
    if p is None:
        return True
    return (keep_days is None or now - dt.timedelta(days=keep_days) <= p) and p <= now + dt.timedelta(hours=2)


# ---------------------------------------------------------------------------
# The user's own feeds: finding the feed from a site's address, and its older pages
# ---------------------------------------------------------------------------
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json")


def looks_like_html(body: bytes) -> bool:
    head = body[:2000].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<head" in head


def feed_links(body: bytes, base: str) -> list[str]:
    """The feeds a page names in <link rel="alternate" type="application/rss+xml" href=…>, absolute, in page
    order. Comment feeds are left out when another exists."""
    out = []
    for tag in _LINK_TAG.findall(body[:400_000].decode("utf-8", "replace")):
        a = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "") for m in _ATTR.finditer(tag)}
        if "alternate" in a.get("rel", "").lower().split() and a.get("type", "").lower().strip() in _FEED_TYPES \
                and a.get("href"):
            u = urllib.parse.urljoin(base, a["href"].replace("&amp;", "&").strip())
            if u.startswith("http") and u not in out:
                out.append(u)
    main = [u for u in out if "comment" not in u.lower()]
    return main or out


def feed_guesses(url: str) -> list[str]:
    """Where a site's feed usually is: under the pasted address, then under the site's root."""
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}/"
    here = root + p.path.strip("/") + "/" if p.path.strip("/") else root
    out = []
    for base in (here, root):
        for g in FEED_GUESSES:
            if base + g not in out:
                out.append(base + g)
    return out


def find_feed(url: str, body: bytes, get=None) -> str | None:
    """The feed behind a site address whose own body is a web page: the first feed the page names, else the
    first usual place that answers with entries. None: there is none."""
    get = get or fetch
    for u in feed_links(body, url)[:3] + feed_guesses(url):
        if u == url:
            continue
        f = get(u)
        if f.status == 200 and not looks_like_html(f.body) and parse_feed(f.body, "outlet"):
            return u
    return None


def next_page(body: bytes, url: str, page: int) -> str | None:
    """The address of a feed's next older page: the one it names (RFC 5005 rel="next"), else WordPress's
    ?paged=N for a feed WordPress wrote. None: the feed offers no older page."""
    import feedparser
    d = feedparser.parse(body)
    for l in d.feed.get("links") or []:
        if l.get("rel") == "next" and str(l.get("href") or "").startswith("http"):
            return l["href"]
    nxt = (d.feed.get("next_url") if hasattr(d.feed, "get") else None)      # JSON Feed
    if isinstance(nxt, str) and nxt.startswith("http"):
        return nxt
    if "wordpress" in str(d.feed.get("generator") or "").lower():
        p = urllib.parse.urlsplit(url)
        qs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != "paged"] + [("paged", str(page + 1))]
        return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(qs)))
    return None


def backfill(conn, feed: dict, target: str, body: bytes, get=None, gap_s: float | None = None) -> int:
    """Read the older pages of one of the user's feeds, once (its first good poll). Stops at the first page
    that fails, repeats, or holds nothing new. Returns how many older items were stored."""
    get, gap = get or fetch, BACKFILL_GAP_S if gap_s is None else gap_s
    total, seen, url = 0, {target}, target
    for page in range(1, BACKFILL_PAGES + 1):
        url = next_page(body, url, page)
        if not url or url in seen:
            break
        seen.add(url)
        time.sleep(gap)
        f = get(url)
        if f.status != 200:
            break
        body = f.body
        new = store(conn, feed["url"], parse_feed(body, feed["kind"]), keep_days=None)
        if not new:
            break
        total += new
    if total:
        conn.execute("UPDATE feeds SET items_total = items_total + %s WHERE url = %s", (total, feed["url"]))
        log(f"{feed['name']}: {total} older items from {len(seen) - 1} more pages")
    return total


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
class Fetched:
    def __init__(self, status: int, body: bytes = b"", etag: str | None = None,
                 modified: str | None = None, retry: float | None = None):
        self.status, self.body, self.etag, self.modified, self.retry = status, body, etag, modified, retry


def fetch(url: str, etag: str | None = None, modified: str | None = None) -> Fetched:
    headers = {"User-Agent": user_agent_for(url), "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/feed+json, */*;q=0.5",
               "Accept-Encoding": "identity"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return Fetched(413)
            return Fetched(r.status, body, r.headers.get("ETag"), r.headers.get("Last-Modified"))
    except urllib.error.HTTPError as e:
        return Fetched(e.code, retry=retry_after(e.headers.get("Retry-After") if e.headers else None))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return Fetched(0)


_robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser, str]] = {}
_robots_lock = threading.Lock()


def robots_for(url: str) -> tuple[urllib.robotparser.RobotFileParser, str]:
    """The host's robots.txt, fetched at most once a day. Unreachable = allow (the RFC 9309
    rule for a missing file); a 401/403 on robots.txt itself = disallow."""
    host = urllib.parse.urlsplit(url)
    key = f"{host.scheme}://{host.netloc}"
    with _robots_lock:
        hit = _robots.get(key)
        if hit and time.time() - hit[0] < ROBOTS_TTL_S:
            return hit[1], hit[2]
    f = fetch(key + "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    text = f.body.decode("utf-8", "replace") if f.status == 200 else ""
    if f.status in (401, 403):
        rp.parse(["User-agent: *", "Disallow: /"])
    else:
        rp.parse(text.splitlines())
    with _robots_lock:
        _robots[key] = (time.time(), rp, text)
    return rp, text


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------
def connect():
    return newsdb.connect()


def ensure_database() -> None:
    """A user's engine starts from a snapshot that has no news database: create it (PGDATABASE).
    With NEWS_SQLITE: the file and its tables."""
    if newsdb.is_sqlite():
        newsdb.ensure_sqlite_schema()
        return
    import psycopg
    name = os.environ.get("PGDATABASE", "news")
    try:
        psycopg.connect(autocommit=True, connect_timeout=10).close()
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
        try:                                 # news + sports start together: one of them wins
            with psycopg.connect(dbname="postgres", autocommit=True) as c:
                c.execute(f'CREATE DATABASE "{name}"')
            log(f"created database {name}")
        except (psycopg.errors.DuplicateDatabase, psycopg.errors.UniqueViolation):
            pass


def sync_feeds(conn, feeds: list[dict]) -> None:
    """The list is the truth: new lines are added (due now), changed ones updated, removed
    ones deleted with their items."""
    urls = {f["url"] for f in feeds}
    due_now = newsdb.ts(newsdb.now())
    for f in feeds:
        conn.execute("""INSERT INTO feeds (url, section, name, lean, kind, poll_min, next_due)
                        VALUES (%(url)s, %(section)s, %(name)s, %(lean)s, %(kind)s, %(poll)s, %(due)s)
                        ON CONFLICT (url) DO UPDATE SET section = EXCLUDED.section, name = EXCLUDED.name,
                            lean = EXCLUDED.lean, kind = EXCLUDED.kind, poll_min = EXCLUDED.poll_min""",
                     dict(f, due=due_now))
    # The user's own feeds hold items that are kept for good: they go only when the settings file was read and
    # no longer lists them, never because the file could not be read this once (2026-11-13).
    keep_mine = bool(SETTINGS_FILE) and not _user_feeds_read
    for url, section in conn.execute("SELECT url, section FROM feeds").fetchall():
        if url not in urls and not (keep_mine and section == "mine"):
            conn.execute("DELETE FROM feeds WHERE url = %s", (url,))


def store(conn, feed_url: str, items: list[dict], keep_days: int | None = KEEP_DAYS) -> int:
    """Insert new items; returns how many were new."""
    now = dt.datetime.now(dt.timezone.utc)
    new = 0
    for it in items:
        if not keep_item(it, now, keep_days):
            continue
        cur = conn.execute("""INSERT INTO items (feed_url, guid, url, title, summary, author, published)
                              VALUES (%s, %s, %s, %s, %s, %s, %s)
                              ON CONFLICT (feed_url, guid) DO NOTHING""",
                           (feed_url, it["guid"], it["url"], it["title"], it["summary"], it["author"],
                            newsdb.ts(it["published"] or now)))
        new += cur.rowcount
    return new


def poll_one(feed: dict) -> None:
    """One poll of one feed, with its outcome written back to feeds."""
    url, kind = feed["url"], feed["kind"]
    target = feed.get("resolved_url") or url
    with connect() as conn:
        try:
            if kind in ("news-sitemap", "sitemap-discover"):
                rp, text = robots_for(target)
                if kind == "sitemap-discover" and not feed.get("resolved_url"):
                    found = news_sitemaps_in_robots(text)
                    if not found:
                        return _disable(conn, feed, "robots.txt lists no news sitemap")
                    target = found[0]
                    conn.execute("UPDATE feeds SET resolved_url = %s WHERE url = %s", (target, url))
                if not rp.can_fetch(USER_AGENT, target):
                    return _disable(conn, feed, f"robots.txt disallows {target}")
            f = fetch(target, feed.get("etag"), feed.get("modified"))
            if f.status == 304:
                return _ok(conn, feed, 0, None, None)
            if f.status == 200:
                if kind in ("news-sitemap", "sitemap-discover"):
                    items, children = parse_news_sitemap(f.body)
                    if not items and children:
                        conn.execute("UPDATE feeds SET resolved_url = %s WHERE url = %s", (children[0], url))
                        return _ok(conn, feed, 0, None, None, soon=True)
                else:
                    items = parse_feed(f.body, kind)
                mine = feed.get("section") == "mine"
                if mine and not items and looks_like_html(f.body):
                    # the person pasted the site, not its feed (2026-11-13): find it, poll it next tick
                    # ... but a feed that has given items and now answers with a page (maintenance, a login wall)
                    # is a failure to retry, never the end of it
                    if feed.get("items_total") or feed.get("resolved_url"):
                        return _fail(conn, feed, 200, None, "answered with a web page, not a feed")
                    found = find_feed(target, f.body)
                    if not found:
                        return _disable(conn, feed, NO_FEED)
                    conn.execute("UPDATE feeds SET resolved_url = %s WHERE url = %s", (found, url))
                    return _ok(conn, feed, 0, None, None, soon=True)
                new = store(conn, url, items, None if mine else KEEP_DAYS)
                first = mine and not feed.get("items_total")      # nothing stored yet: its first items
                out = _ok(conn, feed, new, f.etag, f.modified, parsed=len(items))
                if first and items:
                    backfill(conn, feed, target, f.body)
                return out
            return _fail(conn, feed, f.status, f.retry)
        except Exception as e:                        # a broken feed must never stop the poller
            return _fail(conn, feed, -1, None, f"{type(e).__name__}: {e}"[:200])


def _ok(conn, feed, new, etag, modified, parsed=None, soon=False):
    idle = 0 if new else feed.get("idle_polls", 0) + 1
    mins = 0.5 if soon else next_interval(feed["poll_min"], idle)
    t = newsdb.now()
    conn.execute("""UPDATE feeds SET last_status = 200, last_ok = %s, fails = 0, fails_4xx = 0,
                        idle_polls = %s, next_due = %s,
                        etag = coalesce(%s, etag), modified = coalesce(%s, modified),
                        items_total = items_total + %s, last_error = NULL
                    WHERE url = %s""", (newsdb.ts(t), idle, newsdb.ts(t + dt.timedelta(minutes=mins)), etag, modified,
                                        new, feed["url"]))
    if new or parsed == 0:
        log(f"{feed['name']}: {new} new" + (f" of {parsed}" if parsed is not None else ""))


def _fail(conn, feed, status, retry, err=None):
    fails = feed.get("fails", 0) + 1
    f4 = feed.get("fails_4xx", 0) + 1 if status in (401, 403, 404, 410) else 0
    reason = err or (f"HTTP {status}" if status else "unreachable")
    keeps = feed.get("section") == "mine" and feed.get("items_total")    # the user's feed with items: retried for good
    if (f4 >= DISABLE_AFTER_4XX or fails >= DISABLE_AFTER_FAILS) and not keeps:
        return _disable(conn, feed, f"{reason} ({fails} failures in a row)")
    mins = backoff(feed["poll_min"], fails, retry)
    conn.execute("""UPDATE feeds SET last_status = %s, fails = %s, fails_4xx = %s, last_error = %s,
                        next_due = %s WHERE url = %s""",
                 (status, fails, f4, reason, newsdb.ts(newsdb.now() + dt.timedelta(minutes=mins)), feed["url"]))
    log(f"{feed['name']}: {reason}; next try in {mins:.0f} min")


def _disable(conn, feed, reason):
    conn.execute("UPDATE feeds SET disabled = true, last_error = %s WHERE url = %s", (reason, feed["url"]))
    log(f"{feed['name']}: DISABLED — {reason}")


FEED_COLS = ("url", "section", "name", "lean", "kind", "poll_min", "etag", "modified", "fails",
             "fails_4xx", "idle_polls", "resolved_url", "items_total")


def due(conn) -> list[dict]:
    rows = conn.execute(f"SELECT {', '.join(FEED_COLS)} FROM feeds WHERE NOT disabled AND next_due <= %s "
                        "ORDER BY next_due", (newsdb.ts(newsdb.now()),)).fetchall()
    return [dict(zip(FEED_COLS, r)) for r in rows]


def one_per_host(feeds: list[dict]) -> list[list[dict]]:
    """Group due feeds by host: each group is polled in order by one worker, so a host never
    sees two requests at once."""
    groups: dict[str, list[dict]] = {}
    for f in feeds:
        groups.setdefault(urllib.parse.urlsplit(f.get("resolved_url") or f["url"]).netloc, []).append(f)
    return list(groups.values())


def settings_mtime() -> float:
    try:
        return os.stat(SETTINGS_FILE).st_mtime if SETTINGS_FILE else 0.0
    except OSError:
        return 0.0


def run(once: bool = False) -> None:
    ensure_database()
    with connect() as conn:
        if not newsdb.is_sqlite():                      # ensure_database made the SQLite tables
            conn.execute(SCHEMA.read_text())
        feeds = full_list()
        sync_feeds(conn, feeds)
        log(f"{len(feeds)} feeds in the list ({sum(1 for f in feeds if f['section'] == 'mine')} of the user's own)")
    seen_mtime, last_check = settings_mtime(), time.time()
    last_prune = 0.0
    fx_due = None                                       # fx.step reads the last fetch from the news file
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while True:
            if time.time() - last_check > 60:           # the user edited their feeds in ⚙
                last_check = time.time()
                m = settings_mtime()
                if m != seen_mtime:
                    seen_mtime = m
                    with connect() as conn:
                        feeds = full_list()
                        sync_feeds(conn, feeds)
                    log(f"feed list changed: {len(feeds)} feeds")
            with connect() as conn:
                groups = one_per_host(due(conn))
                if time.time() - last_prune > 3600:
                    # the user's own feeds are never pruned (2026-11-13)
                    n = conn.execute("DELETE FROM items WHERE published < %s AND feed_url NOT IN "
                                     "(SELECT url FROM feeds WHERE section = 'mine')",
                                     (newsdb.ts(newsdb.now() - dt.timedelta(days=KEEP_DAYS)),)).rowcount
                    if n:
                        log(f"pruned {n} items older than {KEEP_DAYS} days")
                    last_prune = time.time()
            list(pool.map(lambda g: [poll_one(f) for f in g], groups))
            fx_due = fx.step(fx_due)                    # at most one small request every 6 h; never raises
            if once:
                return
            time.sleep(TICK_S)


if __name__ == "__main__":
    run(once="--once" in sys.argv)
