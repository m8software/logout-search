#!/usr/bin/env python3
"""Whole-list pass (2026-11-05): Qwen3-14B (llama.cpp on buildbox, thinking off) sees a name's top 12 candidates at once
and answers which ones a searcher wants, best first. stdlib only; starts and stops its own llama-server; 4 requests
in parallel (-np 4); resumable (names already in the output are skipped)."""
import json, os, re, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

T = "C:\\logout-teacher"; PORT = 8478; PAR = 4
cmd = [T + "\\bin\\llama-server.exe", "--host", "127.0.0.1", "--port", str(PORT), "-m", T + "\\models\\Qwen3-14B-Q4_K_M.gguf",
       "-ngl", "99", "-c", str(4096 * PAR), "-np", str(PAR)]
P = json.load(open(sys.argv[1], encoding="utf-8")); outp = sys.argv[2]
done = set()
if os.path.exists(outp):
    for line in open(outp, encoding="utf-8"):
        try:
            done.add(json.loads(line)["i"])
        except ValueError:
            pass
todo = [p for p in P if p["i"] not in done]
print(f"{len(P)} names, {len(todo)} to do", flush=True)


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def ask(p):
    lines = "\n".join(f"{k + 1}. {c['t']} — {(urlsplit(c['u']).hostname or '').removeprefix('www.')}{urlsplit(c['u']).path[:40]}"
                      for k, c in enumerate(p["cands"]))
    user = (f"Search: {p['q']}\n\nResults:\n{lines}\n\nWhich results does a person who typed this search want to see? List their numbers, best first. "
            "Leave out results about a different meaning of the words, results that only share a word, and near-duplicates of a result already listed. "
            "Answer with the numbers separated by commas and nothing else; answer 0 if none fit.")
    prompt = f"<|im_start|>system\nYou pick web search results.<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    try:
        txt = post("/completion", {"prompt": prompt, "n_predict": 48, "temperature": 0, "cache_prompt": True}).get("content", "")
    except Exception as e:  # noqa: BLE001
        return {"i": p["i"], "q": p["q"], "err": type(e).__name__, "rows": []}
    seen = []
    for m in re.findall(r"\d+", txt):
        k = int(m)
        if 1 <= k <= len(p["cands"]) and k not in seen:
            seen.append(k)
    return {"i": p["i"], "q": p["q"], "raw": txt.strip()[:200], "rows": [p["cands"][k - 1]["u"] for k in seen]}


log = open("lw-server.log", "w")
srv = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5)
            break
        except Exception:  # noqa: BLE001
            time.sleep(3)
    t0, lock = time.time(), threading.Lock()
    with open(outp, "a", encoding="utf-8") as f, ThreadPoolExecutor(PAR) as ex:
        for n, r in enumerate(ex.map(ask, todo)):
            with lock:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            if n % 200 == 0:
                f.flush()
                print(n, round(time.time() - t0), "s", flush=True)
    print("done", round(time.time() - t0), "s", flush=True)
finally:
    srv.terminate()
    srv.wait(30)
