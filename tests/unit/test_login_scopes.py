"""LOGIN with pod/organisation-scoped keys, which the fake API cannot produce (it always reports
inbox scope), driven through the real Session and handler with a scripted transport."""

import unittest
from unittest import mock

from imapgw import commands
from imapgw.apiclient import AgentMailClient
from imapgw.config import Settings
from imapgw.parser import Atom, Command
from imapgw.session import CommandFailed, Session, State
from tests.support.scripted_transport import ScriptedTransport, json_response

INBOX = "harsh@agentmail.to"


class FakeWriter:
    def __init__(self):
        self.transport = mock.Mock()
        self.data = bytearray()

    def get_extra_info(self, name):
        return ("127.0.0.1", 1)

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        pass

    async def wait_closed(self):
        return None


def org_router(visible_inboxes):
    def route(call):
        if call.url.endswith("/auth/me"):
            return json_response(
                200, {"scope_type": "organization", "scope_id": "org", "organization_id": "org"}
            )
        for name in visible_inboxes:
            if call.url.endswith("/inboxes/" + name.replace("@", "%40")):
                return json_response(200, {"inbox_id": name})
        return json_response(
            404, {"name": "NotFoundError", "code": "not_found", "message": "Inbox not found"}
        )

    return route


class ScopedLoginTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, router):
        transport = ScriptedTransport(router=router)

        async def nosleep(_):
            return None

        def make_client(key):
            return AgentMailClient(
                "http://api.test/v0", key, transport, max_attempts=1, sleep=nosleep
            )

        writer = FakeWriter()
        session = Session(mock.Mock(), writer, Settings(), mock.Mock(), make_client)
        return session, writer, transport

    async def test_org_key_logs_in_to_a_visible_inbox(self):
        session, writer, transport = self.make_session(org_router([INBOX]))
        await commands.login(session, Command("A1", "LOGIN", (Atom(INBOX), Atom("orgkey123"))))
        self.assertIs(session.state, State.AUTHENTICATED)
        self.assertIn(b"A1 OK", bytes(session._out))
        paths = [c.url.rsplit("/v0", 1)[1] for c in transport.calls]
        self.assertEqual(paths, ["/auth/me", "/inboxes/harsh%40agentmail.to"])

    async def test_org_key_naming_an_unknown_inbox_is_a_credentials_failure(self):
        session, writer, transport = self.make_session(org_router([INBOX]))
        with self.assertRaises(CommandFailed) as cm:
            await commands.login(
                session, Command("A1", "LOGIN", (Atom("nobody@agentmail.to"), Atom("orgkey123")))
            )
        self.assertEqual((cm.exception.status, cm.exception.code), ("NO", "AUTHENTICATIONFAILED"))
        self.assertIs(session.state, State.NOT_AUTHENTICATED)

    async def test_inbox_key_for_other_inbox_refused(self):
        def route(call):
            return json_response(
                200,
                {
                    "scope_type": "inbox",
                    "scope_id": "other@agentmail.to",
                    "inbox_id": "other@agentmail.to",
                    "organization_id": "org",
                },
            )

        session, writer, transport = self.make_session(route)
        with self.assertRaises(CommandFailed) as cm:
            await commands.login(session, Command("A1", "LOGIN", (Atom(INBOX), Atom("inboxkey"))))
        self.assertEqual(cm.exception.code, "AUTHENTICATIONFAILED")
        self.assertEqual(len(transport.calls), 1)  # no inbox lookup needed to refuse
