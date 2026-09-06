# Error Taxonomy

This document defines the standard error response shape used across all API routes.

## Standard Error Response Shape

All error responses return JSON with the following structure:

```json
{
  "error": "<error_code_or_type>",
  "detail": "<human-readable description>",
  "degraded": <boolean | omitted>
}
```

- `error`: A short machine-readable error identifier (e.g., `tenant_not_found`, `job_not_found`, `tenant_mismatch`).
- `detail`: A human-readable description of the error.
- `degraded`: `true` when the service is partially available (e.g., circuit breaker open), `false` or omitted otherwise.

## Error Classes

### 404 Not Found

| Error Code | Description | Routes |
|---|---|---|
| `tenant_not_found` | The tenant identifier was not found in the system. | `GET /{tenant}...`, `POST /{tenant}...`, `DELETE /{tenant}...`, etc. |
| `job_not_found` | The requested job ID does not exist. | `GET /{tenant}/jobs/{job_id}`, `DELETE /{tenant}/jobs/{job_id}` |

### 403 Forbidden

| Error Code | Description | Routes |
|---|---|---|
| `tenant_mismatch` | The API key's tenant does not match the path tenant. | Routes with `{tenant}` path param and auth key |

### 400 Bad Request

| Error Code | Description | Routes |
|---|---|---|
| Missing required fields | Required request body fields are missing. | Various POST routes |
| Invalid input format | Request body does not match the expected schema. | Various routes |

### 502 Bad Gateway

| Error Code | Description | Routes |
|---|---|---|
| `retrieval_unavailable` | The retrieval backend (Qdrant) is temporarily unavailable. | Query routes |

### 503 Service Unavailable

| Error Code | Description | Routes |
|---|---|---|
| `qdrant_unavailable` | The Qdrant vector store is temporarily unavailable. | All routes requiring vector store access |

## Error Handler Implementation

The centralized error handler in `app/main.py` (`_rag_error_handler`) ensures consistent error responses:

```python
@app.exception_handler(RagError)
async def _rag_error_handler(request: Request, exc: RagError):
    status_code = 503 if exc.degraded else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "error": exc.public_detail,
            "degraded": exc.degraded,
            "detail": exc.public_detail,
        },
    )
```

Unhandled exceptions follow a similar pattern with `internal error` as the error type.

## Consistency Rules

1. **All 404 responses** for "not found" errors must use `error: "tenant_not_found"` or `error: "job_not_found"` as appropriate, with `detail` describing the specific missing resource.

2. **All 403 responses** for tenant mismatch must use `error: "tenant_mismatch"`.

3. **All 503 responses** for degraded service must include `"degraded": true` in the response body.

4. **No error response** should leak raw exception types or stack traces to clients.

5. **Validation errors** (422) use the FastAPI-generated `HTTPValidationError` schema with location, message, and error type details.