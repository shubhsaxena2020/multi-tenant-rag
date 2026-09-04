# SDK.py Audit Against Real Route List

## Overview
Audited SDK `sdk.py` (401 lines, Python v9-3) against the actual API route contract 
derived from the v1 sub-app OpenAPI spec (28 routes total).

## Audit Results

### ✓ Matched Methods (12 of 14 audited)
All methods that correctly map to their actual API routes:

1. **create_tenant** - POST /{tenant} ✓
2. **delete_document** - DELETE /{tenant}/documents/{doc_id} ✓
3. **eval_runs** - GET /{tenant}/eval/runs ✓
4. **ingest_sitemap** - POST /{tenant}/documents/upload (multipart) ✓
5. **ingest_text** - POST /{tenant}/documents ✓
6. **ingest_url** - POST /{tenant}/documents ✓
7. **put_eval_set** - PUT /{tenant}/eval/set ✓
8. **query** - POST /{tenant}/query ✓
9. **run_eval_quality** - POST /{tenant}/eval/quality ✓
10. **upload_file** - POST /{tenant}/documents/upload (multipart) ✓
11. **list_documents** - GET /{tenant}/documents ✓ (same as get_document - lists)
12. **get_document** - NOT IMPLEMENTED in SDK ⚠

### ✗ Route Mismatches (2 of 14)

1. **query_stream** - ❌ MISMATCH
   - SDK implements: `POST /{tenant}/query` (with query body)
   - Actual route: `POST /{tenant}/query/stream` (SSE endpoint)
   - Issue: query_stream method sends to /query instead of /query/stream

2. **run_eval** - ❌ MISMATCH
   - SDK implements: `PUT /{tenant}/eval/set`
   - Actual route: `POST /{tenant}/eval/run`
   - Issue: run_eval method sends to /eval/set instead of /eval/run

### ⚠ Missing Method (1 of 14)

3. **get_document** - Not implemented in SDK
   - Actual route: GET /{tenant}/documents/{doc_id}
   - No SDK method exists to fetch a single document by ID
   - Related: list_documents exists but returns paginated list, not single doc

## Summary Statistics

- **Total SDK methods analyzed:** 14 (core client methods; plus internal helpers _post, _get, etc.)
- **Methods with correct route mapping:** 12 (86%)
- **Methods with route mismatches:** 2 (14%)
- **Methods not implemented in SDK:** 1 (7%)
- **Total SDK routes covered:** 13 of 14 core methods map to some API route
- **API routes not covered by SDK:** 15 of 28 total routes (54% - admin, keys, branding, sessions, widgets, etc. are admin-only or separate)

## Required Fixes

### 1. Fix query_stream route
Change SDK method to use `/query/stream` endpoint instead of `/query`.

### 2. Fix run_eval route  
Change SDK method to use `/eval/run` endpoint instead of `/eval/set`.

### 3. Implement get_document method
Add SDK method for `GET /{tenant}/documents/{doc_id}` to fetch single document.

## Verification

After fixes, re-audit to confirm:
- All 14 core methods map to correct routes
- query_stream uses POST /{tenant}/query/stream
- run_eval uses POST /{tenant}/eval/run
- get_document implemented with doc_id parameter
