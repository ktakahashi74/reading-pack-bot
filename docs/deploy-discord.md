# Deploy Reading Pack Bot to Discord

This guide connects one reviewed Reading Pack to one Discord bot through the
Discord Gateway. The bot opens outbound connections to Discord and the model
provider. It does not need a public URL or listening port.

## 1. Create the Discord application

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. Create an application and add a bot.
2. Keep the privileged **Message Content Intent** disabled. The adapter accepts
   only messages that explicitly mention the bot.
3. Under installation settings, enable installation to a server with the
   `bot` scope.
4. Grant only **View Channels**, **Send Messages**, **Read Message History**,
   and **Send Messages in Threads**.
5. Install the app in the selected test server.
6. Reset and copy the bot token into the deployment environment file. Never
   put it in TOML or Git.

Do not grant Administrator, channel-management, message-management, or
mention-everyone permissions. Discord documents the underlying behavior in
[Gateway Intents](https://docs.discord.com/developers/events/gateway#gateway-intents)
and [Message Resource](https://docs.discord.com/developers/resources/message).

## 2. Record the allowed server

Enable Developer Mode in the Discord client, then copy the server ID for
`allowed_installations`.

By default, the bot accepts explicit mentions from every channel it can access
in an allowed server. Discord roles and channel permission overrides remain
the access boundary. The bot ignores direct messages, other servers, messages
from bots or webhooks, and messages without an explicit `@Bot` mention. A
thread keeps its own conversation history. An ordinary text channel has one
shared conversation; use a Discord thread when conversations must be isolated.

Discord may show the bot user and its automatically managed permission role
under the same name, and both mentions are colored. Select the user marked as
an app or bot. If someone selects the bot-managed role, the adapter posts this
correction without sending the empty event to the model. Other role mentions
remain ignored.

## 3. Install the optional adapter

For a Python installation, include the Discord extra and the selected provider:

```sh
python -m pip install -e '.[discord,openai]'
```

Use `anthropic` instead of `openai` when appropriate. The supplied container
image installs the hash-pinned live dependency set, including both platform
adapters.

## 4. Configure the deployment

Start from `deploy/quadlet/config.example.toml`. Keep the service disabled
while editing. A Discord adapter block has this form:

```toml
[policy]
max_question_characters = 4000
max_answer_characters = 2000
requests_per_window = 10
request_window_seconds = 60
daily_requests = 500

[adapter]
kind = "discord"
allowed_installations = ["123456789012345678"]
channel_policy = "accessible"
allowed_channels = []
message_chunk_characters = 2000
queue_size = 1
max_concurrent_generations = 1
post_timeout_seconds = 10
show_generation_status = true
```

To restrict the bot to selected channels instead, set:

```toml
channel_policy = "allowlist"
allowed_channels = ["234567890123456789"]
```

Threads below an allowlisted channel inherit that route.

Discord messages are limited to 2000 characters. Non-local deployments also
require the complete answer to fit one message, `provider.max_retries = 0`, a
SQLite store, and `queue_size = 1`. `show_generation_status` displays Discord's
typing indicator only while an ordinary answer is being generated.

Put the token in the private environment file:

```sh
DISCORD_BOT_TOKEN=replace-with-the-bot-token
READING_PACK_BOT_DISABLED=1
```

Set the environment file mode:

```sh
chmod 600 ~/.config/reading-pack-bot/env
```

Use sections 3 through 7 of the [Slack deployment guide](deploy-slack.md#3-prepare-the-linux-host)
for the Linux host, pinned image, private runtime files, Quadlet installation,
and disabled preflight. Those steps are platform-independent; keep the Discord
configuration and token described above.

## 5. Run preflight and stage the bot

With the disabled container running, verify it before opening a Discord
connection:

```sh
podman exec reading-pack-bot reading-pack-bot verify \
  --config /etc/reading-pack-bot/config.toml
podman exec reading-pack-bot reading-pack-bot doctor \
  --config /etc/reading-pack-bot/config.toml
podman exec reading-pack-bot reading-pack-bot run \
  --config /etc/reading-pack-bot/config.toml --once
```

Then clear both kill switches. Set `runtime.kill_switch = false` in
`config.toml`, and set this value in the private `env` file:

```toml
[runtime]
kill_switch = false
```

```text
READING_PACK_BOT_DISABLED=0
```

Restart the user service and test in one channel the bot can access:

```sh
systemctl --user daemon-reload
systemctl --user restart reading-pack-bot.service
journalctl --user -u reading-pack-bot.service -n 100 --no-pager
```

Check `@Bot help`, one ordinary question, `@Bot status`, and `@Bot reset`.
Generated replies suppress user, role, and everyone mentions and do not expand
links into embeds.

## 6. Stop or roll back

Either kill switch stops platform and model client construction. For an
emergency stop, set `READING_PACK_BOT_DISABLED=1` and restart the service.
SIGTERM stops intake, discards queued work, lets an active answer finish within
the configured shutdown budget, and closes the Gateway client.

Conversation deletion removes local retained context. It does not remove
Discord messages, provider records, backups, or filesystem snapshots. Inform
participants that the complete Pack, their question, and retained visible
turns are sent to the configured model provider.
