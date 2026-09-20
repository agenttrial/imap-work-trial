# Fake AgentMail API: observed behavior

From reading `test-harness/fake-agentmail-api.mjs` and `test-harness/lib/fixtures.mjs`, and probing the running server on 2026-09-19. The harness's own 7 tests pass on Node 24.

## Endpoints implemented

Under `/v0` (auth required unless noted):

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/auth/me` | returns `scope_type: inbox`, `inbox_id: candidate@imap.test` |
| GET | `/inboxes`, `/inboxes/{id}` | single inbox, display name "IMAP Candidate" |
| GET | `/inboxes/{id}/messages` | page size forced to **2**; supports `labels`, `include_trash`, `include_spam`, `ascending`, `limit`, `page_token` |
| GET | `/inboxes/{id}/messages/{mid}` | includes `text` |
| PATCH | `/inboxes/{id}/messages/{mid}` | `add_labels` / `remove_labels` |
| GET | `/inboxes/{id}/messages/{mid}/raw` | JSON `{message_id, size, download_url, expires_at}`; expiry 10 min |
| GET | `/raw/{mid}` (root, **no auth**) | `message/rfc822` bytes; this is the `download_url` target |
| GET | `/inboxes/{id}/drafts` | page size forced to **1**; sorted `updated_at` desc; items lack `text` |
| GET | `/inboxes/{id}/drafts/{did}` | includes `text` |
| POST | `/inboxes/{id}/drafts` | 400 if `html` or non-empty `attachments`; ids `draft_created_001`, `002`, ...; `updated_at` deterministic from 2026-08-18T17:00:00Z + n seconds |

Not implemented (404 "No fake endpoint"): threads, search, batch-get/update, draft update/delete/send, attachments, `before`/`after` filters, `from`/`to`/`subject` filters, `include_blocked`, `include_unauthenticated`.

Test control (root, no auth): `POST /_test/reset`, `POST /_test/add-message`, `POST /_test/fail-next`, `GET /_test/requests`, `GET /_test/state`, `GET /health`. `fail-next` matches on method + path prefix (with `/v0` stripped) and is checked **before** both the raw download route and auth, so it can fault any request including `/raw/...` downloads and `/auth/me`. Request log keeps the last 100 entries with an `authorization_present` flag.

## Fixture inventory

Messages (timestamp order, all `To: candidate@imap.test` unless noted):

| id | labels | notes |
| --- | --- | --- |
| `msg_received_ascii` | received, unread | simplest; 8bit text/plain |
| `msg_received_utf8` | received, read, starred | RFC 2047 encoded From and Subject; body has `naïve`, `東京`, `🚀`; **375 bytes, fewer characters**. Catches byte/char confusion |
| `msg_received_attachment` | received, unread | multipart/mixed with base64 `hello.txt`; exercises BODYSTRUCTURE / part fetches |
| `msg_sent` | sent, read | From is the inbox; To recipient@example.com; **no Content-Transfer-Encoding header** |
| `msg_trash` | trash, read | excluded from unfiltered list |
| `msg_multi_label` | received, trash, unread | **returned by `labels=received`** even though trashed; the "labels are not exclusive folders" case |

`POST /_test/add-message` adds `msg_new_arrival` (received, unread, 2026-08-18) for testing new-mail detection and UID assignment.

Drafts:

| id | fields |
| --- | --- |
| `draft_existing_simple` | to reviewer@example.com, subject, text |
| `draft_existing_addresses` | to, cc, bcc, reply_to all populated; the "keep all recipients" case |

## Behaviors that shape the design

1. **Pagination is unavoidable.** Two messages per page, one draft per page. Any listing that stops after the first page is wrong and the assignment calls this out explicitly.
2. **Label filter semantics.** `labels=a,b` or repeated `labels=` params: every label must be present. With a `labels` filter, trash/spam exclusion is **skipped**. Without it, `trash` and `spam` messages are dropped unless `include_trash`/`include_spam=true`. Whether INBOX should show `msg_multi_label` is a design decision to document (Gmail-style: a trashed message is not in the inbox; label-literal: it has `received`, so it is).
3. **`message_id` is opaque and distinct from the RFC `Message-ID` header.** Production `message_id` values look like `<...@agentmail.to>`. Use `message_id` as the stable key for UID mapping; do not derive it from headers.
4. **Raw download URL requires no auth and must not receive the key.** `GET /_test/requests` exposes `authorization_present` per request, so a test can assert the key was not sent to `/raw/...`.
5. **`size` in list items equals raw byte length** in the fake. In production this is documented as "Size of message in bytes" but whether it equals the `.eml` length exactly is unverified; the raw endpoint's own `size` is the safer source for `RFC822.SIZE`.
6. **Draft list items have no body.** Building a draft's RFC 822 projection requires one `GET` per draft.
7. **Draft create returns the full draft** including `text`, `draft_id`, `updated_at`. Message ordering for drafts is by `updated_at` desc; created drafts sort newest.
8. **Error body shape differs from production** (`{error:{message}}` vs `{name,message,code,...}`). Treat non-2xx as failure by status, log the body, do not parse it for control flow.
9. **Latency and failure injection** via `fail-next` with `delay_ms` allows testing: 503 on `/auth/me` during LOGIN (should yield `NO`, not a crash), 503 mid-pagination during SELECT, slow raw download during FETCH (timeouts), 401 (key revoked mid-session).
10. `updated_at`/`timestamp` values are fixed ISO strings, so INTERNALDATE output is deterministic and testable.
