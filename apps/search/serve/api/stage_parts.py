"""stage_parts: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

import contextvars
import threading
from search_api import (
    PARTS_ON,
    PART_STAGES,
    _warm,
    parts_for,
    stage,
)


def part_run(stage: str, inputs: list, build, keep=lambda v: True):
    """One stage through the parts store. build() -> the stage's value (JSON-like); returns it. Key from the inputs'
    content; a hit replaces the computation; on a miss the builder (warm=1) keeps the value. In ?pcheck=1 mode a miss
    raises PartStale (the engine is asked only whether every part of an answer is current). Stage off = build()."""
    P = parts_for(stage)
    if P is None or _parts_off.get():
        if _pcheck.get():
            raise PartStale(stage)
        return build()
    try:
        key = P.key(stage, inputs)
        got = P.get(stage, key)
    except Exception:  # noqa: BLE001 - a part is extra: compute it live
        P.stats["error"] += 1
        key, got = None, None
    if got is not None:
        note_part(stage, key)
        return got["v"]
    if _pcheck.get():
        raise PartStale(stage)
    val = build()
    if key is not None and _warm.get() and keep(val):    # only the builder writes (a visitor's search is never kept)
        if P.put(stage, key, {"v": val}):
            note_part(stage, key)
    else:
        # why nothing was written, counted per stage (a smoke 2026-11-15 had 101 misses and 98 puts with unstorable and error both
        # 0 and nothing said which of the three reasons it was): P.stats["skip:base:cold"], "...:nokey", "...:keep"
        why = "nokey" if key is None else ("cold" if not _warm.get() else "keep")
        P.stats[f"skip:{stage}:{why}"] = P.stats.get(f"skip:{stage}:{why}", 0) + 1
    return val

class PartStale(Exception):
    """?pcheck=1: a part of this answer is not stored under the current key."""

_pcheck: contextvars.ContextVar = contextvars.ContextVar("pcheck", default=False)       # ?pcheck=1: answer only whether every part is current

_part_keys: contextvars.ContextVar = contextvars.ContextVar("part_keys", default=None)  # the keys of the parts this answer was made from

def _warm_q(qs) -> bool:
    return (qs.get("warm") or ["0"])[0] == "1"

def part_stamp():
    """The provenance of the answer being built: the key of every stage's part and of the residual (the rest of
    _instant's code, settings and data), or None when a stage is off or its part was not stored (nothing then says
    the answer can be re-stamped)."""
    box = _part_keys.get()
    P = _parts_state[0] if _parts_state else None
    if not box or P is None or not set(PART_STAGES) <= PARTS_ON or not {"base", "card", "place", "verdict", "list"} <= set(box):
        return None
    try:                                            # "local" is absent when no names_places lookup ran (a position was sent, or no results)
        return {"v": 2, "rest": P.key("rest", []), **{k: box[k] for k in sorted(box)}}
    except Exception:  # noqa: BLE001
        return None

def note_part(stage: str, key: str) -> None:
    box = _part_keys.get()
    if box is not None:
        box.setdefault(stage, []).append(key)

_parts_off: contextvars.ContextVar = contextvars.ContextVar("parts_off", default=False)       # ?parts=0: the old path (shadow compare)

_parts_state: list = []

_parts_lock = threading.Lock()
