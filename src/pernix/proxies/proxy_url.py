"""Proxy URLs, as the Proxy Pool holds them: one form for each proxy, credentials included."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from pernix.logs.redaction import MASK

__all__ = ["PROXY_SCHEMES", "proxy_address", "proxy_credentials", "proxy_url"]

PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
"""The proxy schemes both Fetchers can use, but SOCKS with credentials, which the browser cannot."""


def proxy_url(text: str) -> str:
    """Return a proxy in the one form the Proxy Pool holds it in, which tells proxies apart.

    The scheme is `http` unless given, and it and the host are lower-cased; the user
    and the password are kept as written, so that two sticky sessions on one gateway,
    which differ only in them, stay two proxies. A proxy written as
    `host:port:user:password`, as some vendors list them, is read as
    `user:password@host:port`, everything after the third colon being the password.

    Args:
        text: A proxy, such as `http://user:password@proxy:8080`, `proxy:8080`, or
            `proxy:8080:user:password`.

    Returns:
        The proxy URL, such as `http://user:password@proxy:8080`.

    Raises:
        ValueError: `text` is not a proxy URL with a scheme of `PROXY_SCHEMES`, a
            host, and a port, and nothing else. The message does not quote it, as it
            may hold credentials.
    """
    text = _with_userinfo(text.strip())
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        port = parts.port
    except ValueError:
        raise ValueError("not a proxy URL") from None
    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError(f"a proxy's scheme is one of {', '.join(sorted(PROXY_SCHEMES))}")
    if not parts.hostname:
        raise ValueError("a proxy needs a host")
    if port is None:
        raise ValueError("a proxy needs a port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("a proxy URL has nothing after its port")
    userinfo, at, _ = parts.netloc.rpartition("@")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{userinfo}{at}{host}:{port}"


def _with_userinfo(text: str) -> str:
    """A proxy written as `host:port:user:password` as `user:password@host:port`; any other as it is."""
    parts = text.split(":", 3)
    if "@" in text or "://" in text or "[" in text or len(parts) != 4:
        return text
    host, port, user, password = parts
    if not port.isdigit() or not user:
        return text
    return f"{user}:{password}@{host}:{port}"


def proxy_credentials(url: str) -> tuple[str, ...]:
    """Return the password of a proxy, as written and decoded, to mask it wherever it occurs.

    Args:
        url: A proxy URL, as `proxy_url()` returns it.

    Returns:
        The forms of its password; none without one.
    """
    password = urlsplit(url).password
    if not password:
        return ()
    return tuple(dict.fromkeys((password, unquote(password))))


def proxy_address(proxy: str) -> str:
    """Return a proxy with whatever precedes its host masked, with or without a scheme, to show it in a `repr`.

    Args:
        proxy: A proxy, such as `http://user:password@proxy:8080`.

    Returns:
        The proxy without its credentials, such as `http://***@proxy:8080`.
    """
    authority, at, host = proxy.rpartition("@")
    if not at:
        return proxy
    scheme, separator, _ = authority.rpartition("://")
    return f"{scheme}{separator}{MASK}@{host}"
