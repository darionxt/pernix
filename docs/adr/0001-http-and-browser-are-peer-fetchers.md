# HTTP and the browser are peer Fetchers, switched by the Request's `render` attribute

The fetch method is chosen per Request, not per Spider type. A single Spider can mix HTTP and the browser: a Request with `render` goes to the browser Fetcher, otherwise it goes to the HTTP Fetcher. Both share the same Session (cookies, proxy, Profile), so login state carries across the two.

## Considered Options

- **Choose the transport by Spider type**: no per-Request switching. A site with both static and dynamic pages would have to be split into two Spiders.
- **`fetcher="browser"` plus a separate `browser=` option**: same expressiveness, more verbose. `fetcher=` is kept for selecting custom transports.
- **A separate `BrowserRequest` type**: upgrading a Blocked HTTP Request to a rendered one would require a type conversion, and the two request types would need separate deduplication, retry, and persistence handling. With a `render` attribute, a Middleware upgrades a Request with `request.replace(render=True)`.
