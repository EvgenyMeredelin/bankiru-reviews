"""The user-visible strings are quoted in the docs — keep the two in step.

README.md and docs/bankiru-reviews-public-api.md print the ``detail`` bodies
verbatim, and external clients match on them. Reword a constant without
touching the documentation and these tests fail instead of the documentation
going quietly stale.

README.md also tabulates every outcome against the test that pins it. Rename a
test without touching the table and the table becomes fiction, silently — hence
the cross-check at the bottom of this module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bankiru.api.routes import (
    INVERTED_RANGE_DETAIL,
    NO_RESULTS_COMMENT,
    SEMANTIC_UNAVAILABLE_DETAIL,
    SUMMARIZE_SPAN_DETAIL,
)

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
README = (ROOT / "README.md").read_text(encoding="utf-8")
PUBLIC_API = (ROOT / "docs" / "bankiru-reviews-public-api.md").read_text(
    encoding="utf-8"
)


@pytest.mark.parametrize(
    "detail",
    [INVERTED_RANGE_DETAIL, SUMMARIZE_SPAN_DETAIL, SEMANTIC_UNAVAILABLE_DETAIL],
)
@pytest.mark.parametrize("document", [README, PUBLIC_API], ids=["readme", "public-api"])
def test_error_detail_is_quoted_verbatim(document, detail):
    assert detail in document


def test_no_results_comment_is_quoted_verbatim():
    assert NO_RESULTS_COMMENT in PUBLIC_API


@pytest.mark.parametrize("document", [README, PUBLIC_API], ids=["readme", "public-api"])
def test_no_stale_today_rule_in_the_reviews_docs(document):
    """The current date dropped out of the rule; the old wording must go too."""
    assert "endDate` = today" not in document
    assert "endDate` = сегодня" not in document
    assert "endDate = сегодня" not in document


# ── The outcome tables in README name real tests ─────────────────────────────
def collected_test_names() -> set[str]:
    """Every test function in ``tests/``, read as text.

    Deliberately not by importing the modules: a broken import anywhere would
    then surface here as a documentation failure.
    """
    names: set[str] = set()
    for path in TESTS.glob("test_*.py"):
        names.update(
            re.findall(r"^(?:async )?def (test_\w+)", path.read_text(encoding="utf-8"), re.M)
        )
    return names


def documented_test_names() -> set[str]:
    """Test names quoted in backticks in README's outcome tables."""
    return set(re.findall(r"`(test_\w+)`", README))


def test_the_tables_name_real_tests():
    documented = documented_test_names()
    assert documented, "the outcome tables vanished from README"
    assert documented <= collected_test_names()


def test_every_test_file_appears_in_the_tables():
    """A whole new file left out of the tables would go unnoticed otherwise."""
    documented_files = set(re.findall(r"`tests/(test_\w+\.py)`", README))
    present = {path.name for path in TESTS.glob("test_*.py")}
    assert present - documented_files == set()


def test_the_tables_name_no_file_that_is_gone():
    """The other direction: a renamed file must not linger in the headings."""
    documented_files = set(re.findall(r"`tests/(test_\w+\.py)`", README))
    present = {path.name for path in TESTS.glob("test_*.py")}
    assert documented_files - present == set()


def test_the_repository_tree_lists_exactly_the_test_files():
    """The tree in the layout section is drawn by hand and quietly goes stale."""
    tree = README.split("├── tests/", 1)[1].split("└── src/bankiru/", 1)[0]
    drawn = set(re.findall(r"(test_\w+\.py)", tree))
    assert drawn == {path.name for path in TESTS.glob("test_*.py")}
