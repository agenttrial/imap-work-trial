import unittest

from imapgw.parser import (
    Atom,
    Command,
    CommandParser,
    ContinuationRequest,
    Limits,
    ListTok,
    LiteralStr,
    ParseError,
    QuotedStr,
    astring_text,
)

UTF8_FIXTURE = (
    b"From: =?UTF-8?Q?Ren=C3=A9e?= <renee@example.net>\r\n"
    b"To: candidate@imap.test\r\n"
    b"Subject: =?UTF-8?B?VW5pY29kZSBjaGVjazogY2Fmw6ksIOadseS6rCwg8J+agA==?=\r\n"
    b"Date: Mon, 17 Aug 2026 17:00:00 +0000\r\n"
    b"Message-ID: <utf8-002@imap.test>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
) + "Character counts are not byte counts: naïve, 東京, 🚀.\r\n".encode()


def drain(parser: CommandParser) -> list:
    return list(parser.events())


def parse_one(data: bytes, limits: Limits | None = None):
    p = CommandParser(limits)
    p.feed(data)
    events = drain(p)
    assert len(events) == 1, events
    return events[0]


class TokenizerTests(unittest.TestCase):
    def test_simple_command(self):
        cmd = parse_one(b"A1 CAPABILITY\r\n")
        self.assertEqual(cmd, Command("A1", "CAPABILITY", ()))

    def test_lowercase_command_is_uppercased(self):
        cmd = parse_one(b"a1 noop\r\n")
        self.assertEqual(cmd.name, "NOOP")
        self.assertEqual(cmd.tag, "a1")

    def test_bare_lf_accepted(self):
        cmd = parse_one(b"A1 NOOP\n")
        self.assertEqual(cmd.name, "NOOP")

    def test_quoted_strings_with_spaces_and_escapes(self):
        cmd = parse_one(b'A2 LOGIN "a b" "x\\"y\\\\z"\r\n')
        self.assertEqual(cmd.args, (QuotedStr(b"a b"), QuotedStr(b'x"y\\z')))

    def test_atoms_with_wildcards_and_ranges(self):
        cmd = parse_one(b'A3 LIST "" *\r\n')
        self.assertEqual(cmd.args, (QuotedStr(b""), Atom("*")))
        cmd = parse_one(b"A4 UID FETCH 1:* (FLAGS RFC822.SIZE)\r\n")
        self.assertEqual(
            cmd.args,
            (Atom("FETCH"), Atom("1:*"), ListTok((Atom("FLAGS"), Atom("RFC822.SIZE")))),
        )

    def test_bracket_section_is_one_atom(self):
        cmd = parse_one(b"A5 UID FETCH 2 (BODY.PEEK[HEADER.FIELDS (From To)]<0.10> UID)\r\n")
        self.assertEqual(
            cmd.args[2],
            ListTok((Atom("BODY.PEEK[HEADER.FIELDS (From To)]<0.10>"), Atom("UID"))),
        )

    def test_nested_lists(self):
        cmd = parse_one(b"A6 X (a (b c) d)\r\n")
        self.assertEqual(
            cmd.args,
            (ListTok((Atom("a"), ListTok((Atom("b"), Atom("c"))), Atom("d"))),),
        )

    def test_multiple_spaces_tolerated(self):
        cmd = parse_one(b"A7  NOOP  \r\n")
        self.assertEqual(cmd.name, "NOOP")

    def test_pipelined_commands_in_one_chunk(self):
        p = CommandParser()
        p.feed(b"A1 NOOP\r\nA2 NOOP\r\n")
        events = drain(p)
        self.assertEqual([e.tag for e in events], ["A1", "A2"])


class LiteralTests(unittest.TestCase):
    def test_literal_login_with_continuations(self):
        p = CommandParser()
        p.feed(b"A2 LOGIN {19}\r\n")
        self.assertEqual(drain(p), [ContinuationRequest(19)])
        p.feed(b"candidate@")
        self.assertEqual(drain(p), [])
        p.feed(b"imap.test {18}\r\n")
        self.assertEqual(drain(p), [ContinuationRequest(18)])
        p.feed(b"test_agentmail_key\r\n")
        events = drain(p)
        self.assertEqual(
            events,
            [
                Command(
                    "A2",
                    "LOGIN",
                    (LiteralStr(b"candidate@imap.test"), LiteralStr(b"test_agentmail_key")),
                )
            ],
        )
        self.assertEqual(astring_text(events[0].args[0]), "candidate@imap.test")

    def test_append_literal_fed_one_byte_at_a_time(self):
        p = CommandParser()
        wire = (
            b"A7 APPEND Drafts (\\Draft) {"
            + str(len(UTF8_FIXTURE)).encode()
            + b"}\r\n"
            + UTF8_FIXTURE
            + b"\r\n"
        )
        events = []
        for i in range(len(wire)):
            p.feed(wire[i : i + 1])
            events.extend(drain(p))
        self.assertEqual(len(events), 2, events)
        self.assertEqual(events[0], ContinuationRequest(len(UTF8_FIXTURE)))
        cmd = events[1]
        self.assertEqual(cmd.name, "APPEND")
        self.assertEqual(cmd.args[0], Atom("Drafts"))
        self.assertEqual(cmd.args[1], ListTok((Atom("\\Draft"),)))
        self.assertEqual(cmd.args[2], LiteralStr(UTF8_FIXTURE))
        self.assertEqual(len(cmd.args[2].value), 375)

    def test_literal_bytes_may_contain_crlf_and_braces(self):
        p = CommandParser()
        body = b"line1\r\n{5}\r\nline3"
        p.feed(b"A1 APPEND Drafts {" + str(len(body)).encode() + b"}\r\n" + body + b"\r\n")
        events = drain(p)
        self.assertEqual(events[1].args[1], LiteralStr(body))

    def test_zero_length_literal_still_requires_continuation(self):
        p = CommandParser()
        p.feed(b"A1 LOGIN {0}\r\n")
        self.assertEqual(drain(p), [ContinuationRequest(0)])
        p.feed(b" x\r\n")
        self.assertEqual(drain(p), [Command("A1", "LOGIN", (LiteralStr(b""), Atom("x")))])

    def test_oversize_literal_rejected_before_any_bytes(self):
        p = CommandParser(Limits(max_literal=100))
        p.feed(b"A3 APPEND Drafts {2000000}\r\n")
        events = drain(p)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ParseError)
        self.assertEqual(events[0].tag, "A3")
        self.assertIn("literal too large", events[0].message)
        p.feed(b"A4 NOOP\r\n")
        self.assertEqual(drain(p), [Command("A4", "NOOP", ())])

    def test_non_synchronising_literal_is_rejected(self):
        err = parse_one(b"A1 LOGIN {5+}\r\n")
        self.assertIsInstance(err, ParseError)

    def test_brace_not_at_end_is_error(self):
        err = parse_one(b"A1 LOGIN {5} x\r\n")
        self.assertIsInstance(err, ParseError)
        self.assertEqual(err.tag, "A1")


class MalformedTests(unittest.TestCase):
    def test_empty_line(self):
        err = parse_one(b"\r\n")
        self.assertEqual(err, ParseError(None, "empty command"))

    def test_tag_without_command(self):
        err = parse_one(b"A1\r\n")
        self.assertIsInstance(err, ParseError)
        self.assertEqual(err.tag, "A1")

    def test_unterminated_quote(self):
        err = parse_one(b'A1 LOGIN "abc\r\n')
        self.assertIsInstance(err, ParseError)
        self.assertEqual(err.tag, "A1")

    def test_bad_escape(self):
        err = parse_one(b'A1 LOGIN "a\\nb"\r\n')
        self.assertIsInstance(err, ParseError)

    def test_nul_byte(self):
        err = parse_one(b"A1 LOGIN a\x00b c\r\n")
        self.assertIsInstance(err, ParseError)
        self.assertEqual(err.tag, "A1")

    def test_non_ascii_atom(self):
        err = parse_one("A1 SELECT café\r\n".encode())
        self.assertIsInstance(err, ParseError)

    def test_unbalanced_parens(self):
        self.assertIsInstance(parse_one(b"A1 X (a b\r\n"), ParseError)
        self.assertIsInstance(parse_one(b"A1 X a)\r\n"), ParseError)

    def test_line_too_long_is_fatal(self):
        p = CommandParser(Limits(max_line=1024))
        p.feed(b"A1 " + b"x" * 2000)
        events = drain(p)
        self.assertEqual(events, [ParseError(None, "line too long", fatal=True)])

    def test_line_too_long_with_terminator_is_fatal(self):
        p = CommandParser(Limits(max_line=1024))
        p.feed(b"A1 " + b"x" * 2000 + b"\r\n")
        events = drain(p)
        self.assertTrue(events and events[0].fatal)

    def test_parser_recovers_after_error(self):
        p = CommandParser()
        p.feed(b'A1 LOGIN "oops\r\nA2 NOOP\r\n')
        events = drain(p)
        self.assertIsInstance(events[0], ParseError)
        self.assertEqual(events[1], Command("A2", "NOOP", ()))


if __name__ == "__main__":
    unittest.main()


class AggregateLimitTests(unittest.TestCase):
    """Review finding F7: one command cannot accumulate unbounded literal data."""

    def test_too_many_literals_refused_before_continuation(self):
        p = CommandParser(Limits(max_literals=3))
        events = []
        for _ in range(3):
            p.feed(b"A1 X {2}\r\n" if not events else b"ab {2}\r\n")
            events.extend(drain(p))
        self.assertTrue(all(isinstance(e, ContinuationRequest) for e in events))
        p.feed(b"ab {2}\r\n")  # fourth literal
        events = drain(p)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ParseError)
        self.assertIn("command too large", events[0].message)
        p.feed(b"A2 NOOP\r\n")
        self.assertEqual(drain(p), [Command("A2", "NOOP", ())])

    def test_total_command_bytes_capped(self):
        p = CommandParser(Limits(max_literal=100, max_command_bytes=150))
        p.feed(b"A1 X {90}\r\n")
        self.assertIsInstance(drain(p)[0], ContinuationRequest)
        p.feed(b"x" * 90 + b" {90}\r\n")
        events = drain(p)
        self.assertIsInstance(events[0], ParseError)
        self.assertIn("command too large", events[0].message)

    def test_in_progress_flag(self):
        p = CommandParser()
        self.assertFalse(p.in_progress)
        p.feed(b"A1 X {2}\r\n")
        drain(p)
        self.assertTrue(p.in_progress)
        p.feed(b"ab\r\n")
        drain(p)
        self.assertFalse(p.in_progress)
