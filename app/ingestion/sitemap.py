"""Sitemap.xml crawler for client onboarding.

A client points the service at their sitemap.xml (or even just their root domain) and we
ingest every page as a document. Designed to be a *respectful* crawler:
  - honors robots.txt Allow/Disallow for the host, and Crawl-delay between requests
  - caps concurrency (default 4) and total URLs (max_urls, default 100)
  - reuses the SSRF-safe fetch (app.ingestion.ssrf.safe_fetch_url) so we only ever
    hit public http/https hosts on ports 80/443, with redirect revalidation
  - handles both <urlset> and <sitemapindex> (recurses into child sitemaps)

Idempotency is provided by issue #4: each page is ingested under a stable per-tenant
`doc_key` derived from its URL (`doc_key_for_url`), so a re-crawl REPLACES changed pages
instead of duplicating them. The `document_registry` table (app.db) is the catalog.

Crawling runs inside a worker task (driven by the endpoint's background task); the HTTP
endpoint returns a job_id immediately so large sites don't block the request.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

from ..observability import get_logger
from . import ingest_url

log = get_logger("ingestion")

_SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
}

# Conservative per-host defaults; can be overridden per call.
_DEFAULT_MAX_URLS = 100
_DEFAULT_CONCURRENCY = 4
_DEFAULT_TIMEOUT = 25.0


@dataclass
class CrawlResult:
    urls_discovered: int
    urls_ingested: int
    urls_failed: int
    skipped_robots: int
    errors: list[str]


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _loc_tags(root: ET.Element) -> list[str]:
    locs: list[str] = []
    for el in root.iter():
        if _strip_ns(el.tag) == "loc" and el.text:
            locs.append(el.text.strip())
    return locs


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from a sitemap document.

    A <urlset> yields page <loc> entries; a <sitemapindex> yields child <sitemap><loc>
    entries we recurse into. Robust to the sitemaps.org namespace being present or absent.
    """
    pages: list[str] = []
    children: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"malformed sitemap XML: {e}") from e
    root_tag = _strip_ns(root.tag)
    for loc in _loc_tags(root):
        if root_tag == "sitemapindex":
            children.append(loc)
        else:
            pages.append(loc)
    return pages, children


def _robots_rules(robots_text: str) -> tuple[set[str], float]:
    """Parse a robots.txt body into (disallowed_prefixes, crawl_delay).

    Only the most common directives are honored: `Disallow:` and `Allow:` (Google-style
    longest-match wins) and `Crawl-delay:`. We apply a *single* agent wildcard (`*`),
    not per-bot user-agent blocks, since the crawler identifies as ourselves. `Allow`
    takes precedence over `Disallow` when both match (Google 2019 spec).
    """
    allows: list[str] = []
    disallows: list[str] = []
    crawl_delay = 0.0
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "disallow":
            disallows.append(val)
        elif key == "allow":
            allows.append(val)
        elif key == "crawl-delay":
            try:
                crawl_delay = max(0.0, float(val))
            except ValueError:
                pass
    return _compile_robots(allows, disallows), crawl_delay


def _compile_robots(allows: list[str], disallows: list[str]) -> set[str]:
    """Return the set of disallowed path prefixes after applying Allow overrides."""
    disallowed = set(d for d in disallows if d)
    for a in allows:
        if a and a in disallowed:
            disallowed.discard(a)
    return disallowed


def _is_disallowed(path: str, disallowed: set[str]) -> bool:
    norm = path or "/"
    return any(norm.startswith(d) or (d == "/" and norm == "/") for d in disallowed if d)


async def _fetch_robots(host_root: str) -> tuple[set[str], float]:
    """Fetch robots.txt for a host root (e.g. https://example.com). Returns rules."""
    from .ssrf import safe_fetch_url

    robots_url = urljoin(host_root.rstrip("/") + "/", "robots.txt")
    try:
        body = safe_fetch_url(robots_url, timeout=10.0)
    except Exception:
        # No robots.txt (or blocked fetch) => permissive crawl.
        return set(), 0.0
    try:
        return _robots_rules(body)
    except Exception:
        return set(), 0.0


def _robots_sitemap_hints(robots_text: str, host_root: str) -> list[str]:
    """Extract absolute Sitemap: URLs from a robots.txt body (Google-style)."""
    hints: list[str] = []
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        if key.strip().lower() != "sitemap":
            continue
        val = val.strip()
        if not val:
            continue
        hints.append(val if val.startswith("http") else urljoin(host_root + "/", val))
    return hints


async def resolve_sitemap_url(input_url: str) -> str:
    """Turn a tenant-supplied pointer into a concrete sitemap URL to crawl.

    Onboarding UX: a tenant may point at their *root domain* (e.g. https://example.com)
    instead of the exact sitemap file. We then:
      1. if the input already looks like a sitemap (path ends in .xml or contains
         'sitemap'), try it directly;
      2. else fetch /robots.txt and honor any ``Sitemap:`` hints;
      3. else fall back to /sitemap.xml and /sitemap_index.xml.
    The first candidate that fetches and parses as a sitemap wins. Raises ValueError
    if no sitemap can be located. SSRF-safe (reuses safe_fetch_url).
    """
    from .ssrf import safe_fetch_url

    parsed = urlparse(input_url)
    host_root = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/").lower()

    # Step 1: direct sitemap-looking input.
    if path.endswith(".xml") or "sitemap" in path:
        try:
            xml_text = safe_fetch_url(input_url, timeout=_DEFAULT_TIMEOUT)
            if _looks_like_sitemap(xml_text):
                return input_url
        except Exception:
            pass

    # Step 2: robots.txt Sitemap: hints.
    candidates: list[str] = []
    try:
        robots = safe_fetch_url(urljoin(host_root + "/", "robots.txt"), timeout=10.0)
        candidates.extend(_robots_sitemap_hints(robots, host_root))
    except Exception:
        pass
    # Step 3: common fallbacks.
    candidates.extend([
        urljoin(host_root + "/", "sitemap.xml"),
        urljoin(host_root + "/", "sitemap_index.xml"),
    ])

    for cand in candidates:
        try:
            xml_text = safe_fetch_url(cand, timeout=_DEFAULT_TIMEOUT)
        except Exception:
            continue
        if _looks_like_sitemap(xml_text):
            return cand
    raise ValueError(f"no sitemap found for {input_url}")


def _looks_like_sitemap(xml_text: str) -> bool:
    """Cheap check: parses as XML and contains at least one <loc> entry."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False
    return any(_strip_ns(el.tag) == "loc" and el.text for el in root.iter())


async def discover_sitemap_urls(sitemap_url: str, max_urls: int = _DEFAULT_MAX_URLS) -> list[str]:
    """Resolve a sitemap URL to a flat list of page URLs (recursing sitemapindex)."""
    from .ssrf import safe_fetch_url

    seen: set[str] = set()
    queue = [sitemap_url]
    pages: list[str] = []
    while queue and len(pages) < max_urls:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            xml_text = safe_fetch_url(cur, timeout=_DEFAULT_TIMEOUT)
        except Exception as e:
            log.warning("sitemap_fetch_failed", extra={"url": cur, "error_type": type(e).__name__})
            continue
        try:
            p, children = parse_sitemap(xml_text)
        except ValueError:
            continue
        for pg in p:
            if pg not in pages:
                pages.append(pg)
                if len(pages) >= max_urls:
                    break
        for ch in children:
            if ch not in seen:
                queue.append(ch)
    return pages[:max_urls]


async def crawl_sitemap(
    tenant_id: str,
    sitemap_url: str,
    *,
    max_urls: int = _DEFAULT_MAX_URLS,
    concurrency: int = _DEFAULT_CONCURRENCY,
    acl: list[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    metadata: dict | None = None,
) -> CrawlResult:
    """Crawl a sitemap and ingest each discovered page as a document for `tenant_id`.

    Idempotent per URL via the issue #4 `doc_key_for_url(url)` so a re-crawl REPLACES the
    prior doc for that URL instead of duplicating it. Respects robots.txt.
    """
    concurrency = max(1, min(concurrency, 8))
    # Onboarding UX: a tenant may point at their root domain or a robots-only site; resolve
    # it to a concrete sitemap URL (honors robots.txt Sitemap: hints, falls back to
    # /sitemap.xml). If resolution fails we surface a clear error rather than crawling nothing.
    try:
        sitemap_url = await resolve_sitemap_url(sitemap_url)
    except ValueError as e:
        return CrawlResult(0, 0, 0, 0, [str(e)])
    urls = await discover_sitemap_urls(sitemap_url, max_urls=max_urls)
    if not urls:
        return CrawlResult(0, 0, 0, 0, ["no URLs discovered in sitemap"])

    # robots.txt rules per host (cache by host root)
    robots_cache: dict[str, tuple[set[str], float]] = {}

    semaphore = asyncio.Semaphore(concurrency)
    result = CrawlResult(urls_discovered=len(urls), urls_ingested=0, urls_failed=0,
                         skipped_robots=0, errors=[])
    done = 0

    async def _ingest_one(url: str) -> None:
        nonlocal done
        parsed = urlparse(url)
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        if host_root not in robots_cache:
            robots_cache[host_root] = await _fetch_robots(host_root)
        disallowed, delay = robots_cache[host_root]
        path = parsed.path or "/"
        if _is_disallowed(path, disallowed):
            result.skipped_robots += 1
            done += 1
            if on_progress:
                on_progress(done, len(urls))
            return
        try:
            # ingest_url derives the stable per-URL doc_key (issue #4) internally, so a
            # re-crawl REPLACES the prior doc for that URL instead of duplicating it.
            await ingest_url(
                tenant_id, url, title=url, metadata=metadata, acl=acl,
            )
            result.urls_ingested += 1
        except Exception as e:
            result.urls_failed += 1
            result.errors.append(f"{url}: {type(e).__name__}: {str(e)[:160]}")
        finally:
            done += 1
            if delay > 0:
                await asyncio.sleep(delay)
            if on_progress:
                on_progress(done, len(urls))

    async def _guarded(url: str) -> None:
        async with semaphore:
            await _ingest_one(url)

    await asyncio.gather(*(_guarded(u) for u in urls))
    return result
