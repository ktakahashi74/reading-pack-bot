"""Runtime factories kept separate from CLI and platform adapters."""

from __future__ import annotations

import os

from .config import AppConfig
from .errors import ConfigurationError, PackValidationError
from .models import PackSnapshot
from .pack import load_pack
from .providers import AnthropicProvider, FakeProvider, OpenAICompatibleProvider
from .providers.base import ModelProvider
from .service import StateStore
from .stores import MemoryStore, SQLiteStore


def verified_pack(config: AppConfig) -> PackSnapshot:
    pack = load_pack(
        config.pack.path,
        expected_sha256=config.pack.sha256,
        max_bytes=config.pack.max_bytes,
    )
    if config.runtime.stage == "production" and pack.header["status"] != "canonical":
        raise PackValidationError("production requires a canonical Reading Pack")
    return pack


def create_store(config: AppConfig) -> StateStore:
    if config.store.kind == "memory":
        return MemoryStore()
    if config.store.path is None:
        raise ConfigurationError("SQLite path is missing")
    return SQLiteStore(config.store.path)


def create_provider(config: AppConfig) -> ModelProvider:
    if config.provider.kind == "fake":
        return FakeProvider()
    parameters = {
        "model": config.provider.model,
        "timeout_seconds": config.provider.timeout_seconds,
        "max_retries": config.provider.max_retries,
        "max_output_tokens": config.provider.max_output_tokens,
    }
    if config.provider.kind == "openai-compatible":
        api_key_env = config.provider.api_key_env
        api_key = os.environ.get(api_key_env) if api_key_env else "local-no-key"
        if api_key_env and not api_key:
            raise ConfigurationError(f"{api_key_env} is required")
        if config.provider.base_url is None:
            raise ConfigurationError("OpenAI-compatible base URL is missing")
        return OpenAICompatibleProvider(
            **parameters,
            base_url=config.provider.base_url,
            api_key=api_key,
            web=config.web,
        )
    if config.provider.kind == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigurationError("ANTHROPIC_API_KEY is required")
        return AnthropicProvider(**parameters, web=config.web)
    raise ConfigurationError("unsupported provider")
