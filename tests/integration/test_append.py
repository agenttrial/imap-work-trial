import asyncio
import unittest
from email import message_from_bytes, policy

from tests.support.gateway import GatewayTestCase

APPEND_RAW = (
    b"From: candidate@imap.test\r\n"
    b'To: Ada <ada@example.com>,\r\n "Lovelace, B" <b@example.com>\r\n'
    b"Cc: copy@example.com\r\n"
    b"Bcc: audit@example.com\r\n"
    b"Reply-To: replies@example.com\r\n"
    b"Subject: =?UTF-8?B?VW5pY29kZSBjaGVjazogY2Fmw6ksIOadseS6rCwg8J+agA==?=\r\n"
    b"Date: Sat, 19 Sep 2026 10:00:00 +0000\r\n"
    b"Message-ID: <client-generated@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"na=C3=AFve line 1\r\nline 2\r\n"
)
EXPECTED_SUBJECT = "Unicode check: café, 東京, \U0001f680"
EXPECTED_TEXT = "naïve line 1\nline 2\n"


class AppendTests(GatewayTestCase):
    async def test_append_round_trips_through_the_api(self):
        c = await self.logged_in()
        resp = await c.cmd_literal("APPEND Drafts (\\Draft)", APPEND_RAW)
        self.assertEqual(resp.status, "OK", resp)
        code = resp.code()
        self.assertIsNotNone(code)
        kind, validity, uid_value = code.split()
        self.assertEqual(kind, "APPENDUID")
        self.assertEqual(uid_value, "3")

        drafts = {d["draft_id"]: d for d in self.fake.state()["drafts"]}
        created = [d for d in drafts.values() if d["draft_id"].startswith("draft_created_")]
        self.assertEqual(len(created), 1)
        d = created[0]
        self.assertEqual(d["to"], ["Ada <ada@example.com>", '"Lovelace, B" <b@example.com>'])
        self.assertEqual(d["cc"], ["copy@example.com"])
        self.assertEqual(d["bcc"], ["audit@example.com"])
        self.assertEqual(d["reply_to"], ["replies@example.com"])
        self.assertEqual(d["subject"], EXPECTED_SUBJECT)
        self.assertEqual(d["text"], EXPECTED_TEXT)

        sel = await c.cmd("SELECT Drafts")
        self.assertEqual(sel.untagged_codes()["UIDVALIDITY"], validity)
        self.assertIn(b"* 3 EXISTS", [u.line for u in sel.untagged])
        u = (await c.cmd("UID FETCH 3 BODY.PEEK[]")).fetch_by_uid()[3]
        msg = message_from_bytes(u.literal, policy=policy.default)
        self.assertEqual(msg["Subject"], EXPECTED_SUBJECT)
        self.assertEqual(str(msg["To"]), 'Ada <ada@example.com>, "Lovelace, B" <b@example.com>')
        self.assertEqual(str(msg["Bcc"]), "audit@example.com")
        self.assertEqual(msg.get_content().replace("\r\n", "\n"), EXPECTED_TEXT)

    async def test_append_while_drafts_selected_announces_exists(self):
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")
        resp = await c.cmd_literal(
            "APPEND Drafts", b"To: a@example.com\r\nSubject: s\r\n\r\nbody\r\n"
        )
        self.assertEqual(resp.status, "OK", resp)
        # The EXISTS follows the tagged OK once the re-listing lands (it arrives before or with
        # the next command's responses), never before the OK, because the OK must not depend
        # on the re-listing.
        follow = await c.cmd("NOOP")
        seen = [u.line for u in resp.untagged] + [u.line for u in follow.untagged]
        self.assertEqual(seen, [b"* 3 EXISTS"])
        existing = sorted((await c.cmd("UID FETCH 1:* (FLAGS)")).fetch_by_uid())
        self.assertEqual(existing, [1, 2, 3])

    async def test_append_with_date_and_flags_arguments(self):
        c = await self.logged_in()
        resp = await c.cmd_literal(
            'APPEND Drafts (\\Draft \\Seen) "19-Sep-2026 10:00:00 +0000"',
            b"Subject: dated\r\n\r\nbody\r\n",
        )
        self.assertEqual(resp.status, "OK", resp)

    async def test_append_multipart_alternative_uses_plain_part(self):
        c = await self.logged_in()
        raw = (
            b"To: x@example.com\r\nSubject: alt\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain body\r\n"
            b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html body</p>\r\n--b--\r\n"
        )
        resp = await c.cmd_literal("APPEND Drafts", raw)
        self.assertEqual(resp.status, "OK", resp)
        created = [
            d for d in self.fake.state()["drafts"] if d["draft_id"].startswith("draft_created_")
        ]
        self.assertEqual(created[0]["text"].strip(), "plain body")

    async def test_html_only_draft_is_saved_as_text(self):
        # Thunderbird's compose window saves drafts as text/html only (observed 2026-09-20).
        c = await self.logged_in()
        raw = (
            b"To: x@example.com\r\nSubject: Test Subject\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/html; charset=UTF-8\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
            b"<html><body><p>This is a line of text</p><p>Second &amp; last</p></body></html>\r\n"
        )
        resp = await c.cmd_literal("APPEND Drafts", raw)
        self.assertEqual((resp.status, resp.code()[:9]), ("OK", "APPENDUID"), resp)
        created = [
            d for d in self.fake.state()["drafts"] if d["draft_id"].startswith("draft_created_")
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["subject"], "Test Subject")
        self.assertEqual(created[0]["text"], "This is a line of text\n\nSecond & last\n")
        self.assertNotIn("html", created[0])

    async def test_binary_only_and_attachments_rejected(self):
        c = await self.logged_in()
        resp = await c.cmd_literal(
            "APPEND Drafts", b"To: x@example.com\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n"
        )
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        self.assertIn("text/plain or text/html", resp.text)
        raw = (
            b"To: x@example.com\r\nSubject: att\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b'--b\r\nContent-Type: application/octet-stream; name="a.bin"\r\n'
            b'Content-Disposition: attachment; filename="a.bin"\r\n\r\nAAAA\r\n--b--\r\n'
        )
        resp = await c.cmd_literal("APPEND Drafts", raw)
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        self.assertIn("attachments", resp.text)
        self.assertEqual(len(self.fake.state()["drafts"]), 2)

    async def test_append_to_other_mailboxes(self):
        c = await self.logged_in()
        resp = await c.cmd_literal("APPEND Nope", b"Subject: x\r\n\r\nbody\r\n")
        self.assertEqual((resp.status, resp.code()), ("NO", "TRYCREATE"))
        resp = await c.cmd_literal("APPEND INBOX", b"Subject: x\r\n\r\nbody\r\n")
        self.assertEqual((resp.status, resp.code()), ("NO", "CANNOT"))
        self.assertEqual(len(self.fake.state()["drafts"]), 2)

    async def test_append_requires_auth_and_literal(self):
        c = await self.client()
        resp = await c.cmd_literal("APPEND Drafts", b"Subject: x\r\n\r\nbody\r\n")
        self.assertEqual(resp.status, "BAD")
        c2 = await self.logged_in()
        self.assertEqual((await c2.cmd("APPEND Drafts")).status, "BAD")
        self.assertEqual((await c2.cmd("APPEND Drafts notaliteral")).status, "BAD")

    async def test_upstream_failure_creates_nothing(self):
        c = await self.logged_in()
        self.fake.fail_next(method="POST", path_prefix="/inboxes", status=503, times=10)
        resp = await c.cmd_literal("APPEND Drafts", b"Subject: x\r\n\r\nbody\r\n")
        self.assertEqual((resp.status, resp.code()), ("NO", "UNAVAILABLE"))
        self.assertEqual(len(self.fake.state()["drafts"]), 2)
        self.assertEqual((await c.cmd("NOOP")).status, "OK")

    async def test_upstream_400_is_reported_not_crashed(self):
        c = await self.logged_in()
        self.fake.fail_next(method="POST", path_prefix="/inboxes", status=400, times=1)
        resp = await c.cmd_literal("APPEND Drafts", b"Subject: x\r\n\r\nbody\r\n")
        self.assertEqual(resp.status, "NO")
        self.assertEqual((await c.cmd("NOOP")).status, "OK")


if __name__ == "__main__":
    unittest.main()


class AppendAtomicityTests(GatewayTestCase):
    """Review findings F6 and F12: the APPEND answer must match what happened upstream."""

    async def test_resync_failure_after_successful_create_still_reports_ok(self):
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")  # existing drafts get UIDs 1 and 2
        # Fail the listings that follow the POST; the POST itself is untouched.
        self.fake.fail_next(
            method="GET", path_prefix="/inboxes/candidate%40imap.test/drafts", status=503, times=10
        )
        resp = await c.cmd_literal(
            "APPEND Drafts", b"To: a@example.com\r\nSubject: atomic\r\n\r\nbody\r\n"
        )
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual(resp.code().split()[0], "APPENDUID")
        self.assertEqual(resp.code().split()[2], "3")
        self.assertEqual(len(self.fake.state()["drafts"]), 3)
        self.fake.reset()  # clears the injected failures

    async def test_append_uid_is_fetchable_immediately(self):
        c = await self.logged_in()
        resp = await c.cmd_literal(
            "APPEND Drafts", b"To: a@example.com\r\nSubject: fetch-me\r\n\r\nbody\r\n"
        )
        uid_value = int(resp.code().split()[2])
        await c.cmd("SELECT Drafts")
        u = (
            await c.cmd(f"UID FETCH {uid_value} BODY.PEEK[HEADER.FIELDS (Subject)]")
        ).fetch_by_uid()[uid_value]
        self.assertEqual(u.literal, b"Subject: fetch-me\r\n\r\n")


class AppendDeadlineTests(GatewayTestCase):
    """Re-review finding 1: a slow re-listing must never turn a successful APPEND into NO,
    even when it outlives the command deadline."""

    settings_overrides = {"command_timeout": 1.0, "http_timeout": 0.5, "post_command_wait": 0.2}

    async def test_slow_resync_beyond_command_deadline_still_answers_ok(self):
        c = await self.logged_in()
        await c.cmd("SELECT Drafts")
        # Three delayed failures: enough to outlive the 1 s command deadline, few enough that
        # the background re-listing's retries exhaust them and later commands recover.
        self.fake.fail_next(
            method="GET",
            path_prefix="/inboxes/candidate%40imap.test/drafts",
            status=503,
            delay_ms=1500,
            times=3,
        )
        resp = await c.cmd_literal(
            "APPEND Drafts", b"To: a@example.com\r\nSubject: slow\r\n\r\nbody\r\n"
        )
        self.assertEqual(resp.status, "OK", resp)
        self.assertEqual(resp.code().split()[0], "APPENDUID")
        uid_value = int(resp.code().split()[2])
        self.assertEqual(len(self.fake.state()["drafts"]), 3)

        # Once the injected failures are used up, the new draft is announced and fetchable
        # under exactly the UID that APPEND reported.
        announced = []
        for _ in range(10):
            noop = await c.cmd("NOOP")
            announced += [u.line for u in noop.untagged]
            if noop.status == "OK" and b"* 3 EXISTS" in announced:
                break
            await asyncio.sleep(0.3)
        self.assertIn(b"* 3 EXISTS", announced)
        u = (
            await c.cmd(f"UID FETCH {uid_value} BODY.PEEK[HEADER.FIELDS (Subject)]")
        ).fetch_by_uid()[uid_value]
        self.assertEqual(u.literal, b"Subject: slow\r\n\r\n")
