"""Deterministic Markdown chunking (PLAN.md 7).

Sections are packed into useful-sized chunks first. A section longer than the
hard cap is split again by character count with an overlap.

Determinism matters: incremental ingest compares chunk text between runs, so a
chunker that produced different output for the same input would make the weekly
cron re-embed everything. No randomness, no dict-ordering dependence, no clock.
"""

from __future__ import annotations

import re

import config

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _iter_sections(md: str) -> list[tuple[list[str], str]]:
    """Split Markdown into (heading_path, body) pairs on heading boundaries.

    Headings inside fenced code blocks are not treated as headings.
    """
    sections: list[tuple[list[str], str]] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(([title for _, title in heading_stack], body))
        buffer.clear()

    for line in md.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            buffer.append(line)
            continue

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            # Keep the heading itself in the body so the chunk carries its title.
            buffer.append(line)
        else:
            buffer.append(line)

    flush()
    return sections


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split text that exceeds max_chars, preferring paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            pieces.append(text[start:].strip())
            break

        # Prefer to cut at the last blank line, then the last newline, inside the window.
        window = text[start:end]
        cut = window.rfind("\n\n")
        if cut < max_chars // 2:
            cut = window.rfind("\n")
        if cut < max_chars // 2:
            cut = max_chars

        pieces.append(text[start : start + cut].strip())
        # Step forward by at least one character so the loop always terminates.
        start += max(cut - overlap, 1)

    return [piece for piece in pieces if piece]


def _join_sections(sections: list[tuple[list[str], str]]) -> str:
    """Join already-trimmed sections without losing their heading lines."""
    return "\n\n".join(body for _, body in sections)


def _chunk(text: str, heading_path: list[str]) -> dict:
    return {"text": text, "heading_path": list(heading_path)}


def chunk_markdown(
    md: str,
    max_chars: int | None = None,
    overlap: int | None = None,
    *,
    target_chars: int | None = None,
    min_chars: int | None = None,
) -> list[dict]:
    """Pack Markdown sections into chunks in document order.

    Sections are packed up to ``target_chars``. A short section at the end of a
    pending chunk is allowed to pull the next section in up to the hard cap, so
    titles and property blocks do not become useless standalone chunks.

    Returns a list of {"text": str, "heading_path": list[str]} in document
    order. The heading path always belongs to the first section in a chunk.
    """
    if max_chars is None:
        max_chars = config.CHUNK_MAX_CHARS
    if overlap is None:
        overlap = config.CHUNK_OVERLAP
    if target_chars is None:
        target_chars = config.CHUNK_TARGET_CHARS
    if min_chars is None:
        min_chars = config.CHUNK_MIN_CHARS

    target_chars = min(target_chars, max_chars)
    if max_chars <= 0 or target_chars <= 0 or min_chars < 0:
        raise ValueError("chunk sizes must be positive")
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")

    sections = _iter_sections(md)
    chunks: list[dict] = []
    pending: list[tuple[list[str], str]] = []
    pending_start = 0

    def flush_pending() -> None:
        if not pending:
            return
        text = _join_sections(pending)
        # The normal packing rules keep this below the hard cap. This fallback
        # also protects callers if a future rule creates an oversized group.
        for piece in _split_long(text, max_chars, overlap):
            chunks.append(_chunk(piece, pending[0][0]))
        pending.clear()

    def emit_long(text: str, heading_path: list[str]) -> None:
        for piece in _split_long(text, max_chars, overlap):
            chunks.append(_chunk(piece, heading_path))

    index = 0
    while index < len(sections):
        heading_path, body = sections[index]

        if len(body) > max_chars:
            if pending:
                # Keep a page's title/property section with its first body even
                # when that first body needs splitting. Other pending sections
                # cannot be merged without making a chunk exceed the hard cap.
                if pending_start == 0 and len(pending) == 1:
                    combined = _join_sections(pending + [(heading_path, body)])
                    emit_long(combined, pending[0][0])
                    pending.clear()
                else:
                    flush_pending()
                    emit_long(body, heading_path)
            else:
                emit_long(body, heading_path)
            index += 1
            continue

        if not pending:
            pending = [(heading_path, body)]
            pending_start = index
            index += 1
            continue

        candidate = _join_sections(pending + [(heading_path, body)])
        if len(candidate) <= target_chars:
            pending.append((heading_path, body))
            index += 1
            continue

        # A short section should not be left alone just because the target was
        # crossed. The hard cap remains the final boundary.
        if len(pending[-1][1]) < min_chars and len(candidate) <= max_chars:
            pending.append((heading_path, body))
            index += 1
            continue

        # A page title/property block is a special first section: it must join
        # the first body section whenever that is possible under the hard cap.
        # If the body is itself close to the cap, split the combined text so the
        # title still accompanies its first body piece.
        if pending_start == 0 and len(pending) == 1 and len(candidate) > max_chars:
            emit_long(candidate, pending[0][0])
            pending.clear()
            index += 1
            continue

        flush_pending()
        pending = [(heading_path, body)]
        pending_start = index
        index += 1

    flush_pending()
    return chunks
