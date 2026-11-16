"""stage_local: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from stage_parts import part_run
from search_api import (
    NoPostgres,
    PLACE_EXACT_SQL,
    _rows,
    cached,
    place_keys,
    place_name_core,
    place_query,
    stored,
)


def local_build(q: str) -> bool:
    """The local stage (parts stage "local"): does q name a business with branches (names_places)?"""
    return bool(cached(("names_places", q.lower()),
                       lambda: stored("names_places", (q.lower(),), lambda: [names_places(q)]), static=True)[0])

def local_part(q: str) -> bool:
    return part_run("local", [q], lambda: local_build(q))

# The middle of the lower 48: only a point to measure from, for a question that has none of its own.
_NOWHERE = (39.83, -98.58)

NAMES_PLACES_MIN = 20         # at 3, "python" was a business with branches (2026-10-11)

def names_places(q: str) -> bool:
    """Does q name a business with branches — NAMES_PLACES_MIN or more places of that name or brand, anywhere?
    Asked for a search that came with no position: the public page then knows the answer depends on where
    the searcher is and may send its area ("t mobile store", "autozone": owner 2026-10-11, a fixed list of
    chains on the page missed them). Exact names only (letters and digits), with and without a trailing
    "store" / "locations"; never a category word, never more than four words."""
    pq = place_query(q)
    if not pq or pq[0] != "name":
        return False
    keys = list(dict.fromkeys(place_keys(pq[1]) + place_keys(place_name_core(pq[1]))))
    try:
        rows = _rows(PLACE_EXACT_SQL, {"lat": _NOWHERE[0], "lon": _NOWHERE[1], "keys": keys, "r": 1e6})
    except NoPostgres:                                      # no places in this database
        return False
    except Exception as e:
        # only "no such table" means no places; a timeout or a dropped connection must not be remembered as "not a chain"
        # for the cache's 6 hours (CORE-A-01): it propagates, cached() keeps nothing, local_part logs it
        if type(e).__name__ == "UndefinedTable" or "no such table" in str(e):
            return False
        raise
    return len({(round(r[8], 3), round(r[9], 3)) for r in rows}) >= NAMES_PLACES_MIN
