#!/usr/bin/env python3
"""Accepted alternatives for the open sets' answer keys (epic): every official website (Wikidata
P856) of the entity a row was generated from. No LLM, no hand edits, no database: Wikidata (CC0) through QLever.

    tools/sq-accept-keys.py [--out tools/evalsets/open/accept] [--cache ~/.cache/logout/sq-accept] [--sets a,b]
                            [--offline]

Owner decision 2026-10-13: for a query that names a thing, its own official site ranking above its article
is a CORRECT result. So a row's key accepts, beside its expected answer (which stays the primary, untouched):

  category wiki   (wiki-redirect, typo-wiki)                 every official site of the expected article's item
  category site   (site-nav, typo-site, head-brand,          every OTHER official site of the same item (a second
                   own-title, maker-product)                 ending, a renamed domain, a value the builder's
                                                             front-page rule left out)

Not the reverse: a row that expects a site does not accept the entity's article. account rows name an account and
place rows are OpenStreetMap features with no Wikidata item: neither gets alternatives here.

All P856 statements of the item count, of every rank but deprecated (Florence: comune.fi.it preferred,
comune.firenze.it normal — both are the city's). A value becomes one token:

  site:<host>        the value is a front page: no path, or only landing segments (a language code, index.html,
                     home, default.aspx — open-evalsets.own_domain's rule, also chained: /en/home/). A result on
                     that host or a subdomain of it is a hit. www. is dropped; the scheme never counts.
  url:<host/path>    the value is a page inside a host. A result at that URL or under it is a hit. A page
                     named by a query string (books.google.com/books?id=…) gives no token: the scorer compares
                     paths, and the path alone would be the whole catalogue.

Tokens the primary key already covers (same host, or a subdomain of an expected domain) are left out.

Which item a row means:
  entity is a QID            (site-nav / typo-site article:no)   that item
  category wiki              the item of the English article titled as the row expects
  otherwise                  the items whose English title has the row's entity key (norm_key of the title). One item:
                             that one. Several: those with an official site on an expected domain. None: no row.

Output: <out>/<set>.accept.tsv — query, entity, qid, accept ("|"-joined tokens, byte order) — one line per set row
that gains at least one token, in the set's order; and <out>/BUILD.json (when the data was read, counts per set).
The set files themselves are not written: expected answers, strata and the dev/test split do not move. tools/
sq_common.py reads the sidecar; tools/eval-queries.py accept_hit() is the match rule.

QLever, politely (importer/wikilinks.py's pattern): a COUNT first, pages of 500,000 checked against it, one request
at a time 5 s apart, 429/503 waited out, every answer cached under --cache. A rerun from the cache writes the same
files; --offline refuses to ask. Delete the cache to refresh from a newer Wikidata.

DEV TOOLING: stdlib only, never imported by apps/search/serve.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

QLEVER = "https://qlever.dev/api/wikidata"
USER_AGENT = "logout-search-evalsets/1 (https://search.logout.cloud)"
PAGE = 500_000
BATCH = 200                      # QIDs per VALUES request
PAUSE = 5                        # seconds between requests
PREFIXES = """PREFIX p: <http://www.wikidata.org/prop/> PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX wd: <http://www.wikidata.org/entity/> PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
"""
# every P856 statement of everything with an English article; the rank travels and deprecated is dropped here
# (FILTER(?rank != wikibase:DeprecatedRank) answered 53,333 of 893,793 rows on 2026-10-13: not trusted)
ARTICLE_BODY = ("?item p:P856 ?st . ?st ps:P856 ?site ; wikibase:rank ?rank . "
                "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .")
ARTICLE_COUNT = PREFIXES + "SELECT (COUNT(*) AS ?n) WHERE { " + ARTICLE_BODY + " }"
ARTICLE_PAGE = PREFIXES + "SELECT ?item ?article ?site ?rank WHERE { " + ARTICLE_BODY + " } ORDER BY ?item ?site ?rank LIMIT %d OFFSET %d"
ITEMS_QUERY = PREFIXES + "SELECT ?item ?site ?rank WHERE { VALUES ?item { %s } ?item p:P856 ?st . ?st ps:P856 ?site ; wikibase:rank ?rank }"
SITE_SETS = ("site-nav", "typo-site", "head-brand", "own-title", "maker-product")
WIKI_SETS = ("wiki-redirect", "typo-wiki")
_QID = re.compile(r"^Q\d+$")
# A front page: no path, or only landing segments — open-evalsets.own_domain's rule (a language code, index.html,
# home) and the same words chained or with another extension (/en/home/, /eng/index.html, /default.aspx).
_LANDING = re.compile(r"[a-z]{2}([-_][a-z]{2})?|eng|(index|home|default|main|welcome|start)(\.(html?|php|aspx?|jsp|shtml|cfm))?")
COLS = ("query", "entity", "qid", "accept")


def log(msg: str) -> None:
    print(f"[sq-accept-keys] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- tokens
def token_of(url: str) -> str | None:
    """One P856 value -> "site:<host>" (a front page) or "url:<host/path>" (a page inside a host); None when it is
    no web address. www., the scheme, a trailing slash and a fragment do not count; a query does not on a front page."""
    url = url.strip()
    if "://" not in url and re.match(r"[a-z][a-z0-9+.-]*:(?!\d)", url, re.I):
        return None                                        # mailto:, tel: — another scheme, not a bare host:port
    try:
        u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    host = host[4:] if host.startswith("www.") else host
    try:
        path = urllib.parse.unquote(u.path).strip("/").lower()
    except ValueError:
        return None
    if re.search(r"[\s|\t]", path):
        return None
    if all(_LANDING.fullmatch(seg) for seg in path.split("/") if seg):
        return "site:" + host
    if u.query:
        return None                                        # books.google.com/books?id=…: the path alone is the whole catalogue
    return f"url:{host}/{path}"


def token_host(tok: str) -> str:
    return tok.split(":", 1)[1].split("/", 1)[0]


def covered(tok: str, domains: list[str]) -> bool:
    """The primary key already takes every result this token would: its host is an expected domain or under one."""
    h = token_host(tok)
    return any(h == d or h.endswith("." + d) for d in domains)


def on_expected(tokens: set[str], domains: list[str]) -> bool:
    return any(covered(t, domains) for t in tokens)


# ---------------------------------------------------------------- QLever
def _post(query: str, timeout: int = 900) -> bytes:
    req = urllib.request.Request(QLEVER, data=urllib.parse.urlencode({"query": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == 4:
                raise
            wait = int(e.headers.get("Retry-After") or 60 * (attempt + 1))
            log(f"QLever answered {e.code}: waiting {wait}s")          # asked to back off: wait, never hammer
            time.sleep(wait)
    raise RuntimeError("unreachable")


def complete(got: int, expected: int) -> bool:
    """importer/wikilinks.complete: a short answer must stop the build; 0.5% slack for rows QLever itself drops."""
    return got >= expected * 0.995


def _lines(body: bytes) -> list[str]:
    return [ln for ln in body.decode("utf-8", "replace").splitlines() if ln and not ln.startswith("?")]


def fetch_articles(cache: Path, offline: bool) -> list[Path]:
    """The article pages, from the cache or QLever. -> the page files, in order."""
    meta = cache / "p856-article.json"
    if meta.exists():
        m = json.loads(meta.read_text())
        pages = [cache / n for n in m["pages"]]
        if all(p.exists() for p in pages):
            return pages
    if offline:
        sys.exit(f"sq-accept-keys: --offline and no cached article pages in {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    expected = int(_lines(_post(ARTICLE_COUNT))[0].strip().strip('"').split("^^")[0].strip('"'))
    log(f"QLever: {expected:,} P856 statements of items with an English article")
    pages, seen, page = [], 0, 0
    while True:
        time.sleep(PAUSE)
        rows = _lines(_post(ARTICLE_PAGE % (PAGE, page * PAGE)))
        p = cache / f"p856-article.{page}.tsv.gz"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        pages.append(p)
        seen += len(rows)
        log(f"QLever: page {page}: {len(rows):,} rows ({seen:,} of {expected:,})")
        if len(rows) < PAGE:
            break
        page += 1
    if not complete(seen, expected):
        sys.exit(f"sq-accept-keys: QLever sent {seen:,} of {expected:,} rows — not building keys from a partial answer")
    meta.write_text(json.dumps({"fetched": time.strftime("%Y-%m-%d"), "expected": expected, "rows": seen,
                                "pages": [p.name for p in pages], "endpoint": QLEVER}, indent=1))
    return pages


def fetch_items(qids: list[str], cache: Path, offline: bool) -> list[str]:
    """P856 statements of these items (things with no English article), in batches. -> TSV lines item, site, rank."""
    out = []
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        p = cache / f"p856-items-{hashlib.md5(' '.join(batch).encode()).hexdigest()[:12]}.tsv"
        if not p.exists():
            if offline:
                sys.exit(f"sq-accept-keys: --offline and no cached answer for a batch of {len(batch)} items in {cache}")
            cache.mkdir(parents=True, exist_ok=True)
            time.sleep(PAUSE)
            body = _post(ITEMS_QUERY % " ".join("wd:" + q for q in batch), timeout=300)
            if body.lstrip().startswith(b"{"):
                sys.exit("sq-accept-keys: QLever answered with an error, not rows - try later")
            p.write_bytes(body)
            log(f"QLever: items {i + 1}-{i + len(batch)} of {len(qids)}")
        out += _lines(p.read_bytes())
    return out


def _iri(v: str) -> str:
    return v.strip().strip("<>")


def _deprecated(rank: str) -> bool:
    return rank.strip().strip("<>").endswith("DeprecatedRank")


def read_articles(pages: list[Path], titles: set[str], keys: set[str]) -> tuple[dict, dict]:
    """The pages, streamed, keeping only what the sets ask for.
    -> ({title: {qid: {token}}} for the wanted titles, {norm_key(title): {qid: {token}}} for the wanted keys)."""
    by_title: dict = defaultdict(lambda: defaultdict(set))
    by_key: dict = defaultdict(lambda: defaultdict(set))
    for p in pages:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 4 or _deprecated(c[3]) or "/wiki/" not in c[1]:
                    continue
                title = urllib.parse.unquote(_iri(c[1]).rsplit("/wiki/", 1)[-1]).replace("_", " ")
                key = C.norm_key(title)
                in_t, in_k = title in titles, key in keys
                if not (in_t or in_k):
                    continue
                tok = token_of(_iri(c[2]))
                qid = _iri(c[0]).rsplit("/", 1)[-1]
                if not tok or not _QID.match(qid):
                    continue
                if in_t:
                    by_title[title][qid].add(tok)
                if in_k:
                    by_key[key][qid].add(tok)
    return by_title, by_key


def read_items(lines: list[str]) -> dict:
    by_qid: dict = defaultdict(set)
    for line in lines:
        c = line.rstrip("\n").split("\t")
        if len(c) < 3 or _deprecated(c[2]):
            continue
        tok, qid = token_of(_iri(c[1])), _iri(c[0]).rsplit("/", 1)[-1]
        if tok and _QID.match(qid):
            by_qid[qid].add(tok)
    return by_qid


# ---------------------------------------------------------------- the keys
def accept_of(r: dict, by_title: dict, by_key: dict, by_qid: dict) -> tuple[str, list[str], str]:
    """One set row -> (qid(s) "|"-joined, tokens in byte order, why none). Pure, for tests."""
    cat, ent = r["category"], r.get("entity", "")
    exp = [] if r["expected"] == "-" else r["expected"].split("|")
    if cat == "wiki":
        items = {}
        for t in exp:
            items.update(by_title.get(t, {}))
        if not items:
            return "", [], "no-site"                       # the article's item has no official site (or was renamed)
        toks = set().union(*items.values())
        return "|".join(sorted(items)), sorted(toks, key=lambda t: t.encode()), ""
    if cat != "site":
        return "", [], "not-a-thing"
    if _QID.match(ent):
        items = {ent: by_qid[ent]} if by_qid.get(ent) else {}
    else:
        items = dict(by_key.get(ent, {}))
        if len(items) > 1:                                 # several articles under one key: the one whose site the row expects
            items = {q: t for q, t in items.items() if on_expected(t, exp)}
    if not items:
        return "", [], "no-item"
    toks = {t for ts in items.values() for t in ts if not covered(t, exp)}
    return "|".join(sorted(items)), sorted(toks, key=lambda t: t.encode()), "" if toks else "nothing-new"


def build(sets: list[str], pages: list[Path], cache: Path, offline: bool) -> tuple[dict, dict]:
    """-> ({set: [(query, entity, qid, tokens)]}, {set: counts})."""
    rows = {s: C.load_set(s, keys="primary") for s in sets}
    titles = {t for s in sets for r in rows[s] if r["category"] == "wiki" for t in r["expected"].split("|")}
    keys = {r["entity"] for s in sets for r in rows[s] if r["category"] == "site" and not _QID.match(r.get("entity", ""))}
    qids = sorted({r["entity"] for s in sets for r in rows[s] if r["category"] == "site" and _QID.match(r.get("entity", ""))},
                  key=lambda q: int(q[1:]))
    by_title, by_key = read_articles(pages, titles, keys)
    by_qid = read_items(fetch_items(qids, cache, offline)) if qids else {}
    out, stats = {}, {}
    for s in sets:
        got, why = [], defaultdict(int)
        for r in rows[s]:
            qid, toks, reason = accept_of(r, by_title, by_key, by_qid)
            why[reason or "accepts"] += 1
            if toks:
                got.append((r["query"], r.get("entity", ""), qid, toks))
        out[s] = got
        stats[s] = {"rows": len(rows[s]), "with_alternatives": len(got), "tokens": sum(len(g[3]) for g in got),
                    "dev": sum(1 for r in rows[s] if r.get("split") == "dev"), **{k: v for k, v in sorted(why.items()) if k != "accepts"}}
    return out, stats


def write(out_dir: Path, built: dict, stats: dict, fetched: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for s, got in built.items():
        p = out_dir / f"{s}.accept.tsv"
        if not got:
            p.unlink(missing_ok=True)
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write("\t".join(COLS) + "\n")
            for q, ent, qid, toks in got:
                f.write("\t".join((q, ent, qid, "|".join(toks))) + "\n")
    (out_dir / "BUILD.json").write_text(json.dumps({
        "source": "Wikidata P856 (official website), every statement of rank normal or preferred, through QLever",
        "licence": "CC0 1.0 (Wikidata)", "endpoint": QLEVER, "fetched": fetched, "issue": "",
        "rule": "owner decision 2026-10-13: a thing's own official site above its article is a correct result",
        "sets": stats}, indent=1, sort_keys=True) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="where the sidecars go (default tools/evalsets/open/accept)")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "logout" / "sq-accept"), help="QLever answers (delete to refresh)")
    ap.add_argument("--sets", default="", help=f"comma-separated subset (default: {', '.join(WIKI_SETS + SITE_SETS)})")
    ap.add_argument("--open", help="the sets directory (default tools/evalsets/open; the unit test's fixture)")
    ap.add_argument("--offline", action="store_true", help="never ask QLever: build from the cache or stop")
    a = ap.parse_args()
    if a.open:
        C.OPEN = Path(a.open)
    have = set(C.all_sets())
    sets = [s.strip() for s in a.sets.split(",") if s.strip()] or [s for s in WIKI_SETS + SITE_SETS if s in have]
    cache = Path(a.cache)
    pages = fetch_articles(cache, a.offline)
    built, stats = build(sets, pages, cache, a.offline)
    fetched = json.loads((cache / "p856-article.json").read_text()).get("fetched", "")
    out = Path(a.out) if a.out else C.OPEN / C.ACCEPT_DIR
    write(out, built, stats, fetched)
    for s in sets:
        st = stats[s]
        log(f"{s:<14} {st['rows']:>5} rows, {st['with_alternatives']:>5} gain alternatives ({st['tokens']} tokens)  "
            + ", ".join(f"{k} {v}" for k, v in st.items() if k not in ("rows", "with_alternatives", "tokens", "dev")))
    log(f"written: {out}")


if __name__ == "__main__":
    main()
