"""Optional answer generation (pluggable provider).

The query API contract supports `generate=true` to return a generated answer grounded
in the retrieved chunks. Generation is OPTIONAL and pluggable: if an OpenAI-compatible
provider is configured (llm_base_url/llm_api_key/llm_model), we call it; otherwise we
return a deterministic extractive answer (the highest-scoring chunk) so the API shape is
stable without external LLM dependencies. This keeps the service self-hostable.
"""
from __future__ import annotations

import json

from .config import get_settings


def generate_answer(question: str, chunks: list[dict], max_context_chars: int = 6000, system_prompt: str = "") -> str:
    s = get_settings()
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            return _openai_generate(s.llm_base_url, s.llm_api_key, s.llm_model, question, chunks, max_context_chars, system_prompt)
        except Exception:
            pass
    return _extractive_answer(chunks)


def stream_answer(question: str, chunks: list[dict], max_context_chars: int = 6000, system_prompt: str = ""):
    """Yield answer tokens as they are produced (v9-3 SSE streaming).

    Yields strings. If a streaming-capable OpenAI-compatible provider is configured we
    stream its deltas; otherwise we yield the extractive answer word-by-word so the
    client still gets a live typing effect without an external LLM dependency.
    """
    s = get_settings()
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            yield from _openai_stream(s.llm_base_url, s.llm_api_key, s.llm_model, question, chunks, max_context_chars, system_prompt)
            return
        except Exception:
            pass
    # Extractive fallback: stream word-by-word.
    answer = _extractive_answer(chunks)
    for word in answer.split(" "):
        yield word + " "


def _build_messages(question: str, chunks: list[dict], max_context_chars: int, system_prompt: str) -> list[dict]:
    """PHASE D (#35): assemble the chat messages. A non-empty operator-set `system_prompt`
    is prepended as a real `system` message (the persona); the grounding instruction + context
    stays as the `user` turn. System prompt is operator-trusted config, NOT end-user input, so
    it is intentionally NOT subject to the user-input injection filtering owned by the parallel
    P0/P1 security session."""
    context = "\n\n".join(c["text"] for c in chunks)[:max_context_chars]
    user_content = (
        "Answer the question using ONLY the context. If the context does not contain "
        "the answer, say you don't know.\n\nContext:\n" + context +
        "\n\nQuestion: " + question
    )
    if system_prompt:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
    return [{"role": "user", "content": user_content}]


def _openai_stream(base_url: str, api_key: str, model: str, question: str, chunks: list[dict], max_context_chars: int, system_prompt: str = ""):
    import httpx

    messages = _build_messages(question, chunks, max_context_chars, system_prompt)
    with httpx.stream(
        "POST",
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.0, "stream": True},
        timeout=30,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if delta:
                yield delta


def _openai_generate(base_url: str, api_key: str, model: str, question: str, chunks: list[dict], max_context_chars: int, system_prompt: str = "") -> str:
    import httpx

    messages = _build_messages(question, chunks, max_context_chars, system_prompt)
    resp = httpx.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.0},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _extractive_answer(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant context was found for this question."
    # honor the post-rerank score when present so the extractive fallback matches the
    # reranked ordering shown in the API response.
    top = max(chunks, key=lambda c: c.get("rerank_score", c.get("score", 0.0)))
    return (
        "Based on the retrieved context:\n\n" + top["text"]
    )
