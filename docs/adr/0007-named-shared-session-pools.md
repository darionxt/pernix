# Session Pools are shared by name across Spiders, with optional credential encryption

A Session Pool on Redis uses its own name as its namespace and is not tied to a `run_id`. Different Spiders and different Runs can therefore share one account pool or proxy pool, and concurrency limits, Cooldowns, and quotas apply to all users together. Isolating pools per Run would let two Spiders use the same Account at the same time.

Credentials (cookies, tokens, account passwords) are stored as follows:

- With a configured key, they are encrypted with AES-GCM.
- Without a key, they are stored in plaintext and a warning is logged at startup, so local development does not require a key.

Boundary: the framework provides only a minimal management API and CLI. Pool dashboards, alerting, and account registration belong to the Platform.
