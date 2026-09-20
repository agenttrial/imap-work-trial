"""Encoders for IMAP server responses. Every function returns ``bytes`` ending in CRLF where
it represents a full line. Message content is only ever accepted as ``bytes`` so that literal
lengths are octet counts by construction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

CRLF = b"\r\n"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def clean(text: str) -> str:
    """Make text safe for a response line: no CR, LF, or other control characters, so client
    supplied strings can never inject an extra protocol line."""
    return "".join(ch if 0x20 <= ord(ch) != 0x7F else " " for ch in text)


def _clean_code(code: str | None) -> str | None:
    if code is None:
        return None
    return clean(code).replace("[", "(").replace("]", ")")


def tagged(tag: str, status: str, text: str, code: str | None = None) -> bytes:
    prefix = f"{clean(tag)} {status} "
    code = _clean_code(code)
    if code:
        prefix += f"[{code}] "
    return (prefix + clean(text)).encode("utf-8") + CRLF


def untagged(text: str) -> bytes:
    return b"* " + clean(text).encode("utf-8") + CRLF


def untagged_ok(text: str, code: str | None = None) -> bytes:
    code = _clean_code(code)
    if code:
        return f"* OK [{code}] {clean(text)}".encode() + CRLF
    return f"* OK {clean(text)}".encode() + CRLF


def bye(text: str) -> bytes:
    return f"* BYE {clean(text)}".encode() + CRLF


def continuation(text: str) -> bytes:
    return f"+ {clean(text)}".encode() + CRLF


def literal(data: bytes) -> bytes:
    """``{N}`` CRLF followed by exactly N octets. Rejects str to keep lengths honest."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("literal data must be bytes")
    data = bytes(data)
    return b"{" + str(len(data)).encode("ascii") + b"}" + CRLF + data


def quoted(value: str | bytes) -> bytes:
    """A quoted string. Falls back to a literal when the value cannot be quoted."""
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if any(b in raw for b in (0x0D, 0x0A, 0x00)) or any(b >= 0x80 for b in raw):
        return literal(raw)
    escaped = raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + escaped + b'"'


def nstring(value: str | bytes | None) -> bytes:
    return b"NIL" if value is None else quoted(value)


def atom_or_quoted(value: str) -> bytes:
    """Mailbox names: plain atom when safe, quoted otherwise. INBOX is always bare."""
    if value and all(ch.isalnum() or ch in "-_." for ch in value):
        return value.encode("ascii")
    return quoted(value)


def flag_list(flags: Iterable[str]) -> bytes:
    return b"(" + " ".join(sorted(flags, key=_flag_sort_key)).encode("ascii") + b")"


def _flag_sort_key(flag: str) -> tuple[int, str]:
    order = {
        "\\Seen": 0,
        "\\Answered": 1,
        "\\Flagged": 2,
        "\\Deleted": 3,
        "\\Draft": 4,
        "\\Recent": 5,
    }
    return (order.get(flag, 10), flag)


def internaldate(dt: datetime) -> bytes:
    """``"17-Aug-2026 16:00:00 +0000"`` per RFC 3501 date-time. Naive datetimes are UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    offset = dt.utcoffset() or datetime.now(UTC).utcoffset()
    total = int(offset.total_seconds()) if offset else 0
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    zone = f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"
    text = f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year:04d} {dt:%H:%M:%S} {zone}"
    return b'"' + text.encode("ascii") + b'"'


def list_response(attributes: Iterable[str], delimiter: str | None, name: str) -> bytes:
    attrs = b"(" + " ".join(attributes).encode("ascii") + b")"
    delim = b"NIL" if delimiter is None else quoted(delimiter)
    return b"* LIST " + attrs + b" " + delim + b" " + atom_or_quoted(name) + CRLF


def fetch_response(seq: int, items: Iterable[bytes]) -> bytes:
    """``* <seq> FETCH (<items>)``. Items are pre-encoded ``NAME value`` byte strings; a value
    may itself be a literal, in which case the closing paren follows the literal bytes."""
    return b"* " + str(seq).encode("ascii") + b" FETCH (" + b" ".join(items) + b")" + CRLF


def search_response(numbers: Iterable[int]) -> bytes:
    nums = " ".join(str(n) for n in numbers)
    return (b"* SEARCH " + nums.encode("ascii")).rstrip() + CRLF


def exists(count: int) -> bytes:
    return f"* {count} EXISTS".encode() + CRLF


def recent(count: int) -> bytes:
    return f"* {count} RECENT".encode() + CRLF


def expunge(seq: int) -> bytes:
    return f"* {seq} EXPUNGE".encode() + CRLF


def capability_line(capabilities: Iterable[str]) -> bytes:
    return b"* CAPABILITY " + " ".join(capabilities).encode("ascii") + CRLF
