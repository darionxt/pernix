# Requests are independent by default; shared state is opt-in through a Session Pool

By default, no cookies persist from one Request to another, on the HTTP side and in the browser. A Request sends only the cookies it carries (`Request(cookies=...)`), cookies flow within its own redirect chain as in a browser, and the cookies held after the fetch are exposed as `Response.cookies`. State that must persist across Requests, such as a login or an anti-bot cookie, comes from a Session Pool, which a Spider enables explicitly.

## Why

- The outcome of a Request does not depend on which other Requests happened to finish before it, so concurrency, retries, resumed crawls, and multiple Workers need no shared cookie state.
- Persisting state is a decision with consequences for identity and anti-detection; it belongs with the Session Pool, which also binds proxies and Profiles, rather than an implicit per-Spider cookie jar.

## Consequences

- Sites that rely on cookie continuity need either `cookies=response.cookies` on follow-up Requests or a Session Pool. This differs from frameworks that share one cookie jar per Spider, and must be stated prominently in the documentation.
- Connections are still reused between independent Requests. Many independent visitors sharing one connection can itself be a signal; full identity separation comes from Sessions with their own proxies.
- `cookies` is not part of the default Fingerprint, so the same URL with different cookies is a duplicate unless `dont_filter` or an explicit Fingerprint says otherwise.
