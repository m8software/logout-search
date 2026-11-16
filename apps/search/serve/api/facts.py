"""The facts layer (epic stride C): a question about a named thing answered outright from
facts.sqlite in the release, with the article as the card.

    "population of lisbon"  -> Population · Lisbon · 545,796 · as of 2021 · en.wikipedia.org/wiki/Lisbon
    "ceo of nvidia"         -> CEO · Nvidia · Jensen Huang (his own article linked)
    "sci-fi films 1982"     -> a list card: the most-viewed science-fiction films released in 1982

Own module (epic rule 1); search_api.py has one hook line behind SEARCH_FACTS=1 (default off). The query
is matched against the template table shipped IN the file (release/facts_templates.tsv -> `templates`), the
entity through the engine's own exact-name matching (ReleaseLookups.names1 — the aliases of the exact-name
tier, nothing re-implemented here), the fact by (doc_id, pid). The result list is never touched: the card
is one more field of the /instant answer.

Two generic pieces every release-SQLite card can reuse (the how-to cards are the next):

    ReleaseTable(root, role, file)   open a stamped release SQLite read-only, or None when the release has none
    answer_card(kind, title, url, answer, detail="", **extra)   the card dict the page renders

No network, no model: a lookup and some formatting.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import threading
import unicodedata
import weakref

FORMAT_VERSION, APP_ID = 1, 0x6C6F7331      # lookups_release.FORMAT_VERSION / APP_ID
N_ALIAS, N_SITE_LABEL, N_OFFICIAL = 2, 3, 10   # lookups_release: an article's names, a site's first label, the articles of a domain
TYPE_SHIFT = 28
MAX_NAME = 80
LIST_N = 10
_TOKEN = re.compile(r"[a-z0-9]+")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_WIKI = "https://en.wikipedia.org/wiki/"
# Two name rules found by the 70 fact questions that got no card, each behind its own flag, default off:
#   SEARCH_FACTS_BARE_TITLE=1|2   "who directed lassiter" -> "Lassiter (film)": see Facts.qualified
#   SEARCH_FACTS_UNICODE_NAMES=1  "gabor maté children" -> "Gabor Maté": see Facts.entity_keys
BARE_TITLE = {"1": 1, "2": 2}.get(os.environ.get("SEARCH_FACTS_BARE_TITLE", "1"), 0)
UNICODE_NAMES_ON = os.environ.get("SEARCH_FACTS_UNICODE_NAMES", "1") == "1"
# A third rule, found by "the john henry newman school": the template "{x} school" meant "where did X study",
# and an institution named after a person is a name, not a question. SEARCH_FACTS_INSTITUTION_NAME=1 (rc9 default 2, =0 restores rc8):
# a query that is the entity and ONE of these nouns and nothing else is the name of an institution, whatever the
# entity is, and gets no card. "education", "alma mater" and every question form ("where did X go to school") keep
# theirs. Not a list of schools: the engine cannot know a school the release does not hold, which is the point.
INSTITUTION_NAME_ON = {"0": 0, "1": 1, "2": 2}.get(os.environ.get("SEARCH_FACTS_INSTITUTION_NAME", "2"), 2)
INSTITUTION_NOUNS = ("school", "college", "university")
# =2 adds the known-name rule for the rest of that class ("<x> stadium", "<x> county", "<x> headquarters"...): the
# whole query is a name the release knows — a site's, an account's or a place's, where the rule below checks only an
# article's — so the user named that thing. It costs the questions typed as a bare name ("yankee stadium" when the
# facts set wants the team's venue): measured, not assumed. See Facts.known_name_kind.
N_HOST, N_DOMAIN, N_ACCOUNT, N_SELF = 4, 5, 7, 11    # lookups_release: a site's host label, its domain, an account's handle, a site's own name
N_SITE_KINDS = (N_SITE_LABEL, N_HOST, N_DOMAIN, N_SELF)
BARE_SCAN_MAX = 300                         # names read per key to find "X (…)" titles; more than that: no answer
_QUALIFIED = re.compile(r"^(.+) \([^()]+\)$")


def unicode_key(s: str) -> str:
    """The key the BUILD stores for a title (importer/wikiparse.py: lower(regexp_replace(t, '[^[:alnum:]]', ''))):
    every letter and digit kept, whatever the alphabet. norm_key drops the letters that are not a-z, so "Gabor Maté"
    is stored as "gabormaté" and asked for as "gabormat"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def norm_key(s: str) -> str:
    """search_api.norm_key: letters and digits only, lowercase."""
    return "".join(_TOKEN.findall(s.lower()))


def pattern_regex(p: str) -> str:
    """facts_build.pattern_regex: the first {x} is the entity, a second must repeat it."""
    first, rest = p.split("{x}", 1)
    return "^" + first + "(?P<x>.+?)" + rest.replace("{x}", "(?P=x)") + "$"


# --------------------------------------------------------------------------- the generic pieces
def answer_card(kind: str, title: str, url: str, answer: str | None, detail: str = "", **extra) -> dict:
    """The answer-card dict (the shape search_api.answer() already returns, so the page renders it as the
    answer box): title, answer, detail, url, question = kind. extra fields ride along (value, unit, as_of,
    source, items...). calls answer_card("howto", ...) with its own extras."""
    card = {"title": title, "answer": answer, "detail": detail or "", "url": url, "question": kind, "kind": kind}
    card.update({k: v for k, v in extra.items() if v is not None})
    return card


class ReleaseTable:
    """A stamped release SQLite (application_id, user_version, meta.role / built_release) opened read-only,
    one connection per thread. open(root, role) -> instance or None when the release has no such file (the
    layer is then simply off). Subclasses read their own tables through self.db / self.q."""

    file = ""
    role = ""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        _TABLES.add(self)
        db = self.db
        appid = db.execute("PRAGMA application_id").fetchone()[0]
        uv = db.execute("PRAGMA user_version").fetchone()[0]
        self.meta = dict(db.execute("SELECT key, value FROM meta"))
        if appid != APP_ID or uv != FORMAT_VERSION or self.meta.get("role") != self.role or not self.meta.get("built_release"):
            raise RuntimeError(f"{path}: application {appid:#x} / format {uv} / role {self.meta.get('role')}, "
                               f"want {APP_ID:#x} / {FORMAT_VERSION} / {self.role} — refusing")

    @classmethod
    def open(cls, root: str):
        path = os.path.join(root, cls.file)
        return cls(path) if os.path.exists(path) else None

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "db", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._local.db = c
        return c

    def release(self) -> None:
        """Close THIS thread's connection (audit DB-08: a request thread is new for every request and a connection sits in a
        reference cycle until a full GC; each one of a 1.1 GB file keeps SQLite's page cache)."""
        c = getattr(self._local, "db", None)
        if c is not None:
            self._local.db = None
            try:
                c.close()
            except sqlite3.Error:
                pass

    def q(self, sql: str, *args):
        return self.db.execute(sql, args).fetchall()


_TABLES: "weakref.WeakSet" = weakref.WeakSet()


def release_thread() -> None:
    """Close the calling thread's connection of every open release table (search_api Handler.finish)."""
    for t in list(_TABLES):
        t.release()


# --------------------------------------------------------------------------- formatting
def fmt_date(value: str, prec: int | None) -> tuple[dt.date | None, str]:
    """'1969-02-11', 11 -> (date, 'February 11, 1969'); precision 10 says the month, 9 the year; '-0044-03-15'
    is 44 BC (search_api.fact_date's rules)."""
    m = re.match(r"^(-?)(\d{1,6})-(\d\d)-(\d\d)$", value or "")
    if not m:
        return None, value
    neg, y, mo, d = m.group(1) == "-", int(m.group(2)), int(m.group(3)), int(m.group(4))
    y_txt = f"{y} BC" if neg else str(y)
    p = prec or 9
    if p >= 11 and mo and d:
        text = f"{_MONTHS[mo - 1]} {d}, {y_txt}"
    elif p == 10 and mo:
        text = f"{_MONTHS[mo - 1]} {y_txt}"
    else:
        text = y_txt
    try:
        return (None if neg or y < 1 else dt.date(y, mo or 1, d or 1)), text
    except ValueError:
        return None, text


def years_between(a: dt.date, b: dt.date) -> int:
    return b.year - a.year - ((b.month, b.day) < (a.month, a.day))


def fmt_number(v: float) -> str:
    """545796 -> '545,796'; 100.05 -> '100.05'; 70.0 -> '70': whole numbers from 10,000, else up to two decimals."""
    if abs(v) >= 10_000:
        return f"{v:,.0f}"
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt_big(v: float, cur: str = "") -> str:
    """12345678900 -> '$12.3 billion'; the currency's sign when known, its code otherwise."""
    sign = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "KRW": "₩"}.get(cur)
    a = abs(v)
    for lim, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if a >= lim:
            num = f"{v / lim:,.1f}".rstrip("0").rstrip(".") + f" {word}"
            break
    else:
        num = fmt_number(v)
    if sign:
        return sign + num
    return f"{num} {cur}".strip()


_LEN_M = {"m": 1.0, "cm": 0.01, "km": 1000.0, "ft": 0.3048, "in": 0.0254, "mi": 1609.344, "yd": 0.9144, "dm": 0.1}
_AREA_KM2 = {"km2": 1.0, "mi2": 2.589988, "ha": 0.01, "m2": 1e-6, "acre": 0.00404686, "ft2": 9.2903e-8}
_MASS_KG = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.0283495, "t": 1000.0}
_DUR_MIN = {"min": 1.0, "h": 60.0, "s": 1 / 60, "d": 1440.0}
_MONEY = {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD", "BRL", "INR", "RUB", "SEK", "ZAR", "DKK", "NOK", "HKD", "SGD",
          "KRW", "ILS", "MXN", "PLN", "NZD", "CZK", "TRY", "HUF", "EGP", "ARS", "CLP", "dollar"}


def fmt_quantity(family: str, v: str, unit: str | None) -> str | None:
    """A stored amount + unit symbol -> what the card says, in the unit family the template declares; None when
    the unit is one we cannot say (a Q-id) or the family does not fit the value."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    u = unit or ""
    if u.startswith("Q"):
        return None
    if family == "count":
        return fmt_number(x) if not u or u == "%" else (f"{fmt_number(x)}%" if u == "%" else None)
    if family == "length":
        if u not in _LEN_M:
            return None
        m = x * _LEN_M[u]
        if abs(m) >= 10_000:
            return f"{m / 1000:,.0f} km ({m / 1609.344:,.0f} mi)"
        return f"{m:,.0f} m ({m / 0.3048:,.0f} ft)" if abs(m) >= 10 else f"{m:,.2f} m ({m / 0.3048:,.1f} ft)"
    if family == "height":
        if u not in _LEN_M:
            return None
        cm = x * _LEN_M[u] * 100
        if not 30 <= cm <= 300:                            # not a person: the length family says it
            return fmt_quantity("length", v, unit)
        inches = round(cm / 2.54)
        return f"{cm / 100:.2f} m ({inches // 12} ft {inches % 12} in)"
    if family == "area":
        if u not in _AREA_KM2:
            return None
        km2 = x * _AREA_KM2[u]
        return f"{fmt_number(km2)} km² ({fmt_number(km2 / 2.589988)} sq mi)"
    if family == "mass":
        if u not in _MASS_KG:
            return None
        kg = x * _MASS_KG[u]
        if kg >= 10_000:
            return f"{kg / 1000:,.0f} t ({kg / 1000 * 1.10231:,.0f} short tons)"
        return f"{fmt_number(kg)} kg ({fmt_number(kg / 0.45359237)} lb)"
    if family == "money":
        if u not in _MONEY:
            return None
        return fmt_big(x, "USD" if u == "dollar" else u)
    if family == "duration":
        if u not in _DUR_MIN:
            return None
        mins = x * _DUR_MIN[u]
        if mins >= 60:
            h, m = divmod(int(round(mins)), 60)
            return f"{h} h {m} min" if m else f"{h} h"
        return f"{mins:,.0f} min"
    return None


def fmt_coord(v: str) -> str | None:
    m = re.match(r"^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$", v or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


# --------------------------------------------------------------------------- the facts file
class Facts(ReleaseTable):
    file = "facts.sqlite"
    role = "facts"

    def __init__(self, path: str):
        super().__init__(path)
        self.props = {pid: {"prop": prop, "name": name, "kind": kind, "unit": unit, "label": label}
                      for pid, prop, name, kind, unit, label in self.q("SELECT pid, prop, name, kind, unit, label FROM props")}
        self.templates = [(pid, re.compile(pattern_regex(p))) for pid, p in self.q("SELECT pid, pattern FROM templates ORDER BY pid, ord")]
        self.classes = {cls: (words.split("|"), dateprop) for cls, words, _q, dateprop in self.q("SELECT cls, words, qid, dateprop FROM list_classes")}
        self.genres = dict(self.q("SELECT word, genre FROM genres"))
        words = sorted({w for ws, _d in self.classes.values() for w in ws}, key=len, reverse=True)
        gen = sorted(self.genres, key=len, reverse=True)
        self._list_rx = re.compile(r"^(?:(?:the |best |top |list of |all )+)?(?:(?P<g>" + "|".join(map(re.escape, gen)) + r") )?"
                                   r"(?P<c>" + "|".join(map(re.escape, words)) + r")(?: (?:of|from|in|released in|made in))?"
                                   r" (?:the )?(?P<y>\d{4}|\d{3}0s|'?\d0s)$")

    # -- parsing
    def parse(self, q: str) -> list[tuple[int, str]]:
        """"how old is tom hanks" -> [(pid of P569, "tom hanks"), ...] — every template that fits, in table order."""
        t = " ".join(q.lower().strip().rstrip("?!. ").split())
        out = []
        for pid, rx in self.templates:
            m = rx.match(t)
            if m and 2 <= len(m.group("x")) <= MAX_NAME and (pid, m.group("x")) not in out:
                out.append((pid, m.group("x")))
        return out

    @staticmethod
    def is_institution_name(q: str, name: str) -> bool:
        """SEARCH_FACTS_INSTITUTION_NAME: the whole query is "<name> school|college|university" — the way a
        school named after a person is typed — so it names an institution, it does not ask about <name>."""
        t = " ".join(q.lower().strip().rstrip("?!. ").split())
        return any(t == f"{name} {noun}" for noun in INSTITUTION_NOUNS)

    def known_name_kind(self, q: str, lk) -> str | None:
        """SEARCH_FACTS_INSTITUTION_NAME=2: the whole query is a name the release knows that is not an article's
        (those are checked where the card is made): "site", "account" or "place", else None. The keys are the
        exact-name tier's (entity_keys: as typed, without and with a leading "the")."""
        keys = self.entity_keys(q.strip().rstrip("?!. "))
        if not keys:
            return None
        for kinds, what in ((N_SITE_KINDS, "site"), ((N_ACCOUNT,), "account")):
            if any(lk.names1(k, kind) for k in keys for kind in kinds):
                return what
        if lk.place_named(keys):
            return "place"
        return None

    def parse_list(self, q: str) -> tuple[str, str | None, int, int] | None:
        """"sci-fi films 1982" -> ("film", "science fiction", 1982, 1982); "albums 1990s" -> ("album", None, 1990, 1999)."""
        t = " ".join(q.lower().strip().rstrip("?!. ").split())
        m = self._list_rx.match(t)
        if not m:
            return None
        cls = next(c for c, (ws, _d) in self.classes.items() if m.group("c") in ws)
        y = m.group("y").lstrip("'")
        if y.endswith("0s"):
            y0 = int(y[:-1]) if len(y) == 5 else (1900 + int(y[:-1]) if int(y[:-1]) >= 30 else 2000 + int(y[:-1]))
            lo, hi = y0, y0 + 9
        else:
            lo = hi = int(y)
        return cls, self.genres.get(m.group("g")) if m.group("g") else None, lo, hi

    # -- lookups
    def facts_of(self, d: int, pid: int) -> list[tuple]:
        return self.q("SELECT v, unit, prec, at, until, vdoc FROM facts WHERE doc_id = ? AND pid = ? ORDER BY ord", d, pid)

    def has(self, d: int, pid: int) -> bool:
        return bool(self.q("SELECT 1 FROM facts WHERE doc_id = ? AND pid = ? LIMIT 1", d, pid))

    @staticmethod
    def entity_keys(name: str) -> list[str]:
        """The keys the exact-name tier would try: the name, without a leading "the", with one; accents folded too."""
        out = set()
        # as typed, and with accents folded: the names table holds "zlatanibrahimovic" for Zlatan Ibrahimović, and
        # norm_key alone would ask for "zlatanibrahimovi"
        for form in (name, "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))):
            nq = norm_key(form)
            if len(nq) < 2:
                continue
            bare = nq[3:] if nq.startswith("the") and len(nq) > 5 else nq
            out |= {nq, bare, "the" + bare}
        if UNICODE_NAMES_ON:
            # the title as the build keyed it, when the name has a letter outside a-z ("çamlıca", "władysław", "frænkel":
            # NFKD does not fold ı, ł or æ, and a folded key is only there when Wikipedia has that redirect)
            uq = unicode_key(name)
            if len(uq) >= 2 and uq not in out:                 # a name in a-z: uq is the key already there
                bare = uq[3:] if uq.startswith("the") and len(uq) > 5 else uq
                out |= {uq, bare, "the" + bare}
        return sorted(out)

    def qualified(self, keys: list[str], named: list[int], pid: int, lk) -> int | None:
        """SEARCH_FACTS_BARE_TITLE: the one article titled "<name> (<qualifier>)" for a bare name, or None. Asked
        only when nothing the name already names has the fact. `named` = the articles the name's keys name today.

        The rule. Collect, over all the release's article names that START with one of the name's keys, the articles
        whose OWN title is "X (…)" with X's key one of the name's (the "the" forms included). The answer is that
        article when there is EXACTLY ONE and it has the fact. Two or more ("Reacher (TV series)", "Reacher (film)"):
        none, whichever has the fact — never a guess between several. A key that starts more than BARE_SCAN_MAX
        names ("the", "john", "1883"): none, the titles were not all read.

        And the name itself must not be taken:
          mode 1  it names no article at all ("lassiter": Wikipedia's "Lassiter" is a disambiguation page, which the
                  release does not hold);
          mode 2  also when it only redirects to articles of another title ("sistas" -> "Sister", "tires" -> "Tire").
                  A redirect is Wikipedia's primary meaning of the name, so this is a guess against it whenever that
                  article lacks the fact: "republic of china" -> "Taiwan" would answer from "Republic of China
                  (1912–1949)".
        In neither mode when an article is titled X itself ("Boardwalk Empire" is the series; "Boardwalk Empire
        (episode)" is not what the bare name means)."""
        want = set(keys)
        named = [d for d in named if d >> TYPE_SHIFT == 1]
        if named and (BARE_TITLE < 2 or any(want & set(self.entity_keys(lk.title(d) or "")) for d in named)):
            return None
        found: set[int] = set()
        for k in keys:
            rows = lk.titles_starting(k, N_ALIAS, BARE_SCAN_MAX)
            if rows is None:
                return None
            for d, title in rows:
                if d >> TYPE_SHIFT != 1 or d in found:
                    continue
                m = _QUALIFIED.match(title or "")
                if m and want & set(self.entity_keys(m.group(1))):
                    found.add(d)
                    if len(found) > 1:
                        return None
        d = next(iter(found), None)
        return d if d is not None and self.has(d, pid) else None

    def resolve(self, name: str, pid: int, lk) -> int | None:
        """The article the name means that has the fact: the most viewed one (signals wiki.pageviews). A name that is
        no article's ("tesla": the title is a disambiguation page) is tried as a site's first label, and the articles
        whose official site that is ("Tesla, Inc.") — the engine's own official-site names, nothing matched here."""
        keys = self.entity_keys(name)
        docs = named = [d for k in keys for d in lk.names1(k, N_ALIAS)]
        if not any(d >> TYPE_SHIFT == 1 and self.has(d, pid) for d in docs):
            docs = [w for k in keys for site in lk.names1(k, N_SITE_LABEL)[:3] for w in lk.names1(lk.reg(site)[1], N_OFFICIAL)]
        best, best_pv = None, -1
        for d in dict.fromkeys(docs):
            if d >> TYPE_SHIFT != 1 or not self.has(d, pid):
                continue
            try:
                pv = lk.S(d, "pageviews") or 0
            except (KeyError, ValueError, IndexError):         # a release whose signals carry no pageviews
                pv = lk.S(d, "sitelinks") or 0
            if pv > best_pv:
                best, best_pv = d, pv
        if best is None and BARE_TITLE:
            best = self.qualified(keys, named, pid, lk)
        return best

    # -- cards
    def answer(self, q: str, lk, today: dt.date | None = None) -> dict | None:
        """The answer card for a fact question, or a list card, or None. lk is the engine's ReleaseLookups
        (names1, title, S)."""
        named = known = None
        for pid, name in self.parse(q):
            if INSTITUTION_NAME_ON and self.is_institution_name(q, name):
                continue
            if q.lower().lstrip().startswith(name):
                # "<name> county", "<name> party": when the whole query is itself an article's name ("Brașov County",
                # "Green Party") the user named that thing; the list answers it, no card
                if named is None:
                    named = any(d >> TYPE_SHIFT == 1 for k in self.entity_keys(q.strip().rstrip("?!. ")) for d in lk.names1(k, N_ALIAS))
                if named:
                    continue
                if INSTITUTION_NAME_ON >= 2:
                    # ... and a site's, an account's or a place's name the same way (the rest of the class)
                    if known is None:
                        known = self.known_name_kind(q, lk)
                    if known:
                        continue
            d = self.resolve(name, pid, lk)
            if d is None:
                continue
            card = self.fact_card(d, pid, lk, today)
            if card:
                return card
        lst = self.parse_list(q)
        return self.list_card(*lst, lk=lk) if lst else None

    def fact_card(self, d: int, pid: int, lk, today: dt.date | None = None) -> dict | None:
        p = self.props[pid]
        rows = self.facts_of(d, pid)
        if not rows:
            return None
        title = lk.title(d) or ""
        url = _WIKI + title.replace(" ", "_")
        today = today or dt.date.today()
        kind, fam, label = p["kind"], p["unit"], p["label"]
        v, unit, prec, at, until, vdoc = rows[0]
        if kind == "date":
            date, text = fmt_date(v, prec)
            detail = ""
            if p["prop"] == "P569" and date:                       # born: the age, unless a death date is on file
                died = self.facts_of(d, self._pid("P570"))
                if died:
                    dd, dtxt = fmt_date(died[0][0], died[0][2])
                    detail = f"died {dtxt}" + (f", aged {years_between(date, dd)}" if dd and (prec or 0) >= 11 else "")
                elif (prec or 0) >= 11:
                    detail = f"{years_between(date, today)} years old"
            elif p["prop"] == "P570" and date:
                born = self.facts_of(d, self._pid("P569"))
                if born:
                    bd, btxt = fmt_date(born[0][0], born[0][2])
                    detail = f"born {btxt}" + (f", aged {years_between(bd, date)}" if bd and (born[0][2] or 0) >= 11 else "")
            elif date and p["prop"] in ("P571", "P1619", "P577", "P575", "P619", "P580"):
                detail = f"{years_between(date, today)} years ago" if years_between(date, today) >= 1 else ""
            return answer_card("fact", title, url, text, detail, label=label, value=v, precision=prec, property=p["prop"], source="Wikidata")
        if kind == "quantity":
            text = None
            for v, unit, prec, at, until, vdoc in rows:              # the first value we can say
                text = fmt_quantity(fam, v, unit)
                if text:
                    break
            if not text:
                return None
            detail = f"as of {at[:4]}" if at and re.match(r"^\d{4}", at) else ""
            return answer_card("fact", title, url, text, detail, label=label, value=v, unit=unit or None, as_of=at, property=p["prop"], source="Wikidata")
        if kind == "coord":
            text = fmt_coord(v)
            return answer_card("fact", title, url, text, "", label=label, value=v, property=p["prop"], source="Wikidata") if text else None
        if kind == "string":
            vals = [r[0] for r in rows[:4]]
            return answer_card("fact", title, url, ", ".join(vals), "", label=label, value=vals[0], property=p["prop"], source="Wikidata")
        # item: current values first (the build ordered them), with each value's own article
        items = []
        for v, unit, prec, at, until, vdoc in rows[:4]:
            it = {"title": v}
            if vdoc is not None and lk.title(vdoc):
                it["url"] = _WIKI + lk.title(vdoc).replace(" ", "_")
            if at and re.match(r"^\d{4}", at):
                it["since"] = at[:4]
            if until and re.match(r"^\d{4}", until):
                it["until"] = until[:4]
            items.append(it)
        current = [i for i in items if "until" not in i] or items
        text = ", ".join(i["title"] for i in current[:4])
        detail = f"since {current[0]['since']}" if len(current) == 1 and current[0].get("since") else ""
        return answer_card("fact", title, url, text, detail, label=label, value=current[0]["title"], items=items,
                           value_url=current[0].get("url"), property=p["prop"], source="Wikidata")

    def _pid(self, prop: str) -> int:
        return next((pid for pid, p in self.props.items() if p["prop"] == prop), -1)

    def list_card(self, cls: str, genre: str | None, lo: int, hi: int, lk) -> dict | None:
        """The most viewed articles of the class in those years (lists.pv = the release's pageviews), genre-filtered
        by substring of the Wikidata genre labels."""
        sql = "SELECT doc_id, year, genre, pv FROM lists WHERE cls = ? AND year BETWEEN ? AND ?"
        args: list = [cls, lo, hi]
        if genre:
            sql += " AND instr(coalesce(genre, ''), ?) > 0"
            args.append(genre)
        rows = self.q(sql + " ORDER BY pv DESC LIMIT ?", *args, LIST_N)
        if not rows:
            return None
        items = []
        for d, year, g, pv in rows:
            t = lk.title(d)
            if t:
                items.append({"title": t, "url": _WIKI + t.replace(" ", "_"), "year": year})
        if not items:
            return None
        when = str(lo) if lo == hi else f"{lo}s"
        words = self.classes[cls][0][0]
        label = f"{genre.capitalize() + ' ' if genre else ''}{words} {when}"
        return answer_card("list", label, items[0]["url"], items[0]["title"], f"{len(items)} {words}, most read first",
                           items=items, cls=cls, genre=genre, years=[lo, hi], source="Wikidata")
