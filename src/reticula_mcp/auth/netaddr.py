"""Who is calling: the client address, honouring trusted proxies only."""

from __future__ import annotations

import ipaddress

from starlette.requests import Request

from reticula_mcp.config import Settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _networks(settings: Settings) -> list[Network]:
    return [ipaddress.ip_network(p, strict=False) for p in settings.trusted_proxies]


def _is_trusted(addr: str, nets: list[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in nets if ip.version == net.version)


def client_address(request: Request, settings: Settings) -> str:
    """The caller's address.

    ``X-Forwarded-For`` is believed only when the TCP peer is listed in
    ``RETICULA_TRUSTED_PROXIES``; the list is then walked from the right, past
    trusted proxies, and the first other entry is the client (entries further
    left are client-supplied and ignored). Anything unparseable falls back to
    the peer. ``"-"`` when nothing is known.
    """
    peer = request.client.host if request.client else ""
    nets = _networks(settings)
    if not peer or not nets or not _is_trusted(peer, nets):
        return peer or "-"
    header = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in header.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not _is_trusted(hop, nets):
            return hop
    return peer


def address_bucket(address: str) -> str:
    """The throttle key of an address.

    IPv4 as is; an IPv4-mapped IPv6 address as the IPv4 it wraps; any other
    IPv6 address folds to its /64 (one subscriber is routinely handed a whole
    /64, so per-address keys would give a guesser 2**64 fresh buckets). A string
    that is not an IP keeps its own bucket; ``"-"`` (unknown) is one shared
    bucket, so unknown peers are throttled together rather than not at all.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address[:64] or "-"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network(((int(ip) >> 64) << 64, 64)))
    return str(ip)


def caller_bucket(request: Request, settings: Settings) -> str:
    return address_bucket(client_address(request, settings))
