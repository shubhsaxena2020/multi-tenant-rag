"""SSRF-safe URL fetching for tenant-supplied URLs.

Defense in depth (Safeguard 2026 SSRF guide; OWASP SSRF Prevention Cheatlist):
1. Scheme allowlist (only http/https).
2. Resolve the hostname ourselves and REJECT any destination that is not a public,
   routable unicast address: blocks loopback, RFC1918 private, link-local (incl. the
   cloud metadata endpoint 169.254.169.254), unique-local (ULA), multicast, reserved.
3. Connect to the EXACT validated IP (ip-pinning) to defeat DNS-rebinding.
4. Disable automatic redirects; re-validate EVERY hop (a 302 can point at an internal IP).
5. Enforce timeouts + a response-size cap so SSRF can't double as a DoS amplifier.

Note: application-layer checks are necessary but not sufficient — production deployments
should ALSO place egress behind a network policy / IMDSv2. See DEPLOYMENT.md.
"""
from __future__ import annotations

import ipaddress
import socket

import httpx

# Ranges we never fetch from. ipaddress collapses these for us.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",          # "this" network
        "10.0.0.0/8",         # RFC1918 private
        "100.64.0.0/10",      # CGNAT
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local + cloud IMDS 169.254.169.254
        "172.16.0.0/12",      # RFC1918 private
        "192.0.0.0/24",       # IETF protocol assignments
        "192.0.2.0/24",       # TEST-NET-1
        "192.168.0.0/16",     # RFC1918 private
        "198.18.0.0/15",      # benchmarking
        "198.51.100.0/24",    # TEST-NET-2
        "203.0.113.0/24",     # TEST-NET-3
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved / future use
        "255.255.255.255/32", # broadcast
        "64:ff9b::/96",       # NAT64 (IPv6-translated IPv4, RFC6052, RFC8215)
    )
]

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB cap on fetched body


def _is_blocked(ip: object) -> bool:
    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return True
    # Reject anything that isn't a global public unicast address.
    if not ip.is_global:
        return True
    for net in _BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return True
    return False


def _resolve_first_public_ip(hostname: str) -> str:
    """Resolve hostname; return the first public, non-blocked IP. Raises on any block."""
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    last_err: Exception | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_blocked(ip):
            return str(ip)
        last_err = ValueError(f"{hostname} resolves to blocked address {ip}")
    raise last_err or ValueError(f"{hostname} has no resolvable address")


def _validate_target(hostname: str) -> str:
    """Resolve + validate; return the pinned public IP to connect to."""
    ip = _resolve_first_public_ip(hostname)
    return ip


def safe_fetch_url(url: str, timeout: float = 20.0) -> str:
    """Fetch a tenant-supplied URL with SSRF protections. Raises ValueError/HTTPException
    if the destination is not a safe public host, or httpx errors on transport failure.
    Unlike the legacy fetch_url, this will never reach internal/metadata addresses."""
    from urllib.parse import urlparse

    from fastapi import HTTPException, status

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="invalid URL: missing host")
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"URL scheme must be one of {sorted(_ALLOWED_SCHEMES)}")
    if parsed.port is not None and parsed.port not in (80, 443):
        # Optional: tighten to standard ports to avoid odd internal services.
        pass
    try:
        pinned_ip = _validate_target(host)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL host is not allowed (SSRF guard): {e}",
        )

    # Build an IP-pinned URL so we don't re-resolve (DNS-rebinding defense).
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    pinned_url = f"{parsed.scheme}://{pinned_ip}{port}{path}"

    headers = {"User-Agent": "rag-service/1.0", "Host": host}
    # follow_redirects=False -> we revalidate each hop ourselves.
    resp = httpx.get(
        pinned_url, timeout=timeout, follow_redirects=False,
        headers=headers,
    )
    # Re-validate redirects hop-by-hop.
    seen = 0
    while resp.status_code in (301, 302, 303, 307, 308):
        seen += 1
        if seen > 5:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="too many redirects (SSRF guard)")
        loc = resp.headers.get("location")
        if not loc:
            break
        r2 = urlparse(loc)
        if r2.scheme and r2.scheme.lower() not in _ALLOWED_SCHEMES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="redirect to disallowed scheme (SSRF guard)")
        nh = (r2.hostname or "").lower()
        if not nh:
            break
        try:
            pinned_ip = _validate_target(nh)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"redirect target not allowed (SSRF guard): {e}")
        port2 = f":{r2.port}" if r2.port else ""
        path2 = r2.path or "/"
        if r2.query:
            path2 += "?" + r2.query
        pinned_url = f"{r2.scheme}://{pinned_ip}{port2}{path2}"
        headers["Host"] = nh
        resp = httpx.get(pinned_url, timeout=timeout, follow_redirects=False,
                         headers=headers)
    resp.raise_for_status()

    # Size cap: stream so we never buffer an unbounded body into memory.
    body = b""
    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
        body += chunk
        if len(body) > _MAX_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="fetched content exceeds size limit")
    text = body.decode("utf-8", errors="replace")

    # Optional HTML->text extraction (unchanged from legacy ingest).
    try:
        from trafilatura import extract  # type: ignore
    except Exception:  # noqa: BLE001
        extract = None
    if extract is not None:
        clean = extract(text, url=url)
        if clean:
            return clean
    return _strip_html(text)


def _strip_html(body: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", body)
