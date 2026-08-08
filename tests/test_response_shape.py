"""What the body looks like: inline or export, summary or not, and the echo.

Every branch echoes the *effective* dates rather than the raw request, so a
client can always tell which interval it actually received.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeReview

from bankiru.api.routes import NO_RESULTS_COMMENT
from bankiru.api.schemas import available_output_formats
from bankiru.config import get_settings

MIN = datetime(2025, 1, 1)
MAX = datetime(2026, 8, 7)
BOUNDS = (MIN, MAX)
FORMATS = sorted(available_output_formats)

# A range short enough for summarization, inside BOUNDS.
SHORT_RANGE = {"startDate": "2026-06-01", "endDate": "2026-06-30"}


def assert_full_span(body):
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2026-08-07"


# ── Inline ───────────────────────────────────────────────────────────────────
async def test_inline_branch(api):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert response.status_code == 200
    assert len(body["reviews"]) == 1
    assert body["reviews"][0]["bankName"] == "Тестбанк"
    assert body["url"] is None
    assert body["filename"] is None
    assert_full_span(body)


async def test_inline_rows_carry_every_documented_field(api):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews")

    row = response.json()["reviews"][0]
    assert set(row) == {
        "id",
        "datePublished",
        "reviewBody",
        "bankName",
        "url",
        "location",
        "product",
    }
    assert row["datePublished"] == "2026-03-01 12:00:00"


# ── Export ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fmt", FORMATS)
async def test_export_branch(api, fmt):
    client, _, boto = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"outputFormat": fmt})

    body = response.json()
    assert response.status_code == 200
    assert body["reviews"] is None
    assert body["filename"].endswith(f".{fmt}")
    assert body["url"] == f"https://obs.test.invalid/{body['filename']}"
    assert len(boto.uploads) == 1
    assert_full_span(body)


async def test_the_export_is_uploaded_with_a_body(api):
    """A pre-signed URL for an empty object would download an empty file.

    Asserting the ``Body`` is truthy would prove nothing — a ``BytesIO`` has
    no ``__len__``, so an empty buffer is truthy too.
    """
    client, _, boto = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"outputFormat": "csv"})

    upload = boto.uploads[0]
    assert upload["Bucket"] == "test-bucket"
    exported = upload["Body"].getvalue().decode("utf-8")
    assert FakeReview().reviewBody in exported
    assert "Тестбанк" in exported


# ── No results ───────────────────────────────────────────────────────────────
async def test_no_results_branch(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews")

    body = response.json()
    assert body["comment"] == NO_RESULTS_COMMENT
    assert body["reviews"] is None
    assert body["url"] is None
    assert_full_span(body)


async def test_no_results_with_an_output_format_has_no_url(api):
    """Nothing was exported, so there must be no link to a missing file."""
    client, _, boto = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"outputFormat": "csv"})

    body = response.json()
    assert body["url"] is None
    assert body["filename"] is None
    assert body["comment"] == NO_RESULTS_COMMENT
    assert boto.uploads == []


async def test_no_results_never_calls_the_summarizer(api, summarizer):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={**SHORT_RANGE, "summarize": True})

    assert response.json()["comment"] == NO_RESULTS_COMMENT
    assert summarizer.calls == []


# ── Summarization ────────────────────────────────────────────────────────────
async def test_the_summary_names_the_default_model(api, summarizer):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={**SHORT_RANGE, "summarize": True})

    model = get_settings().DEFAULT_CLOUD_MODEL
    comment = response.json()["comment"]
    assert comment == f"**Summary model:** `{model}`\n\n{summarizer.SUMMARY}"
    assert summarizer.calls == [([FakeReview().reviewBody], model)]


async def test_an_explicit_model_overrides_the_default(api, summarizer):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={**SHORT_RANGE, "summarize": True, "cloudModel": "vendor/model-x"},
        )

    assert "`vendor/model-x`" in response.json()["comment"]
    assert summarizer.calls[0][1] == "vendor/model-x"


async def test_identical_bodies_are_summarized_once(api, summarizer):
    """Duplicate texts waste tokens; the handler deduplicates them."""
    rows = [FakeReview(id=1), FakeReview(id=2), FakeReview(id=3, reviewBody="Другое")]
    client, _, _ = api(bounds=BOUNDS, rows=rows)
    async with client:
        await client.get("/reviews", params={**SHORT_RANGE, "summarize": True})

    assert summarizer.calls[0][0] == [FakeReview().reviewBody, "Другое"]


async def test_a_summary_and_an_export_arrive_together(api, summarizer):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={**SHORT_RANGE, "summarize": True, "outputFormat": "csv"},
        )

    body = response.json()
    assert body["url"].endswith(".csv")
    assert summarizer.SUMMARY in body["comment"]
    assert body["reviews"] is None


async def test_no_summary_without_the_flag(api, summarizer):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews")

    assert response.json()["comment"] is None
    assert summarizer.calls == []


# ── Echo ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("summarize", [False, True])
async def test_echo_repeats_explicit_dates_unchanged(api, summarizer, summarize):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "startDate": "2026-02-01",
                "endDate": "2026-02-28",
                "summarize": summarize,
            },
        )

    body = response.json()
    assert body["startDate"] == "2026-02-01"
    assert body["endDate"] == "2026-02-28"


async def test_every_filter_is_echoed_back(api, embedder):
    """A client reading only the response can reconstruct what it asked for."""
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                **SHORT_RANGE,
                "bankName": "Тестбанк",
                "product": "Дебетовые карты",
                "location": "Москва",
                "keywords": "очередь",
                "outputFormat": "json",
            },
        )

    body = response.json()
    assert body["bankName"] == ["Тестбанк"]
    assert body["product"] == ["Дебетовые карты"]
    assert body["location"] == ["Москва"]
    assert body["keywords"] == "очередь"
    assert body["outputFormat"] == "json"
    assert body["summarize"] is False


async def test_the_response_holds_no_unexpected_fields(api):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews")

    assert set(response.json()) == {
        "startDate",
        "endDate",
        "bankName",
        "location",
        "product",
        "keywords",
        "outputFormat",
        "summarize",
        "cloudModel",
        "filename",
        "url",
        "comment",
        "reviews",
    }
