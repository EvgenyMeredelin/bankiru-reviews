"""What the query parameters accept and reject, checked over HTTP.

``test_schemas.py`` covers the same model directly; these go through the app so
that FastAPI's own parsing of repeated parameters and booleans is included.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeReview

from bankiru.api.schemas import available_output_formats

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))
FORMATS = sorted(available_output_formats)


# ── outputFormat ─────────────────────────────────────────────────────────────
def test_every_handler_is_reachable_by_name():
    """The Literal is built by introspection — pin the discovered set."""
    assert FORMATS == ["csv", "json", "parquet", "xlsx"]


@pytest.mark.parametrize("fmt", FORMATS)
async def test_each_format_is_accepted(api, fmt):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"outputFormat": fmt})

    assert response.status_code == 200
    assert response.json()["filename"].endswith(f".{fmt}")


@pytest.mark.parametrize("fmt", ["pdf", "CSV", ""], ids=["unknown", "wrong-case", "empty"])
async def test_other_formats_are_rejected(api, fmt):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"outputFormat": fmt})

    assert response.status_code == 422


# ── summarize ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("false", False),
        ("True", True),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
    ],
)
async def test_accepted_booleans(api, summarizer, value, expected):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2026-06-01", "endDate": "2026-06-30", "summarize": value},
        )

    assert response.status_code == 200
    assert response.json()["summarize"] is expected


@pytest.mark.parametrize("value", ["maybe", "2", "-1"])
async def test_rejected_booleans(api, value):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"summarize": value})

    assert response.status_code == 422


# ── List-valued filters ──────────────────────────────────────────────────────
@pytest.mark.parametrize("field", ["bankName", "product", "location"])
async def test_a_single_value_becomes_a_one_element_list(api, field):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={field: "Тестбанк"})

    assert response.status_code == 200
    assert response.json()[field] == ["Тестбанк"]


@pytest.mark.parametrize("field", ["bankName", "product", "location"])
async def test_a_repeated_parameter_collects_every_value(api, field):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params=[(field, "Один"), (field, "Два")])

    assert response.status_code == 200
    assert response.json()[field] == ["Один", "Два"]


# ── Dates over HTTP ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    ["2026-03-01", "20260301", "2026-3-1"],
    ids=["dashed", "compact", "unpadded"],
)
async def test_both_date_spellings_reach_the_same_bound(api, value):
    """Unpadded input works by accident and is harmless: dashes are stripped
    first, and ``strptime`` reads ``%Y%m%d`` fields of variable width, so
    ``202631`` still parses as 1 March 2026.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"startDate": value})

    assert response.status_code == 200
    assert response.json()["startDate"] == "2026-03-01"


async def test_an_empty_date_is_resolved_like_an_omitted_one(api):
    """The UI clears a date field to "" — it must not become an error."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"startDate": "", "endDate": ""})

    body = response.json()
    assert response.status_code == 200
    assert body["startDate"] == "2025-01-01"
    assert body["endDate"] == "2026-08-07"


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-13-45", "01-03-2026", "20260231", "2026-03-01T12:00"],
    ids=["words", "impossible", "reversed", "no-such-day", "with-time"],
)
async def test_malformed_dates_are_rejected(api, value):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"startDate": value})

    assert response.status_code == 422


# ── Unknown parameters ───────────────────────────────────────────────────────
async def test_an_unknown_parameter_names_itself(api):
    """The UI flattens ``loc`` into the toast, so the name has to be there."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"limit": 1})

    detail = response.json()["detail"]
    assert response.status_code == 422
    assert detail[0]["type"] == "extra_forbidden"
    assert detail[0]["loc"][-1] == "limit"


async def test_every_unknown_parameter_is_reported(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"limit": 1, "offset": 2})

    reported = {item["loc"][-1] for item in response.json()["detail"]}
    assert reported == {"limit", "offset"}


async def test_validation_precedes_any_database_work(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"limit": 1})

    assert session.bounds_queries == 0
    assert session.main_queries == 0


# ── cloudModel ───────────────────────────────────────────────────────────────
async def test_cloud_model_without_summarize_is_inert(api, summarizer):
    """Echoed back, but nothing is summarized — ``summarize`` alone decides."""
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"cloudModel": "some/model"})

    body = response.json()
    assert body["cloudModel"] == "some/model"
    assert body["comment"] is None
    assert summarizer.calls == []
