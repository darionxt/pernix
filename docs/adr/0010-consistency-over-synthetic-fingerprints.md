# Basic anti-detection relies on consistency, not synthesized browser characteristics

**HTTP**

- Uses a pinned Chrome Profile whose major version matches the bundled browser engine.
- Does not use aliases such as `chrome` that drift when dependencies are upgraded, so the Profile only changes on an explicit upgrade.

**Browser**

- Uses patchright's real characteristics.
- By default, does not override the UA or inject generated fingerprints.

**Why no randomization**

Randomly generated headers or page properties only change the surface; they cannot change the TLS and HTTP/2 characteristics at the same time. The result is a UA, Client Hints, and TLS characteristics that contradict each other, which is easier to detect, not harder.
