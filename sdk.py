"""Official Python SDK for the multi-tenant RAG service (v9-3).

Minimal, dependency-light client (only `requests`). Mirrors the REST contract:

    from sdk import RagClient
    c = RagClient(base_url="https://rag.example.com/api/v1", api_key="rk_...")
    c.ingest_text(tenant="acme", title="FAQ", content="...")
    resp = c.query(tenant="acme", question="What is the refund policy?")
    for event in c.query_stream(tenant="acme", question="...", generate=True):
        print(event)  # {"event": "sources"|"token"|"done"|"error", "data": ...}

Multi-tenant: every call takes `tenant` (the server resolves tenant identity from the
Bearer api_key; `tenant` is only used to build the URL path and is validated server-side).
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterator

try:
    import requests
except ImportError:  # pragma: no cover
    raise ImportError("rag-service SDK requires `requests`. Install with: pip install requests")

try:
    import httpx
except ImportError:  # pragma: no cover
    raise ImportError("rag-service async SDK requires `httpx`. Install with: pip install httpx")


class RagClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, admin_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_key = admin_key
        self.timeout = timeout

    # ---------------- tenant admin ----------------
    def create_tenant(self, name: str, plan: str = "standard", allowed_groups: list[str] | None = None) -> dict:
        if not self.admin_key:
            raise ValueError("admin_key required for tenant management")
        body = {"name": name, "plan": plan}
        if allowed_groups is not None:
            body["allowed_groups"] = allowed_groups
        return self._post("/tenants", body, admin=True)

    # ---------------- ingestion ----------------
    def ingest_text(self, tenant: str, title: str, content: str, content_type: str = "text", acl: list[str] | None = None) -> dict:
        body = {"title": title, "content": content, "content_type": content_type}
        if acl is not None:
            body["acl"] = acl
        return self._post(f"/{tenant}/documents", body)

    def ingest_url(self, tenant: str, url: str, title: str | None = None, acl: list[str] | None = None) -> dict:
        body = {"url": url}
        if title is not None:
            body["title"] = title
        if acl is not None:
            body["acl"] = acl
        return self._post(f"/{tenant}/ingest/url", body)

    def delete_document(self, tenant: str, doc_id: str) -> dict:
        return self._delete(f"/{tenant}/documents/{doc_id}")

    # ---------------- file + sitemap onboarding (PHASE B/C) ----------------
    def upload_file(self, tenant: str, filename: str, content: bytes, *, title: str | None = None,
                    content_type: str | None = None, acl: list[str] | None = None,
                    metadata: dict | None = None) -> dict:
        """Upload a file (PDF / Markdown / HTML / code / text). The server extracts text.

        Uses multipart/form-data. `content` is raw bytes. Returns the UploadOut payload.
        """
        import io

        files = {"file": (filename, io.BytesIO(content), content_type or "application/octet-stream")}
        data: dict = {}
        if title is not None:
            data["title"] = title
        if acl is not None:
            data["acl"] = ",".join(acl)
        if metadata is not None:
            data["metadata"] = json.dumps(metadata)
        return self._post_multipart(f"/{tenant}/documents/upload", data=data, files=files)

    def ingest_sitemap(self, tenant: str, sitemap_url: str, *, max_urls: int = 100,
                       concurrency: int = 4, metadata: dict | None = None,
                       acl: list[str] | None = None) -> dict:
        body = {"url": sitemap_url, "max_urls": max_urls, "concurrency": concurrency}
        if metadata is not None:
            body["metadata"] = metadata
        if acl is not None:
            body["acl"] = acl
        return self._post(f"/{tenant}/ingest/sitemap", body)

    def list_documents(self, tenant: str, *, limit: int = 200, offset: int = 0) -> dict:
        """Return the paginated document catalog: {items, total, limit, offset}."""
        return self._get(f"/{tenant}/documents", params={"limit": limit, "offset": offset})

    # ---------------- retrieval ----------------
    def query(self, tenant: str, question: str, *, top_k: int = 5, generate: bool = False,
              rerank: bool = True, session_id: str | None = None, acl: list[str] | None = None) -> dict:
        body = {"question": question, "top_k": top_k, "generate": generate, "rerank": rerank}
        if session_id:
            body["session_id"] = session_id
        if acl is not None:
            body["acl"] = acl
        return self._post(f"/{tenant}/query", body)

    def query_stream(self, tenant: str, question: str, *, top_k: int = 5, generate: bool = False,
                     rerank: bool = True, session_id: str | None = None) -> Iterator[dict]:
        """Yield parsed SSE events: {"event": str, "data": any}."""
        body = {"question": question, "top_k": top_k, "generate": generate, "rerank": rerank}
        if session_id:
            body["session_id"] = session_id
        resp = requests.post(
            f"{self.base_url}/{tenant}/query/stream",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body, timeout=self.timeout, stream=True,
        )
        resp.raise_for_status()
        event: str | None = None
        for raw in resp.iter_lines(decode_unicode=True):
            line = raw if isinstance(raw, str) else raw.decode("utf-8")
            if not line:
                # blank line terminates an event block
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                data_raw = line[len("data:"):].strip()
                try:
                    parsed = json.loads(data_raw)
                except json.JSONDecodeError:
                    parsed = data_raw
                yield {"event": event or "message", "data": parsed}
                event = None

    # ---------------- eval (retrieval + answer-quality) ----------------
    # Server contract (verified): PUT /{tenant}/eval/set, POST /{tenant}/eval/run,
    # POST /{tenant}/eval/quality, GET /{tenant}/eval/runs. Note the routes take NO `name`
    # segment — the golden set is keyed by tenant.
    def put_eval_set(self, tenant: str, items: list[dict], *, name: str | None = None) -> dict:
        """Upload a golden set for `tenant`. `name` is accepted for backward-compat but unused
        (the server keys the set by tenant). `items` is a list of
        {question, relevant_doc_ids?, relevant_texts?, expected_answer?}."""
        body: dict = {"items": items}
        if name is not None:
            body["name"] = name
        return self._put(f"/{tenant}/eval/set", body)

    def run_eval(self, tenant: str, *, top_k: int = 8, candidate_k: int = 30,
                 rerank: bool = True) -> dict:
        """Run retrieval eval on the tenant's golden set. Returns EvalReportOut."""
        return self._post(f"/{tenant}/eval/run", {}, params={
            "top_k": top_k, "candidate_k": candidate_k, "rerank": rerank})

    def run_eval_quality(self, tenant: str, *, top_k: int = 8, candidate_k: int = 30,
                         rerank: bool = True, generate_answer: bool = True,
                         persist: bool = True) -> dict:
        """Run answer-quality eval (faithfulness + relevancy) on the golden set."""
        return self._post(f"/{tenant}/eval/quality", {}, params={
            "top_k": top_k, "candidate_k": candidate_k, "rerank": rerank,
            "generate_answer": generate_answer, "persist": persist})

    def eval_runs(self, tenant: str, *, limit: int = 50) -> list[dict]:
        """Return recent eval run history for the tenant."""
        return self._get(f"/{tenant}/eval/runs", params={"limit": limit})

    # ---------------- http plumbing ----------------
    def _headers(self, admin: bool = False) -> dict:
        key = self.admin_key if admin else self.api_key
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return h

    def _get(self, path: str, *, params: dict | None = None, admin: bool = False) -> Any:
        r = requests.get(
            f"{self.base_url}{path}",
            headers={k: v for k, v in self._headers(admin).items() if k != "Content-Type"},
            params=params, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def _post_multipart(self, path: str, *, data: dict, files: dict, admin: bool = False) -> Any:
        # multipart/form-data: don't set Content-Type (requests sets the boundary).
        headers = {"Authorization": self._headers(admin)["Authorization"]}
        r = requests.post(
            f"{self.base_url}{path}", headers=headers, data=data, files=files, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, *, params: dict | None = None, admin: bool = False) -> Any:
        r = requests.post(f"{self.base_url}{path}", headers=self._headers(admin), json=body,
                          params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict, *, params: dict | None = None) -> Any:
        r = requests.put(f"{self.base_url}{path}", headers=self._headers(), json=body,
                         params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> Any:
        r = requests.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class AsyncRagClient:
    """Async counterpart of :class:`RagClient`, built on ``httpx.AsyncClient`` (httpx 0.28+).

    Exposes the same surface as the sync client (ingest/query/query_stream/delete/upload/
    sitemap/list_documents/eval/*) but as coroutines, plus async SSE streaming. Use inside
    ``async with AsyncRagClient(...) as c:`` so the connection pool is closed.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, admin_key: str | None = None,
                 transport: Any = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_key = admin_key
        self.timeout = timeout
        # `transport` lets tests route through an in-process ASGI app (httpx.ASGITransport)
        # instead of the network; when set, base_url is used only for path building.
        client_kwargs: dict = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        else:
            client_kwargs["base_url"] = self.base_url
        self._client = httpx.AsyncClient(**client_kwargs)

    async def __aenter__(self) -> "AsyncRagClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    # ---------------- tenant admin ----------------
    async def create_tenant(self, name: str, plan: str = "standard",
                            allowed_groups: list[str] | None = None) -> dict:
        if not self.admin_key:
            raise ValueError("admin_key required for tenant management")
        body = {"name": name, "plan": plan}
        if allowed_groups is not None:
            body["allowed_groups"] = allowed_groups
        return await self._post("/tenants", body, admin=True)

    # ---------------- ingestion ----------------
    async def ingest_text(self, tenant: str, title: str, content: str, content_type: str = "text",
                          acl: list[str] | None = None) -> dict:
        body = {"title": title, "content": content, "content_type": content_type}
        if acl is not None:
            body["acl"] = acl
        return await self._post(f"/{tenant}/documents", body)

    async def ingest_url(self, tenant: str, url: str, title: str | None = None,
                         acl: list[str] | None = None) -> dict:
        body = {"url": url}
        if title is not None:
            body["title"] = title
        if acl is not None:
            body["acl"] = acl
        return await self._post(f"/{tenant}/ingest/url", body)

    async def delete_document(self, tenant: str, doc_id: str) -> dict:
        return await self._delete(f"/{tenant}/documents/{doc_id}")

    async def upload_file(self, tenant: str, filename: str, content: bytes, *,
                          title: str | None = None, content_type: str | None = None,
                          acl: list[str] | None = None, metadata: dict | None = None) -> dict:
        data: dict = {}
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        if title is not None:
            data["title"] = title
        if acl is not None:
            data["acl"] = ",".join(acl)
        if metadata is not None:
            data["metadata"] = json.dumps(metadata)
        return await self._post_multipart(f"/{tenant}/documents/upload", data=data, files=files)

    async def ingest_sitemap(self, tenant: str, sitemap_url: str, *, max_urls: int = 100,
                             concurrency: int = 4, metadata: dict | None = None,
                             acl: list[str] | None = None) -> dict:
        body = {"url": sitemap_url, "max_urls": max_urls, "concurrency": concurrency}
        if metadata is not None:
            body["metadata"] = metadata
        if acl is not None:
            body["acl"] = acl
        return await self._post(f"/{tenant}/ingest/sitemap", body)

    async def list_documents(self, tenant: str, *, limit: int = 200, offset: int = 0) -> dict:
        return await self._get(f"/{tenant}/documents", params={"limit": limit, "offset": offset})

    # ---------------- retrieval ----------------
    async def query(self, tenant: str, question: str, *, top_k: int = 5, generate: bool = False,
                   rerank: bool = True, session_id: str | None = None,
                   acl: list[str] | None = None) -> dict:
        body = {"question": question, "top_k": top_k, "generate": generate, "rerank": rerank}
        if session_id:
            body["session_id"] = session_id
        if acl is not None:
            body["acl"] = acl
        return await self._post(f"/{tenant}/query", body)

    async def query_stream(self, tenant: str, question: str, *, top_k: int = 5, generate: bool = False,
                           rerank: bool = True, session_id: str | None = None) -> "AsyncIterator[dict]":
        """Async SSE streaming. Yields parsed events: {event, data}."""
        body = {"question": question, "top_k": top_k, "generate": generate, "rerank": rerank}
        if session_id:
            body["session_id"] = session_id
        async with self._client.stream(
            "POST", f"{self.base_url}/{tenant}/query/stream",
            headers=self._auth_headers(), json=body,
        ) as resp:
            resp.raise_for_status()
            event: str | None = None
            async for raw in resp.aiter_lines():
                if not raw:
                    continue
                if raw.startswith("event:"):
                    event = raw[len("event:"):].strip()
                    continue
                if raw.startswith("data:"):
                    data_raw = raw[len("data:"):].strip()
                    try:
                        parsed = json.loads(data_raw)
                    except json.JSONDecodeError:
                        parsed = data_raw
                    yield {"event": event or "message", "data": parsed}
                    event = None

    # ---------------- eval ----------------
    async def put_eval_set(self, tenant: str, items: list[dict], *,
                           name: str | None = None) -> dict:
        body: dict = {"items": items}
        if name is not None:
            body["name"] = name
        return await self._put(f"/{tenant}/eval/set", body)

    async def run_eval(self, tenant: str, *, top_k: int = 8, candidate_k: int = 30,
                       rerank: bool = True) -> dict:
        return await self._post(f"/{tenant}/eval/run", {}, params={
            "top_k": top_k, "candidate_k": candidate_k, "rerank": rerank})

    async def run_eval_quality(self, tenant: str, *, top_k: int = 8, candidate_k: int = 30,
                               rerank: bool = True, generate_answer: bool = True,
                               persist: bool = True) -> dict:
        return await self._post(f"/{tenant}/eval/quality", {}, params={
            "top_k": top_k, "candidate_k": candidate_k, "rerank": rerank,
            "generate_answer": generate_answer, "persist": persist})

    async def eval_runs(self, tenant: str, *, limit: int = 50) -> list[dict]:
        return await self._get(f"/{tenant}/eval/runs", params={"limit": limit})

    # ---------------- http plumbing ----------------
    def _auth_headers(self, admin: bool = False) -> dict:
        key = self.admin_key if admin else self.api_key
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _get(self, path: str, *, params: dict | None = None, admin: bool = False) -> Any:
        r = await self._client.get(f"{self.base_url}{path}", headers=self._auth_headers(admin),
                                   params=params)
        r.raise_for_status()
        return r.json()

    async def _post_multipart(self, path: str, *, data: dict, files: dict,
                              admin: bool = False) -> Any:
        headers = {"Authorization": self._auth_headers(admin)["Authorization"]}
        r = await self._client.post(f"{self.base_url}{path}", headers=headers, data=data, files=files)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict, *, params: dict | None = None,
                   admin: bool = False) -> Any:
        r = await self._client.post(f"{self.base_url}{path}", headers=self._auth_headers(admin),
                                    json=body, params=params)
        r.raise_for_status()
        return r.json()

    async def _put(self, path: str, body: dict, *, params: dict | None = None) -> Any:
        r = await self._client.put(f"{self.base_url}{path}", headers=self._auth_headers(),
                                   json=body, params=params)
        r.raise_for_status()
        return r.json()

    async def _delete(self, path: str) -> Any:
        r = await self._client.delete(f"{self.base_url}{path}", headers=self._auth_headers())
        r.raise_for_status()
        return r.json()
