"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .adapters import DiscordAdapter, SlackAdapter
from .config import AppConfig, load_config
from .doctor import run_checks
from .errors import (
    ConfigurationError,
    PackValidationError,
    ProviderError,
    ReadingPackBotError,
)
from .models import GenerationRequest, __version__
from .policy import MessagePolicy
from .runtime import create_provider, create_store, verified_pack
from .service import BotService, build_runtime_instructions, environment_kill_switch

LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reading-pack-bot")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "doctor", "purge"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
    ask = subparsers.add_parser("ask")
    ask.add_argument("--config", required=True, type=Path)
    ask.add_argument("--allow-live", action="store_true")
    run = subparsers.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--once", action="store_true", help="preflight and exit without connecting")
    return parser


def _logging(config: AppConfig) -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("reading_pack_bot").setLevel(getattr(logging, config.runtime.log_level))
    for logger_name in (
        "anthropic",
        "openai",
        "httpx",
        "httpcore",
        "slack_bolt",
        "slack_sdk",
        "discord",
        "aiohttp",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _verify(config: AppConfig) -> int:
    pack = verified_pack(config)
    print(
        f"OK v={pack.header.get('v', 'unknown')} status={pack.header.get('status', 'unknown')} "
        f"sha256={pack.sha256} bytes={pack.size_bytes}"
    )
    return 0


def _doctor(config: AppConfig) -> int:
    failed = False
    for check in run_checks(config):
        label = "WARN" if check.warning and check.ok else "OK" if check.ok else "FAIL"
        print(f"{label:4} {check.name}: {check.detail}")
        failed = failed or not check.ok
    return 1 if failed else 0


def _read_question(config: AppConfig) -> str:
    limit = config.policy.max_question_characters
    raw = sys.stdin.read(limit + 1)
    if len(raw) > limit:
        raise ConfigurationError("question exceeds configured character limit")
    question = raw.strip()
    if not question:
        raise ConfigurationError("question is empty")
    return question


def _terminal_safe(text: str) -> str:
    rendered: list[str] = []
    for character in text:
        value = ord(character)
        if character in {"\n", "\t"} or (
            value >= 0x20 and not 0x7F <= value <= 0x9F
        ):
            rendered.append(character)
        elif value <= 0xFF:
            rendered.append(f"\\x{value:02x}")
        else:  # pragma: no cover - current Unicode controls are within the BMP
            rendered.append(f"\\u{value:04x}")
    return "".join(rendered)


def _ask(config: AppConfig, allow_live: bool) -> int:
    if config.provider.kind != "fake":
        if config.runtime.kill_switch or environment_kill_switch():
            raise ConfigurationError("live provider is disabled by a kill switch")
        if not allow_live:
            raise ConfigurationError("live provider requires --allow-live")
    question = _read_question(config)
    pack = verified_pack(config)
    provider = create_provider(config)
    result = provider.generate(
        GenerationRequest(
            runtime_instructions=build_runtime_instructions(
                config.policy.max_answer_characters,
                web_enabled=config.web.enabled,
            ),
            pack=pack,
            prior_turns=(),
            current_question=question,
        )
    )
    policy = MessagePolicy(config.runtime, config.adapter, config.policy)
    answer = policy.bound_answer(result.text.strip())
    if not answer:
        raise ProviderError("provider returned an empty response")
    print(_terminal_safe(answer))
    return 0


def _idle(once: bool) -> int:
    LOGGER.info("service_ready adapter=disabled outbound_clients=off")
    if once:
        return 0
    stopped = threading.Event()

    def stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stopped.wait()
    LOGGER.info("service_stopped")
    return 0


def _run(config: AppConfig, once: bool) -> int:
    if config.runtime.stage in {"staging", "production"}:
        failures = tuple(check.name for check in run_checks(config) if not check.ok)
        if failures:
            raise ConfigurationError(
                f"deployment preflight failed: {', '.join(failures)}"
            )
    pack = verified_pack(config)
    store = create_store(config)
    try:
        store.purge(
            time.time(),
            config.store.conversation_ttl_seconds,
            config.store.event_ttl_seconds,
        )
        disabled = config.runtime.kill_switch or environment_kill_switch() or config.adapter.kind == "disabled"
        if once or disabled:
            return _idle(once)
        provider = create_provider(config)
        service = BotService(config, pack, provider, store)
        if config.adapter.kind == "slack":
            adapter = SlackAdapter(
                service,
                config.adapter,
                bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
                app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            )
        elif config.adapter.kind == "discord":
            adapter = DiscordAdapter(
                service,
                config.adapter,
                bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
            )
        else:  # pragma: no cover - disabled adapters return before construction
            raise AssertionError("unsupported platform adapter")
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        previous_sigint = signal.getsignal(signal.SIGINT)

        def stop_adapter(_signum, _frame):
            adapter.signal_stop()

        signal.signal(signal.SIGTERM, stop_adapter)
        signal.signal(signal.SIGINT, stop_adapter)
        try:
            adapter.run()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
        return 0
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _purge(config: AppConfig) -> int:
    store = create_store(config)
    try:
        store.purge(
            time.time(),
            config.store.conversation_ttl_seconds,
            config.store.event_ttl_seconds,
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    print("OK state purged")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        _logging(config)
        if args.command == "verify":
            return _verify(config)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "ask":
            return _ask(config, args.allow_live)
        if args.command == "run":
            return _run(config, args.once)
        if args.command == "purge":
            return _purge(config)
        raise AssertionError("unreachable command")
    except (ConfigurationError, PackValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ReadingPackBotError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
