"""SEARCH criteria: parsing (RFC 3501 6.4.4 subset) and evaluation over mailbox items.

Supported keys: ALL, ANSWERED/UNANSWERED, DELETED/UNDELETED, DRAFT/UNDRAFT, FLAGGED/UNFLAGGED,
SEEN/UNSEEN, NEW, OLD, RECENT, KEYWORD/UNKEYWORD <label>, UID <set>, <sequence-set>,
BEFORE/ON/SINCE <date>, SENTBEFORE/SENTON/SENTSINCE <date>, LARGER/SMALLER <n>,
FROM/TO/CC/BCC/SUBJECT/BODY/TEXT <string>, HEADER <name> <string>, NOT, OR, and parenthesised
groups. CHARSET must be UTF-8 or US-ASCII.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime

from imapgw.fetch import (
    FetchSyntaxError,
    SequenceSet,
    parse_sequence_set,
    select_numbers,
    split_message,
)
from imapgw.mailbox import FLAG_ANSWERED, FLAG_DELETED, FLAG_DRAFT, FLAG_FLAGGED, FLAG_SEEN, Item
from imapgw.parser import Atom, ListTok, Token, astring_bytes


class SearchSyntaxError(ValueError):
    pass


class BadCharset(ValueError):
    pass


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_FLAG_KEYS = {
    "ANSWERED": (FLAG_ANSWERED, True),
    "UNANSWERED": (FLAG_ANSWERED, False),
    "DELETED": (FLAG_DELETED, True),
    "UNDELETED": (FLAG_DELETED, False),
    "DRAFT": (FLAG_DRAFT, True),
    "UNDRAFT": (FLAG_DRAFT, False),
    "FLAGGED": (FLAG_FLAGGED, True),
    "UNFLAGGED": (FLAG_FLAGGED, False),
    "SEEN": (FLAG_SEEN, True),
    "UNSEEN": (FLAG_SEEN, False),
}
_ITEM_TEXT_KEYS = {"FROM", "TO", "SUBJECT"}
_RAW_TEXT_KEYS = {"CC", "BCC", "BODY", "TEXT"}
_DATE_KEYS = {"BEFORE", "ON", "SINCE", "SENTBEFORE", "SENTON", "SENTSINCE"}


@dataclass(frozen=True)
class Crit:
    op: str
    args: tuple = ()


def parse_search(tokens: tuple[Token, ...]) -> Crit:
    """Return an AND criterion over the search keys. Raises SearchSyntaxError / BadCharset."""
    items = list(tokens)
    if items and isinstance(items[0], Atom) and items[0].value.upper() == "CHARSET":
        if len(items) < 2:
            raise SearchSyntaxError("CHARSET requires a value")
        charset = _text(items[1]).upper()
        if charset not in ("UTF-8", "US-ASCII"):
            raise BadCharset(charset)
        items = items[2:]
    if not items:
        raise SearchSyntaxError("SEARCH requires at least one key")
    parser = _Parser(items)
    crits = []
    while not parser.done():
        crits.append(parser.key())
    return Crit("AND", tuple(crits))


class _Parser:
    def __init__(self, items: list[Token]) -> None:
        self.items = items
        self.i = 0

    def done(self) -> bool:
        return self.i >= len(self.items)

    def next(self) -> Token:
        if self.done():
            raise SearchSyntaxError("incomplete search key")
        tok = self.items[self.i]
        self.i += 1
        return tok

    def key(self) -> Crit:
        tok = self.next()
        if isinstance(tok, ListTok):
            sub = _Parser(list(tok.items))
            crits = []
            while not sub.done():
                crits.append(sub.key())
            if not crits:
                raise SearchSyntaxError("empty search group")
            return Crit("AND", tuple(crits))
        if not isinstance(tok, Atom):
            raise SearchSyntaxError("search key must be an atom")
        name = tok.value.upper()
        if name == "ALL":
            return Crit("ALL")
        if name in _FLAG_KEYS:
            flag, wanted = _FLAG_KEYS[name]
            return Crit("FLAG", (flag, wanted))
        if name == "NEW":
            return Crit("NONE")  # no \Recent support: nothing is new
        if name in ("OLD",):
            return Crit("ALL")
        if name == "RECENT":
            return Crit("NONE")
        if name in ("KEYWORD", "UNKEYWORD"):
            return Crit("KEYWORD", (_text(self.next()), name == "KEYWORD"))
        if name == "UID":
            return Crit("UIDSET", (_seqset(self.next()),))
        if name == "NOT":
            return Crit("NOT", (self.key(),))
        if name == "OR":
            return Crit("OR", (self.key(), self.key()))
        if name in _DATE_KEYS:
            return Crit("DATE", (name, _date(self.next())))
        if name in ("LARGER", "SMALLER"):
            n_tok = self.next()
            if not isinstance(n_tok, Atom) or not n_tok.value.isdigit():
                raise SearchSyntaxError(f"{name} requires a number")
            return Crit("SIZE", (name, int(n_tok.value)))
        if name in _ITEM_TEXT_KEYS or name in _RAW_TEXT_KEYS:
            return Crit("TEXT", (name, _needle(self.next())))
        if name == "HEADER":
            field = _text(self.next())
            return Crit("HEADER", (field, _needle(self.next())))
        if _looks_like_seqset(name):
            return Crit("SEQSET", (_seqset(tok),))
        raise SearchSyntaxError("unsupported search key")


def _text(tok: Token) -> str:
    if isinstance(tok, ListTok):
        raise SearchSyntaxError("expected a string")
    try:
        return astring_bytes(tok).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadCharset("UTF-8") from exc


def _needle(tok: Token) -> str:
    return _text(tok).casefold()


def _seqset(tok: Token) -> SequenceSet:
    if not isinstance(tok, Atom):
        raise SearchSyntaxError("expected a sequence set")
    try:
        return parse_sequence_set(tok.value)
    except FetchSyntaxError as exc:
        raise SearchSyntaxError(str(exc)) from exc


def _looks_like_seqset(text: str) -> bool:
    return bool(re.fullmatch(r"[\d*:,]+", text)) and text[0] in "0123456789*"


def _date(tok: Token) -> date:
    text = _text(tok)
    m = _DATE_RE.match(text)
    if not m or m.group(2).lower() not in _MONTHS:
        raise SearchSyntaxError("invalid date")
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError as exc:
        raise SearchSyntaxError("invalid date") from exc


# ----- evaluation -----------------------------------------------------------------------------


@dataclass
class SearchContext:
    seqs: list[int]  # ascending sequence numbers
    uids: list[int]  # ascending UIDs, parallel to seqs
    deleted: set[int]
    raw: Callable[[Item], Awaitable[bytes]]
    size: Callable[[Item], Awaitable[int]]

    def flags(self, item: Item) -> frozenset[str]:
        return item.flags | ({FLAG_DELETED} if item.uid in self.deleted else frozenset())


async def evaluate(crit: Crit, item: Item, seq: int, ctx: SearchContext) -> bool:
    op = crit.op
    if op == "ALL":
        return True
    if op == "NONE":
        return False
    if op == "AND":
        for c in crit.args:
            if not await evaluate(c, item, seq, ctx):
                return False
        return True
    if op == "OR":
        return await evaluate(crit.args[0], item, seq, ctx) or await evaluate(
            crit.args[1], item, seq, ctx
        )
    if op == "NOT":
        return not await evaluate(crit.args[0], item, seq, ctx)
    if op == "FLAG":
        flag, wanted = crit.args
        return (flag in ctx.flags(item)) == wanted
    if op == "KEYWORD":
        label, wanted = crit.args
        return (label in item.labels) == wanted
    if op == "UIDSET":
        return item.uid in select_numbers(crit.args[0], ctx.uids)
    if op == "SEQSET":
        return seq in select_numbers(crit.args[0], ctx.seqs)
    if op == "SIZE":
        name, n = crit.args
        size = await ctx.size(item)
        return size > n if name == "LARGER" else size < n
    if op == "DATE":
        name, d = crit.args
        if name.startswith("SENT"):
            when = _sent_date(await ctx.raw(item))
            if when is None:
                return False
            name = name[4:]
        else:
            when = item.internaldate.date()
        if name == "BEFORE":
            return when < d
        if name == "ON":
            return when == d
        return when >= d
    if op == "TEXT":
        name, needle = crit.args
        if name == "FROM":
            return needle in item.from_.casefold()
        if name == "TO":
            return needle in ", ".join(item.to).casefold()
        if name == "SUBJECT":
            return needle in item.subject.casefold()
        raw = await ctx.raw(item)
        if name == "TEXT":
            return needle in (_decoded_headers(raw) + "\n" + _decoded_body(raw)).casefold()
        if name == "BODY":
            return needle in _decoded_body(raw).casefold()
        return needle in _header_value(raw, name).casefold()
    if op == "HEADER":
        field, needle = crit.args
        value = _header_value(await ctx.raw(item), field)
        return (
            needle in value.casefold()
            if needle
            else field.lower() in _header_names(await ctx.raw(item))
        )
    raise SearchSyntaxError(f"cannot evaluate {op}")


def _decoded_headers(raw: bytes) -> str:
    """All header fields with RFC 2047 encoded-words decoded, as ``Name: value`` lines."""
    header, _ = split_message(raw)
    msg = message_from_bytes(header, policy=policy.default)
    return "\n".join(f"{k}: {v}" for k, v in msg.items())


def _decoded_body(raw: bytes) -> str:
    """Text of every text/* part with transfer encodings and charsets decoded (RFC 3501 6.4.4
    requires SEARCH to match decoded content, not the raw transfer encoding)."""
    msg = message_from_bytes(raw, policy=policy.default)
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - undecodable part: fall back to a lenient decode
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks)


def _header_value(raw: bytes, field: str) -> str:
    header, _ = split_message(raw)
    msg = message_from_bytes(header, policy=policy.default)
    values = msg.get_all(field, [])
    return ", ".join(str(v) for v in values)


def _header_names(raw: bytes) -> set[str]:
    header, _ = split_message(raw)
    msg = message_from_bytes(header, policy=policy.default)
    return {k.lower() for k in msg.keys()}


def _sent_date(raw: bytes) -> date | None:
    value = _header_value(raw, "Date")
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if isinstance(dt, datetime):
        return dt.date()
    return None
