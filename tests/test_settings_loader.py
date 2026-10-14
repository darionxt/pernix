from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

import msgspec
import pytest

from pernix.errors import SettingsError
from pernix.extensions import OffsiteFilter
from pernix.fetchers import HTTPFetcher
from pernix.items import FailedItemsFile
from pernix.settings import (
    ComponentMap,
    CoreSettings,
    NamedComponents,
    ResponseSettings,
    Secret,
    Section,
    load_settings,
)

if TYPE_CHECKING:
    from pernix.settings import Settings


class DomainSettings(Section):
    __prefix__ = "domain"
    delay: Annotated[float, msgspec.Meta(ge=0)] = 0.0
    verify: bool = True
    codes: list[int] = msgspec.field(default_factory=list[int])
    token: Secret | None = None


class PluginSettings(Section):
    __prefix__ = "test"
    plugins: ComponentMap = msgspec.field(default_factory=dict)


class RetrySettings(Section):
    __prefix__ = "retry"
    times: int = 3


class CacheSettings(Section):
    __prefix__ = "cache"
    size: int = 100
    backends: ComponentMap = msgspec.field(default_factory=dict)


class DiskSettings(Section):
    __prefix__ = "disk"
    path: str = "cache"


class Alpha:
    pass


class Beta:
    pass


class Gamma:
    pass


class Retry:
    def __init__(self, settings: RetrySettings) -> None:
        self.settings = settings


class Cache:
    settings_sections: ClassVar[tuple[type[Section], ...]] = (CacheSettings,)

    @classmethod
    def from_crawler(cls, crawler: object) -> Cache:
        return cls()


class Disk:
    def __init__(self, settings: DiskSettings) -> None:
        self.settings = settings


class Marker:
    pass


DEFAULT_MARKER = Marker()


class StorageSettings(Section):
    __prefix__ = "storage"
    cache_dir: Path = Path("cache")
    marker: Marker = DEFAULT_MARKER


class BuiltinSettings(Section):
    __prefix__ = "builtin"
    plugins: ComponentMap = msgspec.field(default_factory=lambda: {f"{__name__}.Retry": 100, Alpha: 200})


SECTIONS = (CoreSettings, DomainSettings, PluginSettings)


def _load(tmp_path: Path, environ: dict[str, str] | None = None, **kwargs: Any) -> Settings:
    return load_settings(sections=SECTIONS, project_dir=tmp_path, environ=environ or {}, **kwargs)


def _problems(excinfo: pytest.ExceptionInfo[SettingsError]) -> str:
    return "\n".join(excinfo.value.problems)


# Precedence


@pytest.mark.parametrize(
    ("layers", "expected", "origin"),
    [
        (0, 16, "built-in default"),
        (1, 2, "Addon"),
        (2, 3, "pernix.toml"),
        (3, 4, "environment variable PERNIX_CONCURRENCY"),
        (4, 5, "Spider.settings"),
        (5, 6, "run() arguments"),
        (6, 7, "-s concurrency"),
    ],
)
def test_each_layer_overrides_the_ones_below(tmp_path: Path, layers: int, expected: int, origin: str) -> None:
    if layers >= 2:
        (tmp_path / "pernix.toml").write_text("concurrency = 3\n")
    settings = _load(
        tmp_path,
        environ={"PERNIX_CONCURRENCY": "4"} if layers >= 3 else {},
        addon_layers=[{"concurrency": 2}] if layers >= 1 else [],
        spider_settings={"concurrency": 5} if layers >= 4 else None,
        overrides={"concurrency": 6} if layers >= 5 else None,
        command_line={"concurrency": "7"} if layers >= 6 else None,
    )

    assert settings["concurrency"] == expected
    assert settings.section(CoreSettings).concurrency == expected
    assert settings.origin("concurrency").startswith(origin)


def test_layers_merge_key_by_key(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("concurrency = 3\n[domain]\ndelay = 1.5\n")

    settings = _load(tmp_path, environ={"PERNIX_DOMAIN__VERIFY": "false"}, spider_settings={"domain.codes": [500]})

    assert settings.section(DomainSettings) == DomainSettings(delay=1.5, verify=False, codes=[500])
    assert settings["concurrency"] == 3


def test_nested_and_dotted_keys_are_equivalent(tmp_path: Path) -> None:
    dotted = _load(tmp_path, spider_settings={"domain.delay": 0.5})
    nested = _load(tmp_path, spider_settings={"domain": {"delay": 0.5}})

    assert dotted["domain.delay"] == nested["domain.delay"] == 0.5


# Validation


def test_misspelled_keys_fail_with_a_suggestion_from_any_layer(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("[domian]\ndelay = 1\n")

    with pytest.raises(SettingsError) as excinfo:
        _load(tmp_path, environ={"PERNIX_CONCURENCY": "2"}, spider_settings={"domain.dealy": 1})

    problems = _problems(excinfo)
    assert "unknown setting 'domian.delay' from pernix.toml" in problems
    assert "did you mean 'domain.delay'?" in problems
    assert "unknown setting 'concurency' from environment variable PERNIX_CONCURENCY; did you mean 'concurrency'?" in (
        problems
    )
    assert "unknown setting 'domain.dealy' from Spider.settings; did you mean 'domain.delay'?" in problems


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _load(tmp_path, overrides={"concurrency": 0, "domain.delay": "slow", "nope": 1})

    assert len(excinfo.value.problems) == 3
    assert "concurrency: Expected `int` >= 1 (from run() arguments)" in excinfo.value.problems
    assert str(excinfo.value).startswith("Invalid settings:\n  - ")


def test_python_layers_are_type_checked_strictly(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match=r"concurrency: Expected `int`, got `str`"):
        _load(tmp_path, spider_settings={"concurrency": "8"})


def test_environment_values_are_parsed_by_type(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        environ={
            "PERNIX_CONCURRENCY": "8",
            "PERNIX_DOMAIN__DELAY": "0.25",
            "PERNIX_DOMAIN__VERIFY": "false",
            "PERNIX_DOMAIN__CODES": "[429, 503]",
            "PERNIX_DOMAIN__TOKEN": "s3cr3t",
        },
    )

    assert settings.section(DomainSettings) == DomainSettings(
        delay=0.25, verify=False, codes=[429, 503], token=Secret("s3cr3t")
    )
    assert settings["concurrency"] == 8


def test_invalid_environment_values_name_the_variable(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _load(tmp_path, environ={"PERNIX_DOMAIN__DELAY": "soon"})

    assert "domain.delay: Expected `float`, got `str` (from environment variable PERNIX_DOMAIN__DELAY)" in (
        excinfo.value.problems
    )


def test_command_line_values_are_parsed_by_type_and_name_their_option(tmp_path: Path) -> None:
    settings = _load(tmp_path, command_line={"domain.codes": "[429]", "domain.verify": "false"})

    assert (settings.section(DomainSettings).codes, settings.section(DomainSettings).verify) == ([429], False)
    with pytest.raises(SettingsError) as excinfo:
        _load(tmp_path, command_line={"domain.delay": "soon", "concurency": "2"})
    assert sorted(excinfo.value.problems) == [
        "domain.delay: Expected `float`, got `str` (from -s domain.delay)",
        "unknown setting 'concurency' from -s concurency; did you mean 'concurrency'?",
    ]


def test_secrets_load_from_strings_and_stay_redacted(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text('[domain]\ntoken = "s3cr3t"\n')

    settings = _load(tmp_path)

    assert settings["domain.token"].reveal() == "s3cr3t"
    assert "s3cr3t" not in repr(settings)
    assert "Secret('**********')" in repr(settings)


def test_custom_type_defaults_are_kept(tmp_path: Path) -> None:
    settings = load_settings(sections=(CoreSettings, StorageSettings), project_dir=tmp_path, environ={})

    assert settings.section(StorageSettings) == StorageSettings()
    assert settings["storage.cache_dir"] == Path("cache")
    assert settings["storage.marker"] is DEFAULT_MARKER


@pytest.mark.parametrize("layer", ["project", "environment", "spider"])
def test_paths_load_from_strings_and_instances(tmp_path: Path, layer: str) -> None:
    if layer == "project":
        (tmp_path / "pernix.toml").write_text('[storage]\ncache_dir = "/data/cache"\n')
    environ = {"PERNIX_STORAGE__CACHE_DIR": "/data/cache"} if layer == "environment" else {}
    spider_settings = {"storage.cache_dir": Path("/data/cache")} if layer == "spider" else None

    settings = load_settings(
        sections=(CoreSettings, StorageSettings), project_dir=tmp_path, environ=environ, spider_settings=spider_settings
    )

    assert settings["storage.cache_dir"] == Path("/data/cache")


def test_custom_types_are_not_built_from_arbitrary_input(tmp_path: Path) -> None:
    other = Marker()

    with pytest.raises(SettingsError) as excinfo:
        load_settings(
            sections=(CoreSettings, StorageSettings),
            project_dir=tmp_path,
            environ={},
            spider_settings={"storage.cache_dir": 5, "storage.marker": "x"},
        )
    kept = load_settings(
        sections=(CoreSettings, StorageSettings),
        project_dir=tmp_path,
        environ={},
        spider_settings={"storage.marker": other},
    )

    assert "storage.cache_dir: Expected `Path`, got `int` (from Spider.settings)" in excinfo.value.problems
    assert "storage.marker: Expected `Marker`, got `str` (from Spider.settings)" in excinfo.value.problems
    assert kept["storage.marker"] is other


# Component maps


def test_component_maps_merge_entries_across_layers(tmp_path: Path) -> None:
    module = __name__
    (tmp_path / "pernix.toml").write_text(f'[test.plugins]\n"{module}.Beta" = false\n')

    settings = _load(
        tmp_path,
        addon_layers=[{"test.plugins": {f"{module}.Alpha": 100, f"{module}:Beta": 200}}],
        spider_settings={"test.plugins": {Gamma: 50}},
        overrides={"test.plugins": {Alpha: 300}},
    )

    assert settings.components("test.plugins") == [Gamma, Alpha]
    assert settings["test.plugins"] == {Alpha: 300, Beta: None, Gamma: 50}


def test_component_maps_can_be_set_from_the_environment(tmp_path: Path) -> None:
    settings = _load(tmp_path, environ={"PERNIX_TEST__PLUGINS": f'{{"{__name__}.Alpha": 1, "{__name__}.Beta": null}}'})

    assert settings.components("test.plugins") == [Alpha]


def test_equal_orders_keep_registration_order(tmp_path: Path) -> None:
    settings = _load(tmp_path, spider_settings={"test.plugins": {Beta: 10, Alpha: 10, Gamma: 5}})

    assert settings.components("test.plugins") == [Gamma, Beta, Alpha]


def test_invalid_component_entries_are_reported(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _load(
            tmp_path,
            spider_settings={
                "test.plugins": {Alpha: "first", Beta: True, "no_such_module_xyz.Thing": 1, f"{__name__}.Missing": 2}
            },
        )

    problems = _problems(excinfo)
    assert "the order of <class 'test_settings_loader.Alpha'> must be an integer, null, or false" in problems
    assert "the order of <class 'test_settings_loader.Beta'> must be an integer, null, or false" in problems
    assert "cannot import module 'no_such_module_xyz'" in problems
    assert "has no attribute 'Missing'" in problems


def test_component_map_defaults_are_kept_and_resolved(tmp_path: Path) -> None:
    settings = load_settings(sections=(CoreSettings, BuiltinSettings), project_dir=tmp_path, environ={})

    assert settings.components("builtin.plugins") == [Retry, Alpha]
    assert settings.origin("builtin.plugins") == "built-in default"


def test_layers_add_to_and_disable_component_map_defaults(tmp_path: Path) -> None:
    settings = load_settings(
        sections=(CoreSettings, BuiltinSettings),
        project_dir=tmp_path,
        environ={},
        spider_settings={"builtin.plugins": {Gamma: 150, f"{__name__}.Alpha": None}},
        overrides={"retry.times": 7},
    )

    assert settings.components("builtin.plugins") == [Retry, Gamma]
    assert settings.section(RetrySettings).times == 7


def test_invalid_component_map_defaults_are_reported(tmp_path: Path) -> None:
    class BrokenDefaults(Section):
        __prefix__ = "broken"
        plugins: ComponentMap = msgspec.field(default_factory=lambda: {Alpha: "first"})  # pyright: ignore[reportAssignmentType]

    with pytest.raises(SettingsError, match=r"the order of .*Alpha.* \(from the default of .*BrokenDefaults\)"):
        load_settings(sections=(CoreSettings, BrokenDefaults), project_dir=tmp_path, environ={})


# Named component registries


class TransportSettings(Section):
    __prefix__ = "transport"
    fetchers: NamedComponents = msgspec.field(default_factory=lambda: {"http": f"{__name__}.Alpha"})


class BucketSettings(Section):
    __prefix__ = "bucket"
    name: str = "data"


class Bucket:
    def __init__(self, settings: BucketSettings) -> None:
        self.settings = settings


def _load_transport(tmp_path: Path, **kwargs: Any) -> Settings:
    return load_settings(sections=(CoreSettings, TransportSettings), project_dir=tmp_path, **kwargs)


def test_named_components_merge_name_by_name_across_layers(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text(f'[transport.fetchers]\nftp = "{__name__}.Beta"\ns3 = "{__name__}.Gamma"\n')

    settings = _load_transport(
        tmp_path,
        environ={},
        spider_settings={"transport.fetchers": {"http": Gamma}},
        overrides={"transport.fetchers": {"ftp": False, "s3": None}},
    )

    assert settings.named_components("transport.fetchers") == {"http": Gamma}
    assert settings["transport.fetchers"] == {"http": Gamma, "ftp": None, "s3": None}


def test_named_component_defaults_are_resolved(tmp_path: Path) -> None:
    settings = _load_transport(tmp_path, environ={})

    assert settings.named_components("transport.fetchers") == {"http": Alpha}
    assert settings.origin("transport.fetchers") == "built-in default"


def test_named_components_register_their_sections_from_the_environment(tmp_path: Path) -> None:
    settings = _load_transport(
        tmp_path,
        environ={
            "PERNIX_TRANSPORT__FETCHERS": f'{{"s3": "{__name__}.Bucket", "http": null}}',
            "PERNIX_BUCKET__NAME": "logs",
        },
    )

    assert settings.named_components("transport.fetchers") == {"s3": Bucket}
    assert settings.section(BucketSettings).name == "logs"


def test_disabled_named_components_still_register_their_sections(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text(
        f'[transport.fetchers]\ns3 = "{__name__}.Bucket"\n\n[bucket]\nname = "logs"\n'
    )

    settings = _load_transport(tmp_path, environ={}, overrides={"transport.fetchers": {"s3": None}})

    assert settings.named_components("transport.fetchers") == {"http": Alpha}
    assert settings.section(BucketSettings).name == "logs"


def test_invalid_named_components_are_reported(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _load_transport(
            tmp_path,
            environ={"PERNIX_TRANSPORT__FETCHERS": "[1]"},
            spider_settings={"transport.fetchers": {"": Alpha, 3: Beta, "x": "no_such_module_xyz.Thing"}},
        )

    problems = _problems(excinfo)
    assert "transport.fetchers: expected a mapping of name to component, got [1]" in problems
    assert "names must be non-empty strings, got '' (from Spider.settings)" in problems
    assert "names must be non-empty strings, got 3 (from Spider.settings)" in problems
    assert "cannot import module 'no_such_module_xyz'" in problems


def test_named_components_are_not_ordered_component_maps(tmp_path: Path) -> None:
    settings = _load_transport(tmp_path, environ={})

    with pytest.raises(TypeError, match="is not a registry of named components"):
        settings.named_components("concurrency")


# Sections registered by components


def test_components_register_sections_through_init_annotations(tmp_path: Path) -> None:
    settings = _load(tmp_path, spider_settings={"test.plugins": {Retry: 1}, "retry.times": 5})

    assert settings.section(RetrySettings).times == 5


def test_sections_registered_by_components_are_discovered_transitively(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        spider_settings={"test.plugins": {Cache: 1}, "cache": {"size": 5, "backends": {Disk: 1}}},
        overrides={"disk.path": "/data/cache"},
    )

    assert settings.section(CacheSettings).size == 5
    assert settings.components("cache.backends") == [Disk]
    assert settings.section(DiskSettings).path == "/data/cache"


def test_disabled_components_still_register_their_sections(tmp_path: Path) -> None:
    settings = _load(tmp_path, spider_settings={"test.plugins": {Retry: None}, "retry.times": 5})

    assert settings.components("test.plugins") == []
    assert settings["retry.times"] == 5


def test_keys_of_unregistered_sections_are_unknown(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match=r"unknown setting 'retry.times' from Spider.settings"):
        _load(tmp_path, spider_settings={"retry.times": 5})


def test_section_defaults_are_validated(tmp_path: Path) -> None:
    class Misdeclared(Section):
        __prefix__ = "misdeclared"
        workers: Annotated[int, msgspec.Meta(ge=1)] = 0
        name: str = 5  # type: ignore[assignment]

    with pytest.raises(SettingsError) as excinfo:
        load_settings(sections=(CoreSettings, Misdeclared), project_dir=tmp_path, environ={})

    problems = _problems(excinfo)
    assert "Misdeclared: the default of 'workers' is invalid: Expected `int` >= 1" in problems
    assert "Misdeclared: the default of 'name' is invalid: Expected `str`, got `int`" in problems


class WindowSettings(Section):
    __prefix__ = "window"
    low: int = 0
    high: int = 10

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"window.low must not exceed window.high, got {self.low} > {self.high}")


def test_section_checks_are_reported_together_with_their_origins(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("[window]\nlow = 20\n")

    with pytest.raises(SettingsError) as excinfo:
        load_settings(
            sections=(WindowSettings, ResponseSettings),
            project_dir=tmp_path,
            environ={"PERNIX_RESPONSE__DEFAULT_ENCODING": "klingon"},
        )

    window, encoding = excinfo.value.problems
    assert window.startswith("window.low must not exceed window.high, got 20 > 10 (from pernix.toml (")
    assert encoding == (
        "response.default_encoding: unknown encoding 'klingon' (from environment variable "
        "PERNIX_RESPONSE__DEFAULT_ENCODING)"
    )


def test_section_checks_of_defaults_name_no_origin(tmp_path: Path) -> None:
    class Inverted(Section):
        __prefix__ = "inverted"
        low: int = 2
        high: int = 1

        def __post_init__(self) -> None:
            raise ValueError("inverted.low must not exceed inverted.high")

    with pytest.raises(SettingsError) as excinfo:
        load_settings(sections=(Inverted,), project_dir=tmp_path, environ={})

    assert excinfo.value.problems == ["inverted.low must not exceed inverted.high"]


def test_encoding_labels_and_codec_names_are_valid_default_encodings(tmp_path: Path) -> None:
    for label in ("GBK", "cp936", "latin1"):
        settings = load_settings(
            sections=(ResponseSettings,),
            project_dir=tmp_path,
            environ={},
            overrides={"response.default_encoding": label},
        )
        assert settings.section(ResponseSettings).default_encoding == label


def test_sections_need_defaults_and_unique_keys(tmp_path: Path) -> None:
    class Incomplete(Section):
        __prefix__ = "incomplete"
        value: int

    class Clashing(Section):
        __prefix__ = "domain"
        delay: float = 1.0

    with pytest.raises(SettingsError) as excinfo:
        load_settings(sections=(CoreSettings, DomainSettings, Incomplete, Clashing), project_dir=tmp_path, environ={})

    problems = _problems(excinfo)
    assert "Incomplete must give every setting a default" in problems
    assert "'domain.delay' is declared by both DomainSettings and" in problems


# Isolation


def test_settings_objects_are_independent(tmp_path: Path) -> None:
    first = _load(tmp_path, overrides={"concurrency": 1, "test.plugins": {Alpha: 1}})
    second = _load(tmp_path, overrides={"concurrency": 2})

    assert (first["concurrency"], second["concurrency"]) == (1, 2)
    assert first.components("test.plugins") == [Alpha]
    assert second.components("test.plugins") == []


def test_defaults_come_from_the_sections(tmp_path: Path) -> None:
    settings = _load(tmp_path)

    assert settings.section(DomainSettings) == DomainSettings()
    assert dict(settings) == {
        "concurrency": 16,
        "extensions": {OffsiteFilter: 100},
        "addons": (),
        "middlewares": {},
        "spider_middlewares": {},
        "fetchers": {"http": HTTPFetcher},
        "pipelines": {},
        "failed_pipelines": {FailedItemsFile: 100},
        "run_id": None,
        "domain.delay": 0.0,
        "domain.verify": True,
        "domain.codes": [],
        "domain.token": None,
        "test.plugins": {},
        # registered by the default HTTP Fetcher
        "http.impersonate": "chrome150",
        "http.timeout": 30.0,
        "http.deadline": 180.0,
        "http.max_redirects": 20,
        "http.max_body_size": 100 * 1024 * 1024,
        "http.pool_size": 8,
        "http.pool_idle": 300.0,
    }
