# Basic anti-detection relies on consistency, not synthesized browser characteristics

**HTTP**

- Uses a pinned Chrome Profile whose TLS and HTTP/2 are those of the bundled browser engine.
- Does not use aliases such as `chrome` that drift when dependencies are upgraded, so the Profile only changes on an explicit upgrade.
- Sends the headers that name the client as the bundled browser sends them: `User-Agent`, `sec-ch-ua`, `sec-ch-ua-mobile`, and `sec-ch-ua-platform` for the platform the Run is on, and the `Accept-Language` Chrome makes of the `locale` setting. They are recorded for the pinned build in `pernix.fetchers.pinned_pair`, so that a Run that never renders needs no browser, and replace the Profile's own in place. Another Profile, chosen in the settings, by a Request, or by a Session, sends its own.
- Sends the headers Chrome sends for each kind of request, measured on the bundled build: a top-level navigation, a form submission, an iframe's navigation, a `fetch()` or `XMLHttpRequest`, and the loads of a style sheet, a script, and an image, in Chrome's order, with their `Priority` and HTTP/2 stream weight, and with the `Referer`, `Origin`, and `Sec-Fetch-Site` made from the page the request comes from. A Request names no kind: its method and body tell it, or its `Sec-Fetch-Dest` header, and its `Referer` header names the page, so that what it sends is plain from its headers and persists with them.

**Browser**

- Uses patchright's real characteristics, brands included: Chrome for Testing names only the `Chromium` brand in `sec-ch-ua`, and neither side adds `Google Chrome`.
- Does not inject generated fingerprints. In headless mode, its contexts present the browser's own User-Agent with `HeadlessChrome` turned into `Chrome`, as the same build presents itself with a window, through patchright's `user_agent` option, which reaches every page, frame, and worker. A Chrome `--user-agent` switch would empty every high-entropy client hint, and a context `locale` would move `Accept-Language` out of its place, so neither is used.
- Presents the languages of the `locale` setting, `["en-US", "en"]` by default, through Chrome's `--accept-lang`. Chrome replaces some lists with its own for their first language, such as `de-DE,de` with `de-DE,de,en-US,en`; the browser Fetcher then warns, naming the list to set.
- On Linux, the high-entropy client hints the browser gives scripts are its own. On macOS and Windows, patchright's `user_agent` option gives them an architecture and a platform version it guesses from the User-Agent, such as `x86` and `10_15_7` on a Mac with Apple silicon, where the build itself gives `arm` and the OS version.

**Why no randomization**

Randomly generated headers or page properties only change the surface; they cannot change the TLS and HTTP/2 characteristics at the same time. The result is a UA, Client Hints, and TLS characteristics that contradict each other, which is easier to detect, not harder.

**The pinned pair**

- The browser Fetcher launches the Chrome for Testing build bundled with patchright 1.62.3 (Chrome 151.0.7922.34), and the HTTP Fetcher impersonates the `chrome150` target of curl-cffi 0.16.3. Chrome 151 changed nothing of its TLS and HTTP/2 from 150.
- Both are pinned to exact versions in `pyproject.toml`, and the build's version and brands are recorded in `pernix.fetchers.pinned_pair`; a test checks the version against the one patchright installs.
- The Profile contract tests (`tests/test_profile_contract.py`) show that, under one Session, the two Fetchers put one client on the wire: a loopback server records what each sends for the same pages, and any difference fails, named. They compare the TLS ClientHello, a resumed one included, the HTTP/2 preface, the HPACK representation of each header field, and the header fields in order with their values, for the default `locale` and others. What clients randomize, such as GREASE values and the order of TLS extensions, and what comes from the server, such as session tickets, is left out; `docs/design.md` §5.4 lists each with its reason.
- A Run warns, once each, about a pair other than the pinned one: an `http.impersonate` other than the pinned Profile, `browser.launch_options` naming another browser with `executable_path` or `channel`, a launched browser of another version than the record, and a browser presenting other languages than `locale`. `pernix check` marks the Profile and the browser as pinned or not. None of them is an error, so that another browser can be chosen on purpose.

**Upgrading either side**

- An upgrade of patchright or curl-cffi, or a change of the default Profile, lands only with the contract tests passing on macOS and Linux, as CI runs them. A browser build that changes what it sends moves the default Profile to the curl-cffi target that matches it, in the same change, and an upgrade of patchright updates the recorded version and brands.
- A difference the contract finds and pernix cannot remove, being inside curl or the browser, is kept as a test expected to fail, with its reason, so that the upgrade that removes it shows.

**Known differences**

- Once a server announces `SETTINGS_HEADER_TABLE_SIZE`, curl's HPACK encoder starts its next header block with a dynamic table size update, even when the size is unchanged; Chrome sends one only when the size changes, and grows its table to a larger size announced, where curl keeps 4096. The difference shows from the second request on a connection, to servers that announce the setting; the contract server does not, as many do not, and a test expected to fail records it.
- curl's HPACK encoder never adds `content-length` to the dynamic table, which Chrome's does, so a request with a body is represented otherwise from that field on; the contract compares its headers and leaves its HPACK out, and a test expected to fail records it.
- Chrome orders the headers a page sets among its own by a hash of their names; a Request's own new headers go first among those instead, and a test expected to fail records it.
- Where a page puts an image, a script, or a style sheet changes the priority Chrome gives it, and Chrome chains concurrent HTTP/2 streams, pools connections across hosts sharing an address and a certificate, and sends CORS preflights; the HTTP Fetcher sends each kind's default priority, one request at a time on its stream, and no preflight.
