"""stage_assembly: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from stage_parts import PartStale
from stage_parts import _part_keys
from stage_card import card_pre
from stage_card import card_stage
from stage_verdict import conf_of
import contextvars
from stage_verdict import intent_kinds
from stage_list import list_stage
from stage_local import local_part
from stage_parts import note_part
from stage_parts import part_stamp
from stage_place import place_stage
from stage_verdict import verdict_stage
from search_api import (
    CURATED_RELEASE,
    FIN_FIELDS,
    FIN_V,
    MAX_K,
    _EXEC,
    _accounts_in_card,
    _staged,
    _warm,
    account_fits,
    answer,
    curated_key,
    curated_rows,
    fin_key,
    home_point,
    name_bare_rows,
    optional,
    parts_for,
    prov_put,
    result_store,
    same_page,
    search,
    short_org,
    stage,
    url_host_ok,
    wordy,
)


def fold_accounts(res: list[dict], card: dict | None) -> list[dict]:
    """The list without the profile rows the card carries as links. No card, or a card without links: res itself."""
    if not card or not card.get("links"):
        return res
    key = same_page(card.get("url") or "")
    carried = {l.get("url") for l in card["links"]}
    kept = [r for r in res if not (r.get("profile") and r.get("of") == key and r.get("url") in carried)]
    return kept if len(kept) != len(res) else res

def glue_build(q: str, res: list[dict], card: dict | None) -> list[dict]:
    """The steps between the stages, run for every answer and not stored (cheap, and their effect is in the content the
    list stage is keyed by): the kind card is taken off the rows, the card carries the accounts, branches, rows named
    by their bare address, accounts the card leaves out."""
    res = [{k: v for k, v in r.items() if k != "kind_card"} if "kind_card" in r else r for r in res]
    if _accounts_in_card.get():                      #: the card carries the accounts, the list keeps sites and the article
        res = fold_accounts(res, card)
    res = branches_only(res)
    # a site row whose address is not a host name is not a link ("https://1=microsoft.com/...", 2026-11-14), and a
    # row titled with its bare address is named after the card it belongs to
    res = name_bare_rows([r for r in res if r.get("source") != "site" or url_host_ok(r.get("url") or "")], card)
    if card:                                         # an account the card leaves out is not a result either ("IKEA on Discogs")
        what = card.get("description") or ""
        key = same_page(card.get("url") or "")
        res = [r for r in res if not (r.get("profile") and r.get("of") == key and not account_fits(r.get("platform", ""), what))]
    return res

def finished_answer(q: str, qs, want: bool, full: bool, plain: bool, fin_full: bool, log):
    """The visitor-independent part of an /instant answer, stage by stage (phase 2). Returns (res, conf, card, place,
    intent, kinds, local, sig): the fields of a finished answer. Each stage that is on goes through the parts store;
    this function's own body (the order of the stages, which fields come from which) is the residual (parts stage
    "rest"), the only code a re-stamp trusts rather than re-checks."""
    # local (does q name a business with branches?) beside the search, not after it (2026-11-03: a whole-world
    # places lookup, 0.15-0.4 s for "home depot", ran after the clock stopped). Only when results are wanted.
    f_local = (_EXEC.submit(contextvars.copy_context().run, _staged, "names_places", local_part, q)
               if want and q and home_point() is None else None)
    # results=0: suggestions only; full=1: a submitted search, results whatever the length and more of them;
    # as-you-type keeps the wordy gate
    if want and (full or wordy(q) or short_org(q)):
        res = search(q, MAX_K if full else 8)
    else:
        res = []
        note_part("base", "none")                    # no search ran: the answer says so, and a change to the gate shows
    pre = None
    if parts_for("verdict") is None:
        conf = optional("conf", lambda: conf_of(q, res), False, log)
    else:
        import copy
        pre = copy.deepcopy(res)                     # the verdict is judged on the rows before the card marks them
    card = None
    with stage("card"):
        try:                                                    # a card is extra: never fail the answer
            top, cres, base = card_pre(q, res)
            card, res = card_stage(q, res, top, cres, base)
        except PartStale:
            raise
        except Exception as e:
            log("card failed: %s", type(e).__name__)
    with stage("place"):
        card, place = place_stage(q, card, want, log)
    if pre is None:
        with stage("verdict"):
            intent, kinds = optional("intent", lambda: intent_kinds(q, res), ("", None), log)
    else:
        with stage("verdict"):
            conf, intent, kinds = optional("verdict", lambda: verdict_stage(q, pre, res), (False, "", None), log)
    with stage("glue"):
        res = optional("glue", lambda: glue_build(q, res, card), res, log)
    with stage("curated"):
        cur = (optional("curated", lambda: curated_rows(q) or curated_by_card(q, card), None, log)
               if res and not (qs.get("cur") or [""])[0] == "0" else None)
    with stage("list"):
        res, sig = optional("list", lambda: list_stage(q, res, card, cur), (res, {}), log)
    # local: this search names a business with branches and came with no position (names_places, started above)
    with stage("local_wait"):
        local = False
        if f_local is not None:
            try:
                lv = f_local.result()
                local = bool(lv) if res else False
            except PartStale:
                if res:
                    raise
            except Exception as e:                         # a lookup that failed says nothing, as before it ran
                log("names_places failed: %s", type(e).__name__)
            if not res:
                (_part_keys.get() or {}).pop("local", None)      # not part of this answer: its thread may or may not have noted it
    if _warm.get() and plain and (qs.get("fin") or [""])[0] == "1":     # the builder (twin/ship.sh) keeps what it computed
        fin_put(q, fin_full, {"results": res, "confident": conf, "card": card, "place": place, "intent": intent, "kinds": kinds,
                              "local": local, "sig": sig, "pk": part_stamp()})
    return res, conf, card, place, intent, kinds, local, sig

# A stop or a car park named after the store is not a branch of it (2026-11-06: "Ikea" listed "IKEA · ferry terminal",
# the Red Hook water-taxi stop, between two stores). Dropped only when real branches are listed beside it.
NOT_BRANCH = frozenset(("ferry terminal", "bus stop", "bus station", "station", "train station", "platform", "stop position",
                        "tram stop", "subway entrance", "halt", "taxi", "parking", "parking entrance", "parking space",
                        "bicycle parking", "motorcycle parking"))

def place_kind(r: dict) -> str:
    return (r.get("content") or "").split(" · ", 1)[0].strip().lower()

def branches_only(res: list[dict]) -> list[dict]:
    places = [r for r in res if r.get("source") == "place"]
    if len(places) < 2 or all(place_kind(r) in NOT_BRANCH for r in places):
        return res
    return [r for r in res if not (r.get("source") == "place" and place_kind(r) in NOT_BRANCH)]

def fin_put(q: str, full: bool, answer: dict) -> bool:
    """Keep the finished answer for q (the builder only: warm=1&fin=1)."""
    st = result_store() if CURATED_RELEASE else None
    if st is None:
        return False
    # eng: the engine (results fingerprint) that built it. Not read when serving; the builder rebuilds, or after its impact
    # check re-stamps, the answers another engine built
    t = (answer.get("card") or {}).get("title")
    body = {"v": FIN_V, "cur_at": st.curated_at(CURATED_RELEASE, curated_key(q)), "eng": st.fp,
            # when the list of the card title's own name ("spider-man" for "spiderman", curated_by_card) was written: a change
            # to it retires this answer too (audit B-07)
            "cur_at_card": st.curated_at(CURATED_RELEASE, curated_key(t)) if isinstance(t, str) and t else None,
            **{k: answer[k] for k in FIN_FIELDS}}
    if answer.get("pk"):
        body["pk"] = answer["pk"]       # the parts it was made from (part_stamp): a deploy that changes none of them re-stamps it
    ok = st.put_finished(CURATED_RELEASE, fin_key(q, full), body)
    if ok:
        prov_put(q, full)
    return ok

def _compact(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def _within(a: str, b: str, k: int) -> bool:
    """Edit distance of a and b at most k (small k: banded)."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] <= k

def curated_by_card(q: str, card: dict | None) -> list[dict] | None:
    """The curated list of the card's own name, when the query IS that name typed another way (owner 2026-11-06:
    "spiderman" has no list, "spider-man" has a good one): the same letters and digits ("spider man", "spiderman"),
    or within two edits for a name of 8 or more ("sarah j mass"). Never for a query that says more ("spiderman
    costume kids" keeps its own list)."""
    title = (card or {}).get("title") or ""
    a, b = _compact(q), _compact(title)
    if not a or not b or curated_key(q) == curated_key(title):
        return None
    if a != b and not (len(b) >= 8 and _within(a, b, 2)):
        return None
    return curated_rows(title)
