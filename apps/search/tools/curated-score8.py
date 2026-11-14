#!/usr/bin/env python3
"""Qwen3-Reranker-4B with 8-bit weights over pairs.jsonl ({id, q, t, u, c}): P(yes) per pair -> scores.jsonl. The
same prompt and document text as curated-score.py; it fits an 8 GB card (indexhost's 3060 Ti: 5.1 GB at batch 16,
20.5 pairs a second, 2026-11-12). Against buildbox's full-precision scores on 500 names: the same top pick for 92%, and its
top 10 holds 98.9% of buildbox's top 8. Resumes: ids already in OUT are skipped.

    curated-score8.py PAIRS OUT [BATCH]"""
import json, sys, time, torch
from urllib.parse import urlsplit
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
M = "Qwen/Qwen3-Reranker-4B"
BS = int(sys.argv[3]) if len(sys.argv) > 3 else 16
tok = AutoTokenizer.from_pretrained(M, padding_side="left")
model = AutoModelForCausalLM.from_pretrained(M, quantization_config=BitsAndBytesConfig(load_in_8bit=True), device_map={"": 0}).eval()
yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
pre = "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n"
suf = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
INS = "Given a web search query, judge whether this search result is one the searcher wants to see near the top"
rows = [json.loads(l) for l in open(sys.argv[1])]
done = set()
try:
    done = {json.loads(l)["id"] for l in open(sys.argv[2])}
except FileNotFoundError:
    pass
todo = sorted((r for r in rows if r["id"] not in done), key=lambda r: len(r["q"]) + len(r["t"]) + len(r["c"]))
print(f"{len(rows)} pairs, {len(todo)} to score", flush=True)
t0 = time.time()
with open(sys.argv[2], "a") as f:
    for i in range(0, len(todo), BS):
        b = todo[i:i + BS]
        texts = [pre + f"<Instruct>: {INS}\n<Query>: {r['q']}\n<Document>: {(urlsplit(r['u']).hostname or '').removeprefix('www.')} | {r['t']}. {r['c']}" + suf for r in b]
        enc = tok(texts, padding=True, truncation=True, max_length=320, return_tensors="pt").to("cuda")
        with torch.no_grad():
            lg = model.lm_head(model.model(**enc).last_hidden_state[:, -1, :])
        p = torch.softmax(torch.stack([lg[:, no], lg[:, yes]], 1).float(), 1)[:, 1].tolist()
        for r, s in zip(b, p):
            f.write(json.dumps({"id": r["id"], "p": round(s, 5)}) + "\n")
        if i % 1600 == 0:
            f.flush(); print(i, round(time.time() - t0), "s", flush=True)
print("done", round(time.time() - t0), "s", flush=True)
