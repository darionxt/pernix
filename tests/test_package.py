from importlib.metadata import version

import pernix


def test_package_exposes_installed_version() -> None:
    assert pernix.__version__ == version("pernix")


def test_package_exports_public_api() -> None:
    assert set(pernix.__all__) == {"Headers", "Request", "Response", "Spider", "__version__"}
