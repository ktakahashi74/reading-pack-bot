# Architecture and trust boundaries

Reading Pack Bot is a narrow serving layer. A separate producer creates and
approves one Reading Pack; this process validates that finished Markdown file
and exposes it through one platform adapter and one model provider.

```text
Reading Pack producer
  canonical data -> build -> release check -> human approval -> Pack Markdown
                                                               |
Reading Pack Bot                                               v
  PackLoader -> BotService -> ModelProvider -> PlatformAdapter
                    |
                 StateStore
```

The direction is one-way. The bot does not mount the producer checkout, read a
manuscript or canonical project data, inspect evaluation records, or repair a
Pack.

## Pack boundary

Before constructing a provider client, the loader:

1. opens one bounded regular file without following a symlink;
2. reads strict UTF-8 with the required line format;
3. computes the SHA-256 of the exact bytes and checks an optional configured
   pin;
4. validates the `PACK` and `ENDPACK` envelopes, the single H1 Pack name,
   and the required `SYS`/`BIB`/`MAP`/`META` order;
5. reads the Pack's self-described metadata.

Production accepts only `status=canonical`. A digest proves byte identity, not
authorship, release approval, or rights clearance; those decisions remain with
the producer and deployment operator.

The verified Pack is trusted application context. Platform messages and
retrieved pages are untrusted input.

## Request flow

```text
Allowed platform mention
  -> route and message policy
  -> event claim and request limits
  -> retained visible thread turns
  -> verified Pack + turns + current question
  -> model provider
  -> bounded reply in the originating conversation
  -> history commit after successful delivery
```

Disallowed routes, automated events, and duplicate events are silent. An event
is claimed before the paid provider call. If generation or delivery then
fails, a repeat delivery of that same event is ignored; the user can send a
new mention.

## Provider boundary

The Anthropic adapter sends the Pack as the first citable plain-text document
with a one-hour prompt-cache marker. Retained visible turns and the current
question follow it; the latest retained turn keeps the default five-minute
cache lifetime. Claude Sonnet 5 uses adaptive thinking at medium effort;
thinking blocks are not rendered.

With hosted web disabled, the OpenAI-compatible adapter places the verified
Pack in a system message and uses the common Chat Completions surface. It sends
no tool or retention parameter. With hosted web enabled, it instead uses the
Responses API with `web_search`, `tool_choice=auto`, a bounded total tool-call
count, and `store=false`. A third-party compatible endpoint therefore needs
Responses web-search support in addition to Chat Completions compatibility.
The configured HTTPS `base_url` is the API-key destination; only a local-stage
loopback endpoint may use plain HTTP or omit a real key.

For Anthropic, hosted web exposes bounded server-side search and fetch tools.
For OpenAI-compatible providers, the hosted `web_search` tool covers search,
page opening, and in-page finding. In both cases the provider handles DNS,
redirects, and target access policy. Retrieved text does not gain application
authority. Provider-returned source URLs are rendered when available, but a
missing citation does not cause an otherwise usable answer to be discarded.

The bot implements no browser, URL allowlist, source classifier, or REF client.

## Slack boundary

The Slack adapter uses Socket Mode and receives only `app_mention`. Every
workspace must match `allowed_installations` exactly. Channel policy has two
modes:

- `allowlist` accepts only IDs in `allowed_channels`;
- `accessible` accepts mentions delivered from channels where the app was
  invited and has access in an allowed workspace.

Neither mode needs channel history, direct-message events, generic message
subscriptions, or `chat:write.public`. An invitation to a Slack Connect channel
can expose the bot to external participants and must be treated as an access
change.

Within one Slack message, the first exact bot mention separates inline context
from the explicit request. Text before the mention is sent to the model as
labeled context, while text after it is used for command recognition and as the
request. When there is no text after the mention, text before it becomes the
request so trailing mentions remain usable. A bare mention displays help.
Commands never send the inline context to the model.

One to four workers may process different threads concurrently. Requests in
the same thread remain serialized through generation, delivery, and history
commit. The queue and locks are process-local, so one Slack installation maps
to one active bot process.

The optional generation status is set immediately before the provider call. It
contains one fixed application string and uses only the validated channel and
thread route. Commands, rejected work, duplicates, rate-limited requests, and
queue waits do not set it. Status failure does not stop the answer.

## Discord boundary

The Discord adapter uses the Gateway with the standard Guilds and Guild
Messages intents. It leaves the privileged Message Content intent disabled and
accepts only messages that explicitly mention the bot. Direct messages, bot
and webhook messages, other servers, and routes outside the configured channel
policy are ignored.

A mention of the managed permission role associated with the bot is not a bot
user mention, even when Discord renders both with the same colored name. The
adapter recognizes only a role whose Discord role tag names the current bot,
posts a local correction, and makes no provider call. Unrelated role mentions
remain silent.

Every server must match `allowed_installations`. By default, explicit mentions
are accepted from every channel the bot can access in an allowed server, with
Discord roles and channel permission overrides as the channel boundary. An
optional channel allowlist can narrow that scope. A thread under an allowlisted
channel inherits that route, while the thread's own channel ID remains the
conversation boundary. One to four workers may process different conversations;
each conversation remains serialized through delivery and history commit.

As on Slack, text before the exact bot mention becomes labeled inline context,
and text after it is the explicit request. A trailing mention uses the text
before it as the request, while a bare mention displays help.

Replies disable all allowed mentions and suppress link embeds. The first chunk
references the triggering message, and each chunk stays within Discord's 2000
character limit. The optional generation indicator uses Discord's typing
context and contains no message content.

## State and logs

The conversation key includes platform, installation, channel, thread, and
Pack SHA-256. Platform identifiers are hashed before SQLite storage. The
database retains visible questions and answers together with event and
rate-limit state. `conversation_ttl_seconds = 0` keeps conversations until a
thread reset or manual deletion; a positive value expires them after that many
seconds. Only the latest configured number of turns is sent back to the model.

Normal logs contain operational fields such as model, Pack hash prefix,
latency, token counts, queue wait, active generation count, status, and random
error ID. They omit Pack text, questions, answers, credentials, and raw
platform IDs.

The provider still receives the complete Pack, current question, and retained
visible turns. Local deletion does not remove platform messages, provider
records, host backups, or filesystem snapshots.

## Process and host boundary

Both the configuration kill switch and `READING_PACK_BOT_DISABLED` stop client
construction. Non-local startup runs permission, dependency, secret, state,
route, Pack, and timeout checks before opening platform or model connections.

The rootless Podman deployment uses a read-only image, drops all capabilities,
publishes no port, and mounts only policy files and state. Container UID 0 maps
to the ordinary host operator, who remains trusted because that account owns
the image, configuration, environment file, Pack, and database.

SIGTERM stops intake, discards queued work, and lets the active request finish
within configured bounds. Configuration checks that one provider call,
optional hosted-tool continuations and generation status, and the final
platform post fit within `TimeoutStopSec=120`.

The design does not protect against a malicious host operator or database
writer, establish provider-side zero retention, verify a cryptographic signer,
or support active-active replicas.
