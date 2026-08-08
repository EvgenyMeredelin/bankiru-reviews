"""Which query parameters the Gradio Submit button actually sends.

The UI drops empty inputs so the API is not handed spurious filters, and it
translates the model dropdown into ``summarize`` plus ``cloudModel``.
"""

from __future__ import annotations

import httpx
import pytest

from bankiru.ui import blocks
from bankiru.ui.blocks import NO_SUMMARY

# get_reviews(start_date, end_date, bank, product, location, keywords,
#             file_format, cloud_model)
EMPTY_FILTERS = (None, None, None, None, None, None)


@pytest.fixture
def sent(monkeypatch):
    """Capture the query parameters of the request the UI makes."""
    captured: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params)
        return httpx.Response(200, json={"comment": "", "url": ""})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(blocks.httpx, "AsyncClient", patched)
    return captured


# ── The model dropdown drives summarize ──────────────────────────────────────
@pytest.mark.parametrize("model", [NO_SUMMARY, "", None], ids=["sentinel", "empty", "none"])
async def test_no_model_means_no_summarization(sent, model):
    await blocks.get_reviews(*EMPTY_FILTERS, "csv", model)

    params = sent[0]
    assert params["summarize"] == "false"
    assert "cloudModel" not in params


async def test_a_chosen_model_requests_a_summary(sent):
    await blocks.get_reviews(*EMPTY_FILTERS, "csv", "vendor/model-x")

    params = sent[0]
    assert params["summarize"] == "true"
    assert params["cloudModel"] == "vendor/model-x"


async def test_summarize_false_is_sent_rather_than_dropped(sent):
    """The filter drops by value, not by truthiness — ``False`` must survive.

    Rewriting it as a truthiness test would silently stop sending
    ``summarize``, and the API default would carry the request instead.
    """
    await blocks.get_reviews(*EMPTY_FILTERS, "csv", NO_SUMMARY)

    assert "summarize" in sent[0]


# ── Empty inputs are dropped ─────────────────────────────────────────────────
async def test_empty_inputs_are_not_sent(sent):
    await blocks.get_reviews(None, "", [], None, [], "", "csv", NO_SUMMARY)

    params = sent[0]
    for name in ("startDate", "endDate", "bankName", "product", "location", "keywords"):
        assert name not in params
    assert params["outputFormat"] == "csv"


# ── Populated inputs are passed through ──────────────────────────────────────
async def test_every_filled_input_reaches_the_api(sent):
    await blocks.get_reviews(
        "20260601",
        "20260630",
        ["Тестбанк", "Другойбанк"],
        ["Дебетовые карты"],
        ["Москва"],
        "очередь в отделении",
        "parquet",
        NO_SUMMARY,
    )

    params = sent[0]
    assert params["startDate"] == "20260601"
    assert params["endDate"] == "20260630"
    assert params.get_list("bankName") == ["Тестбанк", "Другойбанк"]
    assert params.get_list("product") == ["Дебетовые карты"]
    assert params.get_list("location") == ["Москва"]
    assert params["keywords"] == "очередь в отделении"
    assert params["outputFormat"] == "parquet"


@pytest.mark.parametrize("fmt", ["csv", "json", "parquet", "xlsx"])
async def test_each_format_is_passed_through(sent, fmt):
    await blocks.get_reviews(*EMPTY_FILTERS, fmt, NO_SUMMARY)

    assert sent[0]["outputFormat"] == fmt


async def test_a_cleared_format_asks_for_an_inline_answer(sent):
    """Clear empties the Format dropdown, and it is single-select with no
    empty choice, so ``None`` only arrives that way. The request then omits
    ``outputFormat`` and the API answers inline — worth knowing, because an
    unfiltered inline answer can be very large.
    """
    await blocks.get_reviews(*EMPTY_FILTERS, None, NO_SUMMARY)

    assert "outputFormat" not in sent[0]


async def test_no_unexpected_parameters_are_sent(sent):
    """An extra parameter would come back as a 422 from ``extra="forbid"``."""
    await blocks.get_reviews(*EMPTY_FILTERS, "csv", "vendor/model-x")

    assert set(sent[0].keys()) == {"outputFormat", "summarize", "cloudModel"}
