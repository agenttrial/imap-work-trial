"""Durable UID allocation backed by SQLite.

Invariants (RFC 3501 2.3.1.1 and the assignment):
* UIDs within a mailbox are allocated strictly ascending and are never reused; vanished items
  keep a tombstone row so their UID stays taken forever.
* ``next_uid`` moves only when new items are allocated.
* ``uidvalidity`` is fixed when the mailbox row is created; it changes only if the database is
  lost or found corrupt on open (the file is set aside and a fresh one created, and the new
  value is strictly larger than the old one when the old one can be read).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("imapgw.uidstore")

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailbox (
  inbox_id    TEXT    NOT NULL,
  name        TEXT    NOT NULL,
  uidvalidity INTEGER NOT NULL,
  next_uid    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (inbox_id, name)
);
CREATE TABLE IF NOT EXISTS uid_map (
  inbox_id      TEXT    NOT NULL,
  name          TEXT    NOT NULL,
  uid           INTEGER NOT NULL,
  remote_key    TEXT    NOT NULL,
  remote_id     TEXT    NOT NULL,
  first_seen_at TEXT    NOT NULL,
  vanished_at   TEXT,
  PRIMARY KEY (inbox_id, name, uid),
  FOREIGN KEY (inbox_id, name) REFERENCES mailbox(inbox_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS uid_map_live_key
  ON uid_map(inbox_id, name, remote_key) WHERE vanished_at IS NULL;
CREATE TABLE IF NOT EXISTS deleted_mark (
  inbox_id  TEXT    NOT NULL,
  name      TEXT    NOT NULL,
  uid       INTEGER NOT NULL,
  marked_at TEXT    NOT NULL,
  PRIMARY KEY (inbox_id, name, uid)
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_CORRUPTION_MARKERS = ("file is not a database", "malformed", "not a database", "is encrypted")


def _is_corruption(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


class StoreUnavailable(RuntimeError):
    """The UID store exists but cannot be used right now (locked, read-only, disk error).
    The server must refuse to start rather than discard the store."""


class UidStore:
    def __init__(self, path: Path | str, *, clock=time.time, busy_timeout: float = 5.0) -> None:
        self.path = Path(path)
        self._clock = clock
        self._busy_timeout = busy_timeout
        self._conn = self._open()

    # ----- lifecycle ------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """Open the store. Only *positively identified* corruption triggers recovery (the file is
        set aside and a fresh store created). Locks, permissions, disk errors, and unsupported
        schemas raise :class:`StoreUnavailable` so the caller fails loudly instead."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            result = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            if _is_corruption(exc):
                return self._recover(str(exc))
            raise StoreUnavailable(f"UID store {self.path} cannot be opened: {exc}") from exc
        if not result or result[0] != "ok":
            conn.close()
            return self._recover(f"integrity_check reported {result!r}")
        try:
            conn.executescript(_SCHEMA)
            self._check_schema_version(conn)
            # Prove the store is writable now rather than failing mid-allocation later. A real
            # write is required: in WAL mode BEGIN IMMEDIATE succeeds on a read-only file.
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('opened_at', ?)", (_now_iso(),)
            )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StoreUnavailable(f"UID store {self.path} cannot be prepared: {exc}") from exc
        return conn

    def _recover(self, reason: str) -> sqlite3.Connection:
        stamp = int(self._clock())
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        log.error(
            "UID store %s is corrupt (%s); setting it aside as %s", self.path, reason, aside.name
        )
        try:
            self.path.rename(aside)
        except OSError as exc:
            raise StoreUnavailable(f"cannot set aside corrupt store {self.path}: {exc}") from exc
        try:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            self._check_schema_version(conn)
        except sqlite3.DatabaseError as exc:
            raise StoreUnavailable(
                f"cannot create a fresh UID store at {self.path}: {exc}"
            ) from exc
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=self._busy_timeout,
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _check_schema_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )
        elif row[0] != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"unsupported schema version {row[0]}")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    # ----- queries --------------------------------------------------------------------------

    def mailbox_info(self, inbox_id: str, name: str) -> tuple[int, int]:
        """Return ``(uidvalidity, next_uid)``, creating the mailbox row on first use."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._ensure_mailbox(inbox_id, name)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return row

    def _ensure_mailbox(self, inbox_id: str, name: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT uidvalidity, next_uid FROM mailbox WHERE inbox_id = ? AND name = ?",
            (inbox_id, name),
        ).fetchone()
        if row is None:
            uidvalidity = self._next_uidvalidity()
            self._conn.execute(
                "INSERT INTO mailbox(inbox_id, name, uidvalidity, next_uid) VALUES (?, ?, ?, 1)",
                (inbox_id, name, uidvalidity),
            )
            return uidvalidity, 1
        return int(row[0]), int(row[1])

    # ----- UIDVALIDITY watermark -----------------------------------------------------------------
    #
    # UIDVALIDITY values are issued from the clock, but a database recreated within the same
    # second (or after a clock rollback) would otherwise reissue a value and silently redefine
    # existing UIDs. A tiny sidecar file next to the database records the highest value ever
    # issued, so a fresh store still advances. Deleting both files is a deliberate fresh start.

    @property
    def _watermark_path(self) -> Path:
        return self.path.with_name(self.path.name + ".validity")

    def _read_watermark(self) -> int:
        try:
            return int(self._watermark_path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _write_watermark(self, value: int) -> None:
        tmp = self._watermark_path.with_name(self._watermark_path.name + ".tmp")
        try:
            tmp.write_text(str(value))
            tmp.replace(self._watermark_path)
        except OSError as exc:  # pragma: no cover - best effort; the DB value is still durable
            log.warning("could not persist UIDVALIDITY watermark: %s", exc)

    def _next_uidvalidity(self) -> int:
        value = max(1, int(self._clock()), self._read_watermark() + 1)
        value = min(value, 2**32 - 1)
        self._write_watermark(value)
        return value

    def live_map(self, inbox_id: str, name: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT remote_key, uid FROM uid_map WHERE inbox_id = ? AND name = ? "
            "AND vanished_at IS NULL",
            (inbox_id, name),
        ).fetchall()
        return {str(k): int(u) for k, u in rows}

    def ensure_uids(
        self, inbox_id: str, name: str, keys: Sequence[tuple[str, str]]
    ) -> dict[str, int]:
        """Allocate UIDs for unseen ``(remote_key, remote_id)`` pairs in the given order and
        return the complete live map. One transaction, so allocation is atomic."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            _, next_uid = self._ensure_mailbox(inbox_id, name)
            live = self.live_map(inbox_id, name)
            now = _now_iso()
            allocated = 0
            for remote_key, remote_id in keys:
                if remote_key in live:
                    continue
                self._conn.execute(
                    "INSERT INTO uid_map(inbox_id, name, uid, remote_key, remote_id, "
                    "first_seen_at, vanished_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (inbox_id, name, next_uid, remote_key, remote_id, now),
                )
                live[remote_key] = next_uid
                next_uid += 1
                allocated += 1
            if allocated:
                self._conn.execute(
                    "UPDATE mailbox SET next_uid = ? WHERE inbox_id = ? AND name = ?",
                    (next_uid, inbox_id, name),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        if allocated:
            log.debug("allocated %d UIDs in %s/%s (next %d)", allocated, inbox_id, name, next_uid)
        return live

    def tombstone_missing(self, inbox_id: str, name: str, present: Collection[str]) -> list[int]:
        """Mark live keys not in ``present`` as vanished; return their UIDs (ascending)."""
        present_set = set(present)
        live = self.live_map(inbox_id, name)
        gone = sorted(uid for key, uid in live.items() if key not in present_set)
        if not gone:
            return []
        now = _now_iso()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "UPDATE uid_map SET vanished_at = ? WHERE inbox_id = ? AND name = ? AND uid = ?",
                [(now, inbox_id, name, uid) for uid in gone],
            )
            self._conn.executemany(
                "DELETE FROM deleted_mark WHERE inbox_id = ? AND name = ? AND uid = ?",
                [(inbox_id, name, uid) for uid in gone],
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        log.debug("tombstoned %d UIDs in %s/%s", len(gone), inbox_id, name)
        return gone

    def next_uid(self, inbox_id: str, name: str) -> int:
        return self.mailbox_info(inbox_id, name)[1]

    # ----- \Deleted marks (persistent, so PERMANENTFLAGS may advertise \Deleted) --------------

    def deleted_uids(self, inbox_id: str, name: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT uid FROM deleted_mark WHERE inbox_id = ? AND name = ?", (inbox_id, name)
        ).fetchall()
        return {int(r[0]) for r in rows}

    def set_deleted(self, inbox_id: str, name: str, uids: Collection[int], value: bool) -> None:
        if not uids:
            return
        now = _now_iso()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if value:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO deleted_mark(inbox_id, name, uid, marked_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(inbox_id, name, uid, now) for uid in uids],
                )
            else:
                self._conn.executemany(
                    "DELETE FROM deleted_mark WHERE inbox_id = ? AND name = ? AND uid = ?",
                    [(inbox_id, name, uid) for uid in uids],
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
