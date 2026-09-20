"""A deliberately small, hand-written IMAP test client over asyncio streams.

It knows just enough of the wire format to drive the server under test and to make byte-level
assertions: tagged/untagged lines, ``+`` continuations, and server literals ``{N}`` consumed by
their announced octet count. It is not a general IMAP client and is never used by the server.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

_LITERAL_TAIL = re.compile(rb"\{(\d+)\}$")
_CODE = re.compile(r"^\[([^\]]+)\]")
_FETCH_SEQ = re.compile(rb"^\* (\d+) FETCH \(")
_UID_ITEM = re.compile(rb"(?:\(| )UID (\d+)")


class ImapClientError(AssertionError):
    pass


@dataclass
class Untagged:
    line: bytes  # first line, without CRLF and without the literal bytes
    literal: bytes | None = None  # server literal that followed ``{N}`` on ``line``
    tail: bytes = b""  # bytes after the literal up to CRLF (typically b")")
    literals: list[bytes] = field(default_factory=list)  # every literal in this response

    @property
    def text(self) -> str:
        return self.line.decode("utf-8", errors="replace")


@dataclass
class Response:
    tag: str
    status: str
    text: str
    untagged: list[Untagged] = field(default_factory=list)

    def code(self) -> str | None:
        m = _CODE.match(self.text)
        return m.group(1) if m else None

    def find(self, pattern: bytes | str) -> list[Untagged]:
        pat = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        return [u for u in self.untagged if pat.search(u.line)]

    def untagged_codes(self) -> dict[str, str]:
        """``* OK [UIDNEXT 5] text`` -> {"UIDNEXT": "5"}; codes without args map to ""."""
        out: dict[str, str] = {}
        for u in self.untagged:
            m = re.match(rb"^\* OK \[([A-Z-]+)(?: ([^\]]*))?\]", u.line)
            if m:
                out[m.group(1).decode()] = (m.group(2) or b"").decode()
        return out

    def fetch_by_uid(self) -> dict[int, Untagged]:
        out: dict[int, Untagged] = {}
        for u in self.untagged:
            if not _FETCH_SEQ.match(u.line):
                continue
            m = _UID_ITEM.search(u.line) or _UID_ITEM.search(u.tail)
            if m:
                out[int(m.group(1))] = u
        return out

    def fetch_by_seq(self) -> dict[int, Untagged]:
        out: dict[int, Untagged] = {}
        for u in self.untagged:
            m = re.match(rb"^\* (\d+) FETCH ", u.line)
            if m:
                out[int(m.group(1))] = u
        return out

    def __repr__(self) -> str:
        lines = [u.text for u in self.untagged]
        return f"Response({self.tag} {self.status} {self.text!r}, untagged={lines!r})"


class ImapTestClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._timeout = timeout
        self._counter = 0
        self.greeting = b""
        self.transcript: list[bytes] = []

    @classmethod
    async def connect(cls, host: str, port: int, *, timeout: float = 5.0) -> ImapTestClient:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        client = cls(reader, writer, timeout)
        client.greeting = await client.read_line()
        return client

    def next_tag(self) -> str:
        self._counter += 1
        return f"A{self._counter:03d}"

    async def send_raw(self, data: bytes) -> None:
        self.transcript.append(b"C: " + data)
        self._writer.write(data)
        await asyncio.wait_for(self._writer.drain(), self._timeout)

    async def read_line(self) -> bytes:
        try:
            line = await asyncio.wait_for(self._reader.readuntil(b"\r\n"), self._timeout)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                self.transcript.append(b"S: " + exc.partial)
                return exc.partial
            raise ImapClientError("connection closed while waiting for a line") from exc
        except TimeoutError as exc:
            raise ImapClientError(
                f"timed out waiting for a line; transcript={self.transcript[-6:]}"
            ) from exc
        self.transcript.append(b"S: " + line)
        return line[:-2]

    async def read_exactly(self, n: int) -> bytes:
        data = await asyncio.wait_for(self._reader.readexactly(n), self._timeout)
        self.transcript.append(b"S: <%d octets>" % n)
        return data

    async def read_eof(self) -> bytes:
        """Return remaining bytes until the server closes the connection."""
        return await asyncio.wait_for(self._reader.read(), self._timeout)

    async def cmd(self, text: str, *, tag: str | None = None) -> Response:
        tag = tag or self.next_tag()
        await self.send_raw(f"{tag} {text}\r\n".encode())
        return await self.read_response(tag)

    async def cmd_literal(
        self,
        prefix: str,
        literal: bytes,
        *,
        tag: str | None = None,
        expect_continuation: bool = True,
    ) -> Response:
        """Send ``<tag> <prefix> {N}`` and, after the ``+`` line, the literal bytes."""
        tag = tag or self.next_tag()
        await self.send_raw(f"{tag} {prefix} {{{len(literal)}}}\r\n".encode())
        line = await self.read_line()
        if expect_continuation:
            if not line.startswith(b"+"):
                # The server refused before asking for the literal; return that response.
                return await self._finish_response(tag, line)
            await self.send_raw(literal + b"\r\n")
            return await self.read_response(tag)
        return await self._finish_response(tag, line)

    async def read_response(self, tag: str) -> Response:
        line = await self.read_line()
        return await self._finish_response(tag, line)

    async def _finish_response(self, tag: str, first: bytes) -> Response:
        untagged: list[Untagged] = []
        line = first
        prefix = tag.encode() + b" "
        while True:
            if line.startswith(prefix):
                rest = line[len(prefix) :].decode("utf-8", errors="replace")
                status, _, text = rest.partition(" ")
                return Response(tag, status, text, untagged)
            if line.startswith(b"* "):
                m = _LITERAL_TAIL.search(line)
                if m:
                    first_literal = await self.read_exactly(int(m.group(1)))
                    tail = await self.read_line()
                    literals = [first_literal]
                    # A response may carry several literals (e.g. two BODY sections).
                    while (m2 := _LITERAL_TAIL.search(tail)) is not None:
                        literals.append(await self.read_exactly(int(m2.group(1))))
                        tail = tail + b"\r\n<literal>" + await self.read_line()
                    untagged.append(Untagged(line, first_literal, tail, literals))
                else:
                    untagged.append(Untagged(line))
            elif line.startswith(b"+"):
                raise ImapClientError(f"unexpected continuation request: {line!r}")
            else:
                raise ImapClientError(f"unexpected line: {line!r}")
            line = await self.read_line()

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), self._timeout)
        except (TimeoutError, ConnectionError, OSError):
            pass
