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
from typing import Any, Iterator

try:
    import requests
except ImportError:  # pragma: no cover
    raise ImportError("rag-service SDK requires `requests`. Install with: pip install requests")


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

    # ---------------- eval ----------------
    def put_eval_set(self, tenant: str, name: str, items: list[dict]) -> dict:
        return self._put(f"/{tenant}/eval-sets/{name}", {"items": items})

    def run_eval(self, tenant: str, name: str) -> dict:
        return self._post(f"/{tenant}/eval/{name}", {})

    # ---------------- http plumbing ----------------
    def _headers(self, admin: bool = False) -> dict:
        key = self.admin_key if admin else self.api_key
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return h

    def _post(self, path: str, body: dict, admin: bool = False) -> Any:
        r = requests.post(f"{self.base_url}{path}", headers=self._headers(admin), json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict) -> Any:
        r = requests.put(f"{self.base_url}{path}", headers=self._headers(), json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> Any:
        r = requests.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()
