"""An asyncio crawling framework with HTTP and browser fetching as peers."""

from importlib.metadata import version as _version

from pernix.errors import DropItem, IgnoreRequest, Retry
from pernix.extension import Extension
from pernix.headers import Headers
from pernix.middleware import Middleware, NodeMiddleware, middleware
from pernix.request import Request
from pernix.response import Response
from pernix.spider import Spider
from pernix.spider_middleware import SpiderMiddleware, spider_middleware

__version__ = _version("pernix")

__all__ = [
    "DropItem",
    "Extension",
    "Headers",
    "IgnoreRequest",
    "Middleware",
    "NodeMiddleware",
    "Request",
    "Response",
    "Retry",
    "Spider",
    "SpiderMiddleware",
    "__version__",
    "middleware",
    "spider_middleware",
]
