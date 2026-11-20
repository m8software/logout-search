"""stage_list: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

import datetime as dt
import math
import os
from stage_parts import part_run
from stage_base import anchor_owns_name
import re
import threading
import start_trace
import time
from urllib.parse import urlsplit
from search_api import (
    BREADTH_FLOOR,
    CE_EXACT,
    FACTS_ON,
    WIKI_EXACT_SCORE,
    WIKI_EXACT_SITE_SQL,
    _breadth,
    _breadth_floor,
    _card_gate,
    _conn,
    _cur_fill,
    _facts_store,
    _host_key,
    _host_of,
    _instant_list_first,
    _score_all,
    _se_on,
    cached,
    ce_text,
    cross_encoder,
    home_point,
    intent_of,
    lookups,
    official_ok,
    norm_key,
    optional,
    parts_for,
    place_radius_km,
    pop_sql,
    same_page,
    se_credit,
    stage,
    tables,
    usual_gate,
    web_only,
    web_rerank,
    web_signal,
)


# "nike returns", "chase login", "sony wh-1000xm5" (site_slots.py): a search that is a site's name plus
# words is answered by lookup. SEARCH_SITE_SLOTS=<site-slots.jsonl> (tools/site-slots.py build) turns on the slot row: the
# page of the site's own tree that the leftover words ask for (login, returns, support, pricing ...), under the exact rows.
# SEARCH_SITE_BRIDGE=<site-search.jsonl> (tools/site-slots.py search-table) turns on the "search on <site>" row (bridge: true)
# when no slot matched. Both unset = off. Neither is in result_store.NOT_RESULTS: they are in the results fingerprint.
SITE_SLOTS_PATH = os.environ.get("SEARCH_SITE_SLOTS", "").strip()

SITE_BRIDGE_PATH = os.environ.get("SEARCH_SITE_BRIDGE", "").strip()

SITE_SLOTS_LEAD = os.environ.get("SEARCH_SITE_SLOTS_LEAD", "").strip().lower() in ("1", "on", "true", "yes")   # the slot row first, not under the entity's own row

_site_tables: dict[str, object] = {}

_site_tables_lock = start_trace.lock("site_tables")

def _site_table(kind: str):
    """The slot table or the site-search table, read once; None when unset or unreadable (the feature stays off).
    site_slots is imported here, so with both settings unset the module is never loaded."""
    path = SITE_SLOTS_PATH if kind == "slots" else SITE_BRIDGE_PATH
    if not path:
        return None
    with _site_tables_lock:
        if kind not in _site_tables:
            import site_slots
            try:
                t = (site_slots.load_slots if kind == "slots" else site_slots.load_search)(path)
                print(f"site {kind} {path}: {len(t)} sites", flush=True)
            except (OSError, ValueError) as e:
                print(f"site {kind} {path}: not read ({type(e).__name__})", flush=True)
                t = None
            _site_tables[kind] = t
    return _site_tables[kind]

def _official_hosts_of_name(name_key: str) -> list[str]:
    """The official sites of the articles a name is an alias of (tier 2's own lookup, WIKI_EXACT_SITE_SQL): the engine's
    answer to "which site does this name mean" for names whose domain is not the name. [] without the table or on error."""
    try:
        with _conn() as c:
            if "wiki_sites" not in tables(c):
                return []
            rows = c.execute(pop_sql(WIKI_EXACT_SITE_SQL), {"nq": name_key}).fetchall()
        return [_host_key(r[2]) for r in rows if r[2]]
    except Exception:  # noqa: BLE001 - extra: never fail the answer
        return []

def site_intent_rows(q: str, res: list[dict], card: dict | None) -> list[dict]:
    """The shown list with the slot row, else the bridge row, when the settings are on and the search is a name + words."""
    if not (SITE_SLOTS_PATH or SITE_BRIDGE_PATH) or not res:
        return res
    import site_slots
    out = site_slots.apply_slots(q, res, card, _site_table("slots"), _official_hosts_of_name, SITE_SLOTS_LEAD)
    if out is res:
        out = site_slots.apply_bridge(q, res, card, _site_table("bridge"), _official_hosts_of_name)
    return out

_CG_W = (-3.4722, 0.8172, 0.3326, 0.6411, 1.6392, 0.2992, 0.7690, -0.6049, -0.0491, -0.4546, 0.5260)

_CG_MU = (0.0000, -2.8688, 0.0739, 91.1752, 0.2927, 0.0555, 0.2752, 0.7219, 2.5821, 0.3541, 0.5306)

_CG_SD = (1.0000, 3.2312, 0.1256, 199.0217, 0.3229, 0.1785, 0.4466, 0.4480, 1.1108, 0.4783, 0.4991)

_CG_STOP = frozenset("the a an of and in on for to de la le el s".split())

def _cg_toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", (s or "").lower()) if t not in _CG_STOP}

def card_gate_p(q: str, r: dict, pos: int) -> float:
    """How likely the gate's model finds row pos (0-based) relevant to q."""
    qt = _cg_toks(q)
    host = (urlsplit(r.get("url") or "").hostname or "")
    ce, lr = r.get("ce"), r.get("lr")
    x = [1.0, float(ce or 0.0), float(lr or 0.0), float(r.get("score") or 0.0),
         len(qt & _cg_toks(r.get("title") or "")) / max(len(qt), 1),
         len(qt & _cg_toks(re.sub(r"[./-]", " ", host))) / max(len(qt), 1),
         float(r.get("source") == "wikipedia"), float(r.get("source") == "site"), float(pos),
         float(ce is None), float(lr is None)]
    z = _CG_W[0] + sum(w * (v - m) / d for w, v, m, d in zip(_CG_W[1:], x[1:], _CG_MU[1:], _CG_SD[1:]))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

def card_gate(q: str, res: list[dict], th: float | None = None) -> list[dict]:
    """Under a card: the first row, places, and the rows the gate's model is sure of."""
    th = _card_gate.get() if th is None else th
    if th <= 0 or len(res) < 2:
        return res
    return res[:1] + [r for i, r in enumerate(res[1:], 1) if r.get("source") == "place" or card_gate_p(q, r, i) >= th]

# Wikipedia in the result list ("6 or 7 in a row as the top results").
WIKI_MIX_MAX = 2                # other articles kept in the list once the card has its own

WIKI_MIX_SLOTS = (1, 4)         # 0-based places among the rest: one early, one in the middle

BREADTH_LINKS = 3

_BREADTH_SKIP = frozenset(("Official site", "X", "Instagram", "Facebook", "TikTok", "Threads", "LinkedIn", "YouTube",
                           "Pinterest", "Giphy", "Bluesky", "Mastodon", "GitHub", "Amazon", "Vikidia"))

_BREADTH_STOP = frozenset(("the", "and", "for", "with", "how", "what", "who", "when", "where", "why", "near", "best",
                           "from", "into", "your", "you", "are", "was", "does", "can", "of", "in", "on", "to", "a",
                           "an", "is", "it", "my", "me", "do", "vs", "man", "men", "new", "one", "out", "get", "all"))

_BREADTH_UNCAPPED = frozenset(("wikipedia", "stackexchange", "gov", "account", "youtube", "subreddit"))

_BREADTH_QUESTION = re.compile(r"^(how|what|why|when|where|can|should|does|do|is)\b", re.I)

# Hosts whose subdomains are different publishers (CORE-A-02): two projects on *.readthedocs.io are two sites, not one.
# The same list as site_slots._PRIVATE (a test pins that they match); no public-suffix package is added. fandom.com is left
# out on purpose: a wiki farm whose list rows the D rule has been judged on as one site (needs an eval to change).
_TENANT_HOSTS = frozenset(("github.io", "blogspot.com", "wordpress.com", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
                           "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net", "myshopify.com", "wixsite.com",
                           "weebly.com", "tumblr.com", "gitlab.io", "fly.dev", "onrender.com",
                           "readthedocs.io", "substack.com", "medium.com", "gitbook.io", "notion.site", "bitbucket.io", "surge.sh",
                           "glitch.me", "pythonanywhere.com", "netlify.com", "cloudfront.net"))

def _site_of(url: str) -> str:
    h = _host_of(url)
    for pre in ("www.", "m.", "cdn.", "static.", "assets.", "en."):
        if h.startswith(pre) and h.count(".") >= 2:
            h = h[len(pre):]
    p = h.split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in _TENANT_HOSTS:
        return ".".join(p[-3:])
    return ".".join(p[-3:]) if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2 else ".".join(p[-2:])

def _uncapped_row(r: dict, st: str, is_gov: bool = False) -> bool:
    """Wikipedia, Stack Exchange Q&A, channel/account rows, and (on a government search) .gov/.mil portals hold distinct
    pages or agencies under one root domain: D does not cap them at one row per root domain, and J leaves them."""
    return r.get("source") in _BREADTH_UNCAPPED or (is_gov and st.endswith((".gov", ".mil")))

def _digest_of(obj) -> str:
    import parts as _parts
    return _parts.digest(obj)

def demote_unowned_anchor(q: str, res: list[dict]) -> list[dict]:
    """A site the web's anchor text names (rbi.org.in: a Reserve Bank page with many links labelled "Word") does not lead the list
    for a query it carries no letters of when the article titled exactly the query is among the next rows: that article goes first.
    The row stays (intent and confidence are as they were); only the lead is checked."""
    if len(res) < 2:
        return res
    r = res[0]
    if not r.get("anchor_named") or r.get("source") != "site" or anchor_owns_name(q, r.get("url") or "", r.get("title")):
        return res
    k = norm_key(q)
    for i, x in enumerate(res[1:4], 1):
        if x.get("source") == "wikipedia" and norm_key(re.sub(r"\s*\(.*\)$", "", x["title"].rsplit(" — Wikipedia", 1)[0])) == k:
            return [x] + res[:i] + res[i + 1:]
    return res

def list_build(q: str, res: list[dict], card: dict | None, cur: list[dict] | None):
    """The list stage (parts stage "list" phase 2): the rows as shown, from the base rows after the card and the
    place, the card and the offline curated list (None when there is none). Returns (rows, sig). A function of its
    arguments, the settings and data it reads and the models (through the compute cache); the order of the steps is
    the one _instant had inline before the split."""
    def usual(rows):                                 # the list as it is without a curated one
        # kept per search for the cache's time (2026-11-11): the links of the card's article are scored by the
        # reranker in breadth_list, about 0.6 s on the public engine, and that was paid again on EVERY request of
        # a carded name, cached base rows or not. The key is everything the steps below read.
        # CORE-B-01/A-05: rows with one url differ by searcher (a place row's title holds the distance from the searcher's
        # point), by device (ce) and by what the card holds, so the key also has the position, the reach, the list-first
        # setting and a digest of the rows and the card as they are, not only their urls.
        key = ("usual", q, _breadth.get(), _breadth_floor.get(), bool(_score_all.get()), usual_gate(), same_page((card or {}).get("url") or ""),
               home_point(), place_radius_km(), _instant_list_first.get(), _digest_of([rows, card]),
               tuple(r.get("url") for r in rows))
        return cached(key, lambda: usual_now(rows))

    def usual_now(rows):
        rows = optional("mix_wikipedia", lambda: mix_wikipedia(rows, card), rows)
        if card:
            rows = optional("card_gate", lambda: card_gate(q, rows), rows)     # off unless SEARCH_CARD_GATE or ?cg= (2026-11-05)
        if _score_all.get() and rows:                # off unless SEARCH_SCORE_ALL or ?sa=1
            with stage("score_all"):
                rows = optional("score_all", lambda: score_unscored(q, rows[:SCORE_ALL_TOP] if SCORE_ALL_TOP > 0 else rows), rows)
        with stage("breadth"):
            return optional("breadth_list", lambda: breadth_list(q, rows, card, _breadth.get()), rows)   # off unless SEARCH_BREADTH or ?bl= (2026-11-07)
    if cur:
        # the offline list as chosen, in its order (no Wikipedia mix, no gate: the large model already decided);
        # the engine's places stay on top and the card's own article is not repeated under it
        places = [r for r in res if r.get("source") == "place"]
        shown = {same_page(card.get("url") or "")} if card else set()
        top = places + [r for r in cur if r.get("source") != "place" and same_page(r["url"]) not in shown]
        res = optional("curated_fill", lambda: curated_fill(top, usual(res), shown), top) if _cur_fill.get() else top
    else:
        res = usual(res)
    res = optional("office_rows", lambda: with_office_rows(res, card), res)      # "trump": whitehouse.gov just under the card (after the list is final)
    sig = {}
    if optional("web_only", lambda: web_only(q, res), False):           # on the shown list, after the Wikipedia mix: that is what the fit saw
        res = optional("web_rerank", lambda: web_rerank(q, res), res)
        sig = optional("web_signal", lambda: web_signal(q, res), {})
    if not cur:                    # after the web rerank (it must not move the row); a curated list is the large model's, left as chosen
        res = optional("site_intent", lambda: site_intent_rows(q, res, card), res)       # off unless SEARCH_SITE_SLOTS / SEARCH_SITE_BRIDGE
        res = optional("se_howto", lambda: se_howto(q, res), res)     # off unless SEARCH_SE_INDEX; how-to searches only (2026-11-10)
        res = optional("unowned_anchor", lambda: demote_unowned_anchor(q, res), res)
    return res, sig

def list_stage(q: str, res: list[dict], card: dict | None, cur: list[dict] | None):
    import parts as _parts
    if parts_for("list") is None:
        return list_build(q, res, card, cur)
    inputs = [q, _parts.canon(res).decode(), _parts.canon(card).decode(), _parts.canon(cur).decode()]
    out = part_run("list", inputs, lambda: list(list_build(q, res, card, cur)))
    return out[0], out[1]

def _link_rows(card: dict) -> list[dict]:
    """The card's Wikidata community/reference links as list rows, with a title a reader recognises."""
    name = card.get("title") or ""
    rows = []
    for l in (card.get("links") or []) + (card.get("more_links") or []):
        label, url = l.get("title") or "", l.get("url") or ""
        if label in _BREADTH_SKIP or not url.startswith("https://"):
            continue
        if label == "Reddit":
            title = "r/" + url.rstrip("/").rsplit("/", 1)[-1] + " — Reddit"
        elif label == "Fandom":
            title = f"{name} Wiki — Fandom"
        else:
            title = f"{name} — {label}"
        rows.append({"url": url, "title": title, "content": card.get("description") or "", "source": "site",
                     "score": 0.0, "wikidata_link": label})
    return rows

def _scores(q: str, rows: list[dict], scorer=None):
    scorer = scorer if scorer is not None else cross_encoder()
    if not scorer or not rows:
        return None
    return [float(v) for v in scorer(q, [ce_text(r) for r in rows])]

# SEARCH_SCORE_ALL_TOP=N (2026-11-14): with SCORE_ALL, only the list's first N rows are scored and kept; the rest go. A page
# shows about 8 rows, and scoring every row was the costliest step of a new search on public (median 142 ms, slowest tenth
# 1.8 s; new searches held fell from 4 to 2 a second). Simulated on the judged lists of 2026-11-13 (floor 0, first 8 shown):
# N=10 -> everyday-100 pass 69 -> 71, relevant rows 426 -> 402, rows sent to the model -42%; mix-300 pass 185 -> 187,
# relevant 950 -> 900, model rows -44%. 0 = every row, as before.
SCORE_ALL_TOP = int(os.environ.get("SEARCH_SCORE_ALL_TOP", "0") or 0)

def score_unscored(q: str, res: list[dict], scorer=None) -> list[dict]:
    """res with a reranker score (ce) on every row that should have one and has none. One batch; the extra pairs are
    the rows returned without ce, and a search with nothing to score asks the model nothing."""
    if len(res) < 2:
        return res
    todo = [i for i, r in enumerate(res)
            if i > 0 and r.get("ce") is None and r.get("score", 0) < CE_EXACT and not r.get("curated")
            and r.get("source") not in ("place", "stock") and not r.get("stock")]
    if not todo:
        return res
    sc = _scores(q, [res[i] for i in todo], scorer)
    if sc is None:
        return res
    out = list(res)
    for i, v in zip(todo, sc):
        out[i] = dict(res[i], ce=round(v, 2))
    return out

def _scores_reuse(q: str, rows: list[dict], scorer=None):
    """Like _scores, but a row that already has a ce keeps it: the model is asked about the rows without one only."""
    need = [i for i, r in enumerate(rows) if not isinstance(r.get("ce"), (int, float)) and (r.get("score") or 0) < CE_EXACT]
    sc = _scores(q, [rows[i] for i in need], scorer) if need else []
    if sc is None:
        return None
    out = [float(r["ce"]) if isinstance(r.get("ce"), (int, float)) else 0.0 for r in rows]
    for i, v in zip(need, sc):
        out[i] = v
    return out

# The card's Wikipedia article's own outbound links (importer/wikiext.py, 2026-11-09). SEARCH_WIKILINKS is a sqlite file:
# links(title, kind, pos, url, label, domain) and trust(domain, n = distinct articles citing it). Read per card by its
# English Wikipedia title, only when the W or C breadth letter is on.
WIKILINKS_PATH = os.environ.get("SEARCH_WIKILINKS", "").strip()

BREADTH_WIKI = int(os.environ.get("SEARCH_BREADTH_WIKI", "3"))

BREADTH_CITE = int(os.environ.get("SEARCH_BREADTH_CITE", "3"))

BREADTH_WIKI_MIN = float(os.environ.get("SEARCH_BREADTH_WIKI_MIN", "-1.5"))

# A first run (2026-11-09, sq-eval place set) let candidates land above an expected answer (kiehl's 4th -> 7th,
# tornado 4th -> 6th): they only go from this 0-based place of the rows after the first on (default: behind three rows).
BREADTH_WIKI_FROM = int(os.environ.get("SEARCH_BREADTH_WIKI_FROM", "3"))

_wikilinks_db = threading.local()

_GENERIC_LABEL = re.compile(r"^(official\s+(web\s*)?site|official page|website|web site|home\s*page|homepage|home|link|"
                            r"profile|page|site|here|online|web|pdf|full text|text|source|archived|archive|"
                            r"main page|main site)$", re.I)

_WIKI_SKIP_DOMAINS = frozenset(("facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "youtube.com",
                                "tiktok.com", "threads.net", "pinterest.com", "doi.org", "jstor.org", "worldcat.org",
                                "handle.net", "ssrn.com", "semanticscholar.org", "archive.org", "googleusercontent.com",
                                "imdb.com", "wikimedia.org", "amazon.com", "goodreads.com"))

def wiki_links(title: str) -> list[tuple[str, str, str, str, int]]:
    """[(kind, url, label, domain, trust)] the article lists, page order within a kind; [] without the file."""
    if not WIKILINKS_PATH or not title:
        return []
    import sqlite3
    db = getattr(_wikilinks_db, "db", None)
    if db is None:
        try:
            db = sqlite3.connect(f"file:{WIKILINKS_PATH}?mode=ro", uri=True, check_same_thread=False)
            _wikilinks_db.db = db
        except sqlite3.Error:
            return []
    try:
        return db.execute("SELECT l.kind, l.url, l.label, l.domain, coalesce(t.n, 0) FROM links l "
                          "LEFT JOIN trust t ON t.domain = l.domain WHERE l.title = ? ORDER BY l.kind, l.pos",
                          (title,)).fetchall()
    except sqlite3.Error:
        return []

def _wiki_rows(card: dict, flags: str, have: set) -> tuple[list[dict], list[dict]]:
    """(W rows, C rows): the card's article's links as list rows. The row title is the editors' link text (never the
    card's name: a candidate must earn its place by what it says); a generic text ("official website") becomes the
    site's name, and a citation without a usable text is left out."""
    ext, cite, seen = [], [], set(have)
    for kind, url, label, dom, trust in wiki_links(card.get("title") or ""):
        if dom in _WIKI_SKIP_DOMAINS or not url.startswith(("https://", "http://")):
            continue
        st = _site_of(url)
        if st in seen:
            continue
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if kind == "citation":
            if not label or _GENERIC_LABEL.match(label) or len(label) < 6:
                continue
            title = label
        else:
            title = host if (not label or _GENERIC_LABEL.match(label)) else label
        seen.add(st)
        row = {"url": url, "title": title, "content": "", "source": "site", "score": 0.0,
               "wikipedia_link": kind, "trust": trust}
        (cite if kind == "citation" else ext).append(row)
    if "W" not in flags:
        ext = []
    if "C" not in flags:
        cite = []
    return ext, cite

def _merge_wiki(q: str, rest: list[dict], sc, card: dict, flags: str, have: set, scorer=None):
    """W / C: score the article's links with the list's rows and put the best of each kind where their score falls;
    a candidate under BREADTH_WIKI_MIN is not added. Returns (rest, scores or None)."""
    ext, cite = _wiki_rows(card, flags, have)
    cands = ext + cite
    if not cands:
        return rest, sc
    allsc = _scores(q, rest + cands, scorer)
    if allsc is None:
        return rest, sc
    rs, cs = allsc[:len(rest)], allsc[len(rest):]
    take = []
    for pool, n, off in ((ext, BREADTH_WIKI, 0), (cite, BREADTH_CITE, len(ext))):
        ranked = sorted(((cs[off + i], i) for i in range(len(pool)) if cs[off + i] >= BREADTH_WIKI_MIN), reverse=True)
        take += [(v, pool[i]) for v, i in ranked[:n]]
    merged = list(zip(rs, rest))
    for v, row in sorted(take, key=lambda t: -t[0]):
        pos = max(next((k for k, (rv, _r) in enumerate(merged) if v > rv), len(merged)), min(BREADTH_WIKI_FROM, len(merged)))
        merged.insert(pos, (v, dict(row, ce=round(v, 2))))
    return [r for _v, r in merged], [v for v, _r in merged]

def breadth_list(q: str, res: list[dict], card: dict | None, flags: str, scorer=None) -> list[dict]:
    """The shown list after the Wikipedia mix, shaped by the SEARCH_BREADTH flags (above). Places and a stock
    answer stay on top as they are; the first row stays first."""
    if not flags or not res:
        return res
    n = 0
    while n < len(res) and (res[n].get("source") == "place" or res[n].get("stock")):
        n += 1
    head, rows = res[:n], res[n:]
    # places under a site or an article ("taco bell": the official site, then its three branches) are no list rows
    # either: D kept one row of openstreetmap.org and the page, which needs two places for its Nearby box, drew none
    # (owner 2026-11-13, "why no near me"). They sit out the shaping and go back after the row they followed.
    mid, kept = {}, []
    for r in rows:
        if r.get("source") == "place":
            mid.setdefault(len(kept) - 1, []).append(r)
        else:
            kept.append(r)
    rows = kept
    if not rows:
        return res
    official = {_site_of(l["url"]) for l in (card or {}).get("links", []) if l.get("title") == "Official site"}
    first, rest = rows[0], rows[1:]
    # the search's words, and the card's name ("taylor swfit" is answered as Taylor Swift: her name's words count)
    words = {w for w in re.findall(r"[a-z0-9]+", ((q or "") + " " + ((card or {}).get("title") or "")).lower())
             if len(w) >= 3 and w not in _BREADTH_STOP}

    def has_word(r: dict) -> bool:
        text = " ".join((r.get("title") or "", r.get("url") or "", (r.get("content") or "")[:300])).lower()
        flat = re.sub(r"[^a-z0-9]", "", text)              # "spiderman" is in "Spider-Man", "westham" in "West Ham"
        return any(w in text or w in flat for w in words)
    is_gov = any(r.get("source") == "gov" or _site_of(r.get("url") or "").endswith((".gov", ".mil")) for r in rows[:3])
    if "J" in flags:
        # when no row beyond the first shares a word and there is no card, or the search is a question / government task,
        # the query words are not the results' vocabulary: J's word check stays out. With a card (or >=2 matching rows),
        # J stays on even when more than half of rest is junk ("spiderman" had 8 junk rows of 13, 2026-11-08)
        word_ok = (bool(words) and not is_gov and not _BREADTH_QUESTION.match((q or "").strip())
                   and (card is not None or sum(1 for r in rows if has_word(r)) >= 2))
        seen, out = {_site_of(first["url"])}, []
        for r in rest:
            st = _site_of(r["url"])
            if word_ok and not _uncapped_row(r, st, is_gov) and not has_word(r):
                continue
            path = urlsplit(r["url"]).path.strip("/").lower()
            if st in seen and st not in official and path in ("", "en", "en-us", "en-gb", "home", "index.html"):
                continue
            seen.add(st); out.append(r)
        rest = out
    if "D" in flags:
        # one row per site (the card's own site excepted). Wikipedia, Stack Exchange Q&A, government (.gov/.mil) and
        # channel/account rows are NOT capped: Clef 27B judged a second state agency page (courts.oregon.gov after
        # oregon.gov/boli), a second Stack Exchange question or a second article better than what replaced them
        # (2026-11-08, mix-300: government 2.26 -> 3.46, howto 1.64 -> 1.80, people 2.06 -> 2.50)
        out, per = [], {_site_of(first["url"]): 1}
        for r in rest:
            st = _site_of(r["url"])
            if not _uncapped_row(r, st, is_gov) and per.get(st, 0) >= 1 and st not in official:
                continue
            per[st] = per.get(st, 0) + 1; out.append(r)
        rest = out
    links = []
    if card and ("L" in flags or "S" in flags):
        have = {same_page(r["url"]) for r in rows} | {_site_of(r["url"]) for r in rows}
        links = [l for l in _link_rows(card) if same_page(l["url"]) not in have and _site_of(l["url"]) not in have]
    sc = None
    if "L" in flags and links:
        sc = _scores(q, rest + links, scorer)
        if sc is not None:
            rs, ls = sc[:len(rest)], sc[len(rest):]
            best = sorted(zip(ls, range(len(links))), reverse=True)[:BREADTH_LINKS]
            merged = list(zip(rs, rest))
            for v, i in best:
                pos = next((k for k, (rv, _r) in enumerate(merged) if v > rv), len(merged))
                merged.insert(pos, (v, dict(links[i], ce=round(v, 2))))
            rest = [r for _v, r in merged]
            sc = [v for v, _r in merged]
    elif "S" in flags and links:
        for k, l in enumerate([l for l in links if l["wikidata_link"] in ("Reddit", "Fandom")][:2]):
            rest.insert(min(k, len(rest)), l)
    if card and ("W" in flags or "C" in flags) and WIKILINKS_PATH:
        have = {_site_of(r["url"]) for r in rows} | {_site_of(l["url"]) for l in links} | official
        rest, got = _merge_wiki(q, rest, sc, card, flags, have, scorer)
        if got is not None:
            sc = got
    if "F" in flags:
        if sc is None or len(sc) != len(rest):
            sc = _scores_reuse(q, rest, scorer) if _score_all.get() else _scores(q, rest, scorer)
        if sc is not None:
            floor = _breadth_floor.get()
            pin = _score_all.get()                         # with SCORE_ALL an exact-tier row is never under the floor
            rest = [r for k, (r, v) in enumerate(zip(rest, sc))
                    if k < 2 or v >= floor or r.get("wikidata_link") or (pin and (r.get("score") or 0) >= CE_EXACT)]
    out = [first] + rest
    if mid:
        left = {r["url"] for r in out}
        follow = {}
        for k in sorted(mid):                              # after the row it followed, or the nearest one above still shown
            at = next(kept[i]["url"] for i in range(k, -1, -1) if kept[i]["url"] in left or i == 0)
            follow.setdefault(at, []).extend(mid[k])
        out = [x for r in out for x in [r] + follow.pop(r["url"], [])]
    return head + out

# Stack Exchange how-to index (2026-11-10, owner decisions, ADR-081): SEARCH_SE_INDEX=<sqlite> (tools/se-howto.py cut)
# gives how-to and question searches candidate rows from 45 sites' questions, each with a snippet of its best answer.
# Off unless the path is set; per request ?se=0|1 (eval A/B). A candidate goes through the reranker with the rows already
# in the list and is added only when its score clears SEARCH_SE_MIN (default the breadth floor: "ring size" must not
# bring ring flashes); at most SEARCH_SE_ROWS are added. A row the release already lists (same page) is not repeated: it
# gets the snippet and the licence fields. A row carries, besides url / title: content (the snippet), snippet (the same,
# an excerpt), the credit fields of se_credit (author, answer_author and their profile links), answer_url, licence,
# licence_url, excerpt: true. The page must show the question link, both authors, the licence mark and "excerpt" with it.
SE_INDEX_PATH = os.environ.get("SEARCH_SE_INDEX", "").strip()

SE_INDEX_ROWS = int(os.environ.get("SEARCH_SE_ROWS", "3"))

SE_INDEX_MIN = float(os.environ.get("SEARCH_SE_MIN", str(BREADTH_FLOOR)))

SE_INDEX_POOL = int(os.environ.get("SEARCH_SE_POOL", "20"))      # candidates read from the index and scored (each is one reranker pair)

# SEARCH_SE_REUSE=1: a row of the shown list that already carries a reranker score keeps it, and the model is
# asked about the candidates and the unscored rows only. Off = every row is scored again with the candidates.
SE_INDEX_REUSE = os.environ.get("SEARCH_SE_REUSE", "").strip() == "1"

_se_ix_pool: list = []                  # read-only connections to the index kept for the next request: a request thread is new each time, and a
_se_ix_lock = threading.Lock()          # connection of its own started with an empty page cache and re-read the FTS pages (110-190 ms CPU a question)

def _se_ix_conn():
    import sqlite3
    with _se_ix_lock:
        if _se_ix_pool:
            return _se_ix_pool.pop()
    c = sqlite3.connect(f"file:{SE_INDEX_PATH}?mode=ro", uri=True, check_same_thread=False)
    c.execute("PRAGMA cache_size = -16000")           # 16 MB of the index's pages per connection
    return c

def _se_ix_keep(c) -> None:
    with _se_ix_lock:
        if len(_se_ix_pool) < 4:
            _se_ix_pool.append(c)
            return
    c.close()

_SE_STOP = frozenset(("the", "a", "an", "of", "and", "in", "on", "for", "to", "is", "are", "was", "how", "do", "does", "what",
                      "who", "why", "when", "where", "can", "i", "my", "me", "you", "with", "at", "by", "from", "it", "this",
                      "that", "be", "or", "as", "get", "your"))

_SE_LICENCES = ((("2011-04-08",), "2.5"), (("2018-05-02",), "3.0"))      # answers before the date: that version; later: 4.0

def se_ix_licence(date: str) -> tuple[str, str]:
    """(name, deed url) of the CC BY-SA version an answer written on `date` (YYYY-MM-DD) carries: 2.5 before 2011-04-08,
    3.0 before 2018-05-02, 4.0 after (the network's public terms as we read them; the date is stored per answer)."""
    d = (date or "")[:10]
    v = "4.0"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        for (cut,), ver in _SE_LICENCES:
            if d < cut:
                v = ver
                break
    return f"CC BY-SA {v}", f"https://creativecommons.org/licenses/by-sa/{v}/"

def se_ix_lookup(q: str, n: int | None = None) -> list[tuple]:
    """The index's best rows for q: bm25 with the title weighted most; AND of the content words, OR when AND gives
    fewer than 5. [] without the file or without a content word."""
    import sqlite3
    if not SE_INDEX_PATH:
        return []
    n = SE_INDEX_POOL if n is None else n
    words = [w for w in re.findall(r"[a-z0-9]+", (q or "").lower()) if w not in _SE_STOP]
    if not words:
        return []
    try:
        db = _se_ix_conn()
    except sqlite3.Error:
        return []
    rows: list[tuple] = []
    ok = True
    for op in (" AND ", " OR "):
        try:
            rows = db.execute("SELECT s.site, s.qid, s.title, s.score, s.acc, s.asker, s.answerer, s.aid, s.adate, s.snip "
                              "FROM fts JOIN s ON s.id = fts.rowid WHERE fts MATCH ? ORDER BY bm25(fts, 6.0, 2.0, 1.0, 1.0) LIMIT ?",
                              (op.join(f'"{w}"' for w in words[:12]), n)).fetchall()
        except sqlite3.Error:
            rows, ok = [], False
        if len(rows) >= 5:
            break
    if ok:
        _se_ix_keep(db)
    else:
        db.close()
    return rows

def _se_ix_credit(packed: str) -> tuple:
    """'<user id>|<name>' -> (user id or None, name or None)."""
    uid, _, name = (packed or "").partition("|")
    return (int(uid) if uid.isdigit() else None), (name.strip() or None)

def se_ix_row(r: tuple) -> dict:
    """One index row -> a result row with its snippet, credits and licence fields."""
    site, qid, title, score, acc, asker, answerer, aid, adate, snip = r
    url = f"https://{site}/questions/{qid}"
    lic, lic_url = se_ix_licence(adate)
    row = {"url": url, "title": f"{title} \u2014 {site}", "content": snip or "", "source": "stackexchange", "score": 0.0,
           "se_index": True, "accepted": bool(acc), "votes": score, "answer_url": f"https://{site}/a/{aid}" if aid else None,
           "licence": lic, "licence_url": lic_url, "excerpt": True}
    if snip:
        row["snippet"] = snip
    row.update(se_credit(url, {1: _se_ix_credit(asker), 2: _se_ix_credit(answerer)}))
    return row

def se_howto(q: str, res: list[dict], scorer=None) -> list[dict]:
    """The shown list with index rows for a how-to or question search (intent_of, the engine's own signal). Returns the
    list as it was when the index is off, the search is another kind, or no candidate clears the floor. The answer for one
    question over one list is kept for CACHE_TTL_S like every other cached block: it was computed again for every repeat of
    a question (an index query and a reranker batch, 250-330 ms here and 4.4 s on buildbox under load)."""
    if not SE_INDEX_PATH or not _se_on.get() or not res or intent_of(q, res) != "howto":
        return res
    if scorer is not None:                                # a caller's own scorer (a test, an A/B): not shared
        return _se_howto_rows(q, res, scorer)
    return cached(("se_howto", (q or "").lower(), tuple(r.get("url") for r in res), SE_INDEX_ROWS, SE_INDEX_MIN), lambda: [_se_howto_rows(q, res, None)])[0]


def _se_howto_rows(q: str, res: list[dict], scorer=None) -> list[dict]:
    with stage("se_lookup"):
        cands = [se_ix_row(r) for r in se_ix_lookup(q)]
    if not cands:
        return res
    have = {same_page(r["url"]): r for r in res}
    out = list(res)                                       # rows may be shared with the result cache: copy before changing one
    for k, old in enumerate(out):
        c = next((c for c in cands if same_page(c["url"]) == same_page(old["url"])), None)
        if c is not None and (old.get("source") == "stackexchange" or "author" in old or "answer_author" in old):
            # a row the release lists already (a release page row is source "site" with its credits): snippet + licence on it
            upd = {f: c[f] for f in ("content", "snippet", "answer_url", "licence", "licence_url", "excerpt", "accepted",
                                     "votes", "se_index") if f in c}
            upd.update({f: c[f] for f in ("author", "author_url", "answer_author", "answer_author_url") if f in c and f not in old})
            out[k] = dict(old, **upd)
    new = [c for c in cands if same_page(c["url"]) not in have]
    if not new or SE_INDEX_ROWS <= 0:
        return out
    if SE_INDEX_REUSE:                                    # the rows' own scores stand; only what has none is asked
        todo = [k for k, r in enumerate(out) if not isinstance(r.get("ce"), (int, float))]
        with stage("se_scores"):
            sc = _scores(q, [out[k] for k in todo] + new, scorer)
        if sc is not None:
            rs = [float(r["ce"]) if isinstance(r.get("ce"), (int, float)) else 0.0 for r in out]
            for k, v in zip(todo, sc):
                rs[k] = v
            sc = rs + sc[len(todo):]
    else:
        with stage("se_scores"):
            sc = _scores(q, out + new, scorer)
    if sc is None:
        return out
    rs, ns = sc[:len(out)], sc[len(out):]
    take = sorted(((v, i) for i, v in enumerate(ns) if v >= SE_INDEX_MIN), reverse=True)[:SE_INDEX_ROWS]
    if not take:
        return out
    n_head = 0
    while n_head < len(out) and (out[n_head].get("source") == "place" or out[n_head].get("stock")):
        n_head += 1
    merged = list(zip(rs, out))
    for v, i in take:                                     # best first; the first row after the places stays first
        pos = next((k for k, (rv, _r) in enumerate(merged) if k > n_head and v > rv), len(merged))
        merged.insert(pos, (v, dict(new[i], ce=round(v, 2))))
    return [r for _v, r in merged]

_SEASON = re.compile(r"^((?:1[89]|20)\d\d)[–-](?:\d{2}|\d{4})\b.*\bseason\b", re.I)

def old_season(r: dict, year: int | None = None) -> bool:
    """A "1964–65 West Ham United F.C. season" article more than a year old (it took the list's second row for
    "west ham united"): it follows the other articles instead of taking one of the early slots."""
    m = _SEASON.match(r.get("title") or "")
    return bool(m) and int(m.group(1)) < (year or time.gmtime().tm_year) - 1

def mix_wikipedia(res: list[dict], card: dict | None, list_first: bool | None = None) -> list[dict]:
    """The list as shown: the card's article, and the articles the card already lists as
    related, are not repeated; of the remaining articles at most WIKI_MIX_MAX stay near the
    top, each between websites; the rest follow the websites. With no card, a leading exact
    article is the answer itself and stays first. With list_first (SEARCH_INSTANT_LIST_FIRST,
    ?ilf=), a leading article stays first whatever its score, and the card's own article stays
    as the first row flagged in_card instead of leaving the list."""
    if not any(r.get("source") == "wikipedia" for r in res):
        return res
    if list_first is None:
        list_first = _instant_list_first.get()
    lead = [r for r in res if r.get("stock")]          # a stock answer's three links stay together on top
    if lead and res[0].get("stock"):
        return lead + mix_wikipedia([r for r in res if not r.get("stock")], card, list_first)
    n = 0                                              # nearby places lead as one block: a list-only client
    while n < len(res) and res[n].get("source") == "place":      # (launcher, start page) must not see an article
        n += 1                                                   # between two of them
    if n:
        return res[:n] + mix_wikipedia(res[n:], card, list_first)
    shown = set()
    if card:
        shown = {same_page(card.get("url") or "")} | {same_page(r["url"]) for r in (card.get("related") or []) if r.get("url")}
    first = res[0]
    if list_first and first.get("source") == "wikipedia" and not first.get("namesake"):
        # the ranked list's first result is the answer: it stays first; the card's own article is marked
        lead = [dict(first, in_card=True) if card and same_page(first["url"]) in shown else first]
    else:
        lead = [first] if not card and first.get("source") == "wikipedia" and first["score"] >= WIKI_EXACT_SCORE else []
    rest = [r for r in res[len(lead):] if same_page(r["url"]) not in shown or r.get("source") != "wikipedia"]
    web = [r for r in rest if r.get("source") != "wikipedia"]
    articles = [r for r in rest if r.get("source") == "wikipedia" and not r.get("namesake")]
    old = [r for r in articles if old_season(r)]                #: never in an early slot, even when every
    articles = [r for r in articles if not old_season(r)]       # article left is one ("west ham united")
    articles += [r for r in rest if r.get("namesake")]          # a namesake never takes a slot
    out = list(web)
    for slot, art in zip(WIKI_MIX_SLOTS[:WIKI_MIX_MAX], articles):
        out.insert(min(slot, len(out)), art)
    return lead + out + articles[WIKI_MIX_MAX:] + old

# The office a person holds now, and its own site (2026-11-14, owner: "whitehouse.gov should be up there" for "trump").
# From the facts file: Wikidata's position held (P39), the ones with no end date that have begun; each position's
# article and its official site from the release's cards ("President of the United States" -> whitehouse.gov). The
# row goes just under the person's article when the card is that person. A former office gives nothing ("biden").
# SEARCH_OFFICE_SITES=0 turns it off.
OFFICE_SITES = os.environ.get("SEARCH_OFFICE_SITES", "1") == "1"

OFFICE_MAX = 2

def office_rows(t: str, today: dt.date | None = None) -> list[dict]:
    """Rows for the official sites of the offices the article t's subject holds now (release backend only)."""
    if not (OFFICE_SITES and FACTS_ON):
        return []
    lk = lookups()
    if lk is None or lk.name != "release":
        return []
    if not _facts_store:
        import facts
        _facts_store.append(facts.Facts.open(lk.root))
    fx = _facts_store[0]
    if not fx:
        return []
    import lookups_release as lr
    docs = lk.names1(t, lr.N_TITLE)[:1]
    pid = fx.props and next((k for k, v in fx.props.items() if v["prop"] == "P39"), None)
    if not docs or pid is None:
        return []
    today = today or dt.date.today()
    out, seen = [], set()
    for _v, _unit, _prec, at, until, vdoc in fx.facts_of(docs[0], pid):
        if until or vdoc is None:
            continue                                             # an office held before
        if at and str(at)[:10] > today.isoformat():
            continue                                             # elected, not yet in office
        card = lk.card(vdoc) or {}
        url = (card.get("o") or [None])[0]
        office = lk.title(vdoc)
        if not url or not office or url in seen or not official_ok(url):
            continue
        seen.add(url)
        out.append({"url": url, "title": f"{office} — official site", "content": (card.get("l") or "")[:300],
                    "source": "site", "office": True, "score": 0.0})
        if len(out) >= OFFICE_MAX:
            break
    return out

def with_office_rows(res: list[dict], card: dict | None) -> list[dict]:
    """The offices' sites just under the card's article and the person's own site when either is near the top (else
    first); a site already in the list moves up instead of showing twice."""
    if not card or not card.get("title") or not res:
        return res
    try:
        rows = cached(("office", card["title"]), lambda: office_rows(card["title"]), static=True)
    except Exception:  # noqa: BLE001 - an office row is extra: never fail the answer
        return res
    if not rows:
        return res
    urls = {r["url"].rstrip("/") for r in rows}
    rest = [r for r in res if (r.get("url") or "").rstrip("/") not in urls]
    art = next((i for i, r in enumerate(rest) if r.get("source") == "wikipedia"
                and r.get("title", "").rsplit(" — Wikipedia", 1)[0] == card["title"]), None)
    # the person's own site stays above the office's: "trump" led with whitehouse.gov over donaldjtrump.com
    own = next((i for i, r in enumerate(rest[:3]) if r.get("source") == "site"
                and r.get("title", "") == card["title"] + " — official site"), None)
    at = max((art + 1) if art is not None else 0, (own + 1) if own is not None else 0)
    return rest[:at] + [dict(r) for r in rows] + rest[at:]

def curated_fill(top: list[dict], base: list[dict], skip=()) -> list[dict]:
    """top (the places and the curated rows) followed by the rows of base (the engine's own list) that top does not
    show already, to the length the engine's own list has (never shorter than top). A place in base is in top already.
    skip: pages never added (the card's own article, which the curated list leaves out on purpose)."""
    have = {same_page(r.get("url") or "") for r in top} | set(skip)
    out = list(top)
    for r in base:
        if len(out) >= max(len(top), len(base)):
            break
        k = same_page(r.get("url") or "")
        if k in have or r.get("source") == "place":
            continue
        have.add(k)
        out.append(r)
    return out
