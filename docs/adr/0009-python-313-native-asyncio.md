# Python 3.13 minimum, native asyncio

The minimum supported version is Python 3.13. Graceful shutdown depends on two things that 3.12 lacks:

- In 3.12, a TaskGroup loses the cancellation when an external cancel and a child-task failure happen at the same time.
- 3.12 has no `Queue.shutdown`.

The concurrency model is native asyncio, without AnyIO, because:

- The browser driver creates native tasks internally, which AnyIO cancel scopes do not cancel.
- AnyIO cancellation is level-triggered, so user code that swallows a cancellation busy-loops.
