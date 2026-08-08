"""The three-calendar-month limit on summarization."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeReview

from bankiru.api.routes import SUMMARIZE_SPAN_DETAIL

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


@pytest.fixture(autouse=True)
def no_llm(summarizer):
    """No test may reach a real summarizer; hand back the stub to inspect."""
    return summarizer


async def test_exactly_three_months_is_allowed(api, no_llm):
    """A row is needed: with none, the handler returns before the summarizer
    and a 200 would not prove the interval was accepted for summarization.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "startDate": "2026-01-01",
                "endDate": "2026-04-01",
                "summarize": True,
            },
        )

    assert response.status_code == 200
    assert no_llm.SUMMARY in response.json()["comment"]


async def test_one_day_over_three_months_is_rejected(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "startDate": "2026-01-01",
                "endDate": "2026-04-02",
                "summarize": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == SUMMARIZE_SPAN_DETAIL


async def test_same_range_without_summarize_is_fine(api):
    """The limit guards summarization only — a wider range is fine without it."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2026-01-01", "endDate": "2026-04-02"},
        )

    assert response.status_code == 200


async def test_omitted_dates_hit_the_limit(api):
    """The incident case: no dates at all plus summarize must be refused."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"summarize": True})

    assert response.status_code == 400
    assert response.json()["detail"] == SUMMARIZE_SPAN_DETAIL


async def test_rejection_precedes_the_main_query(api, no_llm):
    """Documented as "before SQL select / LLM" — check both halves."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"summarize": True})

    assert session.main_queries == 0
    assert no_llm.calls == []


async def test_omitted_start_over_a_wide_table_is_rejected(api):
    """An omitted startDate opens the interval back to the earliest review."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"endDate": "2026-08-07", "summarize": True},
        )

    assert response.status_code == 400


async def test_omitted_end_within_three_months_is_allowed(api):
    """Data ending two months after startDate leaves the interval short."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2026-06-07", "summarize": True},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("start", "end", "allowed"),
    [
        ("2026-11-30", "2027-02-28", True),
        ("2026-11-30", "2027-03-01", False),
        ("2026-01-31", "2026-04-30", True),
        ("2026-01-31", "2026-05-01", False),
    ],
    ids=["clamped-ok", "clamped-over", "short-month-ok", "short-month-over"],
)
async def test_three_months_are_calendar_months_not_ninety_days(api, start, end, allowed):
    """``relativedelta`` clamps to the shorter month, moving the boundary.

    30 November plus three months is 28 February, not 30 February, so the
    widest allowed interval is a day shorter than the naive reading suggests.
    """
    client, _, _ = api(bounds=(datetime(2020, 1, 1), datetime(2030, 1, 1)), rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": start, "endDate": end, "summarize": True},
        )

    assert response.status_code == (200 if allowed else 400)


async def test_omitted_end_over_stale_data_is_allowed(api):
    """The upper bound is max(datePublished), so a stale table stays summarizable.

    The newest review is two months after startDate but years in the past.
    Resolving the omitted endDate against the current date instead — the old
    behaviour — would stretch the interval past three months and reject this.
    """
    client, _, _ = api(bounds=(datetime(2020, 1, 1), datetime(2020, 3, 1)), rows=[])
    async with client:
        response = await client.get(
            "/reviews",
            params={"startDate": "2020-01-01", "summarize": True},
        )

    assert response.status_code == 200
