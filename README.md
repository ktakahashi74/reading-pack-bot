# Reading Pack Bot

[日本語](README.ja.md)

Reading Pack Bot is a server for publishing a
[Reading Pack](https://github.com/ktakahashi74/reading-pack) as a
conversational interface. See the linked project for the Reading Pack format,
authoring workflow, and release checks.

The core is independent of any particular chat service. This release includes
a Slack adapter. Model connections support the Anthropic API and
OpenAI-compatible APIs. Both model adapters can use provider-hosted web
retrieval when enabled and supported by the endpoint.

The project is alpha software. Configuration and Reading Pack compatibility
may change before 1.0.

## Try it locally

Python 3.11 or newer is required. The bundled test provider and synthetic
Reading Pack make no model or Slack request.

```sh
git clone https://github.com/ktakahashi74/reading-pack-bot.git
cd reading-pack-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp config.example.toml config.local.toml
reading-pack-bot verify --config config.local.toml
reading-pack-bot doctor --config config.local.toml
reading-pack-bot ask --config config.local.toml
```

`ask` reads one question from standard input. Type the question and press
Ctrl-D.

## Deploy to Slack

Follow [Deploy Reading Pack Bot to Slack](docs/deploy-slack.md) to publish a
real Reading Pack. The guide covers Pack selection, Slack app creation, model
configuration, container installation, preflight checks, upgrades, and
rollback.

## Commands

| Command | Purpose |
| --- | --- |
| `verify` | Validate the configuration and Reading Pack and report its SHA-256. |
| `doctor` | Check dependencies, secrets, permissions, storage, routing, and kill switches. |
| `ask` | Send one question; a live provider requires `--allow-live`. |
| `run` | Run the selected adapter, or perform preflight only with `--once`. |
| `purge` | Delete expired conversation, event, and rate-limit state. |

Run `reading-pack-bot <command> --help` for command-line options.

## Data and security

Each model request includes the complete Reading Pack, the current question,
and retained conversation context. Read the [security policy](SECURITY.md)
before using private material. The system boundaries are documented in
[Architecture and trust boundaries](docs/architecture.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and public-content
rules.

```sh
sh scripts/test-suite.sh
```

## License

Code is MIT. Documentation and configuration examples are CC BY 4.0. See the
[license map](LICENSES/README.md). These licenses do not apply to a book or
Reading Pack used in a deployment.
