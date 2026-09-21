# Handoff: Thunderbird interoperability for imapgw

You are picking up an existing, working project. Read this whole brief, then the files it names, before touching anything.

## The project

`~/Desktop/harsh/imap-work-trial` is an AgentMail work trial: `imapgw`, an IMAP4rev1 server written in Python 3.12 (standard library only) that fronts the AgentMail REST API. It presents an AgentMail inbox as the mailboxes `INBOX`, `Drafts`, `Sent`, `Trash`, `Spam`, with persistent UIDs in SQLite, byte-exact FETCH, APPEND to Drafts, UID SEARCH, STORE, and EXPUNGE mapped to the trash label. It is complete for scripted clients: 283 tests pass, the smoke script passes, and it has been verified against a real AgentMail inbox. Two adversarial reviews (27 findings) have been fixed. Everything is committed on `main` of `https://github.com/agenttrial/imap-work-trial` (fork of the trial repo); the latest commit at handoff is `f1d5fa1`.

Read in this order: `CLAUDE.md` (commands, layout, invariants), `README.md` (supported behaviour, mapping rules, limitations), `DESIGN.md` (every decision with alternatives; section 8b lists the review findings and fixes; section 7.1 what production confirmed), `notes/imap-rfc3501.md`, then the code under `imapgw/` and the tests under `tests/`.

## Hard constraints (do not relitigate)

- Python 3.12, **no third-party runtime dependencies**. Run everything with `uv run ...`.
- **No IMAP library anywhere**, not in the server and not in tests, and **not `imaplib`** even for testing. Tests use the hand-written raw-socket client in `tests/support/imap_client.py`. Python's stdlib `email` package is allowed and already used for RFC 822/MIME work (this is disclosed in the README).
- Working version first, then increments. Every step lands with tests; run `uv run python -m unittest discover -s tests -t .` and `IMAP_PORT=11430 ./scripts/smoke_all.sh` (port 1143 may be busy with a real-API server) before calling anything done. `uv run ruff format imapgw tests scripts && uv run ruff check imapgw tests scripts` must be clean.
- Never print, log, or commit the API key. `.env` holds real credentials and is gitignored; do not `cat` it. The user pasted keys into a previous chat and has been told to rotate them; if `.env` still holds a key that starts `am_us_inbox_f9f4`, tell the user it must be rotated.
- Do not modify `test-harness/` (provided fake API).
- The user wants questions answered first, then plans, then code. Explain what a fix does and why in plain terms; they are new to the mail domain. Commit only when they say so, with the message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Work on a branch named `thunderbird` to avoid colliding with another session that may be writing documents in the same checkout. Rebase on `main` before proposing a merge.

## Your task

Make a real desktop client, Thunderbird, work against imapgw using `STANDARD_IMAP_CLIENT.md` (host 127.0.0.1, port 1143, no TLS, normal password, username = inbox address, password = API key). Expected blockers, in likely order:

1. **`FETCH` items `ENVELOPE`, `BODYSTRUCTURE`, and `BODY` (structure)**. Currently rejected with `BAD unsupported fetch item` in `imapgw/fetch.py`. Thunderbird requests these for the message list. Implement them from the cached raw bytes using `email.message_from_bytes(policy=email.policy.default)`: ENVELOPE per RFC 3501 7.4.2 (date, subject, from, sender, reply-to, to, cc, bcc, in-reply-to, message-id, with `((name adl mailbox host))` address lists and NIL where absent, encoded-words passed through undecoded), BODYSTRUCTURE as the full recursive structure with the extension fields, BODY as the non-extensible form. Every string must be emitted through `responses.quoted`/`nstring` so byte counts and escaping are right.
2. **Numbered MIME sections**: `BODY[1]`, `BODY[1.2]`, `BODY[1.MIME]`, `BODY[2.HEADER]`, `BODY[1.TEXT]`, all with `.PEEK` and `<offset.count>`. Currently rejected in `fetch._parse_section`. Slice the actual bytes of the part (not a re-serialisation) so lengths stay exact; the `email` package gives structure, you compute byte offsets against the raw message (boundaries) or re-serialise only when you can prove the bytes are unchanged.
3. Macros `ALL` and `FULL` (need ENVELOPE / BODY).
4. Anything else Thunderbird sends: `IDLE` (advertise only if you implement it; polling with NOOP is acceptable and documented), `ENABLE`, `COMPRESS`, `MOVE`, `SEARCH` keys not yet supported. Reject unknowns cleanly; never crash or hang. Watch the server log at `IMAPGW_LOG_LEVEL=DEBUG` for `BAD` responses to see what it asked for.

Fixtures for tests: the fake API's `msg_received_attachment` is `multipart/mixed` with a text part and a base64 `hello.txt` attachment (see `test-harness/lib/fixtures.mjs`); `msg_received_utf8` has RFC 2047 headers. Use them for ENVELOPE/BODYSTRUCTURE/section tests, plus hand-built raw messages for nested multiparts and `message/rfc822` parts. The real inbox `harshworktrial@agentmail.to` (credentials in `.env`, read by the server automatically) has a Gmail-sent multipart message with an attachment; the user runs Thunderbird and the manual checks themselves and pastes output to you.

Test conventions: unit tests in `tests/unit` (no network), integration tests subclass `GatewayTestCase` in `tests/support/gateway.py` (one fake API per class, fresh server on port 0 and temp SQLite per test; `self.fake.fail_next/add_message/state/requests` for control; note `self.fake.reset()` deletes drafts created during a test). Fixture facts: INBOX UIDs 1-3 are ascii/utf8/attachment in timestamp order (the trashed `msg_multi_label` is hidden), the UTF-8 fixture is exactly 375 bytes, Trash has 2, Sent 1, Spam 0, Drafts 2.

Invariants you must not break (all have tests): every size and literal is an octet count; `RFC822.SIZE` for a UID never changes; UIDs are never reused; a failed or malformed listing keeps the previous view; `\Deleted` marks live in SQLite; APPEND allocates its UID from the create response; every response line passes through `responses.clean()`; error text never echoes client input; downloads send no auth header and must be exactly 200 with a matching length.

Start by reading the files, then give the user a short plan (which of the four blockers you will do first and how you will test each) and wait for their go-ahead.
