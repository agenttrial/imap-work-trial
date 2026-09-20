"""Mailboxes as label views over AgentMail, with durable UIDs.

This is the only module that understands both the IMAP model (mailboxes, UIDs, flags) and the
AgentMail model (labels, messages, drafts). Everything protocol-facing goes through
:class:`MailboxService`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from imapgw import drafts as draftmod
from imapgw.apiclient import (
    AgentMailClient,
    AuthError,
    NotFoundError,
    UnavailableError,
)
from imapgw.bytecache import ByteCache
from imapgw.config import Settings
from imapgw.uidstore import UidStore

log = logging.getLogger("imapgw.mailbox")

FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"
FLAG_DRAFT = "\\Draft"
FLAG_DELETED = "\\Deleted"
FLAG_ANSWERED = "\\Answered"

ALL_FLAGS: tuple[str, ...] = (FLAG_SEEN, FLAG_ANSWERED, FLAG_FLAGGED, FLAG_DELETED, FLAG_DRAFT)


@dataclass(frozen=True)
class MailboxDef:
    name: str
    kind: str  # "messages" | "drafts"
    query_labels: tuple[str, ...] = ()
    exclude_labels: frozenset[str] = frozenset()
    include_trash: bool = False
    include_spam: bool = False
    permanent_flags: tuple[str, ...] = ()
    appendable: bool = False


# Trashed or spam-labelled messages are hidden from INBOX and Sent (the Gmail convention) and
# shown in the Trash and Spam views instead. A message carrying both `received` and `trash`
# therefore appears exactly once, in Trash.
HIDDEN_LABELS = frozenset({"trash", "spam"})

# Flags a client may change with STORE, per view. \Seen and \Flagged become label changes
# (read/unread, starred); \Deleted is session state until EXPUNGE moves the item to trash
# (messages) or deletes it (drafts). Trash and Spam allow no permanent deletion, so no \Deleted.
MESSAGE_PERMANENT = (FLAG_SEEN, FLAG_FLAGGED, FLAG_DELETED)
TRASH_PERMANENT = (FLAG_SEEN, FLAG_FLAGGED)
DRAFT_PERMANENT = (FLAG_DELETED,)

MAILBOXES: dict[str, MailboxDef] = {
    "INBOX": MailboxDef(
        "INBOX",
        "messages",
        query_labels=("received",),
        exclude_labels=HIDDEN_LABELS,
        permanent_flags=MESSAGE_PERMANENT,
    ),
    "Drafts": MailboxDef("Drafts", "drafts", appendable=True, permanent_flags=DRAFT_PERMANENT),
    "Sent": MailboxDef(
        "Sent",
        "messages",
        query_labels=("sent",),
        exclude_labels=HIDDEN_LABELS,
        permanent_flags=MESSAGE_PERMANENT,
    ),
    "Trash": MailboxDef(
        "Trash",
        "messages",
        query_labels=("trash",),
        include_trash=True,
        permanent_flags=TRASH_PERMANENT,
    ),
    "Spam": MailboxDef(
        "Spam",
        "messages",
        query_labels=("spam",),
        include_spam=True,
        permanent_flags=TRASH_PERMANENT,
    ),
}

# IMAP flag -> (labels to add, labels to remove) when set; reversed when cleared.
FLAG_LABELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    FLAG_SEEN: (("read",), ("unread",)),
    FLAG_FLAGGED: (("starred",), ()),
}


def lookup_mailbox(name: str, defs: Mapping[str, MailboxDef] | None = None) -> MailboxDef | None:
    defs = MAILBOXES if defs is None else defs
    if name.upper() == "INBOX":
        return defs.get("INBOX")
    return defs.get(name)


@dataclass(frozen=True)
class Item:
    uid: int
    remote_key: str
    remote_id: str
    flags: frozenset[str]
    internaldate: datetime
    size_hint: int | None
    labels: frozenset[str]
    from_: str = ""
    to: tuple[str, ...] = ()
    subject: str = ""


@dataclass(frozen=True)
class MailboxSnapshot:
    inbox_id: str
    name: str
    uidvalidity: int
    uidnext: int
    items: tuple[Item, ...]
    version: int
    synced_at: float
    by_uid: Mapping[int, Item] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_uid", {item.uid: item for item in self.items})


def message_flags(labels: frozenset[str]) -> frozenset[str]:
    flags = set()
    if "unread" not in labels:
        flags.add(FLAG_SEEN)
    if "starred" in labels:
        flags.add(FLAG_FLAGGED)
    return frozenset(flags)


DRAFT_FLAGS = frozenset({FLAG_DRAFT, FLAG_SEEN})


class MailboxService:
    def __init__(
        self,
        store: UidStore,
        cache: ByteCache,
        settings: Settings,
        defs: Mapping[str, MailboxDef] | None = None,
    ) -> None:
        self.store = store
        self.cache = cache
        self.settings = settings
        self.defs: dict[str, MailboxDef] = dict(defs or MAILBOXES)
        self._latest: dict[tuple[str, str], MailboxSnapshot] = {}
        self._inflight: dict[
            tuple[str, str], tuple[asyncio.Task[MailboxSnapshot], int, AgentMailClient]
        ] = {}
        self._versions: dict[tuple[str, str], int] = {}
        # Mutation generation per mailbox: a sync that started before the latest mutation is
        # never accepted as the post-mutation view (see append_draft).
        self._generation: dict[tuple[str, str], int] = {}
        # Items we created ourselves, pinned until a listing shows them or the pin expires.
        # Upstream listings can lag a write; the pin keeps the allocated UID from being
        # tombstoned and the item visible in the meantime.
        self._pinned: dict[tuple[str, str], dict[str, tuple[float, tuple]]] = {}
        self._pin_ttl = 120.0

    @staticmethod
    def _ckey(inbox_id: str, kind: str, ident: str) -> str:
        """Byte-cache keys are namespaced by inbox and object kind so two inboxes that share an
        id (a Message-ID delivered to both) never share bytes."""
        return f"{inbox_id}\x00{kind}\x00{ident}"

    # ----- lookup -----------------------------------------------------------------------------

    def lookup(self, name: str) -> MailboxDef | None:
        return lookup_mailbox(name, self.defs)

    def names(self) -> list[str]:
        return list(self.defs)

    def latest(self, inbox_id: str, name: str) -> MailboxSnapshot | None:
        mdef = self.lookup(name)
        return self._latest.get((inbox_id, mdef.name)) if mdef else None

    def mailbox_info(self, inbox_id: str, name: str) -> tuple[int, int]:
        return self.store.mailbox_info(inbox_id, name)

    def deleted_uids(self, inbox_id: str, name: str) -> set[int]:
        mdef = self.lookup(name)
        return self.store.deleted_uids(inbox_id, mdef.name) if mdef else set()

    def set_deleted(self, inbox_id: str, name: str, uids: Collection[int], value: bool) -> None:
        mdef = self.lookup(name)
        if mdef is not None:
            self.store.set_deleted(inbox_id, mdef.name, uids, value)

    @staticmethod
    def flags_of(item: Item, deleted: Collection[int]) -> frozenset[str]:
        """An item's flags including the persisted \\Deleted mark."""
        return item.flags | {FLAG_DELETED} if item.uid in deleted else item.flags

    # ----- sync -------------------------------------------------------------------------------

    async def sync(
        self, api: AgentMailClient, inbox_id: str, name: str, *, force: bool = False
    ) -> MailboxSnapshot:
        mdef = self.lookup(name)
        if mdef is None:
            raise NotFoundError(f"no such mailbox {name!r}")
        key = (inbox_id, mdef.name)
        loop = asyncio.get_running_loop()
        snap = self._latest.get(key)
        if snap is not None and not force:
            if loop.time() - snap.synced_at < self.settings.refresh_interval:
                return snap
        generation = self._generation.get(key, 0)
        entry = self._inflight.get(key)
        if entry is not None:
            task, started_gen, owner = entry
            if started_gen >= generation or not force:
                return await self._join(task, owner, api, inbox_id, mdef)
            # A mutation happened after this sync started: let it finish (ignore its outcome)
            # and run a fresh one so the caller sees the post-mutation state.
            try:
                await asyncio.shield(task)
            except Exception:  # noqa: BLE001 - the stale sync's failure is not ours to report
                pass
            entry = self._inflight.get(key)
            if entry is not None and entry[1] >= generation:
                return await self._join(entry[0], entry[2], api, inbox_id, mdef)
        task = loop.create_task(self._do_sync(api, inbox_id, mdef))
        self._inflight[key] = (task, generation, api)

        def _done(t: asyncio.Task, k=key) -> None:
            current = self._inflight.get(k)
            if current is not None and current[0] is t:
                self._inflight.pop(k, None)

        task.add_done_callback(_done)
        return await asyncio.shield(task)

    async def _join(
        self,
        task: asyncio.Task[MailboxSnapshot],
        owner: AgentMailClient,
        api: AgentMailClient,
        inbox_id: str,
        mdef: MailboxDef,
    ) -> MailboxSnapshot:
        """Await another caller's in-flight sync. An authentication failure belongs to the
        credentials that made the request: if they were not ours, run the sync again with our
        own client instead of inheriting the verdict."""
        try:
            return await asyncio.shield(task)
        except AuthError:
            if owner is api:
                raise
            log.info("coalesced sync failed with another session's credentials; retrying with own")
            return await self._do_sync(api, inbox_id, mdef)

    async def _do_sync(
        self, api: AgentMailClient, inbox_id: str, mdef: MailboxDef
    ) -> MailboxSnapshot:
        if mdef.kind == "messages":
            entries = await self._collect_messages(api, inbox_id, mdef)
        else:
            entries = await self._collect_drafts(api, inbox_id, mdef)
        # entries: list of (sort_key, remote_key, remote_id, builder(uid) -> Item)
        entries = self._apply_pins(inbox_id, mdef.name, entries)
        entries.sort(key=lambda e: e[0])
        keys = [(e[1], e[2]) for e in entries]
        uid_map = self.store.ensure_uids(inbox_id, mdef.name, keys)
        self.store.tombstone_missing(inbox_id, mdef.name, {k for k, _ in keys})
        uidvalidity, uidnext = self.store.mailbox_info(inbox_id, mdef.name)
        items = tuple(sorted((e[3](uid_map[e[1]]) for e in entries), key=lambda i: i.uid))
        key = (inbox_id, mdef.name)
        version = self._versions.get(key, 0) + 1
        self._versions[key] = version
        snap = MailboxSnapshot(
            inbox_id=inbox_id,
            name=mdef.name,
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            items=items,
            version=version,
            synced_at=asyncio.get_running_loop().time(),
        )
        self._latest[key] = snap
        log.debug("synced %s/%s: %d items, uidnext %d", inbox_id, mdef.name, len(items), uidnext)
        return snap

    def _apply_pins(self, inbox_id: str, name: str, entries: list) -> list:
        key = (inbox_id, name)
        pins = self._pinned.get(key)
        if not pins:
            return entries
        now = asyncio.get_running_loop().time()
        listed = {e[1] for e in entries}
        for remote_key, (expires, entry) in list(pins.items()):
            if remote_key in listed or expires < now:
                pins.pop(remote_key, None)
                continue
            log.info("listing does not show recently created %s yet; keeping it visible", entry[2])
            entries.append(entry)
        if not pins:
            self._pinned.pop(key, None)
        return entries

    def _unpin(self, inbox_id: str, name: str, remote_key: str) -> None:
        pins = self._pinned.get((inbox_id, name))
        if pins:
            pins.pop(remote_key, None)
            if not pins:
                self._pinned.pop((inbox_id, name), None)

    def _pin(self, inbox_id: str, name: str, remote_key: str, entry: tuple) -> None:
        expires = asyncio.get_running_loop().time() + self._pin_ttl
        self._pinned.setdefault((inbox_id, name), {})[remote_key] = (expires, entry)

    async def _collect_messages(
        self, api: AgentMailClient, inbox_id: str, mdef: MailboxDef
    ) -> list:
        entries = []
        seen: set[str] = set()
        async for m in api.list_messages(
            inbox_id,
            labels=mdef.query_labels,
            include_trash=mdef.include_trash,
            include_spam=mdef.include_spam,
            ascending=True,
        ):
            message_id = str(m.get("message_id") or "")
            if not message_id:
                # Never reconcile against a listing we cannot trust: a missing id would look
                # like a vanished message and tombstone it.
                raise UnavailableError("malformed listing: message without message_id")
            if message_id in seen:
                log.warning("duplicate message_id %s in listing; keeping first", message_id)
                continue
            seen.add(message_id)
            labels = frozenset(str(x) for x in (m.get("labels") or []))
            if labels & mdef.exclude_labels:
                continue
            ts = draftmod.parse_iso8601(m.get("timestamp") or m.get("created_at"))
            size = m.get("size")
            size_hint = int(size) if isinstance(size, int | float) else None
            to = tuple(str(x) for x in (m.get("to") or []))

            def build(uid: int, *, m=m, labels=labels, ts=ts, size_hint=size_hint, to=to) -> Item:
                return Item(
                    uid=uid,
                    remote_key=str(m["message_id"]),
                    remote_id=str(m["message_id"]),
                    flags=message_flags(labels),
                    internaldate=ts,
                    size_hint=size_hint,
                    labels=labels,
                    from_=str(m.get("from") or ""),
                    to=to,
                    subject=str(m.get("subject") or ""),
                )

            entries.append(((ts, message_id), message_id, message_id, build))
        return entries

    async def _collect_drafts(self, api: AgentMailClient, inbox_id: str, mdef: MailboxDef) -> list:
        entries = []
        seen: set[str] = set()
        async for d in api.list_drafts(inbox_id):
            draft_id = str(d.get("draft_id") or "")
            if not draft_id:
                raise UnavailableError("malformed listing: draft without draft_id")
            if draft_id in seen:
                log.warning("duplicate draft_id %s in listing; keeping first", draft_id)
                continue
            seen.add(draft_id)
            updated_at = str(d.get("updated_at") or "")
            rendered = await self._render_draft(api, inbox_id, draft_id, updated_at)
            if rendered is None:
                continue  # vanished between list and get
            key = draftmod.content_key(draft_id, rendered)
            rendered_size = len(rendered)
            self.cache.put(self._ckey(inbox_id, "body", key), rendered)
            ts = draftmod.parse_iso8601(updated_at)
            labels = frozenset(str(x) for x in (d.get("labels") or []))
            to = tuple(str(x) for x in (d.get("to") or []))

            def build(
                uid: int,
                *,
                d=d,
                key=key,
                draft_id=draft_id,
                ts=ts,
                size=rendered_size,
                labels=labels,
                to=to,
            ) -> Item:
                return Item(
                    uid=uid,
                    remote_key=key,
                    remote_id=draft_id,
                    flags=DRAFT_FLAGS,
                    internaldate=ts,
                    size_hint=size,
                    labels=labels,
                    from_=inbox_id,
                    to=to,
                    subject=str(d.get("subject") or ""),
                )

            entries.append(((ts, draft_id), key, draft_id, build))
        return entries

    async def _render_draft(
        self, api: AgentMailClient, inbox_id: str, draft_id: str, updated_at: str
    ) -> bytes | None:
        version_key = self._ckey(inbox_id, "draftv", f"{draft_id}:{updated_at}")
        cached = self.cache.get(version_key)
        if cached is not None:
            return cached
        try:
            full = await api.get_draft(inbox_id, draft_id)
        except NotFoundError:
            return None
        rendered = draftmod.render_draft(full, inbox_id)
        self.cache.put(version_key, rendered)
        return rendered

    # ----- content ---------------------------------------------------------------------------

    async def raw_bytes(self, api: AgentMailClient, inbox_id: str, name: str, item: Item) -> bytes:
        mdef = self.lookup(name)
        assert mdef is not None
        body_key = self._ckey(inbox_id, "body", item.remote_key)
        cached = self.cache.get(body_key)
        if cached is not None:
            return cached
        if mdef.kind == "drafts":
            full = await api.get_draft(inbox_id, item.remote_id)
            rendered = draftmod.render_draft(full, inbox_id)
            if draftmod.content_key(item.remote_id, rendered) != item.remote_key:
                raise NotFoundError("draft changed since it was listed")
            self.cache.put(body_key, rendered)
            return rendered
        meta = await api.get_raw_meta(inbox_id, item.remote_id)
        size_key = self._ckey(inbox_id, "size", item.remote_key)
        remembered = self.cache.get(size_key)
        if remembered is not None and int(remembered) != meta.size:
            # The size we already reported to clients is authoritative for this UID.
            log.error(
                "raw size for %s changed from %s to %d; refusing to serve inconsistent content",
                item.remote_id,
                remembered.decode("ascii"),
                meta.size,
            )
            raise UnavailableError("raw message size changed upstream", retryable=False)
        data = await api.download(meta.download_url)
        if len(data) != meta.size:
            # A truncated or substituted download must never be served or cached as content.
            log.error(
                "raw size mismatch for %s: metadata says %d, downloaded %d; not serving",
                item.remote_id,
                meta.size,
                len(data),
            )
            raise UnavailableError("raw message download was incomplete", retryable=True)
        if item.size_hint is not None and item.size_hint != len(data):
            log.warning(
                "list size %d differs from raw length %d for %s",
                item.size_hint,
                len(data),
                item.remote_id,
            )
        self.cache.put(body_key, data)
        self.cache.put(size_key, str(len(data)).encode("ascii"))
        return data

    async def size_of(self, api: AgentMailClient, inbox_id: str, name: str, item: Item) -> int:
        """Authoritative RFC822.SIZE. Once reported for a UID it never changes: it comes from
        the bytes when cached, otherwise from the raw endpoint's own ``size`` (remembered per
        item), and a later download whose length disagrees is refused rather than served. The
        list item's ``size`` is never used for this; it is only compared and logged."""
        cached = self.cache.get(self._ckey(inbox_id, "body", item.remote_key))
        if cached is not None:
            return len(cached)
        mdef = self.lookup(name)
        assert mdef is not None
        if mdef.kind == "drafts":
            return len(await self.raw_bytes(api, inbox_id, name, item))
        size_key = self._ckey(inbox_id, "size", item.remote_key)
        remembered = self.cache.get(size_key)
        if remembered is not None:
            return int(remembered)
        meta = await api.get_raw_meta(inbox_id, item.remote_id)
        if item.size_hint is not None and item.size_hint != meta.size:
            log.warning(
                "list size %d differs from raw size %d for %s",
                item.size_hint,
                meta.size,
                item.remote_id,
            )
        self.cache.put(size_key, str(meta.size).encode("ascii"))
        return meta.size

    # ----- mutations ---------------------------------------------------------------------------

    async def append_draft(
        self,
        api: AgentMailClient,
        inbox_id: str,
        fields: draftmod.DraftFields,
        client_id: str | None,
    ) -> tuple[int, int, asyncio.Task]:
        """Create the draft and return ``(uidvalidity, uid, resync_task)``.

        The UID is allocated directly from the create response, so the APPEND answer reflects
        exactly what happened upstream: once the POST succeeded the command succeeds, even if
        the follow-up listing fails (its failure is logged, not reported as an APPEND failure).
        """
        if self.latest(inbox_id, "Drafts") is None:
            # First contact with this mailbox: give the existing drafts their UIDs first so the
            # new draft is allocated after them. This runs before the POST, so its failure
            # leaves the mailbox unchanged (RFC 3501 6.3.11).
            await self.sync(api, inbox_id, "Drafts", force=True)
        created = await api.create_draft(inbox_id, draftmod.api_body(fields, client_id))
        draft_id = str(created.get("draft_id") or "")
        if not draft_id:
            raise UnavailableError("draft was created but the response carried no draft_id")
        rendered = draftmod.render_draft(created, inbox_id)
        key = draftmod.content_key(draft_id, rendered)
        updated_at = str(created.get("updated_at") or "")
        self.cache.put(self._ckey(inbox_id, "draftv", f"{draft_id}:{updated_at}"), rendered)
        self.cache.put(self._ckey(inbox_id, "body", key), rendered)
        mailbox_key = (inbox_id, "Drafts")
        uid = self.store.ensure_uids(inbox_id, "Drafts", [(key, draft_id)])[key]
        uidvalidity, _ = self.store.mailbox_info(inbox_id, "Drafts")
        ts = draftmod.parse_iso8601(updated_at)
        labels = frozenset(str(x) for x in (created.get("labels") or []))
        to = tuple(str(x) for x in (created.get("to") or []))
        subject = str(created.get("subject") or "")
        size = len(rendered)

        def build(uid_value: int) -> Item:
            return Item(
                uid=uid_value,
                remote_key=key,
                remote_id=draft_id,
                flags=DRAFT_FLAGS,
                internaldate=ts,
                size_hint=size,
                labels=labels,
                from_=inbox_id,
                to=to,
                subject=subject,
            )

        self._pin(inbox_id, "Drafts", key, ((ts, draft_id), key, draft_id, build))
        self._generation[mailbox_key] = self._generation.get(mailbox_key, 0) + 1
        # The follow-up listing runs as its own task: the APPEND has already succeeded upstream
        # and its UID is allocated, so nothing about the listing (slowness, cancellation of the
        # command, upstream errors) may turn into an APPEND failure.
        resync = asyncio.get_running_loop().create_task(
            self.sync(api, inbox_id, "Drafts", force=True)
        )

        def _log_failure(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.warning(
                    "APPEND succeeded (draft %s, uid %d) but resync failed: %s", draft_id, uid, exc
                )

        resync.add_done_callback(_log_failure)
        return uidvalidity, uid, resync

    async def set_flag(
        self, api: AgentMailClient, inbox_id: str, name: str, item: Item, flag: str, value: bool
    ) -> Item:
        """Set or clear \\Seen / \\Flagged on a message by changing its labels; returns the
        updated item and patches the latest snapshot in place."""
        mdef = self.lookup(name)
        assert mdef is not None
        if mdef.kind != "messages" or flag not in FLAG_LABELS:
            raise UnavailableError(f"flag {flag} cannot be stored in {name}")
        add, remove = FLAG_LABELS[flag]
        if not value:
            add, remove = remove, add
        result = await api.update_labels(inbox_id, item.remote_id, add=add, remove=remove)
        labels = frozenset(str(x) for x in (result.get("labels") or []))
        if not result.get("labels"):
            labels = (item.labels | set(add)) - set(remove)
        updated = Item(
            uid=item.uid,
            remote_key=item.remote_key,
            remote_id=item.remote_id,
            flags=message_flags(labels),
            internaldate=item.internaldate,
            size_hint=item.size_hint,
            labels=labels,
            from_=item.from_,
            to=item.to,
            subject=item.subject,
        )
        self._replace_item(inbox_id, mdef.name, updated)
        return updated

    def _replace_item(self, inbox_id: str, name: str, updated: Item) -> None:
        key = (inbox_id, name)
        snap = self._latest.get(key)
        if snap is None or updated.uid not in snap.by_uid:
            return
        items = tuple(updated if i.uid == updated.uid else i for i in snap.items)
        self._latest[key] = MailboxSnapshot(
            inbox_id=snap.inbox_id,
            name=snap.name,
            uidvalidity=snap.uidvalidity,
            uidnext=snap.uidnext,
            items=items,
            version=snap.version,
            synced_at=snap.synced_at,
        )

    async def expunge_items(
        self, api: AgentMailClient, inbox_id: str, name: str, items: Sequence[Item]
    ) -> list[Exception]:
        """Remove items from a view: messages get the ``trash`` label (soft delete, AgentMail's
        own model); drafts are deleted through the Drafts API. Never calls the permanent
        message-delete endpoint. Returns the errors encountered; successes are kept."""
        mdef = self.lookup(name)
        assert mdef is not None
        errors: list[Exception] = []
        removed = False
        for item in items:
            try:
                if mdef.kind == "drafts":
                    # The UID names one specific version of the draft. If the draft was edited
                    # upstream since we listed it, the client marked a version it never saw:
                    # refuse rather than destroy the newer content. (A GET-then-DELETE still has
                    # a race window; the API offers no conditional delete to close it.)
                    current = await api.get_draft(inbox_id, item.remote_id)
                    rendered = draftmod.render_draft(current, inbox_id)
                    if draftmod.content_key(item.remote_id, rendered) != item.remote_key:
                        raise UnavailableError(
                            "draft was edited upstream since it was listed; not deleted",
                            retryable=False,
                        )
                    await api.delete_draft(inbox_id, item.remote_id)
                    self._unpin(inbox_id, mdef.name, item.remote_key)
                else:
                    await api.update_labels(inbox_id, item.remote_id, add=("trash",))
                removed = True
            except Exception as exc:  # noqa: BLE001 - reported to the caller per item
                errors.append(exc)
        if removed:
            # A listing that started before the removal must not be taken as the post-removal
            # view (it could still show the item); force the caller's sync to start afresh.
            key = (inbox_id, mdef.name)
            self._generation[key] = self._generation.get(key, 0) + 1
        return errors


def sequence_items(snapshot: MailboxSnapshot, uids: Sequence[int]) -> list[Item]:
    return [snapshot.by_uid[u] for u in uids if u in snapshot.by_uid]


__all__ = [
    "ALL_FLAGS",
    "FLAG_DELETED",
    "FLAG_DRAFT",
    "FLAG_FLAGGED",
    "FLAG_SEEN",
    "MAILBOXES",
    "Item",
    "MailboxDef",
    "MailboxService",
    "MailboxSnapshot",
    "UnavailableError",
    "lookup_mailbox",
    "message_flags",
]
