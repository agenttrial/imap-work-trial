import tempfile
import unittest
from pathlib import Path

from imapgw.uidstore import UidStore


class UidStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "uids.sqlite3"
        self.clock_value = 1_700_000_000

    def tearDown(self):
        self.tmp.cleanup()

    def clock(self):
        return self.clock_value

    def test_allocation_in_caller_order_and_stable(self):
        store = UidStore(self.path, clock=self.clock)
        m = store.ensure_uids("in", "INBOX", [("c", "c"), ("a", "a"), ("b", "b")])
        self.assertEqual(m, {"c": 1, "a": 2, "b": 3})
        again = store.ensure_uids("in", "INBOX", [("a", "a"), ("b", "b"), ("c", "c"), ("d", "d")])
        self.assertEqual(again, {"c": 1, "a": 2, "b": 3, "d": 4})
        self.assertEqual(store.mailbox_info("in", "INBOX"), (1_700_000_000, 5))
        store.close()

    def test_persists_across_reopen(self):
        store = UidStore(self.path, clock=self.clock)
        store.ensure_uids("in", "INBOX", [("x", "x"), ("y", "y")])
        validity, nxt = store.mailbox_info("in", "INBOX")
        store.close()
        self.clock_value += 100
        store2 = UidStore(self.path, clock=self.clock)
        self.assertEqual(store2.live_map("in", "INBOX"), {"x": 1, "y": 2})
        self.assertEqual(store2.mailbox_info("in", "INBOX"), (validity, nxt))
        store2.close()

    def test_tombstone_and_reappear_gets_new_uid(self):
        store = UidStore(self.path, clock=self.clock)
        store.ensure_uids("in", "INBOX", [("x", "x"), ("y", "y")])
        gone = store.tombstone_missing("in", "INBOX", {"y"})
        self.assertEqual(gone, [1])
        self.assertEqual(store.live_map("in", "INBOX"), {"y": 2})
        self.assertEqual(store.mailbox_info("in", "INBOX")[1], 3)  # next_uid unchanged
        back = store.ensure_uids("in", "INBOX", [("y", "y"), ("x", "x")])
        self.assertEqual(back, {"y": 2, "x": 3})
        store.close()

    def test_mailboxes_are_independent(self):
        store = UidStore(self.path, clock=self.clock)
        store.ensure_uids("in", "INBOX", [("x", "x")])
        store.ensure_uids("in", "Drafts", [("d#1", "d")])
        store.ensure_uids("other", "INBOX", [("x", "x")])
        self.assertEqual(store.live_map("in", "Drafts"), {"d#1": 1})
        self.assertEqual(store.live_map("other", "INBOX"), {"x": 1})
        store.close()

    def test_no_change_without_new_keys(self):
        store = UidStore(self.path, clock=self.clock)
        store.ensure_uids("in", "INBOX", [("x", "x")])
        store.ensure_uids("in", "INBOX", [("x", "x")])
        self.assertEqual(store.mailbox_info("in", "INBOX")[1], 2)
        self.assertEqual(store.tombstone_missing("in", "INBOX", {"x"}), [])
        store.close()

    def test_corrupt_file_is_set_aside_with_new_validity(self):
        store = UidStore(self.path, clock=self.clock)
        store.ensure_uids("in", "INBOX", [("x", "x")])
        v1 = store.mailbox_info("in", "INBOX")[0]
        store.close()
        self.path.write_bytes(b"this is not a sqlite database at all" * 100)
        for extra in (
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            if extra.exists():
                extra.unlink()
        self.clock_value += 5
        store2 = UidStore(self.path, clock=self.clock)
        self.assertEqual(store2.live_map("in", "INBOX"), {})
        v2 = store2.mailbox_info("in", "INBOX")[0]
        self.assertGreater(v2, v1)
        self.assertTrue(list(self.path.parent.glob("uids.sqlite3.corrupt-*")))
        store2.close()


if __name__ == "__main__":
    unittest.main()


class RecoveryPolicyTests(unittest.TestCase):
    """Only positively identified corruption may set a store aside (review finding F4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "uids.sqlite3"
        store = UidStore(self.path, clock=lambda: 1_700_000_000)
        store.ensure_uids("in", "INBOX", [("x", "x"), ("y", "y")])
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_healthy_store_is_not_discarded(self):
        import sqlite3

        from imapgw.uidstore import StoreUnavailable

        lock = sqlite3.connect(self.path, isolation_level=None)
        lock.execute("PRAGMA journal_mode=DELETE")
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(StoreUnavailable):
                UidStore(self.path, clock=lambda: 1_700_000_001, busy_timeout=0.2)
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])
        store = UidStore(self.path, clock=lambda: 1_700_000_002)
        self.assertEqual(store.live_map("in", "INBOX"), {"x": 1, "y": 2})
        store.close()

    def test_read_only_store_is_not_discarded(self):
        import os

        from imapgw.uidstore import StoreUnavailable

        os.chmod(self.path, 0o444)
        for extra in self.path.parent.glob(self.path.name + "-*"):
            extra.unlink()
        try:
            with self.assertRaises(StoreUnavailable):
                UidStore(self.path, clock=lambda: 1_700_000_001)
        finally:
            os.chmod(self.path, 0o644)
            # SQLite may have created read-only WAL side files during the failed open.
            for extra in self.path.parent.glob(self.path.name + "-*"):
                extra.unlink()
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])
        store = UidStore(self.path, clock=lambda: 1_700_000_002)
        self.assertEqual(store.live_map("in", "INBOX"), {"x": 1, "y": 2})
        store.close()

    def test_unsupported_schema_version_refuses_to_start(self):
        import sqlite3

        from imapgw.uidstore import StoreUnavailable

        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        conn.close()
        with self.assertRaises(StoreUnavailable):
            UidStore(self.path, clock=lambda: 1_700_000_001)
        self.assertEqual(list(self.path.parent.glob("*.corrupt-*")), [])


class UidValidityWatermarkTests(unittest.TestCase):
    """Second review, finding 3: a store recreated within the same second must not reuse
    UIDVALIDITY. A sidecar watermark file records the highest value issued."""

    def test_same_second_recreation_advances_uidvalidity(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "uids.sqlite3"
            clock = lambda: 1_700_000_000  # noqa: E731 - frozen clock on purpose
            store = UidStore(path, clock=clock)
            store.ensure_uids("in", "INBOX", [("old", "old")])
            v1 = store.mailbox_info("in", "INBOX")[0]
            store.close()
            for extra in path.parent.glob(path.name + "-*"):
                extra.unlink()
            path.unlink()  # the database is lost; the sidecar survives
            store2 = UidStore(path, clock=clock)
            store2.ensure_uids("in", "INBOX", [("new", "new")])
            v2 = store2.mailbox_info("in", "INBOX")[0]
            store2.close()
            self.assertEqual(v1, 1_700_000_000)
            self.assertGreater(v2, v1)
            # A clock that went backwards cannot reissue an old value either.
            store3 = UidStore(path, clock=lambda: 1_600_000_000)
            v3 = store3.mailbox_info("in", "Other")[0]
            store3.close()
            self.assertGreater(v3, v2)

    def test_corruption_recovery_also_advances(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "uids.sqlite3"
            clock = lambda: 1_700_000_000  # noqa: E731
            store = UidStore(path, clock=clock)
            v1 = store.mailbox_info("in", "INBOX")[0]
            store.close()
            for extra in path.parent.glob(path.name + "-*"):
                extra.unlink()
            path.write_bytes(b"garbage" * 200)
            store2 = UidStore(path, clock=clock)
            v2 = store2.mailbox_info("in", "INBOX")[0]
            store2.close()
            self.assertGreater(v2, v1)
