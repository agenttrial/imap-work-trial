import hashlib
import unittest

from tests.support.gateway import GatewayTestCase

# UIDs are allocated in ascending timestamp order on a fresh store.
UID_TO_ID = {
    1: "msg_received_ascii",
    2: "msg_received_utf8",
    3: "msg_received_attachment",
}


class FetchTests(GatewayTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.state = {m["message_id"]: m for m in self.fake.state()["messages"]}
        self.c = await self.logged_in()
        resp = await self.c.cmd("SELECT INBOX")
        self.assertEqual(resp.status, "OK", resp)

    async def test_metadata_for_all_messages(self):
        resp = await self.c.cmd("UID FETCH 1:* (FLAGS INTERNALDATE RFC822.SIZE)")
        self.assertEqual(resp.status, "OK", resp)
        by_uid = resp.fetch_by_uid()
        self.assertEqual(sorted(by_uid), [1, 2, 3])
        for uid, mid in UID_TO_ID.items():
            line = by_uid[uid].line
            self.assertIn(f"RFC822.SIZE {self.state[mid]['size']}".encode(), line)
        self.assertIn(b"FLAGS ()", by_uid[1].line)
        self.assertIn(b"FLAGS (\\Seen \\Flagged)", by_uid[2].line)
        self.assertIn(b'INTERNALDATE "17-Aug-2026 16:00:00 +0000"', by_uid[1].line)
        self.assertTrue(by_uid[1].line.startswith(b"* 1 FETCH (UID 1 "))
        self.assertIn(b"RFC822.SIZE 375", by_uid[2].line)

    async def test_bodies_match_fixture_bytes_exactly(self):
        resp = await self.c.cmd("UID FETCH 1:* (RFC822.SIZE BODY.PEEK[])")
        self.assertEqual(resp.status, "OK", resp)
        by_uid = resp.fetch_by_uid()
        self.assertEqual(len(by_uid), 3)
        for uid, mid in UID_TO_ID.items():
            u = by_uid[uid]
            self.assertIsNotNone(u.literal)
            self.assertEqual(hashlib.sha256(u.literal).hexdigest(), self.state[mid]["raw_sha256"])
            self.assertIn(f"RFC822.SIZE {len(u.literal)}".encode(), u.line)
            self.assertTrue(u.line.endswith(b"BODY[] {%d}" % len(u.literal)), u.line)
            self.assertEqual(u.tail, b")")
        # The UTF-8 fixture: 375 octets, fewer characters.
        utf8 = by_uid[2].literal
        self.assertEqual(len(utf8), 375)
        self.assertLess(len(utf8.decode("utf-8")), 375)

    async def test_download_url_never_receives_the_api_key(self):
        await self.c.cmd("UID FETCH 2 BODY.PEEK[]")
        raw_requests = [q for q in self.fake.requests() if q["path"].startswith("/raw/")]
        self.assertTrue(raw_requests)
        self.assertTrue(all(q["authorization_present"] is False for q in raw_requests))
        meta_requests = [q for q in self.fake.requests() if q["path"].endswith("/raw")]
        self.assertTrue(all(q["authorization_present"] is True for q in meta_requests))

    async def test_rfc822_and_body_variants_agree(self):
        peek = (await self.c.cmd("UID FETCH 1 BODY.PEEK[]")).fetch_by_uid()[1].literal
        rfc822 = (await self.c.cmd("UID FETCH 1 RFC822")).fetch_by_uid()[1]
        body = (await self.c.cmd("UID FETCH 1 BODY[]")).fetch_by_uid()[1]
        self.assertEqual(rfc822.literal, peek)
        self.assertEqual(body.literal, peek)
        self.assertIn(b"RFC822 {", rfc822.line)
        self.assertIn(b"BODY[] {", body.line)

    async def test_sections(self):
        full = (await self.c.cmd("UID FETCH 1 BODY.PEEK[]")).fetch_by_uid()[1].literal
        header = (await self.c.cmd("UID FETCH 1 BODY.PEEK[HEADER]")).fetch_by_uid()[1]
        text = (await self.c.cmd("UID FETCH 1 BODY.PEEK[TEXT]")).fetch_by_uid()[1]
        self.assertTrue(header.literal.startswith(b"From: "))
        self.assertTrue(header.literal.endswith(b"\r\n\r\n"))
        self.assertEqual(header.literal + text.literal, full)
        self.assertIn(b"BODY[HEADER] {", header.line)
        fields = (
            await self.c.cmd("UID FETCH 1 BODY.PEEK[HEADER.FIELDS (Subject)]")
        ).fetch_by_uid()[1]
        self.assertEqual(fields.literal, b"Subject: A small deterministic message\r\n\r\n")
        self.assertIn(b"BODY[HEADER.FIELDS (SUBJECT)] {", fields.line)
        partial = (await self.c.cmd("UID FETCH 1 BODY.PEEK[]<0.10>")).fetch_by_uid()[1]
        self.assertEqual(partial.literal, full[:10])
        self.assertIn(b"BODY[]<0> {10}", partial.line)
        rfc_header = (await self.c.cmd("UID FETCH 1 RFC822.HEADER")).fetch_by_uid()[1]
        self.assertEqual(rfc_header.literal, header.literal)

    async def test_multiple_attributes_with_bodies(self):
        resp = await self.c.cmd(
            "UID FETCH 3 (UID FLAGS BODY.PEEK[HEADER.FIELDS (From To Subject)] RFC822.SIZE)"
        )
        u = resp.fetch_by_uid()[3]
        self.assertTrue(
            u.line.startswith(b"* 3 FETCH (UID 3 FLAGS () BODY[HEADER.FIELDS (FROM TO SUBJECT)] {")
        )
        self.assertIn(b"Subject: Multipart fixture\r\n", u.literal)
        self.assertTrue(u.tail.startswith(b" RFC822.SIZE "), u.tail)
        self.assertTrue(u.tail.endswith(b")"))

    async def test_nonexistent_uid_is_ignored(self):
        resp = await self.c.cmd("UID FETCH 999 (FLAGS)")
        self.assertEqual((resp.status, resp.untagged), ("OK", []))
        resp = await self.c.cmd("UID FETCH 2:* (FLAGS)")
        self.assertEqual(sorted(resp.fetch_by_uid()), [2, 3])
        resp = await self.c.cmd("UID FETCH 900:* (FLAGS)")
        self.assertEqual(sorted(resp.fetch_by_uid()), [3])

    async def test_sequence_number_fetch(self):
        resp = await self.c.cmd("FETCH 2 (UID FLAGS)")
        self.assertEqual(
            [u.line for u in resp.untagged], [b"* 2 FETCH (UID 2 FLAGS (\\Seen \\Flagged))"]
        )
        resp = await self.c.cmd("FETCH 1:2 UID")
        self.assertEqual(sorted(resp.fetch_by_seq()), [1, 2])
        resp = await self.c.cmd("FETCH 99 UID")
        self.assertEqual(resp.untagged, [])

    async def test_fast_macro_and_uid_always_included(self):
        resp = await self.c.cmd("UID FETCH 1 FAST")
        u = resp.fetch_by_uid()[1]
        for part in (b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE", b"UID 1"):
            self.assertIn(part, u.line)

    async def test_bodies_are_cached(self):
        await self.c.cmd("UID FETCH 1:* BODY.PEEK[]")
        before = len([q for q in self.fake.requests() if q["path"].startswith("/raw/")])
        await self.c.cmd("UID FETCH 1:* BODY.PEEK[]")
        after = len([q for q in self.fake.requests() if q["path"].startswith("/raw/")])
        self.assertEqual(before, 3)
        self.assertEqual(after, 3)

    async def test_unsupported_and_malformed(self):
        for text in (
            "UID FETCH 1 (ENVELOPE)",
            "UID FETCH 1 BODYSTRUCTURE",
            "UID FETCH 1 ALL",
            "UID FETCH 1 BODY[1]",
        ):
            with self.subTest(text=text):
                resp = await self.c.cmd(text)
                self.assertEqual(resp.status, "BAD", resp)
                self.assertIn("unsupported", resp.text)
        for text in (
            "UID FETCH",
            "UID FETCH x (FLAGS)",
            "UID FETCH 1",
            "UID FETCH 1 NOPE",
            "UID FETCH 1 (BODY[)",
        ):
            with self.subTest(text=text):
                self.assertEqual((await self.c.cmd(text)).status, "BAD")
        self.assertEqual((await self.c.cmd("UID COPY 1 INBOX")).status, "NO")
        self.assertEqual((await self.c.cmd("NOOP")).status, "OK")

    async def test_upstream_failure_during_body_fetch(self):
        self.fake.fail_next(
            method="GET",
            path_prefix="/inboxes/candidate%40imap.test/messages/msg_received_ascii/raw",
            status=503,
            times=10,
        )
        resp = await self.c.cmd("UID FETCH 1 BODY.PEEK[]")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertEqual((await self.c.cmd("UID FETCH 2 (FLAGS)")).status, "OK")


if __name__ == "__main__":
    unittest.main()


class AuthoritativeSizeTests(GatewayTestCase):
    """RFC822.SIZE comes from the raw endpoint (or the bytes), never from the list field, and is
    remembered so a UID's reported size never changes (D7, reversed after the second review)."""

    async def test_metadata_fetch_uses_raw_endpoint_once_and_never_downloads(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID FETCH 1:* (RFC822.SIZE)")
        self.assertEqual(resp.status, "OK", resp)
        by_uid = resp.fetch_by_uid()
        state = {m["message_id"]: m for m in self.fake.state()["messages"]}
        self.assertIn(f"RFC822.SIZE {state['msg_received_utf8']['size']}".encode(), by_uid[2].line)
        reqs = self.fake.requests()
        self.assertEqual(len([q for q in reqs if q["path"].endswith("/raw")]), 3)  # one per message
        self.assertEqual([q for q in reqs if q["path"].startswith("/raw/")], [])  # no downloads
        await c.cmd("UID FETCH 1:* (RFC822.SIZE)")
        self.assertEqual(len([q for q in self.fake.requests() if q["path"].endswith("/raw")]), 3)

    async def test_size_then_body_agree(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        size_line = (await c.cmd("UID FETCH 2 (RFC822.SIZE)")).fetch_by_uid()[2].line
        body = (await c.cmd("UID FETCH 2 BODY.PEEK[]")).fetch_by_uid()[2]
        self.assertIn(f"RFC822.SIZE {len(body.literal)}".encode(), size_line)
        self.assertEqual(len(body.literal), 375)
