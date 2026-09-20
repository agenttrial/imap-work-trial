import unittest
from email import message_from_bytes, policy

from tests.support.gateway import GatewayTestCase


class DraftsFetchTests(GatewayTestCase):
    async def test_drafts_render_with_all_fields(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Drafts")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])
        self.assertEqual(resp.untagged_codes()["UIDNEXT"], "3")

        resp = await c.cmd("UID FETCH 1:* (FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[])")
        self.assertEqual(resp.status, "OK", resp)
        by_uid = resp.fetch_by_uid()
        self.assertEqual(sorted(by_uid), [1, 2])
        for u in by_uid.values():
            self.assertIn(b"FLAGS (\\Seen \\Draft)", u.line)
            self.assertIn(f"RFC822.SIZE {len(u.literal)}".encode(), u.line)
            self.assertNotIn(b"\n", u.literal.replace(b"\r\n", b""))

        # UID 1 is the older draft (updated 22:00), UID 2 the newer one (23:00).
        self.assertIn(b'INTERNALDATE "17-Aug-2026 22:00:00 +0000"', by_uid[1].line)
        simple = message_from_bytes(by_uid[1].literal, policy=policy.default)
        self.assertEqual(simple["Subject"], "Existing review draft")
        self.assertEqual(str(simple["To"]), "reviewer@example.com")
        self.assertIsNone(simple["Cc"])

        msg = message_from_bytes(by_uid[2].literal, policy=policy.default)
        self.assertEqual(msg["From"], "candidate@imap.test")
        self.assertEqual(str(msg["To"]), "primary@example.net")
        self.assertEqual(str(msg["Cc"]), "copy@example.net")
        self.assertEqual(str(msg["Bcc"]), "audit@example.net")
        self.assertEqual(str(msg["Reply-To"]), "replies@example.net")
        self.assertEqual(msg["Subject"], "Draft with address fields")
        self.assertEqual(msg["X-AgentMail-Draft-Id"], "draft_existing_addresses")
        self.assertEqual(msg["Message-ID"], "<draft_existing_addresses@imap.test>")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertEqual(
            msg.get_content().replace("\r\n", "\n").rstrip("\n"),
            "Please keep all recipients on this draft.",
        )

    async def test_draft_listing_is_fully_paginated_and_bodies_fetched_once(self):
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")
        reqs = self.fake.requests()
        list_calls = [q for q in reqs if q["path"].endswith("/drafts")]
        get_calls = [q for q in reqs if "/drafts/draft_" in q["path"]]
        self.assertEqual(len(list_calls), 2)  # page size 1, two drafts
        self.assertEqual(len(get_calls), 2)
        await c.cmd("NOOP")  # re-sync: listing again, but no new per-draft GETs
        reqs = self.fake.requests()
        self.assertEqual(len([q for q in reqs if "/drafts/draft_" in q["path"]]), 2)

    async def test_header_sections_on_drafts(self):
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")
        u = (await c.cmd("UID FETCH 2 BODY.PEEK[HEADER.FIELDS (To Cc Bcc)]")).fetch_by_uid()[2]
        self.assertEqual(
            u.literal,
            b"To: primary@example.net\r\nCc: copy@example.net\r\nBcc: audit@example.net\r\n\r\n",
        )

    async def test_sizes_are_byte_counts_for_non_ascii_drafts(self):
        # Round-trip through APPEND so the mailbox contains a non-ASCII draft, then check size.
        c = await self.logged_in()
        raw = (
            b"To: x@example.com\r\nSubject: =?UTF-8?B?Y2Fmw6k=?=\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            + "naïve 東京 \U0001f680\r\n".encode()
        )
        resp = await c.cmd_literal("APPEND Drafts", raw)
        self.assertEqual(resp.status, "OK", resp)
        await c.cmd("SELECT Drafts")
        by_uid = (await c.cmd("UID FETCH 3 (RFC822.SIZE BODY.PEEK[])")).fetch_by_uid()
        u = by_uid[3]
        self.assertIn(f"RFC822.SIZE {len(u.literal)}".encode(), u.line)
        self.assertGreater(len(u.literal), len(u.literal.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
