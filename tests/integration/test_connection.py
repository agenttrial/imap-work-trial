import asyncio
import unittest

from tests.support.gateway import GatewayTestCase
from tests.support.imap_client import ImapTestClient


class ConnectionTests(GatewayTestCase):
    async def test_greeting_and_capability(self):
        c = await self.client()
        self.assertEqual(
            c.greeting, b"* OK [CAPABILITY IMAP4rev1 UIDPLUS MOVE ID NAMESPACE] imapgw ready"
        )
        resp = await c.cmd("CAPABILITY")
        self.assertEqual(resp.status, "OK")
        self.assertEqual(
            [u.line for u in resp.untagged], [b"* CAPABILITY IMAP4rev1 UIDPLUS MOVE ID NAMESPACE"]
        )
        self.assertEqual(resp.text, "CAPABILITY completed")

    async def test_noop_before_login(self):
        c = await self.client()
        resp = await c.cmd("NOOP")
        self.assertEqual((resp.status, resp.text), ("OK", "NOOP completed"))

    async def test_unknown_command_is_bad_and_connection_survives(self):
        c = await self.client()
        resp = await c.cmd("FOO bar")
        self.assertEqual(resp.status, "BAD")
        self.assertIn("unknown command", resp.text)
        resp = await c.cmd("NOOP")
        self.assertEqual(resp.status, "OK")

    async def test_wrong_state_is_bad(self):
        c = await self.client()
        resp = await c.cmd("SELECT INBOX")
        self.assertEqual(resp.status, "BAD")
        resp = await c.cmd("UID FETCH 1 (FLAGS)")
        self.assertEqual(resp.status, "BAD")

    async def test_malformed_lines(self):
        c = await self.client()
        await c.send_raw(b"\r\n")
        line = await c.read_line()
        self.assertTrue(line.startswith(b"* BAD"), line)
        resp = await c.cmd('LOGIN "unterminated')
        self.assertEqual(resp.status, "BAD")
        resp = await c.cmd("NOOP")
        self.assertEqual(resp.status, "OK")

    async def test_logout(self):
        c = await self.client()
        resp = await c.cmd("LOGOUT")
        self.assertEqual(resp.status, "OK")
        self.assertEqual([u.line for u in resp.untagged], [b"* BYE logging out"])
        self.assertEqual(await c.read_eof(), b"")

    async def test_line_too_long_closes_connection(self):
        c = await self.client()
        await c.send_raw(b"A1 " + b"x" * (70 * 1024) + b"\r\n")
        data = await c.read_eof()
        self.assertIn(b"BAD", data)
        self.assertIn(b"* BYE", data)

    async def test_oversize_literal_rejected_without_hang(self):
        c = await self.client()
        await c.send_raw(b"A9 APPEND Drafts {5000000}\r\n")
        line = await c.read_line()
        self.assertTrue(line.startswith(b"A9 BAD"), line)
        resp = await c.cmd("NOOP")
        self.assertEqual(resp.status, "OK")


class LimitsTests(GatewayTestCase):
    settings_overrides = {"idle_timeout": 1.0, "max_connections": 2}

    async def test_idle_timeout(self):
        c = await self.client(timeout=5.0)
        data = await asyncio.wait_for(c.read_eof(), 4.0)
        self.assertIn(b"* BYE idle timeout", data)

    async def test_max_connections(self):
        c1 = await self.client()
        c2 = await self.client()
        reader, writer = await asyncio.open_connection(self.gw.host, self.gw.port)
        try:
            data = await asyncio.wait_for(reader.read(), 3.0)
        finally:
            writer.close()
        self.assertIn(b"* BYE too many connections", data)
        self.assertEqual((await c1.cmd("NOOP")).status, "OK")
        self.assertEqual((await c2.cmd("NOOP")).status, "OK")

    async def test_client_disconnect_frees_slot(self):
        c1 = await self.client()
        await c1.close()
        self._clients.remove(c1)
        await asyncio.sleep(0.1)
        c2 = await ImapTestClient.connect(self.gw.host, self.gw.port)
        self._clients.append(c2)
        c3 = await ImapTestClient.connect(self.gw.host, self.gw.port)
        self._clients.append(c3)
        self.assertEqual((await c3.cmd("NOOP")).status, "OK")


if __name__ == "__main__":
    unittest.main()


class HostileInputTests(GatewayTestCase):
    """Review findings F14 and F18: hostile bytes must neither crash nor inject responses."""

    async def test_huge_literal_numeral_gets_bad_not_disconnect(self):
        c = await self.client()
        await c.send_raw(b"A1 APPEND Drafts {" + b"9" * 4301 + b"}\r\n")
        line = await c.read_line()
        self.assertTrue(line.startswith(b"A1 BAD"), line)
        self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_hostile_tag_is_not_echoed(self):
        c = await self.client()
        await c.send_raw(b"A1\r* BYE injected NOOP\r\n")
        line = await c.read_line()
        self.assertNotIn(b"\r", line)
        self.assertTrue(line.startswith(b"* BAD") or line.startswith(b"A1"), line)
        self.assertNotIn(b"injected", line)
        self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_error_text_never_echoes_arguments(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd("UID FETCH synthetic_secret FLAGS")
        self.assertEqual(resp.status, "BAD")
        self.assertNotIn("synthetic_secret", resp.text)
        resp = await c.cmd("UID FETCH 1 (BODY[secret_section])")
        self.assertEqual(resp.status, "BAD")
        self.assertNotIn("secret_section", resp.text)

    async def test_charset_injection_yields_one_line(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        resp = await c.cmd_literal(
            "UID SEARCH CHARSET", b"X\r\n* BYE injected", expect_continuation=True
        )
        # The literal completes the command; whatever follows must be a single tagged NO line.
        self.assertEqual(resp.status, "NO", resp)
        self.assertEqual(resp.code(), "BADCHARSET (UTF-8)")
        self.assertEqual(resp.untagged, [])
        self.assertNotIn("injected", resp.text)
        self.assertEqual((await c.cmd("NOOP")).status, "OK")


class AssemblyDeadlineTests(GatewayTestCase):
    settings_overrides = {"assembly_timeout": 1.0, "idle_timeout": 30.0}

    async def test_unfinished_literal_command_is_cut_off(self):  # F7
        c = await self.client(timeout=5.0)
        await c.send_raw(b"A1 APPEND Drafts {10}\r\n")
        self.assertTrue((await c.read_line()).startswith(b"+"))
        started = asyncio.get_event_loop().time()
        data = await asyncio.wait_for(c.read_eof(), 4.0)
        self.assertIn(b"* BYE command not completed in time", data)
        self.assertLess(asyncio.get_event_loop().time() - started, 3.0)


class ListPatternTests(GatewayTestCase):
    async def test_pathological_wildcards_answer_quickly(self):  # F8
        c = await self.logged_in()
        started = asyncio.get_event_loop().time()
        resp = await c.cmd('LIST "" "' + "*" * 400 + 'Z"')
        self.assertEqual((resp.status, resp.untagged), ("OK", []))
        resp = await c.cmd('LIST "" "' + "*%" * 200 + '"')
        self.assertEqual(len(resp.untagged), 5)
        self.assertLess(asyncio.get_event_loop().time() - started, 1.0)
        resp = await c.cmd('LIST "" "' + "a" * 600 + '"')
        self.assertEqual(resp.status, "BAD")


class PartialLineDeadlineTests(GatewayTestCase):
    settings_overrides = {"assembly_timeout": 1.0, "idle_timeout": 30.0}

    async def test_partial_command_line_hits_the_assembly_deadline(self):  # re-review finding 4
        c = await self.client(timeout=5.0)
        await c.send_raw(b"A1 NO")  # no line terminator, no literal
        started = asyncio.get_event_loop().time()
        data = await asyncio.wait_for(c.read_eof(), 4.0)
        self.assertIn(b"* BYE command not completed in time", data)
        self.assertLess(asyncio.get_event_loop().time() - started, 3.0)

    async def test_deadline_resets_between_complete_commands(self):
        c = await self.client(timeout=5.0)
        for _ in range(3):
            await asyncio.sleep(0.5)
            self.assertEqual((await c.cmd("NOOP")).status, "OK")
