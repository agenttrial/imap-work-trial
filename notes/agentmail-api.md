# AgentMail API, the parts this project touches

Sources: `docs.agentmail.to/api-reference/inboxes/{messages,drafts}/*`, `/api-reference/auth/me`, and the Inboxes, Messages, Drafts, Labels, Errors, Rate-limits guides. Fetched 2026-09-19.

## Base, auth, conventions

- Production base URL: `https://api.agentmail.to/v0`. Sandbox: `http://127.0.0.1:3210/v0`.
- Auth: `Authorization: Bearer <api_key>`. OpenAPI declares only the bearer scheme. (The fake also accepts `x-api-key`; do not rely on it.)
- API keys can be **unrestricted** or **inbox-scoped**. An inbox-scoped key only reaches that inbox's threads, messages, drafts. `GET /v0/auth/me` reveals the key's scope.
- All list endpoints paginate with `limit` + `page_token`, returning `count`, `limit`, `next_page_token` (absent on the last page). Follow until absent.
- Errors: JSON with `name`, `message`, optional stable `code` (snake_case, e.g. `not_found`, `missing_permission`, `rate_limit_exceeded`), `fix`, `docs`. Branch on `code`, not message text. Status families: 401 auth, 403 permission, 400/404/422 request, 409 conflict, 413 too large, 429/500/503 transient.
- 429 comes with `Retry-After` (usually 1s). Official SDKs retry 429 automatically; raw HTTP callers should too, with backoff. Polling advice: one list call every few seconds per inbox is plenty.
- Idempotency: `client_id` on create operations (including drafts) prevents duplicate resources on retries.

## Core model

Organization > (Pod) > Inbox > Thread > Message > Attachment. An Inbox is an email account with a unique address; the `inbox_id` **is** the email address (e.g. `candidate@imap.test`). Drafts live under an Inbox and are unsent Messages.

**Labels** are free-form string tags on Messages, Threads, and Drafts. They are the only state mechanism; there is no folder concept and no dedicated read/unread flag. Labels are **not exclusive**: one message can carry several. System-ish labels seen in docs and fixtures: `received`, `sent`, `unread`, `read`, `starred`, `trash`, `spam`, `blocked`, `unauthenticated`, `scheduled` (drafts). Trash is just the `trash` label; list/search exclude trashed items by default (`include_trash=true` to include). Permanent deletion is a separate DELETE endpoint.

## GET /v0/auth/me (Who Am I)

Response: `scope_type` (`inbox` | pod | org), `scope_id`, `organization_id`, optional `pod_id`, `inbox_id`, `api_key_id`. Cheap credential check at LOGIN time; also tells you whether the key is scoped to the inbox the user typed.

## GET /v0/inboxes/{inbox_id}

Response: `inbox_id`, `email`, `display_name`, `pod_id`, `created_at`, `updated_at`, `metadata`. Alternative LOGIN check; also confirms the inbox exists.

## Messages

### GET /v0/inboxes/{inbox_id}/messages (List)

Query: `limit`, `page_token`, `labels` (list; AND semantics), `before`, `after`, `ascending` (default newest first), `include_spam`, `include_blocked`, `include_unauthenticated`, `include_trash`, `from`, `to`, `subject` (substring filters; when used the request is served by search and `limit` caps at 100).

Item fields (list projection): `inbox_id`, `thread_id`, `message_id`, `labels[]`, `timestamp` (sent/received time), `from` (string, `addr` or `Name <addr>`), `to[]`, `size` (bytes), `updated_at`, `created_at`, optional `cc[]`, `bcc[]`, `subject`, `preview`, `attachments[]` (`attachment_id`, `size`, `filename`, `content_type`, `content_disposition`, `content_id`), `in_reply_to`, `references[]`, `headers` (map). **No body in list items.**

### GET /v0/inboxes/{inbox_id}/messages/{message_id} (Get)

Everything in the list item plus `reply_to[]`, `text`, `html`, `extracted_text`, `extracted_html`. Note: `text`/`preview` may be absent for HTML-only mail; docs say treat `html` as primary. Not needed for this project because raw bytes come from the raw endpoint.

### GET /v0/inboxes/{inbox_id}/messages/{message_id}/raw (Get Raw)

Response: `message_id`, `size` (bytes of raw message), `download_url` (S3 presigned URL to the `.eml`), `expires_at`. Two-step: call this with auth, then GET the `download_url` **without** the API key. Cache-ability is bounded by `expires_at`. This is the source of truth for `RFC822`, `BODY[]`, `RFC822.SIZE`, and section fetches.

### PATCH /v0/inboxes/{inbox_id}/messages/{message_id} (Update)

Body: `add_labels` (string or list), `remove_labels` (string or list). Response: `message_id`, `labels[]`. Mark as read = `add_labels:["read"], remove_labels:["unread"]`. This is how a `STORE +FLAGS (\Seen)` would be implemented if that capability is chosen.

### Batch endpoints

`POST .../messages/batch-get` and `.../batch-update` exist in the reference (not in the fake). Could reduce round trips in production; not usable against the sandbox.

## Drafts

### GET /v0/inboxes/{inbox_id}/drafts (List)

Query: `limit`, `page_token`, `labels`, `before`, `after`, `ascending`. Ordered by `updated_at` descending.

Item fields: `inbox_id`, `draft_id`, `labels[]`, `updated_at`, optional `to[]`, `cc[]`, `bcc[]`, `subject`, `preview`, `attachments[]`, `in_reply_to`, `forward_of`, `send_status` (`scheduled`|`sending`|`failed`), `send_at`. **No `text`/`html`, no `reply_to`, no `created_at` in the documented list item** (the fake does include `reply_to` and `created_at`).

### GET /v0/inboxes/{inbox_id}/drafts/{draft_id} (Get)

Adds `created_at`, `client_id`, `reply_to[]`, `text`, `html`, `references[]`. Required to build a draft's RFC 822 body.

### POST /v0/inboxes/{inbox_id}/drafts (Create)

Body (all optional): `labels[]`, `reply_to[]`, `to[]`, `cc[]`, `bcc[]`, `subject`, `text`, `html`, `attachments[]`, `in_reply_to`, `forward_of`, `reply_all`, `send_at`, `client_id`. Returns the full Draft (same shape as Get). Whole request limited to 6 MB. This is the target of IMAP `APPEND` to `Drafts`: parse the RFC 822 literal, extract To/Cc/Bcc/Reply-To/Subject and the text body, POST.

### Other draft operations (not required)

`PATCH .../drafts/{id}` (update fields; omit = unchanged, null/[] = clear; 409 if already sending), `DELETE .../drafts/{id}`, `POST .../drafts/{id}/send` (sends and deletes the draft). A draft's kind (plain, reply, forward) is fixed at creation.

## Guide-level facts worth remembering

- "AgentMail doesn't have a dedicated mark-as-read endpoint": read state is the `read`/`unread` label pair.
- `message_id` in production examples looks like `<abc123@agentmail.to>` (the RFC Message-ID); in the fake it is an opaque `msg_...` token and the RFC Message-ID lives in `headers["Message-ID"]`. Treat `message_id` as an opaque key.
- Every Message lives in a Thread; sending creates a thread, replying appends to it. Threads are not needed for this project.
- Free-tier sends get a "Sent via AgentMail" footer. Irrelevant here (no sending).
- Official SDKs: npm `agentmail` (0.5.27 at time of writing, depends only on `ws`), PyPI `agentmail`. Optional per the assignment.
