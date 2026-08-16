from __future__ import annotations

import hashlib
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "clockwork-garden-reading-pack.en.md"
FIXTURE_SHA256 = "d16280ea15f1e516be157b31547bf21d8991444e78a1e94cd12b83f14ac75c4d"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_text(
    *,
    pack_path: Path = FIXTURE,
    pack_hash: str | None = FIXTURE_SHA256,
    stage: str = "local",
    kill_switch: bool = False,
    provider: str = "fake",
    model: str = "",
    base_url: str | None = None,
    api_key_env: str | None = None,
    store: str = "memory",
    store_path: str = "",
    adapter: str = "slack",
    workspaces: str = '"T1"',
    channel_policy: str = "allowlist",
    channels: str = '"C1"',
    requests_per_window: int = 10,
    daily_requests: int = 500,
    provider_timeout: float = 30.0,
    max_retries: int = 1,
    web_enabled: bool = False,
    max_web_searches: int = 8,
    max_web_fetches: int = 5,
    max_web_pause_continuations: int = 1,
    max_answer_characters: int = 500,
    message_chunk_characters: int = 500,
    queue_size: int = 4,
    max_concurrent_generations: int = 1,
    post_timeout_seconds: int = 10,
    show_generation_status: bool = False,
) -> str:
    pack_hash_line = f'sha256 = "{pack_hash}"\n' if pack_hash is not None else ""
    provider_connection = ""
    if base_url is not None:
        provider_connection += f'base_url = "{base_url}"\n'
    if api_key_env is not None:
        provider_connection += f'api_key_env = "{api_key_env}"\n'
    return f'''schema_version = 3
[runtime]
stage = "{stage}"
kill_switch = {str(kill_switch).lower()}
log_level = "INFO"
[pack]
path = "{pack_path}"
{pack_hash_line}max_bytes = 524288
[provider]
kind = "{provider}"
model = "{model}"
{provider_connection}timeout_seconds = {provider_timeout}
max_retries = {max_retries}
max_output_tokens = 800
[web]
enabled = {str(web_enabled).lower()}
max_search_uses = {max_web_searches}
max_fetch_uses = {max_web_fetches}
max_pause_continuations = {max_web_pause_continuations}
max_content_tokens = 20000
[store]
kind = "{store}"
path = "{store_path}"
history_turns = 4
conversation_ttl_seconds = 3600
event_ttl_seconds = 600
[policy]
max_question_characters = 200
max_answer_characters = {max_answer_characters}
requests_per_window = {requests_per_window}
request_window_seconds = 60
daily_requests = {daily_requests}
[adapter]
kind = "{adapter}"
allowed_installations = [{workspaces}]
channel_policy = "{channel_policy}"
allowed_channels = [{channels}]
message_chunk_characters = {message_chunk_characters}
queue_size = {queue_size}
max_concurrent_generations = {max_concurrent_generations}
post_timeout_seconds = {post_timeout_seconds}
show_generation_status = {str(show_generation_status).lower()}
'''
