#!/usr/bin/env bash
set -euo pipefail
################################################################################
# Unit test — the news file (NEWS_SQLITE / SEARCH_NEWS_BACKEND=sqlite, ADR-063): search.logout.cloud
# keeps headlines and weather in one SQLite file instead of a Postgres database.
# Writers: the real poller.py / wxall.py statements on a temp file (network parts stubbed).
# Reader: the real search_api news/weather paths reading that same file through news_sqlite.
# Every check and setup step is lazy: an exception is a FAIL and the rest still run.
# Usage: bash tests/unit/test-search-news-sqlite.sh
################################################################################
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

NEWS="${REPO}/apps/search/news" API="${REPO}/apps/search/serve/api" \
NEWS_SQLITE="$tmp/news/news.sqlite" SEARCH_NEWS_BACKEND=sqlite SEARCH_NEWS_SQLITE="$tmp/news/news.sqlite" \
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import datetime as dt, gzip, json, os, sys, types
fails = checks = 0
def check(name, fn):
    global fails, checks
    checks += 1
    try:
        ok, detail = fn(), ""
        if isinstance(ok, tuple):
            ok, detail = ok
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{str(detail)[:300]}]")); fails += 0 if ok else 1
def step(name, fn):
    try:
        return fn()
    except Exception as e:
        print(f"  (setup '{name}' raised {type(e).__name__}: {str(e)[:200]})")

# psycopg is only an error base class here (no Postgres in this test)
pg = types.ModuleType("psycopg"); pg.Error = type("Error", (Exception,), {}); sys.modules["psycopg"] = pg
sys.path.insert(0, os.environ["NEWS"])
import poller, wxall, newsdb
utc = dt.timezone.utc
now = dt.datetime.now(utc)
def sql(q, p=()):
    with newsdb.connect() as c:
        return c.execute(q, p).fetchall()

print("=== writers: poller.py on the news file ===")
poller.ensure_database()
A = {"url": "https://a.example/rss", "section": "local-home", "name": "Shore News", "lean": "-", "kind": "rss", "poll": 15}
B = {"url": "https://b.example/rss", "section": "national", "name": "Wire Daily", "lean": "-", "kind": "rss", "poll": 15}
def item(guid, title, hours_ago, summary=""):
    return {"guid": guid, "url": f"https://x.example/{guid}", "title": title, "summary": summary, "author": None,
            "published": now - dt.timedelta(hours=hours_ago)}
def store(feed, items):
    with poller.connect() as c:
        return poller.store(c, feed["url"], items)
def due():
    with poller.connect() as c:
        return poller.due(c)
step("sync", lambda: poller.sync_feeds(poller.connect(), [A, B]))
n = step("store A", lambda: store(A, [item("a1", "Storm floods Twin Falls shore", 2, "Coastal flooding hit the shore"),
                                      item("a2", "Council approves school budget", 5),
                                      item("a3", "Old storm story", 24 * 30)]))
check("store keeps the two fresh items, drops the month-old one", lambda: (n == 2, n))
check("storing the same guids again adds nothing", lambda: store(A, [item("a1", "Storm floods Twin Falls shore", 2)]) == 0)
step("store B", lambda: store(B, [item("b1", "Storm warning across the plains", 1), item("b2", "Markets rise", 3)]))
check("both new feeds are due now", lambda: ({f["url"] for f in due()} == {A["url"], B["url"]}, due()))
step("ok A", lambda: poller._ok(poller.connect(), next(f for f in due() if f["url"] == A["url"]), 2, "etag-1", None))
check("a polled feed is no longer due", lambda: {f["url"] for f in due()} == {B["url"]})
check("the poll's etag, next time (in the future) and count are written",
      lambda: (lambda r: (r[0] == "etag-1" and float(r[1]) > now.timestamp() and r[2] == 2, r))(
          sql("SELECT etag, next_due, items_total FROM feeds WHERE url = %s", (A["url"],))[0]))
step("fail B", lambda: poller._fail(poller.connect(), due()[0], 503, None))
check("a failed feed is put off too", lambda: due() == [])

print("=== writers: wxall.py on the news file (network stubbed) ===")
wxall.fetch = lambda url, accept="*/*": gzip.compress(b"") if url == wxall.METARS else b"{}"
wxall._unzip_first = lambda blob: ""
wxall._unzip_lines = lambda blob: iter(())
wxall.parse_towns = lambda *a: [("twin falls", "Twin Falls", "NJ", 40.633, -105.916, 95000, ["34991"], "America/New_York", "township", "Marsh County"),
                                ("twin falls", "Twin Falls", "XX", 10.0, 10.0, 10, ["99999"], "UTC", "city", None)]
metar = {"temp_f": 63}
wxall.parse_metars = lambda text: {"KZZX": {"lat": 40.613, "lon": -106.006, "now": {"temp_f": metar["temp_f"], "text": "Overcast"}}}
c0 = now.replace(minute=0, second=0, microsecond=0)
d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)          # daily columns sit at 00/12 UTC
iso = lambda h: (c0 + dt.timedelta(hours=h)).isoformat()
wxall.latest_cycle = lambda product, t: ("u", c0)
wxall.parse_nbm = lambda text, keep: {"KZZX": {"valid": [iso(h) for h in range(1, 26)], "rows": {"TMP": list(range(60, 85)), "P01": [0] * 25, "SKY": [5] * 25, "T01": [0] * 25}}
                                      if keep == wxall.KEEP["nbhtx"] else
                                      {"valid": [(d0 + dt.timedelta(hours=h)).isoformat() for h in (24, 36, 48, 60, 72)], "rows": {"TXN": [52, 68, 50, 71, 55], "P12": [10, 20, 70, 5, 0], "SKY": [40, 20, 90, 10, 10], "T12": [0, 0, 40, 0, 0]}}}
wxall.compact = lambda st, labels: st
wxall.parse_alerts = lambda data: [{"id": "al1", "event": "Coastal Flood Warning", "severity": "Severe", "headline": "Flooding at high tide",
                                    "ends": None, "same": ["34991"], "poly": None},
                                   {"id": "al2", "event": "Heat Advisory", "severity": "Moderate", "headline": "Hot", "ends": None,
                                    "same": ["06037"], "poly": None}]
newsdb.ensure_sqlite_schema()
def wx_load():
    with newsdb.connect() as c:
        seen = {}
        wxall.load_towns(c); wxall.load_metars(c)
        wxall.load_nbm(c, "nbhtx", seen); wxall.load_nbm(c, "nbetx", seen); wxall.load_alerts(c)
step("wx load", wx_load)
check("towns stored, counties as JSON", lambda: json.loads(sql("SELECT counties FROM wx_towns WHERE state = 'NJ'")[0][0]) == ["34991"])
check("forecast hours + days stored for the station", lambda: sql("SELECT hours IS NOT NULL AND days IS NOT NULL FROM wx_fc WHERE id = 'KZZX'")[0][0] == 1)
check("alerts stored", lambda: sql("SELECT count(*) FROM wx_alerts")[0][0] == 2)
metar["temp_f"] = 66
step("metars again", lambda: wxall.load_metars(newsdb.connect()))
check("a second metar load updates the observation in place",
      lambda: (lambda r: (len(r) == 1 and json.loads(r[0][0])["temp_f"] == 66, r))(sql("SELECT obs FROM wx_obs")))

print("=== reader: search_api through news_sqlite ===")
sys.path.insert(0, os.environ["API"])
import search_api as a, news_sqlite
def news(q, h=48):
    return a._news_rows(a.NEWS_SQL, {"q": q, "h": h, "n": 80}) or []
check("headline search finds the matching items, best first", lambda: (news("twin falls storm")[0][0] == "Storm floods Twin Falls shore", [r[0] for r in news("twin falls storm")]))
check("an item with no query word is not returned", lambda: news("twin falls storm") and all(r[0] != "Markets rise" for r in news("twin falls storm")))
check("any query word retrieves (the word filter is rank_news's): 'storm budget' finds both",
      lambda: ({"Storm floods Twin Falls shore", "Council approves school budget"} <= {r[0] for r in news("storm budget")}, [r[0] for r in news("storm budget")]))
check("published comes back as an aware datetime", lambda: isinstance(news("storm")[0][3], dt.datetime) and news("storm")[0][3].tzinfo is not None)
check("bm25 is positive (higher = better) and age is in hours",
      lambda: (lambda r: (float(r[7]) > 0 and 1.5 < float(r[8]) < 2.5, r[7:]))(news("twin falls storm")[0]))
check("stemming: 'flood' matches 'floods'", lambda: any("Twin Falls" in r[0] for r in news("flood")))
check("a query of stop words only finds nothing (no error)", lambda: a._news_rows(a.NEWS_SQL, {"q": "the of and", "h": 48, "n": 80}) == [])
check("FTS syntax in a query is inert", lambda: isinstance(a._news_rows(a.NEWS_SQL, {"q": 'storm" OR NEAR(x', "h": 48, "n": 80}), list))
check("weather stories come from local sections only",
      lambda: (lambda w: (bool(w) and {r[6] for r in w} == {"local-home"}, w))(a._news_rows(a.WEATHER_NEWS_SQL, {"sec": list(a.NEWS_LOCAL_SECTIONS), "h": 48})))
check("towns: TOWN_SQL answers", lambda: a._news_rows(a.TOWN_SQL, {"k": "twin falls", "s": "NJ"})[:1] == [("Twin Falls", "township", "Marsh County", 95000)])
check("alerts: the SQL keeps only the county's (and polygon) alerts",
      lambda: (lambda r: ([x[0] for x in r] == ["Coastal Flood Warning"] and r[0][4] == ["34991"], r))(
          news_sqlite.NewsSqlite(os.environ["SEARCH_NEWS_SQLITE"]).execute(a.WX_ALERTS_SQL, {"c": ["34991"]}).fetchall()))
a.HOME = None; a._near.set(None)
w = step("weather", lambda: a.weather("weather in twin falls nj")) or {}
check("weather for a named town reads the file end to end", lambda: (w.get("place") == "Twin Falls, NJ" and w["now"].get("temp_f") == 66, w.get("place")))
check("its forecast has hours and days", lambda: len(w["hours"]) > 0 and len(w["days"]) > 0)
check("only the alert for the town's county is shown", lambda: ([x["event"] for x in w["alerts"]] == ["Coastal Flood Warning"], w.get("alerts")))
check("the forecast cycle is an aware datetime (not stale)", lambda: (w["stale"] is False and w["updated"] == c0.isoformat(), (w.get("updated"), w.get("stale"))))
def raises_news_error(fn):
    try:
        fn()
    except news_sqlite.NewsError:
        return True
    return False
check("a statement the file lacks raises NewsError (a psycopg.Error)",
      lambda: raises_news_error(lambda: news_sqlite.NewsSqlite(os.environ["SEARCH_NEWS_SQLITE"]).execute("SELECT 1 FROM items"))
      and issubclass(news_sqlite.NewsError, pg.Error))

print("=== currency: the ECB rates fx.py stores, read by search_api for the answer box ===")
import fx
fx.log = lambda m: None
today = now.date().isoformat()
reads = []
real_fx = a.fx_rates
a.fx_rates = lambda: (reads.append(1), real_fx())[1]
check("no rates stored yet: fx_rates is None and there is no currency answer (no error)",
      lambda: (real_fx() is None and a.instant_answer("100 usd to eur") is None, real_fx()))
a._cache.clear()
step("fx store", lambda: fx.store(newsdb.connect(), today, {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}, now.timestamp()))
check("fx_rates gives the date and the table as the answer code wants them",
      lambda: (real_fx() == {"date": today, "checked": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_eur": {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}}, real_fx()))
check("100 usd to eur is answered from the file, with the ECB's date",
      lambda: (lambda r: (r == {"answer": "92.59 EUR", "title": "100 USD", "detail": f"1 USD = 0.9259 EUR · ECB reference rate of {now:%b} {now.day} · checked just now", "question": "currency"}, r))(
          a.instant_answer("100 usd to eur")))
n_reads = len(reads)
step("fx change", lambda: fx.store(newsdb.connect(), today, {"USD": 2.0, "GBP": 0.85, "JPY": 160.0}, now.timestamp()))
check("the next searches use the cached rates: the file is not read per search",
      lambda: ([a.instant_answer(q)["answer"] for q in ("100 usd to eur", "100 gbp to jpy", "usd to eur")] == ["92.59 EUR", "18,824 JPY", "0.9259 EUR"]
               and len(reads) == n_reads, len(reads) - n_reads))
check("a sum, a unit or a search never reads the rates",
      lambda: (a.instant_answer("2+2")["answer"] == "4" and a.instant_answer("5 pounds in kg")["question"] == "unit" and a.instant_answer("dollar tree") is None
               and len(reads) == n_reads, len(reads) - n_reads))
a._cache.clear()
check("once the cache has run out the new rates are used", lambda: (a.instant_answer("100 usd to eur")["answer"] == "50.00 EUR", a.instant_answer("100 usd to eur")))
a._cache.clear()
step("fx old", lambda: fx.store(newsdb.connect(), (now - dt.timedelta(days=9)).date().isoformat(), {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}, now.timestamp()))
check("rates the poller has not renewed for over a week are not an answer", lambda: a.instant_answer("100 usd to eur") is None)
a._cache.clear()
def as_postgres():
    a.NEWS_BACKEND = "postgres"
    try:
        return real_fx(), a.instant_answer("100 usd to eur")
    finally:
        a.NEWS_BACKEND = "sqlite"
step("fx unchecked", lambda: fx.store(newsdb.connect(), today, {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}, now.timestamp() - 5 * 3600))
check("the time of the poller's last successful fetch reaches the answer", lambda: (a.instant_answer("100 usd to eur")["detail"].endswith(" · checked 5 h ago"), a.instant_answer("100 usd to eur")))
a._cache.clear()
step("fx dead poller", lambda: fx.store(newsdb.connect(), today, {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}, now.timestamp() - 4 * 86400))
check("a rate not checked for over three days (a dead poller) is not an answer", lambda: a.instant_answer("100 usd to eur") is None)
a._cache.clear()
check("the Postgres news backend has no rates and no currency answer", lambda: (as_postgres() == (None, None), as_postgres()))
a._cache.clear()
step("fx drop", lambda: newsdb.connect().execute("DROP TABLE fx_rates"))
check("a news file without the table (an older poller) is no answer, not an error",
      lambda: (real_fx() is None and a.instant_answer("100 usd to eur") is None and a.instant_answer("2+2")["answer"] == "4", real_fx()))
check("... and the news connection still serves headlines afterwards", lambda: len(news("twin falls storm")) > 0)
a.fx_rates = real_fx; a._cache.clear()
step("fx table back", newsdb.ensure_sqlite_schema)

print("=== scoreboard + home weather (sports.py / weather.py) on the file ===")
import sports, weather as homewx
G = lambda gid, hours, state, away, home, keys_a, keys_h: {"league": "mlb", "game_id": gid, "start_utc": now + dt.timedelta(hours=hours),
    "state": state, "detail": "", "venue": "Citi Field", "broadcasts": ["SNY", "ESPN"], "url": None,
    "away": {"name": away, "nick": away.split()[-1], "abbr": None, "score": 3, "keys": keys_a},
    "home": {"name": home, "nick": home.split()[-1], "abbr": None, "score": 5, "keys": keys_h}}
def sports_write():
    with newsdb.connect() as c:
        sports.store(c, [G("g1", 0.15, "pre", "Atlanta Braves", "New York Mets", ["braves", "atlanta braves"], ["mets", "new york mets"]),
                         G("g2", -48, "final", "New York Mets", "Miami Marlins", ["mets"], ["marlins"]),
                         G("g3", 30, "pre", "Boston Red Sox", "New York Yankees", ["red sox"], ["yankees"])])
        sports.store_teams(c, [{"league": "mlb", "name": "New York Mets", "nick": "Mets", "keys": ["mets", "new york mets"]}])
        return sports.busy(c, "mlb"), sports.busy(c, "nhl")
busy = step("sports write", sports_write)
check("sports.busy: a game starting within the window makes the league busy; another league is idle", lambda: (busy == (True, False), busy))
games = step("sports read", lambda: a._sports_rows(a.SPORTS_SQL, {"k": ["mets"]})) or []
check("SPORTS_SQL: the team's games in the window, soonest first (the 2-days-old final is outside)",
      lambda: ([g[1] for g in games] == ["g1"], [g[1] for g in games]))
check("... start is an aware datetime, broadcasts a list, away_hit a bool",
      lambda: isinstance(games[0][2], dt.datetime) and games[0][2].tzinfo and games[0][12] == ["SNY", "ESPN"] and games[0][14] is False)
check("SPORTS_SQL: away_hit true when the key is the away team's", lambda: [g[14] for g in a._sports_rows(a.SPORTS_SQL, {"k": ["braves"]})] == [True])
check("TEAM_SQL: a team key finds its team", lambda: a._sports_rows(a.TEAM_SQL, {"k": ["mets"]}) == [("mlb", "New York Mets", "Mets")])
check("TEAM_SQL: an unknown key finds nothing", lambda: a._sports_rows(a.TEAM_SQL, {"k": ["dodgers"]}) == [])
step("home weather", lambda: homewx.store_weather(newsdb.connect(), {"now": {"temp_f": 58, "text": "Clear"}, "days": [{"name": "Today"}],
                                                                      "hours": [], "alerts": [], "place": "Home, NJ"}, "nws", now))
hw = step("home weather read", lambda: a.weather("weather")) or {}
check("the home forecast is read from the file (payload as a dict, fresh)", lambda: (hw.get("now", {}).get("temp_f") == 58 and hw.get("stale") is False and hw.get("focus") == "now", hw))
def wx_split():
    import shutil
    other = os.environ["SEARCH_NEWS_SQLITE"] + ".shared"
    shutil.copy(os.environ["SEARCH_NEWS_SQLITE"], other)
    homewx.store_weather(newsdb.SqliteConn(other), {"now": {"temp_f": 61, "text": "Cloudy"}, "days": [], "hours": [], "alerts": []}, "nws", now)
    a.WX_SQLITE, a._news_pool[:], a._wx_pool[:] = other, [], []
    try:
        return (a.weather("weather") or {}).get("now", {}).get("temp_f"), len(news("twin falls storm"))
    finally:
        a.WX_SQLITE, a._news_pool[:], a._wx_pool[:] = os.environ["SEARCH_NEWS_SQLITE"], [], []
check("SEARCH_WX_SQLITE: weather from the shared file, headlines still from the news file",
      lambda: (lambda r: (r[0] == 61 and r[1] > 0, r))(wx_split()))
def mine_status():
    M = {"url": "https://mine.example/rss", "section": "mine", "name": "My feed", "lean": "-", "kind": "rss", "poll": 15}
    with poller.connect() as c:
        poller.sync_feeds(c, [A, M])
        poller._ok(c, dict(M, poll_min=15, idle_polls=0), 3, None, None)
    import native_host
    os.environ["NATIVE_SETTINGS"] = os.environ["SEARCH_NEWS_SQLITE"] + ".settings.json"
    native_host.settings_path = lambda: os.environ["NATIVE_SETTINGS"]
    return native_host.settings_status()["feeds"]
check("native_host: the user's own feeds' status comes from the news file",
      lambda: (lambda st: (list(st) == ["https://mine.example/rss"] and st["https://mine.example/rss"]["ok"] is True
                           and st["https://mine.example/rss"]["items"] == 3 and abs(st["https://mine.example/rss"]["at"] - now.timestamp()) < 60, st))(mine_status()))
def prune():
    sports.KEEP_DAYS_BACK = 1
    with newsdb.connect() as c:
        sports.prune_games(c)
        return [r[0] for r in c.execute("SELECT game_id FROM games ORDER BY game_id").fetchall()]
check("sports.prune_games removes games older than KEEP_DAYS_BACK only", lambda: (prune() == ["g1", "g3"], prune()))
step("drop B", lambda: poller.sync_feeds(poller.connect(), [A]))
check("a feed dropped from the list takes its items with it", lambda: (sql("SELECT count(*) FROM items WHERE feed_url = %s", (B["url"],))[0][0] == 0,
                                                                     sql("SELECT count(*) FROM items WHERE feed_url = %s", (B["url"],))))
check("... and search no longer finds them", lambda: news("storm warning plains") == [] or all(r[4] != "Wire Daily" for r in news("storm warning plains")))
check("... and the headline index forgot them", lambda: sql("SELECT count(*) FROM items_fts WHERE items_fts MATCH 'plains'")[0][0] == 0)
step("age a2", lambda: newsdb.connect().execute("UPDATE items SET published = %s, title = %s WHERE guid = 'a2'",
                                                 (newsdb.ts(now - dt.timedelta(days=30)), "Council approves levy")))
check("an updated item's time is what the window sees", lambda: all(r[0] != "Council approves levy" for r in news("council")))
check("an updated item's title is what the index sees", lambda: len(news("levy", h=24 * 60)) == 1 and news("budget", h=24 * 60) == [])
print(f"\nTotal: {checks}  PASS: {checks - fails}  FAIL: {fails}")
sys.exit(1 if fails else 0)
PY
