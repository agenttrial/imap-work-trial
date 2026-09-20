# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A work-trial workspace containing `imapgw`, a small **IMAP4rev1 server that fronts the AgentMail REST API**, written in Python 3.12 with no third-party runtime dependencies. The spec is `ASSIGNMENT.md`; `API_RESOURCES.md` links the official docs; `DESIGN.md` records every design decision with alternatives; `README.md` documents supported behavior, mapping rules, and limitations. `test-harness/` is a deterministic fake of the AgentMail API provided with the assignment (do not modify it).

Hard rule from the assignment: no IMAP server/protocol library anywhere, and the user also ruled out `imaplib` even in tests. Tests drive the server with `tests/support/imap_client.py`, a hand-written raw-socket client. Python's stdlib `email` package is used for RFC 822/MIME only (APPEND parsing, draft rendering, SEARCH header decoding) and is disclosed in the README.

Hard rules from the assignment that shape every design choice:

- Use only the public AgentMail API (or official SDK). No library that implements the IMAP server/protocol core.
- `INBOX` = received AgentMail messages; `Drafts` = the AgentMail Drafts API. `\Seen` maps to read state, `\Draft` marks drafts.
- Preserve raw message bytes; IMAP literal lengths and `RFC822.SIZE` are **byte** counts, not character counts.
- Never silently truncate paginated API results (the harness forces multi-page responses; see below).
- UIDs must be stable across reconnects and process restarts, never renumbered or reused.
- Out of scope: TLS, deployment, attachments in new drafts, sending drafts, production compatibility.
- Never commit credentials or raw `Authorization` headers.

## Commands

```bash
uv run python -m imapgw                                   # start the IMAP server on 127.0.0.1:1143 (reads .env)
npm --prefix test-harness run api                         # start fake AgentMail API on http://127.0.0.1:3210/v0
uv run python -m unittest discover -s tests -t .          # full suite (~260 tests, ~45 s; spawns the fake itself)
uv run python -m unittest tests.unit.test_parser          # one module
uv run python -m unittest tests.integration.test_append.AppendTests.test_append_round_trips_through_the_api
scripts/smoke_all.sh                                      # end-to-end smoke: fake + server + scripted session
uv run ruff format imapgw tests scripts && uv run ruff check imapgw tests scripts
npm --prefix test-harness test                            # the harness's own tests
```

Toolchain: `uv` (installed via brew) manages Python 3.12 and `.venv`; system Python is 3.9 and will not run the code. Node 20+ for the fake. Sandbox credentials are in `.env.example` (inbox `candidate@imap.test`, key `test_agentmail_key`).

## Code layout (imapgw/)

`parser.py` (incremental tokenizer, literals) → `session.py` (per-connection state machine, dispatch, error mapping) → `commands.py` (one handler per verb, `HANDLERS`/`UID_HANDLERS` tables) → `mailbox.py` (label views, flag mapping, sync, UID allocation, mutations) → `apiclient.py` (HTTP in a thread pool, retries, pagination, credential-free download). Leaf modules: `responses.py` (encoders, bytes-only literals), `fetch.py` (FETCH grammar, sequence sets, section slicing), `search.py`, `drafts.py` (RFC 822 ↔ draft JSON), `uidstore.py` (SQLite), `bytecache.py`, `config.py` (settings, `.env`, log redaction). `commands.py`/`session.py` never touch HTTP; `mailbox.py` is the only module that knows both IMAP and AgentMail.

Tests: `tests/unit` (no network), `tests/integration` (subclass `GatewayTestCase` from `tests/support/gateway.py`: one fake API per class, fresh server + temp SQLite per test, `self.fake.fail_next/add_message/state/requests` for control). Integration assertions rely on fixture facts: INBOX UIDs 1-3 are ascii/utf8/attachment in timestamp order (the trashed `msg_multi_label` is hidden), UTF-8 fixture is 375 bytes, Trash has 2, Sent 1, Spam 0, Drafts 2. Tests named after review findings (F1-F21) are regressions from the adversarial review recorded in DESIGN.md section 8b; keep them passing.

Invariants the review made explicit: `\Deleted` marks live in the `deleted_mark` table (shared across sessions, survive restarts); the UID store refuses to start rather than recover unless SQLite reports corruption; APPEND allocates its UID from the create response and pins the draft until a listing shows it; every response line passes through `responses.clean()`; downloads require exactly 200 and a matching length; the byte cache is keyed by inbox.

## Fake API behavior that matters (test-harness/fake-agentmail-api.mjs)

The fake follows the public API shapes but is deliberately adversarial in a few ways:

- **Tiny page sizes.** Messages list caps `limit` at 2, drafts list at 1, regardless of what you request. Every listing must follow `next_page_token` to completion.
- **Label filtering is AND-of-all-labels and bypasses trash/spam exclusion.** With `?labels=received`, `msg_multi_label` (labels `received,trash,unread`) is returned. Without a `labels` filter, trash/spam are excluded unless `include_trash`/`include_spam=true`.
- **Raw bytes are a two-step fetch.** `GET /inboxes/{id}/messages/{mid}/raw` returns JSON with `download_url`; the actual `message/rfc822` bytes come from that URL and require **no auth header** (mirrors S3 presigned URLs). Do not send the API key to the download URL.
- **Draft list items omit `text`**; `GET /inboxes/{id}/drafts/{did}` includes it. Draft creation rejects `html` or `attachments` with 400.
- **Auth** accepts `Authorization: Bearer <key>` or `x-api-key: <key>`. Errors are `{ "error": { "message" } }` (production uses `{ name, message, code, fix, docs }`; do not depend on either shape).
- `message_id` values are opaque strings (`msg_received_ascii`), distinct from the RFC `Message-ID` header. In production `message_id` is typically the angle-bracketed Message-ID itself.
- Fixture data lives in `test-harness/lib/fixtures.mjs`: 6 base messages (ASCII, UTF-8 with RFC 2047 headers and an emoji body, multipart with attachment, sent, trash-only, received+trash) and 2 drafts (one with to/cc/bcc/reply_to). The UTF-8 fixture exists specifically to catch character-vs-byte length bugs.

Unauthenticated test-control endpoints (root, not under `/v0`), for automated tests of the IMAP server:

| Endpoint | Purpose |
| --- | --- |
| `POST /_test/reset` | restore baseline fixtures |
| `POST /_test/add-message` | add `msg_new_arrival` (tests UID assignment / UIDNEXT) |
| `POST /_test/fail-next` `{method, path_prefix, status, delay_ms, times}` | inject an API failure or latency |
| `GET /_test/state` | dump messages (with `raw_sha256`) and drafts, for asserting an APPEND round-trip |
| `GET /_test/requests` | last 100 requests with `authorization_present` flag (verify no key leaks to download URLs) |
| `GET /health` | liveness |

## Reference notes

`notes/` holds condensed local copies of the linked documentation (AgentMail Messages/Drafts/Inboxes/Labels guides and endpoint schemas, RFC 3501 sections relevant to the slice, and harness observations) so design decisions can be checked without re-fetching. Official docs remain the source of truth; clean markdown of any docs page is available by appending `.md` to its URL.
