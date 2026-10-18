"""URL globs, as the browser's route patterns read them."""

from __future__ import annotations

import functools
import re

__all__ = ["glob_pattern"]


@functools.lru_cache(maxsize=256)
def glob_pattern(glob: str) -> re.Pattern[str]:
    r"""Compile a URL glob into the regular expression a whole URL must match.

    `*` matches anything but `/`, and `**` anything; `**/` also matches nothing
    after a `/`. `{a,b}` matches either. `\` makes the next character literal, and
    every other character, `?` and `[` included, is literal.

    Args:
        glob: The glob, such as `"**/api/list*"`.

    Returns:
        The compiled expression.

    Raises:
        ValueError: The glob has unbalanced braces.

    Example:
        >>> bool(glob_pattern("**/api/*").fullmatch("https://shop.example/api/books?page=2"))
        True
    """
    tokens: list[str] = []
    depth = 0
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == "\\" and index + 1 < len(glob):
            index += 1
            tokens.append(re.escape(glob[index]))
        elif char == "*":
            stars = len(glob) - index - len(glob[index:].lstrip("*"))
            before = glob[index - 1] if index > 0 else ""
            index += stars - 1
            if stars == 1:
                tokens.append("[^/]*")
            elif glob[index + 1 : index + 2] == "/":
                tokens.append("(?:.+/)?" if before == "/" else ".*/")
                index += 1
            else:
                tokens.append(".*")
        elif char == "{":
            depth += 1
            tokens.append("(?:")
        elif char == "}" and depth:
            depth -= 1
            tokens.append(")")
        elif char == "," and depth:
            tokens.append("|")
        else:
            tokens.append(re.escape(char))
        index += 1
    if depth:
        raise ValueError(f"the URL glob {glob!r} opens a brace it does not close")
    return re.compile("".join(tokens), re.DOTALL)
