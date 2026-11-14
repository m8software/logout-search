"""Anchor text from Common Crawl WAT files (2026-09-24).

What other sites call a site is one of the strongest signals there is: "check your
balance" -> chase.com, "the verge" -> theverge.com. The WAT files carry every page's
links with their text. Measured on one file of CC-MAIN-2026-39: 66k pages, 205k
cross-site links with text, 11 s CPU — a whole crawl (100,000 files) is ~$5-6 of
spot time, so this reads a SAMPLE, from inside us-east-1 (S3, free).

    anchors scan --paths P --sites S --out DIR [--procs 2]   per WAT file: unique
        (target site, anchor, linking site) for targets we index -> DIR/NNNNN.tsv.gz,
        and each site's self-description from its OWN pages -> DIR/NNNNN.meta.tsv.gz:
        site_name (og:site_name), twitter (twitter:site), feed (RSS/Atom), and one
        social profile per platform linked from the site (instagram:nike …)
    anchors aggregate --dir DIR --out anchors.tsv.gz         DuckDB: per (target,
        anchor) the number of DISTINCT linking sites (>= 2), top 100 anchors per target;
        per (site, kind) the value seen in the most files -> site_meta.tsv.gz
    search_import.py anchors-load --file anchors.tsv.gz       -> anchors, anchor_docs

Counting linking SITES, not links, is what keeps one site's 10,000 footer links from
defining another site. Social / link-hub targets and generic anchors ("click here",
"read more", "home") are dropped.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

# Anchors that say nothing about the target.
GENERIC = frozenset([
    "here", "click", "click here", "this", "link", "read more", "more", "learn more", "home", "website",
    "web site", "homepage", "official website", "official site", "visit site", "visit website", "source",
    "via", "go", "continue", "continue reading", "download", "view", "see more", "details", "info",
    "more info", "page", "index", "back", "top", "next", "previous", "prev", "skip to content", "menu",
    "login", "log in", "sign in", "sign up", "register", "subscribe", "share", "tweet", "email", "contact",
    "contact us", "about", "about us", "privacy", "privacy policy", "terms", "terms of use", "cookies",
    "cookie policy", "facebook", "twitter", "instagram", "youtube", "linkedin", "pinterest"])
SOCIAL = frozenset("""facebook.com fb.com instagram.com twitter.com x.com t.co youtube.com youtu.be linkedin.com
lnkd.in tiktok.com pinterest.com linktr.ee threads.net whatsapp.com wa.me telegram.me t.me
google.com goo.gl bit.ly tinyurl.com""".split())
_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^\w&'+.-]+", re.U)


def log(msg: str) -> None:
    print(f"[anchors] {msg}", flush=True)


def norm_anchor(text: str) -> str | None:
    """'  Check your   BALANCE »' -> 'check your balance'; None when it says nothing
    about the target (empty, generic, a bare URL, too long)."""
    t = _WS.sub(" ", _NONWORD.sub(" ", (text or "").lower())).strip(" .-'&+")
    if not (2 <= len(t) <= 60) or t in GENERIC:
        return None
    if t.startswith(("http", "www.")) or t.count(" ") > 7:
        return None
    return t


def _host(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


# Hosting platforms whose pages belong to thousands of different people: a user's
# og:site_name ("DigiKat" on github.io) is not the platform's name.
MULTI_TENANT = frozenset("""github.io blogspot.com wordpress.com tumblr.com wixsite.com weebly.com medium.com
substack.com netlify.app vercel.app pages.dev herokuapp.com gitbook.io notion.site webflow.io
squarespace.com jimdofree.com carrd.co neocities.org itch.io bandcamp.com""".split())

# Where a site's own "follow us" links point: platform -> host.
PROFILE_HOSTS = {"instagram.com": "instagram", "facebook.com": "facebook", "twitter.com": "x", "x.com": "x",
                 "youtube.com": "youtube", "linkedin.com": "linkedin", "tiktok.com": "tiktok",
                 "pinterest.com": "pinterest", "github.com": "github", "threads.net": "threads"}
_PROFILE_SKIP = frozenset("share sharer intent home login p reel watch search hashtag explore embed plugins "
                          "dialog tr events groups pages photo.php story.php".split())


def profile_of(url: str) -> str | None:
    """'https://www.instagram.com/nike/?hl=en' -> 'instagram:nike'; share / intent /
    post links -> None. LinkedIn keeps its company/in prefix."""
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    platform = PROFILE_HOSTS.get(_host(url))
    if not platform:
        return None
    parts = [p for p in u.path.split("/") if p]
    if not parts or parts[0].lower() in _PROFILE_SKIP:
        return None
    if platform == "linkedin":
        return f"linkedin:{parts[0]}/{parts[1]}" if len(parts) >= 2 and parts[0] in ("company", "in") else None
    if platform == "youtube":
        h = parts[0] if parts[0].startswith("@") else ("/".join(parts[:2]) if parts[0] in ("channel", "c", "user") and len(parts) > 1 else None)
        return f"youtube:{h}" if h else None
    h = parts[0].lstrip("@")
    if h.lower().endswith((".php", ".html", ".htm")):
        return None                                   # sharer.php, share.html: buttons, not profiles
    return f"{platform}:{h}" if re.fullmatch(r"[A-Za-z0-9._-]{2,40}", h) else None


def meta_of(env: dict, src: str, page_url: str) -> set[tuple[str, str, str]]:
    """A site's description of itself, from one of its own pages: (site, kind, value)."""
    html = env.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {}).get("HTML-Metadata", {})
    head = html.get("Head", {}) or {}
    out = set()
    for m in head.get("Metas", []) or []:
        key = (m.get("property") or m.get("name") or "").lower()
        val = _WS.sub(" ", m.get("content") or "").strip()
        if key == "og:site_name" and 2 <= len(val) <= 80:
            out.add((src, "site_name", val))
        elif key == "twitter:site" and re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", val):
            out.add((src, "twitter", val.lstrip("@").lower()))
    for l in head.get("Link", []) or []:
        typ = (l.get("type") or "").lower()
        if "alternate" in (l.get("rel") or "") and typ in ("application/rss+xml", "application/atom+xml"):
            feed = urljoin(page_url, l.get("url") or "")
            if feed.startswith("http") and len(feed) <= 300:
                out.add((src, "feed", feed))
    for l in html.get("Links", []) or []:
        prof = profile_of(l.get("url") or "")
        if prof:
            out.add((src, "profile", prof))
    return out


# How-to demand, taken from the same WAT pass (2026-10-12): a page title that says
# "how to …" (or "how do I …"), from "how" on, lower-cased — the normalisation into topics
# (apps/search/tools/howto/topics.py) runs at home, so the title keeps its site suffix and filler.
# The same opening pattern as topics._HOWTO; only the match is kept, so no title text that
# is not a how-to leaves the instance.
_HOWTO = re.compile(r"\bhow (?:to|do (?:i|you|we)|can (?:i|you|we)|should (?:i|you)|does one) \S.{1,140}", re.I)


def howto_of(env: dict) -> str | None:
    """'How to Tie a Tie (with Pictures) - wikiHow' -> 'how to tie a tie (with pictures) - wikihow';
    None for a page whose title is not a how-to."""
    head = (env.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {})
            .get("HTML-Metadata", {}).get("Head")) or {}
    title = head.get("Title")
    if not isinstance(title, str) or "how" not in title.lower():
        return None
    m = _HOWTO.search(_WS.sub(" ", title).strip())
    return m.group(0).lower().rstrip(" .") if m else None


def anchors_of(line: bytes, registrable, sites: set, meta: set | None = None,
               howto: set | None = None) -> set[tuple[str, str, str]]:
    """One WAT JSON record -> {(target domain, anchor, source domain)} for links from one
    site to ANOTHER site that we index. With `meta`, the page's self-description is added
    to it (meta_of) when the page's own site is one we index. With `howto`, a how-to page
    title is added to it as (title, page's site) — any site, indexed or not."""
    try:
        env = json.loads(line)["Envelope"]
    except (ValueError, KeyError, TypeError):
        return set()
    page_url = env.get("WARC-Header-Metadata", {}).get("WARC-Target-URI", "")
    src = registrable(_host(page_url))
    if meta is not None and src in sites and src not in MULTI_TENANT:
        meta |= meta_of(env, src, page_url)
    if howto is not None and src:
        t = howto_of(env)
        if t:
            howto.add((t, src))
    links = (env.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {})
             .get("HTML-Metadata", {}).get("Links")) or []
    out = set()
    for link in links:
        if link.get("path") != "A@/href":
            continue
        url = link.get("url") or ""
        if not url.startswith("http"):
            continue
        tgt = registrable(_host(url))
        if not tgt or tgt == src or tgt in SOCIAL or tgt in MULTI_TENANT or tgt not in sites:
            continue
        a = norm_anchor(link.get("text") or "")
        if a:
            out.add((tgt, a, src))
    return out


def scan_file(key: str, out_path: str, registrable, sites: set, s3) -> int:
    body = s3.get_object(Bucket="commoncrawl", Key=key)["Body"]
    seen: set = set()
    meta: set = set()
    howto: set = set()
    with gzip.GzipFile(fileobj=body) as f:
        for line in f:
            if line.startswith(b"{"):
                seen |= anchors_of(line, registrable, sites, meta, howto)
    # NNNNN.howto.tsv.gz (title, site) is a third file per WAT file; the aggregate / shard
    # globs ("*[0-9].tsv.gz", "*.meta.tsv.gz") do not match it, so the anchor rows are as before.
    for path, rows in ((out_path.replace(".tsv.gz", ".howto.tsv.gz"), howto),
                       (out_path.replace(".tsv.gz", ".meta.tsv.gz"), meta), (out_path, seen)):
        tmp = path + ".part"
        with gzip.open(tmp, "wt", encoding="utf-8") as g:
            for row in rows:
                g.write(("\t".join(row) + "\n").replace("\r", " "))
        os.replace(tmp, path)                        # the anchors file last: its presence = done
    return len(seen)


def _worker(args) -> None:
    keys, out_dir, sites_file, part = args
    import boto3
    from botocore.config import Config
    from functools import lru_cache
    from search_import import registrable as _registrable
    # 41 s/file on AWS vs 14.5 s here: tldextract per link. Hosts repeat heavily.
    registrable = lru_cache(maxsize=500_000)(_registrable)
    sites = {l.split("\t")[0].strip() for l in open(sites_file) if l.strip()}
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
    t0 = time.time()
    for i, key in enumerate(keys, 1):
        out = os.path.join(out_dir, os.path.basename(key).replace(".warc.wat.gz", ".tsv.gz"))
        if os.path.exists(out):
            continue                                  # resumable
        try:
            n = scan_file(key, out, registrable, sites, s3)
        except Exception as e:                        # one bad file never stops the scan
            log(f"[part {part}] {key}: {type(e).__name__}: {e}")
            continue
        if i % 25 == 0:
            log(f"[part {part}] {i}/{len(keys)} files, last {n:,} anchors, {(time.time() - t0) / i:.1f} s/file")


def cmd_scan(paths_file: str, sites_file: str, out_dir: str, procs: int = 2) -> None:
    from multiprocessing import Pool
    keys = [l.strip() for l in open(paths_file) if l.strip()]
    os.makedirs(out_dir, exist_ok=True)
    log(f"{len(keys):,} WAT files over {procs} processes")
    parts = [(keys[i::procs], out_dir, sites_file, i + 1) for i in range(procs)]
    with Pool(procs) as pool:
        pool.map(_worker, parts)
    log("scan done")


def _csv(glob: str, cols: tuple) -> str:
    """A DuckDB read of the scan's tab-separated files."""
    return (f"read_csv('{glob}', delim='\\t', header=false, quote='', escape='', "
            "columns={" + ", ".join(f"'{c}': 'VARCHAR'" for c in cols) + "})")


TRIPLE = ("target", "anchor", "source")
META_COLS = ("site", "kind", "value")

AGG_SQL = """
COPY (
  SELECT target, anchor, sources FROM (
    SELECT target, anchor, count(DISTINCT source) AS sources,
           row_number() OVER (PARTITION BY target ORDER BY count(DISTINCT source) DESC, anchor) AS rn
    FROM {src}
    GROUP BY target, anchor
    HAVING count(DISTINCT source) >= 2)
  WHERE rn <= 100
  ORDER BY target, sources DESC
) TO '{out}' (DELIMITER '\\t', HEADER false, COMPRESSION gzip)
"""


# Per (site, kind) the values seen in the most WAT files (one file = one vote): the name
# and X handle a site gives itself most often, its top 3 feeds, and its most-linked
# profile on each platform.
META_SQL = """
COPY (
  WITH v AS (
    SELECT site, kind, value, {files} AS files,
           CASE WHEN kind = 'profile' THEN split_part(value, ':', 1) ELSE kind END AS slot
    FROM {src}
    GROUP BY site, kind, value)
  SELECT site, kind, value, files FROM (
    SELECT *, row_number() OVER (PARTITION BY site, slot ORDER BY files DESC, value) AS rn FROM v)
  WHERE rn <= CASE WHEN kind = 'feed' THEN 3 ELSE 1 END
  ORDER BY site, kind
) TO '{out}' (DELIMITER '\\t', HEADER false, COMPRESSION gzip)
"""

# How many distinct sites link to each site: how established it is, whatever it is
# called (search-api's in-link signal, 2026-09-26).
INLINKS_SQL = """
COPY (SELECT target, count(DISTINCT source) AS sources FROM {src}
      GROUP BY target HAVING count(DISTINCT source) >= 2 ORDER BY target)
TO '{out}' (DELIMITER '\\t', HEADER false, COMPRESSION gzip)
"""

# Several instances scan different WAT files (run.sh --parallel): each ships its scan as
# de-duplicated (target, anchor, source) triples and per-file meta votes, and the counts
# are made once over the union at home — one linking site seen by two instances must
# count once (2026-09-26).
SHARD_SQL = "COPY (SELECT DISTINCT target, anchor, source FROM {src}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)"
SHARD_META_SQL = ("COPY (SELECT site, kind, value, count(*) AS files FROM {src} GROUP BY site, kind, value) "
                  "TO '{out}' (FORMAT parquet, COMPRESSION zstd)")


def _duck(mem: str):
    import duckdb
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{mem}'")
    con.execute("SET temp_directory = '/tmp/duckdb-spill'")
    con.execute("SET preserve_insertion_order = false")
    return con


def cmd_aggregate(in_dir: str, out: str, mem: str = "2500MB") -> None:
    t0 = time.time()
    con = _duck(mem)
    here = os.path.dirname(out) or "."
    # scan files are named after their WAT file ("CC-MAIN-…-00123.tsv.gz"): any name ending
    # in a digit, which leaves out *.meta.tsv.gz. "0*[0-9]" matched none of them and a
    # 9-hour scan was lost (2026-09-25).
    trip = _csv(os.path.join(in_dir, "*[0-9].tsv.gz"), TRIPLE)
    con.execute(AGG_SQL.format(src=trip, out=out))
    con.execute(INLINKS_SQL.format(src=trip, out=os.path.join(here, "inlinks.tsv.gz")))
    con.execute(META_SQL.format(src=_csv(os.path.join(in_dir, "*.meta.tsv.gz"), META_COLS), files="count(*)",
                                out=os.path.join(here, "site_meta.tsv.gz")))
    log(f"aggregated -> {out} in {(time.time() - t0) / 60:.1f} min")


def cmd_shard(in_dir: str, out_dir: str, mem: str = "2500MB") -> None:
    """One instance's scan -> anchors.parquet (distinct triples) + meta.parquet (votes)."""
    t0 = time.time()
    con = _duck(mem)
    os.makedirs(out_dir, exist_ok=True)
    con.execute(SHARD_SQL.format(src=_csv(os.path.join(in_dir, "*[0-9].tsv.gz"), TRIPLE),
                                 out=os.path.join(out_dir, "anchors.parquet")))
    con.execute(SHARD_META_SQL.format(src=_csv(os.path.join(in_dir, "*.meta.tsv.gz"), META_COLS),
                                      out=os.path.join(out_dir, "meta.parquet")))
    log(f"shard -> {out_dir} in {(time.time() - t0) / 60:.1f} min")


def cmd_merge(shards: list[str], out: str, mem: str = "2500MB") -> None:
    """Shards (dirs holding anchors.parquet + meta.parquet) -> anchors.tsv.gz, inlinks.tsv.gz
    and site_meta.tsv.gz beside `out`: what cmd_aggregate makes from one scan."""
    t0 = time.time()
    con = _duck(mem)
    here = os.path.dirname(out) or "."
    trip = "[" + ", ".join(f"'{os.path.join(d, 'anchors.parquet')}'" for d in shards) + "]"
    meta = "[" + ", ".join(f"'{os.path.join(d, 'meta.parquet')}'" for d in shards) + "]"
    con.execute(f"CREATE VIEW t AS SELECT DISTINCT target, anchor, source FROM read_parquet({trip})")
    con.execute(f"CREATE VIEW m AS SELECT site, kind, value, files FROM read_parquet({meta})")
    con.execute(AGG_SQL.format(src="t", out=out))
    con.execute(INLINKS_SQL.format(src="t", out=os.path.join(here, "inlinks.tsv.gz")))
    con.execute(META_SQL.format(src="m", files="sum(files)", out=os.path.join(here, "site_meta.tsv.gz")))
    log(f"merged {len(shards)} shards -> {out}, inlinks.tsv.gz, site_meta.tsv.gz in {(time.time() - t0) / 60:.1f} min")


def cmd_load(conn, path: str) -> None:
    """anchors.tsv.gz -> anchors (domain, anchor, sources) and anchor_docs (domain,
    anchors text, sources) with a BM25 index: search-api's anchor list."""
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS anchors_new")
    conn.execute("CREATE TABLE anchors_new (domain text, anchor text, sources integer)")
    n = 0
    with conn.transaction(), conn.cursor().copy("COPY anchors_new (domain, anchor, sources) FROM STDIN") as cp:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) == 3 and p[2].isdigit():
                    cp.write_row((p[0], p[1], int(p[2])))
                    n += 1
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS anchors")
        conn.execute("ALTER TABLE anchors_new RENAME TO anchors")
        conn.execute("CREATE INDEX anchors_domain ON anchors (domain)")
        conn.execute("CREATE INDEX anchors_anchor ON anchors (anchor)")      # search-api's exact-phrase answer
        conn.execute("DROP TABLE IF EXISTS anchor_docs")
        # Each anchor repeated by log2(sources) so a phrase 200 sites use outweighs one
        # two sites use, without letting the biggest drown everything (BM25 saturates).
        conn.execute("""CREATE TABLE anchor_docs AS
            SELECT domain, string_agg(repeat(anchor || ' . ', greatest(1, least(8, floor(ln(sources) / ln(2))::int))), ' ')
                   AS body, sum(sources)::int AS sources
            FROM anchors GROUP BY domain""")
        conn.execute("ALTER TABLE anchor_docs ADD PRIMARY KEY (domain)")
        conn.execute("CREATE INDEX anchor_docs_bm25 ON anchor_docs USING bm25 (body) WITH (text_config = 'english')")
    k = conn.execute("SELECT count(*), count(DISTINCT domain) FROM anchors").fetchone()
    log(f"{n:,} rows -> {k[0]:,} anchors on {k[1]:,} sites in {(time.time() - t0) / 60:.1f} min")


def cmd_load_inlinks(conn, path: str) -> None:
    """inlinks.tsv.gz -> site_inlinks (domain, sources): distinct sites linking to each site."""
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS site_inlinks_new")
    conn.execute("CREATE TABLE site_inlinks_new (domain text PRIMARY KEY, sources integer)")
    n = 0
    with conn.transaction(), conn.cursor().copy("COPY site_inlinks_new (domain, sources) FROM STDIN") as cp:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) == 2 and p[1].isdigit() and p[0]:
                    cp.write_row((p[0], int(p[1])))
                    n += 1
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS site_inlinks")
        conn.execute("ALTER TABLE site_inlinks_new RENAME TO site_inlinks")
    log(f"site_inlinks: {n:,} sites in {(time.time() - t0) / 60:.1f} min")


META_KINDS = ("site_name", "twitter", "feed", "profile")


def cmd_load_meta(conn, path: str) -> None:
    """site_meta.tsv.gz -> site_meta (site, kind, value, files): the name, X handle,
    feeds and profiles each site gives itself on its own pages (cmd_aggregate's votes)."""
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS site_meta_new")
    conn.execute("CREATE TABLE site_meta_new (site text, kind text, value text, files integer)")
    n = 0
    with conn.transaction(), conn.cursor().copy("COPY site_meta_new (site, kind, value, files) FROM STDIN") as cp:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) == 4 and p[1] in META_KINDS and p[3].isdigit() and p[2].strip():
                    cp.write_row((p[0], p[1], p[2].replace("\x00", "").strip(), int(p[3])))
                    n += 1
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS site_meta")
        conn.execute("ALTER TABLE site_meta_new RENAME TO site_meta")
        conn.execute("CREATE INDEX site_meta_site ON site_meta (site, kind)")
    log(f"site_meta: {n:,} rows in {(time.time() - t0) / 60:.1f} min")


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--paths", required=True); s.add_argument("--sites", required=True)
    s.add_argument("--out", required=True); s.add_argument("--procs", type=int, default=2)
    s = sub.add_parser("aggregate")
    s.add_argument("--dir", required=True); s.add_argument("--out", required=True)
    s.add_argument("--mem", default="2500MB")
    s = sub.add_parser("shard", help="one instance's scan -> distinct triples + meta votes (parquet)")
    s.add_argument("--dir", required=True); s.add_argument("--out", required=True)
    s.add_argument("--mem", default="2500MB")
    s = sub.add_parser("merge", help="shards -> anchors.tsv.gz + inlinks.tsv.gz + site_meta.tsv.gz")
    s.add_argument("--shards", nargs="+", required=True); s.add_argument("--out", required=True)
    s.add_argument("--mem", default="2500MB")
    a = ap.parse_args(argv)
    if a.cmd == "scan":
        cmd_scan(a.paths, a.sites, a.out, a.procs)
    elif a.cmd == "shard":
        cmd_shard(a.dir, a.out, a.mem)
    elif a.cmd == "merge":
        cmd_merge(a.shards, a.out, a.mem)
    else:
        cmd_aggregate(a.dir, a.out, a.mem)


if __name__ == "__main__":
    main(sys.argv[1:])
