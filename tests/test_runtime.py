from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reading_pack_bot.config import load_config
from reading_pack_bot.errors import ConfigurationError
from reading_pack_bot.runtime import create_provider
from tests.helpers import config_text


class RuntimeProviderTests(unittest.TestCase):
    def config(self, **changes):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(provider="anthropic", model="claude-sonnet-5", **changes),
                encoding="utf-8",
            )
            return load_config(path)

    def openai_compatible_config(self, **changes):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(
                    provider="openai-compatible",
                    model="provider/model-v1",
                    **changes,
                ),
                encoding="utf-8",
            )
            return load_config(path)

    def test_anthropic_secret_is_required(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "ANTHROPIC_API_KEY"):
                create_provider(self.config())

    def test_anthropic_provider_receives_minimal_configuration(self):
        config = self.config(web_enabled=True)
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=True),
            patch("reading_pack_bot.runtime.AnthropicProvider") as provider_class,
        ):
            result = create_provider(config)
        self.assertIs(result, provider_class.return_value)
        provider_class.assert_called_once_with(
            model="claude-sonnet-5",
            timeout_seconds=30.0,
            max_retries=1,
            max_output_tokens=800,
            web=config.web,
        )

    def test_openai_compatible_custom_secret_is_required(self):
        config = self.openai_compatible_config(api_key_env="MODEL_API_KEY")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "MODEL_API_KEY"):
                create_provider(config)

    def test_openai_compatible_provider_receives_endpoint_and_secret(self):
        config = self.openai_compatible_config(
            base_url="https://models.example.org/v1",
            api_key_env="MODEL_API_KEY",
        )
        with (
            patch.dict("os.environ", {"MODEL_API_KEY": "test-key"}, clear=True),
            patch(
                "reading_pack_bot.runtime.OpenAICompatibleProvider"
            ) as provider_class,
        ):
            result = create_provider(config)
        self.assertIs(result, provider_class.return_value)
        provider_class.assert_called_once_with(
            model="provider/model-v1",
            timeout_seconds=30.0,
            max_retries=1,
            max_output_tokens=800,
            base_url="https://models.example.org/v1",
            api_key="test-key",
            web=config.web,
        )

    def test_openai_compatible_provider_receives_web_configuration(self):
        config = self.openai_compatible_config(web_enabled=True)
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch(
                "reading_pack_bot.runtime.OpenAICompatibleProvider"
            ) as provider_class,
        ):
            create_provider(config)
        self.assertIs(provider_class.call_args.kwargs["web"], config.web)

    def test_local_loopback_provider_needs_no_secret(self):
        config = self.openai_compatible_config(
            base_url="http://127.0.0.1:11434/v1", api_key_env=""
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "reading_pack_bot.runtime.OpenAICompatibleProvider"
            ) as provider_class,
        ):
            create_provider(config)
        self.assertEqual(provider_class.call_args.kwargs["api_key"], "local-no-key")

    def test_fake_provider_needs_no_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fake.toml"
            path.write_text(config_text(provider="fake"), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(type(create_provider(config)).__name__, "FakeProvider")


if __name__ == "__main__":
    unittest.main()
