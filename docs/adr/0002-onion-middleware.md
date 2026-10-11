# Middleware uses the onion model, not node-style hooks

A download Middleware is `async def __call__(request, call_next)`, and a spider Middleware is `async def __call__(response, call_next)` (an async generator). They are not split into `process_request` / `process_response` / `process_exception` node hooks.

Why the onion:

- A single function can short-circuit (not calling `call_next`, e.g. a cache) or call `call_next` several times (retries, resending after an auth challenge).
- The execution order is naturally symmetric between the request and response directions.
- Exceptions propagate through the chain naturally; an exception raised in one phase cannot bypass exception handling.

The cost is that users used to node-style hooks need to adapt, so a `NodeMiddleware` adapter is provided.

## Consequences

Middlewares inside the retry Middleware run again on every retry; order registrations accordingly.
