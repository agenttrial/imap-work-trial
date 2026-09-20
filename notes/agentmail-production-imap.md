# AgentMail's hosted IMAP/SMTP (prior art)

Source: `https://docs.agentmail.to/imap-smtp`. Fetched 2026-09-19. This is not linked from the assignment but is the closest existing implementation of the thing being built, so it anchors expectations.

## What production offers

- IMAP at `imap.agentmail.to:993`, TLS required. SMTP at `smtp.agentmail.to` on 465 (implicit TLS) or 587 (STARTTLS).
- **Credentials are exactly the assignment's scheme**: username = inbox email (`inbox_id`), password = API key.
- **Folders exposed: `INBOX`, `Sent`, `Trash`, `Spam`.** Folder names other than `INBOX` are case-sensitive.
- **"A `Drafts` folder is also listed for client compatibility but always appears empty; use the API to create and manage drafts."** The work trial's Drafts requirement (`\Draft` flag, APPEND to Drafts backed by the Drafts API) is precisely the gap in the production server.
- Supports `IDLE` (RFC 2177) for push-style new-mail notification.
- Supports `STORE` (flag changes), `MOVE`, `COPY` (implied by the permission table).
- Sync is driven by an **inbox event log** that IMAP reads to stay current, which is why restricted keys must carry `label_*_read` permissions for spam/trash/blocked/unauthenticated: the log can carry any label.
- Failure reporting: when a permission is missing, LOGIN succeeds but the mailbox never updates, and the server reports the cause in an `[ALERT]` response code. Good model for how to surface API-side failures to an IMAP client.

## Permission table for restricted keys (production)

| Permission | Needed for |
| --- | --- |
| `inbox_read` | LOGIN |
| `message_read` | reading messages and the event log |
| `message_update` | STORE, MOVE, COPY |
| `message_send` | SMTP |
| `label_spam_read` | Spam folder |
| `label_trash_read` | Trash folder |
| `label_blocked_read`, `label_unauthenticated_read` | event log |

## Implications for this project

- Mapping labels to folders as `received -> INBOX`, `sent -> Sent`, `trash -> Trash`, `spam -> Spam` matches what AgentMail itself ships, so it is a defensible "mailbox behavior beyond INBOX and Drafts" answer.
- Production makes Drafts read-only-empty; this project must make Drafts real (list, fetch, APPEND), which is the novel part and where the design defense will focus.
- SMTP is out of scope for the trial, consistent with `STANDARD_IMAP_CLIENT.md` ("SMTP is not provided").
