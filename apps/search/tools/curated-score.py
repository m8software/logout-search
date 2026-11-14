#!/usr/bin/env python3
"""Qwen3-Reranker-4B over pairs.jsonl ({id, q, t, u, c}) on the GPU: P(yes) per pair -> scores.jsonl. The model card's
prompt; the document is "<host> | <title>. <text>".

    curated-score.py PAIRS OUT [MODEL [BATCH]]   default Qwen3-Reranker-4B, batch 8. The daytime prefilter
    (curated-prefilter.sh) runs it with Qwen/Qwen3-Reranker-0.6B and batch 32."""
import json, sys, time, torch
from urllib.parse import urlsplit
from transformers import AutoTokenizer, AutoModelForCausalLM
M = sys.argv[3] if len(sys.argv) > 3 else "Qwen/Qwen3-Reranker-4B"
BS = int(sys.argv[4]) if len(sys.argv) > 4 else 8
tok = AutoTokenizer.from_pretrained(M, padding_side="left")
model = AutoModelForCausalLM.from_pretrained(M, torch_dtype=torch.float16).cuda().eval()
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
todo = sorted((r for r in rows if r["id"] not in done), key=lambda r: len(r["q"]) + len(r["t"]) + len(r["c"]))   # less padding per batch
print(f"{len(rows)} pairs, {len(todo)} to score", flush=True)
t0 = time.time()
with open(sys.argv[2], "a") as f:
    for i in range(0, len(todo), BS):
        b = todo[i:i + BS]
        texts = [pre + f"<Instruct>: {INS}\n<Query>: {r['q']}\n<Document>: {(urlsplit(r['u']).hostname or '').removeprefix('www.')} | {r['t']}. {r['c']}" + suf for r in b]
        enc = tok(texts, padding=True, truncation=True, max_length=320, return_tensors="pt").to("cuda")
        with torch.no_grad():
            # only the last position's yes/no logits: the full logits (8 x 320 x 152k) took ~0.8 GB and, beside a
            # desktop's own use, pushed the 12 GB card into Windows paging (2026-11-06: 4.8 pairs/s instead of ~29)
            h = model.model(**enc).last_hidden_state[:, -1, :]
            lg = model.lm_head(h)
        p = torch.softmax(torch.stack([lg[:, no], lg[:, yes]], 1).float(), 1)[:, 1].tolist()
        for r, s in zip(b, p):
            f.write(json.dumps({"id": r["id"], "p": round(s, 5)}) + "\n")
        if i % 800 == 0:
            f.flush(); print(i, round(time.time() - t0), "s", flush=True)
print("done", round(time.time() - t0), "s", flush=True)
