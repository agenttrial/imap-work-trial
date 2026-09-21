"""Projections between AgentMail draft JSON and RFC 822 bytes.

Uses the standard library ``email`` package (message format, RFC 5322/2045/2047). The IMAP
protocol itself is implemented elsewhere in this package by hand.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime
from html.parser import HTMLParser

DRAFT_ID_HEADER = "X-AgentMail-Draft-Id"

_RENDER_POLICY = policy.SMTP.clone(utf8=False, max_line_length=None, cte_type="8bit")
_PARSE_POLICY = policy.default


class DraftParseError(ValueError):
    """The APPEND literal cannot be represented as a plain-text AgentMail draft."""


@dataclass(frozen=True)
class DraftFields:
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: tuple[str, ...] = ()
    subject: str = ""
    text: str = ""


def parse_iso8601(value: object) -> datetime:
    """Parse the API's ISO 8601 timestamps (``2026-08-17T16:00:00.000Z``) to aware UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0, UTC)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _as_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence):
        return tuple(str(v) for v in value if str(v))
    return ()


def render_draft(draft: Mapping[str, object], inbox_id: str) -> bytes:
    """Deterministically render a draft as an RFC 822 message (CRLF, UTF-8, 8bit body)."""
    draft_id = str(draft.get("draft_id") or "")
    msg = EmailMessage(policy=_RENDER_POLICY)
    msg["From"] = inbox_id
    for header, key in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Reply-To", "reply_to")):
        values = _as_list(draft.get(key))
        if values:
            msg[header] = ", ".join(values)
    subject = draft.get("subject")
    if subject:
        msg["Subject"] = str(subject)
    # Date comes from created_at (falling back to updated_at) so that label or timestamp-only
    # updates do not change the rendered bytes and therefore the draft's UID.
    msg["Date"] = format_datetime(parse_iso8601(draft.get("created_at") or draft.get("updated_at")))
    domain = inbox_id.rsplit("@", 1)[-1] if "@" in inbox_id else "imapgw.local"
    msg["Message-ID"] = f"<{draft_id or 'draft'}@{domain}>"
    if draft_id:
        msg[DRAFT_ID_HEADER] = draft_id
    text = str(draft.get("text") or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    msg.set_content(text, subtype="plain", charset="utf-8", cte="8bit")
    return msg.as_bytes()


def content_key(draft_id: str, rendered: bytes) -> str:
    return f"{draft_id}#{hashlib.sha256(rendered).hexdigest()[:16]}"


def client_id_for(raw: bytes) -> str:
    return "imapgw-" + hashlib.sha256(raw).hexdigest()[:32]


def parse_append(raw: bytes) -> DraftFields:
    """Extract draft fields from an RFC 822 message. Plain-text only; raises DraftParseError."""
    try:
        msg = message_from_bytes(raw, policy=_PARSE_POLICY)
    except Exception as exc:  # the parser is lenient; this is belt and braces
        raise DraftParseError(f"message could not be parsed: {exc}") from exc

    def addresses(name: str) -> tuple[str, ...]:
        out: list[str] = []
        for header in msg.get_all(name, []):
            addrs = getattr(header, "addresses", None)
            if addrs:
                out.extend(str(a) for a in addrs)
            else:
                value = str(header).strip()
                if value:
                    out.extend(part.strip() for part in value.split(",") if part.strip())
        return tuple(out)

    subject = str(msg.get("Subject", "") or "").strip()

    if msg.is_multipart():
        for _ in msg.iter_attachments():
            raise DraftParseError("attachments are not supported in new drafts")
    body = msg.get_body(preferencelist=("plain",))
    html_body = None
    if body is None:
        # Desktop clients (Thunderbird, Apple Mail) save drafts as text/html with no plain
        # alternative. AgentMail drafts are plain text here, so the HTML is flattened.
        html_body = msg.get_body(preferencelist=("html",))
        if html_body is None:
            raise DraftParseError("no text/plain or text/html part in the message")
        body = html_body
    if body.get_content_disposition() == "attachment":
        raise DraftParseError("attachments are not supported in new drafts")
    text = _decode_text_part(body).replace("\r\n", "\n")
    if html_body is not None:
        text = html_to_text(text)

    return DraftFields(
        to=addresses("To"),
        cc=addresses("Cc"),
        bcc=addresses("Bcc"),
        reply_to=addresses("Reply-To"),
        subject=subject,
        text=text,
    )


def _decode_text_part(part) -> str:
    """Decode a text/plain part strictly. The ``email`` package's ``get_content`` substitutes
    U+FFFD for undecodable bytes, which would silently alter the saved draft; we refuse instead."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if not isinstance(payload, bytes):
        raise DraftParseError("text body is not text")
    charset = (part.get_content_charset() or "us-ascii").lower()
    if charset in ("us-ascii", "ascii") and any(b >= 0x80 for b in payload):
        # Undeclared 8-bit text: accept it only if it is valid UTF-8.
        charset = "utf-8"
    try:
        return payload.decode(charset, errors="strict")
    except LookupError as exc:
        raise DraftParseError("unsupported text charset") from exc
    except UnicodeDecodeError as exc:
        raise DraftParseError("text body is not valid in its declared charset") from exc


_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dd",
        "dt",
        "fieldset",
        "footer",
        "form",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)
_PARAGRAPH_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6"})
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "template"})


class _HtmlToText(HTMLParser):
    """Flatten HTML to readable plain text. Block elements become line breaks, paragraphs
    and headings blank lines, ``<br>`` a newline, list items a leading dash, links keep their
    target in angle brackets when it differs from the link text. Runs of whitespace collapse
    to one space, as a browser would, except inside ``<pre>``. Scripts, styles and the
    document head are dropped. Character references are decoded by the parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0
        self._pre = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._out.append("\n")
        elif tag == "pre":
            self._pre += 1
            self._out.append("\n")
        elif tag in _PARAGRAPH_TAGS:
            self._out.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")
            if tag == "li":
                self._out.append("- ")
        elif tag == "a":
            href = dict(attrs).get("href")
            self._href = href.strip() if href else None
            self._link_text = []
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._out.append(alt)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img", "hr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._out.append("\n")
        elif tag in _PARAGRAPH_TAGS:
            self._out.append("\n\n")
        elif tag in _BLOCK_TAGS and tag != "li":  # the next item or the list end breaks the line
            self._out.append("\n")
        elif tag == "a":
            text = "".join(self._link_text).strip()
            href = self._href
            self._href = None
            if href and not href.lower().startswith(("javascript:", "cid:")):
                target = href[7:] if href.lower().startswith("mailto:") else href
                if target and target != text:
                    self._out.append(f" <{target}>")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if not self._pre:
            data = re.sub(r"\s+", " ", data.replace("\xa0", " "))
            at_line_start = not self._out or self._out[-1].endswith(("\n", "- "))
            if at_line_start:
                data = data.lstrip(" ")  # indentation between tags, not content
            if not data:
                return
        self._out.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self._out).split("\n")]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
        return text + "\n" if text else ""


def html_to_text(html: str) -> str:
    """Plain-text rendering of an HTML draft body. Formatting is lost by design: AgentMail
    drafts created here are text-only (the sandbox rejects ``html``)."""
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return parser.text()


def api_body(fields: DraftFields, client_id: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {"subject": fields.subject, "text": fields.text}
    for key, values in (
        ("to", fields.to),
        ("cc", fields.cc),
        ("bcc", fields.bcc),
        ("reply_to", fields.reply_to),
    ):
        if values:
            body[key] = list(values)
    if client_id:
        body["client_id"] = client_id
    return body
