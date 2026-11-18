"""Which of this computer's addresses the WebRTC link offers (2026-11-18, buildbox).

aioice offers every address of every adapter and asks the STUN server through each one, waiting 5 s for the ones that cannot reach it. On a
computer with WSL or Hyper-V (a "vEthernet" adapter at 172.x), Tailscale or a link-local 169.254 address that made every link take 5 s to start
(buildbox: 5.7 s for a link that connected at once over the real adapter, 0.9 s without the others). `usable()` keeps the real adapters' addresses:
no loopback, no link-local, nothing on an adapter named like a virtual switch, a VM host or a tunnel. If that leaves nothing, everything is
offered as before. `install()` makes aioice use it."""
from __future__ import annotations

import ipaddress
import re

VIRTUAL = re.compile(r"hyper-v|vethernet|wsl|virtualbox|vmware|vmnet|docker|tailscale|zerotier|tunnel|teredo|isatap|loopback|bluetooth", re.I)


def usable(adapters, use_ipv4: bool = True, use_ipv6: bool = True) -> list[str]:
    """adapters: ifaddr-style objects (nice_name, ips[ip]). -> addresses worth offering, real adapters only; all of them when none qualifies."""
    real, every = [], []
    for ad in adapters:
        virtual = bool(VIRTUAL.search(getattr(ad, "nice_name", "") or ""))
        for ip in ad.ips:
            if isinstance(ip.ip, str):
                addr, v4 = ip.ip, True
            else:
                addr, v4 = ip.ip[0], False
            if (v4 and not use_ipv4) or (not v4 and not use_ipv6):
                continue
            try:
                a = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if a.is_loopback or a.is_unspecified:
                continue
            every.append(addr)
            if not a.is_link_local and not virtual:
                real.append(addr)
    return real or every


def install() -> bool:
    """Make aioice offer usable() addresses. True when installed; False when aioice or ifaddr is missing (nothing changes)."""
    try:
        import aioice.ice as ice
        import ifaddr
    except ImportError:
        return False
    if getattr(ice.get_host_addresses, "_filtered", False):
        return True

    def get_host_addresses(use_ipv4: bool, use_ipv6: bool) -> list[str]:
        try:
            return usable(ifaddr.get_adapters(), use_ipv4, use_ipv6)
        except Exception:  # noqa: BLE001 - the link must still start
            return ice.get_host_addresses.__wrapped__(use_ipv4, use_ipv6)
    get_host_addresses._filtered = True
    get_host_addresses.__wrapped__ = ice.get_host_addresses
    ice.get_host_addresses = get_host_addresses
    return True
