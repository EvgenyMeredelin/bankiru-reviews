"""Small text cleaning utilities for parsed review bodies.

This module provides a composable text cleaning pipeline used by the crawler
(crawler.py) to normalise raw review text extracted from banki.ru's HTML.

The pipeline transforms raw HTML review bodies into clean plain text by:
  1. Stripping HTML tags (twice — see note below)
  2. Removing emoji characters
  3. Trimming leading/trailing whitespace

Connection to other modules:
  - bankiru.parser.crawler — imports clean_text_pipe to clean each review's
                             reviewBody before storing it in the record dict
"""

from __future__ import annotations

from typing import Any, Callable

from bs4 import BeautifulSoup
from emoji import replace_emoji


class Pipeline:
    """Composable function pipeline: applies functions left-to-right.

    ``Pipeline(f, g, h)(v)`` is equivalent to ``h(g(f(v)))``.

    This is a simple functional composition utility that makes the text
    cleaning steps explicit and easy to extend. Each step is a plain
    function that takes a string and returns a string.
    """

    def __init__(self, *funcs: Callable[[Any], Any]) -> None:
        """Store the sequence of functions to apply."""
        self.funcs = funcs

    def __call__(self, value: Any) -> Any:
        """Apply each function in order, passing the result to the next."""
        for func in self.funcs:
            value = func(value)
        return value


def remove_tags(html: str) -> str:
    """Strip all HTML tags from the input string using BeautifulSoup.

    Uses the built-in "html.parser" (no external C dependency) to parse
    the HTML and extract only the text content.
    """
    return BeautifulSoup(html, "html.parser").text


# ── The main text cleaning pipeline ─────────────────────────────────────────
# Applied to every review's reviewBody before it is stored in the database.
#
# The double `remove_tags` pass is intentional: banki.ru sometimes returns
# review bodies whose HTML tags are themselves HTML-encoded (e.g.
# "&lt;br&gt;" instead of "<br>"). The first pass decodes and strips the
# outer tags; the second pass catches any residual markup that was
# previously encoded. This pattern was preserved from the original parser
# because it reliably handles all observed encoding variants.
#
# Steps:
#   1. remove_tags  — first pass: strip HTML tags
#   2. remove_tags  — second pass: strip any residual encoded tags
#   3. replace_emoji — remove emoji characters (they cause issues in some
#                      export formats and LLM tokenizers)
#   4. str.strip    — trim leading/trailing whitespace
clean_text_pipe = Pipeline(
    remove_tags,
    remove_tags,
    replace_emoji,
    str.strip,
)
