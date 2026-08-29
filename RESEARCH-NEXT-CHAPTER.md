# RAG Service — Next-Chapter Research & Capability Plan

**Author:** orchestrator (fresh-session planning pass, 2026-08-30)
**Audience:** agent-1 (dashboard.0) — a solo autonomous agent running long-lived on this VPS
**Constraint:** buildable WITHOUT new paid third-party accounts. Anything needing a paid
external service is explicitly flagged as a "needs operator setup" item, not a blocker.

---

## 1. Competitive landscape (what mature platforms ship)

Grounded in live vendor pages (Vectara, LlamaIndex/LlamaCloud) plus the existing in-repo
`DESIGN-TRADEOFFS.md` / `RESEARCH-2026.md` notes. The takeaway: the *industrial core* of
rag-service (Qdrant hybrid dense+sparse → RRF → cross-encoder rerank, AES-GCM envelope
encryption, per-tenant collection isolation, doc-level RBAC, key tiers, audit log, offline
eval, Prometheus metrics, SSE streaming, sandboxed embeddable widget) is already at
parity with the retrieval/security baseline of Kapa.ai / Vectara / LlamaCloud. The gaps
are in **(a) retrieval-quality depth**, **(b) agentic / multi-hop RAG**, **(c) observability
of retrieval quality & citation faithfulness**, **(d) enterprise onboarding (SSO, audit
export, SOC2-style controls)**, and **(e) monetization plumbing**.

### Vectara (enterprise agent platform)
- Retrieval: neural (dense) + lexical (sparse) search, knowledge re-ranking, agentic
  extraction from documents.
- Governance/observability: "Guardian Agents" with **real-time RAG grounding**,
  **hallucination scoring (HHEM)** and **hallucination correction (VHC)**, full
  observability into agentic execution.
- Compliance: SOC 2 + HIPAA.
- Packaging: SaaS / VPC / on-prem; centralized multi-agent platform.

### LlamaIndex / LlamaCloud
- Parsing: LlamaParse — layout/tables/handwriting/image parsing; LlamaExtract with
  **confidence scores + citations**; LlamaCloud Index with intelligent chunking/embedding.
- Developer surface: Python + TS SDKs, Workflows (multi-step agent orchestration),
  huge OSS ecosystem.

### Common patterns across Kapa.ai / Mendable / Pinecone Assist / Glean (from prior research)
- **Query rewriting / sub-question decomposition** before retrieval (recreases recall).
- **Citation & faithfulness tracking** — answers are grounded with clickable citations and
  a faithfulness/no-answer rate metric.
- **Usage-based billing / tiered plans** — per-query or per-seat metering, plan-gated
  feature ceilings (this repo already has `plan` + chunk quota + token usage metering —
  close to this).
- **SSO / SCIM / audit export** — enterprise buyers require SAML/OIDC login and exportable
  audit logs; Glean is per-seat enterprise search with deep app connectors.

### What this means for rag-service
The codebase is a credible "mini-Vectara". The realistic next chapters that a solo agent
can actually build (no new paid accounts) are the retrieval-quality and observability
layers — query rewriting, multi-hop/agentic retrieval, citation-faithfulness scoring,
retrieval-quality dashboards, and self-serve tenant onboarding. SSO/SOC2 and external
monetization need operator action and are flagged separately.

---

## 2. Prioritized capability areas (buildable solo, no paid accounts)

Ordered by impact-per-effort for a single autonomous agent. Each maps to backlog phases in
`BACKLOG.md`.

1. **Safe Markdown rendering in the embeddable widget (#22).** Render retrieved answer text
   + source snippets as formatted Markdown inside the sandboxed widget WITHOUT XSS. This is
   the immediate next item (decision: agent-1 takes it).
2. **Query rewriting & sub-question decomposition.** Add a pre-retrieval step (LLM- or
   rule-based) that rewrites ambiguous/short queries and decomposes multi-part questions,
   improving recall@10 on the existing hybrid pipeline. Gated behind `LLM_BASE_URL`; falls
   back to passthrough when no LLM configured (matches the existing deterministic-default
   pattern).
3. **Agentic / multi-hop retrieval.** Iterative retrieve→read→reformulate loop (max N hops)
   for questions needing synthesis across chunks. Reuses `retrieve()` + `generation()`.
4. **Citation faithfulness & no-answer detection.** Score whether the generated answer is
   grounded in retrieved chunks (token-overlap / NLI-lite / self-check prompt); emit a
   `faithfulness` score + `answerable` flag in the query response and log it.
5. **Retrieval-quality observability.** Persist per-query eval signals (latency, hit_count,
   faithfulness, rerank delta, rewrite used) to a metrics store + a Grafana panel; trend
   hallucination/citation rate over time.
6. **Document parsing depth (LlamaParse-class).** Improve `app/ingestion/` to better handle
   tables/headings/metadata from HTML/PDF-ish input; add confidence + citation spans on
   extracted chunks. No external API — use local heuristics + the existing chunker.
7. **Self-serve tenant onboarding + plan-gated feature ceilings.** Turn `plan` into real
   feature gates (e.g. max top_k, multi-hop on/off, rewrite on/off, retention window) and a
   small admin SPA over the existing admin API.
8. **Per-tenant rate limiting / quotas (already flagged in BACKLOG).** Make `rate_limit()`
   tenant-aware (req/min, chunks ingested, collection size) to stop noisy-neighbor starvation.
9. **Ingestion webhooks / SSE job progress (already flagged).** Replace client polling with
   SSRF-guarded tenant callback URLs or SSE progress events.
10. **Horizontal scaling of ingestion workers (already flagged).** Add a `JOB_QUEUE` backend
    setting (RQ/Celery/Cloud Tasks) so the job runner is multi-replica safe.

---

## 3. Explicitly OUT OF SCOPE for the solo agent (needs operator setup)

These are real but require human-owned credentials/decisions. Flagged, not blocked-on:

- **SSO / SAML / OIDC login + SCIM** for the admin console. Needs an IdP account/decision.
- **SOC 2 / HIPAA formal controls & audit export packaging.** Needs operator/legal ownership.
- **External monetization (Stripe billing, public SaaS pricing page).** Needs Stripe account
  + business decision; the underlying metering (token usage, chunk quota, `plan`) already
  exists and can be exposed read-only.
- **npm publish of `@hermes-rag/sdk`** (sdk-js skeleton already committed). Needs npm token.
- **Public DNS + TLS for the hosted widget/API.** Needs domain + cert (infra decision).

---

## 4. Recommended execution principle

Work strictly via the existing branch → PR → self-merge (squash) → tag → release workflow,
exactly as the prior phases did. After each phase, self-select the next phase from the
backlog rather than stopping. The expanded `BACKLOG.md` is sequenced so there is always a
concrete next item — short one-line goals are what previously caused idle drift.
