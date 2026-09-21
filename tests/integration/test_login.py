import asyncio
import logging
import time
import unittest

from tests.support.fake_api import API_KEY, INBOX_ID
from tests.support.gateway import GatewayTestCase


class LoginTests(GatewayTestCase):
    async def test_login_ok(self):
        c = await self.client()
        resp = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual(resp.code(), "CAPABILITY IMAP4rev1 UIDPLUS MOVE ID NAMESPACE")
        again = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
        self.assertEqual(again.status, "BAD")

    async def test_login_with_literals(self):
        c = await self.client()
        tag = c.next_tag()
        await c.send_raw(f"{tag} LOGIN {{{len(INBOX_ID)}}}\r\n".encode())
        self.assertTrue((await c.read_line()).startswith(b"+ "))
        await c.send_raw(INBOX_ID.encode() + f" {{{len(API_KEY)}}}\r\n".encode())
        self.assertTrue((await c.read_line()).startswith(b"+ "))
        await c.send_raw(API_KEY.encode() + b"\r\n")
        resp = await c.read_response(tag)
        self.assertEqual(resp.status, "OK", resp)

    async def test_login_unquoted_atoms(self):
        c = await self.client()
        resp = await c.cmd(f"LOGIN {INBOX_ID} {API_KEY}")
        self.assertEqual(resp.status, "OK", resp)

    async def test_wrong_password(self):
        c = await self.client()
        with self.assertLogs("imapgw", level="DEBUG") as logs:
            resp = await c.cmd(f'LOGIN "{INBOX_ID}" "wrong-key-value"')
        self.assertEqual(resp.status, "NO")
        self.assertEqual(resp.code(), "AUTHENTICATIONFAILED")
        self.assertNotIn("wrong-key-value", resp.text)
        joined = "\n".join(logs.output)
        self.assertNotIn("wrong-key-value", joined)
        self.assertNotIn(API_KEY, joined)
        # Still usable, still unauthenticated.
        self.assertEqual((await c.cmd("SELECT INBOX")).status, "BAD")
        self.assertEqual((await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')).status, "OK")

    async def test_wrong_inbox_for_scoped_key(self):
        c = await self.client()
        resp = await c.cmd(f'LOGIN "someone-else@imap.test" "{API_KEY}"')
        self.assertEqual(resp.status, "NO")
        self.assertEqual(resp.code(), "AUTHENTICATIONFAILED")

    async def test_missing_arguments(self):
        c = await self.client()
        self.assertEqual((await c.cmd("LOGIN onlyone")).status, "BAD")
        self.assertEqual((await c.cmd('LOGIN "" ""')).status, "NO")

    async def test_key_never_logged_on_success(self):
        c = await self.client()
        with self.assertLogs("imapgw", level="DEBUG") as logs:
            resp = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
            await c.cmd("NOOP")
        self.assertEqual(resp.status, "OK")
        self.assertNotIn(API_KEY, "\n".join(logs.output))

    async def test_upstream_503_gives_no(self):
        self.fake.fail_next(method="GET", path_prefix="/auth/me", status=503, times=5)
        c = await self.client()
        start = time.monotonic()
        resp = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
        self.assertEqual(resp.status, "NO", resp)
        self.assertEqual(resp.code(), "UNAVAILABLE")
        self.assertLess(time.monotonic() - start, 4.0)
        self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_upstream_429_once_is_retried(self):
        self.fake.fail_next(method="GET", path_prefix="/auth/me", status=429, times=1)
        c = await self.client()
        resp = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
        self.assertEqual(resp.status, "OK", resp)
        auth_calls = [q for q in self.fake.requests() if q["path"].endswith("/auth/me")]
        self.assertEqual(len(auth_calls), 2)

    async def test_slow_upstream_does_not_block_other_connections(self):
        self.fake.fail_next(
            method="GET", path_prefix="/auth/me", status=503, delay_ms=4000, times=5
        )
        slow = await self.client(timeout=10.0)
        started = time.monotonic()
        slow_task = asyncio.create_task(slow.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"'))
        await asyncio.sleep(0.2)
        other = await self.client()
        t0 = time.monotonic()
        resp = await other.cmd("CAPABILITY")
        self.assertEqual(resp.status, "OK")
        self.assertLess(time.monotonic() - t0, 1.0)
        slow_resp = await slow_task
        self.assertEqual(slow_resp.status, "NO", slow_resp)
        self.assertLess(time.monotonic() - started, 8.0)


class AllowlistTests(GatewayTestCase):
    settings_overrides = {"allowed_inbox_id": INBOX_ID}

    async def test_allowlisted_inbox_only(self):
        c = await self.client()
        self.assertEqual((await c.cmd(f'LOGIN "other@imap.test" "{API_KEY}"')).status, "NO")
        self.assertEqual((await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')).status, "OK")


if __name__ == "__main__":
    unittest.main()


class CredentialHygieneTests(GatewayTestCase):
    """Review finding F3: malformed credentials must be refused before any header is built and
    must never appear in logs, including exception text."""

    async def test_crlf_password_is_refused_and_never_logged(self):
        c = await self.client()
        captured: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(logging.Formatter("%(message)s").format(record))

        handler = Capture()
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        try:
            tag = c.next_tag()
            secret = b"synthetic_secret\r\nX: 1"
            await c.send_raw(f"{tag} LOGIN {{{len(INBOX_ID)}}}\r\n".encode())
            await c.read_line()
            await c.send_raw(INBOX_ID.encode() + f" {{{len(secret)}}}\r\n".encode())
            await c.read_line()
            await c.send_raw(secret + b"\r\n")
            resp = await c.read_response(tag)
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual((resp.status, resp.code()), ("NO", "AUTHENTICATIONFAILED"))
        self.assertNotIn("synthetic_secret", resp.text)
        self.assertNotIn("synthetic_secret", "\n".join(captured))
        auth_calls = [q for q in self.fake.requests() if q["path"].endswith("/auth/me")]
        self.assertEqual(auth_calls, [])  # refused before any upstream call
        self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_space_and_non_ascii_credentials_refused(self):
        c = await self.client()
        self.assertEqual((await c.cmd(f'LOGIN "{INBOX_ID}" "has space"')).status, "NO")
        c2 = await self.client()
        tag = c2.next_tag()
        pw = "café".encode()
        await c2.send_raw(f"{tag} LOGIN {INBOX_ID} {{{len(pw)}}}\r\n".encode())
        await c2.read_line()
        await c2.send_raw(pw + b"\r\n")
        self.assertEqual((await c2.read_response(tag)).status, "NO")
