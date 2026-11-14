#!/usr/bin/env bash
set -euo pipefail
################################################################################
# Unit test — Wikipedia in the result list ("6 or 7 in a row")
################################################################################
# REAL search_api.mix_wikipedia, no database: the card's article and the related articles the
# card lists are not repeated in the results; at most two other articles stay near the top, each
# between websites; the rest follow the websites; a leading article with no card stays first.
# Usage: bash tests/unit/test-search-wiki-mix.sh   (PY=python with psycopg)
################################################################################
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
if ! "$PY" -c 'import psycopg' 2>/dev/null; then echo "SKIP: $PY lacks psycopg"; exit 0; fi
API="${REPO}/apps/search/serve/api" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["API"])
import search_api as s
fails = 0
def check(name, ok, detail=""):
    global fails
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += 0 if ok else 1
def wiki(t, score=10.0): return s._wiki(t, "lead", score)
def site(h): return s._site(f"https://{h}/", h, "d", 5.0)
names = lambda rs: [r["title"].replace(" — Wikipedia", "") for r in rs]

# a card: its article and its related articles leave the list
res = [site("a.com"), wiki("Paul Rudd", s.WIKI_EXACT_SCORE), wiki("Ant-Man"), wiki("Clueless"), site("b.com"), site("c.com")]
card = {"title": "Paul Rudd", "url": res[1]["url"], "related": [{"title": "Ant-Man", "url": res[2]["url"]}]}
out = s.mix_wikipedia(res, card)
check("card article not repeated", "Paul Rudd" not in names(out), names(out))
check("card's related articles not repeated", "Ant-Man" not in names(out), names(out))
check("other articles and sites kept", set(names(out)) == {"a.com", "Clueless", "b.com", "c.com"}, names(out))

# six articles in a row -> two, between sites, the rest after the sites
res = [wiki(f"W{i}") for i in range(6)] + [site(f"s{i}.com") for i in range(12)]
# (SEARCH_INSTANT_LIST_FIRST off: the rule before rc9, kept under the switch)
out = s.mix_wikipedia(res, {"title": "X", "url": "https://x.example/", "related": []}, False)
kinds = ["w" if r["source"] == "wikipedia" else "s" for r in out]
check("no run of articles at the top", "ww" not in "".join(kinds[:8]), "".join(kinds))
check("only two articles among the first ten", kinds[:10].count("w") == 2, "".join(kinds))
check("nothing lost", len(out) == 18 and {r["url"] for r in out} == {r["url"] for r in res})
check("first result is a website", kinds[0] == "s", "".join(kinds))
# (on, the rc9 default: the ranked first article stays first, then the same mix: still no run, two more at most)
out = s.mix_wikipedia(res, {"title": "X", "url": "https://x.example/", "related": []}, True)
kinds = ["w" if r["source"] == "wikipedia" else "s" for r in out]
check("list first: the ranked first article stays first", names(out)[0] == "W0", names(out))
check("list first: still no run of articles at the top", "ww" not in "".join(kinds[:8]), "".join(kinds))
check("list first: the lead plus two articles among the first ten", kinds[:10].count("w") == 3, "".join(kinds))
check("list first: nothing lost", len(out) == 18 and {r["url"] for r in out} == {r["url"] for r in res})

# no card: a leading exact article is the answer and stays first
res = [wiki("Thing", s.WIKI_EXACT_SCORE)] + [wiki(f"W{i}") for i in range(4)] + [site(f"s{i}.com") for i in range(10)]
out = s.mix_wikipedia(res, None)
check("no card: leading exact article stays first", names(out)[0] == "Thing", names(out))
check("no card: the others are still spread", "".join("w" if r["source"] == "wikipedia" else "s" for r in out[1:9]).count("ww") == 0)

# nearby places lead as one block: no article between two places, the rest mixed as before
def place(n): return s._place(f"node/{n}", f"P{n}", "amenity=pharmacy", "1 Main St", "Town", "", "", "", 0.5 + n, score=1000 - n)
res = [place(i) for i in range(3)] + [site(f"s{i}.com") for i in range(8)] + [wiki(f"W{i}") for i in range(3)]
out = s.mix_wikipedia(res, None)
check("places stay a block at the top", [r["source"] for r in out[:3]] == ["place"] * 3, [r["source"] for r in out[:5]])
check("an article still mixes in after the places", any(r["source"] == "wikipedia" for r in out[3:11]))
check("places + nothing lost", len(out) == len(res))

# untouched when there is nothing to mix
res = [site("a.com"), site("b.com")]
check("no articles: same list", s.mix_wikipedia(res, None) == res)
res = [wiki("Only")]
check("only articles: kept", names(s.mix_wikipedia(res, None)) == ["Only"])

# an old season article takes no early slot ("1964-65 West Ham United F.C. season" was the second row)
res = [site("whufc.com"), wiki("1964–65 West Ham United F.C. season"), site("a.com"), wiki("London Stadium"),
       site("b.com"), site("c.com"), wiki("2026–27 West Ham United F.C. season")]
out = s.mix_wikipedia(res, None, False)
check("old season: a current article takes the early slot", names(out)[1] == "London Stadium", names(out))
check("old season: it follows the others, nothing lost", names(out)[-1] == "1964–65 West Ham United F.C. season" and len(out) == len(res), names(out))
res = [site("whufc.com"), wiki("1964–65 West Ham United F.C. season"), site("a.com"), wiki("2023–24 West Ham United F.C. season"),
       site("b.com"), site("c.com")]
out = s.mix_wikipedia(res, None, False)
check("old season: only old seasons left, still none early", names(out)[:4] == ["whufc.com", "a.com", "b.com", "c.com"] and len(out) == len(res), names(out))
check("old season: this season and last are not old", not s.old_season({"title": "2026–27 X season"}, 2026)
      and not s.old_season({"title": "2025–26 X season"}, 2026) and s.old_season({"title": "2023–24 X season"}, 2026)
      and not s.old_season({"title": "Season (film)"}, 2026))

# one site, one entry (whufc.com, whufc.com/en and cdn.whufc.com/en were three results)
k = lambda u: s.home_key({"url": u, "source": "site"})
check("home key: a language root is the home page", k("https://www.whufc.com/en") == k("https://whufc.com/") == k("https://whufc.com/en-gb/"))
check("home key: a CDN host is the home page", k("https://cdn.whufc.com/en") == k("https://whufc.com"))
check("home key: a section page stays its own", k("https://whufc.com/tv") != k("https://whufc.com/") and k("https://whufc.com/en/news") != k("https://whufc.com/"))
check("home key: a CDN brand's own site is not cut", k("https://cdn.com/") == "cdn.com" and k("https://media.mit.edu/") != k("https://mit.edu/"))
check("home key: the page key itself is unchanged", s.result_key({"url": "https://whufc.com/en", "source": "site"}) != s.result_key({"url": "https://whufc.com/", "source": "site"}))
one = s.one_home_page([site("benefitcosmetics.com/en-au"), site("maybelline.com"), site("benefitcosmetics.com")])
check("one home page: the first copy keeps its row", [r["title"] for r in one] == ["benefitcosmetics.com/en-au", "maybelline.com"], [r["url"] for r in one])
print(f"\nTotal FAIL: {fails}"); sys.exit(1 if fails else 0)
PY
