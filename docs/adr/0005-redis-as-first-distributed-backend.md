# Redis is the first distributed backend

One Redis holds the four kinds of state shared by multiple Workers: the Frontier, Session Pools, rate-limit budgets, and control state.

The Frontier is implemented with sorted sets and Lua scripts, covering priority, delays, and token-carrying Leases. Streams are not used because:

- Streams have no message priority.
- A late ack takes effect by default and deletes the new holder's entry.

## Considered Options

PostgreSQL has better transactional semantics: deduplication, enqueueing, and writing results can share one transaction. Its throughput and operational cost are both worse than Redis, so it comes later, sharing the same protocol and conformance test suite.

## Consequences

- Redis must run with AOF persistence; otherwise a server restart loses the Frontier.
- Only commands also supported by Valkey are used.
- All keys of one Run or one Session Pool share a hash tag, for Redis Cluster compatibility.
