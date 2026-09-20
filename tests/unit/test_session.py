import asyncio
import unittest
from unittest import mock

from imapgw.config import Settings
from imapgw.session import Session, State


class HangingWriter:
    """A StreamWriter stand-in whose drain() never completes (client stopped reading)."""

    def __init__(self):
        self.transport = mock.Mock()
        self.closed = False

    def get_extra_info(self, name):
        return ("127.0.0.1", 1)

    def write(self, data):
        pass

    async def drain(self):
        await asyncio.Event().wait()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class FlushTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_drain_aborts_within_write_timeout(self):  # F9
        writer = HangingWriter()
        settings = Settings(write_timeout=0.2)
        session = Session(mock.Mock(), writer, settings, mock.Mock(), lambda key: mock.Mock())
        session.write(b"* OK hello\r\n")
        started = asyncio.get_running_loop().time()
        await session.flush()
        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        writer.transport.abort.assert_called_once()
        self.assertIs(session.state, State.LOGOUT)
        # close() must also return promptly and not try to flush again.
        await asyncio.wait_for(session.close(), 1.0)
