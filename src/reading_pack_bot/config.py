"""Strict TOML configuration without secret values."""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigurationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ANTHROPIC_DATED_MODEL_RE = re.compile(r"^claude-[a-z0-9-]+-\d{8}$")
_ANTHROPIC_CURRENT_MODEL_RE = re.compile(
    r"^claude-[a-z0-9]+-(?P<major>[1-9]\d*)(?:-(?P<minor>[1-9]\d*))?$"
)
_PROVIDER_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
OPENAI_COMPATIBLE_DEFAULT_BASE_URL = "https://api.openai.com/v1"
SERVICE_TIMEOUT_STOP_SECONDS = 120
SERVICE_SHUTDOWN_MARGIN_SECONDS = 5


def _anthropic_model_is_pinned(model: str) -> bool:
    if _ANTHROPIC_DATED_MODEL_RE.fullmatch(model):
        return True
    match = _ANTHROPIC_CURRENT_MODEL_RE.fullmatch(model)
    if match is None:
        return False
    major = int(match.group("major"))
    minor = int(match.group("minor")) if match.group("minor") else None
    return major > 4 or (major == 4 and minor is not None and minor >= 6)


@dataclass(frozen=True)
class RuntimeConfig:
    stage: str
    kill_switch: bool
    log_level: str


@dataclass(frozen=True)
class PackConfig:
    path: Path
    sha256: str | None
    max_bytes: int


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    model: str
    base_url: str | None
    api_key_env: str | None
    timeout_seconds: float
    max_retries: int
    max_output_tokens: int


@dataclass(frozen=True)
class WebConfig:
    enabled: bool
    max_search_uses: int
    max_fetch_uses: int
    max_pause_continuations: int
    max_content_tokens: int


@dataclass(frozen=True)
class StoreConfig:
    kind: str
    path: Path | None
    history_turns: int
    conversation_ttl_seconds: int
    event_ttl_seconds: int


@dataclass(frozen=True)
class PolicyConfig:
    max_question_characters: int
    max_answer_characters: int
    requests_per_window: int
    request_window_seconds: int
    daily_requests: int


@dataclass(frozen=True)
class AdapterConfig:
    kind: str
    allowed_installations: tuple[str, ...]
    channel_policy: str
    allowed_channels: tuple[str, ...]
    message_chunk_characters: int
    queue_size: int
    max_concurrent_generations: int
    post_timeout_seconds: int
    show_generation_status: bool

    def allows_channel(self, channel_id: str) -> bool:
        if not channel_id:
            return False
        return self.channel_policy in {
            "accessible",
            "joined",
        } or channel_id in self.allowed_channels


@dataclass(frozen=True)
class AppConfig:
    source: Path
    runtime: RuntimeConfig
    pack: PackConfig
    provider: ProviderConfig
    web: WebConfig
    store: StoreConfig
    policy: PolicyConfig
    adapter: AdapterConfig


def _table(root: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = root.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def _only_keys(table: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise ConfigurationError(f"{label} has unknown keys: {', '.join(extra)}")


def _string(table: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a string")
    if any(ord(character) < 32 for character in value):
        raise ConfigurationError(f"{key} contains control characters")
    return value


def _boolean(table: Mapping[str, Any], key: str, *, default: bool | None = None) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be true or false")
    return value


def _integer(
    table: Mapping[str, Any], key: str, *, default: int | None = None, minimum: int, maximum: int
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _retention_seconds(
    table: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = table.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (value != 0 and not minimum <= value <= maximum)
    ):
        raise ConfigurationError(
            f"{key} must be 0 for unlimited retention or an integer "
            f"from {minimum} to {maximum}"
        )
    return value


def _number(
    table: Mapping[str, Any], key: str, *, default: float | None = None, minimum: float, maximum: float
) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{key} must be a number")
    converted = float(value)
    if not minimum <= converted <= maximum:
        raise ConfigurationError(f"{key} must be from {minimum} to {maximum}")
    return converted


def _strings(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = table.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigurationError(f"{key} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ConfigurationError(f"{key} must not contain duplicates")
    return tuple(value)


def _path(value: str, base: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    # Preserve the lexical path. Resolving here would hide a symlink from the
    # pack/store loader, which must make its own no-follow decision.
    return Path(os.path.abspath(candidate))


def _is_literal_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_openai_compatible_base_url(value: str, stage: str) -> bool:
    """Validate the configured credential destination and return loopback status."""

    if not value or len(value) > 2048 or any(character.isspace() for character in value):
        raise ConfigurationError("provider.base_url must be a non-empty URL without whitespace")
    if "?" in value or "#" in value:
        raise ConfigurationError("provider.base_url must not contain a query or fragment")
    try:
        parsed = urlsplit(value)
        # Accessing port also rejects malformed or out-of-range port values.
        parsed.port
    except ValueError:
        raise ConfigurationError("provider.base_url is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("provider.base_url must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("provider.base_url must not contain credentials")
    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigurationError("provider.base_url hostname must be ASCII") from None

    is_loopback = _is_literal_loopback_host(parsed.hostname)
    if parsed.scheme != "https" and not (stage == "local" and is_loopback):
        raise ConfigurationError(
            "provider.base_url requires https except for a local-stage loopback endpoint"
        )
    return is_loopback


def _read_configuration(source: Path) -> str:
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode):
                raise ConfigurationError("configuration must be a regular file")
            if details.st_size > 1_048_576:
                raise ConfigurationError("configuration exceeds 1 MiB")
            raw = handle.read(1_048_577)
            after = os.fstat(handle.fileno())
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration: {type(exc).__name__}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > 1_048_576:
        raise ConfigurationError("configuration exceeds 1 MiB")
    if (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ConfigurationError("configuration changed while it was being read")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ConfigurationError("configuration is not strict UTF-8") from None


def load_config(path: str | Path) -> AppConfig:
    source = Path(os.path.abspath(path))
    try:
        root = tomllib.loads(_read_configuration(source))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"cannot read configuration: {type(exc).__name__}") from None
    if not isinstance(root, dict):
        raise ConfigurationError("configuration root must be a table")
    _only_keys(root, {"schema_version", "runtime", "pack", "provider", "web", "store", "policy", "adapter"}, "root")
    if root.get("schema_version") != 3:
        raise ConfigurationError("schema_version must equal 3")

    runtime_raw = _table(root, "runtime")
    _only_keys(runtime_raw, {"stage", "kill_switch", "log_level"}, "[runtime]")
    stage_name = _string(runtime_raw, "stage")
    if stage_name not in {"local", "staging", "production"}:
        raise ConfigurationError("runtime.stage must be local, staging, or production")
    log_level = _string(runtime_raw, "log_level", default="INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("runtime.log_level is invalid")
    if stage_name != "local" and log_level == "DEBUG":
        raise ConfigurationError("DEBUG logging is allowed only in local stage")
    runtime = RuntimeConfig(
        stage=stage_name,
        kill_switch=_boolean(runtime_raw, "kill_switch", default=True),
        log_level=log_level,
    )

    pack_raw = _table(root, "pack")
    _only_keys(pack_raw, {"path", "sha256", "max_bytes"}, "[pack]")
    pinned_hash = _string(pack_raw, "sha256", default="")
    if pinned_hash and not _SHA256_RE.fullmatch(pinned_hash):
        raise ConfigurationError("pack.sha256 must be a lowercase SHA-256 digest")
    pack = PackConfig(
        path=_path(_string(pack_raw, "path"), source.parent),
        sha256=pinned_hash or None,
        max_bytes=_integer(pack_raw, "max_bytes", default=524288, minimum=1024, maximum=4_194_304),
    )

    provider_raw = _table(root, "provider")
    _only_keys(
        provider_raw,
        {
            "kind",
            "model",
            "base_url",
            "api_key_env",
            "timeout_seconds",
            "max_retries",
            "max_output_tokens",
        },
        "[provider]",
    )
    provider_kind = _string(provider_raw, "kind")
    if provider_kind == "openai":
        # Compatibility with schema_version=2 configurations created before
        # the provider-neutral name was introduced.
        provider_kind = "openai-compatible"
    if provider_kind not in {"fake", "openai-compatible", "anthropic"}:
        raise ConfigurationError(
            "provider.kind must be fake, openai-compatible, or anthropic"
        )
    provider_model = _string(provider_raw, "model", default="")
    provider_base_url: str | None = None
    provider_api_key_env: str | None = None
    if provider_kind == "openai-compatible":
        if not provider_model:
            raise ConfigurationError(
                "provider.model is required for an OpenAI-compatible provider"
            )
        if not _PROVIDER_MODEL_RE.fullmatch(provider_model):
            raise ConfigurationError("provider.model is not a safe model identifier")
        provider_base_url = _string(
            provider_raw,
            "base_url",
            default=OPENAI_COMPATIBLE_DEFAULT_BASE_URL,
        )
        is_loopback = _validate_openai_compatible_base_url(
            provider_base_url, runtime.stage
        )
        provider_api_key_env = _string(
            provider_raw, "api_key_env", default="OPENAI_API_KEY"
        )
        if provider_api_key_env:
            if not _ENVIRONMENT_NAME_RE.fullmatch(provider_api_key_env):
                raise ConfigurationError(
                    "provider.api_key_env must be a valid environment variable name"
                )
        elif not (runtime.stage == "local" and is_loopback):
            raise ConfigurationError(
                "provider.api_key_env may be empty only for a local loopback endpoint"
            )
    elif "base_url" in provider_raw or "api_key_env" in provider_raw:
        raise ConfigurationError(
            "provider.base_url and provider.api_key_env require provider.kind=openai-compatible"
        )
    if provider_kind == "anthropic" and not provider_model:
        raise ConfigurationError("provider.model is required for Anthropic")
    if provider_kind == "anthropic" and not _anthropic_model_is_pinned(provider_model):
        raise ConfigurationError("Anthropic requires a pinned model ID, not a floating alias")
    provider_timeout = _number(provider_raw, "timeout_seconds", default=90.0, minimum=1.0, maximum=600.0)
    if runtime.stage != "local" and provider_timeout > 90.0:
        raise ConfigurationError("staging and production limit provider timeout_seconds to 90")
    provider = ProviderConfig(
        kind=provider_kind,
        model=provider_model,
        base_url=provider_base_url,
        api_key_env=provider_api_key_env,
        timeout_seconds=provider_timeout,
        max_retries=_integer(provider_raw, "max_retries", default=1, minimum=0, maximum=5),
        max_output_tokens=_integer(provider_raw, "max_output_tokens", default=4096, minimum=64, maximum=16384),
    )

    web_raw = root.get("web", {})
    if not isinstance(web_raw, dict):
        raise ConfigurationError("[web] must be a TOML table")
    _only_keys(
        web_raw,
        {
            "enabled",
            "max_search_uses",
            "max_fetch_uses",
            "max_pause_continuations",
            "max_content_tokens",
        },
        "[web]",
    )
    web = WebConfig(
        enabled=_boolean(web_raw, "enabled", default=False),
        max_search_uses=_integer(
            web_raw, "max_search_uses", default=8, minimum=1, maximum=20
        ),
        max_fetch_uses=_integer(
            web_raw, "max_fetch_uses", default=5, minimum=1, maximum=20
        ),
        max_pause_continuations=_integer(
            web_raw,
            "max_pause_continuations",
            default=1,
            minimum=0,
            maximum=5,
        ),
        max_content_tokens=_integer(
            web_raw,
            "max_content_tokens",
            default=20000,
            minimum=1000,
            maximum=100000,
        ),
    )
    if web.enabled and provider.kind not in {"anthropic", "openai-compatible"}:
        raise ConfigurationError(
            "hosted web access requires provider.kind=anthropic or openai-compatible"
        )

    store_raw = _table(root, "store")
    _only_keys(store_raw, {"kind", "path", "history_turns", "conversation_ttl_seconds", "event_ttl_seconds"}, "[store]")
    store_kind = _string(store_raw, "kind")
    if store_kind not in {"memory", "sqlite"}:
        raise ConfigurationError("store.kind must be memory or sqlite")
    store_path_text = _string(store_raw, "path", default="")
    if store_kind == "sqlite" and not store_path_text:
        raise ConfigurationError("store.path is required for sqlite")
    store = StoreConfig(
        kind=store_kind,
        path=_path(store_path_text, source.parent) if store_path_text else None,
        history_turns=_integer(store_raw, "history_turns", default=8, minimum=1, maximum=50),
        conversation_ttl_seconds=_retention_seconds(
            store_raw,
            "conversation_ttl_seconds",
            default=604800,
            minimum=60,
            maximum=31_536_000,
        ),
        event_ttl_seconds=_integer(store_raw, "event_ttl_seconds", default=86400, minimum=60, maximum=2_592_000),
    )

    policy_raw = _table(root, "policy")
    _only_keys(policy_raw, {"max_question_characters", "max_answer_characters", "requests_per_window", "request_window_seconds", "daily_requests"}, "[policy]")
    policy = PolicyConfig(
        max_question_characters=_integer(policy_raw, "max_question_characters", default=4000, minimum=1, maximum=20000),
        max_answer_characters=_integer(policy_raw, "max_answer_characters", default=3500, minimum=256, maximum=50000),
        requests_per_window=_integer(policy_raw, "requests_per_window", default=10, minimum=1, maximum=1000),
        request_window_seconds=_integer(policy_raw, "request_window_seconds", default=60, minimum=1, maximum=3600),
        daily_requests=_integer(policy_raw, "daily_requests", default=500, minimum=1, maximum=100000),
    )

    adapter_raw = _table(root, "adapter")
    _only_keys(
        adapter_raw,
        {
            "kind",
            "allowed_installations",
            "channel_policy",
            "allowed_channels",
            "message_chunk_characters",
            "queue_size",
            "max_concurrent_generations",
            "post_timeout_seconds",
            "show_generation_status",
        },
        "[adapter]",
    )
    adapter_kind = _string(adapter_raw, "kind")
    if adapter_kind not in {"disabled", "slack", "discord"}:
        raise ConfigurationError("adapter.kind must be disabled, slack, or discord")
    allowed_channels = _strings(adapter_raw, "allowed_channels")
    default_channel_policy = (
        "accessible" if not allowed_channels else "allowlist"
    )
    channel_policy = _string(
        adapter_raw, "channel_policy", default=default_channel_policy
    )
    if channel_policy == "joined":
        channel_policy = "accessible"
    adapter = AdapterConfig(
        kind=adapter_kind,
        allowed_installations=_strings(adapter_raw, "allowed_installations"),
        channel_policy=channel_policy,
        allowed_channels=allowed_channels,
        message_chunk_characters=_integer(adapter_raw, "message_chunk_characters", default=3500, minimum=500, maximum=10000),
        queue_size=_integer(adapter_raw, "queue_size", default=64, minimum=1, maximum=1000),
        max_concurrent_generations=_integer(
            adapter_raw,
            "max_concurrent_generations",
            default=1,
            minimum=1,
            maximum=4,
        ),
        post_timeout_seconds=_integer(adapter_raw, "post_timeout_seconds", default=10, minimum=1, maximum=60),
        show_generation_status=_boolean(
            adapter_raw, "show_generation_status", default=False
        ),
    )
    if adapter.channel_policy not in {"allowlist", "accessible"}:
        raise ConfigurationError(
            "adapter.channel_policy must be allowlist or accessible"
        )
    if adapter.kind == "slack" and not adapter.allowed_installations:
        raise ConfigurationError("Slack requires a non-empty workspace allowlist")
    if adapter.kind == "discord" and not adapter.allowed_installations:
        raise ConfigurationError("Discord requires a non-empty server allowlist")
    if (
        adapter.kind == "slack"
        and adapter.channel_policy == "allowlist"
        and not adapter.allowed_channels
    ):
        raise ConfigurationError(
            "Slack channel_policy=allowlist requires a non-empty channel allowlist"
        )
    if (
        adapter.kind == "slack"
        and adapter.channel_policy == "accessible"
        and adapter.allowed_channels
    ):
        raise ConfigurationError(
            "Slack channel_policy=accessible requires adapter.allowed_channels=[]"
        )
    if (
        adapter.kind == "discord"
        and adapter.channel_policy == "allowlist"
        and not adapter.allowed_channels
    ):
        raise ConfigurationError(
            "Discord channel_policy=allowlist requires a non-empty channel allowlist"
        )
    if (
        adapter.kind == "discord"
        and adapter.channel_policy == "accessible"
        and adapter.allowed_channels
    ):
        raise ConfigurationError(
            "Discord channel_policy=accessible requires adapter.allowed_channels=[]"
        )
    if adapter.kind == "discord" and adapter.message_chunk_characters > 2000:
        raise ConfigurationError(
            "Discord limits adapter.message_chunk_characters to 2000"
        )
    if (
        adapter.max_concurrent_generations != 1
        and adapter.kind not in {"slack", "discord"}
    ):
        raise ConfigurationError(
            "adapter.max_concurrent_generations requires adapter.kind=slack or discord"
        )
    if (
        adapter.show_generation_status
        and adapter.kind not in {"slack", "discord"}
    ):
        raise ConfigurationError(
            "adapter.show_generation_status requires adapter.kind=slack or discord"
        )
    platform_adapter = adapter.kind in {"slack", "discord"}
    if runtime.stage != "local" and platform_adapter and store.kind != "sqlite":
        raise ConfigurationError(
            f"non-local {adapter.kind.title()} deployments require the sqlite store"
        )
    if runtime.stage != "local" and platform_adapter:
        platform_name = adapter.kind.title()
        if provider.max_retries != 0:
            raise ConfigurationError(
                f"non-local {platform_name} requires provider.max_retries=0"
            )
        if adapter.queue_size != 1:
            raise ConfigurationError(
                f"non-local {platform_name} requires adapter.queue_size=1"
            )
        if adapter.post_timeout_seconds > 10:
            raise ConfigurationError(
                f"non-local {platform_name} limits adapter.post_timeout_seconds to 10"
            )
        if policy.max_answer_characters > adapter.message_chunk_characters:
            raise ConfigurationError(
                f"non-local {platform_name} requires answers to fit one platform message"
            )
        provider_calls = 1 + (
            web.max_pause_continuations
            if web.enabled and provider.kind == "anthropic"
            else 0
        )
        platform_calls = 1 + int(adapter.show_generation_status)
        worst_case_seconds = (
            provider_calls * provider.timeout_seconds
            + platform_calls * adapter.post_timeout_seconds
        )
        if worst_case_seconds > (
            SERVICE_TIMEOUT_STOP_SECONDS - SERVICE_SHUTDOWN_MARGIN_SECONDS
        ):
            raise ConfigurationError(
                f"non-local {platform_name} status, provider, retrieval, continuation, and post "
                f"timeouts must fit TimeoutStopSec={SERVICE_TIMEOUT_STOP_SECONDS}"
            )
    if runtime.stage == "production" and provider.kind not in {"openai-compatible", "anthropic"}:
        raise ConfigurationError("production requires an external model provider")
    if runtime.stage == "production" and store.kind != "sqlite":
        raise ConfigurationError("production requires the sqlite store")

    return AppConfig(
        source=source,
        runtime=runtime,
        pack=pack,
        provider=provider,
        web=web,
        store=store,
        policy=policy,
        adapter=adapter,
    )
