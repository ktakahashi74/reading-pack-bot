"""Restart-safe SQLite state with opaque route and event identifiers."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path

from ..errors import StoreError
from ..models import ConversationKey, Turn

_SCHEMA_VERSION = "1"
_UMASK_LOCK = threading.Lock()
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _digest(*values: str) -> str:
    encoded = "\x00".join(values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise StoreError("SQLite parent directory does not exist")
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._prepare_files()
            # SQLite creates WAL/SHM sidecars. A private process umask during
            # connection setup prevents a local default umask from exposing
            # conversation content before permissions can be checked.
            with _UMASK_LOCK:
                old_umask = os.umask(0o077)
                try:
                    self._conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=FULL")
                    self._conn.execute("PRAGMA foreign_keys=ON")
                    self._conn.execute("PRAGMA busy_timeout=5000")
                    self._conn.execute("PRAGMA secure_delete=ON")
                    self._initialize()
                    self._check_private_files()
                finally:
                    os.umask(old_umask)
        except StoreError:
            if self._conn is not None:
                self._conn.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if self._conn is not None:
                self._conn.close()
            raise StoreError(f"cannot initialize SQLite store: {type(exc).__name__}") from None

    @staticmethod
    def _is_private_regular(path: Path) -> bool:
        details = path.lstat()
        return (
            stat.S_ISREG(details.st_mode)
            and details.st_uid == os.geteuid()
            and stat.S_IMODE(details.st_mode) == 0o600
        )

    def _prepare_files(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if not self._is_private_regular(self.path):
                raise StoreError(
                    "existing SQLite file must be service-owned, a regular file, and mode 0600"
                ) from None
        else:
            os.close(descriptor)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = Path(str(self.path) + suffix)
            if os.path.lexists(sidecar) and not self._is_private_regular(sidecar):
                raise StoreError(
                    "existing SQLite sidecar must be service-owned, a regular file, and mode 0600"
                )

    def _check_private_files(self) -> None:
        if not self._is_private_regular(self.path):
            raise StoreError("SQLite file ownership or permissions changed unexpectedly")
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = Path(str(self.path) + suffix)
            if os.path.lexists(sidecar) and not self._is_private_regular(sidecar):
                raise StoreError("SQLite sidecar ownership or permissions are unsafe")

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("SQLite store is closed")
        return self._conn

    def _initialize(self) -> None:
        connection = self._connection()
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS processed_events_expires
                    ON processed_events(expires_at);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_key TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_conversation_created
                    ON turns(conversation_key, created_at);
                CREATE TABLE IF NOT EXISTS rate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_scope_occurred
                    ON rate_events(scope, occurred_at);
                """
            )
            row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
            elif row[0] != _SCHEMA_VERSION:
                raise StoreError("unsupported SQLite schema version")

    @staticmethod
    def _conversation_key(key: ConversationKey) -> str:
        return _digest(*key.values())

    def claim_event(
        self,
        platform: str,
        installation_id: str,
        event_id: str,
        ttl_seconds: int,
        now: float,
    ) -> bool:
        event_key = _digest(platform, installation_id, event_id)
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    connection.execute("DELETE FROM processed_events WHERE expires_at <= ?", (now,))
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO processed_events(event_key, expires_at) VALUES(?, ?)",
                        (event_key, now + ttl_seconds),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StoreError(f"cannot claim event: {type(exc).__name__}") from None

    def allow_request(self, scope: str, limit: int, window_seconds: int, now: float) -> bool:
        cutoff = now - window_seconds
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    connection.execute(
                        "DELETE FROM rate_events WHERE scope = ? AND occurred_at <= ?",
                        (scope, cutoff),
                    )
                    count = connection.execute(
                        "SELECT COUNT(*) FROM rate_events WHERE scope = ? AND occurred_at > ?",
                        (scope, cutoff),
                    ).fetchone()[0]
                    if count >= limit:
                        return False
                    connection.execute(
                        "INSERT INTO rate_events(scope, occurred_at) VALUES(?, ?)",
                        (scope, now),
                    )
                    return True
        except sqlite3.Error as exc:
            raise StoreError(f"cannot update rate limit: {type(exc).__name__}") from None

    def load_turns(
        self,
        key: ConversationKey,
        limit: int,
        ttl_seconds: int,
        now: float,
    ) -> tuple[Turn, ...]:
        opaque = self._conversation_key(key)
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    if ttl_seconds > 0:
                        connection.execute(
                            "DELETE FROM turns WHERE conversation_key = ? AND created_at <= ?",
                            (opaque, now - ttl_seconds),
                        )
                    rows = connection.execute(
                        "SELECT role, content, created_at FROM turns "
                        "WHERE conversation_key = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                        (opaque, limit),
                    ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot load conversation: {type(exc).__name__}") from None
        return tuple(Turn(role, content, created_at) for role, content, created_at in reversed(rows))

    def append_exchange(self, key: ConversationKey, question: str, answer: str, now: float) -> None:
        opaque = self._conversation_key(key)
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    connection.executemany(
                        "INSERT INTO turns(conversation_key, role, content, created_at) VALUES(?, ?, ?, ?)",
                        (
                            (opaque, "user", question, now),
                            (opaque, "assistant", answer, now),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot append conversation: {type(exc).__name__}") from None

    def reset(self, key: ConversationKey) -> None:
        opaque = self._conversation_key(key)
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    connection.execute("DELETE FROM turns WHERE conversation_key = ?", (opaque,))
                self._checkpoint_truncate()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot reset conversation: {type(exc).__name__}") from None

    def purge(self, now: float, conversation_ttl_seconds: int, event_ttl_seconds: int) -> None:
        try:
            with self._lock:
                connection = self._connection()
                with connection:
                    if conversation_ttl_seconds > 0:
                        connection.execute(
                            "DELETE FROM turns WHERE created_at <= ?",
                            (now - conversation_ttl_seconds,),
                        )
                    connection.execute("DELETE FROM processed_events WHERE expires_at <= ?", (now,))
                    connection.execute("DELETE FROM rate_events WHERE occurred_at <= ?", (now - 86400,))
                self._checkpoint_truncate()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot purge store: {type(exc).__name__}") from None

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._checkpoint_truncate()
            finally:
                self._conn.close()
                self._conn = None

    def _checkpoint_truncate(self) -> None:
        row = self._connection().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or row[0] != 0:
            raise StoreError("cannot truncate SQLite WAL because the database is busy")
        self._check_private_files()
