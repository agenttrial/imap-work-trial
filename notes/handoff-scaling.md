# Handoff: scaling questions for imapgw

You are helping the user prepare for a discussion about how `imapgw` would scale to millions of requests a day. This is a thinking and writing task first; code only if the user asks.

## The project

`~/Desktop/harsh/imap-work-trial` holds `imapgw`, an IMAP4rev1 server in Python 3.12 (stdlib only) that fronts the AgentMail REST API: label views as mailboxes, persistent UIDs in SQLite, byte-exact FETCH, APPEND to Drafts, SEARCH, STORE, EXPUNGE-to-trash. It is complete and verified against a real AgentMail inbox; 283 tests pass. Committed on `main` of `https://github.com/agenttrial/imap-work-trial`, latest `f1d5fa1`.

Read in this order: `DESIGN.md` sections 2, 3, 5 (the existing scaling path and, in 5.6, the reviewer's list of claims the current code does not support), 8b (review findings; several are about concurrency, admission, and consistency), 7.1 (what production behaviour was confirmed, including that AgentMail's list endpoint lags label writes by seconds); `README.md` architecture and limitations; `notes/agentmail-api.md` (rate limits: 429 with `Retry-After`, per-organisation limits per the rate-limit guide, `client_id` idempotency, webhooks and WebSockets exist); `notes/review_findings.md` section "scaling claims" in both reviews; then `imapgw/mailbox.py`, `imapgw/apiclient.py`, `imapgw/session.py`, `imapgw/uidstore.py`.

## What the user will be asked

Something like: "How do we make this scalable? Millions of requests a day. What tradeoffs did you make, why, and what was the thought process?" They want to (a) defend the current design as a sound base, (b) explain clearly what changes at each order of magnitude and why, and (c) be honest about what is not demonstrated. The evaluators reward candid technical communication over grand claims.

## Facts to build on

- Current architecture: one asyncio process; one session per connection with serial commands; a mailbox service with one in-flight listing per (inbox, mailbox) coalesced across sessions, a mutation generation barrier, and pins/holds for read-after-write lag; SQLite UID store with atomic allocation and a UIDVALIDITY watermark sidecar; a byte-bounded LRU cache namespaced by inbox; HTTP through `http.client` in a 16-thread pool with retries on 429/5xx honouring numeric `Retry-After`.
- Measured against production: LOGIN ~110 ms, listing 3 messages ~160 ms, body download ~370 ms, APPEND ~930 ms including its re-listing. Sizes for a first metadata fetch cost one raw-metadata call per message (deliberate, see DESIGN D7).
- Known gaps the reviewers named (DESIGN 5.6): no throughput benchmark; thread pool has no admission deadline; SQLite calls are synchronous on the loop; no token bucket or circuit breaker; shielded sync tasks are not joined at shutdown; allocation and tombstoning are separate transactions; coalescing is per mailbox not per inbox; rate limits per organisation vs per key should be confirmed against live docs.
- Levers already identified: webhooks/WebSockets to replace polling (AgentMail's own IMAP syncs from an inbox event log); `after=` incremental listing once its semantics are confirmed; batch endpoints; moving the UID store to Postgres/DynamoDB with conditional increments; shared cache; per-key token buckets; L4 load balancing with sticky connections.

## Deliverables

1. `notes/scaling.md`: a structured set of talking points. For each expected question, a two-to-four sentence answer, the numbers behind it, and the honest caveat. Include a "what changes at 10x / 100x / 1000x" table, the request budget math (a million requests a day is about 12 per second average; what is the upstream call count per IMAP command today, and how coalescing, caching, and webhooks change it), and the failure-mode story (rate limits, upstream outage, one inbox misbehaving).
2. Optionally, if the user wants evidence: a small load script under `scripts/` that opens N connections against the fake API, runs a fixed command mix, and reports upstream request counts from `/_test/requests` and latency percentiles. Zero dependencies, no IMAP libraries; reuse `tests/support/imap_client.py`. Ask before writing it.

Constraints: do not change server code without the user asking; do not print or commit anything from `.env`; another session may be editing the repo on a `thunderbird` branch, so work on `main` only in `notes/` or on your own branch. Answer the user's questions first, then propose; they prefer plain explanations and being told what is and is not demonstrated.
