"""COPY and MOVE into Trash: the delete path desktop clients actually use.

Thunderbird (observed 2026-09-20) deletes a message with ``UID COPY n "Trash"`` followed by
``UID STORE n +FLAGS (\\Deleted)`` and ``EXPUNGE``, or with ``UID MOVE`` when the server
advertises MOVE. Both are mapped to "add the trash label", the same write EXPUNGE makes.
Fixture facts: INBOX UIDs 1-3, Trash has 2 messages (UIDs 1-2), so the first message copied
into Trash gets Trash UID 3.
"""

import unittest

from tests.support.gateway import GatewayTestCase


class CopyMoveTests(GatewayTestCase):
    def labels_of(self, message_id: str) -> list[str]:
        return next(
            m["labels"] for m in self.fake.state()["messages"] if m["message_id"] == message_id
        )

    async def test_move_is_advertised(self):
        c = await self.client()
        self.assertIn(b" MOVE ", c.greeting)

    async def test_uid_copy_to_trash(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID COPY 1 Trash")
        self.assertEqual(resp.status, "OK", resp)
        validity = (await c.cmd("STATUS Trash (UIDVALIDITY)")).untagged[0].line
        self.assertEqual(resp.code(), f"COPYUID {validity.split()[-1][:-1].decode()} 1 3")
        self.assertIn("trash", self.labels_of("msg_received_ascii"))
        # The source view is only re-listed at a safe point; NOOP announces the removal.
        resp = await c.cmd("NOOP")
        self.assertEqual([u.line for u in resp.untagged], [b"* 1 EXPUNGE"])
        resp = await c.cmd("SELECT Trash")
        self.assertIn(b"* 3 EXISTS", [u.line for u in resp.untagged])
        u = (await c.cmd("UID FETCH 3 (FLAGS BODY.PEEK[HEADER.FIELDS (Subject)])")).fetch_by_uid()
        self.assertIn(b"A small deterministic message", u[3].literal)
        self.assertIn(b"FLAGS ()", u[3].line)  # still unread

    async def test_thunderbird_delete_sequence(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.assertEqual((await c.cmd("UID COPY 2 Trash")).status, "OK")
        resp = await c.cmd("UID STORE 2 +FLAGS (\\Deleted)")
        self.assertEqual(resp.status, "OK", resp)
        resp = await c.cmd("EXPUNGE")
        self.assertEqual((resp.status, [u.line for u in resp.untagged]), ("OK", [b"* 2 EXPUNGE"]))
        self.assertEqual(sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()), [1, 3])
        self.assertIn("trash", self.labels_of("msg_received_utf8"))
        resp = await c.cmd("SELECT INBOX")
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])

    async def test_uid_move(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID MOVE 1,3 Trash")
        self.assertEqual(resp.status, "OK", resp)
        lines = [u.line for u in resp.untagged]
        self.assertTrue(lines[0].startswith(b"* OK [COPYUID "), lines)
        self.assertTrue(lines[0].endswith(b" 1,3 3:4] Moved"), lines)
        self.assertEqual(lines[1:], [b"* 3 EXPUNGE", b"* 1 EXPUNGE"])
        self.assertEqual(sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()), [2])
        self.assertIn("trash", self.labels_of("msg_received_ascii"))
        self.assertIn("trash", self.labels_of("msg_received_attachment"))
        resp = await c.cmd("SELECT Trash")
        self.assertIn(b"* 4 EXISTS", [u.line for u in resp.untagged])
        self.assertEqual(
            sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()), [1, 2, 3, 4]
        )

    async def test_sequence_number_forms(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("COPY 2 Trash")
        self.assertEqual(resp.status, "OK", resp)
        self.assertTrue(resp.code().endswith(" 2 3"), resp)
        resp = await c.cmd("MOVE 1 Trash")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 1 EXPUNGE", [u.line for u in resp.untagged])

    async def test_trash_uids_survive_restart(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        code = (await c.cmd("UID COPY 3 Trash")).code()
        await self.restart_gateway()
        c = await self.logged_in()
        await c.cmd("SELECT Trash")
        u = (await c.cmd("UID FETCH 3 BODY.PEEK[HEADER.FIELDS (Subject)]")).fetch_by_uid()
        self.assertIn(b"Multipart fixture", u[3].literal)
        self.assertTrue(code.endswith(" 3 3"), code)

    async def test_refusals(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        for text, code in (
            ("UID COPY 1 Sent", "CANNOT"),
            ("UID MOVE 1 INBOX", "CANNOT"),
            ("UID COPY 1 Nope", "TRYCREATE"),
        ):
            with self.subTest(text=text):
                resp = await c.cmd(text)
                self.assertEqual((resp.status, resp.code()), ("NO", code), resp)
        self.assertEqual((await c.cmd("UID COPY 1")).status, "BAD")
        self.assertEqual((await c.cmd("UID COPY x Trash")).status, "BAD")
        # Nonexistent UIDs are ignored, nothing is written.
        resp = await c.cmd("UID COPY 999 Trash")
        self.assertEqual((resp.status, resp.code()), ("OK", None))
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))
        await c.cmd("SELECT Drafts")
        resp = await c.cmd("UID COPY 1 Trash")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        await c.cmd("EXAMINE INBOX")
        resp = await c.cmd("UID MOVE 1 Trash")
        self.assertEqual((resp.status, resp.code()), ("NO", "READ-ONLY"))
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))

    async def test_upstream_failure_is_reported(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.fake.fail_next(
            method="PATCH",
            path_prefix="/inboxes/candidate%40imap.test/messages/msg_received_ascii",
            status=503,
            times=10,
        )
        resp = await c.cmd("UID COPY 1 Trash")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"), resp)
        self.assertNotIn("trash", self.labels_of("msg_received_ascii"))
        self.assertEqual((await c.cmd("UID FETCH 1 (FLAGS)")).status, "OK")


if __name__ == "__main__":
    unittest.main()
