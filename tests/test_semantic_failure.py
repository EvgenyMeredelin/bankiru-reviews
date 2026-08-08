"""A keywords query the embedder cannot embed is a 503, not an empty 200.

An empty 200 was indistinguishable from "nothing matches your query": a client
would record "no complaints on this topic" for a search that never ran.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import (
    ADMIN_TOKEN,
    GUEST_TOKEN,
    PROVIDER_ERROR,
    PROVIDER_HOST,
    FakeReview,
    gateway,
)

from bankiru.api.routes import SEMANTIC_UNAVAILABLE_DETAIL

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


async def test_embedder_failure_is_a_503(api, broken_embedder):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"keywords": "очередь"})

    assert response.status_code == 503
    assert response.json()["detail"] == SEMANTIC_UNAVAILABLE_DETAIL


async def test_the_provider_message_stays_out_of_the_response(api, broken_embedder):
    """The provider names an internal endpoint — it belongs in the log only."""
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"keywords": "очередь"})

    assert PROVIDER_ERROR not in response.text
    assert PROVIDER_HOST not in response.text


async def test_the_provider_message_is_logged(api, broken_embedder, monkeypatch):
    """The other half of the rule: diagnosable from the log, not from the body."""
    from bankiru.api import routes

    logged: list[str] = []
    # Tolerant signature: another warning on this path, positional or not,
    # must not turn into a TypeError that reads like a handler bug.
    monkeypatch.setattr(
        routes.logfire, "warning", lambda *args, **kw: logged.append(f"{args}{kw}")
    )

    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    assert any(PROVIDER_HOST in entry for entry in logged)


async def test_an_empty_provider_answer_is_the_same_503(api, empty_embedder):
    """A successful call with no vector leaves the search just as impossible.

    Indexing the answer outside the ``try`` turned this into a 500.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"keywords": "очередь"})

    assert response.status_code == 503
    assert response.json()["detail"] == SEMANTIC_UNAVAILABLE_DETAIL


async def test_no_reviews_are_read_when_the_search_cannot_run(api, broken_embedder):
    """Bounds are resolved first, but the failure precedes the main query.

    The status assertion matters: the old fail-soft 200 also returned before
    the main query, so the counters alone would pass against the regression.
    """
    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"keywords": "очередь"})

    assert response.status_code == 503
    assert session.bounds_queries == 1
    assert session.main_queries == 0


@pytest.mark.parametrize("token", [GUEST_TOKEN, ADMIN_TOKEN], ids=["guest", "admin"])
async def test_the_same_503_arrives_through_the_gateway(api, broken_embedder, token):
    """Not to be mistaken for an authorization failure by a public client."""
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get(
            "/reviews",
            params={"keywords": "очередь"},
            headers=gateway(token),
        )

    assert response.status_code == 503
    assert response.json()["detail"] == SEMANTIC_UNAVAILABLE_DETAIL


async def test_a_working_embedder_still_searches(api, embedder):
    """The guard must not have disabled the happy path."""
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        response = await client.get("/reviews", params={"keywords": "очередь"})

    assert response.status_code == 200
    assert len(response.json()["reviews"]) == 1
    assert embedder.calls == [(["очередь"], "query")]


async def test_the_query_is_stripped_before_embedding(api, embedder):
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "  очередь  "})

    assert embedder.calls == [(["очередь"], "query")]
