from __future__ import annotations

import ast
import asyncio
import code
import inspect
import threading
import types
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

__all__ = ["AsyncConsole", "LoopThread"]


class LoopThread:
    """An event loop running in a background thread, for code called from the main thread."""

    def __init__(self) -> None:
        """Start the loop."""
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="pernix-loop", daemon=True)
        self._thread.start()

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the loop and wait for its result.

        Args:
            coroutine: The coroutine.

        Returns:
            Its result.

        Raises:
            KeyboardInterrupt: Interrupted while waiting, and the coroutine is
                cancelled; or the coroutine raised it.
            SystemExit: The coroutine raised it, such as by calling `exit()`.
        """
        future = asyncio.run_coroutine_threadsafe(_carry_exits(coroutine), self.loop)
        try:
            return future.result()
        except _Exit as carried:
            raise carried.exception from None
        except KeyboardInterrupt:
            future.cancel()
            raise

    def close(self) -> None:
        """Stop the loop and wait for its thread to end."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()


class _Exit(Exception):
    """Carries `SystemExit` or `KeyboardInterrupt` out of the loop, which they would otherwise stop."""

    def __init__(self, exception: BaseException) -> None:
        super().__init__(exception)
        self.exception = exception


async def _carry_exits[T](coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        return await coroutine
    except (SystemExit, KeyboardInterrupt) as exc:
        raise _Exit(exc) from None


class AsyncConsole(code.InteractiveConsole):
    """An interactive console whose code may `await` at the top level.

    Awaiting code runs on a `LoopThread`, so the objects it works with, such as the
    Fetchers of `fetch()`, live on that loop. Ctrl-C cancels what is being awaited.
    """

    def __init__(self, namespace: dict[str, Any], run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
        """Create the console.

        Args:
            namespace: The variables the console starts with.
            run: Runs a coroutine to completion, such as `LoopThread.run`.
        """
        super().__init__(namespace)
        self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        self._run = run

    def runcode(self, code: types.CodeType) -> None:
        """Run compiled input, awaiting it on the loop if it awaits.

        Args:
            code: The compiled input.
        """
        try:
            result = types.FunctionType(code, self.locals)()
            if inspect.iscoroutine(result):
                self._run(result)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException:
            self.showtraceback()
