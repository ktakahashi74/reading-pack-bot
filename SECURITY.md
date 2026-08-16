# Security policy

## Supported status

This project is pre-1.0. Only the current development line receives security
fixes. Reading Pack format compatibility may change while the upstream
specification remains draft.

## Reporting

Use GitHub private vulnerability reporting. Do not open a public issue
containing credentials, private Pack content, platform identifiers,
transcripts, or working attack strings.

## Security boundary

A Reading Pack SYS section is quality guidance, not access control. The primary
controls are information minimization, one bounded read-only artifact, strict
format validation, provider/platform role separation, default-deny Slack
routes, bounded retention, and regression tests. The bot always computes the
artifact SHA-256 and can optionally compare it with an operator-supplied pin.

Optional web access uses provider-hosted server tools. The bot does not fetch
target sites, resolve their DNS, follow redirects, or classify source
authority. URL validation and target access restrictions are provider API and
organization-policy responsibilities. Anthropic requests separately bound
search, fetch, fetched tokens, and server-tool continuations. OpenAI-compatible
requests bound the total Responses API built-in tool calls; third-party
endpoints must implement that surface themselves.

Retrieved pages are untrusted model input; page instructions have no
application authority. This is enforced by the model instruction and provider
tool boundary, not by inspecting fetched content locally. Enabling hosted web
sends search queries, pages, and source metadata through the selected provider.
Do not enable it for material that must not enter that processing path.

The bot does not establish:

- that the rights holder approved a pack;
- that `check --release` was run;
- cryptographic signer identity;
- protection from a malicious host administrator or database writer;
- confidentiality beyond the selected external provider's current terms.

In the preferred rootless Podman deployment, container UID 0 maps to the
ordinary operator account rather than host root. The container drops all
capabilities and mounts only policy files read-only plus its state directory
read-write. The operator account is nevertheless trusted: it owns the local
image, Quadlet, configuration, environment file, and state and can replace
them. Do not run unrelated or untrusted workloads under that account.

## Secret handling

Tokens are accepted only through environment variables or an external secret
facility. Never pass them as CLI arguments, bake them into an image, store them
in TOML, or print them. Known environment, database, log, and local-config
names are excluded from Git, and the public-boundary audit checks tracked files
for common mistakes. This audit is heuristic: arbitrary transcript filenames
or novel secret formats require a human review before publication.

Rotate a credential immediately if it reaches a shell transcript, Git object,
CI log, or public issue. Stop the service and activate
`READING_PACK_BOT_DISABLED=1` before rotation.

For an OpenAI-compatible provider, `base_url` is the destination that receives
the configured API key. Configuration rejects credentials, query strings, and
fragments in that URL and requires HTTPS except for a local-stage literal
loopback endpoint. An empty API-key environment name is likewise restricted to
local loopback. The configuration file and its operator remain trusted; verify
the endpoint independently before enabling the service.

Optional Slack generation status sends only a fixed application string plus
the validated destination channel and thread. It never includes the question,
answer, Pack, provider metadata, URLs, token usage, credentials, or internal
error IDs. Status API failures log only the operation and exception type.

Slack workspace IDs remain exact-allowlisted in every live routing mode. The
optional `joined` channel policy does not grant `chat:write.public` or subscribe
to channel history: it accepts only `app_mention` events delivered by Slack for
channels where operators invited the app. This expands the operational surface
from a static channel list to invitation management, so channel invitation and
removal must be treated as access-control changes.
Inviting the app to a Slack Connect channel may expose it to external channel
members. This bot does not request or inspect channel metadata, so operators
must exclude unapproved shared channels.

Generation concurrency is process-local and bounded to four workers. It is a
capacity control, not a distributed exclusion mechanism; do not run multiple
active bot processes against one Slack installation in the initial design.
Rate limits and the deployment-wide daily request bound still apply when
parallelism is greater than one.

## Data handling

The complete pack, each question, and retained visible conversation turns are
sent to the configured model provider. When hosted web is enabled, the provider
also processes search queries, fetched pages, and their source metadata in the
same request. Visible turns are also kept in local
SQLite for the configured TTL. Normal application logs exclude message bodies.
With web disabled, the OpenAI-compatible adapter uses Chat Completions and does
not assume a portable retention-control parameter. With web enabled, it uses
Responses and sends `store=false`; this does not by itself establish Zero Data
Retention. The Anthropic adapter uses an ephemeral prompt-cache marker on the
citable Pack document. Operators must independently review the selected
compatible provider's terms, or the current
[OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data#data-retention-controls-for-abuse-monitoring)
or [Anthropic retention terms](https://privacy.anthropic.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data),
training, region, and confidentiality terms before using a real pack. Database
backups and filesystem snapshots require their own expiry policy.

## Dependency and CI policy

Core tests are offline and use only a CC0 synthetic pack, fake provider, and
mocked platform client. CI must not receive production secrets or make live
Slack or model-provider calls. The build toolchain, direct integrations, and every
transitive live dependency are version- and hash-pinned for the documented
Linux x86_64 deployment. The container base image is pinned by index digest.
Refresh locks and the image digest only as an explicit dependency and
vulnerability review.
