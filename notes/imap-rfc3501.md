# RFC 3501 (IMAP4rev1), the parts the slice depends on

Source: `https://www.rfc-editor.org/rfc/rfc3501.txt`. Section numbers refer to the RFC. A local copy of RFC 5322 (message format) and RFC 2047 (encoded-word headers) was also pulled because draft projection and ENVELOPE need them.

## Protocol shape (2.2, 4.x, 7.x)

- Line-oriented over TCP, CRLF terminated. Client sends `<tag> <command> [args]`; server sends zero or more **untagged** responses (`* ...`), then one **tagged** completion: `<tag> OK|NO|BAD [<response-code>] text`.
- `OK` success, `NO` operational failure (bad login, no such mailbox, API down), `BAD` protocol error (unknown command, bad syntax, wrong state). `BYE` (untagged) before closing. Server greeting on connect is `* OK [CAPABILITY ...] text`.
- Response codes in square brackets on status lines: `ALERT`, `CAPABILITY`, `PERMANENTFLAGS`, `READ-ONLY`, `READ-WRITE`, `TRYCREATE`, `UIDNEXT`, `UIDVALIDITY`, `UNSEEN`, `PARSE`, `BADCHARSET`.
- Data types: atom, number, **quoted string** (7-bit, no CR/LF, `"..."` with `\"` and `\\` escapes), **literal** `{N}CRLF` followed by exactly N octets, parenthesized list, `NIL`.
- **Literal handshake (4.3, 7.5):** for client-to-server literals the client sends `{N}` + CRLF and MUST wait for the server's continuation `+ text` line before sending the N octets and the rest of the command. This is what `APPEND` uses; the parser must handle a command that spans multiple reads. Even `{0}` requires a continuation. (RFC 2088 `LITERAL+` `{N+}` non-synchronizing literals are an optional extension; not required.)
- Server-to-client literals (`FETCH` bodies) are `{N}` CRLF then bytes, no handshake. N is an **octet** count.
- 8-bit data is allowed in literals. NUL bytes are not.

## States (3)

Not Authenticated -> (LOGIN) -> Authenticated -> (SELECT/EXAMINE) -> Selected -> (CLOSE or another SELECT) -> Authenticated. LOGOUT from anywhere. Commands issued in the wrong state get `BAD`. Server may autologout after inactivity (at least 30 min per 5.4); clients use NOOP as keepalive.

## Commands in the required slice

### CAPABILITY (6.1.1)
`* CAPABILITY IMAP4rev1 ...` then tagged OK. `IMAP4rev1` must be listed. Anything else advertised must actually work. Since there is no TLS, servers usually advertise `LOGINDISABLED` only when plaintext login is forbidden; here plaintext LOGIN over localhost is the point, so do not advertise it.

### NOOP (6.1.2)
Does nothing but may carry pending untagged updates (`* n EXISTS`, `* n RECENT`, `* n FETCH (FLAGS ...)`). Natural hook for re-polling the API and announcing new messages (the harness's `/_test/add-message` exists to test this).

### LOGOUT (6.1.3)
Server MUST send `* BYE` then tagged OK, then close.

### LOGIN (6.2.3)
`LOGIN <userid> <password>`, both astrings (atom, quoted, or literal). Success -> Authenticated. Failure -> `NO`. Never echo the password. Here userid = inbox_id, password = API key.

### LIST (6.3.8, 7.2.2)
`LIST <reference> <mailbox-pattern>`. Wildcards `*` (any, including delimiter) and `%` (any, not crossing delimiter). Response per mailbox: `* LIST (<attrs>) "<delim>" <name>`, delimiter `NIL` for flat namespaces. `LIST "" ""` returns just the delimiter/root. Attributes: `\Noinferiors`, `\Noselect`, `\Marked`, `\Unmarked`. `INBOX` is case-insensitive and special; other names are case-sensitive. Clients commonly issue `LIST "" "*"`, `LIST "" "%"`, `LIST "" INBOX`. LSUB (subscriptions) is a sibling many clients also call.

### SELECT (6.3.1) and EXAMINE (6.3.2)
Before the tagged OK, the server MUST send untagged `FLAGS (...)`, `<n> EXISTS`, `<n> RECENT`, and OK responses with codes `UNSEEN <seqno>` (first unseen; omit if none), `PERMANENTFLAGS (...)`, `UIDNEXT <n>`, `UIDVALIDITY <n>`. Tagged OK carries `[READ-WRITE]` or `[READ-ONLY]`. Only one mailbox selected per connection; a failed SELECT leaves nothing selected. EXAMINE is identical but read-only.

### FETCH / UID FETCH (6.4.5, 6.4.8, 7.4.2)
`FETCH <sequence-set> <item | (items)>`. Sequence sets: `1`, `1:3`, `1,3,5`, `1:*`, `*`. Macros: `ALL` = `(FLAGS INTERNALDATE RFC822.SIZE ENVELOPE)`, `FAST` = `(FLAGS INTERNALDATE RFC822.SIZE)`, `FULL` = ALL + `BODY`.

Data items and what they need:
- `UID`: the message's UID. **MUST be included implicitly in every response to a UID command.**
- `FLAGS`: `(\Seen \Draft ...)`.
- `INTERNALDATE`: `"17-Aug-2026 16:00:00 +0000"` format (dd-Mon-yyyy hh:mm:ss +zzzz). Map from `timestamp`.
- `RFC822.SIZE`: octet count of the full raw message. Must equal the length of what `BODY[]` returns.
- `RFC822` = `BODY[]` (full message, sets `\Seen`); `RFC822.HEADER` = `BODY.PEEK[HEADER]`; `RFC822.TEXT` = `BODY[TEXT]`.
- `BODY[<section>]<<partial>>`: sections `HEADER`, `HEADER.FIELDS (a b)`, `HEADER.FIELDS.NOT (...)`, `TEXT`, `MIME`, numeric parts `1`, `1.2`, or empty for the whole message. Partial `<offset.count>`. `BODY.PEEK[...]` is the same without setting `\Seen`. Header subsets keep the blank separator line. Response literal is `BODY[section]<offset> {N}` with N = octets actually returned.
- `ENVELOPE`: parsed header structure `(date subject from sender reply-to to cc bcc in-reply-to message-id)`; addresses are `((name adl mailbox host) ...)` or `NIL`. Needs an RFC 5322 header parser; encoded-words are passed through as-is (clients decode).
- `BODYSTRUCTURE` / `BODY`: MIME tree. Needs a MIME parser; the multipart fixture exists to exercise this. Optional for the required path, but Thunderbird asks for it.
- The number after `*` in `* n FETCH` is always a **sequence number**, even for UID FETCH. Non-existent UIDs are silently ignored (OK with no data).
- A plain (non-PEEK) `BODY[...]`/`RFC822`/`RFC822.TEXT` fetch implicitly sets `\Seen` and the server should report `FLAGS` in the same FETCH response.

### APPEND (6.3.11)
`APPEND <mailbox> [(flags)] [date-time] {N}` then continuation, then N octets. Server SHOULD honor the flags and date; on failure the mailbox MUST be unchanged (no partial append). Non-existent mailbox -> `NO [TRYCREATE]`. If the mailbox is currently selected, send `* n EXISTS`. RFC explicitly allows draft messages to omit otherwise-required headers. RFC 4315 `UIDPLUS` adds `OK [APPENDUID <uidvalidity> <uid>]`, which is what lets clients find the message they just appended; it is an optional extension and a strong candidate for the "one additional capability".

### UID (6.4.8)
`UID FETCH|STORE|COPY|SEARCH ...`: same as the base command but sequence-set numbers are UIDs. `UID SEARCH ALL` returning UIDs is what most clients use to enumerate a mailbox.

## UID rules (2.3.1.1)

- 32-bit, strictly ascending in the order messages were added, not necessarily contiguous. MUST NOT change within a session; SHOULD NOT change between sessions.
- Any change between sessions MUST be signaled by a **greater UIDVALIDITY**. `(mailbox, UIDVALIDITY, UID)` must forever denote one immutable message: internal date, size, envelope, structure, body text never change. Flags may change.
- `UIDNEXT` MUST NOT change unless messages are added, and MUST change when they are (even if later expunged).
- A store with no UID persistence must regenerate UIDs each session with a new UIDVALIDITY each time. The assignment forbids this route: UIDs must survive restarts, so persistence is mandatory.

## Flags (2.3.2, 7.2.6)

System flags: `\Seen`, `\Answered`, `\Flagged`, `\Deleted`, `\Draft`, `\Recent` (server-set only; not settable via STORE and not in PERMANENTFLAGS). `\*` in PERMANENTFLAGS means clients may create keywords. Flags listed in `FLAGS` but not in `PERMANENTFLAGS` are session-only.

## Other commands clients commonly send (not required, decide how to reject)

`AUTHENTICATE`, `STARTTLS`, `CREATE`, `DELETE`, `RENAME`, `SUBSCRIBE`, `LSUB`, `STATUS`, `CHECK`, `CLOSE`, `EXPUNGE`, `SEARCH`, `STORE`, `COPY`, `ID` (RFC 2971), `NAMESPACE` (RFC 2342), `IDLE` (RFC 2177), `ENABLE`, `COMPRESS`. Unknown commands get `BAD`; known-but-unsupported may get `NO` or `BAD`. The assignment allows deliberately rejecting valid IMAP outside the chosen slice.

## Sample exchange for the required path

```
S: * OK [CAPABILITY IMAP4rev1] ready
C: A1 CAPABILITY
S: * CAPABILITY IMAP4rev1
S: A1 OK CAPABILITY completed
C: A2 LOGIN "candidate@imap.test" "test_agentmail_key"
S: A2 OK LOGIN completed
C: A3 LIST "" "*"
S: * LIST (\HasNoChildren) NIL INBOX
S: * LIST (\HasNoChildren) NIL Drafts
S: A3 OK LIST completed
C: A4 SELECT INBOX
S: * FLAGS (\Seen \Draft)
S: * 4 EXISTS
S: * 0 RECENT
S: * OK [UNSEEN 1]
S: * OK [PERMANENTFLAGS (\Seen)]
S: * OK [UIDVALIDITY 1]
S: * OK [UIDNEXT 5]
S: A4 OK [READ-WRITE] SELECT completed
C: A5 UID FETCH 1:* (FLAGS RFC822.SIZE)
S: * 1 FETCH (UID 1 FLAGS () RFC822.SIZE 296)
S: ...
S: A5 OK UID FETCH completed
C: A6 UID FETCH 2 BODY.PEEK[]
S: * 2 FETCH (UID 2 BODY[] {375}
S: <375 raw octets>
S: )
S: A6 OK UID FETCH completed
C: A7 APPEND Drafts (\Draft) {123}
S: + Ready for literal data
C: <123 octets of RFC 822 draft>
S: A7 OK APPEND completed
C: A8 LOGOUT
S: * BYE logging out
S: A8 OK LOGOUT completed
```
