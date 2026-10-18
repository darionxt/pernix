"""Options of rendered Requests, the declarative actions they run on their page, and what rendering gives.

`Request(render=Render(...))` loads the page in the browser Fetcher, waits, runs
actions, blocks resource types, and captures XHR and fetch responses; the Response
holds the results in `response.browser`. Nothing here needs patchright, which only
the browser Fetcher imports.
"""

from pernix.browser.actions import Action, Click, Evaluate, Screenshot, Scroll, Type, WaitFor, WaitState
from pernix.browser.render import Render
from pernix.browser.render_result import CapturedResponse, RenderResult
from pernix.browser.resource_types import RESOURCE_TYPES, ResourceType

__all__ = [
    "RESOURCE_TYPES",
    "Action",
    "CapturedResponse",
    "Click",
    "Evaluate",
    "Render",
    "RenderResult",
    "ResourceType",
    "Screenshot",
    "Scroll",
    "Type",
    "WaitFor",
    "WaitState",
]
