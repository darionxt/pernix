"""Tools for testing Spiders and components without a network.

`FakeFetcher` answers Requests from canned answers, `make_response()` builds
Responses, and `LoopbackServer` serves pages on this machine. `crawl()` runs a Spider
and reports its Items and stats, and `run_callback()` runs one Callback. While
`blocking_network()` is in effect, Fetchers reach only loopback hosts.

The pytest plugin (`pernix.testing.pytest_plugin`) turns the network guard on in
every test, and the Frontier conformance suite (`pernix.testing.frontier`) tests
Frontier backends; both need pytest, which the rest does not.
"""

from pernix.fetchers.network_guard import NetworkGuard, blocking_network
from pernix.testing.crawling import CrawlReport, crawl, run_callback
from pernix.testing.fake_fetcher import Answer, FakeFetcher
from pernix.testing.loopback_server import Handler, LoopbackServer, ReceivedRequest, Reply
from pernix.testing.responses import make_response

__all__ = [
    "Answer",
    "CrawlReport",
    "FakeFetcher",
    "Handler",
    "LoopbackServer",
    "NetworkGuard",
    "ReceivedRequest",
    "Reply",
    "blocking_network",
    "crawl",
    "make_response",
    "run_callback",
]
