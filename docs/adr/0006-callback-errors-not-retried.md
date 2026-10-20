# Exceptions raised in a Callback are not retried by default

## Decision

When a Callback raises:

- The Request is marked as failed and its errback is called.
- Results produced before the exception are kept.
- A Callback that genuinely needs a retry raises `Retry()` explicitly.

## Why

Exceptions in a Callback usually come from parsing logic that does not match the page structure. Re-running the same Request most likely gives the same result and produces duplicate Items.

Blocked pages are detected by the framework before the Callback is called and retried with another Session; this does not depend on the Callback raising.

## Exception: kept pages that went away

A Callback that keeps its page (`Render(keep_page=True)`) works with the browser while it runs. When it raises while that page crashed, or the page's browser was lost, the failure is the browser's, not the parsing logic's, and running it again on a new page likely succeeds. Its error is then replaced by the `PageCrashed` or `BrowserLost` the fetch would have failed with, with the Callback's own error as the cause, and the retry policy judges it as it would that fetch failure: the default policy retries it as a crash, or under the `browser_lost` retry budget. Results produced before the exception are kept, as for `Retry()`.

## Time limits

A Callback that runs longer than `callbacks.timeout` is cancelled and fails with `CallbackTimeout`. It is a Callback error like any other: it is not retried, the errback gets it, and the results produced before are kept. Running the Callback again would most likely wait on the same thing again; a Callback that knows what it waits for can raise `Retry()` itself. The limit is off by default.
