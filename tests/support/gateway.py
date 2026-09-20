"""Integration fixture: one fake API per test class, a fresh gateway (port 0, temp DB) per test."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from imapgw.config import REDACTOR, Settings
from imapgw.server import Server
from tests.support.fake_api import API_KEY, INBOX_ID, FakeApiProcess
from tests.support.imap_client import ImapTestClient

_root = logging.getLogger()
_root.addFilter(REDACTOR)
if not _root.handlers:
    _root.addHandler(logging.NullHandler())  # keep lastResort stderr noise out of test output


def test_settings(api_url: str, db_path: Path, **overrides) -> Settings:
    base = Settings(
        imap_host="127.0.0.1",
        imap_port=0,
        api_url=api_url,
        db_path=db_path,
        http_timeout=1.0,
        backoff_base=0.05,
        retry_budget=3.0,
        command_timeout=8.0,
        refresh_interval=0.0,
        log_level="DEBUG",
    )
    return base.with_overrides(**overrides)


class GatewayHandle:
    def __init__(self, server: Server) -> None:
        self.server = server

    @property
    def host(self) -> str:
        return self.server.host

    @property
    def port(self) -> int:
        return self.server.port

    async def stop(self) -> None:
        await self.server.stop()


async def start_gateway(api_url: str, db_path: Path, **overrides) -> GatewayHandle:
    server = Server(test_settings(api_url, db_path, **overrides))
    await server.start()
    return GatewayHandle(server)


class GatewayTestCase(unittest.IsolatedAsyncioTestCase):
    fake: ClassVar[FakeApiProcess]
    settings_overrides: ClassVar[dict] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.fake = FakeApiProcess()
        cls.fake.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fake.stop()

    async def asyncSetUp(self) -> None:
        await asyncio.to_thread(self.fake.reset)
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "uids.sqlite3"
        self.gw = await start_gateway(self.fake.base_url, self.db_path, **self.settings_overrides)
        self._clients: list[ImapTestClient] = []

    async def asyncTearDown(self) -> None:
        for c in self._clients:
            await c.close()
        await self.gw.stop()
        self._tmp.cleanup()

    async def restart_gateway(self, **overrides) -> None:
        await self.gw.stop()
        self.gw = await start_gateway(
            self.fake.base_url, self.db_path, **{**self.settings_overrides, **overrides}
        )

    async def client(self, timeout: float = 5.0) -> ImapTestClient:
        c = await ImapTestClient.connect(self.gw.host, self.gw.port, timeout=timeout)
        self._clients.append(c)
        return c

    async def logged_in(self, timeout: float = 5.0) -> ImapTestClient:
        c = await self.client(timeout)
        resp = await c.cmd(f'LOGIN "{INBOX_ID}" "{API_KEY}"')
        assert resp.status == "OK", resp
        return c

    async def wait_for(self, predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(interval)
