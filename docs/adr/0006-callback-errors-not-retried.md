# Exceptions raised in a Callback are not retried by default

## Decision

When a Callback raises:

- The Request is marked as failed and its errback is called.
- Results produced before the exception are kept.
- A Callback that genuinely needs a retry raises `Retry()` explicitly.

## Why

Exceptions in a Callback usually come from parsing logic that does not match the page structure. Re-running the same Request most likely gives the same result and produces duplicate Items.

Blocked pages are detected by the framework before the Callback is called and retried with another Session; this does not depend on the Callback raising.
