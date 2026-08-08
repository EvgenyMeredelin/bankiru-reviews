"""Validation of the GET /reviews query model."""

from __future__ import annotations

from datetime import datetime

import pytest

from bankiru.api.schemas import ReviewsQuery

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


async def test_unknown_query_param_is_rejected(api):
    """extra="forbid" — a typo in a parameter name must not be ignored."""
    client, _, _ = api(bounds=BOUNDS)
    async with client:
        response = await client.get("/reviews", params={"limit": 1})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


async def test_summarize_defaults_to_false(api):
    """No summarize in the request ⇒ false, whatever the caller."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews")

    assert response.status_code == 200
    assert response.json()["summarize"] is False


@pytest.mark.parametrize("value", ["2026-03-01", "20260301"])
def test_both_date_formats_are_accepted(value):
    """The Gradio DateTime component sends YYYYMMDD, humans send YYYY-MM-DD."""
    assert ReviewsQuery(startDate=value).startDate == datetime(2026, 3, 1).date()


def test_datetime_input_is_normalized_to_date():
    query = ReviewsQuery(endDate=datetime(2026, 3, 1, 23, 30))
    assert query.endDate == datetime(2026, 3, 1).date()


def test_empty_string_means_no_bound():
    """Clearing a date field in the UI must read as "omitted", not as an error."""
    query = ReviewsQuery(startDate="", endDate="")
    assert query.startDate is None
    assert query.endDate is None


async def test_malformed_date_is_rejected(api):
    client, _, _ = api(bounds=BOUNDS)
    async with client:
        response = await client.get("/reviews", params={"startDate": "not-a-date"})

    assert response.status_code == 422
