---- New Review -----

## 1. Summary

  I reviewed the current working-tree submission, including updated and untracked files, in a fresh, separate git worktree; I did not modify the main worktree or
  test-harness/. The supplied suite ran 264 tests with no failures and one skip, and the end-to-end smoke test passed on isolated ports. Six additional probes
  confirmed remaining problems involving APPEND outcomes, malformed listings, UIDVALIDITY, command deadlines, draft deletion, and UID responses. Several earlier
  fixes are effective, but the documentation still overstates the guarantees around successful APPENDs, malformed synchronization, and command-assembly
  deadlines.

  Review worktree: /private/tmp/imapgw-rereview.z8GGMZ/worktree

  ## 2. Findings

  ### 1. Critical — APPEND still reports failure after successfully creating the draft

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:497, /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:532, /private/tmp/
  imapgw-rereview.z8GGMZ/worktree/imapgw/session.py:237

  Evidence: The fix catches AgentMailError from the post-create synchronization, but not cancellation from the enclosing command deadline.

  I selected Drafts, allowed the real create request against the fake API to succeed, and delayed only the subsequent synchronization beyond the command timeout:

  Before: 2 drafts
  APPEND response: NO [UNAVAILABLE] upstream timeout (APPEND)
  After:  3 drafts

  This is not an ambiguous POST timeout: creation has already returned successfully and the UID has been allocated. Nevertheless, the client receives a failure
  and may retry.

  The README’s “Once the create request succeeds, APPEND answers OK” claim is false. An unsuccessful APPEND must leave the mailbox unchanged. RFC 3501 §6.3.11

  Suggested fix: Once creation and UID allocation have succeeded, return the successful APPEND result without awaiting optional reconciliation inside the
  failure-producing command deadline. Handle post-commit reconciliation separately. Cost: medium, including cancellation and persistence-failure tests.

  ### 2. High — Malformed list items still tombstone valid messages

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:335, /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:372, /private/tmp/
  imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:282

  Evidence: Top-level response validation was added, but a dictionary without its required identifier is silently skipped.

  After synchronizing three valid messages, I supplied:

  {"messages": [{}]}

  Observed:

  Snapshot: 3 messages → 0 messages
  Persistent live UID map: {}

  The malformed response is accepted as a successful empty mailbox. Clients can receive EXPUNGE notifications; when the valid items return, they receive new
  UIDs.

  This directly contradicts the README statement that a malformed sync preserves the previous snapshot and tombstones nothing.

  Suggested fix: Validate required identifiers and relevant field types before committing any synchronization. A malformed item should fail the whole
  synchronization, preserving existing state. Cost: small–medium.

  ### 3. High — UIDVALIDITY can still identify different messages with the same UID

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/uidstore.py:183

  Evidence: Repeated the previous deterministic reproduction:

  Clock: 1700000000
  First store:  UIDVALIDITY=1700000000, UID 1 = "old"
  Recreated store in the same second:
                UIDVALIDITY=1700000000, UID 1 = "new"

  The identical mailbox/UIDVALIDITY/UID tuple now denotes different content. A reconnecting client cannot detect that its cached UID mapping is invalid.

  The limitation is now disclosed, which improves the documentation, but does not fix the invariant. Ordinary restarts with an intact database are not
  implicated.

  Suggested fix: Preserve an independently durable, monotonically advancing mailbox epoch across store recovery/recreation, with an explicit recovery procedure
  when all state is lost. Enforce the unsigned 32-bit range. Cost: medium.

  ### 4. High — The command-assembly deadline does not cover ordinary partial command lines

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/parser.py:130, /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/session.py:156

  Evidence: in_progress considers pending literal assembly, but ignores an incomplete line already buffered in _buf.

  With an assembly deadline of 120 ms and idle timeout of one second, I sent:

  "A " …70ms… "N" …70ms… "O" …70ms… "O" …70ms… "P" …70ms… CRLF

  The server answered:

  A OK NOOP completed

  The command took over 350 ms to assemble, yet the 120 ms deadline never applied. An unauthenticated client can keep connection slots occupied by sending
  partial-line bytes within successive idle intervals.

  The README describes a deadline for finishing one command, not only literal-bearing commands.

  Suggested fix: Start the assembly deadline with the first buffered byte and reset it at each complete command boundary. Cover pipelined boundaries explicitly.
  Cost: small–medium.

  ### 5. Medium — Recently created drafts remain visible after successful deletion

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:301, /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/mailbox.py:609

  Evidence: The new APPEND pin protects against lagging listings, but successful deletion does not invalidate that pin.

  Using a scripted transport:

  1. APPEND creates draft new.
  2. Subsequent listings omit it, activating the intended pin behavior.
  3. Version verification succeeds.
  4. DELETE succeeds.
  5. Synchronization returns another empty listing.

  Observed:

  Deletion errors: []
  DELETE requests: ["/inboxes/candidate%40imap.test/drafts/new"]
  Post-delete snapshot UIDs: [1]

  The deleted draft is reinserted from the pin and remains visible until expiry. EXPUNGE therefore cannot announce its disappearance promptly; cached content can
  remain fetchable.

  This was tested with a scripted transport because the supplied fake does not implement draft deletion.

  Suggested fix: Invalidate the relevant pin after successful deletion and prevent an older in-flight listing from resurrecting it. Cost: medium.

  ### 6. Medium — UID EXPUNGE can emit FETCH responses without UID

  Location: /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/session.py:324, /private/tmp/imapgw-rereview.z8GGMZ/worktree/imapgw/commands.py:621

  Evidence: Two authenticated sessions select INBOX. Session A sends:

  UID STORE 1 +FLAGS (\Deleted)
  UID STORE 2 -FLAGS (\Flagged)

  Session B sends:

  UID EXPUNGE 1

  Actual response:

  * 1 EXPUNGE
  * 1 FETCH (FLAGS (\Seen))
  A003 OK UID EXPUNGE completed

  The FETCH notification concerns surviving UID 2 but contains no UID. RFC 3501 explicitly requires UID in FETCH responses caused by any UID command, including
  commands other than UID FETCH and UID STORE. RFC 3501 §6.4.8

  Suggested fix: Include UID in unsolicited flag notifications, or pass UID-command context into notification generation. Always including it is simpler. Cost:
  small.

  The five new reproductions are in /private/tmp/imapgw-rereview.z8GGMZ/worktree/rereview_probes.py. The retained UIDVALIDITY reproduction is in /private/tmp/
  imapgw-rereview.z8GGMZ/worktree/previous_review_probes.py.

  .venv/bin/python -m unittest rereview_probes \
    previous_review_probes.ParserAndPersistence.test_uidvalidity_reused_after_missing_store -v

  These probes assert the observed defective behavior; their passing means the defects reproduced.

  ## 3. Design challenges

  - Local trash/spam exclusion: Agree. It makes the view definition explicit instead of relying solely on API defaults.
  - Content-hashed draft UIDs: Agree with immutable content per UID. Version-checked deletion improves safety; the documented check/delete race remains a genuine
    limitation.

  - Full relisting: Agree for this scope. An after= cursor alone cannot reliably discover removals and edits. Strong validation and mutation ordering are
    essential.

  - List-derived RFC822.SIZE: Amend. The uncertainty is now disclosed, but a UID’s reported size should not change after downloading its body. Establish an
    authoritative size source or fetch bytes before claiming an exact size.

  - \Deleted: Persisting it across sessions is a significant improvement. Soft deletion through trash, and refusing permanent message deletion, are reasonable
    scoped choices.

  - Non-PEEK setting \Seen: Agree. That side effect follows the advertised IMAP semantics; document its upstream label effect.
  - Bare LF: Acceptable intentional leniency. It does not excuse incomplete command deadlines.
  - Always-zero RECENT: Acceptable as a disclosed limitation of this gateway, not full fidelity.
  - Zero runtime dependencies: Reasonable here. The official SDK would not solve IMAP correctness; avoiding it increases responsibility for transport validation.
  - Scaling: The revised caveats are more credible. A 16-thread pool is not admission control, synchronous SQLite can block the loop, and there is no
    demonstrated multi-process synchronization model or capacity benchmark.

  - SEARCH KEYWORD: I would reverse the deliberate mapping to invisible upstream labels. Standard keyword searches should agree with the flags clients can
    observe; expose agent labels through an explicitly documented extension instead.

  ## 4. Test gaps

  Add these regression tests:

  - Successful APPEND POST followed by command-timeout cancellation must not produce a false failure.
  - Missing identifiers inside otherwise valid list responses must preserve snapshots and UID mappings.
  - APPEND pin followed by successful DELETE must disappear immediately, including with an older listing in flight.
  - Every FETCH response produced during every UID command must include UID.
  - Slow partial nonliteral lines must hit the assembly deadline.
  - Assembly deadlines must reset correctly between pipelined commands.
  - Same-second store recovery must not reuse UIDVALIDITY with new UID meanings.
  - Full-disk/locked-store failures after upstream creation must have an explicit, tested recovery outcome.
  - Inconsistent list and raw sizes must not silently change an already reported message size.

  The regression suite has grown substantially, but passing isolated fixes does not establish their composition. APPEND’s new reconciliation and pinning behavior
  illustrates that gap particularly clearly.

  ## 5. What I tried that did not break

  - The complete updated unit/integration suite: 264 run, one skipped, no failures.
  - End-to-end smoke: login, mailbox listing, metadata/body FETCH, Drafts rendering, APPENDUID, appended-draft visibility, NOOP, and logout.
  - Smoke literal length matched the downloaded message size.
  - The updated suite’s regression coverage passed for credential redaction, cache isolation, locked-store handling, redirects/download validation, literal
    limits, decoded SEARCH, strict APPEND decoding, and shared deletion flags.

  - The two-session probe preserved sequence-number renumbering after EXPUNGE; its failure was specifically the missing UID on the subsequent flag notification.

  I did not verify hosted-production behavior or Thunderbird interoperability, and I am not treating those untested assumptions as confirmed bugs.



-------------- Original Review --------------

## 1. Summary

  The required happy path works: the baseline suite passed 206 tests with one skipped, and the smoke test passed. I read the requested documentation,
  implementation, tests, and scripts, then ran 36 additional review probes covering failures and successful edge cases. Those probes exposed credential leakage,
  cross-inbox cache contamination, destructive stale-draft deletion, broken UID recovery, and APPEND failures that leave committed drafts behind. I would not
  accept the submission as meeting its reliability claims until these issues are addressed.

  The review ran in the separate git worktree (/private/tmp/imapgw-review.nKA2Hv/worktree). Submission source files and test-harness/ were not modified. The
  reproducible probes are in /private/tmp/imapgw-review.nKA2Hv/worktree/review_probes.py:

  cd /private/tmp/imapgw-review.nKA2Hv/worktree
  uv run python -m unittest review_probes -v

  The probes deliberately assert the observed behavior, including bugs; their passing does not mean the implementation is correct.

  ## 2. Findings

  ### F1. Critical — Stale draft deletion destroys an unseen upstream edit

  Location: imapgw/commands.py:561, imapgw/mailbox.py:448, README.md:168.

  Evidence: Using the existing scripted-transport interface:

  1. Select Drafts containing draft d, rendered as UID 1.
  2. UID STORE 1 +FLAGS (\Deleted).
  3. Edit d upstream, changing its text and updated_at.
  4. Issue EXPUNGE without another sync.

  The server deletes /drafts/d and returns:

  * 1 EXPUNGE
  A004 OK EXPUNGE completed

  The deleted object contains the new text the client never saw. Deletion operates on remote_id without verifying the content hash associated with the selected
  UID.

  This directly falsifies README’s claim that saving over a stale draft cannot destroy an agent’s edit. Reproduced by
  test_stale_draft_delete_destroys_upstream_edit; no production system was accessed.

  Suggested fix and cost: Verify draft version before deletion and reject stale targets. A GET followed by DELETE still has a race; the strong guarantee requires
  conditional deletion or a different preservation strategy. Medium to high cost, depending on API support; remove the unconditional documentation guarantee
  immediately.

  ### F2. Critical — Shared byte caches are not isolated by inbox

  Location: imapgw/mailbox.py:314, imapgw/mailbox.py:331, imapgw/mailbox.py:357.

  Evidence: Scripted API responses supplied inboxes alice@x and bob@x, each with message ID same-id, but different raw content. Fetching Alice’s message
  populated the global cache. Bob’s subsequent fetch returned:

  b"private body for alice@x"

  Bob’s raw endpoint was unnecessary because the cache key was only same-id. Draft version-cache keys similarly omit inbox identity.

  The UID database correctly partitions inboxes; the content cache does not. The local isolation failure is confirmed. Whether production permits the particular
  cross-inbox ID collision needed to trigger it was not established.

  Suggested fix and cost: Namespace every cache key by API/account namespace, inbox, resource kind, and remote identity/version. Small cost.

  ### F3. Critical — Malformed LOGIN credentials leak an Authorization value into logs

  Location: imapgw/config.py:115, imapgw/commands.py:84, imapgw/session.py:193, imapgw/apiclient.py:97.

  Evidence: Send a LOGIN password literal containing:

  b"synthetic_secret\r\nX: 1"

  http.client rejects the header. The actual session error log includes:

  ValueError: Invalid header value b'Bearer synthetic_secret\r\nX: 1'

  The filter only changes record.msg and record.args; exception formatting happens afterward. LOGIN also unregisters the credential before dispatch logs the
  exception.

  Separately, the registry is a set, not reference-counted: two sessions registering the same key followed by one unregister removes protection for the remaining
  session.

  This contradicts README’s “redacted from all log output” claim.

  Suggested fix and cost: Validate credentials before constructing headers; never log header-bearing exceptions verbatim; redact the final formatted output,
  including exception chains; reference-count registrations. Small to medium cost.

  ### F4. High — A healthy locked database is discarded as corrupt

  Location: imapgw/uidstore.py:65, particularly lines 75–85.

  Evidence: On a disposable valid store containing an existing UID mapping:

  lock.execute("PRAGMA journal_mode=DELETE")
  lock.execute("BEGIN EXCLUSIVE")
  replacement = UidStore(path)

  After approximately 5.2 seconds, the server renamed the healthy database to uid.sqlite.corrupt-1700000000 and opened an empty replacement.

  sqlite3.OperationalError subclasses DatabaseError, so locking, read-only access, disk errors, and corruption enter the same recovery branch. Existing users of
  the old database can also continue operating against a different file from the replacement.

  Suggested fix and cost: Recover only from positively identified corruption. Fail safely on locking, permissions, disk exhaustion, incompatible schema, and
  failed recovery operations. Preserve the original mapping. Small to medium cost.

  ### F5. High — UIDVALIDITY can be reused when the UID database is recreated

  Location: imapgw/uidstore.py:82, imapgw/uidstore.py:130, README.md:198, DESIGN.md:221.

  Evidence: With the supported injectable clock fixed at 1700000000, create a store assigning UID 1 to old, move the database aside, and recreate it assigning
  UID 1 to new.

  Both stores report:

  UIDVALIDITY 1700000000
  UIDNEXT 2

  The same mailbox/UIDVALIDITY/UID now identifies different messages. Recovery never reads or preserves a previous validity watermark. Clock rollback has the
  same problem; values also lack 32-bit bounds.

  The existing tests avoid this defect by advancing the clock five seconds or sleeping 1.1 seconds.

  Suggested fix and cost: Persist a generation watermark independently of the replaceable UID database, with explicit recovery and 32-bit exhaustion behavior.
  Guaranteeing an increase after deleting all durable state requires retaining some external state. Medium cost.

  ### F6. High — APPEND returns failure after successfully creating a draft

  Location: imapgw/mailbox.py:377, imapgw/mailbox.py:382, imapgw/session.py:180.

  Evidence: Against the unchanged fake:

  fake.fail_next(
      method="GET",
      path_prefix="/inboxes/candidate%40imap.test/drafts",
      status=503,
  )

  Then APPEND a valid plain-text draft. POST succeeds; the following listing fails:

  A002 NO [UNAVAILABLE] upstream unavailable, try again (APPEND)

  The fake contains three drafts instead of two. The client is told the operation failed although the mailbox changed. Retrying against this fake can create
  another draft.

  This violates APPEND’s failure atomicity requirement. RFC 3501, §6.3.11

  Suggested fix and cost: Establish a durable commit boundary. Use the successful create response to record the created draft and allocate its UID; do not turn a
  subsequent refresh failure into an APPEND failure. Handle ambiguous POST outcomes separately. Medium cost.

  ### F7. High — A single unfinished command can retain unlimited literal data

  Location: imapgw/parser.py:109, imapgw/parser.py:132, imapgw/parser.py:169, imapgw/session.py:125.

  Evidence: Start:

  A ID {4096}

  Then repeatedly send 4096 bytes followed by another  {4096}\r\n, completing every continuation handshake but never completing the command.

  After 128 literals, the parser retained 524,421 bytes. There is no cumulative command-byte, literal-count, token-count, or assembly-time limit. The 1 MiB cap
  applies independently to each literal, and command timeout starts only after parsing finishes.

  A separate probe showed a 0.2-second idle limit allowing a command assembled over 0.6 seconds when bytes arrived every 0.1 seconds.

  Suggested fix and cost: Bound total command bytes, literal count, nesting, and assembly duration. Keep idle timeout distinct from an absolute command-assembly
  deadline. Small to medium cost.

  ### F8. High — LIST wildcard patterns can monopolize the event loop

  Location: imapgw/commands.py:136, imapgw/commands.py:156.

  Evidence:

  _wildcard_regex("*" * 1000 + "Z").fullmatch("INBOX")

  This failed to finish before the isolated probe process was killed after 1.5 seconds. Each wildcard becomes .*, creating pathological backtracking even against
  a five-character mailbox name.

  The corresponding authenticated LIST command executes synchronously on the shared event loop. asyncio.timeout cannot interrupt it.

  Suggested fix and cost: Use a bounded wildcard matcher appropriate to the fixed flat namespace; at minimum collapse adjacent wildcards and enforce pattern
  limits. Small cost.

  ### F9. High — Output backpressure can hang shutdown

  Location: imapgw/session.py:102, imapgw/session.py:258, imapgw/server.py:79.

  Evidence: A probe supplied a writer whose drain() remained blocked. Server.stop() hung in the first session’s close operation and never reached session-task
  cancellation.

  Ordinary post-command flushing also occurs outside the command timeout. A client that stops reading can therefore retain a session indefinitely. The two-second
  wait_closed() timeout does not cover the preceding flush.

  Suggested fix and cost: Apply a deadline to draining, abort stalled transports, cancel session work before awaiting graceful shutdown, and bound total shutdown
  time. Small to medium cost.

  ### F10. High — HTTP redirects are accepted as successful message downloads

  Location: imapgw/apiclient.py:325, imapgw/apiclient.py:206, imapgw/mailbox.py:343.

  Evidence: Inject a 302 on /raw/, then fetch UID 1:

  * 1 FETCH (UID 1 RFC822.SIZE 59 BODY[] {59}
  {
    "error": {
      "message": "Injected test failure"
    }
  })
  A003 OK UID FETCH completed

  The real message is 310 bytes. The 59-byte error body is cached as its immutable content because every status below 400 counts as success. Length mismatches
  merely generate warnings.

  The transport does not follow redirects, so I did not reproduce credential forwarding through redirects. The confirmed problem is serving and caching the
  redirect response body.

  Suggested fix and cost: Require appropriate 2xx success statuses, reject unexpected redirects and empty/partial download responses, and validate content before
  caching it. Small cost.

  ### F11. High — Malformed successful listings can erase the live view or duplicate UIDs

  Location: imapgw/apiclient.py:259, imapgw/mailbox.py:208, imapgw/mailbox.py:239.

  Evidence: After a valid sync, returning:

  {"error": {"message": "wrong shape"}}

  with HTTP 200 causes an apparently successful empty sync and tombstones every live item. The required messages member silently defaults to [].

  A second probe supplied the same message twice in a listing. The resulting snapshot contained UIDs:

  [1, 1]

  UID allocation deduplicates keys, but snapshot construction does not deduplicate entries.

  These are injected upstream-shape failures, not claims that production normally emits these responses.

  Suggested fix and cost: Validate required collection fields, items, IDs, and pagination tokens before committing reconciliation. Deduplicate identical entries
  and reject conflicting duplicates. Small to medium cost.

  ### F12. High — APPEND can join a listing that predates its own creation

  Location: imapgw/mailbox.py:189, imapgw/mailbox.py:382, imapgw/commands.py:400.

  Evidence: Hold a Drafts listing in flight with an empty result captured before creation. Create a draft, then release the old listing.

  The APPEND “forced” sync joins that existing task. The probe returned:

  uid is None
  snapshot.uidnext == 1

  The draft was created, but APPEND cannot report APPENDUID or announce it in the selected view. force=True bypasses snapshot age, not the age of an in-flight
  operation.

  Suggested fix and cost: Give mutations a generation barrier: their reconciliation must start after the mutation commits, or incorporate the returned resource
  directly. Medium cost.

  ### F13. Medium — Coalescing transfers one key’s authentication failure to another key

  Location: imapgw/mailbox.py:183, imapgw/mailbox.py:191, imapgw/session.py:173.

  Evidence: Start a sync with a revoked key; while it is pending, request the same inbox/mailbox using a valid client. Both callers receive AuthError; the valid
  client makes zero upstream requests.

  At session level, that error triggers “credentials no longer valid” and disconnection for the valid session. Differently scoped credentials can also share a
  result without their authorization differences being considered.

  Suggested fix and cost: Partition in-flight work by authorization identity/scope, or explicitly reauthorize callers and isolate authentication failures. Medium
  cost.

  ### F14. Medium — Error responses echo arguments and permit response-line injection

  Location: imapgw/commands.py:428, imapgw/fetch.py:61, imapgw/responses.py:15, imapgw/parser.py:179.

  Evidence:

  UID FETCH synthetic_secret FLAGS

  returns:

  BAD invalid sequence number 'synthetic_secret'

  More seriously, a CHARSET literal containing X\r\n* BYE injected produces:

  A003 NO [BADCHARSET (UTF-8)] charset X
  * BYE INJECTED is not supported

  Client-supplied text becomes an additional protocol response line. Malformed tags containing CR are also copied into parser-error responses without validation.

  This falsifies DESIGN’s blanket claim that protocol errors never echo arguments.

  Suggested fix and cost: Use fixed error descriptions, validate tags before retaining them for responses, and reject CR/LF/control characters in every response-
  text encoder. Small cost.

  ### F15. Medium — NOOP silently misses flag-change notifications

  Location: imapgw/session.py:228, imapgw/mailbox.py:421.

  Evidence:

  Session A: SELECT INBOX
  Session B: SELECT INBOX
  Session A: UID STORE 1 +FLAGS (\Seen)
  Session B: NOOP

  B receives only tagged OK. A subsequent explicit UID FETCH 1 FLAGS reports \Seen.

  flush_pending() handles membership changes only; it has no per-session flag baseline. A polling client can therefore retain stale read/star state indefinitely
  despite successful NOOPs.

  Suggested fix and cost: Track last-announced flags per selected session and emit changed FLAGS during synchronization. Medium cost.

  ### F16. Medium — CLOSE reports success after failed deletion and loses the retry state

  Location: imapgw/commands.py:600.

  Evidence: Mark UID 1 deleted, inject a PATCH 503, then CLOSE:

  A004 OK CLOSE completed

  The item is not trashed. Reselecting INBOX shows it without \Deleted, because CLOSE discarded the session containing the only deletion mark.

  The caught failure is operationally significant. “Silent expunge” means suppressing EXPUNGE responses, not concealing failed removal.

  Suggested fix and cost: Return failure and preserve retryable state, or durably record outstanding deletion intent. Small to medium cost.

  ### F17. Medium — SEARCH returns incorrect results for encoded content and invisible keywords

  Location: imapgw/search.py:223, imapgw/search.py:257.

  Evidence: On the existing UTF-8 fixture:

  UID SEARCH CHARSET UTF-8 SUBJECT <literal café>  → * SEARCH 2
  UID SEARCH CHARSET UTF-8 TEXT    <literal café>  → * SEARCH

  TEXT searches encoded raw header bytes rather than decoded text. A separate base64 body containing café also fails CHARSET UTF-8 BODY.

  Additionally, SEARCH KEYWORD starred matches UID 2 even though FETCH never exposes starred as an IMAP keyword. SEARCH queries upstream labels instead of the
  advertised flag model.

  Suggested fix and cost: Decode MIME transfer encodings, charsets, and encoded headers for text search; evaluate keywords against IMAP flags or explicitly
  reject unsupported keyword behavior. Medium cost. The decoding requirement is explicit in RFC 3501, §6.4.4.

  ### F18. Medium — A large literal numeral escapes parser error handling and closes the connection

  Location: imapgw/parser.py:164, imapgw/session.py:136.

  Evidence:

  b"A APPEND Drafts {" + b"9" * 4301 + b"}\r\n"

  This is below the line limit but raises Python’s integer-conversion ValueError. It occurs while iterating parser events, outside dispatch’s exception boundary.

  The raw-socket probe received EOF without BAD or BYE; the event loop received an unhandled exception. The server process survived, but README’s malformed-input
  guarantee is false.

  Suggested fix and cost: Validate decimal length before conversion, catch conversion errors, and add a parser-event exception boundary. Small cost.

  ### F19. Medium — APPEND silently replaces undecodable body bytes

  Location: imapgw/drafts.py:124.

  Evidence:

  parse_append(
      b"Content-Type: text/plain; charset=utf-8\r\n\r\n\xff"
  ).text
  # '\ufffd'

  get_content() defaults to replacement decoding, so the surrounding UnicodeDecodeError handler does not catch this case. The server can accept and save altered
  text instead of rejecting an undecodable message.

  Suggested fix and cost: Decode strictly and reject malformed charset/transfer-encoding input before POST. Small cost.

  ### F20. Medium — LF-only header subsets return missing or unrelated fields

  Location: imapgw/fetch.py:207, imapgw/fetch.py:218.

  Evidence:

  raw = b"From: a@x\nSubject: secret\n\nbody"

  BODY.PEEK[HEADER.FIELDS (Subject)] returns only b"\r\n". Requesting From returns both From and Subject, with extra line endings.

  The initial splitter explicitly accepts LF-only messages, but the header filter only splits CRLF and consequently treats the entire header as one field.

  Suggested fix and cost: Parse physical header lines with their actual terminators and preserve selected byte spans. Also avoid inventing a blank separator for
  header-only messages without one. Small cost.

  ### F21. Medium — FETCH deduplication removes a required Seen side effect

  Location: imapgw/fetch.py:134, imapgw/commands.py:287.

  Evidence:

  UID FETCH 1 (BODY.PEEK[] BODY[])

  returns OK but leaves the message unread. Both requests normalize to the label BODY[]; deduplication retains the first PEEK attribute and drops the non-PEEK
  attribute before sets_seen is calculated.

  Suggested fix and cost: Calculate side effects from the complete request, or merge duplicate attributes while preserving non-PEEK semantics. Small cost.

  ## 3. Design challenges

   Decision                                          Verdict and proposed amendment
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   D1: Python and stdlib email                       Agree. The IMAP implementation is handwritten; email is used for message-format work. The main weakness is
                                                     treating its lenient decoding as validation.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D2: Zero runtime dependencies                     Defensible for this trial. An SDK is optional, but custom HTTP handling still needs correct status,
                                                     deadline, and response validation. Dependency count is not evidence of reliability.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D3: Incremental parser                            Agree with the architecture. Add aggregate resource limits and validate literal context before issuing
                                                     continuations. The existing caps do not bound a command.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D4: Serial commands per session                   Agree. Pipelining worked in the probes. Shared-loop synchronous regex and SQLite work, and unbounded drain
                                                     waits, undermine the concurrency argument.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D5: Label views and local trash/spam exclusion    Agree: explicit local exclusion makes the mapping robust to the fake’s unusual filtering. These remain
                                                     overlapping views, not mutually exclusive folders.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D6: Persistent UIDs and draft content hashes      Keep content-hash versioning rather than changing bytes behind an existing UID. Amend deletion semantics,
                                                     cache isolation, recovery, and metadata persistence. Hashing does not preserve old versions or prevent
                                                     stale deletion.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D7: List size as RFC822.SIZE                      Reverse for the correctness-focused slice. Use downloaded bytes, or raw metadata followed by strict
                                                     consistency checks. SEARCH also continues using the old size_hint even after the cache learns another size.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D8: Draft projection                              Plain-text projection is reasonable. Strictly validate decoding. Ignoring APPEND date/flags is disclosed,
                                                     but the parser also accepts malformed optional-argument ordering rather than validating the promised
                                                     grammar.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D9: Full listing and polling                      Agree with full listings until after= semantics are established. Add mutation barriers, authorization-aware
                                                     coalescing, cancellation ownership, and flag notifications.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D10: Errors and security                          Reverse the “log everything then redact” approach. Keep credential-bearing values out of errors, validate
                                                     upstream schemas, and distinguish pre-commit failures from committed mutations.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D11: Testing                                      Strong fixture-level byte checks; weak boundary and concurrency coverage. Some tests encode the
                                                     implementation’s assumptions instead of checking the protocol independently.
  ────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   D12: Extra commands                               The expanded command surface exceeded the demonstrated core reliability. Resolve persistence, APPEND,
                                                     parsing, and deletion failures before adding further capabilities.

  Additional decisions requiring amendment:

  - Session-local \Deleted: Allowed as a session-only flag concept, but it is advertised in PERMANENTFLAGS. That promises persistence across concurrent and
    subsequent sessions which the implementation does not provide. Remove it from that advertisement or persist it. Rejecting deletion in Trash/Spam is a
    reasonable disclosed limit.

  - Non-PEEK setting \Seen: Agree. Once these FETCH forms are supported, the side effect is expected. Its effect on agent labels deserves clear documentation,
    which largely exists.

  - Bare LF commands: Reasonable explicitly disclosed convenience. It does not justify accepting malformed literal contents or generating malformed responses.
  - RECENT always zero: A disclosed simplification, internally consistent with NEW returning none and OLD returning all. It sacrifices notification semantics; it
    should not be presented as full recent-message tracking.

  - Content-hash stability: Two independent Python processes produced identical draft hashes. However, changing only updated_at changed the Date header and hash.
    DESIGN’s claim that no-op/label-only updates cannot churn UIDs is therefore false whenever such updates advance updated_at.

  - Immutable metadata: A scripted timestamp edit changed INTERNALDATE while retaining UID 1. The code does not persist immutable message metadata. Whether
    production edits that field is unconfirmed; the local behavior is reproduced.

  The scaling claims in DESIGN §5 are not established:

  - Coalescing is per inbox and mailbox, not per inbox. Sequential forced NOOPs each relist; coalescing only reduces overlapping work.
  - Sixteen occupied HTTP workers prevented an unrelated request from starting, even beyond its supplied request timeout. The queue wait has no independent
    deadline.

  - Cancelling a sync waiter leaves the shielded task alive; a probe confirmed it later committed. The service has no explicit task shutdown/join lifecycle.
  - SQLite operations are synchronous and can wait for locks. There is no token bucket, circuit breaker, bounded executor admission queue, or multi-host
    reconciliation protocol.

  - UID allocation is transactional, but allocation and tombstoning are separate transactions. A unique index alone does not prove that concurrent stale listings
    cannot invalidate each other.

  - No throughput benchmark supports the stated million-request/day path. The proposed future components are reasonable directions, not demonstrated properties.
  - DESIGN describes API rate limits as organization-based, while the current public guide describes per-key limits. AgentMail rate-limit documentation

  Production/API assumptions and documentation accuracy:

   Claim or assumption                                     Assessment
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   List size is documented as raw .eml length              Overstated in README.md:220. The list schema says message size; the raw endpoint explicitly says raw-
                                                           message size. Production equality remains unconfirmed. List schema, raw schema
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Trash/spam filtering with label queries                 The requested parameters are documented; their exact interaction with label filters is not
                                                           established by the reviewed reference. No production failure claimed.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   limit=100                                               No reviewed endpoint documentation established that this value is invalid. Draft limits and token
                                                           snapshot stability remain unverified; do not claim a proven compatibility bug.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Draft updated_at is a perfect content-version marker    Unconfirmed. An unchanged timestamp can hide an edit until cache eviction; a changed timestamp itself
                                                           changes rendered bytes.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Draft create fields                                     The emitted fields are documented. Empty recipient lists are omitted. No evidence was found that
                                                           empty subject/text are rejected. Create Draft
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Every 403 means revoked credentials                     Incorrect as a general claim. Permission failures are also documented. A denied write or expired
                                                           download URL should not automatically imply invalid LOGIN credentials. AgentMail 403 documentation
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Retry-After is fully honored                            Only numeric values are parsed; HTTP-date values fall back to exponential backoff. No production
                                                           incidence was tested.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Literal-hash client_id represents retry identity        Questionable: two intentional identical APPENDs receive the same ID. Production duplicate-resource
                                                           prevention is documented, but exact repeated-APPEND behavior was not tested.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   “Message content never becomes str”                     Only defensible for received-message passthrough. APPEND and SEARCH explicitly decode content; drafts
                                                           are regenerated.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Refresh occurs when snapshots age                       Overstated in README.md:208. Even with refresh interval zero, FETCH after adding a message still
                                                           returned only UIDs 1–3. FETCH/SEARCH/STORE do not perform an age-based refresh of an existing
                                                           snapshot.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   “No headers at all” on downloads                        Means no application-supplied headers. http.client still supplies normal HTTP headers. The meaningful
                                                           no-Authorization property passed.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Fake failure injection happens after raw handling       notes/harness-observations.md:24 is wrong: injection precedes the raw route. Raw-download failure
                                                           tests do execute.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   README FETCH syntax table                               “All with .PEEK and partials” is too broad: modifiers apply to supported BODY forms, not arbitrary
                                                           RFC822 or metadata names.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   README keyword rejection                                It says BAD, but STORE keywords return NO [CANNOT]; SEARCH accepts upstream labels as keywords.
  ──────────────────────────────────────────────────────  ───────────────────────────────────────────────────────────────────────────────────────────────────────
   Subscription support                                    UNSUBSCRIBE returns OK but changes nothing; subsequent LSUB still lists everything. “Minimal
                                                           responses” should explicitly disclose the no-op semantics.

  No production systems were accessed. Unverified production behavior is not treated as a submission failure by itself.

  ## 4. Test gaps

  Concrete tests that should exist:

  - APPEND POST succeeds, subsequent listing fails, and client-visible status agrees with committed state.
  - APPEND overlaps a listing started before creation and still returns the correct APPENDUID.
  - Stale draft UID deletion preserves or rejects a newer upstream version.
  - Two inboxes with the same message/draft ID never share cached bytes.
  - Same-second database loss and recovery never reuse UIDVALIDITY.
  - Locked, read-only, full-disk, unsupported-schema, and rename-failure paths preserve the UID database.
  - Malformed 200 pages and duplicate IDs cannot tombstone valid state or duplicate sequence entries.
  - Aggregate literal count/bytes and command-assembly time are bounded before authentication.
  - Long numeric literals produce BAD without unhandled exceptions or silent disconnection.
  - Adversarial LIST patterns cannot block another session.
  - Backpressured clients cannot block shutdown or retain slots indefinitely.
  - Sixteen slow upstream requests do not indefinitely prevent an unrelated authorized request from starting.
  - Cancellation and shutdown account for shielded sync tasks and active HTTP workers.
  - Real formatted tracebacks never contain credentials, including malformed literal credentials.
  - Two sessions sharing a key retain redaction after either session disconnects.
  - Error text and malformed tags cannot inject CRLF responses.
  - NOOP reports flags changed by another session or upstream.
  - CLOSE preserves deletion intent and reports upstream removal failures.
  - UTF-8 BODY/TEXT searches decode base64, quoted-printable, and encoded headers.
  - HEADER.FIELDS preserves folded/duplicate fields across CRLF, LF, and header-only messages.
  - Duplicate PEEK/non-PEEK FETCH attributes preserve required side effects.
  - APPEND rejects undecodable content without changing the mailbox.
  - HTTP 3xx/304 and malformed downloads cannot become cached message bodies.
  - Repeat identical APPENDs distinguish intentional additions from transport retries.

  Specific weaknesses in existing tests:

  - tests/integration/test_failures.py:78 resets the fake before checking draft count. That assertion cannot detect APPEND side effects.
  - test_partial_listing_failure_keeps_view says it faults page two, but its prefix faults the first listing request. The separate unit test does cover a real
    page-two failure.

  - The UIDVALIDITY tests deliberately advance time, masking the same-second failure rather than testing it.
  - Slow-upstream tests exercise only one slow request and a cached operation or CAPABILITY. They do not establish isolation under worker saturation.
  - Fixed sleeps and one-second timing assertions are CI-flake candidates. Fake startup’s blocking readline() also makes its apparent readiness deadline
    ineffective if the process stays alive without printing.

  - Reset clears pending failure entries between normal tests. However, teardown does not join shielded syncs or running HTTP workers, so delayed operations can
    outlive test boundaries.

  - ImapTestClient normally requires CRLF and consumes exact literal lengths—good—but accepts partial lines at EOF and does not generally validate FETCH grammar
    or closing parentheses.

  - The test client stops at tagged completion; later untagged data can be assigned to the next command without identifying an ordering violation.
  - Its timeout exception embeds recent client transcript bytes. scripts/smoke.py masks the transcript but prints failure-detail strings separately, leaving
    another potential credential-output path on timeout.

  - Smoke can accept an empty fetched message set and vacuously accept missing draft FETCH responses. It also uses fixed ports and tests port openness rather
    than confirming the newly started process owns the listener.

  The handwritten test client is not a prohibited imported protocol library. I found no violation of the assignment’s library rule.

  ## 5. What you tried that did not break

  - The baseline required flow: LOGIN, mailbox discovery, selection, metadata/raw FETCH, draft inspection, APPEND, NOOP, and LOGOUT.
  - Exact raw hashes and byte lengths for the received fixtures, including the 375-byte UTF-8 canary.
  - Multi-section FETCH with two literals, including HEADER plus TEXT reconstructing the complete message.
  - Pipelined FETCH commands sent before the first response completed; responses remained ordered.
  - One-byte-at-a-time literal input, zero-length literals, and a literal inside a parenthesized list.
  - Normal quote/backslash escaping, malformed quotes, non-ASCII atoms/quoted strings, and tags containing + or *.
  - *, 559:* beyond the largest UID, and reversed ranges.
  - UID FETCH/STORE responses included UID where required; UID EXPUNGE without \Deleted left the message intact.
  - Partial EXPUNGE with two successes and one failure emitted 3 EXPUNGE, then 1 EXPUNGE, and reported failure.
  - A second session retained its old sequence numbering until NOOP; subsequent sequence numbers mapped correctly.
  - Failed SELECT due to API failure deselected the previous mailbox.
  - Repeated LOGIN, wrong-state commands, EXAMINE restrictions, and normal logout behavior.
  - Bcc-only/header-only APPEND parsing; Latin-1 base64 and quoted-printable bodies; multipart/alternative with plain text second; multipart/mixed containing
    only plain text; folded encoded-word subjects.

  - Rendering the same draft in two independent Python processes produced identical bytes.
  - Normal UID persistence, tombstone/reappearance behavior, stable existing UIDs after new arrivals, and per-inbox database partitioning.
  - Complete normal pagination, repeated-token detection, and preservation of the previous view after an explicit page failure.
  - No Authorization header on raw downloads; the transport does not follow redirects.
  - Exact allowlist comparison has no normalization-based acceptance path; unknown/non-inbox scope types take the additional inbox lookup rather than bypassing
    it.

  Coverage limits: I did not run Thunderbird, establish cross-Python-version rendering stability, fill the disk, exhaust machine memory, or exercise production
  credentials. Full physical resource exhaustion and production-specific timing/authorization behavior remain unconfirmed; the report identifies the bounded
  probes and code paths supporting each related conclusion.