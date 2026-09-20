import unittest
from datetime import UTC, datetime, timedelta, timezone

from imapgw import responses as r

UTF8_BODY = "Character counts are not byte counts: naïve, 東京, 🚀.\r\n".encode()


class ResponseEncodingTests(unittest.TestCase):
    def test_tagged_with_and_without_code(self):
        self.assertEqual(r.tagged("A1", "OK", "done"), b"A1 OK done\r\n")
        self.assertEqual(
            r.tagged("A1", "NO", "bad", code="AUTHENTICATIONFAILED"),
            b"A1 NO [AUTHENTICATIONFAILED] bad\r\n",
        )

    def test_literal_uses_byte_length_not_char_length(self):
        self.assertGreater(len(UTF8_BODY), len(UTF8_BODY.decode()))
        out = r.literal(UTF8_BODY)
        header = b"{" + str(len(UTF8_BODY)).encode() + b"}\r\n"
        self.assertTrue(out.startswith(header))
        self.assertEqual(out[len(header) :], UTF8_BODY)

    def test_literal_rejects_str(self):
        with self.assertRaises(TypeError):
            r.literal("not bytes")  # type: ignore[arg-type]

    def test_quoted_escapes(self):
        self.assertEqual(r.quoted('a"b\\c'), b'"a\\"b\\\\c"')
        self.assertEqual(r.quoted(""), b'""')

    def test_quoted_falls_back_to_literal_for_8bit_or_crlf(self):
        self.assertTrue(r.quoted("café").startswith(b"{5}\r\n"))
        self.assertTrue(r.quoted("a\r\nb").startswith(b"{4}\r\n"))

    def test_nstring(self):
        self.assertEqual(r.nstring(None), b"NIL")
        self.assertEqual(r.nstring("x"), b'"x"')

    def test_internaldate_format(self):
        dt = datetime(2026, 8, 17, 16, 0, 0, tzinfo=UTC)
        self.assertEqual(r.internaldate(dt), b'"17-Aug-2026 16:00:00 +0000"')
        dt2 = datetime(2026, 1, 5, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
        self.assertEqual(r.internaldate(dt2), b'"05-Jan-2026 03:04:05 -0530"')

    def test_flag_list_order_and_empty(self):
        self.assertEqual(r.flag_list([]), b"()")
        self.assertEqual(r.flag_list(["\\Draft", "\\Seen"]), b"(\\Seen \\Draft)")

    def test_list_response(self):
        self.assertEqual(
            r.list_response(["\\HasNoChildren"], None, "INBOX"),
            b"* LIST (\\HasNoChildren) NIL INBOX\r\n",
        )
        self.assertEqual(
            r.list_response(["\\Noselect"], None, ""),
            b'* LIST (\\Noselect) NIL ""\r\n',
        )

    def test_fetch_response_with_literal_item(self):
        item = b"BODY[] " + r.literal(b"abc")
        out = r.fetch_response(3, [b"UID 7", item])
        self.assertEqual(out, b"* 3 FETCH (UID 7 BODY[] {3}\r\nabc)\r\n")

    def test_search_response(self):
        self.assertEqual(r.search_response([1, 2, 3]), b"* SEARCH 1 2 3\r\n")
        self.assertEqual(r.search_response([]), b"* SEARCH\r\n")

    def test_mailbox_size_lines(self):
        self.assertEqual(r.exists(4), b"* 4 EXISTS\r\n")
        self.assertEqual(r.recent(0), b"* 0 RECENT\r\n")
        self.assertEqual(r.expunge(2), b"* 2 EXPUNGE\r\n")

    def test_capability_and_bye(self):
        self.assertEqual(r.capability_line(["IMAP4rev1"]), b"* CAPABILITY IMAP4rev1\r\n")
        self.assertEqual(r.bye("x"), b"* BYE x\r\n")
        self.assertEqual(r.continuation("Ready for 5 octets"), b"+ Ready for 5 octets\r\n")


if __name__ == "__main__":
    unittest.main()
