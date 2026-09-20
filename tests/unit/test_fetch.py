import unittest

from imapgw.fetch import (
    FetchSyntaxError,
    UnsupportedFetch,
    filter_header,
    parse_fetch_attributes,
    parse_sequence_set,
    section_bytes,
    select_numbers,
    split_message,
)
from imapgw.parser import Atom, ListTok

RAW = (
    b"From: Ada <ada@example.com>\r\n"
    b"To: candidate@imap.test\r\n"
    b"Subject: folded\r\n subject line\r\n"
    b"Date: Mon, 17 Aug 2026 16:00:00 +0000\r\n"
    b"\r\n"
    b"body line 1\r\nbody line 2\r\n"
)


class SequenceSetTests(unittest.TestCase):
    def test_parse_and_select(self):
        s = parse_sequence_set("1,3:5,9:7")
        self.assertEqual(select_numbers(s, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), [1, 3, 4, 5, 7, 8, 9])

    def test_star_means_largest(self):
        s = parse_sequence_set("559:*")
        self.assertEqual(select_numbers(s, [10, 495, 600]), [600])
        s = parse_sequence_set("*")
        self.assertEqual(select_numbers(s, [10, 495, 600]), [600])
        s = parse_sequence_set("1:*")
        self.assertEqual(select_numbers(s, [10, 495, 600]), [10, 495, 600])
        self.assertEqual(select_numbers(s, []), [])

    def test_nonexistent_numbers_ignored(self):
        s = parse_sequence_set("999")
        self.assertEqual(select_numbers(s, [1, 2, 3]), [])

    def test_invalid(self):
        for text in ("", "0", "a", "1:", "1,,2", "1:2:3"):
            with self.subTest(text=text), self.assertRaises(FetchSyntaxError):
                parse_sequence_set(text)


class AttributeParsingTests(unittest.TestCase):
    def test_simple_list(self):
        attrs = parse_fetch_attributes(ListTok((Atom("UID"), Atom("flags"), Atom("RFC822.SIZE"))))
        self.assertEqual([a.label for a in attrs], ["UID", "FLAGS", "RFC822.SIZE"])

    def test_single_atom_and_macro(self):
        self.assertEqual([a.label for a in parse_fetch_attributes(Atom("FLAGS"))], ["FLAGS"])
        self.assertEqual(
            [a.label for a in parse_fetch_attributes(Atom("FAST"))],
            ["FLAGS", "INTERNALDATE", "RFC822.SIZE"],
        )

    def test_body_sections(self):
        a = parse_fetch_attributes(Atom("BODY.PEEK[]"))[0]
        self.assertEqual((a.kind, a.peek, a.section, a.label), ("BODY", True, "", "BODY[]"))
        a = parse_fetch_attributes(Atom("BODY[HEADER]"))[0]
        self.assertEqual((a.peek, a.section, a.label), (False, "HEADER", "BODY[HEADER]"))
        a = parse_fetch_attributes(Atom('BODY.PEEK[HEADER.FIELDS (From to "Subject")]<0.100>'))[0]
        self.assertEqual(a.section, "HEADER.FIELDS")
        self.assertEqual(a.fields, ("FROM", "TO", "SUBJECT"))
        self.assertEqual(a.partial, (0, 100))
        self.assertEqual(a.label, "BODY[HEADER.FIELDS (FROM TO SUBJECT)]<0>")
        a = parse_fetch_attributes(Atom("BODY[HEADER.FIELDS.NOT (Received)]"))[0]
        self.assertEqual((a.section, a.fields), ("HEADER.FIELDS.NOT", ("RECEIVED",)))
        a = parse_fetch_attributes(Atom("RFC822"))[0]
        self.assertEqual((a.kind, a.section, a.peek, a.label), ("BODY", "", False, "RFC822"))
        a = parse_fetch_attributes(Atom("RFC822.HEADER"))[0]
        self.assertEqual((a.section, a.label), ("HEADER", "RFC822.HEADER"))

    def test_unsupported_items(self):
        for name in ("ENVELOPE", "BODYSTRUCTURE", "BODY", "ALL", "FULL", "BODY[1]", "BODY[1.MIME]"):
            with self.subTest(name=name), self.assertRaises(UnsupportedFetch):
                parse_fetch_attributes(Atom(name))

    def test_syntax_errors(self):
        for name in ("BODY[", "BODY[WHATEVER]", "BODY[HEADER.FIELDS ()]", "NOPE"):
            with self.subTest(name=name), self.assertRaises(FetchSyntaxError):
                parse_fetch_attributes(Atom(name))

    def test_duplicates_collapse(self):
        attrs = parse_fetch_attributes(ListTok((Atom("UID"), Atom("uid"))))
        self.assertEqual(len(attrs), 1)


class SectionSlicingTests(unittest.TestCase):
    def test_split(self):
        header, body = split_message(RAW)
        self.assertTrue(header.endswith(b"+0000\r\n\r\n"))
        self.assertEqual(body, b"body line 1\r\nbody line 2\r\n")
        self.assertEqual(header + body, RAW)
        self.assertEqual(split_message(b"no blank line"), (b"no blank line", b""))

    def test_header_fields_keeps_folding_and_blank_line(self):
        header, _ = split_message(RAW)
        out = filter_header(header, ("SUBJECT", "FROM"), invert=False)
        self.assertEqual(
            out, b"From: Ada <ada@example.com>\r\nSubject: folded\r\n subject line\r\n\r\n"
        )
        out = filter_header(header, ("SUBJECT",), invert=True)
        self.assertNotIn(b"Subject", out)
        self.assertIn(b"Date:", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_section_bytes_and_partial(self):
        a = parse_fetch_attributes(Atom("BODY.PEEK[TEXT]"))[0]
        self.assertEqual(section_bytes(RAW, a), b"body line 1\r\nbody line 2\r\n")
        a = parse_fetch_attributes(Atom("BODY.PEEK[]<5.4>"))[0]
        self.assertEqual(section_bytes(RAW, a), RAW[5:9])
        a = parse_fetch_attributes(Atom("BODY.PEEK[]<100000.4>"))[0]
        self.assertEqual(section_bytes(RAW, a), b"")
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS (Missing)]"))[0]
        self.assertEqual(section_bytes(RAW, a), b"\r\n")


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTests(unittest.TestCase):
    def test_lf_only_header_fields(self):  # F20
        raw = b"From: a@x\nSubject: secret\n\nbody"
        header, body = split_message(raw)
        self.assertEqual(body, b"body")
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS (Subject)]"))[0]
        self.assertEqual(section_bytes(raw, a), b"Subject: secret\n\r\n")
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS (From)]"))[0]
        self.assertEqual(section_bytes(raw, a), b"From: a@x\n\r\n")
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS.NOT (From)]"))[0]
        self.assertEqual(section_bytes(raw, a), b"Subject: secret\n\r\n")

    def test_header_only_message_without_blank_line(self):  # F20
        raw = b"From: a@x\r\nSubject: s"
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS (Subject)]"))[0]
        self.assertEqual(section_bytes(raw, a), b"Subject: s\r\n\r\n")

    def test_duplicate_and_folded_fields_preserved(self):
        raw = b"Received: one\r\nReceived: two\r\n folded\r\nSubject: s\r\n\r\nbody"
        a = parse_fetch_attributes(Atom("BODY.PEEK[HEADER.FIELDS (Received)]"))[0]
        self.assertEqual(
            section_bytes(raw, a), b"Received: one\r\nReceived: two\r\n folded\r\n\r\n"
        )

    def test_peek_and_non_peek_merge_keeps_side_effect(self):  # F21
        attrs = parse_fetch_attributes(ListTok((Atom("BODY.PEEK[]"), Atom("BODY[]"))))
        self.assertEqual(len(attrs), 1)
        self.assertFalse(attrs[0].peek)
        attrs = parse_fetch_attributes(ListTok((Atom("BODY[]"), Atom("BODY.PEEK[]"))))
        self.assertFalse(attrs[0].peek)
        attrs = parse_fetch_attributes(ListTok((Atom("BODY.PEEK[]"), Atom("BODY.PEEK[]"))))
        self.assertTrue(attrs[0].peek)
