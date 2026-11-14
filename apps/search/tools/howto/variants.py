#!/usr/bin/env python3
"""Prompt variants for the how-to card (overnight 2026-10-14): arms B, C and D against the detailed prompt A.

    variants.py run --arm B|C|D --model ID --label LABEL --endpoint URL [--out bakeoff/out3] [--no-think]
                    [--parallel 4] [--extra JSON] [--stop-file F]
    variants.py check --in FILE ...        numbers in grounded answers that are not in their source; "not stated"
                                            slots; copies of the arm-B/C/D example
    variants.py section [--write bakeoff/VARIANTS.md] [--log bakeoff/variants-job.txt]
                                            the "Prompt variants" section, appended to BAKEOFF.md by report.py

Why (owner, 2026-10-13: the answers are bare; the detailed prompt A helps but contradicts itself for grounded
topics - "every step says how much" against "use only the source"):

  A  the detailed prompt (bakeoff.PROMPT_SYSTEM_DETAIL), run on 2026-10-13 - reused, not regenerated.
  B  A + one bare-versus-good example (a subject that is not one of the 50 topics) + a per-step length (15-30
     words, 3-8 steps) instead of the 240-word total.
  C  a fill-in form the server enforces: JSON schema (llama.cpp response_format) with needed[], time,
     steps[{do, how, done_when}] (3-8), mistake, warning|null, source (grounded only); per-field maxLength, so
     nothing can run over. The grounding rule is explicit: every amount and setting comes from the source, and
     where the source does not say, the slot says exactly "not stated". Sources up to SOURCE_CHARS_FORM.
  D  C + one subject line chosen by the topic's category + a second pass in which the model re-reads its own
     JSON against the checklist and rewrites only steps whose how or done_when is empty or vague.

C and D are rendered to the same card text as A and B (render()); the JSON is kept in the row. Every arm keeps
the frozen 50 topics, the grounding sources, the Source line and the refusal of denied topics (bakeoff.build_request).
Nothing here is called by the box.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import glob
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bakeoff  # noqa: E402
import categories  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT3 = os.path.join(HERE, "bakeoff", "out3")
STEP_WORDS = (15, 30)
SOURCE_CHARS_FORM = 7000     # C and D only: 4 slots x 4,096 tokens of context leave room for ~7,000 characters

# The example: re-potting a houseplant. None of the 50 topics is about it (the gardening five are pruning roses
# and tomatoes, compost, grass seed, dandelions); its distinctive words are listed for the copy check.
EXAMPLE_BARE = "Move the plant to the new pot."
EXAMPLE_GOOD = ("Tip the old pot on its side, slide the plant out by the base of its stems, and tease the outer roots "
                "loose with your fingers until they hang free.")
EXAMPLE_MARKERS = ("tip the old pot", "base of its stems", "tease", "hang free", "outer roots")

CORE = ("You write how-to answers for a search engine card. The reader is an adult who has never done this. "
        "Use plain words. For each step give what to do (one action), how to do it (which tool, how much, which "
        "setting, where to find it) and how to tell it worked (what you see, hear or feel).\n"
        f"Bare step: \"{EXAMPLE_BARE}\"\nGood step: \"{EXAMPLE_GOOD}\"\n")

PROMPT_SYSTEM_B = (
    CORE +
    "Format, exactly:\n"
    "Needed: <comma-separated items, or 'nothing special'>\n"
    "Time: <a realistic estimate>\n"
    "1. <first step>\n2. <second step>\n... (3 to 8 numbered steps, each 15 to 30 words)\n"
    "Mistake: <one line: the most common mistake and how to avoid it>\n"
    "Warning: <one line, only if a step can hurt someone or break something; otherwise omit the line>\n"
    "No introduction, no closing remark, no headings other than those above."
)

FORM_RULES = (
    CORE +
    "Fill in the form. needed: the items. time: a realistic estimate. steps: 3 to 8, each with do = one action, "
    "how = which tool, how much, which setting, where to find it, done_when = what you see, hear or feel when it "
    "worked. mistake: the single most common mistake and how to avoid it. warning: only if a step can hurt someone "
    "or break something, otherwise null.\n"
)
GROUNDED_RULE_FORM = (
    "Use ONLY the source text below. Every amount, time and setting must come from the source; where the source "
    "does not say, write exactly \"not stated\" in that slot - never guess. If the source does not cover the topic "
    "at all, set insufficient_source to true. Put the source id in source."
)
UNGROUNDED_RULE_FORM = (
    "No source text is supplied. Give only amounts and settings that are standard for this everyday task; where they "
    "vary, give a range. If you are not sure of a step, leave it out. Do not invent brand names or sources."
)
SUBJECT_LINES = {
    "cooking": "Cooking: give amounts, temperatures and times, and what the food looks like when it is ready.",
    "software": "Software and phones: give the exact menu path or keys, and say that names can differ between versions.",
    "household": "Household and repair: name the tool, and say what tight, clean or done looks like.",
    "vehicle-trivial": "Car: name each part and where it is, and what you see when the step worked.",
    "travel": "Travel: give the options and how to choose between them; prices change, so give none.",
    "crafts": "Crafts: name the material and size, and what the piece looks like after each step.",
    "gardening": "Gardening: give depths, spacing and timing in the season, and what healthy growth looks like.",
    "school": "School subjects: show the working on a small example the reader can check.",
}
SECOND_PASS = (
    "Below is a how-to answer you wrote, as JSON. Check every step against this checklist: do = one action; how = "
    "which tool, how much, which setting, where to find it; done_when = what you see, hear or feel when it worked. "
    "Rewrite ONLY the steps whose how or done_when is empty, vague or generic (\"carefully\", \"as needed\", \"until "
    "done\", \"properly\"); copy every other field unchanged. {rule}\n\nTopic: {topic}\n\nYour answer:\n{answer}{source}"
)


def schema(grounded: bool) -> dict:
    s = {"type": "string"}
    props = {
        "needed": {"type": "array", "items": {**s, "maxLength": 60}, "minItems": 1, "maxItems": 8},
        "time": {**s, "maxLength": 40},
        "steps": {"type": "array", "minItems": 3, "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "required": ["do", "how", "done_when"],
            "properties": {"do": {**s, "maxLength": 90}, "how": {**s, "maxLength": 170}, "done_when": {**s, "maxLength": 110}}}},
        "mistake": {**s, "maxLength": 160},
        "warning": {"anyOf": [{**s, "maxLength": 140}, {"type": "null"}]},
    }
    req = ["needed", "time", "steps", "mistake", "warning"]
    if grounded:
        props["insufficient_source"] = {"type": "boolean"}
        props["source"] = {**s, "maxLength": 80}
        req = ["insufficient_source", *req, "source"]
    return {"type": "object", "additionalProperties": False, "required": req, "properties": props}


def render(d: dict, grounded: bool) -> str:
    """The form as the same card text the other arms produce (so the same checks and judges read it)."""
    if grounded and d.get("insufficient_source"):
        return "INSUFFICIENT SOURCE"
    lines = ["Needed: " + ", ".join(x.strip() for x in d.get("needed", []) if x.strip()),
             "Time: " + d.get("time", "").strip()]
    for i, st in enumerate(d.get("steps", []), 1):
        parts = [st.get("do", "").strip().rstrip("."), st.get("how", "").strip().rstrip(".")]
        line = f"{i}. " + ". ".join(p for p in parts if p) + "."
        if st.get("done_when", "").strip():
            line += " Done when: " + st["done_when"].strip().rstrip(".") + "."
        lines.append(line)
    if (d.get("mistake") or "").strip():
        lines.append("Mistake: " + d["mistake"].strip())
    if (d.get("warning") or "").strip():
        lines.append("Warning: " + d["warning"].strip())
    if grounded:
        lines.append("Source: " + (d.get("source") or "[source]").strip())
    return "\n".join(lines)


def not_stated(d: dict | None) -> int:
    return json.dumps(d or {}).lower().count("not stated")


_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?(?:/\d+)?)(?![\w])")


def numbers_not_in_source(text: str, source: str) -> list[str]:
    """Numbers in a grounded answer that do not appear in its source text (step numbers, the Time line - which the
    format demands of every answer - and the Source line are not counted). A word-number in the source ("two") does not match a digit in the answer: listed, and counted."""
    body = "\n".join(re.sub(r"^\s*\d+\.\s+", "", ln) for ln in text.splitlines()
                     if not ln.startswith(("Source:", "Time:")))   # the format asks every answer for a time estimate
    src = set(_NUM.findall(source or ""))
    return [n for n in _NUM.findall(body) if n not in src]


def check_b(text: str, grounded: bool) -> tuple[bool, str]:
    """Arm B: the usual format rules with no total cap, and each step at most 30 words."""
    ok, why = bakeoff.check_output(text, grounded, max_words=10_000)
    if not ok:
        return ok, why
    long = [len(s.split()) for s in re.findall(r"^\s*\d+\.\s+(.*)$", text, re.M) if len(s.split()) > STEP_WORDS[1]]
    return (False, f"{len(long)} step(s) over {STEP_WORDS[1]} words") if long else (True, "")


def build(arm: str, t: dict, sources: dict, rules, model: str, no_think: bool, max_tokens: int) -> dict | None:
    """-> the first request for one topic (A's user text and refusals; arm-specific system text and form)."""
    base = bakeoff.build_request(t, sources, rules, model, max_tokens, no_think, prompt="detail")
    if base is None:
        return None
    meta, grounded = base["_meta"], base["_meta"]["grounded"]
    if arm == "B":
        base["messages"][0]["content"] = PROMPT_SYSTEM_B
        return base
    sysmsg = FORM_RULES + (SUBJECT_LINES.get(meta["category"], "") + "\n" if arm == "D" else "")
    if grounded:
        src = sources[t["source"]]
        user = (f"Topic: {t['topic']}\n\n{GROUNDED_RULE_FORM}\n\nSource ({src['id']}, {src.get('licence', 'CC BY-SA')}):\n"
                f"\"\"\"\n{(src.get('text') or '')[:SOURCE_CHARS_FORM]}\n\"\"\"")
    else:
        user = f"Topic: {t['topic']}\n\n{UNGROUNDED_RULE_FORM}"
    base["messages"] = [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]
    base["response_format"] = {"type": "json_schema", "json_schema": {"name": "howto", "schema": schema(grounded)}}
    base["_meta"]["source_text_form"] = (sources[t["source"]].get("text") or "")[:SOURCE_CHARS_FORM] if grounded else ""
    return base


def second_pass(req: dict, first_json: str) -> dict:
    meta = req["_meta"]
    rule = GROUNDED_RULE_FORM if meta["grounded"] else UNGROUNDED_RULE_FORM
    srcblock = f"\n\nSource:\n\"\"\"\n{meta['source_text_form']}\n\"\"\"" if meta["grounded"] else ""
    body = {k: v for k, v in req.items() if not k.startswith("_")}
    body["messages"] = [req["messages"][0], {"role": "user", "content": SECOND_PASS.format(
        rule=rule, topic=meta["topic"], answer=first_json, source=srcblock)}]
    return body


def run(a, post=bakeoff.post_chat) -> str:
    rules = categories.Rules.load()
    sources = bakeoff.read_sources(a.sources)
    reqs = [r for r in (build(a.arm, t, sources, rules, a.model, a.no_think, a.max_tokens) for t in bakeoff.read_topics(a.topics)) if r]
    extra = json.loads(a.extra) if a.extra else {}
    for r in reqs:
        r.update(extra)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, bakeoff.slug(a.label) + ".jsonl")
    have = bakeoff.done_topics(path)
    todo = [r for r in reqs if r["_meta"]["topic"] not in have]
    today = dt.date.today().isoformat()

    def one(r):
        if a.stop_file and os.path.exists(a.stop_file):
            return None
        meta = {k: v for k, v in r["_meta"].items() if k != "source_text_form"}
        row = {**meta, "model": a.label, "prompt": f"arm-{a.arm}", "date": today}
        try:
            got = post(a.endpoint, r)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return {**row, "text": "", "ok": False, "why": f"{type(e).__name__}: {e}", "words": 0, "completion_tokens": 0}
        row.update({k: got[k] for k in ("completion_tokens", "prompt_tokens", "tokens_per_s", "seconds")})
        if a.arm == "B":
            text = got["text"]
            ok, why = check_b(text, meta["grounded"]) if text else (False, "empty answer")
            return {**row, "text": text, "words": len(text.split()), "ok": ok, "why": why}
        try:
            d = json.loads(got["text"])
        except ValueError as e:
            return {**row, "text": "", "raw": got["text"][:2000], "words": 0, "ok": False, "why": f"not JSON: {e}"}
        row["json_first"] = d
        if a.arm == "D":
            try:
                g2 = post(a.endpoint, {**second_pass(r, json.dumps(d, ensure_ascii=False)), "_meta": r["_meta"]})
                d2 = json.loads(g2["text"])
                row.update({"second_pass_tokens": g2["completion_tokens"], "second_pass_seconds": g2["seconds"]})
                d = d2
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                row["second_pass_error"] = f"{type(e).__name__}: {e}"        # keep the first pass
        text = render(d, meta["grounded"])
        ok, why = bakeoff.check_output(text, meta["grounded"], max_words=10_000)
        return {**row, "json": d, "not_stated": not_stated(d), "text": text, "words": len(text.split()), "ok": ok, "why": why}

    t0 = time.time()
    with open(path, "a", encoding="utf-8") as f, cf.ThreadPoolExecutor(a.parallel) as pool:
        for row in pool.map(one, todo):
            if row is None:
                continue
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    rows = list(bakeoff.done_topics(path).values())
    print(f"[variants] {a.label} arm {a.arm}: {len(rows)}/{len(reqs)} written, {sum(r['ok'] for r in rows)} pass the "
          f"format check, {time.time() - t0:.1f} s wall at {a.parallel} stream(s) -> {path}", file=sys.stderr)
    return path


def check_rows(rows: list[dict], sources: dict) -> dict:
    """Per file: numbers not in the source (grounded answers), "not stated" slots, example copies."""
    nums, ns, copies = [], 0, []
    for r in rows:
        if not r.get("text"):
            continue
        low = r["text"].lower()
        hits = [m for m in EXAMPLE_MARKERS if m in low]
        if hits:
            copies.append((r["topic"], hits))
        if r.get("grounded") and r.get("ok"):
            src = sources.get(r["source_ids"][0], {}).get("text", "") if r.get("source_ids") else ""
            limit = SOURCE_CHARS_FORM if str(r.get("prompt", "")).startswith(("arm-C", "arm-D")) else bakeoff.SOURCE_CHARS
            bad = numbers_not_in_source(r["text"], src[:limit])
            if bad:
                nums.append((r["topic"], bad))
            ns += r.get("not_stated", r["text"].lower().count("not stated"))
    return {"numbers": nums, "not_stated": ns, "copies": copies}


# ---- the "Prompt variants" section ---------------------------------------------------------------------------
MODELS = ["Gemma-4-12B QAT Q4_0", "Qwen3-14B Q4_K_M", "gpt-oss-20b MXFP4 reasoning-low"]
A_LABEL = {"Gemma-4-12B QAT Q4_0": "Gemma-4-12B QAT Q4_0 detail", "Qwen3-14B Q4_K_M": "Qwen3-14B Q4_K_M detail",
           "gpt-oss-20b MXFP4 reasoning-low": "gpt-oss-20b MXFP4 reasoning-low detail"}
A_WALL = {"Gemma-4-12B QAT Q4_0": (98.2, 4), "Qwen3-14B Q4_K_M": (80.2, 4), "gpt-oss-20b MXFP4 reasoning-low": (168.3, 1)}
SAMPLE_TOPICS = ["how to jump start a car", "how to tie a tie", "how to get from narita airport to tokyo",
                 "how to make cold brew coffee", "how to factory reset an android phone"]


def read_dq(path: str) -> dict:
    """{(model, topic, tag): True|False|None} from the committed verdict file."""
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8").read().splitlines()[1:]:
            model, topic, tag, v = line.split("\t")
            out[(model, topic, tag)] = {"True": True, "False": False}.get(v)
    return out


def read_walls(log: str) -> dict:
    """{(model, arm): (seconds, streams)} from the buildbox job log lines 'generated <model> arm <X>: N s wall at P stream(s)'."""
    out = {}
    if os.path.exists(log):
        for m in re.finditer(r"generated (.+?) arm ([BCD]): ([\d.]+) s wall at (\d+) stream", open(log, encoding="utf-8").read()):
            out[(m.group(1), m.group(2))] = (float(m.group(3)), int(m.group(4)))
    return out


def section(scores: dict, dq: dict, walls: dict, sources: dict, dirs: list[str]) -> str:
    import report  # noqa: E402  (report imports nothing from here)
    lines = ["## Prompt variants (overnight 2026-10-14, buildbox)", "",
             "A = the detailed prompt of 2026-10-13 (reused); B = A + a bare-versus-good example (re-potting a houseplant, "
             "not one of the 50) + 15-30 words per step; C = a JSON form the server enforces (do / how / done_when per "
             "step, \"not stated\" where the source does not say, sources up to 7,000 characters); D = C + a subject line by "
             "category + a second pass that rewrites vague steps. Judges: Qwen3-14B and clef-flash for pass counts; the "
             "fourth question (\"does each step say how\") by gpt-oss-20b at default reasoning, asked twice (dq1 / dq2). "
             "Numbers not in source: digits in a grounded answer that do not appear in the source text the model was given.", "",
             "| model | arm | format ok | usable: Qwen3-14B judge | usable: clef-flash | usable: both | detailed enough dq1 / dq2 (flips) "
             "| wrong/dangerous flagged: Qwen3-14B / clef-flash | \"not stated\" slots | numbers not in source (answers) "
             "| example copies | words per step | tokens/answer | 20,000 topics on buildbox |",
             "|---" * 14 + "|"]
    numbers_lists = []
    for m in MODELS:
        for arm in "ABCD":
            label = A_LABEL[m] if arm == "A" else f"{m} arm-{arm}"
            path = next((os.path.join(d, bakeoff.slug(label) + ".jsonl") for d in dirs if os.path.exists(os.path.join(d, bakeoff.slug(label) + ".jsonl"))), None)
            if path is None:
                lines.append(f"| {m} | {arm} | **not run** |" + " |" * 11)
                continue
            rows = list(bakeoff.done_topics(path).values())
            ok = [r for r in rows if r["ok"]]
            def usable(js):
                if not all(any(k[0] == label and k[1] == j for k in scores) for j in js):
                    return None
                return sum(1 for r in ok if all(scores.get((label, j, r["topic"]), {}).get("pass") == "True" for j in js))
            fmt = lambda v: "—" if v is None else str(v)  # noqa: E731
            flag = lambda j: "—" if usable((j,)) is None else str(sum(1 for r in ok if float(scores.get((label, j, r["topic"]), {"dangerous": 0})["dangerous"]) >= 0.5))  # noqa: E731
            d1 = [dq.get((label, r["topic"], "dq1")) for r in ok]
            d2 = [dq.get((label, r["topic"], "dq2")) for r in ok]
            flips = sum(1 for x, y in zip(d1, d2) if x is not None and y is not None and x != y)
            n1, n2 = sum(x is not None for x in d1), sum(x is not None for x in d2)
            dqcell = (f"{sum(x is True for x in d1)} / " + (f"{sum(x is True for x in d2)} ({flips} flips{'' if n2 == n1 else f'; unjudged: dq1 {len(d1) - n1}, dq2 {len(d2) - n2}'})" if n2 else "not read")
                      if n1 else "—")
            res = check_rows(rows, sources)
            numbers_lists.append((label, res["numbers"]))
            _, _, per = report.shape(rows)
            tok = statistics.mean(r.get("completion_tokens", 0) + r.get("second_pass_tokens", 0) for r in rows)
            wall = A_WALL.get(m) if arm == "A" else walls.get((m, arm))
            est = f"{wall[0] / len(rows) * 20000 / 3600:.1f} h ({wall[1]} stream{'s' if wall[1] > 1 else ''})" if wall else "—"
            lines.append(f"| {m} | {arm} | {len(ok)}/{len(rows)} | {fmt(usable(('local',)))} | {fmt(usable(('flash',)))} | "
                         f"{fmt(usable(('local', 'flash')))} | {dqcell} | {flag('local')} / {flag('flash')} | "
                         f"{res['not_stated'] if arm in 'CD' else '—'} | {sum(len(b) for _, b in res['numbers'])} ({len(res['numbers'])}) | "
                         f"{len(res['copies'])} | {per:.1f} | {tok:.0f} | {est} |")
    lines += ["", "Format failures, per run:", ""]
    for m in MODELS:
        for arm in "BCD":
            label = f"{m} arm-{arm}"
            for d in dirs:
                p = os.path.join(d, bakeoff.slug(label) + ".jsonl")
                if os.path.exists(p):
                    bad = [f"{r['topic']} ({r['why']})" for r in bakeoff.done_topics(p).values() if not r["ok"]]
                    lines.append(f"- {label}: " + ("; ".join(bad) if bad else "none"))
    lines += ["", "Numbers in grounded answers that are not in the source text given to the model:", ""]
    for label, nums in numbers_lists:
        if nums:
            lines.append(f"- **{label}**: " + "; ".join(f"{t} — {', '.join(b)}" for t, b in nums))
    lines += ["", "### Samples — the same five topics, every arm", ""]
    for topic in SAMPLE_TOPICS:
        lines += [f"#### {topic}", ""]
        for m in MODELS:
            for arm in "ABCD":
                label = A_LABEL[m] if arm == "A" else f"{m} arm-{arm}"
                for d in dirs:
                    p = os.path.join(d, bakeoff.slug(label) + ".jsonl")
                    if os.path.exists(p):
                        r = bakeoff.done_topics(p).get(topic)
                        if r:
                            lines += [f"**{m}, arm {arm}** — {r['words']} words{'' if r['ok'] else ' — FORMAT: ' + r['why']}", "", "```", r["text"], "```", ""]
                        break
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=["B", "C", "D"], required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--endpoint", required=True)
    r.add_argument("--out", default=OUT3)
    r.add_argument("--topics", default=bakeoff.DEFAULT_TOPICS)
    r.add_argument("--sources", default=os.path.join(HERE, "bakeoff", "sources.jsonl"))
    r.add_argument("--no-think", action="store_true")
    r.add_argument("--max-tokens", type=int, default=900)
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--extra", help="JSON merged into every request, e.g. '{\"reasoning_effort\": \"low\"}'")
    r.add_argument("--stop-file")
    c = sub.add_parser("check")
    c.add_argument("--in", dest="inputs", nargs="+", required=True)
    c.add_argument("--sources", default=os.path.join(HERE, "bakeoff", "sources.jsonl"))
    q = sub.add_parser("dqtsv", help="the fourth-question verdicts (reasoning stays in the results file) -> TSV")
    q.add_argument("--results", required=True)
    q.add_argument("--out", default=os.path.join(HERE, "bakeoff", "dq-gpt-oss-20b.tsv"))
    w = sub.add_parser("section")
    w.add_argument("--write", default=os.path.join(HERE, "bakeoff", "VARIANTS.md"))
    w.add_argument("--log", default=os.path.join(HERE, "bakeoff", "variants-job.txt"))
    a = ap.parse_args(argv)
    if a.cmd == "dqtsv":
        n = 0
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("model\ttopic\ttag\tdetailed\n")
            for line in open(a.results, encoding="utf-8"):
                r = json.loads(line)
                v = r["verdict"]["detailed"] if r.get("verdict") else "UNJUDGED"
                f.write(f"{r['model']}\t{r['topic']}\t{r['tag']}\t{v}\n")
                n += 1
        print(f"[variants] {n} verdicts -> {a.out}", file=sys.stderr)
        return 0
    if a.cmd == "section":
        import report  # noqa: E402
        scores = report.read_scores(sorted(glob.glob(os.path.join(HERE, "bakeoff", "scores-*.tsv"))))
        text = section(scores, read_dq(os.path.join(HERE, "bakeoff", "dq-gpt-oss-20b.tsv")), read_walls(a.log),
                       bakeoff.read_sources(os.path.join(HERE, "bakeoff", "sources.jsonl")),
                       [OUT3, os.path.join(HERE, "bakeoff", "out2")])
        with open(a.write, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[variants] section -> {a.write}", file=sys.stderr)
        return 0
    if a.cmd == "run":
        run(a)
        return 0
    sources = bakeoff.read_sources(a.sources)
    for p in a.inputs:
        rows = list(bakeoff.done_topics(p).values())
        res = check_rows(rows, sources)
        print(f"{os.path.basename(p)}: numbers not in source {sum(len(b) for _, b in res['numbers'])} in "
              f"{len(res['numbers'])} answers; not stated {res['not_stated']}; example copies {len(res['copies'])}")
        for topic, bad in res["numbers"]:
            print(f"   {topic}: {', '.join(bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
