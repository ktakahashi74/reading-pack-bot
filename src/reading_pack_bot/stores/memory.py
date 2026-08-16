"""Deterministic in-memory store for local use and tests."""

from __future__ import annotations

import threading
from collections import defaultdict

from ..models import ConversationKey, Turn


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[tuple[str, str, str], float] = {}
        self._rates: dict[str, list[float]] = defaultdict(list)
        self._turns: dict[ConversationKey, list[Turn]] = defaultdict(list)

    def claim_event(
        self,
        platform: str,
        installation_id: str,
        event_id: str,
        ttl_seconds: int,
        now: float,
    ) -> bool:
        key = (platform, installation_id, event_id)
        with self._lock:
            expired = [item for item, expiry in self._events.items() if expiry <= now]
            for item in expired:
                del self._events[item]
            if key in self._events:
                return False
            self._events[key] = now + ttl_seconds
            return True

    def allow_request(self, scope: str, limit: int, window_seconds: int, now: float) -> bool:
        cutoff = now - window_seconds
        with self._lock:
            current = [timestamp for timestamp in self._rates[scope] if timestamp > cutoff]
            if len(current) >= limit:
                self._rates[scope] = current
                return False
            current.append(now)
            self._rates[scope] = current
            return True

    def load_turns(
        self,
        key: ConversationKey,
        limit: int,
        ttl_seconds: int,
        now: float,
    ) -> tuple[Turn, ...]:
        with self._lock:
            current = self._turns[key]
            if ttl_seconds > 0:
                cutoff = now - ttl_seconds
                current = [turn for turn in current if turn.created_at > cutoff]
                self._turns[key] = current
            return tuple(current[-limit:])

    def append_exchange(self, key: ConversationKey, question: str, answer: str, now: float) -> None:
        with self._lock:
            self._turns[key].append(Turn("user", question, now))
            self._turns[key].append(Turn("assistant", answer, now + 0.000001))

    def reset(self, key: ConversationKey) -> None:
        with self._lock:
            self._turns.pop(key, None)

    def purge(self, now: float, conversation_ttl_seconds: int, event_ttl_seconds: int) -> None:
        with self._lock:
            self._events = {key: expiry for key, expiry in self._events.items() if expiry > now}
            if conversation_ttl_seconds > 0:
                cutoff = now - conversation_ttl_seconds
                for key in list(self._turns):
                    current = [turn for turn in self._turns[key] if turn.created_at > cutoff]
                    if current:
                        self._turns[key] = current
                    else:
                        del self._turns[key]
            rate_cutoff = now - max(event_ttl_seconds, 86400)
            for scope in list(self._rates):
                current = [timestamp for timestamp in self._rates[scope] if timestamp > rate_cutoff]
                if current:
                    self._rates[scope] = current
                else:
                    del self._rates[scope]

    def close(self) -> None:
        return None
