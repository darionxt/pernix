from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pernix.events import RequestScheduled, Veto, on
from pernix.extensions.extension import Extension

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.crawler import Crawler

__all__ = ["OffsiteFilter"]


class OffsiteFilter(Extension):
    """Drops Requests to hosts outside the Spider's `allowed_domains`, at admission.

    A host is allowed if it is one of the domains or a subdomain of one; ports do not
    matter. Dropped Requests are emitted as `RequestDropped` with the reason
    `"offsite"`, whatever their `dont_filter`. A Spider without `allowed_domains` is
    not filtered. Enabled by default in the `extensions` setting.
    """

    def __init__(self, crawler: Crawler) -> None:
        """Read the allowed domains of the Run's Spider.

        Args:
            crawler: The Crawler of the Run.

        Raises:
            ValueError: An allowed domain is not a bare domain, such as one with a
                scheme, a port, or a path.
        """
        self._domains = _normalized(crawler.spider.allowed_domains)

    @on(RequestScheduled)
    def admit(self, event: RequestScheduled) -> Veto | None:
        """Veto a Request to a host outside the allowed domains.

        Args:
            event: The Request about to enter the Frontier.

        Returns:
            A Veto with the reason `"offsite"`, or `None` to admit the Request.
        """
        if not self._domains:
            return None
        host = (urlsplit(event.request.url).hostname or "").rstrip(".")
        if any(host == domain or host.endswith(f".{domain}") for domain in self._domains):
            return None
        return Veto("offsite")


def _normalized(domains: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for domain in domains:
        name = domain.strip().lower().rstrip(".")
        if not name or any(character in name for character in ":/?#@ "):
            raise ValueError(
                f"allowed_domains holds {domain!r}; write bare domains such as 'example.com', "
                "without a scheme, port, or path"
            )
        normalized.append(name)
    return tuple(normalized)
