# The Frontier uses token-carrying Leases with at-least-once delivery

When a Worker claims a Request from the Frontier, it gets a Lease carrying a token. The Request then ends in one of three ways:

- Done: `ack`.
- Try again later: `nack(delay)`.
- Failed for good: `dead`, kept as a Dead Request.

Lease rules:

- Leases are renewed automatically while the Request is being processed.
- Every write is conditional on `(id, token)`; writes from an expired Lease have no effect.
- Deduplication and enqueueing happen in one atomic operation.

## Why

- **Not "delete on dequeue"**: a Worker crash would lose its in-flight Requests.
- **Not "record the Fingerprint first, enqueue second"**: a crash between the two steps loses the Request.
- **Not exactly-once**: the side effects of crawling (the target site, downstream storage) are not in the same transaction; downstream writes must be idempotent on a business key.

## Acknowledging after the Items

A Request is acknowledged, returned for a retry, or kept as a Dead Request only once the Items its Callback and its errback yielded are processed: stored, dropped, or failed and passed through the failure Pipelines.

- **Not once the Items are queued**: a crash would lose them while the Frontier holds the Request as done, so a resumed Run would never produce them.
- **Workers do not wait**: a worker goes on to the next Request while the Lease waits, renewed; the Item queue bounds how many Requests wait so.
- **Every Frontier**: the in-memory one acknowledges the same way, so there is one semantics to test and document.
