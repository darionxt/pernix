from importlib.metadata import version

import pernix


def test_package_exposes_installed_version() -> None:
    assert pernix.__version__ == version("pernix")


def test_package_exports_public_api() -> None:
    assert set(pernix.__all__) == {
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
    }


def test_events_are_importable_from_the_package() -> None:
    from pernix import Extension, events

    assert issubclass(events.SpiderClosed, events.Event)
    assert Extension.__module__ == "pernix.extensions.extension"
