"""Side-effect-minimal deployment diagnostics."""

from __future__ import annotations

import importlib
import importlib.util
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .runtime import verified_pack
from .service import environment_kill_switch


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _secure_policy_file(path: Path) -> bool:
    details = path.lstat()
    mode = stat.S_IMODE(details.st_mode)
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == 0
        and mode & 0o022 == 0
        and mode & 0o007 == 0
    )


def _trusted_parent_chain(path: Path) -> bool:
    current = path.parent
    while True:
        details = current.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            return False
        if current.parent == current:
            return True
        current = current.parent


def _trusted_policy_path(path: Path) -> bool:
    return _secure_policy_file(path) and _trusted_parent_chain(path)


def _private_state_file(path: Path) -> bool:
    details = path.lstat()
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and stat.S_IMODE(details.st_mode) == 0o600
    )


def _private_secret_file(path: Path) -> bool:
    details = path.lstat()
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == 0
        and stat.S_IMODE(details.st_mode) == 0o600
    )


def _slack_generation_status_supported() -> bool:
    try:
        slack_sdk = importlib.import_module("slack_sdk")
    except Exception:  # noqa: BLE001 - optional package diagnostics must stay offline
        return False
    web_client = getattr(slack_sdk, "WebClient", None)
    return callable(getattr(web_client, "assistant_threads_setStatus", None))


def run_checks(config: AppConfig) -> tuple[Check, ...]:
    checks: list[Check] = []
    pack = None
    try:
        pack = verified_pack(config)
        hash_mode = "pinned" if config.pack.sha256 is not None else "computed"
        checks.append(Check(
            "pack",
            True,
            f"verified {hash_mode}_sha256={pack.sha256[:12]} bytes={pack.size_bytes}",
        ))
    except Exception as exc:  # noqa: BLE001 - diagnostics must report every pack failure
        checks.append(Check("pack", False, type(exc).__name__))
    if config.web.enabled:
        if config.provider.kind == "anthropic":
            web_detail = (
                f"provider-hosted search_uses={config.web.max_search_uses} "
                f"fetch_uses={config.web.max_fetch_uses}; network not tested"
            )
        else:
            web_detail = (
                "provider-hosted Responses web_search "
                f"max_tool_calls={config.web.max_search_uses + config.web.max_fetch_uses}; "
                "endpoint support and network not tested"
            )
        checks.append(
            Check(
                "web",
                config.provider.kind in {"anthropic", "openai-compatible"},
                web_detail,
            )
        )
    else:
        checks.append(Check("web", True, "disabled", warning=True))
    if config.runtime.stage in {"staging", "production"}:
        try:
            private = _trusted_policy_path(config.source)
        except OSError:
            private = False
        checks.append(Check("config_permissions", private, "root-owned trusted path" if private else "must be root-owned, non-symlinked, and outside writable parent paths"))
        try:
            pack_private = _trusted_policy_path(config.pack.path)
        except OSError:
            pack_private = False
        checks.append(Check("pack_permissions", pack_private, "root-owned trusted path" if pack_private else "must be root-owned, non-symlinked, and outside writable parent paths"))
        environment_file = config.source.with_name("env")
        if os.path.lexists(environment_file):
            try:
                environment_private = (
                    _private_secret_file(environment_file)
                    and _trusted_parent_chain(environment_file)
                )
            except OSError:
                environment_private = False
            checks.append(Check(
                "environment_permissions",
                environment_private,
                "root-owned mode 0600 on trusted path" if environment_private else "must be root-owned, mode 0600, non-symlinked, and outside writable parent paths",
            ))
        else:
            checks.append(Check(
                "environment_permissions",
                True,
                "no sibling env file; external secret injection assumed",
                warning=True,
            ))
    else:
        checks.append(Check("config_permissions", True, "not enforced in local stage"))
    if config.store.kind == "sqlite" and config.store.path is not None:
        parent = config.store.path.parent
        ok = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
        if ok and config.runtime.stage in {"staging", "production"}:
            try:
                details = parent.lstat()
                ok = (
                    stat.S_ISDIR(details.st_mode)
                    and details.st_uid == os.geteuid()
                    and stat.S_IMODE(details.st_mode) & 0o077 == 0
                )
            except OSError:
                ok = False
        checks.append(Check(
            "state_directory",
            ok,
            "private and writable" if ok else "must exist, be service-owned, private, and writable",
        ))
        if config.store.path.exists():
            try:
                private = _private_state_file(config.store.path)
            except OSError:
                private = False
            checks.append(Check("state_permissions", private, "private" if private else "expected service owner and mode 0600"))
    else:
        checks.append(Check("state", True, "memory store"))
    disabled = config.runtime.kill_switch or environment_kill_switch() or config.adapter.kind == "disabled"
    checks.append(Check("kill_switch", True, "active" if disabled else "inactive", warning=disabled))
    if config.provider.kind in {"openai-compatible", "anthropic"}:
        package_name = (
            "openai" if config.provider.kind == "openai-compatible" else "anthropic"
        )
        check_prefix = config.provider.kind.replace("-", "_")
        installed = importlib.util.find_spec(package_name) is not None
        checks.append(
            Check(
                f"{check_prefix}_package",
                installed,
                "installed" if installed else "missing",
            )
        )
        secret_name = (
            config.provider.api_key_env
            if config.provider.kind == "openai-compatible"
            else "ANTHROPIC_API_KEY"
        )
        if secret_name:
            key_present = bool(os.environ.get(secret_name))
            checks.append(
                Check(
                    f"{check_prefix}_secret",
                    key_present or disabled,
                    "present" if key_present else "not required while disabled",
                    warning=not key_present,
                )
            )
        else:
            checks.append(
                Check(
                    f"{check_prefix}_secret",
                    True,
                    "not configured for local loopback endpoint",
                )
            )
        if config.provider.kind == "openai-compatible":
            checks.append(
                Check(
                    "openai_compatible_endpoint",
                    config.provider.base_url is not None,
                    "configured; network not tested",
                )
            )
        checks.append(Check("model", True, config.provider.model))
    else:
        checks.append(Check("provider", True, "fake; no network"))
    if config.adapter.kind == "slack":
        installed = importlib.util.find_spec("slack_bolt") is not None
        checks.append(Check("slack_package", installed, "installed" if installed else "missing"))
        if config.adapter.show_generation_status:
            status_supported = installed and _slack_generation_status_supported()
            checks.append(
                Check(
                    "slack_generation_status",
                    status_supported,
                    (
                        "assistant_threads_setStatus available"
                        if status_supported
                        else "Slack SDK lacks assistant_threads_setStatus"
                    ),
                )
            )
        tokens_present = bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN"))
        checks.append(Check("slack_secrets", tokens_present or disabled, "present" if tokens_present else "not required while disabled", warning=not tokens_present))
        route_configured = bool(config.adapter.allowed_installations) and (
            config.adapter.channel_policy == "joined"
            or bool(config.adapter.allowed_channels)
        )
        checks.append(
            Check(
                "allowlist",
                route_configured,
                f"workspace_allowlist + channel_policy={config.adapter.channel_policy}",
            )
        )
    elif config.adapter.kind == "discord":
        installed = importlib.util.find_spec("discord") is not None
        checks.append(
            Check(
                "discord_package",
                installed,
                "installed" if installed else "missing",
            )
        )
        token_present = bool(os.environ.get("DISCORD_BOT_TOKEN"))
        checks.append(
            Check(
                "discord_secret",
                token_present or disabled,
                "present" if token_present else "not required while disabled",
                warning=not token_present,
            )
        )
        route_configured = bool(
            config.adapter.allowed_installations and config.adapter.allowed_channels
        )
        checks.append(
            Check(
                "allowlist",
                route_configured,
                "server_allowlist + channel_policy=allowlist",
            )
        )
    else:
        checks.append(Check("adapter", True, "disabled; no platform connection"))
    return tuple(checks)
