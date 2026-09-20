"""Label views beyond INBOX and Drafts: Sent, Trash, Spam, and the hidden-from-INBOX rule."""

import unittest

from tests.support.gateway import GatewayTestCase


class ViewTests(GatewayTestCase):
    async def test_inbox_hides_trashed_message(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        self.assertIn(b"* 3 EXISTS", [u.line for u in resp.untagged])
        subjects = await c.cmd("UID FETCH 1:* BODY.PEEK[HEADER.FIELDS (Subject)]")
        texts = b"".join(u.literal for u in subjects.untagged)
        self.assertNotIn(b"A message in two mailboxes", texts)
        self.assertIn(b"A small deterministic message", texts)

    async def test_trash_shows_trash_only_and_multi_label(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Trash")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])
        fetched = await c.cmd("UID FETCH 1:* (FLAGS BODY.PEEK[HEADER.FIELDS (Subject)])")
        by_uid = fetched.fetch_by_uid()
        self.assertEqual(sorted(by_uid), [1, 2])
        self.assertIn(b"Subject: Trash-only message", by_uid[1].literal)  # 20:00, older
        self.assertIn(b"Subject: A message in two mailboxes", by_uid[2].literal)  # 21:00
        self.assertIn(b"FLAGS (\\Seen)", by_uid[1].line)
        self.assertIn(b"FLAGS ()", by_uid[2].line)  # still unread
        # (The fake logs paths without query strings; the query shape is covered in
        # tests.unit.test_apiclient and the presence of trashed items here proves it worked.)

    async def test_sent_view(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Sent")
        self.assertIn(b"* 1 EXISTS", [u.line for u in resp.untagged])
        u = (await c.cmd("UID FETCH 1 (FLAGS BODY.PEEK[HEADER.FIELDS (From To)])")).fetch_by_uid()[
            1
        ]
        self.assertIn(b"FLAGS (\\Seen)", u.line)
        self.assertIn(b"From: candidate@imap.test", u.literal)
        self.assertIn(b"To: recipient@example.com", u.literal)

    async def test_spam_is_empty_but_selectable(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Spam")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 0 EXISTS", [u.line for u in resp.untagged])
        self.assertEqual(resp.untagged_codes()["UIDNEXT"], "1")
        self.assertEqual((await c.cmd("UID FETCH 1:* (FLAGS)")).untagged, [])

    async def test_each_view_has_independent_uids(self):
        c = await self.logged_in()
        inbox = (await c.cmd("SELECT INBOX")).untagged_codes()
        trash = (await c.cmd("SELECT Trash")).untagged_codes()
        self.assertEqual((inbox["UIDNEXT"], trash["UIDNEXT"]), ("4", "3"))
        # UID 1 in INBOX and UID 1 in Trash are different messages.
        t1 = (await c.cmd("UID FETCH 1 BODY.PEEK[HEADER.FIELDS (Message-ID)]")).fetch_by_uid()[1]
        await c.cmd("SELECT INBOX")
        i1 = (await c.cmd("UID FETCH 1 BODY.PEEK[HEADER.FIELDS (Message-ID)]")).fetch_by_uid()[1]
        self.assertNotEqual(t1.literal, i1.literal)

    async def test_mailbox_names_are_case_sensitive_except_inbox(self):
        c = await self.logged_in()
        self.assertEqual((await c.cmd("SELECT trash")).status, "NO")
        self.assertEqual((await c.cmd("SELECT Trash")).status, "OK")
        self.assertEqual((await c.cmd("SELECT iNbOx")).status, "OK")


if __name__ == "__main__":
    unittest.main()
