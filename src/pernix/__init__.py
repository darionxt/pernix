"""An asyncio crawling framework with HTTP and browser fetching as peers."""

from importlib.metadata import version as _version

from pernix.extension import Extension
from pernix.headers import Headers
from pernix.request import Request
from pernix.response import Response
from pernix.spider import Spider

__version__ = _version("pernix")

__all__ = ["Extension", "Headers", "Request", "Response", "Spider", "__version__"]
