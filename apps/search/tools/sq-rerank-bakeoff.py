#!/usr/bin/env python3
"""The reranker bake-off harness: rerank a candidate dump OFFLINE with any cross-encoder and score it
against the shipped one, on the DEV halves only, with the accept keys and a paired interval. No engine.

    sq-rerank-bakeoff.py subset --dump DIR --out DIR [--sets a,b] [--rows all|select|train]
    sq-rerank-bakeoff.py score  --dump DIR --out DIR --model NAME=SPEC [--form domain|title] [--threads 4]
                                [--device cpu|cuda] [--limit N]
    sq-rerank-bakeoff.py check  --dump DIR --out DIR --model NAME [--tol 0.02]
    sq-rerank-bakeoff.py table  --dump DIR --out DIR --shipped NAME [--models a,b] [--rows all|select|train]
    sq-rerank-bakeoff.py holdout --out FILE
    sq-rerank-bakeoff.py judged --dump DIR --out DIR --shipped NAME --verdicts FILE [--set judged-info] [--unjudged FILE]

The dump is tools/sq-candidates.py's (<set>.jsonl: one /candidates answer per query). Both kinds are read:
  cut dump   every title cut at 90 characters, no content (the dump of 2026-10-12). Every model is fed LESS than at
             serving, so every number from it is a HARNESS CHECK, NOT A RESULT — the table says so in its first line.
  text dump  sq-candidates.py --text: each row also has title_full, content and ce_text (the one string
             search_api.ce_text() handed the model). Used whenever present; only then is a number a result.

What is mirrored from the engine (search_api.py; tests/unit/test-search-sq-rerank-bakeoff.sh runs the engine's own
ce_rerank and ce_text next to these and fails when they part):
  - the model reads the list of the step before it: `ranker` when the learned ranker ran (it does not when
    keep_top() pins the first result), else `merged`. A query without a `ce` step was not reranked at serving
    (web-only wording): it is left as it was.
  - ce_rerank: rows scoring >= CE_EXACT (897: named sites, exactly-named articles, places) keep their hand order on
    top and are NEVER moved; the first CE_TOP (8) rows below them are reordered by the model, highest first; the
    rest follow as they were. Fewer than two rows below the pins: nothing is scored.
  - the query is the corrected spelling when the engine answered a corrected search (the dump's `corrected`).
  - one call per query with its up-to-CE_TOP texts, as ce_rerank makes it: the int8 model's score for a row
    depends on the rows beside it, so the batch is part of what is mirrored.
  - ce_text: "<host without www.> | <title>. <first 240 characters of content>" (SEARCH_CE_TEXT=domain), 96 tokens.
NOT mirrored: the engine's Wikipedia floor (CE_WIKI_FLOOR: an article the model scores at -7 or less keeps
its place unless the query names a state; calibrated on the shipped model, so every arm here is the model alone, as
drop=cewiki serves it), and the passes after the model (other_endings_last, profiles_follow_article, name_front_pages,
untitled_last). Every arm is therefore scored on the list as the reranker left it (the `ce` step), and the table
shows the shipped model's `final` step beside it so the size of that gap is on the page.

Arms: `dumped` is the dump's own `ce` step (the shipped model as it ran at serving, on the full text). Every
--model is scored through this harness on the dump's text. The paired interval is candidate minus --shipped (the
shipped model re-scored here on the SAME text), 95% paired bootstrap, compare-evals.py's rule — the one sq-eval.py
uses. `reach` is how many rows the reranker could change at all (no pinned row: hit@1; fewer than five: hit@5).

SPEC: onnx:DIR[:FILE]   tokenizer.json + model_int8.onnx / model.onnx (or FILE, relative to DIR), fed exactly as
                        search_api._CrossEncoder feeds it. --device cuda uses the CUDA provider.
      hf:DIR_OR_ID      transformers AutoModelForSequenceClassification (torch), first logit.
      hf+code:DIR       the same with the model's own code (trust_remote_code: JinaBERT). Read the code first.
      py:FILE           FILE defines make_scorer(threads, device) -> scorer(query, [text]) -> [float]
                        (a model that is not a sequence classifier, e.g. Qwen3-Reranker's yes/no logits).
      replay            the dump's own rounded `ce` scores (checks the mirror, no model).

Selection split (docs/internal/search-jev/14-reranker-bakeoff-plan.md): the labels v2 are drawn from the
dev-half queries, so a model trained on them has seen the dev halves. held_out() puts one third of the dev rows
aside, by entity (else by the query's key) and a fixed seed; `holdout` writes those queries for the trainer to
leave out, and `table --rows select` scores on them alone. A zero-shot model can be scored on --rows all.

TEST halves are never read: there is no option for them. `subset` writes the dev queries' reranker inputs only
(what a GPU host needs for `score`), marked so `score` runs there without the eval sets.
DEV TOOLING — never imported by apps/search/serve."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

# search_api.py: CE_EXACT = WIKI_EXACT_SCORE - 3, CE_TOP, CE_MAX_TOKENS, ce_text's cut. The unit test reads the
# engine's own values and fails when these differ.
CE_EXACT = 897.0
CE_TOP = 8
CE_MAX_TOKENS = 96
CE_CONTENT_CHARS = 240
TITLE_CUT = 90                       # search_api.trace_rows: the cut dump's title
ROUND_TOL = 0.005                    # the dump rounds a model score to two places
HOLDOUT_SEED, HOLDOUT_PARTS = "sq-475", 3   # the selection third of the dev halves; changing either re-deals it
SUBSET_MARK = "SUBSET.json"
KEEP_STEPS = ("merged", "ranker", "ce", "final", "text", "corrected")
CUT_BANNER = ("**HARNESS CHECK, NOT A RESULT.** This dump cuts every title at 90 characters and has no content, so "
              "each model read less than it reads at serving.")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the dump
def read_dump(dump: Path, name: str) -> dict:
    """{query: answer} from <dump>/<name>.jsonl (the last answer for a query wins, as sq-candidates.py reads it)."""
    out = {}
    path = dump / f"{name}.jsonl"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if "error" not in d:
                    out[d["q"]] = d
    return out


def is_subset(dump: Path) -> bool:
    return (dump / SUBSET_MARK).exists()


def dump_sets(dump: Path, want: str = "") -> list[str]:
    have = sorted(p.stem for p in dump.glob("*.jsonl"))
    if want:
        return [s.strip() for s in want.split(",") if s.strip() in have]
    return [s for s in C.DEFAULT_SETS if s in have] + [s for s in have if s not in C.DEFAULT_SETS]


def dev_queries(dump: Path, name: str) -> list[str]:
    """The DEV-half queries of one set, in set order. A subset holds dev queries only (cmd_subset wrote it from
    the sets), so it is read as it is; anything else is filtered by the set file. Never the test half."""
    if is_subset(dump):
        mark = json.loads((dump / SUBSET_MARK).read_text())
        if mark.get("split") != "dev":
            raise SystemExit(f"{dump}: {SUBSET_MARK} does not say split=dev")
        return list(read_dump(dump, name))
    return list(dict.fromkeys(r["query"] for r in C.load_set(name, "dev")))


def has_text(d: dict) -> bool:
    """A text-mode answer (sq-candidates.py --text): the body says which form the engine fed its model."""
    return bool(d.get("text"))


def ce_input(d: dict) -> tuple[list[dict], int] | None:
    """(rows, n) of the list the engine's reranker was handed, or None when it did not run for this query."""
    if "ce" not in d:
        return None
    step = d.get("ranker") or d.get("merged")
    if not step:
        return None
    return step.get("rows", []), int(step.get("n", len(step.get("rows", []))))


def model_query(d: dict) -> str:
    """The query the engine handed its reranker: the corrected spelling when the answer is a corrected search's
    (search_api._search_corrected searches the fixed spelling; sq_candidates.run records it as `corrected`)."""
    return d.get("corrected") or d["q"]


def split_rows(rows: list[dict], n: int | None = None) -> tuple[list[dict], list[dict], list[dict], bool]:
    """(pinned, candidates, tail, complete) as ce_rerank splits a list. complete is False when the dump kept
    fewer rows than the step had and that may have cut a candidate off."""
    top = [r for r in rows if r["score"] >= CE_EXACT]
    rest = [r for r in rows if r["score"] < CE_EXACT]
    complete = not (n is not None and len(rows) < n and len(rest) < CE_TOP)
    return top, rest[:CE_TOP], rest[CE_TOP:], complete


def ce_text(r: dict, form: str = "domain") -> str:
    """search_api.ce_text for a dump row. A text dump's title_full / content give the serving string; a cut dump
    has the 90-character title and nothing else."""
    title = r["title_full"] if "title_full" in r else (r.get("title") or "")
    text = title + ". " + (r.get("content") or "")[:CE_CONTENT_CHARS]
    if form == "domain":
        host = (urlsplit(r.get("url") or "").hostname or "").removeprefix("www.")
        return host + " | " + text
    return text


def row_text(r: dict, d: dict, form: str) -> tuple[str, bool]:
    """(what the model reads for this row, whether that is the full serving text). The engine's own string is
    used when the dump carries it in the form asked for."""
    if has_text(d) and "ce_text" in r and (d["text"].get("ce_text") or "domain") == form:
        return r["ce_text"], True
    return ce_text(r, form), has_text(d) and "title_full" in r


def rerank(q: str, rows: list[dict], scorer) -> list[dict]:
    """search_api.ce_rerank on dump rows."""
    top, cand, tail, _ = split_rows(rows)
    if len(cand) < 2:
        return rows
    sc = scorer(q, cand)
    order = sorted(range(len(cand)), key=lambda i: -sc[i])
    return top + [dict(cand[i], ce=round(float(sc[i]), 2)) for i in order] + tail


# ---------------------------------------------------------------- models
class OnnxScorer:
    """search_api._CrossEncoder, with the thread count and the provider as arguments."""

    def __init__(self, model_dir: str, file: str | None, threads: int, device: str):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        self.np = np
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=CE_MAX_TOKENS)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        opts.intra_op_num_threads = threads
        path = os.path.join(model_dir, file) if file else os.path.join(model_dir, "model_int8.onnx")
        if not file and not os.path.exists(path):
            path = os.path.join(model_dir, "model.onnx")
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if device == "cuda" else ["CPUExecutionProvider"]
        if device == "cuda" and hasattr(ort, "preload_dlls"):
            ort.preload_dlls()                       # CUDA/cuDNN from the torch wheels, when they are what is installed
        self.sess = ort.InferenceSession(path, opts, providers=providers)
        if device == "cuda" and self.sess.get_providers()[0] != "CUDAExecutionProvider":
            raise SystemExit(f"{path}: asked for cuda, onnxruntime gave {self.sess.get_providers()}")
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.path, self.size = path, os.path.getsize(path)

    def __call__(self, q: str, texts: list[str]) -> list[float]:
        np = self.np
        enc = self.tok.encode_batch([(q, t) for t in texts])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        return self.sess.run(None, feed)[0].reshape(len(texts), -1)[:, 0].tolist()


class HFScorer:
    """A sequence-classification cross-encoder through transformers (a fine-tuned checkpoint before its export)."""

    def __init__(self, ident: str, threads: int, device: str, remote_code: bool = False):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(threads)
        self.torch, self.device = torch, device
        self.tok = AutoTokenizer.from_pretrained(ident, trust_remote_code=remote_code)
        self.model = AutoModelForSequenceClassification.from_pretrained(ident, trust_remote_code=remote_code).to(device).eval()
        self.path = ident
        self.size = sum(p.numel() * p.element_size() for p in self.model.parameters())

    def __call__(self, q: str, texts: list[str]) -> list[float]:
        torch = self.torch
        enc = self.tok([q] * len(texts), texts, truncation=True, max_length=CE_MAX_TOKENS, padding=True,
                       return_tensors="pt").to(self.device)
        with torch.no_grad():
            out = self.model(**enc).logits.reshape(len(texts), -1)[:, 0].float()
        if self.device == "cuda":
            torch.cuda.synchronize()
        return out.cpu().tolist()


def load_scorer(spec: str, threads: int, device: str):
    """(scorer(query, [text]) -> [float], {path, size_bytes}) for a SPEC other than replay."""
    kind, _, rest = spec.partition(":")
    if kind == "onnx":
        d, _, f = rest.partition(":")
        s = OnnxScorer(d, f or None, threads, device)
        return s, {"path": s.path, "size_bytes": s.size}
    if kind in ("hf", "hf+code"):
        s = HFScorer(rest, threads, device, remote_code=kind == "hf+code")
        return s, {"path": s.path, "size_bytes": s.size}
    if kind == "py":
        spec_ = importlib.util.spec_from_file_location("sq_bakeoff_scorer", rest)
        m = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(m)
        return m.make_scorer(threads, device), {"path": rest, "size_bytes": int(getattr(m, "SIZE_BYTES", 0))}
    raise SystemExit(f"model spec {spec!r}: onnx:DIR[:FILE], hf:DIR_OR_ID, py:FILE or replay")


def replay_scores(d: dict, cand: list[dict]) -> list[float] | None:
    """The dump's own `ce` score of each candidate (rounded to two places), or None when one is missing."""
    have = {r["url"]: r["ce"] for r in d["ce"].get("rows", []) if "ce" in r}
    if any(r["url"] not in have for r in cand):
        return None
    return [float(have[r["url"]]) for r in cand]


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def score_set(dump: Path, name: str, scorer, form: str, limit: int = 0) -> tuple[list[dict], dict]:
    """One record per dev query the engine reranked: {q, urls, sc, ms, full}. scorer=None replays the dump."""
    answers = read_dump(dump, name)
    recs, stat = [], {"queries": 0, "scored": 0, "not_reranked": 0, "under_two": 0, "cut_by_dump": 0, "rows": 0,
                      "rows_full_text": 0, "no_dump": 0}
    for q in dev_queries(dump, name)[:limit or None]:
        stat["queries"] += 1
        d = answers.get(q)
        if d is None:
            stat["no_dump"] += 1
            continue
        inp = ce_input(d)
        if inp is None:
            stat["not_reranked"] += 1
            continue
        _top, cand, _tail, complete = split_rows(*inp)
        if len(cand) < 2:
            stat["under_two"] += 1
            continue
        if not complete:
            stat["cut_by_dump"] += 1
            continue
        texts = [row_text(r, d, form) for r in cand]
        t0 = time.perf_counter()
        sc = replay_scores(d, cand) if scorer is None else [float(x) for x in scorer(model_query(d), [t for t, _ in texts])]
        ms = (time.perf_counter() - t0) * 1000
        if sc is None:
            stat["cut_by_dump"] += 1
            continue
        stat["scored"] += 1
        stat["rows"] += len(cand)
        stat["rows_full_text"] += sum(full for _, full in texts)
        recs.append({"q": q, "urls": [r["url"] for r in cand], "sc": sc, "ms": round(ms, 3)})
    return recs, stat


def read_scores(out: Path, model: str, name: str) -> dict:
    path = out / model / f"{name}.scores.jsonl"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {o["q"]: o for o in map(json.loads, filter(str.strip, f))}


def model_list(d: dict, rec: dict | None) -> list[dict]:
    """The list as this model's reranking leaves it. Without a record (not reranked at serving, under two
    candidates, cut by the dump) the list is what the engine's own `ce` step, else the step before it, holds."""
    inp = ce_input(d)
    if inp is None:
        step = d.get("ranker") or d.get("merged") or {}
        return step.get("rows", [])
    if rec is None:
        return d["ce"].get("rows", [])
    top, cand, tail, _ = split_rows(*inp)
    if [r["url"] for r in cand] != rec["urls"]:
        raise SystemExit(f"scores for {d['q']!r} are for other candidates: scored from another dump?")
    by = dict(zip(rec["urls"], rec["sc"]))
    return rerank(d["q"], inp[0], lambda _q, rows: [by[r["url"]] for r in rows])


# ---------------------------------------------------------------- the selection split
def held_out(r: dict) -> bool:
    """Is this dev row in the selection third: never trained on, the rows candidates are chosen on. Dealt by the
    row's entity, so a name, its typo and its account query fall on the same side; by the query's key without one."""
    group = r.get("entity") or C.norm_key(r["query"])
    return zlib.crc32(f"{HOLDOUT_SEED}\x00{group}".encode()) % HOLDOUT_PARTS == 0


def in_rows(r: dict, which: str) -> bool:
    return which == "all" or held_out(r) == (which == "select")


# ---------------------------------------------------------------- the reproduce check
def reproduce(d: dict, rec: dict, tol: float) -> dict:
    """Does this record give the dump's own `ce` step for the query? structure: the same pinned rows on top, the
    same candidates, the same tail. scores: each candidate's score within tol of the dumped one. order: the
    dumped order is highest-first under this record's scores, within tol (the dump rounds, so two rows 0.004
    apart can be dumped in either order)."""
    top, cand, tail, _ = split_rows(*ce_input(d))
    got = d["ce"].get("rows", [])
    urls = [r["url"] for r in got]
    k = len(top) + len(cand)
    by = dict(zip(rec["urls"], rec["sc"]))
    structure = (urls[:len(top)] == [r["url"] for r in top] and sorted(urls[len(top):k]) == sorted(rec["urls"])
                 and urls[k:] == [r["url"] for r in tail][:max(0, len(urls) - k)])
    if not structure:
        return {"structure": False, "scores": False, "order": False, "diff": None}
    seq = [by[u] for u in urls[len(top):k]]
    dumped = [r.get("ce") for r in got[len(top):k]]
    if any(x is None for x in dumped):
        return {"structure": False, "scores": False, "order": False, "diff": None}
    diff = max(abs(a - b) for a, b in zip(seq, dumped))
    return {"structure": True, "scores": diff <= tol + ROUND_TOL,
            "order": all(a >= b - tol for a, b in zip(seq, seq[1:])), "diff": diff}


# ---------------------------------------------------------------- scoring against the sets
def rank_of(eq, r: dict, rows: list[dict]) -> int:
    return next((i + 1 for i, x in enumerate(rows) if C.row_hit(eq, r, x)), 99)


def paired(a: list[float], b: list[float], boot, passes) -> dict | None:
    """b - a over paired rows: the means, the difference, its 95% paired-bootstrap interval, the verdict."""
    if not a or len(a) != len(b):
        return None
    d = [y - x for x, y in zip(a, b)]
    lo, hi = boot(d) if any(d) else (0.0, 0.0)       # no row differs: the resampled mean is 0 every time
    delta = sum(d) / len(d)
    return {"n": len(d), "a": sum(a) / len(a), "b": sum(b) / len(b), "delta": delta, "lo": lo, "hi": hi,
            "pass": passes(lo, hi, delta)}


def fmt_ci(c: dict | None) -> str:
    return "" if c is None else f"{100 * c['delta']:+.1f} [{100 * c['lo']:+.1f}, {100 * c['hi']:+.1f}]"


# ---------------------------------------------------------------- commands
def cmd_subset(a) -> None:
    dump, out = Path(a.dump), Path(a.out)
    if is_subset(dump):
        raise SystemExit(f"{dump} is already a subset")
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for s in dump_sets(dump, a.sets):
        answers = read_dump(dump, s)
        n = 0
        want = {r["query"] for r in C.load_set(s, "dev") if in_rows(r, a.rows)}
        with open(out / f"{s}.jsonl", "w", encoding="utf-8") as f:
            for q in dev_queries(dump, s):
                if q not in want:
                    continue
                d = answers.get(q)
                if d is not None and ce_input(d) is not None:
                    f.write(json.dumps({"q": q, **{k: d[k] for k in KEEP_STEPS if k in d}}) + "\n")
                    n += 1
        log(f"  {s}: {n} dev queries")
        total += n
    (out / SUBSET_MARK).write_text(json.dumps({"split": "dev", "rows": a.rows, "from": str(dump), "queries": total}) + "\n")
    print(out)


def cmd_score(a) -> None:
    dump, out = Path(a.dump), Path(a.out)
    name, _, spec = a.model.partition("=")
    if not spec:
        raise SystemExit("--model NAME=SPEC")
    if spec == "replay":
        scorer, info = None, {"path": "the dump's own ce scores", "size_bytes": 0}
    else:
        scorer, info = load_scorer(spec, a.threads, a.device)
    (out / name).mkdir(parents=True, exist_ok=True)
    stats, lat, text_dump = {}, [], False
    for s in dump_sets(dump, a.sets):
        recs, st = score_set(dump, s, scorer, a.form, a.limit)
        with open(out / name / f"{s}.scores.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in recs)
        stats[s] = st
        lat += [r["ms"] for r in recs if len(r["urls"]) == CE_TOP]
        text_dump |= st["rows_full_text"] > 0
        log(f"  {name} {s}: {st['scored']} of {st['queries']} dev queries scored")
    lat = lat[10:]                                   # the first calls warm the model up
    rows = sum(st["rows"] for st in stats.values())
    meta = dict(info, model=name, spec=spec, form=a.form, device=a.device, threads=a.threads,
                max_tokens=CE_MAX_TOKENS, sets=stats, rows=rows,
                rows_full_text=sum(st["rows_full_text"] for st in stats.values()),
                latency_ms={"calls": len(lat), "pairs": CE_TOP, "p50": pct(lat, 0.5), "p90": pct(lat, 0.9)},
                when=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    (out / name / "model.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps({k: meta[k] for k in ("model", "size_bytes", "rows", "rows_full_text", "latency_ms")}))


def cmd_check(a) -> None:
    dump, out = Path(a.dump), Path(a.out)
    n = st = sc = od = 0
    worst, text_rows, rows = 0.0, 0, 0
    for s in dump_sets(dump, a.sets):
        answers, scores = read_dump(dump, s), read_scores(out, a.model, s)
        for q, rec in scores.items():
            r = reproduce(answers[q], rec, a.tol)
            n += 1
            st += r["structure"]
            sc += r["scores"]
            od += r["order"]
            worst = max(worst, r["diff"] or 0.0)
    meta_p = out / a.model / "model.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text())
        text_rows, rows = meta.get("rows_full_text", 0), meta.get("rows", 0)
    res = {"model": a.model, "queries": n, "structure": st, "scores_within_tol": sc, "order_within_tol": od,
           "max_abs_diff": round(worst, 4), "tol": a.tol, "rows_full_text": text_rows, "rows": rows,
           "reproduced": bool(n) and st == n and sc == n and od == n}
    (out / a.model / "check.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res))
    if not n:
        raise SystemExit(f"no scores for {a.model} under {out}")
    if st != n:
        raise SystemExit(f"NOT REPRODUCED: {n - st} of {n} queries differ in which rows are pinned / scored / left: "
                         "the mirror of ce_rerank is wrong, or the scores are from another dump")
    if (sc != n or od != n) and not a.allow_text_gap:
        why = ("this is a cut dump (no content, titles cut at 90): the model read other text than at serving"
               if text_rows < rows else "the model, its text form or its tokenizer settings differ from serving")
        raise SystemExit(f"NOT REPRODUCED: scores within tolerance for {sc} of {n} queries, order for {od}: {why}")


def cmd_table(a) -> None:
    if is_subset(Path(a.dump)):
        raise SystemExit("table needs the full dump and the eval sets (a subset has no set rows)")
    dump, out = Path(a.dump), Path(a.out)
    eq = C.load_tool("eval_queries", C.TOOLS / "eval-queries.py")
    cev = C.load_tool("compare_evals", C.TOOLS / "compare-evals.py")
    models = [m.strip() for m in a.models.split(",") if m.strip()] or sorted(
        p.name for p in out.iterdir() if (p / "model.json").exists())
    if a.shipped not in models:
        raise SystemExit(f"--shipped {a.shipped}: no scores under {out}")
    arms = ["final", "dumped"] + models
    hit: dict = {arm: {} for arm in arms}            # arm -> set -> [(hit1, hit5)]
    mov: dict = {arm: [] for arm in arms}            # arm -> [(hit1, hit5, first url)] on the rows whose first result the reranker picks
    reach, cut, full_rows, all_rows = {}, False, 0, 0
    sets = dump_sets(dump, a.sets)
    for s in sets:
        answers = read_dump(dump, s)
        scores = {m: read_scores(out, m, s) for m in models}
        rows = [r for r in C.load_set(s, "dev") if in_rows(r, a.rows)]
        r1 = r5 = 0
        for arm in arms:
            hit[arm][s] = []
        for r in rows:
            d = answers.get(r["query"])
            if d is None:
                continue
            cut |= not has_text(d)
            inp = ce_input(d)
            if inp is not None:
                top, cand, _t, _c = split_rows(*inp)
                r1 += len(top) == 0 and len(cand) >= 2
                r5 += len(top) < 5 and len(cand) >= 2
            lists = {"final": d.get("final", {}).get("rows", []), "dumped": model_list(d, None)}
            for m in models:
                lists[m] = model_list(d, scores[m].get(r["query"]))
            movable = inp is not None and len(top) == 0 and len(cand) >= 2
            answerable = movable and any(C.row_hit(eq, r, x) for x in cand)
            for arm in arms:
                k = rank_of(eq, r, lists[arm])
                hit[arm][s].append((float(k == 1), float(k <= 5)))
                if movable:
                    mov[arm].append((float(k == 1), float(k <= 5), lists[arm][0]["url"] if lists[arm] else "", answerable))
        reach[s] = (len(hit["final"][s]), r1, r5)
    for m in models:
        meta = json.loads((out / m / "model.json").read_text())
        full_rows += meta.get("rows_full_text", 0)
        all_rows += meta.get("rows", 0)
        cut |= meta.get("rows_full_text", 0) < meta.get("rows", 0)

    def col(arm, s, i):
        return [h[i] for x in ([s] if s else sets) for h in hit[arm][x]]

    which = {"all": "dev halves", "select": "dev halves, the held-out selection third",
             "train": "dev halves, the two thirds labels may be drawn from"}[a.rows]
    md = [f"# Reranker bake-off — {which}, accept keys ({C.KEYS}), dump {dump.name}", ""]
    if cut:
        md += [CUT_BANNER, ""]
    md += [f"Scored on the list as the reranker leaves it (the `ce` step). `final` is the shipped engine's answer after "
           f"its later passes; `dumped` is the shipped model as it ran at serving. Change = arm minus `{a.shipped}` "
           "(the shipped model re-scored here on the same text), points, 95% paired bootstrap.", ""]
    tsv = ["arm\tset\tn\treach1\treach5\thit1\thit5\td_hit1\tlo1\thi1\td_hit5\tlo5\thi5\tverdict"]
    md += ["| arm | set | n | reach@1 | reach@5 | hit@1 | hit@5 | Δ hit@1 [95% CI] | Δ hit@5 [95% CI] | verdict |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        for s in [""] + sets:
            h1, h5 = col(arm, s, 0), col(arm, s, 1)
            if not h1:
                continue
            n, r1, r5 = (sum(reach[x][i] for x in sets) for i in range(3)) if not s else reach[s]
            c1 = c5 = None
            if arm != a.shipped:
                c1 = paired(col(a.shipped, s, 0), h1, cev.boot, cev.passes)
                c5 = paired(col(a.shipped, s, 1), h5, cev.boot, cev.passes)
            verdict = "" if c1 is None else ("pass" if c1["pass"] and c5["pass"] else "FAIL")
            md.append(f"| {arm} | {s or 'ALL'} | {n} | {r1} | {r5} | {100 * sum(h1) / n:.1f} | {100 * sum(h5) / n:.1f} | "
                      f"{fmt_ci(c1)} | {fmt_ci(c5)} | {verdict} |")
            tsv.append("\t".join(map(str, (arm, s or "ALL", n, r1, r5, f"{sum(h1) / n:.4f}", f"{sum(h5) / n:.4f}",
                                           *(("", "", "") if c1 is None else (f"{c1['delta']:.4f}", f"{c1['lo']:.4f}", f"{c1['hi']:.4f}")),
                                           *(("", "", "") if c5 is None else (f"{c5['delta']:.4f}", f"{c5['lo']:.4f}", f"{c5['hi']:.4f}")),
                                           verdict))))
    n_ans = sum(x[3] for x in mov[a.shipped])
    md += ["", f"Rows whose first result the reranker picks (no pinned row): {len(mov[a.shipped])}; of them {n_ans} have a right answer "
           f"among the rows it scores (`answerable`). `first changed` = rows where the arm's first result is not `{a.shipped}`'s.", "",
           "| arm | rows | n | hit@1 | hit@5 | Δ hit@1 [95% CI] | Δ hit@5 [95% CI] | first changed |", "|---|---|---|---|---|---|---|---|"]
    mtsv = ["arm\trows\tn\thit1\thit5\td_hit1\tlo1\thi1\td_hit5\tlo5\thi5\tfirst_changed"]
    for arm, which in [(x, w) for w in ("movable", "answerable") for x in arms]:
        base = [x for x in mov[a.shipped] if which == "movable" or x[3]]
        m = [x for x in mov[arm] if which == "movable" or x[3]]
        if not m:
            continue
        c1 = c5 = None
        if arm != a.shipped:
            c1 = paired([x[0] for x in base], [x[0] for x in m], cev.boot, cev.passes)
            c5 = paired([x[1] for x in base], [x[1] for x in m], cev.boot, cev.passes)
        changed = sum(x[2] != y[2] for x, y in zip(base, m))
        md.append(f"| {arm} | {which} | {len(m)} | {100 * sum(x[0] for x in m) / len(m):.1f} | {100 * sum(x[1] for x in m) / len(m):.1f} | "
                  f"{fmt_ci(c1)} | {fmt_ci(c5)} | {'' if arm == a.shipped else changed} |")
        mtsv.append("\t".join(map(str, (arm, which, len(m), f"{sum(x[0] for x in m) / len(m):.4f}", f"{sum(x[1] for x in m) / len(m):.4f}",
                                         *(("", "", "") if c1 is None else (f"{c1['delta']:.4f}", f"{c1['lo']:.4f}", f"{c1['hi']:.4f}")),
                                         *(("", "", "") if c5 is None else (f"{c5['delta']:.4f}", f"{c5['lo']:.4f}", f"{c5['hi']:.4f}")),
                                         "" if arm == a.shipped else changed))))
    md += ["", "| model | spec | size on disk | text form | rows with full text | device | threads | p50 ms (8 pairs) | p90 ms | reproduces the dump |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        meta = json.loads((out / m / "model.json").read_text())
        chk = out / m / "check.json"
        rep = json.loads(chk.read_text()) if chk.exists() else None
        lat = meta["latency_ms"]
        md.append(f"| {m} | `{meta['spec']}` | {meta['size_bytes'] / 1e6:.1f} MB | {meta['form']} | "
                  f"{meta['rows_full_text']} of {meta['rows']} | {meta['device']} | {meta['threads']} | "
                  f"{'' if lat['p50'] is None else format(lat['p50'], '.1f')} | "
                  f"{'' if lat['p90'] is None else format(lat['p90'], '.1f')} | "
                  f"{'' if rep is None else ('yes' if rep['reproduced'] else 'NO: scores ' + str(rep['scores_within_tol']) + ' / order ' + str(rep['order_within_tol']) + ' of ' + str(rep['queries']) + ', structure ' + str(rep['structure']))} |")
    md.append("")
    stem = "table" if a.rows == "all" else f"table.{a.rows}"
    (out / f"{stem}.md").write_text("\n".join(md))
    (out / f"{stem}.tsv").write_text("\n".join(tsv) + "\n")
    (out / f"{stem}.movable.tsv").write_text("\n".join(mtsv) + "\n")
    print("\n".join(md))


RIGHT = 0.7                                  # search-answer-eval's "the result is right" threshold (judged-info's report)


def template_of(r: dict) -> str:
    return next((f.split(":", 1)[1] for f in r.get("stratum", "").split(",") if f.startswith("template:")), "")


def load_verdicts(path: Path) -> dict:
    """{(query key, url): P(right)} from a judged set's verdict file (judged-info-judge.py)."""
    out = {}
    with open(path, encoding="utf-8") as f:
        cols = None
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            p = line.rstrip("\n").split("\t")
            if cols is None:
                cols = p
                continue
            r = dict(zip(cols, p))
            out[(r["key"], r["url"])] = float(r["score"])
    return out


def judged_stats(rows: list[dict], answers: dict, scores: dict, verdicts: dict, shipped: str, models: list[str],
                 top: int = 5) -> tuple[dict, dict]:
    """Per model and template, against the shipped model's first result: how many first results change, and of the new
    ones how many have a verdict, their mean P(right), the share >= RIGHT, the shipped first's mean P on the same
    queries; plus every unjudged (query, url) in a model's first `top` results. Pure, for tests."""
    stats: dict = {}
    unjudged: dict = {}
    lists = {}
    for r in rows:
        d = answers.get(r["query"])
        if d is None:
            continue
        lists[r["query"]] = {m: model_list(d, scores[m].get(r["query"])) for m in models}
    for m in models:
        for r in rows:
            if r["query"] not in lists:
                continue
            key, tpl = C.norm_query(r["query"]), template_of(r)
            new, old = lists[r["query"]][m], lists[r["query"]][shipped]
            for t in (tpl, "ALL"):
                st = stats.setdefault((m, t), {"n": 0, "changed": 0, "judged": 0, "p": [], "p_shipped": [], "unjudged": 0,
                                               "first_judged": 0, "p_first": [], "pair_new": [], "pair_old": []})
                st["n"] += 1
                pf = verdicts.get((key, new[0]["url"])) if new else None
                if pf is not None:
                    st["first_judged"] += 1
                    st["p_first"].append(pf)
                po0 = verdicts.get((key, old[0]["url"])) if old else None
                if pf is not None and po0 is not None:      # both firsts judged: the paired measure
                    st["pair_new"].append(pf)
                    st["pair_old"].append(po0)
                if new and old and new[0]["url"] != old[0]["url"]:
                    st["changed"] += 1
                    pn, po = verdicts.get((key, new[0]["url"])), verdicts.get((key, old[0]["url"]))
                    if pn is None:
                        st["unjudged"] += 1
                    else:
                        st["judged"] += 1
                        st["p"].append(pn)
                        if po is not None:
                            st["p_shipped"].append(po)
            for rank, x in enumerate(new[:top], 1):
                if (key, x["url"]) not in verdicts:
                    u = unjudged.setdefault((r["query"], x["url"]), {"query": r["query"], "url": x["url"],
                                                                   "title": x.get("title_full") or x.get("title") or "",
                                                                   "kind": x.get("source", ""), "template": tpl,
                                                                   "best_rank": rank, "models": set()})
                    u["best_rank"] = min(u["best_rank"], rank)
                    u["models"].add(m)
    return stats, unjudged


def cmd_judged(a) -> None:
    dump, out = Path(a.dump), Path(a.out)
    models = [m.strip() for m in a.models.split(",") if m.strip()] or sorted(
        p.name for p in out.iterdir() if (p / "model.json").exists())
    if a.shipped not in models:
        raise SystemExit(f"--shipped {a.shipped}: no scores under {out}")
    rows = C.load_set(a.set, "dev")
    answers = read_dump(dump, a.set)
    scores = {m: read_scores(out, m, a.set) for m in models}
    verdicts = load_verdicts(Path(a.verdicts))
    stats, unjudged = judged_stats(rows, answers, scores, verdicts, a.shipped, models, a.top)
    tpls = ["ALL"] + sorted({t for _m, t in stats if t != "ALL"})

    def mean(xs):
        return "" if not xs else f"{sum(xs) / len(xs):.3f}"

    md = [f"# Rerankers on the judged set `{a.set}` — DEV half, against `{a.shipped}`", "",
          "**Partial.** Verdicts exist only for the results the engine showed in its top five when the set was judged. A model "
          "whose new first result has no verdict is UNDER-MEASURED there, not worse: `unjudged` counts those queries.", "",
          "| model | template | n | first changed | new first judged | its mean P | its share ≥ 0.7 | shipped's mean P, same queries | unjudged | P(top-1), judged firsts | firsts judged |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    tsv = ["model\ttemplate\tn\tchanged\tjudged\tmean_p\tshare_right\tshipped_mean_p\tunjudged\tp_top1\tfirst_judged"]
    cev = C.load_tool("compare_evals", C.TOOLS / "compare-evals.py")
    pair_md = ["", "Paired P(top-1) against the shipped model on the queries where BOTH first results have a verdict (an unjudged "
               "first leaves its query out, so a model that changes many firsts to unjudged results is measured on fewer queries):", "",
               "| model | template | queries | shipped P(top-1) | model P(top-1) | Δ [95% CI] |", "|---|---|---|---|---|---|"]
    pair_tsv = ["model\ttemplate\tn\tshipped\tmodel\tdelta\tlo\thi"]
    for m in models:
        for t in tpls:
            st = stats.get((m, t))
            if not st:
                continue
            share = "" if not st["p"] else f"{sum(x >= RIGHT for x in st['p']) / len(st['p']):.3f}"
            cells = (m, t, st["n"], "" if m == a.shipped else st["changed"], "" if m == a.shipped else st["judged"],
                     "" if m == a.shipped else mean(st["p"]), "" if m == a.shipped else share,
                     "" if m == a.shipped else mean(st["p_shipped"]), "" if m == a.shipped else st["unjudged"],
                     mean(st["p_first"]), st["first_judged"])
            md.append("| " + " | ".join(map(str, cells)) + " |")
            tsv.append("\t".join(map(str, cells)))
            if m != a.shipped and st["pair_new"]:
                c = paired(st["pair_old"], st["pair_new"], cev.boot, cev.passes)
                pair_md.append(f"| {m} | {t} | {c['n']} | {c['a']:.3f} | {c['b']:.3f} | {fmt_ci(c).replace('+', '+')} |")
                pair_tsv.append("\t".join(map(str, (m, t, c["n"], f"{c['a']:.4f}", f"{c['b']:.4f}", f"{c['delta']:.4f}", f"{c['lo']:.4f}", f"{c['hi']:.4f}"))))
    firsts = sum(1 for u in unjudged.values() if u["best_rank"] == 1)
    md += pair_md
    (out / "judged.paired.tsv").write_text("\n".join(pair_tsv) + "\n")
    md += ["", f"Unjudged (query, url) pairs across these models: {firsts} first results, {len(unjudged)} in the top {a.top}."]
    (out / "judged.md").write_text("\n".join(md) + "\n")
    (out / "judged.tsv").write_text("\n".join(tsv) + "\n")
    if a.unjudged:
        with open(a.unjudged, "w", encoding="utf-8") as f:
            f.write("query\turl\ttitle\tkind\ttemplate\tbest_rank\tmodels\n")
            for u in sorted(unjudged.values(), key=lambda u: (u["best_rank"], u["query"], u["url"])):
                f.write("\t".join(str(u[k]).replace("\t", " ") for k in ("query", "url", "title", "kind", "template", "best_rank"))
                        + "\t" + ",".join(sorted(u["models"])) + "\n")
    print("\n".join(md))


def cmd_holdout(a) -> None:
    """The held-out dev queries, one per line with their key and set: what a trainer must leave out of its
    labels (match on the key). Reads the set files only."""
    n = total = 0
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(f"# sq-rerank-bakeoff.py holdout: seed={HOLDOUT_SEED} parts={HOLDOUT_PARTS} — dev rows never trained on\n")
        f.write("key\tquery\tset\n")
        for s in C.all_sets():
            for r in C.load_set(s, "dev"):
                total += 1
                if held_out(r):
                    n += 1
                    f.write(f"{C.norm_key(r['query'])}\t{r['query']}\t{s}\n")
    log(f"{n} of {total} dev rows held out -> {a.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("judged")
    p.set_defaults(fn=cmd_judged)
    p.add_argument("--dump", required=True)
    p.add_argument("--out", required=True, help="the scores directory (one sub-directory per model)")
    p.add_argument("--shipped", required=True)
    p.add_argument("--verdicts", required=True, help="the judged set's verdict file (judged-info.verdicts.tsv)")
    p.add_argument("--set", default="judged-info")
    p.add_argument("--models", default="")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--unjudged", help="write the unjudged (query, url, title) pairs here")
    p = sub.add_parser("holdout")
    p.set_defaults(fn=cmd_holdout)
    p.add_argument("--out", required=True, help="the file to write")
    for name, fn in (("subset", cmd_subset), ("score", cmd_score), ("check", cmd_check), ("table", cmd_table)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--dump", required=True, help="a candidate dump directory (sq-candidates.py), or a subset of one")
        p.add_argument("--out", required=True, help="where scores and the table go (subset: the subset directory)")
        p.add_argument("--sets", default="", help="comma-separated subset of the dump's sets")
        if name == "subset":
            p.add_argument("--rows", default="all", choices=("all", "select", "train"),
                           help="select = only the held-out third's queries")
        if name == "score":
            p.add_argument("--model", required=True, help="NAME=SPEC")
            p.add_argument("--form", default="domain", choices=("domain", "title"),
                           help="what the model reads: domain = the serving form, title = without the host")
            p.add_argument("--threads", type=int, default=4)
            p.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
            p.add_argument("--limit", type=int, default=0, help="first N dev queries per set (smoke runs)")
        if name == "check":
            p.add_argument("--model", required=True, help="a scored model's NAME (the shipped one)")
            p.add_argument("--tol", type=float, default=0.02)
            p.add_argument("--allow-text-gap", action="store_true",
                           help="exit 0 when only the scores differ (a cut dump): the result still says not reproduced")
        if name == "table":
            p.add_argument("--shipped", required=True, help="the scored NAME every other arm is paired against")
            p.add_argument("--models", default="", help="scored NAMEs to show (default: all under --out)")
            p.add_argument("--rows", default="all", choices=("all", "select", "train"),
                           help="select = the held-out third (a model trained on labels v2 is judged on these alone)")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
