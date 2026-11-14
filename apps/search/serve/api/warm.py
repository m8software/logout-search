"""Keep local search warm (2026-09-24). Runs as the search-warm container (search-api image).

WARM_HEAD=N (2026-11-04, no Postgres): keep the N most-searched names answered from the engine's cache instead.
The names come from the release itself (lookup.sqlite suggest_items, ranked by signals.bin suggest_demand; sites
and Wikipedia articles, "Inception (film)" asked as "inception"). Each is asked as the page asks on Enter
(/instant?full=1) plus warm=1, which recomputes an entry past half its life: with a cycle of a quarter of the
engine's static TTL, a name never expires between visits. No query a person typed is read or kept.

Every WARM_EVERY_S seconds: pg_prewarm('read') over the serving database's indexes (~16 GB;
cheap while they sit in the ZFS ARC, restores them after a reboot or an evicting read) and
one real search through search-api, which also exercises the query path and the embedding
model. Logs each pass's duration: a slow pass means search had gone cold.
"""
import collections
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

EVERY = int(os.environ.get("WARM_EVERY_S", "900"))
API = os.environ.get("WARM_API", "http://search-api:8090")
PREWARM = ("SELECT count(*) FROM (SELECT pg_prewarm(c.oid, 'read') FROM pg_class c "
           "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'i') x")


HEAD = int(os.environ.get("WARM_HEAD", "0"))
HEAD_CYCLE = int(os.environ.get("WARM_HEAD_CYCLE_S", "5400"))        # SEARCH_CACHE_STATIC_TTL_S (6 h) / 4
REL = os.environ.get("WARM_REL", "/rel")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def head_name(name: str) -> str | None:
    """A suggestion's name as a person types it: no trailing "(film)", lower case; None for one nobody types."""
    n = _PAREN.sub("", name or "").strip().lower()
    n = re.sub(r"\s+", " ", n)
    return n if 2 <= len(n) <= 60 and re.search(r"[a-z0-9]", n) else None


def head_queries(n: int, rel: str = REL) -> list[str]:
    """The n names with the most suggest demand, sites and Wikipedia articles only (links carry page titles such as
    "Your browser is deprecated"), each once. The demand arrays are read whole (memory-mapped) and only the leading
    candidates' names are fetched: the full list is 2.8 M names, too many to hold in a small container."""
    import numpy as np
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lookups_release as lr
    sig = lr.Signals(rel)
    if not sig.t5:
        raise ValueError(f"{rel}/signals.bin: not the T5 format; WARM_HEAD reads only T5 releases")
    cand = []                                            # (demand, doc_id)
    for t, name in lr.T5_NAMES.items():
        if name not in ("wiki", "site"):
            continue
        arr = sig.f.get(f"{name}.suggest_demand")
        if arr is None or not len(arr):
            continue
        a = np.nan_to_num(np.asarray(arr, dtype=np.float64), nan=0.0)
        m = min(len(a), 4 * n)
        top = np.argpartition(-a, m - 1)[:m]
        cand += [(float(a[i]), (t << sig.shift) | int(i)) for i in top if a[i] > 0]
    cand.sort(reverse=True)
    db = sqlite3.connect(f"file:{rel}/lookup.sqlite?mode=ro", uri=True)
    out, seen = [], set()
    for k in range(0, len(cand), 500):
        ids = [d for _dem, d in cand[k:k + 500]]
        names = dict(db.execute(f"SELECT doc_id, name FROM suggest_items WHERE doc_id IN ({','.join('?' * len(ids))})", ids))
        for d in ids:
            q = head_name(names.get(d, ""))
            if q and q not in seen:
                seen.add(q)
                out.append(q)
                if len(out) >= n:
                    db.close()
                    return out
    db.close()
    return out


# Beside the popular names (2026-11-04, owner: "also include what's in news headlines, current sports, weather in
# major metropolitan us cities"): what was searched here more than once, today's teams, the big cities' weather and
# the names in the day's headlines. Each source is optional: a missing file or table gives nothing.
EVENTS_DB = os.environ.get("WARM_EVENTS_DB", "")           # the gateway's counts (gateway.py SCHEMA); "" = off
NEWS_DB = os.environ.get("WARM_NEWS_DB", "")               # news.sqlite: items, games, wx_towns; "" = off
SEARCHED_MIN = int(os.environ.get("WARM_SEARCHED_MIN", "2"))
SEARCHED_DAYS = int(os.environ.get("WARM_SEARCHED_DAYS", "30"))
CITIES = int(os.environ.get("WARM_CITIES", "50"))
NEWS_MAX = int(os.environ.get("WARM_NEWS_MAX", "300"))
NEWS_MIN_HEADLINES = int(os.environ.get("WARM_NEWS_MIN_HEADLINES", "4"))
_WORD = re.compile(r"[^\W_]+(?:['’.\-][^\W_]+)*", re.UNICODE)


def _ro(path: str):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def searched_queries(db_path: str = EVENTS_DB, min_n: int = SEARCHED_MIN, days: int = SEARCHED_DAYS) -> list[str]:
    """What people pressed Enter on here at least min_n times in the last `days` days, most first. Read from the
    gateway's day counts; nothing is written and no query is printed."""
    if not db_path or not os.path.exists(db_path):
        return []
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    db = _ro(db_path)
    try:
        rows = db.execute("SELECT q, sum(n) AS c FROM events WHERE kind = 'search' AND day >= ? GROUP BY q "
                          "HAVING c >= ? ORDER BY c DESC, q", (since, min_n)).fetchall()
    finally:
        db.close()
    return [q for q, _c in rows if head_name(q)]


def sports_queries(db_path: str = NEWS_DB, now: float | None = None) -> list[str]:
    """The teams of games from 6 hours ago to 30 hours ahead, by name and nickname ("kansas city chiefs", "chiefs")."""
    if not db_path or not os.path.exists(db_path):
        return []
    now = time.time() if now is None else now
    db = _ro(db_path)
    try:
        rows = db.execute("SELECT away_name, away_nick, home_name, home_nick FROM games WHERE start_utc BETWEEN ? AND ? "
                          "ORDER BY start_utc", (now - 6 * 3600, now + 30 * 3600)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    out = []
    for row in rows:
        for v in row:
            q = head_name(v or "")
            if q and q not in out:
                out.append(q)
    return out


SPORTS_NEWS_MAX = int(os.environ.get("WARM_SPORTS_NEWS_MAX", "200"))


def sports_news_queries(db_path: str = NEWS_DB, rel: str = REL, now: float | None = None) -> list[str]:
    """Teams and players in the sports feeds' last 48 hours of headlines (owner 2026-11-04: "sports can use headlines
    in the RSS feeds as well"): any league, also those public has no scores for, by the news rules, 3+ headlines."""
    return news_queries(db_path, rel, 48, SPORTS_NEWS_MAX, 3, now, section="sports")


def weather_queries(db_path: str = NEWS_DB, n: int = CITIES) -> list[str]:
    """"weather in X" and "X weather" for the n largest US cities (wx_towns, by population)."""
    if not db_path or not os.path.exists(db_path) or n <= 0:
        return []
    db = _ro(db_path)
    try:
        rows = db.execute("SELECT name FROM wx_towns WHERE kind = 'city' ORDER BY pop DESC LIMIT ?", (n,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    out = []
    for (name,) in rows:
        c = head_name(name)
        if c:
            out += [f"weather in {c}", f"{c} weather"]
    return out


def news_queries(db_path: str = NEWS_DB, rel: str = REL, hours: int = 24, n: int = NEWS_MAX,
                 min_headlines: int = NEWS_MIN_HEADLINES, now: float | None = None, section: str | None = None) -> list[str]:
    """Names in the last `hours` of headlines: a run of 1-3 words in at least min_headlines headlines that is the
    name of a Wikipedia article or site in the release (lookup.sqlite names, exact), most headlines first. Stop
    words alone never count."""
    if not db_path or not os.path.exists(db_path) or n <= 0:
        return []
    now = time.time() if now is None else now
    db = _ro(db_path)
    try:
        if section:
            titles = [t for (t,) in db.execute("SELECT i.title FROM items i JOIN feeds f ON f.url = i.feed_url "
                                                "WHERE f.section = ? AND i.published > ?", (section, now - hours * 3600))]
        else:
            titles = [t for (t,) in db.execute("SELECT title FROM items WHERE published > ?", (now - hours * 3600,))]
    except sqlite3.Error:
        titles = []
    finally:
        db.close()
    try:
        stop = {w.strip() for w in open(f"{rel}/english.stop", encoding="utf-8") if w.strip()}
    except OSError:
        stop = set()
    # A name is capitalised mid-sentence; a common word is not. Only sentence-case headlines ("Trump says tariffs
    # rise", AP style) can tell them apart, title case ("Trump Says Tariffs Rise") capitalises everything: a run
    # counts when, in sentence-case headlines, it is capitalised (not as the first word) at least 80% of the time.
    seen_in = {}                                          # lower-cased run -> [headlines, capitalised, lower, spelling]
    for t in titles:
        words = _WORD.findall(t or "")
        long_ = [w for w in words if len(w) > 3]
        title_case = bool(long_) and sum(w[:1].isupper() for w in long_) / len(long_) > 0.85
        runs = {}
        for i in range(len(words)):
            for k in (1, 2, 3):
                if i + k <= len(words):
                    run = words[i:i + k]
                    if run[0].lower() in stop or run[-1].lower() in stop or any(w.isdigit() for w in run):
                        continue
                    key = " ".join(run).lower()
                    cap = all(w[:1].isupper() for w in run)
                    if key not in runs or (i > 0 and not title_case):
                        runs[key] = (" ".join(run), None if (title_case or i == 0) else cap)
        for key, (sp, cap) in runs.items():
            e = seen_in.setdefault(key, [0, 0, 0, sp])
            e[0] += 1
            if cap is True:
                e[1] += 1
                e[3] = sp
            elif cap is False:
                e[2] += 1
    allc = {k: e[0] for k, e in seen_in.items()}
    cap2 = {k for k, e in seen_in.items() if e[1] >= 1 and k.count(" ") == 1}   # "Mike Tomlin" once is enough
    seen_in = {k: (e[0], e[3]) for k, e in seen_in.items() if e[1] >= 2 and e[1] >= 0.8 * (e[1] + e[2])}
    # pieces: "zealand" mostly inside "new zealand", "mike" mostly before a surname, "nov" / "sen" — dropped.
    # A short single word stays only as an acronym ("NFL", "GOP").
    longer = collections.defaultdict(int)
    first = collections.defaultdict(int)
    for k, c in allc.items():
        ws = k.split(" ")
        if len(ws) > 1:
            for a in range(len(ws)):
                for b in range(a + 1, len(ws) + 1):
                    if (a, b) != (0, len(ws)):
                        sub = " ".join(ws[a:b])
                        longer[sub] = max(longer[sub], c)
            if k in cap2:
                first[ws[0]] += c
    kept = {}
    for k, (c, sp) in seen_in.items():
        if longer.get(k, 0) >= 0.6 * c:
            continue
        if " " not in k:
            if first.get(k, 0) >= 0.5 * c:
                continue
            if len(k) <= 3 and not (sp.isupper() and len(k) >= 2):
                continue
        kept[k] = (c, sp)
    seen_in = kept
    cand = sorted(((c, key, sp) for key, (c, sp) in seen_in.items() if c >= min_headlines), key=lambda x: (-x[0], x[1]))
    names = _ro(f"{rel}/lookup.sqlite")
    out = []
    try:
        for _c, key, sp in cand:
            forms = {sp, key, key.title(), key.replace(" ", "")}
            hit = names.execute(f"SELECT 1 FROM names WHERE name IN ({','.join('?' * len(forms))}) AND kind IN (1, 2) LIMIT 1",
                                list(forms)).fetchone()
            q = head_name(key)
            if hit and q and q not in out:
                out.append(q)
                if len(out) >= n:
                    break
    finally:
        names.close()
    return out


# What English Wikipedia's readers looked up most in the last days (owner 2026-11-04: "movies that are in theaters,
# music in the top 40, major shows"): Wikimedia's daily most-read list (public domain, no key), the freshest free
# signal for films out now, songs and shows airing. Only the date goes out; nothing about a visitor. A day is
# published a day or two late and some are missing, so the last TREND_DAYS that exist are merged. Off: WARM_TREND=0.
TREND = os.environ.get("WARM_TREND", "1") != "0"
TREND_DAYS = int(os.environ.get("WARM_TREND_DAYS", "4"))
TREND_MAX = int(os.environ.get("WARM_TREND_MAX", "500"))
TREND_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{ymd}"
TREND_UA = "logout-search-warm/1.0 (https://search.logout.cloud)"
_NS = re.compile(r"^(Main_Page|Special:|Wikipedia:|File:|Portal:|Talk:|User:|Help:|Template:|Category:|Draft:)")
MEDIA = re.compile(r"\((?:\d{4} )?(?:[A-Za-z ]+ )?(film|TV series|miniseries|series|song|album|band|singer|rapper|musician|soundtrack)\)$")
_trend_days: dict = {}                                      # "YYYY/MM/DD" -> [(article, views)]; a missing day is not kept


def _trend_day(ymd: str, fetch=None) -> list | None:
    if ymd in _trend_days:
        return _trend_days[ymd]
    try:
        if fetch:
            body = fetch(ymd)
        else:
            req = urllib.request.Request(TREND_URL.format(ymd=ymd), headers={"User-Agent": TREND_UA})
            body = json.load(urllib.request.urlopen(req, timeout=20))
        arts = [(a["article"], int(a.get("views") or 0)) for a in body["items"][0]["articles"]]
    except Exception:                                       # not published yet, a gap, no network: asked again next cycle
        return None
    _trend_days[ymd] = arts
    for old in sorted(_trend_days)[:-14]:
        del _trend_days[old]
    return arts


def trending_queries(n: int = TREND_MAX, days: int = TREND_DAYS, now: float | None = None, fetch=None) -> list[str]:
    """The most-read English Wikipedia articles of the last `days` published days, views added up, as typed
    ("Project_Hail_Mary_(film)" -> "project hail mary"). Wikipedia's own pages are left out."""
    if not TREND or n <= 0:
        return []
    now = time.time() if now is None else now
    views = collections.Counter()
    got = 0
    for back in range(1, days + 4):                         # a day or two late, with gaps: look a little further back
        if got >= days:
            break
        arts = _trend_day(time.strftime("%Y/%m/%d", time.gmtime(now - back * 86400)), fetch)
        if arts is None:
            continue
        got += 1
        for a, v in arts:
            if not _NS.match(a):
                views[a] += v
    out, media = [], 0
    for a, _v in views.most_common():
        q = head_name(a.replace("_", " "))
        if q and q not in out:
            out.append(q)
            media += bool(MEDIA.search(a.replace("_", " ")))
            if len(out) >= n:
                break
    trending_queries.media = media                          # films, shows and music among them, for the cycle's log line
    return out


def warm_list(n_head: int = HEAD) -> tuple[list[str], dict]:
    """The cycle's list, each query once: searched here, sports, weather, headlines, then the popular names."""
    out, seen, counts = [], set(), {}
    for label, fn in (("searched", searched_queries), ("sports", sports_queries), ("sports_news", sports_news_queries),
                      ("weather", weather_queries),
                      ("news", news_queries), ("trending", trending_queries), ("popular", lambda: head_queries(n_head))):
        try:
            got = fn()
        except Exception as e:                             # one source failing never stops the others
            got = []
            counts[label + "_error"] = type(e).__name__
        new = [q for q in got if q not in seen]
        seen.update(new)
        out += new
        counts[label] = len(new)
    if getattr(trending_queries, "media", None) is not None and counts.get("trending"):
        counts["trending_film_tv_music"] = trending_queries.media
    return out, counts


AUDIT_SHARE = float(os.environ.get("WARM_AUDIT_SHARE", "0.01"))   # names per cycle computed afresh and compared
# and stored searches drawn from the whole store (rows ship.sh brought from another engine too), audited the same way
AUDIT_STORED = int(os.environ.get("WARM_AUDIT_STORED", "20"))
REBUILD_WORKERS = int(os.environ.get("WARM_REBUILD_WORKERS", "2"))
IDLE_WAIT_S = float(os.environ.get("WARM_IDLE_WAIT_S", "10"))      # the longest wait for visitors' searches to finish
_last_fp = [None]


def storez(api: str = API) -> dict:
    """The engine's store counts and visitors in flight; {} when it has none (an older engine)."""
    try:
        return json.load(urllib.request.urlopen(f"{api}/storez", timeout=5))
    except Exception:
        return {}


def storez_sample(api: str, n: int) -> list:
    """n random stored searches' words, from the engine (back network only); [] from an older engine."""
    try:
        got = json.load(urllib.request.urlopen(f"{api}/storez?sample={int(n)}", timeout=10)).get("sample")
        return [q for q in got if isinstance(q, str) and q] if isinstance(got, list) else []
    except Exception:
        return []


def wait_idle(api: str = API, limit: float = IDLE_WAIT_S) -> None:
    """Hold the next warm request while a visitor's search is in flight (at most `limit` seconds)."""
    t = time.time()
    while time.time() - t < limit and storez(api).get("busy", 0) > 0:
        time.sleep(0.2)


# The warmer files a FINISHED answer for each name (2026-11-14, owner: "yes switch the warmer to finished answers"):
# keyed by release, so a deploy no longer empties what it built, and today's names (searched here, sports, weather,
# headlines, trending) join the answers the home builder files. WARM_FIN=0: rows under the fingerprint only, as before.
FIN = os.environ.get("WARM_FIN", "1") != "0"


def ask(api: str, q: str, audit: bool = False) -> dict:
    """One warm request; the engine's note on how it was answered: cache hit|miss|disk, audit same|diff."""
    qs = {"q": q, "full": "1", "warm": "1", **({"audit": "1"} if audit else {"fin": "1"} if FIN else {})}
    d = json.load(urllib.request.urlopen(f"{api}/instant?" + urllib.parse.urlencode(qs), timeout=60))
    return d.get("store") or {}


def extra_list(path: str = None) -> tuple[list[str], dict]:
    """The second tier (apps/search/twin: the 20,000 open-data names ship.sh computes for public), one per line;
    ([], {}) when the file is missing. For an engine that computes them itself (the home engine)."""
    path = path or EXTRA_LIST
    try:
        with open(path, encoding="utf-8") as f:
            qs = list(dict.fromkeys(ln.strip() for ln in f if ln.strip()))
    except OSError:
        return [], {"tier2_missing": 1}
    return qs, {"tier2": len(qs)}


EXTRA_LIST = os.environ.get("WARM_EXTRA_LIST", "")              # "" = off
EXTRA_CYCLE = int(os.environ.get("WARM_EXTRA_CYCLE_S", "21600"))  # 6 h: rows refresh at 12 h, expire at 24 h
_extra_fp = [None]


def head_cycle(api: str = API, lst=None, cycle_s: float | None = None, last_fp: list | None = None,
               audit_stored: int | None = None) -> str:
    """One pass over the warm list. After a deploy (a new fingerprint) the list is first rebuilt flat out with
    REBUILD_WORKERS in order (searched, sports, news ... first); otherwise spread over HEAD_CYCLE seconds, each
    request only when no visitor's search is in flight. AUDIT_SHARE of the names are computed afresh and compared.
    lst / cycle_s / last_fp: another list on its own cadence (the second tier, extra_list)."""
    import random
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    last_fp = _last_fp if last_fp is None else last_fp
    cycle_s = HEAD_CYCLE if cycle_s is None else cycle_s
    audit_stored = AUDIT_STORED if audit_stored is None else audit_stored
    qs, counts = lst() if lst else warm_list(HEAD)
    fp = storez(api).get("fp")
    rebuild = fp is not None and fp != last_fp[0]
    last_fp[0] = fp
    audit = set(random.sample(range(len(qs)), max(1, int(len(qs) * AUDIT_SHARE)))) if qs and fp else set()
    if fp and audit_stored > 0:          # stored rows this list did not ask for (shipped ones): audited at the end
        mine = {q.lower() for q in qs}
        extra = [q for q in (storez_sample(api, audit_stored * 2)) if q.lower() not in mine][:audit_stored]
        audit |= set(range(len(qs), len(qs) + len(extra)))
        qs = qs + extra
        if extra:
            counts = dict(counts, stored_sample=len(extra))
    tally = collections.Counter()

    def one(iq):
        i, q = iq
        wait_idle(api)
        try:
            r = ask(api, q, i in audit)
            tally[r.get("cache", "?")] += 1
            if r.get("note") in ("same", "diff"):
                tally["audit_" + r["note"]] += 1
        except Exception:
            tally["failed"] += 1

    if rebuild:
        with ThreadPoolExecutor(REBUILD_WORKERS) as ex:
            list(ex.map(one, enumerate(qs)))
    else:
        gap = cycle_s / max(len(qs), 1)
        for i, q in enumerate(qs):
            if fp and i and i % 50 == 0 and storez(api).get("fp") not in (None, fp):
                tally["cut_short_new_fingerprint"] += 1      # a deploy: end this cycle; the next one rebuilds flat out
                break
            one((i, q))
            time.sleep(max(0.0, t0 + (i + 1) * gap - time.time()))
    parts = ", ".join(f"{k} {v}" for k, v in counts.items())
    got = ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
    warn = " WARNING: stored results differ from fresh ones" if tally.get("audit_diff") else ""
    return (f"{'rebuild ' if rebuild else ''}head: {len(qs)} queries ({parts}) in {time.time() - t0:.0f} s: {got}"
            f"; store {fp or 'off'}{warn}")


def once() -> str:
    import psycopg
    t0 = time.time()
    try:
        with psycopg.connect(autocommit=True, connect_timeout=10) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
            n = c.execute(PREWARM).fetchone()[0]
    except Exception as e:  # the next pass tries again
        return f"prewarm failed: {type(e).__name__}"
    t1 = time.time()
    try:
        r = json.load(urllib.request.urlopen(f"{API}/search?k=3&q=wikipedia", timeout=15))
        probe = f"{len(r.get('results', []))} results"
    except Exception as e:
        probe = f"search failed: {type(e).__name__}"
    return f"warmed {n} indexes in {t1 - t0:.1f} s; probe search {time.time() - t1:.2f} s ({probe})"


def in_process(handler_cls, delay_s: float | None = None) -> None:
    """The head warmer inside an engine process (2026-11-04: the home engine answers over WebRTC and has no HTTP
    port): the same cycle and list, asking search_api's /instant code directly with warm=1. Never returns."""
    import search_api
    global storez, storez_sample, ask

    class Local(handler_cls):
        def __init__(self):                           # no socket: only _instant/_send are used
            self.body = None

        def _send(self, code, body, ctype="application/json"):
            self.body = (code, body)

        def log_message(self, fmt, *args):
            pass

    def storez(api=None):                             # noqa: F811 - in-process stand-ins for the HTTP calls
        st = search_api.result_store()
        return {"busy": search_api._busy[0], **(st.counts() if st else {"off": True})}

    def storez_sample(api, n):                        # noqa: F811
        st = search_api.result_store()
        return st.sample_queries(int(n)) if st else []

    def ask(api, q, audit=False):                     # noqa: F811
        h = Local()
        h._instant({"q": [q], "full": ["1"], "warm": ["1"], **({"audit": ["1"]} if audit else {})})
        code, raw = h.body
        if code != 200:
            raise RuntimeError(f"instant {code}")
        return json.loads(raw).get("store") or {}

    time.sleep(int(os.environ.get("WARM_HEAD_DELAY_S", "120")) if delay_s is None else delay_s)
    if storez().get("off"):
        print("warm: no result store here; the in-process warmer stops", flush=True)
        return
    if EXTRA_LIST:                                   # the second tier on its own, slower cadence
        import threading
        threading.Thread(target=_loop, args=("tier2 ", lambda: head_cycle("in-process", extra_list, EXTRA_CYCLE, _extra_fp, 0)),
                         daemon=True, name="store-warm-tier2").start()
    _loop("", lambda: head_cycle("in-process"))


def _loop(label: str, cycle) -> None:
    while True:
        try:
            print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), label + cycle(), flush=True)
        except Exception as e:  # noqa: BLE001 - one bad cycle must not end the warmer
            print(f"warm: {label}cycle failed ({type(e).__name__})", flush=True)
            time.sleep(60)


if __name__ == "__main__":
    if HEAD:
        time.sleep(int(os.environ.get("WARM_HEAD_DELAY_S", "120")))     # let the engine load after a restart
    while True:
        if HEAD:
            print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), head_cycle(), flush=True)
            continue
        print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), once(), flush=True)
        time.sleep(EVERY)
