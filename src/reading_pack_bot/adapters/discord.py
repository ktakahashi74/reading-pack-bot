"""Discord Gateway adapter.

Only guild messages that explicitly mention the bot enter the bounded worker
queue. Provider calls run outside the Gateway event handler, while all Discord
SDK calls remain on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import AdapterConfig
from ..errors import ConfigurationError
from ..models import IncomingMessage
from ..service import BotService
from .common import split_message

LOGGER = logging.getLogger(__name__)
_OVERLOAD_TEXT = "現在処理が混み合っています。少し待ってから、もう一度メンションしてください。"
_ROLE_MENTION_GUIDANCE = (
    "これはBotの権限ロールへのメンションです。"
    "候補から「アプリ」または「BOT」と表示されるBotユーザーを選び、"
    "質問を再送してください。"
)


def _bot_mention_pattern(bot_user_id: str) -> re.Pattern[str]:
    return re.compile(rf"<@!?{re.escape(bot_user_id)}>")


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove one exact Discord user mention and preserve other mentions."""

    return _bot_mention_pattern(bot_user_id).sub("", text, count=1).strip()


def split_bot_invocation(text: str, bot_user_id: str) -> tuple[str, str]:
    """Return same-message context and the explicit request around a mention."""

    match = _bot_mention_pattern(bot_user_id).search(text)
    if match is None:
        return "", text.strip()
    before = text[: match.start()].strip()
    after = text[match.end() :].strip()
    if after:
        return before, after
    if before:
        return "", before
    return "", "help"


@dataclass(frozen=True)
class _Job:
    source: Any
    message: IncomingMessage
    enqueued_at: float
    turn_key: tuple[str, str, str]


class DiscordAdapter:
    def __init__(
        self,
        service: BotService,
        config: AdapterConfig,
        *,
        bot_token: str,
        client_factory: Callable[..., Any] | None = None,
        discord_module: Any | None = None,
        clock=time.monotonic,
    ) -> None:
        if not bot_token:
            raise ValueError("Discord bot token is required")
        self.service = service
        self.config = config
        self.bot_token = bot_token
        self._client_factory = client_factory
        self._discord_module = discord_module
        self._clock = clock
        self._jobs: asyncio.Queue[_Job | None] = asyncio.Queue(
            maxsize=config.queue_size
        )
        self._workers: list[asyncio.Task[None]] = []
        self._pending_turns: set[tuple[str, str, str]] = set()
        self._accepting = True
        self._signal_stop_requested = False
        self._closed = False
        self._client: Any | None = None

    @staticmethod
    def _identity(value: Any) -> str:
        identifier = getattr(value, "id", "")
        return str(identifier or "")

    @staticmethod
    def _role_is_managed_by_bot(role: Any, bot_user_id: str) -> bool:
        tags = getattr(role, "tags", None)
        role_bot_id = getattr(tags, "bot_id", "")
        return bool(bot_user_id) and str(role_bot_id or "") == bot_user_id

    async def receive_message(self, source: Any, client: Any | None = None) -> None:
        """Validate and enqueue one discord.py Message without calling a model."""

        client = client or self._client
        guild = getattr(source, "guild", None)
        channel = getattr(source, "channel", None)
        author = getattr(source, "author", None)
        if guild is None:
            LOGGER.info("discord_event_ignored reason=direct_message")
            return
        installation_id = self._identity(guild)
        channel_id = self._identity(channel)
        parent_channel_id = self._identity(getattr(channel, "parent", None))
        route_channel_id = parent_channel_id or channel_id
        installation_allowed = installation_id in self.config.allowed_installations
        channel_allowed = self.config.allows_channel(route_channel_id)
        if not installation_allowed or not channel_allowed:
            LOGGER.info(
                "discord_event_ignored reason=route_not_allowed "
                "installation_allowed=%s channel_allowed=%s",
                installation_allowed,
                channel_allowed,
            )
            return
        if author is None or bool(getattr(author, "bot", False)) or getattr(
            source, "webhook_id", None
        ) is not None:
            LOGGER.info("discord_event_ignored reason=automated_message")
            return
        bot_user_id = self._identity(getattr(client, "user", None))
        mentioned = any(
            self._identity(user) == bot_user_id
            for user in (getattr(source, "mentions", ()) or ())
        )
        if bot_user_id and not mentioned and any(
            self._role_is_managed_by_bot(role, bot_user_id)
            for role in (getattr(source, "role_mentions", ()) or ())
        ):
            try:
                await self._post(source, _ROLE_MENTION_GUIDANCE)
            except Exception as exc:  # noqa: BLE001 - isolate Discord post failures
                LOGGER.error(
                    "discord_role_mention_guidance_failed kind=%s",
                    type(exc).__name__,
                )
            else:
                LOGGER.info("discord_role_mention_guidance_posted")
            return
        if not bot_user_id or not mentioned:
            LOGGER.info("discord_event_ignored reason=not_mentioned")
            return
        event_id = self._identity(source)
        actor_id = self._identity(author)
        if not event_id or not channel_id or not actor_id:
            LOGGER.warning("discord_event_ignored reason=missing_identity")
            return
        inline_context, question = split_bot_invocation(
            str(getattr(source, "content", "")), bot_user_id
        )
        message = IncomingMessage(
            event_id=event_id,
            platform="discord",
            installation_id=installation_id,
            # The policy rechecks the configured parent route. A Discord
            # thread's own channel ID remains the conversation boundary.
            channel_id=route_channel_id,
            thread_id=channel_id,
            actor_id=actor_id,
            text=question,
            triggered=True,
            automated=False,
            inline_context=inline_context,
        )
        turn_key = (installation_id, route_channel_id, channel_id)
        if not self._accepting:
            LOGGER.info("discord_event_ignored reason=adapter_stopping")
            return
        if turn_key in self._pending_turns:
            await self._post_notice(source, reason="thread_busy")
            return
        job = _Job(
            source=source,
            message=message,
            enqueued_at=self._clock(),
            turn_key=turn_key,
        )
        self._pending_turns.add(turn_key)
        try:
            self._jobs.put_nowait(job)
        except asyncio.QueueFull:
            self._pending_turns.discard(turn_key)
            await self._post_notice(source, reason="queue_full")

    async def _post_notice(self, source: Any, *, reason: str) -> None:
        LOGGER.warning("discord_job_rejected reason=%s", reason)
        try:
            await self._post(source, _OVERLOAD_TEXT)
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary SDK failures
            LOGGER.error(
                "discord_overload_post_failed kind=%s", type(exc).__name__
            )

    async def _post(self, source: Any, text: str) -> None:
        discord_module = self._discord_module
        if discord_module is None:  # pragma: no cover - run() always sets this
            raise RuntimeError("Discord SDK is not initialized")
        channel = getattr(source, "channel", None)
        if channel is None:
            raise RuntimeError("Discord message has no channel")
        allowed_mentions = discord_module.AllowedMentions.none()
        for index, chunk in enumerate(
            split_message(text, self.config.message_chunk_characters)
        ):
            parameters: dict[str, Any] = {
                "allowed_mentions": allowed_mentions,
                "mention_author": False,
                "suppress_embeds": True,
            }
            if index == 0:
                parameters["reference"] = source
            await asyncio.wait_for(
                channel.send(chunk, **parameters),
                timeout=self.config.post_timeout_seconds,
            )

    async def _typing_until_stopped(
        self, channel: Any, stopped: asyncio.Event
    ) -> None:
        try:
            async with channel.typing():
                await stopped.wait()
        except Exception as exc:  # noqa: BLE001 - optional UX is non-authoritative
            LOGGER.warning("discord_typing_failed kind=%s", type(exc).__name__)

    async def _call_service(
        self,
        message: IncomingMessage,
        *,
        deliver: Callable[[str], None],
        generation_started: Callable[[], None],
    ) -> None:
        """Run the synchronous service without occupying the Gateway loop."""

        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        def resolve_failure(exc: Exception) -> None:
            if not completed.done():
                completed.set_exception(exc)

        def resolve_success() -> None:
            if not completed.done():
                completed.set_result(None)

        def run_service() -> None:
            try:
                self.service.handle(
                    message,
                    deliver=deliver,
                    generation_started=generation_started,
                )
            except Exception as exc:  # propagate into the adapter worker
                loop.call_soon_threadsafe(resolve_failure, exc)
            else:
                loop.call_soon_threadsafe(resolve_success)

        threading.Thread(
            target=run_service,
            name="reading-pack-bot-discord-service",
            daemon=True,
        ).start()
        await completed

    async def _handle_job(self, job: _Job) -> None:
        loop = asyncio.get_running_loop()
        typing_stopped = asyncio.Event()
        typing_tasks: list[asyncio.Task[None]] = []
        message_posted = False
        queue_wait_ms = max(0, round((self._clock() - job.enqueued_at) * 1000))

        def deliver(text: str) -> None:
            nonlocal message_posted
            future = asyncio.run_coroutine_threadsafe(
                self._post(job.source, text), loop
            )
            try:
                future.result(timeout=self.config.post_timeout_seconds + 1)
            except Exception:
                future.cancel()
                raise
            message_posted = True

        def generation_started() -> None:
            LOGGER.info(
                "discord_generation_started queue_wait_ms=%d", queue_wait_ms
            )
            if not self.config.show_generation_status or self._signal_stop_requested:
                return

            def begin_typing() -> None:
                if typing_tasks:
                    return
                typing_tasks.append(
                    asyncio.create_task(
                        self._typing_until_stopped(
                            getattr(job.source, "channel", None), typing_stopped
                        )
                    )
                )

            loop.call_soon_threadsafe(begin_typing)

        try:
            await self._call_service(
                job.message,
                deliver=deliver,
                generation_started=generation_started,
            )
        except Exception as exc:  # noqa: BLE001 - keep worker alive across SDK failures
            error_code = uuid.uuid4().hex[:12]
            LOGGER.error(
                "discord_worker_failed code=%s kind=%s",
                error_code,
                type(exc).__name__,
            )
            if not message_posted:
                try:
                    await self._post(
                        job.source,
                        f"一時的な処理エラーが発生しました。 error={error_code}",
                    )
                except Exception as post_exc:  # noqa: BLE001 - last-resort platform path
                    LOGGER.error(
                        "discord_error_post_failed kind=%s", type(post_exc).__name__
                    )
        finally:
            typing_stopped.set()
            # Let a generation_started callback queued by the provider thread
            # run before checking whether it created a typing task.
            await asyncio.sleep(0)
            if typing_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*typing_tasks, return_exceptions=True),
                        timeout=self.config.post_timeout_seconds,
                    )
                except TimeoutError:
                    LOGGER.warning("discord_typing_failed kind=TimeoutError")

    async def _worker(self) -> None:
        while True:
            job = await self._jobs.get()
            try:
                if job is None:
                    return
                if self._signal_stop_requested:
                    LOGGER.info("discord_job_dropped reason=adapter_stopping")
                    continue
                await self._handle_job(job)
            finally:
                if job is not None:
                    self._pending_turns.discard(job.turn_key)
                self._jobs.task_done()

    def _start_workers(self) -> None:
        if self._workers:
            return
        for index in range(self.config.max_concurrent_generations):
            self._workers.append(
                asyncio.create_task(
                    self._worker(), name=f"reading-pack-bot-discord-{index + 1}"
                )
            )

    def _discard_pending(self) -> int:
        discarded = 0
        while True:
            try:
                job = self._jobs.get_nowait()
            except asyncio.QueueEmpty:
                return discarded
            if job is not None:
                discarded += 1
                self._pending_turns.discard(job.turn_key)
            self._jobs.task_done()

    async def _shutdown(self, client: Any) -> None:
        if self._closed:
            return
        self._accepting = False
        dropped = self._discard_pending()
        if dropped:
            LOGGER.warning("discord_shutdown_dropped jobs=%d", dropped)
        await self._jobs.join()
        for _ in self._workers:
            self._jobs.put_nowait(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        try:
            is_closed = getattr(client, "is_closed", lambda: False)
            if not is_closed():
                await client.close()
        except Exception as exc:  # noqa: BLE001 - SDK shutdown must not leak detail
            LOGGER.error("discord_client_close_failed kind=%s", type(exc).__name__)
        self._closed = True

    async def _run_async(self, client: Any) -> None:
        if self._signal_stop_requested:
            await self._shutdown(client)
            return
        self._start_workers()
        start_task = asyncio.create_task(client.start(self.bot_token, reconnect=True))
        try:
            while not start_task.done() and not self._signal_stop_requested:
                await asyncio.sleep(0.1)
            if self._signal_stop_requested:
                await self._shutdown(client)
            await start_task
        except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK failures
            raise ConfigurationError(
                f"cannot connect Discord adapter ({type(exc).__name__})"
            ) from None
        finally:
            await self._shutdown(client)

    def signal_stop(self) -> None:
        """Request stop from a Python signal callback without locks or SDK calls."""

        self._signal_stop_requested = True
        self._accepting = False

    def run(self) -> None:
        try:
            if self._discord_module is None:
                import discord as discord_module

                self._discord_module = discord_module
            else:
                discord_module = self._discord_module
            intents = discord_module.Intents.none()
            intents.guilds = True
            intents.guild_messages = True
            intents.message_content = False
            client_factory = self._client_factory or discord_module.Client
            client = client_factory(
                intents=intents,
                allowed_mentions=discord_module.AllowedMentions.none(),
                max_messages=None,
            )

            async def on_message(message: Any) -> None:
                await self.receive_message(message, client)

            async def on_ready() -> None:
                LOGGER.info("service_ready adapter=discord outbound_clients=on")

            client.event(on_message)
            client.event(on_ready)
            self._client = client
        except ImportError:
            raise ConfigurationError("install reading-pack-bot[discord]") from None
        except Exception as exc:  # noqa: BLE001 - redact SDK initialization failures
            raise ConfigurationError(
                f"cannot initialize Discord adapter ({type(exc).__name__})"
            ) from None
        asyncio.run(self._run_async(client))
