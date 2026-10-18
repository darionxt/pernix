# Basic anti-detection relies on consistency, not synthesized browser characteristics

**HTTP**

- Uses a pinned Chrome Profile whose major version matches the bundled browser engine.
- Does not use aliases such as `chrome` that drift when dependencies are upgraded, so the Profile only changes on an explicit upgrade.

**Browser**

- Uses patchright's real characteristics.
- By default, does not override the UA or inject generated fingerprints.

**Why no randomization**

Randomly generated headers or page properties only change the surface; they cannot change the TLS and HTTP/2 characteristics at the same time. The result is a UA, Client Hints, and TLS characteristics that contradict each other, which is easier to detect, not harder.

**The pinned pair**

- The browser Fetcher launches the Chrome for Testing build bundled with patchright 1.62.3 (Chrome 151), and the HTTP Fetcher impersonates the `chrome150` target of curl-cffi 0.16.3. Chrome 151 changed nothing of its TLS and HTTP/2 from 150.
- Both are pinned to exact versions in `pyproject.toml`.
- The Profile contract tests (`tests/test_profile_contract.py`) show that, under one Session, the two Fetchers put one client on the wire: a loopback server records what each sends for the same pages, and any difference fails, named. They compare the TLS ClientHello, a resumed one included, the HTTP/2 preface, the HPACK representation of each header field, and the header fields in order. What clients randomize, such as GREASE values and the order of TLS extensions, and what comes from the server, such as session tickets, is left out; `docs/design.md` §5.4 lists each with its reason.
- The values of `User-Agent`, `sec-ch-ua`, `sec-ch-ua-platform`, and `Accept-Language` are not compared yet: the HTTP side sends the Profile's, which differ from the headless browser's, until it derives them from the browser.

**Upgrading either side**

- An upgrade of patchright or curl-cffi, or a change of the default Profile, lands only with the contract tests passing on macOS and Linux, as CI runs them. A browser build that changes what it sends moves the default Profile to the curl-cffi target that matches it, in the same change.
- A difference the contract finds and pernix cannot remove, being inside curl or the browser, is kept as a test expected to fail, with its reason, so that the upgrade that removes it shows.

**Known difference**

- Once a server announces `SETTINGS_HEADER_TABLE_SIZE`, curl's HPACK encoder starts its next header block with a dynamic table size update, even when the size is unchanged; Chrome sends one only when the size changes, and grows its table to a larger size announced, where curl keeps 4096. The difference shows from the second request on a connection, to servers that announce the setting; the contract server does not, as many do not, and a test expected to fail records it.
