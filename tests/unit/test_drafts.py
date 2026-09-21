import unittest
from email import message_from_bytes, policy

from imapgw.drafts import (
    DraftFields,
    DraftParseError,
    api_body,
    client_id_for,
    content_key,
    html_to_text,
    parse_append,
    parse_iso8601,
    render_draft,
)

INBOX = "candidate@imap.test"

DRAFT_ADDRESSES = {
    "inbox_id": INBOX,
    "draft_id": "draft_existing_addresses",
    "labels": [],
    "to": ["primary@example.net"],
    "cc": ["copy@example.net"],
    "bcc": ["audit@example.net"],
    "reply_to": ["replies@example.net"],
    "subject": "Draft with address fields",
    "text": "Please keep all recipients on this draft.",
    "updated_at": "2026-08-17T23:00:00.000Z",
    "created_at": "2026-08-17T23:00:00.000Z",
}


class RenderTests(unittest.TestCase):
    def test_render_is_deterministic_and_crlf(self):
        a = render_draft(DRAFT_ADDRESSES, INBOX)
        b = render_draft(dict(DRAFT_ADDRESSES), INBOX)
        self.assertEqual(a, b)
        self.assertNotIn(b"\n", a.replace(b"\r\n", b""))
        self.assertTrue(a.endswith(b"\r\n"))

    def test_render_round_trips_all_fields(self):
        raw = render_draft(DRAFT_ADDRESSES, INBOX)
        msg = message_from_bytes(raw, policy=policy.default)
        self.assertEqual(msg["From"], INBOX)
        self.assertEqual(str(msg["To"]), "primary@example.net")
        self.assertEqual(str(msg["Cc"]), "copy@example.net")
        self.assertEqual(str(msg["Bcc"]), "audit@example.net")
        self.assertEqual(str(msg["Reply-To"]), "replies@example.net")
        self.assertEqual(msg["Subject"], "Draft with address fields")
        self.assertEqual(msg["Date"], "Mon, 17 Aug 2026 23:00:00 +0000")
        self.assertEqual(msg["Message-ID"], "<draft_existing_addresses@imap.test>")
        self.assertEqual(msg["X-AgentMail-Draft-Id"], "draft_existing_addresses")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertEqual(
            msg.get_content().replace("\r\n", "\n").rstrip("\n"), DRAFT_ADDRESSES["text"]
        )

    def test_non_ascii_subject_and_body(self):
        draft = {
            **DRAFT_ADDRESSES,
            "subject": "Unicode: café 東京 \U0001f680",
            "text": "naïve \U0001f680",
        }
        raw = render_draft(draft, INBOX)
        self.assertGreater(len(raw), len(raw.decode("utf-8")))
        header_part = raw.split(b"\r\n\r\n", 1)[0]
        self.assertTrue(all(b < 0x80 for b in header_part), "headers must be 7-bit")
        msg = message_from_bytes(raw, policy=policy.default)
        self.assertEqual(msg["Subject"], draft["subject"])
        self.assertEqual(msg.get_content().replace("\r\n", "\n").rstrip("\n"), draft["text"])

    def test_empty_optional_fields_omitted(self):
        raw = render_draft(
            {"draft_id": "d1", "to": [], "subject": "", "text": "", "updated_at": ""}, INBOX
        )
        self.assertNotIn(b"\r\nTo:", raw)
        self.assertNotIn(b"\r\nSubject:", raw)
        self.assertIn(b"\r\nFrom: " + INBOX.encode(), b"\r\n" + raw)

    def test_content_key_changes_only_with_content(self):
        base = render_draft(DRAFT_ADDRESSES, INBOX)
        same_labels = render_draft({**DRAFT_ADDRESSES, "labels": ["reviewed"]}, INBOX)
        edited = render_draft({**DRAFT_ADDRESSES, "text": "changed"}, INBOX)
        self.assertEqual(content_key("d", base), content_key("d", same_labels))
        self.assertNotEqual(content_key("d", base), content_key("d", edited))
        self.assertTrue(content_key("d", base).startswith("d#"))


class ParseTests(unittest.TestCase):
    RAW = (
        b"From: candidate@imap.test\r\n"
        b'To: Ada <ada@example.com>,\r\n "Lovelace, B" <b@example.com>\r\n'
        b"Cc: copy@example.com\r\n"
        b"Bcc: audit@example.com\r\n"
        b"Reply-To: replies@example.com\r\n"
        b"Subject: =?UTF-8?B?VW5pY29kZSBjaGVjazogY2Fmw6ksIOadseS6rCwg8J+agA==?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"na=C3=AFve line 1\r\nline 2\r\n"
    )

    def test_parse_plain_message(self):
        fields = parse_append(self.RAW)
        self.assertEqual(fields.to, ("Ada <ada@example.com>", '"Lovelace, B" <b@example.com>'))
        self.assertEqual(fields.cc, ("copy@example.com",))
        self.assertEqual(fields.bcc, ("audit@example.com",))
        self.assertEqual(fields.reply_to, ("replies@example.com",))
        self.assertEqual(fields.subject, "Unicode check: café, 東京, \U0001f680")
        self.assertEqual(fields.text, "naïve line 1\nline 2\n")

    def test_parse_multipart_alternative_takes_plain(self):
        raw = (
            b"To: x@example.com\r\nSubject: alt\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain body\r\n"
            b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html body</p>\r\n"
            b"--b--\r\n"
        )
        fields = parse_append(raw)
        self.assertEqual(fields.text.strip(), "plain body")

    def test_html_only_is_flattened_to_text(self):
        # Thunderbird saves drafts as text/html with no plain alternative (observed 2026-09-20).
        raw = (
            b"To: x@example.com\r\nSubject: h\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/html; charset=UTF-8\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
            b"<!DOCTYPE html>\r\n<html>\r\n  <head>\r\n\r\n"
            b'    <meta http-equiv="content-type" content="text/html; charset=UTF-8">\r\n'
            b"  </head>\r\n  <body>\r\n    <p>This is a line of text</p>\r\n  </body>\r\n</html>"
        )
        fields = parse_append(raw)
        self.assertEqual(fields.text, "This is a line of text\n")
        self.assertEqual(fields.subject, "h")

    def test_html_in_undecodable_charset_rejected(self):
        raw = b"To: x@example.com\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>\xff</p>\r\n"
        with self.assertRaises(DraftParseError):
            parse_append(raw)

    def test_no_text_part_at_all_rejected(self):
        raw = (
            b"To: x@example.com\r\nMIME-Version: 1.0\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n"
        )
        with self.assertRaises(DraftParseError) as cm:
            parse_append(raw)
        self.assertIn("text/plain or text/html", str(cm.exception))

    def test_attachment_rejected(self):
        raw = (
            b"To: x@example.com\r\nSubject: att\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b'--b\r\nContent-Type: text/plain; name="a.txt"\r\n'
            b'Content-Disposition: attachment; filename="a.txt"\r\n\r\nfile\r\n'
            b"--b--\r\n"
        )
        with self.assertRaises(DraftParseError) as cm:
            parse_append(raw)
        self.assertIn("attachments", str(cm.exception))

    def test_minimal_message_without_recipients(self):
        fields = parse_append(b"Subject: only\r\n\r\nbody\r\n")
        self.assertEqual(fields, DraftFields(subject="only", text="body\n"))

    def test_render_then_parse_round_trip(self):
        raw = render_draft(DRAFT_ADDRESSES, INBOX)
        fields = parse_append(raw)
        self.assertEqual(fields.to, ("primary@example.net",))
        self.assertEqual(fields.cc, ("copy@example.net",))
        self.assertEqual(fields.bcc, ("audit@example.net",))
        self.assertEqual(fields.reply_to, ("replies@example.net",))
        self.assertEqual(fields.subject, DRAFT_ADDRESSES["subject"])
        self.assertEqual(fields.text.rstrip("\n"), DRAFT_ADDRESSES["text"])

    def test_api_body_and_client_id(self):
        fields = parse_append(self.RAW)
        body = api_body(fields, client_id_for(self.RAW))
        self.assertEqual(body["to"], list(fields.to))
        self.assertEqual(body["cc"], ["copy@example.com"])
        self.assertNotIn("html", body)
        self.assertNotIn("attachments", body)
        self.assertTrue(body["client_id"].startswith("imapgw-"))
        self.assertEqual(client_id_for(self.RAW), client_id_for(self.RAW))

    def test_parse_iso8601(self):
        dt = parse_iso8601("2026-08-17T16:00:00.000Z")
        self.assertEqual((dt.year, dt.hour, dt.tzinfo is not None), (2026, 16, True))
        self.assertEqual(parse_iso8601("garbage").year, 1970)


class ReviewRegressionTests(unittest.TestCase):
    def test_undecodable_body_is_rejected_not_replaced(self):  # F19
        with self.assertRaises(DraftParseError):
            parse_append(
                b"To: x@example.com\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n\xff\r\n"
            )

    def test_declared_latin1_decodes_and_undeclared_utf8_accepted(self):
        f = parse_append(b"Content-Type: text/plain; charset=iso-8859-1\r\n\r\ncaf\xe9\r\n")
        self.assertEqual(f.text, "café\n")
        f = parse_append(b"Subject: x\r\n\r\ncaf\xc3\xa9\r\n")
        self.assertEqual(f.text, "café\n")
        with self.assertRaises(DraftParseError):
            parse_append(b"Content-Type: text/plain; charset=no-such-charset\r\n\r\nabc\r\n")

    def test_base64_body_decoded_strictly(self):
        import base64

        good = base64.b64encode("café".encode()).decode()
        raw = (
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Transfer-Encoding: base64\r\n\r\n{good}\r\n"
        ).encode()
        f = parse_append(raw)
        self.assertEqual(f.text, "café")

    def test_date_comes_from_created_at_so_metadata_updates_do_not_churn(self):
        base = {**DRAFT_ADDRESSES, "created_at": "2026-08-17T23:00:00.000Z"}
        relabelled = {**base, "labels": ["reviewed"], "updated_at": "2026-08-18T09:00:00.000Z"}
        self.assertEqual(render_draft(base, INBOX), render_draft(relabelled, INBOX))
        self.assertEqual(
            content_key("d", render_draft(base, INBOX)),
            content_key("d", render_draft(relabelled, INBOX)),
        )
        msg = message_from_bytes(render_draft(relabelled, INBOX), policy=policy.default)
        self.assertEqual(msg["Date"], "Mon, 17 Aug 2026 23:00:00 +0000")


class HtmlToTextTests(unittest.TestCase):
    def test_paragraphs_breaks_and_entities(self):
        self.assertEqual(
            html_to_text("<p>One</p><p>Two &amp; three&nbsp;four</p>"), "One\n\nTwo & three four\n"
        )
        self.assertEqual(
            html_to_text("line one<br>line two<br/>line three"), "line one\nline two\nline three\n"
        )

    def test_lists_and_blocks(self):
        self.assertEqual(
            html_to_text("<ul><li>alpha</li><li>beta</li></ul><div>after</div>"),
            "- alpha\n- beta\n\nafter\n",
        )

    def test_links_keep_target_when_it_differs(self):
        self.assertEqual(
            html_to_text(
                '<p>See <a href="https://example.com/x">the docs</a> and '
                '<a href="mailto:a@b.c">a@b.c</a>.</p>'
            ),
            "See the docs <https://example.com/x> and a@b.c.\n",
        )

    def test_pre_keeps_whitespace_and_head_is_dropped(self):
        self.assertEqual(html_to_text("<pre>  keep\n   this</pre>"), "  keep\n   this\n")
        self.assertEqual(
            html_to_text("<style>p{color:red}</style><script>x()</script><h1>Title</h1>plain"),
            "Title\n\nplain\n",
        )

    def test_unicode_and_degenerate_input(self):
        self.assertEqual(html_to_text("<p>café 東京 \U0001f680</p>"), "café 東京 🚀\n")
        self.assertEqual(html_to_text(""), "")
        self.assertEqual(html_to_text("no tags at all"), "no tags at all\n")
        self.assertEqual(html_to_text("<p><b>unclosed"), "unclosed\n")


if __name__ == "__main__":
    unittest.main()
