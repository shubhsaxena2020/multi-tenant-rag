# @hermes-rag/sdk

Official JavaScript / TypeScript SDK for the **Hermes multi-tenant RAG service**.

- Dependency-free, works in Node 18+ and modern browsers.
- Covers query (sync + SSE streaming), file upload, sitemap ingestion, and the document catalog.
- Browser embedding: pass a **publishable** key (`pk_*`) — read-only, query-only, safe to ship.
  Never put a **secret** key (`rk_*`) in client-side code.

## Install (publish step pending — see note)

> **Publish status:** the npm package skeleton is committed and builds locally, but the actual
> `npm publish` is **deferred** — it requires an npm registry token / publish access, which is an
> external credential not available in this environment (tracked in NEEDS_HUMAN.md). Until then,
> consume it directly:

```bash
# From the repo root
cd sdk-js
npm install        # installs typescript (dev) for build
npm run build      # tsc -> dist/
```

Then import the built output, or copy `src/index.ts` into your project.

## Usage

```ts
import { RagClient } from "@hermes-rag/sdk";

// Browser (publishable key) — safe to embed:
const client = new RagClient({
  baseUrl: "https://rag.example.com/api/v1",
  apiKey: "pk_xxx", // pk_* only, never rk_*
});

// Stream an answer with clickable citations surfaced via the `sources` event.
for await (const ev of client.queryStream("acme", "What is the refund policy?", { generate: true })) {
  if (ev.event === "sources") console.log("sources:", ev.data);
  else if (ev.event === "token") process.stdout.write(ev.data);
  else if (ev.event === "done") console.log("\n[done]", ev.data);
}

// File upload (server extracts PDF/Markdown/HTML/text):
await client.uploadFile("acme", "guide.pdf", pdfBytes, { title: "Guide" });

// Sitemap onboarding:
await client.ingestSitemap("acme", "https://example.com/sitemap.xml");

// Catalog:
const page = await client.listDocuments("acme", { limit: 50, offset: 0 });
console.log(page.total, page.items);
```

## Python SDK

The Python SDK lives at the repo root as `sdk.py` (dependency-light: only `requests`).

```python
from sdk import RagClient
c = RagClient(base_url="https://rag.example.com/api/v1", api_key="rk_...")
c.ingest_text(tenant="acme", title="FAQ", content="...")
c.query_stream(tenant="acme", question="Refund policy?")
```
