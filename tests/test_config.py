from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from reading_pack_bot.config import load_config
from reading_pack_bot.errors import ConfigurationError, PackValidationError
from reading_pack_bot.runtime import verified_pack
from tests.helpers import FIXTURE, FIXTURE_SHA256, config_text


class ConfigTests(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def staging_slack(self, **changes):
        values = {
            "stage": "staging",
            "provider": "openai",
            "model": "gpt-test-2026-01-01",
            "store": "sqlite",
            "store_path": "/tmp/state.sqlite3",
            "provider_timeout": 60.0,
            "max_retries": 0,
            "queue_size": 1,
            "post_timeout_seconds": 10,
            "max_answer_characters": 500,
            "message_chunk_characters": 500,
        }
        values.update(changes)
        return config_text(**values)

    def test_valid_configuration(self):
        config = self.load(config_text())
        self.assertEqual(config.pack.sha256, FIXTURE_SHA256)
        self.assertEqual(config.pack.path, FIXTURE.resolve())
        self.assertEqual(config.adapter.allowed_channels, ("C1",))
        self.assertEqual(config.adapter.allowed_installations, ("T1",))
        self.assertEqual(config.adapter.channel_policy, "allowlist")
        self.assertEqual(config.adapter.max_concurrent_generations, 1)
        self.assertFalse(config.adapter.show_generation_status)
        self.assertFalse(config.web.enabled)

    def test_pack_hash_is_optional(self):
        config = self.load(config_text(pack_hash=None))
        self.assertIsNone(config.pack.sha256)
        pack = verified_pack(config)
        self.assertEqual(pack.sha256, FIXTURE_SHA256)

    def test_generation_status_defaults_off_when_omitted(self):
        config = self.load(
            config_text().replace("show_generation_status = false\n", "")
        )
        self.assertFalse(config.adapter.show_generation_status)

    def test_routing_and_concurrency_defaults_when_omitted(self):
        text = config_text().replace('channel_policy = "allowlist"\n', "").replace(
            "max_concurrent_generations = 1\n", ""
        )
        config = self.load(text)
        self.assertEqual(config.adapter.channel_policy, "allowlist")
        self.assertEqual(config.adapter.max_concurrent_generations, 1)

    def test_output_defaults_match_single_message_evaluation(self):
        text = config_text().replace("max_output_tokens = 800\n", "").replace(
            "max_answer_characters = 500\n", ""
        )
        config = self.load(text)
        self.assertEqual(config.provider.max_output_tokens, 4096)
        self.assertEqual(config.policy.max_answer_characters, 3500)

    def test_conversation_retention_can_be_unlimited(self):
        text = config_text().replace(
            "conversation_ttl_seconds = 3600",
            "conversation_ttl_seconds = 0",
        )
        self.assertEqual(self.load(text).store.conversation_ttl_seconds, 0)

    def test_conversation_retention_rejects_short_nonzero_ttl(self):
        text = config_text().replace(
            "conversation_ttl_seconds = 3600",
            "conversation_ttl_seconds = 1",
        )
        with self.assertRaisesRegex(ConfigurationError, "0 for unlimited retention"):
            self.load(text)

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            self.load(config_text() + "\nunknown = true\n")

    def test_malformed_hash_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "SHA-256"):
            self.load(config_text(pack_hash="abc"))

    def test_uppercase_hash_is_rejected_instead_of_normalized(self):
        with self.assertRaisesRegex(ConfigurationError, "lowercase"):
            self.load(config_text(pack_hash=FIXTURE_SHA256.upper()))

    def test_openai_compatible_requires_model(self):
        with self.assertRaisesRegex(ConfigurationError, "model"):
            self.load(config_text(provider="openai-compatible"))

    def test_legacy_openai_kind_is_normalized(self):
        config = self.load(
            config_text(provider="openai", model="gpt-test-2026-01-01")
        )
        self.assertEqual(config.provider.kind, "openai-compatible")
        self.assertEqual(config.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.provider.api_key_env, "OPENAI_API_KEY")

    def test_openai_compatible_accepts_provider_neutral_model_ids(self):
        for model in (
            "gpt-5.1",
            "openai/gpt-5",
            "anthropic/claude-sonnet-4",
            "local_model:v2",
        ):
            with self.subTest(model=model):
                config = self.load(
                    config_text(provider="openai-compatible", model=model)
                )
                self.assertEqual(config.provider.model, model)

    def test_openai_compatible_rejects_unsafe_model_ids(self):
        for model in (" model", "model name", "/model", "model?variant", "x" * 201):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ConfigurationError, "safe model"):
                    self.load(
                        config_text(provider="openai-compatible", model=model)
                    )

    def test_openai_compatible_accepts_https_endpoint_and_custom_secret_name(self):
        config = self.load(
            config_text(
                provider="openai-compatible",
                model="provider/model",
                base_url="https://models.example.org/openai/v1",
                api_key_env="EXAMPLE_MODEL_KEY",
            )
        )
        self.assertEqual(
            config.provider.base_url, "https://models.example.org/openai/v1"
        )
        self.assertEqual(config.provider.api_key_env, "EXAMPLE_MODEL_KEY")

    def test_openai_compatible_allows_no_key_only_for_local_loopback(self):
        for base_url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:8080/v1",
            "http://[::1]:8080/v1",
            "https://localhost/v1",
        ):
            with self.subTest(base_url=base_url):
                config = self.load(
                    config_text(
                        provider="openai-compatible",
                        model="local-model",
                        base_url=base_url,
                        api_key_env="",
                    )
                )
                self.assertEqual(config.provider.api_key_env, "")

        with self.assertRaisesRegex(ConfigurationError, "may be empty"):
            self.load(
                config_text(
                    provider="openai-compatible",
                    model="remote-model",
                    base_url="https://models.example.org/v1",
                    api_key_env="",
                )
            )

    def test_openai_compatible_rejects_insecure_remote_or_nonlocal_http(self):
        for stage, base_url in (
            ("local", "http://models.example.org/v1"),
            ("staging", "http://localhost:8080/v1"),
        ):
            with self.subTest(stage=stage, base_url=base_url):
                with self.assertRaisesRegex(ConfigurationError, "requires https"):
                    self.load(
                        config_text(
                            stage=stage,
                            provider="openai-compatible",
                            model="model-v1",
                            base_url=base_url,
                        )
                    )

    def test_openai_compatible_rejects_ambiguous_or_credentialed_urls(self):
        cases = (
            "https://user:password@models.example.org/v1",
            "https://models.example.org/v1?tenant=x",
            "https://models.example.org/v1#fragment",
            "https://models.example.org:bad/v1",
            "https://models.example.org/with space",
        )
        for base_url in cases:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ConfigurationError):
                    self.load(
                        config_text(
                            provider="openai-compatible",
                            model="model-v1",
                            base_url=base_url,
                        )
                    )

    def test_openai_compatible_rejects_invalid_secret_environment_name(self):
        for name in ("9KEY", "BAD-NAME", "KEY NAME", "x" * 129):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, "environment variable"):
                    self.load(
                        config_text(
                            provider="openai-compatible",
                            model="model-v1",
                            api_key_env=name,
                        )
                    )

    def test_provider_connection_fields_are_openai_compatible_only(self):
        for provider, model in (("fake", ""), ("anthropic", "claude-sonnet-5")):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(
                    ConfigurationError, "provider.kind=openai-compatible"
                ):
                    self.load(
                        config_text(
                            provider=provider,
                            model=model,
                            base_url="https://models.example.org/v1",
                        )
                    )

    def test_anthropic_requires_model(self):
        with self.assertRaisesRegex(ConfigurationError, "model"):
            self.load(config_text(provider="anthropic"))

    def test_anthropic_accepts_current_and_dated_snapshots(self):
        for model in (
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
        ):
            with self.subTest(model=model):
                config = self.load(config_text(provider="anthropic", model=model))
                self.assertEqual(config.provider.model, model)

    def test_anthropic_rejects_floating_or_malformed_model_ids(self):
        for model in (
            "claude-sonnet-4-5",
            "claude-3-5-sonnet",
            "claude-sonnet-latest",
            "claude-sonnet-5@20260814",
        ):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ConfigurationError, "pinned"):
                    self.load(config_text(provider="anthropic", model=model))

    def test_web_access_supports_anthropic_and_openai_compatible(self):
        for provider, model in (
            ("anthropic", "claude-sonnet-5"),
            ("openai-compatible", "gpt-test-2026-01-01"),
        ):
            with self.subTest(provider=provider):
                config = self.load(
                    config_text(
                        provider=provider,
                        model=model,
                        web_enabled=True,
                    )
                )
                self.assertTrue(config.web.enabled)
                self.assertEqual(config.web.max_search_uses, 8)
                self.assertEqual(config.web.max_fetch_uses, 5)

    def test_web_access_rejects_fake_provider(self):
        with self.assertRaisesRegex(ConfigurationError, "anthropic or openai-compatible"):
            self.load(config_text(provider="fake", web_enabled=True))

    def test_removed_reference_and_web_scope_keys_are_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            self.load(config_text() + "\n[references]\nenabled = false\n")
        for key, value in (
            ("official_hosts", '["example.com"]'),
            ("external_request_prefix", '"web:"'),
        ):
            with self.subTest(key=key):
                text = config_text().replace("[web]\n", f"[web]\n{key} = {value}\n")
                with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
                    self.load(text)

    def test_nonlocal_hosted_web_continuation_must_fit_service_stop_budget(self):
        common = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "web_enabled": True,
        }
        config = self.load(self.staging_slack(provider_timeout=35.0, **common))
        self.assertTrue(config.web.enabled)
        no_continuation = self.load(
            self.staging_slack(
                provider_timeout=60.0,
                max_web_pause_continuations=0,
                **common,
            )
        )
        self.assertEqual(no_continuation.web.max_pause_continuations, 0)
        with self.assertRaisesRegex(ConfigurationError, "TimeoutStopSec"):
            self.load(self.staging_slack(provider_timeout=38.0, **common))
        with self.assertRaisesRegex(ConfigurationError, "TimeoutStopSec"):
            self.load(
                self.staging_slack(
                    provider_timeout=26.0,
                    max_web_pause_continuations=2,
                    **common,
                )
            )

    def test_openai_hosted_web_needs_no_local_continuation_budget(self):
        config = self.load(
            self.staging_slack(
                provider="openai-compatible",
                model="gpt-test-2026-01-01",
                provider_timeout=60.0,
                web_enabled=True,
            )
        )
        self.assertTrue(config.web.enabled)

    def test_schema_version_three_is_required(self):
        with self.assertRaisesRegex(ConfigurationError, "equal 3"):
            self.load(config_text().replace("schema_version = 3", "schema_version = 2"))

    def test_removed_pack_policy_keys_are_rejected(self):
        for key, value in (
            ("release_attested", "true"),
            ("expected_version", '"1.0.0"'),
            ("expected_status", '"canonical"'),
            ("expected_language", '"en"'),
            ("expected_primary", '"ja"'),
            ("expected_profile", '"nonfiction-reading:required"'),
        ):
            with self.subTest(key=key):
                text = config_text().replace(
                    "[provider]\n", f"{key} = {value}\n[provider]\n"
                )
                with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
                    self.load(text)

    def test_slack_requires_workspace_allowlist(self):
        with self.assertRaisesRegex(ConfigurationError, "workspace allowlist"):
            self.load(config_text(workspaces="", channels=""))

    def test_valid_local_discord_configuration(self):
        config = self.load(
            config_text(
                adapter="discord",
                workspaces='"G1"',
                channels='"C1"',
                show_generation_status=True,
                max_concurrent_generations=4,
            )
        )
        self.assertEqual(config.adapter.kind, "discord")
        self.assertEqual(config.adapter.allowed_installations, ("G1",))
        self.assertTrue(config.adapter.show_generation_status)
        self.assertEqual(config.adapter.max_concurrent_generations, 4)

    def test_discord_requires_server_and_channel_allowlists(self):
        with self.assertRaisesRegex(ConfigurationError, "server allowlist"):
            self.load(
                config_text(
                    adapter="discord",
                    workspaces="",
                    channels='"C1"',
                )
            )
        with self.assertRaisesRegex(ConfigurationError, "channel allowlist"):
            self.load(
                config_text(
                    adapter="discord",
                    workspaces='"G1"',
                    channels="",
                )
            )

    def test_discord_rejects_joined_policy_and_chunks_over_2000(self):
        with self.assertRaisesRegex(ConfigurationError, "channel_policy=allowlist"):
            self.load(
                config_text(
                    adapter="discord",
                    workspaces='"G1"',
                    channel_policy="joined",
                    channels="",
                )
            )
        with self.assertRaisesRegex(ConfigurationError, "to 2000"):
            self.load(
                config_text(
                    adapter="discord",
                    workspaces='"G1"',
                    channels='"C1"',
                    message_chunk_characters=2001,
                )
            )

    def test_channel_allowlist_policy_requires_channels(self):
        with self.assertRaisesRegex(ConfigurationError, "channel allowlist"):
            self.load(config_text(channels=""))

    def test_joined_channel_policy_accepts_empty_channel_list(self):
        config = self.load(config_text(channel_policy="joined", channels=""))
        self.assertEqual(config.adapter.channel_policy, "joined")
        self.assertEqual(config.adapter.allowed_channels, ())
        self.assertTrue(config.adapter.allows_channel("C-any-joined-channel"))
        self.assertFalse(config.adapter.allows_channel(""))

    def test_joined_channel_policy_rejects_ambiguous_channel_list(self):
        with self.assertRaisesRegex(ConfigurationError, r"allowed_channels=\[\]"):
            self.load(config_text(channel_policy="joined"))

    def test_unknown_channel_policy_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "allowlist or joined"):
            self.load(config_text(channel_policy="workspace"))

    def test_disabled_adapter_accepts_empty_allowlists(self):
        config = self.load(config_text(adapter="disabled", workspaces="", channels=""))
        self.assertEqual(config.adapter.kind, "disabled")

    def test_generation_status_requires_platform_adapter(self):
        with self.assertRaisesRegex(
            ConfigurationError, "requires adapter.kind=slack or discord"
        ):
            self.load(
                config_text(
                    adapter="disabled",
                    workspaces="",
                    channels="",
                    show_generation_status=True,
                )
            )

    def test_multiple_generation_workers_require_platform_adapter(self):
        with self.assertRaisesRegex(
            ConfigurationError, "requires adapter.kind=slack or discord"
        ):
            self.load(
                config_text(
                    adapter="disabled",
                    workspaces="",
                    channels="",
                    max_concurrent_generations=2,
                )
            )

    def test_generation_worker_limit_is_one_to_four(self):
        config = self.load(config_text(max_concurrent_generations=4))
        self.assertEqual(config.adapter.max_concurrent_generations, 4)
        for value in (0, 5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigurationError, "integer from 1 to 4"):
                    self.load(config_text(max_concurrent_generations=value))

    def test_sqlite_requires_path(self):
        with self.assertRaisesRegex(ConfigurationError, "store.path"):
            self.load(config_text(store="sqlite"))

    def test_duplicate_allowlist_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "duplicates"):
            self.load(config_text(workspaces='"T1", "T1"'))

    def test_invalid_stage_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "stage"):
            self.load(config_text(stage="public"))

    def test_debug_logging_is_local_only(self):
        text = config_text(stage="staging").replace('log_level = "INFO"', 'log_level = "DEBUG"')
        with self.assertRaisesRegex(ConfigurationError, "DEBUG"):
            self.load(text)

    def test_nonlocal_openai_compatible_accepts_operator_selected_model_id(self):
        config = self.load(
            self.staging_slack(
                provider="openai-compatible", model="provider/floating-alias"
            )
        )
        self.assertEqual(config.provider.model, "provider/floating-alias")

    def test_pack_symlink_is_preserved_for_no_follow_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "pack.md"
            link.symlink_to(FIXTURE)
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(config_text(pack_path=link), encoding="utf-8")
            config = load_config(config_path)
            self.assertTrue(config.pack.path.is_symlink())
            with self.assertRaisesRegex(PackValidationError, "symbolic"):
                verified_pack(config)

    def test_configuration_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target.toml"
            target.write_text(config_text(), encoding="utf-8")
            link = Path(temporary) / "config.toml"
            link.symlink_to(target)
            with self.assertRaisesRegex(ConfigurationError, "configuration"):
                load_config(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_configuration_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(ConfigurationError, "regular file"):
                load_config(path)

    def test_production_rejects_fake_provider(self):
        with self.assertRaisesRegex(ConfigurationError, "external model"):
            self.load(
                config_text(
                    stage="production",
                    provider="fake",
                    store="sqlite",
                    store_path="/tmp/state.sqlite3",
                    adapter="disabled",
                    workspaces="",
                    channels="",
                )
            )

    def test_production_accepts_anthropic_provider(self):
        config = self.load(
            config_text(
                stage="production",
                pack_hash=None,
                provider="anthropic",
                model="claude-sonnet-5",
                store="sqlite",
                store_path="/tmp/state.sqlite3",
                adapter="disabled",
                workspaces="",
                channels="",
            )
        )
        self.assertEqual(config.provider.kind, "anthropic")

    def test_production_requires_pack_header_to_be_canonical(self):
        with tempfile.TemporaryDirectory() as temporary:
            pack_path = Path(temporary) / "beta.md"
            pack_path.write_bytes(
                FIXTURE.read_bytes().replace(
                    b"status=canonical", b"status=beta", 1
                )
            )
            config = self.load(
                config_text(
                    stage="production",
                    pack_path=pack_path,
                    pack_hash=None,
                    provider="anthropic",
                    model="claude-sonnet-5",
                    store="sqlite",
                    store_path="/tmp/state.sqlite3",
                    adapter="disabled",
                    workspaces="",
                    channels="",
                )
            )
            with self.assertRaisesRegex(PackValidationError, "canonical"):
                verified_pack(config)

    def test_production_rejects_memory_store(self):
        with self.assertRaisesRegex(ConfigurationError, "sqlite"):
            self.load(
                config_text(
                    stage="production",
                    provider="openai",
                    model="gpt-test-2026-01-01",
                    store="memory",
                    adapter="disabled",
                    workspaces="",
                    channels="",
                )
            )

    def test_nonlocal_slack_requires_persistent_store(self):
        with self.assertRaisesRegex(ConfigurationError, "sqlite"):
            self.load(config_text(stage="staging", store="memory"))

    def test_nonlocal_slack_bounded_shutdown_configuration(self):
        config = self.load(self.staging_slack())
        self.assertEqual(config.provider.max_retries, 0)
        self.assertEqual(config.adapter.queue_size, 1)
        self.assertEqual(config.adapter.max_concurrent_generations, 1)
        self.assertEqual(config.adapter.post_timeout_seconds, 10)

    def test_nonlocal_discord_bounded_shutdown_configuration(self):
        config = self.load(
            self.staging_slack(
                adapter="discord",
                workspaces='"G1"',
                channels='"C1"',
                message_chunk_characters=2000,
                max_answer_characters=2000,
                show_generation_status=True,
            )
        )
        self.assertEqual(config.adapter.kind, "discord")
        self.assertEqual(config.adapter.queue_size, 1)
        self.assertTrue(config.adapter.show_generation_status)
        with self.assertRaisesRegex(ConfigurationError, "TimeoutStopSec"):
            self.load(
                self.staging_slack(
                    adapter="discord",
                    workspaces='"G1"',
                    channels='"C1"',
                    message_chunk_characters=2000,
                    max_answer_characters=2000,
                    show_generation_status=True,
                    provider="anthropic",
                    model="claude-sonnet-5",
                    web_enabled=True,
                    provider_timeout=35.0,
                )
            )

    def test_nonlocal_slack_allows_bounded_parallel_workers(self):
        config = self.load(self.staging_slack(max_concurrent_generations=4))
        self.assertEqual(config.adapter.max_concurrent_generations, 4)
        self.assertEqual(config.adapter.queue_size, 1)

    def test_generation_status_is_included_in_shutdown_budget(self):
        common = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "web_enabled": True,
            "show_generation_status": True,
        }
        config = self.load(self.staging_slack(provider_timeout=30.0, **common))
        self.assertTrue(config.adapter.show_generation_status)
        with self.assertRaisesRegex(ConfigurationError, "TimeoutStopSec"):
            self.load(self.staging_slack(provider_timeout=35.0, **common))

    def test_nonlocal_slack_rejects_timeout_over_shutdown_bound(self):
        with self.assertRaisesRegex(ConfigurationError, "timeout_seconds"):
            self.load(self.staging_slack(provider_timeout=61.0))

    def test_nonlocal_slack_rejects_provider_retries(self):
        with self.assertRaisesRegex(ConfigurationError, "max_retries"):
            self.load(self.staging_slack(max_retries=1))

    def test_nonlocal_slack_rejects_backlog(self):
        with self.assertRaisesRegex(ConfigurationError, "queue_size"):
            self.load(self.staging_slack(queue_size=2))

    def test_nonlocal_slack_rejects_long_post_timeout(self):
        with self.assertRaisesRegex(ConfigurationError, "post_timeout_seconds"):
            self.load(self.staging_slack(post_timeout_seconds=11))

    def test_nonlocal_slack_rejects_multi_message_answer(self):
        with self.assertRaisesRegex(ConfigurationError, "one platform message"):
            self.load(self.staging_slack(max_answer_characters=501))


if __name__ == "__main__":
    unittest.main()
