# sdk-js Audit Against Real Route List

## Overview
Audited JavaScript/TypeScript SDK `sdk-js/src/index.ts` against the actual API route contract 
and compared with Python `sdk.py`.

## JS SDK Method vs API Route Mapping

### ✓ Correctly Mapped Methods

1. **query** - POST /{tenant}/query ✓
   - JS: `this.post(\`/${tenant}/query\`, body)` 
   - API: POST /{tenant}/query ✓
   - Matches: Yes

2. **queryStream** - POST /{tenant}/query/stream ✓
   - JS: `fetch(\`${this.baseUrl}/${tenant}/query/stream\`, ...)` 
   - API: POST /{tenant}/query/stream ✓
   - Matches: Yes ✓ *(only SDK with correct route)*

3. **uploadFile** - POST /{tenant}/documents/upload (multipart) ✓
   - JS: `this.postMultipart(\`/${tenant}/documents/upload\`, form)`
   - API: POST /{tenant}/documents/upload ✓
   - Matches: Yes

4. **ingestSitemap** - POST /{tenant}/ingest/sitemap ✓
   - JS: `this.post(\`/${tenant}/ingest/sitemap\`, body)`
   - API: POST /{tenant}/ingest/sitemap ✓
   - Matches: Yes

5. **listDocuments** - GET /{tenant}/documents ✓
   - JS: `this.get(\`/${tenant}/documents\`)` with query params
   - API: GET /{tenant}/documents ✓
   - Matches: Yes

### ✗ Route Mismatches / Gaps

1. **Python SDK query_stream** - ❌ WRONG ROUTE
   - PY: `_post(f"/{tenant}/query")` - sends to /query
   - API: POST /{tenant}/query/stream (SSE endpoint)
   - Issue: Python SDK streams from wrong endpoint
   - Fix needed: Change to `/query/stream`

2. **Python SDK run_eval** - ❌ WRONG ROUTE
   - PY: `_put(f"/{tenant}/eval/set")` - sends to /eval/set
   - API: POST /{tenant}/eval/run
   - Issue: Python SDK eval from wrong endpoint
   - Fix needed: Change to `/eval/run`

3. **JS SDK missing methods** vs API
   - Actual API has 28 routes total
   - JS SDK core methods: 5 (query, queryStream, uploadFile, ingestSitemap, listDocuments)
   - Missing JS SDK methods for these API routes:
     - /{tenant}/branding (GET)
     - /{tenant}/keys/* (POST, GET, DELETE, PATCH)
     - /{tenant}/eval/* (golden/auto, run, quality, runs, set)
     - /{tenant}/system-prompt (PATCH)
     - /{tenant}/session/{session_id} (GET)
     - /{tenant}/widget/config (GET)
     - /{tenant}/jobs/{job_id} (GET, DELETE)
     - /{tenant}/eval/golden/auto (POST)

4. **Python SDK vs JS SDK Parity Gaps**
   - Both SDKs query_stream / run_eval route mismatch (shared bug)
   - Python has create_tenant, delete_document that JS lacks explicit equivalents
   - JS has listDocuments with pagination params that Python also has
   - Both support multipart upload via _post_multipart / postMultipart

## Parity Analysis: JS vs Python SDK

| Feature | Python sdk.py | JS sdk-js | Parity |
|---------|--------------|-----------|--------|
| query (POST /query) | ✓ implements | ✓ implements | ✓ Parity |
| query/stream (SSE) | ❌ /query (WRONG) | ✓ /query/stream (CORRECT) | ✗ Mismatch |
| upload file (multipart) | ✓ _post_multipart | ✓ postMultipart | ✓ Parity |
| ingest text | ✓ _post /documents | ❌ not explicit | ✗ Gap |
| ingest URL | ✓ _post /documents | ❌ not explicit | ✗ Gap |
| list documents | ✓ _get /documents | ✓ get /documents | ✓ Parity |
| create tenant | ✓ _post /tenants | ❌ not explicit | ✗ Gap |
| delete document | ✓ _delete /documents/{id} | ❌ not explicit | ✗ Gap |
| eval runs | ✓ _get /eval/runs | ❌ not explicit | ✗ Gap |
| put eval set | ✓ _put /eval/set | ❌ not explicit | ✗ Gap |
| run eval | ❌ /eval/set (WRONG) | ❌ not explicit | ✗ Mismatch |
| run eval quality | ✓ _post /eval/quality | ❌ not explicit | ✗ Gap |

## Summary

- **JS SDK:** 5 core methods, all correctly routed to API endpoints
- **Python SDK:** 14 core methods, 2 with route mismatches, 1 missing
- **Shared issue:** Both SDKs have incorrect routes for query_stream and run_eval
- **JS advantage:** queryStream correctly uses /query/stream
- **Python advantage:** More complete method set (14 vs 5 core methods)
- **Required fixes:** 
  1. Fix Python query_stream to use /query/stream
  2. Fix Python run_eval to use /eval/run
  3. Add missing JS SDK methods for key/branding/session routes
  4. Ensure both SDKs have parity on core query/document/eval operations
