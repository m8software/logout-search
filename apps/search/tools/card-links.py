#!/usr/bin/env python3
"""card-links.py FIELDS.json OUT_DIR CARDLINKS.sqlite: every useful link Wikidata records for a topic, for its card
(owner 2026-11-06: "If it's legal and ok license wise to use the stuff from Wikidata we should be using all of it").

Wikidata is CC0: its values may be used for anything. Only links are kept (a subreddit's name, a film's Letterboxd id
made into its address); nothing is fetched from the linked sites, so their terms are not engaged.

FIELDS.json: [{p, label, uses, fmt}] the chosen link fields (external ids with a formatter URL, in classes a searcher
clicks: accounts, film, TV, music, games, books, sport, business, fan wikis, encyclopedias; catalogue and scholarly
ids left out), most used first. OUT_DIR: one TSV per field (English Wikipedia title, value) from QLever.
CARDLINKS.sqlite: links(title, p, url) by title, fields(p, label, uses). The engine (SEARCH_CARDLINKS) reads it per
card; the card shows the known accounts and the rest under "More links".
"""
import json
import os
import re
import sqlite3
import sys
import urllib.parse

fields, out_dir, dest = sys.argv[1:4]
F = json.load(open(fields, encoding="utf-8"))

# the name a person knows the site by (the field's label minus "ID", "username", "handle" ...)
SHORT = {"P3984": "Reddit", "P4073": "Fandom", "P6262": "Fandom", "P345": "IMDb", "P2002": "X", "P2003": "Instagram",
         "P2013": "Facebook", "P11892": "Threads", "P11245": "YouTube", "P2397": "YouTube", "P6634": "LinkedIn",
         "P4264": "LinkedIn", "P7085": "TikTok", "P12361": "Bluesky", "P4033": "Mastodon", "P2037": "GitHub",
         "P5749": "Amazon", "P1417": "Britannica", "P2088": "Crunchbase", "P8013": "Trakt", "P12492": "Trakt"}
_TAIL = re.compile(r"\s*\((?:old|new|former)\)|\s+(?:ID|username|handle|identifier|ID number|code|profile ID|"
                   r"person ID|player ID|athlete ID|film ID|movie ID|game ID|artist ID|work ID|series ID|show ID|"
                   r"organization ID|company ID|people ID|article ID|channel ID|user ID|page ID)\s*$", re.I)


_WORDS = re.compile(r"\s+(?:ID|IDs|slug|character|team|author|work|title|film|movie|game|person|people|player|athlete|"
                    r"artist|series|show|company|organization|organisation|username|handle|identifier|profile|page|"
                    r"entry|article|code|number|channel|user|release|venue|album|book|edition|club|league|"
                    r"\(\d{4}\)|\([^)]*edition[^)]*\)|\((?:old|new|former)\))\s*$", re.I)


def short(f: dict) -> str:
    """The site's name: the field's label without its trailing kind words ("Transfermarkt team ID" -> "Transfermarkt")."""
    if f["p"] in SHORT:
        return SHORT[f["p"]]
    s, prev = f["label"].strip(), None
    while s != prev:
        prev, s = s, _WORDS.sub("", s).strip()
    s = _TAIL.sub("", s).strip() or f["label"]
    return s[:1].upper() + s[1:]


def url_of(f: dict, v: str) -> str | None:
    v = v.strip()
    if not v or len(v) > 200 or any(c in v for c in "\n\t<>\"'"):
        return None
    if f["fmt"] == "imdb":
        m = re.match(r"^(tt|nm|co)\d{7,8}$", v)
        return f"https://www.imdb.com/{ {'tt': 'title', 'nm': 'name', 'co': 'company'}[m.group(1)] }/{v}/" if m else None
    u = f["fmt"].replace("$1", urllib.parse.quote(v, safe="/:@-._~"))
    return u if u.startswith("https://") or u.startswith("http://") else None


# an English-language search page: sites on international or English-speaking domains; a country's own film,
# music or sport database (.cz, .ru, .de, .fr ...) is left out (2026-11-06)
_TLD_OK = {"com", "org", "net", "io", "co", "uk", "us", "ca", "au", "nz", "ie", "tv", "fm", "gg", "info", "edu", "gov",
           "app", "me", "eu", "int", "ai", "dev", "live", "games", "movie", "music", "news", "online", "site", "world"}


def english(f: dict) -> bool:
    if f["fmt"] == "imdb":
        return True
    host = (urllib.parse.urlsplit(f["fmt"].replace("$1", "x")).hostname or "")
    return host.rsplit(".", 1)[-1] in _TLD_OK


# a library, catalogue or archive record is not a place a searcher goes; nor is the Encyclopedia of China
_CATALOGUE = re.compile(r"librar|catalog|archive|LCCN|authority|bibliograph|Encyclopedia of China", re.I)
F = [f for f in F if english(f) and not _CATALOGUE.search(f["label"])]
db = sqlite3.connect(dest + ".part")
db.executescript("""DROP TABLE IF EXISTS links; DROP TABLE IF EXISTS fields;
CREATE TABLE fields (p TEXT PRIMARY KEY, label TEXT, uses INTEGER, rank INTEGER);
CREATE TABLE links (title TEXT NOT NULL, p TEXT NOT NULL, url TEXT NOT NULL);""")
n = 0
for rank, f in enumerate(F):
    db.execute("INSERT INTO fields VALUES (?,?,?,?)", (f["p"], short(f), f["uses"], rank))
    path = os.path.join(out_dir, f["p"] + ".tsv")
    if not os.path.exists(path):
        continue
    seen = set()
    rows = []
    for line in open(path, encoding="utf-8"):
        t, _, v = line.rstrip("\n").partition("\t")
        if not t or (t, f["p"]) in seen:        # one value per topic and field: the first (most fields have one)
            continue
        u = url_of(f, v)
        if u:
            seen.add((t, f["p"]))
            rows.append((t, f["p"], u))
    db.executemany("INSERT INTO links VALUES (?,?,?)", rows)
    n += len(rows)
db.execute("CREATE INDEX links_title ON links (title)")
db.commit()
db.execute("VACUUM")
db.close()
os.replace(dest + ".part", dest)
print(f"{n} links, {len(F)} fields -> {dest}", file=sys.stderr)
