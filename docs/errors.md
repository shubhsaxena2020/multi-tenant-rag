# Error Taxonomy

## Overview

All API responses follow consistent error conventions. No stack traces are ever leaked to clients. Every error response has the same top-level shape:

```json
{
  "error": "<short machine-readable error type>",
  "detail": "<human-readable description>",
  "degraded": <bool | null>
}
```

The `degraded` field is only present when the service is partially unavailable (see `RagError` below).

---

## Error Types by Category

### 4xx Client Errors (Validation / Not Found)

| Status | Error Key | Detail Convention | Example |
|--------|-----------|-------------------|---------|
| **400** | `validation_error` | Describe invalid field(s) | `"Invalid or missing required field"` |
| **401** | `authentication_error` | Missing/invalid auth token | `"Missing or invalid authentication token"` |
| **403** | `authorization_error` | Insufficient permissions | `"Access denied: insufficient permissions"` |
| **404** | `not_found` | Resource does not exist | `"Tenant not found"` |
| **422** | `validation_error` | Field-level validation errors | See [Validation Errors](#validation-errors) below |

### 5xx Server Errors

| Status | Error Key | Detail Convention | Example |
|--------|-----------|-------------------|---------|
| **500** | `internal_error` | Unexpected server error | `"internal error"` |
| **503** | `service_unavailable` | Dependency degraded (Qdrant, embedder, reranker) | `"qdrant temporarily unavailable (degraded mode)"` |

---

## Validation Errors (422)

FastAPI\x27s default 422 validation error response is overridden to a consistent shape:

```json
{
  "error": "validation_error",
  "detail": [
    {
      "loc": ["body", "field_name", ...],
      "msg": "<human-readable message>",
      "type": "<validation error type>",
      "ctx": { "<context keys>" }
    }
  ]
}
```

The `detail` field is an array of validation error objects, each containing:
- `loc`: JSON path to the invalid field
- `msg`: Description of the validation failure
- `type`: Error type name (e.g., "missing", "value_error.strict")
- `ctx`: Additional context (e.g., `min_length`, `max_length`)

---

## RagError (Service-Degraded Errors)

Raised when a backend dependency (Qdrant, embedder, reranker) is partially unavailable. Returns a clean, contract-shaped response:

```json
{
  "error": "service_degraded",
  "detail": "<public_detail from RagError>",
  "degraded": true
}
```

The `RagError` class:

| Field | Type | Description |
|-------|------|-------------|
| `public_detail` | `str` | Safe to return to clients; describes the issue in user terms |
| `internal` | `str \| None` | Internal diagnostic info; **never** leaked to clients |
| `degraded` | `bool` | `True` when service is partially available; `False` when fully unavailable |

Handler: `_rag_error_handler` (line 257-272 in `app/main.py`) formats `RagError` into the standard error response shape.

---

## HTTPException (Generic Server Errors)

All `HTTPException` raises follow this convention:

```json
{
  "error": "<error_key>",
  "detail": "<descriptive_message>",
  "degraded": null
}
```

- **503**: `"qdrant unreachable"` (from line 396 of `app/main.py`)
- **404**: `"tenant not found"` / `"not found"` (various routes)
- **422**: Various validation messages (lines 962-975 of `app/main.py`)

---

## Convention Summary

| Aspect | Rule |
|--------|------|
| **Top-level keys** | `error`, `detail`, `degraded` (optional) |
| **`error` value** | Machine-readable error type key (see table above) |
| **`detail` value** | String (top-level) or array of validation error objects (422) |
| **`degraded` value** | `true`, `false`, or `null` (absent when not applicable) |
| **No stack traces** | Never returned to clients; always logged server-side |
| **Error keys** | Use the consistent keys defined in this taxonomy |

---

## Global Error Handler

The `_unhandled_error_handler` (line 325 in `app/main.py`) serves as the last-resort handler. It logs the exception type only and returns:

```json
{
  "error": "internal error",
  "degraded": false
}
```

This ensures that even unexpected exceptions never leak raw Python exception text or stack traces to clients.