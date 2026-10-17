"""An asyncio crawling framework with HTTP and browser fetching as peers."""

from importlib.metadata import version as _version

from pernix.account import Account
from pernix.cookie_jar import Cookie, CookieJar
from pernix.errors import DropItem, IgnoreRequest, Retry
from pernix.extensions import Extension
from pernix.headers import Headers
from pernix.items import FailedItem, ItemContext, Pipeline
from pernix.middlewares import Middleware, NodeMiddleware, SpiderMiddleware, middleware, spider_middleware
from pernix.request import Request
from pernix.response import Response
from pernix.run_result import RunResult
from pernix.session import Session
from pernix.spider import Spider

__version__ = _version("pernix")

__all__ = [
    "Account",
    "Cookie",
    "CookieJar",
    "DropItem",
    "Extension",
    "FailedItem",
    "Headers",
    "IgnoreRequest",
    "ItemContext",
    "Middleware",
    "NodeMiddleware",
    "Pipeline",
    "Request",
    "Response",
    "Retry",
    "RunResult",
    "Session",
    "Spider",
    "SpiderMiddleware",
    "__version__",
    "middleware",
    "spider_middleware",
]
