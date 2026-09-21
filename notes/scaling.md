# Scaling and operations: talking points for imapgw

Written 2026-09-20 against `main` at `f1d5fa1`. Each question below gets a short answer, the
numbers behind it, and the honest caveat. Section 8 says what is demonstrated and what is not;
say those words out loud rather than letting the evaluator find them.

Sources checked today: AgentMail rate-limit guide, webhooks overview and event list, WebSocket
guide, `GET /inboxes/{id}/events`, `batch-update`. Codebase facts come from `imapgw/`,
`DESIGN.md` sections 5, 7.1, 8b, and `notes/review_findings.md`.

---

## 0. The one-minute version

The gateway is a stateless protocol translator with one small piece of durable state: the map
from AgentMail ids to IMAP UIDs, plus `\Deleted` marks, in SQLite. Everything else it holds is a
cache rebuilt from an AgentMail listing. That is what makes it a sound base: crashes lose nothing
that cannot be re-derived, a second host can serve any inbox once that one table is shared, and
correctness never depends on notifications arriving.

Today the expensive thing is polling: every `NOOP` and `SELECT` re-lists the mailbox upstream.
That is right for a trial (correctness first, no public endpoint) and wrong at scale. The fix is
not a bigger server; it is making upstream calls proportional to changes in the inbox rather
than to connections or commands: push for new mail (WebSocket or webhooks), an event log for
label changes, a slow reconcile as the safety net, and `IDLE` so clients stop asking.

What I can show: bounded and coalesced listings under many connections, correct behaviour under
injected 429/503/latency, UID stability across restarts, no credential leakage. What I cannot
show: a throughput number, more than one host, or event-driven sync. Those are directions with
reasoning behind them, not measured properties.

---

## 1. What exists today, in numbers

Architecture: one asyncio process; one session per TCP connection with commands executed
serially; a mailbox service that keeps one snapshot per (inbox, mailbox) and coalesces
concurrent listings of the same mailbox into one upstream pass; SQLite UID store with atomic
allocation; 64 MiB byte-bounded LRU cache namespaced by inbox; HTTP via `http.client` in a
16-thread pool with 3 attempts inside a 10 s budget, honouring integer `Retry-After`.

Measured against production (one inbox, three messages):

| Operation | Latency |
| --- | --- |
| LOGIN (`/auth/me`) | ~110 ms |
| SELECT (one listing page) | ~160 ms |
| Body download (raw meta + presigned GET) | ~370 ms |
| APPEND including its re-listing | ~930 ms |

Upstream calls per IMAP command today (P = listing pages; we request `limit=100`, production
returned one page for our inbox, the fake caps pages at 2 messages / 1 draft):

| IMAP command | Upstream calls | Notes |
| --- | --- | --- |
| CAPABILITY, LIST, LSUB, ID, NAMESPACE, unselected NOOP | 0 | |
| LOGIN | 1, or 2 for pod/org keys | `/auth/me`, then `GET /inboxes/{id}` |
| SELECT / EXAMINE (messages) | P | always re-lists |
| SELECT (Drafts) | P + one `GET` per draft not cached by `(id, updated_at)` | list items lack the body |
| NOOP (selected) | P | forced re-list, coalesced only with an overlapping listing of the same mailbox |
| STATUS | P if snapshot older than 5 s, else 0 | |
| FETCH FLAGS / INTERNALDATE / UID | 0 | served from snapshot |
| FETCH RFC822.SIZE (first time, no body cached) | 1 per message | raw metadata; remembered per message (D7) |
| FETCH BODY[] / sections (first time) | 2 per message | raw meta + download; 0 while cached |
| SEARCH on flags/dates/size | 0 | body/header keys download each candidate once |
| STORE | 1 `PATCH` per message per flag | no batch endpoint used |
| EXPUNGE / CLOSE | 1 per item + P | `PATCH trash` (messages) or `GET`+`DELETE` (drafts), then re-list |
| APPEND | 1 `POST` + P (+ P if Drafts never listed) | UID from the create response; re-list is off the command path |

Capacity ceiling of the HTTP layer: 16 workers each held for one round trip (~150-400 ms) gives
roughly 40-100 upstream calls per second per process. Beyond that, calls queue with no admission
deadline (reviewer finding, DESIGN 5.6).

---

## 2. The request budget: a million requests a day

A million per day is 11.6 per second average; assume 5-10x at peak, so ~100 per second. Two
different things get called "requests": IMAP commands from clients and HTTP calls to AgentMail.
The IMAP side is cheap (a snapshot lookup). The AgentMail side is rate-limited per organisation,
costs 100-400 ms each, and is the real budget.

Illustrative mix: 2,000 sessions on 2,000 inboxes of at most 100 messages (P = 1), each client
polling every 5 minutes, opening the mailbox 12 times a day, fetching 100 message metadata and
20 bodies and changing 10 flags. That is about 430 commands per session per day, ~0.9M IMAP
commands per day.

| Design | Upstream calls per day | Per second | What dominates |
| --- | --- | --- | --- |
| Today (poll on NOOP, cache bodies) | ~0.7-1.0M | 8-12 | 576k listings from NOOP; sizes and bodies once each |
| Plus push for arrivals (WebSocket or webhook), NOOP served locally, events log polled every 5 min, full reconcile hourly | ~0.7M | ~8 | still per-inbox polling for label changes; arrivals now instant and free |
| Plus label-change webhooks (do not exist today; the event log does) | ~0.15-0.25M | 2-3 | one call per real change + hourly reconcile + first-time bodies |

Three things to say about this table:

- Coalescing does not help this mix at all. It merges listings that overlap in time for the
  same mailbox. With one session per inbox nothing overlaps. It matters for the "many clients on
  one shared inbox" shape (a team reading one support address), where it bounds cost at one
  listing per ~160 ms per mailbox regardless of client count.
- Caching zeroes the second and later body fetches, which is where the bytes are. Body downloads
  come from a presigned CDN URL with no auth header, so they do not even count against the API.
- Polling the label event log is not cheaper than polling a one-page listing. Its value is
  correctness (it catches label changes on old messages that an `after=` listing would miss) and
  big inboxes (one small call instead of P pages). The order-of-magnitude win only comes from
  push, and push today covers arrivals, not label changes. That is a concrete API ask (section
  3.1).

---

## 3. The questions

### 3.1 What was easy, what was hard, and what API changes would help

Easy, because the API fits IMAP well: key-as-password auth with `/auth/me` as a cheap check;
raw bytes via a presigned URL, which makes byte-exact `FETCH` trivial and keeps the key away
from the CDN; `read`/`unread`/`starred` labels mapping onto `\Seen`/`\Flagged`; `client_id`
idempotency on draft creation, which makes `APPEND` retries safe; draft create returning the
full object, which let `APPEND` allocate its UID without a second round trip.

Hard, because of real model mismatches (DESIGN section 2):

1. Labels are not folders. A message can be `received` and `trash` at once; `labels=` filtering
   is AND-of-all and bypasses trash exclusion. Solution: each mailbox is a label view with an
   explicit include/exclude filter, so a trashed message appears exactly once, in Trash.
2. Drafts are mutable; IMAP UIDs name immutable bytes. Solution: a draft's UID is keyed by
   (draft id, content hash); an edit made elsewhere shows as expunge plus a new UID. Gmail does
   the same.
3. The list endpoint lags writes by seconds (found only against production, not the fake). A
   `STORE` followed by `NOOP` could flip a flag back; `EXPUNGE` could answer `OK` with no
   `EXPUNGE` line. Solution: labels we wrote and drafts we created are held over stale listings
   for 60-120 s or until the listing agrees.
4. No conditional writes. Deleting a draft is GET-then-DELETE with a race window the API cannot
   close.
5. `RFC822.SIZE` without the body costs one raw-metadata call per message, because a UID's size
   must never change and the list `size` is a different field from the raw `size` (they matched
   in production, but a contract is not an observation).
6. No change feed for drafts or permanent deletions; webhooks cover arrivals and sending, the
   event log covers labels, nothing covers drafts.

API changes that would remove the most gateway code, in order of value:

- Label change events (`label.added`/`label.removed`) as webhook and WebSocket event types. The
  event log already records them; exposing them as push turns flag sync from polling into O(changes).
- Draft events (`draft.created/updated/deleted`) and a permanent-delete event, so Drafts stops
  needing polling and expunges are push-driven.
- Conditional update/delete (`If-Match` on `updated_at`), closing the draft race.
- A cursor on the events endpoint (`after_event_id` or `after=`) with documented semantics, so
  catch-up after a disconnect is one call rather than paging until a known id.
- Read-your-writes on the list endpoint, or a documented lag bound, so the holds can go.
- Short-lived, device-scoped credentials. Today a phone would store a long-lived API key as an
  IMAP password.

### 3.2 Can an existing phone mail app use it?

Yes in principle: the phone speaks IMAP, the gateway speaks IMAP, and `INBOX`, `Drafts`,
`Sent`, `Trash`, `Spam` with stable UIDs is exactly what a phone caches against. In practice
four things stand between the current build and iOS Mail or Gmail on Android:

- TLS on 993. Phones refuse plaintext IMAP (iOS allows it only with an explicit override). The
  stdlib `ssl` module can wrap the listener in a few lines, or an L4 TLS terminator can sit in
  front for implicit TLS. Out of scope for the trial by the assignment, not hard.
- `ENVELOPE` and `BODYSTRUCTURE`. Every real client asks for them to draw the message list. The
  `thunderbird` branch is adding them now.
- `IDLE`. iOS Mail and most Android clients use it for push; without it they fall back to
  polling every 15 minutes or so. Section 3.6 covers what IDLE needs behind it.
- A public hostname and SMTP if the user expects to send. SMTP is out of scope; AgentMail's
  own `smtp.agentmail.to` could be pointed to directly.

Caveat to say plainly: AgentMail already hosts IMAP at `imap.agentmail.to`, minus a working
Drafts folder. The honest pitch for this gateway on a phone is "the Drafts folder that theirs
lacks", not "IMAP access", and I have not run a phone against it.

### 3.3 Can we deploy it?

Yes, as one container per host: no runtime dependencies, one process, one port, config from
environment, graceful shutdown that sends `BYE` to every client (exists), credentials never
persisted, log redaction of keys already in place. It needs:

- A persistent local disk for `imapgw.sqlite3` and its `.validity` sidecar. SQLite on a network
  filesystem is unsafe; at the multi-host step the store moves to a database (section 5).
- TLS termination, a health check (`/health`-style TCP probe or a `NOOP` prober), metrics
  (upstream call counts, 429s, listing latency, sessions), and structured logs.
- One config change I would make before any deployment: SQLite `synchronous=FULL` for the
  allocation transaction. Today it is `NORMAL` under WAL, which is durable across a process
  crash but can lose the last commits on power loss. A lost allocation would let a UID be
  re-issued to a different message after reboot, which the assignment forbids.
- Sticky L4 load balancing. IMAP connections are long-lived and stateful, so a plain TCP
  balancer that never moves a connection is all that is needed; there is no cross-request state
  outside the shared store.

### 3.4 How is state tracked when data changes in AgentMail's database?

AgentMail is the source of truth. The gateway durably owns three things: the UID map
(`remote key -> uid`, with tombstones so UIDs are never reused), `next_uid`/`UIDVALIDITY` per
mailbox, and `\Deleted` marks. Every other thing it holds is a cache.

State tracking is one operation, reconcile: list the mailbox completely (every page), then for
each id: known -> keep its UID and take the new labels; unknown -> allocate the next UID in
timestamp order, atomically; known but absent -> tombstone it. The result becomes the new
snapshot; each session compares its own view against the snapshot at the next safe point and
announces `EXISTS`, `EXPUNGE`, and `FETCH FLAGS`. Reconcile is idempotent, so it can be run at
any time, by any host, after any failure.

How each external change shows up:

| Change made through the API by an agent | What the IMAP client sees |
| --- | --- |
| New message arrives | `* n EXISTS`; UID = next_uid |
| Message marked read/starred | untagged `FETCH (FLAGS ...)` at the next safe point |
| Message trashed | `EXPUNGE` from INBOX; appears in Trash with its own UID |
| Message permanently deleted | `EXPUNGE`; UID tombstoned forever |
| Draft edited | `EXPUNGE` of the old UID, `EXISTS` with a new one (content-hash key) |
| Draft sent or deleted | `EXPUNGE` |

The concurrency machinery around this is what the adversarial reviews forced into existence: a
mutation generation so a listing that started before our own write is never taken as the
post-write view; holds and pins for the seconds when the list endpoint has not caught up with a
write; per-session flag baselines so a change made in another session is announced exactly once.

Caveat: allocation and tombstoning are separate transactions and reconcile is serialised only
within one process. Two hosts reconciling the same mailbox from listings taken seconds apart
could tombstone each other's fresh allocation. Multi-host needs one reconciler per mailbox at a
time (a lease, or consistent hashing of inbox to host) or a single transaction that checks a
listing version. Not built.

### 3.5 What happens when the API errors out?

Failures are classified by status only (bodies are logged, never parsed for control flow):

| Upstream | Gateway behaviour |
| --- | --- |
| 429, 5xx, timeout, connection refused | retry up to 3 attempts within 10 s, `Retry-After` honoured; then tagged `NO [UNAVAILABLE]`, connection stays open, previous snapshot kept |
| 401 mid-session | `* BYE`, key revoked |
| 403 mid-session | `NO [CANNOT]`, key lacks the permission; session continues (verified with a restricted key) |
| 404 on an item | that item skipped or `NO`; never a crash |
| 200 with a malformed body | treated as failure; the previous view is kept, nothing tombstoned |
| Download not exactly 200 or wrong length | refused, not cached |
| Failure inside a coalesced listing | attributed to the credentials that ran it; other sessions retry with their own |

The principle: a failed or malformed listing can never shrink a mailbox. Only a complete,
well-formed listing may tombstone. So an outage degrades the gateway to "stale but consistent",
and clients keep working from their own caches, which is what IMAP clients are built for.

What is missing at scale, and what I would add first: a per-organisation token bucket so a
hundred sessions cannot each independently trigger and retry a 429; a circuit breaker that,
after k consecutive failures, answers `NO [UNAVAILABLE]` immediately for T seconds instead of
holding a thread for 10 s each; an admission deadline on the thread pool so a burst of slow
calls cannot delay an unrelated session past its own timeout; and an `[ALERT]` on the next
tagged response so the user sees "upstream degraded" in their client, which is what AgentMail's
own IMAP does for permission problems.

### 3.6 Notifying people of new mail without polling every five seconds

Two halves: how the gateway learns, and how the client learns.

Client side is standard: `IDLE` (RFC 2177). The client sends `IDLE`, the session parks on an
asyncio event for that (inbox, mailbox), and when the snapshot changes it writes `* n EXISTS`
without being asked. About fifty lines on top of the existing safe-point announcer. Clients
without IDLE keep polling with NOOP, which we now serve from the snapshot with zero upstream
calls.

Gateway side, per active inbox (one with at least one selected session):

- Arrivals: subscribe once per process over AgentMail's WebSocket with `inbox_ids` for the
  inboxes this host serves, event type `message.received`. No public URL needed; latency is
  the WebSocket's. Or register a webhook (org-level, or up to 10 inboxes per webhook) at a public
  HTTPS endpoint, verify the Svix signature with the returned `secret`, and de-duplicate on
  `event_id`.
- Label changes made by agents: `GET /inboxes/{id}/events` returns `label.added` /
  `label.removed` with `event_id`, `message_id`, `label`, `event_at`. This is the event log
  AgentMail's own IMAP syncs from. Poll it at a slow cadence per active inbox and apply the
  deltas; the cursor is the last `event_id` seen.
- Safety net: a full reconcile per active inbox every N minutes and on every `SELECT`.
  Nothing above is trusted as the source of truth; an event means "go look", the reconcile
  means "this is what is there".

Numbers: today one poll costs P listing calls every NOOP interval for every session. With push,
arrivals cost zero polls and appear within the WebSocket's latency, and the per-inbox cost is
one small events call per cadence plus the reconcile. Polling five-second listings for 2,000
inboxes would be 34M calls a day; push plus a five-minute events poll is about 0.6M.

Caveats: no draft events exist, so Drafts stays on reconcile polling. The WebSocket docs say
nothing about replay after a disconnect, and the webhook docs say nothing about retries or
ordering; treat both as at-least-once, unordered, and lossy (section 3.8). None of this is
built; the seams are the mailbox service's `sync` and the session's safe-point announcer.

### 3.7 Crashes

- Gateway process crash: connections drop, clients reconnect (every mail client does this),
  `SELECT` reconciles, UIDs come back from SQLite identical (tested across restart). In-memory
  pins and holds are lost, so for up to a minute after a restart a lagging listing could hide a
  draft created just before the crash; it would come back with a new UID once listed. A UID
  churn, not a corruption. At scale, pins and holds move next to the UID map.
- Power loss: see `synchronous=FULL` in 3.3.
- UID store unusable at start (locked, read-only, wrong schema): the server refuses to start
  rather than invent UIDs. A corrupt file, positively identified by SQLite, is set aside and a
  new `UIDVALIDITY` strictly above the sidecar watermark is issued, so clients discard their
  caches instead of trusting stale UIDs. Deleting both files is the only way to reuse a value.
- Client crash or silent disappearance: idle timeout (30 min) and a write timeout for clients
  that stop reading reap the session and its buffers.
- AgentMail down: section 3.5. Every cached body stays readable; every listing keeps the last
  good view.
- Half-done work: `APPEND` answers `OK` only after the create succeeded and its UID is stored,
  and the re-listing is a separate task whose failure is logged; `EXPUNGE` persists `\Deleted`
  marks before acting, so a crash mid-expunge leaves marks to retry, never a half-trashed view
  that forgets what was asked.

### 3.8 Pub/sub down

The event channel is an accelerator, never the source of truth, so its failure costs latency,
not correctness. Concretely:

- WebSocket drops: mark every inbox subscribed on it as degraded, raise their reconcile cadence
  (e.g. every 30-60 s), reconnect with backoff, and on reconnect run one reconcile per inbox
  before trusting new events, because replay is not documented.
- Webhooks unreachable (our endpoint was down): deliveries during the gap may or may not be
  retried (undocumented). We never apply an event as a delta without a listing anyway, so the
  worst case is that new mail shows up at the next reconcile instead of instantly. On startup
  each inbox is reconciled at its first `SELECT` regardless.
- Events endpoint down or returning 5xx: it is one more upstream call under the same retry and
  breaker rules; the reconcile cadence covers it.
- Duplicate or out-of-order events: harmless by construction, because reconcile is idempotent
  and `event_id` de-duplicates the trigger.

Design rule to state: level-triggered with edge-triggered hints. Every piece of state can be
rebuilt from a listing; hints only decide when to look.

### 3.9 One inbox misbehaving

A noisy inbox (10,000 messages, an agent flipping labels in a loop, a client fetching every
body) hurts in three places, each with its own bound:

- Its organisation's rate limit. Limits are per organisation (checked today), and the gateway
  uses each user's own key, so a customer's noisy inbox costs that customer, not others. If the
  gateway itself were run under one AgentMail organisation for many users, that isolation
  vanishes and a per-inbox token bucket becomes mandatory.
- The shared thread pool. Today sixteen slow calls from one inbox delay everyone on the host.
  Fix: per-inbox concurrency cap (e.g. 4) and the admission deadline.
- The shared byte cache. It is byte-bounded and LRU, so one inbox can evict others' bodies but
  cannot grow memory. A per-inbox share would stop the eviction.

Per-inbox reconcile cost grows with P; a 10,000-message inbox is 100 pages per `NOOP` today.
That is the strongest argument for the events log: one call for changes instead of P pages
for a listing, independent of mailbox size.

### 3.10 Rate limits

Per organisation, no published numbers ("generous"), 429 with integer `Retry-After` (usually
1 s), guidance to use webhooks/WebSockets instead of polling and, if polling, one list call
every few seconds per inbox. The gateway honours `Retry-After`, backs off exponentially with
jitter otherwise, retries at most 3 times inside 10 s, then returns `NO [UNAVAILABLE]` and lets
the client retry later. Not yet observed in production: a real 429. Not built: HTTP-date
`Retry-After`, a token bucket, and "back off further on consecutive 429s" as the guide asks.

---

## 4. What changes at 10x, 100x, 1000x

Baseline: the trial, one inbox, a handful of connections, one process.

| Concern | 10x: ~100 inboxes, hundreds of connections, one host | 100x: thousands of inboxes, several hosts | 1000x: tens of thousands of inboxes, many hosts |
| --- | --- | --- | --- |
| Sync | `IDLE`; NOOP served from snapshot; WebSocket for arrivals; events log for labels; reconcile every few minutes | same, but one reconciler per inbox across the fleet (lease or consistent hashing) | separate ingestion tier: webhooks at org level -> queue -> per-inbox reconcilers; connection tier only reads snapshots |
| UID store | SQLite, `synchronous=FULL`, store calls off the event loop | Postgres (or DynamoDB): allocation is `SELECT ... FOR UPDATE` on the mailbox row; rare, so contention is low | same; allocation is the only serialised operation per mailbox |
| Snapshots and cache | in-process | in-process per owning host, or Redis keyed by inbox with a version stamp so any host can serve any inbox | Redis or equivalent; bodies from the CDN with local LRU |
| Upstream protection | per-org token bucket, admission deadline, circuit breaker, per-inbox concurrency cap | same, buckets shared via Redis if one org spans hosts | same |
| Connections | one process, `max_connections` raised, TLS | L4 balancer, sticky by connection; host loss = clients reconnect | same; connection tier is stateless apart from sessions |
| Ops | metrics, health, logs | leases and reconciler ownership become the thing to monitor | ask whether the gateway belongs inside AgentMail, whose IMAP already reads the event log directly |

The seams that make this possible are already in the code: `UidStore` is the only thing that
knows SQLite; `MailboxService.sync` is the only thing that lists; `ByteCache` is the only thing
that holds bytes; `Transport` is the only thing that does HTTP. Each is swapped without touching
the protocol layer.

---

## 5. Tradeoffs made deliberately, and the thought process

- Correctness before throughput. Every reviewer finding fixed (27) was about consistency and
  safety; none was about speed, and that was the right order for a gateway whose failure mode is
  a client showing the wrong message under a UID.
- Full listing over incremental `after=`. The semantics of `after` (timestamp vs `updated_at`)
  are undocumented; missing a label change on an old message is a silent bug. Chose the slower
  thing that is provably complete, and marked the events log as the path to incremental.
- Polling over push. Push needs a public endpoint or a long-lived WebSocket and a lifecycle for
  it; polling on the client's own NOOP needs nothing and cannot get out of sync. Right for the
  trial; the first thing to change after it.
- SQLite over a database. One process, one host, zero dependencies, real transactions. The
  interface is the seam.
- One extra call per message for `RFC822.SIZE`. Paid for the guarantee that a UID's size never
  changes. Would revisit if AgentMail documents that list `size` equals raw length.
- Content-hash UIDs for drafts over "let the bytes change". Costs UID churn on edits; buys the
  sentence "every UID is immutable" without an asterisk.
- Stdlib only, no IMAP library. The assignment's rule, but also why the HTTP layer is a thread
  pool rather than an async client. That is the first dependency I would add once allowed.

---

## 6. Failure-mode story in one breath

Rate limit: retry with `Retry-After`, then `NO`, client retries later; add a bucket per org.
Upstream outage: last good view stays, bodies stay cached, nothing tombstoned, `NO [UNAVAILABLE]`
on anything that needs upstream; add a breaker and an `[ALERT]`. One inbox misbehaving: its
own org's limit, a per-inbox concurrency cap, a byte-bounded cache. Notifications down: reconcile
catches up; latency, not correctness. Crash: UIDs in SQLite, clients reconnect, first `SELECT`
reconciles.

---

## 7. Numbers to have ready

- 1M/day = 11.6/s average; plan for ~100/s peak.
- Production latencies: LOGIN 110 ms, listing 160 ms, body 370 ms, APPEND 930 ms.
- HTTP pool: 16 workers, so ~40-100 upstream calls/s per process before queueing.
- Retry policy: 3 attempts, 10 s budget, base backoff 0.5 s with 25% jitter.
- Cache: 64 MiB LRU; refresh interval 5 s (STATUS only); holds 60 s; pins 120 s.
- Batch update: 50 ids per call, atomic; would cut a 50-message `STORE` from 50 calls to 1.
- Webhooks: up to 10 `inbox_ids` per webhook or org-level; Svix-signed; `event_id` for dedupe.
- Tests: 283 passing, ~45 s; about a third are regressions from two adversarial reviews.

---

## 8. What is demonstrated and what is not

Demonstrated by tests or the production run:

- Coalescing: concurrent NOOPs on one mailbox produce one upstream listing (count via
  `/_test/requests`).
- Injected 429, 503, and latency on every path yield `NO` without hanging, crashing, or
  shrinking the mailbox.
- UIDs identical across a process restart; new arrivals get higher UIDs; UIDVALIDITY strictly
  increases after store loss.
- No API key ever reaches a download URL.
- Against production: presigned downloads, label writes, draft deletion, restricted and
  org-scoped keys, the listing lag and its fix.

Not demonstrated:

- Any throughput or latency number under load (no benchmark exists).
- More than one process or host.
- Event-driven sync, `IDLE`, WebSocket or webhook handling.
- A real 429, an inbox larger than one page, or a phone or desktop client end to end (Thunderbird
  in progress on its branch).
- The SQLite durability setting under power loss.

If evidence is wanted before the conversation, a load script under `scripts/` that opens N
connections against the fake, runs a fixed command mix, and reports upstream call counts and
latency percentiles would turn the coalescing and caching claims into numbers in an afternoon.
It would not say anything about production rate limits or multiple hosts.
