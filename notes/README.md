# Reference notes

Condensed local copies of the material linked from `API_RESOURCES.md`, plus observations from running the fake API. Fetched 2026-09-19. Official docs are the source of truth; any docs page is available as clean markdown by appending `.md` to its URL (e.g. `https://docs.agentmail.to/drafts.md`), and the full index is at `https://docs.agentmail.to/llms.txt`. The OpenAPI 3.1 spec is at `https://docs.agentmail.to/openapi.json`.

| File | Source |
| --- | --- |
| `agentmail-api.md` | API reference pages for Inboxes, Messages, Drafts, Auth; Messages, Drafts, Inboxes, Labels guides; error and rate-limit pages |
| `agentmail-production-imap.md` | `https://docs.agentmail.to/imap-smtp` (AgentMail's own hosted IMAP, the closest prior art) |
| `imap-rfc3501.md` | RFC 3501 sections that govern the required command slice |
| `harness-observations.md` | Behavior of `test-harness/` observed by reading the code and probing it live |
| `assignment-analysis.md` | Problem framing, objectives, design decision space, risks, and open questions |
