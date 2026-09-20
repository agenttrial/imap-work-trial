# imapgw: an IMAP4rev1 gateway for AgentMail

A small IMAP server that fronts the public AgentMail REST API. An IMAP client logs in with an
AgentMail inbox id and API key, sees the inbox as `INBOX`, `Drafts`, `Sent`, `Trash`, and `Spam`,
downloads messages byte for byte, reads existing drafts, and saves new plain-text drafts with
`APPEND`. Built for the AgentMail work trial; the original trial materials are
[ASSIGNMENT.md](./ASSIGNMENT.md), [API_RESOURCES.md](./API_RESOURCES.md),
[MANUAL_SMOKE_TEST.md](./MANUAL_SMOKE_TEST.md), and [STANDARD_IMAP_CLIENT.md](./STANDARD_IMAP_CLIENT.md).

The design, every decision with its alternatives, the outcome of an adversarial review, and the
scaling path are in [DESIGN.md](./DESIGN.md). Condensed source material (API schemas, RFC 3501
notes, fake API behavior) is in [notes/](./notes/README.md).

## Setup

Requirements: Python 3.12 and Node 20+ (the fake AgentMail API is a Node script). The server
itself has **no third-party runtime dependencies**.

```bash
brew install uv                 # or: https://docs.astral.sh/uv/getting-started/installation/
uv python install 3.12          # once; uv manages the interpreter and .venv
```

## Start

Terminal 1, the local AgentMail sandbox:

```bash
npm --prefix test-harness run api
```

Terminal 2, the IMAP server (reads `.env` if present, else the defaults below):

```bash
uv run python -m imapgw
```

It listens on `127.0.0.1:1143` and talks to `http://127.0.0.1:3210/v0`. Stop with Ctrl-C. If the
UID store cannot be opened safely (locked, read-only, unsupported schema), the server logs why and
exits with status 2 instead of discarding UID state.

Configuration (environment or `.env`; see `.env.example` for the full list):

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMAP_HOST`, `IMAP_PORT` | `127.0.0.1`, `1143` | bind address |
| `AGENTMAIL_API_URL` | `http://127.0.0.1:3210/v0` | the only place the API key is ever sent |
| `AGENTMAIL_INBOX_ID` | unset | optional allowlist: if set, `LOGIN` accepts only this inbox id |
| `AGENTMAIL_API_KEY` | unset | **ignored by the server**; used only by the smoke script. Credentials come from `LOGIN` |
| `IMAPGW_DB_PATH` | `./imapgw.sqlite3` | UID store (SQLite) |
| `IMAPGW_HTTP_TIMEOUT`, `IMAPGW_HTTP_RETRIES` | `10` s, `3` | per-request timeout and attempts against the API |
| `IMAPGW_IDLE_TIMEOUT`, `IMAPGW_COMMAND_TIMEOUT` | `1800` s, `30` s | connection idle limit; per-command budget once parsed |
| `IMAPGW_ASSEMBLY_TIMEOUT`, `IMAPGW_WRITE_TIMEOUT` | `60` s, `30` s | deadline to finish sending one command, from its first byte; how long a client may stall reading |
| `IMAPGW_POST_COMMAND_WAIT` | `2` s | how long to wait after `APPEND` answers `OK` for the re-listing, so `EXISTS` can follow promptly |
| `IMAPGW_MAX_LINE`, `IMAPGW_MAX_LITERAL` | 64 KiB, 1 MiB | per-line and per-literal caps |
| `IMAPGW_MAX_COMMAND_BYTES`, `IMAPGW_MAX_LITERALS` | 1 MiB + 64 KiB, `16` | caps for one whole command |
| `IMAPGW_MAX_CONNECTIONS` | `100` | concurrent client connections |
| `IMAPGW_REFRESH_INTERVAL` | `5` s | minimum age before `STATUS` re-lists a mailbox |
| `IMAPGW_LOG_LEVEL` | `INFO` | logging level |

## Test

```bash
uv run python -m unittest discover -s tests -t .       # unit + integration (starts the fake API itself)
uv run python -m unittest tests.unit.test_parser        # one module
uv run python -m unittest tests.integration.test_append.AppendTests.test_append_round_trips_through_the_api
uv run ruff format imapgw tests scripts && uv run ruff check imapgw tests scripts   # dev-only tooling
```

The integration tests spawn the fake API on an ephemeral port and the server on port 0, so they
do not conflict with a running instance. There are about 260 tests; the suite takes under a
minute. Roughly a third of them are regression tests for findings from an adversarial review of
the first complete version (see DESIGN.md section 8b).

## Smoke test

Repeatable end-to-end run: starts the fake API and the server, drives a real TCP session through
CAPABILITY, LOGIN, LIST, SELECT INBOX, UID FETCH (metadata and a full body, checking that
`RFC822.SIZE` equals the literal length), SELECT Drafts, APPEND, verification of the new draft,
NOOP, LOGOUT, then tears everything down. Exit code 0 on success. The transcript is printed with the
key masked.

```bash
scripts/smoke_all.sh
```

Against an already running server (or a hosted sandbox), set the `AGENTMAIL_*` and `IMAP_*`
variables and run `uv run python scripts/smoke.py`.

Manual check with `nc` (the server accepts bare LF line endings for exactly this):

```text
$ nc 127.0.0.1 1143
* OK [CAPABILITY IMAP4rev1 UIDPLUS ID NAMESPACE] imapgw ready
A1 LOGIN "candidate@imap.test" "test_agentmail_key"
A1 OK [CAPABILITY IMAP4rev1 UIDPLUS ID NAMESPACE] LOGIN completed
A2 LIST "" "*"
A3 SELECT INBOX
A4 UID FETCH 1:* (FLAGS RFC822.SIZE INTERNALDATE)
A5 UID FETCH 2 BODY.PEEK[]
A6 SELECT Drafts
A7 UID FETCH 1:* (FLAGS BODY.PEEK[HEADER])
A8 APPEND Drafts (\Draft) {57}
To: someone@example.com
Subject: hello

Saved from IMAP.
A9 LOGOUT
```

(The `{57}` counts bytes with LF line endings, which is what `nc` sends; with CRLF it would be
`{61}`. Do not press Return before the first header line: a message that starts with a blank line
has no headers.)

## What is implemented

Commands (RFC 3501 unless noted):

| Command | Notes |
| --- | --- |
| `CAPABILITY`, `NOOP`, `LOGOUT` | `NOOP` in a selected mailbox re-lists it and announces `EXISTS`, `EXPUNGE`, and `FETCH FLAGS` for changes made elsewhere |
| `LOGIN user password` | user = inbox id, password = API key; atoms, quoted strings, or literals; printable ASCII only. Verified against `GET /auth/me` (and `GET /inboxes/{id}` for org-wide keys) |
| `LIST`, `LSUB` | flat namespace, `NIL` delimiter, `*` and `%` wildcards (equivalent in a flat namespace); `INBOX` matches case-insensitively, other names are case-sensitive. `LSUB` lists every mailbox: subscriptions are not modelled |
| `SELECT`, `EXAMINE`, `CLOSE`, `CHECK` | emits `FLAGS`, `EXISTS`, `RECENT` (always 0), `UNSEEN`, `PERMANENTFLAGS`, `UIDVALIDITY`, `UIDNEXT`; `CLOSE` expunges `\Deleted` items without announcing them and answers `NO` if any removal failed (the mailbox stays selected for a retry) |
| `STATUS` | `MESSAGES RECENT UIDNEXT UIDVALIDITY UNSEEN` |
| `FETCH`, `UID FETCH` | items `UID FLAGS INTERNALDATE RFC822.SIZE RFC822 RFC822.HEADER RFC822.TEXT` and `BODY[]`, `BODY[HEADER]`, `BODY[TEXT]`, `BODY[HEADER.FIELDS (...)]`, `BODY[HEADER.FIELDS.NOT (...)]`; the `BODY[...]` forms also accept `.PEEK` and `<offset.count>` partials; macro `FAST`. Non-PEEK body fetches set `\Seen` |
| `SEARCH`, `UID SEARCH` | `ALL SEEN UNSEEN FLAGGED UNFLAGGED DELETED UNDELETED DRAFT UNDRAFT ANSWERED UNANSWERED NEW OLD RECENT KEYWORD UNKEYWORD UID <set> <set> BEFORE ON SINCE SENTBEFORE SENTON SENTSINCE LARGER SMALLER FROM TO CC BCC SUBJECT BODY TEXT HEADER NOT OR (...)`; `CHARSET` UTF-8 or US-ASCII; `TEXT`/`BODY` match decoded content (encoded-word headers, base64 and quoted-printable bodies). `KEYWORD x` matches the AgentMail label `x`, see below |
| `STORE`, `UID STORE` | `FLAGS`, `+FLAGS`, `-FLAGS`, with `.SILENT`; flags `\Seen`, `\Flagged`, `\Deleted` (see mapping below) |
| `EXPUNGE`, `UID EXPUNGE` (RFC 4315) | moves `\Deleted` messages to trash; deletes `\Deleted` drafts after verifying they were not edited since they were listed |
| `APPEND mailbox [(flags)] [date-time] literal` | `Drafts` only; creates a draft through `POST /inboxes/{id}/drafts`; replies `OK [APPENDUID uidvalidity uid]` (RFC 4315) |
| `ID` (RFC 2971), `NAMESPACE` (RFC 2342), `SUBSCRIBE`, `UNSUBSCRIBE` | minimal responses for client compatibility; `SUBSCRIBE`/`UNSUBSCRIBE` succeed for known mailboxes and change nothing |

Deliberately rejected with `NO [CANNOT]`: `STARTTLS`, `AUTHENTICATE`, `CREATE`, `DELETE`, `RENAME`,
`COPY`, `IDLE`, and `STORE` of `\Answered` or of keywords (non-system flags). Rejected with `BAD`:
`FETCH` items `ENVELOPE`, `BODYSTRUCTURE`, `BODY` (structure), numbered MIME sections such as
`BODY[1]`, the `ALL`/`FULL` macros, non-synchronising literals `{N+}`, unknown commands, and
malformed syntax. Error text never echoes client-supplied arguments and never contains control
characters. Malformed input never closes the connection except: a line over 64 KiB, a command that
exceeds the aggregate caps, or a command still incomplete after the assembly deadline, each of
which gets `BAD` or `BYE` and a close.

### Mailbox and flag mapping

AgentMail has labels, not folders; a message can carry several. Each mailbox is a **label view**:

| Mailbox | API query | Local rule |
| --- | --- | --- |
| `INBOX` | `labels=received` | messages also labelled `trash` or `spam` are hidden (they appear in Trash/Spam) |
| `Drafts` | Drafts API | rendered as RFC 822 (below) |
| `Sent` | `labels=sent` | same hiding rule as INBOX |
| `Trash` | `labels=trash&include_trash=true` | |
| `Spam` | `labels=spam&include_spam=true` | |

| Flag | Meaning | Changeable |
| --- | --- | --- |
| `\Seen` | message lacks the `unread` label; drafts are always seen | yes: `read`/`unread` labels via `PATCH` |
| `\Flagged` | `starred` label | yes: `starred` label |
| `\Draft` | item is a draft | no |
| `\Deleted` | stored in the gateway's SQLite database, so it is shared across sessions and survives restarts; `EXPUNGE` then adds the `trash` label (messages) or deletes the draft | yes, in INBOX, Sent, Drafts. Not in Trash or Spam, because the only remaining operation would be permanent deletion, which this server never performs |
| `\Answered`, `\Recent`, keywords | not derivable from the API | no; `RECENT` is always 0 and `NEW` never matches |

`PERMANENTFLAGS` advertises exactly what each view accepts. `SEARCH KEYWORD <label>` and
`UNKEYWORD` test AgentMail labels directly (for example `KEYWORD starred` or a label an agent
applied), even though labels are not exposed as IMAP keywords in `FLAGS`. This is deliberate: it
is the one place an IMAP client can see the agent's own labels.

### Drafts as RFC 822

A draft is rendered deterministically as `From: <inbox>`, `To`, `Cc`, `Bcc`, `Reply-To`, `Subject`
(RFC 2047 encoded if needed), `Date` (from `created_at`, so metadata-only updates do not change the
bytes), `Message-ID: <draft_id@domain>`, `X-AgentMail-Draft-Id`, then a UTF-8 `text/plain` body
with CRLF line endings. `RFC822.SIZE` is the byte length of that rendering. For received messages,
`RFC822.SIZE` comes from the raw-message endpoint or the downloaded bytes, never from the list
endpoint (see limitations). `Bcc` is included
because the owner is reading their own draft.

`APPEND` accepts `text/plain` in any charset and transfer encoding, or a `multipart/alternative`
that contains a `text/plain` part. Bodies are decoded strictly: bytes invalid in the declared
charset are refused rather than replaced. Recipients keep their display names; the subject is
decoded. HTML-only messages and attachments are refused with `NO [CANNOT]` (attachments in new
drafts are out of scope and the sandbox rejects them). Flags and date arguments are accepted and
ignored. A `client_id` derived from the literal is sent so a transport-level retry of the same
APPEND does not create duplicates on the real API (two intentional identical APPENDs would share it).

Once the create request succeeds, `APPEND` answers `OK` and allocates the UID from the create
response itself. The follow-up listing runs as a separate task outside the command's deadline;
its failure or slowness is logged, never reported as an APPEND failure. When Drafts is selected,
the `* n EXISTS` for the new draft is sent after the tagged `OK`, as soon as that listing lands
(normally within milliseconds) or at the next safe point. A listing that does not yet show the
new draft cannot remove it: freshly created drafts stay visible for two minutes or until a listing
includes them, and the pin is dropped the moment the draft is deleted through the gateway.

### Editing drafts elsewhere

A draft's UID is keyed on the draft id **and a hash of its rendered content**. If an agent edits a
draft through the API, the old UID disappears (`EXPUNGE`) and the edit appears as a new message
with a new UID, which keeps every UID immutable as RFC 3501 requires. Gmail's IMAP drafts behave
the same way.

Deletion is version-checked: before deleting a draft the gateway re-fetches it and refuses if the
content no longer matches the UID's version, answering `NO`. This protects an agent's edit from a
client that marks a stale copy deleted, except in the window between that check and the delete
request, which the API offers no conditional delete to close.

## Architecture and persistence

```
TCP → session (parser, state machine, serial dispatch) → command handlers
      → mailbox service (label views, flags, sync, UID allocation) → AgentMail client (HTTP)
                            ↕                    ↕
                     SQLite UID store      in-memory byte cache
```

- **Parser** (`imapgw/parser.py`): hand-written incremental tokenizer; handles `{N}` literals with
  the `+` continuation handshake, quoted strings, nested lists, and FETCH section brackets. Every
  size is an octet count. Per-line, per-literal, and per-command caps are enforced before any
  literal bytes are accepted.
- **Session** (`imapgw/session.py`): one per connection; states not-authenticated, authenticated,
  selected, logout. Commands run serially. Each session remembers the flags it last reported per
  UID so changes made elsewhere are announced at the next safe point. The API key lives only in
  the session's client and is registered with a reference-counted log filter that replaces it in
  log messages, exception text, and stack traces. Failures map to `NO` (`[UNAVAILABLE]` for
  upstream trouble), protocol errors to `BAD`; internal errors are logged with a reference id and
  answered with `NO` without dropping the connection. A revoked key mid-session gives `* BYE`.
- **Mailbox service** (`imapgw/mailbox.py`): lists a view completely (pagination is followed to the
  last page, never truncated), validates the response shape, sorts by timestamp then id, allocates
  UIDs for unseen items, and tombstones vanished ones. One sync is in flight per mailbox regardless
  of how many sessions ask; an authentication failure is attributed to the credentials that made
  the request, and other sessions retry with their own. A failed or malformed sync keeps the
  previous snapshot and tombstones nothing.
- **UID store** (`imapgw/uidstore.py`): SQLite, WAL mode. Tables `mailbox(inbox_id, name,
  uidvalidity, next_uid)`, `uid_map(inbox_id, name, uid, remote_key, remote_id, first_seen_at,
  vanished_at)` with a unique index on live keys, and `deleted_mark(inbox_id, name, uid)`.
  Allocation is one `BEGIN IMMEDIATE` transaction, so UIDs are strictly ascending and survive
  restarts. Vanished items keep their row, so a UID is never reused even if the message reappears
  (it gets a new, higher UID). `UIDVALIDITY` is set when a mailbox row is first created. It
  changes only if the database is found corrupt on start (SQLite reports "not a database" or
  "malformed"), in which case the file is set aside and a new one created; a locked, read-only, or
  otherwise unusable store makes the server refuse to start.
- **Byte cache** (`imapgw/bytecache.py`): LRU bounded by bytes (64 MiB default), keyed by inbox,
  object kind, and id, so two inboxes that receive the same Message-ID never share bytes.
- **API client** (`imapgw/apiclient.py`): stdlib `http.client` in a bounded thread pool so the event
  loop never blocks; bearer auth; retries on 429 and 5xx honouring a numeric `Retry-After` within a
  budget; only 2xx counts as success (a redirect is an error); errors classified by HTTP status.
  Raw downloads go through a separate path that sends **no application headers**, so the API key
  never reaches the presigned URL (asserted via the fake's request log), and must return exactly
  200 with a body whose length matches the announced size, or nothing is served or cached.

A mailbox is re-listed on `SELECT`/`EXAMINE`, `NOOP`, after `APPEND` and `EXPUNGE`, and on `STATUS`
when the snapshot is older than the refresh interval. `FETCH`, `SEARCH`, and `STORE` work from the
current snapshot without re-listing. New mail is announced as `* n EXISTS`, removals as
`* n EXPUNGE`, and flag changes as `* n FETCH (FLAGS ...)`, only at safe points, never in the
middle of a `FETCH`, `STORE`, or `SEARCH`.

## Known limitations and next steps

- **No `ENVELOPE` / `BODYSTRUCTURE` / numbered MIME parts.** Clients that insist on them (some
  desktop clients do for the message list) get `BAD`. Adding them is the first thing to build for
  full desktop-client support; Python's `email` parser makes it tractable.
- **No `IDLE`; clients poll with `NOOP`.** The production AgentMail IMAP supports IDLE; here it
  would need a background poller (or AgentMail webhooks/WebSockets) per selected inbox.
- **`RFC822.SIZE` costs one metadata request per message the first time it is asked for without
  the body.** The size comes from the raw-message endpoint (the same source as the bytes) and is
  remembered per message, so a UID's reported size never changes; a later download whose length
  disagrees is refused rather than served. The list endpoint's `size` field is only compared and
  logged. Desktop clients that request sizes for every message on first open therefore issue one
  extra call per message once.
- **`UIDVALIDITY` is issued from the clock with a sidecar watermark** (`<db>.validity`) that
  records the highest value ever issued, so a database that is lost, corrupt, or recreated within
  the same second still gets a strictly larger value, even if the clock went backwards. Deleting
  both the database and the sidecar is a deliberate fresh start and the only way to reuse a value.
- **Every 401 or 403 is treated as invalid credentials.** A key that is valid but lacks a specific
  permission is indistinguishable from a revoked one and ends the session with `BYE`.
- **`Retry-After` is honoured only in seconds form**; an HTTP-date value falls back to exponential
  backoff.
- **Draft deletion cannot be exercised against the fake** (no `DELETE /drafts` route); it is
  unit-tested with a scripted transport and returns `NO` end to end against the fake.
- **`CHARSET` other than UTF-8/US-ASCII** is refused with `NO [BADCHARSET (UTF-8)]`.
- **Single process.** Coalescing and caching are per process; scaling out requires the shared
  UID store described in DESIGN.md section 5.
- **`\Recent` is never set**, `RECENT` is always 0, and `NEW` matches nothing.
- **Verified against a real AgentMail inbox once** (DESIGN.md section 7.1): ids, sizes, the
  presigned download, drafts, label writes, and draft deletion all behaved as assumed. One
  production behaviour the fake lacks was found and handled: the list endpoint lags label writes
  by a few seconds, so labels the gateway writes itself are held over stale listings for up to
  60 seconds. Not yet observed in production: 429 responses, permission-scoped keys, mailboxes
  larger than one page.
- Next: `ENVELOPE`/`BODYSTRUCTURE`, `IDLE`, `MOVE`, `AUTHENTICATE PLAIN`, `LITERAL+`, a
  Thunderbird interoperability pass (see `STANDARD_IMAP_CLIENT.md`), and a smoke run against the
  hosted sandbox.

## Generated and third-party code disclosure

- The IMAP protocol implementation (tokenizer, commands, responses, state machine, UID and
  mailbox semantics, search evaluation) is original code written for this trial with the help of
  an AI coding assistant (Claude Code), which also produced the design document and tests under
  the author's direction. No IMAP server or protocol library is used, in the server or in the
  tests. The tests drive the server with a small hand-written raw-socket client
  (`tests/support/imap_client.py`).
- An independent AI-driven adversarial review of the first complete version produced 21 findings;
  their resolution is recorded in DESIGN.md section 8b.
- Python's standard library `email` package is used for message-format work only: parsing the
  RFC 822 literal given to `APPEND`, rendering drafts as RFC 822, decoding headers and bodies for
  `SEARCH`. This is RFC 5322/MIME handling, not IMAP.
- The fake AgentMail API under `test-harness/` was provided with the assignment and is unchanged.
