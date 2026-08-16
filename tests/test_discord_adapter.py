from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reading_pack_bot.adapters.discord import (
    DiscordAdapter,
    split_bot_invocation,
    strip_bot_mention,
)
from reading_pack_bot.config import AdapterConfig
from reading_pack_bot.errors import ConfigurationError
from reading_pack_bot.models import BotReply
from reading_pack_bot.policy import MessagePolicy


class FakeAllowedMentions:
    value = object()

    @classmethod
    def none(cls):
        return cls.value


class FakeIntents:
    def __init__(self):
        self.guilds = False
        self.guild_messages = False
        self.message_content = True

    @classmethod
    def none(cls):
        return cls()


class FakeDiscord:
    AllowedMentions = FakeAllowedMentions
    Intents = FakeIntents


class Identity:
    def __init__(self, identifier, *, bot=False):
        self.id = identifier
        self.bot = bot


class FakeRole(Identity):
    def __init__(self, identifier, *, bot_id=None):
        super().__init__(identifier)
        self.tags = SimpleNamespace(bot_id=bot_id) if bot_id is not None else None


class TypingContext:
    def __init__(self, channel):
        self.channel = channel

    async def __aenter__(self):
        self.channel.typing_started += 1
        return self

    async def __aexit__(self, *_args):
        self.channel.typing_stopped += 1


class FakeChannel(Identity):
    def __init__(self, identifier, *, parent=None):
        super().__init__(identifier)
        self.parent = parent
        self.posts = []
        self.typing_started = 0
        self.typing_stopped = 0

    async def send(self, text, **parameters):
        self.posts.append((text, parameters))

    def typing(self):
        return TypingContext(self)


class FakeMessage(Identity):
    def __init__(
        self,
        identifier="E1",
        *,
        guild_id="G1",
        channel=None,
        author=None,
        content="<@42> question",
        mentions=None,
        role_mentions=None,
        webhook_id=None,
    ):
        super().__init__(identifier)
        self.guild = Identity(guild_id) if guild_id is not None else None
        self.channel = channel or FakeChannel("C1")
        self.author = author or Identity("U1")
        self.content = content
        self.mentions = [Identity("42")] if mentions is None else mentions
        self.role_mentions = [] if role_mentions is None else role_mentions
        self.webhook_id = webhook_id


class FakeClient:
    def __init__(self, *, user_id="42"):
        self.user = Identity(user_id)
        self.closed = False

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


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


def adapter_config(**changes):
    values = {
        "kind": "discord",
        "allowed_installations": ("G1",),
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


class DiscordAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self, service=None, **changes):
        return DiscordAdapter(
            service or FakeService(),
            adapter_config(**changes),
            bot_token="token-placeholder",
            discord_module=FakeDiscord,
        )

    async def test_strip_exact_bot_mention(self):
        self.assertEqual(
            strip_bot_mention("<@!42> hello <@7>", "42"),
            "hello <@7>",
        )

    async def test_split_invocation_supports_context_trailing_and_bare_mentions(self):
        self.assertEqual(
            split_bot_invocation("前置き\n<@42> 比較して", "42"),
            ("前置き", "比較して"),
        )
        self.assertEqual(
            split_bot_invocation("質問です <@!42>", "42"),
            ("", "質問です"),
        )
        self.assertEqual(split_bot_invocation("<@42>", "42"), ("", "help"))

    async def test_listener_enqueues_platform_neutral_message(self):
        service = FakeService()
        adapter = self.make_adapter(service)
        await adapter.receive_message(FakeMessage(), FakeClient())
        self.assertEqual(service.messages, [])
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.platform, "discord")
        self.assertEqual(job.message.installation_id, "G1")
        self.assertEqual(job.message.channel_id, "C1")
        self.assertEqual(job.message.thread_id, "C1")
        self.assertEqual(job.message.actor_id, "U1")
        self.assertEqual(job.message.text, "question")
        self.assertEqual(job.message.inline_context, "")
        adapter._jobs.task_done()

    async def test_listener_keeps_text_before_mention_as_inline_context(self):
        adapter = self.make_adapter()
        source = FakeMessage(content="前置きです。\n<@42> 比較してください")
        await adapter.receive_message(source, FakeClient())
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.inline_context, "前置きです。")
        self.assertEqual(job.message.text, "比較してください")
        adapter._jobs.task_done()

    async def test_parent_allowlist_accepts_discord_thread_as_conversation(self):
        parent = FakeChannel("C1")
        thread = FakeChannel("THREAD1", parent=parent)
        adapter = self.make_adapter()
        await adapter.receive_message(FakeMessage(channel=thread), FakeClient())
        job = adapter._jobs.get_nowait()
        self.assertEqual(job.message.channel_id, "C1")
        self.assertEqual(job.message.thread_id, "THREAD1")
        policy = MessagePolicy(
            SimpleNamespace(kill_switch=False),
            adapter.config,
            SimpleNamespace(max_question_characters=4000),
        )
        self.assertTrue(policy.check(job.message).allowed)
        adapter._jobs.task_done()

    async def test_unmentioned_automated_direct_and_disallowed_messages_are_ignored(self):
        cases = (
            FakeMessage(mentions=[]),
            FakeMessage(author=Identity("U1", bot=True)),
            FakeMessage(webhook_id="WEBHOOK1"),
            FakeMessage(guild_id=None),
            FakeMessage(guild_id="G2"),
            FakeMessage(channel=FakeChannel("C2")),
        )
        for source in cases:
            with self.subTest(source=source):
                adapter = self.make_adapter()
                await adapter.receive_message(source, FakeClient())
                self.assertTrue(adapter._jobs.empty())

    async def test_bot_managed_role_mention_gets_guidance_without_model_call(self):
        service = FakeService()
        adapter = self.make_adapter(service)
        source = FakeMessage(
            content="",
            mentions=[],
            role_mentions=[FakeRole("R1", bot_id="42")],
        )

        await adapter.receive_message(source, FakeClient())

        self.assertTrue(adapter._jobs.empty())
        self.assertEqual(service.messages, [])
        self.assertEqual(len(source.channel.posts), 1)
        text, parameters = source.channel.posts[0]
        self.assertIn("Botの権限ロール", text)
        self.assertIn("Botユーザー", text)
        self.assertIs(parameters["allowed_mentions"], FakeAllowedMentions.value)
        self.assertIs(parameters["reference"], source)

    async def test_unrelated_role_mention_is_ignored(self):
        adapter = self.make_adapter()
        source = FakeMessage(
            content="",
            mentions=[],
            role_mentions=[FakeRole("R1", bot_id="99")],
        )

        await adapter.receive_message(source, FakeClient())

        self.assertTrue(adapter._jobs.empty())
        self.assertEqual(source.channel.posts, [])

    async def test_post_is_safe_and_splits_long_messages(self):
        adapter = self.make_adapter()
        source = FakeMessage()
        await adapter._post(source, "a" * 1200)
        self.assertEqual([len(text) for text, _ in source.channel.posts], [500, 500, 200])
        first_parameters = source.channel.posts[0][1]
        self.assertIs(first_parameters["allowed_mentions"], FakeAllowedMentions.value)
        self.assertIs(first_parameters["reference"], source)
        self.assertFalse(first_parameters["mention_author"])
        self.assertTrue(first_parameters["suppress_embeds"])
        self.assertNotIn("reference", source.channel.posts[1][1])

    async def test_generation_status_uses_typing_context(self):
        adapter = self.make_adapter(show_generation_status=True)
        source = FakeMessage()
        stopped = asyncio.Event()
        typing = asyncio.create_task(
            adapter._typing_until_stopped(source.channel, stopped)
        )
        await asyncio.sleep(0)
        stopped.set()
        await typing
        self.assertEqual(source.channel.typing_started, 1)
        self.assertEqual(source.channel.typing_stopped, 1)

    async def test_service_bridge_runs_the_synchronous_service(self):
        class EagerThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        service = FakeService()
        adapter = self.make_adapter(service)
        message = FakeMessage()
        await adapter.receive_message(message, FakeClient())
        job = adapter._jobs.get_nowait()
        adapter._jobs.task_done()
        delivered = []
        started = []
        with patch("reading_pack_bot.adapters.discord.threading.Thread", EagerThread):
            await adapter._call_service(
                job.message,
                deliver=delivered.append,
                generation_started=lambda: started.append(True),
            )
        self.assertEqual(len(service.messages), 1)
        self.assertEqual(delivered, ["answer"])
        self.assertEqual(started, [True])

    async def test_busy_conversation_is_rejected_before_queueing(self):
        adapter = self.make_adapter()
        first = FakeMessage("E1")
        second = FakeMessage("E2")
        await adapter.receive_message(first, FakeClient())
        await adapter.receive_message(second, FakeClient())
        self.assertEqual(adapter._jobs.qsize(), 1)
        self.assertEqual(len(second.channel.posts), 1)
        self.assertIn("混み合っています", second.channel.posts[0][0])
        job = adapter._jobs.get_nowait()
        adapter._pending_turns.discard(job.turn_key)
        adapter._jobs.task_done()

    async def test_queue_full_rejects_a_different_allowed_channel(self):
        adapter = self.make_adapter(
            allowed_channels=("C1", "C2"),
            queue_size=1,
        )
        first = FakeMessage("E1", channel=FakeChannel("C1"))
        second = FakeMessage("E2", channel=FakeChannel("C2"))
        await adapter.receive_message(first, FakeClient())
        await adapter.receive_message(second, FakeClient())
        self.assertEqual(adapter._jobs.qsize(), 1)
        self.assertEqual(len(second.channel.posts), 1)
        job = adapter._jobs.get_nowait()
        adapter._pending_turns.discard(job.turn_key)
        adapter._jobs.task_done()

    async def test_worker_error_is_redacted_and_returns_an_error_code(self):
        adapter = self.make_adapter()

        async def fail_service(*_args, **_kwargs):
            raise RuntimeError("SECRET G1 C1")

        adapter._call_service = fail_service
        client = FakeClient()
        source = FakeMessage(content="<@42> PRIVATE")
        adapter._start_workers()
        with self.assertLogs("reading_pack_bot.adapters.discord", level="ERROR") as logs:
            await adapter.receive_message(source, client)
            await adapter._jobs.join()
        await adapter._shutdown(client)
        combined = "\n".join(logs.output)
        for private_value in ("SECRET", "PRIVATE", "G1", "C1"):
            self.assertNotIn(private_value, combined)
        self.assertIn("一時的な処理エラー", source.channel.posts[0][0])

    async def test_token_is_required(self):
        with self.assertRaisesRegex(ValueError, "token"):
            DiscordAdapter(
                FakeService(),
                adapter_config(),
                bot_token="",
                discord_module=FakeDiscord,
            )


class LifecycleClient(FakeClient):
    latest = None

    def __init__(self, **parameters):
        super().__init__()
        self.parameters = parameters
        self.events = {}
        self.started_with = None
        type(self).latest = self

    def event(self, listener):
        self.events[listener.__name__] = listener
        return listener

    async def start(self, token, *, reconnect):
        self.started_with = (token, reconnect)


class DiscordRunTests(unittest.TestCase):
    def make_adapter(self, *, client_factory=LifecycleClient):
        return DiscordAdapter(
            FakeService(),
            adapter_config(),
            bot_token="token-placeholder",
            client_factory=client_factory,
            discord_module=FakeDiscord,
        )

    def test_run_uses_only_non_privileged_message_intents(self):
        adapter = self.make_adapter()
        adapter.run()
        client = LifecycleClient.latest
        intents = client.parameters["intents"]
        self.assertTrue(intents.guilds)
        self.assertTrue(intents.guild_messages)
        self.assertFalse(intents.message_content)
        self.assertIs(
            client.parameters["allowed_mentions"], FakeAllowedMentions.value
        )
        self.assertIsNone(client.parameters["max_messages"])
        self.assertEqual(client.started_with, ("token-placeholder", True))
        self.assertEqual(set(client.events), {"on_message", "on_ready"})
        self.assertTrue(client.closed)

    def test_initialization_error_is_redacted(self):
        def fail_client(**_parameters):
            raise RuntimeError("SECRET token-placeholder")

        with self.assertRaisesRegex(
            ConfigurationError,
            r"cannot initialize Discord adapter \(RuntimeError\)",
        ) as captured:
            self.make_adapter(client_factory=fail_client).run()
        self.assertNotIn("SECRET", str(captured.exception))

    def test_connection_error_is_redacted(self):
        class FailingClient(LifecycleClient):
            async def start(self, _token, *, reconnect):
                raise RuntimeError("SECRET token-placeholder")

        with self.assertRaisesRegex(
            ConfigurationError,
            r"cannot connect Discord adapter \(RuntimeError\)",
        ) as captured:
            self.make_adapter(client_factory=FailingClient).run()
        self.assertNotIn("SECRET", str(captured.exception))
