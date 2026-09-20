"""Mailbox sync against a scripted API: deterministic UIDs regardless of page order, no
tombstoning on partial failure, coalesced concurrent syncs, and draft rendering keys."""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from imapgw.apiclient import AgentMailClient, HttpResponse, UnavailableError
from imapgw.bytecache import ByteCache
from imapgw.config import Settings
from imapgw.mailbox import MailboxService
from imapgw.uidstore import UidStore
from tests.support.scripted_transport import Call, ScriptedTransport, json_response

INBOX = "candidate@imap.test"
BASE = "http://api.test/v0"


def msg(mid: str, ts: str, labels: list[str], size: int = 100) -> dict:
    return {
        "inbox_id": INBOX,
        "thread_id": "t",
        "message_id": mid,
        "labels": labels,
        "timestamp": ts,
        "from": "a@example.com",
        "to": [INBOX],
        "subject": mid,
        "size": size,
        "updated_at": ts,
        "created_at": ts,
    }


MESSAGES = [
    msg("m_new", "2026-08-17T21:00:00.000Z", ["received", "unread"]),
    msg("m_mid", "2026-08-17T18:00:00.000Z", ["received", "read", "starred"]),
    msg("m_old", "2026-08-17T16:00:00.000Z", ["received", "unread"]),
]
DRAFTS = [
    {
        "draft_id": "d2",
        "updated_at": "2026-08-17T23:00:00.000Z",
        "to": ["b@x"],
        "subject": "two",
        "labels": [],
    },
    {
        "draft_id": "d1",
        "updated_at": "2026-08-17T22:00:00.000Z",
        "to": ["a@x"],
        "subject": "one",
        "labels": [],
    },
]
DRAFT_TEXT = {"d1": "text one", "d2": "text two"}


class FakeRouter:
    """Serves newest-first pages of size ``page`` for messages and drafts, like the harness."""

    def __init__(self, messages=MESSAGES, drafts=DRAFTS, page=2, fail_on_page: int | None = None):
        self.messages = messages
        self.drafts = drafts
        self.page = page
        self.fail_on_page = fail_on_page
        self.get_draft_calls = 0

    def __call__(self, call: Call):
        parts = urlsplit(call.url)
        q = parse_qs(parts.query)
        path = parts.path
        if path.endswith("/messages"):
            return self._page(self.messages, q, "messages")
        if path.endswith("/drafts"):
            return self._page(self.drafts, q, "drafts")
        m = re.search(r"/drafts/([^/]+)$", path)
        if m:
            self.get_draft_calls += 1
            did = m.group(1)
            d = next(x for x in self.drafts if x["draft_id"] == did)
            return json_response(200, {**d, "text": DRAFT_TEXT[did], "created_at": d["updated_at"]})
        return json_response(404, {})

    def _page(self, items, q, key):
        offset = int(q.get("page_token", ["0"])[0])
        page_no = offset // self.page + 1
        if self.fail_on_page == page_no:
            return json_response(503, {})
        chunk = items[offset : offset + self.page]
        body = {"count": len(chunk), key: chunk}
        if offset + self.page < len(items):
            body["next_page_token"] = str(offset + self.page)
        return json_response(200, body)


class MailboxSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.cache = ByteCache(10_000_000)
        self.settings = Settings(refresh_interval=0.0)
        self.service = MailboxService(self.store, self.cache, self.settings)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def client(self, router) -> tuple[AgentMailClient, ScriptedTransport]:
        t = ScriptedTransport(router=router)

        async def nosleep(_):
            return None

        return AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep), t

    async def test_uids_follow_timestamp_order_not_page_order(self):
        api, t = self.client(FakeRouter())
        snap = await self.service.sync(api, INBOX, "INBOX")
        self.assertEqual([i.remote_id for i in snap.items], ["m_old", "m_mid", "m_new"])
        self.assertEqual([i.uid for i in snap.items], [1, 2, 3])
        self.assertEqual(snap.uidnext, 4)
        self.assertEqual(len([c for c in t.calls if "/messages" in c.url]), 2)  # two pages
        self.assertEqual(snap.items[1].flags, frozenset({"\\Seen", "\\Flagged"}))
        self.assertEqual(snap.items[0].flags, frozenset())

    async def test_two_services_with_empty_stores_agree(self):
        api, _ = self.client(FakeRouter(page=1))
        snap_a = await self.service.sync(api, INBOX, "INBOX")
        with tempfile.TemporaryDirectory() as d:
            other = MailboxService(UidStore(Path(d) / "x.sqlite3"), ByteCache(10), self.settings)
            api2, _ = self.client(FakeRouter(page=2))
            snap_b = await other.sync(api2, INBOX, "INBOX")
            other.store.close()
        self.assertEqual(
            [(i.uid, i.remote_id) for i in snap_a.items],
            [(i.uid, i.remote_id) for i in snap_b.items],
        )

    async def test_partial_failure_keeps_previous_snapshot_and_tombstones_nothing(self):
        api, _ = self.client(FakeRouter())
        first = await self.service.sync(api, INBOX, "INBOX")
        api_fail, _ = self.client(FakeRouter(fail_on_page=2))
        with self.assertRaises(UnavailableError):
            await self.service.sync(api_fail, INBOX, "INBOX", force=True)
        self.assertIs(self.service.latest(INBOX, "INBOX"), first)
        self.assertEqual(self.store.live_map(INBOX, "INBOX"), {"m_old": 1, "m_mid": 2, "m_new": 3})

    async def test_vanished_and_new_items(self):
        api, _ = self.client(FakeRouter())
        await self.service.sync(api, INBOX, "INBOX")
        changed = [
            MESSAGES[0],
            msg("m_newer", "2026-08-18T00:00:00.000Z", ["received"]),
        ]  # m_mid, m_old gone
        api2, _ = self.client(FakeRouter(messages=changed))
        snap = await self.service.sync(api2, INBOX, "INBOX", force=True)
        self.assertEqual([(i.uid, i.remote_id) for i in snap.items], [(3, "m_new"), (4, "m_newer")])
        self.assertEqual(snap.uidnext, 5)
        # A vanished message that returns gets a fresh UID, never its old one.
        api3, _ = self.client(FakeRouter())
        snap3 = await self.service.sync(api3, INBOX, "INBOX", force=True)
        uids = {i.remote_id: i.uid for i in snap3.items}
        self.assertEqual(uids["m_new"], 3)
        self.assertGreater(uids["m_old"], 4)
        self.assertGreater(uids["m_mid"], 4)

    async def test_concurrent_syncs_are_coalesced(self):
        router = FakeRouter()
        t = ScriptedTransport(router=router, delay=0.05)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        results = await asyncio.gather(
            *(self.service.sync(api, INBOX, "INBOX", force=True) for _ in range(5))
        )
        self.assertTrue(all(r is results[0] for r in results))
        self.assertEqual(len([c for c in t.calls if "/messages" in c.url]), 2)

    async def test_refresh_interval_serves_cached_snapshot(self):
        service = MailboxService(self.store, self.cache, Settings(refresh_interval=60.0))
        api, t = self.client(FakeRouter())
        a = await service.sync(api, INBOX, "INBOX")
        b = await service.sync(api, INBOX, "INBOX")
        self.assertIs(a, b)
        c = await service.sync(api, INBOX, "INBOX", force=True)
        self.assertIsNot(a, c)

    async def test_drafts_are_rendered_and_keyed_by_content(self):
        router = FakeRouter()
        api, _ = self.client(router)
        snap = await self.service.sync(api, INBOX, "Drafts")
        self.assertEqual([i.remote_id for i in snap.items], ["d1", "d2"])
        self.assertTrue(all("\\Draft" in i.flags for i in snap.items))
        self.assertTrue(snap.items[0].remote_key.startswith("d1#"))
        raw = await self.service.raw_bytes(api, INBOX, "Drafts", snap.items[0])
        self.assertIn(b"text one", raw)
        self.assertEqual(len(raw), snap.items[0].size_hint)
        self.assertEqual(router.get_draft_calls, 2)
        # Second sync with unchanged updated_at needs no draft GETs.
        await self.service.sync(api, INBOX, "Drafts", force=True)
        self.assertEqual(router.get_draft_calls, 2)

    async def test_edited_draft_gets_new_uid_and_old_vanishes(self):
        api, _ = self.client(FakeRouter())
        snap = await self.service.sync(api, INBOX, "Drafts")
        old_uid_d1 = snap.items[0].uid
        DRAFT_TEXT["d1"] = "text one edited"
        try:
            edited = [dict(DRAFTS[0]), {**DRAFTS[1], "updated_at": "2026-08-18T01:00:00.000Z"}]
            api2, _ = self.client(FakeRouter(drafts=edited))
            snap2 = await self.service.sync(api2, INBOX, "Drafts", force=True)
        finally:
            DRAFT_TEXT["d1"] = "text one"
        uids = {i.remote_id: i.uid for i in snap2.items}
        self.assertNotEqual(uids["d1"], old_uid_d1)
        self.assertGreater(uids["d1"], max(i.uid for i in snap.items))
        self.assertNotIn(old_uid_d1, snap2.by_uid)


if __name__ == "__main__":
    unittest.main()


class MutationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.service = MailboxService(
            self.store, ByteCache(10_000_000), Settings(refresh_interval=0.0)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_expunge_drafts_calls_delete_endpoint(self):
        router = FakeRouter()
        t = ScriptedTransport(router=router)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        snap = await self.service.sync(api, INBOX, "Drafts")
        deleted: list[str] = []

        def routed(call):
            if call.method == "DELETE":
                deleted.append(call.url)
                return json_response(204, {})
            return router(call)

        t.router = routed
        errors = await self.service.expunge_items(api, INBOX, "Drafts", [snap.items[0]])
        self.assertEqual(errors, [])
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0].endswith("/drafts/d1"))

    async def test_expunge_messages_adds_trash_label_only(self):
        router = FakeRouter()
        patches: list[tuple[str, bytes]] = []

        def routed(call):
            if call.method == "PATCH":
                patches.append((call.url, call.body))
                return json_response(200, {"message_id": "x", "labels": ["received", "trash"]})
            if call.method == "DELETE":
                raise AssertionError("permanent delete must never be called")
            return router(call)

        t = ScriptedTransport(router=routed)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        snap = await self.service.sync(api, INBOX, "INBOX")
        errors = await self.service.expunge_items(api, INBOX, "INBOX", list(snap.items[:2]))
        self.assertEqual(errors, [])
        self.assertEqual(len(patches), 2)
        self.assertTrue(all(b'"add_labels": ["trash"]' in body for _, body in patches))

    async def test_set_flag_patches_labels_and_updates_snapshot(self):
        router = FakeRouter()

        def routed(call):
            if call.method == "PATCH":
                return json_response(200, {"message_id": "m_old", "labels": ["received", "read"]})
            return router(call)

        t = ScriptedTransport(router=routed)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        snap = await self.service.sync(api, INBOX, "INBOX")
        item = snap.items[0]
        self.assertNotIn("\\Seen", item.flags)
        updated = await self.service.set_flag(api, INBOX, "INBOX", item, "\\Seen", True)
        self.assertIn("\\Seen", updated.flags)
        latest = self.service.latest(INBOX, "INBOX")
        self.assertIn("\\Seen", latest.by_uid[item.uid].flags)
        self.assertEqual(latest.version, snap.version)


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Regressions for review findings F2, F6, F10, F11, F12."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.cache = ByteCache(10_000_000)
        self.service = MailboxService(self.store, self.cache, Settings(refresh_interval=0.0))

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def client(self, router, **kw):
        t = ScriptedTransport(router=router, **kw)

        async def nosleep(_):
            return None

        return AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep), t

    async def test_malformed_200_keeps_view_and_tombstones_nothing(self):  # F11
        api, _ = self.client(FakeRouter())
        first = await self.service.sync(api, INBOX, "INBOX")
        bad_api, _ = self.client(
            lambda call: json_response(200, {"error": {"message": "wrong shape"}})
        )
        with self.assertRaises(UnavailableError):
            await self.service.sync(bad_api, INBOX, "INBOX", force=True)
        self.assertIs(self.service.latest(INBOX, "INBOX"), first)
        self.assertEqual(len(self.store.live_map(INBOX, "INBOX")), 3)

    async def test_duplicate_ids_in_listing_yield_one_item(self):  # F11
        dup = [MESSAGES[0], MESSAGES[0], MESSAGES[2]]
        api, _ = self.client(FakeRouter(messages=dup, page=3))
        snap = await self.service.sync(api, INBOX, "INBOX")
        self.assertEqual([i.uid for i in snap.items], [1, 2])
        self.assertEqual(len(set(i.uid for i in snap.items)), len(snap.items))

    async def test_cache_is_namespaced_by_inbox(self):  # F2
        alice = msg("same-id", "2026-08-17T16:00:00.000Z", ["received"], size=25)
        bob = msg("same-id", "2026-08-17T16:00:00.000Z", ["received"], size=23)
        bodies = {"alice@x": b"private body for alice@x\n", "bob@x": b"private body for bob@x\n"}

        def router(call):
            inbox = "alice@x" if "alice" in call.url else "bob@x"
            if "/download" in call.url:
                return HttpResponse(200, {}, bodies[inbox])
            if call.url.endswith("/raw"):
                return json_response(
                    200,
                    {
                        "message_id": "same-id",
                        "size": len(bodies[inbox]),
                        "download_url": f"http://files/{inbox}/download",
                        "expires_at": "",
                    },
                )
            return json_response(200, {"messages": [alice if inbox == "alice@x" else bob]})

        api, _ = self.client(router)
        snap_a = await self.service.sync(api, "alice@x", "INBOX")
        snap_b = await self.service.sync(api, "bob@x", "INBOX")
        got_a = await self.service.raw_bytes(api, "alice@x", "INBOX", snap_a.items[0])
        got_b = await self.service.raw_bytes(api, "bob@x", "INBOX", snap_b.items[0])
        self.assertEqual(got_a, bodies["alice@x"])
        self.assertEqual(got_b, bodies["bob@x"])

    async def test_download_size_mismatch_is_not_served_or_cached(self):  # F10
        def router(call):
            if "/download" in call.url:
                return HttpResponse(200, {}, b"short")
            if call.url.endswith("/raw"):
                return json_response(
                    200,
                    {
                        "message_id": "m_old",
                        "size": 500,
                        "download_url": "http://files/download",
                        "expires_at": "",
                    },
                )
            return FakeRouter()(call)

        api, _ = self.client(router)
        snap = await self.service.sync(api, INBOX, "INBOX")
        with self.assertRaises(UnavailableError):
            await self.service.raw_bytes(api, INBOX, "INBOX", snap.items[0])
        self.assertEqual(len(self.cache), 0)

    async def test_append_reports_success_when_resync_fails(self):  # F6
        from imapgw.drafts import DraftFields

        router = FakeRouter()
        state = {"created": False}

        def routed(call):
            if call.method == "POST":
                state["created"] = True
                return json_response(
                    200,
                    {
                        "draft_id": "d_new",
                        "updated_at": "2026-08-18T00:00:00.000Z",
                        "to": ["z@x"],
                        "subject": "new",
                        "text": "body",
                        "labels": [],
                    },
                )
            if state["created"] and "/drafts?" in call.url and call.method == "GET":
                return json_response(503, {})
            return router(call)

        api, _ = self.client(routed)
        await self.service.sync(api, INBOX, "Drafts")  # existing drafts get UIDs 1 and 2
        validity, uid, resync = await self.service.append_draft(
            api, INBOX, DraftFields(to=("z@x",), subject="new", text="body"), None
        )
        await asyncio.gather(resync, return_exceptions=True)
        self.assertEqual(uid, 3)
        self.assertEqual(validity, self.store.mailbox_info(INBOX, "Drafts")[0])
        self.assertIn(uid, self.store.live_map(INBOX, "Drafts").values())
        raw = await self.service.raw_bytes(
            api,
            INBOX,
            "Drafts",
            type(
                "I",
                (),
                {
                    "remote_key": next(
                        k for k, u in self.store.live_map(INBOX, "Drafts").items() if u == uid
                    ),
                    "remote_id": "d_new",
                },
            )(),
        )
        self.assertIn(b"Subject: new", raw)

    async def test_append_before_first_sync_allocates_after_existing_drafts(self):
        from imapgw.drafts import DraftFields

        router = FakeRouter()

        def routed(call):
            if call.method == "POST":
                return json_response(
                    200,
                    {
                        "draft_id": "d_new",
                        "updated_at": "2026-08-18T00:00:00.000Z",
                        "to": ["z@x"],
                        "subject": "new",
                        "text": "body",
                        "labels": [],
                    },
                )
            return router(call)

        api, _ = self.client(routed)
        _, uid, resync = await self.service.append_draft(
            api, INBOX, DraftFields(to=("z@x",), subject="new", text="body"), None
        )
        await asyncio.gather(resync, return_exceptions=True)
        self.assertEqual(uid, 3)

    async def test_append_does_not_join_a_listing_that_predates_it(self):  # F12
        from imapgw.drafts import DraftFields

        created = {"done": False}
        drafts_now = list(DRAFTS)

        def routed(call):
            if call.method == "POST":
                created["done"] = True
                new = {
                    "draft_id": "d_new",
                    "updated_at": "2026-08-18T00:00:00.000Z",
                    "to": ["z@x"],
                    "subject": "new",
                    "text": "body",
                    "labels": [],
                }
                drafts_now.insert(0, new)
                DRAFT_TEXT["d_new"] = "body"
                return json_response(200, new)
            return FakeRouter(drafts=drafts_now, page=5)(call)

        try:
            api, t = self.client(routed, delay=0.05)
            stale = asyncio.create_task(self.service.sync(api, INBOX, "Drafts", force=True))
            await asyncio.sleep(0.01)  # the stale listing is in flight and pre-dates the create
            _, uid, resync = await self.service.append_draft(
                api, INBOX, DraftFields(to=("z@x",), subject="new", text="body"), None
            )
            await asyncio.gather(resync, return_exceptions=True)
            await stale
            latest = self.service.latest(INBOX, "Drafts")
            self.assertIn(uid, latest.by_uid)
            self.assertEqual(latest.by_uid[uid].remote_id, "d_new")
        finally:
            DRAFT_TEXT.pop("d_new", None)

    async def test_stale_upstream_listing_does_not_tombstone_new_draft(self):
        """Read-after-write lag: the listing right after the create does not include the new
        draft yet. Its UID must survive and the item must stay visible."""
        from imapgw.drafts import DraftFields

        router = FakeRouter()

        def routed(call):
            if call.method == "POST":
                return json_response(
                    200,
                    {
                        "draft_id": "d_new",
                        "updated_at": "2026-08-18T00:00:00.000Z",
                        "to": ["z@x"],
                        "subject": "new",
                        "text": "body",
                        "labels": [],
                    },
                )
            return router(call)  # never lists d_new

        api, _ = self.client(routed)
        await self.service.sync(api, INBOX, "Drafts")
        _, uid, resync = await self.service.append_draft(
            api, INBOX, DraftFields(to=("z@x",), subject="new", text="body"), None
        )
        await asyncio.gather(resync, return_exceptions=True)
        self.assertEqual(uid, 3)
        self.assertIn(3, self.store.live_map(INBOX, "Drafts").values())
        latest = self.service.latest(INBOX, "Drafts")
        self.assertIn(3, latest.by_uid)
        self.assertEqual(latest.by_uid[3].remote_id, "d_new")
        raw = await self.service.raw_bytes(api, INBOX, "Drafts", latest.by_uid[3])
        self.assertIn(b"Subject: new", raw)
        # Once the pin expires and the listing still lacks it, it vanishes like any other item.
        self.service._pin_ttl = -1.0
        self.service._pinned[(INBOX, "Drafts")] = {
            k: (-1.0, v[1]) for k, v in self.service._pinned[(INBOX, "Drafts")].items()
        }
        snap = await self.service.sync(api, INBOX, "Drafts", force=True)
        self.assertNotIn(3, snap.by_uid)
        self.assertEqual(self.store.mailbox_info(INBOX, "Drafts")[1], 4)  # UID 3 never reused


class StaleDraftDeletionTests(unittest.IsolatedAsyncioTestCase):
    """Review finding F1: never delete a draft version the client has not seen."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.service = MailboxService(
            self.store, ByteCache(10_000_000), Settings(refresh_interval=0.0)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_edited_upstream_draft_is_not_deleted(self):
        router = FakeRouter()
        deletes: list[str] = []

        def routed(call):
            if call.method == "DELETE":
                deletes.append(call.url)
                return json_response(204, {})
            return router(call)

        t = ScriptedTransport(router=routed)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        snap = await self.service.sync(api, INBOX, "Drafts")
        target = snap.items[0]  # d1
        DRAFT_TEXT["d1"] = "text one EDITED UPSTREAM"
        try:
            errors = await self.service.expunge_items(api, INBOX, "Drafts", [target])
        finally:
            DRAFT_TEXT["d1"] = "text one"
        self.assertEqual(len(errors), 1)
        self.assertIn("edited upstream", str(errors[0]))
        self.assertEqual(deletes, [])

    async def test_unchanged_draft_is_deleted(self):
        router = FakeRouter()
        deletes: list[str] = []

        def routed(call):
            if call.method == "DELETE":
                deletes.append(call.url)
                return json_response(204, {})
            return router(call)

        t = ScriptedTransport(router=routed)

        async def nosleep(_):
            return None

        api = AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep)
        snap = await self.service.sync(api, INBOX, "Drafts")
        errors = await self.service.expunge_items(api, INBOX, "Drafts", [snap.items[0]])
        self.assertEqual(errors, [])
        self.assertEqual(len(deletes), 1)


class CoalescingAuthTests(unittest.IsolatedAsyncioTestCase):
    """Review finding F13: another session's revoked key must not fail our sync."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.service = MailboxService(
            self.store, ByteCache(10_000_000), Settings(refresh_interval=0.0)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_valid_client_retries_when_owner_key_is_rejected(self):
        from imapgw.apiclient import AuthError

        async def nosleep(_):
            return None

        revoked_t = ScriptedTransport(router=lambda call: json_response(401, {}), delay=0.05)
        revoked = AgentMailClient(BASE, "revoked", revoked_t, max_attempts=1, sleep=nosleep)
        good_t = ScriptedTransport(router=FakeRouter())
        good = AgentMailClient(BASE, "good", good_t, max_attempts=1, sleep=nosleep)

        first = asyncio.create_task(self.service.sync(revoked, INBOX, "INBOX", force=True))
        await asyncio.sleep(0.01)  # the revoked key's sync is in flight
        second = asyncio.create_task(self.service.sync(good, INBOX, "INBOX", force=True))
        with self.assertRaises(AuthError):
            await first
        snap = await second
        self.assertEqual(len(snap.items), 3)
        self.assertTrue(good_t.calls)  # the valid client did its own listing


class ReReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Second review: findings 2 and 5."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.service = MailboxService(
            self.store, ByteCache(10_000_000), Settings(refresh_interval=0.0)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def client(self, router, **kw):
        t = ScriptedTransport(router=router, **kw)

        async def nosleep(_):
            return None

        return AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep), t

    async def test_item_without_id_fails_the_sync_and_keeps_state(self):  # finding 2
        api, _ = self.client(FakeRouter())
        first = await self.service.sync(api, INBOX, "INBOX")
        bad_api, _ = self.client(lambda call: json_response(200, {"messages": [{}]}))
        with self.assertRaises(UnavailableError):
            await self.service.sync(bad_api, INBOX, "INBOX", force=True)
        self.assertIs(self.service.latest(INBOX, "INBOX"), first)
        self.assertEqual(len(self.store.live_map(INBOX, "INBOX")), 3)
        bad_drafts, _ = self.client(
            lambda call: json_response(200, {"drafts": [{"subject": "no id"}]})
        )
        with self.assertRaises(UnavailableError):
            await self.service.sync(bad_drafts, INBOX, "Drafts", force=True)

    async def test_deleting_a_pinned_draft_removes_it_immediately(self):  # finding 5
        from imapgw.drafts import DraftFields

        router = FakeRouter()
        deletes: list[str] = []

        def routed(call):
            if call.method == "POST":
                return json_response(
                    200,
                    {
                        "draft_id": "d_new",
                        "updated_at": "2026-08-18T00:00:00.000Z",
                        "to": ["z@x"],
                        "subject": "new",
                        "text": "body",
                        "labels": [],
                    },
                )
            if call.method == "DELETE":
                deletes.append(call.url)
                return json_response(204, {})
            if "/drafts/d_new" in call.url:
                return json_response(
                    200,
                    {
                        "draft_id": "d_new",
                        "updated_at": "2026-08-18T00:00:00.000Z",
                        "created_at": "2026-08-18T00:00:00.000Z",
                        "to": ["z@x"],
                        "subject": "new",
                        "text": "body",
                        "labels": [],
                    },
                )
            return router(call)  # listings never include d_new: the pin keeps it visible

        api, _ = self.client(routed)
        await self.service.sync(api, INBOX, "Drafts")
        _, uid, resync = await self.service.append_draft(
            api, INBOX, DraftFields(to=("z@x",), subject="new", text="body"), None
        )
        await asyncio.gather(resync, return_exceptions=True)
        latest = self.service.latest(INBOX, "Drafts")
        self.assertIn(uid, latest.by_uid)  # pinned
        errors = await self.service.expunge_items(api, INBOX, "Drafts", [latest.by_uid[uid]])
        self.assertEqual(errors, [])
        self.assertEqual(len(deletes), 1)
        snap = await self.service.sync(api, INBOX, "Drafts", force=True)
        self.assertNotIn(uid, snap.by_uid)  # the pin died with the deletion
        self.assertNotIn(uid, self.store.live_map(INBOX, "Drafts").values())


class AuthoritativeSizeUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UidStore(Path(self.tmp.name) / "u.sqlite3")
        self.service = MailboxService(
            self.store, ByteCache(10_000_000), Settings(refresh_interval=0.0)
        )

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def client(self, router):
        t = ScriptedTransport(router=router)

        async def nosleep(_):
            return None

        return AgentMailClient(BASE, "k", t, max_attempts=1, sleep=nosleep), t

    async def test_list_size_is_ignored_and_reported_size_is_stable(self):
        body = b"x" * 40
        meta_size = {"value": 40}

        def router(call):
            if "/download" in call.url:
                return HttpResponse(200, {}, body)
            if call.url.endswith("/raw"):
                return json_response(
                    200,
                    {
                        "message_id": "m_old",
                        "size": meta_size["value"],
                        "download_url": "http://files/download",
                        "expires_at": "",
                    },
                )
            return FakeRouter()(call)  # list items claim size=100 for every message

        api, t = self.client(router)
        snap = await self.service.sync(api, INBOX, "INBOX")
        item = snap.items[0]
        self.assertEqual(item.size_hint, 100)  # what the list said
        self.assertEqual(await self.service.size_of(api, INBOX, "INBOX", item), 40)  # what raw says
        meta_calls = len([c for c in t.calls if c.url.endswith("/raw")])
        self.assertEqual(await self.service.size_of(api, INBOX, "INBOX", item), 40)
        self.assertEqual(
            len([c for c in t.calls if c.url.endswith("/raw")]), meta_calls
        )  # remembered
        # Upstream now claims a different size for the same message: refuse rather than serve a
        # body that contradicts the size already reported.
        meta_size["value"] = 41
        with self.assertRaises(UnavailableError):
            await self.service.raw_bytes(api, INBOX, "INBOX", item)
        meta_size["value"] = 40
        self.assertEqual(await self.service.raw_bytes(api, INBOX, "INBOX", item), body)
        self.assertEqual(await self.service.size_of(api, INBOX, "INBOX", item), 40)
