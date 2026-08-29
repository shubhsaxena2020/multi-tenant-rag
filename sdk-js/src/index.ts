/**
 * Hermes RAG Service — JavaScript/TypeScript SDK (v1.0.0).
 *
 * Thin, dependency-free wrapper around the REST + SSE contract. Mirrors the Python
 * `sdk.py`. Every method takes `tenant` (used only to build the URL; the server resolves
 * tenant identity from the Bearer api_key).
 *
 * Browser usage: pass a PUBLISHABLE key (`pk_*`) — it is read-only (query-only) and safe to
 * embed. Never ship a secret key (`rk_*`) to the client.
 */

export interface RagClientOptions {
  baseUrl: string;            // e.g. "https://rag.example.com/api/v1"
  apiKey: string;             // Bearer token (pk_* for browser, rk_* for server)
  timeoutMs?: number;         // default 30_000
}

export interface QueryOptions {
  topK?: number;
  generate?: boolean;
  rerank?: boolean;
  sessionId?: string;
  acl?: string[];
}

export interface RetrievedSource {
  chunk_id?: string;
  title?: string | null;
  snippet?: string;
  url?: string | null;
}

export type SseEvent =
  | { event: "rewritten"; data: { query: string } }
  | { event: "sources"; data: RetrievedSource[] }
  | { event: "token"; data: string }
  | { event: "done"; data: Record<string, unknown> }
  | { event: "error"; data: { error: string } };

export class RagClient {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly timeoutMs: number;

  constructor(opts: RagClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, "");
    this.apiKey = opts.apiKey;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  /** Non-streaming query. */
  async query(tenant: string, question: string, opts: QueryOptions = {}): Promise<unknown> {
    return this.post(`/${tenant}/query`, {
      question,
      top_k: opts.topK ?? 5,
      generate: opts.generate ?? false,
      rerank: opts.rerank ?? true,
      ...(opts.sessionId ? { session_id: opts.sessionId } : {}),
      ...(opts.acl ? { acl: opts.acl } : {}),
    });
  }

  /** Streaming query → async iterator of parsed SSE events. */
  async *queryStream(
    tenant: string,
    question: string,
    opts: QueryOptions = {},
  ): AsyncGenerator<SseEvent> {
    const body = JSON.stringify({
      question,
      top_k: opts.topK ?? 5,
      generate: opts.generate ?? true,
      rerank: opts.rerank ?? true,
      ...(opts.sessionId ? { session_id: opts.sessionId } : {}),
    });
    const resp = await fetch(`${this.baseUrl}/${tenant}/query/stream`, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`query/stream failed: ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let event: string | null = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const blocks = buf.split("\n\n");
      buf = blocks.pop() ?? "";
      for (const block of blocks) {
        const m = block.match(/^event: (\w+)\ndata: ([\s\S]*)$/);
        if (!m) continue;
        event = m[1];
        let data: unknown;
        try {
          data = JSON.parse(m[2]);
        } catch {
          data = m[2];
        }
        yield { event: event as SseEvent["event"], data } as SseEvent;
      }
    }
  }

  /** Upload a file (PDF / Markdown / HTML / code / text); server extracts text. */
  async uploadFile(
    tenant: string,
    filename: string,
    content: Blob | ArrayBuffer | Uint8Array,
    opts: { title?: string; contentType?: string; acl?: string[]; metadata?: Record<string, unknown> } = {},
  ): Promise<unknown> {
    const form = new FormData();
    const blob = content instanceof Blob ? content : new Blob([content]);
    form.append("file", blob, filename);
    if (opts.title) form.append("title", opts.title);
    if (opts.acl) form.append("acl", opts.acl.join(","));
    if (opts.metadata) form.append("metadata", JSON.stringify(opts.metadata));
    return this.postMultipart(`/${tenant}/documents/upload`, form);
  }

  /** Start a sitemap crawl for onboarding. */
  async ingestSitemap(
    tenant: string,
    sitemapUrl: string,
    opts: { maxUrls?: number; concurrency?: number; acl?: string[]; metadata?: Record<string, unknown> } = {},
  ): Promise<unknown> {
    return this.post(`/${tenant}/ingest/sitemap`, {
      url: sitemapUrl,
      max_urls: opts.maxUrls ?? 100,
      concurrency: opts.concurrency ?? 4,
      ...(opts.acl ? { acl: opts.acl } : {}),
      ...(opts.metadata ? { metadata: opts.metadata } : {}),
    });
  }

  /** Paginated document catalog: { items, total, limit, offset }. */
  async listDocuments(tenant: string, opts: { limit?: number; offset?: number } = {}): Promise<unknown> {
    const params = new URLSearchParams();
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    if (opts.offset !== undefined) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return this.get(`/${tenant}/documents${qs ? `?${qs}` : ""}`);
  }

  private async post(path: string, body: unknown): Promise<unknown> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`${path} failed: ${resp.status}`);
    return resp.json();
  }

  private async postMultipart(path: string, form: FormData): Promise<unknown> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}` },
      body: form,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`${path} failed: ${resp.status}`);
    return resp.json();
  }

  private async get(path: string): Promise<unknown> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new Error(`${path} failed: ${resp.status}`);
    return resp.json();
  }
}

export default RagClient;
