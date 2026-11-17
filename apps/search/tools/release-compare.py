#!/usr/bin/env python3
"""release-compare.py - serve a search data release in a scratch engine, ask names, compare two releases.

FINDINGS (measured 2026-11-16 on the dev workstation, rootless podman, rc15, image docker.io/logout/search-api:public):

  How the engine serves a release folder (indexhost's compose, `installer/install.py start --print-env`):
    mount the release at /rel:ro and set
      SEARCH_POSTGRES=off  SEARCH_LOOKUP_BACKEND=release  SEARCH_RELEASE_DIR=/rel  SEARCH_RELEASE_ID=<manifest "release">
      SEARCH_WIKI_BACKEND=tantivy-release  SEARCH_WEB_BACKEND=tantivy-release
      SEARCH_SEM_BACKEND=ivf  SEARCH_SEM_IVF=/rel/vectors/ivf  SEARCH_NOMIC_DIR=/rel/models/nomic-embed-text-v1.5
      SEARCH_MODEL_DIR=/rel/models/bge-small-en-v1.5  SEARCH_RANKER=/rel/ranker.json
      SEARCH_CE_DIR=/rel/models/rerank-turbo  SEARCH_CE_GOV_DIR=/rel/models/rerank-turbo  SEARCH_CE_DEVICE=cpu
      SEARCH_REQUIRE_RERANKER=1  SEARCH_REQUIRE_KINDS_MODEL=1
      SEARCH_API_ALLOW_FROM=0.0.0.0/0   (the port is published on 127.0.0.1 only; the default allow-list rejects
                                         the container-side source address of the port forward)
    Image entry: `python /app/search_api.py`, listens on 8090. The image HAS tantivy (0.26.2), no pull or build needed.
    The scratch engine is the same CODE as the image (/app), not the checkout; to test engine code from a branch pass
    --api DIR (mounted over /app, read-only).

  Differences from indexhost on purpose (so the answer depends only on the release folder):
    no news store (no headlines/weather/scoreboard/towns), no result store (SEARCH_STORE), no sitenav/cardlinks/
    wikilinks/SE/curated mounts, SEARCH_CE_DIR is the release's own rerank-turbo (indexhost uses ce-st3), and none of the
    breadth/product settings. Add any of them with --env K=V and --mount HOST:CONT[:ro] if a comparison needs them
    (both arms must then be started alike). Both releases compared must be served with the same flags.

  Gotchas:
    * 2026-11-16: /home/owner/data/wf/rel/rc15/vectors/ivf/codes.u8 is TRUNCATED (97,664,512 bytes, the live file is
      651,879,168; mtime 2026-11-16 01:46). The engine then dies at start with
      "ValueError: mmap length is greater than file size" in sem_ivf.py. Every other file matches indexhost by size.
      Workaround without touching the release: --overlay /home/owner/data/wf/rc-cmp/rc15-ivf-codes.u8:vectors/ivf/codes.u8
      (a good copy fetched from indexhost read-only). A new rc16 folder needs no overlay if its copy is whole: compare
      `find . -type f -printf '%s %p\\n'` with the source first.
    * :Z relabels the folder for SELinux (needed on this Fedora host); it only changes labels, not content. With :ro
      the release is never written.
    * Host memory is tight (19 GB total, often 4 GB free). The engine's own anon use is ~3 GB; the 12g cap is a ceiling,
      the rest is page cache that two engines on one release share.
    * Ask with --workers 1 or 2: the engine runs the reranker on 6 cpus; more workers only queue. Do not ask with
      warm=1 (that writes the result store; this engine has none).
    * Containers are named rc-cmp-<name>; `stop` removes by exact name. Nothing here touches indexhost, testbox, public or
      homeserver; the engine is reachable on 127.0.0.1 only.

  Commands:
    release-compare.py serve REL_DIR --port 18081 --name a [--overlay HOST:RELPATH] [--env K=V] [--cpus 6] [--memory 12g]
    release-compare.py ask URL NAMES.txt OUT.jsonl [--workers 2] [--limit N]
    release-compare.py compare A.jsonl B.jsonl [--changed-ids FILE] [--out report.md]
    release-compare.py stop NAME [NAME ...]
    release-compare.py selftest            (no engine: compare logic on synthetic rows)

  --changed-ids FILE: one line per name, "name<TAB>new text fragment" (the fragment that must appear in B's card lead or
  result snippets and not in A's); a bare "name" line only checks that the card/snippets differ between A and B.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IMAGE = "docker.io/logout/search-api:public"
PREFIX = "rc-cmp-"


def sh(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        sys.exit(f"release-compare: {' '.join(cmd[:3])} failed ({p.returncode}): {p.stderr.strip()[-600:]}")
    return p


# ---------------------------------------------------------------- serve / stop
def cmd_serve(a):
    rel = os.path.abspath(a.rel_dir)
    mf = os.path.join(rel, "manifest.json")
    if not os.path.isfile(mf):
        sys.exit(f"release-compare: no manifest.json in {rel}")
    rid = a.release_id or json.load(open(mf))["release"]
    name = PREFIX + a.name
    if sh(["podman", "container", "exists", name], check=False).returncode == 0:
        sys.exit(f"release-compare: container {name} exists; run `stop {a.name}` first")
    env = {
        "SEARCH_POSTGRES": "off", "SEARCH_LOOKUP_BACKEND": "release", "SEARCH_RELEASE_DIR": "/rel",
        "SEARCH_RELEASE_ID": rid, "SEARCH_WIKI_BACKEND": "tantivy-release", "SEARCH_WEB_BACKEND": "tantivy-release",
        "SEARCH_SEM_BACKEND": "ivf", "SEARCH_SEM_IVF": "/rel/vectors/ivf",
        "SEARCH_NOMIC_DIR": "/rel/models/nomic-embed-text-v1.5", "SEARCH_MODEL_DIR": "/rel/models/bge-small-en-v1.5",
        "SEARCH_RANKER": "/rel/ranker.json", "SEARCH_CE_DIR": "/rel/models/rerank-turbo",
        "SEARCH_CE_GOV_DIR": "/rel/models/rerank-turbo", "SEARCH_CE_DEVICE": "cpu",
        "SEARCH_REQUIRE_RERANKER": "1", "SEARCH_REQUIRE_KINDS_MODEL": "1", "SEARCH_API_ALLOW_FROM": "0.0.0.0/0",
    }
    for kv in a.env:
        k, _, v = kv.partition("=")
        env[k] = v
    cmd = ["podman", "run", "-d", "--name", name, "--cpus", str(a.cpus), "--memory", a.memory,
           "-p", f"127.0.0.1:{a.port}:8090", "-v", f"{rel}:/rel:ro,Z"]
    for ov in a.overlay:
        host, _, relp = ov.partition(":")
        cmd += ["-v", f"{os.path.abspath(host)}:/rel/{relp.lstrip('/')}:ro,Z"]
    for m in a.mount:
        cmd += ["-v", m if m.count(":") >= 2 or m.endswith(("ro", "rw")) else m + ":ro,Z"]
    if a.api:
        cmd += ["-v", f"{os.path.abspath(a.api)}:/app:ro,Z"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(a.image)
    t0 = time.time()
    sh(cmd)
    url = f"http://127.0.0.1:{a.port}"
    while time.time() - t0 < a.timeout:
        if sh(["podman", "container", "inspect", name, "--format", "{{.State.Running}}"], check=False).stdout.strip() != "true":
            print(sh(["podman", "logs", "--tail", "25", name], check=False).stderr[-2500:], file=sys.stderr)
            sys.exit(f"release-compare: {name} exited during start (see log above)")
        try:
            if urllib.request.urlopen(url + "/readyz", timeout=3).status == 200:
                print(json.dumps({"name": name, "url": url, "release": rid, "startup_s": round(time.time() - t0, 1)}))
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    sys.exit(f"release-compare: {name} not ready after {a.timeout}s; container left running for `podman logs {name}`")


def cmd_stop(a):
    for n in a.names:
        name = n if n.startswith(PREFIX) else PREFIX + n
        p = sh(["podman", "rm", "-f", "-t", "5", name], check=False)
        print(f"{name}: {'removed' if p.returncode == 0 else p.stderr.strip()}")


# ---------------------------------------------------------------- ask
def _s(x):
    return x if isinstance(x, str) else ("" if x is None else json.dumps(x, sort_keys=True))


def extract(resp):
    """The parts a release change can move: the card (title, description, lead) and the top-10 results."""
    card = resp.get("card") or resp.get("answer") or {}
    if not isinstance(card, dict):
        card = {"lead": _s(card)}
    results = resp.get("results") or []
    top = []
    for r in results[:10]:
        if isinstance(r, dict):
            top.append({"title": _s(r.get("title")), "url": _s(r.get("url")),
                        "snippet": _s(r.get("content") or r.get("snippet") or r.get("description"))})
    return {
        "card_title": _s(card.get("title") or card.get("name")),
        "card_description": _s(card.get("description") or card.get("short")),
        "card_lead": _s(card.get("lead") or card.get("extract") or card.get("text") or card.get("summary")),
        "top": top,
    }


def ask_one(url, name, timeout):
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"{url}/search?" + urllib.parse.urlencode({"q": name}), timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        return {"q": name, "status": 0, "error": str(e), "ms": round((time.time() - t0) * 1000)}
    ms = round((time.time() - t0) * 1000)
    row = {"q": name, "status": status, "ms": ms}
    try:
        resp = json.loads(raw)
    except ValueError:
        row["error"] = "not json"
        row["sha"] = hashlib.sha256(raw).hexdigest()[:16]
        return row
    row.update(extract(resp))
    # what the engine answered, minus anything that varies per call (timings, ids of the request)
    row["sha"] = hashlib.sha256(json.dumps({k: row[k] for k in ("card_title", "card_description", "card_lead", "top")},
                                            sort_keys=True).encode()).hexdigest()[:16]
    return row


def cmd_ask(a):
    names = [l.strip() for l in open(a.namefile, encoding="utf-8") if l.strip() and not l.startswith("#")]
    names = [n.split("\t")[0] for n in names][: a.limit or None]
    rows = [None] * len(names)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(ask_one, a.url.rstrip("/"), n, a.timeout): i for i, n in enumerate(names)}
        for f in cf.as_completed(futs):
            rows[futs[f]] = f.result()
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    ok = sum(r["status"] == 200 for r in rows)
    ms = sorted(r["ms"] for r in rows)
    print(json.dumps({"names": len(rows), "ok_200": ok, "median_ms": ms[len(ms) // 2], "p95_ms": ms[int(len(ms) * .95) - 1 if len(ms) > 1 else 0],
                      "max_ms": ms[-1]}))


# ---------------------------------------------------------------- compare
def load(p):
    return {r["q"]: r for r in (json.loads(l) for l in open(p, encoding="utf-8") if l.strip())}


def texts(r):
    return " \n".join([r.get("card_lead", ""), r.get("card_description", "")] + [t["snippet"] for t in r.get("top", [])])


def compare(A, B, changed):
    names = [n for n in A if n in B]
    same = [n for n in names if A[n].get("sha") and A[n].get("sha") == B[n].get("sha")]
    diff = [n for n in names if n not in same]
    bad = [n for n in names if A[n]["status"] != 200 or B[n]["status"] != 200]
    out = {"names": len(names), "identical": len(same), "differ": len(diff), "not_200": len(bad),
           "only_in_a": len(A) - len(names), "only_in_b": len(B) - len(names), "differ_names": diff, "not_200_names": bad}
    parts = {"card_lead": 0, "card_description": 0, "card_title": 0, "top_urls": 0, "snippets": 0}
    for n in diff:
        a, b = A[n], B[n]
        for k in ("card_lead", "card_description", "card_title"):
            parts[k] += a.get(k) != b.get(k)
        parts["top_urls"] += [t["url"] for t in a.get("top", [])] != [t["url"] for t in b.get("top", [])]
        parts["snippets"] += [t["snippet"] for t in a.get("top", [])] != [t["snippet"] for t in b.get("top", [])]
    out["differ_by_part"] = parts
    ch = []
    for n, frag in changed.items():
        if n not in A or n not in B:
            ch.append({"q": n, "verdict": "not asked"})
            continue
        if frag:
            in_b, in_a = frag in texts(B[n]), frag in texts(A[n])
            where = [w for w, t in (("lead", B[n].get("card_lead", "")), ("description", B[n].get("card_description", "")),
                                    ("snippets", " ".join(x["snippet"] for x in B[n].get("top", [])))) if frag in t]
            v = ("new text shown in " + "+".join(where)) if in_b and not in_a else ("text in both (old already had it)" if in_b else "NEW TEXT MISSING")
        else:
            v = "card/snippets changed" if texts(A[n]) != texts(B[n]) else "NO CHANGE SHOWN"
        ch.append({"q": n, "verdict": v})
    out["changed"] = ch
    return out


def render(o, a_path, b_path, ms_a, ms_b):
    L = [f"# release compare: {os.path.basename(a_path)} vs {os.path.basename(b_path)}", "",
         f"- names compared: {o['names']} (only in A: {o['only_in_a']}, only in B: {o['only_in_b']})",
         f"- byte-identical answers (card + top 10 titles/urls/snippets): {o['identical']}",
         f"- differ: {o['differ']}   not 200 in either arm: {o['not_200']}",
         f"- differing names by part: " + ", ".join(f"{k} {v}" for k, v in o["differ_by_part"].items()),
         f"- median latency ms: A {ms_a}, B {ms_b}"]
    if o["changed"]:
        c = {}
        for x in o["changed"]:
            c[x["verdict"].split(" in ")[0]] = c.get(x["verdict"].split(" in ")[0], 0) + 1
        L += ["", f"## names listed as article changed ({len(o['changed'])})", ""] + [f"- {k}: {v}" for k, v in sorted(c.items())]
        L += ["", "| name | verdict |", "|---|---|"] + [f"| {x['q']} | {x['verdict']} |" for x in o["changed"][:60]]
    if o["differ_names"]:
        L += ["", "## first differing names", ""] + [f"- {n}" for n in o["differ_names"][:40]]
    if o["not_200_names"]:
        L += ["", "## not 200", ""] + [f"- {n}" for n in o["not_200_names"][:40]]
    return "\n".join(L) + "\n"


def read_changed(p):
    ch = {}
    if p:
        for l in open(p, encoding="utf-8"):
            l = l.rstrip("\n")
            if l.strip() and not l.startswith("#"):
                n, _, frag = l.partition("\t")
                ch[n.strip()] = frag.strip()
    return ch


def cmd_compare(a):
    A, B = load(a.a), load(a.b)
    o = compare(A, B, read_changed(a.changed_ids))
    med = lambda D: sorted(r["ms"] for r in D.values())[len(D) // 2] if D else 0
    md = render(o, a.a, a.b, med(A), med(B))
    if a.out:
        open(a.out, "w").write(md)
    print(md)


def cmd_selftest(_a):
    def row(q, lead, url="u1", st=200):
        r = {"q": q, "status": st, "ms": 5, "card_title": q, "card_description": "d", "card_lead": lead,
             "top": [{"title": "t", "url": url, "snippet": "s " + lead}]}
        r["sha"] = hashlib.sha256(json.dumps({k: r[k] for k in ("card_title", "card_description", "card_lead", "top")}, sort_keys=True).encode()).hexdigest()[:16]
        return r
    A = {"x": row("x", "old text"), "y": row("y", "same"), "z": row("z", "old")}
    B = {"x": row("x", "old text NEWFACT"), "y": row("y", "same"), "z": row("z", "old")}
    o = compare(A, B, {"x": "NEWFACT", "z": "NEWFACT", "y": ""})
    assert (o["identical"], o["differ"], o["not_200"]) == (2, 1, 0), o
    v = {c["q"]: c["verdict"] for c in o["changed"]}
    assert v["x"].startswith("new text shown in lead"), v
    assert v["z"] == "NEW TEXT MISSING" and v["y"] == "NO CHANGE SHOWN", v
    e = extract({"card": {"title": "T", "lead": "L"}, "results": [{"title": "a", "url": "u", "content": "c"}] * 12})
    assert e["card_lead"] == "L" and len(e["top"]) == 10
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("serve"); x.add_argument("rel_dir"); x.add_argument("--port", type=int, required=True)
    x.add_argument("--name", required=True); x.add_argument("--overlay", action="append", default=[])
    x.add_argument("--env", action="append", default=[]); x.add_argument("--mount", action="append", default=[])
    x.add_argument("--cpus", default="6"); x.add_argument("--memory", default="12g"); x.add_argument("--image", default=IMAGE)
    x.add_argument("--release-id"); x.add_argument("--api"); x.add_argument("--timeout", type=int, default=600)
    x.set_defaults(f=cmd_serve)
    x = s.add_parser("stop"); x.add_argument("names", nargs="+"); x.set_defaults(f=cmd_stop)
    x = s.add_parser("ask"); x.add_argument("url"); x.add_argument("namefile"); x.add_argument("out")
    x.add_argument("--workers", type=int, default=2); x.add_argument("--limit", type=int); x.add_argument("--timeout", type=int, default=120)
    x.set_defaults(f=cmd_ask)
    x = s.add_parser("compare"); x.add_argument("a"); x.add_argument("b"); x.add_argument("--changed-ids"); x.add_argument("--out")
    x.set_defaults(f=cmd_compare)
    x = s.add_parser("selftest"); x.set_defaults(f=cmd_selftest)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
