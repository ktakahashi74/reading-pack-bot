from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reading_pack_bot.errors import StoreError
from reading_pack_bot.models import ConversationKey
from reading_pack_bot.stores import MemoryStore, SQLiteStore


def conversation(thread="TH1", channel="C1"):
    return ConversationKey("slack", "T1", channel, thread, "a" * 64)


class StoreContractMixin:
    store = None

    def test_event_deduplication_and_expiry(self):
        self.assertTrue(self.store.claim_event("slack", "T1", "E1", 10, 100.0))
        self.assertFalse(self.store.claim_event("slack", "T1", "E1", 10, 101.0))
        self.assertTrue(self.store.claim_event("slack", "T1", "E1", 10, 111.0))

    def test_rate_limit(self):
        self.assertTrue(self.store.allow_request("scope", 2, 10, 100.0))
        self.assertTrue(self.store.allow_request("scope", 2, 10, 101.0))
        self.assertFalse(self.store.allow_request("scope", 2, 10, 102.0))
        self.assertTrue(self.store.allow_request("scope", 2, 10, 111.0))

    def test_conversation_isolation_and_reset(self):
        first = conversation("TH1")
        second = conversation("TH2")
        self.store.append_exchange(first, "q1", "a1", 100.0)
        self.store.append_exchange(second, "q2", "a2", 100.0)
        self.assertEqual([turn.content for turn in self.store.load_turns(first, 10, 1000, 101.0)], ["q1", "a1"])
        self.assertEqual([turn.content for turn in self.store.load_turns(second, 10, 1000, 101.0)], ["q2", "a2"])
        self.store.reset(first)
        self.assertEqual(self.store.load_turns(first, 10, 1000, 101.0), ())

    def test_history_limit_and_ttl(self):
        key = conversation()
        self.store.append_exchange(key, "old", "old answer", 10.0)
        self.store.append_exchange(key, "new", "new answer", 100.0)
        limited = self.store.load_turns(key, 2, 1000, 101.0)
        self.assertEqual([turn.content for turn in limited], ["new", "new answer"])
        expired = self.store.load_turns(key, 10, 10, 111.0)
        self.assertEqual(expired, ())

    def test_zero_ttl_retains_conversation_during_load_and_purge(self):
        key = conversation()
        self.store.append_exchange(key, "old", "old answer", 10.0)
        self.store.purge(1000.0, conversation_ttl_seconds=0, event_ttl_seconds=10)
        retained = self.store.load_turns(key, 10, 0, 1000.0)
        self.assertEqual(
            [turn.content for turn in retained],
            ["old", "old answer"],
        )


class MemoryStoreTests(StoreContractMixin, unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()


class SQLiteStoreTests(StoreContractMixin, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = SQLiteStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_file_mode_is_private(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_event_expiry_cleanup_uses_index_after_reopen(self):
        self.store.close()
        self.store = SQLiteStore(self.path)
        connection = self.store._connection()
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(processed_events)")
        }
        self.assertIn("processed_events_expires", indexes)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN DELETE FROM processed_events WHERE expires_at <= ?",
            (100.0,),
        ).fetchall()
        self.assertTrue(
            any("processed_events_expires" in str(row) for row in plan),
            plan,
        )

    def test_database_and_live_sidecars_are_private(self):
        self.store.append_exchange(conversation(), "private question", "private answer", 100.0)
        paths = [self.path] + [Path(str(self.path) + suffix) for suffix in ("-wal", "-shm")]
        existing = [path for path in paths if path.exists()]
        self.assertGreaterEqual(len(existing), 2)
        self.assertTrue(all(os.stat(path).st_mode & 0o777 == 0o600 for path in existing))

    def test_unsafe_existing_database_mode_is_rejected(self):
        unsafe = Path(self.temporary.name) / "unsafe.sqlite3"
        unsafe.write_bytes(b"")
        unsafe.chmod(0o640)
        with self.assertRaisesRegex(StoreError, "0600"):
            SQLiteStore(unsafe)

    def test_existing_database_owned_by_another_user_is_rejected(self):
        unsafe = Path(self.temporary.name) / "foreign.sqlite3"
        unsafe.write_bytes(b"")
        unsafe.chmod(0o600)
        actual = unsafe.lstat()
        foreign = SimpleNamespace(st_mode=actual.st_mode, st_uid=os.geteuid() + 1)
        with (
            patch.object(Path, "lstat", return_value=foreign),
            self.assertRaisesRegex(StoreError, "service-owned"),
        ):
            SQLiteStore(unsafe)

    def test_database_symlink_is_rejected(self):
        target = Path(self.temporary.name) / "target.sqlite3"
        target.write_bytes(b"")
        target.chmod(0o600)
        link = Path(self.temporary.name) / "link.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(StoreError, "regular file"):
            SQLiteStore(link)

    def test_existing_sidecar_symlink_is_rejected(self):
        database = Path(self.temporary.name) / "sidecar.sqlite3"
        database.write_bytes(b"")
        database.chmod(0o600)
        target = Path(self.temporary.name) / "sidecar-target"
        target.write_bytes(b"")
        target.chmod(0o600)
        Path(str(database) + "-wal").symlink_to(target)
        with self.assertRaisesRegex(StoreError, "sidecar"):
            SQLiteStore(database)

    def test_purge_removes_expired_plaintext_from_active_database_files(self):
        secret = b"PHYSICAL-PURGE-MARKER-9f314"
        self.store.append_exchange(conversation(), secret.decode(), "answer", 10.0)
        self.store.purge(100.0, conversation_ttl_seconds=10, event_ttl_seconds=10)
        paths = [self.path] + [Path(str(self.path) + suffix) for suffix in ("-wal", "-shm")]
        combined = b"".join(path.read_bytes() for path in paths if path.exists())
        self.assertNotIn(secret, combined)

    def test_reset_removes_plaintext_from_active_database_files(self):
        secret = b"PHYSICAL-RESET-MARKER-a681e"
        key = conversation()
        self.store.append_exchange(key, secret.decode(), "answer", 10.0)
        self.store.reset(key)
        paths = [self.path] + [Path(str(self.path) + suffix) for suffix in ("-wal", "-shm")]
        combined = b"".join(path.read_bytes() for path in paths if path.exists())
        self.assertNotIn(secret, combined)

    def test_restart_preserves_dedup_and_turns(self):
        key = conversation()
        self.assertTrue(self.store.claim_event("slack", "T1", "E-restart", 100, 100.0))
        self.store.append_exchange(key, "question", "answer", 100.0)
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.assertFalse(self.store.claim_event("slack", "T1", "E-restart", 100, 101.0))
        self.assertEqual([turn.content for turn in self.store.load_turns(key, 10, 1000, 101.0)], ["question", "answer"])

    def test_closed_store_fails_with_application_error(self):
        self.store.close()
        with self.assertRaisesRegex(StoreError, "closed"):
            self.store.allow_request("scope", 1, 10, 100.0)

    def test_database_does_not_store_platform_identifiers(self):
        key = ConversationKey("slack", "TEAM-SECRET", "CHANNEL-SECRET", "THREAD-SECRET", "b" * 64)
        self.store.claim_event("slack", "TEAM-SECRET", "EVENT-SECRET", 100, 100.0)
        self.store.append_exchange(key, "question", "answer", 100.0)
        self.store.close()
        raw = self.path.read_bytes()
        for secret in (b"TEAM-SECRET", b"CHANNEL-SECRET", b"THREAD-SECRET", b"EVENT-SECRET"):
            self.assertNotIn(secret, raw)
        self.store = SQLiteStore(self.path)

    def test_concurrent_event_claim_has_one_winner(self):
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def claim():
            barrier.wait()
            result = self.store.claim_event("slack", "T1", "E-concurrent", 100, 100.0)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(True), 1)

    def test_unknown_schema_fails_closed(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(Exception, "schema"):
            SQLiteStore(self.path)
        self.store = SQLiteStore.__new__(SQLiteStore)
        self.store.close = lambda: None


if __name__ == "__main__":
    unittest.main()
