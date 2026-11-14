"""/candidates — every list the fusion blends and each reordering after it, with scores.

Development tooling for the candidate dump (tools/sq-candidates.py; read by the recall audit, hard
negatives and the precompute). It runs the same search /explain runs — one more time, with the
trace on, never from or into the result cache — and returns up to `rows` results per step instead of
/explain's TRACE_ROWS. Ranking is untouched: the trace is a read of what each step produced.

    GET /candidates?q=...&rows=20   ->  {"q", "corrected"?, "lists": [{"name", "n", "rows"}], "named",
                                         "fused", "merged", "ranker", "ce", "final", "ms"}

Each row: url, title, source, score, and buildbox / ce / pos0 / flags when the step set them (search_api.trace_rows).
`lists` are the fusion's inputs in the order they were blended (wiki, sites, anchors, pages, sem, ...);
`named` the exact tiers above them; `fused` the RRF of the lists; `merged` the whole pool in one order;
`ranker` and `ce` the two reorderings; `final` what /search answers (after the endings and front-page passes).
One hook line in search_api.py routes here; nothing in it is logged.

Text mode, opt-in (`&text=1`; the reranker label pool, the bake-off, trained retrieval): each row
also carries `title_full` (the title uncut — `title` stays cut at 90 characters), `content` (the result's whole
text) and `ce_text` (the one string search_api.ce_text() hands the cross-encoder for that row: under
SEARCH_CE_TEXT=domain "<host> | <title>. <first 240 characters of content>"), and the body says which form and
token cap were in force: "text": {"ce_text": "domain", "ce_max_tokens": 96}. The rows the cross-encoder really
scores are the first SEARCH_CE_TOP below the exact tiers of the list before it (`ranker`, else `merged`); the
passes after it (endings, front-page names) can rename a `final` row, so its text is that row's as answered.
Without `text=1` the answer is byte for byte what it was before text mode existed.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs  # noqa: F401  (the handler passes a parsed query string; kept for callers that pass raw)

MAX_ROWS = 50
DEFAULT_ROWS = 20


def text_fields(r: dict) -> dict:
    """Text mode's three fields for one result row, read from the row as the step left it. Nothing is cut here:
    the 240-character cut is ce_text's own, the token cap the model's."""
    import search_api as S
    return {"title_full": r.get("title") or "", "content": r.get("content") or "", "ce_text": S.ce_text(r)}


def shape(steps: list[dict], final: dict, q: str, corrected: str | None, ms: int, text: dict | None = None) -> dict:
    """The body from a trace: the lists by name, then one entry per reordering step. Pure, for tests."""
    body: dict = {"q": q, "lists": [], "ms": ms}
    if corrected:
        body["corrected"] = corrected
    for s in steps:
        if s.get("step") == "list":
            body["lists"].append({"name": s.get("name", ""), "n": s.get("n", 0), "rows": s.get("rows", [])})
        elif s.get("step") in ("named", "fused", "merged", "ranker", "ce", "fuzzy"):
            body[s["step"]] = {"n": s.get("n", 0), "rows": s.get("rows", [])}
    body["final"] = final
    if text:
        body["text"] = text
    return body


def run(q: str, rows: int, text: bool = False) -> dict:
    import search_api as S
    tr, st, t0 = [], {}, time.monotonic()
    tok_t, tok_s, tok_n = S._trace.set(tr), S._stages.set(st), S._trace_n.set(rows)
    tok_m = S._trace_more.set(text_fields) if text else None
    try:
        res = S._search_retry(q, S.MAX_K, True, True, True)
        final = S.trace_rows(res)                      # inside the context: `rows` results, not TRACE_ROWS
    finally:
        S._trace.reset(tok_t)                          # worker threads are reused: never leave a trace on
        S._stages.reset(tok_s)
        S._trace_n.reset(tok_n)
        if tok_m is not None:
            S._trace_more.reset(tok_m)
    fixed = res[0].get("corrected") if res else None
    return shape(S.trace_run(tr, fixed or q), final, q, fixed, int((time.monotonic() - t0) * 1000),
                 {"ce_text": S.CE_TEXT, "ce_max_tokens": S.CE_MAX_TOKENS} if text else None)


def handle(qs: dict) -> tuple[int, bytes]:
    """(status, body) for the HTTP handler. 503 while the engine is still loading, like /explain."""
    import search_api as S
    if not S.serving_ready():
        return 503, b'{"error":"starting"}'
    q = (qs.get("q") or [""])[0].strip()[:200]
    if len(q) < 2:
        return 400, b'{"error":"q"}'
    try:
        rows = max(1, min(MAX_ROWS, int((qs.get("rows") or [str(DEFAULT_ROWS)])[0])))
    except ValueError:
        rows = DEFAULT_ROWS
    try:
        return 200, json.dumps(run(q, rows, (qs.get("text") or [""])[0] == "1")).encode()
    except Exception as e:                             # never the query text
        return 503, json.dumps({"error": type(e).__name__}).encode()
