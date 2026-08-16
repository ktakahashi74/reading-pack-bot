from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reading_pack_bot.config import load_config
from reading_pack_bot.doctor import (
    _private_secret_file,
    _private_state_file,
    _secure_policy_file,
    run_checks,
)
from tests.helpers import FIXTURE_SHA256, config_text


class DoctorPermissionTests(unittest.TestCase):
    @staticmethod
    def result(mode, owner=0):
        return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner)

    def test_root_group_read_only_modes_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.toml"
            path.write_text("policy", encoding="utf-8")
            for mode in (0o440, 0o640, 0o600):
                with patch.object(Path, "lstat", return_value=self.result(mode)):
                    self.assertTrue(_secure_policy_file(path), oct(mode))

    def test_group_write_or_world_access_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.toml"
            path.write_text("policy", encoding="utf-8")
            for mode in (0o660, 0o644, 0o604):
                with patch.object(Path, "lstat", return_value=self.result(mode)):
                    self.assertFalse(_secure_policy_file(path), oct(mode))

    def test_non_root_owner_is_rejected(self):
        with patch.object(Path, "lstat", return_value=self.result(0o440, owner=1000)):
            self.assertFalse(_secure_policy_file(Path("/etc/reading-pack-bot/config.toml")))

    def test_private_state_requires_exact_mode_and_current_owner(self):
        owner = os.geteuid()
        with patch.object(Path, "lstat", return_value=self.result(0o600, owner=owner)):
            self.assertTrue(_private_state_file(Path("state.sqlite3")))
        with patch.object(Path, "lstat", return_value=self.result(0o640, owner=owner)):
            self.assertFalse(_private_state_file(Path("state.sqlite3")))
        with patch.object(Path, "lstat", return_value=self.result(0o600, owner=owner + 1)):
            self.assertFalse(_private_state_file(Path("state.sqlite3")))

    def test_secret_file_requires_root_and_exact_mode(self):
        with patch.object(Path, "lstat", return_value=self.result(0o600, owner=0)):
            self.assertTrue(_private_secret_file(Path("env")))
        with patch.object(Path, "lstat", return_value=self.result(0o640, owner=0)):
            self.assertFalse(_private_secret_file(Path("env")))

    def test_pack_check_reports_computed_or_pinned_hash(self):
        cases = ((None, "computed_sha256="), (FIXTURE_SHA256, "pinned_sha256="))
        for pack_hash, expected in cases:
            with self.subTest(pack_hash=pack_hash):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "config.toml"
                    path.write_text(
                        config_text(pack_hash=pack_hash), encoding="utf-8"
                    )
                    config = load_config(path)
                    check = {item.name: item for item in run_checks(config)}["pack"]
                self.assertTrue(check.ok)
                self.assertIn(expected, check.detail)
        with patch.object(Path, "lstat", return_value=self.result(0o600, owner=1000)):
            self.assertFalse(_private_secret_file(Path("env")))

    def test_anthropic_package_and_secret_are_checked_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(provider="anthropic", model="claude-sonnet-5"),
                encoding="utf-8",
            )
            config = load_config(path)
            with patch("reading_pack_bot.doctor.importlib.util.find_spec", return_value=object()), \
                 patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
                checks = {check.name: check for check in run_checks(config)}
        self.assertTrue(checks["anthropic_package"].ok)
        self.assertTrue(checks["anthropic_secret"].ok)
        self.assertEqual(checks["model"].detail, "claude-sonnet-5")

    def test_openai_compatible_package_endpoint_and_custom_secret_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(
                    provider="openai-compatible",
                    model="provider/model-v1",
                    base_url="https://models.example.org/v1",
                    api_key_env="MODEL_API_KEY",
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ) as find_spec, patch.dict(
                os.environ, {"MODEL_API_KEY": "test-key"}, clear=True
            ):
                checks = {check.name: check for check in run_checks(config)}
        find_spec.assert_any_call("openai")
        self.assertTrue(checks["openai_compatible_package"].ok)
        self.assertTrue(checks["openai_compatible_secret"].ok)
        self.assertTrue(checks["openai_compatible_endpoint"].ok)
        self.assertNotIn("models.example.org", checks["openai_compatible_endpoint"].detail)

    def test_openai_compatible_web_check_reports_responses_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(
                    provider="openai-compatible",
                    model="gpt-test-2026-01-01",
                    web_enabled=True,
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
                check = {item.name: item for item in run_checks(config)}["web"]
        self.assertTrue(check.ok)
        self.assertIn("Responses web_search", check.detail)
        self.assertIn("max_tool_calls=13", check.detail)

    def test_local_loopback_doctor_accepts_absent_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(
                    provider="openai-compatible",
                    model="local-model",
                    base_url="http://localhost:11434/v1",
                    api_key_env="",
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ), patch.dict(os.environ, {}, clear=True):
                checks = {check.name: check for check in run_checks(config)}
        self.assertTrue(checks["openai_compatible_secret"].ok)
        self.assertIn("local loopback", checks["openai_compatible_secret"].detail)

    def test_generation_status_sdk_method_is_checked_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(show_generation_status=True), encoding="utf-8"
            )
            config = load_config(path)

            class SupportedClient:
                def assistant_threads_setStatus(self):
                    return None

            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ), patch(
                "reading_pack_bot.doctor.importlib.import_module",
                return_value=SimpleNamespace(WebClient=SupportedClient),
            ):
                checks = {check.name: check for check in run_checks(config)}
        self.assertTrue(checks["slack_generation_status"].ok)

    def test_generation_status_fails_doctor_when_sdk_method_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(show_generation_status=True), encoding="utf-8"
            )
            config = load_config(path)
            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ), patch(
                "reading_pack_bot.doctor.importlib.import_module",
                return_value=SimpleNamespace(WebClient=object),
            ):
                checks = {check.name: check for check in run_checks(config)}
        self.assertFalse(checks["slack_generation_status"].ok)

    def test_joined_channel_policy_is_reported_as_configured_routing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                config_text(channel_policy="joined", channels=""),
                encoding="utf-8",
            )
            config = load_config(path)
            with patch(
                "reading_pack_bot.doctor.importlib.util.find_spec",
                return_value=object(),
            ):
                checks = {check.name: check for check in run_checks(config)}
        self.assertTrue(checks["allowlist"].ok)
        self.assertIn("channel_policy=joined", checks["allowlist"].detail)


if __name__ == "__main__":
    unittest.main()
