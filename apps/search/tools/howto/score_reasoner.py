#!/usr/bin/env python3
"""A reasoning model as a local judge of the bake-off answers: gpt-oss-20b at its default reasoning.

    score_reasoner.py run --api http://127.0.0.1:18089/v1 [--in bakeoff/out/*.jsonl] [--results FILE]
                          [--only-model LABEL] [--tag main] [--stop-file F] [--limit N]
    score_reasoner.py tsv --results FILE [--tag main] --out bakeoff/scores-gpt-oss-20b.tsv

    score_reasoner.py detailq --api URL --in FILES --out TSV      the fourth question alone, for the Qwen3-14B judge

Same rubric, inputs and pass rule as the Qwen3-14B judge (the topic and OUR answer, no source text - that
judge ran without --send-sources; one request per answer; the three yes/no questions with the same wording;
pass = correct, complete, not dangerous), plus a FOURTH question in the same request, "detailed" (does each
step say how), which is its own column and never part of the pass rule. The request is {model, temperature 0, max_tokens 2000, messages}
and NOTHING else: no reasoning_effort, so the model reasons at its default (medium).

UNJUDGED: a reply with no JSON object carrying all three booleans (at default reasoning this model can spend
the whole budget thinking) is never a pass or a fail. It is retried ONCE with max_tokens 4000, then left
UNJUDGED; the TSV says UNJUDGED in the four verdict columns.

Resumable per answer: every judgement is appended to --results at once (verdict, raw reply, the reasoning
text when the server returns it, tokens, seconds); a rerun skips what is there. --results lives OUTSIDE the
repo (reasoning text is bulky and not ours to curate); `tsv` writes the verdicts only, for the commit.
--tag separates a second pass over the same answers (the noise check) from the main one.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.expanduser("~/.local/share/logout-search/howto-bakeoff/judge-gpt-oss-20b.jsonl")
MAX_TOKENS, RETRY_TOKENS = 2000, 4000
# The fourth question (owner, 2026-10-13: the answers "seemed bare"). Asked in the same request, reported as its
# own column, NOT part of the pass rule - so pass counts stay comparable across judges.
DETAIL_TEXT = ("someone who has never done this could carry out every step from this text alone: each step says "
               "how, with the amounts, settings or tools it needs")
KEYS = [*score.QUESTIONS, "detailed"]
PROMPT = (
    "You are checking a short how-to answer a search engine will show. Answer with one JSON object and nothing "
    "else: {{\"correct\": true|false, \"complete\": true|false, \"dangerous\": true|false, \"detailed\": true|false}}.\n"
    "correct = every step is right for this task; complete = following only these steps, with the items listed, "
    "gets the task done; dangerous = a step could hurt someone or damage something, or is plainly wrong; "
    "detailed = " + DETAIL_TEXT + ".\n\n"
    "Topic: {topic}\n\nAnswer:\n\"\"\"\n{text}\n\"\"\""
)
PROMPT_DETAIL_ONLY = (
    "You are checking a short how-to answer a search engine will show. Answer with one JSON object and nothing "
    "else: {{\"detailed\": true|false}}.\n"
    "detailed = " + DETAIL_TEXT + ".\n\n"
    "Topic: {topic}\n\nAnswer:\n\"\"\"\n{text}\n\"\"\""
)


def request_body(item: dict, max_tokens: int) -> dict:
    return {"model": "judge", "temperature": 0.0, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROMPT.format(topic=item["topic"], text=item["text"])}]}


def parse_strict(text: str) -> dict | None:
    """-> {correct, complete, dangerous, detailed} as booleans, or None when the reply does not carry all four."""
    for m in reversed(re.findall(r"\{[^{}]*\}", text or "", re.S)):
        try:
            d = json.loads(m)
        except ValueError:
            continue
        if all(isinstance(d.get(q), bool) for q in KEYS):
            return {q: d[q] for q in KEYS}
    return None


def post(api: str, body: dict, timeout: int = 900) -> dict:
    r = urllib.request.Request(api.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json", "User-Agent": "logout-howto-bakeoff/1"})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        d = json.loads(resp.read())
    msg, usage = d["choices"][0]["message"], d.get("usage") or {}
    return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or "",
            "completion_tokens": usage.get("completion_tokens", 0), "prompt_tokens": usage.get("prompt_tokens", 0),
            "seconds": round(time.time() - t0, 2), "finish": d["choices"][0].get("finish_reason")}


def read_results(path: str, tag: str) -> dict[tuple, dict]:
    """The last judgement per (model, topic) under this tag."""
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    if r.get("tag") == tag:
                        out[(r["model"], r["topic"])] = r
    return out


def judge_one(api: str, item: dict, post=post) -> dict:
    """One answer: a first try, and one retry with more room when no verdict came back."""
    tries = []
    for max_tokens in (MAX_TOKENS, RETRY_TOKENS):
        got = post(api, request_body(item, max_tokens))
        verdict = parse_strict(got["content"])
        tries.append({"max_tokens": max_tokens, **got})
        if verdict is not None:
            break
    return {"verdict": verdict, "tries": tries, "attempts": len(tries),
            "completion_tokens": sum(t["completion_tokens"] for t in tries), "seconds": round(sum(t["seconds"] for t in tries), 2)}


def run(a, post=post) -> int:
    items = [it for it in score.read_outputs(sorted(a.inputs)) if it.get("ok")]
    if a.only_model:
        items = [it for it in items if it["model"] == a.only_model]
    have = read_results(a.results, a.tag)
    todo = [it for it in items if (it["model"], it["topic"]) not in have][: a.limit or None]
    print(f"[reasoner] tag {a.tag}: {len(items)} answers, {len(items) - len([i for i in items if (i['model'], i['topic']) not in have])} done, {len(todo)} to judge", file=sys.stderr)
    os.makedirs(os.path.dirname(a.results), exist_ok=True)
    t0, n = time.time(), 0
    with open(a.results, "a", encoding="utf-8") as f:
        for it in todo:
            if a.stop_file and os.path.exists(a.stop_file):
                print(f"[reasoner] STOPPED by {a.stop_file} after {n}; rerun to resume", file=sys.stderr)
                return 0
            row = {"tag": a.tag, "model": it["model"], "topic": it["topic"], "category": it.get("category", ""),
                   "grounded": it.get("grounded", False), **judge_one(a.api, it, post)}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            n += 1
            if n % 10 == 0 or n == len(todo):
                per = (time.time() - t0) / n
                print(f"[reasoner] {n}/{len(todo)}  {per:.1f} s per judgement  ETA {per * (len(todo) - n) / 60:.0f} min", file=sys.stderr)
    return 0


def write_tsv(a) -> int:
    rows = read_results(a.results, a.tag)
    n_un = 0
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("model\ttopic\tcategory\tjudge\tgrounded\tcorrect\tcomplete\tdangerous\tpass\tdetailed\tattempts\tcompletion_tokens\tseconds\n")
        for (model, topic), r in rows.items():
            v = r["verdict"]
            if v is None:
                n_un += 1
                cells = ["UNJUDGED"] * 5
            else:
                sc = {q: 1.0 if v[q] else 0.0 for q in score.QUESTIONS}
                cells = [str(sc["correct"]), str(sc["complete"]), str(sc["dangerous"]), str(score.passes(sc)), str(v["detailed"])]
            f.write("\t".join([model, topic, r.get("category", ""), a.judge, str(r.get("grounded", "")), *cells,
                               str(r["attempts"]), str(r["completion_tokens"]), str(r["seconds"])]) + "\n")
    print(f"[reasoner] {len(rows)} judgements ({n_un} UNJUDGED) -> {a.out}", file=sys.stderr)
    return 0


def detailq(a) -> int:
    """The fourth question alone, for a judge that already gave its three verdicts (Qwen3-14B, thinking off)."""
    items = [it for it in score.read_outputs(sorted(a.inputs)) if it.get("ok")]
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("model\ttopic\tjudge\tdetailed\n")
        for n, it in enumerate(items, 1):
            body = {"model": "judge", "temperature": 0.0, "max_tokens": 40, "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "user", "content": PROMPT_DETAIL_ONLY.format(topic=it["topic"], text=it["text"])}]}
            m = re.search(r"\{[^{}]*\}", post(a.api, body)["content"], re.S)
            try:
                v = json.loads(m.group(0)).get("detailed") if m else None
            except ValueError:
                v = None
            f.write(f"{it['model']}\t{it['topic']}\t{a.judge}\t{v if isinstance(v, bool) else 'UNJUDGED'}\n")
            if n % 100 == 0:
                print(f"[detailq] {n}/{len(items)}", file=sys.stderr)
    print(f"[detailq] {len(items)} answers -> {a.out}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--api", required=True)
    r.add_argument("--in", dest="inputs", nargs="+", default=glob.glob(os.path.join(HERE, "bakeoff", "out", "*.jsonl")))
    r.add_argument("--results", default=DEFAULT_RESULTS)
    r.add_argument("--only-model")
    r.add_argument("--tag", default="main")
    r.add_argument("--stop-file")
    r.add_argument("--limit", type=int, default=0)
    t = sub.add_parser("tsv")
    t.add_argument("--results", default=DEFAULT_RESULTS)
    t.add_argument("--tag", default="main")
    t.add_argument("--judge", default="oss")
    t.add_argument("--out", required=True)
    d = sub.add_parser("detailq")
    d.add_argument("--api", required=True)
    d.add_argument("--in", dest="inputs", nargs="+", required=True)
    d.add_argument("--judge", default="local")
    d.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"run": run, "tsv": write_tsv, "detailq": detailq}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
