from pernix import Request
from pernix.memory_frontier import MemoryFrontier

URL = "https://example.com/"


async def _drain(frontier: MemoryFrontier, count: int) -> list[Request]:
    requests = []
    for _ in range(count):
        requests.append(await frontier.get())
        frontier.done()
    return requests


async def test_identical_requests_are_deduplicated_regardless_of_cookies() -> None:
    frontier = MemoryFrontier()

    assert frontier.add(Request(URL, cookies={"session": "a"}))
    assert not frontier.add(Request(URL, cookies={"session": "b"}))
    assert not frontier.add(Request(URL, headers={"Cookie": "session=c"}))


async def test_explicit_fingerprints_decide_deduplication() -> None:
    frontier = MemoryFrontier()

    assert frontier.add(Request(URL, fingerprint="account-a"))
    assert frontier.add(Request(URL, fingerprint="account-b"))
    assert not frontier.add(Request("https://example.com/other", fingerprint="account-a"))
    assert frontier.add(Request(URL, fingerprint=""))
    assert [r.fingerprint for r in await _drain(frontier, 3)] == ["account-a", "account-b", ""]


async def test_dont_filter_always_enqueues() -> None:
    frontier = MemoryFrontier()

    assert frontier.add(Request(URL))
    assert frontier.add(Request(URL, dont_filter=True))
    assert frontier.add(Request(URL, dont_filter=True))
    assert not frontier.add(Request(URL))
