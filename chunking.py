"""Deterministic Markdown chunking (PLAN.md 7).

Splitting happens on heading boundaries first, then long sections are split
again by character count with an overlap.

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


def chunk_markdown(
    md: str,
    max_chars: int = config.CHUNK_MAX_CHARS,
    overlap: int = config.CHUNK_OVERLAP,
) -> list[dict]:
    """Split Markdown into chunks.

    Returns a list of {"text": str, "heading_path": list[str]} in document order.
    """
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")

    chunks: list[dict] = []
    for heading_path, body in _iter_sections(md):
        for piece in _split_long(body, max_chars, overlap):
            chunks.append({"text": piece, "heading_path": list(heading_path)})
    return chunks
