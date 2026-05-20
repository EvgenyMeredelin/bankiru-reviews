"""Small text cleaning utilities for parsed review bodies."""

from __future__ import annotations

from typing import Any, Callable

from bs4 import BeautifulSoup
from emoji import replace_emoji


class Pipeline:
    """`Pipeline(f, g, h)(v) == h(g(f(v)))`."""

    def __init__(self, *funcs: Callable[[Any], Any]) -> None:
        self.funcs = funcs

    def __call__(self, value: Any) -> Any:
        for func in self.funcs:
            value = func(value)
        return value


def remove_tags(html: str) -> str:
    return BeautifulSoup(html, "html.parser").text


# The double `remove_tags` pass is intentional: banki.ru sometimes returns
# review bodies whose tags are themselves HTML-encoded, so one pass alone
# leaves residual markup. Preserved from the original parser.
clean_text_pipe = Pipeline(
    remove_tags,
    remove_tags,
    replace_emoji,
    str.strip,
)
