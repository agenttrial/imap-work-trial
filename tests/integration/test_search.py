import unittest

from tests.support.gateway import GatewayTestCase


class SearchTests(GatewayTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.c = await self.logged_in()
        self.assertEqual((await self.c.cmd("SELECT INBOX")).status, "OK")

    async def uids(self, criteria: str) -> list[int]:
        resp = await self.c.cmd(f"UID SEARCH {criteria}")
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual(len(resp.untagged), 1, resp)
        line = resp.untagged[0].line
        self.assertTrue(line.startswith(b"* SEARCH"), line)
        return [int(x) for x in line.split()[2:]]

    async def test_flags_and_all(self):
        self.assertEqual(await self.uids("ALL"), [1, 2, 3])
        self.assertEqual(await self.uids("UNSEEN"), [1, 3])
        self.assertEqual(await self.uids("SEEN"), [2])
        self.assertEqual(await self.uids("FLAGGED"), [2])
        self.assertEqual(await self.uids("UNFLAGGED"), [1, 3])
        self.assertEqual(await self.uids("DELETED"), [])
        self.assertEqual(await self.uids("UNDRAFT"), [1, 2, 3])
        self.assertEqual(await self.uids("NEW"), [])
        self.assertEqual(await self.uids("KEYWORD starred"), [2])
        self.assertEqual(await self.uids("UNKEYWORD unread"), [2])

    async def test_header_fields_from_metadata(self):
        self.assertEqual(await self.uids("SUBJECT unicode"), [2])
        self.assertEqual(await self.uids('SUBJECT "small deterministic"'), [1])
        self.assertEqual(await self.uids("FROM ada"), [1])
        self.assertEqual(await self.uids("FROM example"), [1, 2, 3])
        self.assertEqual(await self.uids("TO candidate"), [1, 2, 3])
        self.assertEqual(await self.uids("SUBJECT nomatch"), [])

    async def test_dates(self):
        self.assertEqual(await self.uids("SINCE 17-Aug-2026"), [1, 2, 3])
        self.assertEqual(await self.uids("ON 17-Aug-2026"), [1, 2, 3])
        self.assertEqual(await self.uids("BEFORE 17-Aug-2026"), [])
        self.assertEqual(await self.uids("SINCE 18-Aug-2026"), [])
        self.assertEqual(await self.uids('SENTON "17-Aug-2026"'), [1, 2, 3])
        self.assertEqual(await self.uids("SENTBEFORE 17-Aug-2026"), [])

    async def test_sets_and_boolean_logic(self):
        self.assertEqual(await self.uids("UID 2:3"), [2, 3])
        self.assertEqual(await self.uids("UID 900:*"), [3])
        self.assertEqual(await self.uids("2:*"), [2, 3])
        self.assertEqual(await self.uids("NOT SEEN"), [1, 3])
        self.assertEqual(await self.uids("OR FLAGGED FROM ada"), [1, 2])
        self.assertEqual(await self.uids("(UNSEEN FROM fixtures)"), [3])
        self.assertEqual(await self.uids("UNSEEN NOT FROM fixtures"), [1])

    async def test_size_and_body_text(self):
        self.assertEqual(await self.uids("LARGER 400"), [3])
        self.assertEqual(await self.uids("SMALLER 320"), [1])
        self.assertEqual(await self.uids('TEXT "byte counts"'), [2])
        self.assertEqual(await self.uids("BODY attachment"), [3])
        self.assertEqual(await self.uids("HEADER Message-ID utf8"), [2])
        self.assertEqual(await self.uids("HEADER Content-Type multipart"), [3])

    async def test_search_by_sequence_number(self):
        resp = await self.c.cmd("SEARCH UNSEEN")
        self.assertEqual(resp.untagged[0].line, b"* SEARCH 1 3")
        resp = await self.c.cmd("SEARCH 3")
        self.assertEqual(resp.untagged[0].line, b"* SEARCH 3")

    async def test_charset_handling(self):
        self.assertEqual(await self.uids("CHARSET UTF-8 ALL"), [1, 2, 3])
        self.assertEqual(await self.uids("CHARSET us-ascii FROM ada"), [1])
        resp = await self.c.cmd("UID SEARCH CHARSET ISO-8859-1 ALL")
        self.assertEqual((resp.status, resp.code()), ("NO", "BADCHARSET (UTF-8)"))

    async def test_syntax_errors(self):
        for text in (
            "UID SEARCH",
            "UID SEARCH NOPE",
            "UID SEARCH OR ALL",
            "UID SEARCH SINCE yesterday",
            "UID SEARCH LARGER x",
        ):
            with self.subTest(text=text):
                self.assertEqual((await self.c.cmd(text)).status, "BAD")

    async def test_deleted_reflects_session_flag(self):
        await self.c.cmd("UID STORE 1 +FLAGS.SILENT (\\Deleted)")
        self.assertEqual(await self.uids("DELETED"), [1])
        self.assertEqual(await self.uids("UNDELETED"), [2, 3])

    async def test_empty_result_line(self):
        resp = await self.c.cmd("UID SEARCH SUBJECT zzz")
        self.assertEqual(resp.untagged[0].line, b"* SEARCH")

    async def test_search_in_drafts(self):
        await self.c.cmd("SELECT Drafts")
        self.assertEqual(await self.uids("DRAFT"), [1, 2])
        self.assertEqual(await self.uids("TO primary"), [2])
        self.assertEqual(await self.uids("CC copy"), [2])
        self.assertEqual(await self.uids("BCC audit"), [2])


if __name__ == "__main__":
    unittest.main()


class DecodedSearchTests(GatewayTestCase):
    """Review finding F17: TEXT and BODY match decoded content, not transfer encodings."""

    async def test_text_and_body_match_decoded_utf8(self):
        c = await self.logged_in()
        await c.cmd("SELECT INBOX")
        needle = "café".encode()
        resp = await c.cmd_literal("UID SEARCH CHARSET UTF-8 TEXT", needle)
        self.assertEqual(resp.untagged[0].line, b"* SEARCH 2")  # encoded-word subject decoded
        resp = await c.cmd_literal("UID SEARCH CHARSET UTF-8 BODY", "naïve".encode())
        self.assertEqual(resp.untagged[0].line, b"* SEARCH 2")  # 8bit body
        resp = await c.cmd_literal("UID SEARCH TEXT", "東京".encode())
        self.assertEqual(resp.untagged[0].line, b"* SEARCH 2")
