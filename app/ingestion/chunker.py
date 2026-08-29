"""Token-aware recursive chunker with structural metadata (PHASE F, #30-#33).

Strategy (Firecrawl chunking 2026): recursive split respecting structural separators
(paragraph > line > sentence > word), targeting ~512 tokens with ~64-token overlap. We split by
tokens (not characters) because embeddings are token-based. `tiktoken` provides the tokenizer;
falls back to whitespace words if unavailable.

PHASE F enhancements:
- `Chunk` now carries `metadata` (heading hierarchy, table/code flags) and `char_span`
  (start/end character offsets in the ORIGINAL source) so citations can point back to the exact
  span.
- `chunk_structured()` preserves heading hierarchy (current `#`/`##` context), marks table blocks
  (`is_table`, `table_rows`), and keeps fenced code blocks whole (never split mid-token). Plain
  text falls back to the recursive splitter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclass
class Chunk:
    text: str
    index: int
    metadata: dict = field(default_factory=dict)
    # (start_offset, end_offset) character span within the original source document.
    char_span: tuple[int, int] | None = None


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
        except Exception:
            return ""
    return " ".join(tail)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return bool(s) and "|" in s and not s.startswith("```")


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and "|" in s and set(s) <= set("-|: ")


def chunk_structured(
    text: str, content_type: str = "text", chunk_tokens: int = 512,
    overlap_tokens: int = 64, enc=None,
) -> list[Chunk]:
    """Chunk while preserving structure (headings, tables, code fences) + char_span citations.

    - Headings (`#`/`##`/...) set the inherited `heading` metadata for subsequent chunks.
    - Consecutive `|`-delimited rows (with a `---` separator) become a single `is_table` block
      with `table_rows` = number of data rows.
    - Fenced code blocks (```...```) are kept whole (never split mid-token).
    - Every chunk gets `char_span` = (start, end) offsets in the original `text`.
    Plain `text`/unknown types fall back to the recursive splitter with a global char_span.
    """
    if content_type not in ("markdown", "code", "html"):
        out = recursive_split(text.strip(), enc, chunk_tokens, overlap_tokens)
        off = 0
        for c in out:
            start = text.find(c.text, off)
            if start < 0:
                start = off
            c.char_span = (start, start + len(c.text))
            off = start + len(c.text)
        for i, c in enumerate(out):
            c.index = i
        return out

    lines = text.split("\n")
    chunks: list[Chunk] = []
    cur_heading: str | None = None
    cur_heading_raw: str | None = None
    cur_heading_line = 0
    buf: list[str] = []
    buf_line0 = 0

    def flush() -> None:
        if not buf:
            return
        joined = "\n".join(buf)
        # Reconstruct the block as the EXACT source slice from the heading line through the end of
        # the buffered lines. This keeps char_span faithful (md[start:end] == block) and preserves
        # the heading keywords for retrieval without injecting synthetic separators.
        if cur_heading_raw:
            start_off = _line_offset(lines, cur_heading_line)
            buf_off = _line_offset(lines, buf_line0)
            end_off = buf_off + len(joined)
            block = text[start_off:end_off]
        else:
            start_off = _line_offset(lines, buf_line0)
            end_off = start_off + len(joined)
            block = joined
        meta: dict[str, object] = {"heading": cur_heading}
        kind, rows = _block_kind(buf)
        if kind == "code":
            meta["kind"] = "code"
        elif kind == "table":
            meta["kind"] = "table"
            meta["is_table"] = True
            meta["table_rows"] = rows
        if _len_tokens(block, enc) <= chunk_tokens or kind in ("code", "table"):
            # Keep tables and code fences whole; small prose blocks too.
            if block.strip():
                chunks.append(Chunk(text=block.strip(), index=len(chunks), metadata=dict(meta),
                                   char_span=(start_off, end_off)))
        else:
            for sub in recursive_split(block, enc, chunk_tokens, overlap_tokens):
                local = block.find(sub.text)
                if local < 0:
                    local = 0
                sub.metadata = dict(meta)
                sub.char_span = (start_off + local, start_off + local + len(sub.text))
                sub.index = len(chunks)
                chunks.append(sub)
        buf.clear()

    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                buf.append(line)
                flush()
                in_code = False
            else:
                flush()
                buf = [line]
                in_code = True
            i += 1
            continue
        if in_code:
            buf.append(line)
            i += 1
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            # Heading: flush prior prose, then update heading context (heading itself is not a chunk).
            flush()
            cur_heading = stripped.lstrip("#").strip()
            cur_heading_raw = line.strip()
            cur_heading_line = i
            buf_line0 = i + 1
            i += 1
            continue
        if _is_table_row(line) and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            flush()
            # Consume the table: header + separator + data rows until a non-table line.
            table_lines = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_lines.append(lines[j])
                j += 1
            table_block = "\n".join(table_lines)
            start_off = _line_offset(lines, i)
            end_off = start_off + len(table_block)
            chunks.append(Chunk(
                text=table_block, index=len(chunks),
                metadata={"heading": cur_heading, "kind": "table", "is_table": True,
                          "table_rows": max(0, len(table_lines) - 2)},
                char_span=(start_off, end_off),
            ))
            buf_line0 = j
            i = j
            continue
        if not buf:
            buf_line0 = i
        buf.append(line)
        i += 1
    flush()
    return chunks


def _line_offset(lines: list[str], line_idx: int) -> int:
    # character offset of the start of line `line_idx` (accounting for the '\n' between lines).
    off = 0
    for k in range(line_idx):
        off += len(lines[k]) + 1
    return off


def _block_kind(buf: list[str]) -> tuple[str | None, int]:
    if buf and buf[0].strip().startswith("```"):
        return "code", 0
    if len(buf) >= 2 and _is_table_sep(buf[1]) and _is_table_row(buf[0]):
        return "table", max(0, len(buf) - 2)
    return None, 0


def chunk_text(
    text: str, chunk_tokens: int = 512, overlap_tokens: int = 64, enc=None,
    content_type: str = "text",
) -> list[Chunk]:
    """Chunk `text` into ~chunk_tokens token pieces with overlap_tokens overlap.

    When `content_type` is markdown/code/html, structural metadata + char_span citations are
    preserved via `chunk_structured`; otherwise a plain recursive split is used.
    """
    return chunk_structured(
        text, content_type=content_type, chunk_tokens=chunk_tokens,
        overlap_tokens=overlap_tokens, enc=enc,
    )
