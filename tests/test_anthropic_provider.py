from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from reading_pack_bot.config import WebConfig
from reading_pack_bot.errors import ConfigurationError, ProviderError
from reading_pack_bot.models import GenerationRequest, Turn
from reading_pack_bot.pack import load_pack
from reading_pack_bot.providers.anthropic import AnthropicProvider
from tests.helpers import FIXTURE, FIXTURE_SHA256


def block(block_type: str, **values):
    return SimpleNamespace(type=block_type, **values)


def usage(
    input_tokens=10,
    output_tokens=4,
    cache_read_input_tokens=2,
    cache_creation_input_tokens=0,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        output_tokens_details=SimpleNamespace(thinking_tokens=1),
    )


def response(*, content=None, stop_reason="end_turn", model="claude-sonnet-5", id="msg-1", response_usage=None):
    return SimpleNamespace(
        content=content if content is not None else [block("text", text="answer", citations=[])],
        stop_reason=stop_reason,
        model=model,
        id=id,
        usage=response_usage if response_usage is not None else usage(),
    )


class AnthropicProviderTests(unittest.TestCase):
    def setUp(self):
        self.pack = load_pack(FIXTURE, expected_sha256=FIXTURE_SHA256, max_bytes=524288)

    def request(self, question="current"):
        return GenerationRequest(
            runtime_instructions="runtime rules",
            pack=self.pack,
            prior_turns=(
                Turn(role="user", content="prior", created_at=1),
                Turn(role="assistant", content="previous answer", created_at=2),
            ),
            current_question=question,
        )

    def web(self, *, enabled=True, pauses=1):
        return WebConfig(
            enabled=enabled,
            max_search_uses=8,
            max_fetch_uses=5,
            max_pause_continuations=pauses,
            max_content_tokens=20000,
        )

    def provider(self, *, responses=None, web=None, model="claude-sonnet-5"):
        client = Mock()
        if responses is None:
            client.messages.create.return_value = response(model=model)
        else:
            client.messages.create.side_effect = responses
        provider = AnthropicProvider(
            model=model,
            timeout_seconds=30,
            max_retries=0,
            max_output_tokens=4096,
            web=web,
            client=client,
        )
        return provider, client

    def test_model_is_required(self):
        with self.assertRaises(ConfigurationError):
            AnthropicProvider(
                model="",
                timeout_seconds=30,
                max_retries=0,
                max_output_tokens=100,
                client=Mock(),
            )

    def test_pack_is_a_citable_cached_document_before_history(self):
        provider, client = self.provider(web=self.web())
        provider.generate(self.request("question"))
        kwargs = client.messages.create.call_args.kwargs
        self.assertNotIn(self.pack.raw_markdown, kwargs["system"])
        self.assertEqual(kwargs["system"], "runtime rules")
        self.assertNotIn("primary source", kwargs["system"])
        messages = kwargs["messages"]
        document = messages[0]["content"][0]
        self.assertEqual(document["type"], "document")
        self.assertEqual(document["source"]["data"], self.pack.raw_markdown)
        self.assertEqual(document["citations"], {"enabled": True})
        self.assertEqual(document["cache_control"], {"type": "ephemeral"})
        self.assertIn(self.pack.sha256, document["context"])
        companion = messages[0]["content"][1]["text"]
        self.assertEqual(
            companion,
            "The attached document is the Reading Pack referenced by the system instruction.",
        )
        self.assertNotIn("primary context", companion)
        self.assertEqual([item["role"] for item in messages], ["user", "user", "assistant", "user"])
        self.assertEqual(
            messages[2]["content"][0]["cache_control"], {"type": "ephemeral"}
        )
        self.assertEqual(messages[-1]["content"], "question")

    def test_web_tools_are_provider_hosted_and_not_domain_filtered(self):
        provider, client = self.provider(web=self.web())
        provider.generate(self.request())
        search, fetch = client.messages.create.call_args.kwargs["tools"]
        self.assertEqual(search["type"], "web_search_20260318")
        self.assertEqual(search["max_uses"], 8)
        self.assertEqual(fetch["type"], "web_fetch_20260318")
        self.assertEqual(fetch["max_uses"], 5)
        self.assertEqual(fetch["max_content_tokens"], 20000)
        for tool in (search, fetch):
            self.assertEqual(tool["allowed_callers"], ["direct"])
            self.assertNotIn("allowed_domains", tool)
            self.assertNotIn("blocked_domains", tool)

    def test_disabled_web_sends_no_tools(self):
        provider, client = self.provider(web=self.web(enabled=False))
        provider.generate(self.request())
        self.assertNotIn("tools", client.messages.create.call_args.kwargs)

    def test_sonnet_five_uses_medium_adaptive_thinking(self):
        for model in ("claude-sonnet-5", "claude-sonnet-5-20260801"):
            with self.subTest(model=model):
                provider, client = self.provider(model=model)
                provider.generate(self.request())
                kwargs = client.messages.create.call_args.kwargs
                self.assertEqual(kwargs["thinking"], {"type": "adaptive", "display": "omitted"})
                self.assertEqual(kwargs["output_config"], {"effort": "medium"})

    def test_other_model_gets_no_sonnet_specific_options(self):
        provider, client = self.provider(model="claude-fable-5")
        provider.generate(self.request())
        kwargs = client.messages.create.call_args.kwargs
        self.assertNotIn("thinking", kwargs)
        self.assertNotIn("output_config", kwargs)

    def test_text_blocks_are_joined_and_nontext_blocks_ignored(self):
        content = [
            block("thinking", thinking="private"),
            block("server_tool_use", name="web_search"),
            block("future_provider_block", value="ignored"),
            block("text", text="first", citations=[]),
            block("text", text=" second", citations=[]),
        ]
        provider, _client = self.provider(responses=[response(content=content)])
        self.assertEqual(provider.generate(self.request()).text, "first second")

    def test_web_search_citation_url_is_appended(self):
        citation = SimpleNamespace(type="web_search_result_location", url="https://example.com/source")
        content = [block("text", text="answer", citations=[citation])]
        provider, _client = self.provider(responses=[response(content=content)], web=self.web())
        self.assertEqual(
            provider.generate(self.request()).text,
            "answer\n\n出典:\n- https://example.com/source",
        )

    def test_visible_source_url_is_not_duplicated(self):
        url = "https://example.com/source"
        citation = SimpleNamespace(type="web_search_result_location", url=url)
        provider, _client = self.provider(
            responses=[response(content=[block("text", text=f"answer {url}", citations=[citation])])],
            web=self.web(),
        )
        self.assertEqual(provider.generate(self.request()).text, f"answer {url}")

    def test_prefix_source_url_is_not_hidden_by_longer_visible_url(self):
        source = "https://example.com/source"
        longer = source + "2"
        citation = SimpleNamespace(type="web_search_result_location", url=source)
        provider, _client = self.provider(
            responses=[response(content=[block("text", text=longer, citations=[citation])])],
            web=self.web(),
        )
        self.assertIn(f"\n- {source}", provider.generate(self.request()).text)

    def test_pack_citation_needs_no_external_source_line(self):
        citation = SimpleNamespace(type="char_location", document_index=0, url=None)
        provider, _client = self.provider(
            responses=[response(content=[block("text", text="pack answer", citations=[citation])])]
        )
        self.assertEqual(provider.generate(self.request()).text, "pack answer")

    def test_fetch_document_citation_maps_after_pack_document(self):
        url = "https://example.com/page"
        fetch = block(
            "web_fetch_tool_result",
            content=SimpleNamespace(type="web_fetch_result", url=url),
        )
        citation = SimpleNamespace(type="char_location", document_index=1, url=None)
        content = [fetch, block("text", text="fetched answer", citations=[citation])]
        provider, _client = self.provider(responses=[response(content=content)], web=self.web())
        self.assertIn(url, provider.generate(self.request()).text)

    def test_fetch_without_citation_is_still_a_usable_answer(self):
        fetch = block(
            "web_fetch_tool_result",
            content=SimpleNamespace(type="web_fetch_result", url="https://example.com/page"),
        )
        content = [fetch, block("text", text="answer without structured citation", citations=[])]
        provider, _client = self.provider(responses=[response(content=content)], web=self.web())
        self.assertEqual(provider.generate(self.request()).text, "answer without structured citation")

    def test_pause_turn_is_resubmitted_and_text_is_preserved(self):
        paused_content = [
            block("text", text="prefix ", citations=[]),
            block("server_tool_use", name="web_search"),
        ]
        final_content = [block("text", text="answer", citations=[])]
        provider, client = self.provider(
            responses=[
                response(content=paused_content, stop_reason="pause_turn", id="pause"),
                response(content=final_content, id="final"),
            ],
            web=self.web(),
        )
        result = provider.generate(self.request())
        self.assertEqual(result.text, "prefix answer")
        second_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(second_messages[-1], {"role": "assistant", "content": paused_content})
        self.assertEqual(result.response_id, "final")

    def test_pause_turn_is_bounded(self):
        paused = response(
            content=[block("server_tool_use", name="web_search")],
            stop_reason="pause_turn",
        )
        provider, _client = self.provider(
            responses=[paused, paused],
            web=self.web(pauses=1),
        )
        with self.assertRaisesRegex(ProviderError, "exceeded"):
            provider.generate(self.request())

    def test_pause_turn_requires_enabled_web(self):
        provider, _client = self.provider(
            responses=[response(content=[], stop_reason="pause_turn")],
            web=self.web(enabled=False),
        )
        with self.assertRaisesRegex(ProviderError, "complete normally"):
            provider.generate(self.request())

    def test_empty_pause_turn_is_rejected_before_resubmission(self):
        provider, client = self.provider(
            responses=[response(content=[], stop_reason="pause_turn")],
            web=self.web(),
        )
        with self.assertRaisesRegex(ProviderError, "no resubmittable content"):
            provider.generate(self.request())
        self.assertEqual(client.messages.create.call_count, 1)

    def test_empty_or_malformed_content_is_rejected(self):
        cases = (
            response(content=[]),
            response(content="invalid"),
            response(content=[block("thinking", thinking="only")]),
        )
        for item in cases:
            with self.subTest(content=item.content):
                provider, _client = self.provider(responses=[item])
                with self.assertRaises(ProviderError):
                    provider.generate(self.request())

    def test_non_end_turn_is_rejected(self):
        provider, _client = self.provider(responses=[response(stop_reason="max_tokens")])
        with self.assertRaisesRegex(ProviderError, "complete normally"):
            provider.generate(self.request())

    def test_sdk_exception_is_redacted(self):
        provider, _client = self.provider(responses=[RuntimeError("SECRET")])
        with self.assertRaises(ProviderError) as captured:
            provider.generate(self.request())
        self.assertNotIn("SECRET", str(captured.exception))
        self.assertIn("RuntimeError", str(captured.exception))

    def test_response_model_must_match(self):
        for model in (None, "claude-other-5"):
            with self.subTest(model=model):
                provider, _client = self.provider(responses=[response(model=model)])
                with self.assertRaisesRegex(ProviderError, "model differs"):
                    provider.generate(self.request())

    def test_usage_is_combined_across_pause(self):
        provider, _client = self.provider(
            responses=[
                response(
                    content=[block("server_tool_use", name="web_search")],
                    stop_reason="pause_turn",
                    response_usage=usage(10, 2, 3, 7),
                ),
                response(response_usage=usage(20, 4, 5, 11)),
            ],
            web=self.web(),
        )
        result = provider.generate(self.request())
        self.assertEqual(result.usage.input_tokens, 56)
        self.assertEqual(result.usage.output_tokens, 6)
        self.assertEqual(result.usage.total_tokens, 62)
        self.assertEqual(result.usage.cached_tokens, 8)


if __name__ == "__main__":
    unittest.main()
