"""Provider protocol and shared verified-context construction."""

from __future__ import annotations

import re
from typing import Protocol

from ..models import GenerationRequest, GenerationResult, Usage

_URL_CHARACTER = r"A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-"


def provider_instructions(request: GenerationRequest) -> str:
    """Return the provider-neutral runtime bridge."""
    return request.runtime_instructions


def provider_text_context(request: GenerationRequest) -> str:
    """Fallback context for providers without a citable document input."""
    return (
        provider_instructions(request)
        + "\n\nBEGIN OPERATOR-VERIFIED READING PACK "
        + f"sha256={request.pack.sha256}\n"
        + request.pack.raw_markdown
        + "END OPERATOR-VERIFIED READING PACK"
    )


def append_source_urls(text: str, sources: list[str]) -> str:
    """Append unique source URLs that are not already visible in the answer."""
    unseen_sources: list[str] = []
    for url in sources:
        if url in unseen_sources:
            continue
        if re.search(
            rf"(?<![{_URL_CHARACTER}]){re.escape(url)}(?![{_URL_CHARACTER}])",
            text,
        ) is None:
            unseen_sources.append(url)
    if not unseen_sources:
        return text
    return text.rstrip() + "\n\n出典:\n" + "\n".join(
        f"- {url}" for url in unseen_sources
    )


class ModelProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...


def combined_usage(usages: list[Usage]) -> Usage:
    """Sum provider usage across a bounded local tool-use loop."""

    def total(field: str) -> int | None:
        values = [getattr(usage, field) for usage in usages]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    return Usage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
        cached_tokens=total("cached_tokens"),
        reasoning_tokens=total("reasoning_tokens"),
    )
