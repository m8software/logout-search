"""stage_verdict: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from stage_parts import part_run
from search_api import (
    intent_of,
    is_confident,
)


def conf_of(q: str, res: list[dict]) -> bool:
    return bool(res) and is_confident(res) and intent_of(q, res) == "nav"

def intent_kinds(q: str, res: list[dict]):
    intent = intent_of(q, res) if res else ""        # judged on the list as ranked, before the mix
    kinds = res[0].get("kind_card") if res and res[0].get("kind_top") else None
    return intent, kinds

def verdict_build(q: str, pre: list[dict], res: list[dict]) -> dict:
    """The verdict stage (parts stage "verdict"): the confidence flag (judged on the base rows, before the card),
    the intent and the kind card (on the rows after the card)."""
    intent, kinds = intent_kinds(q, res)
    return {"conf": conf_of(q, pre), "intent": intent, "kinds": kinds}

def verdict_stage(q: str, pre: list[dict], res: list[dict]):
    import parts as _parts
    v = part_run("verdict", [q, _parts.canon(pre).decode(), _parts.canon(res).decode()], lambda: verdict_build(q, pre, res))
    return v["conf"], v["intent"], v["kinds"]
