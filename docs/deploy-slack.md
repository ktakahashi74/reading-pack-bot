# Deploy Reading Pack Bot to Slack

This runbook installs one Reading Pack Bot on one Linux host with rootless
Podman, Quadlet, and Slack Socket Mode. The bot opens outbound connections to
Slack and the selected model API. It needs no public URL, listening port,
reverse proxy, or TLS certificate.

The checked-in deployment targets Linux x86_64, Podman 4.9 or newer, systemd
with cgroup v2, and CPython 3.12 inside the container. The offline quickstart in
the README also works on macOS; this systemd and Quadlet runbook does not cover
a macOS host.

Keep these values outside Git throughout the installation:

- the Reading Pack used in the deployment;
- Slack `xapp-` and `xoxb-` tokens;
- model API keys;
- Slack workspace and channel IDs;
- the edited runtime configuration.

## 1. Prepare the Reading Pack

Build and check the Pack in its producer project:

```sh
reading-pack validate --project <project>
reading-pack build --project <project> --lang <lang>
reading-pack check --project <project> --lang <lang> --release
```

A person must then select the exact file under `dist/`, confirm its content and
rights, and approve it for deployment. Production accepts only a Pack whose own
header has `status=canonical`. The bot always computes its SHA-256; a deployment
may also pin that value in its configuration.

## 2. Create the Slack app

The repository includes a minimal [Slack App
manifest](../deploy/slack/manifest.yml). In the Slack app administration page:

1. Choose **Create New App**, then **From an app manifest**.
2. Select the intended workspace and enter the checked-in manifest.
3. Under **Basic Information**, generate an app-level token with only
   `connections:write`. Save the resulting `xapp-` token as
   `SLACK_APP_TOKEN`.
4. Under **OAuth & Permissions**, install the app to the workspace. Save the
   resulting `xoxb-` token as `SLACK_BOT_TOKEN`.
5. Invite the app to one private test channel.

The manifest grants only `app_mentions:read` and `chat:write`, subscribes only
to `app_mention`, and enables Socket Mode. It does not grant message history,
direct-message events, `chat:write.public`, or `assistant:write`. The optional
generation status also works with `chat:write`.

Slack documents the relevant surfaces in its [App manifest
reference](https://docs.slack.dev/reference/app-manifest/), [Socket Mode
guide](https://docs.slack.dev/apis/events-api/using-socket-mode/), and
[`assistant.threads.setStatus`
reference](https://docs.slack.dev/reference/methods/assistant.threads.setStatus/).

Record the workspace ID beginning with `T` and the test-channel ID beginning
with `C`. The web client URL normally has the form
`https://app.slack.com/client/T.../C...`. A channel link also contains the
channel ID after `/archives/`.

Inviting the app is an access-control decision. In a Slack Connect channel,
external participants may also be able to use it.

## 3. Prepare the Linux host

Use a dedicated ordinary user for the bot. Install Podman from the operating
system packages and enable lingering so its user-level systemd manager survives
logout. On Ubuntu:

```sh
sudo apt-get update
sudo apt-get install podman
sudo loginctl enable-linger "$USER"
```

Confirm the runtime before continuing:

```sh
podman version
podman info --format '{{.Host.CgroupsVersion}}'
systemctl --user --version
loginctl show-user "$USER" -p Linger
```

The cgroup version must be `v2`, and `Linger=yes` must be present. Do not give
the operator access to a privileged Docker socket; that access is effectively
host-root access.

## 4. Build the pinned image

From the repository root:

```sh
podman build --platform linux/amd64 \
  --tag localhost/reading-pack-bot:0.4.0 .
podman image inspect localhost/reading-pack-bot:0.4.0 \
  --format '{{.Id}} {{.Digest}}'
```

The Dockerfile pins the Python image by index digest. The build and live
dependency locks pin every Python wheel by SHA-256. Updating either lock or the
base digest requires a new dependency and vulnerability review. Never pass a
secret as a build argument, image label, or Dockerfile `ENV` value.

## 5. Install the private runtime files

Create the host directories and copy the examples:

```sh
install -d -m 0700 "$HOME/.config/reading-pack-bot"
install -d -m 0700 "$HOME/.config/reading-pack-bot/packs"
install -d -m 0700 "$HOME/.local/state/reading-pack-bot"
install -m 0600 deploy/quadlet/config.example.toml \
  "$HOME/.config/reading-pack-bot/config.toml"
install -m 0600 deploy/quadlet/env.example \
  "$HOME/.config/reading-pack-bot/env"
install -m 0400 /path/to/approved-reading-pack.md \
  "$HOME/.config/reading-pack-bot/packs/reading-pack.md"
```

Edit `config.toml` while its mode is `0600`. Leave
`runtime.kill_switch=true` until the final activation.

Choose one model provider. For Anthropic:

```toml
[provider]
kind = "anthropic"
model = "claude-sonnet-5"
timeout_seconds = 60.0
max_retries = 0
max_output_tokens = 4096
```

Provider-hosted web access is optional. Keep it disabled until its data path
and organization access policy have been reviewed:

```toml
[web]
enabled = false
max_search_uses = 8
max_fetch_uses = 5
max_pause_continuations = 1
max_content_tokens = 20000
```

For OpenAI or another Chat Completions-compatible API:

```toml
[provider]
kind = "openai-compatible"
model = "provider-model-id"
timeout_seconds = 60.0
max_retries = 0
max_output_tokens = 4096
# Omit both fields for the OpenAI API.
# base_url = "https://models.example.org/v1"
# api_key_env = "MODEL_API_KEY"
```

The configured `base_url` receives the API key. Remote endpoints must use
HTTPS. Verify the endpoint independently before placing a key in the
environment file. When `[web].enabled=true`, this adapter uses the Responses
API `web_search` tool rather than Chat Completions and sends `store=false`.
OpenAI supports this surface; another compatible endpoint and selected model
must support it explicitly. The sum of `max_search_uses` and `max_fetch_uses`
sets `max_tool_calls`; `max_pause_continuations` and `max_content_tokens` apply
only to Anthropic.

Configure Slack for the test workspace. `accessible` makes each app invitation
the channel opt-in:

```toml
[adapter]
kind = "slack"
allowed_installations = ["T01234567"]
channel_policy = "accessible"
allowed_channels = []
message_chunk_characters = 3500
queue_size = 1
max_concurrent_generations = 1
post_timeout_seconds = 10
show_generation_status = true
```

For a fixed channel list, use:

```toml
channel_policy = "allowlist"
allowed_channels = ["C01234567"]
```

Every workspace is always explicitly allowlisted. `accessible` does not read
all workspace messages; Slack delivers only mentions from channels where the
app was invited.

Put the two Slack tokens and the selected model key in `env`. Keep the
environment kill switch active:

```sh
READING_PACK_BOT_DISABLED=1
SLACK_APP_TOKEN=replace-with-app-level-token
SLACK_BOT_TOKEN=replace-with-bot-token
ANTHROPIC_API_KEY=replace-me
```

Use `OPENAI_API_KEY` for the OpenAI API or the name selected by
`api_key_env` for another compatible endpoint. Remove unused empty secret
entries, then make the configuration read-only:

```sh
chmod 0400 "$HOME/.config/reading-pack-bot/config.toml"
chmod 0600 "$HOME/.config/reading-pack-bot/env"
```

## 6. Install Quadlet and start disabled

```sh
install -d -m 0700 "$HOME/.config/containers/systemd"
install -d -m 0700 "$HOME/.config/systemd/user"
install -m 0600 deploy/quadlet/reading-pack-bot.container \
  deploy/quadlet/reading-pack-bot-purge.container \
  "$HOME/.config/containers/systemd/"
install -m 0600 deploy/quadlet/reading-pack-bot-purge.timer \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start reading-pack-bot.service
```

The main Quadlet uses a read-only root filesystem, drops all capabilities,
publishes no port, and mounts only the configuration, Pack directory, and state
directory. Container UID 0 maps to the unprivileged host operator through the
rootless user namespace; it is not host root.

## 7. Verify before connecting

The disabled process remains available for inspection without constructing a
Slack or model client:

```sh
systemctl --user status reading-pack-bot.service --no-pager
journalctl --user -u reading-pack-bot.service --since today --no-pager
podman exec reading-pack-bot reading-pack-bot verify \
  --config /etc/reading-pack-bot/config.toml
podman exec reading-pack-bot reading-pack-bot doctor \
  --config /etc/reading-pack-bot/config.toml
podman exec reading-pack-bot reading-pack-bot run \
  --config /etc/reading-pack-bot/config.toml --once
podman inspect reading-pack-bot --format \
  'readonly={{.HostConfig.ReadonlyRootfs}} network={{.HostConfig.NetworkMode}} user={{.Config.User}}'
podman top reading-pack-bot user huser capeff
```

Resolve every `FAIL`. Confirm the selected Pack version, status, and SHA-256;
the `T...` and optional `C...` IDs; private file modes; provider destination;
model; answer bound; and both kill switches. A warning that hosted web is
disabled is expected when that feature is not needed.

The journal must not contain a Pack, question, answer, token, or raw Slack ID.

## 8. Enable one test channel

First set `runtime.kill_switch=false` in `config.toml`, return the file to mode
`0400`, and restart. The environment override still prevents external clients.
Run the checks above once more.

Then change `READING_PACK_BOT_DISABLED=0` in `env` and restart:

```sh
systemctl --user restart reading-pack-bot.service
systemctl --user status reading-pack-bot.service --no-pager
journalctl --user -u reading-pack-bot.service -f
```

In the invited channel, check these paths in order:

1. `@Reading Pack Bot status` returns the Pack and model identity without a
   model call.
2. `@Reading Pack Bot help` lists the commands without a model call.
3. A message containing introductory text followed by a new line with
   `@Reading Pack Bot help` also lists the commands without a model call.
4. Introductory text followed by `@Reading Pack Bot <question>` sends the text
   before the mention as labeled context and the text after it as the request.
5. One ordinary question receives one threaded answer.
6. A second question in that thread uses the visible conversation context.
7. `@Reading Pack Bot reset` removes the bot's retained context for that
   thread.
8. If `show_generation_status=true`, the fixed waiting status appears only
   while an ordinary answer is being generated and clears after the post.

Pin a notice in every enabled channel: the Pack, mention, and retained visible
thread context are sent to the configured model provider. Participants must not
post secrets, personal data, unpublished material, or content they are not
authorized to share. Set `store.conversation_ttl_seconds = 0` only when the
operator intends to retain conversations until reset or manual deletion. Local
deletion does not remove Slack messages, provider records, or backups.

After the staging test, set `runtime.stage="production"` only with an approved
canonical Pack. Invite the app to additional channels one at a time.

## 9. Operate and stop the service

Enable the daily state purge after the first successful test:

```sh
systemctl --user start reading-pack-bot-purge.service
systemctl --user show reading-pack-bot-purge.service -p Result -p ExecMainStatus
systemctl --user enable --now reading-pack-bot-purge.timer
```

Routine commands are:

```sh
systemctl --user status reading-pack-bot.service --no-pager
journalctl --user -u reading-pack-bot.service --since today --no-pager
systemctl --user restart reading-pack-bot.service
systemctl --user stop reading-pack-bot.service
```

For an emergency stop, stop the service, restore
`READING_PACK_BOT_DISABLED=1`, and leave it stopped until the incident has been
reviewed.

Start with one generation worker. If measured queue waits justify more
parallelism, raise `max_concurrent_generations` gradually to at most four while
keeping `queue_size=1`. Requests in one Slack thread remain serialized. Run
only one active bot process for a Slack installation; the queue and thread
locks are process-local.

## Upgrade and rollback

Use a new immutable image tag for every release. Stop the bot and purge timer,
build and verify the new image, update `Image=` in both Quadlets, restore both
kill switches, reload systemd, and start disabled. Re-run the complete preflight
before enabling it.

Rollback restores both Quadlets to the prior immutable tag. Back up the SQLite
state only while the bot and purge service are stopped, and restore that backup
only when the release changed its state schema. Do not use `latest`, automatic
image updates, or two active instances against one Slack installation.

## Troubleshooting

- **The service exits with status 2:** configuration or deployment preflight
  failed. Run `doctor` inside the disabled container and inspect its `FAIL`
  lines.
- **The bot does not answer:** check both kill switches, the two Slack tokens,
  workspace and channel IDs, the app invitation, the `app_mention`
  subscription, and the `journalctl` connection result.
- **Slack reports `missing_scope`:** reinstall the app after confirming
  `app_mentions:read` and `chat:write`. The app-level token separately needs
  `connections:write`.
- **The answer posts but no waiting status appears:** confirm
  `show_generation_status=true`, the current Slack SDK, and `chat:write`. Status
  failure does not stop answer generation.
- **Configuration rejects the timeouts:** keep provider timeout at 60 seconds
  or less, Slack post timeout at 10 seconds or less, provider retries at zero,
  and answers within one Slack message. Hosted-web continuations and generation
  status must also fit the 90-second service stop budget.
