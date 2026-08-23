"""Parsing of ``GUEST_API_TOKEN`` as ``owner@example.org:token`` pairs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bankiru.config import Settings, get_settings


def _reload(monkeypatch: pytest.MonkeyPatch, value: str) -> Settings:
    monkeypatch.setenv("GUEST_API_TOKEN", value)
    get_settings.cache_clear()
    return Settings()


def test_empty_string_yields_no_tokens(monkeypatch):
    settings = _reload(monkeypatch, "")
    assert settings.GUEST_API_TOKEN == []
    assert settings.guest_api_tokens == frozenset()


def test_two_pairs_keep_only_the_tokens(monkeypatch):
    settings = _reload(
        monkeypatch,
        "alice@example.org:tok-aaa,bob@example.org:tok-bbb",
    )
    assert settings.GUEST_API_TOKEN == ["tok-aaa", "tok-bbb"]
    assert settings.guest_api_tokens == frozenset({"tok-aaa", "tok-bbb"})


def test_whitespace_around_commas_and_colons_is_stripped(monkeypatch):
    settings = _reload(
        monkeypatch,
        "  alice@example.org : tok-aaa , bob@example.org:tok-bbb  ",
    )
    assert settings.GUEST_API_TOKEN == ["tok-aaa", "tok-bbb"]
    assert settings.guest_api_tokens == frozenset({"tok-aaa", "tok-bbb"})


def test_a_trailing_comma_is_ignored(monkeypatch):
    settings = _reload(monkeypatch, "alice@example.org:tok-aaa,")
    assert settings.GUEST_API_TOKEN == ["tok-aaa"]
    assert settings.guest_api_tokens == frozenset({"tok-aaa"})


def test_empty_comma_segments_are_skipped(monkeypatch):
    settings = _reload(
        monkeypatch,
        ",alice@example.org:tok-aaa,,bob@example.org:tok-bbb,",
    )
    assert settings.GUEST_API_TOKEN == ["tok-aaa", "tok-bbb"]
    assert settings.guest_api_tokens == frozenset({"tok-aaa", "tok-bbb"})


def test_a_token_may_contain_colons(monkeypatch):
    settings = _reload(monkeypatch, "alice@example.org:tok:with:colons")
    assert settings.GUEST_API_TOKEN == ["tok:with:colons"]
    assert settings.guest_api_tokens == frozenset({"tok:with:colons"})


def test_a_list_is_kept_as_tokens():
    settings = Settings(GUEST_API_TOKEN=["tok-aaa", " tok-bbb "])
    assert settings.GUEST_API_TOKEN == ["tok-aaa", "tok-bbb"]
    assert settings.guest_api_tokens == frozenset({"tok-aaa", "tok-bbb"})


@pytest.mark.parametrize(
    "value",
    [
        "just-a-token",
        "alice@example.org",
        "alice@example.org:",
        ":tok-aaa",
        "alice@example.org: ",
        "alice@example.org:tok-aaa,just-a-token",
    ],
    ids=[
        "bare",
        "no-colon",
        "empty-token",
        "empty-owner",
        "whitespace-token",
        "mixed-malformed",
    ],
)
def test_malformed_entries_are_rejected(monkeypatch, value):
    with pytest.raises(ValidationError):
        _reload(monkeypatch, value)
