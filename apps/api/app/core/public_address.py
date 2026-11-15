"""Is a resolved address one the server may connect to (C7; G2 sheet photos).

`is_public_address(address)` is True only for a global unicast address. It is the one check behind
every server-side fetch of a link someone else named: the Amazon PTD schema link
(`app.adapters.amazon.definitions`) and the seller's photo links (`app.adapters.photo_fetch`).

Refused: private, loopback, link-local (169.254.169.254, the cloud metadata address, is one),
CGNAT 100.64.0.0/10, unique-local fc00::/7 (fd00:ec2::254, the AWS IPv6 metadata address, is
one), multicast, reserved, unspecified, and every IPv6 form that carries an IPv4 address the
network may route to: IPv4-mapped (judged by its IPv4 address), IPv4-compatible ::/96, NAT64
64:ff9b::/96 and 64:ff9b:1::/48, 6to4 2002::/16 and Teredo 2001::/32. A zone suffix (`%eth0`) is
dropped before parsing; anything that does not parse is refused.
"""

from __future__ import annotations

import ipaddress
from typing import Final

__all__ = ["is_public_address"]

_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")
_ULA: Final = ipaddress.ip_network("fc00::/7")
# IPv6 prefixes that embed or tunnel to an IPv4 address: never connected to, whatever they embed
_V6_TUNNELS: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/96",  # IPv4-compatible (deprecated)
        "64:ff9b::/96",  # NAT64 well-known prefix
        "64:ff9b:1::/48",  # NAT64 local-use prefix
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
    )
)


def is_public_address(address: str) -> bool:
    """True only for a global unicast address (module docstring)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _V6_TUNNELS):
            return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA:
        return False
    return ip.is_global
