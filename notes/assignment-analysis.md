# Assignment analysis

Written 2026-09-19 after reading the repo, the four linked AgentMail doc pages plus the endpoint references and related guides, RFC 3501, and probing the fake API. Timelines in `ASSIGNMENT.md` are ignored per the user's instruction.

## The problem in one paragraph

AgentMail exposes email as REST resources: Inboxes, Messages with free-form labels, and Drafts. Ordinary email clients and libraries speak IMAP, a 1990s stateful line protocol with folders, sequence numbers, UIDs, and flags. The task is a **protocol adapter**: a TCP server that speaks enough IMAP4rev1 that a client can log in with an inbox id and API key, see two folders (`INBOX`, `Drafts`), download messages byte-for-byte, view existing drafts, and save a new plain-text draft, while the server translates every step into AgentMail REST calls. The hard parts are not the REST calls; they are the impedance mismatches between the two models.

## The impedance mismatches (why this is interesting)

| IMAP assumes | AgentMail provides | Consequence |
| --- | --- | --- |
| Exclusive folders; a message is in exactly one | Labels; a message can carry `received` and `trash` at once | Must define a label-to-mailbox mapping and document edge cases (`msg_multi_label`) |
| Per-message 32-bit UIDs, strictly ascending in arrival order, stable forever | Opaque string ids (`message_id`, `draft_id`), listing newest-first | Server must mint and **persist** its own UID map per mailbox, and never reuse |
| A `\Seen` flag | `read`/`unread` labels; `unread` on new mail | Flag = label projection; changing a flag = PATCH labels (if STORE is in scope) |
| Full RFC 822 bytes on demand, exact sizes | Two-step raw download (presigned URL, expiring), plus a `size` field | Fetch, cache within expiry, compute sizes from real bytes |
| Drafts are RFC 822 messages | Drafts are structured JSON (`to[]`, `cc[]`, `subject`, `text`) | Two lossy projections: JSON to RFC 822 for FETCH, RFC 822 to JSON for APPEND |
| One server holds the mailbox; new mail appears via EXISTS | Pull-only REST (webhooks/websockets exist but are out of scope) | Poll on NOOP/SELECT; reconcile, assign UIDs to new ids |
| Synchronous, per-connection, in-order | Paginated, rate-limited HTTP with transient errors | Complete pagination, retry 429 with backoff, map failures to `NO` without hanging or crashing |
| Line-based commands with embedded byte literals | n/a | Hand-written incremental parser that handles `{N}` + continuation (no IMAP library allowed) |

## Objectives, restated as acceptance checks

1. One documented start command; server listens on `127.0.0.1:1143`.
2. `nc` or a script can `LOGIN "candidate@imap.test" "test_agentmail_key"` and get `OK`; a wrong key gets `NO` and no stack trace, no key in logs.
3. `LIST "" "*"` shows `INBOX` and `Drafts`; `SELECT` either works and emits `FLAGS`, `EXISTS`, `RECENT`, `UIDVALIDITY`, `UIDNEXT`, `PERMANENTFLAGS`, `UNSEEN`.
4. `UID FETCH 1:* (UID FLAGS RFC822.SIZE INTERNALDATE)` lists all 4 received messages (or 3, depending on the trash decision) across the harness's forced pagination; `UID FETCH n BODY.PEEK[]` returns bytes whose sha256 equals `raw_sha256` from `/_test/state`, and `RFC822.SIZE` equals the literal length (375 for the UTF-8 fixture, not the character count).
5. `SELECT Drafts` shows both fixture drafts with `\Draft`; fetching one yields an RFC 822 rendering with To/Cc/Bcc/Reply-To/Subject and the text body.
6. `APPEND Drafts (\Draft) {N}` with a plain-text RFC 822 message creates a draft; `/_test/state` shows the same recipients, subject, and body; a subsequent `SELECT Drafts` shows it with a new, higher UID; the old UIDs are unchanged.
7. `LOGOUT` yields `* BYE` and a closed socket. Restart the server; UIDs are identical.
8. Malformed input (`FOO`, missing tag, bad literal length, oversized literal, FETCH before SELECT) yields `BAD`/`NO`, connection stays healthy. Injected 503/timeouts surface as `NO` (or `BYE` if unrecoverable) without hanging.

## Decisions to make before coding

### Language and dependencies
The harness is Node; Node 24 is installed, Python is 3.9 (old, no modern typing features), no Go. Node/TypeScript is the natural fit: same runtime as the harness, `net` module for TCP, built-in `node:test`, zero third-party deps possible. Python's `email` package is a tempting MIME parser but Python 3.9 is dated. Recommendation: **TypeScript on Node, no runtime dependencies**, or plain modern JS if type tooling is deemed overhead. Confirm with user.

### Parser and session state
Byte-buffer accumulator per connection; a tokenizer that understands atoms, quoted strings, parenthesized lists, and synchronizing literals; a state machine `NotAuthenticated -> Authenticated -> Selected -> Logout`. Commands processed serially per connection. Cap on literal size (e.g. 1 MB, matching the harness's request cap) and on line length to avoid memory abuse.

### Mailbox model
Options for what beyond INBOX/Drafts to expose:
- Minimal: `INBOX`, `Drafts` only.
- Match production AgentMail IMAP: add `Sent`, `Trash`, `Spam` as label views (`sent`, `trash`, `spam`). Cheap once INBOX exists because they are the same code path with a different label filter, and it is a defensible choice because AgentMail itself does it.
INBOX definition choice: `labels=received` verbatim (includes trashed `msg_multi_label`) vs `received` minus `trash`/`spam` (Gmail-like). Recommendation: exclude `trash` and `spam` from INBOX and document it; show `msg_multi_label` in `Trash`.

### UID persistence
Requirement: stable across restarts, never reused, adding never renumbers. Design: per-mailbox table `(mailbox, uidvalidity, next_uid, {remote_id -> uid})` persisted to a small JSON or SQLite file keyed by inbox id. New remote ids get `next_uid++` in a deterministic order (e.g. ascending `timestamp`, tiebreak by id). UIDVALIDITY is fixed at creation (e.g. a timestamp) and only bumps if the store file is lost or corrupt. Ids that disappear from the API (deleted, or draft sent) are dropped from the live view but their UIDs are never reissued. Question: SQLite (`node:sqlite` is built in on Node 22+) vs JSON file with atomic rename. Both viable; JSON is simpler and inspectable.

### Draft to RFC 822 projection (FETCH on Drafts)
Synthesize: `From: <inbox_id>`, `To`, `Cc`, `Bcc`, `Reply-To` (joined with `, `), `Subject` (RFC 2047 encode if non-ASCII), `Date` from `updated_at`, `Message-ID: <draft_id@inbox-domain>`, `MIME-Version: 1.0`, `Content-Type: text/plain; charset=utf-8`, `Content-Transfer-Encoding: 8bit`, blank line, `text` with CRLF line endings. Store the exact rendered bytes alongside the UID so `RFC822.SIZE` and `BODY[]` agree and the message stays immutable per UID. Open question: if a draft is edited via the API, its bytes change while its UID must not; options are to key UIDs on `(draft_id, updated_at)` so an edit appears as a new message, or to accept the violation and document it.

### RFC 822 to draft projection (APPEND)
Parse headers (unfold, case-insensitive), take `To`/`Cc`/`Bcc`/`Reply-To` as address lists (split on commas outside quotes/angle brackets), `Subject` (decode RFC 2047), and the body. Accept `text/plain` (any charset, decode `quoted-printable`/`base64` transfer encodings to UTF-8) and `multipart/alternative` by picking the `text/plain` part. Reject `text/html`-only or attachments with `NO` and a clear message, since the harness returns 400 for `html`/`attachments` and the assignment says attachments in new drafts are out of scope. After POST, refresh the Drafts UID map so the new draft gets a UID; optionally report it via `APPENDUID` if UIDPLUS is chosen.

### Synchronization and caching
Refresh a mailbox's listing on SELECT and on NOOP (and optionally at most every N seconds), paginating fully. Cache raw message bytes in memory keyed by `message_id` (immutable per UID) with a size bound; drafts rendered bytes cached per UID. Do not cache the presigned URL past `expires_at`. Report new arrivals as `* n EXISTS` on NOOP.

### Error handling and security
Map: 401 at LOGIN -> `NO [AUTHENTICATIONFAILED]`; 401 mid-session -> `BYE`; 404 -> `NO`; 429 -> retry with `Retry-After` up to a bound, then `NO`; 5xx/timeouts -> `NO` with a generic message. Never include the API key or `Authorization` header in logs or protocol text. Per-request HTTP timeout. Only send the key to the configured API base URL, never to `download_url`.

### Tests
Unit: tokenizer/parser (quoted, literal, continuation, malformed), RFC 822 header parser, draft projections both ways, UID store persistence. Integration: spin up the fake API on port 0 and the IMAP server on port 0 in-process, drive a raw TCP client through the acceptance checks above, use `/_test/*` to inject failures and add messages, assert `raw_sha256` equality and `authorization_present` on the raw URL. A shell smoke test with `nc` or a tiny script for the README.

### The one extra capability (only if the core is solid)
Candidates ranked by value-to-effort: `UIDPLUS` (`APPENDUID`, small, directly improves the APPEND story), `STORE`/`UID STORE` for `\Seen` (maps to the documented read/unread PATCH; makes the flag mapping bidirectional), `SEARCH ALL`/`UID SEARCH` (Thunderbird uses it constantly), `IDLE` (nice demo, needs polling loop), `STATUS`.

## Risks

- Byte-vs-character mistakes in literal lengths (the UTF-8 fixture is a trap). Use `Buffer` everywhere for message bytes.
- Partial pagination (harness forces it).
- UID instability across restarts if the store is not written atomically or ordering is not deterministic.
- Draft edits mutating bytes under a fixed UID.
- Parser hangs on a literal whose announced length never arrives; need per-connection timeouts and size limits.
- Desktop clients (Thunderbird) issue many commands outside the slice (`ID`, `NAMESPACE`, `LSUB`, `STATUS`, `IDLE`, `SEARCH`, `BODYSTRUCTURE`); decide early whether desktop-client compatibility is a goal or a stretch.
- Production vs fake divergence: production `message_id` format, list-item fields, error body shape. Write against the documented schema, test against the fake.

## Open questions for the user

1. Language: TypeScript/Node with zero deps, or something else you are more fluent in?
2. INBOX semantics: hide trashed/spam messages from INBOX (recommended) or show everything labeled `received`?
3. Extra mailboxes: add `Sent`/`Trash`/`Spam` views to match AgentMail's production IMAP, or stay strictly at INBOX + Drafts?
4. Is Thunderbird actually connecting a goal, or is a scripted TCP client the bar? This decides whether `SEARCH`, `BODYSTRUCTURE`, `LSUB`, `STATUS`, `ID` need stubs.
5. Draft edits: accept that an API-side edit changes bytes under a UID (documented limitation), or treat each `updated_at` as a new message?
6. Persistence format: JSON file with atomic rename, or SQLite via `node:sqlite`?
7. Which extra capability appeals: `UIDPLUS`, `STORE \Seen`, `SEARCH`, or `IDLE`?
8. Is a hosted sandbox with real credentials expected later? That affects whether we test against production shapes at all.
