import unittest

from tests.support.gateway import GatewayTestCase


class StoreTests(GatewayTestCase):
    def labels_of(self, message_id: str) -> list[str]:
        return next(
            m["labels"] for m in self.fake.state()["messages"] if m["message_id"] == message_id
        )

    async def test_seen_round_trip_changes_labels(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        self.assertEqual(resp.untagged_codes()["PERMANENTFLAGS"], "(\\Seen \\Flagged \\Deleted)")
        self.assertEqual(self.labels_of("msg_received_ascii"), ["received", "unread"])

        resp = await c.cmd("UID STORE 1 +FLAGS (\\Seen)")
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS (\\Seen))"])
        labels = self.labels_of("msg_received_ascii")
        self.assertIn("read", labels)
        self.assertNotIn("unread", labels)
        # The change is visible to a fresh FETCH and to UNSEEN without a re-sync.
        self.assertIn(
            b"FLAGS (\\Seen)", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line
        )
        self.assertEqual((await c.cmd("UID SEARCH UNSEEN")).untagged[0].line, b"* SEARCH 3")

        resp = await c.cmd("UID STORE 1 -FLAGS (\\Seen)")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS ())"])
        labels = self.labels_of("msg_received_ascii")
        self.assertIn("unread", labels)
        self.assertNotIn("read", labels)

    async def test_silent_and_sequence_forms(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID STORE 1,3 +FLAGS.SILENT (\\Seen)")
        self.assertEqual((resp.status, resp.untagged), ("OK", []))
        self.assertIn("read", self.labels_of("msg_received_attachment"))
        resp = await c.cmd("STORE 1 -FLAGS (\\Seen)")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (FLAGS ())"])

    async def test_flagged_maps_to_starred_and_replace_mode(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID STORE 1 FLAGS (\\Flagged)")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS (\\Flagged))"])
        self.assertIn("starred", self.labels_of("msg_received_ascii"))
        resp = await c.cmd("UID STORE 2 FLAGS ()")  # clears \Seen and \Flagged on the utf8 message
        self.assertEqual([u.line for u in resp.untagged], [b"* 2 FETCH (UID 2 FLAGS ())"])
        labels = self.labels_of("msg_received_utf8")
        self.assertNotIn("starred", labels)
        self.assertIn("unread", labels)

    async def test_unsupported_flags_and_keywords(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Answered)")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        resp = await c.cmd("UID STORE 1 +FLAGS (custom)")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        self.assertEqual((await c.cmd("UID STORE 1 FLAGZ (\\Seen)")).status, "BAD")
        self.assertEqual((await c.cmd("UID STORE 1 +FLAGS")).status, "BAD")
        self.assertEqual(self.labels_of("msg_received_ascii"), ["received", "unread"])

    async def test_read_only_mailbox_rejects_store(self):
        c = await self.logged_in()
        await c.cmd("EXAMINE INBOX")
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Seen)")
        self.assertEqual((resp.status, resp.code()), ("NO", "READ-ONLY"))

    async def test_drafts_only_allow_deleted(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Drafts")
        self.assertEqual(resp.untagged_codes()["PERMANENTFLAGS"], "(\\Deleted)")
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Seen)")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Deleted)")
        self.assertEqual(
            [u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS (\\Seen \\Deleted \\Draft))"]
        )

    async def test_non_peek_body_fetch_sets_seen(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID FETCH 3 BODY.PEEK[]")
        self.assertNotIn(b"FLAGS", resp.fetch_by_uid()[3].line + resp.fetch_by_uid()[3].tail)
        self.assertIn("unread", self.labels_of("msg_received_attachment"))
        resp = await c.cmd("UID FETCH 3 BODY[]")
        u = resp.fetch_by_uid()[3]
        self.assertIn(b"FLAGS (\\Seen)", u.tail)
        self.assertIn("read", self.labels_of("msg_received_attachment"))
        # RFC822 also implies \Seen; already seen, so no FLAGS item is added.
        resp = await c.cmd("UID FETCH 3 RFC822")
        self.assertNotIn(b"FLAGS", resp.fetch_by_uid()[3].tail)

    async def test_upstream_failure_leaves_flags(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.fake.fail_next(method="PATCH", path_prefix="/inboxes", status=503, times=10)
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Seen)")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertEqual(self.labels_of("msg_received_ascii"), ["received", "unread"])
        self.assertIn(b"FLAGS ()", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)


if __name__ == "__main__":
    unittest.main()


class PeekMergeTests(GatewayTestCase):
    async def test_peek_plus_non_peek_still_sets_seen(self):  # F21
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID FETCH 1 (BODY.PEEK[] BODY[])")
        self.assertEqual(resp.status, "OK", resp)
        u = resp.fetch_by_uid()[1]
        self.assertEqual(len(u.literals), 1)
        self.assertIn(b"FLAGS (\\Seen)", u.tail)
        labels = next(
            m["labels"]
            for m in self.fake.state()["messages"]
            if m["message_id"] == "msg_received_ascii"
        )
        self.assertIn("read", labels)


class FlagNotificationTests(GatewayTestCase):
    """Review finding F15: NOOP announces flag changes made elsewhere."""

    async def test_noop_reports_flags_changed_by_another_session(self):
        a = await self.logged_in()
        b = await self.logged_in()
        await a.cmd("SELECT INBOX")
        await b.cmd("SELECT INBOX")
        self.assertEqual((await b.cmd("NOOP")).untagged, [])
        await a.cmd("UID STORE 1 +FLAGS.SILENT (\\Seen)")
        resp = await b.cmd("NOOP")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS (\\Seen))"])
        # Announced once; a quiet NOOP stays quiet.
        self.assertEqual((await b.cmd("NOOP")).untagged, [])
        await a.cmd("UID STORE 3 +FLAGS.SILENT (\\Deleted)")
        resp = await b.cmd("NOOP")
        self.assertEqual([u.line for u in resp.untagged], [b"* 3 FETCH (UID 3 FLAGS (\\Deleted))"])

    async def test_own_store_is_not_re_announced(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 1 +FLAGS (\\Seen)")
        self.assertEqual((await c.cmd("NOOP")).untagged, [])
        await c.cmd("UID FETCH 3 BODY[]")  # implicit \Seen, reported in the FETCH itself
        self.assertEqual((await c.cmd("NOOP")).untagged, [])
