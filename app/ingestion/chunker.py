"""Token-aware recursive chunker.

Strategy (Firecrawl chunking 2026): recursive split respecting structural
separators (paragraph > line > sentence > word), targeting ~512 tokens with ~64-token
overlap. We split by tokens (not characters) because embeddings are token-based.
`tiktoken` provides the tokenizer; falls back to whitespace words if unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass

_SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclass
class Chunk:
    text: str
    index: int


def _len_tokens(text: str, enc) -> int:
    if enc is None:
        return max(1, len(text.split()))
    return len(enc.encode(text))


def _split_once(text: str, sep: str) -> list[str]:
    if sep == " ":
        return text.split(" ")
    return text.split(sep)


def recursive_split(text: str, enc, chunk_tokens: int, overlap_tokens: int) -> list[Chunk]:
    if _len_tokens(text, enc) <= chunk_tokens:
        return [Chunk(text=text, index=0)] if text.strip() else []

    # find pieces at the current separator granularity
    pieces = _split_once(text, _SEPARATORS[0])
    # build chunks by greedily concatenating pieces up to chunk_tokens
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + (_SEPARATORS[0] if current else "") + piece
        if _len_tokens(candidate, enc) > chunk_tokens and current:
            chunks.append(current)
            # start overlap: keep tail of previous chunk
            tail_tokens = _tail_tokens(current, enc, overlap_tokens)
            current = tail_tokens + (_SEPARATORS[0] if tail_tokens else "") + piece
        else:
            current = candidate
    if current.strip():
        chunks.append(current)

    # if a single piece is still too big, go one separator level deeper
    if len(chunks) == 1 and _len_tokens(chunks[0], enc) > chunk_tokens:
        return _split_deeper(chunks[0], 1, enc, chunk_tokens, overlap_tokens)

    return [Chunk(text=c, index=i) for i, c in enumerate(chunks) if c.strip()]


def _split_deeper(text: str, level: int, enc, chunk_tokens: int, overlap_tokens: int) -> list[Chunk]:
    sep = _SEPARATORS[min(level, len(_SEPARATORS) - 1)]
    pieces = _split_once(text, sep)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + (sep if current else "") + piece
        if _len_tokens(candidate, enc) > chunk_tokens and current:
            chunks.append(current)
            tail = _tail_tokens(current, enc, overlap_tokens)
            current = tail + (sep if tail else "") + piece
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    if len(chunks) == 1 and _len_tokens(chunks[0], enc) > chunk_tokens and level < len(_SEPARATORS) - 1:
        return _split_deeper(chunks[0], level + 1, enc, chunk_tokens, overlap_tokens)
    return [Chunk(text=c, index=i) for i, c in enumerate(chunks) if c.strip()]


def _tail_tokens(text: str, enc, overlap_tokens: int) -> str:
    toks = enc.encode(text) if enc else text.split()
    tail = toks[-overlap_tokens:] if overlap_tokens else []
    if enc:
        try:
            return enc.decode(tail)
        except Exception:  # noqa: BLE001 - partial multibyte token; drop it
            return ""
    return " ".join(tail)


def chunk_text(text: str, chunk_tokens: int = 512, overlap_tokens: int = 64, enc=None) -> list[Chunk]:
    """Chunk `text` into ~chunk_tokens token pieces with overlap_tokens overlap."""
    return recursive_split(text.strip(), enc, chunk_tokens, overlap_tokens)
