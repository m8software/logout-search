"""Pictures on the knowledge card ("add pictures to the cards … performance
minimally impacted and licensing issues non existent").

Where the picture comes from decides the licence question:
  - Wikidata's image (P18), else its logo (P154) — never Wikipedia's lead image. Wikidata
    only takes files on Wikimedia Commons, and Commons only takes free files, so the
    non-free "fair use" posters and covers that live on English Wikipedia never get here.
  - Each file's licence from Commons' own structured data (P275 licence, P6216 copyright
    status), read through QLever — the Commons API answered HTTP 429 to its first request.
  - Kept: public domain, CC0, CC BY and CC BY-SA (any version, ports with a known
    jurisdiction). Dropped: GFDL-only, NonCommercial/NoDerivs, "no known restrictions",
    anything without a licence statement, and a CC BY/BY-SA file with no author on record
    (its credit could not be given).
  - The card shows the credit: author · licence (linked to its deed) · Wikimedia Commons
    (linked to the file page). The picture is scaled to fit, never cropped (a crop is a
    change CC BY 4.0 would require us to state).

Measured 2026-09-29 (n=1000 per group): a card gets a picture for ~56% of articles in 10+
languages and ~75% in 50+ with CC BY/BY-SA allowed; public domain alone was 14% / 21%.

Performance: one standard Commons thumbnail per article (250 px wide), fetched once — at
most 2 requests at a time and 25 Mbps (Wikimedia's robot policy for upload.wikimedia.org) —
scaled into a 250 px box and stored as WebP (<= 16 KB) in the working file card-images.sqlite
(CARD_IMAGES_DB; ADR-063: no Postgres table). `release_v1.py pictures-from` makes a release's pictures.sqlite from it.
search-api sends it inline in the card as a data: URI — no second request, and it works
the same over HTTP, the native host and WebRTC. Nothing on the page ever loads from
Wikimedia.

    search_import.py card-images meta [--top N]    candidates + licences -> src (ranks read from the build DB: bridge)
    search_import.py card-images fetch [--limit N]    thumbnails -> pictures (here, e.g. indexhost)
    search_import.py card-images todo [--limit N] > todo.tsv.gz   the next N articles, for AWS
    card_images.py download --todo F --out F [--deadline-min M]    on the build instance (no database)
    search_import.py card-images load --file F       the instance's results -> pictures
    search_import.py card-images seed                ONE-SHOT BRIDGE: Postgres card_image_src + card_images -> the file

`meta` re-reads everything and drops pictures whose file or licence changed (a file
Commons deleted disappears from the card on the next run). `fetch` resumes where it
stopped, most-read articles first, and stops the whole run on the first 403/429/503 or
network error (infra/build-aws/jobs/card-images.sh runs the download on AWS).
"""

from __future__ import annotations

import base64
import collections
import gzip
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager

from youtube import lit

WIKIDATA = "https://qlever.dev/api/wikidata"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia's User-Agent policy asks for a way to reach the operator.
USER_AGENT = "logout-search-card-images/1 (https://search.logout.cloud; personal search index)"
_WIKI = "https://en.wikipedia.org/wiki/"
_FILEPATH = "http://commons.wikimedia.org/wiki/Special:FilePath/"
_WD = "http://www.wikidata.org/entity/"

PAGE = 500_000          # Wikidata rows per QLever page
# Wikidata's logo first where there is one (a company, a product, software: Python showed a
# screenshot of code and Costco a store photo "yes" to logo-first), then the
# article's own lead image (what Wikipedia shows, and the only kind the Kiwix ZIM can supply
# safely — see zim_match), then Wikidata's image.
KIND_ORDER = "CASE kind WHEN 'logo' THEN 0 WHEN 'lead' THEN 1 ELSE 2 END"
API_BATCH = 50          # files per Commons API request (its maximum for titles=)
THUMB_W = 250           # a standard Commons thumbnail width (arbitrary widths are refused)
BOX = 250               # stored picture fits inside BOX x BOX: shown up to 176 CSS px (was 176 -> 88 px:
                        # "teeny tiny", 2026-09-30); 250 is also the widest the ZIM and the thumbnails give
MAX_BYTES = 16_000      # per stored picture (measured at 250 px, quality 60: mean 7.5 KB, 5% over 16 KB)
MAX_DOWNLOAD = 5_000_000
AUTHOR_MAX = 1_000      # a longer credit is junk (a pasted page): never cut (G3), the file is dropped instead

# Formats a browser-sized raster comes back for. tif/pdf/djvu/video thumbnails have other
# URL shapes and are rarely a portrait or logo — skipped.
MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"}

# CC ports: the jurisdiction in the licence's English label -> its path on creativecommons.org.
# A port not listed here is dropped (its deed URL would be a guess).
JURISDICTION = {
    "argentina": "ar", "australia": "au", "austria": "at", "belgium": "be", "brazil": "br",
    "bulgaria": "bg", "canada": "ca", "chile": "cl", "china mainland": "cn", "colombia": "co",
    "costa rica": "cr", "croatia": "hr", "czech republic": "cz", "czechia": "cz", "denmark": "dk",
    "ecuador": "ec", "egypt": "eg", "england and wales": "uk", "estonia": "ee", "finland": "fi",
    "france": "fr", "germany": "de", "greece": "gr", "guatemala": "gt", "hong kong": "hk",
    "igo": "igo", "india": "in", "ireland": "ie", "israel": "il", "italy": "it", "japan": "jp",
    "luxembourg": "lu", "macedonia": "mk", "malaysia": "my", "malta": "mt", "mexico": "mx",
    "netherlands": "nl", "new zealand": "nz", "norway": "no", "peru": "pe", "philippines": "ph",
    "poland": "pl", "portugal": "pt", "puerto rico": "pr", "romania": "ro", "scotland": "scotland",
    "serbia": "rs", "singapore": "sg", "slovenia": "si", "south africa": "za", "south korea": "kr",
    "spain": "es", "sweden": "se", "switzerland": "ch", "taiwan": "tw", "thailand": "th",
    "uganda": "ug", "united states": "us", "vietnam": "vn",
}
CODES = set(JURISDICTION.values())
_CC_CODE = re.compile(r"^cc-by(-sa)?-(\d\.\d)(?:-([a-z]+))?$")


def log(msg: str) -> None:
    print(f"[card-images] {msg}", flush=True)


def complete(got: int, expected: int) -> bool:
    """wikilinks.complete: a short answer must stop the load (0.5% slack for rows QLever drops)."""
    return got >= expected * 0.995


# ---- pure logic (tests/unit/test-search-card-images.sh) ----------------------------------

def licence_from_ext(meta: dict) -> tuple[str, str | None, bool] | None:
    """A file's Commons extmetadata (what the file page itself shows) -> (short name, deed
    URL, needs credit), or None when it may not be shown. License is Commons' machine code:
    pd, cc0, cc-by-sa-4.0, cc-by-3.0-de …; anything else (GFDL, OGL, FAL, NC/ND, none) and
    anything Commons marks NonFree is dropped."""
    val = lambda k: str((meta.get(k) or {}).get("value", "")).strip()
    if val("NonFree"):
        return None
    code = val("License").lower()
    if code == "pd":
        return "Public domain", None, False
    if code == "cc0":
        return "CC0", "https://creativecommons.org/publicdomain/zero/1.0/", False
    m = _CC_CODE.match(code)
    if not m:
        return None
    sa, ver, where = bool(m.group(1)), m.group(2), m.group(3)
    if where is not None and where not in CODES:
        return None                                   # an unknown port: no deed URL to link
    kind = "by-sa" if sa else "by"
    url = f"https://creativecommons.org/licenses/{kind}/{ver}/" + (f"{where}/" if where else "")
    return f"CC {kind.upper()} {ver}" + (f" {where.upper()}" if where else ""), url, True


def decode_entities(v: str, rounds: int = 3) -> str:
    """HTML entities decoded until the text stops changing, at most `rounds` times: some Commons values are escaped
    twice ("Elliott &amp;amp; Fry"), and one decode left "&amp;" in 2,813 of rc9's credits (audit 2026-10-22 G3)."""
    for _ in range(rounds):
        d = html.unescape(v)
        if d == v:
            break
        v = d
    return v


def clean_credit(v: str) -> str:
    """One credit as the card shows it: markup and control characters removed, entities decoded, spaces collapsed.
    Markup goes before the decode, so a decoded "<name@example.org>" stays (it is part of the author's chosen credit)."""
    v = re.sub(r"[\x00-\x1f\x7f]", "", decode_entities(re.sub(r"<[^>]*>", "", v))).strip()
    return re.sub(r"\s+", " ", v)


def author_of(names: list[str], creator_labels: list[str], users: list[str]) -> str | None:
    """The credit's author: the name the uploader gave, else the creator's Wikidata label,
    else the Commons user name. Markup and control characters removed, entities decoded
    (Commons' Artist is HTML: "Chapman &amp; Hall" was shown as such, 2026-10-22). Stored WHOLE: the old cut at
    80 characters dropped creators CC BY asks us to name (audit 2026-10-22 G3). A credit over AUTHOR_MAX is None, not
    cut and not replaced by a later group: a CC BY/BY-SA file is then dropped."""
    for group in (names, creator_labels, users):
        for v in group:
            v = clean_credit(v)
            if v and not re.match(r"(unknown|anonymous|author unknown)\b", v, re.I):
                return v if len(v) <= AUTHOR_MAX else None
    return None


def file_name(iri: str) -> str | None:
    """'<http://commons.wikimedia.org/wiki/Special:FilePath/Foo%20bar.jpg>' -> 'Foo bar.jpg'."""
    iri = iri.strip().strip("<>")
    if not iri.startswith(_FILEPATH):
        return None
    name = urllib.parse.unquote(iri[len(_FILEPATH):]).replace("_", " ").strip()
    return name or None


def file_page(name: str) -> str:
    return "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(name.replace(" ", "_"))


def thumb_url(name: str, width: int | None, mime: str | None) -> str | None:
    """The Commons thumbnail URL for a file (MediaWiki's hashed upload path), the original
    when it is already narrower than THUMB_W, or None for a format we skip."""
    if mime not in MIMES:
        return None
    u = name.replace(" ", "_")
    h = hashlib.md5(u.encode("utf-8")).hexdigest()
    q = urllib.parse.quote(u)
    if mime != "image/svg+xml" and width is not None and width <= THUMB_W:
        return f"https://upload.wikimedia.org/wikipedia/commons/{h[0]}/{h[:2]}/{q}"
    thumb = f"{THUMB_W}px-{u}"
    ext = u.rsplit(".", 1)[-1] if "." in u else ""
    if len(thumb.encode("utf-8")) > 160:              # MediaWiki shortens long thumbnail names
        thumb = f"{THUMB_W}px-thumbnail.{ext}"
    if mime == "image/svg+xml":
        thumb += ".png"
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{h[0]}/{h[:2]}/{q}/{urllib.parse.quote(thumb)}"


# Busy pictures (maps, crowds, text-heavy logos) that do not fit MAX_BYTES at BOX even at the
# lowest quality get a smaller box rather than no picture: 2,564 files were refused on every
# run until 2026-10-04. The card scales them up to its 220 px box (slightly soft).
FALLBACK_BOXES = (200, 160)


def encode(raw: bytes) -> tuple[bytes, int, int] | None:
    """Any browser image -> (WebP bytes, width, height) fitted inside BOX x BOX (then the
    FALLBACK_BOXES), aspect kept, never enlarged, at most MAX_BYTES; None when even the
    smallest box cannot be made that small."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as src:
        src.seek(0)                                   # a GIF's first frame
        src = ImageOps.exif_transpose(src)
        src = src.convert("RGBA" if src.mode in ("RGBA", "LA", "P", "PA") else "RGB")
        for box in (BOX,) + FALLBACK_BOXES:
            im = src.copy()
            im.thumbnail((box, box), Image.Resampling.LANCZOS)
            for quality in (60, 45, 30):
                out = io.BytesIO()
                im.save(out, "WEBP", quality=quality, method=6)
                if out.tell() <= MAX_BYTES:
                    return out.getvalue(), im.width, im.height
    return None


# ---- QLever -------------------------------------------------------------------------------

def _query(endpoint: str, query: str, what: str) -> list[list[str]]:
    """TSV rows (header dropped). 429/503 = QLever asking us to back off: wait, never hammer."""
    req = urllib.request.Request(endpoint, data=urllib.parse.urlencode({"query": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            return [line.split("\t") for line in text.splitlines()[1:]]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == 2:
                raise
            log(f"{what}: HTTP {e.code}, waiting {60 * (attempt + 1)}s")
            time.sleep(60 * (attempt + 1))
    raise RuntimeError("unreachable")


CANDIDATES = """PREFIX schema: <http://schema.org/> PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT ?article ?p ?img WHERE {
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  { ?item wdt:P18 ?img BIND("p18" AS ?p) } UNION { ?item wdt:P154 ?img BIND("logo" AS ?p) }
} ORDER BY ?article ?p ?img"""

CANDIDATES_COUNT = """PREFIX schema: <http://schema.org/> PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT (COUNT(*) AS ?n) WHERE {
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  { ?item wdt:P18 ?img BIND("p18" AS ?p) } UNION { ?item wdt:P154 ?img BIND("logo" AS ?p) }
}"""
# A refresh that would take more than this share of the stored pictures off the cards is
# refused: that is a short read (QLever rate-limited us 2026-09-29), not Commons deleting files.
MAX_DROP = 0.05

_PAGE_IMAGE_FREE = re.compile(r"\((\d+),'page_image_free','((?:[^'\\]|\\.)*)',")


def parse_page_props(lines):
    """Wikipedia's page_props dump (INSERT statements) -> (page_id, file name) for every
    article's free lead image (page_image_free: the picture the article shows first, when it
    is free — non-free posters and covers are page_image, never read here)."""
    for line in lines:
        if "page_image_free" not in line:
            continue
        for m in _PAGE_IMAGE_FREE.finditer(line):
            name = re.sub(r"\\(.)", r"\1", m.group(2)).replace("_", " ").strip()
            if name:
                yield int(m.group(1)), name


def parse_page(pg: dict) -> dict:
    """One Commons API page (formatversion=2) -> {name, width, mime, licence, author}; licence
    is licence_from_ext's tuple or None, missing files have no width/mime."""
    name = pg.get("title", "").removeprefix("File:")
    ii = (pg.get("imageinfo") or [None])[0]
    if not ii:                                        # a missing file has no imageinfo
        return {"name": name, "width": None, "height": None, "mime": None, "licence": None, "author": None}
    meta = ii.get("extmetadata") or {}
    artist = str((meta.get("Artist") or {}).get("value", ""))
    return {"name": name, "width": ii.get("width"), "height": ii.get("height"), "mime": ii.get("mime"),
            "licence": licence_from_ext(meta), "author": author_of([artist], [], []),
            "credit_too_long": len(clean_credit(artist)) > AUTHOR_MAX}


class ApiPace:
    """Commons API manners (robot policy): one request at a time, maxlag=5, a pause between
    requests and 5 s after any that took over 1 s, Retry-After honoured on 429/503/maxlag."""
    GAP = 1.0

    def __init__(self):
        self.next_at = 0.0

    def request(self, names: list[str]) -> dict:
        data = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo",
            "iiprop": "extmetadata|size|mime", "maxlag": "5",
            "iiextmetadatafilter": "Artist|License|LicenseShortName|LicenseUrl|NonFree|Restrictions",
            "titles": "|".join("File:" + n for n in names)}).encode()
        for attempt in range(4):
            time.sleep(max(0.0, self.next_at - time.monotonic()))
            t = time.monotonic()
            req = urllib.request.Request(COMMONS_API, data=data, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.load(resp)
                    retry = resp.headers.get("Retry-After")
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503) or attempt == 3:
                    raise
                wait = retry_after(e.headers.get("Retry-After"), 60 * (attempt + 1))
                log(f"commons api: HTTP {e.code}, Retry-After {e.headers.get('Retry-After')} — waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            took = time.monotonic() - t
            self.next_at = time.monotonic() + (5.0 if took > 1.0 else self.GAP)
            if (body.get("error") or {}).get("code") == "maxlag":
                wait = retry_after(retry, 5)
                log(f"commons api: maxlag, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if "error" in body:
                raise RuntimeError(f"commons api: {body['error']}")
            return body
        raise RuntimeError("commons api: still lagged/limited after 4 tries")


def retry_after(value: str | None, default: float) -> float:
    """A Retry-After header (seconds; the HTTP-date form is rare here) -> seconds, bounded."""
    try:
        return min(max(float(value), 1.0), 3600.0)
    except (TypeError, ValueError):
        return default


def commons_files(names: list[str], pace: ApiPace | None = None) -> dict[str, dict]:
    """File names -> parse_page() per file, API_BATCH per request."""
    pace = pace or ApiPace()
    out: dict[str, dict] = {}
    for i in range(0, len(names), API_BATCH):
        body = pace.request(names[i:i + API_BATCH])
        norm = {n["to"]: n["from"] for n in body.get("query", {}).get("normalized", [])}
        for pg in body.get("query", {}).get("pages", []):
            f = parse_page(pg)
            f["name"] = norm.get("File:" + f["name"], "File:" + f["name"]).removeprefix("File:")
            out[f["name"]] = f
        if i // API_BATCH % 200 == 0:
            log(f"commons api: {min(i + API_BATCH, len(names)):,} of {len(names):,} files")
    return out


# The working file (ADR-063, off Postgres): candidates (src) and stored pictures (pictures), title-keyed.
# `release_v1.py pictures-from` turns it into a release's doc_id-keyed pictures.sqlite.
CARD_DB = os.environ.get("CARD_IMAGES_DB", "/card/card-images.sqlite")   # writable (compose: ./card-images:/card)
SCHEMA = """
CREATE TABLE IF NOT EXISTS src (
    title TEXT NOT NULL, file TEXT NOT NULL, kind TEXT NOT NULL, ord INTEGER NOT NULL, rank INTEGER, width INTEGER,
    mime TEXT, licence TEXT, licence_url TEXT, author TEXT, usable INTEGER NOT NULL DEFAULT 0, status TEXT, height INTEGER,
    PRIMARY KEY (title, file));
CREATE INDEX IF NOT EXISTS src_title_ord ON src (title, ord);
CREATE TABLE IF NOT EXISTS pictures (
    title TEXT PRIMARY KEY, file TEXT NOT NULL, img BLOB NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
    author TEXT, licence TEXT NOT NULL, licence_url TEXT, fetched TEXT NOT NULL DEFAULT (date('now')));
"""
SRC_COLS = ("title", "file", "kind", "ord", "rank", "width", "mime", "licence", "licence_url", "author", "usable", "status", "height")


def open_db(path: str | None = None) -> sqlite3.Connection:
    """The card-images working file (created when missing). Autocommit; tx() groups writes."""
    db = sqlite3.connect(path or CARD_DB, isolation_level=None, timeout=60)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(SCHEMA)
    return db


@contextmanager
def tx(db: sqlite3.Connection):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _temp(db: sqlite3.Connection, name: str, cols: str) -> None:
    db.execute(f"DROP TABLE IF EXISTS temp.{name}")
    db.execute(f"CREATE TEMP TABLE {name} ({cols})")


def top_pages(pg, top: int) -> list[tuple]:
    """BRIDGE (until the wiki importers write staging files, Postgres exit phase 5b): the `top`
    most-read articles as (title, page_id, rank), from the build database's wiki_pop / wiki_docs.
    Titles are looked up by page_id — never a scan of the 7.8 GB wiki_docs heap, which would push
    live `serve` out of indexhost's shared ARC (2026-09-26); at 500k rows the planner prefers a hash
    join over the whole heap, so it is kept on the pkey."""
    if pg.execute("SELECT to_regclass('wiki_pop') IS NULL").fetchone()[0]:
        raise RuntimeError("wiki_pop is missing — run `search_import.py wiki-pop` (and pageviews) first")
    pg.execute("SET enable_hashjoin = off")
    pg.execute("SET enable_mergejoin = off")
    rows = pg.execute("""SELECT w.title, t.page_id, t.rank FROM (
          SELECT page_id, row_number() OVER (ORDER BY coalesce(pageviews, 0) DESC, coalesce(sitelinks, 0) DESC, page_id) AS rank
          FROM wiki_pop ORDER BY rank LIMIT %d) t
        JOIN wiki_docs w USING (page_id)""" % int(top)).fetchall()
    pg.execute("RESET enable_hashjoin")
    pg.execute("RESET enable_mergejoin")
    return rows


def cmd_meta(db, pg, top: int, lead_dump: str | None = None) -> None:
    """Candidates for the `top` most-read articles — Wikidata's image, the article's free lead
    image (page_props dump, when given), Wikidata's logo — with each file's licence, author
    and size as the Commons file page shows them. db: the working file; pg: the build database,
    read for the article ranks only (top_pages)."""
    t0 = time.time()
    expected = int(float(lit(_query(WIKIDATA, CANDIDATES_COUNT, "candidates")[0][0])))
    _temp(db, "ci_cand", "title TEXT, kind TEXT, iri TEXT, ord INTEGER")
    page = n = ord_ = seen = 0
    last = None                                       # a title's files can straddle two pages
    while True:
        rows = _query(WIKIDATA, CANDIDATES + f"\nLIMIT {PAGE} OFFSET {page * PAGE}", "candidates")
        batch = []
        for r in rows:
            if len(r) < 3 or not r[0].strip("<>").startswith(_WIKI):
                continue
            title = urllib.parse.unquote(r[0].strip("<>")[len(_WIKI):]).replace("_", " ")
            ord_ = ord_ + 1 if title == last else 0
            last = title
            batch.append((title, lit(r[1]), r[2].strip(), ord_))
            n += 1
        with tx(db):
            db.executemany("INSERT INTO ci_cand VALUES (?, ?, ?, ?)", batch)
        seen += len(rows)
        log(f"candidates page {page}: {len(rows):,} rows")
        if len(rows) < PAGE:
            break
        page += 1
        time.sleep(5)
    if not complete(seen, expected):
        raise RuntimeError(f"QLever sent {seen:,} of {expected:,} candidate rows — not replacing the candidates")
    # The card population: the `top` most-read articles. Articles without a candidate get no picture.
    _temp(db, "ci_pages", "title TEXT, page_id INTEGER, rank INTEGER")
    with tx(db):
        db.executemany("INSERT INTO ci_pages VALUES (?, ?, ?)", top_pages(pg, top))
    db.execute("CREATE INDEX temp.ci_pages_id ON ci_pages (page_id)")
    _temp(db, "ci_rank", "title TEXT PRIMARY KEY, rank INTEGER")
    db.execute("INSERT INTO ci_rank SELECT title, min(rank) FROM ci_pages GROUP BY title")
    if lead_dump:
        # the article's own free lead image: often the picture Wikidata lacks or differs from
        # (Elvis, Germany, Singapore in the 2026-09-29 ZIM sample)
        _temp(db, "ci_lead", "page_id INTEGER, iri TEXT")
        with gzip.open(lead_dump, "rt", encoding="utf-8", errors="replace") as f, tx(db):
            db.executemany("INSERT INTO ci_lead VALUES (?, ?)",
                           ((page_id, _FILEPATH + urllib.parse.quote(name)) for page_id, name in parse_page_props(f)))
        leads = db.execute("SELECT count(*) FROM ci_lead").fetchone()[0]
        db.execute("""INSERT INTO ci_cand (title, kind, iri, ord)
            SELECT p.title, 'lead', l.iri, 0 FROM ci_lead l JOIN ci_pages p USING (page_id)""")
        log(f"{leads:,} free lead images in page_props")
    # one row per file (a file that is several kinds keeps the first), KIND_ORDER, stable order
    _temp(db, "ci_top", "title TEXT, kind TEXT, iri TEXT, ord INTEGER, rank INTEGER")
    db.execute("""INSERT INTO ci_top
        SELECT title, kind, iri, row_number() OVER (PARTITION BY title ORDER BY %(k)s, ord), rank FROM (
          SELECT c.title, c.kind, c.iri, c.ord, r.rank,
                 row_number() OVER (PARTITION BY c.title, c.iri ORDER BY %(k)s, c.ord) AS one
          FROM ci_cand c JOIN ci_rank r USING (title)) WHERE one = 1""" % {"k": KIND_ORDER})
    iris = [r[0] for r in db.execute("SELECT DISTINCT iri FROM ci_top ORDER BY iri")]
    names = sorted({n for n in (file_name(x) for x in iris) if n})
    log(f"{n:,} candidates; {len(names):,} files for the top {top:,} articles")
    # licence + author as the file page shows them (Commons API extmetadata): Commons'
    # structured data lacked the creator for 269 and the licence for 194 of 894 files in the
    # 2026-09-29 trial; the file pages had both for 48 of 49 of those
    files = commons_files(names)

    why: dict[str, int] = {}
    new: list[tuple] = []
    done: set = set()
    for title, kind, iri, ord_, rank in db.execute("SELECT title, kind, iri, ord, rank FROM ci_top ORDER BY title, ord").fetchall():
        name = file_name(iri)
        # the same file under two spellings (Wikidata's encoding of an image that is also
        # the lead image): the first kind keeps it
        if (title, name or iri) in done:
            continue
        done.add((title, name or iri))
        f = files.get(name) if name else None
        lic = author = None
        if not f or f["mime"] is None:
            reason = "not on Commons"
        elif not thumb_url(name, f["width"], f["mime"]):
            reason = "format"
        else:
            lic, author = f["licence"], f["author"]
            reason = ("licence" if lic is None else
                      f"credit over {AUTHOR_MAX} characters" if lic[2] and f.get("credit_too_long") else
                      "no author for a CC BY/BY-SA credit" if lic[2] and not author else "")
        why[reason or "usable"] = why.get(reason or "usable", 0) + 1
        new.append((title, name or iri, kind, ord_, rank, f and f["width"], f and f["mime"], lic and lic[0], lic and lic[1],
                    author, int(not reason), None, f and f["height"]))
    _temp(db, "ci_new", ", ".join(SRC_COLS))
    with tx(db):
        db.executemany(f"INSERT INTO ci_new VALUES ({', '.join('?' * len(SRC_COLS))})", new)
        stored = db.execute("SELECT count(*) FROM pictures").fetchone()[0]
        # keep what the fetcher learned (404s) for files that did not change
        db.execute("UPDATE ci_new SET status = (SELECT o.status FROM src o WHERE o.title = ci_new.title AND o.file = ci_new.file)")
        db.execute("DELETE FROM src")
        db.execute(f"INSERT INTO src ({', '.join(SRC_COLS)}) SELECT {', '.join(SRC_COLS)} FROM ci_new")
        # only the credit changed (stored whole and decoded since G3, 2026-10-22): the picture keeps its bytes and takes
        # the checked credit, so the fix needs no second download
        recredited = db.execute("""UPDATE pictures SET author = (SELECT s.author FROM src s WHERE s.title = pictures.title
              AND s.file = pictures.file AND s.usable AND s.licence = pictures.licence AND s.licence_url IS pictures.licence_url)
            WHERE EXISTS (SELECT 1 FROM src s WHERE s.title = pictures.title AND s.file = pictures.file AND s.usable
              AND s.licence = pictures.licence AND s.licence_url IS pictures.licence_url AND s.author IS NOT pictures.author)""").rowcount
        # a picture whose file or licence no longer checks out leaves the card now
        gone = db.execute("""DELETE FROM pictures WHERE NOT EXISTS (
            SELECT 1 FROM src s WHERE s.title = pictures.title AND s.file = pictures.file AND s.usable
              AND s.licence = pictures.licence AND s.licence_url IS pictures.licence_url
              AND s.author IS pictures.author)""").rowcount
        if stored >= 100 and gone > stored * MAX_DROP:
            raise RuntimeError(f"refresh would drop {gone:,} of {stored:,} stored pictures (> {MAX_DROP:.0%}) — "
                               "a short read, not deletions; nothing replaced")
    titles = db.execute("SELECT count(DISTINCT CASE WHEN usable THEN title END), count(DISTINCT title) FROM src").fetchone()
    log(f"files by outcome: {', '.join(f'{k} {v:,}' for k, v in sorted(why.items(), key=lambda x: -x[1]))}")
    log(f"{titles[0]:,} of {titles[1]:,} articles have a usable picture; {gone:,} stored pictures dropped; "
        f"{recredited:,} took a corrected credit; "
        f"{(time.time() - t0) / 60:.1f} min")


def cmd_seed(db, pg) -> None:
    """ONE-SHOT BRIDGE (ADR-063, Postgres exit phase 5f): copy the build database's card_image_src and
    card_images into an empty working file, once. Delete this function after the seed has run."""
    if db.execute("SELECT count(*) FROM pictures").fetchone()[0] or db.execute("SELECT count(*) FROM src").fetchone()[0]:
        raise SystemExit("[card-images] seed: the working file already holds data — refusing to overwrite it")
    t0 = time.time()
    n_src = n_pic = 0
    with pg.transaction(), pg.cursor(name="ci_src") as cur, tx(db):     # a named cursor needs a transaction
        cur.itersize = 20000
        cur.execute(f"SELECT {', '.join(SRC_COLS)} FROM card_image_src")
        for rows in iter(lambda: cur.fetchmany(20000), []):
            db.executemany(f"INSERT INTO src VALUES ({', '.join('?' * len(SRC_COLS))})",
                           [tuple(int(v) if isinstance(v, bool) else v for v in r) for r in rows])
            n_src += len(rows)
    with pg.transaction(), pg.cursor(name="ci_pic") as cur:
        cur.itersize = 2000
        cur.execute("SELECT title, file, img, w, h, author, licence, licence_url, fetched::text FROM card_images")
        for rows in iter(lambda: cur.fetchmany(2000), []):
            with tx(db):
                db.executemany("INSERT INTO pictures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               [(r[0], r[1], bytes(r[2]), *r[3:]) for r in rows])
            n_pic += len(rows)
    want = pg.execute("SELECT (SELECT count(*) FROM card_image_src), (SELECT count(*) FROM card_images)").fetchone()
    log(f"seed: src {n_src:,} of {want[0]:,}, pictures {n_pic:,} of {want[1]:,} in {(time.time() - t0) / 60:.1f} min")
    if (n_src, n_pic) != tuple(want):
        raise SystemExit("[card-images] seed: counts differ — the file is incomplete, delete it and run again")


class Stop(Exception):
    """Wikimedia said stop (403/429/503), or the network is gone: no file is held
    responsible. With a Retry-After (429/503) the pool waits that long, slows down, and
    carries on (the robot policy: "respect the delay specified by the Retry-After header");
    otherwise the run ends and progress is kept."""

    def __init__(self, msg: str, retry: float | None = None):
        super().__init__(msg)
        self.retry = retry


# 2026-09-29 diagnostic (AWS, 1 request at a time): upload.wikimedia.org answered 429 with
# Retry-After: 11 about every 15-20 thumbnails (~7/s between them) — a per-address limit
# over a ~10 s window. So a 429 is pacing, not the end: wait it out, double the gap
# between requests, shrink it again slowly on success. The run ends on a 403, a 429
# without Retry-After or asking for more than MAX_RETRY_AFTER, or MAX_429_STREAK in a row.
MAX_RETRY_AFTER = 120.0


class FileFailing(Exception):
    """upload.wikimedia.org refuses one file's thumbnail (it keeps failing to render): that
    article's failure, recorded as "thumb failing"; the run goes on."""
MAX_429_STREAK = 20
SLOW_S, SLOW_PAUSE_S = 1.0, 5.0     # robot policy: a request over 1 s -> wait 5 s before the next


# Wikimedia's robot policy for upload.wikimedia.org: "a total concurrency of at most 2, and
# limit your total download speed to 25 Mbps (as measured over 10 second intervals)".
CONCURRENCY = 2
MAX_MBPS = 25


class Budget:
    """The 25 Mbps ceiling, shared by the workers: bytes over the last 10 s."""

    def __init__(self, mbps: float = MAX_MBPS, window: float = 10.0):
        self.cap = mbps * 1e6 / 8 * window
        self.window = window
        self.seen: collections.deque = collections.deque()
        self.lock = threading.Lock()

    def spend(self, n: int) -> None:
        with self.lock:
            now = time.monotonic()
            self.seen.append((now, n))
            while self.seen and self.seen[0][0] < now - self.window:
                self.seen.popleft()
            over = sum(b for _, b in self.seen) > self.cap
            wait = self.seen[0][0] + self.window - now if over else 0.0
        if wait > 0:
            time.sleep(wait)


def _download(url: str, budget: Budget | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(MAX_DOWNLOAD + 1)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            ra = e.headers.get("Retry-After") if e.headers else None
            try:
                why = e.read(4000).decode("utf-8", "replace")
            except Exception:
                why = ""
            why = re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>|\s+", " ", why, flags=re.S | re.I).strip()[:160]
            # "Too many thumbnail requests for failing image": a limit on THIS file, whose thumbnail
            # Wikimedia cannot render — that file's problem, not a stop signal (it ended a whole
            # 12 h run after 5 h, 2026-10-01)
            if e.code == 429 and "failing image" in why.lower():
                raise FileFailing(why) from e
            # everything Wikimedia said, for the log: the limit is not published
            raise Stop(f"HTTP {e.code} from {urllib.parse.urlsplit(url).hostname}, Retry-After {ra!r}: {why}",
                       retry_after(ra, 60) if e.code != 403 and ra else None) from e
        raise
    except (urllib.error.URLError, OSError) as e:     # the network, not the file
        raise Stop(f"network: {type(e).__name__}") from e
    if budget is not None:
        budget.spend(len(data))
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("too large")
    return data


def fetch_one(item: tuple, budget: Budget | None = None) -> tuple:
    """(title, file, width, mime) -> (title, file, status, picture). status "" = stored;
    otherwise what is wrong with THIS file (http 404, too large, unreadable). Raises Stop."""
    title, name, width, mime = item[:4]
    t = time.monotonic()
    try:
        raw = _download(thumb_url(name, width, mime), budget)
    except urllib.error.HTTPError as e:
        return title, name, f"http {e.code}", None
    except FileFailing:
        return title, name, "thumb failing", None
    except ValueError:
        return title, name, "too large", None
    finally:
        if time.monotonic() - t > SLOW_S:            # this worker's slot waits; the other keeps going
            time.sleep(SLOW_PAUSE_S)
    try:
        enc = encode(raw)
    except Exception as e:                            # a broken image (Pillow) is that file's problem only
        return title, name, type(e).__name__, None
    return title, name, "" if enc else "too large", enc


class Pacer:
    """The gap between request starts: 0 until the first 429, then doubled on every 429
    (at least 0.2 s, at most 5 s) and shrunk 2% on every success."""

    def __init__(self):
        self.gap = 0.0
        self.next_at = 0.0

    def wait(self) -> float:
        return max(0.0, self.next_at - time.monotonic())

    def started(self) -> None:
        self.next_at = time.monotonic() + self.gap

    def ok(self) -> None:
        self.gap *= 0.98

    def limited(self) -> None:
        self.gap = min(5.0, max(0.2, self.gap * 2))


def run_pool(items, emit, concurrency: int = CONCURRENCY, deadline: float | None = None,
             budget: Budget | None = None) -> str | None:
    """fetch_one over items with at most `concurrency` requests in flight; emit(result) on
    the calling thread. Returns why it stopped early (Stop / deadline), else None.
    A Stop with a Retry-After (<= MAX_RETRY_AFTER) pauses every new request that long,
    slows the pace and puts its article back at the front; one without Retry-After, a 403,
    or MAX_429_STREAK in a row ends the run (the requests in flight finish and are kept)."""
    concurrency = max(1, min(int(concurrency), CONCURRENCY))
    budget = budget or Budget()
    pace = Pacer()
    it = iter(items)
    again: collections.deque = collections.deque()    # articles a 429 bounced
    stopped = None
    streak = limited = 0
    resume_at = 0.0
    inflight: dict = {}
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        while True:
            while stopped is None and len(inflight) < concurrency and time.monotonic() >= resume_at and pace.wait() == 0:
                if deadline is not None and time.monotonic() >= deadline:
                    stopped = "time budget"
                    break
                item = again.popleft() if again else next(it, None)
                if item is None:
                    break
                pace.started()
                inflight[ex.submit(fetch_one, item, budget)] = item
            if not inflight:
                if stopped is None and (again or time.monotonic() < resume_at or pace.wait() > 0):
                    until = max(resume_at, time.monotonic() + pace.wait())
                    if deadline is not None and until >= deadline:
                        return "time budget"
                    time.sleep(max(0.0, until - time.monotonic()))
                    continue
                return stopped
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED, timeout=pace.wait() or None)
            for f in done:
                item = inflight.pop(f)
                try:
                    emit(f.result())
                    streak = 0
                    pace.ok()
                except Stop as e:
                    if e.retry is not None and e.retry <= MAX_RETRY_AFTER and streak < MAX_429_STREAK:
                        streak += 1
                        limited += 1
                        pace.limited()
                        again.append(item)
                        resume_at = max(resume_at, time.monotonic() + e.retry)
                        if limited == 1 or limited % 50 == 0:
                            log(f"rate limited ({limited} so far): waiting {e.retry:.0f}s, then 1 request per "
                                f"{pace.gap:.2f}s — {e}")
                    else:
                        log(f"stop signal: {e}")
                        stopped = stopped or str(e)


# The next picture per article: the first usable candidate in order that has not failed.
NEXT_SQL = """SELECT title, file, width, mime, height, kind FROM (
  SELECT s.*, row_number() OVER (PARTITION BY s.title ORDER BY s.ord) AS rn FROM src s
  WHERE s.usable AND s.status IS NULL AND NOT EXISTS (SELECT 1 FROM pictures c WHERE c.title = s.title))
WHERE rn = 1
ORDER BY rank, title
LIMIT :n"""
# --redo (a new BOX or MAX_BYTES): every article whose stored picture is smaller than BOX, and
# the files an earlier, smaller cap refused ("too large"); 404s and unreadable files stay out.
# ... and every article whose stored picture is not its first choice any more (a new KIND_ORDER).
# The first choice is picked BEFORE comparing, so a stored first choice never sends a runner-up.
REDO_SQL = """WITH first AS (
  SELECT title, file, width, mime, height, kind, rank FROM (
    SELECT s.*, row_number() OVER (PARTITION BY s.title ORDER BY s.ord) AS rn FROM src s
    WHERE s.usable AND (s.status IS NULL OR s.status = 'too large'))
  WHERE rn = 1)
SELECT f.title, f.file, f.width, f.mime, f.height, f.kind FROM first f
WHERE NOT EXISTS (SELECT 1 FROM pictures c
                  WHERE c.title = f.title AND c.file = f.file
                    AND (max(c.w, c.h) >= :box                                                -- full size
                         OR max(coalesce(f.width, 0), coalesce(f.height, 0)) <= max(c.w, c.h)))   -- as big as the original
ORDER BY f.rank, f.title
LIMIT :n"""


def _store(db, results: list[tuple], replace: bool = False) -> tuple[int, int]:
    """Results -> pictures (licence and author from src, never from the download) and failure
    statuses. Returns (stored, failed)."""
    _temp(db, "ci_load", "title TEXT, file TEXT, status TEXT, img BLOB, w INTEGER, h INTEGER")
    with tx(db):
        db.executemany("INSERT INTO ci_load VALUES (?, ?, ?, ?, ?, ?)",
                       [(title, name, status, enc[0] if enc else None, enc[1] if enc else None, enc[2] if enc else None)
                        for title, name, status, enc in results])
        stored = db.execute("""INSERT INTO pictures (title, file, img, w, h, author, licence, licence_url, fetched)
            SELECT l.title, l.file, l.img, l.w, l.h, s.author, s.licence, s.licence_url, date('now')
            FROM ci_load l JOIN src s ON s.title = l.title AND s.file = l.file AND s.usable
            WHERE l.status = '' AND l.img IS NOT NULL
            ON CONFLICT (title) DO """ + ("""UPDATE SET file = excluded.file, img = excluded.img, w = excluded.w,
                h = excluded.h, author = excluded.author, licence = excluded.licence,
                licence_url = excluded.licence_url, fetched = excluded.fetched
              WHERE excluded.file <> pictures.file
                 OR max(excluded.w, excluded.h) >= max(pictures.w, pictures.h)"""
                                          if replace else "NOTHING")).rowcount   # never a smaller picture
        failed = db.execute("""UPDATE src SET status = l.status FROM ci_load l
            WHERE src.title = l.title AND src.file = l.file AND l.status <> ''""").rowcount
    return stored, failed


def _report(db, got: int, failed: int, t0: float, stopped: str | None) -> None:
    size = db.execute("SELECT count(*), coalesce(sum(length(img)), 0) FROM pictures").fetchone()
    log(f"{got:,} stored, {failed:,} failed in {(time.time() - t0) / 60:.1f} min; "
        f"pictures: {size[0]:,}, {size[1] / 1e6:.1f} MB")
    if stopped:
        raise SystemExit(f"[card-images] stopped: {stopped} — progress kept; wait before running again")


def cmd_fetch(db, limit: int, concurrency: int = CONCURRENCY) -> None:
    """Thumbnails for the next `limit` articles, most-read first, straight into the working file.
    The AWS path is todo -> download -> load."""
    t0 = time.time()
    todo = db.execute(NEXT_SQL, {"n": limit}).fetchall()
    log(f"{len(todo):,} articles to fetch, {min(concurrency, CONCURRENCY)} at a time")
    got = failed = 0
    batch: list[tuple] = []

    def emit(r):
        nonlocal got, failed
        batch.append(r)
        if len(batch) >= 200:
            g, f = _store(db, batch)
            got, failed = got + g, failed + f
            batch.clear()
            log(f"{got:,} stored, {failed:,} failed")
    stopped = run_pool(todo, emit, concurrency)
    if batch:
        g, f = _store(db, batch)
        got, failed = got + g, failed + f
    _report(db, got, failed, t0, stopped)


def cmd_reorder(db) -> None:
    """Re-rank every article's candidates by the current KIND_ORDER, keeping the order within
    a kind — a new preference without re-reading Wikidata and Commons (cmd_meta)."""
    with tx(db):
        n = db.execute("""UPDATE src SET ord = r.o FROM (
              SELECT title, file, row_number() OVER (PARTITION BY title ORDER BY %s, ord) AS o FROM src) r
            WHERE r.title = src.title AND r.file = src.file AND src.ord <> r.o""" % KIND_ORDER).rowcount
    first = db.execute("""SELECT kind, count(*) FROM (
                            SELECT kind, row_number() OVER (PARTITION BY title ORDER BY ord) AS rn FROM src WHERE usable)
                          WHERE rn = 1 GROUP BY kind ORDER BY 2 DESC""").fetchall()
    log(f"reorder: {n:,} candidates moved; first choice by kind: {', '.join(f'{k} {c:,}' for k, c in first)}")


def cmd_todo(db, limit: int, redo: bool = False) -> None:
    """The next `limit` articles to fetch -> gzip TSV (title, file, width, mime, height, kind)
    on stdout, for the build instance. redo: re-fetch pictures stored smaller than BOX and
    files an earlier cap refused (REDO_SQL). Log lines go to stderr."""
    rows = db.execute(REDO_SQL if redo else NEXT_SQL, {"n": limit, "box": BOX} if redo else {"n": limit}).fetchall()
    with gzip.open(sys.stdout.buffer, "wt", encoding="utf-8", newline="\n") as out:
        for row in rows:
            write_todo(out, row)
    print(f"[card-images] todo: {len(rows):,} articles", file=sys.stderr, flush=True)


def _tsv(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _untsv(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n"}.get(m.group(1), m.group(1)), v)


def write_todo(out, row: tuple) -> None:
    """(title, file, width, mime, height, kind) -> one todo line."""
    title, name, width, mime, height, kind = (tuple(row) + (None, None))[:6]
    out.write("\t".join(_tsv(v) for v in (title, name, width or "", mime or "", height or "", kind or "")) + "\n")


def read_todo(path: str):
    """A todo file -> (title, file, width, mime, height, kind); 4-column files (before height
    and kind) read with both None."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            p = [_untsv(x) for x in line.rstrip("\n").split("\t")]
            if len(p) in (4, 6):
                p += [""] * (6 - len(p))
                yield (p[0], p[1], int(p[2]) if p[2] else None, p[3] or None,
                       int(p[4]) if p[4] else None, p[5] or None)


def cmd_download(todo: str, out: str, deadline_min: float | None = None, concurrency: int = CONCURRENCY) -> int:
    """No database: todo file -> results file (gzip TSV: title, file, status, w, h, base64
    WebP). Exit code 0 = all done, 3 = stopped early (stop signal or time budget) — what it
    has is in the file either way."""
    t0 = time.time()
    deadline = time.monotonic() + deadline_min * 60 if deadline_min else None
    n = {"stored": 0, "failed": 0}
    with gzip.open(out, "at", encoding="utf-8", newline="\n") as f:
        def emit(r):
            title, name, status, enc = r
            n["failed" if status else "stored"] += 1
            f.write("\t".join((_tsv(title), _tsv(name), status, str(enc[1]) if enc else "", str(enc[2]) if enc else "",
                               base64.b64encode(enc[0]).decode("ascii") if enc else "")) + "\n")
            if sum(n.values()) % 5000 == 0:
                log(f"{n['stored']:,} stored, {n['failed']:,} failed, {(time.time() - t0) / 60:.0f} min")
        stopped = run_pool(read_todo(todo), emit, concurrency, deadline)
    log(f"download: {n['stored']:,} stored, {n['failed']:,} failed in {(time.time() - t0) / 60:.1f} min"
        + (f"; stopped: {stopped}" if stopped else ""))
    return 3 if stopped else 0


def cmd_load(db, path: str, replace: bool = False) -> None:
    """A results file from `download`/`zim` -> pictures + failure statuses. replace: a
    picture at least as large as the stored one replaces it (a --redo run)."""
    t0 = time.time()
    got = failed = bad = 0
    batch: list[tuple] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            try:
                if len(p) != 6:
                    raise ValueError("columns")
                enc = (base64.b64decode(p[5], validate=True), int(p[3]), int(p[4])) if p[5] else None
            except ValueError:                        # a torn line (a copy taken mid-write): skip it, never abort
                bad += 1
                continue
            batch.append((_untsv(p[0]), _untsv(p[1]), p[2], enc))
            if len(batch) >= 2000:
                g, fl = _store(db, batch, replace)
                got, failed = got + g, failed + fl
                batch.clear()
    if batch:
        g, fl = _store(db, batch, replace)
        got, failed = got + g, failed + fl
    if bad:
        log(f"load: {bad:,} malformed lines skipped")
    _report(db, got, failed, t0, None)


_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z-]+)="([^"]*)"')
ZIM_MIN_PX = 120      # a ZIM copy smaller than this (a gallery thumb) is left to the download


def zim_match(html: str, width: int | None, height: int | None) -> str | None:
    """The one ZIM asset in an article whose ORIGINAL file is exactly width x height
    (data-file-width/-height, which Kiwix keeps); None when there is none, or more than one
    distinct asset matches — a same-sized different file must never be credited as ours."""
    if not width or not height:
        return None
    found = set()
    for tag in _IMG_TAG.findall(html):
        a = dict(_ATTR.findall(tag))
        if a.get("data-file-width") == str(width) and a.get("data-file-height") == str(height):
            m = re.search(r"(_assets_/[^\"?#]+)", a.get("src", ""))
            if m:
                found.add(urllib.parse.unquote(m.group(1)))
    return found.pop() if len(found) == 1 else None


def _zim_html(z, title: str) -> str | None:
    path = title.replace(" ", "_")
    if not z.has_entry_by_path(path):
        return None
    e = z.get_entry_by_path(path)
    for _ in range(5):
        if not e.is_redirect:
            break
        e = e.get_redirect_entry()
    if e.is_redirect:                                  # a chain longer than 5: not an article
        return None
    item = e.get_item()
    return bytes(item.content).decode("utf-8", "replace") if item.mimetype.startswith("text/html") else None


def cmd_zim(todo: str, zim, out: str, leftover: str) -> tuple[int, int]:
    """Pictures from a Kiwix Wikipedia ZIM (maxi) instead of upload.wikimedia.org.

    Only an article's LEAD image (page_image_free: the file MediaWiki itself picked from the
    article's lead) is taken from the ZIM, and only when exactly one image in that article
    has the file's exact original size — the ZIM names images by a hash, so size is the
    link back to the Commons file whose licence and author we checked. Everything else goes
    to `leftover` (a todo file for `download`). `zim` is a path or an open libzim Archive.
    Returns (stored, left)."""
    if isinstance(zim, str):
        from libzim.reader import Archive
        zim = Archive(zim)
    t0 = time.time()
    got = left = 0
    why: dict[str, int] = {}
    with gzip.open(out, "at", encoding="utf-8", newline="\n") as res, \
            gzip.open(leftover, "wt", encoding="utf-8", newline="\n") as rest:
        for row in read_todo(todo):
            title, name, width, mime, height, kind = row
            reason = "not a lead image"
            if kind == "lead":
                html = _zim_html(zim, title)
                asset = zim_match(html, width, height) if html else None
                reason = "no article" if html is None else "no unique match" if asset is None else ""
                if asset:
                    try:
                        enc = encode(bytes(zim.get_entry_by_path(asset).get_item().content))
                    except Exception as e:                # unreadable or missing asset: download it instead
                        enc, reason = None, type(e).__name__
                    if enc and max(enc[1], enc[2]) < ZIM_MIN_PX and max(width, height) > ZIM_MIN_PX:
                        enc, reason = None, "too small in the ZIM"
                    if enc:
                        res.write("\t".join((_tsv(title), _tsv(name), "", str(enc[1]), str(enc[2]),
                                              base64.b64encode(enc[0]).decode("ascii"))) + "\n")
                        got += 1
                        continue
                    reason = reason or "too large"
            why[reason] = why.get(reason, 0) + 1
            write_todo(rest, row)
            left += 1
            if (got + left) % 20000 == 0:
                log(f"zim: {got:,} from the ZIM, {left:,} left, {(time.time() - t0) / 60:.0f} min")
    log(f"zim: {got:,} from the ZIM, {left:,} left for download ({', '.join(f'{k} {v:,}' for k, v in sorted(why.items(), key=lambda x: -x[1]))}); "
        f"{(time.time() - t0) / 60:.1f} min")
    return got, left


if __name__ == "__main__":                            # the build instance: no database, no psycopg
    import argparse
    ap = argparse.ArgumentParser(description="card pictures on a build instance (no database)")
    ap.add_argument("phase", choices=("download", "zim"))
    ap.add_argument("--todo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--deadline-min", type=float, default=None)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY, help="download: 1 or 2 (the policy maximum)")
    ap.add_argument("--zim", help="zim: the Kiwix wikipedia_en_all_maxi file")
    ap.add_argument("--leftover", help="zim: todo file for what the ZIM could not supply")
    a = ap.parse_args()
    if a.phase == "zim":
        if not a.zim or not a.leftover:
            ap.error("zim needs --zim and --leftover")
        cmd_zim(a.todo, a.zim, a.out, a.leftover)
        sys.exit(0)
    sys.exit(cmd_download(a.todo, a.out, a.deadline_min, a.concurrency))
