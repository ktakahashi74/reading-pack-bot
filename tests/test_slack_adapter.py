from __future__ import annotations

import inspect
import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import unittest

from reading_pack_bot.adapters.slack import (
    SlackAdapter,
    neutralize_mentions,
    split_message,
    strip_bot_mention,
)
from reading_pack_bot.config import AdapterConfig
from reading_pack_bot.errors import ConfigurationError
from reading_pack_bot.models import BotReply


class FakeService:
    def __init__(self, reply="answer"):
        self.messages = []
        self.reply = reply

    def handle(self, message, *, deliver=None, generation_started=None):
        self.messages.append(message)
        if generation_started is not None:
            generation_started()
        reply = BotReply(handled=True, text=self.reply)
        if deliver is not None:
            deliver(reply.text)
        return reply


class FakeClient:
    def __init__(self):
        self.posts = []
        self.statuses = []
        self.events = []
        self.retry_handlers = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        self.events.append(("post", kwargs))
        return {"ok": True}

    def assistant_threads_setStatus(self, **kwargs):
        self.statuses.append(kwargs)
        self.events.append(("status", kwargs))
        return {"ok": True}


class FakeApp:
    def __init__(self, token):
        self.token = token
        self.client = FakeClient()
        self.listeners = {}

    def event(self, name):
        def register(listener):
            self.listeners[name] = listener
            return listener
        return register


class BlockingHandler:
    def __init__(self, app, token):
        self.app = app
        self.token = token
        self.connected = threading.Event()
        self.closed = threading.Event()

    def connect(self):
        self.connected.set()

    def close(self):
        self.closed.set()


def adapter_config(**changes):
    values = {
        "kind": "slack",
        "allowed_installations": ("T1",),
        "channel_policy": "allowlist",
        "allowed_channels": ("C1",),
        "message_chunk_characters": 500,
        "queue_size": 4,
        "max_concurrent_generations": 1,
        "post_timeout_seconds": 10,
        "show_generation_status": False,
    }
    values.update(changes)
    return AdapterConfig(**values)


class SlackAdapterTests(unittest.TestCase):
    def make_adapter(self, service=None, **changes):
        return SlackAdapter(
            service or FakeService(),
            adapter_config(**changes),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            sleeper=lambda _delay: None,
        )

    def body(self, **event_changes):
        event = {
            "channel": "C1",
            "user": "U1",
            "text": "<@UBOT> question",
            "ts": "100.1",
        }
        event.update(event_changes)
        return {"team_id": "T1", "event_id": "Ev1"}, event

    def test_split_message_is_bounded_and_loss_aware(self):
        text = "a" * 300 + "\n  " + "b" * 300 + "  \n\t" + "c" * 300
        chunks = split_message(text, 500)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_strip_exact_bot_mention(self):
        self.assertEqual(strip_bot_mention("<@UBOT> hello <@U2>", "UBOT"), "hello <@U2>")

    def test_mentions_are_neutralized_in_generated_output(self):
        self.assertEqual(
            neutralize_mentions(
                "hello <@U2> <@U2|bob> <!here> <!channel|@channel> "
                "<!everyone|everyone> <!subteam^S1>"
            ),
            "hello [x] [x] [x] [x] [x] [x]",
        )

    def test_markdown_is_posted_for_slack_rendering_after_mention_neutralization(self):
        adapter = self.make_adapter()
        client = FakeClient()
        adapter._post(client, "C1", "TH1", "## Heading\n\n**bold** <@U2> <!here>")
        self.assertEqual(
            client.posts,
            [
                {
                    "channel": "C1",
                    "thread_ts": "TH1",
                    "markdown_text": "## Heading\n\n**bold** [x] [x]",
                    "reply_broadcast": False,
                    "link_names": False,
                    "unfurl_links": False,
                    "unfurl_media": False,
                }
            ],
        )

    def test_listener_enqueues_without_calling_service(self):
        service = FakeService()
        adapter = self.make_adapter(service)
        client = FakeClient()
        body, event = self.body()
        adapter.receive_mention(body=body, event=event, client=client, context={"bot_user_id": "UBOT"})
        self.assertEqual(service.messages, [])
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.text, "question")
        self.assertEqual(job.message.thread_id, "100.1")
        adapter._jobs.task_done()

    def test_listener_arguments_are_discoverable_by_slack_bolt(self):
        parameters = inspect.getfullargspec(self.make_adapter().receive_mention)
        self.assertEqual(parameters.kwonlyargs, [])
        self.assertTrue({"body", "event", "client", "context"} <= set(parameters.args))

    def test_nested_reply_uses_parent_thread(self):
        adapter = self.make_adapter()
        body, event = self.body(thread_ts="90.0")
        adapter.receive_mention(body=body, event=event, client=FakeClient(), context={"bot_user_id": "UBOT"})
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.thread_id, "90.0")
        adapter._jobs.task_done()

    def test_disallowed_workspace_and_channel_do_not_enqueue(self):
        adapter = self.make_adapter()
        body, event = self.body()
        body["team_id"] = "T2"
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as workspace_logs:
            adapter.receive_mention(body=body, event=event, client=FakeClient())
        self.assertIn(
            "installation_allowed=False channel_allowed=True",
            workspace_logs.output[0],
        )
        body["team_id"] = "T1"
        event["channel"] = "C2"
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as channel_logs:
            adapter.receive_mention(body=body, event=event, client=FakeClient())
        self.assertIn(
            "installation_allowed=True channel_allowed=False",
            channel_logs.output[0],
        )
        with self.assertRaises(queue.Empty):
            adapter._jobs.get_nowait()

    def test_joined_policy_accepts_any_delivered_channel_in_allowed_workspace(self):
        adapter = self.make_adapter(channel_policy="joined", allowed_channels=())
        body, event = self.body(channel="C2")
        adapter.receive_mention(body=body, event=event, client=FakeClient())
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.channel_id, "C2")
        adapter._jobs.task_done()

    def test_joined_policy_still_rejects_missing_channel(self):
        adapter = self.make_adapter(channel_policy="joined", allowed_channels=())
        body, event = self.body(channel="")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as captured:
            adapter.receive_mention(body=body, event=event, client=FakeClient())
        self.assertIn("channel_allowed=False", "\n".join(captured.output))
        with self.assertRaises(queue.Empty):
            adapter._jobs.get_nowait()

    def test_bot_and_subtype_are_ignored(self):
        adapter = self.make_adapter()
        body, event = self.body(bot_id="B1")
        adapter.receive_mention(body=body, event=event, client=FakeClient())
        body["event_id"] = "Ev2"
        event.pop("bot_id")
        event["subtype"] = "message_changed"
        adapter.receive_mention(body=body, event=event, client=FakeClient())
        with self.assertRaises(queue.Empty):
            adapter._jobs.get_nowait()

    def test_worker_posts_to_original_thread(self):
        service = FakeService("answer")
        adapter = self.make_adapter(service)
        client = FakeClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        adapter.receive_mention(body=body, event=event, client=client, context={"bot_user_id": "UBOT"})
        adapter._jobs.join()
        adapter.close()
        self.assertEqual(len(service.messages), 1)
        self.assertEqual(client.posts, [{
            "channel": "C1",
            "thread_ts": "90.0",
            "markdown_text": "answer",
            "reply_broadcast": False,
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }])
        self.assertEqual(client.statuses, [])

    def test_multiple_workers_process_different_threads_concurrently(self):
        class ConcurrentService(FakeService):
            def __init__(self):
                super().__init__()
                self.lock = threading.Lock()
                self.release = threading.Event()
                self.two_active = threading.Event()
                self.active = 0
                self.maximum_active = 0

            def handle(self, message, *, deliver=None, generation_started=None):
                with self.lock:
                    self.messages.append(message)
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                    if self.active == 2:
                        self.two_active.set()
                try:
                    if not self.release.wait(timeout=2):
                        raise AssertionError("concurrent requests were not released")
                    if deliver is not None:
                        deliver(self.reply)
                    return BotReply(handled=True, text=self.reply)
                finally:
                    with self.lock:
                        self.active -= 1

        service = ConcurrentService()
        adapter = self.make_adapter(
            service,
            queue_size=2,
            max_concurrent_generations=2,
        )
        client = FakeClient()
        self.assertTrue(adapter._start_workers())
        self.assertEqual(len(adapter._workers), 2)
        for index, thread_id in enumerate(("90.0", "91.0"), start=1):
            body, event = self.body(thread_ts=thread_id, ts=f"10{index}.1")
            body["event_id"] = f"Ev{index}"
            adapter.receive_mention(body=body, event=event, client=client)
        self.assertTrue(service.two_active.wait(timeout=2))
        service.release.set()
        adapter._jobs.join()
        adapter.close()
        self.assertEqual(service.maximum_active, 2)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(adapter._active_generations, 0)

    def test_busy_thread_is_rejected_without_blocking_another_thread(self):
        class ConcurrentService(FakeService):
            def __init__(self):
                super().__init__()
                self.first_entered = threading.Event()
                self.other_entered = threading.Event()
                self.release_first = threading.Event()

            def handle(self, message, *, deliver=None, generation_started=None):
                self.messages.append(message)
                if message.text == "first":
                    self.first_entered.set()
                    if not self.release_first.wait(timeout=2):
                        raise AssertionError("first request was not released")
                else:
                    self.other_entered.set()
                if deliver is not None:
                    deliver(message.text)
                return BotReply(handled=True, text=message.text)

        service = ConcurrentService()
        adapter = self.make_adapter(
            service,
            queue_size=2,
            max_concurrent_generations=2,
        )
        client = FakeClient()
        adapter._start_workers()
        body, event = self.body(
            thread_ts="90.0", ts="101.1", text="<@UBOT> first"
        )
        adapter.receive_mention(body=body, event=event, client=client)
        self.assertTrue(service.first_entered.wait(timeout=2))

        body, event = self.body(
            thread_ts="90.0", ts="102.1", text="<@UBOT> second"
        )
        body["event_id"] = "Ev2"
        adapter.receive_mention(body=body, event=event, client=client)

        body, event = self.body(
            thread_ts="91.0", ts="103.1", text="<@UBOT> other"
        )
        body["event_id"] = "Ev3"
        adapter.receive_mention(body=body, event=event, client=client)
        self.assertTrue(service.other_entered.wait(timeout=2))
        service.release_first.set()
        adapter._jobs.join()
        adapter.close()
        self.assertEqual(
            sorted(message.text for message in service.messages),
            ["first", "other"],
        )
        self.assertFalse(adapter._turn_waiters)
        self.assertFalse(adapter._active_turns)

    def test_generation_metrics_exclude_message_content_and_route_ids(self):
        adapter = self.make_adapter()
        client = FakeClient()
        adapter._start_workers()
        body, event = self.body(text="<@UBOT> SECRET question")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as captured:
            adapter.receive_mention(body=body, event=event, client=client)
            adapter._jobs.join()
        adapter.close()
        combined = "\n".join(captured.output)
        self.assertIn("queue_wait_ms=", combined)
        self.assertIn("active_generations=1", combined)
        for private_value in ("SECRET", "question", "C1", "100.1"):
            self.assertNotIn(private_value, combined)

    def test_generation_status_precedes_provider_answer_and_uses_reply_route(self):
        service = FakeService("answer")
        adapter = self.make_adapter(service, show_generation_status=True)
        client = FakeClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        adapter.receive_mention(
            body=body,
            event=event,
            client=client,
            context={"bot_user_id": "UBOT"},
        )
        adapter._jobs.join()
        adapter.close()
        self.assertEqual(
            client.statuses,
            [
                {
                    "channel_id": "C1",
                    "thread_ts": "90.0",
                    "status": "が回答を作成しています…",
                }
            ],
        )
        self.assertEqual([event[0] for event in client.events], ["status", "post"])
        self.assertEqual(client.posts[0]["channel"], client.statuses[0]["channel_id"])
        self.assertEqual(client.posts[0]["thread_ts"], client.statuses[0]["thread_ts"])

    def test_generation_status_failure_is_redacted_and_answer_still_posts(self):
        class FailingStatusClient(FakeClient):
            def assistant_threads_setStatus(self, **_kwargs):
                raise RuntimeError("SECRET C1 90.0 question")

        adapter = self.make_adapter(show_generation_status=True)
        client = FailingStatusClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="WARNING") as captured:
            adapter.receive_mention(
                body=body,
                event=event,
                client=client,
                context={"bot_user_id": "UBOT"},
            )
            adapter._jobs.join()
        adapter.close()
        self.assertEqual([post["markdown_text"] for post in client.posts], ["answer"])
        combined = "\n".join(captured.output)
        for secret in ("SECRET", "C1", "90.0", "question"):
            self.assertNotIn(secret, combined)
        self.assertIn("kind=RuntimeError", combined)

    def test_abnormal_path_clears_status_when_no_message_was_posted(self):
        class FailingService(FakeService):
            def handle(self, message, *, deliver=None, generation_started=None):
                if generation_started is not None:
                    generation_started()
                raise RuntimeError("worker failed")

        class FailingPostClient(FakeClient):
            def chat_postMessage(self, **_kwargs):
                raise RuntimeError("post failed")

        adapter = self.make_adapter(
            FailingService(), show_generation_status=True
        )
        client = FailingPostClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="ERROR"):
            adapter.receive_mention(
                body=body,
                event=event,
                client=client,
                context={"bot_user_id": "UBOT"},
            )
            adapter._jobs.join()
        adapter.close()
        self.assertEqual(
            [status["status"] for status in client.statuses],
            ["が回答を作成しています…", ""],
        )

    def test_worker_error_post_relies_on_slack_automatic_status_clear(self):
        class FailingService(FakeService):
            def handle(self, message, *, deliver=None, generation_started=None):
                if generation_started is not None:
                    generation_started()
                raise RuntimeError("worker failed")

        adapter = self.make_adapter(
            FailingService(), show_generation_status=True
        )
        client = FakeClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="ERROR"):
            adapter.receive_mention(
                body=body,
                event=event,
                client=client,
                context={"bot_user_id": "UBOT"},
            )
            adapter._jobs.join()
        adapter.close()
        self.assertEqual(len(client.posts), 1)
        self.assertIn("処理エラー", client.posts[0]["markdown_text"])
        self.assertEqual(
            [status["status"] for status in client.statuses],
            ["が回答を作成しています…"],
        )

    def test_abnormal_clear_is_skipped_without_shutdown_budget(self):
        class MutableClock:
            value = 100.0

            def __call__(self):
                return self.value

        clock = MutableClock()

        class SlowFailingService(FakeService):
            def handle(self, message, *, deliver=None, generation_started=None):
                if generation_started is not None:
                    generation_started()
                clock.value = 200.0
                raise RuntimeError("worker failed")

        class FailingPostClient(FakeClient):
            attempted_posts = 0

            def chat_postMessage(self, **_kwargs):
                self.attempted_posts += 1
                raise RuntimeError("post failed")

        adapter = SlackAdapter(
            SlowFailingService(),
            adapter_config(show_generation_status=True),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            clock=clock,
            sleeper=lambda _delay: None,
        )
        client = FailingPostClient()
        adapter._start_workers()
        body, event = self.body(thread_ts="90.0")
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as captured:
            adapter.receive_mention(
                body=body,
                event=event,
                client=client,
                context={"bot_user_id": "UBOT"},
            )
            adapter._jobs.join()
        adapter.close()
        self.assertEqual(
            [status["status"] for status in client.statuses],
            ["が回答を作成しています…"],
        )
        self.assertEqual(client.attempted_posts, 0)
        self.assertIn("slack_error_post_skipped", "\n".join(captured.output))
        self.assertIn("insufficient_time", "\n".join(captured.output))

    def test_queue_full_only_enqueues_bounded_async_notice(self):
        service = FakeService("answer")
        adapter = self.make_adapter(
            service, queue_size=1, show_generation_status=True
        )
        client = FakeClient()
        body, event = self.body()
        adapter.receive_mention(body=body, event=event, client=client, context={"bot_user_id": "UBOT"})
        second_body, second_event = self.body(text="<@UBOT> second", ts="101.1")
        second_body["event_id"] = "Ev2"
        adapter.receive_mention(
            body=second_body,
            event=second_event,
            client=client,
            context={"bot_user_id": "UBOT"},
        )
        self.assertEqual(client.posts, [])
        self.assertEqual(service.messages, [])
        self.assertEqual(adapter._notices.qsize(), 1)
        adapter._start_workers()
        adapter._jobs.join()
        adapter._notices.join()
        adapter.close()
        self.assertEqual(len(service.messages), 1)
        self.assertEqual(len(client.statuses), 1)
        self.assertEqual(
            {post["markdown_text"] for post in client.posts},
            {"answer", "現在処理が混み合っています。少し待ってから、もう一度メンションしてください。"},
        )

    def test_stop_and_enqueue_are_atomic(self):
        service = FakeService("answer")
        adapter = self.make_adapter(service)
        client = FakeClient()
        entered = threading.Event()
        release = threading.Event()
        original_put = adapter._jobs.put_nowait

        def delayed_put(item):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("enqueue was not released")
            return original_put(item)

        adapter._jobs.put_nowait = delayed_put
        body, event = self.body()
        receiver = threading.Thread(
            target=adapter.receive_mention,
            kwargs={"body": body, "event": event, "client": client, "context": {"bot_user_id": "UBOT"}},
        )
        closer = threading.Thread(target=adapter.close)
        receiver.start()
        self.assertTrue(entered.wait(timeout=2))
        closer.start()
        release.set()
        receiver.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(receiver.is_alive() or closer.is_alive())
        self.assertEqual(adapter._jobs.unfinished_tasks, 0)
        self.assertEqual(adapter._workers, [])
        self.assertEqual(len(service.messages), 0)

    def test_shutdown_drops_pending_jobs_and_finishes_active_job(self):
        class BlockingService(FakeService):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def handle(self, message, *, deliver=None, generation_started=None):
                self.messages.append(message)
                if generation_started is not None:
                    generation_started()
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("active request was not released")
                if deliver is not None:
                    deliver(self.reply)
                return BotReply(handled=True, text=self.reply)

        service = BlockingService()
        adapter = self.make_adapter(
            service, queue_size=4, show_generation_status=True
        )
        client = FakeClient()
        adapter._start_workers()
        for index in range(3):
            body, event = self.body(ts=f"10{index}.1", text=f"<@UBOT> question {index}")
            body["event_id"] = f"Ev{index}"
            adapter.receive_mention(
                body=body,
                event=event,
                client=client,
                context={"bot_user_id": "UBOT"},
            )
        self.assertTrue(service.entered.wait(timeout=2))
        closer = threading.Thread(target=adapter.close)
        closer.start()
        service.release.set()
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(service.messages), 1)
        self.assertEqual(len(client.statuses), 1)
        self.assertEqual(adapter._jobs.unfinished_tasks, 0)

    def test_shutdown_after_same_thread_busy_rejection(self):
        class BlockingService(FakeService):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def handle(self, message, *, deliver=None, generation_started=None):
                self.messages.append(message)
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("active request was not released")
                if deliver is not None:
                    deliver(self.reply)
                return BotReply(handled=True, text=self.reply)

        service = BlockingService()
        adapter = self.make_adapter(
            service,
            queue_size=1,
            max_concurrent_generations=2,
        )
        client = FakeClient()
        adapter._start_workers()
        for index, text in enumerate(("first", "second"), start=1):
            body, event = self.body(
                thread_ts="90.0",
                ts=f"10{index}.1",
                text=f"<@UBOT> {text}",
            )
            body["event_id"] = f"Ev{index}"
            adapter.receive_mention(body=body, event=event, client=client)
            if text == "first":
                self.assertTrue(service.entered.wait(timeout=2))
        closer = threading.Thread(target=adapter.close)
        closer.start()
        service.release.set()
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertEqual([message.text for message in service.messages], ["first"])
        self.assertEqual(adapter._jobs.unfinished_tasks, 0)
        self.assertFalse(adapter._turn_waiters)
        self.assertFalse(adapter._active_turns)

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "requires POSIX signals")
    def test_signal_stop_is_lock_free_in_real_signal_callback(self):
        script = textwrap.dedent(
            """
            import os
            import signal
            from reading_pack_bot.adapters.slack import SlackAdapter
            from reading_pack_bot.config import AdapterConfig

            config = AdapterConfig(
                kind="slack",
                allowed_installations=("T1",),
                channel_policy="allowlist",
                allowed_channels=("C1",),
                message_chunk_characters=500,
                queue_size=1,
                max_concurrent_generations=1,
                post_timeout_seconds=10,
                show_generation_status=False,
            )
            adapter = SlackAdapter(object(), config, bot_token="bot", app_token="app")
            adapter._intake_lock.acquire()
            signal.signal(signal.SIGUSR1, lambda *_: adapter.signal_stop())
            os.kill(os.getpid(), signal.SIGUSR1)
            assert adapter._signal_stop_requested
            adapter._intake_lock.release()
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env=os.environ.copy(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_adapter_stop_event_unblocks_run_when_handler_close_does_not(self):
        created = threading.Event()
        holder = {}

        def handler_factory(app, token):
            handler = BlockingHandler(app, token)
            holder["handler"] = handler
            created.set()
            return handler

        adapter = SlackAdapter(
            FakeService(),
            adapter_config(),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            app_factory=FakeApp,
            handler_factory=handler_factory,
            sleeper=lambda _delay: None,
        )
        runner = threading.Thread(target=adapter.run)
        runner.start()
        self.assertTrue(created.wait(timeout=2))
        self.assertTrue(holder["handler"].connected.wait(timeout=2))
        holder["handler"].close()
        runner.join(timeout=0.05)
        self.assertTrue(runner.is_alive())
        adapter.request_stop()
        runner.join(timeout=2)
        self.assertFalse(runner.is_alive())
        self.assertTrue(holder["handler"].closed.is_set())
        self.assertTrue(all(not worker.is_alive() for worker in adapter._workers))

    def test_handler_construction_failure_starts_no_workers(self):
        def fail_handler(_app, _token):
            raise RuntimeError("constructor failed")

        adapter = SlackAdapter(
            FakeService(),
            adapter_config(),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            app_factory=FakeApp,
            handler_factory=fail_handler,
            sleeper=lambda _delay: None,
        )
        with self.assertRaisesRegex(ConfigurationError, "RuntimeError"):
            adapter.run()
        self.assertEqual(adapter._workers, [])
        self.assertEqual(adapter._notice_workers, [])

    def test_handler_connect_failure_is_redacted_and_closes_workers(self):
        class FailingHandler(BlockingHandler):
            def connect(self):
                raise RuntimeError("SECRET connection detail")

        adapter = SlackAdapter(
            FakeService(),
            adapter_config(),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            app_factory=FakeApp,
            handler_factory=FailingHandler,
            sleeper=lambda _delay: None,
        )
        with self.assertRaisesRegex(ConfigurationError, r"RuntimeError") as captured:
            adapter.run()
        self.assertNotIn("SECRET", str(captured.exception))
        self.assertTrue(all(not worker.is_alive() for worker in adapter._workers))

    def test_close_between_accepting_check_and_worker_start_leaves_no_workers(self):
        created = threading.Event()
        start_entered = threading.Event()
        release_start = threading.Event()

        def handler_factory(app, token):
            created.set()
            return BlockingHandler(app, token)

        adapter = SlackAdapter(
            FakeService(),
            adapter_config(),
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            app_factory=FakeApp,
            handler_factory=handler_factory,
            sleeper=lambda _delay: None,
        )
        original_start_workers = adapter._start_workers

        def gated_start_workers():
            start_entered.set()
            if not release_start.wait(timeout=2):
                raise AssertionError("worker start was not released")
            return original_start_workers()

        adapter._start_workers = gated_start_workers
        runner = threading.Thread(target=adapter.run)
        runner.start()
        self.assertTrue(created.wait(timeout=2))
        self.assertTrue(start_entered.wait(timeout=2))
        adapter.close()
        release_start.set()
        runner.join(timeout=2)
        self.assertFalse(runner.is_alive())
        self.assertEqual(adapter._workers, [])
        self.assertEqual(adapter._notice_workers, [])

    def test_post_splits_long_output(self):
        adapter = self.make_adapter(message_chunk_characters=500)
        client = FakeClient()
        adapter._post(client, "C1", "TH1", "x" * 1200)
        self.assertEqual(
            [len(post["markdown_text"]) for post in client.posts],
            [500, 500, 200],
        )
        self.assertTrue(all(post["thread_ts"] == "TH1" for post in client.posts))

    def test_tokens_are_required(self):
        with self.assertRaises(ValueError):
            SlackAdapter(FakeService(), adapter_config(), bot_token="", app_token="")


if __name__ == "__main__":
    unittest.main()
