"""Application-level routing and bounded-input policy."""

from __future__ import annotations

from dataclasses import dataclass

from .config import AdapterConfig, PolicyConfig, RuntimeConfig
from .models import IncomingMessage


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


class MessagePolicy:
    def __init__(self, runtime: RuntimeConfig, adapter: AdapterConfig, limits: PolicyConfig):
        self.runtime = runtime
        self.adapter = adapter
        self.limits = limits

    def check(self, message: IncomingMessage, *, environment_disabled: bool = False) -> PolicyDecision:
        if self.runtime.kill_switch or environment_disabled:
            return PolicyDecision(False, "kill_switch")
        if self.adapter.kind == "disabled" or message.platform != self.adapter.kind:
            return PolicyDecision(False, "unsupported_platform")
        if message.installation_id not in self.adapter.allowed_installations:
            return PolicyDecision(False, "installation_not_allowed")
        if not self.adapter.allows_channel(message.channel_id):
            return PolicyDecision(False, "channel_not_allowed")
        if not message.triggered:
            return PolicyDecision(False, "trigger_required")
        if message.automated:
            return PolicyDecision(False, "automated_message")
        if not message.event_id or not message.thread_id or not message.actor_id:
            return PolicyDecision(False, "missing_identity")
        if not message.text.strip():
            return PolicyDecision(False, "empty_question")
        if (
            len(message.inline_context) + len(message.text)
            > self.limits.max_question_characters
        ):
            return PolicyDecision(False, "question_too_long")
        return PolicyDecision(True, "allowed")

    def bound_answer(self, text: str) -> str:
        if len(text) <= self.limits.max_answer_characters:
            return text
        suffix = "\n\n[回答は運用上の長さ上限で打ち切られました]"
        return text[: self.limits.max_answer_characters - len(suffix)] + suffix
