"""Sentinel-aligned guard extensions for the security suite.

Task 5: Add Sentinel-aligned guard tests to the security suite — extend
the test suite with SSRF/port/payload tests that mirror the real attack
surface. Done when the test suite exercises dangerous inputs and still
fails closed, with all tests passing.
"""

import pytest


def test_ssrf_blocks_dangerous_schemes():
    """Sentinel: additional dangerous URL schemes beyond the existing test.

    Verifies that ftp, gopher, and other non-http/https schemes are rejected
    by safe_fetch_url (which has a scheme allowlist of only http/https).
    """
    from fastapi import HTTPException
    from app.ingestion.ssrf import safe_fetch_url

    for bad in ("ftp://evil.example.com/", "gopher://127.0.0.1:6379/",
                 "file:///etc/passwd", "mailto:test@example.com"):
        with pytest.raises((ValueError, HTTPException)):
            safe_fetch_url(bad)


def test_ssrf_blocks_localhost_via_fetch():
    """Sentinel: localhost is blocked when fetched via safe_fetch_url.

    safe_fetch_url first checks scheme (only http/https allowed), then
    resolves and blocks loopback/private/link-local addresses.
    """
    from fastapi import HTTPException
    from app.ingestion.ssrf import safe_fetch_url

    for bad in ("http://127.0.0.1/", "http://localhost/"):
        with pytest.raises((ValueError, HTTPException)):
            safe_fetch_url(bad)


def test_ssrf_blocks_rfc1918_via_fetch():
    """Sentinel: RFC1918 private IPs are blocked when fetched via safe_fetch_url.

    safe_fetch_url resolves and blocks RFC1918 private addresses
    (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16).
    """
    from fastapi import HTTPException
    from app.ingestion.ssrf import safe_fetch_url

    for bad in ("http://10.0.0.1/", "http://172.16.0.1/",
                 "http://192.168.1.1/"):
        with pytest.raises((ValueError, HTTPException)):
            safe_fetch_url(bad)


def test_ssrf_blocks_link_local_via_fetch():
    """Sentinel: link-local addresses (169.254.0.0/16) are blocked.

    safe_fetch_url blocks the cloud metadata endpoint 169.254.169.254.
    """
    from fastapi import HTTPException
    from app.ingestion.ssrf import safe_fetch_url

    with pytest.raises((ValueError, HTTPException)):
        safe_fetch_url("http://169.254.169.254/")
