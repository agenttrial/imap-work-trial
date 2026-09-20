"""Incremental, sans-I/O parser for IMAP4rev1 client commands.

Feed raw bytes with :meth:`CommandParser.feed`, then drain :meth:`CommandParser.events`.
Events are :class:`Command` (a complete command), :class:`ContinuationRequest`
(the client announced a ``{N}`` literal and must be told to continue), or
:class:`ParseError`.

Grammar covered (RFC 3501 section 9, the subset needed for the supported slice):

* tag, command name, arguments separated by single spaces;
* atoms, including FETCH section brackets such as ``BODY.PEEK[HEADER.FIELDS (From To)]<0.10>``
  which are consumed as one atom;
* quoted strings with ``\\"`` and ``\\\\`` escapes;
* synchronising literals ``{N}`` (the client waits for ``+`` before sending N octets);
* parenthesised lists, nested.

Deliberately rejected: non-synchronising literals ``{N+}``, NUL bytes, quoted strings containing
CR or LF, lines longer than :attr:`Limits.max_line`, literals larger than
:attr:`Limits.max_literal`. Bare LF is accepted as a line terminator for convenience with ``nc``.

All message content stays ``bytes``; only atoms are decoded (ASCII).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Atom:
    value: str


@dataclass(frozen=True)
class QuotedStr:
    value: bytes


@dataclass(frozen=True)
class LiteralStr:
    value: bytes


@dataclass(frozen=True)
class ListTok:
    items: tuple[Token, ...]


Token = Atom | QuotedStr | LiteralStr | ListTok


@dataclass(frozen=True)
class Command:
    tag: str
    name: str
    args: tuple[Token, ...]


@dataclass(frozen=True)
class ContinuationRequest:
    length: int


@dataclass(frozen=True)
class ParseError:
    tag: str | None
    message: str
    fatal: bool = False


Event = Command | ContinuationRequest | ParseError


@dataclass(frozen=True)
class Limits:
    max_line: int = 64 * 1024
    max_literal: int = 1024 * 1024
    # Aggregate bounds for one command, so a client cannot keep a single command open forever
    # while feeding literal after literal (each individually under max_literal).
    max_command_bytes: int = 1024 * 1024 + 64 * 1024
    max_literals: int = 16


class TokenizeError(Exception):
    pass


_LITERAL_TAIL = re.compile(rb"(?:^|[ (])\{(\d{1,12})\}$")  # digit cap: int() must never fail
_TAG_RE = re.compile(rb"[\x21-\x7e]{1,64}")
_ATOM_STOP = frozenset(b'(){" ')


def astring_bytes(token: Token) -> bytes:
    """Return the byte value of an astring token (atom, quoted, or literal)."""
    if isinstance(token, Atom):
        return token.value.encode("ascii")
    if isinstance(token, QuotedStr | LiteralStr):
        return token.value
    raise TokenizeError("expected a string, got a list")


def astring_text(token: Token) -> str:
    """Return an astring token decoded as UTF-8 (mailbox names, credentials)."""
    return astring_bytes(token).decode("utf-8", errors="strict")


@dataclass
class _Pending:
    """State of a command whose segments are still arriving."""

    tag: str | None = None
    pieces: list[tuple[str, bytes]] = field(default_factory=list)  # ("line"|"literal", data)
    total_bytes: int = 0
    literal_count: int = 0


class CommandParser:
    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._buf = bytearray()
        self._pending: _Pending | None = None
        self._awaiting_literal: int | None = None

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    @property
    def in_progress(self) -> bool:
        """True while any command is being assembled: a partial line in the buffer, a pending
        multi-segment command, or an announced literal still arriving."""
        return bool(self._buf) or self._pending is not None or self._awaiting_literal is not None

    def reset(self) -> None:
        self._buf.clear()
        self._pending = None
        self._awaiting_literal = None

    def events(self) -> Iterator[Event]:
        while True:
            if self._awaiting_literal is not None:
                n = self._awaiting_literal
                if len(self._buf) < n:
                    return
                literal = bytes(self._buf[:n])
                del self._buf[:n]
                self._awaiting_literal = None
                assert self._pending is not None
                self._pending.pieces.append(("literal", literal))
                continue

            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) > self.limits.max_line:
                    self._buf.clear()
                    self._pending = None
                    yield ParseError(None, "line too long", fatal=True)
                return
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > self.limits.max_line:
                self._pending = None
                yield ParseError(None, "line too long", fatal=True)
                return
            yield from self._consume_line(line)

    def _consume_line(self, line: bytes) -> Iterator[Event]:
        if self._pending is None:
            self._pending = _Pending(tag=_extract_tag(line))
        pending = self._pending

        if b"\x00" in line:
            self._pending = None
            yield ParseError(pending.tag, "NUL byte in command")
            return

        pending.total_bytes += len(line) + 2
        m = _LITERAL_TAIL.search(line)
        if m:
            n = int(m.group(1))
            if n > self.limits.max_literal:
                self._pending = None
                yield ParseError(pending.tag, f"literal too large (max {self.limits.max_literal})")
                return
            pending.literal_count += 1
            pending.total_bytes += n
            if (
                pending.literal_count > self.limits.max_literals
                or pending.total_bytes > self.limits.max_command_bytes
            ):
                # Refuse before the continuation, so no literal bytes are ever accepted.
                self._pending = None
                yield ParseError(pending.tag, "command too large")
                return
            pending.pieces.append(("line", line[: m.start(1) - 1]))  # drop the "{N}"
            self._awaiting_literal = n
            yield ContinuationRequest(n)
            return

        if pending.total_bytes > self.limits.max_command_bytes:
            self._pending = None
            yield ParseError(pending.tag, "command too large")
            return
        pending.pieces.append(("line", line))
        self._pending = None
        yield _build_command(pending)


def _extract_tag(line: bytes) -> str | None:
    """The tag echoed in error responses. Anything that is not short printable ASCII is not
    echoed at all (the response is untagged), so a hostile tag cannot reach the wire."""
    head = line.split(b" ", 1)[0]
    if not head or not _TAG_RE.fullmatch(head):
        return None
    return head.decode("ascii")


def _build_command(pending: _Pending) -> Event:
    try:
        tokens = _tokenize_pieces(pending.pieces)
    except TokenizeError as exc:
        return ParseError(pending.tag, str(exc))
    if not tokens:
        return ParseError(None, "empty command")
    if len(tokens) < 2:
        return ParseError(pending.tag, "missing command")
    tag_tok, name_tok = tokens[0], tokens[1]
    if not isinstance(tag_tok, Atom) or not isinstance(name_tok, Atom):
        return ParseError(pending.tag, "tag and command must be atoms")
    if any(ch in tag_tok.value for ch in "+*"):
        return ParseError(None, "invalid tag")
    return Command(tag_tok.value, name_tok.value.upper(), tuple(tokens[2:]))


def _tokenize_pieces(pieces: list[tuple[str, bytes]]) -> list[Token]:
    root: list[Token] = []
    stack: list[list[Token]] = [root]
    for kind, data in pieces:
        if kind == "literal":
            stack[-1].append(LiteralStr(data))
            continue
        _tokenize_line(data, stack)
    if len(stack) != 1:
        raise TokenizeError("unbalanced parentheses")
    return root


def _tokenize_line(line: bytes, stack: list[list[Token]]) -> None:
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == 0x20:
            i += 1
            continue
        if ch == ord("("):
            new: list[Token] = []
            stack.append(new)
            i += 1
            continue
        if ch == ord(")"):
            if len(stack) == 1:
                raise TokenizeError("unbalanced parentheses")
            items = stack.pop()
            stack[-1].append(ListTok(tuple(items)))
            i += 1
            continue
        if ch == ord('"'):
            value, i = _read_quoted(line, i + 1)
            stack[-1].append(QuotedStr(value))
            continue
        if ch == ord("{"):
            raise TokenizeError("literal must be the last item on a line")
        if ch < 0x20 or ch == 0x7F:
            raise TokenizeError("control character in command")
        value, i = _read_atom(line, i)
        stack[-1].append(Atom(value))


def _read_quoted(line: bytes, i: int) -> tuple[bytes, int]:
    out = bytearray()
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == ord("\\"):
            if i + 1 >= n or line[i + 1] not in (ord('"'), ord("\\")):
                raise TokenizeError("invalid escape in quoted string")
            out.append(line[i + 1])
            i += 2
            continue
        if ch == ord('"'):
            return bytes(out), i + 1
        if ch < 0x20 or ch == 0x7F:
            raise TokenizeError("control character in quoted string")
        if ch >= 0x80:
            raise TokenizeError("non-ASCII in quoted string, use a literal")
        out.append(ch)
        i += 1
    raise TokenizeError("unterminated quoted string")


def _read_atom(line: bytes, i: int) -> tuple[str, int]:
    start = i
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == ord("["):
            i = _skip_bracket(line, i)
            continue
        if ch in _ATOM_STOP:
            break
        if ch < 0x20 or ch == 0x7F:
            raise TokenizeError("control character in atom")
        if ch >= 0x80:
            raise TokenizeError("non-ASCII in atom, use a quoted string or literal")
        i += 1
    if i == start:
        raise TokenizeError("expected an atom")
    return line[start:i].decode("ascii"), i


def _skip_bracket(line: bytes, i: int) -> int:
    """Consume a FETCH section from '[' through its matching ']' (quotes allowed inside)."""
    depth = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == ord('"'):
            _, i = _read_quoted(line, i + 1)
            continue
        if ch == ord("["):
            depth += 1
        elif ch == ord("]"):
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch < 0x20 or ch == 0x7F or ch >= 0x80:
            raise TokenizeError("invalid character in section")
        i += 1
    raise TokenizeError("unterminated section bracket")
