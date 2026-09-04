# API Surface — Route Enumeration

Generated from the running app's OpenAPI spec (`openapi.json`). This table lists every route with its method, auth scope, request model, and response model.

| Method | Route | Summary | Auth | Request Model | Response Model |
|--------|-------|---------|------|---------------|----------------|
| GET | /health | Health | None | — | `application/json` |
| GET | /health/deps | Health Deps | None | — | `application/json` |
| GET | /health/ready | Ready | None | — | `application/json` |
| GET | /widget.js | Widget Js | None | — | `application/json` |
| GET | /widget.html | Widget Html | None | — | `application/json` |
| GET | /demo | Widget Demo | None | — | `application/json` |
| GET | /admin/console | Admin Console Page | Admin-Key | — | `application/json` |
| GET | /doc/{tenant}/{doc_id} | Document Viewer | None | path: `tenant` (string), path: `doc_id` (string) | `application/json`, 422: `HTTPValidationError` |
| GET | /health/slo | Health Slo | None | — | `application/json` |
| GET | /metrics | Metrics | Admin-Key or Authorization header | query: `limit` (integer, default 200), header: `Admin-Key`, header: `authorization` | `application/json`, 422: `HTTPValidationError` |
| GET | /api/v1/openapi.json | Openapi Schema | Admin-Key or Authorization header | header: `Admin-Key`, header: `authorization` | `application/json`, 422: `HTTPValidationError` |
| GET | /audit | Audit Log | Admin-Key or Authorization header | query: `limit` (integer, default 200), header: `Admin-Key`, header: `authorization` | `application/json`, 422: `HTTPValidationError` |
| GET | /audit/verify | Audit Verify | Admin-Key or Authorization header | header: `Admin-Key`, header: `authorization` | `application/json`, 422: `HTTPValidationError` |
| GET | /admin/usage/{tenant} | Admin Usage | Admin-Key or Authorization header | path: `tenant` (string), query: `days` (integer, default 30), query: `fmt` (string, default json), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/feedback/{tenant} | Admin Feedback | Admin-Key or Authorization header | path: `tenant` (string), query: `limit` (integer, default 100), query: `rating` (string, optional), query: `fmt` (string, default json), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/leads/{tenant} | Admin Leads | Admin-Key or Authorization header | path: `tenant` (string), query: `limit` (integer, default 100), query: `fmt` (string, default json), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/knowledge-gaps/{tenant} | Admin Knowledge Gaps | Admin-Key or Authorization header | path: `tenant` (string), query: `limit` (integer, default 100), query: `fmt` (string, default json), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/token-usage/{tenant} | Admin Token Usage | Admin-Key or Authorization header | path: `tenant` (string), query: `days` (integer, default 30), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/token-usage | Admin Token Usage Fleet | Admin-Key or Authorization header | query: `days` (integer, default 30), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/analytics/{tenant} | Admin Analytics | Admin-Key or Authorization header | path: `tenant` (string), query: `days` (integer, default 30), query: `fmt` (string, default json), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/summary | Admin Summary | Admin-Key or Authorization header | query: `days` (integer, default 30), header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| GET | /admin/release-incidents | Admin Release Incidents | Admin-Key or Authorization header | header: `Admin-Key`, header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/branding | Update Branding | Admin-Key or Authorization header | path: `tenant` (string), header: `Admin-Key`, header: `authorization` | body: `application/json` (TenantBranding), 200: `TenantOut`, 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/query/stream | Query Stream | Authorization header | path: `tenant` (string), header: `authorization`, body: `application/json` (QueryRequest) | SSE stream: `sources`, `token`, `done`, `error` events |
| GET | /api/v1/{tenant}/usage | Tenant Usage | Authorization header | path: `tenant` (string), query: `days` (integer, default 30), header: `authorization` | `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/feedback | Post Feedback | Authorization header | path: `tenant` (string), header: `authorization`, body: `application/json` (FeedbackIn) | 200: `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/handoff | Post Handoff | Authorization header | path: `tenant` (string), header: `authorization`, body: `application/json` (HandoffIn) | 200: `application/json` object (additionalProperties), 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/keys | Rotate Api Key | Authorization | — | `TenantOut` |
| GET | /api/v1/{tenant}/keys | List Keys | Authorization | — | `#/components/schemas/TenantKeysOut` |
| POST | /api/v1/{tenant}/keys/publishable | Create Publishable Key | Authorization | body: `application/json` (PublishableKeyRequest) | `TenantOut` |
| POST | /api/v1/{tenant}/keys/secret | Create Secret Key | Authorization | body: `application/json` (SecretKeyRequest) | `TenantOut` |
| DELETE | /api/v1/{tenant}/keys/{prefix} | Revoke Key | Authorization | — | `{dict with revoked count}` |
| PATCH | /api/v1/{tenant}/keys/{prefix}/expiry | Set Key Expiry | Authorization | body: `application/json` (KeyExpiryRequest) | `{dict with prefix, expires_at, updated}` |
| GET | /api/v1/{tenant}/branding | Update Branding | Admin-Key or Authorization | path: `tenant` (string), header: `Admin-Key`, header: `authorization` | body: `application/json` (TenantBranding), 200: `TenantOut`, 422: `HTTPValidationError` |
| POST | /api/v1/{tenant}/query/stream | Query Stream | Authorization | path: `tenant` (string), header: `authorization`, body: `application/json` (QueryRequest) | SSE stream: `sources`, `token`, `done`, `error` events |

## Auth Convention

- **No auth**: Health, widget, demo, document viewer routes
- **Admin-Key**: Admin routes (`/admin/console`, `/metrics`, `/audit`, `/api/v1/openapi.json`, all `/admin/*` endpoints). The `Admin-Key` header is checked via `require_admin` dependency.
- **Authorization**: User-facing API routes (`/api/v1/{tenant}/*`). The `Authorization` header carries the bearer token.

## Notes

- All routes return 422 `HTTPValidationError` on validation failure, with detail array containing `loc`, `msg`, `type`, `ctx`.
- SSE route (`/query/stream`) has a non-standard response shape emitted as events (`sources`, `token`, `done`, `error`).
- Tenant path parameters are `{tenant}` across user-facing routes; admin routes may use `{tenant}` or no tenant path.
- Response models marked `additionalProperties` are `dict`-like shapes derived from Pydantic models; exact schemas are defined in the OpenAPI components.