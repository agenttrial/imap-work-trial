"""TCP listener, connection accounting, and graceful shutdown."""

from __future__ import annotations

import asyncio
import logging

from imapgw import responses as r
from imapgw.apiclient import AgentMailClient, ThreadedTransport, Transport
from imapgw.bytecache import ByteCache
from imapgw.config import Settings
from imapgw.mailbox import MailboxService
from imapgw.session import Session
from imapgw.uidstore import UidStore

log = logging.getLogger("imapgw.server")


class Server:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self._owns_transport = transport is None
        self.transport: Transport = transport or ThreadedTransport()
        self.store = UidStore(settings.db_path)
        self.cache = ByteCache(settings.cache_bytes)
        self.mailboxes = MailboxService(self.store, self.cache, settings)
        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[Session, asyncio.Task[None]] = {}
        self.host = settings.imap_host
        self.port = settings.imap_port

    def make_client(self, api_key: str) -> AgentMailClient:
        s = self.settings
        return AgentMailClient(
            s.api_url,
            api_key,
            self.transport,
            timeout=s.http_timeout,
            max_attempts=s.max_attempts,
            retry_budget=s.retry_budget,
            backoff_base=s.backoff_base,
        )

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self.settings.imap_host, self.settings.imap_port
        )
        sock = self._server.sockets[0]
        self.host, self.port = sock.getsockname()[:2]
        log.info("imapgw listening on %s:%d, API %s", self.host, self.port, self.settings.api_url)

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._sessions) >= self.settings.max_connections:
            try:
                writer.write(r.bye("too many connections"))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        session = Session(reader, writer, self.settings, self.mailboxes, self.make_client)
        task = asyncio.current_task()
        assert task is not None
        self._sessions[session] = task
        try:
            await session.run()
        finally:
            self._sessions.pop(session, None)

    async def serve_forever(self) -> None:
        assert self._server is not None
        await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

        async def _farewell(session: Session) -> None:
            try:
                await asyncio.wait_for(session.say_bye_and_close("server shutting down"), 5.0)
            except Exception:  # noqa: BLE001 - best effort; a stuck client must not block stop
                session._abort()

        await asyncio.gather(*(_farewell(s) for s in list(self._sessions)), return_exceptions=True)
        for task in list(self._sessions.values()):
            task.cancel()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except TimeoutError:
                pass
            self._server = None
        self.store.close()
        if self._owns_transport and hasattr(self.transport, "close"):
            self.transport.close()
