#!/usr/bin/env python3
"""The how-to model bake-off harness: one prompt, every candidate, same 50 topics. Stdlib only.

    bakeoff.py prompts [--topics topics-bakeoff.tsv] [--sources sources.jsonl]
        print every request that `run` would send (no model, no network)
    bakeoff.py run --model HF_ID --endpoint http://127.0.0.1:8000/v1 [--out DIR] [--topics F] [--sources F]
                   [--dry-run] [--no-think] [--max-tokens 400] [--parallel 4] [--label NAME] [--stop-file F]
                   [--reasoning-effort low|medium|high]
        POST {endpoint}/chat/completions (OpenAI-compatible: vLLM, llama.cpp server, the buildbox judge box)
        -> DIR/<label slug>.jsonl: {topic, category, grounded, source_ids, model, date, text, words, ok,
           completion_tokens, prompt_tokens, tokens_per_s, seconds}; appended per topic, a rerun resumes.
        --dry-run writes DIR/<model slug>.requests.jsonl instead and contacts nothing.

The prompt (PROMPT_SYSTEM / PROMPT_USER): numbered steps, the items needed, how long it takes, one
warning line where one is due, at most 180 words. GROUNDED when a source text is supplied: the model
is told to follow it and to cite it as [source]; the card then says "generated from Wikibooks".
UNGROUNDED (no source) is permitted only in cooking / crafts / household / software
(categories.UNGROUNDED_OK) and the output row says so (`grounded: false`). A topic that categories.py
does not allow is refused here too — the deny list is checked on every path, not only build-side.

Nothing in here is called by the box (rule 7): development tooling, workstation and buildbox only.
The candidate list and its licence rows are's; this file takes any --model string.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import categories  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOPICS = os.path.join(HERE, "topics-bakeoff.tsv")
MAX_WORDS = 180

PROMPT_SYSTEM = (
    "You write short, safe, practical how-to answers for a search engine card. Write for an adult who has "
    "never done this. Use plain words. Format, exactly:\n"
    "Needed: <comma-separated items, or 'nothing special'>\n"
    "Time: <a realistic estimate>\n"
    "1. <first step>\n2. <second step>\n... (3 to 8 numbered steps, one action each)\n"
    "Warning: <one line, only if a step can hurt someone or break something; otherwise omit the line>\n"
    f"At most {MAX_WORDS} words in total. No introduction, no closing remark, no headings other than those above."
)
# The "detailed" arm (owner, 2026-10-13: "the qwen14b answers seemed bare" - every model wrote ~100-120 of the 180
# words and no step said HOW). Same shape, same grounding and Source rules; each step must carry its how, one
# line names the most common mistake, the cap rises to 240. The first prompt and its outputs stay as they are.
MAX_WORDS_DETAIL = 240
PROMPT_SYSTEM_DETAIL = (
    "You write short, safe, practical how-to answers for a search engine card. Write for an adult who has "
    "never done this. Use plain words. Format, exactly:\n"
    "Needed: <comma-separated items, or 'nothing special'>\n"
    "Time: <a realistic estimate>\n"
    "1. <first step>\n2. <second step>\n... (3 to 8 numbered steps, one action each)\n"
    "Mistake: <one line: the most common mistake and how to avoid it>\n"
    "Warning: <one line, only if a step can hurt someone or break something; otherwise omit the line>\n"
    "Every step must say HOW to do it, not only what: the amount, the setting, the tool, and what 'done' looks "
    "like, wherever that matters for the step.\n"
    f"At most {MAX_WORDS_DETAIL} words in total. No introduction, no closing remark, no headings other than those above."
)
PROMPTS = {"base": (PROMPT_SYSTEM, MAX_WORDS), "detail": (PROMPT_SYSTEM_DETAIL, MAX_WORDS_DETAIL)}
PROMPT_USER_GROUNDED = (
    "Topic: {topic}\n\n"
    "Use ONLY the source text below. Do not add steps, quantities or claims that are not in it; if the source "
    "does not cover the topic, reply with the single line: INSUFFICIENT SOURCE. End with the line "
    "'Source: [source]'.\n\n"
    "Source ({source_id}, {licence}):\n\"\"\"\n{source_text}\n\"\"\""
)
PROMPT_USER_UNGROUNDED = (
    "Topic: {topic}\n\n"
    "No source text is supplied. Write only what is common, well-established practice for this everyday task; "
    "if you are not sure of a step, leave it out. Do not invent brand names, numbers or sources."
)
SOURCE_CHARS = 3500


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def read_topics(path: str = DEFAULT_TOPICS) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if not row or row[0].startswith("#") or len(row) < 3:
                continue
            out.append({"topic": row[0].strip(), "category": row[1].strip(), "source": row[2].strip()})
    return out


def read_sources(path: str | None) -> dict[str, dict]:
    """JSONL {id, url, title, text, licence} -> {id: row}. Missing file = no sources (everything ungrounded)."""
    if not path:
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[r["id"]] = r
    return out


def build_request(t: dict, sources: dict[str, dict], rules: categories.Rules, model: str,
                  max_tokens: int = 400, no_think: bool = False, prompt: str = "base") -> dict | None:
    """-> the chat request for one topic, with the row metadata under "_meta"; None when the topic is
    refused (denied / health / allowed-but-no-source-and-not-ungroundable), with the reason logged."""
    verdict, category, rule = rules.classify(t["topic"])
    src = sources.get(t["source"]) if t["source"] != "-" else None
    mode = rules.generation_mode(t["topic"], has_source=src is not None)
    if mode in ("deny", "health", "skip"):
        print(f"[bakeoff] skip {t['topic']!r}: {mode} ({verdict} {category} {rule!r})", file=sys.stderr)
        return None
    if src is not None:
        user = PROMPT_USER_GROUNDED.format(topic=t["topic"], source_id=src["id"], licence=src.get("licence", "CC BY-SA"),
                                           source_text=(src.get("text") or "")[:SOURCE_CHARS])
    else:
        user = PROMPT_USER_UNGROUNDED.format(topic=t["topic"])
    req = {"model": model, "messages": [{"role": "system", "content": PROMPTS[prompt][0]}, {"role": "user", "content": user}],
           "max_tokens": max_tokens, "temperature": 0.2, "top_p": 0.9}
    if no_think:
        req["chat_template_kwargs"] = {"enable_thinking": False}     # vLLM's switch for Qwen3's thinking mode
    req["_meta"] = {"topic": t["topic"], "category": category or t["category"], "grounded": src is not None,
                    "source_ids": [src["id"]] if src else [], "source_url": src.get("url") if src else None}
    return req


def check_output(text: str, grounded: bool, max_words: int = MAX_WORDS) -> tuple[bool, str]:
    """The format rules the prompt sets, checked mechanically before any judge sees the text."""
    words = len(re.findall(r"\S+", text))
    if "INSUFFICIENT SOURCE" in text:
        return False, "insufficient source"
    if words > max_words:
        return False, f"{words} words > {max_words}"
    steps = re.findall(r"^\s*(\d+)\.\s", text, re.M)
    if len(steps) < 3:
        return False, f"{len(steps)} numbered steps < 3"
    if not re.search(r"^Needed:", text, re.M) or not re.search(r"^Time:", text, re.M):
        return False, "missing Needed:/Time: line"
    if grounded and not re.search(r"^Source:\s*\S", text, re.M):
        # the prompt asks for the line 'Source: [source]'; a model that names the source there instead
        # ("Source: wikibooks:Cookbook:Roux, CC BY-SA 4.0") has cited it - measured on the first buildbox run,
        # where Qwen3-14B did so on 29 of 30 grounded topics. The card takes the source from source_ids.
        return False, "grounded answer has no Source: line"
    return True, ""


def post_chat(endpoint: str, req: dict, timeout: int = 600) -> dict:
    """-> {text, completion_tokens, prompt_tokens, tokens_per_s, seconds}. tokens_per_s is the server's own
    generation rate for this request (llama.cpp `timings.predicted_per_second`), else completion tokens over
    wall seconds. A reasoning model's thinking arrives in `reasoning_content` and is not part of the text."""
    body = json.dumps({k: v for k, v in req.items() if not k.startswith("_")}).encode()
    r = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=body,
                               headers={"Content-Type": "application/json",
                                        "User-Agent": "logout-howto-bakeoff/1",   # a hosted proxy refuses Python-urllib (403)
                                        "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "none")})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        d = json.loads(resp.read())
    secs = time.time() - t0
    msg = d["choices"][0]["message"]
    usage, timings = d.get("usage") or {}, d.get("timings") or {}
    ctok = usage.get("completion_tokens", 0)
    return {"text": clean(msg.get("content") or ""), "completion_tokens": ctok,
            "prompt_tokens": usage.get("prompt_tokens", 0), "seconds": round(secs, 2),
            "reasoning_chars": len(msg.get("reasoning_content") or ""),
            "tokens_per_s": round(timings.get("predicted_per_second") or (ctok / secs if secs else 0.0), 1)}


def clean(text: str) -> str:
    """Drop a thinking block a chat template left in the content, and surrounding blank lines."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def done_topics(path: str) -> dict[str, dict]:
    """Rows already written to an output file that carry text - the checkpoint a rerun resumes from."""
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("text"):
                        out[row["topic"]] = row
    return out


def run(model: str, endpoint: str, out_dir: str, topics_path: str, sources_path: str | None, dry_run: bool,
        no_think: bool, max_tokens: int, parallel: int, post=post_chat, label: str | None = None,
        stop_file: str | None = None, extra: dict | None = None, prompt: str = "base") -> str:
    """`label` names the output file and the `model` field of each row (the quantised build that actually
    ran, e.g. "Qwen3-14B Q4_K_M"); `model` is what the endpoint is asked for. RESUMABLE: every finished topic
    is appended and flushed at once, and a rerun skips the topics the file already holds - so a run stopped
    for the desktop's owner (touch the --stop-file, or kill it) loses at most the requests in flight."""
    rules = categories.Rules.load()
    sources = read_sources(sources_path)
    reqs = [r for r in (build_request(t, sources, rules, model, max_tokens, no_think, prompt) for t in read_topics(topics_path)) if r]
    for r in reqs:
        r.update(extra or {})
    os.makedirs(out_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    label = label or model
    if dry_run:
        path = os.path.join(out_dir, f"{slug(label)}.requests.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in reqs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[bakeoff] dry run: {len(reqs)} requests written to {path}; nothing was sent", file=sys.stderr)
        return path
    path = os.path.join(out_dir, f"{slug(label)}.jsonl")
    have = done_topics(path)
    todo = [r for r in reqs if r["_meta"]["topic"] not in have]
    if have:
        print(f"[bakeoff] resuming {path}: {len(have)} topics done, {len(todo)} to go", file=sys.stderr)

    def one(r):
        if stop_file and os.path.exists(stop_file):
            return None
        try:
            got = post(endpoint, r)
            err = ""
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            got, err = "", f"{type(e).__name__}: {e}"
        meta = got if isinstance(got, dict) else {"text": got}
        text = meta.get("text", "")
        ok, why = check_output(text, r["_meta"]["grounded"], PROMPTS[prompt][1]) if text else (False, err or "empty answer")
        return {**r["_meta"], "model": label, "prompt": prompt, "date": today, **meta, "words": len(text.split()), "ok": ok, "why": why}

    n_stopped = 0
    with open(path, "a", encoding="utf-8") as f, cf.ThreadPoolExecutor(parallel) as pool:
        for row in pool.map(one, todo):
            if row is None:
                n_stopped += 1
                continue
            if row["text"]:
                have[row["topic"]] = row
            else:
                print(f"[bakeoff] no text for {row['topic']!r}: {row['why']} (not checkpointed; rerun retries it)", file=sys.stderr)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    if n_stopped:
        print(f"[bakeoff] STOPPED by {stop_file}: {n_stopped} topics not sent; rerun the same command to resume", file=sys.stderr)
    n_ok = sum(1 for r in have.values() if r["ok"])
    print(f"[bakeoff] {label}: {len(have)}/{len(reqs)} topics written, {n_ok} pass the format check -> {path}", file=sys.stderr)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts")
    p.add_argument("--topics", default=DEFAULT_TOPICS)
    p.add_argument("--sources")
    r = sub.add_parser("run")
    r.add_argument("--model", required=True, help="the model id the endpoint serves (its HF id under vLLM)")
    r.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1")
    r.add_argument("--out", default=os.path.expanduser("~/.local/share/logout-search/howto-bakeoff"))
    r.add_argument("--topics", default=DEFAULT_TOPICS)
    r.add_argument("--sources")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-think", action="store_true")
    r.add_argument("--max-tokens", type=int, default=400)
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--label", help="name of the build that ran, for the output file and rows (default: --model)")
    r.add_argument("--prompt", choices=sorted(PROMPTS), default="base", help="detail: each step says how; 240-word cap")
    r.add_argument("--stop-file", help="stop before the next topic once this file exists (resume by rerunning)")
    r.add_argument("--reasoning-effort", choices=["low", "medium", "high"],
                   help="sent as reasoning_effort, for a model that always reasons (gpt-oss)")
    a = ap.parse_args(argv)
    if a.cmd == "prompts":
        rules = categories.Rules.load()
        sources = read_sources(a.sources)
        for t in read_topics(a.topics):
            req = build_request(t, sources, rules, "MODEL")
            if req:
                print(f"=== {t['topic']} ({'grounded' if req['_meta']['grounded'] else 'UNGROUNDED'})")
                print(req["messages"][1]["content"])
                print()
        return 0
    run(a.model, a.endpoint, a.out, a.topics, a.sources, a.dry_run, a.no_think, a.max_tokens, a.parallel, label=a.label, prompt=a.prompt,
        stop_file=a.stop_file, extra={"reasoning_effort": a.reasoning_effort} if a.reasoning_effort else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
