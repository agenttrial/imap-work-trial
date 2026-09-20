import unittest

from tests.support.gateway import GatewayTestCase


class ListTests(GatewayTestCase):
    async def test_list_all(self):
        c = await self.logged_in()
        resp = await c.cmd('LIST "" "*"')
        self.assertEqual(resp.status, "OK", resp)
        lines = sorted(u.line for u in resp.untagged)
        self.assertEqual(
            lines,
            sorted(
                b"* LIST (\\HasNoChildren) NIL " + name
                for name in (b"INBOX", b"Drafts", b"Sent", b"Trash", b"Spam")
            ),
        )

    async def test_list_percent_and_specific(self):
        c = await self.logged_in()
        resp = await c.cmd('LIST "" "%"')
        self.assertEqual(len(resp.untagged), 5)
        resp = await c.cmd('LIST "" INBOX')
        self.assertEqual([u.line for u in resp.untagged], [b"* LIST (\\HasNoChildren) NIL INBOX"])
        resp = await c.cmd('LIST "" inbox')
        self.assertEqual(len(resp.untagged), 1)
        resp = await c.cmd('LIST "" drafts')  # case-sensitive outside INBOX
        self.assertEqual(len(resp.untagged), 0)
        self.assertEqual(resp.status, "OK")
        resp = await c.cmd('LIST "" "Dra*"')
        self.assertEqual([u.line for u in resp.untagged], [b"* LIST (\\HasNoChildren) NIL Drafts"])

    async def test_list_root_returns_delimiter(self):
        c = await self.logged_in()
        resp = await c.cmd('LIST "" ""')
        self.assertEqual([u.line for u in resp.untagged], [b'* LIST (\\Noselect) NIL ""'])

    async def test_list_requires_auth_and_two_args(self):
        c = await self.client()
        self.assertEqual((await c.cmd('LIST "" "*"')).status, "BAD")
        c2 = await self.logged_in()
        self.assertEqual((await c2.cmd("LIST")).status, "BAD")


class SelectTests(GatewayTestCase):
    async def test_select_inbox(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual(resp.code(), "READ-WRITE")
        lines = [u.line for u in resp.untagged]
        self.assertIn(b"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)", lines)
        self.assertIn(b"* 3 EXISTS", lines)  # 4 received, one of them trashed and hidden
        self.assertIn(b"* 0 RECENT", lines)
        codes = resp.untagged_codes()
        self.assertEqual(codes["UNSEEN"], "1")
        self.assertEqual(codes["PERMANENTFLAGS"], "(\\Seen \\Flagged \\Deleted)")
        self.assertEqual(codes["UIDNEXT"], "4")
        self.assertTrue(int(codes["UIDVALIDITY"]) > 0)
        # The fake serves two messages per page: both pages must have been fetched.
        list_calls = [q for q in self.fake.requests() if q["path"].endswith("/messages")]
        self.assertEqual(len(list_calls), 2)

    async def test_select_lowercase_inbox(self):
        c = await self.logged_in()
        self.assertEqual((await c.cmd("SELECT inbox")).status, "OK")

    async def test_select_drafts(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT Drafts")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 2 EXISTS", [u.line for u in resp.untagged])
        self.assertNotIn("UNSEEN", resp.untagged_codes())

    async def test_examine_is_read_only(self):
        c = await self.logged_in()
        resp = await c.cmd("EXAMINE INBOX")
        self.assertEqual((resp.status, resp.code()), ("OK", "READ-ONLY"))

    async def test_select_unknown_deselects(self):
        c = await self.logged_in()
        self.assertEqual((await c.cmd("SELECT INBOX")).status, "OK")
        resp = await c.cmd("SELECT Nope")
        self.assertEqual(resp.status, "NO")
        self.assertIn("does not exist", resp.text)
        self.assertEqual((await c.cmd("UID FETCH 1 (FLAGS)")).status, "BAD")

    async def test_select_upstream_failure(self):
        c = await self.logged_in()
        self.fake.fail_next(
            method="GET",
            path_prefix="/inboxes/candidate%40imap.test/messages",
            status=503,
            times=10,
        )
        resp = await c.cmd("SELECT INBOX")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertEqual((await c.cmd("NOOP")).status, "OK")
        self.fake.reset()
        self.assertEqual((await c.cmd("SELECT INBOX")).status, "OK")

    async def test_close_returns_to_authenticated(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.assertEqual((await c.cmd("CLOSE")).status, "OK")
        self.assertEqual((await c.cmd("UID FETCH 1 (FLAGS)")).status, "BAD")
        self.assertEqual((await c.cmd("CLOSE")).status, "BAD")


if __name__ == "__main__":
    unittest.main()
