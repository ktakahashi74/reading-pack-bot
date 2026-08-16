"""Small helpers shared by platform adapters."""

from __future__ import annotations


def split_message(text: str, limit: int) -> tuple[str, ...]:
    """Split text without dropping whitespace or exceeding a platform limit."""

    if not text:
        return ()
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n", limit // 2)
        if cut < 0:
            cut = window.rfind(" ", limit // 2)
        if cut >= 0:
            cut += 1
        else:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)
