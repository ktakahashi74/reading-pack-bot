from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from reading_pack_bot.config import WebConfig
from reading_pack_bot.errors import ConfigurationError, ProviderError
from reading_pack_bot.models import GenerationRequest, Turn
from reading_pack_bot.pack import load_pack
from reading_pack_bot.providers.openai import (
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from tests.helpers import FIXTURE, FIXTURE_SHA256


def response(
    *,
    finish_reason="stop",
    model="provider/model-v1",
    output_text="answer",
    choices_count=1,
    response_id="chatcmpl-1",
):
    choices = [
        SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=output_text),
        )
        for _ in range(choices_count)
    ]
    return SimpleNamespace(
        id=response_id,
        model=model,
        choices=choices,
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        ),
    )


def responses_response(
    *,
    status="completed",
    model="provider/model-v1",
    output_text="answer",
    annotations=None,
    response_id="resp-1",
):
    content = [
        SimpleNamespace(
            type="output_text",
            text=output_text,
            annotations=[] if annotations is None else annotations,
        )
    ]
    return SimpleNamespace(
        id=response_id,
        status=status,
        model=model,
        output=[
            SimpleNamespace(type="web_search_call", status="completed"),
            SimpleNamespace(type="message", role="assistant", content=content),
        ],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=6,
            total_tokens=18,
            input_tokens_details=SimpleNamespace(cached_tokens=4),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )


def web_config(enabled=True):
    return WebConfig(
        enabled=enabled,
        max_search_uses=8,
        max_fetch_uses=5,
        max_pause_continuations=1,
        max_content_tokens=20000,
    )


class OpenAICompatibleProviderTests(unittest.TestCase):
    def setUp(self):
        self.pack = load_pack(
            FIXTURE, expected_sha256=FIXTURE_SHA256, max_bytes=524288
        )

    def request(self):
        return GenerationRequest(
            runtime_instructions="runtime",
            pack=self.pack,
            prior_turns=(
                Turn(role="user", content="prior", created_at=1),
                Turn(role="assistant", content="old answer", created_at=2),
            ),
            current_question="current",
        )

    def provider(self, result=None, *, web=None, web_result=None):
        client = Mock()
        client.chat.completions.create.return_value = result or response()
        client.responses.create.return_value = web_result or responses_response()
        return (
            OpenAICompatibleProvider(
                model="provider/model-v1",
                timeout_seconds=30,
                max_retries=0,
                max_output_tokens=800,
                web=web,
                client=client,
            ),
            client,
        )

    def test_legacy_class_name_is_an_alias(self):
        self.assertIs(OpenAIProvider, OpenAICompatibleProvider)

    def test_model_is_required(self):
        with self.assertRaises(ConfigurationError):
            OpenAICompatibleProvider(
                model="",
                timeout_seconds=30,
                max_retries=0,
                max_output_tokens=800,
                client=Mock(),
            )

    def test_pack_runtime_and_history_are_sent_once(self):
        provider, client = self.provider()
        provider.generate(self.request())
        kwargs = client.chat.completions.create.call_args.kwargs
        system = kwargs["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertEqual(system["content"].count(self.pack.raw_markdown), 1)
        self.assertIn(self.pack.sha256, system["content"])
        self.assertEqual(
            kwargs["messages"][1:],
            [
                {"role": "user", "content": "prior"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"},
            ],
        )
        self.assertEqual(kwargs["model"], "provider/model-v1")
        self.assertEqual(kwargs["max_tokens"], 800)
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("store", kwargs)
        client.responses.create.assert_not_called()

    def test_web_uses_responses_hosted_search_without_provider_storage(self):
        provider, client = self.provider(web=web_config())
        provider.generate(self.request())
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "provider/model-v1")
        self.assertEqual(kwargs["instructions"].count(self.pack.raw_markdown), 1)
        self.assertIn(self.pack.sha256, kwargs["instructions"])
        self.assertEqual(
            kwargs["input"],
            [
                {"role": "user", "content": "prior"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"},
            ],
        )
        self.assertEqual(kwargs["max_output_tokens"], 800)
        self.assertEqual(kwargs["max_tool_calls"], 13)
        self.assertEqual(kwargs["tools"], [{"type": "web_search"}])
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertIs(kwargs["store"], False)
        client.chat.completions.create.assert_not_called()

    def test_disabled_web_keeps_chat_completions_path(self):
        provider, client = self.provider(web=web_config(enabled=False))
        self.assertEqual(provider.generate(self.request()).text, "answer")
        client.chat.completions.create.assert_called_once()
        client.responses.create.assert_not_called()

    def test_web_citation_urls_are_appended_once(self):
        source = "https://example.com/source"
        citations = [
            SimpleNamespace(type="url_citation", url=source, title="Source"),
            SimpleNamespace(type="url_citation", url=source, title="Source"),
        ]
        provider, _client = self.provider(
            web=web_config(),
            web_result=responses_response(annotations=citations),
        )
        self.assertEqual(
            provider.generate(self.request()).text,
            f"answer\n\n出典:\n- {source}",
        )

    def test_visible_web_citation_url_is_not_duplicated(self):
        source = "https://example.com/source"
        citation = SimpleNamespace(type="url_citation", url=source, title="Source")
        provider, _client = self.provider(
            web=web_config(),
            web_result=responses_response(
                output_text=f"answer {source}", annotations=[citation]
            ),
        )
        self.assertEqual(provider.generate(self.request()).text.count(source), 1)

    def test_web_result_and_usage_are_extracted(self):
        provider, _client = self.provider(web=web_config())
        result = provider.generate(self.request())
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.model, "provider/model-v1")
        self.assertEqual(result.response_id, "resp-1")
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(result.usage.output_tokens, 6)
        self.assertEqual(result.usage.total_tokens, 18)
        self.assertEqual(result.usage.cached_tokens, 4)
        self.assertEqual(result.usage.reasoning_tokens, 3)

    def test_web_response_must_complete(self):
        for status in ("in_progress", "incomplete", None):
            with self.subTest(status=status):
                provider, _client = self.provider(
                    web=web_config(),
                    web_result=responses_response(status=status),
                )
                with self.assertRaisesRegex(ProviderError, "complete"):
                    provider.generate(self.request())

    def test_web_response_requires_assistant_output_text(self):
        result = responses_response()
        result.output = [SimpleNamespace(type="web_search_call", status="completed")]
        provider, _client = self.provider(web=web_config(), web_result=result)
        with self.assertRaisesRegex(ProviderError, "no text"):
            provider.generate(self.request())

    def test_web_sdk_exception_is_redacted(self):
        provider, client = self.provider(web=web_config())
        client.responses.create.side_effect = RuntimeError("SECRET")
        with self.assertRaises(ProviderError) as captured:
            provider.generate(self.request())
        self.assertNotIn("SECRET", str(captured.exception))
        self.assertIn("RuntimeError", str(captured.exception))

    def test_result_and_usage_are_extracted(self):
        provider, _client = self.provider()
        result = provider.generate(self.request())
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.model, "provider/model-v1")
        self.assertEqual(result.response_id, "chatcmpl-1")
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.output_tokens, 4)
        self.assertEqual(result.usage.total_tokens, 14)
        self.assertEqual(result.usage.cached_tokens, 3)
        self.assertEqual(result.usage.reasoning_tokens, 2)

    def test_missing_usage_is_accepted(self):
        result = response()
        result.usage = None
        provider, _client = self.provider(result)
        self.assertIsNone(provider.generate(self.request()).usage.total_tokens)

    def test_non_stop_response_is_rejected(self):
        for finish_reason in ("length", "content_filter", None):
            with self.subTest(finish_reason=finish_reason):
                provider, _client = self.provider(
                    response(finish_reason=finish_reason)
                )
                with self.assertRaisesRegex(ProviderError, "complete"):
                    provider.generate(self.request())

    def test_exactly_one_choice_is_required(self):
        for count in (0, 2):
            with self.subTest(count=count):
                provider, _client = self.provider(response(choices_count=count))
                with self.assertRaisesRegex(ProviderError, "one choice"):
                    provider.generate(self.request())

    def test_untrusted_response_model_falls_back_to_configured_model(self):
        for model in (None, "model with spaces", "x" * 201):
            with self.subTest(model=model):
                provider, _client = self.provider(response(model=model))
                self.assertEqual(
                    provider.generate(self.request()).model, "provider/model-v1"
                )

    def test_untrusted_response_id_is_dropped(self):
        for response_id in (None, "bad\nidentifier", "x" * 201):
            with self.subTest(response_id=response_id):
                provider, _client = self.provider(response(response_id=response_id))
                self.assertIsNone(provider.generate(self.request()).response_id)

    def test_empty_output_is_rejected(self):
        for output in (None, "", "   "):
            with self.subTest(output=output):
                provider, _client = self.provider(response(output_text=output))
                with self.assertRaisesRegex(ProviderError, "no text"):
                    provider.generate(self.request())

    def test_sdk_exception_is_redacted(self):
        provider, client = self.provider()
        client.chat.completions.create.side_effect = RuntimeError("SECRET")
        with self.assertRaises(ProviderError) as captured:
            provider.generate(self.request())
        self.assertNotIn("SECRET", str(captured.exception))
        self.assertIn("RuntimeError", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
