"""Minimal Anthropic Messages API adapter with provider-hosted web tools."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..config import WebConfig
from ..errors import ConfigurationError, ProviderError
from ..models import GenerationRequest, GenerationResult, Usage
from .base import append_source_urls, combined_usage, provider_instructions


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    input_tokens = _integer(getattr(usage, "input_tokens", None))
    output_tokens = _integer(getattr(usage, "output_tokens", None))
    cache_creation_tokens = _integer(
        getattr(usage, "cache_creation_input_tokens", None)
    )
    cache_read_tokens = _integer(getattr(usage, "cache_read_input_tokens", None))
    input_parts = [
        value
        for value in (input_tokens, cache_creation_tokens, cache_read_tokens)
        if value is not None
    ]
    complete_input_tokens = sum(input_parts) if input_parts else None
    output_details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=complete_input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            complete_input_tokens + output_tokens
            if complete_input_tokens is not None and output_tokens is not None
            else None
        ),
        cached_tokens=cache_read_tokens,
        reasoning_tokens=_integer(getattr(output_details, "thinking_tokens", None)),
    )


def _content_items(content: Any) -> list[Any] | None:
    if not isinstance(content, Iterable) or isinstance(content, (str, bytes, dict)):
        return None
    return list(content)


def _fetch_urls(batches: list[list[Any]]) -> list[str]:
    urls: list[str] = []
    for blocks in batches:
        for block in blocks:
            if getattr(block, "type", None) != "web_fetch_tool_result":
                continue
            url = getattr(getattr(block, "content", None), "url", None)
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _citation_url(citation: Any, fetched_urls: list[str]) -> str | None:
    url = getattr(citation, "url", None)
    if isinstance(url, str) and url:
        return url
    if getattr(citation, "type", None) not in {
        "char_location",
        "content_block_location",
        "page_location",
    }:
        return None
    document_index = getattr(citation, "document_index", None)
    if not isinstance(document_index, int) or isinstance(document_index, bool):
        return None
    # The request's Reading Pack is document 0. Provider-fetched documents
    # follow it in fetch-result order. If a future API version indexes them
    # differently, omit the URL rather than rejecting an otherwise useful answer.
    fetch_index = document_index - 1
    return fetched_urls[fetch_index] if 0 <= fetch_index < len(fetched_urls) else None


def _render_text(batches: list[list[Any]]) -> str | None:
    fetched_urls = _fetch_urls(batches)
    parts: list[str] = []
    sources: list[str] = []
    for blocks in batches:
        for block in blocks:
            if getattr(block, "type", None) != "text":
                continue
            value = getattr(block, "text", None)
            if not isinstance(value, str):
                continue
            parts.append(value)
            citations = getattr(block, "citations", None) or ()
            if not isinstance(citations, Iterable) or isinstance(
                citations, (str, bytes, dict)
            ):
                continue
            for citation in citations:
                url = _citation_url(citation, fetched_urls)
                if url is not None and url not in sources:
                    sources.append(url)
    text = "".join(parts)
    if not text.strip():
        return None
    return append_source_urls(text, sources)


def _pack_message(request: GenerationRequest) -> dict[str, Any]:
    version = request.pack.header.get("v", "unknown")
    return {
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": request.pack.raw_markdown,
                },
                "title": f"Reading Pack {version}",
                "context": f"Operator-verified sha256={request.pack.sha256}",
                "citations": {"enabled": True},
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {
                "type": "text",
                "text": "The attached document is the Reading Pack referenced by the system instruction.",
            },
        ],
    }


def _web_tools(config: WebConfig) -> list[dict[str, Any]]:
    if not config.enabled:
        return []
    return [
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "allowed_callers": ["direct"],
            "max_uses": config.max_search_uses,
        },
        {
            "type": "web_fetch_20260318",
            "name": "web_fetch",
            "allowed_callers": ["direct"],
            "max_uses": config.max_fetch_uses,
            "max_content_tokens": config.max_content_tokens,
            "citations": {"enabled": True},
        },
    ]


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        max_output_tokens: int,
        web: WebConfig | None = None,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ConfigurationError("Anthropic model must be configured")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.web = web
        if client is None:
            try:
                from anthropic import Anthropic
            except ImportError:
                raise ConfigurationError("install reading-pack-bot[anthropic]") from None
            client = Anthropic(timeout=timeout_seconds, max_retries=max_retries)
        self._client = client

    def generate(self, request: GenerationRequest) -> GenerationResult:
        messages: list[dict[str, Any]] = [_pack_message(request)]
        history: list[dict[str, Any]] = [
            {"role": turn.role, "content": turn.content} for turn in request.prior_turns
        ]
        if history:
            history[-1]["content"] = [
                {
                    "type": "text",
                    "text": history[-1]["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        messages.extend(history)
        messages.append({"role": "user", "content": request.current_question})
        request_options: dict[str, Any] = {
            "model": self.model,
            "system": provider_instructions(request),
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if self.web is not None:
            tools = _web_tools(self.web)
            if tools:
                request_options["tools"] = tools
        if self.model == "claude-sonnet-5" or re.fullmatch(
            r"claude-sonnet-5-\d{8}", self.model
        ):
            request_options["thinking"] = {"type": "adaptive", "display": "omitted"}
            request_options["output_config"] = {"effort": "medium"}

        usages: list[Usage] = []
        response_batches: list[list[Any]] = []
        pause_continuations = 0
        max_pause_continuations = (
            self.web.max_pause_continuations
            if self.web is not None and self.web.enabled
            else 0
        )
        while True:
            request_options["messages"] = list(messages)
            try:
                response = self._client.messages.create(**request_options)
            except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK/transport failures
                raise ProviderError(f"Anthropic request failed ({type(exc).__name__})") from None
            response_model = getattr(response, "model", None)
            if not isinstance(response_model, str) or response_model != self.model:
                raise ProviderError("Anthropic response model differs from the configured snapshot")
            usages.append(_usage(response))
            content = _content_items(getattr(response, "content", None))
            if content is None:
                raise ProviderError("Anthropic response contained malformed content")
            response_batches.append(content)
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "end_turn":
                output_text = _render_text(response_batches)
                if output_text is None:
                    raise ProviderError("Anthropic response contained no text")
                response_id = getattr(response, "id", None)
                return GenerationResult(
                    text=output_text,
                    model=response_model,
                    response_id=response_id if isinstance(response_id, str) else None,
                    usage=combined_usage(usages),
                )
            if stop_reason != "pause_turn" or max_pause_continuations == 0:
                raise ProviderError("Anthropic response did not complete normally")
            if not content:
                raise ProviderError("Anthropic pause_turn contained no resubmittable content")
            pause_continuations += 1
            if pause_continuations > max_pause_continuations:
                raise ProviderError("Anthropic server-tool continuation exceeded its bound")
            messages.append({"role": "assistant", "content": content})
