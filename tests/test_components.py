from __future__ import annotations

import json.decoder
from collections import abc
from typing import Any

import pytest

from pernix.components import build_component, init_type_hints, load_object, resolve_component
from pernix.errors import ComponentError


class Service:
    pass


class NeedsService:
    def __init__(self, service: Service, label: str = "default") -> None:
        self.service = service
        self.label = label


class NeedsUnknown:
    def __init__(self, count: int) -> None:
        self.count = count


class NeedsUnresolvable:
    def __init__(self, thing: Undefined) -> None:  # type: ignore[name-defined]  # noqa: F821
        self.thing = thing


class BuiltFromCrawler:
    def __init__(self, crawler: object) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: object) -> BuiltFromCrawler:
        return cls(crawler)


def _provide_service(service_type: type) -> object | None:
    return SERVICE if service_type is Service else None


SERVICE = Service()
CRAWLER = object()


@pytest.mark.parametrize("path", ["json.decoder.JSONDecoder", "json.decoder:JSONDecoder"])
def test_load_object_accepts_dotted_and_colon_paths(path: str) -> None:
    assert load_object(path) is json.decoder.JSONDecoder


def test_load_object_follows_attribute_paths_after_a_colon() -> None:
    assert load_object("collections:abc.Mapping") is abc.Mapping


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("nodots", "is not an import path"),
        ("no_such_module_xyz.Thing", "cannot import module 'no_such_module_xyz'"),
        ("json.decoder.Missing", "'json.decoder' has no attribute 'Missing'"),
    ],
)
def test_load_object_reports_bad_paths(path: str, message: str) -> None:
    with pytest.raises(ComponentError, match=message):
        load_object(path)


def test_resolve_component_only_imports_strings() -> None:
    marker = object()

    assert resolve_component(marker) is marker
    assert resolve_component(f"{__name__}.Service") is Service


def test_objects_are_used_as_is() -> None:
    component = Service()

    assert build_component(component, crawler=CRAWLER, provide=_provide_service) is component


def test_from_crawler_takes_precedence() -> None:
    component = build_component(BuiltFromCrawler, crawler=CRAWLER, provide=_provide_service)

    assert isinstance(component, BuiltFromCrawler)
    assert component.crawler is CRAWLER


def test_annotated_init_parameters_are_injected_and_defaults_kept() -> None:
    component: Any = build_component(f"{__name__}.NeedsService", crawler=CRAWLER, provide=_provide_service)

    assert isinstance(component, NeedsService)
    assert (component.service, component.label) == (SERVICE, "default")


def test_required_parameters_without_a_service_fail() -> None:
    with pytest.raises(ComponentError, match="no service can be injected for the required parameter 'count'"):
        build_component(NeedsUnknown, crawler=CRAWLER, provide=_provide_service)


def test_unresolvable_annotations_fail_clearly() -> None:
    with pytest.raises(ComponentError, match=r"its annotation .Undefined. cannot be resolved at runtime"):
        build_component(NeedsUnresolvable, crawler=CRAWLER, provide=_provide_service)


class TypeOnly:
    def __init__(self, service: Ghost, extra: Phantom = None) -> None:  # type: ignore[name-defined]  # noqa: F821
        self.service = service
        self.extra = extra


def test_known_types_resolve_names_the_module_only_imports_for_type_checking() -> None:
    component: Any = build_component(
        TypeOnly, crawler=CRAWLER, provide=_provide_service, known_types={"Ghost": Service}
    )

    assert (component.service, component.extra) == (SERVICE, None)


def test_init_type_hints_skips_unresolvable_annotations() -> None:
    assert init_type_hints(TypeOnly) == {}
    assert init_type_hints(TypeOnly, known_types={"Ghost": Service}) == {"service": Service}
    assert init_type_hints(NeedsService) == {"service": Service, "label": str}


class Other:
    pass


def test_names_defined_by_the_module_win_over_known_types() -> None:
    assert init_type_hints(NeedsService, known_types={"Service": Other}) == {"service": Service, "label": str}
