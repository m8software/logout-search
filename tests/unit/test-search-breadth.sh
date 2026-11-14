#!/usr/bin/env bash
# The list under the card, wider and cleaner (search_api.breadth_list, SEARCH_BREADTH / ?bl=, 2026-11-07): each flag
# alone, with a stand-in reranker, no database.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
API="${REPO}/apps/search/serve/api" "$PY" - <<'PY'
import os, sys, types
sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
sys.path.insert(0, os.environ["API"])
import search_api as s
fails = 0
def check(name, ok, detail=""):
    global fails
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += 0 if ok else 1
def row(url, title, src="site", content=""):
    return {"url": url, "title": title, "source": src, "content": content, "score": 1.0}
card = {"title": "Spider-Man", "url": "https://en.wikipedia.org/wiki/Spider-Man", "description": "superhero",
        "links": [{"title": "Official site", "url": "https://www.marvel.com/characters/spider-man"},
                  {"title": "Reddit", "url": "https://www.reddit.com/r/Spiderman/"},
                  {"title": "Fandom", "url": "https://spiderman.fandom.com/"},
                  {"title": "X", "url": "https://x.com/SpiderMan"}],
        "more_links": [{"title": "Britannica", "url": "https://www.britannica.com/topic/Spider-Man"}]}
res = [row("https://www.marvel.com/characters/spider-man", "Spider-Man — Marvel"),
       row("https://en.wikipedia.org/wiki/Spider-Man_in_film", "Spider-Man in film", "wikipedia"),
       row("https://ferniplast.com/", "ferniplast.com"),
       row("https://en.wikipedia.org/wiki/Spider-Man_(2017_TV_series)", "Spider-Man (2017 TV series)", "wikipedia"),
       row("https://en.wikipedia.org/wiki/Spider-Man_in_other_media", "Spider-Man in other media", "wikipedia"),
       row("https://sonypictures.com/", "Spider-Man movies — Sony Pictures"),
       row("https://sonypictures.com/en/", "Sony Pictures home")]
urls = lambda rs: [r["url"] for r in rs]
score = {"ferniplast": -6.0, "Reddit": 3.0, "Fandom": 2.5, "Britannica": -1.0}
def scorer(q, texts):
    return [next((v for k, v in score.items() if k in t), 0.5) for t in texts]
check("off: no flags, the list as it was", s.breadth_list("spider man", res, card, "", scorer) == res)
j = urls(s.breadth_list("spider man", res, card, "J", scorer))
check("J: a row with none of the words goes", "https://ferniplast.com/" not in j, j)
check("J: a site's home again under /en goes", "https://sonypictures.com/en/" not in j, j)
check("J: the first row stays first", j[0] == res[0]["url"], j)
check("J: a hyphenated or spaced name still matches (spiderman / Spider-Man)", "https://en.wikipedia.org/wiki/Spider-Man_in_film" in urls(s.breadth_list("spiderman", res, card, "J", scorer)))
check("J: a typo'd search matches through the card's name (spidremna -> Spider-Man rows kept)",
      "https://en.wikipedia.org/wiki/Spider-Man_in_film" in urls(s.breadth_list("spidremna", res, card, "J", scorer)))
junk_heavy = [row("https://www.marvel.com/characters/spider-man", "Spider-Man — official site"),
              row("https://en.wikipedia.org/wiki/Spider-Man_in_film", "Spider-Man in film", "wikipedia"),
              row("https://ferniplast.com/", "ferniplast.com"),
              row("https://america250.omnihotels.com/movies", "Movies | Omni Hotels"),
              row("https://gbi.georgia.gov/press-releases/bank-robber", "GBI seeking help identifying man"),
              row("https://pdfroom.com/", "PDF Room"),
              row("https://ifilm.com/", "iFilm")]
jh = urls(s.breadth_list("spiderman", junk_heavy, card, "J", scorer))
check("J: stays on when >50% of rest is junk, and 'man' in Spider-Man does not keep unrelated .gov/man rows",
      jh == ["https://www.marvel.com/characters/spider-man", "https://en.wikipedia.org/wiki/Spider-Man_in_film"], jh)
q_rows = [row("https://a.com/", "First"), row("https://b.com/", "Bee keeping guide"), row("https://c.com/", "Hives at home"),
          row("https://d.com/", "Honey basics")]
check("J: when most rows share no word (a question), nothing is dropped",
      urls(s.breadth_list("how do i start beekeping", q_rows, None, "J", scorer)) == urls(q_rows))
d = urls(s.breadth_list("spider man", res, card, "D", scorer))
check("D: Wikipedia articles are not capped (Clef judged them better than what replaced them)", sum("wikipedia" in u for u in d) == 3, d)
check("D: one row per site", d.count("https://sonypictures.com/en/") == 0, d)
gov_rows = [row("https://www.oregon.gov/boli/workers/Pages/leave-for-jury-duty.aspx", "Jury duty leave — BOLI", "gov"),
            row("https://www.courts.oregon.gov/how/pages/jury.aspx", "Oregon Judicial Department : Jury Service", "gov"),
            row("https://berkspa.gov/departments/js/faqs", "Jury Services FAQs", "site")]
check("JD: a second state .gov agency and a county .gov page stay on a government search",
      urls(s.breadth_list("or jury duty", gov_rows, None, "JD", scorer)) == urls(gov_rows))
how_rows = [row("https://diy.stackexchange.com/questions/1", "How to hang a heavy frame", "stackexchange"),
            row("https://diy.stackexchange.com/questions/2", "Picture frame wire vs D-rings", "stackexchange"),
            row("https://www.calculator.net/", "Calculator.net: Free Online Calculators", "site")]
check("JD: a second Stack Exchange question and a tool site stay on a how-to question",
      urls(s.breadth_list("how to hang a picture frame", how_rows, None, "JD", scorer)) == urls(how_rows))
l = s.breadth_list("spider man", res, card, "L", scorer)
check("L: the subreddit and fan wiki join, placed by score above weaker rows",
      urls(l).index("https://www.reddit.com/r/Spiderman/") < urls(l).index("https://ferniplast.com/"), urls(l))
check("L: a link row has a title a reader knows", any(r["title"] == "r/Spiderman — Reddit" for r in l), [r["title"] for r in l])
check("L: never the card's accounts or official site again", "https://x.com/SpiderMan" not in urls(l) and urls(l).count(res[0]["url"]) == 1, urls(l))
sl = urls(s.breadth_list("spider man", res, card, "S", scorer))
check("S: subreddit 2nd, fan wiki 3rd", sl[1:3] == ["https://www.reddit.com/r/Spiderman/", "https://spiderman.fandom.com/"], sl)
f = urls(s.breadth_list("spider man", res, card, "F", scorer))
check("F: a row under the floor after the 3rd goes, the first three stay", "https://ferniplast.com/" in f[:3] and len(f) >= 3, f)
places = [dict(row("https://osm.org/node/1", "Spider shop", "place"))] + res
check("places stay on top untouched", s.breadth_list("spider man", places, card, "JDL", scorer)[0]["source"] == "place")
# places under the official site ("taco bell", owner 2026-11-13): D kept one openstreetmap.org row of three
pl = lambda i: row(f"https://www.openstreetmap.org/node/{i}", f"Spider shop — {i} mi", "place")
under = res[:1] + [pl(1), pl(2)] + res[1:3] + [pl(3)] + res[3:]
for fl in ("D", "J", "JDL", "JDLF"):
    got = s.breadth_list("spider man", under, card, fl, scorer)
    check(f"{fl}: every place under a site is kept", [r["url"] for r in got if r["source"] == "place"] == [pl(i)["url"] for i in (1, 2, 3)], urls(got))
    check(f"{fl}: the places follow the row they followed", urls(got)[:3] == [res[0]["url"], pl(1)["url"], pl(2)["url"]], urls(got))
    plain = s.breadth_list("spider man", res, card, fl, scorer)
    check(f"{fl}: the other rows are shaped as without places", [u for u in urls(got) if "openstreetmap" not in u] == urls(plain), urls(got))
gone = s.breadth_list("spider man", under, card, "J", scorer)
check("a place whose row above was dropped goes after the nearest row still shown",
      urls(gone).index(pl(3)["url"]) == urls(gone).index(res[1]["url"]) + 1, urls(gone))
check("no card: L adds nothing", urls(s.breadth_list("spider man", res, None, "L", scorer)) == urls(res))
# W / C: the card's Wikipedia article's own links (SEARCH_WIKILINKS sqlite), scored with the rows
import sqlite3, tempfile
wl = os.path.join(tempfile.mkdtemp(), "wikilinks.sqlite")
db = sqlite3.connect(wl)
db.executescript("CREATE TABLE links (title TEXT, kind TEXT, pos INTEGER, url TEXT, label TEXT, domain TEXT);"
                 "CREATE TABLE trust (domain TEXT PRIMARY KEY, n INTEGER);")
db.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)", [
    ("Spider-Man", "external", 0, "https://spidey-fans.example.org/", "Spider-Man history at Fans", "example.org"),
    ("Spider-Man", "external", 1, "https://www.marvel.com/x", "Marvel again", "marvel.com"),
    ("Spider-Man", "external", 2, "https://www.facebook.com/spiderman", "Facebook", "facebook.com"),
    ("Spider-Man", "external", 3, "https://comics.example.net/", "Official website", "example.net"),
    ("Spider-Man", "citation", 0, "https://www.nytimes.com/spider", "Why Spider-Man still swings", "nytimes.com"),
    ("Spider-Man", "citation", 1, "https://books.example.com/p", "39", "example.com")])
db.executemany("INSERT INTO trust VALUES (?,?)", [("nytimes.com", 90000)]); db.commit(); db.close()
s.WIKILINKS_PATH = wl
score.update({"Fans": 2.0, "comics.example.net": -4.0, "Why Spider-Man": 1.5})
w = s.breadth_list("spider man", res, card, "W", scorer)
check("W: an External links row joins under its link text", any(r["title"] == "Spider-Man history at Fans" for r in w), [r["title"] for r in w])
check("W: the card's own site, a social account and a candidate under the floor are not added",
      "https://www.marvel.com/x" not in urls(w) and "https://www.facebook.com/spiderman" not in urls(w) and "https://comics.example.net/" not in urls(w), urls(w))
check("W: citations are not added without C", "https://www.nytimes.com/spider" not in urls(w), urls(w))
c = s.breadth_list("spider man", res, card, "C", scorer)
check("C: a citation with a link text joins; one with only a page number does not",
      "https://www.nytimes.com/spider" in urls(c) and "https://books.example.com/p" not in urls(c), urls(c))
check("C: no W rows under C alone", "https://spidey-fans.example.org/" not in urls(c), urls(c))
check("W: off by default (flags empty or JDL): no wiki rows", all("wikipedia_link" not in r for r in s.breadth_list("spider man", res, card, "JDL", scorer)))
check("W: no card, nothing added", urls(s.breadth_list("spider man", res, None, "WC", scorer)) == urls(res))
s.WIKILINKS_PATH = ""
check("W: no file, nothing added", urls(s.breadth_list("spider man", res, card, "WC", scorer)) == urls(res))
print(f"\nTotal FAIL: {fails}"); sys.exit(1 if fails else 0)
PY
