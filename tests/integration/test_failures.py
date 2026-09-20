"""Failure sweep: every upstream path fails in every way; every command must answer NO without
hanging, and the connection must stay usable afterwards."""

import time
import unittest

from tests.support.fake_api import API_KEY, INBOX_ID
from tests.support.gateway import GatewayTestCase

INBOX_PATH = "/inboxes/candidate%40imap.test"

# (label, setup commands run before the injection, command under test, method, path prefix)
SCENARIOS = [
    ("login", [], f'LOGIN "{INBOX_ID}" "{API_KEY}"', "GET", "/auth/me"),
    ("select-inbox", ["LOGIN"], "SELECT INBOX", "GET", f"{INBOX_PATH}/messages"),
    ("select-drafts", ["LOGIN"], "SELECT Drafts", "GET", f"{INBOX_PATH}/drafts"),
    ("noop-resync", ["LOGIN", "SELECT INBOX"], "NOOP", "GET", f"{INBOX_PATH}/messages"),
    (
        "fetch-raw-meta",
        ["LOGIN", "SELECT INBOX"],
        "UID FETCH 1 BODY.PEEK[]",
        "GET",
        f"{INBOX_PATH}/messages/msg_received_ascii/raw",
    ),
    ("fetch-download", ["LOGIN", "SELECT INBOX"], "UID FETCH 1 BODY.PEEK[]", "GET", "/raw/"),
    ("draft-get", ["LOGIN"], "SELECT Drafts", "GET", f"{INBOX_PATH}/drafts/draft_existing"),
]
MODES = [
    ("503x10", {"status": 503, "times": 10}),
    ("429x10", {"status": 429, "times": 10}),
    ("slow-3s", {"status": 503, "delay_ms": 3000, "times": 10}),
]


class FailureSweepTests(GatewayTestCase):
    async def _setup(self, c, steps):
        for step in steps:
            if step == "LOGIN":
                self.assertEqual((await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')).status, "OK")
            else:
                self.assertEqual((await c.cmd(step)).status, "OK")

    async def test_every_path_every_mode(self):
        for label, setup, command, method, prefix in SCENARIOS:
            for mode_label, injection in MODES:
                with self.subTest(scenario=label, mode=mode_label):
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                    c = await self.client(timeout=10.0)
                    await self._setup(c, setup)
                    self.fake.fail_next(method=method, path_prefix=prefix, **injection)
                    started = time.monotonic()
                    resp = await c.cmd(command)
                    elapsed = time.monotonic() - started
                    self.assertEqual(resp.status, "NO", resp)
                    self.assertEqual(resp.code(), "UNAVAILABLE", resp)
                    self.assertLess(elapsed, 7.0)
                    self.fake.reset()
                    self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_single_429_is_absorbed_by_retry(self):
        c = await self.logged_in()
        self.fake.fail_next(method="GET", path_prefix=f"{INBOX_PATH}/messages", status=429, times=1)
        resp = await c.cmd("SELECT INBOX")
        self.assertEqual(resp.status, "OK", resp)
        self.assertIn(b"* 3 EXISTS", [u.line for u in resp.untagged])
        list_calls = [q for q in self.fake.requests() if q["path"].endswith("/messages")]
        self.assertEqual(len(list_calls), 3)  # two pages plus one retried request

    async def test_append_failure_leaves_no_draft(self):
        c = await self.logged_in()
        for mode_label, injection in MODES:
            with self.subTest(mode=mode_label):
                self.fake.reset()
                self.fake.fail_next(method="POST", path_prefix=f"{INBOX_PATH}/drafts", **injection)
                resp = await c.cmd_literal("APPEND Drafts", b"Subject: x\r\n\r\nbody\r\n")
                self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"), resp)
                # Assert on the fake's state BEFORE resetting it: a failed POST must create nothing.
                self.assertEqual(len(self.fake.state()["drafts"]), 2)
                self.fake.reset()

    async def test_revoked_credentials_mid_session(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        self.fake.fail_next(method="GET", path_prefix=f"{INBOX_PATH}/messages", status=401, times=1)
        tag = c.next_tag()
        await c.send_raw(f"{tag} NOOP\r\n".encode())
        data = await c.read_eof()
        self.assertIn(b"* BYE", data)
        self.assertIn(b"credentials", data)

    async def test_bad_json_from_upstream(self):
        # A 200 with a non-JSON body cannot be injected by the fake; covered by unit tests.
        self.skipTest("covered in tests.unit.test_apiclient")


class ConcurrencyUnderFailureTests(GatewayTestCase):
    async def test_slow_upstream_for_one_session_does_not_stall_another(self):
        slow = await self.logged_in(timeout=10.0)
        fast = await self.logged_in()
        self.assertEqual((await fast.cmd("SELECT INBOX")).status, "OK")
        self.fake.fail_next(
            method="GET",
            path_prefix=f"{INBOX_PATH}/messages/msg_received_utf8/raw",
            status=503,
            delay_ms=3000,
            times=10,
        )
        await slow.cmd("SELECT INBOX")
        import asyncio

        slow_task = asyncio.create_task(slow.cmd("UID FETCH 2 BODY.PEEK[]"))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        resp = await fast.cmd("UID FETCH 1 (FLAGS RFC822.SIZE)")
        self.assertEqual(resp.status, "OK")
        self.assertLess(time.monotonic() - t0, 1.0)
        slow_resp = await slow_task
        self.assertEqual(slow_resp.status, "NO")


if __name__ == "__main__":
    unittest.main()
