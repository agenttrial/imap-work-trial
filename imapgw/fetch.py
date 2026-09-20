"""FETCH attribute grammar, sequence sets, and section slicing over raw message bytes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from imapgw.parser import Atom, ListTok, QuotedStr, Token


class FetchSyntaxError(ValueError):
    pass


class UnsupportedFetch(ValueError):
    pass


# ----- sequence sets ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequenceSet:
    ranges: tuple[tuple[int, int], ...]

    def contains(self, n: int) -> bool:
        return any(lo <= n <= hi for lo, hi in self.ranges)

    @property
    def has_star(self) -> bool:
        return any(hi == _STAR for _, hi in self.ranges)


_STAR = 2**32  # sentinel larger than any UID or sequence number


def parse_sequence_set(text: str) -> SequenceSet:
    """Parse ``1``, ``1:3``, ``1,3,5:*``. ``*`` becomes an open upper bound; callers resolve it
    against the current mailbox (see :func:`select_numbers`)."""
    if not text:
        raise FetchSyntaxError("empty sequence set")
    ranges: list[tuple[int, int]] = []
    for part in text.split(","):
        if not part:
            raise FetchSyntaxError("empty item in sequence set")
        lo_s, colon, hi_s = part.partition(":")
        if colon and not hi_s:
            raise FetchSyntaxError("invalid range in sequence set")
        lo = _seq_number(lo_s)
        hi = _seq_number(hi_s) if hi_s else lo
        if lo > hi:
            lo, hi = hi, lo
        ranges.append((lo, hi))
    return SequenceSet(tuple(ranges))


def _seq_number(text: str) -> int:
    if text == "*":
        return _STAR
    if not text.isdigit():
        raise FetchSyntaxError("invalid sequence number")
    value = int(text)
    if value == 0:
        raise FetchSyntaxError("sequence numbers start at 1")
    return value


def select_numbers(seq: SequenceSet, candidates: list[int]) -> list[int]:
    """Return the candidates (ascending) that fall in the set. ``*`` denotes the largest
    candidate, so ``559:*`` always includes the last one when the mailbox is non-empty."""
    if not candidates:
        return []
    largest = candidates[-1]
    out: list[int] = []
    for n in candidates:
        for lo, hi in seq.ranges:
            lo_r = largest if lo == _STAR else lo
            hi_r = largest if hi == _STAR else hi
            if lo_r > hi_r:
                lo_r, hi_r = hi_r, lo_r
            if lo_r <= n <= hi_r:
                out.append(n)
                break
    return out


# ----- fetch attributes ------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchAttr:
    kind: str  # UID FLAGS INTERNALDATE RFC822.SIZE BODY
    label: str  # exact text to echo in the response, e.g. "BODY[HEADER.FIELDS (FROM TO)]"
    peek: bool = True
    section: str = ""  # "", "HEADER", "TEXT", "HEADER.FIELDS", "HEADER.FIELDS.NOT"
    fields: tuple[str, ...] = ()
    partial: tuple[int, int] | None = None  # (offset, count)

    @property
    def needs_body(self) -> bool:
        return self.kind == "BODY"


_MACROS = {
    "FAST": ("FLAGS", "INTERNALDATE", "RFC822.SIZE"),
}
_UNSUPPORTED_MACROS = {"ALL", "FULL"}
_SIMPLE = {"UID", "FLAGS", "INTERNALDATE", "RFC822.SIZE"}
_UNSUPPORTED_ITEMS = {"ENVELOPE", "BODYSTRUCTURE", "BODY"}
_BODY_RE = re.compile(r"^(BODY(?:\.PEEK)?)\[(.*)\](?:<(\d+)\.(\d+)>)?$", re.IGNORECASE | re.DOTALL)
_FIELDS_RE = re.compile(r"^(HEADER\.FIELDS(?:\.NOT)?)\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)


def parse_fetch_attributes(token: Token) -> list[FetchAttr]:
    if isinstance(token, ListTok):
        names = []
        for item in token.items:
            if not isinstance(item, Atom):
                raise FetchSyntaxError("fetch attributes must be atoms")
            names.append(item.value)
        if not names:
            raise FetchSyntaxError("empty fetch attribute list")
    elif isinstance(token, Atom):
        upper = token.value.upper()
        if upper in _MACROS:
            names = list(_MACROS[upper])
        elif upper in _UNSUPPORTED_MACROS:
            raise UnsupportedFetch(f"unsupported fetch macro {upper} (ENVELOPE is not implemented)")
        else:
            names = [token.value]
    else:
        raise FetchSyntaxError("invalid fetch attributes")
    attrs = [_parse_attr(name) for name in names]
    by_label: dict[str, FetchAttr] = {}
    order: list[str] = []
    for a in attrs:
        existing = by_label.get(a.label)
        if existing is None:
            by_label[a.label] = a
            order.append(a.label)
        elif existing.peek and not a.peek:
            # BODY[] and BODY.PEEK[] share a label; the non-PEEK side effect must survive.
            by_label[a.label] = FetchAttr(
                existing.kind,
                existing.label,
                False,
                existing.section,
                existing.fields,
                existing.partial,
            )
    return [by_label[label] for label in order]


def _parse_attr(name: str) -> FetchAttr:
    upper = name.upper()
    if upper in _SIMPLE:
        return FetchAttr(upper, upper)
    if upper == "RFC822":
        return FetchAttr("BODY", "RFC822", peek=False)
    if upper == "RFC822.HEADER":
        return FetchAttr("BODY", "RFC822.HEADER", peek=True, section="HEADER")
    if upper == "RFC822.TEXT":
        return FetchAttr("BODY", "RFC822.TEXT", peek=False, section="TEXT")
    if upper in _UNSUPPORTED_ITEMS:
        raise UnsupportedFetch(f"unsupported fetch item {upper}")
    m = _BODY_RE.match(name)
    if not m:
        raise FetchSyntaxError("invalid fetch item")
    peek = m.group(1).upper() == "BODY.PEEK"
    section_text = m.group(2).strip()
    partial = (int(m.group(3)), int(m.group(4))) if m.group(3) is not None else None
    section, fields = _parse_section(section_text)
    if section in ("HEADER.FIELDS", "HEADER.FIELDS.NOT"):
        label_section = f"{section} ({' '.join(fields)})"
    else:
        label_section = section
    label = f"BODY[{label_section}]"
    if partial is not None:
        label += f"<{partial[0]}>"
    return FetchAttr("BODY", label, peek=peek, section=section, fields=fields, partial=partial)


def _parse_section(text: str) -> tuple[str, tuple[str, ...]]:
    if text == "":
        return "", ()
    upper = text.upper()
    if upper in ("HEADER", "TEXT"):
        return upper, ()
    m = _FIELDS_RE.match(text)
    if m:
        names = _split_field_names(m.group(2))
        if not names:
            raise FetchSyntaxError("HEADER.FIELDS requires at least one field name")
        return m.group(1).upper(), tuple(n.upper() for n in names)
    if upper == "MIME" or re.match(r"^\d", upper):
        raise UnsupportedFetch("unsupported body section (part numbers are not implemented)")
    raise FetchSyntaxError("invalid body section")


def _split_field_names(text: str) -> list[str]:
    names: list[str] = []
    for quoted_, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', text):
        names.append(quoted_.replace('\\"', '"').replace("\\\\", "\\") if quoted_ else bare)
    return names


def unsupported_tokens(items: tuple[Token, ...]) -> None:
    for item in items:
        if isinstance(item, QuotedStr):
            raise FetchSyntaxError("fetch attributes must be atoms")


# ----- section slicing ------------------------------------------------------------------------


def split_message(raw: bytes) -> tuple[bytes, bytes]:
    """Return (header including the blank separator line, body)."""
    idx = raw.find(b"\r\n\r\n")
    if idx >= 0:
        return raw[: idx + 4], raw[idx + 4 :]
    idx = raw.find(b"\n\n")  # tolerate LF-only input
    if idx >= 0:
        return raw[: idx + 2], raw[idx + 2 :]
    return raw, b""


def header_lines(header: bytes) -> list[bytes]:
    """Split a header block into logical (folded) header lines, preserving each physical line's
    original bytes and terminator (CRLF or bare LF)."""
    lines: list[bytes] = []
    for physical in header.splitlines(keepends=True):
        if physical in (b"\r\n", b"\n"):
            break  # blank separator line: end of header
        if physical[:1] in (b" ", b"\t") and lines:
            lines[-1] += physical  # folded continuation
        else:
            lines.append(physical)
    return lines


def filter_header(header: bytes, names: tuple[str, ...], *, invert: bool) -> bytes:
    """Return the selected header fields followed by the blank separator line, as RFC 3501
    6.4.5 requires for HEADER.FIELDS and HEADER.FIELDS.NOT."""
    wanted = {n.upper().encode("ascii") for n in names}
    out = bytearray()
    for line in header_lines(header):
        field = line.split(b":", 1)[0].strip().upper()
        keep = (field in wanted) != invert
        if keep:
            out += line
            if not line.endswith(b"\n"):
                out += b"\r\n"
    out += b"\r\n"
    return bytes(out)


def section_bytes(raw: bytes, attr: FetchAttr) -> bytes:
    if attr.section == "":
        data = raw
    elif attr.section == "HEADER":
        data, _ = split_message(raw)
    elif attr.section == "TEXT":
        _, data = split_message(raw)
    elif attr.section == "HEADER.FIELDS":
        header, _ = split_message(raw)
        data = filter_header(header, attr.fields, invert=False)
    elif attr.section == "HEADER.FIELDS.NOT":
        header, _ = split_message(raw)
        data = filter_header(header, attr.fields, invert=True)
    else:  # pragma: no cover - guarded by _parse_section
        raise UnsupportedFetch(attr.section)
    if attr.partial is not None:
        offset, count = attr.partial
        data = data[offset : offset + count]
    return data
