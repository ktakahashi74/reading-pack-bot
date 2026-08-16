from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reading_pack_bot.adapters.slack import SlackAdapter
from reading_pack_bot.config import load_config
from reading_pack_bot.models import IncomingMessage
from reading_pack_bot.pack import load_pack
from reading_pack_bot.providers import FakeProvider
from reading_pack_bot.service import BotService
from reading_pack_bot.stores import MemoryStore
from tests.helpers import FIXTURE, FIXTURE_SHA256, config_text


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.statuses: list[dict[str, object]] = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True}

    def assistant_threads_setStatus(self, **kwargs):
        self.statuses.append(kwargs)
        return {"ok": True}


class SlackEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        config_path = Path(self.temporary.name) / "config.toml"
        config_path.write_text(config_text(), encoding="utf-8")
        self.config = load_config(config_path)
        self.pack = load_pack(
            FIXTURE,
            expected_sha256=FIXTURE_SHA256,
            max_bytes=524288,
        )
        self.provider = FakeProvider("## Answer\n\n**bold** <@U999> <!here>")
        self.store = MemoryStore()
        self.service = BotService(
            self.config,
            self.pack,
            self.provider,
            self.store,
            clock=lambda: 100.0,
        )
        self.adapter = SlackAdapter(
            self.service,
            self.config.adapter,
            bot_token="bot-placeholder",
            app_token="app-placeholder",
            sleeper=lambda _delay: None,
        )
        self.client = FakeSlackClient()
        self.assertTrue(self.adapter._start_workers())
        self.addCleanup(self.adapter.close)
        self.environment = patch.dict(
            "os.environ",
            {"READING_PACK_BOT_DISABLED": ""},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sequence = 0

    def send(
        self,
        text: str,
        *,
        event_id: str,
        workspace: str = "T1",
        channel: str = "C1",
        thread: str | None = "90.0",
    ) -> list[dict[str, object]]:
        self.sequence += 1
        timestamp = f"10{self.sequence}.1"
        event = {
            "channel": channel,
            "user": "U1",
            "text": f"<@UBOT> {text}",
            "ts": timestamp,
        }
        if thread is not None:
            event["thread_ts"] = thread
        start = len(self.client.posts)
        self.adapter.receive_mention(
            body={"team_id": workspace, "event_id": event_id},
            event=event,
            client=self.client,
            context={"bot_user_id": "UBOT"},
        )
        self.adapter._jobs.join()
        return self.client.posts[start:]

    def conversation_key(self, thread: str):
        return IncomingMessage(
            event_id="unused",
            platform="slack",
            installation_id="T1",
            channel_id="C1",
            thread_id=thread,
            actor_id="U1",
            text="unused",
        ).conversation_key(self.pack.sha256)

    def test_help_reaches_slack_without_model_call(self) -> None:
        posts = self.send("help", event_id="E-help")
        self.assertEqual(len(posts), 1)
        self.assertIn("**使い方**", posts[0]["markdown_text"])
        self.assertIn("`status`", posts[0]["markdown_text"])
        self.assertEqual(posts[0]["thread_ts"], "90.0")
        self.assertFalse(posts[0]["reply_broadcast"])
        self.assertFalse(posts[0]["link_names"])
        self.assertFalse(posts[0]["unfurl_links"])
        self.assertFalse(posts[0]["unfurl_media"])
        self.assertNotIn("text", posts[0])
        self.assertNotIn("mrkdwn", posts[0])
        self.assertEqual(self.provider.requests, [])

    def test_status_reaches_slack_without_model_call(self) -> None:
        posts = self.send("status", event_id="E-status")
        self.assertEqual(len(posts), 1)
        self.assertIn("Reading Pack v=1.0.0", posts[0]["markdown_text"])
        self.assertIn(f"sha256={self.pack.sha256[:12]}", posts[0]["markdown_text"])
        self.assertEqual(self.provider.requests, [])

    def test_question_preserves_markdown_and_neutralizes_mentions(self) -> None:
        posts = self.send("question", event_id="E-question")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["markdown_text"], "## Answer\n\n**bold** [x] [x]")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.provider.requests[0].current_question, "question")

    def test_generation_status_appears_only_for_unique_model_requests(self) -> None:
        self.adapter.config = replace(
            self.adapter.config, show_generation_status=True
        )
        posts = self.send("question", event_id="E-status-question")
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            self.client.statuses,
            [
                {
                    "channel_id": "C1",
                    "thread_ts": "90.0",
                    "status": "が回答を作成しています…",
                }
            ],
        )
        self.send("help", event_id="E-status-help")
        self.send("status", event_id="E-status-command")
        self.send("reset", event_id="E-status-reset")
        self.send("question", event_id="E-status-question")
        self.assertEqual(len(self.client.statuses), 1)

    def test_thread_context_is_forwarded_to_next_question(self) -> None:
        self.send("first", event_id="E-first")
        self.send("second", event_id="E-second")
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(
            [turn.content for turn in self.provider.requests[1].prior_turns],
            ["first", "## Answer\n\n**bold** <@U999> <!here>"],
        )

    def test_reset_clears_only_the_selected_thread(self) -> None:
        self.send("first", event_id="E-first", thread="90.0")
        self.send("second", event_id="E-second", thread="91.0")
        posts = self.send("reset", event_id="E-reset", thread="90.0")
        self.assertEqual(len(posts), 1)
        self.assertIn("消去", posts[0]["markdown_text"])
        self.assertEqual(
            self.store.load_turns(self.conversation_key("90.0"), 10, 3600, 100.0),
            (),
        )
        self.assertEqual(
            len(self.store.load_turns(self.conversation_key("91.0"), 10, 3600, 100.0)),
            2,
        )

    def test_disallowed_route_is_silent(self) -> None:
        with self.assertLogs("reading_pack_bot.adapters.slack", level="INFO") as captured:
            posts = self.send("help", event_id="E-denied", channel="C2")
        self.assertEqual(posts, [])
        self.assertIn("channel_allowed=False", "\n".join(captured.output))
        self.assertEqual(self.provider.requests, [])

    def test_joined_policy_handles_an_invited_channel_without_channel_allowlist(self) -> None:
        self.adapter.config = replace(
            self.adapter.config,
            channel_policy="joined",
            allowed_channels=(),
        )
        self.service.policy.adapter = self.adapter.config
        posts = self.send("help", event_id="E-joined", channel="C2")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["channel"], "C2")

    def test_duplicate_event_is_posted_only_once(self) -> None:
        self.assertEqual(len(self.send("help", event_id="E-duplicate")), 1)
        self.assertEqual(self.send("help", event_id="E-duplicate"), [])
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.provider.requests, [])


if __name__ == "__main__":
    unittest.main()
