"""Platform-neutral data contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__version__ = "0.4.0"


@dataclass(frozen=True)
class PackSnapshot:
    path: Path
    raw_markdown: str
    sha256: str
    header: Mapping[str, str]
    end_counts: Mapping[str, int]
    size_bytes: int


@dataclass(frozen=True)
class ConversationKey:
    platform: str
    installation_id: str
    channel_id: str
    thread_id: str
    pack_sha256: str

    def values(self) -> tuple[str, str, str, str, str]:
        return (
            self.platform,
            self.installation_id,
            self.channel_id,
            self.thread_id,
            self.pack_sha256,
        )


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    platform: str
    installation_id: str
    channel_id: str
    thread_id: str
    actor_id: str
    text: str
    triggered: bool = True
    automated: bool = False

    def conversation_key(self, pack_sha256: str) -> ConversationKey:
        return ConversationKey(
            platform=self.platform,
            installation_id=self.installation_id,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            pack_sha256=pack_sha256,
        )


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str
    created_at: float


@dataclass(frozen=True)
class GenerationRequest:
    runtime_instructions: str
    pack: PackSnapshot
    prior_turns: tuple[Turn, ...]
    current_question: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    response_id: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class BotReply:
    handled: bool
    text: str = ""
    error_code: str | None = None
    usage: Usage = field(default_factory=Usage)
