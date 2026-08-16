"""OpenAI-compatible adapter with optional hosted Responses web search."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..config import OPENAI_COMPATIBLE_DEFAULT_BASE_URL, WebConfig
from ..errors import ConfigurationError, ProviderError
from ..models import GenerationRequest, GenerationResult, Usage
from .base import append_source_urls, provider_text_context

_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chat_usage(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return Usage(
        input_tokens=_integer(getattr(usage, "prompt_tokens", None)),
        output_tokens=_integer(getattr(usage, "completion_tokens", None)),
        total_tokens=_integer(getattr(usage, "total_tokens", None)),
        cached_tokens=_integer(getattr(prompt_details, "cached_tokens", None)),
        reasoning_tokens=_integer(getattr(completion_details, "reasoning_tokens", None)),
    )


def _responses_usage(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=_integer(getattr(usage, "input_tokens", None)),
        output_tokens=_integer(getattr(usage, "output_tokens", None)),
        total_tokens=_integer(getattr(usage, "total_tokens", None)),
        cached_tokens=_integer(getattr(input_details, "cached_tokens", None)),
        reasoning_tokens=_integer(getattr(output_details, "reasoning_tokens", None)),
    )


def _items(value: Any) -> list[Any] | None:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return None
    return list(value)


def _responses_text(response: Any) -> str | None:
    output = _items(getattr(response, "output", None))
    if output is None:
        return None
    parts: list[str] = []
    sources: list[str] = []
    for item in output:
        if (
            getattr(item, "type", None) != "message"
            or getattr(item, "role", None) != "assistant"
        ):
            continue
        content = _items(getattr(item, "content", None))
        if content is None:
            continue
        for block in content:
            if getattr(block, "type", None) != "output_text":
                continue
            value = getattr(block, "text", None)
            if isinstance(value, str):
                parts.append(value)
            annotations = _items(getattr(block, "annotations", None))
            if annotations is None:
                continue
            for annotation in annotations:
                if getattr(annotation, "type", None) != "url_citation":
                    continue
                url = getattr(annotation, "url", None)
                if isinstance(url, str) and url and url not in sources:
                    sources.append(url)
    text = "".join(parts)
    if not text.strip():
        return None
    return append_source_urls(text, sources)


def _safe_optional_identifier(value: Any, *, maximum: int = 200) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        max_output_tokens: int,
        base_url: str = OPENAI_COMPATIBLE_DEFAULT_BASE_URL,
        api_key: str | None = None,
        web: WebConfig | None = None,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ConfigurationError("OpenAI-compatible model must be configured")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.web = web
        if client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise ConfigurationError("install reading-pack-bot[openai]") from None
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
        self._client = client

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if self.web is not None and self.web.enabled:
            return self._generate_with_web(request)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": provider_text_context(request)}
        ]
        messages.extend(
            {"role": turn.role, "content": turn.content}
            for turn in request.prior_turns
        )
        messages.append({"role": "user", "content": request.current_question})
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK/transport failures
            raise ProviderError(
                f"OpenAI-compatible request failed ({type(exc).__name__})"
            ) from None

        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderError("OpenAI-compatible response must contain one choice")
        choice = choices[0]
        if getattr(choice, "finish_reason", None) != "stop":
            raise ProviderError("OpenAI-compatible response did not complete")
        message = getattr(choice, "message", None)
        output_text = getattr(message, "content", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise ProviderError("OpenAI-compatible response contained no text")

        response_model = getattr(response, "model", None)
        if not isinstance(response_model, str) or not _SAFE_MODEL_RE.fullmatch(
            response_model
        ):
            response_model = self.model
        response_id = _safe_optional_identifier(getattr(response, "id", None))
        return GenerationResult(
            text=output_text,
            model=response_model,
            response_id=response_id,
            usage=_chat_usage(response),
        )

    def _generate_with_web(self, request: GenerationRequest) -> GenerationResult:
        messages: list[dict[str, str]] = [
            {"role": turn.role, "content": turn.content}
            for turn in request.prior_turns
        ]
        messages.append({"role": "user", "content": request.current_question})
        assert self.web is not None
        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=provider_text_context(request),
                input=messages,
                max_output_tokens=self.max_output_tokens,
                max_tool_calls=self.web.max_search_uses + self.web.max_fetch_uses,
                tools=[{"type": "web_search"}],
                tool_choice="auto",
                store=False,
            )
        except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK/transport failures
            raise ProviderError(
                f"OpenAI-compatible request failed ({type(exc).__name__})"
            ) from None

        if getattr(response, "status", None) != "completed":
            raise ProviderError("OpenAI-compatible response did not complete")
        output_text = _responses_text(response)
        if output_text is None:
            raise ProviderError("OpenAI-compatible response contained no text")
        response_model = getattr(response, "model", None)
        if not isinstance(response_model, str) or not _SAFE_MODEL_RE.fullmatch(
            response_model
        ):
            response_model = self.model
        response_id = _safe_optional_identifier(getattr(response, "id", None))
        return GenerationResult(
            text=output_text,
            model=response_model,
            response_id=response_id,
            usage=_responses_usage(response),
        )


# Preserve the import used by schema_version=2 applications while the public
# provider name changes from OpenAI to OpenAI-compatible.
OpenAIProvider = OpenAICompatibleProvider
