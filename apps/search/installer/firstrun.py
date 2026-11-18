#!/usr/bin/env python3
"""First-run readiness: the page opens only when this computer's engine can really answer it (owner 2026-11-17: "when the
installer is done it needs to be working correctly out of the gate: no slowness, no extra steps").

What `setup` used to do: wait for the engine's /readyz, then open search.logout.cloud. At that moment the page's way to the
engine did not exist yet: the engine's peer (`logout-search peer`, which carries the page's searches over WebRTC) is started by
`start` only after its three smoke searches and then needs seconds to load and sign in to the rendezvous, so the page opened,
found no engine, and showed the public site until a refresh; and the first searches ran on a cold engine.

`wait_first_run` waits, with honest progress lines, until all of these hold, then returns:
  1. /readyz answers 200 and, on an engine that reports it (release 2: "ready; warming N%"), the warm-up is over;
  2. the peer is signed in to the rendezvous AND a connection made the way the page makes it (rendezvous, offer, answer, data
     channel) answers a ping and a search ("link test");
  3. the first FIRST_SEARCHES searches over that link, timed, are quick (median under SLOW_MS): a cold engine still reading
     its files from disk answers in seconds, and the first searches of a new user are exactly those.
It gives up after its limits and says so: the page is then opened anyway, with the same hint, and keeps looking for the engine
(app.js), so a slow computer ends up working too, it just does not show "ready" first.

`link_test` is the page's connection made by this program (aiortc and websockets, both in the bundle); every part is
replaceable so the tests run a stand-in engine. Standard library only except those two, imported when the test runs.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

RV_URL = "wss://search.logout.cloud/rv"
STUN = "stun:search.logout.cloud:3478"
FIRST_SEARCHES = ("wikipedia", "weather", "history of the bicycle", "gmail login", "how to remove a stripped screw")
SLOW_MS = 2500                 # the median of the first searches must be under this
WARM_MAX_S = 240               # release 2's warm-up ends within 150 s; a little margin
TOTAL_MAX_S = 420              # everything: after this the page is opened anyway
PAGE_HINT = "#engine=1"        # what the page reads: "an engine is expected on this computer; wait for it, do not settle on public"


def readyz(port: int, timeout: float = 3.0):
    """(status, body) of the engine's /readyz; (0, "") when nothing answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=timeout) as r:
            return r.status, r.read(200).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return e.code, (e.read(200).decode("utf-8", "replace").strip() if e.fp else "")
    except Exception:  # noqa: BLE001
        return 0, ""


def warming(body: str):
    """N from "ready; warming N%", else None (an engine that does not say, or one that is warm)."""
    if "warming" not in body:
        return None
    digits = "".join(c for c in body.split("warming", 1)[1] if c.isdigit())
    return int(digits) if digits else 0


async def _link_test(rv_url: str, name: str, searches, timeout: float, stun=STUN) -> dict:
    import websockets
    try:
        import ice_addresses                               # the engine's own filter: real adapters only (WSL / Tailscale / link-local ones cost a 5 s STUN wait each)
        ice_addresses.install()
    except Exception:  # noqa: BLE001 - not on the path (a checkout's tests): aiortc's default
        pass
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    t0 = time.monotonic()
    out = {"ok": False, "steps": {}, "searches_ms": []}
    lap = lambda k: out["steps"].__setitem__(k, int((time.monotonic() - t0) * 1000))
    async with websockets.connect(rv_url, open_timeout=10, max_size=64 * 1024) as ws:
        lap("rendezvous open")
        await ws.send(json.dumps({"t": "find"}))
        found = json.loads(await asyncio.wait_for(ws.recv(), 5))
        lap("list")
        mine = [e for e in found.get("list") or [] if e.get("name") == name]
        if not mine:
            out["why"] = f"the rendezvous does not list {name!r} yet ({len(found.get('list') or [])} engine(s) on this network)"
            return out
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=stun)] if stun else []))
        try:
            dc = pc.createDataChannel("search")
            inbox, opened = asyncio.Queue(), asyncio.Event()
            dc.on("open", opened.set)
            dc.on("message", lambda m: inbox.put_nowait(json.loads(m)))
            await pc.setLocalDescription(await pc.createOffer())
            await ws.send(json.dumps({"t": "offer", "to": mine[0]["id"], "sdp": {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}}))
            ans = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            lap("answer")
            if not ans.get("sdp"):
                out["why"] = "the engine's peer refused: " + str(ans.get("error"))
                return out
            await pc.setRemoteDescription(RTCSessionDescription(**ans["sdp"]))
            await asyncio.wait_for(opened.wait(), timeout)
            lap("data channel open")
            for i, q in enumerate(("",) + tuple(searches)):
                t1 = time.monotonic()
                dc.send(json.dumps({"id": f"t{i}", "ping": True} if not q else {"id": f"t{i}", "q": q, "results": True, "full": True}))
                while True:
                    m = await asyncio.wait_for(inbox.get(), timeout)
                    if m.get("id") == f"t{i}":
                        break
                if q:
                    if m.get("error"):
                        out["why"] = f"a search over the link failed: {m['error']}"
                        return out
                    out["searches_ms"].append(int((time.monotonic() - t1) * 1000))
                else:
                    lap("ping")
            out["ok"] = True
            return out
        finally:
            try:                                           # a close that never returns held buildbox's setup for 12+ minutes (2026-11-17); wait_for cannot
                await asyncio.wait({asyncio.ensure_future(pc.close())}, timeout=5)      # bound it, asyncio.wait can (engine agent, buildbox)
            except Exception:  # noqa: BLE001
                pass


PAGE_SEEN_WAIT_S = 60          # after the page is opened: a message from it must reach the engine within this


def local_link(port: int, name: str, rv_url: str = RV_URL, searches=FIRST_SEARCHES, search_timeout: float = 20.0, fetch=None, rv_list=None) -> dict:
    """What must hold BEFORE the page is opened, without a WebRTC connection made by this program (it never returned on buildbox: the link
    test from the engine's own computer): the peer is signed in to the rendezvous under this computer's name, and the engine answers the
    first searches quickly over plain HTTP. -> {"ok", "why"?, "searches_ms"} (the shape wait_first_run wants)."""
    out = {"ok": False, "searches_ms": [], "steps": {}}
    try:
        names = (rv_list or _rv_names)(rv_url)
    except Exception as e:  # noqa: BLE001
        out["why"] = f"the rendezvous could not be asked ({type(e).__name__}: {str(e)[:100]})"
        return out
    if name not in names:
        out["why"] = f"the rendezvous does not list {name!r} yet ({len(names)} engine(s) on this network)"
        return out
    for q in searches:
        t1 = time.monotonic()
        try:
            if fetch:
                fetch(q)
            else:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/instant?results=1&full=1&q={urllib.parse.quote(q)}", timeout=search_timeout) as r:
                    r.read(2000000)
        except Exception as e:  # noqa: BLE001
            out["why"] = f"a search on the engine failed: {type(e).__name__}"
            return out
        out["searches_ms"].append(int((time.monotonic() - t1) * 1000))
    out["ok"] = True
    return out


def _rv_names(rv_url: str) -> list:
    """The names of the engines the rendezvous lists for this network (one websocket, one `find`; no WebRTC)."""
    import threading
    box: dict = {}

    async def go():
        import websockets
        async with websockets.connect(rv_url, open_timeout=10, max_size=64 * 1024) as ws:
            await ws.send(json.dumps({"t": "find"}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 8))
                if m.get("t") == "engines" or "list" in m:
                    return [e.get("name") for e in m.get("list") or []]

    def run():
        try:
            box["r"] = asyncio.run(go())
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(30)
    if "r" not in box:
        raise box.get("e") or TimeoutError("no answer")
    return box["r"]


def wait_page_seen(state_dir: str, since: float, wait_s: float = PAGE_SEEN_WAIT_S, clock=time.time, sleep=time.sleep) -> dict:
    """After the page was opened: has a REAL browser reached this engine? The peer writes state/page-seen.json when a message from a page
    arrives over its data channel. -> {"ok", "after_s"?, "via"?}; bounded by wait_s."""
    import os
    p = os.path.join(state_dir, "page-seen.json")
    t0 = clock()
    while True:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if float(d.get("at") or 0) >= since:
                return {"ok": True, "after_s": round(clock() - t0, 1), "via": d.get("via")}
        except (OSError, ValueError, TypeError):
            pass
        if clock() - t0 >= wait_s:
            return {"ok": False, "after_s": round(clock() - t0, 1)}
        sleep(1.0)


LINK_HARD_S = 60.0       # one link attempt never takes longer than this, whatever aiortc is doing


def link_test(name: str, rv_url: str = RV_URL, searches=FIRST_SEARCHES, timeout: float = 20.0, stun=STUN, hard: float = LINK_HARD_S) -> dict:
    """One connection, made the way the page makes it, then a ping and the searches. Never raises and never hangs: {"ok": False, "why"}.
    The attempt runs in its own thread with its own loop and is given up after `hard` seconds (the thread is a daemon: it cannot hold the
    process)."""
    import threading
    box: dict = {}

    def run():
        try:
            box["r"] = asyncio.run(_link_test(rv_url, name, searches, timeout, stun))
        except Exception as e:  # noqa: BLE001
            box["r"] = {"ok": False, "why": f"{type(e).__name__}: {str(e)[:160]}", "steps": {}, "searches_ms": []}

    th = threading.Thread(target=run, name="link-test", daemon=True)
    th.start()
    th.join(hard)
    if "r" not in box:
        return {"ok": False, "why": f"the link test did not finish in {int(hard)} s", "steps": {}, "searches_ms": []}
    return box["r"]


def wait_first_run(port: int, say, link, *, get_ready=readyz, clock=time.monotonic, sleep=time.sleep, total_max: float = TOTAL_MAX_S,
                   warm_max: float = WARM_MAX_S, slow_ms: int = SLOW_MS, progress=None) -> dict:
    """Wait until the page can open and be right (conditions in the module's text). link(): the link test's result.
    progress(text): where the same words go besides `say` (the tray's tip). -> {"ok", "why"?, "waited_s", "searches_ms"}."""
    t0 = clock()
    warm_from = None
    told = {}

    def tell(key, text, every=15.0):
        now = clock()
        if now - told.get(key, -1e9) >= every:
            told[key] = now
            say("     " + text)
            if progress:
                progress(text)

    last = {"searches_ms": []}
    while True:
        waited = clock() - t0
        if waited >= total_max:
            return {"ok": False, "why": f"not ready after {int(waited)} s: " + (last.get("why") or "slow searches"), "waited_s": int(waited),
                    "searches_ms": last.get("searches_ms", [])}
        code, body = get_ready(port)
        if code != 200:
            tell("start", f"starting your search engine… ({int(waited)} s; the first start reads the index into memory)")
            sleep(2.0)
            continue
        n = warming(body)
        if n is not None and n < 100:
            warm_from = warm_from if warm_from is not None else clock()
            if clock() - warm_from < warm_max:
                tell("warm", f"your engine is warming up: {n}%")
                sleep(2.0)
                continue
        last = link()
        if not last.get("ok"):
            tell("link", "joining the search page… " + (last.get("why") or "not yet"))
            sleep(3.0 if waited < 60 else 6.0)
            continue
        ms = last.get("searches_ms") or []
        med = statistics.median(ms) if ms else 0
        if med > slow_ms:
            tell("slow", f"the first searches take {med / 1000:.1f} s while the disk is read; waiting until they are quick")
            sleep(8.0)
            continue
        return {"ok": True, "waited_s": int(clock() - t0), "searches_ms": ms}
