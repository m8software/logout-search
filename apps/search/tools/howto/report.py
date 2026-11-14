#!/usr/bin/env python3
"""Write the bake-off table from the outputs and the judge rows. Stdlib only, no network.

    report.py [--models bakeoff/models.tsv] [--out-dir bakeoff/out bakeoff/out2] [--scores bakeoff/scores-*.tsv ...]
              [--write bakeoff/BAKEOFF.md] [--samples 5]

bakeoff/models.tsv lists every candidate, run or not: label, HF id, build (quant, file size), where it
ran, status, note. A row whose label has an output file in --out-dir gets its measured numbers; the
others stay in the table as clearly marked empty rows (the 32B class waits for paid GPU time).
Scores are score.py --out files (one row per model, topic, judge). A judge whose name is the model it
scores is marked SELF: a model must not be the only scorer of its own output.

Per model: tokens/s on buildbox (median of the server's own generation rate per request, one stream), mean
generated tokens per answer (reasoning included - that is what a run pays for), answers that pass the
format check, then per judge the mean correct / complete, the number of answers flagged "a dangerous
or wrong step", and the number that pass all three.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bakeoff  # noqa: E402
import score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_TOPICS = ["how to jump start a car", "how to tie a tie", "how to get from narita airport to tokyo",
                 "how to make cold brew coffee", "how to factory reset an android phone"]


def read_models(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE) if r and not r[0].startswith("#")]
    keys = ["label", "hf_id", "build", "ran_on", "status", "judge_name", "note"]
    return [dict(zip(keys, r + [""] * (len(keys) - len(r)))) for r in rows]


def read_scores(paths: list[str]) -> dict[tuple, dict]:
    """-> {(model, judge, topic): row}"""
    out = {}
    for p in paths:
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                out[(r["model"], r["judge"], r["topic"])] = r
    return out


JUDGES = [("local", "Qwen3-14B on lr"), ("flash", "clef-flash on lr"), ("oss", "gpt-oss-20b (default reasoning) on lr"), ("clef", "Clef 27B")]
LOCAL_JUDGES = ("local", "flash", "oss")
USABLE_COLS = (*LOCAL_JUDGES, "clef")
# a judge from the same family as the model it scores: not its own output, but not an independent opinion either
SIBLING = {("Qwen3-32B Q8_0", "local"), ("Qwen3-30B-A3B Q8_0", "local"), ("Qwen3.5-27B Q8_0", "flash"),
           ("gpt-oss-120b MXFP4 reasoning-low", "oss")}
PENDING = "*pending*"
REFERENCE = "Qwen3-14B Q4_K_M"   # the buildbox model every paid model is compared with


def judge_cells(model: dict, rows: list[dict], judge: str, scores: dict) -> list[str]:
    """[correct, complete, flagged, pass] over the answers this judge read; format failures are not judged."""
    allgot = [scores[(model["label"], judge, r["topic"])] for r in rows if r["ok"] and (model["label"], judge, r["topic"]) in scores]
    got = [g for g in allgot if g["pass"] != "UNJUDGED"]
    if not got:
        return [PENDING] * 4
    n = len(got)
    mean = lambda k: sum(float(g[k]) for g in got) / n  # noqa: E731
    flagged = sum(1 for g in got if float(g["dangerous"]) >= 0.5)
    passed = sum(1 for g in got if g["pass"] == "True")
    un = f", {len(allgot) - n} unjudged" if len(allgot) > n else ""
    cells = [f"{mean('correct'):.2f}", f"{mean('complete'):.2f}", f"{flagged}/{n}", f"{passed}/{n}{un}"]
    mark = " (SELF)" if judge in model["judge_name"].split(",") else " (SIBLING)" if (model["label"], judge) in SIBLING else ""
    return [c + mark for c in cells]


def agreement(outputs: dict, scores: dict, a: str = "local", b: str = "flash") -> list[str]:
    """How often two judges give the same pass/fail verdict on the same answer: the 2x2 count and Cohen's
    kappa (agreement beyond what their pass rates alone would give). Unjudged answers are left out."""
    names = dict(JUDGES)
    cell = collections.Counter()
    for label, rows in outputs.items():
        for r in rows:
            sa, sb = scores.get((label, a, r["topic"])), scores.get((label, b, r["topic"]))
            if r["ok"] and sa and sb and "UNJUDGED" not in (sa["pass"], sb["pass"]):
                cell[(sa["pass"] == "True", sb["pass"] == "True")] += 1
    n = sum(cell.values())
    if not n:
        return []
    same = cell[(True, True)] + cell[(False, False)]
    pa, pb = (cell[(True, True)] + cell[(True, False)]) / n, (cell[(True, True)] + cell[(False, True)]) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    kappa = (same / n - pe) / (1 - pe) if pe < 1 else 1.0
    return [f"## Agreement: {names[a]} and {names[b]}", "",
            f"Same verdict on **{same} of {n} answers ({same / n:.0%})**, Cohen's kappa {kappa:.2f}.", "",
            f"| | {names[b]}: pass | {names[b]}: fail |", "|---|---|---|",
            f"| **{names[a]}: pass** | {cell[(True, True)]} | {cell[(True, False)]} |",
            f"| **{names[a]}: fail** | {cell[(False, True)]} | {cell[(False, False)]} |", ""]


def shape(rows: list[dict]) -> tuple[float, float, float]:
    """-> (median words, median numbered steps, median words per step) over the answers that kept the format."""
    ws, st, per = [], [], []
    for r in rows:
        if not r["ok"]:
            continue
        steps = re.findall(r"^\s*\d+\.\s+(.*)$", r["text"], re.M)
        if steps:
            ws.append(r["words"])
            st.append(len(steps))
            per.append(sum(len(x.split()) for x in steps) / len(steps))
    med = lambda v: statistics.median(v) if v else 0.0  # noqa: E731
    return med(ws), med(st), med(per)


def read_detailq(paths: list[str]) -> dict[tuple, str]:
    """The fourth question ("does each step say how"): {(model, judge, topic): 'True' | 'False' | 'UNJUDGED'} from
    the reasoner's TSVs (a `detailed` column) and the detailq TSVs."""
    out = {}
    for p in paths:
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                if r.get("detailed"):
                    out[(r["model"], r["judge"], r["topic"])] = r["detailed"]
    return out


def bare_section(models: list[dict], dirs: list[str], scores: dict, detailq: dict) -> list[str]:
    """Words per step for every model, the fourth question, and wrong-step flags - old prompt next to new."""
    names = dict(JUDGES)
    qj = [j for j in ("oss", "local") if any(k[1] == j for k in detailq)]
    fj = [j for j in LOCAL_JUDGES if any(k[1] == j for k in scores)]
    head = ("| model | prompt | format ok | median words | median steps | median words per step | tokens/answer |"
            + "".join(f" detailed enough ({names[j]}) |" for j in qj) + "".join(f" wrong/dangerous step flagged ({names[j]}) |" for j in fj))
    lines = [head, "|---" * (7 + len(qj) + len(fj)) + "|"]
    for m in models:
        for d in dirs:
            path = os.path.join(d, bakeoff.slug(m["label"]) + ".jsonl")
            if not os.path.exists(path):
                continue
            rows = score.read_outputs([path])
            w, st, per = shape(rows)
            tok = statistics.mean(r["completion_tokens"] for r in rows)
            cells = []
            for j in qj:
                got = [detailq.get((m["label"], j, r["topic"])) for r in rows if r["ok"]]
                got = [g for g in got if g in ("True", "False")]
                cells.append(f"{sum(g == 'True' for g in got)} of {len(rows)}" + (f" ({len(got)} judged)" if got and len(got) < len(rows) else "") if got else PENDING)
            for j in fj:
                got = [scores.get((m["label"], j, r["topic"])) for r in rows if r["ok"]]
                got = [g for g in got if g and g["dangerous"] != "UNJUDGED"]
                cells.append(f"{sum(float(g['dangerous']) >= 0.5 for g in got)}/{len(got)}" if got else PENDING)
            lines.append(f"| {m['label']} | {rows[0].get('prompt', 'base')} | {sum(1 for r in rows if r['ok'])}/{len(rows)} | {w:.0f} | {st:.0f} | {per:.1f} | {tok:.0f} | " + " | ".join(cells) + " |")
    return ["## Bare answers", "", *lines, ""]


def build(models: list[dict], out_dirs: list[str], scores: dict, n_samples: int, detailq: dict | None = None) -> str:
    judges = [j for j, _ in JUDGES]
    have = {k[1] for k in scores}
    lines = ["| model | build | ran on | tokens/s per stream as run (buildbox rows: 1 stream; RunPod rows: 4 streams, the one-stream rate is under Per model) | tokens/answer | format ok |"
             + "".join(f" {n}: correct | {n}: complete | {n}: dangerous/wrong flagged | {n}: pass |" for _, n in JUDGES)
             + "".join(f" usable of 50: {dict(JUDGES)[j]} |" for j in USABLE_COLS)
             + " usable of 50: Qwen3-14B and gpt-oss judges both | usable of 50: all three local judges | vs Qwen3-14B, all three local judges |",
             "|---|---|---|---|---|---|" + "---|---|---|---|" * len(JUDGES) + "---|" * (len(USABLE_COLS) + 3)]
    outputs = {}

    def find(label: str) -> str | None:
        for d in out_dirs:
            path = os.path.join(d, bakeoff.slug(label) + ".jsonl")
            if os.path.exists(path):
                return path
        return None

    def usable(label: str, which: tuple = ("local",)) -> int | None:
        """Answers of the 50 that kept the format AND passed every judge in `which`."""
        path = find(label)
        if path is None or not all(any(k[0] == label and k[1] == j for k in scores) for j in which):
            return None                                   # a judge that has not read this model: pending, not 0
        return sum(1 for r in score.read_outputs([path])
                   if r["ok"] and all(scores.get((label, j, r["topic"]), {}).get("pass") == "True" for j in which))
    ref_all = usable(REFERENCE, LOCAL_JUDGES)
    for m in models:
        path = find(m["label"])
        if path is None:
            cells = " | ".join(["**NOT RUN**"] * (3 + 4 * len(JUDGES) + len(USABLE_COLS) + 3))
            lines.append(f"| {m['label']} | {m['build']} | {m['ran_on']} | {cells} |")
            continue
        rows = score.read_outputs([path])
        outputs[m["label"]] = rows
        tps = statistics.median(r["tokens_per_s"] for r in rows if r.get("tokens_per_s"))
        tok = statistics.mean(r["completion_tokens"] for r in rows)
        cells = [c for j in judges for c in judge_cells(m, rows, j, scores)]
        selfs = set(m["judge_name"].split(","))
        fmt = lambda v, mark="": PENDING if v is None else f"{v}/{len(rows)}{mark}"  # noqa: E731
        for j in USABLE_COLS:
            cells.append(fmt(usable(m["label"], (j,)), " (SELF)" if j in selfs else " (SIBLING)" if (m["label"], j) in SIBLING else ""))
        cells.append(fmt(usable(m["label"], ("local", "oss")), " (one judge is SELF)" if selfs & {"local", "oss"} else ""))
        ua = usable(m["label"], LOCAL_JUDGES)
        cells.append(fmt(ua, " (one judge is SELF)" if selfs & set(LOCAL_JUDGES) else ""))
        cells.append("reference" if m["label"] == REFERENCE else f"{ua - ref_all:+d}" if None not in (ua, ref_all) else PENDING)
        lines.append(f"| {m['label']} | {m['build']} | {m['ran_on']} | {tps:.1f} | {tok:.0f} | {sum(1 for r in rows if r['ok'])}/{len(rows)} | "
                     + " | ".join(cells) + " |")
    judges = [j for j in judges if j in have]
    text = ["## The table", "", *lines, ""]
    text += bare_section(models, out_dirs, scores, detailq or {})
    for ja, jb in (("local", "flash"), ("local", "oss"), ("flash", "oss")):
        text += agreement(outputs, scores, ja, jb)
    notes = [f"- **{m['label']}** ({m['status']}): {m['note']}" for m in models if m["note"]]
    text += ["## Per model", "", *notes, ""]
    fails = []
    for label, rows in outputs.items():
        for r in rows:
            if not r["ok"]:
                fails.append(f"- {label} — {r['topic']}: {r['why']}")
    if fails:
        text += ["## Answers that failed the format check (not judged)", "", *fails, ""]
    flagged = collections.defaultdict(list)
    for (model, judge, topic), r in sorted(scores.items()):
        if r["dangerous"] != "UNJUDGED" and float(r["dangerous"]) >= 0.5 and any(x["topic"] == topic and x["ok"] for x in outputs.get(model, [])):
            flagged[topic].append(f"{model} ({dict(JUDGES).get(judge, judge)})")
    if flagged:
        text += ["## Answers a judge flagged for a dangerous or wrong step", ""]
        text += [f"- {t}: {', '.join(v)}" for t, v in sorted(flagged.items())] + [""]
    text += [f"## Samples — the same {n_samples} topics from every model", ""]
    for topic in SAMPLE_TOPICS[:n_samples]:
        text += [f"### {topic}", ""]
        for label, rows in outputs.items():
            r = next((x for x in rows if x["topic"] == topic), None)
            if r is None:
                continue
            verdicts = "; ".join(
                f"{dict(JUDGES).get(j, j)}: correct {s['correct']}, complete {s['complete']}, dangerous {s['dangerous']}"
                for j in judges for s in [scores.get((label, j, topic))] if s) or ("not judged: " + r["why"] if not r["ok"] else "not judged")
            src = ", ".join(r["source_ids"]) if r["grounded"] else "UNGROUNDED"
            text += [f"**{label}** — {src} — {r['words']} words — {verdicts}", "", "```", r["text"] or "(no answer)", "```", ""]
    return "\n".join(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=os.path.join(HERE, "bakeoff", "models.tsv"))
    ap.add_argument("--out-dir", nargs="+", default=[os.path.join(HERE, "bakeoff", "out"), os.path.join(HERE, "bakeoff", "out2")],
                    help="out/ = the runs the Clef batch covers; out2/ = runs added after it (detailed prompt, Gemma 4)")
    ap.add_argument("--detailq", nargs="*", default=sorted(glob.glob(os.path.join(HERE, "bakeoff", "detailq-*.tsv"))
                                                          + glob.glob(os.path.join(HERE, "bakeoff", "scores-gpt-oss-20b*.tsv"))))
    ap.add_argument("--scores", nargs="*", default=sorted(glob.glob(os.path.join(HERE, "bakeoff", "scores-*.tsv"))))
    ap.add_argument("--head", default=os.path.join(HERE, "bakeoff", "BAKEOFF.head.md"), help="hand-written top of the page")
    ap.add_argument("--write", help="file to write (default: stdout)")
    ap.add_argument("--samples", type=int, default=5)
    a = ap.parse_args(argv)
    body = build(read_models(a.models), a.out_dir, read_scores(a.scores), a.samples, read_detailq(a.detailq))
    head = open(a.head, encoding="utf-8").read().rstrip() + "\n\n" if os.path.exists(a.head) else ""
    variants_md = os.path.join(HERE, "bakeoff", "VARIANTS.md")      # variants.py section writes it
    if os.path.exists(variants_md):
        body += "\n\n" + open(variants_md, encoding="utf-8").read()
    if a.write:
        with open(a.write, "w", encoding="utf-8") as f:
            f.write(head + body + "\n")
    else:
        print(head + body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
