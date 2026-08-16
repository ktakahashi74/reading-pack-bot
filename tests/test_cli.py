from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reading_pack_bot.cli import main
from reading_pack_bot.errors import ConfigurationError
from reading_pack_bot.models import GenerationResult
from tests.helpers import config_text


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def config(self, **kwargs):
        path = Path(self.temporary.name) / f"config-{len(list(Path(self.temporary.name).glob('config-*')))}.toml"
        path.write_text(config_text(**kwargs), encoding="utf-8")
        return path

    def call(self, argv, *, stdin=""):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_verify(self):
        code, stdout, stderr = self.call(["verify", "--config", str(self.config())])
        self.assertEqual(code, 0, stderr)
        self.assertIn("status=canonical", stdout)
        self.assertIn("sha256=", stdout)

    def test_doctor_local_fake(self):
        code, stdout, stderr = self.call(["doctor", "--config", str(self.config(adapter="disabled", workspaces="", channels=""))])
        self.assertEqual(code, 0, stderr)
        self.assertIn("pack", stdout)
        self.assertIn("no network", stdout)

    def test_ask_fake(self):
        code, stdout, stderr = self.call(
            [
                "ask",
                "--config",
                str(self.config(adapter="disabled", workspaces="", channels="")),
            ],
            stdin="hello\n",
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("hello", stdout)

    def test_ask_rejects_empty_or_oversize_stdin(self):
        path = self.config(adapter="disabled", workspaces="", channels="")
        code, _stdout, stderr = self.call(["ask", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("question is empty", stderr)
        code, _stdout, stderr = self.call(
            ["ask", "--config", str(path)],
            stdin="x" * 201,
        )
        self.assertEqual(code, 2)
        self.assertIn("character limit", stderr)

    def test_ask_rejects_retired_question_argument(self):
        path = self.config(adapter="disabled", workspaces="", channels="")
        with self.assertRaises(SystemExit):
            self.call(
                ["ask", "--config", str(path), "--question", "private"]
            )

    def test_run_once_preflights_without_network(self):
        path = self.config(adapter="disabled", workspaces="", channels="", kill_switch=True)
        with patch.dict("os.environ", {"READING_PACK_BOT_DISABLED": "1"}):
            code, _stdout, stderr = self.call(["run", "--config", str(path), "--once"])
        self.assertEqual(code, 0, stderr)

    def test_nonlocal_run_fails_closed_on_deployment_preflight(self):
        path = self.config(
            stage="staging",
            adapter="disabled",
            workspaces="",
            channels="",
            store="sqlite",
            store_path=str(Path(self.temporary.name) / "state.sqlite3"),
            kill_switch=True,
        )
        failed = SimpleNamespace(name="config_permissions", ok=False)
        with (
            patch.dict("os.environ", {"READING_PACK_BOT_DISABLED": "1"}),
            patch("reading_pack_bot.cli.run_checks", return_value=(failed,)),
        ):
            code, _stdout, stderr = self.call(["run", "--config", str(path), "--once"])
        self.assertEqual(code, 2)
        self.assertIn("deployment preflight failed: config_permissions", stderr)

    def test_slack_startup_error_is_clean_non_restart_exit(self):
        path = self.config()
        with (
            patch.dict(
                "os.environ",
                {
                    "READING_PACK_BOT_DISABLED": "",
                    "SLACK_BOT_TOKEN": "bot-placeholder",
                    "SLACK_APP_TOKEN": "app-placeholder",
                },
            ),
            patch(
                "reading_pack_bot.cli.SlackAdapter.run",
                side_effect=ConfigurationError("cannot connect Slack adapter (SDKError)"),
            ),
        ):
            code, _stdout, stderr = self.call(["run", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("SDKError", stderr)

    def test_discord_startup_error_is_clean_non_restart_exit(self):
        path = self.config(
            adapter="discord",
            workspaces='"G1"',
            channels='"C1"',
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "READING_PACK_BOT_DISABLED": "",
                    "DISCORD_BOT_TOKEN": "token-placeholder",
                },
            ),
            patch(
                "reading_pack_bot.cli.DiscordAdapter.run",
                side_effect=ConfigurationError(
                    "cannot connect Discord adapter (SDKError)"
                ),
            ),
        ):
            code, _stdout, stderr = self.call(["run", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("SDKError", stderr)

    def test_disabled_openai_run_once_does_not_construct_provider(self):
        path = self.config(
            provider="openai",
            model="gpt-test-2026-01-01",
            adapter="disabled",
            workspaces="",
            channels="",
            kill_switch=True,
        )
        with patch.dict("os.environ", {"READING_PACK_BOT_DISABLED": "1", "OPENAI_API_KEY": ""}), \
             patch("reading_pack_bot.cli.create_provider", side_effect=AssertionError("must stay offline")):
            code, _stdout, stderr = self.call(["run", "--config", str(path), "--once"])
        self.assertEqual(code, 0, stderr)

    def test_purge_sqlite(self):
        database = Path(self.temporary.name) / "state.sqlite3"
        path = self.config(
            adapter="disabled",
            workspaces="",
            channels="",
            store="sqlite",
            store_path=str(database),
        )
        code, stdout, stderr = self.call(["purge", "--config", str(path)])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(database.exists())
        self.assertIn("purged", stdout)

    def test_live_provider_requires_explicit_flag(self):
        path = self.config(
            provider="openai",
            model="gpt-test-2026-01-01",
            adapter="disabled",
            workspaces="",
            channels="",
        )
        code, _stdout, stderr = self.call(["ask", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("--allow-live", stderr)

    def test_live_ask_passes_question_unchanged_to_provider(self):
        path = self.config(
            provider="anthropic",
            model="claude-sonnet-5",
            web_enabled=True,
            adapter="disabled",
            workspaces="",
            channels="",
        )
        provider = Mock()
        provider.generate.return_value = GenerationResult(
            text="answer", model="claude-sonnet-5"
        )
        with (
            patch.dict(
                "os.environ",
                {"READING_PACK_BOT_DISABLED": "", "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ),
            patch(
                "reading_pack_bot.cli.create_provider", return_value=provider
            ) as provider_factory,
        ):
            code, stdout, stderr = self.call(
                [
                    "ask",
                    "--config",
                    str(path),
                    "--allow-live",
                ],
                stdin="external evidence\n",
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn("answer", stdout)
        request = provider.generate.call_args.args[0]
        self.assertEqual(request.current_question, "external evidence")
        provider_factory.assert_called_once()

    def test_live_ask_kill_switch_stops_before_provider_construction(self):
        path = self.config(
            provider="openai",
            model="gpt-test-2026-01-01",
            adapter="disabled",
            workspaces="",
            channels="",
            kill_switch=True,
        )
        with patch("reading_pack_bot.cli.create_provider", side_effect=AssertionError("must stay offline")):
            code, _stdout, stderr = self.call([
                "ask", "--config", str(path), "--allow-live"
            ])
        self.assertEqual(code, 2)
        self.assertIn("kill switch", stderr)

    def test_live_ask_environment_kill_switch_stops_before_provider_construction(self):
        path = self.config(
            provider="openai",
            model="gpt-test-2026-01-01",
            adapter="disabled",
            workspaces="",
            channels="",
        )
        with patch.dict("os.environ", {"READING_PACK_BOT_DISABLED": "1"}), \
             patch("reading_pack_bot.cli.create_provider", side_effect=AssertionError("must stay offline")):
            code, _stdout, stderr = self.call([
                "ask", "--config", str(path), "--allow-live"
            ])
        self.assertEqual(code, 2)
        self.assertIn("kill switch", stderr)

    def test_ask_neutralizes_terminal_control_sequences(self):
        path = self.config(adapter="disabled", workspaces="", channels="")
        provider = Mock()
        provider.generate.return_value = GenerationResult(
            text="safe\x1b[2J\rtitle\x9b31m", model="fake"
        )
        with patch("reading_pack_bot.cli.create_provider", return_value=provider):
            code, stdout, stderr = self.call(
                ["ask", "--config", str(path)],
                stdin="question\n",
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "safe\\x1b[2J\\x0dtitle\\x9b31m\n")
        self.assertNotIn("\x1b", stdout)

    def test_configuration_error_has_no_file_contents(self):
        path = Path(self.temporary.name) / "bad.toml"
        path.write_text("SECRET malformed", encoding="utf-8")
        code, _stdout, stderr = self.call(["verify", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET", stderr)

    def test_transient_operating_error_uses_restartable_exit_code(self):
        path = self.config(adapter="disabled", workspaces="", channels="")
        with patch("reading_pack_bot.cli.verified_pack", side_effect=OSError("temporary")):
            code, _stdout, stderr = self.call(["verify", "--config", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("temporary", stderr)


if __name__ == "__main__":
    unittest.main()
