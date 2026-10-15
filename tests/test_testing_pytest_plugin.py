"""The pytest plugin, in pytest sessions of their own."""

from __future__ import annotations

import pytest

# The sessions below run in this process, so this test's own guard must be off for
# theirs to be seen.
pytestmark = pytest.mark.pernix_network

INI = """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
asyncio_default_test_loop_scope = function
"""


@pytest.fixture
def session(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(INI)
    return pytester


def test_a_test_whose_fetch_the_guard_refused_fails_even_if_the_run_carried_on(session: pytest.Pytester) -> None:
    session.makepyfile(
        """
        from pernix import Spider
        from pernix.testing import crawl

        class Public(Spider):
            start_urls = ["http://192.0.2.1/"]

            async def parse(self, response):
                yield {"url": response.url}

        async def test_crawl():
            report = await crawl(Public)
            assert report.items == []  # the fetch failed inside the Run, which went on
        """
    )

    result = session.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*network guard refused http://192.0.2.1/ while running this test*pernix_network*"])


def test_a_skip_or_a_failure_does_not_hide_a_refused_url(session: pytest.Pytester) -> None:
    session.makepyfile(
        """
        import pytest
        from pernix import Request
        from pernix.errors import NetworkBlocked
        from pernix.fetchers import HTTPFetcher

        async def test_skipped_when_unavailable():
            async with HTTPFetcher() as fetcher:
                try:
                    await fetcher.fetch(Request("http://192.0.2.1/"))
                except NetworkBlocked:
                    pytest.skip("the service is unavailable")

        async def test_failing():
            async with HTTPFetcher() as fetcher:
                await fetcher.fetch(Request("http://192.0.2.1/other"))
        """
    )

    result = session.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(
        [
            "*refused http://192.0.2.1/ while running this test*",
            "*refused http://192.0.2.1/other while running this test*",
        ]
    )


def test_fixtures_of_every_scope_are_guarded_as_they_are_set_up_and_torn_down(session: pytest.Pytester) -> None:
    session.makepyfile(
        """
        import asyncio
        import pytest
        from pernix import Request
        from pernix.errors import NetworkBlocked
        from pernix.fetchers import HTTPFetcher

        def fetch(url):
            async def attempt():
                async with HTTPFetcher() as fetcher:
                    try:
                        await fetcher.fetch(Request(url))
                    except NetworkBlocked:
                        pass
            asyncio.run(attempt())

        @pytest.fixture(scope="session")
        def remote_setup():
            fetch("http://192.0.2.1/setup")

        @pytest.fixture(scope="module")
        def remote_teardown():
            yield
            fetch("http://192.0.2.1/teardown")

        def test_set_up(remote_setup):
            pass

        def test_torn_down(remote_teardown):
            pass
        """
    )

    result = session.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(
        [
            "*refused http://192.0.2.1/setup while setting up this test*",
            "*refused http://192.0.2.1/teardown while tearing down this test*",
        ]
    )


def test_a_marked_test_or_an_allowing_ini_option_turns_the_guard_off(session: pytest.Pytester) -> None:
    session.makepyfile(
        """
        import pytest
        from pernix.fetchers.network_guard import current_guard

        def test_guarded():
            assert current_guard() is not None

        @pytest.mark.pernix_network
        def test_marked():
            assert current_guard() is None
        """
    )
    assert session.runpytest("-p", "no:cacheprovider").ret == pytest.ExitCode.OK

    session.makeini(INI + "pernix_network = allow\n")
    session.makepyfile(
        """
        from pernix.fetchers.network_guard import current_guard

        def test_allowed():
            assert current_guard() is None
        """
    )
    assert session.runpytest("-p", "no:cacheprovider").ret == pytest.ExitCode.OK


def test_the_ini_option_is_checked(session: pytest.Pytester) -> None:
    session.makeini(INI + "pernix_network = sometimes\n")
    session.makepyfile("def test_nothing(): pass")

    result = session.runpytest("-p", "no:cacheprovider")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*pernix_network must be 'block' or 'allow', got 'sometimes'*"])


def test_the_loopback_server_fixture_serves_during_the_test(session: pytest.Pytester) -> None:
    session.makepyfile(
        """
        from pernix import Request
        from pernix.fetchers import HTTPFetcher

        async def test_served(loopback_server):
            loopback_server.route("/", "hello")
            async with HTTPFetcher() as fetcher:
                response = await fetcher.fetch(Request(loopback_server.url("/")))
            assert response.body == b"hello"
        """
    )

    session.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)
