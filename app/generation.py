"""Optional answer generation (pluggable provider).

The query API contract supports `generate=true` to return a generated answer grounded
in the retrieved chunks. Generation is OPTIONAL and pluggable: if an OpenAI-compatible
provider is configured (llm_base_url/llm_api_key/llm_model), we call it; otherwise we
return a deterministic extractive answer (the highest-scoring chunk) so the API shape is
stable without external LLM dependencies. This keeps the service self-hostable.
"""
from __future__ import annotations

from .config import get_settings


def generate_answer(question: str, chunks: list[dict], max_context_chars: int = 6000) -> str:
    s = get_settings()
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            return _openai_generate(s.llm_base_url, s.llm_api_key, s.llm_model, question, chunks, max_context_chars)
        except Exception:  # noqa: BLE001,S110 - silent fallback to extractive answer on any provider error
            pass
    return _extractive_answer(chunks)


def _openai_generate(base_url: str, api_key: str, model: str, question: str, chunks: list[dict], max_context_chars: int) -> str:
    import httpx

    context = "\n\n".join(c["text"] for c in chunks)[:max_context_chars]
    prompt = (
        "Answer the question using ONLY the context. If the context does not contain "
        "the answer, say you don't know.\n\nContext:\n" + context +
        "\n\nQuestion: " + question
    )
    resp = httpx.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.0},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _extractive_answer(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant context was found for this question."
    top = max(chunks, key=lambda c: c.get("score", 0.0))
    return (
        "Based on the retrieved context:\n\n" + top["text"]
    )
