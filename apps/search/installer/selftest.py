#!/usr/bin/env python3
"""The self-test an install runs after every update: a few searches against the local engine.

  engine      /readyz answers 200 and the fixed smoke searches (install.smoke_failures) return a card or results.
  stored      a name that the answers file holds (read from the install's own store, read-only: only the published answers
              are there, never a visitor's or the user's search) is served as a stored answer: /explain says origin
              "finished". Skipped, and said so, when the file holds no answer young enough to be served (21 days) for the
              release in use: an update to a new data release has none until the lists are taken in again.
  card        that name's answer has a card, and /instant returns it.

`run` returns {"ok", "at", "ms", "checks": [{"name", "status": pass|fail|skip, "ms", "why"}]}. ok is False only when a check
failed; a skipped one is not a failure. A failing check is tried once more after a few seconds (a machine that is busy loading
its indexes answers slowly), so a good update is not reverted over one slow answer.

autoupdate.verify decides what a failure means (switch back, never try that version again). Standard library only.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

FIN_MAX_AGE_S = 21 * 86400                     # search_api.FIN_MAX_AGE_S: an older stored answer is not served
TIMEOUT_S = 30.0
RETRY_AFTER_S = 5.0


def store_path(data: str) -> str:
    return os.environ.get("SEARCH_STORE") or os.path.join(data, "store", "results.sqlite")


def known_names(data: str, release: str, n: int = 3, now: float | None = None, rng=None) -> list:
    """[(name, has_card)]: up to n names whose finished answer (full form) the store holds for this release and would serve."""
    p = store_path(data)
    if not release or not os.path.isfile(p):
        return []
    now = now or time.time()
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        # the engine serves a finished answer only when no hand-picked list for the name is newer (search_api.fin_get): such a name
        # is left out, or the test would call a healthy engine broken
        rows = db.execute("SELECT f.key, f.body FROM finished f LEFT JOIN curated c ON c.release = f.release "
                          "AND c.key = substr(f.key, 1, length(f.key) - 5) AND c.at > f.at "
                          "WHERE f.release = ? AND f.at > ? AND f.key LIKE '%' || char(31) || 'full' AND c.key IS NULL "
                          "ORDER BY f.at DESC LIMIT 400", (release, now - FIN_MAX_AGE_S + 3600)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    pool = []
    for key, body in rows:
        name = key.split("\x1f", 1)[0]
        if len(name) < 3 or len(name) > 60:
            continue
        try:
            d = json.loads(zlib.decompress(body))
        except (zlib.error, ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("results"):
            pool.append((name, bool(d.get("card"))))
    (rng or random.Random()).shuffle(pool)
    cards = [x for x in pool if x[1]]
    rest = [x for x in pool if not x[1]]
    return (cards[:2] + rest[:1] + cards[2:] + rest[1:])[:n]


def explain_origin(port: int, q: str, timeout: float = TIMEOUT_S) -> dict:
    url = f"http://127.0.0.1:{port}/explain?" + urllib.parse.urlencode({"q": q})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        d = json.load(r)
    return d.get("origin") or {} if isinstance(d, dict) else {}


def instant(port: int, q: str, timeout: float = TIMEOUT_S) -> dict:
    import install
    return install._get_json(port, q, timeout)


def readyz(port: int) -> int:
    import install
    return install._readyz(port)


def run(port: int, data: str, release: str, *, has_se: bool = False, names=None, get=None, explain=None, ready=None,
        sleep=time.sleep, quarantined: bool = False) -> dict:
    """One self-test. get/explain/ready/sleep are replaced in tests. names: [(name, has_card)] (default: from the store)."""
    import install
    get, explain, ready = get or instant, explain or explain_origin, ready or readyz
    t0 = time.monotonic()
    checks = []

    def attempt(name, fn):
        """fn() -> "" (pass), "skip: why" or a failure text; tried twice."""
        s0 = time.monotonic()
        why = fn()
        if why and not why.startswith("skip"):
            sleep(RETRY_AFTER_S)
            why = fn()
        status = "pass" if not why else "skip" if why.startswith("skip") else "fail"
        checks.append({"name": name, "status": status, "ms": int((time.monotonic() - s0) * 1000), "why": why[:300]})

    attempt("engine ready", lambda: "" if ready(port) == 200 else f"/readyz did not answer 200 (got {ready(port)})")

    def smoke():
        bad = install.smoke_failures(port, has_se, timeout=TIMEOUT_S, get=lambda p, q, t: get(p, q, t))
        return "; ".join(bad)[:300]
    attempt("fixed searches", smoke)

    if names is None:
        names = [] if quarantined else known_names(data, release)
    if not names:
        checks.append({"name": "stored answers served", "status": "skip", "ms": 0,
                       "why": "skip: " + ("stored answers are switched off" if quarantined else
                                          "no stored answer young enough for release " + str(release or "?"))})
        checks.append({"name": "card present", "status": "skip", "ms": 0, "why": "skip: no stored name to ask for"})
    else:
        def served():
            # one of three is enough: a name can fall out of use for reasons the file cannot show (a hand-picked list written a
            # second ago, an answer built by another layout); none of three is an answers file the engine does not read
            bad, hit = [], 0
            for name, _c in names[:3]:
                try:
                    origin = explain(port, name)
                except (OSError, ValueError) as e:
                    bad.append(f"{type(e).__name__} for a stored name")
                    continue
                if origin.get("kind") == "finished":
                    hit += 1
                else:
                    bad.append(f"a stored name was answered {origin.get('kind') or 'with no origin'}, not from its stored answer")
            return "" if hit else "; ".join(bad)
        attempt("stored answers served", served)
        withcard = [nm for nm, c in names if c]

        def card():
            if not withcard:
                return "skip: no stored answer with a card"
            try:
                d = get(port, withcard[0], TIMEOUT_S)
            except (OSError, ValueError) as e:
                return f"{type(e).__name__} asking for a stored name"
            return "" if isinstance(d, dict) and d.get("card") else "a stored name's card is missing from the answer"
        attempt("card present", card)
    ok = not any(c["status"] == "fail" for c in checks)
    return {"ok": ok, "at": int(time.time()), "ms": int((time.monotonic() - t0) * 1000), "release": release, "checks": checks}


def summary(r: dict) -> str:
    bad = [f"{c['name']}: {c['why']}" for c in r.get("checks", []) if c["status"] == "fail"]
    return "self-test passed" if r.get("ok") else "self-test failed: " + " | ".join(bad)
