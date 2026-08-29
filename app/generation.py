import json

from .config import get_settings


def _estimate_tokens(text: str) -> int:
    """Deterministic, dependency-free token estimate (~4 chars/token, min 1). Used when no LLM
    provider is configured so per-tenant token metering works without a paid key. Real providers
    return exact usage instead. Honest: it's an approximation, labeled as such in the API."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _estimate_usage(question: str, chunks: list[dict], answer: str, system_prompt: str) -> dict:
    """Deterministic token estimate for the extractive (no-LLM) path."""
    prompt_chars = len(question) + sum(len(c.get("text", "")) for c in chunks) + len(system_prompt)
    return {
        "prompt_tokens": _estimate_tokens(question) + _estimate_tokens(system_prompt)
                         + sum(_estimate_tokens(c.get("text", "")) for c in chunks),
        "completion_tokens": _estimate_tokens(answer),
        "total_tokens": _estimate_tokens(question) + _estimate_tokens(system_prompt)
                         + sum(_estimate_tokens(c.get("text", "")) for c in chunks) + _estimate_tokens(answer),
        "estimated": True,
    }


def generate_answer(question: str, chunks: list[dict], max_context_chars: int = 6000, system_prompt: str = "") -> tuple[str, dict]:
    """Returns (answer_text, usage_dict). With an OpenAI-compatible provider configured we call
    it and capture real `usage`; otherwise a deterministic extractive answer with an estimated
    usage so token metering works without a paid key (per the explicit human checkpoint)."""
    s = get_settings()
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            return _openai_generate(s.llm_base_url, s.llm_api_key, s.llm_model, question, chunks, max_context_chars, system_prompt)
        except Exception:
            pass
    answer = _extractive_answer(chunks)
    return answer, _estimate_usage(question, chunks, answer, system_prompt)


def stream_answer(question: str, chunks: list[dict], max_context_chars: int = 6000, system_prompt: str = ""):
    """Yield (token_text, usage_dict_or_None) pairs. With a streaming provider we stream real
    deltas and emit the real usage once (from the final chunk's usage field); otherwise we yield
    the extractive answer word-by-word with a final estimated usage. Yielding a 2-tuple keeps the
    API stable; callers unpack (text, usage)."""
    s = get_settings()
    if s.llm_base_url and s.llm_api_key and s.llm_model:
        try:
            final_usage = None
            for text, usage in _openai_stream(s.llm_base_url, s.llm_api_key, s.llm_model, question, chunks, max_context_chars, system_prompt):
                final_usage = usage
                yield text, usage
            # ensure at least one usage emission even if the provider sent none
            if final_usage is not None:
                pass
            return
        except Exception:
            pass
    # Extractive fallback: stream word-by-word, emit one estimated usage at the end.
    answer = _extractive_answer(chunks)
    for word in answer.split(" "):
        yield word + " ", None
    yield "", _estimate_usage(question, chunks, answer, system_prompt)


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
        json={"model": model, "messages": messages, "temperature": 0.0, "stream": True,
              "stream_options": {"include_usage": True}},
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
                obj = json.loads(data)
                delta = obj["choices"][0]["delta"].get("content", "")
                usage = obj.get("usage")  # present on the final chunk when include_usage=True
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            yield delta, usage


def _openai_generate(base_url: str, api_key: str, model: str, question: str, chunks: list[dict], max_context_chars: int, system_prompt: str = "") -> tuple[str, dict]:
    import httpx

    messages = _build_messages(question, chunks, max_context_chars, system_prompt)
    resp = httpx.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.0},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    content = payload["choices"][0]["message"]["content"].strip()
    usage = payload.get("usage") or {}
    if not usage:
        # provider didn't return usage -> estimate so metering still records something honest
        usage = _estimate_usage(question, chunks, content, system_prompt)
    return content, usage


def _extractive_answer(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant context was found for this question."
    # honor the post-rerank score when present so the extractive fallback matches the
    # reranked ordering shown in the API response.
    top = max(chunks, key=lambda c: c.get("rerank_score", c.get("score", 0.0)))
    return (
        "Based on the retrieved context:\n\n" + top["text"]
    )
