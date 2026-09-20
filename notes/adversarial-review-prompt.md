# Adversarial review: imapgw (IMAP4rev1 gateway over the AgentMail API)

You are a senior reviewer doing a hostile, evidence-based review of a work-trial submission. Your job is to find real bugs, protocol violations, security problems, questionable design decisions, and gaps between what the documentation claims and what the code does. Assume the authors are competent and that the obvious things work; look for what they missed. Do not pad the report with style nits.

## Context you must read first, in this order

1. `ASSIGNMENT.md` — the specification and its rules. Pay special attention to: "preserve raw message bytes and use byte lengths", "do not silently truncate paginated API results", "UIDs stable across reconnects and process restarts; adding an item must not renumber or reuse existing UIDs", "handle malformed commands, invalid credentials, unsupported behavior, and API failures without crashing, hanging, or exposing secrets", and the rule "do not use a library that implements the IMAP server or protocol core".
2. `DESIGN.md` — every design decision with alternatives and rationale. Treat it as a set of claims to falsify.
3. `README.md` — what the server claims to support, reject, and map. Every claim there is testable.
4. `CLAUDE.md` and `notes/harness-observations.md` — how the fake AgentMail API behaves and where it differs from production.
5. `notes/agentmail-api.md` and `notes/imap-rfc3501.md` — the API schema and the RFC rules the implementation is held to. The primary sources are `https://www.rfc-editor.org/rfc/rfc3501` and `https://docs.agentmail.to/llms.txt` (append `.md` to any docs page for markdown). Consult them directly when the notes are not enough.
6. The code, all of it: `imapgw/*.py` (about 3,500 lines) and `tests/` (about 3,100 lines) plus `scripts/smoke.py` and `scripts/smoke_all.sh`.

## How to run things

```bash
uv run python -m unittest discover -s tests -t .     # 206 tests, ~40 s; spawns the fake API itself
scripts/smoke_all.sh                                  # end-to-end smoke: fake + server + scripted session
npm --prefix test-harness run api                     # fake API on http://127.0.0.1:3210/v0 (terminal 1)
uv run python -m imapgw                               # IMAP server on 127.0.0.1:1143 (terminal 2)
nc 127.0.0.1 1143                                     # hand-drive it; bare LF line endings are accepted
```

Sandbox credentials: inbox `candidate@imap.test`, key `test_agentmail_key`. The fake exposes unauthenticated test-control endpoints on `http://127.0.0.1:3210/_test/{reset,add-message,fail-next,state,requests}` (see `CLAUDE.md`). Use them to inject failures and latency and to inspect what the server sent upstream, including whether an `Authorization` header was present on each request.

You are encouraged to write throwaway scripts that open raw TCP sockets and send hostile input. Do not add dependencies. Do not modify files under `test-harness/`.

## What to attack

Work through every area below. For each, try to construct a concrete failing input or sequence. If you cannot, say what you tried.

### A. Protocol correctness against RFC 3501
- Tokenizer and literal handling (`imapgw/parser.py`): quoted-string escapes, `{N}` at odd positions, `{0}`, literals split across TCP reads one byte at a time, a literal announced inside a parenthesised list, NUL and 8-bit bytes in atoms and quoted strings, `]` and `[` in atoms, very long tags, empty tags, tags containing `+` or `*`, pipelined commands where the second arrives before the first is answered, a command line with only a tag.
- Response formatting (`imapgw/responses.py`, `imapgw/commands.py`): is every server literal's `{N}` an exact octet count? Are FETCH responses well-formed when several body sections are requested? Is `UID` included in every response to a `UID` command? Are `EXPUNGE` responses sent highest-sequence-first and never during FETCH/STORE/SEARCH? Are `EXISTS`/`EXPUNGE` ever emitted at a point where RFC 3501 5.2/7.4.1 forbid them? Is `RECENT` handling defensible?
- State machine (`imapgw/session.py`): commands in the wrong state, `SELECT` failing after a successful `SELECT` (RFC says the mailbox is deselected), `LOGIN` twice, `LOGOUT` mid-literal, `CLOSE` with `\Deleted` items on an `EXAMINE`d mailbox.
- Sequence numbers vs UIDs (`imapgw/fetch.py`, `commands.py`): `*` semantics in sets, `559:*` when 559 exceeds the largest UID, reversed ranges, sequence numbers after an `EXPUNGE` in the same session, `FETCH` by sequence number while another session has caused items to vanish.
- FETCH sections: `BODY[HEADER.FIELDS (...)]` with folded headers, duplicate header names, header names given as quoted strings, `HEADER.FIELDS.NOT`, partials with offsets past the end, `BODY[TEXT]` on a message with no blank line, `RFC822.HEADER`/`RFC822.TEXT` equivalences, the echoed section label (do clients such as Thunderbird match it?).
- SEARCH (`imapgw/search.py`): operator precedence of `NOT`/`OR`, nested groups, date parsing edge cases, `CHARSET` handling, keys that silently return wrong results rather than `BAD` (e.g. `NEW`, `OLD`, `RECENT`, `ANSWERED`).
- STORE/EXPUNGE: replace-mode `FLAGS (...)` semantics against non-permanent flags, `\Deleted` visibility across sessions, `UID EXPUNGE` when the UID is not `\Deleted`, partial failure mid-EXPUNGE (some items trashed, some not), CLOSE swallowing errors.
- APPEND: flags and date arguments, a literal containing only headers, a message with `Bcc` only, non-UTF-8 charsets, quoted-printable and base64 bodies, `multipart/alternative` with the plain part second, `multipart/mixed` with only a text part, `Content-Disposition: inline` attachments, 8-bit headers, CRLF vs LF bodies, a `Subject` with encoded words spanning folds. Does `APPENDUID` report the right UID? Is the mailbox left unchanged on every failure path?

### B. Byte handling
Find any place where message content passes through `str` and back, where `len()` is taken on text rather than bytes, where line endings are normalised in a way that changes what the client receives, or where an RFC 2047 or UTF-8 boundary could shift a size. The UTF-8 fixture (`msg_received_utf8`, 375 bytes) is the canary; try to construct another that breaks.

### C. UID and persistence invariants (`imapgw/uidstore.py`, `imapgw/mailbox.py`)
- Can two syncs (two sessions, or a sync racing an APPEND) allocate UIDs out of arrival order, skip, or duplicate? Is allocation actually atomic under SQLite WAL with `check_same_thread=False`?
- Can `next_uid` move without a new item, or fail to move with one?
- Can a vanished item ever be reissued its old UID? Does the partial unique index behave as intended when a key vanishes and returns in the same second?
- Is the sort key `(timestamp, id)` sufficient? What happens when two messages share a timestamp, when `timestamp` is missing or unparseable, when the API returns a timezone-offset timestamp, or when a message's `timestamp` is later edited upstream?
- Draft content-hash keying: is the rendering truly deterministic across processes and Python versions (dictionary order, `email` policy defaults, `Date` formatting, header folding)? A nondeterministic render would churn UIDs on every restart. Try rendering the same draft in two processes.
- `UIDVALIDITY`: is it 32-bit safe, monotonic on corruption recovery, and is the "delete DB" path guaranteed to increase it (it is seconds-based)?
- Multiple inboxes sharing one store file: any cross-inbox leakage?
- What happens if the SQLite file is read-only, locked, on a full disk, or if `rename` fails during corruption recovery?

### D. Concurrency and resource exhaustion
- One event loop, HTTP in a 16-thread pool: can a slow upstream saturate the pool and stall unrelated sessions? Is there any code path that blocks the loop (SQLite calls are synchronous by design; are any of them potentially slow)?
- Coalesced sync uses the first caller's API client (key) for everyone awaiting that inbox. Under what conditions is that wrong (revoked key, differently scoped keys for the same inbox)?
- `asyncio.shield` around the sync task: what happens when the awaiting session is cancelled or disconnects mid-sync?
- Memory: byte cache bound, per-connection buffers, literal cap, output buffering for large FETCH responses, connection limit. Can a client force unbounded growth (many small literals, pipelined FETCH of every body, many connections opened and left idle)?
- Slowloris: a client that sends one byte per idle-timeout interval.
- Shutdown: sessions mid-command, in-flight HTTP threads, executor shutdown with `cancel_futures`.

### E. Security
- Credential handling: every place the API key exists (session, client, headers, logs, exceptions, tracebacks, `repr` of objects, the fake's request log). The redaction filter replaces registered secrets in log messages: what about exception messages, `log.exception` tracebacks, messages logged before registration, or keys shorter than a few characters that would over-redact?
- Is the key ever sent anywhere other than `AGENTMAIL_API_URL`? Check redirects (the transport does not follow them, but verify), the download URL path, and error paths.
- Does the server echo any client-supplied bytes back in error text (LOGIN arguments, mailbox names, search strings)? Could that leak a password typed into the wrong field?
- Inbox allowlist (`AGENTMAIL_INBOX_ID`): bypasses via case, whitespace, Unicode normalisation, or literal-form arguments.
- The LOGIN check uses `/auth/me` and, for non-inbox-scoped keys, `GET /inboxes/{userid}`. Can a key for organisation A log in as an inbox it does not own? What does the server do with `scope_type` values it does not recognise?
- Plaintext by design (TLS out of scope), but are there any claims in the docs that overstate security?

### F. Assumptions about the AgentMail API that could be wrong in production
Compare `imapgw/apiclient.py` and `imapgw/mailbox.py` against `notes/agentmail-api.md` and the live docs.
- `size` in list items is used for `RFC822.SIZE` before bytes are cached. The docs say "size of message in bytes" without defining which bytes. Argue whether this is acceptable and what a client sees if it is wrong.
- `labels=trash&include_trash=true` for the Trash view; `labels=received` for INBOX with local exclusion of `trash`/`spam`. Does production apply `include_trash` semantics to label-filtered queries the same way the fake does?
- Pagination: `limit=100` requested; is 100 within production limits for every endpoint used? Is `next_page_token` guaranteed opaque and stable?
- Draft list items lack `text`; the server GETs each draft once per `updated_at`. Could `updated_at` fail to change on an edit (or change without an edit) in production, and what would each do to UIDs?
- `message_id` format differences (`msg_...` in the fake, `<...@agentmail.to>` in production) in URL path segments, `remote_key` storage, and the `Message-ID` header of rendered drafts.
- Error body shapes, 401 vs 403 semantics for scoped keys, `Retry-After` formats (seconds vs HTTP-date), 429 on the download URL (S3 does not send `Retry-After`).
- The Drafts API `create` fields the server sends (`to`, `cc`, `bcc`, `reply_to`, `subject`, `text`, `client_id`): any that production rejects when empty or that the fake accepts but production would not?

### G. Design decisions to question (see `DESIGN.md` D1-D12 and section 5)
For each, say whether you agree, and if not, what you would do instead and why:
- Hiding `trash`/`spam` from INBOX locally rather than relying on the API's default exclusion.
- Draft UID keyed on content hash (delete-plus-new on edit) versus stable UID with changing bytes.
- Full re-listing on every SELECT/NOOP versus incremental sync with `after=`.
- `RFC822.SIZE` from the list `size` field.
- `\Deleted` as session-local state; EXPUNGE mapped to the `trash` label; no `\Deleted` in Trash/Spam.
- Non-PEEK body fetches setting `\Seen` via a PATCH (a read in a client changes the agent-visible label state; is that desirable?).
- Accepting bare LF line endings.
- `RECENT` always 0 and no `\Recent`.
- Zero third-party dependencies (versus the official SDK).
- The scaling story in section 5: which claims are unsupported by the current code?

### H. Test quality
- Which README claims have no test? Which tests would pass against a wrong implementation (weak assertions, asserting only status codes)?
- Are any tests timing-dependent or order-dependent in a way that will flake on a slow machine or CI? Look at the failure sweep, the slow-upstream tests, and the seconds-based UIDVALIDITY test.
- Do the integration tests share state through the fake in a way that could mask bugs (`reset()` between tests, `fail_next` entries left behind)?
- Does `tests/support/imap_client.py` parse the protocol leniently enough to hide server bugs (e.g. accepting malformed FETCH responses, not checking CRLF, not checking that untagged data precedes the tagged line)?
- Is anything in `tests/` or `scripts/` an IMAP protocol library in disguise, which the assignment forbids?

### I. Documentation accuracy
Read README and DESIGN as a hostile evaluator. Flag every statement that is untrue, overstated, unverifiable, or that hides a limitation. Flag missing disclosures.

## Output format

Produce a single report with these sections:

1. **Summary**: three to five sentences on overall quality and the most important findings.
2. **Findings**, ordered by severity, each with:
   - Severity: Critical (data loss, security, protocol violation a real client would hit) / High / Medium / Low.
   - Title (one line).
   - Location: `file:line` references.
   - Evidence: the exact input, command sequence, or code path, and what happens versus what should happen. Include a reproducible snippet where you can (an `nc` transcript, a Python one-liner against the parser, a test you wrote).
   - Suggested fix, and its cost.
3. **Design challenges**: decisions you would reverse or amend, with the argument.
4. **Test gaps**: concrete tests that should exist, as one-line descriptions.
5. **What you tried that did not break**: so the authors know what was covered.

Rules: every finding needs evidence, not speculation. If you believe something is a bug but could not reproduce it, label it "unconfirmed" and say what would confirm it. Do not report style, naming, or formatting. Do not report things the assignment declares out of scope (TLS, deployment, attachments in new drafts, draft sending, production compatibility) unless the docs make a claim about them.
