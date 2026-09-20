"""IMAP command handlers. Each handler receives the session and the parsed command and writes
responses through the session; it raises :class:`CommandFailed` for NO/BAD outcomes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from imapgw import responses as r
from imapgw.parser import Command, ListTok, Token, TokenizeError, astring_text
from imapgw.session import CAPABILITIES, Session, State, bad, no

log = logging.getLogger("imapgw.commands")

Handler = Callable[[Session, Command], Awaitable[None]]


def _arg_text(cmd: Command, index: int, what: str) -> str:
    if index >= len(cmd.args):
        raise bad(f"{cmd.name} requires {what}")
    token: Token = cmd.args[index]
    if isinstance(token, ListTok):
        raise bad(f"{what} must be a string")
    try:
        return astring_text(token)
    except (TokenizeError, UnicodeDecodeError) as exc:
        raise bad(f"invalid {what}") from exc


# ----- any state --------------------------------------------------------------------------


async def capability(session: Session, cmd: Command) -> None:
    if cmd.args:
        raise bad("CAPABILITY takes no arguments")
    session.write(r.capability_line(CAPABILITIES))
    session.write(r.tagged(cmd.tag, "OK", "CAPABILITY completed"))


async def noop(session: Session, cmd: Command) -> None:
    if cmd.args:
        raise bad("NOOP takes no arguments")
    if session.state is State.SELECTED and session.api and session.inbox_id and session.selected:
        await session.mailboxes.sync(
            session.api, session.inbox_id, session.selected.name, force=True
        )
        session.flush_pending()
    session.write(r.tagged(cmd.tag, "OK", "NOOP completed"))


async def logout(session: Session, cmd: Command) -> None:
    session.write(r.bye("logging out"))
    session.write(r.tagged(cmd.tag, "OK", "LOGOUT completed"))
    session.state = State.LOGOUT


# ----- not authenticated ---------------------------------------------------------------------


async def login(session: Session, cmd: Command) -> None:
    if session.state is not State.NOT_AUTHENTICATED:
        raise bad("already authenticated")
    if len(cmd.args) != 2:
        raise bad("LOGIN requires a user id and a password")
    userid = _arg_text(cmd, 0, "user id")
    password = _arg_text(cmd, 1, "password")
    # Credentials must be printable ASCII without spaces: anything else can never be a valid
    # inbox id or API key, and control characters must not reach an HTTP header.
    if not _printable(userid) or not _printable(password):
        raise no("invalid credentials", "AUTHENTICATIONFAILED")
    allowed = session.settings.allowed_inbox_id
    if allowed and userid != allowed:
        log.info("[%s] LOGIN refused: user id not allowed", session.conn_id)
        raise no("invalid credentials", "AUTHENTICATIONFAILED")

    api = session.set_credentials(userid, password)
    # On failure the session clears the credentials after dispatch has logged the outcome, so
    # the redaction filter still knows the secret while any error is being recorded.
    identity = await api.auth_me()
    scope_type = str(identity.get("scope_type", ""))
    scoped_inbox = identity.get("inbox_id")
    if scope_type == "inbox" and scoped_inbox and scoped_inbox != userid:
        log.info("[%s] LOGIN refused: key is scoped to a different inbox", session.conn_id)
        raise no("invalid credentials", "AUTHENTICATIONFAILED")
    if scope_type != "inbox" or not scoped_inbox:
        await api.get_inbox(userid)
    session.state = State.AUTHENTICATED
    log.info("[%s] LOGIN ok for inbox %s", session.conn_id, userid)
    session.write(
        r.tagged(cmd.tag, "OK", "LOGIN completed", code=f"CAPABILITY {' '.join(CAPABILITIES)}")
    )


def _printable(text: str) -> bool:
    return bool(text) and all(0x21 <= ord(ch) <= 0x7E for ch in text)


# ----- rejections -------------------------------------------------------------------------


def _unsupported(text: str) -> Handler:
    async def handler(session: Session, cmd: Command) -> None:
        raise no(text, "CANNOT")

    return handler


HANDLERS: dict[str, Handler] = {
    "CAPABILITY": capability,
    "NOOP": noop,
    "LOGOUT": logout,
    "LOGIN": login,
    "STARTTLS": _unsupported("TLS is not available on this server"),
    "AUTHENTICATE": _unsupported("only LOGIN is supported"),
}


def register(name: str, handler: Handler) -> None:
    HANDLERS[name] = handler


# ----- authenticated: LIST / SELECT / EXAMINE / CLOSE -----------------------------------------

import re as _re  # noqa: E402

from imapgw.apiclient import NotFoundError  # noqa: E402
from imapgw.fetch import (  # noqa: E402
    FetchAttr,
    FetchSyntaxError,
    UnsupportedFetch,
    parse_fetch_attributes,
    parse_sequence_set,
    section_bytes,
    select_numbers,
)
from imapgw.mailbox import ALL_FLAGS, FLAG_DELETED, FLAG_SEEN  # noqa: E402
from imapgw.parser import Atom  # noqa: E402

_MAX_LIST_PATTERN = 512


def _wildcard_match(pattern: str, name: str, *, ignore_case: bool = False) -> bool:
    """Match a LIST pattern where ``*`` and ``%`` both mean "any run of characters" (the namespace
    is flat, so ``%`` cannot stop at a delimiter). Linear time: adjacent wildcards collapse and the
    matcher never backtracks more than one wildcard, unlike a naive ``.*`` regex."""
    if ignore_case:
        pattern, name = pattern.lower(), name.lower()
    collapsed = []
    for ch in pattern:
        if ch in "*%":
            if collapsed and collapsed[-1] == "*":
                continue
            collapsed.append("*")
        else:
            collapsed.append(ch)
    p = "".join(collapsed)
    pi = ni = 0
    star = -1
    match_ni = 0
    while ni < len(name):
        if pi < len(p) and p[pi] == "*":
            star, match_ni = pi, ni
            pi += 1
        elif pi < len(p) and p[pi] == name[ni]:
            pi += 1
            ni += 1
        elif star >= 0:
            pi = star + 1
            match_ni += 1
            ni = match_ni
        else:
            return False
    while pi < len(p) and p[pi] == "*":
        pi += 1
    return pi == len(p)


async def list_(session: Session, cmd: Command) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) != 2:
        raise bad("LIST requires a reference and a mailbox pattern")
    reference = _arg_text(cmd, 0, "reference")
    pattern = _arg_text(cmd, 1, "mailbox pattern")
    if pattern == "":
        session.write(r.list_response(["\\Noselect"], None, ""))
        session.write(r.tagged(cmd.tag, "OK", "LIST completed"))
        return
    full = reference + pattern
    if len(full) > _MAX_LIST_PATTERN:
        raise bad("mailbox pattern too long")
    for name in session.mailboxes.names():
        if _wildcard_match(full, name, ignore_case=(name == "INBOX")):
            session.write(r.list_response(["\\HasNoChildren"], None, name))
    session.write(r.tagged(cmd.tag, "OK", "LIST completed"))


async def lsub(session: Session, cmd: Command) -> None:
    """Subscriptions are not modelled; every mailbox counts as subscribed."""
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) != 2:
        raise bad("LSUB requires a reference and a mailbox pattern")
    reference = _arg_text(cmd, 0, "reference")
    pattern = _arg_text(cmd, 1, "mailbox pattern")
    full = reference + pattern
    if len(full) > _MAX_LIST_PATTERN:
        raise bad("mailbox pattern too long")
    for name in session.mailboxes.names():
        if pattern and _wildcard_match(full, name, ignore_case=(name == "INBOX")):
            session.write(
                r.list_response(["\\HasNoChildren"], None, name).replace(b"* LIST", b"* LSUB", 1)
            )
    session.write(r.tagged(cmd.tag, "OK", "LSUB completed"))


async def _select(session: Session, cmd: Command, *, read_only: bool) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) != 1:
        raise bad(f"{cmd.name} requires a mailbox name")
    name = _arg_text(cmd, 0, "mailbox name")
    # RFC 3501 6.3.1: a SELECT deselects the current mailbox even if it then fails.
    session.selected = None
    session.state = State.AUTHENTICATED
    mdef = session.mailboxes.lookup(name)
    if mdef is None:
        raise no("mailbox does not exist")
    assert session.api is not None and session.inbox_id is not None
    snapshot = await session.mailboxes.sync(session.api, session.inbox_id, mdef.name, force=True)
    sel = session.apply_snapshot(snapshot, read_only)

    session.write(r.untagged("FLAGS " + r.flag_list(ALL_FLAGS).decode("ascii")))
    session.write(r.exists(len(sel.uids)))
    session.write(r.recent(0))
    first_unseen = next(
        (
            seq
            for seq, uid in enumerate(sel.uids, start=1)
            if FLAG_SEEN not in snapshot.by_uid[uid].flags
        ),
        None,
    )
    if first_unseen is not None:
        session.write(
            r.untagged_ok(f"Message {first_unseen} is first unseen", code=f"UNSEEN {first_unseen}")
        )
    permanent = () if read_only else mdef.permanent_flags
    session.write(
        r.untagged_ok("Limited", code="PERMANENTFLAGS " + r.flag_list(permanent).decode("ascii"))
    )
    session.write(r.untagged_ok("UIDs valid", code=f"UIDVALIDITY {snapshot.uidvalidity}"))
    session.write(r.untagged_ok("Predicted next UID", code=f"UIDNEXT {snapshot.uidnext}"))
    session.write(
        r.tagged(
            cmd.tag, "OK", f"{cmd.name} completed", code="READ-ONLY" if read_only else "READ-WRITE"
        )
    )


async def select(session: Session, cmd: Command) -> None:
    await _select(session, cmd, read_only=False)


async def examine(session: Session, cmd: Command) -> None:
    await _select(session, cmd, read_only=True)


async def close(session: Session, cmd: Command) -> None:
    session.require_selected()
    session.selected = None
    session.state = State.AUTHENTICATED
    session.write(r.tagged(cmd.tag, "OK", "CLOSE completed"))


# ----- selected: FETCH / UID -----------------------------------------------------------------


async def fetch(session: Session, cmd: Command) -> None:
    await _fetch(session, cmd, cmd.args, uid_mode=False)


async def uid(session: Session, cmd: Command) -> None:
    session.require_selected()
    if not cmd.args or not isinstance(cmd.args[0], Atom):
        raise bad("UID requires a sub-command")
    sub = cmd.args[0].value.upper()
    handler = UID_HANDLERS.get(sub)
    if handler is None:
        raise bad("unsupported UID command")
    await handler(session, cmd, cmd.args[1:])


async def _uid_fetch(session: Session, cmd: Command, args: tuple) -> None:
    await _fetch(session, cmd, args, uid_mode=True)


async def _fetch(session: Session, cmd: Command, args: tuple, *, uid_mode: bool) -> None:
    sel = session.require_selected()
    assert session.api is not None and session.inbox_id is not None
    label = "UID FETCH" if uid_mode else "FETCH"
    if len(args) != 2:
        raise bad(f"{label} requires a sequence set and attributes")
    if not isinstance(args[0], Atom):
        raise bad("invalid sequence set")
    try:
        seqset = parse_sequence_set(args[0].value)
        attrs = parse_fetch_attributes(args[1])
    except UnsupportedFetch as exc:
        raise bad(str(exc)) from exc
    except FetchSyntaxError as exc:
        raise bad(str(exc)) from exc
    if uid_mode and not any(a.kind == "UID" for a in attrs):
        attrs.insert(0, FetchAttr("UID", "UID"))

    snapshot = session.mailboxes.latest(session.inbox_id, sel.name)
    if snapshot is None:
        snapshot = await session.mailboxes.sync(session.api, session.inbox_id, sel.name, force=True)

    if uid_mode:
        targets = [(sel.seq_of[u], u) for u in select_numbers(seqset, sel.uids)]
    else:
        seqs = select_numbers(seqset, list(range(1, len(sel.uids) + 1)))
        targets = [(s, sel.uids[s - 1]) for s in seqs]

    needs_body = any(a.needs_body for a in attrs)
    deleted = session.deleted_uids()
    sets_seen = any(a.needs_body and not a.peek for a in attrs) and not sel.read_only
    mdef = session.mailboxes.lookup(sel.name)
    can_set_seen = mdef is not None and FLAG_SEEN in mdef.permanent_flags
    for seq, uid_value in targets:
        item = snapshot.by_uid.get(uid_value)
        if item is None:
            continue  # vanished since the session snapshot; announced at the next safe point
        try:
            raw = (
                await session.mailboxes.raw_bytes(session.api, session.inbox_id, sel.name, item)
                if needs_body
                else None
            )
            report_flags = False
            if sets_seen and can_set_seen and FLAG_SEEN not in item.flags:
                item = await session.mailboxes.set_flag(
                    session.api, session.inbox_id, sel.name, item, FLAG_SEEN, True
                )
                report_flags = not any(a.kind == "FLAGS" for a in attrs)
            parts: list[bytes] = []
            for attr in attrs:
                if attr.kind == "UID":
                    parts.append(b"UID " + str(uid_value).encode("ascii"))
                elif attr.kind == "FLAGS":
                    flags = session.mailboxes.flags_of(item, deleted)
                    session.record_flags(uid_value, flags)
                    parts.append(b"FLAGS " + r.flag_list(flags))
                elif attr.kind == "INTERNALDATE":
                    parts.append(b"INTERNALDATE " + r.internaldate(item.internaldate))
                elif attr.kind == "RFC822.SIZE":
                    size = (
                        len(raw)
                        if raw is not None
                        else await session.mailboxes.size_of(
                            session.api, session.inbox_id, sel.name, item
                        )
                    )
                    parts.append(b"RFC822.SIZE " + str(size).encode("ascii"))
                elif attr.kind == "BODY":
                    assert raw is not None
                    parts.append(
                        attr.label.encode("ascii") + b" " + r.literal(section_bytes(raw, attr))
                    )
            if report_flags:
                flags = session.mailboxes.flags_of(item, deleted)
                session.record_flags(uid_value, flags)
                parts.append(b"FLAGS " + r.flag_list(flags))
        except NotFoundError:
            log.info("[%s] item uid %d vanished during FETCH", session.conn_id, uid_value)
            continue
        session.write(r.fetch_response(seq, parts))
        await session.flush()
    session.write(r.tagged(cmd.tag, "OK", f"{label} completed"))


UID_HANDLERS: dict = {"FETCH": _uid_fetch}

HANDLERS.update(
    {
        "LIST": list_,
        "LSUB": lsub,
        "SELECT": select,
        "EXAMINE": examine,
        "CLOSE": close,
        "FETCH": fetch,
        "UID": uid,
    }
)


# ----- APPEND ---------------------------------------------------------------------------------

from imapgw.drafts import DraftParseError, client_id_for, parse_append  # noqa: E402
from imapgw.parser import LiteralStr, QuotedStr  # noqa: E402


async def append(session: Session, cmd: Command) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) < 2:
        raise bad("APPEND requires a mailbox name and a message literal")
    name = _arg_text(cmd, 0, "mailbox name")
    message_tok = cmd.args[-1]
    if not isinstance(message_tok, LiteralStr | QuotedStr):
        raise bad("APPEND message must be a literal")
    optional = cmd.args[1:-1]
    if len(optional) > 2:
        raise bad("APPEND accepts at most a flag list and a date-time before the message")
    for tok in optional:
        if not isinstance(tok, ListTok | QuotedStr):
            raise bad("APPEND optional arguments must be a flag list or a quoted date-time")
    # Flags and date-time are accepted and ignored: every draft is \Draft, and AgentMail sets
    # the draft's timestamp itself.
    raw = message_tok.value

    mdef = session.mailboxes.lookup(name)
    if mdef is None:
        raise no("mailbox does not exist", "TRYCREATE")
    if not mdef.appendable:
        raise no("APPEND is only supported on Drafts", "CANNOT")
    try:
        fields = parse_append(raw)
    except DraftParseError as exc:
        raise no(str(exc), "CANNOT") from exc

    assert session.api is not None and session.inbox_id is not None
    uidvalidity, uid_value, resync = await session.mailboxes.append_draft(
        session.api, session.inbox_id, fields, client_id_for(raw)
    )
    code = f"APPENDUID {uidvalidity} {uid_value}"
    log.info("[%s] APPEND created draft uid %s in %s", session.conn_id, uid_value, mdef.name)
    session.write(r.tagged(cmd.tag, "OK", "APPEND completed", code=code))
    # The EXISTS for the new draft follows the tagged OK once the re-listing lands (bounded
    # wait, outside this command's deadline), or at the next safe point.
    session.defer(resync)


HANDLERS["APPEND"] = append


# ----- SEARCH / STORE / EXPUNGE / STATUS and small stubs ----------------------------------------

from imapgw import search as searchmod  # noqa: E402
from imapgw.mailbox import FLAG_LABELS  # noqa: E402


async def search(session: Session, cmd: Command) -> None:
    await _search(session, cmd, cmd.args, uid_mode=False)


async def _uid_search(session: Session, cmd: Command, args: tuple) -> None:
    await _search(session, cmd, args, uid_mode=True)


async def _search(session: Session, cmd: Command, args: tuple, *, uid_mode: bool) -> None:
    sel = session.require_selected()
    assert session.api is not None and session.inbox_id is not None
    label = "UID SEARCH" if uid_mode else "SEARCH"
    try:
        crit = searchmod.parse_search(args)
    except searchmod.BadCharset as exc:
        raise no("charset is not supported", "BADCHARSET (UTF-8)") from exc
    except searchmod.SearchSyntaxError as exc:
        raise bad(str(exc)) from exc
    snapshot = session.mailboxes.latest(session.inbox_id, sel.name)
    if snapshot is None:
        snapshot = await session.mailboxes.sync(session.api, session.inbox_id, sel.name, force=True)

    async def raw(item):
        return await session.mailboxes.raw_bytes(session.api, session.inbox_id, sel.name, item)

    async def size(item):
        return await session.mailboxes.size_of(session.api, session.inbox_id, sel.name, item)

    ctx = searchmod.SearchContext(
        seqs=list(range(1, len(sel.uids) + 1)),
        uids=list(sel.uids),
        deleted=session.deleted_uids(),
        raw=raw,
        size=size,
    )
    hits: list[int] = []
    for seq, uid_value in enumerate(sel.uids, start=1):
        item = snapshot.by_uid.get(uid_value)
        if item is None:
            continue
        try:
            if await searchmod.evaluate(crit, item, seq, ctx):
                hits.append(uid_value if uid_mode else seq)
        except NotFoundError:
            continue
    session.write(r.search_response(hits))
    session.write(r.tagged(cmd.tag, "OK", f"{label} completed"))


_STORE_RE = _re.compile(r"^([+-]?)FLAGS(\.SILENT)?$", _re.IGNORECASE)


async def store(session: Session, cmd: Command) -> None:
    await _store(session, cmd, cmd.args, uid_mode=False)


async def _uid_store(session: Session, cmd: Command, args: tuple) -> None:
    await _store(session, cmd, args, uid_mode=True)


async def _store(session: Session, cmd: Command, args: tuple, *, uid_mode: bool) -> None:
    sel = session.require_selected()
    assert session.api is not None and session.inbox_id is not None
    label = "UID STORE" if uid_mode else "STORE"
    if len(args) != 3 or not isinstance(args[0], Atom) or not isinstance(args[1], Atom):
        raise bad(f"{label} requires a sequence set, an action, and a flag list")
    m = _STORE_RE.match(args[1].value)
    if not m:
        raise bad("STORE action must be FLAGS, +FLAGS or -FLAGS, optionally .SILENT")
    mode, silent = m.group(1), bool(m.group(2))
    flags_tok = args[2]
    flag_tokens = flags_tok.items if isinstance(flags_tok, ListTok) else (flags_tok,)
    flags: list[str] = []
    for tok in flag_tokens:
        if not isinstance(tok, Atom):
            raise bad("flags must be atoms")
        flags.append(_canonical_flag(tok.value))
    try:
        seqset = parse_sequence_set(args[0].value)
    except FetchSyntaxError as exc:
        raise bad(str(exc)) from exc
    if sel.read_only:
        raise no("mailbox is read-only", "READ-ONLY")
    mdef = session.mailboxes.lookup(sel.name)
    assert mdef is not None
    for flag in flags:
        if flag not in mdef.permanent_flags:
            raise no(f"flag {flag} cannot be changed in {sel.name}", "CANNOT")

    snapshot = session.mailboxes.latest(session.inbox_id, sel.name)
    if snapshot is None:
        snapshot = await session.mailboxes.sync(session.api, session.inbox_id, sel.name, force=True)
    if uid_mode:
        targets = [(sel.seq_of[u], u) for u in select_numbers(seqset, sel.uids)]
    else:
        targets = [
            (s, sel.uids[s - 1]) for s in select_numbers(seqset, list(range(1, len(sel.uids) + 1)))
        ]

    deleted = session.deleted_uids()
    for seq, uid_value in targets:
        item = snapshot.by_uid.get(uid_value)
        if item is None:
            continue
        current = session.mailboxes.flags_of(item, deleted)
        if mode == "+":
            wanted = current | set(flags)
        elif mode == "-":
            wanted = current - set(flags)
        else:
            wanted = (current - set(mdef.permanent_flags)) | set(flags)
        for flag in mdef.permanent_flags:
            before, after = flag in current, flag in wanted
            if before == after:
                continue
            if flag == FLAG_DELETED:
                session.mailboxes.set_deleted(session.inbox_id, sel.name, [uid_value], after)
                (deleted.add if after else deleted.discard)(uid_value)
            elif flag in FLAG_LABELS:
                item = await session.mailboxes.set_flag(
                    session.api, session.inbox_id, sel.name, item, flag, after
                )
        final = session.mailboxes.flags_of(item, deleted)
        session.record_flags(uid_value, final)
        if not silent:
            parts = [b"FLAGS " + r.flag_list(final)]
            if uid_mode:
                parts.insert(0, b"UID " + str(uid_value).encode("ascii"))
            session.write(r.fetch_response(seq, parts))
    session.write(r.tagged(cmd.tag, "OK", f"{label} completed"))


def _canonical_flag(text: str) -> str:
    for flag in ALL_FLAGS:
        if flag.lower() == text.lower():
            return flag
    if text.startswith("\\"):
        raise no("unknown system flag", "CANNOT")
    raise no("keywords are not supported", "CANNOT")


async def _expunge_deleted(session: Session, *, only_uids: set[int] | None, silent: bool) -> None:
    sel = session.require_selected()
    assert session.api is not None and session.inbox_id is not None
    mdef = session.mailboxes.lookup(sel.name)
    assert mdef is not None
    if sel.read_only:
        raise no("mailbox is read-only", "READ-ONLY")
    marked = session.deleted_uids()
    if FLAG_DELETED not in mdef.permanent_flags:
        if marked:
            raise no(
                "permanent deletion is not supported; items in this view cannot be expunged",
                "CANNOT",
            )
        return
    doomed = sorted(u for u in marked if only_uids is None or u in only_uids)
    if not doomed:
        return
    snapshot = session.mailboxes.latest(session.inbox_id, sel.name)
    items = [snapshot.by_uid[u] for u in doomed if snapshot and u in snapshot.by_uid]
    errors = await session.mailboxes.expunge_items(session.api, session.inbox_id, sel.name, items)
    # Marks for removed items are cleared by the tombstone step of the sync below; marks for
    # items that failed to be removed stay in the store so the next EXPUNGE retries them.
    await session.mailboxes.sync(session.api, session.inbox_id, sel.name, force=True)
    if silent:
        # Update the session view without announcing (CLOSE): drop vanished UIDs quietly.
        latest = session.mailboxes.latest(session.inbox_id, sel.name)
        if latest is not None:
            sel.uids = [u for u in sel.uids if u in latest.by_uid]
            sel.seq_of = {u: i + 1 for i, u in enumerate(sel.uids)}
            sel.view_version = latest.version
    else:
        session.flush_pending()
    if errors:
        raise no(f"{len(errors)} item(s) could not be removed; try again", "UNAVAILABLE")


async def expunge(session: Session, cmd: Command) -> None:
    if cmd.args:
        raise bad("EXPUNGE takes no arguments")
    await _expunge_deleted(session, only_uids=None, silent=False)
    session.write(r.tagged(cmd.tag, "OK", "EXPUNGE completed"))


async def _uid_expunge(session: Session, cmd: Command, args: tuple) -> None:
    if len(args) != 1 or not isinstance(args[0], Atom):
        raise bad("UID EXPUNGE requires a sequence set")
    sel = session.require_selected()
    try:
        seqset = parse_sequence_set(args[0].value)
    except FetchSyntaxError as exc:
        raise bad(str(exc)) from exc
    await _expunge_deleted(session, only_uids=set(select_numbers(seqset, sel.uids)), silent=False)
    session.write(r.tagged(cmd.tag, "OK", "UID EXPUNGE completed"))


async def close_with_expunge(session: Session, cmd: Command) -> None:
    sel = session.require_selected()
    if not sel.read_only and session.deleted_uids():
        # A failed removal is reported: the tagged NO leaves the mailbox selected and the
        # \Deleted marks persisted, so the client can retry rather than lose the intent.
        await _expunge_deleted(session, only_uids=None, silent=True)
    session.selected = None
    session.state = State.AUTHENTICATED
    session.write(r.tagged(cmd.tag, "OK", "CLOSE completed"))


async def status(session: Session, cmd: Command) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) != 2 or not isinstance(cmd.args[1], ListTok):
        raise bad("STATUS requires a mailbox name and a parenthesised item list")
    name = _arg_text(cmd, 0, "mailbox name")
    mdef = session.mailboxes.lookup(name)
    if mdef is None:
        raise no("mailbox does not exist")
    wanted = []
    for tok in cmd.args[1].items:
        if not isinstance(tok, Atom) or tok.value.upper() not in (
            "MESSAGES",
            "RECENT",
            "UIDNEXT",
            "UIDVALIDITY",
            "UNSEEN",
        ):
            raise bad("invalid STATUS item")
        wanted.append(tok.value.upper())
    assert session.api is not None and session.inbox_id is not None
    snap = await session.mailboxes.sync(session.api, session.inbox_id, mdef.name, force=False)
    values = {
        "MESSAGES": len(snap.items),
        "RECENT": 0,
        "UIDNEXT": snap.uidnext,
        "UIDVALIDITY": snap.uidvalidity,
        "UNSEEN": sum(1 for i in snap.items if FLAG_SEEN not in i.flags),
    }
    body = " ".join(f"{k} {values[k]}" for k in wanted)
    session.write(
        b"* STATUS " + r.atom_or_quoted(mdef.name) + f" ({body})".encode("ascii") + r.CRLF
    )
    session.write(r.tagged(cmd.tag, "OK", "STATUS completed"))


async def id_(session: Session, cmd: Command) -> None:
    session.write(r.untagged('ID ("name" "imapgw" "version" "0.1.0")'))
    session.write(r.tagged(cmd.tag, "OK", "ID completed"))


async def namespace(session: Session, cmd: Command) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    session.write(r.untagged('NAMESPACE (("" NIL)) NIL NIL'))
    session.write(r.tagged(cmd.tag, "OK", "NAMESPACE completed"))


async def subscribe(session: Session, cmd: Command) -> None:
    session.require_state(State.AUTHENTICATED, State.SELECTED)
    if len(cmd.args) != 1:
        raise bad(f"{cmd.name} requires a mailbox name")
    if session.mailboxes.lookup(_arg_text(cmd, 0, "mailbox name")) is None:
        raise no("mailbox does not exist")
    session.write(r.tagged(cmd.tag, "OK", f"{cmd.name} completed"))


async def check(session: Session, cmd: Command) -> None:
    session.require_selected()
    session.write(r.tagged(cmd.tag, "OK", "CHECK completed"))


UID_HANDLERS.update({"SEARCH": _uid_search, "STORE": _uid_store, "EXPUNGE": _uid_expunge})
HANDLERS.update(
    {
        "SEARCH": search,
        "STORE": store,
        "EXPUNGE": expunge,
        "CLOSE": close_with_expunge,
        "STATUS": status,
        "ID": id_,
        "NAMESPACE": namespace,
        "SUBSCRIBE": subscribe,
        "UNSUBSCRIBE": subscribe,
        "CHECK": check,
        "CREATE": _unsupported("mailboxes are fixed views and cannot be created"),
        "DELETE": _unsupported("mailboxes are fixed views and cannot be deleted"),
        "RENAME": _unsupported("mailboxes are fixed views and cannot be renamed"),
        "COPY": _unsupported("COPY is not supported"),
        "IDLE": _unsupported("IDLE is not supported; poll with NOOP"),
    }
)
