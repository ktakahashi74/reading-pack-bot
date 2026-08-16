"""Slack Socket Mode adapter.

The inbound listener performs only validation and bounded enqueueing. Model
generation and posting run on worker threads, outside Slack's acknowledgement
path.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import (
    SERVICE_SHUTDOWN_MARGIN_SECONDS,
    SERVICE_TIMEOUT_STOP_SECONDS,
    AdapterConfig,
)
from ..errors import ConfigurationError
from ..models import IncomingMessage
from ..service import BotService

LOGGER = logging.getLogger(__name__)
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_SPECIAL_MENTION_RE = re.compile(
    r"<!(?:channel|here|everyone)(?:\|[^>\r\n]*)?>"
    r"|<!subteam\^[^>\r\n]+>"
    r"|<@[A-Z0-9]+(?:\|[^>\r\n]*)?>"
)
_GENERATION_STATUS = "が回答を作成しています…"


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        pattern = re.compile(rf"<@{re.escape(bot_user_id)}>")
        return pattern.sub("", text, count=1).strip()
    return _MENTION_RE.sub("", text, count=1).strip()


def split_message(text: str, limit: int) -> tuple[str, ...]:
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


def neutralize_mentions(text: str) -> str:
    # The replacement must not expand the answer: non-local shutdown bounds
    # rely on a validated one-message maximum after mention neutralization.
    return _SPECIAL_MENTION_RE.sub("[x]", text)


@dataclass(frozen=True)
class _Job:
    client: Any
    message: IncomingMessage
    enqueued_at: float
    turn_key: tuple[str, str, str]
    turn_token: object


@dataclass(frozen=True)
class _Notice:
    client: Any
    channel_id: str
    thread_id: str


class SlackAdapter:
    def __init__(
        self,
        service: BotService,
        config: AdapterConfig,
        *,
        bot_token: str,
        app_token: str,
        app_factory: Callable[..., Any] | None = None,
        handler_factory: Callable[..., Any] | None = None,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        if not bot_token or not app_token:
            raise ValueError("Slack bot and app tokens are required")
        self.service = service
        self.config = config
        self.bot_token = bot_token
        self.app_token = app_token
        self._app_factory = app_factory
        self._handler_factory = handler_factory
        self._clock = clock
        self._sleeper = sleeper
        self._jobs: queue.Queue[_Job | None] = queue.Queue(maxsize=config.queue_size)
        self._notices: queue.Queue[_Notice | None] = queue.Queue(maxsize=1)
        self._workers: list[threading.Thread] = []
        self._notice_workers: list[threading.Thread] = []
        self._post_lock = threading.Lock()
        self._last_post_by_channel: dict[str, float] = {}
        self._closed = False
        self._accepting = True
        self._intake_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._active_generations = 0
        self._turn_condition = threading.Condition()
        self._turn_waiters: dict[tuple[str, str, str], deque[object]] = {}
        self._active_turns: set[tuple[str, str, str]] = set()
        self._close_lock = threading.Lock()
        self._handler_lock = threading.Lock()
        self._handler: Any | None = None
        self._stop_event = threading.Event()
        self._signal_stop_requested = False

    def receive_mention(
        self,
        body: dict[str, Any],
        event: dict[str, Any],
        client: Any,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        installation_id = str(body.get("team_id") or event.get("team") or "")
        channel_id = str(event.get("channel") or "")
        installation_allowed = installation_id in self.config.allowed_installations
        channel_allowed = self.config.allows_channel(channel_id)
        if not installation_allowed or not channel_allowed:
            LOGGER.info(
                "slack_event_ignored reason=route_not_allowed "
                "installation_allowed=%s channel_allowed=%s",
                installation_allowed,
                channel_allowed,
            )
            return
        event_id = str(body.get("event_id") or "")
        event_ts = str(event.get("ts") or "")
        thread_id = str(event.get("thread_ts") or event_ts)
        actor_id = str(event.get("user") or "")
        if (
            not channel_id
            or not event_id
            or not event_ts
            or not thread_id
            or not actor_id
        ):
            LOGGER.warning("slack_event_ignored reason=missing_identity")
            return
        if event.get("bot_id") or event.get("subtype"):
            LOGGER.info("slack_event_ignored reason=automated_message")
            return
        bot_user_id = str((context or {}).get("bot_user_id") or "") or None
        question = strip_bot_mention(str(event.get("text") or ""), bot_user_id)
        message = IncomingMessage(
            event_id=event_id,
            platform="slack",
            installation_id=installation_id,
            channel_id=channel_id,
            thread_id=thread_id,
            actor_id=actor_id,
            text=question,
            triggered=True,
            automated=False,
        )
        turn_key = (installation_id, channel_id, thread_id)
        job = _Job(
            client=client,
            message=message,
            enqueued_at=self._clock(),
            turn_key=turn_key,
            turn_token=object(),
        )
        with self._intake_lock:
            if not self._accepting:
                LOGGER.info("slack_event_ignored reason=adapter_stopping")
                return
            if not self._register_turn(job):
                self._enqueue_overload_notice(
                    client,
                    channel_id,
                    thread_id,
                    reason="thread_busy",
                )
                return
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                self._cancel_turn(job)
                self._enqueue_overload_notice(
                    client,
                    channel_id,
                    thread_id,
                    reason="queue_full",
                )

    def _register_turn(self, job: _Job) -> bool:
        with self._turn_condition:
            if (
                job.turn_key in self._active_turns
                or self._turn_waiters.get(job.turn_key)
            ):
                return False
            self._turn_waiters.setdefault(job.turn_key, deque()).append(
                job.turn_token
            )
            return True

    def _enqueue_overload_notice(
        self,
        client: Any,
        channel_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> None:
        LOGGER.warning("slack_job_rejected reason=%s", reason)
        try:
            self._notices.put_nowait(
                _Notice(client=client, channel_id=channel_id, thread_id=thread_id)
            )
        except queue.Full:
            LOGGER.error("slack_overload_notice_dropped")

    def _remove_waiter_locked(self, job: _Job) -> None:
        waiters = self._turn_waiters.get(job.turn_key)
        if waiters is None:
            return
        try:
            waiters.remove(job.turn_token)
        except ValueError:
            return
        if not waiters:
            del self._turn_waiters[job.turn_key]

    def _cancel_turn(self, job: _Job) -> None:
        with self._turn_condition:
            self._remove_waiter_locked(job)
            self._turn_condition.notify_all()

    def _begin_turn(self, job: _Job) -> bool:
        with self._turn_condition:
            while True:
                if self._stop_event.is_set():
                    self._remove_waiter_locked(job)
                    self._turn_condition.notify_all()
                    return False
                waiters = self._turn_waiters.get(job.turn_key)
                if (
                    waiters
                    and waiters[0] is job.turn_token
                    and job.turn_key not in self._active_turns
                ):
                    waiters.popleft()
                    if not waiters:
                        del self._turn_waiters[job.turn_key]
                    self._active_turns.add(job.turn_key)
                    return True
                self._turn_condition.wait(timeout=0.1)

    def _finish_turn(self, job: _Job) -> None:
        with self._turn_condition:
            self._active_turns.discard(job.turn_key)
            self._turn_condition.notify_all()

    def _notice_worker(self) -> None:
        while True:
            notice = self._notices.get()
            try:
                if notice is None:
                    return
                if self._stop_event.is_set():
                    LOGGER.info("slack_overload_notice_dropped reason=adapter_stopping")
                    continue
                self._post(
                    notice.client,
                    notice.channel_id,
                    notice.thread_id,
                    "現在処理が混み合っています。少し待ってから、もう一度メンションしてください。",
                )
            except Exception as exc:  # noqa: BLE001 - isolate arbitrary SDK/client failures
                LOGGER.error("slack_overload_post_failed kind=%s", type(exc).__name__)
            finally:
                self._notices.task_done()

    def _pace_channel(self, channel_id: str) -> None:
        with self._post_lock:
            now = self._clock()
            previous = self._last_post_by_channel.get(channel_id)
            if previous is not None:
                delay = 1.05 - (now - previous)
                if delay > 0:
                    self._sleeper(delay)
            self._last_post_by_channel[channel_id] = self._clock()

    def _post(
        self,
        client: Any,
        channel_id: str,
        thread_id: str,
        text: str,
        *,
        on_posted: Callable[[], None] | None = None,
    ) -> None:
        safe_text = neutralize_mentions(text)
        for chunk in split_message(safe_text, self.config.message_chunk_characters):
            self._pace_channel(channel_id)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_id,
                markdown_text=chunk,
                reply_broadcast=False,
                link_names=False,
                unfurl_links=False,
                unfurl_media=False,
            )
            if on_posted is not None:
                on_posted()

    @staticmethod
    def _set_generation_status(
        client: Any,
        channel_id: str,
        thread_id: str,
        status: str,
        *,
        action: str,
    ) -> bool:
        try:
            client.assistant_threads_setStatus(
                channel_id=channel_id,
                thread_ts=thread_id,
                status=status,
            )
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary SDK/client failures
            LOGGER.warning(
                "slack_generation_status_failed action=%s kind=%s",
                action,
                type(exc).__name__,
            )
            return False
        return True

    def _clear_generation_status_if_time(
        self,
        client: Any,
        channel_id: str,
        thread_id: str,
        deadline: float,
    ) -> None:
        if self._clock() + self.config.post_timeout_seconds > deadline:
            LOGGER.info(
                "slack_generation_status_clear_skipped reason=insufficient_time"
            )
            return
        self._set_generation_status(
            client,
            channel_id,
            thread_id,
            "",
            action="clear",
        )

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            status_started = False
            message_posted = False
            generation_active = False
            turn_started = False
            queue_wait_ms = 0
            deadline = self._clock() + (
                SERVICE_TIMEOUT_STOP_SECONDS - SERVICE_SHUTDOWN_MARGIN_SECONDS
            )

            def mark_posted() -> None:
                nonlocal message_posted
                message_posted = True

            def deliver(text: str) -> None:
                if job is None:  # pragma: no cover - closure invariant
                    return
                self._post(
                    job.client,
                    job.message.channel_id,
                    job.message.thread_id,
                    text,
                    on_posted=mark_posted,
                )

            def generation_started() -> None:
                nonlocal generation_active, status_started
                if job is None:
                    return
                if not generation_active:
                    with self._activity_lock:
                        self._active_generations += 1
                        active_generations = self._active_generations
                        generation_active = True
                    LOGGER.info(
                        "slack_generation_started "
                        "queue_wait_ms=%d active_generations=%d",
                        queue_wait_ms,
                        active_generations,
                    )
                if (
                    not self.config.show_generation_status
                    or self._stop_event.is_set()
                ):
                    return
                status_started = self._set_generation_status(
                    job.client,
                    job.message.channel_id,
                    job.message.thread_id,
                    _GENERATION_STATUS,
                    action="start",
                )

            try:
                if job is None:
                    return
                if self._stop_event.is_set():
                    self._cancel_turn(job)
                    LOGGER.info("slack_job_dropped reason=adapter_stopping")
                    continue
                queue_wait_ms = max(
                    0,
                    round((self._clock() - job.enqueued_at) * 1000),
                )
                if not self._begin_turn(job):
                    LOGGER.info("slack_job_dropped reason=adapter_stopping")
                    continue
                turn_started = True
                self.service.handle(
                    job.message,
                    deliver=deliver,
                    generation_started=generation_started,
                )
            except Exception as exc:  # noqa: BLE001 - keep the worker alive across boundary failures
                error_code = uuid.uuid4().hex[:12]
                LOGGER.error("slack_worker_failed code=%s kind=%s", error_code, type(exc).__name__)
                if job is not None:
                    if self._clock() + self.config.post_timeout_seconds > deadline:
                        LOGGER.info(
                            "slack_error_post_skipped reason=insufficient_time"
                        )
                    else:
                        try:
                            self._post(
                                job.client,
                                job.message.channel_id,
                                job.message.thread_id,
                                f"一時的な処理エラーが発生しました。 error={error_code}",
                                on_posted=mark_posted,
                            )
                        except Exception as post_exc:  # noqa: BLE001 - last-resort platform error path
                            LOGGER.error("slack_error_post_failed kind=%s", type(post_exc).__name__)
            finally:
                if job is not None and status_started and not message_posted:
                    self._clear_generation_status_if_time(
                        job.client,
                        job.message.channel_id,
                        job.message.thread_id,
                        deadline,
                    )
                if generation_active:
                    with self._activity_lock:
                        self._active_generations -= 1
                if job is not None and turn_started:
                    self._finish_turn(job)
                self._jobs.task_done()

    def _start_workers(self) -> bool:
        # Serialize the closed-state check and startup with close(). Otherwise
        # close could finish just before these daemon threads are created.
        with self._close_lock:
            if self._closed or self._stop_event.is_set():
                return False
            if self._workers or self._notice_workers:
                return True
            for index in range(self.config.max_concurrent_generations):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"reading-pack-bot-worker-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            notice_worker = threading.Thread(
                target=self._notice_worker,
                name="reading-pack-bot-overload-notice",
                daemon=True,
            )
            notice_worker.start()
            self._notice_workers.append(notice_worker)
            return True

    def request_stop(self) -> None:
        with self._intake_lock:
            self._accepting = False
        self._stop_event.set()
        with self._turn_condition:
            self._turn_condition.notify_all()
        with self._handler_lock:
            handler = self._handler
            self._handler = None
        if handler is not None:
            closer = getattr(handler, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - SDK close must not block shutdown
                    LOGGER.error("slack_handler_close_failed kind=%s", type(exc).__name__)

    def signal_stop(self) -> None:
        """Request stop from a Python signal callback without locks or SDK calls."""

        self._signal_stop_requested = True

    def run(self) -> None:
        try:
            if self._app_factory is None or self._handler_factory is None:
                from slack_bolt import App
                from slack_bolt.adapter.socket_mode import SocketModeHandler
                from slack_sdk import WebClient

                app_factory = App
                handler_factory = SocketModeHandler
                web_client_factory = WebClient
            else:
                app_factory = self._app_factory
                handler_factory = self._handler_factory
                web_client_factory = None
        except ImportError:
            raise ConfigurationError("install reading-pack-bot[slack]") from None
        try:
            if web_client_factory is None:
                app = app_factory(token=self.bot_token)
            else:
                web_client = web_client_factory(
                    token=self.bot_token,
                    timeout=self.config.post_timeout_seconds,
                    retry_handlers=[],
                )
                app = app_factory(client=web_client)
            app.event("app_mention")(self.receive_mention)
            handler = handler_factory(app, self.app_token)
        except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK initialization failures
            raise ConfigurationError(
                f"cannot initialize Slack adapter ({type(exc).__name__})"
            ) from None
        with self._handler_lock:
            self._handler = handler
        with self._intake_lock:
            accepting = self._accepting
        if not accepting or self._signal_stop_requested:
            self.request_stop()
            return
        try:
            if not self._start_workers():
                return
            # SocketModeHandler.start() blocks on an internal anonymous Event
            # that close() does not release. Own the lifecycle so SIGTERM can
            # stop intake, wake this wait, drain work, and close the store.
            with self._handler_lock:
                should_connect = (
                    self._handler is handler
                    and not self._stop_event.is_set()
                    and not self._signal_stop_requested
                )
                if should_connect:
                    try:
                        handler.connect()
                    except Exception as exc:  # noqa: BLE001 - redact arbitrary SDK connection failures
                        raise ConfigurationError(
                            f"cannot connect Slack adapter ({type(exc).__name__})"
                        ) from None
            if not should_connect:
                self.request_stop()
                return
            while not self._stop_event.wait(timeout=0.1):
                if self._signal_stop_requested:
                    self.request_stop()
        finally:
            self.close()

    def _discard_pending(self, items: queue.Queue[Any]) -> int:
        discarded = 0
        while True:
            try:
                item = items.get_nowait()
            except queue.Empty:
                return discarded
            else:
                if item is not None:
                    discarded += 1
                    if isinstance(item, _Job):
                        self._cancel_turn(item)
                items.task_done()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.request_stop()
            dropped_jobs = self._discard_pending(self._jobs)
            dropped_notices = self._discard_pending(self._notices)
            if dropped_jobs or dropped_notices:
                LOGGER.warning(
                    "slack_shutdown_dropped jobs=%d notices=%d",
                    dropped_jobs,
                    dropped_notices,
                )
            # Accepted but not-yet-running work is dropped on shutdown. The
            # active requests have bounded provider/post timeouts; the store
            # closes after it finishes and every worker exits.
            if self._workers:
                self._jobs.join()
            else:
                while True:
                    try:
                        self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._jobs.task_done()
            if self._notice_workers:
                self._notices.join()
            else:
                while True:
                    try:
                        self._notices.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._notices.task_done()
            for _ in self._workers:
                self._jobs.put(None)
            for _ in self._notice_workers:
                self._notices.put(None)
            for worker in self._workers:
                worker.join()
            for worker in self._notice_workers:
                worker.join()
            self._closed = True
