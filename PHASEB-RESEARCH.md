# PHASE B — Production Ingestion Pipeline (research + decisions)

Goal (from rag_v10_goal.txt PHASE B): close the gap between "demo RAG" and "a real
paying client website can actually use this" by hardening ingestion:
  1. document content-hash DEDUPLICATION (re-ingest must replace stale chunks, not duplicate)
  2. sitemap.xml CRAWLER (onboard a client site via sitemap, not one URL at a time)
  3. PDF / Markdown FILE UPLOAD parsing
  4. DOCUMENT CATALOG listing endpoint (tenant sees what's indexed)

## 1. Content-hash deduplication (idempotent ingestion)
- Best practice (RAG architecture 2025, Medium; Apache Airflow ingestion pipeline 2026;
  VertexFrontier RAG data preprocessing 2026): generate a content hash per chunk and use
  idempotent upserts keyed on (tenant_id, doc_id, chunk hash) so re-ingestion is a no-op or
  a clean replace, never a duplicate.
- Decision: deterministic SHA-256 over the *chunk text* (not the embedding) → stable across
  re-embeds and model swaps. Store `content_hash` in the Qdrant chunk payload. On re-ingest
  of a known `doc_id`, delete the previous chunk set for that (tenant, doc_id) then upsert —
  this both dedupes AND lets edits update in place (idempotent replace). For sitemap recrawls
  we key by `source_url` so a re-crawl updates the same doc rather than fanning out copies.
- Isolation is preserved: every delete/upsert is tenant-scoped (existing `tenant_id` filter).
- Idempotency is also exposed to callers: a client can pass the same `doc_id` to update.

## 2. Sitemap crawler (respectful, SSRF-safe)
- Respect robots.txt Allow/Disallow + Crawl-delay (DZone 2026 "Respecting robots.txt";
  Parallel.ai 2026 "What is a web crawler"): honor Crawl-delay between requests to a host,
  cap concurrency (default 4), cap per-host requests, cap total URLs (max_urls default 100).
- Reuse the existing SSRF-safe `app.ingestion.ssrf.safe_fetch_url` (scheme allowlist,
  public-IP only, port 80/443, redirect revalidation, size cap). Public sitemaps are exactly
  the allowed surface, so we get SSRF protection for free.
- Sitemap parsing: handle both <urlset><url><loc> and <sitemapindex><sitemap><loc> (recurse
  into child sitemaps). Robots: look for Sitemap: hints, else try /sitemap.xml / /sitemap_index.xml.
- Deliver as an ASYNC job (POST /{tenant}/ingest/sitemap → 202 with job_id) so large sites
  don't block the request; progress reflects URLs crawled.

## 3. PDF / Markdown file upload
- PDF extraction benchmark (2025/2026): pypdf = reliable zero-dep default for plain text;
  pypdfium2 = fastest; PyMuPDF (fitz) = best fidelity/structure. Decision: add `pypdf`
  (pure-python, tiny, no native build) as the default PDF text extractor; degrade gracefully
  to a clear 400 if pypdf is not installed (operator can `uv pip install pypdf`). Markdown is
  passed through the existing markdown content_type path (already supported by chunker).
- Endpoint: POST /{tenant}/documents/upload (multipart/form-data: file, title?, content_type?,
  acl?, metadata?). content_type auto-detected from extension (.pdf→pdf, .md→markdown,
  .txt→text, .html→html, .py/.js/.ts→code) when not supplied. Secret-key required.

## 4. Document catalog
- A `documents` registry table (per-tenant) records each ingested document: doc_id, tenant_id,
  title, content_type, chunk_count, source_url (optional), source_hash (optional, for dedup),
  created_at, updated_at. Powers GET /{tenant}/documents (catalog) and deletion bookkeeping.
- DELETE /{tenant}/documents/{doc_id} now also removes the registry row + decrements chunk_count
  (currently the chunk_count is NOT decremented on delete — fix that too; it's a quota-accuracy bug).
