import asyncio
import unittest

from tests.support.gateway import GatewayTestCase


class PersistenceTests(GatewayTestCase):
    async def test_uids_survive_restart_and_new_mail_gets_next_uid(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        codes = resp.untagged_codes()
        validity, uidnext = int(codes["UIDVALIDITY"]), int(codes["UIDNEXT"])
        before = sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid())
        self.assertEqual(before, [1, 2, 3])
        await c.close()
        self._clients.remove(c)

        await self.restart_gateway()
        self.fake.add_message()  # arrives while the gateway is "down"

        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        codes = resp.untagged_codes()
        self.assertEqual(int(codes["UIDVALIDITY"]), validity)
        self.assertEqual(int(codes["UIDNEXT"]), uidnext + 1)
        self.assertIn(b"* 4 EXISTS", [u.line for u in resp.untagged])
        after = (await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()
        self.assertEqual(sorted(after), before + [uidnext])
        # Same UID still names the same message after the restart.
        utf8 = (await c.cmd("UID FETCH 2 BODY.PEEK[HEADER.FIELDS (Message-ID)]")).fetch_by_uid()[2]
        self.assertIn(b"<utf8-002@imap.test>", utf8.literal)

    async def test_lost_store_bumps_uidvalidity(self):
        c = await self.logged_in()
        v1 = int((await c.cmd("SELECT INBOX")).untagged_codes()["UIDVALIDITY"])
        await c.close()
        self._clients.remove(c)
        await self.gw.stop()
        for p in self.db_path.parent.glob(self.db_path.name + "*"):
            p.unlink()
        await asyncio.sleep(1.1)  # UIDVALIDITY is seconds-based
        await self.restart_gateway()
        c = await self.logged_in()
        v2 = int((await c.cmd("SELECT INBOX")).untagged_codes()["UIDVALIDITY"])
        self.assertGreater(v2, v1)

    async def test_noop_announces_new_mail(self):
        c = await self.logged_in()
        resp = await c.cmd("SELECT INBOX")
        self.assertIn(b"* 3 EXISTS", [u.line for u in resp.untagged])
        self.assertEqual((await c.cmd("NOOP")).untagged, [])
        self.fake.add_message()
        resp = await c.cmd("NOOP")
        self.assertEqual([u.line for u in resp.untagged], [b"* 4 EXISTS"])
        self.assertEqual(resp.status, "OK")
        new = (
            await c.cmd("UID FETCH 4 (FLAGS BODY.PEEK[HEADER.FIELDS (Subject)])")
        ).fetch_by_uid()[4]
        self.assertIn(b"Subject: A later delivery", new.literal)
        old = (await c.cmd("UID FETCH 1:3 UID")).fetch_by_uid()
        self.assertEqual(sorted(old), [1, 2, 3])

    async def test_listing_failure_keeps_view(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        # Fail the next listing (the fake cannot target page two specifically; the unit test
        # test_partial_failure_keeps_previous_snapshot_and_tombstones_nothing covers page two).
        self.fake.fail_next(
            method="GET",
            path_prefix="/inboxes/candidate%40imap.test/messages",
            status=503,
            times=10,
        )
        resp = await c.cmd("NOOP")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        # The old snapshot is still served and nothing was tombstoned.
        resp = await c.cmd("UID FETCH 1:* (FLAGS)")
        self.assertEqual(sorted(resp.fetch_by_uid()), [1, 2, 3])
        self.fake.reset()
        resp = await c.cmd("NOOP")
        self.assertEqual(resp.status, "OK")
        self.assertEqual(sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid()), [1, 2, 3])

    async def test_two_sessions_share_uids(self):
        a = await self.logged_in()
        b = await self.logged_in()
        ua = (await a.cmd("SELECT INBOX")).untagged_codes()
        ub = (await b.cmd("SELECT INBOX")).untagged_codes()
        self.assertEqual(ua["UIDVALIDITY"], ub["UIDVALIDITY"])
        fa = (await a.cmd("UID FETCH 1:* BODY.PEEK[HEADER.FIELDS (Message-ID)]")).fetch_by_uid()
        fb = (await b.cmd("UID FETCH 1:* BODY.PEEK[HEADER.FIELDS (Message-ID)]")).fetch_by_uid()
        self.assertEqual(
            {k: v.literal for k, v in fa.items()}, {k: v.literal for k, v in fb.items()}
        )


if __name__ == "__main__":
    unittest.main()
