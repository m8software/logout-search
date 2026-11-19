"""stage_card: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from card_text import card_image
from card_text import clean_lead
from card_text import cut_lead
from card_text import strip_wiki_markup
from stage_parts import PartStale
from stage_parts import _card_live
import card_text
from stage_parts import _parts_off
from stage_parts import _pcheck
import base64
import json
from stage_parts import note_part
import os
from urllib.parse import quote
import re
import threading
import start_trace
import unicodedata
from urllib.parse import urlsplit
from search_api import (
    ACCOUNT_LABEL,
    FAMOUS_LEAD_SQL,
    PROFILES_SQL,
    PROFILE_PLATFORMS,
    RELATED_N,
    RELATED_SQL,
    WIKI_EXACT_SCORE,
    _TOKEN,
    _conn,
    _domain_of,
    _host_key,
    _label,
    _plain_name,
    _warm,
    account_fits,
    cached,
    lookups,
    norm_key,
    official_inner_page,
    official_link_ok,
    official_ok,
    parts_for,
    profile_results,
    related_results,
    same_page,
    search,
    tables,
    url_host_ok,
)


# The platform's own home page, for the card whose article IS the platform (platform_home; owner 2026-11-10: "YouTube
# card should have YouTube.com be the official link, same across all the sites that get a social media spot").
# Hand list, one line per platform, keyed on the Wikipedia article title (lower case): the Wikidata website (P856) is
# in wiki_sites but official_ok drops every _SOCIAL_HOSTS host, and a profile-path row (instagram.com/instagram) is
# not a home page. The PROFILE_SOCIAL platforms plus the other big ones.
PLATFORM_HOME = {
    "youtube": "https://www.youtube.com/", "facebook": "https://www.facebook.com/", "instagram": "https://www.instagram.com/",
    "twitter": "https://x.com/", "x (social network)": "https://x.com/", "tiktok": "https://www.tiktok.com/",
    "twitch": "https://www.twitch.tv/", "soundcloud": "https://soundcloud.com/", "bluesky": "https://bsky.app/",
    "github": "https://github.com/", "reddit": "https://www.reddit.com/", "reddit (website)": "https://www.reddit.com/", "linkedin": "https://www.linkedin.com/",
    "pinterest": "https://www.pinterest.com/", "snapchat": "https://www.snapchat.com/", "whatsapp": "https://www.whatsapp.com/",
    "telegram (software)": "https://telegram.org/", "threads (social network)": "https://www.threads.com/",
    "tumblr": "https://www.tumblr.com/", "discord": "https://discord.com/", "mastodon (social network)": "https://joinmastodon.org/",
    "mastodon (software)": "https://joinmastodon.org/", "telegram": "https://telegram.org/",
    "threads": "https://www.threads.com/", "mastodon": "https://joinmastodon.org/",
}

# The card over an exact answer (start page, 2026-09-25): what the thing is, from its
# Wikidata description (suggest_items.descr), its lead, and its accounts and related
# articles as links — built from the results already found, one lookup more.
CARD_DESCR_SQL = "SELECT descr FROM suggest_items WHERE wiki = %(t)s AND descr IS NOT NULL ORDER BY demand DESC NULLS LAST LIMIT 1"

# Its own stored official site (wiki_sites, the table WIKI_EXACT_SITE_SQL joins), for the card whose result list holds no
# official row of it ("claude": claude.ai is a plain site row and the exact-title path found another article).
CARD_SITE_SQL = ("SELECT ws.url FROM wiki_sites ws JOIN wiki_docs w USING (page_id) "
                 "WHERE w.title = %(t)s AND ws.url IS NOT NULL LIMIT 1")

# Its picture (2026-09-29, importer/card_images.py): free-licensed Commons files only, stored
# as small WebP and sent inline, with the credit the licence asks for.
CARD_IMAGE_SQL = "SELECT img, w, h, author, licence, licence_url, file FROM card_images WHERE title = %(t)s"

CARD_WITHIN = 3

def card_article(res: list[dict]) -> dict | None:
    """The exact-answer article the card is about: the first one in the top CARD_WITHIN,
    not counting the top site's sisters: on other endings (starbucks.com.cn, starbucks.co.jp
    pushed Starbucks' article to 4th, 2026-09-25) and under its own name (plus.cnn.com and
    cnnespanol.cnn.com pushed CNN's to 4th: "cnn" had no card, 2026-10-10)."""
    top = _label(res[0]["url"]) if res and res[0].get("source") == "site" else None
    under = "." + _domain_of(res[0]["url"]) if top else None
    sister = lambda r: r.get("source") == "site" and (_label(r["url"]) == top or _domain_of(r["url"]).endswith(under))  # noqa: E731
    # nearby branches are the Nearby box, not rows: "Ikea" with an area put three IKEA stores between the official site
    # and the article, which then sat 5th and the card was lost (2026-11-06)
    window = [r for i, r in enumerate(res) if not (i and top and sister(r)) and r.get("source") != "place" and not r.get("stock")]
    return next((r for r in window[:CARD_WITHIN] if r.get("source") == "wikipedia"
                 and (r["score"] >= WIKI_EXACT_SCORE or r.get("respelled"))), None)

# A misspelled name gets its card (2026-11-06, owner: "on misspellings we should be hitting the correct card"):
# "sarah j mass" is not respelled (every word is a word), so no exact article answers it, yet the list leads with
# "Sarah J. Maas — Wikipedia" and sarahjmaas.com. The article counts as the card's when its title is the query
# word for word with each word at most one letter off (a swap counts once), and the name's own site (its words
# joined, "sarahjmaas") is among the first six rows (the engine's order: sarahjmaas.com sat 4th). "mass" alone is
# one word against three: never.
_RW = re.compile(r"[a-z0-9]+")

def _name_words(s: str) -> list[str]:
    return _RW.findall(unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())

def _one_off(a: str, b: str) -> bool:
    """a and b equal, or one edit apart (a letter changed, added, dropped, or two neighbours swapped)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 3:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))

def respells_name(q: str, title: str) -> bool:
    """title (its "(…)" dropped) is q word for word, each word at most one edit off, at least one differing."""
    qw, tw = _name_words(q), _name_words(re.sub(r"\s*\([^)]*\)\s*$", "", title))
    return len(qw) >= 2 and len(qw) == len(tw) and qw != tw and all(_one_off(a, b) for a, b in zip(qw, tw))

def mark_respelled(q: str, res: list[dict]) -> None:
    """Marks the first or second row as the card's article when it respells q and the name's own site is in the
    first six rows (card_article then takes it)."""
    for i, r in enumerate(res[:2]):
        if r.get("source") != "wikipedia" or r["score"] >= WIKI_EXACT_SCORE:
            continue
        title = r["title"].rsplit(" — Wikipedia", 1)[0]
        if not respells_name(q, title):
            continue
        joined = "".join(_name_words(re.sub(r"\s*\([^)]*\)\s*$", "", title)))
        if any(x.get("source") == "site" and _label(x["url"]) == joined for x in res[:6]):
            res[i] = dict(r, respelled=True)            # a new row in the caller's list: the dict itself may be shared (CORE-A-04)
        return

# The site tree under the card's official site (2026-11-05, owner: "a site tree type of thing that has links to where
# important parts of the site might be useful"): a few of the site's own sections (Tickets, Shop, Menu, Locations,
# News, Support, Contact ...), picked from its home page's navigation by tools/site-nav.py at build time. Nothing is
# fetched at search time. SEARCH_SITENAV: that tool's sitenav.jsonl; unset or missing, cards have no tree.
SITENAV_PATH = os.environ.get("SEARCH_SITENAV", "").strip()

SITENAV_MAX = 6

_sitenav: dict[str, list[dict]] | None = None

_sitenav_lock = start_trace.lock("sitenav")

def sitenav() -> dict[str, list[dict]]:
    """{site host (no www.): [{title, url}, ...]}, read once."""
    global _sitenav
    if _sitenav is None:
        with _sitenav_lock:
            if _sitenav is None:
                got: dict[str, list[dict]] = {}
                try:
                    bad = 0
                    with open(SITENAV_PATH, encoding="utf-8") as f:
                        for line in f:
                            try:                                      # one bad line skips that line, not the rest (CORE-A-10)
                                r = json.loads(line)
                                links = [{"title": x["text"], "url": x["url"], **({"section": x["section"]} if x.get("section") else {})}
                                         for x in r.get("links") or []
                                         if x.get("text") and str(x.get("url", "")).startswith("https://")]
                                if links:
                                    got[r["site"]] = links[:SITENAV_MAX]
                            except (ValueError, KeyError, TypeError, AttributeError):
                                bad += 1
                    if bad:
                        print(f"site tree {SITENAV_PATH}: {bad} bad lines skipped", flush=True)
                except OSError as e:
                    if SITENAV_PATH:
                        print(f"site tree {SITENAV_PATH}: not read ({type(e).__name__})", flush=True)
                _sitenav = got
                if got:
                    print(f"site tree {SITENAV_PATH}: {len(got)} sites", flush=True)
    return _sitenav

# Every link Wikidata records for a topic (2026-11-06, owner: "If it's legal and ok license wise to use the stuff from
# Wikidata we should be using all of it"; Wikidata is CC0, and these are links only). tools/card-links.py builds
# SEARCH_CARDLINKS (sqlite: links(title, p, url), fields(p, label, rank)); read per card, by its English Wikipedia
# title. Accounts and fan communities join the card's marks; the rest go under "More links".
CARDLINKS_PATH = os.environ.get("SEARCH_CARDLINKS", "").strip()

CARDLINKS_MAIN = frozenset(("Reddit", "Fandom", "X", "Instagram", "Facebook", "YouTube", "TikTok", "Threads", "Bluesky",
                            "Mastodon", "LinkedIn", "GitHub"))

CARDLINKS_MORE = 16

# a film or music catalogue only for what it catalogues (account_fits): its label as one of those platforms
_CARDLINK_KIND = {**{k: "imdb" for k in ("IMDb", "Letterboxd", "TMDB", "Trakt", "Rotten Tomatoes", "Metacritic", "TheTVDB",
                                         "LUMIERE", "MovieLens", "FilmAffinity")},
                  **{k: "discogs" for k in ("MusicBrainz", "MusicBrainz release group", "Discogs", "AllMusic", "Songkick",
                                            "Genius", "Spotify", "Apple Music", "SoundCloud", "Last.fm", "Bandcamp", "Deezer")}}

_cardlinks_db = threading.local()

def card_links(title: str) -> list[tuple[str, str]]:
    """[(label, url)] for the topic, in the fields' order (most used first); [] without the file."""
    if not CARDLINKS_PATH or not title:
        return []
    import sqlite3
    db = getattr(_cardlinks_db, "db", None)
    if db is None:
        try:
            db = sqlite3.connect(f"file:{CARDLINKS_PATH}?mode=ro", uri=True, check_same_thread=False)
            _cardlinks_db.db = db
        except sqlite3.Error:
            return []
    try:
        # several accounts of one kind (X: GoogleColombia, Google, GoogleArabia...): the card shows the first, so the
        # shortest address leads, which is the brand's own handle (2026-11-15: "google" showed x.com/GoogleColombia)
        return db.execute("SELECT f.label, l.url FROM links l JOIN fields f ON f.p = l.p WHERE l.title = ? "
                          "ORDER BY f.rank, length(l.url), l.url", (title,)).fetchall()
    except sqlite3.Error:
        return []

def site_tree(url: str) -> list[dict]:
    """The section links of the site at url (its own host, or its www./bare twin), [] if none."""
    return sitenav().get(_host_key(url), []) if SITENAV_PATH else []

_PLATFORM_IS = re.compile(r"\b(social (media|network)|video[- ]sharing|video platform|messaging|instant messag|microblog|"
                          r"streaming (service|platform)|online (platform|service|community)|photo[- ]sharing|"
                          r"image[- ]sharing|code hosting|hosting service|social news|networking (service|site|platform)|"
                          r"voip|chat (app|platform)|mobile app)", re.I)

_PLATFORM_NOT = re.compile(r"\b(film|novel|album|band|song|newspaper|magazine|letter|concept|"
                           r"television|tv series|play|poem|disambiguation)\b", re.I)

def platform_home(title: str, descr: str | None, lead: str | None) -> list[dict]:
    """The card's official site when the article IS a social platform (PLATFORM_HOME): the article title is on the
    list, and its Wikidata description or lead's first 200 characters call it a platform and not a film, a newspaper,
    a letter. The card carries no Wikidata id, so the title and this wording stand in for it."""
    url = PLATFORM_HOME.get((title or "").strip().lower())
    if not url:
        return []
    for text in (descr, (lead or "")[:200]):
        if text and _PLATFORM_IS.search(text) and not _PLATFORM_NOT.search(text):
            return [{"title": "Official site", "url": url}]
    return []

DESCR_MAX = 120

DESCR_STORED_CUT = 60                                    # the release keeps a card's description to this many characters

def card_description(descr: str | None, what: str | None) -> str:
    """The card's one line: the stored description, or the whole one an account row carries when the stored one is its
    start (the release cuts it at 60: Google's read "American multinational technology company, a subsidiary of A",
    owner 2026-11-15), cut at a word with "…" past DESCR_MAX or when only a cut one is known."""
    d = (descr or what or "").strip()
    if descr and what and len(what) > len(descr) and what.startswith(descr.rstrip()):
        d = what.strip()
    if not (len(d) > DESCR_MAX or (d == (descr or "").strip() and len(d) == DESCR_STORED_CUT)):
        return d
    head = d[:DESCR_MAX]
    head = head.rsplit(" ", 1)[0] if " " in head else head     # its last word may be cut: it goes
    return head.rstrip(" ,;:") + "…"

def whole_description(lk, title: str, descr: str | None) -> str | None:
    """The release keeps a card's description to DESCR_STORED_CUT characters; its accounts carry the whole Wikidata
    one ("google": "..., a subsidiary of A" -> "..., a subsidiary of Alphabet Inc."). The stored one when none does."""
    if not descr or len(descr) < DESCR_STORED_CUT:
        return descr
    try:
        rows = lk.h[PROFILES_SQL]({"t": title, "p": PROFILE_PLATFORMS})
    except Exception:  # noqa: BLE001 - the stored one is still a description
        return descr
    return next((r[3] for r in rows if r[3] and len(r[3]) > len(descr) and r[3].startswith(descr.rstrip())), descr)

def with_own_accounts(rows: list[dict], top: dict, fetch) -> list[dict]:
    """The rows the card is built from, with the article's own accounts when the list carries none of them ("google":
    its list had no account rows, and the card's X and Facebook came from the card-links file, which holds
    x.com/GoogleColombia and facebook.com/GoogleArabia; the accounts table has x.com/Google). Only for the card."""
    key = same_page(top["url"])
    if any(r.get("profile") and r.get("of") == key for r in rows):
        return rows
    try:
        return rows + profile_results(fetch(), WIKI_EXACT_SCORE, top["url"])
    except Exception:  # noqa: BLE001 - the card is built without them, as before
        return rows

def own_name_site(title: str, official: list[dict], res: list[dict]) -> list[dict]:
    """The card's official site is the subject's own-name domain when the stored one is a page on someone else's site
    (owner 2026-11-14, "gmail": "the official site should just be gmail.com"; Wikidata's is
    workspace.google.com/products/gmail/, whose site tree then gave the card Calendar, Blog, Pricing). Only then: a
    stored site on another domain that is a page, not a front page, and a site row among the first five that is the
    title's letters at .com (gmail.com for Gmail; iphone.cz is not the iPhone's). A stored front page, or one on the own-name domain, stays.
    The stored site is kept as the next link, named by its host."""
    official = [l for l in official if url_host_ok(l.get("url") or "")]
    key = norm_key(title)
    if not official or len(key) < 3 or not _plain_name(title):
        return official
    url = official[0]["url"]
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if _label(url) == key:
        return official
    # a front page stays, unless the name is one of its host's labels but not the first ("about.google" for Google,
    # owner 2026-11-15: "google" showed about.google and its Products / Locations / Contact us, while google.com led
    # the list): the brand's own .com is the official site then
    if urlsplit(url).path.strip("/") == "" and key not in host.split(".")[1:]:
        return official
    # CORE-B-08: the stored page must be the *product page of this name on a company's site* (workspace.google.com/products/gmail/),
    # not a government, university or non-profit page: science.nasa.gov/mars/ is Mars's site, mars.com is the candy maker
    # (a front page whose host carries the name, about.google for Google, has no path to hold it: only a page is held to it)
    path = urlsplit(url).path.strip("/")
    if host.rsplit(".", 1)[-1] in ("gov", "edu", "mil", "int", "org") or (path and key not in re.sub(r"[^a-z0-9]", "", path.lower())):
        return official
    own = next((r for r in res[:5] if r.get("source") == "site" and _domain_of(r.get("url") or "") == key + ".com"
                and url_host_ok(r.get("url") or "")), None)                     # gmail.com; not iphone.cz for the iPhone
    if not own:
        return official
    # the stored site stays as a second link, under its own name
    return [{"title": "Official site", "url": own["url"]}, {"title": host, "url": url}] + official[1:]

# A bare short name that is the everyday name of a big maker's product (owner 2026-11-14: "outlook" had no card, "keep"
# carried the KEEP radio station, "premiere" the dictionary word, "teams" the article Team): "<Maker> <name>" or
# "<Maker> <name> Pro" is an article, and it is read far more than the article the card would show (Wikipedia
# pageviews, the release's signal; SHORT_RATIO times, and at least SHORT_FLOOR). Then it is the card. "music" stays
# Music (63,689 against Apple Music's 10,495); "word" was already Microsoft Word. Release lookups only (pageviews).
SHORT_MAKERS = ("Microsoft", "Google", "Apple", "Amazon", "Adobe", "Nintendo", "Sony", "Samsung", "Meta", "Facebook", "Mozilla")

SHORT_SUFFIXES = ("", " Pro")

SHORT_QUALIFIERS = ("software", "virtual assistant", "web browser", "presentation software", "service")

SHORT_RATIO = float(os.environ.get("SEARCH_SHORT_RATIO", "3"))

SHORT_FLOOR = int(os.environ.get("SEARCH_SHORT_FLOOR", "1000"))

SHORT_NAMES = os.environ.get("SEARCH_SHORT_NAMES", "1") == "1"

def _title_views(lk, title: str):
    from lookups_release import N_TITLE
    ds = lk.names1(title, N_TITLE)[:1]
    return (lk.S(ds[0], "pageviews") or 0) if ds else None

def plain_title_first(q: str, top: dict | None, rows: list[dict]) -> dict | None:
    """The article titled exactly the search wins the card over another exact one read far less (owner 2026-11-15,
    "google": the list ranked Google+ — Wikipedia (23,573 views) above Google (1,423,778), and the card was the dead
    social network's). Only among the exact articles the list already has, and only SHORT_RATIO times more read."""
    lk = lookups()
    if top is None or lk is None or lk.name != "release":
        return top
    k = " ".join((q or "").lower().split())
    cur = top["title"].rsplit(" — Wikipedia", 1)[0]
    if cur.lower() == k:
        return top
    plain = next((r for r in rows[:CARD_WITHIN + 4] if r.get("source") == "wikipedia" and r.get("score", 0) >= WIKI_EXACT_SCORE
                  and r.get("title", "").rsplit(" — Wikipedia", 1)[0].lower() == k), None)
    if plain is None:
        return top
    try:
        a = _title_views(lk, plain["title"].rsplit(" — Wikipedia", 1)[0]) or 0
        b = _title_views(lk, cur) or 0
    except Exception:  # noqa: BLE001 - a card is extra
        return top
    return plain if a >= SHORT_RATIO * max(b, 1) else top

# Two- and three-letter names (owner 2026-11-15: "two letter searches should be able to get their own card"; "lg" had
# none, "ea" showed E&A): the article whose official site is the name's own .com front page, when that .com is among
# the first three rows and the article is read at least SHORT_ENTITY_VIEWS times (LG Electronics 15,034; Go.com 1,843
# and MobileMe 2,019 are not). A two-letter search with no such article and a card on another site gets no card ("is",
# "go", "us"). Release lookups only.
SHORT_ENTITY_VIEWS = int(os.environ.get("SEARCH_SHORT_ENTITY_VIEWS", "10000"))

def top_is(top: dict | None, title: str) -> bool:
    return bool(top) and top["title"].rsplit(" — Wikipedia", 1)[0] == title

# Two-letter searches that keep their card although its site is not <name>.com (CORE-B-03): the query -> the article titles
# that are the thing people mean. Owner 2026-11-17: "ny" is the state (ny.gov); "nyc" is the city (the .com rule: nyc.gov). Not "us" (the owner's own example), "is", "go", "me".
TWO_LETTER_KEEP = {"uk": ("United Kingdom",), "eu": ("European Union",), "un": ("United Nations",),
                   "ny": ("New York (state)",), "la": ("Los Angeles",), "dc": ("Washington, D.C.",),
                   "ai": ("Artificial intelligence",)}

def short_entity(q: str, rows: list[dict], current: str | None):
    """None when the rule does not apply (longer than 3, not letters and digits, no release); else (title, drop):
    the article to show (or None), and whether a two-letter search's current card should go."""
    k = (q or "").lower().strip()
    lk = lookups()
    if not re.fullmatch(r"[a-z0-9]{2,3}", k) or lk is None or lk.name != "release":
        return None
    host = k + ".com"
    best = None
    if any(r.get("source") == "site" and _domain_of(r.get("url") or "") == host for r in rows[:3]):
        try:
            claims = lk.site_claims(host)[1]
        except Exception:  # noqa: BLE001 - a card is extra
            claims = ()
        titles = [c[0] for c in claims]
        if current in titles:                   # "hp": HP Inc. is hp.com's, it stays (not Hewlett-Packard)
            return (current, False)
        for t in titles:
            v = _title_views(lk, t) or 0
            if v >= SHORT_ENTITY_VIEWS and (best is None or v > best[1]):
                best = (t, v)
    if best:
        return (best[0], False)
    for t in (TWO_LETTER_KEEP.get(k) or ()) if len(k) == 2 else ():
        # the owner's approved two-letter list (uk eu un ny la dc ai) names the article people mean: the card IS that article, whatever
        # card the list produced (eu, ny, la, ai had none: the article is not titled "eu", so no result was an exact title) and read as much as the floor says
        try:
            views = _title_views(lk, t)
        except Exception:  # noqa: BLE001
            views = None
        if views is not None and views >= SHORT_ENTITY_VIEWS:
            return (t, False)
    if len(k) == 2 and current is not None:
        # CORE-B-03: the rule drops a two-letter search's card unless it is on its own .com ("is", "go", "me", "us"). It also
        # dropped the right card of the places and bodies people really mean by two letters; these keep it, when the card is
        # that exact article and it is read as much as the floor says. "us" stays card-less (owner decision, 2026-11-15).
        kept = TWO_LETTER_KEEP.get(k)
        if kept and re.sub(r"\s*\([^)]*\)\s*$", "", current) in kept:
            try:
                views = _title_views(lk, current)
            except Exception:  # noqa: BLE001
                views = None
            if views is None or views >= SHORT_ENTITY_VIEWS:
                return (None, False)
        site = None
        try:
            site = (lk.h[CARD_SITE_SQL]({"t": current}) or [(None,)])[0][0]
        except Exception:  # noqa: BLE001
            pass
        return (None, not (site and _domain_of(site) == host))
    return (None, False)

def _short_plain_current(q: str, k: str, current: str | None) -> str:
    """'plain' when current IS the plain title for k, 'related' when it shares a word/stem
    with k (Prime (drink), Acrobatics, MESSENGER, Bill Bixby), else 'unrelated' (a redirect
    like Hangover or Prednisone)."""
    if not current:
        return "unrelated"
    base = re.sub(r"\s*\(.*\)$", "", current).strip()
    if base.lower() == k and "(" not in current and not (current.isupper() and len(current) >= 3 and not (q or "").isupper()):
        return "plain"
    q_words = _TOKEN.findall(k)
    c_words = _TOKEN.findall(base.lower())
    if any(cw.startswith(qw) or qw.startswith(cw) for qw in q_words for cw in c_words if len(qw) >= 3 and len(cw) >= 3):
        return "related"
    if norm_key(k) in norm_key(base):
        return "related"
    return "unrelated"

def short_name_title(q: str, current: str | None) -> str | None:
    """The maker's product article the bare name means, or None (see SHORT_MAKERS, SHORT_QUALIFIERS).
    A maker's own name is never one of its products ("google" is Google). A plain word keeps its
    plain meaning unless the product is SHORT_RATIO times more read; a non-plain or redirect card
    (Prime (drink), Acrobatics, Hangover, Prednisone, MESSENGER) yields more easily."""
    k = " ".join((q or "").lower().split())
    if not SHORT_NAMES or not re.fullmatch(r"[a-z]{2,}( [a-z]{2,})?", k) or k in {m.lower() for m in SHORT_MAKERS}:
        return None
    lk = lookups()
    if lk is None or lk.name != "release":
        return None
    def look():
        name = " ".join(w.capitalize() for w in k.split())
        cands = [f"{m} {name}{suf}" for m in SHORT_MAKERS for suf in SHORT_SUFFIXES]
        cands += [f"{name} Pro"] + [f"{name} ({qual})" for qual in SHORT_QUALIFIERS]
        best = None
        for t in cands:
            v = _title_views(lk, t)
            if v is not None and (best is None or v > best[1]):
                best = (t, v)
        return [best]
    best = cached(("short_name", k), look, static=True)[0]
    if best is None or best[0] == current or best[1] < SHORT_FLOOR:
        return None
    # read SHORT_RATIO times more than the plain word, and the plain word's own article not read SHORT_RATIO times
    # more than it ("music": Music is 63,689 to Sony Music's 19,628; "watch": Watch is 13,131 to Apple Watch's 28,685).
    # Non-plain cards (Prime (drink), Acrobatics, MESSENGER) only require best >= now; unrelated redirects
    # (Hangover, Prednisone) do not block at all.
    now = (_title_views(lk, current) or 0) if current else 0
    kind = _short_plain_current(q, k, current)
    req = SHORT_RATIO * now if kind == "plain" else (now if kind == "related" else 0)
    plain = _title_views(lk, " ".join(w.capitalize() for w in k.split())) or 0
    return best[0] if best[1] >= req and plain <= SHORT_RATIO * best[1] else None

# "gmail login", "chase sign in" (owner 2026-11-14: "gmail login" is the README's example and had no card): the card
# is the card of the name without the account words.
_LOGIN_TAIL = re.compile(r"\s+(log ?in|sign ?in|sign ?on|log ?on|account|my account|login page|sign in page)$")

def account_base(q: str) -> str | None:
    k = " ".join((q or "").lower().split())
    m = _LOGIN_TAIL.search(k)
    base = k[:m.start()].strip() if m else ""
    return base if len(base) >= 2 else None

def wiki_top(lk_or_conn, title: str) -> dict | None:
    """A card article row for a title found by lookup (not in the list): its url and lead."""
    try:
        if hasattr(lk_or_conn, "h"):
            rows = lk_or_conn.h[FAMOUS_LEAD_SQL]({"t": title})
        else:
            rows = lk_or_conn.execute(FAMOUS_LEAD_SQL, {"t": title}).fetchall()
    except Exception:  # noqa: BLE001 - a card is extra
        return None
    if not rows:
        return None
    return {"title": title + " — Wikipedia", "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,'!-.:"),
            "content": rows[0][0] or "", "source": "wikipedia"}

def lead_with_official(res: list[dict], card: dict | None, article: bool = False) -> list[dict]:
    """A short name's card (short_name_title) leads the list with its official site: "keep" listed KEEP's radio site
    and article first and Google Keep's site third, "premiere" never listed Adobe's site at all (2026-11-14)."""
    off = next((l["url"] for l in (card or {}).get("links", []) if l.get("title") == "Official site"), None)
    if not off:
        if article and card and card.get("url"):
            # a card with no official site (ai: Artificial intelligence): its article leads, not a stock quote or a namesake domain
            places = [r for r in res if r.get("source") == "place"]
            have = next((r for r in res if same_page(r.get("url") or "") == same_page(card["url"])), None)
            row = have or {"url": card["url"], "title": f"{card['title']} — Wikipedia", "source": "wikipedia",
                           "content": card.get("description") or "", "score": max((r.get("score") or 0 for r in res), default=0)}
            return places + [row] + [r for r in res if r is not have and r.get("source") != "place"]
        return res
    places = [r for r in res if r.get("source") == "place"]
    have = next((r for r in res if same_page(r.get("url") or "") == same_page(off)), None)
    row = have or {"url": off, "title": f"{card['title']} — official site", "source": "site", "official": True,
                   "content": card.get("description") or "", "score": max((r.get("score") or 0 for r in res), default=0)}
    return places + [row] + [r for r in res if r is not have and r.get("source") != "place"]

def card_of(res: list[dict], descr: str | None = None, related: list[dict] | None = None,
            image: dict | None = None, site: str | None = None, top: dict | None = None) -> dict | None:
    """An exact article among the first CARD_WITHIN results -> {title, url, description,
    lead, links: its official site then its accounts, related: readers' next articles,
    image: its picture + credit (card_image)}; else None. ("taylor swift" leads with her official site, the article second.)"""
    top = top or card_article(res)
    if top is None:
        return None
    title = top["title"].rsplit(" — Wikipedia", 1)[0]
    key = same_page(top["url"])
    # its official site wherever it ranked: donaldjtrump.com sat 4th and never reached the card
    official = [{"title": "Official site", "url": r["url"]} for r in res
                if r.get("official") and r["title"].startswith(title + " — ")
                and official_link_ok(r["url"])][:1]
    if not official and site and official_link_ok(site) and not official_inner_page(site):
        official = [{"title": "Official site", "url": site}]       # the article's own stored site (CARD_SITE_SQL)
    if not official:
        official = platform_home(title, descr, top.get("content"))     # a platform whose own site is a social host
    official = own_name_site(title, official, res)
    mine = [r for r in res if r.get("profile") and r.get("of") == key]
    # what it is, in one line (2026-11-06): its Wikidata short description, which every account row of it carries
    what = next((r["content"].strip() for r in mine if (r.get("content") or "").strip()), "")
    links = official + [{"title": ACCOUNT_LABEL.get(r.get("platform", ""), r["title"].rsplit(" on ", 1)[-1].split(" (@")[0]),
                         "url": r["url"]} for r in mine if account_fits(r.get("platform", ""), what)]
    # related: looked up for the card (the top-8 cut and the rerankers keep only some of
    # them in res — "paul rudd" showed 1 of 4); else whichever made it into res
    if related is None:
        related = [r for r in res if r.get("related")]
    related = [{"title": r["title"].rsplit(" — Wikipedia", 1)[0], "url": r["url"]} for r in related]
    more = []
    if CARDLINKS_PATH:                                   # Wikidata's other links for it (card_links)
        have = {_host_key(l["url"]) for l in links} | {l["title"] for l in links}
        for label, url in card_links(title):
            h = _host_key(url)
            kind = _CARDLINK_KIND.get(label, "")
            # a film or music catalogue only for what it catalogues; unknown (no description) = left out
            if h in have or label in have or (kind and not (descr or what)) or not account_fits(kind, descr or what):
                continue
            have |= {h, label}
            if label in CARDLINKS_MAIN:
                links.append({"title": label, "url": url})
            elif len(more) < CARDLINKS_MORE:
                more.append({"title": label, "url": url})
    card = {"title": title, "url": top["url"], "lead": clean_lead(top.get("content", "")), "links": links, "related": related}
    if more:
        card["more_links"] = more
    if official and (tree := site_tree(official[0]["url"])):
        card["nav"] = tree
    if descr or what:
        card["description"] = card_description(descr, what)
    if image:
        card["image"] = image
    return card

def card_pre(q: str, res: list[dict]):
    """The card's first step, run for every answer (never stored): marks a respelled name's rows in place, finds the
    article that heads the list and, for "gmail login", the rows of the account's base name ("gmail"). Returns
    (top, cres, base): the article row, the rows the card is built from, the base name or None."""
    if card_article(res) is None:
        mark_respelled(q, res)                          # "sarah j mass": Sarah J. Maas's card
    top = card_article(res)
    cres = res                                          # the rows the card is built from; a short name's card
    base = account_base(q) if top is None else None
    if base and any(r.get("source") == "site" and _label(r.get("url") or "") == norm_key(base) for r in res[:3]):
        # "gmail login": the card of "gmail", when the name's own domain leads the list
        cres = search(base, 8)                          # search() caches by its own key (position, reach, every setting): CORE-A-11
        top = card_article(cres)
    return top, cres, base

def card_build(q: str, res: list[dict], top: dict | None, cres: list[dict], base: str | None):
    """The card stage (parts stage "card" phase 2): a pure function of its arguments, the release data and the
    settings it reads. Returns (card, res, failed): the card or None, the rows after a short name's official site
    leads (the same list when it did not), and the exception's name when the last step failed (then nothing is kept).
    It never calls search(): the account base's rows come in as cres, so a change to the search code does not
    change this stage's version."""
    card, swapped = None, False
    lk = lookups()
    alt = short_name_title(base or q, top["title"].rsplit(" — Wikipedia", 1)[0] if top else None)
    if not alt:                                 # "google": Google, not Google+ (a maker's name never swaps above)
        top = plain_title_first(base or q, top, cres)
    short = short_entity(base or q, cres, top["title"].rsplit(" — Wikipedia", 1)[0] if top else None) if not alt else None
    if short and short[0] and not top_is(top, short[0]):
        alt = short[0]                          # "lg": LG Electronics (lg.com's own article); "ea": Electronic Arts
    kept_two = bool(short and short[0] and short[0] in TWO_LETTER_KEEP.get(norm_key(base or q), ()))
    if alt:                                     # "outlook" -> Microsoft Outlook, "keep" -> Google Keep
        swapped = True
        if lk is not None:
            top = wiki_top(lk, alt) or top
        else:
            with _conn() as c:
                top = wiki_top(c, alt) or top
    if top is not None:
        t = top["title"].rsplit(" — Wikipedia", 1)[0]
        if lk is not None and lk.name == "release":     # the card is one precomputed record
            descr, rel_rows, img_row, _person = lk.card_parts(t, RELATED_N)
            descr = whole_description(lk, t, descr)
            row, rel, img = (descr,) if descr else None, related_results(rel_rows), card_image(img_row)
            site_row = (lk.h[CARD_SITE_SQL]({"t": t}) or [None])[0]
            cres = with_own_accounts(cres, top, lambda: lk.h[PROFILES_SQL]({"t": t, "p": PROFILE_PLATFORMS}))
        else:
            with _conn() as c:
                has = tables(c)
                row = c.execute(CARD_DESCR_SQL, {"t": t}).fetchone() if "suggest_items" in has else None
                rel = (related_results(c.execute(RELATED_SQL, {"t": t, "n": RELATED_N}))
                       if "wiki_next" in has else None)
                img = (card_image(c.execute(CARD_IMAGE_SQL, {"t": t}).fetchone())
                       if "card_images" in has else None)
                site_row = c.execute(CARD_SITE_SQL, {"t": t}).fetchone() if "wiki_sites" in has else None
                if "wiki_links" in has:
                    cres = with_own_accounts(cres, top, lambda: c.execute(PROFILES_SQL, {"t": t, "p": PROFILE_PLATFORMS}).fetchall())
        card = card_of(cres, row[0] if row else None, rel, img, site_row[0] if site_row else None, top=top)
        if short is not None and not short[0] and short[1]:
            return None, res, None              # "is", "go", "us": a two-letter word gets no card without the entity rule
        if swapped or kept_two:
            try:
                res = lead_with_official(res, card, article=kept_two)
            except Exception as e:                  # as before the split: the card stays, the rows stay as they were
                return card, res, type(e).__name__
    return card, res, None

def card_stage(q: str, res: list[dict], top, cres, base):
    """card_build, through the parts store when SEARCH_PARTS has "card" (and live otherwise or on any trouble). The key's
    inputs are the content of q, the rows after card_pre and the account base's rows."""
    P = parts_for("card")
    if P is None or _parts_off.get():
        if _pcheck.get():
            raise PartStale("card")
        card, res2, failed = card_build(q, res, top, cres, base)
        _card_live.set(card_text.VERSION)
        if failed:
            print(f"card failed: {failed}", flush=True)
        return card, res2
    import parts as _parts
    try:
        before = _parts.canon(res)
        key = P.key("card", [q, before.decode(), None if cres is res else _parts.canon(cres).decode(), base])
        got = P.get("card", key)
    except Exception:  # noqa: BLE001
        P.stats["error"] += 1
        key, got = None, None
    if got is not None:
        note_part("card", key)
        return got["card"], (got["res"] if got.get("res") is not None else res)
    if _pcheck.get():
        raise PartStale("card")
    card, res2, failed = card_build(q, res, top, cres, base)
    _card_live.set(card_text.VERSION)
    if failed:
        print(f"card failed: {failed}", flush=True)
    elif key is not None and _warm.get():                # only the builder writes (a visitor's search is never kept)
        try:
            if P.put("card", key, {"card": card, "res": None if _parts.canon(res2) == before else res2}):
                note_part("card", key)
        except Exception:  # noqa: BLE001
            P.stats["error"] += 1
    return card, res2
