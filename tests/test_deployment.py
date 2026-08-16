from __future__ import annotations

import unittest
from pathlib import Path

from reading_pack_bot.config import SERVICE_TIMEOUT_STOP_SECONDS
from reading_pack_bot.config import load_config
from reading_pack_bot.models import __version__


ROOT = Path(__file__).resolve().parents[1]


class QuadletDeploymentTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return (ROOT / "deploy" / "quadlet" / name).read_text(encoding="utf-8")

    def test_main_container_is_network_capable_and_hardened(self):
        unit = self.text("reading-pack-bot.container")
        for setting in (
            f"Image=localhost/reading-pack-bot:{__version__}",
            "Pull=never",
            "User=0",
            "ReadOnly=true",
            "DropCapability=all",
            "NoNewPrivileges=true",
            "PidsLimit=64",
            "EnvironmentFile=%h/.config/reading-pack-bot/env",
            "Volume=%h/.config/reading-pack-bot/env:/etc/reading-pack-bot/env:ro",
            "Volume=%h/.config/reading-pack-bot/config.toml:/etc/reading-pack-bot/config.toml:ro",
            "Volume=%h/.config/reading-pack-bot/packs:/etc/reading-pack-bot/packs:ro",
            f"TimeoutStopSec={SERVICE_TIMEOUT_STOP_SECONDS}s",
        ):
            self.assertIn(setting, unit)
        self.assertNotIn("PublishPort=", unit)
        self.assertNotIn("AutoUpdate=", unit)
        self.assertNotIn("Network=none", unit)
        self.assertNotIn(
            "Volume=%h/.config/reading-pack-bot:/etc/reading-pack-bot:ro", unit
        )

    def test_purge_container_has_no_network_or_secret_injection(self):
        unit = self.text("reading-pack-bot-purge.container")
        self.assertIn("Network=none", unit)
        self.assertIn("ReadOnly=true", unit)
        self.assertIn("DropCapability=all", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("PidsLimit=32", unit)
        self.assertIn("TasksMax=64", unit)
        self.assertIn(
            "Volume=%h/.config/reading-pack-bot/config.toml:/etc/reading-pack-bot/config.toml:ro",
            unit,
        )
        self.assertNotIn("/etc/reading-pack-bot/packs", unit)
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn(
            "Volume=%h/.config/reading-pack-bot:/etc/reading-pack-bot:ro", unit
        )
        self.assertNotIn("PublishPort=", unit)

    def test_image_contains_no_runtime_content(self):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(ignored[0], "**")
        for entry in (
            "!pyproject.toml",
            "!README.md",
            "!requirements-build.lock",
            "!requirements-live-linux-amd64.lock",
            "!src/",
            "!src/**",
        ):
            self.assertIn(entry, ignored)
        for unsafe_entry in ("!tests/", "!docs/", "!deploy/", "!.env"):
            self.assertNotIn(unsafe_entry, ignored)

    def test_only_quadlet_deployment_is_shipped(self):
        self.assertTrue((ROOT / "deploy" / "quadlet" / "config.example.toml").is_file())
        self.assertFalse((ROOT / "deploy" / "systemd").exists())
        self.assertFalse((ROOT / "docs" / "deployment-systemd.md").exists())

    def test_deployment_example_starts_disabled_with_slack_safe_bounds(self):
        config = load_config(ROOT / "deploy" / "quadlet" / "config.example.toml")
        self.assertEqual(config.runtime.stage, "staging")
        self.assertTrue(config.runtime.kill_switch)
        self.assertEqual(config.provider.max_retries, 0)
        self.assertEqual(config.adapter.kind, "disabled")
        self.assertEqual(config.adapter.queue_size, 1)

    def test_slack_manifest_requests_only_the_required_surface(self):
        manifest = (ROOT / "deploy" / "slack" / "manifest.yml").read_text(
            encoding="utf-8"
        )
        for setting in (
            "app_mentions:read",
            "chat:write",
            "app_mention",
            "socket_mode_enabled: true",
        ):
            self.assertIn(setting, manifest)
        for forbidden in (
            "chat:write.public",
            "assistant:write",
            "channels:history",
            "groups:history",
            "im:history",
            "message.im",
        ):
            self.assertNotIn(forbidden, manifest)

    def test_release_version_is_consistent_across_build_and_deployment(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"reading-pack-bot=={__version__}", dockerfile)
        self.assertIn(
            f"reading_pack_bot-{__version__}-py3-none-any.whl",
            workflow,
        )
        for unit_name in (
            "reading-pack-bot.container",
            "reading-pack-bot-purge.container",
        ):
            self.assertIn(
                f"Image=localhost/reading-pack-bot:{__version__}",
                self.text(unit_name),
            )


if __name__ == "__main__":
    unittest.main()
