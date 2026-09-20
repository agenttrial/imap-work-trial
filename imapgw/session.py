"""One IMAP session per TCP connection: read loop, literal handshake, state machine, dispatch."""

from __future__ import annotations

import asyncio
import enum
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from imapgw import responses as r
from imapgw.apiclient import AgentMailClient, AgentMailError, AuthError, UnavailableError
from imapgw.config import REDACTOR, Settings
from imapgw.parser import Command, CommandParser, ContinuationRequest, Limits, ParseError

if TYPE_CHECKING:
    from imapgw.mailbox import MailboxService, MailboxSnapshot

log = logging.getLogger("imapgw.session")

CAPABILITIES: tuple[str, ...] = ("IMAP4rev1", "UIDPLUS", "ID", "NAMESPACE")


class State(enum.Enum):
    NOT_AUTHENTICATED = 1
    AUTHENTICATED = 2
    SELECTED = 3
    LOGOUT = 4


@dataclass
class Selected:
    name: str
    read_only: bool
    uids: list[int]
    seq_of: dict[int, int]
    view_version: int
    # Flags last reported to this client per UID (including \Deleted), so that changes made by
    # other sessions or upstream can be announced as untagged FETCH at the next safe point.
    flags_seen: dict[int, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_snapshot(
        cls, snapshot: MailboxSnapshot, read_only: bool, deleted: set[int]
    ) -> Selected:
        uids = [item.uid for item in snapshot.items]
        return cls(
            name=snapshot.name,
            read_only=read_only,
            uids=uids,
            seq_of={uid: i + 1 for i, uid in enumerate(uids)},
            view_version=snapshot.version,
            flags_seen={
                item.uid: (item.flags | {"\\Deleted"} if item.uid in deleted else item.flags)
                for item in snapshot.items
            },
        )


class CommandFailed(Exception):
    """Raised by handlers to produce a tagged NO/BAD without crashing the session."""

    def __init__(self, status: str, text: str, code: str | None = None) -> None:
        super().__init__(text)
        self.status = status
        self.text = text
        self.code = code


def bad(text: str, code: str | None = None) -> CommandFailed:
    return CommandFailed("BAD", text, code)


def no(text: str, code: str | None = None) -> CommandFailed:
    return CommandFailed("NO", text, code)


class Session:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        settings: Settings,
        mailboxes: MailboxService,
        make_client,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.settings = settings
        self.mailboxes = mailboxes
        self._make_client = make_client
        self.conn_id = secrets.token_hex(4)
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.state = State.NOT_AUTHENTICATED
        self.inbox_id: str | None = None
        self.api: AgentMailClient | None = None
        self._api_key: str | None = None
        self.selected: Selected | None = None
        self.parser = CommandParser(
            Limits(
                settings.max_line,
                settings.max_literal,
                settings.max_command_bytes,
                settings.max_literals,
            )
        )
        self._out = bytearray()
        self._closed = False
        self._assembly_deadline: float | None = None
        self._deferred: list[asyncio.Future] = []

    def defer(self, task: asyncio.Future) -> None:
        """Register follow-up work (already running) to be awaited, bounded, after the current
        command has answered. Its outcome can never change the command's tagged response."""
        self._deferred.append(task)

    # ----- output ------------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        self._out.extend(data)

    async def flush(self) -> None:
        if not self._out or self._closed:
            self._out.clear()
            return
        data = bytes(self._out)
        self._out.clear()
        try:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), timeout=self.settings.write_timeout)
        except TimeoutError:
            # The client stopped reading. Abort rather than hold the session (and its buffers)
            # open indefinitely.
            log.warning("[%s] client is not reading; aborting connection", self.conn_id)
            self._abort()
        except (ConnectionError, OSError):
            self._closed = True
            self.state = State.LOGOUT

    def _abort(self) -> None:
        self._closed = True
        self.state = State.LOGOUT
        transport = self.writer.transport
        try:
            transport.abort()
        except Exception:  # pragma: no cover - transport already gone
            pass

    # ----- main loop ---------------------------------------------------------------------

    async def run(self) -> None:
        log.info("[%s] connection from %s", self.conn_id, self.peer)
        caps = " ".join(CAPABILITIES)
        self.write(r.untagged_ok("imapgw ready", code=f"CAPABILITY {caps}"))
        await self.flush()
        try:
            while self.state is not State.LOGOUT and not self._closed:
                loop = asyncio.get_running_loop()
                timeout = self.settings.idle_timeout
                if self.parser.in_progress:
                    if self._assembly_deadline is None:
                        self._assembly_deadline = loop.time() + self.settings.assembly_timeout
                    timeout = min(timeout, max(0.0, self._assembly_deadline - loop.time()))
                else:
                    self._assembly_deadline = None
                try:
                    data = await asyncio.wait_for(self.reader.read(65536), timeout=timeout)
                except TimeoutError:
                    if self.parser.in_progress:
                        self.write(r.bye("command not completed in time"))
                    else:
                        self.write(r.bye("idle timeout"))
                    break
                except (ConnectionError, OSError):
                    break
                if not data:
                    break
                self.parser.feed(data)
                try:
                    for event in self.parser.events():
                        await self._handle_event(event)
                        await self.flush()
                        if self.state is State.LOGOUT or self._closed:
                            break
                except Exception:
                    # A parser defect must never drop the connection silently.
                    log.exception("[%s] internal error while parsing input", self.conn_id)
                    self.write(r.untagged("BAD internal parser error"))
                    self.write(r.bye("closing connection"))
                    self.state = State.LOGOUT
        finally:
            await self.close()

    async def _handle_event(self, event) -> None:
        if isinstance(event, ContinuationRequest):
            self.write(r.continuation(f"Ready for {event.length} octets"))
        elif isinstance(event, ParseError):
            tag = event.tag or "*"
            self.write(
                r.tagged(tag, "BAD", event.message)
                if event.tag
                else r.untagged(f"BAD {event.message}")
            )
            if event.fatal:
                self.write(r.bye("closing connection"))
                self.state = State.LOGOUT
        elif isinstance(event, Command):
            await self.dispatch(event)

    async def dispatch(self, cmd: Command) -> None:
        from imapgw import commands  # local import keeps module dependency direction simple

        handler = commands.HANDLERS.get(cmd.name)
        log.debug("[%s] %s %s", self.conn_id, cmd.tag, cmd.name)
        if handler is None:
            self.write(r.tagged(cmd.tag, "BAD", f"unknown command {cmd.name}"))
            return
        try:
            async with asyncio.timeout(self.settings.command_timeout):
                await handler(self, cmd)
        except CommandFailed as exc:
            self.write(r.tagged(cmd.tag, exc.status, exc.text, exc.code))
        except AuthError as exc:
            if self.state is State.NOT_AUTHENTICATED:
                # At LOGIN, 401 and 403 both mean the credentials are no good (production
                # answers an unknown key with 403).
                self.write(r.tagged(cmd.tag, "NO", "invalid credentials", "AUTHENTICATIONFAILED"))
            elif exc.status == 403:
                # A valid key that lacks the permission for this operation (restricted keys).
                # Refuse the command, keep the session.
                log.info("[%s] upstream denied %s for this key (403)", self.conn_id, cmd.name)
                self.write(
                    r.tagged(cmd.tag, "NO", f"this API key may not perform {cmd.name}", "CANNOT")
                )
            else:
                log.warning("[%s] upstream rejected credentials mid-session", self.conn_id)
                self.write(r.bye("credentials no longer valid"))
                self.state = State.LOGOUT
        except UnavailableError as exc:
            log.warning("[%s] upstream unavailable during %s: %s", self.conn_id, cmd.name, exc)
            self.write(
                r.tagged(
                    cmd.tag, "NO", f"upstream unavailable, try again ({cmd.name})", "UNAVAILABLE"
                )
            )
        except AgentMailError as exc:
            log.warning("[%s] upstream error during %s: %s", self.conn_id, cmd.name, exc)
            self.write(r.tagged(cmd.tag, "NO", f"upstream error ({cmd.name})"))
        except TimeoutError:
            log.warning("[%s] command %s timed out", self.conn_id, cmd.name)
            self.write(r.tagged(cmd.tag, "NO", f"upstream timeout ({cmd.name})", "UNAVAILABLE"))
        except Exception:
            log.exception("[%s] internal error during %s", self.conn_id, cmd.name)
            self.write(r.tagged(cmd.tag, "NO", f"internal error (ref {self.conn_id})"))
        finally:
            # A failed LOGIN leaves credentials registered until here so the redaction filter
            # covered whatever was logged above; now drop them.
            if self.state is State.NOT_AUTHENTICATED and self.api is not None:
                self.clear_credentials()
        await self._run_deferred()

    async def _run_deferred(self) -> None:
        """Wait briefly for post-command work (for example the re-listing after APPEND) so the
        resulting EXISTS can be announced now rather than at the next NOOP. The wait is bounded
        and shielded: a slow or failing follow-up neither blocks nor fails anything."""
        pending, self._deferred = self._deferred, []
        for task in pending:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.settings.post_command_wait
                )
            except TimeoutError:
                log.debug("[%s] post-command work still running; will announce later", self.conn_id)
            except Exception as exc:  # noqa: BLE001 - already logged where it happened
                log.debug("[%s] post-command work failed: %s", self.conn_id, exc)
        if pending and self.state is State.SELECTED:
            self.flush_pending()
            await self.flush()

    # ----- credentials -------------------------------------------------------------------

    def set_credentials(self, inbox_id: str, api_key: str) -> AgentMailClient:
        REDACTOR.register(api_key)
        self._api_key = api_key
        self.inbox_id = inbox_id
        self.api = self._make_client(api_key)
        return self.api

    def clear_credentials(self) -> None:
        if self._api_key:
            REDACTOR.unregister(self._api_key)
        self._api_key = None
        self.api = None

    # ----- mailbox helpers ---------------------------------------------------------------

    def require_state(self, *states: State) -> None:
        if self.state not in states:
            raise bad("command not allowed in this state")

    def require_selected(self) -> Selected:
        if self.state is not State.SELECTED or self.selected is None:
            raise bad("no mailbox selected")
        return self.selected

    def apply_snapshot(self, snapshot: MailboxSnapshot, read_only: bool) -> Selected:
        assert self.inbox_id is not None
        deleted = self.mailboxes.deleted_uids(self.inbox_id, snapshot.name)
        self.selected = Selected.from_snapshot(snapshot, read_only, deleted)
        self.state = State.SELECTED
        return self.selected

    def deleted_uids(self) -> set[int]:
        if self.selected is None or self.inbox_id is None:
            return set()
        return self.mailboxes.deleted_uids(self.inbox_id, self.selected.name)

    def record_flags(self, uid: int, flags: frozenset[str]) -> None:
        """Remember what this client was told, so flush_pending only announces real changes."""
        if self.selected is not None:
            self.selected.flags_seen[uid] = flags

    def flush_pending(self) -> None:
        """Announce changes since the client last heard from us, at a safe point: EXPUNGE for
        vanished items (highest sequence first), EXISTS for arrivals, and untagged FETCH FLAGS
        for items whose flags changed through another session or upstream."""
        sel = self.selected
        if sel is None or self.inbox_id is None:
            return
        latest = self.mailboxes.latest(self.inbox_id, sel.name)
        if latest is None:
            return
        live = {item.uid for item in latest.items}
        for seq in range(len(sel.uids), 0, -1):
            uid = sel.uids[seq - 1]
            if uid not in live:
                self.write(r.expunge(seq))
                del sel.uids[seq - 1]
                sel.flags_seen.pop(uid, None)
        known = set(sel.uids)
        added = [item.uid for item in latest.items if item.uid not in known]
        if added:
            sel.uids.extend(added)
            sel.uids.sort()
            self.write(r.exists(len(sel.uids)))
        sel.seq_of = {uid: i + 1 for i, uid in enumerate(sel.uids)}
        sel.view_version = latest.version
        deleted = self.mailboxes.deleted_uids(self.inbox_id, sel.name)
        for seq, uid in enumerate(sel.uids, start=1):
            item = latest.by_uid.get(uid)
            if item is None:
                continue
            flags = self.mailboxes.flags_of(item, deleted)
            previous = sel.flags_seen.get(uid)
            if previous is not None and previous != flags:
                # UID is included so the response is valid whatever command caused it,
                # including UID commands (RFC 3501 6.4.8).
                self.write(
                    r.fetch_response(
                        seq, [b"UID " + str(uid).encode("ascii"), b"FLAGS " + r.flag_list(flags)]
                    )
                )
            sel.flags_seen[uid] = flags

    # ----- teardown ----------------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            self.clear_credentials()
            return
        await self.flush()  # bounded by write_timeout; aborts a non-reading client
        self._closed = True
        self.state = State.LOGOUT
        self.clear_credentials()
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
        except (TimeoutError, ConnectionError, OSError):
            pass
        log.info("[%s] connection closed", self.conn_id)

    async def say_bye_and_close(self, text: str) -> None:
        self.write(r.bye(text))
        await self.close()
