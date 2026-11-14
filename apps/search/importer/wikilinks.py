"""Every account and website Wikidata records (2026-09-24, "can we
just use every wiki url available"): "nike instagram", "taylor swift spotify",
"oppenheimer imdb", "torvalds github" -> the account, answered locally.

One QLever query per platform (Wikidata is CC0; nothing is fetched from the platforms).
YouTube has its own table (youtube.py: follower counts, topic search); subreddits come
from the Arctic Shift directory. "described at URL" (P973, 3.9M pages about the thing:
catalogues, databases, reference entries) is platform "described" — shown beside an
exact-name match, never at tier 1 ("add the described at urls too").

Also: official websites (P856) of items WITHOUT an English Wikipedia article —
wiki_sites only covers articles.

    search_import.py wiki-links [--platforms a,b]   -> wiki_links
"""

from __future__ import annotations

import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from subreddits import norm
from wikisites import normalize
from youtube import ENDPOINT, USER_AGENT, lit

_WIKI = "https://en.wikipedia.org/wiki/"


def _handle(pattern: str, fmt: str):
    rx = re.compile(pattern)

    def build(v: str) -> str | None:
        v = v.strip().lstrip("@")
        return fmt.format(v=v) if rx.match(v) else None
    return build


def _mastodon(v: str) -> str | None:
    m = re.match(r"^@?([A-Za-z0-9_.]{1,64})@([a-z0-9.-]+\.[a-z]{2,})$", v.strip(), re.I)
    return f"https://{m.group(2).lower()}/@{m.group(1)}" if m else None


def _imdb(v: str) -> str | None:
    m = re.match(r"^(tt|nm|co)\d{7,8}$", v.strip())
    return f"https://www.imdb.com/{ {'tt': 'title', 'nm': 'name', 'co': 'company'}[m.group(1)] }/{v.strip()}/" if m else None


def _url(v: str) -> str | None:
    v = v.strip().strip("<>")
    return v if re.match(r"^https?://[^\s/]+\.[^\s/]+", v) and len(v) <= 500 else None


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/'(),]{0,119}$")


def _fmt(fmt: str):
    """An external ID through Wikidata's formatter URL (P1630, checked 2026-09-25). Only
    URL-safe characters pass _ID (Ballotpedia's page names: "Beto_O'Rourke"), never free text."""
    def build(v: str) -> str | None:
        v = v.strip()
        return fmt.replace("$1", v) if _ID.match(v) and ".." not in v else None
    return build


# The sites that identify a person, film, band, book author, game or athlete (owner
# 2026-09-25: "paul rudd" should look like the web's IMDb / Rotten Tomatoes / Letterboxd
# spread). Which ones an item has says what it is: only actors have a TMDB person ID.
_IDS = {
    "rottentomatoes":  ("P1258", "Rotten Tomatoes", "https://www.rottentomatoes.com/$1"),
    "letterboxd":      ("P6127", "Letterboxd", "https://letterboxd.com/film/$1/"),
    "tmdb-person":     ("P4985", "TMDB", "https://www.themoviedb.org/person/$1"),
    "tmdb-movie":      ("P4947", "TMDB", "https://www.themoviedb.org/movie/$1"),
    "tmdb-tv":         ("P4983", "TMDB", "https://www.themoviedb.org/tv/$1"),
    "metacritic":      ("P1712", "Metacritic", "https://www.metacritic.com/$1"),
    "applemusic":      ("P2850", "Apple Music", "https://music.apple.com/artist/$1"),
    "genius":          ("P2373", "Genius", "https://genius.com/artists/$1"),
    "discogs":         ("P1953", "Discogs", "https://www.discogs.com/artist/$1"),
    "allmusic":        ("P1728", "AllMusic", "https://www.allmusic.com/artist/$1"),
    "soundcloud":      ("P3040", "SoundCloud", "https://soundcloud.com/$1"),
    "goodreads":       ("P2963", "Goodreads", "https://www.goodreads.com/author/show/$1"),
    # The free text of a book, and an author's shelf of them (2026-10-12: 1,797 works and
    # 14,250 authors with an English article). A link and the name as credit only: nothing
    # is fetched from gutenberg.org, which blocks robots.
    "gutenberg":       ("P2034", "Project Gutenberg", "https://www.gutenberg.org/ebooks/$1"),
    "gutenberg-author": ("P1938", "Project Gutenberg", "https://www.gutenberg.org/ebooks/author/$1"),
    "steam":           ("P1733", "Steam", "https://store.steampowered.com/app/$1/"),
    "espn-nfl":        ("P3686", "ESPN", "https://www.espn.com/nfl/player/_/id/$1"),
    "nba":             ("P3647", "NBA.com", "https://www.nba.com/stats/player/$1/career"),
    "basketball-ref":  ("P2685", "Basketball Reference", "https://www.basketball-reference.com/players/$1.html"),
    "mlb":             ("P3541", "MLB.com", "https://www.mlb.com/player/$1"),
    "baseball-ref":    ("P1825", "Baseball Reference", "https://www.baseball-reference.com/players/$1.shtml"),
    "hockey-ref":      ("P3598", "Hockey Reference", "https://www.hockey-reference.com/players/$1.html"),
    "transfermarkt":   ("P2446", "Transfermarkt", "https://www.transfermarkt.com/-/profil/spieler/$1"),
    "fbref":           ("P5750", "FBref", "https://fbref.com/en/players/$1/"),
    "youtube":         ("P2397", "YouTube", "https://www.youtube.com/channel/$1"),
    # Public office ("if someone holds public office or is a public figure
    # there links as public figures should be in the card")
    "bioguide":        ("P1157", "Congress", "https://bioguide.congress.gov/search/bio/$1"),
    "govtrack":        ("P12644", "GovTrack", "https://www.govtrack.us/congress/person.xpd?id=$1"),
    "ballotpedia":     ("P2390", "Ballotpedia", "https://ballotpedia.org/$1"),
    "opensecrets":     ("P2686", "OpenSecrets", "https://www.opensecrets.org/personal-finances/net-worth?cid=$1"),
    "votesmart":       ("P3344", "Vote Smart", "https://justfacts.votesmart.org/candidate/$1"),
    "fec":             ("P1839", "FEC", "https://www.fec.gov/data/candidate/$1"),
    "cspan":           ("P2190", "C-SPAN", "https://www.c-span.org/person/?$1"),
    "ukparliament":    ("P10428", "UK Parliament", "https://members.parliament.uk/member/$1/career"),
}

# platform -> (Wikidata property, display name, url builder, handle is a lookup key)
PLATFORMS = {
    "x":         ("P2002", "X", _handle(r"^[A-Za-z0-9_]{1,15}$", "https://x.com/{v}"), True),
    "instagram": ("P2003", "Instagram", _handle(r"^[A-Za-z0-9_.]{1,30}$", "https://www.instagram.com/{v}/"), True),
    "facebook":  ("P2013", "Facebook", _handle(r"^[A-Za-z0-9.\-]{2,80}$", "https://www.facebook.com/{v}"), True),
    "tiktok":    ("P7085", "TikTok", _handle(r"^[A-Za-z0-9_.]{2,24}$", "https://www.tiktok.com/@{v}"), True),
    "linkedin":  ("P4264", "LinkedIn", _handle(r"^[A-Za-z0-9\-_%.]{2,100}$", "https://www.linkedin.com/company/{v}/"), True),
    "linkedin-in": ("P6634", "LinkedIn", _handle(r"^[A-Za-z0-9\-_%.]{2,100}$", "https://www.linkedin.com/in/{v}/"), True),
    "spotify":   ("P1902", "Spotify", _handle(r"^[A-Za-z0-9]{22}$", "https://open.spotify.com/artist/{v}"), False),
    "github":    ("P2037", "GitHub", _handle(r"^[A-Za-z0-9-]{1,39}$", "https://github.com/{v}"), True),
    "twitch":    ("P5797", "Twitch", _handle(r"^[A-Za-z0-9_]{3,25}$", "https://www.twitch.tv/{v}"), True),
    "imdb":      ("P345", "IMDb", _imdb, False),
    "mastodon":  ("P4033", "Mastodon", _mastodon, False),
    "bluesky":   ("P12361", "Bluesky", _handle(r"^[a-z0-9.\-]+\.[a-z]{2,}$", "https://bsky.app/profile/{v}"), False),
    "blog":      ("P1581", "blog", _url, False),
    "site":      ("P856", "official site", _url, False),
    "described": ("P973", "described at", _url, False),
}
PLATFORMS.update({k: (prop, label, _fmt(fmt), False) for k, (prop, label, fmt) in _IDS.items()})
ID_PLATFORMS = list(_IDS)

QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?item ?v (SAMPLE(?en) AS ?name) (SAMPLE(?mul) AS ?mname) (SAMPLE(?desc) AS ?about)
       (SAMPLE(?article) AS ?wiki) (MAX(?links) AS ?sitelinks) WHERE {
  ?item wdt:%(prop)s ?v .
  %(filter)s
  OPTIONAL { ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }
  OPTIONAL { ?item rdfs:label ?mul . FILTER(LANG(?mul) = "mul") }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?item wikibase:sitelinks ?links }
} GROUP BY ?item ?v"""
# Official sites of articles are already in wiki_sites (with infobox kinds and Tranco
# ranks); here only the items English Wikipedia has no article for.
_NO_ARTICLE = ("FILTER NOT EXISTS { ?a schema:about ?item ; "
               "schema:isPartOf <https://en.wikipedia.org/> }")

_SHORT = re.compile(r"\s+[–—|:-]\s+|:\s+")


def query_for(platform: str, page: int | None = None) -> str:
    prop = PLATFORMS[platform][0]
    q = QUERY % {"prop": prop, "filter": _NO_ARTICLE if platform == "site" else ""}
    if page is not None:
        q += f"\nORDER BY ?item ?v LIMIT {PAGE} OFFSET {page * PAGE}"
    return q


# QLever cut the 3.9M-row P973 answer off at 2.42M with no error (2026-09-24): every
# platform is read in pages and checked against a COUNT before the table is swapped in.
PAGE = 500_000
COUNT = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
SELECT (COUNT(*) AS ?n) WHERE { SELECT DISTINCT ?item ?v WHERE { ?item wdt:%(prop)s ?v . %(filter)s } }"""


def count_query(platform: str) -> str:
    return COUNT % {"prop": PLATFORMS[platform][0], "filter": _NO_ARTICLE if platform == "site" else ""}


def complete(got: int, expected: int) -> bool:
    """A short answer must stop the load: 0.5% slack for rows QLever itself drops."""
    return got >= expected * 0.995


def parse_row(platform: str, line: str):
    """One QLever TSV line -> (platform, qid, value, url, name, description, wiki_title,
    sitelinks, key, hkey, wkey, skey), or None for the header / an unusable value."""
    p = line.rstrip("\n").split("\t")
    if len(p) < 7:
        return None
    qid = p[0].strip().strip("<>").rsplit("/", 1)[-1]
    if not re.match(r"^Q\d+$", qid):
        return None                                        # also the "?item …" header
    value = lit(p[1]).strip("<>")
    build = PLATFORMS[platform][2]
    url = build(value)
    if url and platform in ("site", "blog", "described"):
        got = normalize(url, None)
        url = got[0] if got else None
    if not url:
        return None
    wiki = p[5].strip().strip("<>")
    wiki = urllib.parse.unquote(wiki[len(_WIKI):]).replace("_", " ") if wiki.startswith(_WIKI) else ""
    name = (lit(p[2]) or lit(p[3]) or wiki)[:300]
    if not name:
        return None
    key = norm(name)
    wkey = norm(re.sub(r"\s*\(.*\)$", "", wiki)) if wiki else ""
    skey = norm(_SHORT.split(name, 1)[0])
    hkey = norm(value) if PLATFORMS[platform][3] else ""
    try:
        links = int(float(lit(p[6]))) if p[6].strip() else 0
    except ValueError:
        links = 0
    return (platform, qid, value[:300], url, name, lit(p[4])[:500], wiki or None, links,
            key or None, hkey or None, (wkey if wkey != key else "") or None,
            skey if skey and skey != key and len(skey) >= 3 else None)


def log(msg: str) -> None:
    print(f"[wiki-links] {msg}", flush=True)


def fetch(platform: str, query: str):
    """The open QLever response, read line by line."""
    req = urllib.request.Request(ENDPOINT, data=urllib.parse.urlencode({"query": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    for attempt in range(3):
        try:
            return urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            # 429/503 = QLever asking us to back off: wait, never hammer.
            if e.code not in (429, 503) or attempt == 2:
                raise
            log(f"{platform}: HTTP {e.code}, waiting {60 * (attempt + 1)}s")
            time.sleep(60 * (attempt + 1))
    raise RuntimeError("unreachable")


COLS = "platform, qid, value, url, name, description, wiki_title, sitelinks, key, hkey, wkey, skey"
# Rows other loaders keep in this table (gutenberg.py): a full run carries them over.
OTHER_LOADERS = ["gutenberg-catalog"]


def cmd_wiki_links(conn, platforms: list[str] | None = None) -> None:
    t0 = time.time()
    want = platforms or list(PLATFORMS)
    conn.execute("DROP TABLE IF EXISTS wiki_links_new")
    conn.execute("CREATE TABLE wiki_links_new (platform text, qid text, value text, url text, name text, "
                 "description text, wiki_title text, sitelinks integer, key text, hkey text, wkey text, skey text)")
    if conn.execute("SELECT to_regclass('wiki_links')").fetchone()[0]:
        if platforms:
            # A partial refresh keeps the other platforms' rows.
            conn.execute("INSERT INTO wiki_links_new SELECT * FROM wiki_links WHERE platform <> ALL(%s)", (want,))
        else:
            conn.execute("INSERT INTO wiki_links_new SELECT * FROM wiki_links WHERE platform = ANY(%s)", (OTHER_LOADERS,))
    for platform in want:
        t1 = time.time()
        with fetch(platform, count_query(platform)) as resp:
            expected = int(resp.read().decode().splitlines()[1].strip().strip('"').split("^^")[0].strip('"'))
        n = seen = page = 0
        while True:
            got = 0
            with fetch(platform, query_for(platform, page)) as resp, conn.transaction(), \
                    conn.cursor().copy(f"COPY wiki_links_new ({COLS}) FROM STDIN") as cp:
                for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                    if line.startswith("?"):
                        continue
                    got += 1
                    r = parse_row(platform, line)
                    if r:
                        cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
                        n += 1
            seen += got
            if got < PAGE:
                break
            page += 1
            time.sleep(5)
        if not complete(seen, expected):
            raise RuntimeError(f"{platform}: QLever sent {seen:,} of {expected:,} rows — not swapping in a partial table")
        log(f"{platform}: {seen:,} of {expected:,} from QLever -> {n:,} usable in {time.time() - t1:.0f}s")
        time.sleep(5)                                      # one query at a time, spaced out
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_links")
        conn.execute("ALTER TABLE wiki_links_new RENAME TO wiki_links")
        for col in ("key", "hkey", "wkey", "skey"):
            conn.execute(f"CREATE INDEX wiki_links_{col} ON wiki_links (platform, {col}) WHERE {col} IS NOT NULL")
        # an exact article's own accounts under it (search-api PROFILES_SQL)
        conn.execute("CREATE INDEX wiki_links_title ON wiki_links (wiki_title) WHERE wiki_title IS NOT NULL")
    for platform, n in conn.execute("SELECT platform, count(*) FROM wiki_links GROUP BY 1 ORDER BY 2 DESC"):
        log(f"  {platform:<12} {n:>10,}")
    log(f"done in {(time.time() - t0) / 60:.1f} min")
