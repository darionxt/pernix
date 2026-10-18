"""Proxies: the Proxy Pool Requests are sent through, its sources, the health of each proxy, and how Sessions are bound."""

from pernix.proxies.binding_policy import Binding, BindingPolicy, BindingReason, DefaultBindingPolicy
from pernix.proxies.memory_proxy_pool import MemoryProxyPool
from pernix.proxies.proxy import Proxy
from pernix.proxies.proxy_pool import ProxyCandidate, ProxyCounts, ProxyHealth, ProxyPool
from pernix.proxies.proxy_sources import ProxyProvider, read_proxies
from pernix.proxies.proxy_url import proxy_url

__all__ = [
    "Binding",
    "BindingPolicy",
    "BindingReason",
    "DefaultBindingPolicy",
    "MemoryProxyPool",
    "Proxy",
    "ProxyCandidate",
    "ProxyCounts",
    "ProxyHealth",
    "ProxyPool",
    "ProxyProvider",
    "proxy_url",
    "read_proxies",
]
