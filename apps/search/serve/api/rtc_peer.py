#!/usr/bin/env python3
"""The engine's side of "use my own engine from any device on my network".

Holds a WebSocket to the rendezvous on search.logout.cloud (outbound only — no listening
port), over IPv4 and IPv6 both (a household shares one public IPv4 but each device has its
own IPv6 address inside one /64, and a phone may pick either family). When a page on the same
network asks, it answers the page's WebRTC offer; from then on the page and this engine talk
directly over an encrypted data channel. Searches never pass through the rendezvous.

The data channel carries the native-messaging protocol (native_host.handle): {id, q, results}
-> {id, body}; {id, ping} -> pong; {id, info} -> how the engine is reached; {id, details} -> its name,
LAN address and index size, only when the person asks. Nothing is logged per search.

Pairing (VPN / cellular — anywhere the public IP differs): the engine has a permanent room
id at the rendezvous and one secret per paired device (RTC_STATE, JSON). A device pairs with
the "Pair this device" button while on the same network ({pair: true, pin} over the data channel —
refused on a paired, remote connection), or from a link/QR. The button needs a PIN the engine's
owner asks for (before it, any device on the home network could press the button and
keep a permanent secret): six digits, good for PIN_TTL_S, for one pairing, gone after PIN_TRIES
wrong guesses.

No command line is needed (owner 2026-11-13: "the pin situation needs to be addressed so that its dead simple"):
  - An engine with no devices is unclaimed: the first device on its own LAN that presses the button is paired
    with no PIN. From then on the button needs a PIN again. "On its own LAN" is the engine's own observation —
    the connection's far end is a private address (peer_on_lan) — not the rendezvous's "same network", which is
    "same public address" and under carrier NAT covers strangers.
  - Later devices pair the same way: one press of the button, from the engine's own LAN (owner 2026-11-13: "just
    a button and easy"). Being on the home network is the proof, as it is for a printer or a TV. Every device paired
    this way is marked new until another device has been shown it, so the owner sees who joined and can remove them.
    An owner who wants more locks pairing ({"op":"lock","on":true}): the button then needs a PIN again.
  - A paired device asks for the next pairing itself ({op_json: '{"op":"invite"}', auth}): the engine answers
    with a PIN to type at home and a link/QR that works anywhere. The link's device is pending for PIN_TTL_S;
    unused, it is removed.
  - A paired device lists the devices and revokes one ("devices", "revoke"), and names itself ("rename").
Every op is signed like a settings change: auth.mac = HMAC-SHA256(device secret, "op|" + op_json).

    python rtc_peer.py --pin                   # prints the PIN to type on the device

    python rtc_peer.py --pair "Sean's phone"   # prints the link + a QR code
    python rtc_peer.py --devices               # list paired devices
    python rtc_peer.py --revoke <device id>    # that device can no longer connect

A paired offer carries HMAC-SHA256(device secret, "offer|" + the SDP's DTLS fingerprints);
the answer carries the same over "answer|" + its fingerprints. The rendezvous never has the
secret, so it cannot swap in its own DTLS keys (no man in the middle). Paired connections get
TURN credentials from the rendezvous, so they also work where no direct path exists.
A paired device signs its same-network offers too and refuses an unsigned answer from an engine
it has paired with: another engine on the home network cannot take this one's place. The
engine signs its answer to any validly signed offer; an unsigned same-network offer (an unpaired
device, an older page) is answered unsigned, as before.

Env: RV_URL (wss://search.logout.cloud/rv), STUN_URL (stun:search.logout.cloud:3478),
LOGOUT_ENGINE_NAME (defaults to the computer's friendly name, native_host.friendly_name), RTC_STATE (/state/engine.json),
PAIR_BASE (https://search.logout.cloud/), plus search_api's PG* settings.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import errno
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import time

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
try:
    import ice_addresses
    ice_addresses.install()                  # offer the real adapters only: a WSL/Hyper-V/Tailscale/link-local address cost every link a 5 s STUN wait (buildbox)
except ImportError:
    pass


async def _close(pc) -> None:
    """pc.close() for at most 5 s. On Windows (buildbox, aiortc 1.15) closing a connected pair never returned and the first-run link test waited 12 minutes.
    asyncio.wait, not wait_for: wait_for cancels the close and then waits for the cancellation to finish, which is what never happens; the stuck
    task is abandoned."""
    task = asyncio.ensure_future(pc.close())
    try:
        done, _pending = await asyncio.wait({task}, timeout=5)
        if done and not task.cancelled():
            task.exception()                    # a failed close is not an error here
    except Exception:  # noqa: BLE001 - a connection that will not close is dropped
        pass

import native_host

RV_URL = os.environ.get("RV_URL", "wss://search.logout.cloud/rv")
STUN_URL = os.environ.get("STUN_URL", "stun:search.logout.cloud:3478")
NAME = native_host.friendly_name()     # the computer's own name, as its owner set it
MAX_PEERS = 16
STATE = os.environ.get("RTC_STATE", "/state/engine.json")
PAIR_BASE = os.environ.get("PAIR_BASE", "https://search.logout.cloud/")
PIN_TTL_S = 600               # a PIN is good for ten minutes
PIN_TRIES = 5                 # wrong guesses before it is thrown away (5 of 1,000,000: a guesser gets nowhere)
# Requests one data channel may send (a paired device could issue unlimited searches). Typing sends one or
# two per keystroke; 20 a second with a burst of 40 is far above a person and far below a script.
RATE_PER_S = float(os.environ.get("RTC_RATE_PER_S", "20"))
RATE_BURST = float(os.environ.get("RTC_RATE_BURST", "40"))

# How long an answer to a SAME-NETWORK offer waits for STUN (2026-11-16). aioice gathers a server-reflexive candidate on every IPv4
# address of the computer and waits up to 5 s for ALL of them: one adapter that cannot reach the STUN server (a VPN, WSL's or
# Hyper-V's virtual switch, a disconnected card) made every connection's answer take 5.0 s, measured on buildbox's engine over the
# public rendezvous (5.1-5.4 s from offer to answer; the page then connected in 60 ms). The page behind the same address reaches
# this engine's LAN address, so the host candidates are enough and a STUN answer that comes within this time is a bonus. A
# healthy STUN answer takes 30-50 ms, and the wait ends as soon as every query is back. A paired device from elsewhere (via
# "room") keeps aioice's 5 s: its relay candidate needs the time.
GATHER_NETWORK_S = float(os.environ.get("RTC_GATHER_NETWORK_S", "0.15"))
_gather_s: contextvars.ContextVar = contextvars.ContextVar("rtc_gather_s", default=None)


def _patch_gather() -> bool:
    """aioice's gathering reads its wait from the current offer's setting (_gather_s); no setting: its own 5 s."""
    try:
        from aioice import ice
        orig = ice.Connection.get_component_candidates
    except (ImportError, AttributeError):
        return False
    if getattr(orig, "_logout_patched", False):
        return True

    async def get_component_candidates(self, component, addresses, timeout=5):
        s = _gather_s.get()
        return await orig(self, component, addresses, timeout=timeout if s is None else s)
    get_component_candidates._logout_patched = True
    ice.Connection.get_component_candidates = get_component_candidates
    return True


_patch_gather()

_answer = None
_info = None                  # how the engine is reached, for {details}; None = native_host.engine_info (in-process engine)
_admin = None                 # the downloaded program's admin_handler: settings a paired device changes with the admin PIN
ADMIN_UNLOCK_S = 600          # an unlock lasts this long, on its own connection only
_pcs: set = set()
# RT-07: what each connection is, so the table cannot be filled by strangers. Paired devices always have slots (MAX_PEERS minus
# MAX_UNPAIRED are theirs); a source (the rendezvous' id of the page) holds at most MAX_PER_SOURCE; a connection that says
# nothing is closed after IDLE_UNPAIRED_S (paired: IDLE_PAIRED_S); a refused offer is answered "busy", not left waiting.
MAX_UNPAIRED = int(os.environ.get("RTC_MAX_UNPAIRED", "8"))
MAX_PER_SOURCE = int(os.environ.get("RTC_MAX_PER_SOURCE", "3"))
IDLE_UNPAIRED_S = float(os.environ.get("RTC_IDLE_UNPAIRED_S", "600"))
IDLE_PAIRED_S = float(os.environ.get("RTC_IDLE_PAIRED_S", "1800"))
_pc_info: dict = {}           # pc -> {"src": id, "paired": bool, "last": monotonic time of its last message or start}
_pending: dict = {}           # in-flight offers awaiting turn_servers(), before pc joins _pcs (counted by admit)
_turn_waiters: dict = {}      # websocket -> future for the next {"t": "turn"} reply


# ---- pairing state ----------------------------------------------------------------------
def load_state() -> dict:
    """Read on every use: `--pair` / `--revoke` run in another process and must take effect
    without a restart."""
    try:
        with open(STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not st.get("room"):
        st = {"room": secrets.token_urlsafe(24), "devices": st.get("devices", {}), **({"locked": True} if st.get("locked") else {})}
        save_state(st)
    st.setdefault("devices", {})
    return st


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = f"{STATE}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def add_device(name: str, pending_s: int = 0, now: float | None = None) -> dict:
    """pending_s: an invitation — the device must connect within that many seconds or it is removed."""
    st = load_state()
    dev = secrets.token_urlsafe(9)
    sec = secrets.token_urlsafe(32)
    st["devices"][dev] = {"secret": sec, "name": name[:60] or "device", "added": time.strftime("%Y-%m-%d")}
    if pending_s:
        st["devices"][dev]["pending_until"] = int((now or time.time()) + pending_s)
    save_state(st)
    return {"r": st["room"], "d": dev, "s": sec, "n": NAME}


def live_device(dev_id: str, now: float | None = None, claim: bool = False) -> dict | None:
    """The device's record, or None: unknown, revoked, or an invitation nobody used in time (removed here).
    claim: this is the device itself connecting — an invitation in time becomes a device for good."""
    st = load_state()
    dev = st["devices"].get(dev_id)
    if dev is None:
        return None
    until = dev.get("pending_until")
    if until is not None:
        if (now or time.time()) > until:
            st["devices"].pop(dev_id)
            save_state(st)
            return None
        if claim:
            dev.pop("pending_until")
            save_state(st)
    return dev


def claimed(now: float | None = None) -> bool:
    """Has this engine an owner's device? Invitations do not count, and stale ones are cleared."""
    st = load_state()
    return any(live_device(d, now) is not None and "pending_until" not in v for d, v in list(st["devices"].items()))


def _pin_hash(salt: str, pin: str) -> str:
    return hashlib.sha256(f"{salt}|{pin}".encode()).hexdigest()


def new_pin(now: float | None = None) -> str:
    """A fresh six-digit PIN for one pairing; replaces any earlier one. Only its salted hash is stored."""
    st = load_state()
    pin, salt = f"{secrets.randbelow(10 ** 6):06d}", secrets.token_hex(8)
    st["pin"] = {"salt": salt, "hash": _pin_hash(salt, pin), "expires": int((now or time.time()) + PIN_TTL_S), "tries": 0}
    save_state(st)
    return pin


NO_PIN = ("this engine has no pairing code waiting: on a device that is already paired open the menu, choose "
          "\"Add a device\", then type the code here (or run `rtc_peer.py --pin` on the engine)")


def check_pin(pin: str, now: float | None = None) -> str | None:
    """None when `pin` is the waiting PIN (which is then used up), else why not. A wrong guess counts; the
    PIN_TRIES-th throws the PIN away, so six digits cannot be walked through."""
    st = load_state()
    p = st.get("pin")
    if not isinstance(p, dict) or (now or time.time()) > p.get("expires", 0):
        if "pin" in st:
            st.pop("pin")
            save_state(st)
        return NO_PIN
    if not pin:
        return "type the PIN the engine printed"      # asking is not a guess
    if hmac.compare_digest(_pin_hash(str(p.get("salt")), pin), str(p.get("hash"))):
        st.pop("pin")
        save_state(st)
        return None
    p["tries"] = int(p.get("tries", 0)) + 1
    if p["tries"] >= PIN_TRIES:
        st.pop("pin")
    save_state(st)
    return "wrong PIN" + ("" if "pin" in st else ": too many tries, ask the engine for a new one")


class Bucket:
    """A token bucket: `burst` requests at once, `rate` a second after that."""

    def __init__(self, rate: float = RATE_PER_S, burst: float = RATE_BURST, now: float | None = None):
        self.rate, self.burst, self.tokens, self.at = rate, burst, burst, time.monotonic() if now is None else now

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.at) * self.rate)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def pair_link(p: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(p, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{PAIR_BASE}#pair={raw}"      # the part after # never leaves the device


def signed_by_paired_device(msg: dict, field: str = "set_settings_json", kind: str = "settings") -> bool:
    """{set_settings_json, auth: {device, mac}}: mac = HMAC-SHA256(device secret, "settings|" + json).
    An op is the same with field "op_json" and kind "op"."""
    auth = msg.get("auth") or {}
    body = msg.get(field)
    dev = live_device(str(auth.get("device"))) if isinstance(auth, dict) else None
    if dev is None or "pending_until" in dev or not isinstance(body, str):
        return False
    want = hmac.new(dev["secret"].encode(), f"{kind}|{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(auth.get("mac")), want)


def qr_rows(text: str) -> list[str] | None:
    """The QR code of text as rows of "0"/"1" for the page to draw; None without the qrcode package."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(text)
    q.make(fit=True)
    return ["".join("1" if c else "0" for c in row) for row in q.get_matrix()]


def device_op(msg: dict) -> dict:
    """One signed op from a paired device (the signature was checked): invite, devices, revoke, rename."""
    try:
        op = json.loads(msg["op_json"])
        me = str(msg["auth"]["device"])
        what = op.get("op")
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"error": "bad op"}
    if what == "invite":
        link = pair_link(add_device("New device", pending_s=PIN_TTL_S))
        say("a paired device asked for a pairing code")
        return {"invite": {"pin": new_pin(), "link": link, "qr": qr_rows(link), "seconds": PIN_TTL_S}}
    if what == "devices":
        st = load_state()
        out = {"locked": bool(st.get("locked")),
               "devices": [{"id": d, "name": v.get("name") or "device", "added": v.get("added") or "", "me": d == me,
                            "new": bool(v.get("new")) and d != me}
                           for d, v in st["devices"].items() if live_device(d) is not None and "pending_until" not in v]}
        if op.get("seen"):                          # this device's owner has now been shown the new ones
            for d, v in st["devices"].items():
                if d != me:
                    v.pop("new", None)
            save_state(st)
        return out
    if what == "lock":
        st = load_state()
        st["locked"] = bool(op.get("on"))
        save_state(st)
        say("pairing from the LAN was " + ("locked" if st["locked"] else "unlocked"))
        return {"locked": st["locked"]}
    if what == "revoke":
        st = load_state()
        gone = st["devices"].pop(str(op.get("device")), None)
        save_state(st)
        if gone:
            say("a device was removed by a paired device")
        return {"revoked": bool(gone)}
    if what == "rename":
        st = load_state()
        name = " ".join(str(op.get("name") or "").split())[:60]
        if me in st["devices"] and name:
            st["devices"][me]["name"] = name
            save_state(st)
        return {"renamed": bool(name)}
    return {"error": "unknown op"}


def fingerprints(sdp: str) -> str:
    return "\n".join(sorted(l.strip() for l in sdp.splitlines() if l.startswith("a=fingerprint:")))


def mac(secret: str, kind: str, sdp: str) -> str:
    return hmac.new(secret.encode(), f"{kind}|{fingerprints(sdp)}".encode(), hashlib.sha256).hexdigest()


def signed_offer_secret(auth, offer_sdp) -> str | None:
    """The secret of the paired device that signed this offer ({device, mac}), or None: no auth, an
    unknown or revoked device, or a MAC that is not this offer's."""
    if not isinstance(auth, dict) or not isinstance(offer_sdp, str):
        return None
    dev_id = str(auth.get("device"))
    dev = live_device(dev_id)
    if dev is None or not hmac.compare_digest(str(auth.get("mac")).encode(), mac(dev["secret"], "offer", offer_sdp).encode()):
        return None
    live_device(dev_id, claim=True)           # an invitation used in time is a device from now on
    return dev["secret"]


_LAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                                               "fc00::/7", "fe80::/10")]


def is_lan_addr(host: str) -> bool:
    """A private, link-local or unique-local address: a device on the engine's own LAN. Not carrier NAT or
    Tailscale (100.64/10), not loopback, not a public address."""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except (ValueError, AttributeError):
        return False
    return any(ip.version == n.version and ip in n for n in _LAN_NETS)


def peer_on_lan(pc) -> bool:
    """Is the far end of this connection on the engine's LAN? Read from the ICE pair in use (aioice keeps it
    privately; anything unexpected is "no", and pairing then asks for a code as before)."""
    try:
        conn = pc.sctp.transport.transport._connection
        pairs = list(conn._nominated.values())
        return bool(pairs) and all(is_lan_addr(p.remote_candidate.host) for p in pairs)
    except Exception:
        return False


_page_seen_at = [0.0]


def note_page_seen(via) -> None:
    """A page's message came over a data channel: write STATE's folder/page-seen.json (at most every 5 s) so the installer's first run can
    tell that a real browser reached this engine. Never raises; nothing but the time, the route and a count is written."""
    now = time.time()
    if now - _page_seen_at[0] < 5:
        return
    _page_seen_at[0] = now
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(STATE)), "page-seen.json")
        n = 0
        try:
            with open(p, encoding="utf-8") as f:
                n = int(json.load(f).get("n") or 0)
        except (OSError, ValueError, TypeError):
            pass
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"at": now, "via": via, "n": n + 1}, f)
        os.replace(p + ".tmp", p)
    except Exception:  # noqa: BLE001
        pass


def say(msg: str) -> None:
    print(f"rtc_peer: {msg}", flush=True)       # connection-level events only, never a search


async def reply(channel, raw, via: str, lan=None, conn: dict | None = None) -> None:
    """lan: a call that says whether the far end is on the engine's own LAN (peer_on_lan); None = not known.
    conn: this connection's own state (the admin unlock), kept by on_datachannel."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        msg = {"_error": "bad json"}
    if isinstance(msg, dict) and msg.get("pair"):
        # only a device on this network may pair itself with the button; a paired one, wherever it is, hands out
        # the next pairing with a signed "invite" op instead (below)
        if via != "network":
            out = {"id": msg.get("id"), "error": "pair from the home network"}
        elif lan is not None and lan() and (not claimed() or not load_state().get("locked")):
            # on the engine's own LAN the button is enough: the first device always, later ones unless the owner
            # locked pairing. A later one is marked new until another device has been shown it.
            first = not claimed()
            got = add_device(str(msg.get("device") or "device"))
            if not first:
                st = load_state()
                st["devices"][got["d"]]["new"] = True
                save_state(st)
            out = {"id": msg.get("id"), "pair": got, "first": first}
            say("paired its first device" if first else "paired a device from the LAN")
        else:
            pin = msg.get("pin")
            why = check_pin(pin.strip() if isinstance(pin, str) else "")
            if why:                                   # need: the page shows its PIN field
                out = {"id": msg.get("id"), "error": why, "need": "pin"}
                say("refused a pairing: " + ("no PIN waiting" if why == NO_PIN else "no or wrong PIN"))
            else:
                out = {"id": msg.get("id"), "pair": add_device(str(msg.get("device") or "device"))}
                say("paired a device")
        if channel.readyState == "open":
            channel.send(json.dumps(out))
        return
    if isinstance(msg, dict) and "op_json" in msg:
        # a paired device's own business (invite, devices, revoke, rename): signed wherever it is
        out = device_op(msg) if signed_by_paired_device(msg, "op_json", "op") else {"error": "pair this device first"}
        if channel.readyState == "open":
            channel.send(json.dumps(dict(out, id=msg.get("id"))))
        return
    if isinstance(msg, dict) and "admin_json" in msg:
        # the admin settings (owner 2026-11-14): signed by a paired device, then unlocked with the admin PIN for this
        # connection only. The PIN is set and read only on the engine's own computer (installer/adminpin.py).
        if _admin is None:
            out = {"error": "this engine has no admin settings"}
        elif not signed_by_paired_device(msg, "admin_json", "admin"):
            out = {"error": "pair this device first"}
        else:
            conn = conn if conn is not None else {}
            try:
                op = json.loads(msg["admin_json"])
            except (ValueError, TypeError):
                op = {}
            unlocked = float(conn.get("admin_until") or 0) > time.time()
            loop = asyncio.get_running_loop()
            out = await loop.run_in_executor(None, _admin, op, unlocked)
            if isinstance(op, dict) and op.get("op") == "unlock" and out.get("unlocked"):
                conn["admin_until"] = time.time() + ADMIN_UNLOCK_S
        if channel.readyState == "open":
            channel.send(json.dumps(dict(out, id=msg.get("id"))))
        return
    if isinstance(msg, dict) and ("set_settings" in msg or "set_settings_json" in msg) and via == "network" \
            and not signed_by_paired_device(msg):
        # an unpaired device on the network may read the settings, not change them; a paired
        # one at home signs the change with its pairing secret
        if channel.readyState == "open":
            channel.send(json.dumps({"id": msg.get("id"), "error": "pair this device to change settings"}))
        return
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None, native_host.handle, msg, _answer, _info or native_host.engine_info)
    if channel.readyState == "open":
        channel.send(json.dumps(out))


def too_many(raw) -> dict:
    """The refusal for a request over the channel's rate; it carries the request's id so the page's call ends."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        msg = None
    return {"id": msg.get("id") if isinstance(msg, dict) else None, "error": "too many requests"}


def without_mdns(sdp: str) -> str:
    """Browsers hide their LAN address behind a random "<uuid>.local" name. aioice would open
    the mDNS port (5353) to resolve it — already taken by Avahi/mDNSResponder on most machines
    (OSError 98 on indexhost, 2026-09-27), and the offer then fails. The name is not needed: the
    browser's checks reach this engine's own candidates, and ICE learns the browser's real
    address from them (a peer-reflexive candidate)."""
    return "\r\n".join(line for line in sdp.split("\r\n")
                         if not (line.startswith("a=candidate:") and ".local " in line))


def own_addresses() -> set:
    """The addresses this machine goes out on (UDP connect: no packet is sent), plus LOGOUT_ENGINE_IP."""
    out = {os.environ.get("LOGOUT_ENGINE_IP", "")}
    for fam, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):   # documentation ranges
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                out.add(s.getsockname()[0])
        except OSError:
            pass
    return {a for a in out if a}


def outward_only(sdp: str, own: set) -> str:
    """The answer offers the browser only the candidates it can use: host candidates on the addresses this
    machine goes out on (and Tailscale's 100.64/10), STUN candidates found from them, and every relay.
    Docker bridges and their IPv6 (indexhost offered ~20 candidates; the browser checks one every ~50 ms in
    priority order, so the LAN one came ~6th: 320 ms of every page load, 2026-10-22). No address known:
    unchanged."""
    if not own:
        return sdp
    cgnat = ipaddress.ip_network("100.64.0.0/10")

    def keep(line: str) -> bool:
        f = line.split()
        if not line.startswith("a=candidate:") or len(f) < 8:
            return True
        addr, kind = f[4], f[7]
        raddr = f[f.index("raddr") + 1] if "raddr" in f[8:] else None
        if kind == "host":
            try:
                return addr in own or ipaddress.ip_address(addr) in cgnat
            except ValueError:
                return True
        if kind == "srflx":
            return raddr is None or raddr in own
        return True
    kept = [line for line in sdp.split("\r\n") if keep(line)]
    if not any(x.startswith("a=candidate:") and " typ host" in x for x in kept):
        return sdp                                    # never leave the browser nothing local to try
    return "\r\n".join(kept)


async def turn_servers(ws) -> list:
    fut = asyncio.get_running_loop().create_future()
    _turn_waiters[ws] = fut
    await ws.send(json.dumps({"t": "turn"}))
    try:
        return [RTCIceServer(urls=x["urls"], username=x["username"], credential=x["credential"])
                for x in await asyncio.wait_for(fut, 5)]
    except (asyncio.TimeoutError, KeyError, TypeError):
        return []


def _forget(pc) -> None:
    _pcs.discard(pc)
    _pc_info.pop(pc, None)


STALE_S = float(os.environ.get("RTC_STALE_S", "45"))      # a page pings every 20 s: a connection silent this long is a closed tab, not an idle one


async def make_room(paired: bool) -> int:
    """Before an offer is refused for lack of room (RT-07), close the connections that are stale: a page that was reloaded or
    closed leaves its old connection here until the browser's consent checks fail or the idle time runs out, and four
    reloads in two minutes filled the table, so the fifth page was answered "busy" and went without its engine (2026-11-17).
    A live page pings every 20 s, so only connections silent for STALE_S are taken, the oldest first, and only as many as the
    new offer needs. Returns how many were closed."""
    why = admit(None, paired)
    if not why or why == "too many connections from one source":
        return 0
    now, closed = time.monotonic(), 0
    stale = sorted((p for p in _pcs if now - _pc_info.get(p, {}).get("last", now) > STALE_S and (paired or not _pc_info.get(p, {}).get("paired"))),
                   key=lambda p: _pc_info.get(p, {}).get("last", now))
    for p in stale:
        if not admit(None, paired):
            break
        _forget(p)
        try:
            await p.close()
        except Exception:  # noqa: BLE001
            pass
        closed += 1
    if closed:
        say(f"closed {closed} stale connection(s) to make room")
    return closed


def admit(src, paired: bool) -> str:
    """"" when a new connection may be taken, else why not (RT-07)."""
    active = [_pc_info.get(p, {}) for p in _pcs] + list(_pending.values())
    if len(active) >= MAX_PEERS:
        return "table full"
    unpaired = sum(1 for info in active if not info.get("paired"))
    if not paired and unpaired >= MAX_UNPAIRED:
        return "too many unpaired connections"
    if src is not None and sum(1 for info in active if info.get("src") == src) >= MAX_PER_SOURCE:
        return "too many connections from one source"
    return ""


async def reap_idle(now: float | None = None) -> int:
    """Closes connections that have said nothing for their idle time; how many."""
    now = time.monotonic() if now is None else now
    old = [p for p in list(_pcs) if now - _pc_info.get(p, {}).get("last", now) >
           (IDLE_PAIRED_S if _pc_info.get(p, {}).get("paired") else IDLE_UNPAIRED_S)]
    for p in old:
        _forget(p)
        try:
            await p.close()
        except Exception:  # noqa: BLE001
            pass
    if old:
        say(f"closed {len(old)} idle connection(s)")
    return len(old)


async def reap_loop() -> None:
    while True:
        await asyncio.sleep(30)
        await reap_idle()


async def on_offer(ws, m: dict) -> None:
    frm, sdp, via = m.get("from"), m.get("sdp"), m.get("via")
    if not isinstance(sdp, dict) or not isinstance(sdp.get("sdp"), str) or not isinstance(sdp.get("type"), str):
        say("offer ignored: malformed sdp")
        return
    secret = signed_offer_secret(m.get("auth"), sdp["sdp"])
    await make_room(secret is not None)
    why = admit(frm, secret is not None)
    if why:
        say(f"offer refused: {why}")
        await ws.send(json.dumps({"t": "answer", "to": frm, "error": "busy"}))
        return
    # A paired device signs its offer wherever it is (at home too, so that an engine that is not
    # this one cannot answer in its place). A valid signature gets a signed answer on any `via`. Without
    # one, "room" is refused, and "network" is answered unsigned as before: an unpaired device at home,
    # or a page from before — the device, not the engine, decides whether it accepts that.
    if secret is None and via == "room":
        say("refused an unpaired or revoked device")
        await ws.send(json.dumps({"t": "answer", "to": frm, "error": "not paired"}))
        return
    if secret is None and m.get("auth"):
        say("a home offer was signed by no paired device: answered unsigned")
    slot = object()
    _pending[slot] = {"src": frm, "paired": secret is not None}
    try:
        ice = [RTCIceServer(urls=STUN_URL)] + (await turn_servers(ws) if via == "room" else [])
        _gather_s.set(GATHER_NETWORK_S if via == "network" else None)         # this offer's own task: no other offer sees it
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        _pcs.add(pc)
        _pc_info[pc] = {"src": frm, "paired": secret is not None, "last": time.monotonic()}
    finally:
        _pending.pop(slot, None)

    @pc.on("datachannel")
    def on_datachannel(channel):
        bucket = Bucket()
        conn = {}                                   # this connection's own state: the admin unlock

        @channel.on("close")
        def on_close():                               # the page closed it (a reload, a closed tab): the slot is free now
            _forget(pc)
            asyncio.ensure_future(_close(pc))

        @channel.on("message")
        def on_message(raw):
            if pc in _pc_info:
                _pc_info[pc]["last"] = time.monotonic()
            note_page_seen(via)
            if bucket.allow():
                asyncio.ensure_future(reply(channel, raw, via, lambda: peer_on_lan(pc), conn))
            elif channel.readyState == "open":
                channel.send(json.dumps(too_many(raw)))

    @pc.on("connectionstatechange")
    async def on_state():
        if pc.connectionState in ("failed", "closed"):
            _forget(pc)
            await _close(pc)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=without_mdns(sdp["sdp"]), type=sdp["type"]))
    except Exception as e:                            # a bad offer must not kill anything else
        say(f"offer rejected: {type(e).__name__}")
        _forget(pc)
        await _close(pc)
        return
    try:
        # aiortc gathers every candidate before setLocalDescription returns: one answer, no trickle
        await pc.setLocalDescription(await pc.createAnswer())
        answer = outward_only(pc.localDescription.sdp, own_addresses())
        out = {"t": "answer", "to": frm, "sdp": {"sdp": answer, "type": pc.localDescription.type}}
        if secret:
            out["auth"] = {"mac": mac(secret, "answer", answer)}
        await ws.send(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - a failed answer must not keep its slot (RT-07)
        say(f"answer failed: {type(e).__name__}")
        _forget(pc)
        await _close(pc)


IPV6_RETRY_S = 1800           # no IPv6 here: look again every half hour, silently


def family_unavailable(family: int, e: BaseException) -> bool:
    """IPv6 that cannot be used from here (no AAAA record for the rendezvous, no route, no IPv6 at all, a route that goes
    nowhere): normal on most home networks, not an error, and never a reason to disturb the IPv4 registration."""
    if family != socket.AF_INET6:
        return False
    return (isinstance(e, (socket.gaierror, asyncio.TimeoutError))
            or getattr(e, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.ENETDOWN))


async def rendezvous(family: int, label: str) -> None:
    delay, told = 2, False
    while True:
        try:
            async with websockets.connect(RV_URL, family=family, open_timeout=10,
                                          ping_interval=25, ping_timeout=20, max_size=64 * 1024) as ws:
                await ws.send(json.dumps({"t": "engine", "name": NAME, "room": load_state()["room"],
                                          "v": native_host.VERSION}))
                say(f"registered over {label}")
                delay, told = 2, False
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("t") == "offer" and isinstance(m.get("sdp"), dict):
                        asyncio.ensure_future(on_offer(ws, m))
                    elif m.get("t") == "turn":
                        fut = _turn_waiters.pop(ws, None)
                        if fut and not fut.done():
                            fut.set_result(m.get("iceServers") or [])
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            if family_unavailable(family, e):
                if not told:
                    say(f"{label}: not available from here ({type(e).__name__}); using IPv4 only, looking again every {IPV6_RETRY_S // 60} minutes")
                    told = True
                await asyncio.sleep(IPV6_RETRY_S)
                continue
            say(f"{label}: {type(e).__name__}; retrying in {delay}s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def main(answer=None, info=None, admin=None) -> None:
    """answer: where searches go. None (the servers' own peer): the engine is loaded into this process. Given (the
    downloaded program, `logout-search peer`): a call that asks an engine already running on this computer, so this
    process holds no index and the two can be started and stopped apart."""
    global _answer, _info, _admin
    if answer is not None:
        _answer, _info, _admin = answer, info, admin
        await asyncio.gather(rendezvous(socket.AF_INET, "IPv4"), rendezvous(socket.AF_INET6, "IPv6"), reap_loop())
        return
    _answer = native_host.search_api_answer()
    if os.environ.get("SEARCH_STORE") and int(os.environ.get("WARM_HEAD", "0") or 0) > 0:
        # precomputed results on disk for the home engine too (2026-11-04): the head warmer in this process
        import threading
        import search_api
        import warm
        threading.Thread(target=warm.in_process, args=(search_api.Handler,), daemon=True, name="store-warm").start()
    if os.environ.get("SEARCH_STORE"):
        # the store's inbox for the home engine too (2026-11-12): search_api starts this thread only in its own
        # main(), which this process never runs, so hand-picked lists shipped to <state>/inbox since 2026-11-09 sat unread
        import threading
        import search_api
        if search_api.STORE_INBOX_EVERY_S > 0:
            threading.Thread(target=search_api.store_inbox_loop, daemon=True, name="store-inbox").start()
    threading_warm = asyncio.get_running_loop().run_in_executor(None, native_host.warm, _answer)
    await asyncio.gather(rendezvous(socket.AF_INET, "IPv4"), rendezvous(socket.AF_INET6, "IPv6"), threading_warm, reap_loop())


def cli(argv: list) -> int:
    if argv[:1] == ["--pair"]:
        link = pair_link(add_device(" ".join(argv[1:]) or "device"))
        print(f"Open on the device (or scan):\n{link}\n")
        try:
            import qrcode
            q = qrcode.QRCode(border=1)
            q.add_data(link)
            q.print_ascii(invert=True)
        except ImportError:
            pass
        return 0
    if argv == ["--pin"]:
        print(f"PIN {new_pin()}  (type it on the device after \"Pair this device\"; good for {PIN_TTL_S // 60} minutes, one pairing)")
        return 0
    if argv[:1] == ["--devices"]:
        for dev, d in load_state()["devices"].items():
            print(f"{dev}  {d['added']}  {d['name']}" + ("  (invitation, not used yet)" if "pending_until" in d else ""))
        return 0
    if argv[:1] == ["--revoke"] and len(argv) == 2:
        st = load_state()
        gone = st["devices"].pop(argv[1], None)
        save_state(st)
        print("revoked" if gone else "no such device")
        return 0 if gone else 1
    print("usage: rtc_peer.py [--pair NAME | --pin | --devices | --revoke ID]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    asyncio.run(main())
