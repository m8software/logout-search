"""The check types a case file can name (`"check": "<name>"`). Each takes (ctx, params) and returns a list of
(sub_id, status, evidence) where status is 'pass', 'fail', 'skip' or 'pending'. A check never raises for a bad
answer from the engine: that is a 'fail' with the evidence. Standard library only."""
import concurrent.futures as cf
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse

from rg_http import facts, host, normal, percentile

PASS, FAIL, SKIP, PENDING, FLAG = "pass", "fail", "skip", "pending", "flag"

NEWS_PATH = re.compile(
    r"/(19|20)\d\d/\d\d?/|\.(pdf|docx?|xlsx?|pptx?)(\?|$)|/(news|article|articles|press|archive|archives|magazine|bitstream|"
    r"mediacentre|entertainment|bbs|forum|search|recipe|guide|review|opinion|features|lifestyle)(/|$)|-news(/|$)|/how-to",
    re.I)
NEWS_HOST = re.compile(r"^(news|press|blog|archive)\.", re.I)


# ----------------------------------------------------------------------------- shared helpers

def names_from(ctx, p):
    """The names a case works on: inline `names`, or a file (`names_file`, one per line, # comments) under the repo."""
    if "names" in p:
        return list(p["names"])
    path = ctx.path(p["names_file"])
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if ln.strip() and not ln.startswith("#"):
                out.append(ln.split("\t")[-1] if p.get("tsv_last") else ln.strip())
    return out[: p.get("limit", 10**9)]


def read_expect(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.startswith("#") or not ln.strip():
                continue
            c = ln.rstrip("\n").split("\t")
            if len(c) >= 5:
                rows.append({"name": c[0], "card": c[1], "site": c[2], "first": c[3], "status": c[4]})
    return rows


def ask_many(ctx, names, workers=4, **extra):
    """name -> (answer|None, seconds, err), through the context's cache (a name is asked once per run)."""
    out = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {n: ex.submit(ctx.ask, n, **extra) for n in names}
        for n, f in futs.items():
            out[n] = f.result()
    return out


def official_of(d):
    card = (d or {}).get("card") or {}
    return next((l.get("url") for l in card.get("links", []) if l.get("title") == "Official site"), None)


def agg(sub, bad, total, what):
    """One result for a list: pass when `bad` (a list of 'name: evidence') is empty."""
    if not bad:
        return [(sub, PASS, f"{total} of {total} {what}")]
    return [(sub, FAIL, f"{len(bad)} of {total} {what} failed: " + "; ".join(bad[:12]) + (" ..." if len(bad) > 12 else ""))]


# ----------------------------------------------------------------------------- answers

def golden_rows(ctx, p):
    """Every enforced row of the golden file: the card title, the card's official-site host and the first result's host."""
    rows = [r for r in read_expect(ctx.path(p.get("expect", "apps/search/twin/golden/expect.tsv"))) if r["status"] == "ok"]
    got = ask_many(ctx, [r["name"] for r in rows])
    res = []
    bad = []
    for r in rows:
        d, secs, err = got[r["name"]]
        if d is None:
            res.append((r["name"], FAIL, f"no answer: {err}"))
            continue
        have = facts(d)
        want = (r["card"], r["site"], r["first"])
        res.append((r["name"], PASS if have == want else FAIL,
                    "" if have == want else f"expected card/site/first {want}, got {have}"))
    return res


def must_answer(ctx, p):
    """Every name of must.txt gets an answer: HTTP 200, JSON, and a card or a result."""
    names = names_from(ctx, p)
    got = ask_many(ctx, names)
    bad = []
    for n in names:
        d, secs, err = got[n]
        if d is None:
            bad.append(f"{n}: {err}")
        elif not d.get("results") and not d.get("card"):
            bad.append(f"{n}: empty answer")
    return agg("must.txt", bad, len(names), "names answered")


def bad_values(ctx, p):
    """Answers that were wrong and must not come back: field must not equal / contain the bad value."""
    res = []
    for it in p["items"]:
        d, secs, err = ctx.ask(it["name"])
        sub = it["name"] + (":" + it["field"])
        if d is None:
            res.append((sub, FAIL, f"no answer: {err}"))
            continue
        card_t, site_h, first_h = facts(d)
        vals = {"card": card_t, "official_host": site_h, "first_host": first_h,
                "first_url": ((d.get("results") or [{}])[0].get("url") or ""),
                "official_url": official_of(d) or ""}
        v = vals[it["field"]]
        bad = it["not"]
        hit = (v == bad) if it.get("op", "eq") == "eq" else (bad.lower() in v.lower())
        res.append((sub, FAIL if hit else PASS, f"{it['field']} is {v!r}: {it.get('why', 'the old wrong answer')}" if hit else f"{it['field']} = {v!r}"))
    return res


def expect_card(ctx, p):
    """Names that must have a card with a given title (or no card: title '-')."""
    res = []
    for it in p["items"]:
        d, secs, err = ctx.ask(it["name"])
        if d is None:
            res.append((it["name"], FAIL, f"no answer: {err}"))
            continue
        t, s, f = facts(d)
        ok = t == it["card"] and (not it.get("first") or f == it["first"])
        res.append((it["name"], PASS if ok else FAIL, "" if ok else f"expected card {it['card']!r}" + (f" first {it['first']!r}" if it.get('first') else "") + f", got card {t!r} first {f!r}"))
    return res


def official_not_news(ctx, p):
    """A card's official link is never a news article, archive, PDF, board post or magazine page."""
    names = names_from(ctx, p)
    got = ask_many(ctx, names)
    bad = []
    for n in names:
        d, secs, err = got[n]
        u = official_of(d) if d else None
        if u:
            sp = urllib.parse.urlsplit(u)
            if NEWS_PATH.search(sp.path or "") or NEWS_HOST.search(sp.hostname or ""):
                bad.append(f"{n}: {u}")
    return agg("official link", bad, len(names), "official links clean")


def description_whole(ctx, p):
    """The card's one-line description ends on a whole word (never cut at a character count), and the lead holds no raw wiki markup."""
    names = names_from(ctx, p)
    got = ask_many(ctx, names)
    cut, markup, leadcut = [], [], []
    for n in names:
        d, secs, err = got[n]
        c = (d or {}).get("card") or {}
        desc = (c.get("description") or "").strip()
        lead = c.get("lead") or ""
        if desc:
            # the release stores a description cut at 60 characters (DESCR_STORED_CUT): a description of exactly that
            # length that does not end in punctuation is the stored cut; one over 120 must end with the ellipsis
            if len(desc) == 60 and desc[-1].isalnum():
                cut.append(f"{n}: exactly 60 characters, ...{desc[-25:]!r}")
            elif len(desc) > 121 and not desc.endswith("…"):
                cut.append(f"{n}: {len(desc)} characters without the ellipsis")
            if re.search(r"(,|-|\b(and|of|the|a|an|in|for|with|by|to)$)\s*$", desc, re.I):
                cut.append(f"{n}: ends with {desc[-12:]!r}")
        if len(lead) >= 280 and lead[-1].isalnum() and not lead.endswith("…"):
            leadcut.append(f"{n}: ...{lead[-25:]!r}")
        if re.search(r"\[\[|\{\{|<ref|\}\}", lead + desc):
            markup.append(f"{n}: {re.search(r'.{0,12}(\[\[|\{\{|<ref).{0,12}', lead + desc).group(0)!r}" if re.search(r'.{0,12}(\[\[|\{\{|<ref).{0,12}', lead + desc) else n)
    return (agg("description", cut, len(names), "descriptions end on a whole word") + agg("lead cut", leadcut, len(names), "leads end on a whole word (the engine caps a lead at 300 characters)")
            + agg("lead markup", markup, len(names), "leads free of wiki markup"))


def suggest_lead(ctx, p):
    """Typing suggestions: the first thing the page shows for a prefix is the expected entity; phrases are never first."""
    res = []
    for it in p["items"]:
        d, secs, err = ctx.t.suggest(it["prefix"])
        if d is None:
            res.append((it["prefix"], FAIL, f"no answer: {err}"))
            continue
        ents = d.get("suggest") or []
        phrases = [{"phrase": x} for x in d.get("phrases") or []]
        lead = min(int(d.get("suggest_lead") or 0), len(ents))
        order = ents[:lead] + phrases + ents[lead:]
        first = order[0] if order else None
        title = (first or {}).get("title") or (first or {}).get("phrase") or ""
        ok_first = bool(first) and it["expect"].lower() in title.lower()
        phrase_first = bool(first) and first.get("phrase") is not None and bool(ents)
        shown = [(o.get("title") or o.get("phrase")) for o in order[:4]]
        if phrase_first:
            res.append((it["prefix"], FAIL, f"a phrase is first although entities exist: {shown}"))
        else:
            res.append((it["prefix"], PASS if ok_first else FAIL, f"expected first ~{it['expect']!r}, page shows {shown}"))
    return res


def answer_and_list(ctx, p):
    """A question answered by a person or thing: the answer box has it, and the result list is about that answer."""
    d, secs, err = ctx.ask(p["name"])
    if d is None:
        return [("answer", FAIL, f"no answer: {err}")]
    a = d.get("answer") or {}
    box = json.dumps(a, ensure_ascii=False)
    res = [("answer box", PASS if p["expect"].lower() in box.lower() else FAIL, f"answer box: {box[:160]}")]
    if p.get("part") == "box":
        return res
    top = (d.get("results") or [])[:p.get("top", 3)]
    hit = [r.get("url") for r in top if p["expect"].lower() in json.dumps(r, ensure_ascii=False).lower()]
    res.append(("result list", PASS if hit else FAIL, f"top {len(top)} results mentioning {p['expect']!r}: {hit or 'none'}; first: {[r.get('url') for r in top]}"))
    return res[1:] if p.get("part") == "list" else res


def parallel_serial(ctx, p):
    """: an answer is the same whether asked alone or among others (8 at a time). Live fields set aside."""
    names = names_from(ctx, p)
    serial = {}
    for n in names:
        d, secs, err = ctx.t.instant(n)
        serial[n] = normal(d) if d else {"error": err}
    par = {}
    with cf.ThreadPoolExecutor(max_workers=p.get("workers", 8)) as ex:
        futs = {n: ex.submit(ctx.t.instant, n) for n in names}
        for n, f in futs.items():
            d, secs, err = f.result()
            par[n] = normal(d) if d else {"error": err}
    bad = []
    for n in names:
        if serial[n] != par[n]:
            diff = [k for k in set(serial[n]) | set(par[n]) if serial[n].get(k) != par[n].get(k)]
            bad.append(f"{n}: differs in {diff}")
    return agg("parallel==serial", bad, len(names), "names identical alone and in parallel")


# ----------------------------------------------------------------------------- engine contract, stored answers

def endpoints(ctx, p):
    res = []
    gw = bool(ctx.tags & set(p.get("gateway_tags", [])))     # a public gateway exposes only /api/instant, not the engine's probes
    st, body, secs, err = ctx.t.raw("/healthz", timeout=20)
    res.append(("healthz", SKIP if gw and st == 404 else PASS if st == 200 else FAIL, f"HTTP {st} {body[:40]!r} {err}"))
    st, text = ctx.t.readyz()
    res.append(("readyz", SKIP if gw and st == 404 else PASS if st == 200 and text.startswith("ready") else FAIL, f"HTTP {st} {text!r}"))
    d, secs, err = ctx.ask("wikipedia")
    ready_ok = d is not None
    # truthful: ready means searches answer
    res.append(("readyz truthful", SKIP if gw and st == 404 else PASS if (st == 200) == ready_ok else FAIL, f"/readyz {st}, a search {'answered' if ready_ok else 'did not: ' + err}"))
    z = ctx.t.storez()
    if z is None:
        res.append(("storez", SKIP, "/storez is not served by this target (the engine's back network only)"))
    else:
        need = {"fp", "rows", "hit", "miss", "finished_hit"}
        res.append(("storez", PASS if need <= set(z) and not z.get("off") else FAIL, f"keys {sorted(z)[:12]}{' STORE OFF' if z.get('off') else ''}"))
    return res


def stored_served(ctx, p):
    """The showcase names are served as stored answers (the engine log's cache=fin): /storez finished_hit rises by one per name."""
    z0 = ctx.t.storez()
    if z0 is None:
        return [("finished_hit", SKIP, "/storez is not served by this target")]
    names = names_from(ctx, p)
    for n in names:
        ctx.t.instant(n, **p.get("extra", {}))
    z1 = ctx.t.storez() or {}
    got = z1.get("finished_hit", 0) - z0.get("finished_hit", 0)
    need = int(len(names) * p.get("min_share", 0.8))
    return [("finished_hit", PASS if got >= need else FAIL,
             f"{got} of {len(names)} showcase searches served from stored answers (need {need}); curated {z1.get('curated')}, rows {z1.get('rows')}, hit {z1.get('hit')}->{z1.get('hit')}, miss {z0.get('miss')}->{z1.get('miss')}")]


def store_on(ctx, p):
    """Stored answers and curated lists are on and loaded (never switched off by a slow take-in)."""
    z = ctx.t.storez()
    if z is None:
        return [("store", SKIP, "/storez is not served by this target")]
    cur = sum((z.get("curated") or {}).values()) if isinstance(z.get("curated"), dict) else 0
    return [("store open", PASS if not z.get("off") else FAIL, f"fp {z.get('fp')} up_s {z.get('up_s')}"),
            ("curated lists loaded", PASS if cur > 0 else FAIL, f"{cur} curated rows")]


# ----------------------------------------------------------------------------- speed and stability

def latency_budget(ctx, p):
    """Median and p95 of each kind of search against a budget. `kinds`: {kind: {"names":[...] | names_file, "median_ms", "p95_ms", "max_ms"}}."""
    res = []
    for kind, k in p["kinds"].items():
        names = names_from(ctx, k)
        t = []
        errs = 0
        for _ in range(k.get("runs", 3)):
            for n in names:
                d, secs, err = ctx.t.instant(n)
                if d is None:
                    errs += 1
                t.append(secs * 1000)
        med, p95, mx = percentile(t, 0.5), percentile(t, 0.95), max(t) if t else 0
        bad = []
        if errs:
            bad.append(f"{errs} failed requests")
        if med > k["median_ms"]:
            bad.append(f"median {med:.0f} > {k['median_ms']}")
        if p95 > k["p95_ms"]:
            bad.append(f"p95 {p95:.0f} > {k['p95_ms']}")
        if k.get("max_ms") and mx > k["max_ms"]:
            bad.append(f"max {mx:.0f} > {k['max_ms']}")
        res.append((kind, FAIL if bad else PASS, f"{len(t)} requests: median {med:.0f} ms, p95 {p95:.0f} ms, max {mx:.0f} ms" + (" -- " + "; ".join(bad) if bad else "")))
    return res


def soak(ctx, p):
    """One search a second for `seconds` (more with --soak): no failed request, no stall above the bound."""
    secs_total = 3 if ctx.quick else max(p.get("seconds", 30), ctx.soak_s)
    names = names_from(ctx, p)
    t_end = time.monotonic() + secs_total
    lat, errs, i = [], [], 0
    while time.monotonic() < t_end:
        n = names[i % len(names)]
        i += 1
        t0 = time.monotonic()
        d, secs, err = ctx.t.instant(n)
        lat.append(secs * 1000)
        if d is None:
            errs.append(f"{n}: {err}")
        time.sleep(max(0, 1 - (time.monotonic() - t0)))
    mx, p99 = max(lat), percentile(lat, 0.99)
    bad = []
    if errs:
        bad.append(f"{len(errs)} failed: {errs[:3]}")
    if mx > p["max_ms"]:
        bad.append(f"slowest {mx:.0f} ms > {p['max_ms']}")
    return [("soak", FAIL if bad else PASS, f"{len(lat)} searches in {secs_total}s, p99 {p99:.0f} ms, max {mx:.0f} ms" + (" -- " + "; ".join(bad) if bad else ""))]


def rss(ctx, p):
    """Engine anonymous memory after N searches stays under a bound (a 4 GB machine), and does not grow across the run."""
    pid = ctx.pid
    if not pid or not sys.platform.startswith("linux"):
        return [("rss", SKIP, "needs --pid of the engine on Linux (reads /proc/<pid>/status)")]

    def anon():
        for ln in open(f"/proc/{pid}/status"):
            if ln.startswith("RssAnon:"):
                return int(ln.split()[1]) / 1024
        return 0.0
    names = names_from(ctx, p)
    n_total = 100 if ctx.quick else (p["searches"] if ctx.long else p.get("searches_short", 300))
    marks, i = {}, 0
    start = anon()
    with cf.ThreadPoolExecutor(max_workers=p.get("workers", 3)) as ex:
        while i < n_total:
            batch = [names[(i + j) % len(names)] for j in range(min(100, n_total - i))]
            list(ex.map(lambda q: ctx.t.instant(q), batch))
            i += len(batch)
            if i in (n_total // 5 // 100 * 100 or 100, n_total // 2 // 100 * 100 or 100):
                marks[i] = anon()
    end = anon()
    first_mark = marks[min(marks)] if marks else start
    bad = []
    if end > p["max_mb"]:
        bad.append(f"RssAnon {end:.0f} MB > {p['max_mb']}")
    if end > first_mark * (1 + p.get("growth", 0.15)) + 50:
        bad.append(f"grew from {first_mark:.0f} to {end:.0f} MB across the run")
    return [("rss", FAIL if bad else PASS, f"RssAnon {start:.0f} -> {end:.0f} MB after {n_total} searches (mid-run {marks}); bound {p['max_mb']} MB" + (" -- " + "; ".join(bad) if bad else ""))]


def disk_bound(ctx, p):
    if not ctx.install_dir:
        return [("disk", SKIP, "needs --install-dir")]
    total = 0
    for root, dirs, files in os.walk(ctx.install_dir):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    gb = total / 2**30
    return [("disk", PASS if gb <= p["max_gb"] else FAIL, f"{gb:.1f} GB under {ctx.install_dir} (bound {p['max_gb']} GB)")]


# ----------------------------------------------------------------------------- repo-level guards (existing unit tests)

def script(ctx, p):
    """A repo unit test that guards the same defect. Needs the checkout (--repo). Pass = exit 0; SKIP when the script says SKIP or is absent."""
    if not ctx.repo:
        return [("script", SKIP, "needs --repo (a checkout of the repository)")]
    out = []
    for cmd in p["commands"]:
        path = cmd.split()[1] if cmd.startswith("bash ") else cmd.split()[0]
        if not os.path.exists(os.path.join(ctx.repo, path)):
            out.append((path, SKIP, f"{path} does not exist in this checkout"))
            continue
        env = dict(os.environ, PY=ctx.py)
        try:
            r = subprocess.run(cmd, shell=True, cwd=ctx.repo, capture_output=True, text=True, timeout=p.get("timeout", 600), env=env)
        except subprocess.TimeoutExpired:
            out.append((path, FAIL, f"timed out after {p.get('timeout', 600)} s"))
            continue
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-4:])
        text = r.stdout + r.stderr
        first = (r.stdout.strip().splitlines() or [""])[0]
        if (r.returncode == 0 and re.match(r"\s*SKIP", first)) or "SKIPPED (NOT RUN)" in text:
            out.append((path, SKIP, tail))
        else:
            out.append((path, PASS if r.returncode == 0 else FAIL, tail))
    return out


def browser_first_run(ctx, p):
    """First run, in a headless browser: after setup the search page shows the engine pill and a search is answered by the
    engine with zero requests to the public site's /api/instant (browser/first_run.mjs; needs node + playwright-core + Chrome)."""
    if not ctx.browser_page:
        return [("first run", SKIP, "needs --browser-page URL (the search page; with the install's engine on this computer or network)")]
    cmd = ["node", os.path.join(ctx.root, "tests", "regress", "browser", "first_run.mjs"), "--page", ctx.browser_page,
           "--query", p.get("query", "gmail"), "--expect-card", p.get("expect_card", "Gmail")]
    if ctx.chrome:
        cmd += ["--chrome", ctx.chrome]
    env = dict(os.environ, **({"NODE_PATH": ctx.node_path} if ctx.node_path else {}))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    except FileNotFoundError:
        return [("first run", SKIP, "node is not installed")]
    except subprocess.TimeoutExpired:
        return [("first run", FAIL, "the browser case took more than 180 s")]
    line = (r.stdout.strip().splitlines() or [""])[-1]
    if r.returncode == 77:
        return [("first run", SKIP, line[:200])]
    return [("first run", PASS if r.returncode == 0 else FAIL, line[:600] or r.stderr[-300:])]


def pending(ctx, p):
    """Written and reasoned, not yet runnable: says why. Not counted as a pass."""
    return [("pending", PENDING, p["why"])]


# ----------------------------------------------------------------------------- the link (public rendezvous)

def stun_rtt(ctx, p):
    """A STUN binding request over UDP: the public server answers, fast (the engine's candidate gathering waits for it)."""
    hp = p.get("server", "search.logout.cloud:3478")
    h, port = hp.rsplit(":", 1)
    tid = os.urandom(12)
    req = struct.pack("!HHI", 0x0001, 0, 0x2112A442) + tid
    best = None
    for fam in (socket.AF_INET,):
        try:
            addr = socket.getaddrinfo(h, int(port), fam, socket.SOCK_DGRAM)[0][4]
        except OSError as e:
            return [("stun", FAIL, f"cannot resolve {hp}: {e}")]
        s = socket.socket(fam, socket.SOCK_DGRAM)
        s.settimeout(3)
        t0 = time.monotonic()
        try:
            s.sendto(req, addr)
            data, _ = s.recvfrom(2048)
            best = (time.monotonic() - t0) * 1000
            ok = data[:2] == b"\x01\x01" and data[8:20] == tid
        except OSError as e:
            return [("stun", FAIL, f"no answer from {hp} in 3 s: {e}")]
        finally:
            s.close()
    lim = p.get("max_ms", 500)
    return [("stun", PASS if ok and best <= lim else FAIL, f"binding success in {best:.0f} ms (limit {lim}) from {addr[0]}")]


def rv_handshake(ctx, p):
    """The rendezvous WebSocket accepts a connection (HTTP 101) quickly. Needs --rv."""
    if not ctx.rv:
        return [("rv", SKIP, "needs --rv wss://host/rv")]
    u = urllib.parse.urlsplit(ctx.rv)
    port = u.port or (443 if u.scheme == "wss" else 80)
    t0 = time.monotonic()
    try:
        raw = socket.create_connection((u.hostname, port), timeout=10)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        s.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = s.recv(512).split(b"\r\n")[0].decode("latin1")
        s.close()
    except OSError as e:
        return [("rv", FAIL, f"cannot connect to {ctx.rv}: {e}")]
    ms = (time.monotonic() - t0) * 1000
    return [("rv", PASS if " 101 " in head and ms <= p.get("max_ms", 3000) else FAIL, f"{head!r} in {ms:.0f} ms")]


def matrix(ctx, p):
    """One type of the matrix group (rg_matrix): answered, no errors, the speed flags, and the baseline once there is one."""
    import rg_matrix as M
    typ = p["type"]
    rows = M.run_type(ctx.t, ctx.root, typ, n=ctx.matrix_n, modes=ctx.matrix_modes, workers=ctx.matrix_workers, typing_sample=ctx.matrix_typing)
    ctx.matrix_rows += rows
    o = M.summarize(rows)[typ]
    res = []
    info = (f"{o['queries']} queries, {o['requests']} requests; card {o['card_rate']}%, first result right {o['right_rate']}% of {o['right_n']}; "
            f"cache {o['cache']}; cold engine p50/p90/p99/max {M.fmt(o['cold']['engine'])}, repeat {M.fmt(o['repeat']['engine'])}, typing {M.fmt(o['typing']['engine'])}, parallel {M.fmt(o['parallel']['engine'])}")
    res.append(("answered", PASS if o["answered"] >= 99 else FAIL, f"{o['answered']}% answered. " + info))
    bad = [f"{r['q']!r}: {r['err'][:60]}" for r in rows if not r["ok"]][:5]
    res.append(("errors", PASS if o["errors"] == 0 else FAIL, f"{o['errors']} failed requests {bad}"))
    c, r = o["cold"]["engine"], o["repeat"]["engine"]
    if c:
        slow = sorted(((x.get("ms") or 0, x["q"]) for x in rows if x["mode"] == "cold"), reverse=True)[:3]
        res.append(("cold speed", PASS if c["p90"] <= M.COLD_MAX_MS else (FAIL if ctx.matrix_baseline else FLAG), f"cold p90 {c['p90']} ms (flag over {M.COLD_MAX_MS}); {o['cold_over_2s']} queries over; slowest {slow}"))
    if r:
        slow = sorted(((x.get("ms") or 0, x["q"]) for x in rows if x["mode"] == "repeat"), reverse=True)[:3]
        res.append(("repeat speed", PASS if r["p90"] <= M.REPEAT_MAX_MS else (FAIL if ctx.matrix_baseline else FLAG), f"repeat p90 {r['p90']} ms (flag over {M.REPEAT_MAX_MS}); {o['repeat_over_100ms']} requests over; slowest {slow}"))
    if ctx.matrix_baseline:
        base = json.load(open(ctx.matrix_baseline)).get(ctx.platform_key(), {}).get(typ)
        if base and c:
            lim = base["cold_p90"] * 1.5 + 50
            res.append(("baseline", PASS if c["p90"] <= lim else FAIL, f"cold p90 {c['p90']} ms against baseline {base['cold_p90']} x 1.5 + 50 = {lim:.0f}"))
    return res


CHECKS = {f.__name__: f for f in (browser_first_run, matrix, 
    golden_rows, must_answer, bad_values, expect_card, official_not_news, description_whole, suggest_lead, answer_and_list,
    parallel_serial, endpoints, stored_served, store_on, latency_budget, soak, rss, disk_bound, script, pending, stun_rtt,
    rv_handshake)}


def compare_targets(ta, tb, names, workers=3):
    """-> (identical count, [diff dicts], [(name, why)] unanswered) for the same searches on two targets."""
    def one(n):
        da, _, ea = ta.instant(n)
        db, _, eb = tb.instant(n)
        if da is None or db is None:
            return n, None, f"A: {ea or 'ok'}; B: {eb or 'ok'}"
        na, nb = normal(da), normal(db)
        return n, [k for k in na if na[k] != nb[k]], (na, nb)
    diffs, errs, same = [], [], 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for n, d, ev in ex.map(one, names):
            if d is None:
                errs.append((n, ev))
            elif d:
                diffs.append(dict(name=n, fields=d, a={k: ev[0][k] for k in d}, b={k: ev[1][k] for k in d}))
            else:
                same += 1
    return same, diffs, errs


def parity_with(ctx, p):
    """The same searches on this target and the --compare target: identical cards and result lists (live fields set aside)."""
    from rg_http import Target
    if not ctx.compare:
        return [("parity", SKIP, "needs --compare URL (the other platform's engine)")]
    tb = Target(ctx.compare, timeout=ctx.t.timeout)
    if not tb.detect():
        return [("parity", FAIL, f"{ctx.compare} does not answer")]
    names = list(dict.fromkeys(n for n in names_from(ctx, dict(p, limit=10**6)) if n))[: p.get("limit", 300)]
    same, diffs, errs = compare_targets(ctx.t, tb, names)
    fields = {}
    for d in diffs:
        for f in d["fields"]:
            fields[f] = fields.get(f, 0) + 1
    ev = f"{len(names)} searches: {same} identical, {len(diffs)} differ {fields}, {len(errs)} unanswered by one side. " + \
         "; ".join(f"{d['name']!r}: {d['fields']}" for d in diffs[:6])
    ctx.parity_diffs = diffs
    return [("parity", FAIL if diffs or errs else PASS, ev)]


CHECKS["parity_with"] = parity_with
try:
    import rg_life
    CHECKS.update(rg_life.CHECKS)
except ImportError:
    pass
