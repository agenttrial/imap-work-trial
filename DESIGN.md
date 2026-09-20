# Design: an IMAP4rev1 gateway for AgentMail

Status: implemented 2026-09-19 (steps 0-12 of section 8a; Thunderbird pass pending). Originally a draft for discussion; decision records below are kept as written, with implementation notes where the build deviated. Every decision below lists the alternatives considered, the tradeoffs, the choice, and what would make us revisit it. Decisions marked **PENDING** need the user's input.

Companion material: `ASSIGNMENT.md` (the spec), `notes/` (condensed source material), `CLAUDE.md` (harness behavior).

---

## 1. Problem statement

AgentMail exposes email as REST resources with free-form labels. Email clients and libraries speak IMAP4rev1, a stateful TCP protocol with folders, permanent numeric UIDs, flags, and byte-exact RFC 822 message bodies. We are building a **protocol gateway**: a TCP server that presents an AgentMail inbox as an IMAP mailbox, translating each IMAP command into AgentMail API calls and each API response into IMAP responses.

The required vertical slice: connect, `LOGIN` with inbox id and API key, `LIST` and `SELECT` the `INBOX` and `Drafts` mailboxes, `UID FETCH` metadata and full raw bytes, `APPEND` a plain-text draft that round-trips recipients, subject, and body, `NOOP`, `LOGOUT`. Supporting rules: `\Seen` mirrors read state, `\Draft` marks drafts, byte lengths for all literals and sizes, complete pagination, UIDs stable across restarts and never reused, graceful handling of bad input and API failures, no secret leakage.

Out of scope by the assignment: TLS, deployment, attachments in new drafts, sending drafts, production compatibility.

## 2. The impedance mismatches that drive the design

| IMAP model | AgentMail model | Design consequence |
| --- | --- | --- |
| Exclusive folders | Non-exclusive labels (`received`, `sent`, `trash`, `unread`, ...) | A mailbox is a **label view** with a defined filter (D5) |
| 32-bit UID per message, strictly ascending, immutable forever with UIDVALIDITY | Opaque string ids, list order newest-first, mutable drafts | We mint and persist our own UIDs (D6) |
| `\Seen` flag | `read` / `unread` labels | Flags are projections of labels (D5) |
| Raw RFC 822 bytes, exact sizes | Two-step download via expiring presigned URL | Fetch bytes, cache immutably, size from bytes (D7) |
| Drafts are messages | Drafts are JSON | Two projections, JSON to RFC 822 and back (D8) |
| Server pushes `EXISTS` for new mail | Pull-only REST (webhooks exist, out of scope) | Poll and reconcile at safe points (D9) |
| One synchronous conversation per connection | Paginated, rate-limited HTTP with transient failures | Complete pagination, bounded retries, map failures to `NO` (D9, D10) |
| Commands can contain byte literals announced by `{N}` with a continuation handshake | n/a | Hand-written incremental parser (D3) |

## 3. Architecture overview

```
                 TCP 127.0.0.1:1143
                        │
             ┌──────────▼──────────┐
             │   Connection/Session │  one per client socket
             │  state machine:      │  NotAuth → Auth → Selected → Logout
             │  buffer, parser,     │
             │  seq-number snapshot │
             └──────────┬──────────┘
                        │ parsed command
             ┌──────────▼──────────┐
             │   Command handlers   │  CAPABILITY LOGIN LIST SELECT/EXAMINE
             │  (pure protocol      │  UID FETCH / FETCH  APPEND  NOOP  LOGOUT
             │   logic, emit        │  + rejections for everything else
             │   responses)         │
             └──────────┬──────────┘
                        │ mailbox operations
             ┌──────────▼──────────┐       ┌──────────────────┐
             │   Mailbox layer      │◄──────┤  UID store        │ durable:
             │  INBOX, Drafts,      │       │  per (inbox, mbox)│ uidvalidity,
             │  (Sent, Trash, Spam) │       │  remote-id → UID  │ next_uid, map
             │  label views, flags, │       └──────────────────┘
             │  draft projection    │
             └──────────┬──────────┘       ┌──────────────────┐
                        │                  │  Byte cache       │ immutable
             ┌──────────▼──────────┐       │  message_id →     │ raw bytes,
             │   Sync engine        │──────►│  bytes; draft     │ bounded LRU
             │  list-all-pages,     │       │  hash → bytes     │
             │  assign new UIDs,    │       └──────────────────┘
             │  detect vanished     │
             └──────────┬──────────┘
                        │ HTTP
             ┌──────────▼──────────┐
             │   AgentMail client   │  bearer auth, timeouts, 429 backoff,
             │  (thin, typed)       │  pagination iterator, never sends key
             └──────────┬──────────┘  to download URLs
                        │
                  AgentMail API  (fake on :3210, or hosted)
```

Layering rule: protocol code never calls HTTP directly, and the API client knows nothing about IMAP. The mailbox layer is the only place that understands both, and it is where every mapping decision lives.

### 3.1 The three critical paths

**SELECT INBOX**
1. Sync engine lists messages with the INBOX filter, following `next_page_token` until absent.
2. For each `message_id` not in the UID store, allocate `next_uid++` in deterministic order (ascending `timestamp`, then id). Persist atomically.
3. Build the session's sequence-number snapshot: the live UIDs sorted ascending; sequence number i is the i-th UID.
4. Emit `FLAGS`, `EXISTS`, `RECENT`, `UNSEEN`, `PERMANENTFLAGS`, `UIDNEXT`, `UIDVALIDITY`, tagged `OK [READ-WRITE]`.

**UID FETCH n BODY.PEEK[]**
1. Resolve UID to `message_id` via the snapshot.
2. Byte cache hit? Serve. Miss? `GET .../messages/{id}/raw` for `download_url`, then `GET download_url` with no auth, verify length, cache.
3. Emit `* seq FETCH (UID n BODY[] {N}` + N bytes + `)`; N is the byte length of the buffer.

**APPEND Drafts (\Draft) {N}**
1. Parser sees `{N}`, validates N against the literal cap, sends `+ ready`, accumulates exactly N bytes.
2. Parse the RFC 822 message: recipients, subject, plain-text body (D8).
3. `POST .../drafts`. On non-2xx, `NO`, nothing changed.
4. Re-sync Drafts, allocate a UID for the new `draft_id`, respond `OK` (with `[APPENDUID uidvalidity uid]` if UIDPLUS is in scope). If Drafts is the selected mailbox, emit `* n EXISTS`.

---

## 4. Decisions

Each decision: options, tradeoffs, choice, revisit trigger.

### D1. Language and runtime **DECIDED: Python 3.12, stdlib only; no imaplib anywhere, tests use a hand-written raw-socket client**

Rule check: the assignment bans "a library that implements the IMAP server or protocol core". Python's `email` package implements RFC 5322 and MIME (message format), not RFC 3501 (IMAP). Every IMAP server in existence uses a message-format library; the protocol core (tokenizer, command grammar, state machine, response formatting, UID and mailbox semantics) stays entirely ours. `imaplib` is an IMAP *client* and would be used only inside tests as an independent referee, never by the server. Both uses will be disclosed explicitly in the README.

The candidates are the ones that are installed or trivially installable and that the user could be fluent in: TypeScript on Node 24, Python 3.12 (upgrade from the installed 3.9), Go.

What the project actually needs from a language:
- Raw TCP server with many long-lived, mostly idle connections.
- First-class byte buffers distinct from text strings, because every size in IMAP is an octet count.
- RFC 5322 header parsing and generation, RFC 2047 encoded-word encode/decode, MIME tree parsing (for APPEND, draft rendering, and `ENVELOPE`/`BODYSTRUCTURE` if any client asks).
- HTTP client with timeouts.
- A small durable store.
- A test runner that can spin up two servers in-process and drive raw sockets.

| Criterion | TypeScript / Node 24 | Python 3.12 | Go |
| --- | --- | --- | --- |
| TCP server | `node:net`, event loop; idle connections are nearly free | `asyncio.start_server`; same model | goroutine per connection; simplest mental model |
| Bytes vs text | `Buffer` is explicit and good; easy to accidentally `.toString()` | `bytes` vs `str` is enforced by the type system at runtime; mixing raises | `[]byte` vs `string`, explicit |
| RFC 5322 / MIME / RFC 2047 | **Nothing in stdlib.** Hand-write header unfolding, address-list splitting, encoded-word codec, MIME boundary walking. ~300-500 lines of subtle code, or a heavy dependency like `mailparser` | **Full stdlib**: `email.message_from_bytes`, `policy.SMTP`, address headers parsed into structured addresses, `get_body(preferencelist=('plain',))` handles `multipart/alternative` and decodes transfer encodings, `email.header` for RFC 2047. Decades of production use | `net/mail` (headers, addresses, dates), `mime`, `mime/multipart`, `mime/quotedprintable`. Solid but lower level than Python |
| HTTP client | global `fetch`, `AbortSignal.timeout` | `urllib` is clunky; `httpx` or `aiohttp` is a dependency. `http.client` works but is synchronous (fine in a thread or with small volumes) | `net/http`, excellent |
| Durable store | `node:sqlite` built in (unflagged since 22.13) or JSON file | `sqlite3` stdlib or JSON | `database/sql` needs a driver dependency; JSON otherwise |
| Test harness affinity | Same runtime as the fake API; can import `createFakeAgentMailServer` and start it on port 0 in-process | Must spawn the fake as a subprocess (`node fake-agentmail-api.mjs`) and wait for readiness; ~20 lines | Same as Python |
| IMAP client for tests | None built in; write a small raw-socket test client (also gives exact byte control) | `imaplib` in stdlib: a real, widely deployed IMAP client to exercise our server. Allowed: it is a client, not a server library | None built in |
| Types and tooling | TS runs natively on Node 24 via type stripping, no build step | Type hints + `mypy`/`pyright` optional | Static, compiled |
| Concurrency at scale | Single event loop per process; scale by processes | Same, plus the GIL for CPU work (irrelevant here, I/O bound) | Best story, native parallelism |
| One-command start | `node src/server.ts` | `python -m imapgw` after `uv sync` | single static binary |
| Setup cost today | none | `uv python install 3.12` (one command) | install Go toolchain, learn curve if unfamiliar |

Tradeoff summary:
- **TypeScript** wins on harness affinity and zero setup, loses on the mail-parsing surface, which is where the subtle correctness bugs live (header folding, quoted local parts, encoded words spanning folds, transfer encodings).
- **Python** wins decisively on the mail-parsing surface and on having a real IMAP client for tests, loses a little on HTTP client ergonomics and needs a one-command interpreter upgrade.
- **Go** wins on the scalability narrative and deployability, loses on iteration speed for a two-day build unless the author is already fluent.

Recommendation: **Python 3.12 with asyncio and stdlib only**, if the user is at least as fluent in Python as in TypeScript. Otherwise TypeScript on Node 24. The reasoning that changed from the first-pass recommendation: the first pass weighted "same runtime as the harness"; a fuller look at the work shows that parsing and generating RFC 822 correctly is the largest correctness risk, and Python's `email` package removes most of it. The evaluation criteria reward protocol correctness and candid tradeoffs, not toolchain uniformity.

Revisit trigger: the user is markedly more fluent in one language, or Python's `email` package turns out to alter bytes in a way we cannot control (mitigation: we never regenerate received messages, only drafts we render ourselves).

### D2. Dependencies

Options: (a) zero third-party runtime dependencies; (b) official AgentMail SDK; (c) SDK plus an HTTP client and a MIME library.

- (a) keeps the review surface small, avoids the "disclose copied code" burden, and makes the one-command start trivial. Cost: we write ~150 lines of HTTP wrapper (auth header, timeouts, pagination, 429 retry).
- (b) The SDK (npm `agentmail` 0.5.27, PyPI `agentmail`) retries 429 for us and has typed models. Cost: its request shapes are for production; we would need to confirm it tolerates the fake's differences (error body shape). It also hides the HTTP layer we want to instrument for tests (assert no auth on download URL). The raw download step is outside the SDK anyway.
- (c) heaviest; unnecessary if D1 picks Python.

Choice: **(a) zero runtime dependencies**, dev dependencies allowed for type checking and formatting. The API client is a thin typed wrapper we own and can test with the harness's failure injection.

Revisit trigger: hosted sandbox behaves in ways the SDK already handles (auth variants, regional hosts) and we are short on time.

### D3. Command parser

Options:
- (a) Line-at-a-time regex per command. Fast to write; breaks on literals, which span lines and contain arbitrary bytes, and on quoted strings containing spaces.
- (b) Incremental tokenizer over a byte buffer with an explicit continuation state for literals. Tokens: tag, atom, number, quoted string (with `\"` and `\\` escapes), literal `{N}`, parenthesized list, `[...]` section brackets for FETCH. Handlers receive an AST.
- (c) Grammar-generated parser from the RFC 3501 ABNF. Most correct; slowest to set up; the assignment permits deliberately rejecting valid forms, so full grammar coverage buys little.

Choice: **(b)**. Rationale: it is the minimum that is correct for `APPEND` and for `LOGIN` with quoted or literal credentials, and it isolates all byte handling in one module that can be unit-tested with malformed inputs (missing CRLF, bad `{N}`, oversized literal, NUL bytes, unterminated quote, garbage before tag).

Safety limits: max line length (e.g., 64 KiB), max literal (e.g., 1 MiB, matching the fake's request cap), idle timeout per connection (e.g., 30 min per RFC 3501 5.4), max concurrent connections. Exceeding a limit yields `BAD` or `BYE`, never a crash.

Revisit trigger: none expected within scope.

### D4. Session and concurrency model

Options: (a) one process, one event loop, one coroutine or callback chain per connection, commands executed strictly serially per connection; (b) thread per connection; (c) allow pipelined commands to execute concurrently.

Choice: **(a)**. IMAP allows a client to pipeline commands, but RFC 3501 5.5 lets the server process them in order, and most ambiguity (a `FETCH` racing a `SELECT`) disappears with serial execution. Per-connection state is small: authenticated identity (inbox id and key held in memory only), selected mailbox name and mode, the sequence-number snapshot (array of UIDs), pending untagged updates to flush at the next safe point. Mailbox and store state is shared across connections through the mailbox layer.

Revisit trigger: measured latency where serial execution matters (unlikely; a `FETCH` of one message is one or two API calls).

### D5. Mailbox model and label mapping **DECIDED: (b) and (b), sequenced after the basic slice**

Decision 2026-09-19: ship `INBOX` + `Drafts` with literal `received` first (working version), then hide `trash`/`spam` from INBOX, then add `Sent`, `Trash`, `Spam` views. Each step is a filter change on the same code path.

Sub-choice 1, which mailboxes exist:
- (a) `INBOX` and `Drafts` only. Minimum required.
- (b) Add `Sent`, `Trash`, `Spam` as label views. Matches AgentMail's own hosted IMAP, and the fake has fixtures for `sent` and `trash`. Once INBOX is a parameterized label view, each extra mailbox is a filter definition, not new code.
- (c) Expose every label as a mailbox. Labels are free-form and can be thousands; a mailbox per label is unbounded and semantically wrong (a message in five labels appears five times).

Recommendation: **(b)**, because it is nearly free and lets the multi-label fixture be shown somewhere sensible.

Sub-choice 2, INBOX membership:
- (a) `labels=received` literally. Includes `msg_multi_label` (received + trash).
- (b) `received` and not `trash` and not `spam`. The Gmail convention: trashing removes from the inbox; the multi-label fixture shows in `Trash` instead.

Recommendation: **(b)**. The API call is the same (`labels=received`); the exclusion is a local filter. Documented as a mapping rule.

Flag mapping:

| IMAP flag | Source | Settable? |
| --- | --- | --- |
| `\Seen` | message has `read` label, or lacks `unread` (define precedence: `unread` present means unseen) | Only if STORE is in scope (D12); then `PATCH add_labels/remove_labels` |
| `\Draft` | item is a draft | No |
| `\Flagged` | `starred` label (fixture has it) | Read-only unless STORE |
| `\Deleted` | never set by us; only meaningful with EXPUNGE (D12) | Only if in scope |
| `\Answered` | not derivable | never |
| `\Recent` | `0 RECENT` always; we cannot know "first session to see this" across restarts without more state | n/a |

`PERMANENTFLAGS` advertises only what STORE can actually persist; without STORE it is `()`, and the tagged SELECT response is still `[READ-WRITE]` because APPEND is allowed on Drafts. INBOX may be `[READ-ONLY]` if no STORE, which is honest and stops clients from trying.

Revisit trigger: hosted sandbox reveals label semantics that differ (e.g., `read` and `unread` both present).

### D6. UID allocation and persistence

Requirement recap (RFC 3501 2.3.1.1 plus the assignment): UIDs strictly ascending in arrival order, immutable within and across sessions, never reused, `UIDNEXT` only moves when items are added, `(mailbox, UIDVALIDITY, UID)` names one immutable message forever.

Allocation options:
- (a) Sequence-number-as-UID, regenerated each SELECT. Violates the assignment outright.
- (b) Hash of `message_id` truncated to 32 bits. Stable and stateless, but not ascending in arrival order, and collisions are possible. Violates "strictly ascending".
- (c) Persistent counter per mailbox with a map `remote_id -> uid`, allocating in deterministic order for newly seen ids. Satisfies every rule. Needs durable storage.

Choice: **(c)**.

Storage options for (c):
- JSON file per inbox, written with write-to-temp then atomic rename. Human-inspectable, zero dependencies, fine for one process. Multi-process safety requires a lock.
- SQLite (`node:sqlite` or Python `sqlite3`). Transactions give atomic allocation, multiple processes on one host are safe, schema is explicit. Slightly more code.
- Remote database. Overkill for the trial; see section 5 for when it is needed.

Choice: **SQLite** if the language has it in the stdlib (both candidates do), else JSON with atomic rename. Schema sketch: `mailbox(inbox_id, name, uidvalidity, next_uid)` and `uid_map(inbox_id, name, uid, remote_key, first_seen_at)` with `remote_key` unique per mailbox. Allocation is one transaction: read `next_uid`, insert rows, bump `next_uid`.

Ordering rule for newly seen ids within one sync: ascending `timestamp` (messages) or `updated_at` (drafts), tiebreak by id. Deterministic, so two servers with the same empty store and the same API state assign the same UIDs.

UIDVALIDITY: fixed at first creation of the mailbox record (Unix seconds at creation). It changes only if the store is missing or corrupt on start, in which case a new, larger value is written, and clients correctly discard caches.

Vanished items: an id that stops appearing in the API (permanently deleted message, sent draft) is removed from the live view and, in the selected session, reported as `* n EXPUNGE` at a safe point. Its UID is never reissued because `next_uid` is monotonic and the row is kept (tombstoned) for auditability.

**Draft mutability.** AgentMail drafts can be edited through the API. Our rendering of a draft is derived from its fields, so an edit changes the bytes. IMAP forbids the bytes behind a UID from changing.

Options:
- (a) Keep `draft_id -> uid` and let the bytes change. Simple. Clients that cached the old body show stale content; a client that cached `RFC822.SIZE` and then fetches a partial range may get inconsistent data. A documented RFC violation.
- (b) Key drafts by `(draft_id, content_hash)` where `content_hash` is a digest of the rendered bytes. An edit makes the old key vanish (an EXPUNGE from the client's view) and a new key appear with a new, higher UID. Fully RFC-compliant: every UID is immutable. Costs: UID churn on edits; client-side per-message state (local tags, "replied" markers) is lost on edit; the store grows one tombstone per edit; the fake API cannot update drafts, so this path is unit-tested only. Using a content hash rather than `updated_at` means label-only or no-op updates do not churn.
- (c) Bump `UIDVALIDITY` for Drafts on any change. Forces a full re-download of all drafts on every edit. Correct but hostile to clients.

Choice: **(b)**. It costs a composite key in the store and nothing else, and it is the option that lets us say "every UID is immutable" without an asterisk. Documented limitation: an edited draft appears as delete-plus-new.

Revisit trigger: a real client turns out to behave badly on frequent Drafts EXPUNGE/EXISTS churn.

Implementation notes after the adversarial review (section 8b):
- The rendered `Date` header comes from `created_at`, not `updated_at`, so a label-only or timestamp-only update does not change the bytes and therefore does not churn the UID. The original claim that "no-op updates cannot churn" was false while `Date` tracked `updated_at`.
- Content-hash keying does not by itself prevent deleting a newer version: `EXPUNGE` on a stale UID would have deleted the draft by id. Deletion now re-fetches the draft and refuses if its content no longer matches the UID's version. A GET-then-DELETE race remains because the API has no conditional delete.
- `\Deleted` marks are persisted in a `deleted_mark` table so that `PERMANENTFLAGS` advertising `\Deleted` is truthful: marks are shared across sessions and survive restarts, and are cleared when the item is expunged or vanishes.
- Items created through `APPEND` are pinned in the view for two minutes or until a listing shows them, because an upstream listing can lag a write; without the pin the freshly allocated UID would be tombstoned by the first stale listing.
- Corruption recovery triggers only on SQLite's own corruption diagnostics. A locked, read-only, or schema-incompatible store makes the server refuse to start. `UIDVALIDITY` is seconds-based; recreating a deleted store within the same second would reuse the value, and no watermark is kept outside the database.

### D7. Raw bytes and caching

Raw message bytes are immutable per `message_id` (received mail never changes). Options for `RFC822.SIZE` without fetching bytes:
- (a) Trust the list item's `size`. One fewer round trip on `FETCH (RFC822.SIZE)`. Risk: if production `size` is not exactly the `.eml` length, a client that allocates by size then reads `BODY[]` sees a mismatch. The fake makes them equal; the docs say "size of message in bytes" without defining which bytes.
- (b) Always derive size from the fetched bytes. Always consistent; costs a download on first metadata fetch of each message.
- (c) Use the raw endpoint's own `size` field (first step of the two-step) without downloading. Same trust question as (a) but from the endpoint whose job is the raw file.

Choice: **(b) with (c) as a pre-check**: return `size` from the raw metadata step when bytes are not cached, and log a warning if the downloaded length ever disagrees. In the fake they always agree; in production a disagreement would be a finding worth reporting.

Review outcome (superseded, see below): the first reviewer recommended reversing this for a correctness-focused slice. We initially kept the list `size` as the pre-download `RFC822.SIZE` (one fewer round trip on the metadata-only fetches clients issue for every message) but hardened the download path: a download whose length differs from the raw endpoint's `size` is an error, is not served, and is not cached; a redirect or any non-200 status is likewise an error. The residual risk is a client that reads `RFC822.SIZE` from the list value and then fetches a body of a different length; that would surface as the logged warning, and the list value would then be dropped in favour of the raw endpoint's.

Final decision after the second review: **reversed**. The second reviewer's framing settled it: a UID's reported size must never change, and using one source before download and another after could change it. `RFC822.SIZE` for a received message now comes from the raw endpoint's `size` (one metadata call per message, remembered), or from the bytes once downloaded; a download whose length disagrees with the remembered size is refused, not served. The list `size` is compared and logged only. Cost: one extra API call per message the first time a client asks for its size without its body. SEARCH `LARGER`/`SMALLER` use the same authoritative size.

Implementation note: the built server uses the list item's `size` for `RFC822.SIZE` when the bytes are not cached (one fewer round trip per message on the metadata-only fetches clients issue for every message), and logs a warning if the downloaded length differs from either the list `size` or the raw metadata `size`. Once bytes are cached, sizes always come from the bytes.

Cache: in-memory, keyed by `message_id`, LRU bounded by total bytes (e.g., 64 MiB). Rendered draft bytes cached by content hash. Presigned URLs are never cached beyond `expires_at`; in practice we do not cache them at all, we cache the bytes.

`BODY[HEADER]`, `BODY[TEXT]`, `BODY[HEADER.FIELDS (...)]`, partials `<start.len>`: computed by splitting cached bytes at the first empty line and slicing by byte offsets. `BODY.PEEK` variants never touch flags; non-PEEK fetches would set `\Seen` only if STORE semantics are in scope, otherwise they behave like PEEK and we document it.

### D8. Draft projections

**JSON to RFC 822 (FETCH on Drafts).** Render deterministically:

```
From: <inbox_id>
To: a@x, b@y                (each list joined with ", ")
Cc: ...                     (omitted if empty)
Bcc: ...                    (included: it is a draft the owner is reading, not a sent message)
Reply-To: ...
Subject: <RFC 2047-encoded if non-ASCII>
Date: <updated_at as RFC 5322 date>
Message-ID: <draft_id@inbox-domain>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit
X-AgentMail-Draft-Id: <draft_id>

<text with line endings normalized to CRLF>
```

Alternatives considered: omit `Bcc` (mail transport convention) versus include it. Including it is right for a draft view, because the assignment requires drafts to be inspectable without losing recipients. Base64 versus 8bit body encoding: 8bit keeps bytes readable and IMAP permits 8-bit literals; base64 would be needed only if a downstream expects 7-bit.

**RFC 822 to JSON (APPEND to Drafts).**
- Headers: unfold, case-insensitive names. `To`, `Cc`, `Bcc`, `Reply-To` parsed as address lists (comma-separated outside quotes and angle brackets; display names kept, so `Ada <ada@x>` is passed through as a string, which the API accepts). `Subject` RFC 2047-decoded.
- Body: `text/plain` in any charset with any transfer encoding, decoded to a UTF-8 string. `multipart/alternative` or `multipart/mixed` containing a `text/plain` part: take that part. Anything else (`text/html` only, real attachments): `NO` with a message naming the limitation, because the fake rejects `html` and `attachments` and the assignment excludes attachments in new drafts.
- Flags and date in the APPEND arguments are accepted and ignored (all drafts are `\Draft`; the API sets `updated_at`).
- Idempotency: pass `client_id` derived from a hash of the literal so a client retrying an APPEND after a network blip does not create a duplicate draft. The fake ignores `client_id`; production honors it.

Round-trip test: APPEND a literal, read `/_test/state`, assert `to`, `cc`, `bcc`, `reply_to`, `subject`, `text` equal the parsed inputs, then `SELECT Drafts`, `UID FETCH` the new UID, and assert the rendered message re-parses to the same fields.

### D9. Synchronization strategy

Options:
- (a) Sync on every command. Simple; hammers the API; `FETCH` of 100 messages would list the mailbox 100 times.
- (b) Sync on `SELECT`/`EXAMINE`, on `NOOP`, after our own `APPEND`, and otherwise at most once per N seconds when a command needs the live view. Untagged `EXISTS`/`EXPUNGE` announcements only at safe points (NOOP, or the end of a command that is not FETCH/STORE/SEARCH), per RFC 3501 5.2 and 7.4.1 constraints on renumbering.
- (c) Background poller per selected inbox pushing updates. Needed for IDLE; otherwise adds concurrency for little gain in the trial.

Choice: **(b)** with N around 5 seconds, matching AgentMail's own polling guidance. A per-inbox refresh is coalesced so several connections on the same inbox share one paginated listing.

Implementation notes: coalescing is per (inbox, mailbox), not per inbox; sequential forced NOOPs each re-list. A mutation (APPEND) bumps a per-mailbox generation so a forced sync never joins a listing that started before the mutation. A coalesced sync that fails with an authentication error is attributed to the credentials that ran it; a waiting session with different credentials re-runs the listing with its own client rather than inheriting the verdict. Each session keeps a per-UID baseline of the flags it last reported so NOOP can announce flag changes made by other sessions or upstream as untagged `FETCH FLAGS`.

Incremental sync: the list API supports `after=<timestamp>`, which would let us fetch only newer messages. Whether `after` compares against `timestamp` or `updated_at` is not stated; label changes on old messages would be missed if it is `timestamp`. For the trial, full listing is cheap (page size 2 in the fake, 50 in production). Section 5 covers when incremental sync matters.

### D10. Error handling and security

Mapping table:

| Condition | IMAP response |
| --- | --- |
| Unknown command, bad syntax, wrong state | `BAD` with a short reason; connection stays open |
| Literal too large, line too long | `BAD`; if the client keeps sending, `BYE` and close |
| 401 during LOGIN | `NO [AUTHENTICATIONFAILED] invalid credentials` (no detail on which part) |
| 401 mid-session (key revoked) | `* BYE credentials no longer valid`, close |
| 404 mailbox item vanished mid-fetch | skip that item (RFC: nonexistent UIDs are ignored), tagged `OK` |
| 429 | retry honoring `Retry-After` with jitter, bounded (e.g., 3 tries, 10 s total); then `NO [UNAVAILABLE] upstream rate limited` |
| 5xx, timeout, connection refused | `NO [UNAVAILABLE] upstream error`; the mailbox view is left as it was |
| APPEND parse failure | `NO` naming the missing or unsupported element |
| APPEND to a nonexistent mailbox | `NO [TRYCREATE]` |
| Bug in our code | caught at the command boundary, `NO internal error`, logged with a correlation id, connection preserved |

Secrets: the API key exists only in the session object and the `Authorization` header. Logs record `authorization_present: true/false`, never the value. Protocol error text never echoes arguments (so a mistyped LOGIN does not echo the key). The key is sent only to the configured API base URL; the download URL is fetched with a fresh client that has no default headers, and a test asserts this via `/_test/requests`.

Per-request HTTP timeout (e.g., 10 s) and a per-command overall budget so a slow upstream cannot hang the connection indefinitely; the harness's `delay_ms` injection tests this.

Review outcome: the mapping table stands, with these additions. Upstream response shapes are validated before reconciliation (a 200 without the expected collection is an error, not an empty mailbox). Only 2xx statuses count as success; downloads require exactly 200 and a matching length. Credentials are validated as printable ASCII before any header is built, so a malformed password can never appear in an HTTP library's exception text; the redaction filter also scrubs exception and stack text and is reference-counted across sessions. Every response line is sanitised of control characters, and error text never echoes client input, so client-supplied strings cannot inject protocol lines. The parser enforces per-command aggregate caps and the session an absolute assembly deadline, a bounded write timeout, and an exception boundary around parsing. The earlier claim that "protocol text never echoes arguments" was false until these changes. Known gap that remains: `Retry-After` is honoured only in seconds form. (An earlier gap, treating every mid-session 403 as a revoked key, was closed: 403 after login now means a missing permission and yields `NO [CANNOT]` without ending the session; 401 still ends it. Pod/organisation keys naming an inbox they cannot see get `AUTHENTICATIONFAILED` rather than a generic error.)

### D11. Testing strategy

Layers:
1. **Unit**: tokenizer and parser (valid forms, each malformed form, literal continuation across chunk boundaries, 8-bit and NUL bytes), RFC 822 header parsing and address splitting, both draft projections, UID store (allocation order, persistence across reopen, tombstones, UIDVALIDITY on corruption), response formatting (byte-length literals with the UTF-8 fixture).
2. **Integration**: start the fake API on an ephemeral port and our server on an ephemeral port in-process (or subprocess), drive a raw socket client through the acceptance path; assert `raw_sha256` equality against `/_test/state`, `RFC822.SIZE` equals literal length, no auth on `/raw/...`, complete pagination (all 4 received messages appear), `add-message` produces a new higher UID and an `EXISTS` on NOOP, restart the server and assert identical UIDs, inject 503 and latency and assert `NO` without hang, wrong password gives `NO`.
3. **Smoke**: a script in the README that starts both servers and runs the `nc`-style exchange from `MANUAL_SMOKE_TEST.md`, exiting non-zero on any unexpected response.
4. **Optional interop**: Thunderbird with the settings in `STANDARD_IMAP_CLIENT.md`, only after 1-3 pass.

Test-only endpoints of the fake are used by the integration tests, never by the server.

### D12. Additional capability **DONE: UID SEARCH, STORE (\Seen, \Flagged, \Deleted), EXPUNGE/UID EXPUNGE to trash, UIDPLUS, STATUS, ID, NAMESPACE, LSUB, SUBSCRIBE, CHECK**

Decision 2026-09-19: after the basic slice and the D5 sequence, implement `UID SEARCH` then `STORE \Seen`, then `\Deleted`/`EXPUNGE` to `trash`. Anything not reached is documented under "next steps". Thunderbird is a comprehensive end-to-end check run only after the automated tests pass.

Only after the core is reliable. Ranked by effort and by who benefits:

| Capability | Effort | Value to AgentMail | Value to a person using a client | Notes |
| --- | --- | --- | --- | --- |
| `ID`, `NAMESPACE`, `LSUB` stubs | XS | low | medium (clients stop complaining) | Needed for Thunderbird regardless |
| `STATUS` | XS | low | medium (unread counts) | Reuses the sync view |
| `LITERAL+` (RFC 2088) | XS | low | low | Lets clients skip the `+` handshake |
| `UIDPLUS` (`APPENDUID`) | S | medium (agent tooling that appends can find its draft) | medium | Tiny once APPEND resyncs |
| `AUTHENTICATE PLAIN` | S | low | medium | Many libraries prefer it to LOGIN |
| `UID SEARCH` (ALL, UNSEEN, UID set, SINCE/BEFORE, FROM/TO/SUBJECT substrings, TEXT) | M | medium (mirrors their `from/to/subject` filters and `search` endpoint) | **high**; clients need `UID SEARCH ALL` to enumerate a mailbox at all | Served locally over cached headers; `TEXT` needs bodies |
| `STORE` / `UID STORE` for `\Seen` | S-M | **high**: closes the loop on their read/unread label pattern, so a human reading in a client marks mail read and the agent's `labels=unread` filter skips it | high | Maps to one PATCH per changed message |
| `\Deleted` + `EXPUNGE` (and `CLOSE`) | M | **high**: maps to the `trash` label, which is exactly AgentMail's soft-delete semantics; for drafts maps to `DELETE .../drafts/{id}` | high (delete from a client) | Destructive path: never call the permanent message DELETE; the fake lacks draft delete, so the draft branch is unit-tested only |
| `MOVE` (RFC 6851) / `COPY` | M | medium (label add/remove between views) | medium | Semantics for label views are odd: copying INBOX to Trash means adding `trash`, which also removes it from INBOX |
| `IDLE` (RFC 2177) | M | medium (their production supports it; a future version could back it with webhooks) | high for a live demo | Needs the background poller from D9 option (c) |
| `BODYSTRUCTURE` / `ENVELOPE` | M (Python) / L (TS) | low | high for desktop clients | Python's `email` package makes this tractable |

Easiest wins: the XS/S rows. Most valuable to AgentMail: `STORE \Seen` and `\Deleted -> trash`, because they make IMAP a first-class participant in the label state machine their docs recommend. Most valuable to a user with a client: `UID SEARCH`, `STORE \Seen`, delete.

Recommendation: **`UID SEARCH` (ALL, UNSEEN, UID, SINCE/BEFORE, FROM/TO/SUBJECT) plus `STORE \Seen`** as the pair, then `\Deleted`/`EXPUNGE` to `trash` if time remains. The user's instinct toward search and delete is sound; search is what clients need to function, and delete has the cleanest story because AgentMail's own model is soft delete via label.

Design note on `SEARCH KEYWORD`: it matches AgentMail labels directly although labels are not exposed as IMAP keywords in `FLAGS`. The reviewer called this inconsistent; we keep it as a deliberate feature, since it is the one way an IMAP client can query the agent's own labels, and document it in the README. `TEXT` and `BODY` search decoded content (RFC 3501 6.4.4) after the review found them matching raw transfer-encoded bytes.

---

## 5. Scaling path: from a two-day gateway to millions of requests a day

First, calibrate: one million requests per day is about 12 per second on average, perhaps 100 per second at peak. A single event-loop process serves that if the expensive part, the upstream API calls, is cached and coalesced. The design question is not raw throughput; it is **where state lives and how upstream load is bounded**. The architecture in section 3 is drawn so each box can be swapped without touching the others.

### 5.1 What "requests" means here

Two distinct load sources:
- **IMAP commands** from clients: cheap when served from the sync view and byte cache, expensive when they force an upstream call.
- **Upstream API calls**: paginated listings on sync, raw downloads, PATCHes. These are rate-limited per organization and are the real bottleneck.

Goal: make upstream calls proportional to **changes in the inbox**, not to the number of connections or commands.

### 5.2 State placement

| State | Trial | 10x (one host, many inboxes) | 100x (many hosts) |
| --- | --- | --- | --- |
| Session (selected mailbox, seq snapshot) | in-process | in-process | in-process; IMAP connections are sticky by nature, L4 load balancing suffices |
| UID store | SQLite file | SQLite with WAL, one writer | Postgres or DynamoDB; UID allocation is a per-mailbox conditional increment (row lock or conditional write). Allocation is rare (only on new items), so contention is low |
| Sync view (id, labels, timestamp per item) | in-process per inbox | in-process, coalesced per inbox | Shared cache (Redis) keyed by inbox with a version stamp, or recomputed per host; any host can serve any inbox because the UID map is shared |
| Raw bytes | in-process LRU | in-process LRU plus on-disk cache | Shared object cache, or rely on S3 presigned URL locality; bytes are immutable so caching is trivially safe |
| Credentials | per session, memory only | same | same; never persisted; the gateway is a pass-through, not a credential store |

### 5.3 Bounding upstream load

1. **Coalescing**: one in-flight sync per inbox per host regardless of how many connections asked. The trial design already does this.
2. **Change-driven sync instead of polling**: AgentMail offers webhooks (`message.received`, etc.) and WebSockets. A hosted gateway subscribes per inbox and invalidates or applies deltas, dropping the per-connection poll to a heartbeat. This is the single biggest lever; it turns upstream cost from O(connections x poll rate) into O(events). AgentMail's own IMAP does this via an inbox event log.
3. **Incremental listing**: use `after=` with the last seen timestamp when the semantics are confirmed, otherwise a full list at a low cadence plus event-driven deltas for changes.
4. **Per-key budgets and backoff**: rate limits are per organization and the gateway uses each user's key, so a noisy inbox only harms its own organization. A token bucket per key, jittered retry on 429, and a circuit breaker per upstream host protect against retry storms.
5. **Batch endpoints**: `messages/batch-get` and `batch-update` reduce round trips for `STORE` on many messages and for metadata fills.

### 5.4 Horizontal scaling

- Stateless with respect to mailbox state once the UID store and byte cache are shared. Add hosts behind a TCP load balancer; connections stick to a host for their lifetime, which IMAP requires anyway.
- UID monotonicity across hosts is guaranteed by the store's atomic allocation, not by process memory. Two hosts syncing the same inbox at once both attempt to allocate for the same new ids; the store's unique constraint on `remote_key` makes one win and the other read back the winner's UID.
- Failure of a host drops its connections; clients reconnect and resume via UIDs, which is exactly what UID stability is for.

### 5.5 Tradeoffs made deliberately in the trial

- SQLite over a network database: right for one process, wrong for many hosts; the store interface is the seam.
- Polling over webhooks: webhooks need a public endpoint and are out of scope; the sync engine is the seam.
- Full listing over incremental: correctness first while `after` semantics are unverified.
- In-memory byte cache: fine at one host; the cache interface is the seam.

### 5.6 Claims not supported by the current code

The adversarial review checked this section against the implementation. The following are directions, not demonstrated properties: no throughput benchmark exists for the million-request figure; the HTTP thread pool has no admission deadline, so sixteen slow upstream requests delay an unrelated request beyond its own timeout; SQLite calls are synchronous on the event loop and can wait on locks; there is no token bucket, circuit breaker, or multi-host reconciliation protocol; cancelling a waiter leaves the shielded sync task running to completion, and there is no explicit join of in-flight syncs at shutdown; UID allocation and tombstoning are separate transactions. AgentMail's rate-limit guide describes limits per organization; the reviewer noted per-key wording elsewhere in the docs, so the per-key budget design below should be confirmed against the live documentation.

### 5.7 What we can demonstrate

Within the trial we can show: bounded and coalesced upstream calls (count requests in `/_test/requests` while many client connections hammer `NOOP`), correct behavior under injected 429/503/latency, and UID stability across a process restart. That is the evidence that the design would survive the scaling steps above.

---

## 6. Thunderbird and desktop-client interoperability

Thunderbird is Mozilla's free desktop email client, the most common independent IMAP client available for testing. `STANDARD_IMAP_CLIENT.md` gives settings for it. Its role here is an **external referee**: it exercises the protocol the way real software does, not the way our own tests do.

Advantages: catches formatting errors our tests would share blind spots on; a convincing live demo; validates UID/UIDVALIDITY handling because it caches aggressively and misbehaves visibly on violations.

Disadvantages: it issues many commands outside the slice (`ID`, `NAMESPACE`, `LSUB`, `STATUS`, `ENABLE`, `IDLE`, `UID SEARCH`, `BODYSTRUCTURE`, `BODY.PEEK[HEADER.FIELDS (...)]` with long field lists); it resists plaintext connections and needs security set to None with plaintext authentication allowed; debugging its behavior is a time sink; and it may create or expect folders (Trash, Sent, Archives) and try `CREATE` or `SUBSCRIBE`.

Position: scripted tests are the acceptance bar; Thunderbird is a stretch goal attempted only after the core is solid, with the stubs in D12's XS rows in place.

---

## 7. Hosted sandbox and production shapes

`API_RESOURCES.md` says a separate hosted sandbox may be provided during the interview. That means a real AgentMail base URL (production or staging), a real inbox, and a real API key, so our server would talk to the real service. Differences to expect versus the fake: `message_id` values shaped like `<...@agentmail.to>`, real S3 presigned HTTPS download URLs on a different host, production error bodies (`name`, `message`, `code`), real page sizes and tokens, real 429s with `Retry-After`, TLS on the API side (the IMAP side stays plaintext by the assignment).

Design implications already built in: base URL from configuration, no assumptions about page size or token format, ids treated as opaque, download URL treated as an opaque absolute URL fetched without credentials, status-code-based error handling, fields from the documented schema only. A smoke script gated by an environment variable would run the acceptance path against the hosted sandbox without committing any credential.

---

### 7.1 Verified against a real AgentMail inbox (2026-09-20)

Run against `api.agentmail.to` with a fresh free-tier inbox, an inbox-scoped key, three Gmail-sent messages (plain, Unicode subject and body, attachment) and drafts created through the API. The smoke script passed eleven of eleven steps; the hand-driven session exercised STORE, EXPUNGE to trash, the Trash view, and real draft deletion. Findings:

| Assumption | Result |
| --- | --- |
| Message ids are RFC Message-IDs (`<...@mail.gmail.com>`, containing `=`, `+`) | Confirmed; percent-encoded path segments accepted by every endpoint |
| List `size` equals raw `size` equals downloaded length | Confirmed on all three messages (6308, 19079, 6392 bytes) |
| Raw download is a presigned URL that needs no credentials | Confirmed: `https://cdn.agentmail.to/raw-messages/...`, 200, `message/rfc822`, `Content-Length` matches, one-hour expiry |
| Draft list items omit `text`, include `created_at` | Confirmed; new drafts also carry a `draft` label |
| Error body shape | `{name, code, message}` with `code: not_found` on 404; an invalid key yields **403** `{"message": "Forbidden"}`, not 401. Both are mapped to authentication failure |
| Inbox-scoped key | `/auth/me` reports `scope_type: inbox`; LOGIN takes the strict path |
| Label writes | `PATCH` returns the updated label list immediately |
| `DELETE /drafts/{id}` | Works; EXPUNGE in Drafts removed the draft and announced `* 2 EXPUNGE` |
| Trash view (`labels=trash&include_trash=true`) | Shows a message trashed by our EXPUNGE |
| **List endpoint consistency** | **Lags label writes by seconds.** A listing 0.4 s after a trash `PATCH` still returned the old labels; 60 s later it had caught up. The fake has no such lag. Consequence before the fix: `EXPUNGE` answered `OK` without `* n EXPUNGE`, and a `NOOP` right after `STORE` could flip a flag back briefly. Fix: labels we write are held locally over stale listings until the listing agrees or 60 s pass, mirroring the APPEND pin |
| Latency | LOGIN 110 ms, SELECT of 3 messages 160 ms, body download 370 ms, APPEND about 930 ms including its re-listing |

Not yet verified against production: a real 429 and its `Retry-After` format, permission-scoped keys (403 on specific operations), behaviour with more than one page of messages, and Thunderbird.

## 8. Risks and unknowns

- Byte-versus-character mistakes. Mitigation: bytes everywhere for message content, the UTF-8 fixture in every size assertion.
- Partial pagination. Mitigation: a pagination iterator in the API client that cannot return early; a test that counts.
- Non-deterministic UID assignment order. Mitigation: explicit sort, restart test.
- Draft edit churn (D6). Mitigation: content-hash keying, documented.
- Parser hang on an incomplete literal. Mitigation: idle timeout, size caps, chunk-boundary tests.
- Desktop client scope creep. Mitigation: explicit reject list, Thunderbird as stretch.
- Fake-versus-production drift. Mitigation: documented schema as the contract, fake as the test double, optional hosted smoke.
- Unverified `after=` semantics. Mitigation: full listing in the trial; noted in section 5.

---

## 8a. Delivery sequence (working version first, then build on it)

Status 2026-09-19: steps 0-12 complete, then an adversarial review and four fix batches (section 8b); about 260 automated tests passing, `scripts/smoke_all.sh` green. Step 13 (Thunderbird) not yet run; `ENVELOPE`/`BODYSTRUCTURE` remain unsupported and are the expected blocker there.

Each step has an automated test that passes before the next step starts, plus an `nc` check.

| Step | Builds | Verified by |
| --- | --- | --- |
| 1 | Tokenizer/parser | unit tests: quoted, literal, continuation across chunks, malformed forms |
| 2 | Listener, greeting, CAPABILITY, NOOP, LOGOUT | raw-socket integration test; `nc` |
| 3 | LOGIN via `/auth/me` | good/bad key, injected 503, key absent from logs |
| 4 | LIST, SELECT INBOX (literal `received`), UID store | full pagination count, UIDVALIDITY/UIDNEXT present |
| 5 | UID FETCH metadata, then BODY.PEEK[] / RFC822 | sha256 vs `/_test/state`, size == literal length on UTF-8 fixture, no auth on download URL |
| 6 | Persistence and new-mail detection | restart mid-test, identical UIDs; `add-message` gives higher UID and EXISTS on NOOP |
| 7 | Drafts SELECT/FETCH rendering | rendered message re-parses to fixture fields, `\Draft` set |
| 8 | APPEND Drafts | round-trip via `/_test/state`, new UID fetchable |
| 9 | Failure sweep | 429/503/latency on every path yields NO, no hang |
| 10 | README smoke script | the step 2-8 command sequence as a runnable script with pass/fail |
| 11 | INBOX hides trash/spam; Sent/Trash/Spam views | multi-label fixture appears in Trash only |
| 12 | UID SEARCH, STORE \Seen, then \Deleted/EXPUNGE to trash | per-capability tests; label PATCH observed in `/_test/state` |
| 13 | Thunderbird session | manual: folders, messages, drafts visible; compose-save creates a draft |

## 8b. Adversarial review and its outcome

After steps 0-12 an independent AI reviewer was given the repository, the design document, and the review prompt in `notes/adversarial-review-prompt.md`, with the fake API to probe. It produced 21 findings, each with a reproduction. All 21 were confirmed against the code. Their resolution:

| # | Finding | Resolution |
| --- | --- | --- |
| F1 | Expunging a stale draft UID deleted a newer upstream version | Version check before delete; refused with `NO` if content changed. Residual GET-then-DELETE race documented |
| F2 | Byte cache shared across inboxes | Cache keys namespaced by inbox and object kind |
| F3 | Malformed password reached a header and a traceback | Credentials validated as printable ASCII before use; redaction covers exception and stack text; reference-counted registry; secret kept registered until the failure is logged |
| F4 | A merely locked database was renamed as corrupt | Recovery only on SQLite corruption diagnostics; otherwise the server refuses to start; writability proven at open |
| F5 | UIDVALIDITY reusable within one second of recreating a deleted store | Initially documented only; fixed after the second review (R3) with a sidecar watermark file |
| F6 | APPEND answered `NO` after the draft was created | UID allocated from the create response; re-listing failure is logged, not reported |
| F7 | One command could accumulate unbounded literal data | Per-command byte and literal-count caps before continuation; absolute assembly deadline; both configurable |
| F8 | LIST wildcard regex backtracked catastrophically | Linear glob matcher; pattern length cap |
| F9 | A client that stopped reading could hang shutdown | Bounded drain with transport abort; bounded farewell at shutdown |
| F10 | A redirect body was served and cached as a message | Downloads require exactly 200 and a matching length; API calls require 2xx |
| F11 | A malformed 200 emptied the mailbox; duplicate ids duplicated sequence entries | Response shape validated; duplicates collapsed |
| F12 | APPEND joined a listing that predated the create | Generation barrier; created drafts pinned until listed |
| F13 | A revoked key's coalesced sync failure disconnected valid sessions | Failure attributed to the owning credentials; others retry with their own |
| F14 | Error text echoed arguments and allowed CRLF injection | All response text sanitised; fixed error wording; hostile tags not echoed |
| F15 | NOOP missed flag changes from other sessions | Per-session flag baseline; untagged `FETCH FLAGS` at safe points |
| F16 | CLOSE swallowed failed deletions and lost the marks | Marks persisted; CLOSE answers `NO` and stays selected on failure |
| F17 | TEXT/BODY searched raw transfer-encoded bytes; KEYWORD matched labels | Decoded search; KEYWORD kept as a documented feature |
| F18 | A 4301-digit literal length crashed the read loop | Digit cap; exception boundary around parsing |
| F19 | Undecodable APPEND bodies were saved with replacement characters | Strict decoding; refused |
| F20 | LF-only header filtering returned wrong fields | Header lines split on either terminator, bytes preserved |
| F21 | `BODY.PEEK[]` plus `BODY[]` dropped the `\Seen` side effect | Merge keeps the non-PEEK behaviour |

A second review of the fixed version found six more defects, all confirmed and fixed:

| # | Finding | Resolution |
| --- | --- | --- |
| R1 | APPEND still answered `NO` when the follow-up listing outlived the command deadline | The listing runs as a separate task; APPEND answers `OK` right after allocation and the session waits for the listing (bounded, shielded) only after answering, then announces `EXISTS` |
| R2 | A listing item without an id was skipped, so `{"messages": [{}]}` emptied the mailbox | An item without an id fails the whole sync; the previous view is kept |
| R3 | UIDVALIDITY reusable when the store is recreated in the same second | Sidecar watermark file records the highest value issued; new values are strictly larger even after loss, corruption, or clock rollback |
| R4 | The assembly deadline ignored a partially received command line | The deadline starts with the first buffered byte and resets at each complete command |
| R5 | A draft pinned after APPEND stayed visible after being deleted | Successful deletion drops the pin and bumps the mutation generation so an older listing cannot resurrect it |
| R6 | Unsolicited `FETCH FLAGS` during a UID command lacked `UID` | Unsolicited flag notifications always carry `UID` |

The second reviewer also re-raised two design points: `RFC822.SIZE` from the list field, now reversed (see D7), and `SEARCH KEYWORD` matching labels, kept as a documented feature.

Documentation claims the reviewer found overstated were corrected in the README: "message content never becomes `str`", "redacted from all log output", the FETCH modifier wording, keyword rejection status, the refresh-on-age claim, the download "no headers at all" wording, the stale-draft protection claim, and the fake's injection ordering in the notes. Two weak tests it identified were fixed (an assertion that ran after resetting the fake; a test that claimed to fault page two).

## 9. Open questions for the user

1. **Language**: Python 3.12 (recommended if fluency is equal) or TypeScript on Node 24? See D1.
2. **INBOX membership**: exclude `trash` and `spam` (recommended) or literal `received`? See D5.
3. **Extra mailboxes**: add Sent, Trash, Spam views (recommended)? See D5.
4. **Thunderbird**: stretch goal or required demo? See section 6.
5. **Extra capabilities**: `UID SEARCH` + `STORE \Seen`, then `\Deleted`/`EXPUNGE` to trash (recommended)? See D12.
6. **Hosted sandbox**: will one be available before the presentation? Affects whether the hosted smoke script is worth writing.

---

## 10. Glossary

- **RFC 822 / RFC 5322**: the format of an email as bytes: header lines, a blank line, a body. IMAP calls the whole thing `RFC822` or `BODY[]`.
- **MIME**: extensions that let a body contain multiple parts (text, HTML, attachments) separated by boundaries, with transfer encodings like base64 and quoted-printable.
- **RFC 2047 encoded-word**: how non-ASCII text is placed in headers, e.g. `=?UTF-8?B?...?=`.
- **UID**: a permanent integer for a message within a mailbox. **UIDVALIDITY**: a mailbox-level number that, when it changes, tells clients all their cached UIDs are void. **UIDNEXT**: the UID the next new message will get or exceed.
- **Sequence number**: a message's 1-based position in the mailbox at this moment; changes when messages are removed. UID commands avoid this fragility.
- **Literal**: `{N}` followed by exactly N bytes; the only way to send arbitrary bytes in IMAP.
- **Tagged / untagged response**: `A1 OK ...` completes command `A1`; `* 3 EXISTS` is data that can arrive any time.
- **Presigned URL**: a time-limited URL that grants download access without credentials; sending credentials to it is a leak.
- **Label view**: our term for a mailbox defined as "messages matching a label filter".
