"""The ECB's daily euro reference rates, kept in the news file (2026-10-06: "100 usd to eur").

One small public file, https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml: about 30 currencies
against the euro for one date, published once per working day around 16:00 CET. The news poller's loop calls
step() on every tick; it fetches at most once every EVERY_S (6 h, so four requests a day), remembers the last
fetch in the table itself (a restart does not fetch again), and replaces the fx_rates table in one transaction.
search-api reads the table (news_sqlite); a search never causes a request.

`fetched` is the time of the last SUCCESSFUL fetch (Unix seconds, UTC, like every time in the news file): it
moves on every good fetch, also when the ECB's file is the same as last time, and never on a failed one. The
answer says it ("checked 2 h ago") and stops answering when it is over three days old.

SQLite only (NEWS_SQLITE): the legacy Postgres news database has no fx_rates table and gets no rates.
NEWS_FX=0 turns the fetch off.

Terms (read 2026-10-06, docs/legal/SEARCH-DATA-SOURCES.md): free use, shown accurately, the ECB cited as the
source, a calculation on the data stated as one. The answer box does both (instant_answers.currency).
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import time
import urllib.error
import urllib.request

import newsdb  # noqa: E402  (the NEWS_SQLITE file)

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
EVERY_S = 6 * 3600            # the file changes once per working day: four looks a day find it within 6 h
RETRY_S = 1800                # after a failed fetch or a locked file
TIMEOUT_S = 20
MAX_BYTES = 256 * 1024        # the real file is under 2 KB
MIN_CURRENCIES = 10           # fewer than this is not the file we know: keep what is stored
ENABLED = os.environ.get("NEWS_FX", "1") != "0"
# The news poller's own User-Agent when it is set (the public site's names a contact, ADR-062); otherwise one
# that says what this request is and where to reach the project.
USER_AGENT = os.environ.get("NEWS_USER_AGENT") or \
    "logout-news/1 (daily ECB euro reference rates for a home search box; contact +https://logout.cloud)"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE = re.compile(r"^[A-Z]{3}$")


def log(msg: str) -> None:
    print(f"[fx] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def parse(body: bytes) -> tuple[str, dict[str, float]]:
    """eurofxref-daily.xml -> ("2026-10-06", {"USD": 1.1269, ...}): units of each currency per euro. Entity
    expansion is off (defusedxml). Anything that is not the file we know raises ValueError, so the caller keeps
    the rates it has."""
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"not XML: {type(e).__name__}") from None
    day = next((el for el in root.iter() if el.tag.rpartition("}")[2] == "Cube" and el.get("time")), None)
    if day is None:
        raise ValueError("no dated Cube")
    date = day.get("time").strip()
    if not _DATE.match(date):
        raise ValueError("bad date")
    dt.date.fromisoformat(date)                       # 2026-13-45 is a ValueError too
    rates: dict[str, float] = {}
    for el in day:
        code, raw = (el.get("currency") or "").strip(), (el.get("rate") or "").strip()
        if not _CODE.match(code) or code == "EUR":
            continue
        try:
            r = float(raw)
        except ValueError:
            continue
        if math.isfinite(r) and r > 0:
            rates[code] = r
    if len(rates) < MIN_CURRENCIES or "USD" not in rates:
        raise ValueError(f"only {len(rates)} usable rates")
    return date, rates


def store(conn, date: str, rates: dict[str, float], fetched: float | None = None) -> int:
    """Replace the table in one transaction: a reader sees the old day or the new one, never half. `fetched`
    (now) is written on every row, so an unchanged file still moves the time of the last successful check."""
    at = time.time() if fetched is None else fetched
    rows = [(c, r, date, at) for c, r in sorted(rates.items())]
    with conn.transaction():
        conn.execute("DELETE FROM fx_rates")
        conn.executemany("INSERT INTO fx_rates (currency, per_eur, as_of, fetched) VALUES (%s, %s, %s, %s)", rows)
    return len(rows)


def last_fetched(conn) -> float:
    row = conn.execute("SELECT max(fetched) FROM fx_rates").fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def fetch() -> bytes:
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml",
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large")
    return body


def step(due: float | None, now: float | None = None, fetcher=None) -> float:
    """One look from the poller's loop: fetch and store when due, and say when to look next. due None (the first
    call) reads the last fetch from the table. Never raises: a failure keeps the stored rates and retries later."""
    now = time.time() if now is None else now
    if not ENABLED or not newsdb.is_sqlite():
        return math.inf
    try:
        if due is None:
            with newsdb.connect() as conn:
                due = last_fetched(conn) + EVERY_S
        if now < due:
            return due
        date, rates = parse((fetcher or fetch)())
        with newsdb.connect() as conn:
            n = store(conn, date, rates, now)
        log(f"ECB reference rates of {date}: {n} currencies")
        return now + EVERY_S
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, newsdb.sqlite3.Error) as e:
        log(f"failed ({type(e).__name__}: {str(e)[:120]}) — retry in {RETRY_S // 60} min")
        return now + RETRY_S
