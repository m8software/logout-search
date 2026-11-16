#!/usr/bin/env bash
set -euo pipefail
################################################################################
# Unit test — engine-side pairing (apps/search/serve/api/rtc_peer.py)
################################################################################
# The REAL module with a temp RTC_STATE and a fake rendezvous socket:
#   1. first use creates a room; devices persist (0600) and survive a reload
#   2. the HMAC binds the device secret to the SDP's DTLS fingerprints: a swapped
#      fingerprint (a man in the middle) or another device's secret does not verify
#   3. a paired ("room") offer with a bad MAC, an unknown or a revoked device is refused
#      with "not paired" — no peer connection is created
#   4. {pair: true} over the data channel works only on a same-network connection
#   5. pair links round-trip (base64url JSON after #pair=)
#   6. browsers' ".local" candidates are dropped, everything else kept
#   8.: a paired device's signed offer gets a signed answer on the home network too; an unsigned or
#      badly signed home offer is answered unsigned (old pages, unpaired devices), never signed; "room"
#      still refuses those
#   7.: {info} carries no name / LAN address / index size; {details} does, only when asked
# Needs: python3 with aiortc + websockets (skips when missing)
# Usage: bash tests/unit/test-search-rtc-pairing.sh
################################################################################
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${RTC_TEST_PYTHON:-python3}"
if ! "$PY" -c 'import aiortc, websockets' 2>/dev/null; then echo "SKIP: aiortc/websockets not installed for $PY"; exit 0; fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
API="${REPO}/apps/search/serve/api" RTC_STATE="${TMP}/state/engine.json" LOGOUT_ENGINE_NAME=testbox "$PY" - <<'PY'
import asyncio, base64, json, os, stat, sys
sys.path.insert(0, os.environ["API"])
import rtc_peer as rp
rp.MAX_PER_SOURCE = rp.MAX_UNPAIRED = rp.MAX_PEERS = 10 ** 6      # RT-07's caps (3 per source, 4 unpaired, 16 in all) are tested in test-search-audit-rtc.sh; these cases leave their offers open
fails = 0
def check(name, ok, detail=""):
    global fails
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += 0 if ok else 1

st = rp.load_state()
check("room created on first use", len(st["room"]) >= 22 and st["devices"] == {}, st)
p = rp.add_device("Pixel")
st2 = rp.load_state()
check("device persisted", p["d"] in st2["devices"] and st2["devices"][p["d"]]["secret"] == p["s"] and p["r"] == st["room"], st2)
check("state file is 0600", stat.S_IMODE(os.stat(rp.STATE).st_mode) == 0o600, oct(os.stat(rp.STATE).st_mode))

offer = "v=0\r\na=fingerprint:sha-256 AA:BB:CC\r\na=candidate:1 1 udp 1 10.0.0.2 5000 typ host\r\n"
evil = offer.replace("AA:BB:CC", "EE:VV:II")
good = rp.mac(p["s"], "offer", offer)
check("same secret + same fingerprints verify", good == rp.mac(p["s"], "offer", offer.replace("10.0.0.2", "10.9.9.9")))
check("swapped DTLS fingerprint does not verify", good != rp.mac(p["s"], "offer", evil))
check("another secret does not verify", good != rp.mac("other", "offer", offer))
check("offer MAC is not valid as an answer MAC", good != rp.mac(p["s"], "answer", offer))

class FakeWS:
    def __init__(self): self.sent = []
    async def send(self, raw): self.sent.append(json.loads(raw))
async def offer_with(auth):
    ws = FakeWS(); before = len(rp._pcs)
    await rp.on_offer(ws, {"from": "p9", "via": "room", "sdp": {"type": "offer", "sdp": offer}, "auth": auth})
    return ws.sent, len(rp._pcs) - before
sent, made = asyncio.run(offer_with({"device": p["d"], "mac": "0" * 64}))
check("bad MAC refused, no peer connection", sent == [{"t": "answer", "to": "p9", "error": "not paired"}] and made == 0, (sent, made))
sent, made = asyncio.run(offer_with({"device": "nosuch", "mac": good}))
check("unknown device refused", sent and sent[0].get("error") == "not paired" and made == 0, sent)
rc = rp.cli(["--revoke", p["d"]])
sent, made = asyncio.run(offer_with({"device": p["d"], "mac": good}))
check("revoked device refused", rc == 0 and sent and sent[0].get("error") == "not paired" and made == 0, (rc, sent))

class FakeChannel:
    readyState = "open"
    def __init__(self): self.out = []
    def send(self, raw): self.out.append(json.loads(raw))
async def pair_via(via, pin=None, lan=None):
    msg = {"id": "x", "pair": True, "device": "iPhone Safari"}
    if pin is not None: msg["pin"] = pin
    ch = FakeChannel(); await rp.reply(ch, json.dumps(msg), via, lan); return ch.out[0]
def devices(): return len(rp.load_state()["devices"])
r = asyncio.run(pair_via("room"))
check("pairing refused over a paired remote connection", r.get("error") and "pair" not in r, r)

# --- 2026-11-13: an engine nobody has paired with gives its first device a pairing with no PIN; after that, a PIN
check("the engine starts unclaimed here (its one device was revoked)", devices() == 0 and not rp.claimed())
r = asyncio.run(pair_via("room"))
check("unclaimed: a remote connection still pairs nothing", "pair" not in r and devices() == 0, r)
r = asyncio.run(pair_via("network"))
check("unclaimed, but the far end is not known to be on the LAN: a code is asked for", "pair" not in r and r.get("need") == "pin" and devices() == 0, r)
r = asyncio.run(pair_via("network", lan=lambda: False))
check("unclaimed, same public address but not the LAN (carrier NAT): a code is asked for", "pair" not in r and r.get("need") == "pin" and devices() == 0, r)
lan_is = [rp.is_lan_addr(a) for a in ("192.168.1.7", "10.0.0.108", "172.20.0.3", "fe80::1%eth0", "fd12::5", "100.72.1.1", "127.0.0.1", "203.0.113.9", "2001:db8::1", "x", None)]
check("LAN addresses: private and link-local are; carrier NAT, loopback and public are not", lan_is == [True] * 5 + [False] * 6, lan_is)
check("a connection whose ICE pair cannot be read is not the LAN", rp.peer_on_lan(object()) is False)
r = asyncio.run(pair_via("network", lan=lambda: True))
check("unclaimed: the first device on the LAN is paired with no PIN", r.get("first") is True and r.get("pair", {}).get("d") in rp.load_state()["devices"] and rp.claimed(), r)
owner = r["pair"]

# --- 2026-11-13: later devices pair with the button alone from the LAN; each is marked new until another device saw it
r = asyncio.run(pair_via("network", lan=lambda: True))
check("a later device on the LAN is paired by the button alone, and is not the first", "pair" in r and r.get("first") is False and rp.load_state()["devices"][r["pair"]["d"]].get("new") is True, r)
guest = r["pair"]
r = asyncio.run(pair_via("network", lan=lambda: False))
check("same public address but not the LAN: the button alone pairs nothing", "pair" not in r and r.get("need") == "pin", r)
import hashlib as _h0, hmac as _hm0
def op0(body, dev, secret):
    raw = json.dumps(body)
    ch = FakeChannel(); asyncio.run(rp.reply(ch, json.dumps({"id": "o", "op_json": raw, "auth": {"device": dev, "mac": _hm0.new(secret.encode(), f"op|{raw}".encode(), _h0.sha256).hexdigest()}}), "room")); return ch.out[0]
lst = op0({"op": "devices"}, owner["d"], owner["s"])
check("the owner's device is told of the new one, and that pairing is not locked", lst.get("locked") is False and [d["new"] for d in lst["devices"] if d["id"] == guest["d"]] == [True], lst)
check("the new device is not new to itself", [d["new"] for d in op0({"op": "devices"}, guest["d"], guest["s"])["devices"] if d["me"]] == [False])
op0({"op": "devices", "seen": True}, guest["d"], guest["s"])
check("its own look does not clear the mark", rp.load_state()["devices"][guest["d"]].get("new") is True)
op0({"op": "devices", "seen": True}, owner["d"], owner["s"])
check("another device having been shown it does", "new" not in rp.load_state()["devices"][guest["d"]])
check("lock: a paired device locks pairing", op0({"op": "lock", "on": True}, owner["d"], owner["s"]) == {"locked": True, "id": "o"} and rp.load_state()["locked"] is True)
r = asyncio.run(pair_via("network", lan=lambda: True))
check("locked: the button alone pairs nothing even from the LAN", "pair" not in r and r.get("need") == "pin", r)
op0({"op": "revoke", "device": guest["d"]}, owner["d"], owner["s"])

# ---: the button needs a PIN the engine's owner asked for (pairing locked, or not from the LAN)
n0 = devices()
r = asyncio.run(pair_via("network"))
check("no PIN waiting: the button alone pairs nothing, and the page is told to ask for one", "pair" not in r and r.get("need") == "pin" and "--pin" in r.get("error", "") and devices() == n0, r)
r = asyncio.run(pair_via("network", "123456"))
check("a PIN typed when none is waiting pairs nothing", "pair" not in r and r.get("need") == "pin" and devices() == n0, r)
pin = rp.new_pin()
stp = rp.load_state()["pin"]
check("the PIN is six digits and only its salted hash is stored", len(pin) == 6 and pin.isdigit() and pin not in json.dumps(stp) and set(stp) == {"salt", "hash", "expires", "tries"}, stp)
r = asyncio.run(pair_via("network"))
check("asking without a PIN is not a guess", "pair" not in r and r.get("need") == "pin" and rp.load_state()["pin"]["tries"] == 0, r)
wrong = "000000" if pin != "000000" else "111111"
r = asyncio.run(pair_via("network", wrong))
check("a wrong PIN pairs nothing and counts", r.get("error") == "wrong PIN" and r.get("need") == "pin" and rp.load_state()["pin"]["tries"] == 1 and devices() == n0, r)
r = asyncio.run(pair_via("room", pin))
check("the right PIN over a remote connection is still refused, and not used up", "pair" not in r and "pin" in rp.load_state(), r)
r = asyncio.run(pair_via("network", " " + pin + " "))
check("pairing works on the same network with the PIN", r.get("pair", {}).get("r") == st["room"] and r["pair"]["d"] in rp.load_state()["devices"] and devices() == n0 + 1, r)
paired = r
check("the PIN is used up by one pairing", "pin" not in rp.load_state() and asyncio.run(pair_via("network", pin)).get("need") == "pin" and devices() == n0 + 1)
pin = rp.new_pin()
for i in range(rp.PIN_TRIES - 1):
    asyncio.run(pair_via("network", wrong if wrong != pin else "222222"))
check("the PIN survives PIN_TRIES - 1 wrong guesses", rp.load_state().get("pin", {}).get("tries") == rp.PIN_TRIES - 1)
r = asyncio.run(pair_via("network", wrong if wrong != pin else "222222"))
check("the last wrong guess throws the PIN away", "too many tries" in r.get("error", "") and "pin" not in rp.load_state(), r)
r = asyncio.run(pair_via("network", pin))
check("...and then even the right PIN pairs nothing", "pair" not in r and devices() == n0 + 1, r)
import time as _t
pin = rp.new_pin(now=_t.time() - rp.PIN_TTL_S - 1)
r = asyncio.run(pair_via("network", pin))
check("an expired PIN pairs nothing and is removed", "pair" not in r and r.get("need") == "pin" and "pin" not in rp.load_state() and devices() == n0 + 1, r)
check("check_pin answers None only for the waiting PIN", (lambda p: (rp.check_pin("", None) is not None, rp.check_pin(p) is None, rp.check_pin(p) is not None))(rp.new_pin()) == (True, True, True))
p1 = rp.new_pin(); p2 = rp.new_pin()
check("a new PIN replaces the old one", rp.check_pin(p1) is not None if p1 != p2 else True)
rp.load_state()   # leave no PIN behind for the checks below
_st = rp.load_state(); _st.pop("pin", None); rp.save_state(_st)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rc_pin = rp.cli(["--pin"])
shown = buf.getvalue().split()[1] if buf.getvalue().startswith("PIN ") else ""
check("--pin prints a PIN that then pairs", rc_pin == 0 and len(shown) == 6 and "pair" in asyncio.run(pair_via("network", shown)), buf.getvalue())

# ---: a channel cannot send without limit
b = rp.Bucket(rate=10, burst=5, now=100.0)
first = [b.allow(100.0) for _ in range(7)]
check("a burst passes, the rest are refused", first == [True] * 5 + [False] * 2, first)
check("tokens come back at the rate", [b.allow(100.125), b.allow(100.125), b.allow(100.375), b.allow(100.375), b.allow(100.375)] == [True, False, True, True, False])
check("idle time never saves up more than the burst", [b.allow(500.0)] + [b.allow(500.0) for _ in range(5)] == [True] * 5 + [False])
check("the defaults let a person type and stop a script", rp.RATE_PER_S == 20 and rp.RATE_BURST == 40)
check("the refusal carries the request's id", rp.too_many(json.dumps({"id": "q7", "q": "x"})) == {"id": "q7", "error": "too many requests"} and rp.too_many("not json") == {"id": None, "error": "too many requests"})
src = open(os.environ["API"] + "/rtc_peer.py").read()
check("every channel message goes through its bucket", "if bucket.allow():\n                asyncio.ensure_future(reply(channel, raw, via, lambda: peer_on_lan(pc), conn))" in src and src.count("asyncio.ensure_future(reply(") == 1, "")
r = paired

async def settings_via(via):
    ch = FakeChannel(); await rp.reply(ch, json.dumps({"id": "s", "set_settings": {"pass": "duckduckgo", "enter": "pass"}}), via); return ch.out[0]
r_net = asyncio.run(settings_via("network"))
check("unpaired network device cannot change settings", "pair this device" in str(r_net.get("error")), r_net)
import hashlib, hmac as _hmac
dev_id, dev = next(iter(rp.load_state()["devices"].items()))
body = json.dumps({"pass": "mojeek", "enter": "logout"})
async def signed(mac):
    ch = FakeChannel(); await rp.reply(ch, json.dumps({"id": "s", "set_settings_json": body, "auth": {"device": dev_id, "mac": mac}}), "network"); return ch.out[0]
good_mac = _hmac.new(dev["secret"].encode(), f"settings|{body}".encode(), hashlib.sha256).hexdigest()
r_sig = asyncio.run(signed(good_mac))
check("paired device at home: signed change accepted", r_sig.get("settings", {}).get("pass") == "mojeek", r_sig)
r_bad = asyncio.run(signed("0" * 64))
check("...a wrong signature is refused", "pair this device" in str(r_bad.get("error")), r_bad)
r_room = asyncio.run(settings_via("room"))
check("paired device can change settings", r_room.get("settings", {}).get("pass") == "duckduckgo", r_room)

# ---: a connection is not told the engine's name, LAN address or index size; they are asked for
import native_host as _nh
_calls = []
def _details():
    _calls.append(1); return {"engine": "testbox", "ip": "10.0.0.7", "via": "", "host_version": 1, "postgres": "18.6", "database_gb": 51.2, "articles": 6800000}
_real_info, _nh.engine_info = _nh.engine_info, _details
async def ask(msg, via):
    ch = FakeChannel(); await rp.reply(ch, json.dumps(dict(msg, id="i")), via); return ch.out[0]
for _via in ("network", "room"):
    r_info = asyncio.run(ask({"info": True}, _via))
    check(f"{_via}: info says how the engine is reached and nothing about the machine", r_info == {"id": "i", "info": {"via": "", "host_version": _nh.VERSION}} and not _calls, (r_info, _calls))
    leaked = [k for k in ("engine", "ip", "postgres", "database_gb", "sites", "articles", "pages", "places") if k in json.dumps(r_info)]
    check(f"{_via}: ...no name, address or index size in it", leaked == [] and "testbox" not in json.dumps(r_info) and "10.0.0.7" not in json.dumps(r_info), leaked)
    r_ping = asyncio.run(ask({"ping": True}, _via))
    check(f"{_via}: the ping on connecting carries none of them either", r_ping == {"id": "i", "pong": True, "version": _nh.VERSION} and not _calls, r_ping)
    r_det = asyncio.run(ask({"details": True}, _via))
    check(f"{_via}: details, when asked for, has them", r_det.get("details", {}).get("ip") == "10.0.0.7" and r_det["details"]["engine"] == "testbox" and r_det["details"]["database_gb"] == 51.2 and len(_calls) == 1, r_det)
    _calls.clear()
check("a search reply is not the details either", "details" not in asyncio.run(ask({"q": 5}, "network")) and not _calls)
_nh.engine_info = _real_info

# ---: a paired device's home-network connection is signed too (real aiortc offers and answers)
from aiortc import RTCPeerConnection as _PC
_Cfg = rp.RTCConfiguration
rp.RTCConfiguration = lambda iceServers=None: _Cfg(iceServers=[])   # host candidates only: no STUN query, nothing leaves this machine
other = rp.add_device("other tablet")
class TurnWS(FakeWS):                          # the rendezvous' reply to {"t": "turn"}: no relay
    async def send(self, raw):
        m = json.loads(raw); self.sent.append(m)
        if m.get("t") == "turn":
            fut = rp._turn_waiters.pop(self, None)
            if fut and not fut.done(): fut.set_result([])
def page_accepts(pair_secret, ans):
    """The page's rule (app.js connectEngine/answerProven): with a pairing for this engine the answer must be
    signed by its secret; with none, any answer is taken (introduced on trust, as before)."""
    if not ans.get("sdp"): return False
    return pair_secret is None or (ans.get("auth") or {}).get("mac") == rp.mac(pair_secret, "answer", ans["sdp"]["sdp"])
async def item4():
    page = _PC(); page.createDataChannel("search")
    await page.setLocalDescription(await page.createOffer())
    osdp = page.localDescription.sdp
    sec = dev["secret"]
    async def answer(via, auth, sdp=osdp):
        ws = TurnWS(); before = len(rp._pcs)
        msg = {"from": "p7", "via": via, "sdp": {"type": "offer", "sdp": sdp}}
        if auth != "absent": msg["auth"] = auth
        await rp.on_offer(ws, msg)
        ans = [x for x in ws.sent if x.get("t") == "answer"]
        return (ans[0] if ans else {}), len(rp._pcs) - before
    good = {"device": dev_id, "mac": rp.mac(sec, "offer", osdp)}
    check("a real offer has a DTLS fingerprint to sign", rp.fingerprints(osdp).startswith("a=fingerprint:sha-256 "), rp.fingerprints(osdp))

    a, made = await answer("network", good)
    check("home, paired device: the answer is signed with its secret over the answer's fingerprints", a.get("to") == "p7" and a.get("sdp", {}).get("type") == "answer" and made == 1
          and a.get("auth") == {"mac": rp.mac(sec, "answer", a["sdp"]["sdp"])} and "error" not in a, a.get("auth"))
    check("...which the device accepts", page_accepts(sec, a))
    check("...and it is not the offer's MAC, nor valid under another device's secret", a["auth"]["mac"] != good["mac"] and not page_accepts(other["s"], a) and not page_accepts("guess", a))
    swapped = dict(a, sdp={"type": "answer", "sdp": a["sdp"]["sdp"].replace("a=fingerprint:sha-256 ", "a=fingerprint:sha-256 00:")})
    check("...an answer whose DTLS fingerprint was swapped is refused by the device", swapped["sdp"]["sdp"] != a["sdp"]["sdp"] and not page_accepts(sec, swapped))

    a_un, made = await answer("network", "absent")
    check("home, no auth (an unpaired device, a page from before): answered as before, unsigned", a_un.get("sdp", {}).get("type") == "answer" and "auth" not in a_un and "error" not in a_un and made == 1, a_un.get("auth"))
    a_none, _ = await answer("network", None)      # the rendezvous forwards "auth": null
    check("home, auth null (as the rendezvous forwards it): the same", a_none.get("sdp") and "auth" not in a_none and "error" not in a_none)
    check("...an unpaired device accepts it; a device paired with this engine does not (a rogue engine's answer looks like this)", page_accepts(None, a_un) and not page_accepts(sec, a_un))

    for label, auth in (("a wrong MAC", {"device": dev_id, "mac": "0" * 64}),
                        ("an unknown device", {"device": "nosuch", "mac": good["mac"]}),
                        ("a revoked device", {"device": p["d"], "mac": rp.mac(p["s"], "offer", osdp)}),
                        ("another device's MAC", {"device": dev_id, "mac": rp.mac(other["s"], "offer", osdp)}),
                        ("the MAC of another offer", {"device": dev_id, "mac": rp.mac(sec, "offer", offer)}),
                        ("an answer MAC used as the offer's", {"device": dev_id, "mac": rp.mac(sec, "answer", osdp)}),
                        ("no MAC", {"device": dev_id}),
                        ("a non-ASCII MAC", {"device": dev_id, "mac": "é" * 64}),
                        ("auth that is not an object", "x" * 64)):
        b, made = await answer("network", auth)
        check(f"home, {label}: never a signed answer (answered unsigned, the device then refuses it)", b.get("sdp", {}).get("type") == "answer" and "auth" not in b and made == 1 and not page_accepts(sec, b), b.get("auth"))
        c, made = await answer("room", auth)
        check(f"room, {label}: refused, no peer connection", c == {"t": "answer", "to": "p7", "error": "not paired"} and made == 0, c)

    r_ok, made = await answer("room", good)
    check("room, paired device: signed answer (unchanged)", r_ok.get("auth") == {"mac": rp.mac(sec, "answer", r_ok["sdp"]["sdp"])} and made == 1 and page_accepts(sec, r_ok), r_ok.get("auth"))
    r_no, made = await answer("room", "absent")
    check("room, no auth: refused (unchanged)", r_no == {"t": "answer", "to": "p7", "error": "not paired"} and made == 0, r_no)
    o_ok, _ = await answer("network", {"device": other["d"], "mac": rp.mac(other["s"], "offer", osdp)})
    check("each device's answer is signed with its own secret", page_accepts(other["s"], o_ok) and not page_accepts(sec, o_ok))
    check("signed_offer_secret: the device's secret for its own offer only", rp.signed_offer_secret(good, osdp) == sec and rp.signed_offer_secret(good, osdp.replace("a=fingerprint:sha-256 ", "a=fingerprint:sha-256 00:")) is None
          and rp.signed_offer_secret(None, osdp) is None and rp.signed_offer_secret(good, None) is None and rp.signed_offer_secret({}, osdp) is None)
    # teardown: these engines were mid ICE check toward a page that never applied their answers; aioice's
    # check tasks can die on the closed socket (seen 1 run in 8) — that is the teardown, not a result
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: None)
    for pc in list(rp._pcs): await pc.close()
    rp._pcs.clear()
    await page.close()
asyncio.run(item4())
# the same vector is checked against the page's fingerprints() in tests/unit/test-search-page-own-engine.sh
VEC = "v=0\r\na=fingerprint:sha-256 AA:BB\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n  a=fingerprint:sha-1 ZZ  \r\na=fingerprint:sha-256 00:11\r\na=setup:active\r\n"
check("MAC vector shared with the page's test", rp.mac("s3cret-of-the-device", "answer", VEC) == "8f5d194fa6110389b2f2aba1e3c0936e25d8a16d61676073196eed2993d15e7f", rp.mac("s3cret-of-the-device", "answer", VEC))

link = rp.pair_link(r["pair"])
raw = link.split("#pair=", 1)[1]
back = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
check("pair link round-trips after #pair=", back == r["pair"] and link.startswith("https://search.logout.cloud/#pair="), link)

sdp = "v=0\r\na=candidate:1 1 udp 2 3f1c-uuid.local 5000 typ host\r\na=candidate:2 1 udp 1 203.0.113.9 6000 typ srflx\r\na=end\r\n"
out = rp.without_mdns(sdp)
check(".local candidates dropped, others kept", ".local" not in out and "203.0.113.9" in out and "a=end" in out, out)
# --- 2026-11-13: a paired device asks for the next pairing, lists the devices and removes one — no command line
import hashlib as _h, hmac as _hm
def op(body, dev=None, secret=None, via="room"):
    dev, secret = dev or owner["d"], secret or owner["s"]
    raw = json.dumps(body)
    msg = {"id": "o", "op_json": raw, "auth": {"device": dev, "mac": _hm.new(secret.encode(), f"op|{raw}".encode(), _h.sha256).hexdigest()}}
    ch = FakeChannel(); asyncio.run(rp.reply(ch, json.dumps(msg), via)); return ch.out[0]
r = op({"op": "devices"}, secret="wrong")
check("an op with a wrong signature is refused", "pair this device" in str(r.get("error")) and "devices" not in r, r)
ch = FakeChannel(); asyncio.run(rp.reply(ch, json.dumps({"id": "o", "op_json": json.dumps({"op": "invite"})}), "network"))
check("an unsigned op from the home network is refused", "pair this device" in str(ch.out[0].get("error")), ch.out)
r = op({"op": "devices"}, via="network")
check("devices: the list names this device as me, with no secret in it",
      any(d["me"] and d["id"] == owner["d"] for d in r.get("devices", [])) and owner["s"] not in json.dumps(r), r)
n1 = devices()
inv = op({"op": "invite"}).get("invite", {})
check("invite: a six-digit code, a link, and how long they last", len(inv.get("pin", "")) == 6 and "#pair=" in inv.get("link", "") and inv.get("seconds") == rp.PIN_TTL_S, inv)
check("invite: the QR is a square of 0/1 rows (or absent without the qrcode package)",
      inv.get("qr") is None or (len(inv["qr"]) == len(inv["qr"][0]) > 20 and set("".join(inv["qr"])) <= {"0", "1"}), str(inv.get("qr"))[:80])
link = json.loads(base64.urlsafe_b64decode(inv["link"].split("#pair=")[1] + "=="))
check("an invitation is not in the device list and cannot sign ops until it has connected",
      link["d"] not in [d["id"] for d in op({"op": "devices"})["devices"]] and "error" in op({"op": "devices"}, link["d"], link["s"]))
check("the code from the invitation pairs a device at home", "pair" in asyncio.run(pair_via("network", inv["pin"])))
check("the link's device connecting in time becomes a device for good",
      rp.signed_offer_secret({"device": link["d"], "mac": rp.mac(link["s"], "offer", "a=fingerprint:sha-256 AA")}, "a=fingerprint:sha-256 AA") == link["s"]
      and "pending_until" not in rp.load_state()["devices"][link["d"]])
check("...and may name itself", op({"op": "rename", "name": "  Kitchen   iPad "}, link["d"], link["s"]).get("renamed") and rp.load_state()["devices"][link["d"]]["name"] == "Kitchen iPad")
late = rp.add_device("New device", pending_s=600, now=1)
check("an invitation nobody used in time is refused and removed",
      rp.signed_offer_secret({"device": late["d"], "mac": rp.mac(late["s"], "offer", "a=fingerprint:x")}, "a=fingerprint:x") is None and late["d"] not in rp.load_state()["devices"])
r = op({"op": "revoke", "device": link["d"]})
check("revoke: the device is gone from the engine and can do nothing more",
      r.get("revoked") is True and link["d"] not in rp.load_state()["devices"] and "error" in op({"op": "devices"}, link["d"], link["s"]), r)
check("revoke of an unknown device says so", op({"op": "revoke", "device": "nope"}).get("revoked") is False)
check("an unknown op is an error", op({"op": "format-disk"}).get("error") == "unknown op")
# --- the admin settings (2026-11-14): signed by a paired device, unlocked with the admin PIN per connection
sys.path.insert(0, os.path.join(os.environ["API"], "..", "..", "installer"))
import logout_search as _ls, adminpin as _ap, homesettings as _hs
_data = os.path.dirname(os.path.dirname(rp.STATE))
rp._admin = _ls.admin_handler(_data)
def adm(body, conn, dev=None, secret=None, signed=True):
    raw = json.dumps(body)
    dev, secret = dev or owner["d"], secret or owner["s"]
    m = {"id": "a", "admin_json": raw, "auth": {"device": dev, "mac": _hm0.new(secret.encode(), f"admin|{raw}".encode(), _h0.sha256).hexdigest() if signed else "0" * 64}}
    ch = FakeChannel(); asyncio.run(rp.reply(ch, json.dumps(m), "room", None, conn)); return ch.out[0]
A, B = {}, {}
check("admin: unsigned is refused", adm({"op": "status"}, A, signed=False).get("error") == "pair this device first")
st0 = adm({"op": "status"}, A)
check("admin: status says no PIN and locked", st0.get("pin_set") is False and st0.get("unlocked") is False and "collectors" in st0, st0)
check("admin: no PIN set, nothing unlocks", "no admin PIN" in adm({"op": "unlock", "pin": "1234"}, A).get("error", ""))
check("admin: locked, a change is refused", adm({"op": "set", "key": "weather", "value": False}, A).get("error", "").startswith("locked")
      and _hs.load(_data)["weather"] is True)
_ap.set_pin(_data, "4821")
check("admin: a wrong PIN does not unlock", adm({"op": "unlock", "pin": "0000"}, A).get("error") == "wrong PIN" and "admin_until" not in A)
check("admin: the right PIN unlocks this connection", adm({"op": "unlock", "pin": "4821"}, A).get("unlocked") is True and A.get("admin_until"))
r = adm({"op": "set", "key": "weather", "value": False}, A)
check("admin: ...which may then change a setting", r.get("collectors", {}).get("weather") is False and _hs.load(_data)["weather"] is False, r)
check("admin: another connection is still locked", adm({"op": "set", "key": "weather", "value": True}, B).get("error", "").startswith("locked"))
check("admin: an unknown setting is refused", "no setting" in adm({"op": "set", "key": "radar", "value": 1}, A).get("error", ""))
check("admin: a bad value is refused", "one of" in adm({"op": "set", "key": "news_every", "value": 3}, A).get("error", ""))
r = adm({"op": "set", "key": "at_hour", "value": 19}, A)
check("admin: the update time is set", r.get("updates", {}).get("at_hour") == 19, r)
A["admin_until"] = 1
check("admin: an unlock ends (10 minutes)", adm({"op": "set", "key": "weather", "value": True}, A).get("error", "").startswith("locked"))
outs = [adm({"op": "unlock", "pin": "9999"}, B) for _ in range(5)]
check("admin: five wrong PINs lock it, even the right one, for 15 minutes",
      outs[-1].get("error") == "too many wrong PINs" and adm({"op": "unlock", "pin": "4821"}, B).get("error") == "too many wrong PINs"
      and 800 < _ap.locked_for(_data) <= 900, outs[-1])
seen = json.dumps([st0, r] + outs + [adm({"op": "status"}, A)])
check("admin: no reply carries the PIN", "4821" not in seen)
rp._admin = None
check("admin: an engine without admin settings says so", adm({"op": "status"}, A).get("error") == "this engine has no admin settings")
_ap.clear(_data)

rp.save_state({"room": rp.load_state()["room"], "devices": {late["d"]: {"secret": "s", "name": "x", "added": "", "pending_until": 1}}})
check("only stale invitations left: the engine is unclaimed again, and they are cleared", not rp.claimed() and devices() == 0)

print(f"\nTotal FAIL: {fails}")
sys.exit(1 if fails else 0)
PY
