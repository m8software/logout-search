#!/usr/bin/env python3
"""Accepted alternatives for the place sets (follow-up, epic): the place's own website, as its
OpenStreetMap feature names it. No LLM, no hand edits, no network, no live database: one release copy on disk.

    tools/sq-accept-places.py [--release ~/search-eval/rc8-acc] [--out tools/evalsets/open/accept] [--sets a,b]

Owner decision 2026-10-13: for a place query, the place's own website ranking first is a CORRECT result,
the same way a thing's official site (Wikidata P856, tools/sq-accept-keys.py) counts above its article. So a
place row's key accepts, beside its expected map row (which stays the primary, untouched), its website.

Source. The release's places-osm.sqlite, column `website`: the feature's `website` tag, else its
`contact:website` tag (importer/osmplaces.py row_of — the store keeps one value and not which tag it was). A set
row's entity (`way/559761359`) is the path of a registry.sqlite row; that row's doc_id is the place's. ODbL 1.0,
© OpenStreetMap contributors — a different licence from the P856 sidecars (CC0), so every line says where it came
from in its `source` column (`osm:website`) and the counts live in BUILD-places.json, not BUILD.json.

A value becomes one token, by sq-accept-keys.token_of (the P856 keys' normalisation, unchanged):
  site:<host>        the value is a front page. A result on that host or a subdomain of it is a hit.
  url:<host/path>    the value is a page inside a host (a chain's page for this branch, a town's page for this
                     park). A result at that URL or under it is a hit — never the rest of the host.
The host is taken as tagged (www. dropped). There is no public-suffix list in this tooling (stdlib only), so a
tag on `order.example.com` does not accept `example.com`; the other direction (tag `example.com`, result on
`order.example.com`) is a hit.

Not "its own website" — the value gives no token:
  listed      the host is, or is under, a domain of NOT_OWN: social networks, review / listing / map sites,
              ordering and booking platforms, marketplaces, encyclopaedias. The place has a page there; the site
              is not the place's.
  platform    the value is the bare front page of a site builder (PLATFORM_APEX: weebly.com, business.site …).
              A place's own subdomain of one (`joesdeli.weebly.com`) is its site and is kept.
  shared      a front page that places of SHARED_NAMES or more different names in the whole store give as
              their website: a chain, a mall, a township — not this place's own.
  not-a-url   token_of made nothing of it (another scheme, no host, a page named only by its query string).

Output: <out>/<set>.accept.tsv — query, entity, source, accept — one line per set row that gains a token, in
the set's order; and <out>/BUILD-places.json (the release, counts per set, every value left out and why). The
set files are not written. tools/sq_common.py reads the sidecar; tools/eval-queries.py accept_hit() is the match
rule. A rerun on the same release writes the same bytes.

DEV TOOLING: stdlib only, never imported by apps/search/serve.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

ak = C.load_tool("sq_accept_keys", C.TOOLS / "sq-accept-keys.py")

PLACE_SETS = ("place", "place-town", "typo-place")
SOURCE = "osm:website"
COLS = ("query", "entity", "source", "accept")
BUILD_FILE = "BUILD-places.json"
SHARED_NAMES = 3
# A page on one of these is the place's page on somebody else's site. Matched as the host or anything under it.
NOT_OWN = (
    # social networks, link pages
    "facebook.com", "fb.com", "fb.me", "m.me", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
    "youtu.be", "linkedin.com", "pinterest.com", "snapchat.com", "threads.net", "nextdoor.com", "linktr.ee", "t.me",
    "wa.me", "whatsapp.com",
    # maps, search engines, encyclopaedias
    "google.com", "goo.gl", "g.page", "g.co", "maps.apple.com", "bing.com", "mapquest.com", "waze.com",
    "openstreetmap.org", "osm.org", "wikipedia.org", "wikidata.org", "wikimedia.org",
    # reviews, listings, directories
    "yelp.com", "tripadvisor.com", "tripadvisor.co.uk", "tripadvisor.ca", "tripadvisor.com.au", "tripadvisor.de",
    "tripadvisor.fr", "tripadvisor.es", "tripadvisor.it", "foursquare.com", "yellowpages.com", "superpages.com",
    "manta.com", "bbb.org", "zomato.com", "untappd.com", "menupages.com", "allmenus.com", "zmenu.com",
    "restaurantji.com", "healthgrades.com", "zocdoc.com", "vitals.com", "alltrails.com", "loopnet.com",
    # ordering, booking, tickets
    "toasttab.com", "grubhub.com", "seamless.com", "doordash.com", "ubereats.com", "postmates.com", "slicelife.com",
    "chownow.com", "beyondmenu.com", "order.online", "menufy.com", "clover.com", "opentable.com", "resy.com",
    "exploretock.com", "booking.com", "expedia.com", "hotels.com", "airbnb.com", "vrbo.com", "vagaro.com",
    "booksy.com", "styleseat.com", "mindbodyonline.com", "eventbrite.com", "ticketmaster.com",
    # marketplaces
    "etsy.com", "amazon.com", "ebay.com",
)
# Site builders: the bare front page is the builder's; a subdomain is the customer's own site.
PLATFORM_APEX = (
    "weebly.com", "wixsite.com", "wix.com", "business.site", "square.site", "squarespace.com", "myshopify.com",
    "shopify.com", "wordpress.com", "blogspot.com", "godaddysites.com", "godaddy.com", "webflow.io", "github.io",
    "carrd.co", "jimdofree.com", "site123.me", "webnode.page", "tumblr.com",
)


def log(msg: str) -> None:
    print(f"[sq-accept-places] {msg}", file=sys.stderr, flush=True)


def under(host: str, domains: tuple) -> str:
    """The domain of `domains` this host is or is under; "" when none."""
    return next((d for d in domains if host == d or host.endswith("." + d)), "")


def own_token(website: str, shared: dict | None = None) -> tuple[str | None, str]:
    """One OSM website value -> (token, "") or (None, why not). Pure, for tests. shared = {host: number of
    different place names in the store whose website is that front page}."""
    if not (website or "").strip():
        return None, "no-website"
    tok = ak.token_of(website)
    if not tok:
        return None, "not-a-url"
    host = ak.token_host(tok)
    if under(host, NOT_OWN):
        return None, "listed"
    if tok.startswith("site:"):
        if host in PLATFORM_APEX:
            return None, "platform"
        if (shared or {}).get(host, 0) >= SHARED_NAMES:
            return None, "shared"
    return tok, ""


# ---------------------------------------------------------------- the release
def open_release(release: Path) -> sqlite3.Connection:
    for f in ("places-osm.sqlite", "registry.sqlite"):
        if not (release / f).exists():
            sys.exit(f"sq-accept-places: {release / f} is not here — --release wants a release copy with its places store")
    db = sqlite3.connect(f"file:{release / 'places-osm.sqlite'}?mode=ro", uri=True)
    db.execute("ATTACH DATABASE ? AS reg", (f"file:{release / 'registry.sqlite'}?mode=ro",))
    return db


def websites(db: sqlite3.Connection, entities: set[str]) -> dict:
    """{osm key: website ("" when the feature has none)} for the set's entities that the release carries."""
    db.execute("CREATE TEMP TABLE want (path TEXT PRIMARY KEY) WITHOUT ROWID")
    db.executemany("INSERT INTO want VALUES (?)", ((e,) for e in sorted(entities)))
    docs = dict(db.execute("SELECT doc_id, path FROM reg.registry WHERE host_id IS NULL AND path IN (SELECT path FROM want)"))
    out: dict = {}
    ids = sorted(docs)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for doc, web in db.execute(f"SELECT doc_id, website FROM places WHERE doc_id IN ({','.join('?' * len(chunk))})", chunk):
            out[docs[doc]] = web or ""
    return out


def shared_front_pages(db: sqlite3.Connection, hosts: set[str]) -> dict:
    """{host: how many different place names in the whole store give that front page as their website}."""
    names: dict = defaultdict(set)
    for key, web in db.execute("SELECT name_key, website FROM places WHERE website <> ''"):
        tok = ak.token_of(web)
        if tok and tok.startswith("site:") and tok[5:] in hosts:
            names[tok[5:]].add(key)
    return {h: len(v) for h, v in names.items()}


# ---------------------------------------------------------------- the keys
def build(sets: list[str], web: dict, shared: dict) -> tuple[dict, dict, list]:
    """-> ({set: [(query, entity, token)]}, {set: counts}, [(entity, website, why)] left out, one per entity)."""
    out, stats, left = {}, {}, {}
    for s in sets:
        got, why = [], defaultdict(int)
        rows = C.load_set(s, keys="primary")
        for r in rows:
            ent = r.get("entity", "")
            if r["category"] != "place" or ent not in web:
                why["not-in-release" if r["category"] == "place" else "not-a-place"] += 1
                continue
            tok, reason = own_token(web[ent], shared)
            why[reason or "accepts"] += 1
            if tok:
                got.append((r["query"], ent, tok))
            elif reason != "no-website":
                left[ent] = (ent, web[ent], reason)
        out[s] = got
        stats[s] = {"rows": len(rows), "with_website_key": len(got),
                    "dev": sum(1 for r in rows if r.get("split") == "dev"),
                    "dev_with_website_key": sum(1 for r in rows if r.get("split") == "dev" and (r["query"], r.get("entity", "")) in {(q, e) for q, e, _ in got}),
                    **{k: v for k, v in sorted(why.items()) if k != "accepts"}}
    return out, stats, sorted(left.values())


def write(out_dir: Path, built: dict, stats: dict, left: list, release_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for s, got in built.items():
        p = out_dir / f"{s}.accept.tsv"
        if not got:
            p.unlink(missing_ok=True)
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write("\t".join(COLS) + "\n")
            for q, ent, tok in got:
                f.write("\t".join((q, ent, SOURCE, tok)) + "\n")
    (out_dir / BUILD_FILE).write_text(json.dumps({
        "source": "OpenStreetMap `website`, else `contact:website`, as the release's places-osm.sqlite carries it",
        "licence": "ODbL 1.0 (© OpenStreetMap contributors)", "release": release_id, "issue": "",
        "rule": "owner decision 2026-10-13: for a place query, the place's own website first is a correct result",
        "not_own": sorted(NOT_OWN), "platform_apex": sorted(PLATFORM_APEX), "shared_names": SHARED_NAMES,
        "left_out": [{"entity": e, "website": w, "why": y} for e, w, y in left],
        "sets": stats}, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default=os.environ.get("SQ_RELEASE") or str(Path.home() / "search-eval" / "rc8-acc"),
                    help="a release copy: places-osm.sqlite and registry.sqlite are read, nothing is written there")
    ap.add_argument("--out", help="where the sidecars go (default tools/evalsets/open/accept)")
    ap.add_argument("--sets", default="", help=f"comma-separated subset (default: {', '.join(PLACE_SETS)})")
    ap.add_argument("--open", help="the sets directory (default tools/evalsets/open; the unit test's fixture)")
    a = ap.parse_args()
    if a.open:
        C.OPEN = Path(a.open)
    have = set(C.all_sets())
    sets = [s.strip() for s in a.sets.split(",") if s.strip()] or [s for s in PLACE_SETS if s in have]
    db = open_release(Path(a.release))
    release_id = (db.execute("SELECT value FROM meta WHERE key = 'built_release'").fetchone() or ("",))[0]
    entities = {r.get("entity", "") for s in sets for r in C.load_set(s, keys="primary") if r["category"] == "place"}
    web = websites(db, entities)
    hosts = {ak.token_host(t) for t in (ak.token_of(w) for w in web.values() if w) if t and t.startswith("site:")}
    built, stats, left = build(sets, web, shared_front_pages(db, hosts))
    out = Path(a.out) if a.out else C.OPEN / C.ACCEPT_DIR
    write(out, built, stats, left, release_id)
    for s in sets:
        st = stats[s]
        log(f"{s:<11} {st['rows']:>5} rows, {st['with_website_key']:>4} gain a website key ({st['dev_with_website_key']} dev)  "
            + ", ".join(f"{k} {v}" for k, v in st.items() if k not in ("rows", "with_website_key", "dev", "dev_with_website_key")))
    for e, w, y in left:
        log(f"  left out ({y}): {e}  {w}")
    log(f"release {release_id}; written: {out}")


if __name__ == "__main__":
    main()
