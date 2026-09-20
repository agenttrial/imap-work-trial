import unittest

from tests.support.gateway import GatewayTestCase


class ExpungeTests(GatewayTestCase):
    def labels_of(self, message_id: str) -> list[str]:
        return next(
            m["labels"] for m in self.fake.state()["messages"] if m["message_id"] == message_id
        )

    async def test_deleted_then_expunge_moves_to_trash(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Deleted)")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 1 FLAGS (\\Deleted))"])
        self.assertNotIn(
            "trash", self.labels_of("msg_received_ascii")
        )  # session-local until EXPUNGE

        resp = await c.cmd("EXPUNGE")
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 EXPUNGE"])
        self.assertIn("trash", self.labels_of("msg_received_ascii"))
        # Sequence numbers shifted: what was seq 2 is now seq 1 and still UID 2.
        resp = await c.cmd("FETCH 1 (UID)")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 FETCH (UID 2)"])
        self.assertEqual(sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()), [2, 3])

        resp = await c.cmd("SELECT INBOX")
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])
        self.assertEqual(resp.untagged_codes()["UIDNEXT"], "4")  # UID 1 is never reused
        resp = await c.cmd("SELECT Trash")
        self.assertIn(b"* 3 EXISTS", [u.line for u in resp.untagged])
        subjects = await c.cmd("UID FETCH 1:* BODY.PEEK[HEADER.FIELDS (Subject)]")
        self.assertIn(
            b"A small deterministic message", b"".join(u.literal for u in subjects.untagged)
        )

    async def test_uid_expunge_subset(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 1,3 +FLAGS.SILENT (\\Deleted)")
        resp = await c.cmd("UID EXPUNGE 3")
        self.assertEqual([u.line for u in resp.untagged], [b"* 3 EXPUNGE"])
        self.assertIn("trash", self.labels_of("msg_received_attachment"))
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))
        self.assertIn(b"\\Deleted", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)

    async def test_expunge_with_nothing_deleted(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("EXPUNGE")
        self.assertEqual((resp.status, resp.untagged), ("OK", []))

    async def test_close_expunges_silently(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 2 +FLAGS.SILENT (\\Deleted)")
        resp = await c.cmd("CLOSE")
        self.assertEqual((resp.status, resp.untagged), ("OK", []))
        self.assertIn("trash", self.labels_of("msg_received_utf8"))
        resp = await c.cmd("SELECT INBOX")
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])

    async def test_examine_close_does_not_expunge(self):
        c = await self.logged_in()
        await c.cmd("EXAMINE INBOX")
        resp = await c.cmd("EXPUNGE")
        self.assertEqual((resp.status, resp.code()), ("NO", "READ-ONLY"))
        self.assertEqual((await c.cmd("CLOSE")).status, "OK")
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))

    async def test_trash_and_spam_cannot_expunge(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Trash")
        self.assertEqual(resp.untagged_codes()["PERMANENTFLAGS"], "(\\Seen \\Flagged)")
        resp = await c.cmd("UID STORE 1 +FLAGS (\\Deleted)")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        self.assertEqual((await c.cmd("EXPUNGE")).status, "OK")

    async def test_draft_expunge_uses_delete_endpoint(self):
        # The fake has no DELETE /drafts route, so this exercises the failure path end to end;
        # the success path is covered by tests.unit.test_mailbox_sync.
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")
        await c.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        resp = await c.cmd("EXPUNGE")
        self.assertEqual(resp.status, "NO", resp)
        self.assertEqual(len(self.fake.state()["drafts"]), 2)
        delete_calls = [q for q in self.fake.requests() if q["method"] == "DELETE"]
        self.assertEqual(len(delete_calls), 1)
        self.assertTrue(delete_calls[0]["path"].endswith("/drafts/draft_existing_simple"))
        # Still marked for deletion so a later EXPUNGE can retry.
        self.assertIn(b"\\Deleted", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)

    async def test_upstream_failure_during_expunge(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        self.fake.fail_next(method="PATCH", path_prefix="/inboxes", status=503, times=10)
        resp = await c.cmd("EXPUNGE")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))
        self.fake.reset()
        self.assertEqual((await c.cmd("EXPUNGE")).status, "OK")
        self.assertIn("trash", self.labels_of("msg_received_ascii"))


class StatusAndStubTests(GatewayTestCase):
    async def test_status(self):
        c = await self.logged_in()
        resp = await c.cmd("STATUS INBOX (MESSAGES UNSEEN UIDNEXT UIDVALIDITY RECENT)")
        self.assertEqual(resp.status, "OK", resp)
        line = resp.untagged[0].line
        self.assertTrue(
            line.startswith(b"* STATUS INBOX (MESSAGES 3 UNSEEN 2 UIDNEXT 4 UIDVALIDITY "), line
        )
        self.assertTrue(line.endswith(b" RECENT 0)"))
        resp = await c.cmd("STATUS Drafts (MESSAGES)")
        self.assertEqual(resp.untagged[0].line, b"* STATUS Drafts (MESSAGES 2)")
        self.assertEqual((await c.cmd("STATUS Nope (MESSAGES)")).status, "NO")
        self.assertEqual((await c.cmd("STATUS INBOX (BOGUS)")).status, "BAD")

    async def test_id_namespace_subscribe_check(self):
        c = await self.logged_in()
        resp = await c.cmd('ID ("name" "test")')
        self.assertEqual(resp.status, "OK")
        self.assertTrue(resp.untagged[0].line.startswith(b"* ID ("))
        resp = await c.cmd("NAMESPACE")
        self.assertEqual(resp.untagged[0].line, b'* NAMESPACE (("" NIL)) NIL NIL')
        self.assertEqual((await c.cmd("SUBSCRIBE INBOX")).status, "OK")
        self.assertEqual((await c.cmd("SUBSCRIBE Nope")).status, "NO")
        self.assertEqual((await c.cmd('LSUB "" "*"')).status, "OK")
        self.assertEqual(len((await c.cmd('LSUB "" "*"')).untagged), 5)
        self.assertEqual((await c.cmd("CHECK")).status, "BAD")  # not selected
        await c.cmd("SELECT INBOX")
        self.assertEqual((await c.cmd("CHECK")).status, "OK")
        for text in (
            "CREATE Foo",
            "DELETE INBOX",
            "RENAME INBOX Bar",
            "COPY 1 Trash",
            "IDLE",
            "STARTTLS",
        ):
            with self.subTest(text=text):
                resp = await c.cmd(text)
                self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))


if __name__ == "__main__":
    unittest.main()


class PersistedDeletedTests(GatewayTestCase):
    """\\Deleted is advertised in PERMANENTFLAGS, so it must survive sessions and restarts and
    be visible to other sessions (review: F16 and the PERMANENTFLAGS objection)."""

    def labels_of(self, message_id: str) -> list[str]:
        return next(
            m["labels"] for m in self.fake.state()["messages"] if m["message_id"] == message_id
        )

    async def test_mark_visible_to_other_session_and_after_restart(self):
        a = await self.logged_in()
        b = await self.logged_in()
        await a.cmd("SELECT INBOX")
        await b.cmd("SELECT INBOX")
        await a.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        self.assertIn(b"\\Deleted", (await b.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)
        await a.close()
        await b.close()
        self._clients.clear()
        await self.restart_gateway()
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.assertIn(b"\\Deleted", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)
        self.assertEqual((await c.cmd("UID SEARCH DELETED")).untagged[0].line, b"* SEARCH 1")
        resp = await c.cmd("EXPUNGE")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 EXPUNGE"])
        self.assertIn("trash", self.labels_of("msg_received_ascii"))

    async def test_close_reports_failed_removal_and_keeps_the_mark(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        self.fake.fail_next(method="PATCH", path_prefix="/inboxes", status=503, times=10)
        resp = await c.cmd("CLOSE")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))
        # Still selected, mark still present, so the client can retry.
        self.assertIn(b"\\Deleted", (await c.cmd("UID FETCH 1 (FLAGS)")).fetch_by_uid()[1].line)
        self.fake.reset()
        self.assertEqual((await c.cmd("CLOSE")).status, "OK")
        self.assertIn("trash", self.labels_of("msg_received_ascii"))

    async def test_unmark_clears_persisted_state(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        await c.cmd("UID STORE 2 +FLAGS.SILENT (\\Deleted)")
        await c.cmd("UID STORE 2 -FLAGS.SILENT (\\Deleted)")
        d = await self.logged_in()
        await d.cmd("SELECT INBOX")
        self.assertNotIn(b"\\Deleted", (await d.cmd("UID FETCH 2 (FLAGS)")).fetch_by_uid()[2].line)
        self.assertEqual((await d.cmd("EXPUNGE")).untagged, [])


class UidCommandNotificationTests(GatewayTestCase):
    async def test_flag_notifications_during_uid_expunge_carry_uid(self):  # re-review finding 6
        a = await self.logged_in()
        b = await self.logged_in()
        await a.cmd("SELECT INBOX")
        await b.cmd("SELECT INBOX")
        await a.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        await a.cmd("UID STORE 2 -FLAGS.SILENT (\\Flagged)")
        resp = await b.cmd("UID EXPUNGE 1")
        self.assertEqual(resp.status, "OK", resp)
        lines = [u.line for u in resp.untagged]
        self.assertEqual(lines[0], b"* 1 EXPUNGE")
        fetches = [line for line in lines if b" FETCH " in line]
        self.assertEqual(fetches, [b"* 1 FETCH (UID 2 FLAGS (\\Seen))"])
