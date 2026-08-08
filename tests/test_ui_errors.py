"""Gradio surfaces API failures as error toasts carrying the API's own text.

Importing ``bankiru.ui.blocks`` builds the whole Blocks tree; it stays offline
because ``conftest`` leaves ``OPENAI_API_KEY`` empty, which makes
``list_foundation_models()`` fall back to its hardcoded list.
"""

from __future__ import annotations

import gradio as gr
import httpx
import pytest

from bankiru.api.routes import (
    INVERTED_RANGE_DETAIL,
    SEMANTIC_UNAVAILABLE_DETAIL,
    SUMMARIZE_SPAN_DETAIL,
)
from bankiru.ui import blocks
from bankiru.ui.blocks import NO_SUMMARY, _api_error_detail, _clear_submit_outputs

SUBMIT_ARGS = (None, None, None, None, None, None, "csv", NO_SUMMARY)


def make_response(status_code: int, json=None, text: str | None = None):
    return httpx.Response(
        status_code,
        json=json,
        text=text,
        request=httpx.Request("GET", "http://api:1706/reviews"),
    )


# ── _api_error_detail ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "detail",
    [INVERTED_RANGE_DETAIL, SUMMARIZE_SPAN_DETAIL, SEMANTIC_UNAVAILABLE_DETAIL],
)
def test_string_detail_is_returned_verbatim(detail):
    """The real constants, not a paraphrase — the wording is the contract."""
    assert _api_error_detail(make_response(400, {"detail": detail})) == detail


def test_validation_errors_are_flattened():
    response = make_response(
        422,
        {
            "detail": [
                {"loc": ["query", "limit"], "msg": "Extra inputs are not permitted"},
            ]
        },
    )
    assert _api_error_detail(response) == "limit: Extra inputs are not permitted"


def test_non_json_body_falls_back_to_text():
    assert _api_error_detail(make_response(502, text="Bad Gateway")) == "Bad Gateway"


def test_empty_body_falls_back_to_the_status_code():
    assert _api_error_detail(make_response(500, text="")) == "API error (500)"


# ── get_reviews ──────────────────────────────────────────────────────────────
@pytest.fixture
def fake_api(monkeypatch):
    """Route the UI's httpx client to a handler supplied by the test."""

    def install(handler):
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(blocks.httpx, "AsyncClient", patched)

    return install


@pytest.fixture
def toasts(monkeypatch):
    """Capture gr.Info calls instead of relying on real toasts."""
    recorded: list[str] = []
    monkeypatch.setattr(
        blocks.gr, "Info", lambda message, **kwargs: recorded.append(message)
    )
    return recorded


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (400, INVERTED_RANGE_DETAIL),
        (400, SUMMARIZE_SPAN_DETAIL),
        (503, SEMANTIC_UNAVAILABLE_DETAIL),
        (403, "Forbidden"),
        (401, "Not authenticated"),
    ],
    ids=["inverted", "span", "semantic", "forbidden", "unauthenticated"],
)
async def test_an_api_error_becomes_an_error_toast(fake_api, toasts, status_code, detail):
    """Any non-2xx carries the API's own wording, character for character.

    ``raise_for_status`` covers 4xx and 5xx alike, which is why the 503 needed
    no UI change: the toast text comes straight from ``detail``.
    """
    fake_api(lambda request: httpx.Response(status_code, json={"detail": detail}))

    with pytest.raises(gr.Error) as excinfo:
        await blocks.get_reviews(*SUBMIT_ARGS)

    # ``message`` is what Gradio renders; str() would add repr quotes.
    assert excinfo.value.message == detail
    assert toasts == []


async def test_a_failed_request_shows_no_download_prompt(fake_api, toasts):
    """The complaint that started this: an info toast implies success."""
    fake_api(
        lambda request: httpx.Response(
            503, json={"detail": SEMANTIC_UNAVAILABLE_DETAIL}
        )
    )

    with pytest.raises(gr.Error):
        await blocks.get_reviews(*SUBMIT_ARGS)

    assert "Download your file" not in toasts


async def test_network_failure_becomes_an_error_toast(fake_api, toasts):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    fake_api(boom)

    with pytest.raises(gr.Error) as excinfo:
        await blocks.get_reviews(*SUBMIT_ARGS)

    assert "Error talking to the API" in str(excinfo.value)
    assert toasts == []


async def test_download_toast_only_fires_with_a_url(fake_api, toasts):
    fake_api(
        lambda request: httpx.Response(
            200, json={"url": "https://obs.test.invalid/x.csv", "comment": ""}
        )
    )

    url, summary = await blocks.get_reviews(*SUBMIT_ARGS)

    assert url == "https://obs.test.invalid/x.csv"
    assert summary == ""
    assert toasts == ["Download your file"]


async def test_no_toast_without_a_url(fake_api, toasts):
    """Inline queries return no URL — the download toast would be a lie."""
    fake_api(lambda request: httpx.Response(200, json={"comment": "Summary"}))

    url, summary = await blocks.get_reviews(*SUBMIT_ARGS)

    assert url == ""
    assert summary == "Summary"
    assert toasts == []


# ── Post-failure cleanup ─────────────────────────────────────────────────────
def test_failure_handler_clears_url_and_summary():
    """Wired to submit.click(...).failure — a 400 must not leave a stale export."""
    assert _clear_submit_outputs() == ("", "")
