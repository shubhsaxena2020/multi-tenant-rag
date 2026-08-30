"""FastAPI application: multi-tenant RAG service, versioned under /api/v1.

Isolation: tenant identity is derived server-side from the Bearer API key on every
request (never from the request body). All vector operations are scoped to the
tenant's own Qdrant collection, so cross-tenant reads are structurally impossible.

Versioning: all tenant-facing routes live under /api/v1 (mounted sub-app). The
OpenAPI schema is served at /api/v1/openapi.json and interactive docs at /api/v1/docs.
Operability: structured JSON logging + Prometheus metrics at /metrics; per-tenant and
per-IP rate limiting on every tenant route.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import jobs as job_store
from . import tenants
from .audit import append_audit, list_audit, verify_chain
from .auth import generate_api_key, generate_publishable_key, get_tenant_from_header, require_admin, require_secret_key


def _parse_expiry(value: str | None):
    """v10.8: parse a UTC ISO-8601 expiry string into a tz-aware datetime, or None.

    Raises HTTPException(422) on a malformed/naive value so callers get a clear error
    rather than a silent never-expire. Naive datetimes are rejected (we require explicit
    UTC) to avoid ambiguous expiry semantics.
    """
    if value is None:
        return None
    from datetime import UTC, datetime
    from fastapi import HTTPException
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422,
                            detail=f"invalid expires_at (expected UTC ISO-8601, e.g. 2026-12-31T23:59:59Z): {value!r}")
    if dt.tzinfo is None:
        raise HTTPException(422,
                            detail="expires_at must be timezone-aware (UTC). Append 'Z' or '+00:00'.")
    return dt.astimezone(UTC)
from .config import get_settings
from .conversation import (
    assess_confidence,
    detect_injection,
    get_session_store,
    rewrite_query,
)
from .retrieval.rewrite import rewrite_query as pre_retrieval_rewrite
from .retrieval.agentic import retrieve_multi_hop
from .plans import capabilities_for, max_top_k_for
from .faithfulness import score_faithfulness, is_refusal
from .quality_store import record_quality_bg
from .generation import generate_answer, stream_answer
from .ingestion import ingest_text, ingest_url
from .ingestion.runner import submit
from .models import (
    DocumentCatalogItem,
    DocumentCatalogPage,
    DocumentChunksOut,
    DocumentChunkOut,
    DocumentCreate,
    DocumentOut,
    EvalReportOut,
    EvalSetIn,
    FeedbackIn,
    HandoffIn,
    IngestJobRequest,
    IngestText,
    IngestUrl,
    JobStatus,
    KeyInfo,
    KeyExpiryRequest,
    PublishableKeyRequest,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
    SecretKeyRequest,
    SitemapIngestIn,
    SitemapJobOut,
    TenantBranding,
    TenantCreate,
    TenantKeysOut,
    TenantOut,
    TenantSystemPromptIn,
    WidgetConfigOut,
    UploadOut,
)
from .models import sanitize_branding
from .observability import (
    INGEST_CHUNKS,
    INGEST_JOBS,
    QUERY_HITS,
    RETRIEVAL_LATENCY,
    REWRITE_USED,
    FAITHFULNESS_SCORE,
    NO_ANSWER_TOTAL,
    MetricsMiddleware,
    get_logger,
    metrics_response,
)
from .usage import get_usage_summary, get_usage_timeseries, record_usage, record_usage_bg
from .feedback import get_feedback_summary, list_feedback, save_feedback
from .leads import get_lead_summary, list_leads, save_lead
from .knowledge_gaps import list_knowledge_gaps, record_knowledge_gap, record_knowledge_gap_bg, count_knowledge_gaps
from .token_usage import get_token_usage, get_fleet_token_usage, record_token_usage, record_token_usage_bg
from .analytics import get_analytics_csv_rows, get_tenant_analytics, get_fleet_summary
from .ratelimit import rate_limit
from .rbac import (
    PUBLIC_GROUP,
    build_acl_filter,
    resolve_acl,
)
from .resilience import RagError, circuit_status
from .retrieval import retrieve
from .validation import (
    validate_content,
    validate_content_type,
    validate_metadata,
    parse_acl_field,
    parse_json_field,
)
from .vector_store import (
    delete_document,
    delete_tenant_collection,
    ensure_collection,
    get_client,
    get_document_chunks,
)

log = get_logger("rag")

# Public root app (health, metrics, docs). Tenant API mounted at /api/v1.
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # v9-5: durable job recovery — reset any jobs orphaned in `running` by a previous
    # crashed worker back to `pending` so they get retried.
    try:
        from .db import init_db, requeue_orphaned_jobs

        await init_db()
        recovered = await requeue_orphaned_jobs()
        if recovered:
            log.info("jobs_recovered_on_startup", extra={"recovered": recovered})
    except Exception as e:
        log.warning("startup_recovery_failed", extra={"error_type": type(e).__name__})
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="RAG Service",
    version="1.0.0",
    description="Multi-tenant Retrieval-Augmented Generation service. Tenant API is "
    "versioned under /api/v1. See /api/v1/docs for the interactive contract.",
    openapi_url=None,  # root has no schema; /api/v1 owns the versioned contract
)
# ---------------- Safe CORS (deny-by-default, allowlist-only) ----------------
# The embeddable widget (sdk.py / /widget.js) is loaded cross-origin inside a
# client's own site, so the browser needs CORS to call /api/v1/{tenant}/query[/stream].
# A naive `allow_origins=["*"]` would let ANY website drive a tenant's API with a
# victim's key. We therefore mirror the `allowed_embed_origins` allowlist (also used
# for CSP frame-ancestors) and DENY everything else. The matched Origin is echoed
# EXACTLY (never a wildcard, never an arbitrary client-supplied value), and we never
# set Access-Control-Allow-Credentials — auth is via the Authorization header, not
# cookies, so cross-origin credentialed requests are not needed and must stay blocked.
_CORS_ALLOW_HEADERS = ("Authorization", "Content-Type", "Admin-Key")
_CORS_ALLOW_METHODS = ("GET", "POST", "DELETE", "OPTIONS")


class CORSMiddleware:
    """Allowlist-only CORS. No Origin is ever reflected unless explicitly configured."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = ""
        for raw_key, raw_val in scope.get("headers", []):
            if raw_key == b"origin":
                origin = raw_val.decode("latin-1")
                break
        allowed = get_settings().allowed_embed_origins
        allow = bool(origin) and origin in allowed

        if scope.get("method") == "OPTIONS":
            # Preflight: only respond when the Origin is explicitly allowlisted.
            if allow:
                await self._send_preflight(send, origin)
            else:
                await _send_status(send, 403, b"")
            return

        if allow:
            async def _wrap(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, _wrap)
        else:
            await self.app(scope, receive, send)

    async def _send_preflight(self, send, origin):
        headers = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (
                b"access-control-allow-methods",
                b",".join(m.encode() for m in _CORS_ALLOW_METHODS),
            ),
            (
                b"access-control-allow-headers",
                b",".join(h.encode() for h in _CORS_ALLOW_HEADERS),
            ),
            (b"access-control-max-age", b"600"),
            (b"content-length", b"0"),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


async def _send_status(send, status, body):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


app.add_middleware(CORSMiddleware)
app.add_middleware(MetricsMiddleware)


# ---------------- Global exception handlers (v9-1 graceful degradation) ----------------
# A Qdrant/embedder/reranker outage used to produce raw HTTP 500 stack traces on every
# chatbot query. These handlers convert backend failures into a clean, friendly response
# carrying `degraded: true` instead of crashing — so client chatbots look professional
# during any infra hiccup.
@app.exception_handler(RagError)
async def _rag_error_handler(request: Request, exc: RagError):
    log.warning(
        "rag_error",
        extra={"path": request.url.path, "error_type": type(exc).__name__,
               "detail": exc.public_detail},
    )
    status_code = 503 if exc.degraded else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "error": exc.public_detail,
            "degraded": exc.degraded,
            "detail": exc.public_detail,
        },
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    # Last-resort: never leak raw exception text/stack to clients. Log type only.
    if isinstance(exc, RagError):
        return await _rag_error_handler(request, exc)
    log.error("unhandled_error", extra={"path": request.url.path, "error_type": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content={"error": "internal error", "degraded": False,
                 "detail": type(exc).__name__},
    )

v1 = FastAPI(
    title="RAG Service API",
    version="1.0.0",
    root_path="/api/v1",  # so the generated OpenAPI + Swagger reflect the real mounted URL
    description=(
        "Versioned multi-tenant RAG API. Every tenant route requires "
        "`Authorization: Bearer *** Tenant identity is resolved "
        "server-side from the key; the {tenant} path segment is informational."
    ),
    # E: docs/openapi are unauthenticated by default in FastAPI — disable the built-in
    # endpoints and serve them only via the admin-gated routes below so they don't
    # disclose tenant-id/path/schema info to the public.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


TenantDep = Annotated[tenants.TenantRow, Depends(get_tenant_from_header)]


async def audit_event(action: str, actor: str, target: str = "", meta: dict | None = None) -> None:
    """Best-effort, fail-open audit append. Never raises (see app/audit.py).

    `actor` is always server-derived (admin key fingerprint or 'system'), never the
    untrusted request body — so the audit trail itself can't be spoofed by a caller.
    """
    # Hash the actor to a stable, non-secret fingerprint. Applies to EVERY non-system
    # actor (short OR long) so a short ADMIN_API_KEY is never written to the audit table
    # in plaintext (P1 #3). `system` is the only literal passthrough (not a secret).
    if actor and actor != "system":
        actor = "admin:" + hashlib.sha256(actor.encode()).hexdigest()[:16]
    await append_audit(action, actor, target=target, meta=meta)


async def _audit_data_plane(action: str, tenant_id: str, target: str = "", meta: dict | None = None) -> None:
    """Sampled, fail-open audit of tenant data-plane actions (ingest/query/delete/eval).

    P1 #5: the accountability trail previously only covered admin actions. High-volume
    data-plane events are sampled (config.audit_sample_rate) so the trail is observable
    without writing every row. The tenant_id (a non-secret opaque id) is the actor; never
    the caller-supplied body. We await append_audit directly (it is itself fail-open and
    never raises) so the write is deterministic and testable; the cost is one sampled
    sqlite insert per event — sub-millisecond, and reduced further by lowering
    audit_sample_rate in high-throughput deployments.
    """
    import random

    s = get_settings()
    if s.audit_sample_rate <= 0:
        return
    if random.random() >= s.audit_sample_rate:
        return
    await append_audit(action, "tenant:" + tenant_id, target=target, meta=meta or {})


def _resolved_acl(requested: list[str] | None, auth: tenants.TenantRow, *, default_to_public: bool) -> list[str]:
    """Server-side RBAC: narrow caller-requested groups to what the tenant is provisioned
    for. Unauthorized groups are dropped and surfaced in telemetry (self-escalation attempt).
    Returns the concrete acl list to apply (never None)."""
    eff = resolve_acl(requested, auth.allowed_groups, default_to_public=default_to_public)
    if requested is not None:
        dropped = [g for g in requested if g not in eff and g != PUBLIC_GROUP]
        if dropped:
            log.warning(
                "rbac_self_escalation_blocked",
                extra={"tenant_id": auth.tenant_id, "dropped_groups": dropped,
                       "allowed": auth.allowed_groups},
            )
    return eff


# ---------------- Root (operability) routes ----------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "rag-service", "version": "1.0.0"}


@app.get("/health/deps")
def health_deps():
    """Dependency/circuit-breaker status for ops dashboards (v9-1 resilience)."""
    return {
        "status": "ok",
        "circuit_breakers": {
            "qdrant": circuit_status("qdrant"),
            "embedder": circuit_status("embedder"),
            "reranker": circuit_status("reranker"),
        },
    }


@app.get("/health/ready")
def ready():
    try:
        c = get_client()
        c.get_collections()
        return {"status": "ready", "qdrant": "reachable"}
    except Exception as exc:
        # Never leak raw backend exception text/stack to clients (G: log sanitization).
        log.warning("qdrant_unreachable", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=503, detail="qdrant unreachable")


# ---------------- v9-3: embeddable widget ----------------
def _frame_ancestors_csp() -> str:
    """CSP frame-ancestors from allowed_embed_origins. Empty list => 'none' (no embedding)."""
    origins = get_settings().allowed_embed_origins
    if not origins:
        return "frame-ancestors 'none'"
    return "frame-ancestors " + " ".join(origins)


@app.get("/widget.js")
def widget_js():
    from pathlib import Path

    p = Path(__file__).parent / "static" / "widget.js"
    body = p.read_text(encoding="utf-8")
    return HTMLResponse(body, media_type="application/javascript",
                        headers={"Content-Security-Policy": _frame_ancestors_csp()})


@app.get("/widget.html")
def widget_html():
    from pathlib import Path

    p = Path(__file__).parent / "static" / "widget.html"
    body = p.read_text(encoding="utf-8")
    # The iframe itself is locked to allowed embed origins; the inner page gets a
    # permissive-enough CSP for its own scripts but no frame nesting beyond what we set.
    return HTMLResponse(body, media_type="text/html",
                        headers={"Content-Security-Policy": _frame_ancestors_csp() + "; default-src 'self' 'unsafe-inline'"})


@app.get("/demo")
def widget_demo():
    """Hosted demo page that embeds the chat widget (PHASE C). A visitor can paste a
    publishable key (`pk_*`, read-only — safe for client-side embedding) and a tenant name
    to try the widget live. Never expose a secret key (`rk_*`) here.
    """
    from pathlib import Path

    p = Path(__file__).parent / "static" / "index.html"
    if not p.exists():
        return HTMLResponse("<h1>Demo not found</h1>", status_code=404)
    body = p.read_text(encoding="utf-8")
    return HTMLResponse(body, media_type="text/html")


@app.get("/admin/console")
def admin_console_page():
    """Lightweight operator console — a real HTML page (no raw curl) for tenant management,
    per-tenant document lists, and widget config. The operator pastes the Admin-Key in the
    browser; it is sent only as the Admin-Key header to the API. The page itself is unauthenticated
    (it renders an input); every data call behind it is Admin-Key gated and fail-closed."""
    from pathlib import Path

    p = Path(__file__).parent / "static" / "admin.html"
    if not p.exists():
        return HTMLResponse("<h1>Admin console not found</h1>", status_code=404)
    body = p.read_text(encoding="utf-8")
    return HTMLResponse(body, media_type="text/html")


@app.get("/doc/{tenant}/{doc_id}")
def document_viewer(tenant: str, doc_id: str):
    """Hosted source viewer for citation deep-links (issue #6).

    Rendered as a real, openable page so every citation is a clickable link — including for
    documents ingested by upload/text (which have no external `source_url`). The tenant's
    secret key is read from the URL *fragment* (`#token=...`) so it is never sent to the
    server in the request line or leaked via Referer; the page then fetches the document via
    the API with `Authorization: Bearer ***.
    """
    html = _DOC_VIEWER_HTML.format(tenant=tenant, doc_id=doc_id, api_base="/api/v1")
    return HTMLResponse(
        html,
        media_type="text/html",
        headers={"Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'"},
    )


_DOC_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Source &mdash; {tenant}</title>
<style>
  body{{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin:0; background:#0f172a; color:#e2e8f0; }}
  header{{ padding:14px 18px; background:#1e293b; position:sticky; top:0; border-bottom:1px solid #334155; }}
  header a{{ color:#38bdf8; }}
  main{{ max-width:820px; margin:0 auto; padding:20px; }}
  .meta{{ color:#94a3b8; font-size:13px; margin:6px 0 18px; }}
  .chunk{{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px 16px; margin:12px 0; white-space:pre-wrap; line-height:1.55; }}
  .chunk-id{{ color:#64748b; font-size:11px; }}
  #err{{ color:#fca5a5; }}
</style></head>
<body>
<header><strong id="title">Loading source&hellip;</strong></header>
<main>
  <div class="meta" id="meta"></div>
  <div id="err"></div>
  <div id="chunks"></div>
</main>
<script>
  var TENANT = "{tenant}", DOC = "{doc_id}", API = "{api_base}";
  function token() {{
    var h = location.hash || "";
    var m = h.match(/token=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }}
  function esc(s) {{ var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }}
  (async function () {{
    var tk = token();
    if (!tk) {{ document.getElementById("err").textContent = "Missing access token (#token=...). Open this link from the chat widget."; return; }}
    try {{
      var r = await fetch(API + "/" + encodeURIComponent(TENANT) + "/documents/" + encodeURIComponent(DOC), {{
        headers: {{ "Authorization": "Bearer " + tk }}
      }});
      if (!r.ok) {{ document.getElementById("err").textContent = "Error " + r.status; return; }}
      var d = await r.json();
      document.getElementById("title").textContent = d.title || DOC;
      var meta = document.getElementById("meta");
      meta.innerHTML = "content_type: " + esc(d.content_type);
      if (d.source_url) meta.innerHTML += ' &middot; <a href="' + esc(d.source_url) + '" target="_blank" rel="noopener noreferrer">original source</a>';
      var box = document.getElementById("chunks");
      (d.chunks || []).forEach(function (c, i) {{
        var el = document.createElement("div"); el.className = "chunk";
        el.innerHTML = '<div class="chunk-id">chunk ' + (i + 1) + (c.title ? " &middot; " + esc(c.title) : "") + '</div>' + esc(c.text);
        box.appendChild(el);
      }});
    }} catch (e) {{ document.getElementById("err").textContent = "Failed to load: " + e.message; }}
  }})();
</script>
</body></html>"""


@app.get("/health/slo")
def health_slo():
    """v9-5: current SLO status (availability + p95 latency) vs configured targets."""
    from .config import get_settings
    from .observability import compute_slo_status

    s = get_settings()
    status = compute_slo_status(s.slo_latency_p95_s, s.slo_availability)
    status["status"] = "ok" if (status["availability_met"] and status["latency_met"]) else "breach"
    return status


@app.get("/metrics")
def metrics(_: None = Depends(require_admin)):
    body, ctype = metrics_response()
    return body, {"content-type": ctype}


# E: admin-gated OpenAPI schema + interactive docs. Served on the root app (not the
# public v1 sub-app) so only an operator with the Admin-Key can fetch the contract.
@app.get("/api/v1/openapi.json")
def openapi_schema(_: None = Depends(require_admin)):
    return v1.openapi()


@app.get("/api/v1/docs", include_in_schema=False)
def docs(_: None = Depends(require_admin)):
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="RAG Service API")


# ---------------- Admin: tamper-evident audit log ----------------
@app.get("/audit")
async def audit_log(_: None = Depends(require_admin), limit: int = 200):
    """Read the hash-chained audit trail. Admin-gated (fail-closed like /metrics)."""
    rows = await list_audit(limit=limit)
    return {"entries": rows, "count": len(rows)}


@app.get("/audit/verify")
async def audit_verify(_: None = Depends(require_admin)):
    """Verify chain integrity. `ok=False` + `first_break_id` indicates tampering."""
    return await verify_chain()


# ---------------- Admin: per-tenant usage & business reporting (PHASE D #27) ----------------
@app.get("/admin/usage/{tenant}", response_model=dict)
async def admin_usage(tenant: str, _: None = Depends(require_admin), days: int = 30,
                      fmt: str = "json"):
    """Operator-only business reporting for a tenant: current-period summary plus a daily
    timeseries for charts/exports. `fmt=csv` returns an exportable CSV (text/csv) of the
    daily rollup so finance/ops can drop it into a spreadsheet."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    summary = await get_usage_summary(tenant, days=days)
    series = await get_usage_timeseries(tenant, days=days)
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "queries", "docs_ingested", "chunks_ingested", "eval_runs", "total"])
        for row in series:
            w.writerow([row["date"], row["queries"], row["docs_ingested"],
                        row["chunks_ingested"], row["eval_runs"], row["total"]])
        csv_text = buf.getvalue()
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=usage_{tenant}.csv"})
    return {"summary": summary.to_dict(), "timeseries": series}


# ---------------- Admin: per-tenant feedback reporting (PHASE D #29) ----------------
@app.get("/admin/feedback/{tenant}", response_model=dict)
async def admin_feedback(tenant: str, _: None = Depends(require_admin), limit: int = 100,
                         rating: str | None = None, fmt: str = "json"):
    """Operator-only feedback report for a tenant: summary (up/down/total + positive_rate) plus
    recent entries. `fmt=csv` returns an exportable CSV of the entries."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    summary = await get_feedback_summary(tenant)
    entries = await list_feedback(tenant, limit=limit, rating=rating)
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "rating", "session_id", "message_id", "question", "answer", "comment", "ts"])
        for e in entries:
            w.writerow([e["id"], e["rating"], e.get("session_id"), e.get("message_id"),
                        (e.get("question") or "").replace("\n", " "),
                        (e.get("answer") or "").replace("\n", " "),
                        (e.get("comment") or "").replace("\n", " "), e["ts"]])
        csv_text = buf.getvalue()
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=feedback_{tenant}.csv"})
    return {"summary": summary, "entries": entries}


# ---------------- Admin: per-tenant lead/handoff reporting (PHASE D #33) ----------------
@app.get("/admin/leads/{tenant}", response_model=dict)
async def admin_leads(tenant: str, _: None = Depends(require_admin), limit: int = 100, fmt: str = "json"):
    """Operator-only handoff-lead report for a tenant: summary (total / with_email / with_phone)
    plus recent leads. `fmt=csv` returns an exportable CSV of the leads."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    summary = await get_lead_summary(tenant)
    entries = await list_leads(tenant, limit=limit)
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "session_id", "question", "name", "email", "phone", "message", "ts"])
        for e in entries:
            w.writerow([e["id"], e.get("session_id"), (e.get("question") or "").replace("\n", " "),
                        (e.get("name") or "").replace("\n", " "), (e.get("email") or "").replace("\n", " "),
                        (e.get("phone") or "").replace("\n", " "), (e.get("message") or "").replace("\n", " "),
                        e["ts"]])
        csv_text = buf.getvalue()
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=leads_{tenant}.csv"})
    return {"summary": summary, "entries": entries}


# ---------------- Admin: per-tenant knowledge-gap reporting (PHASE E #13) ----------------
@app.get("/admin/knowledge-gaps/{tenant}", response_model=dict)
async def admin_knowledge_gaps(tenant: str, _: None = Depends(require_admin), limit: int = 100, fmt: str = "json"):
    """Operator-only unanswered-question / knowledge-gap report for a tenant (issue #13):
    recent out-of-scope questions, most-recent first. `fmt=csv` exports the gaps. 404 on unknown tenant."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    count = await count_knowledge_gaps(tenant)
    entries = await list_knowledge_gaps(tenant, limit=limit)
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "session_id", "question", "ts"])
        for e in entries:
            w.writerow([e["id"], e.get("session_id") or "", (e.get("question") or "").replace("\n", " "),
                        e["ts"]])
        csv_text = buf.getvalue()
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=knowledge_gaps_{tenant}.csv"})
    return {"count": count, "entries": entries}


# ---------------- Admin: per-tenant token usage + cost (PHASE E #14) ----------------
@app.get("/admin/token-usage/{tenant}", response_model=dict)
async def admin_token_usage(tenant: str, _: None = Depends(require_admin), days: int = 30):
    """Operator-only per-tenant LLM token consumption + cost rollup (issue #14). Token counts
    are exact when an LLM provider is configured, estimated (flagged `estimated_rows`) when the
    self-hosted extractive fallback is used. Cost is computed from operator-set per-1k prices;
    no price is assumed by default (billing integration is a human checkpoint)."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return await get_token_usage(tenant, days=days)


@app.get("/admin/token-usage", response_model=dict)
async def admin_token_usage_fleet(_: None = Depends(require_admin), days: int = 30):
    """Operator-only fleet-wide token + cost rollup across all tenants (issue #14)."""
    return await get_fleet_token_usage(days=days)


# ---------------- Admin: unified per-tenant analytics (PHASE E #37) ----------------
@app.get("/admin/analytics/{tenant}", response_model=dict)
async def admin_analytics(tenant: str, _: None = Depends(require_admin), days: int = 30, fmt: str = "json"):
    """Operator-only unified analytics for a tenant (issue #37): combines usage volume,
    out-of-scope rate, answer-quality feedback, handoff->lead funnel, and top questions into
    one aggregate. `fmt=csv` exports a daily timeseries enriched with headline metrics."""
    from app.tenants import get_tenant

    if await get_tenant(tenant) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    if fmt == "csv":
        import csv
        import io

        rows = await get_analytics_csv_rows(tenant, days=days)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "queries", "docs_ingested", "chunks_ingested", "eval_runs",
                    "out_of_scope_rate", "feedback_up", "feedback_down", "leads_total",
                    "handoff_capture_rate"])
        for r in rows:
            w.writerow([r["date"], r["queries"], r["docs_ingested"], r["chunks_ingested"],
                        r["eval_runs"], r["out_of_scope_rate"], r["feedback_up"],
                        r["feedback_down"], r["leads_total"], r["handoff_capture_rate"]])
        csv_text = buf.getvalue()
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=analytics_{tenant}.csv"})

    return await get_tenant_analytics(tenant, days=days)


# ---------------- Admin: fleet-wide summary analytics (PHASE E #15) ----------------
@app.get("/admin/summary", response_model=dict)
async def admin_summary(_: None = Depends(require_admin), days: int = 30):
    """Operator-only fleet-wide summary analytics (issue #15): one call rolls up per-tenant
    usage, feedback, leads, knowledge gaps, and token/cost across every tenant. Fail-closed
    behind Admin-Key."""
    return await get_fleet_summary(days=days)


# ---------------- Admin: tenants ----------------
@v1.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, request: Request, _: None = Depends(require_admin)):
    admin_key = request.headers.get("Admin-Key") or request.headers.get("Authorization", "")
    tenant_id = f"t_{uuid.uuid4().hex[:12]}"
    api_key = generate_api_key()
    branding = sanitize_branding(body.branding) if body.branding else None
    row = await tenants.create_tenant(
        body.name, tenant_id, api_key, body.plan, body.allowed_groups, branding=branding
    )
    try:
        ensure_collection(get_client())
    except Exception as exc:
        log.warning("ensure_collection failed for %s: %s", tenant_id, exc)
    log.info("tenant_created", extra={"tenant_id": tenant_id, "tenant_name": body.name})
    await audit_event(
        "tenant.create", actor=admin_key, target=tenant_id,
        meta={"name": body.name, "plan": body.plan, "allowed_groups": body.allowed_groups},
    )
    return TenantOut(
        tenant_id=row.tenant_id, name=row.name, api_key=api_key,
        plan=row.plan, created_at=row.created_at, chunk_count=row.chunk_count,
        allowed_groups=row.allowed_groups, branding=row.branding or {},
    )


@v1.get("/tenants", response_model=list[TenantOut])
async def list_tenants(_: None = Depends(require_admin)):
    return [
        TenantOut(
            tenant_id=t.tenant_id, name=t.name, api_key=f"{t.api_key}...",
            plan=t.plan, created_at=t.created_at, chunk_count=t.chunk_count,
            branding=t.branding or {},
        )
        for t in await tenants.list_tenants()
    ]


@v1.delete("/tenants/{tenant_id}", status_code=status.HTTP_200_OK)
async def delete_tenant(tenant_id: str, request: Request, _: None = Depends(require_admin)):
    """Offboard a tenant: drop its Qdrant collection (hard data removal) and remove
    the registry row. This guarantees no residual vectors remain."""
    admin_key = request.headers.get("Admin-Key") or request.headers.get("Authorization", "")
    dropped = delete_tenant_collection(tenant_id)
    removed = await tenants.delete_tenant(tenant_id)
    if not removed:
        raise HTTPException(status_code=404, detail="tenant not found")
    log.info("tenant_offboarded", extra={"tenant_id": tenant_id, "collection_dropped": dropped})
    await audit_event(
        "tenant.delete", actor=admin_key, target=tenant_id,
        meta={"collection_dropped": dropped},
    )
    return {"deleted": tenant_id, "collection_dropped": dropped}


# ---------------- Synchronous ingestion (convenience, small payloads) ----------------
@v1.post("/{tenant}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def create_document(tenant: str, body: DocumentCreate, request: Request, auth: TenantDep, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    validate_content(body.content)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    from .ingestion import doc_key_for_text
    res = await ingest_text(auth.tenant_id, body.title, body.content, ct, meta, acl=acl,
                            doc_key=doc_key_for_text(body.title, ct))
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    log.info("document_ingested", extra={"tenant_id": auth.tenant_id, "chunk_count": res["chunk_count"]})
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "document"},
    )
    # PHASE D (#27): per-tenant usage metering.
    await record_usage(auth.tenant_id, "doc_ingested")
    await record_usage(auth.tenant_id, "chunk_ingested", count=res["chunk_count"])
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
        content_hash=res.get("content_hash"), previous_doc_id=res.get("previous_doc_id"),
    )


@v1.post("/{tenant}/ingest/url", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_url(tenant: str, body: IngestUrl, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    res = await ingest_url(auth.tenant_id, body.url, body.title, meta, acl=acl)
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "url"},
    )
    # PHASE D (#27): per-tenant usage metering.
    await record_usage(auth.tenant_id, "doc_ingested")
    await record_usage(auth.tenant_id, "chunk_ingested", count=res["chunk_count"])
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type="html", metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
        content_hash=res.get("content_hash"), previous_doc_id=res.get("previous_doc_id"),
    )


@v1.post("/{tenant}/ingest/text", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def ingest_from_text(tenant: str, body: IngestText, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    validate_content(body.text)
    ct = validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    from .ingestion import doc_key_for_text
    res = await ingest_text(auth.tenant_id, body.title, body.text, ct, meta, acl=acl,
                            doc_key=doc_key_for_text(body.title, ct))
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": body.title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "text"},
    )
    return DocumentOut(
        doc_id=res["doc_id"], title=res["title"], chunk_count=res["chunk_count"],
        content_type=ct, metadata=meta or {}, quarantined_chunks=res["quarantined_chunks"],
        content_hash=res.get("content_hash"), previous_doc_id=res.get("previous_doc_id"),
    )


# ---------------- Async ingestion jobs (canonical, status-tracked) ----------------
@v1.post("/{tenant}/ingest/jobs", response_model=JobStatus, status_code=status.HTTP_202_ACCEPTED)
async def create_ingest_job(tenant: str, body: IngestJobRequest, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    s = get_settings()
    # protect the embedding worker pool. NOTE: ingest limit is per-MINUTE, so window_min=1
    # (the bug here previously passed rate_ingest_jobs_per_min as window_min, enforcing
    # ~1 job/hour). Fixed via explicit named args so this class of bug can't recur.
    from fastapi import HTTPException
    from fastapi import status as _st

    from .ratelimit import _limiter
    allowed, retry = _limiter.hit(
        f"ingest:{auth.tenant_id}", limit=s.rate_ingest_jobs_per_min, window_min=1
    )
    if not allowed:
        raise HTTPException(
            status_code=_st.HTTP_429_TOO_MANY_REQUESTS,
            detail="ingest job rate limit exceeded (per tenant, per minute)",
            headers={"Retry-After": str(retry)},
        )
    # Auto-infer kind from the field that is actually present in the body
    # (model defaults kind="text", but senders may only provide one field)
    has_url = body.url is not None
    has_text = body.text is not None
    has_content = body.content is not None
    kind = "url" if has_url else ("text" if has_text else ("document" if has_content else body.kind))
    title = body.title
    acl = _resolved_acl(body.acl, auth, default_to_public=True)
    if kind == "url":
        if not body.url:
            raise HTTPException(422, "url is required for kind=url")
        payload = {"url": body.url, "title": title, "acl": acl}
    elif kind == "text":
        if not body.text:
            raise HTTPException(422, "text is required for kind=text")
        validate_content(body.text)
        payload = {"text": body.text, "title": title, "content_type": body.content_type, "acl": acl}
    elif kind == "document":
        if not body.content:
            raise HTTPException(422, "content is required for kind=document")
        validate_content(body.content)
        payload = {"content": body.content, "title": title, "content_type": body.content_type, "acl": acl}
    else:
        raise HTTPException(422, f"unknown kind: {kind}")
    validate_content_type(body.content_type)
    meta = validate_metadata(body.metadata)
    job_id = await job_store.create_job(auth.tenant_id, kind, title or (body.url or "untitled"))
    INGEST_JOBS.labels(status="pending").inc()
    submit(job_id, auth.tenant_id, kind, payload, meta)
    log.info("ingest_job_submitted", extra={"tenant_id": auth.tenant_id, "job_id": job_id, "kind": kind})
    return JobStatus(
        job_id=job_id, tenant_id=auth.tenant_id, kind=kind, status="pending",
        progress=0.0, total_chunks=0, done_chunks=0, title=title,
    )


@v1.get("/{tenant}/jobs", response_model=list[JobStatus])
async def list_ingest_jobs(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), limit: int = 50):
    rate_limit(request, auth.tenant_id)
    return [JobStatus(**j) for j in await job_store.list_jobs(auth.tenant_id, limit)]


@v1.get("/{tenant}/jobs/{job_id}", response_model=JobStatus)
async def get_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    j = await job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus(**j)


@v1.delete("/{tenant}/jobs/{job_id}", status_code=status.HTTP_200_OK)
async def delete_ingest_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    ok = await job_store.delete_job(job_id, auth.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"deleted": job_id}


# ---------------- Document management ----------------
@v1.get("/{tenant}/documents/{doc_id}", response_model=DocumentChunksOut, status_code=status.HTTP_200_OK)
async def get_document(tenant: str, doc_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """Fetch a document's full content (all chunks) for the hosted source viewer / citation
    deep-links (issue #6). Secret-key gated so a link can't expose another tenant's content."""
    rate_limit(request, auth.tenant_id)
    try:
        chunks = get_document_chunks(auth.tenant_id, doc_id)
    except Exception:
        raise HTTPException(status_code=502, detail="retrieval backend unavailable")
    if not chunks:
        raise HTTPException(status_code=404, detail="document not found")
    first = chunks[0]
    meta = first.get("metadata") or {}
    source_url = meta.get("source_url")
    return DocumentChunksOut(
        doc_id=doc_id,
        title=first.get("title") or doc_id,
        content_type=meta.get("content_type") or "text",
        source_url=source_url,
        chunks=[DocumentChunkOut(
            chunk_id=c["chunk_id"], title=c.get("title"), text=c.get("text", ""),
            metadata=c.get("metadata") or {},
        ) for c in chunks],
    )


@v1.delete("/{tenant}/documents/{doc_id}", status_code=status.HTTP_200_OK)
async def delete_doc(tenant: str, doc_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    # Drop the Qdrant vectors first (tenant-scoped), then remove the catalog/registry row so
    # the document disappears from GET /documents immediately (issue #12 accuracy fix).
    delete_document(auth.tenant_id, doc_id)
    from .db import delete_registry_by_doc_id

    await delete_registry_by_doc_id(auth.tenant_id, doc_id)
    await _audit_data_plane("tenant.delete_doc", auth.tenant_id, target=doc_id)
    return {"deleted": doc_id}


# ---------------- Widget branding (issue #23) ----------------
@v1.get("/{tenant}/widget/config", response_model=WidgetConfigOut, status_code=status.HTTP_200_OK)
async def widget_config(tenant: str, auth: TenantDep):
    """Return the tenant's branding so the embeddable widget can self-theme.

    Uses TenantDep (any key tier) so the widget's loader — which only holds the tenant's
    secret key — can fetch it. Branding is non-sensitive display configuration.
    """
    if auth.tenant_id != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    return WidgetConfigOut(tenant_id=auth.tenant_id, branding=auth.branding or {})


@v1.get("/{tenant}/session/{session_id}", response_model=dict, status_code=status.HTTP_200_OK)
async def session_history(
    tenant: str,
    session_id: str,
    auth: TenantDep,
    request: Request,
    _: None = Depends(require_secret_key),
):
    """PHASE D (#31): return the stored multi-turn history for a session so the widget can
    replay prior context after a reload. Tenant-scoped (key resolves tenant; path tenant must
    match). Sessions are lazily created, so an unknown session returns empty turns (not 404)."""
    rate_limit(request, auth.tenant_id)
    if auth.tenant_id != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    store = get_session_store()
    turns = store.history(auth.tenant_id, session_id)
    return {
        "session_id": session_id,
        "turns": [{"role": t.role, "text": t.text} for t in turns],
    }


@v1.patch("/{tenant}/branding", response_model=TenantOut, status_code=status.HTTP_200_OK)
async def update_branding(tenant: str, body: TenantBranding, request: Request, _: None = Depends(require_admin)):
    """Set/update a tenant's widget branding (admin only). Sanitized server-side."""
    clean = sanitize_branding(body.model_dump(exclude_unset=True))
    success = await tenants.set_tenant_branding(tenant, clean)
    if not success:
        raise HTTPException(status_code=404, detail="tenant not found")
    row = await tenants.get_tenant(tenant)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return TenantOut(
        tenant_id=row.tenant_id, name=row.name, api_key=f"{row.api_key}...",
        plan=row.plan, created_at=row.created_at, chunk_count=row.chunk_count,
        allowed_groups=row.allowed_groups, branding=row.branding or {},
    )


@v1.patch("/{tenant}/system-prompt", response_model=TenantOut, status_code=status.HTTP_200_OK)
async def update_system_prompt(tenant: str, body: TenantSystemPromptIn, request: Request, _: None = Depends(require_admin)):
    """Set/update a tenant's custom system prompt / persona (admin only)."""
    success = await tenants.set_tenant_system_prompt(tenant, body.system_prompt)
    if not success:
        raise HTTPException(status_code=404, detail="tenant not found")
    row = await tenants.get_tenant(tenant)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return TenantOut(
        tenant_id=row.tenant_id, name=row.name, api_key=f"{row.api_key}...",
        plan=row.plan, created_at=row.created_at, chunk_count=row.chunk_count,
        allowed_groups=row.allowed_groups, branding=row.branding or {},
        system_prompt=row.system_prompt or "",
    )


# ---------------- Sitemap onboarding (issue #7) ----------------
@v1.post("/{tenant}/ingest/sitemap", response_model=SitemapJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_sitemap_job(tenant: str, body: SitemapIngestIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """Onboard a client site by crawling its sitemap.xml. Returns a job_id to poll.

    Respectful crawler: honors robots.txt (Allow/Disallow + Crawl-delay), caps
    concurrency and total URLs, and reuses the SSRF-safe fetcher. Re-crawls are
    idempotent (each URL is upserted under a stable per-URL doc_key from issue #4), so
    re-running a sitemap REPLACES changed pages instead of duplicating them.
    """
    rate_limit(request, auth.tenant_id)
    from fastapi import HTTPException

    from .ratelimit import _limiter

    s = get_settings()
    allowed, retry = _limiter.hit(f"ingest:{auth.tenant_id}", limit=s.rate_ingest_jobs_per_min, window_min=1)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="ingest job rate limit exceeded (per tenant, per minute)",
            headers={"Retry-After": str(retry)},
        )
    job_title = body.title or f"sitemap:{body.url}"
    job_id = await job_store.create_job(auth.tenant_id, "sitemap", job_title)
    INGEST_JOBS.labels(status="pending").inc()
    meta = validate_metadata(body.metadata)
    acl = _resolved_acl(body.acl, auth, default_to_public=True)

    import asyncio

    async def _run():
        await job_store.update_job(job_id, status="running", progress=0.05)
        try:
            from .ingestion.sitemap import crawl_sitemap

            res = await crawl_sitemap(
                auth.tenant_id, body.url,
                max_urls=body.max_urls, concurrency=body.concurrency,
                acl=acl, metadata=meta,
                on_progress=lambda done, total: asyncio.create_task(
                    job_store.update_job(job_id, progress=0.1 + 0.9 * (done / total))
                ),
            )
            await job_store.update_job(
                job_id, status="completed", progress=1.0,
                result_doc_id=None,
                total_chunks=res.urls_ingested,
                done_chunks=res.urls_ingested,
            )
            summary = (f"discovered={res.urls_discovered} ingested={res.urls_ingested} "
                       f"failed={res.urls_failed} skipped_robots={res.skipped_robots}")
            await job_store.update_job(
                job_id,
                error=(summary if (res.urls_failed or res.errors) else None),
            )
            INGEST_JOBS.labels(status="completed").inc()
            log.info("sitemap_crawl_done",
                     extra={"tenant_id": auth.tenant_id, "job_id": job_id,
                            "ingested": res.urls_ingested, "failed": res.urls_failed})
        except Exception as e:
            safe_err = f"{type(e).__name__}: {str(e)[:200]}"
            await job_store.update_job(job_id, status="failed", error=safe_err)
            INGEST_JOBS.labels(status="failed").inc()
            log.error("sitemap_crawl_failed",
                      extra={"tenant_id": auth.tenant_id, "job_id": job_id, "error": safe_err})

    asyncio.create_task(_run())
    return SitemapJobOut(
        job_id=job_id, tenant_id=auth.tenant_id, status="pending",
        kind="sitemap", title=job_title,
    )


@v1.get("/{tenant}/ingest/sitemap/{job_id}", response_model=SitemapJobOut)
async def get_sitemap_job(tenant: str, job_id: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    j = await job_store.get_job(job_id, auth.tenant_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    summary = j.get("error") or ""
    urls_ingested = j.get("total_chunks") or 0
    return SitemapJobOut(
        job_id=job_id, tenant_id=auth.tenant_id, status=j["status"],
        kind="sitemap", progress=j.get("progress", 0.0),
        total_chunks=j.get("total_chunks", 0), done_chunks=j.get("done_chunks", 0),
        urls_ingested=urls_ingested,
        urls_failed=(1 if summary and "failed=" in summary and urls_ingested == 0 else 0),
        error=(summary if "failed=" in summary and urls_ingested == 0 else None),
        title=j.get("title"),
    )


@v1.get("/{tenant}/documents", response_model=DocumentCatalogPage)
async def list_documents(
    tenant: str,
    auth: TenantDep,
    request: Request,
    _: None = Depends(require_secret_key),
    limit: int = 200,
    offset: int = 0,
):
    """Document catalog — a tenant sees exactly what is indexed for them (issue #7/#12).

    Tenant-scoped (backed by document_registry, filtered on tenant_id); never cross-tenant.
    Returns a paginated page with the true `total` (independent of `limit`), so a client can
    page through a large catalog and know how many documents exist.
    """
    rate_limit(request, auth.tenant_id)
    from .db import count_documents, list_documents as _list_documents

    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows = await _list_documents(auth.tenant_id, limit=limit, offset=offset)
    total = await count_documents(auth.tenant_id)
    return DocumentCatalogPage(
        items=[DocumentCatalogItem(**d) for d in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------- File upload (issue #10) ----------------
from fastapi import File, Form, UploadFile


@v1.post("/{tenant}/documents/upload", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    tenant: str,
    request: Request,
    auth: TenantDep,
    _: None = Depends(require_secret_key),
    file: UploadFile = File(None),
    title: str | None = Form(None),
    content_type: str | None = Form(None),
    metadata: str | None = Form(None),
    acl: str | None = Form(None),
):
    """Upload a file (PDF / Markdown / HTML / code / text) and ingest it (issue #10).

    multipart/form-data: `file` (required), `title` (optional), `content_type` (optional,
    auto-detected from extension/MIME), `metadata` (optional JSON object string),
    `acl` (optional JSON array of groups). PDFs use pypdf; DOCX uses python-docx (clear 400
    if the parser is missing). Re-uploading the same file REPLACES prior chunks (issue #4
    per-file doc_key), so it never duplicates.
    """
    rate_limit(request, auth.tenant_id)
    from .ingestion.files import detect_content_type, extract_text

    upload = file
    if upload is None:
        raise HTTPException(status_code=422, detail="multipart field 'file' is required")
    fname = getattr(upload, "filename", None) or "upload"
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    routing_ct = detect_content_type(fname, content_type)
    try:
        text, pipeline_ct = extract_text(fname, data, routing_ct)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    validate_content(text)
    meta = validate_metadata(parse_json_field(metadata))
    resolved_acl = _resolved_acl(parse_acl_field(acl), auth, default_to_public=True)
    doc_title = title or fname
    # Stable per-file doc_key (issue #4): same filename+content -> replaced, not duplicated.
    import hashlib

    file_key = hashlib.sha256(f"{fname}:{text}".encode("utf-8")).hexdigest()
    doc_key = f"file:{file_key}"
    res = await ingest_text(
        auth.tenant_id, doc_title, text, pipeline_ct, meta, acl=resolved_acl, doc_key=doc_key,
    )
    INGEST_CHUNKS.inc(res["chunk_count"])
    INGEST_JOBS.labels(status="success").inc()
    await _audit_data_plane(
        "tenant.ingest", auth.tenant_id, target=res["doc_id"],
        meta={"title": doc_title, "chunks": res["chunk_count"],
              "quarantined": res["quarantined_chunks"], "kind": "upload",
              "content_type": pipeline_ct},
    )
    # PHASE D (#27): per-tenant usage metering.
    await record_usage(auth.tenant_id, "doc_ingested")
    await record_usage(auth.tenant_id, "chunk_ingested", count=res["chunk_count"])
    return UploadOut(
        doc_id=res["doc_id"], title=doc_title, chunk_count=res["chunk_count"],
        quarantined_chunks=res["quarantined_chunks"], content_type=pipeline_ct,
    )


# ---------------- Query ----------------
@v1.post("/{tenant}/query", response_model=QueryResponse)
async def query(tenant: str, body: QueryRequest, auth: TenantDep, request: Request):
    rate_limit(request, auth.tenant_id)
    s = get_settings()
    t0 = time.perf_counter()

    # (9a) Input-layer guardrail: flag overt injection/jailbreak before anything else.
    injection = bool(s.injection_guard_enabled) and detect_injection(body.question)

    # (9b) Pre-retrieval rewrite (PHASE B): clarify/expand short queries and decompose
    # multi-part questions BEFORE the vector search. Passthrough when disabled or no LLM.
    pre_rewritten, sub_questions, pre_used = pre_retrieval_rewrite(body.question, enabled=body.rewrite)
    REWRITE_USED.inc(1) if pre_used else None

    # (9c) Conversational rewrite: resolve follow-ups against session history (multi-turn).
    # Applied on top of the pre-retrieval rewrite when a session is active.
    rewritten = pre_rewritten
    was_rewritten = pre_used
    if s.rewrite_enabled and body.session_id:
        conv_rewritten, conv_used = rewrite_query(auth.tenant_id, body.session_id, pre_rewritten)
        rewritten = conv_rewritten
        was_rewritten = was_rewritten or conv_used

    acl_filter = build_acl_filter(_resolved_acl(body.acl, auth, default_to_public=False))

    # (9d) Plan-gated ceilings (PHASE C multi-hop + PHASE G caps). Resolve the tenant's plan
    # capabilities once and enforce: top_k cap and multi-hop requirement for paid plans.
    caps = capabilities_for(auth.plan)
    if body.top_k > caps.max_top_k:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "plan_top_k_limit",
                      "detail": f"top_k={body.top_k} exceeds your plan limit of {caps.max_top_k}."},
        )

    # Multi-hop retrieval (PHASE C). Standard tenants may only do a single retrieval; requesting
    # >1 hop without a multi-hop plan is rejected with 402.
    hop_count: int | None = None
    if body.hops and body.hops > 1 and not caps.allow_multi_hop:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={"error": "multi_hop_requires_paid_plan",
                      "detail": "Multi-hop retrieval (>1 hop) requires an enterprise/pro plan."},
        )
    hits = []
    degraded = False
    try:
        if body.hops and body.hops > 1 and caps.allow_multi_hop:
            merged, hop_count = retrieve_multi_hop(
                auth.tenant_id, rewritten, plan=auth.plan, requested_hops=body.hops,
                top_k=body.top_k, candidate_k=body.candidate_k, rerank=body.rerank,
                acl_filter=acl_filter,
            )
            hits = merged
        else:
            hits = retrieve(
                auth.tenant_id, rewritten, top_k=body.top_k,
                candidate_k=body.candidate_k, rerank=body.rerank, acl_filter=acl_filter,
            )
            if body.hops == 1:
                hop_count = 1
    except RagError as exc:
        # Backend degraded (Qdrant/embedder/reranker). Return a clean, contract-shaped
        # response with degraded=True rather than a 500 stack trace.
        log.warning("query_degraded", extra={"tenant_id": auth.tenant_id, "error_type": type(exc).__name__})
        degraded = True
    # Filter out any chunks that contain injection
    original_len = len(hits)
    hits = [hit for hit in hits if not detect_injection(hit["text"])]
    filtered_len = len(hits)
    if filtered_len < original_len:
        log.warning(
            "injection_detected_in_retrieved_chunks",
            extra={
                "tenant_id": auth.tenant_id,
                "filtered_count": original_len - filtered_len,
            },
        )
    RETRIEVAL_LATENCY.observe(time.perf_counter() - t0)
    QUERY_HITS.observe(len(hits))

    # (9c) Confidence gating / abstention (Self-RAG style).
    in_scope, _reason = assess_confidence(hits, s.retrieval_confidence_threshold)

    # PHASE E (#13): an out-of-scope query (low retrieval confidence, NOT an injection block)
    # is a knowledge gap — log it so operators can mine missing content. Injection is a security
    # signal and is intentionally NOT recorded as a gap.
    if not in_scope and not injection:
        await record_knowledge_gap(
            auth.tenant_id, question=body.question, session_id=body.session_id
        )

    results = [
        RetrievedChunk(
            chunk_id=h["chunk_id"], doc_id=h["doc_id"], title=h["title"],
            text=h["text"],
            score=h.get("rerank_score", h["score"]),
            rerank_score=h.get("rerank_score"),
            metadata=h.get("metadata", {}),
        )
        for h in hits
    ]

    answer = None
    if body.generate:
        if injection:
            # Do not forward a manipulative instruction into the LLM; return a safe refusal.
            answer = ("I can't follow those instructions. Ask me a question about the "
                      "documented content and I'll help.")
        elif not in_scope:
            answer = ("I don't have information on that in the available documents. "
                      "Let me connect you with support, or try rephrasing your question.")
        else:
            answer, usage = generate_answer(rewritten, hits, system_prompt=auth.system_prompt or "")
            # PHASE E (#14): meter tokens per tenant (estimated when no LLM provider is configured).
            await record_token_usage(
                auth.tenant_id, prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                estimated=bool(usage.get("estimated", False)),
                model=get_settings().llm_model, session_id=body.session_id,
            )

    # PHASE D (#19-#24): citation faithfulness + no-answer detection.
    # Score the generated answer's grounding in the retrieved context whenever an answer exists.
    faithfulness: float | None = None
    answerable: bool | None = None
    if answer is not None:
        context_chunks = [h.get("text", "") for h in hits]
        faithfulness, answerable = score_faithfulness(answer, context_chunks)
        FAITHFULNESS_SCORE.observe(faithfulness)
        # No-answer path: a safe refusal ("I don't know") is already a correct no-answer with
        # citations; leave it intact. Only when the answer is NOT a refusal but scored as
        # unanswerable do we swap in the safe no-answer message (citations still surfaced).
        if not answerable and not is_refusal(answer):
            answer = (
                "I don't have enough information in the available documents to answer that. "
                "See the cited sources for related context."
            )

    # Best-effort answer text for session history (anchors follow-up rewriting).
    turn_answer = answer or (hits[0]["text"] if hits else "")

    # Record history for the session (only when a session is in use).
    if body.session_id:
        store = get_session_store()
        store.append(auth.tenant_id, body.session_id, "user", body.question)
        store.append(auth.tenant_id, body.session_id, "assistant", turn_answer)

    log.info("query", extra={"tenant_id": auth.tenant_id, "hits": len(hits),
                             "generate": body.generate, "injection": injection,
                             "out_of_scope": (not in_scope), "rewritten": was_rewritten})
    await _audit_data_plane(
        "tenant.query", auth.tenant_id,
        meta={"hits": len(hits), "generate": body.generate, "injection": injection,
              "out_of_scope": (not in_scope), "degraded": degraded},
    )
    # PHASE D (#27): reliable per-tenant usage metering (business value), distinct from the
    # sampled audit trail. One query == one billable event. PHASE E: persist out_of_scope in
    # meta so analytics can compute the out-of-scope rate (uses the same in-scope signal the
    # response already returns — non-P0/P1 analytics enrichment, not a new security behavior).
    await record_usage(auth.tenant_id, "query", meta={"out_of_scope": bool(not in_scope)})

    # PHASE E (#25): persist retrieval-quality signals for trending/dashboards.
    if not answerable:
        NO_ANSWER_TOTAL.inc()
    rerank_delta = None
    if hits:
        try:
            rerank_delta = max(
                (h.get("rerank_score", h.get("score", 0.0)) - h.get("score", 0.0))
                for h in hits
            )
        except Exception:
            rerank_delta = None
    record_quality_bg(
        auth.tenant_id,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        hit_count=len(hits),
        faithfulness=faithfulness,
        rerank_delta=rerank_delta,
        rewrite_used=was_rewritten,
        multi_hop=bool(hop_count and hop_count > 1),
        no_answer=bool(not answerable),
    )

    return QueryResponse(
        results=results, answer=answer, tenant_id=auth.tenant_id,
        rewritten_query=rewritten if was_rewritten else None,
        sub_questions=sub_questions if (sub_questions and len(sub_questions) != 1) else None,
        hop_count=hop_count,
        faithfulness=faithfulness,
        answerable=answerable,
        out_of_scope=not in_scope, injection_detected=injection,
        degraded=degraded,
    )


@app.post("/api/v1/{tenant}/query/stream")
def query_stream(
    tenant: str,
    body: QueryRequest,
    auth: TenantDep,
    request: Request,
):
    """Server-Sent Events streaming query (v9-3). Emits:
      event: sources  data: <json list of retrieved chunks (titles+snippets)>
      event: token    data: <answer token delta>   (repeated)
      event: done     data: <json {tenant_id, out_of_scope, injection_detected, degraded}>
      event: error    data: <json {error}>           (on failure, instead of done)
    A client can subscribe with EventSource (note: EventSource is GET-only; for POST
    payloads use fetch() + ReadableStream on the client side — see widget.js).
    """
    # tenant identity is derived server-side from the Bearer key (auth.tenant_id);
    # the {tenant} path segment is a URL namespace and is not trusted (matches /query).
    _ = tenant

    # P0 FIX: the SSE endpoint does the same expensive retrieval/rerank/generation as
    # /query, so it MUST enforce the same rate limit. Without this an authenticated
    # tenant could hammer /query/stream with zero quota enforcement.
    rate_limit(request, auth.tenant_id)
    # PHASE D (#27): each stream is one billable query (same as /query). Recorded inside _sse
    # after in_scope is known so PHASE E analytics can carry the out_of_scope meta flag.

    def _sse():
        try:
            rewritten, was_rewritten = rewrite_query(auth.tenant_id, body.session_id, body.question)
            if was_rewritten:
                # Surface the clarified query to the client (useful for chat UIs).
                yield f"event: rewritten\ndata: {json.dumps({'query': rewritten})}\n\n"
            injection = bool(body.question) and detect_injection(body.question)
            acl_filter = build_acl_filter(_resolved_acl(body.acl, auth, default_to_public=False))
            hits = []
            degraded = False
            try:
                hits = retrieve(
                    auth.tenant_id, rewritten, top_k=body.top_k,
                    candidate_k=body.candidate_k, rerank=body.rerank, acl_filter=acl_filter,
                )
            except RagError as exc:
                log.warning("query_stream_degraded", extra={"tenant_id": auth.tenant_id, "error_type": type(exc).__name__})
                degraded = True
            hits = [h for h in hits if not detect_injection(h["text"])]
            in_scope, _ = assess_confidence(hits, get_settings().retrieval_confidence_threshold)
            # PHASE D (#27): one billable query. PHASE E: persist out_of_scope in meta for analytics.
            record_usage_bg(auth.tenant_id, "query", meta={"out_of_scope": bool(not in_scope)})
            # PHASE E (#13): out-of-scope (not injection-blocked) query -> knowledge gap.
            if not in_scope and not injection:
                record_knowledge_gap_bg(auth.tenant_id, question=body.question, session_id=body.session_id)

            # Include source_url so the widget can render clickable citations (PHASE C #6).
            # doc_id + chunk_id let the widget deep-link to the hosted viewer when no external
            # URL exists (uploaded/text docs), so every citation is a real, openable link.
            sources = [{"chunk_id": h["chunk_id"], "doc_id": h.get("doc_id"),
                        "title": h.get("title"), "snippet": h["text"][:280],
                        "url": (h.get("metadata") or {}).get("source_url")}
                       for h in hits[:body.top_k]]
            yield f"event: sources\ndata: {json.dumps(sources)}\n\n"

            answer = None
            if body.generate:
                if injection:
                    answer = "I can't follow those instructions. Ask me a question about the documented content and I'll help."
                elif not in_scope:
                    answer = "I don't have information on that in the available documents. Let me connect you with support, or try rephrasing your question."
                else:
                    collected: list[str] = []
                    stream_usage: dict | None = None
                    for tok, tok_usage in stream_answer(rewritten, hits, system_prompt=auth.system_prompt or ""):
                        collected.append(tok)
                        if tok_usage:
                            stream_usage = tok_usage
                        yield f"event: token\ndata: {json.dumps(tok)}\n\n"
                    answer = "".join(collected)
                    # PHASE E (#14): meter tokens (estimated when no LLM provider configured).
                    if stream_usage:
                        record_token_usage_bg(
                            auth.tenant_id, prompt_tokens=stream_usage.get("prompt_tokens", 0),
                            completion_tokens=stream_usage.get("completion_tokens", 0),
                            total_tokens=stream_usage.get("total_tokens", 0),
                            estimated=bool(stream_usage.get("estimated", False)),
                            model=get_settings().llm_model, session_id=body.session_id,
                        )

            if body.session_id:
                store = get_session_store()
                store.append(auth.tenant_id, body.session_id, "user", body.question)
                store.append(auth.tenant_id, body.session_id, "assistant", answer or (hits[0]["text"] if hits else ""))

            done = {"tenant_id": auth.tenant_id, "out_of_scope": (not in_scope),
                    "injection_detected": injection, "degraded": degraded}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
        except Exception as exc:
            err = {"error": getattr(exc, "public_detail", type(exc).__name__)}
            yield f"event: error\ndata: {json.dumps(err)}\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@v1.post("/{tenant}/keys", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def rotate_api_key(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), body: SecretKeyRequest | None = None):
    """Issue a new API key for this tenant. The old key remains valid until revoked.

    v10.8: the new key may be time-boxed via body.expires_at (UTC ISO-8601). Missing body
    mints a non-expiring key (backward compatible).
    """
    rate_limit(request, auth.tenant_id)
    new_key = generate_api_key()
    expires_at = _parse_expiry(body.expires_at if body else None)
    await tenants.add_api_key(auth.tenant_id, new_key, kind="secret", expires_at=expires_at)
    await audit_event(
        "key.rotate", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": new_key[:8], "expires_at": body.expires_at if body else None},
    )
    # return only the new key (shown once) alongside tenant info
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


@v1.post("/{tenant}/keys/secret", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_secret_key(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), body: SecretKeyRequest | None = None):
    """v10.8: mint an additional full-power secret key (rk_*), optionally time-boxed.

    Distinct from POST /{tenant}/keys (rotate) so callers can add non-rotating keys.
    """
    rate_limit(request, auth.tenant_id)
    new_key = generate_api_key()
    expires_at = _parse_expiry(body.expires_at if body else None)
    await tenants.add_api_key(auth.tenant_id, new_key, kind="secret", expires_at=expires_at)
    await audit_event(
        "key.create_secret", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": new_key[:8], "expires_at": body.expires_at if body else None},
    )
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


@v1.get("/{tenant}/keys", response_model=TenantKeysOut)
async def list_keys(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    keys = [KeyInfo(**k) for k in await tenants.list_key_prefixes(auth.tenant_id)]
    return TenantKeysOut(tenant_id=auth.tenant_id, keys=keys)


@v1.delete("/{tenant}/keys/{prefix}", status_code=status.HTTP_200_OK)
async def revoke_key(tenant: str, prefix: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    n = await tenants.revoke_api_key(auth.tenant_id, prefix)
    if n == 0:
        return {"revoked": 0, "note": "no change (last valid key is protected)"}
    await audit_event(
        "key.revoke", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": prefix, "revoked": n},
    )
    return {"revoked": n}


@v1.patch("/{tenant}/keys/{prefix}/expiry", status_code=status.HTTP_200_OK)
async def set_key_expiry_endpoint(tenant: str, prefix: str, body: KeyExpiryRequest, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """v10.8: set or clear a key's expiry (time-boxed keys) by prefix. None = never expire."""
    rate_limit(request, auth.tenant_id)
    expires_at = _parse_expiry(body.expires_at)
    n = await tenants.set_key_expiry(auth.tenant_id, prefix, expires_at)
    if n == 0:
        from fastapi import HTTPException, status as _st
        raise HTTPException(status_code=_st.HTTP_404_NOT_FOUND, detail="key not found or already revoked")
    await audit_event(
        "key.expiry_set", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": prefix, "expires_at": body.expires_at},
    )
    return {"prefix": prefix, "expires_at": body.expires_at, "updated": n}


@v1.post("/{tenant}/keys/publishable", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_publishable_key(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), body: PublishableKeyRequest | None = None):
    """P1 #9: mint a read-only key (pk_*) safe to embed client-side in the widget.

    The publishable key resolves to the SAME tenant (isolation unchanged) but is
    scope-locked to query endpoints by require_secret_key(); it cannot ingest, delete,
    rotate, or revoke. A tenant may hold any number of publishable keys plus secret keys.
    v10.8: the key may be time-boxed via body.expires_at. Missing body = non-expiring.
    """
    rate_limit(request, auth.tenant_id)
    new_key = generate_publishable_key()
    expires_at = _parse_expiry(body.expires_at if body else None)
    await tenants.add_api_key(auth.tenant_id, new_key, kind="publishable", expires_at=expires_at)
    await audit_event(
        "key.create_publishable", actor=auth.tenant_id, target=auth.tenant_id,
        meta={"prefix": new_key[:8], "expires_at": body.expires_at if body else None},
    )
    return TenantOut(
        tenant_id=auth.tenant_id, name=auth.name, api_key=new_key,
        plan=auth.plan, created_at=auth.created_at, chunk_count=auth.chunk_count,
    )


# ---------------- Evaluation (offline RAG quality, no prod traffic) ----------------
@v1.put("/{tenant}/eval/set", status_code=status.HTTP_200_OK)
async def put_eval_set(tenant: str, body: EvalSetIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    rate_limit(request, auth.tenant_id)
    from .eval import save_golden_set
    from .models import GoldenItem

    items = [GoldenItem(**it.model_dump()) for it in body.items]
    n = await save_golden_set(auth.tenant_id, items)
    return {"saved": n}


@v1.post("/{tenant}/eval/run", response_model=EvalReportOut)
async def run_eval(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
             top_k: int = 8, candidate_k: int = 30, rerank: bool = True):
    rate_limit(request, auth.tenant_id)
    from .eval import evaluate, load_golden_set
    from .models import EvalReportOut as _O

    items = await load_golden_set(auth.tenant_id)
    if not items:
        raise HTTPException(status_code=400, detail="no golden set; PUT /eval/set first")
    rep = evaluate(auth.tenant_id, items, top_k=top_k, candidate_k=candidate_k, rerank=rerank)
    await _audit_data_plane("tenant.eval", auth.tenant_id,
                      meta={"kind": "retrieval", "top_k": top_k, "rerank": rerank})
    # PHASE D (#27): per-tenant usage metering.
    await record_usage(auth.tenant_id, "eval_run")
    return _O(**rep.__dict__)


@v1.post("/{tenant}/eval/quality", response_model=dict)
async def run_eval_quality(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
                     top_k: int = 8, candidate_k: int = 30, rerank: bool = True,
                     generate_answer: bool = True, persist: bool = True):
    """v9-4: evaluate answer QUALITY (faithfulness + relevancy) via self-hosted LLM judge,
    plus classic retrieval metrics. Persists a run to the trend history when persist=True."""
    rate_limit(request, auth.tenant_id)
    from .eval import evaluate_quality, load_golden_set, save_eval_run

    items = await load_golden_set(auth.tenant_id)
    if not items:
        raise HTTPException(status_code=400, detail="no golden set; PUT /eval/set first")
    rep = evaluate_quality(auth.tenant_id, items, top_k=top_k, candidate_k=candidate_k,
                           rerank=rerank, generate_answer=generate_answer)
    out = {**rep.__dict__}
    if persist:
        rid = await save_eval_run(auth.tenant_id, rep, run_kind="manual_quality")
        out["run_id"] = rid
    await _audit_data_plane("tenant.eval", auth.tenant_id,
                      meta={"kind": "quality", "top_k": top_k, "rerank": rerank,
                            "generate_answer": generate_answer, "persisted": persist})
    # PHASE D (#27): per-tenant usage metering.
    await record_usage(auth.tenant_id, "eval_run")
    return out


@v1.get("/{tenant}/eval/runs", response_model=list[dict])
async def eval_runs(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), limit: int = 50):
    """v9-4: recent eval run history (trend tracking) for this tenant."""
    rate_limit(request, auth.tenant_id)
    from .eval import load_eval_runs

    return await load_eval_runs(auth.tenant_id, limit=limit)


@app.get("/api/v1/{tenant}/usage", response_model=dict)
async def tenant_usage(tenant: str, auth: TenantDep, request: Request, _: None = Depends(require_secret_key), days: int = 30):
    """PHASE D (#27): per-tenant usage summary the tenant can read with its own secret key.
    Returns counts of queries / docs ingested / chunks ingested / eval runs over `days`."""
    rate_limit(request, auth.tenant_id)
    summary = await get_usage_summary(auth.tenant_id, days=days)
    return summary.to_dict()


@app.post("/api/v1/{tenant}/feedback", response_model=dict, status_code=status.HTTP_200_OK)
async def post_feedback(tenant: str, body: FeedbackIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """PHASE D (#29): capture a thumbs up/down (and optional comment) for an answer.
    `rating` is validated by FeedbackIn ('up'|'down'). The caller supplies the session/question/
    answer context so the signal is analyzable."""
    rate_limit(request, auth.tenant_id)
    fid = await save_feedback(
        auth.tenant_id,
        rating=body.rating,
        session_id=body.session_id,
        message_id=body.message_id,
        comment=body.comment,
        question=body.question,
        answer=body.answer,
    )
    return {"id": fid, "rating": body.rating}


@app.post("/api/v1/{tenant}/handoff", response_model=dict, status_code=status.HTTP_200_OK)
async def post_handoff(tenant: str, body: HandoffIn, auth: TenantDep, request: Request, _: None = Depends(require_secret_key)):
    """PHASE D (#33): capture a human-handoff lead for an out-of-scope / unanswered query.
    Requires a reachable contact (valid email or phone) — a lead with no way to follow up is
    rejected with 422. The caller (widget) supplies the original question + optional name/message."""
    rate_limit(request, auth.tenant_id)
    try:
        lid = await save_lead(
            auth.tenant_id,
            question=body.question,
            name=body.name,
            email=body.email,
            phone=body.phone,
            message=body.message,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"id": lid}


@v1.post("/{tenant}/eval/golden/auto", response_model=dict)
async def auto_golden(tenant: str, body: dict, auth: TenantDep, request: Request, _: None = Depends(require_secret_key),
                      n: int = 3):
    """v9-4: auto-generate golden QA pairs from a provided document via the LLM judge.
    Body: {"title": str, "text": str}. Returns generated EvalItems (and optionally saves)."""
    rate_limit(request, auth.tenant_id)
    from .eval import JudgeLLM, save_golden_set

    title = body.get("title", "")
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    judge = JudgeLLM()
    items = judge.generate_golden(title, text, n=n)
    if body.get("save"):
        await save_golden_set(auth.tenant_id, items)
    return {"generated": [it.__dict__ for it in items], "judge_available": judge.available}


# Mount the versioned API
app.mount("/api/v1", v1)
