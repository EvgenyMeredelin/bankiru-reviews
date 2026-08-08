"""How each filter reaches SQL, and which of the two query paths runs.

The fake session compiles every statement with literal binds, so the assertions
below read the SQL the database would have received.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeReview

from bankiru.config import get_settings

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


# ── Scalar filters ───────────────────────────────────────────────────────────
def where_clause(sql: str) -> str:
    """The WHERE fragment only — the column list mentions every column."""
    return sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]


def prefix_match(value: str) -> str:
    """How ``startswith`` compiles: ``LIKE 'value' || '%%'`` (escaped percent)."""
    return f"LIKE '{value}' || '%%'"


async def test_without_filters_only_the_dates_are_constrained(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews")

    where = where_clause(session.main_sql)
    assert where.count("datePublished") == 2
    for column in ("bankName", "product", "location"):
        assert column not in where


@pytest.mark.parametrize("field", ["bankName", "product"])
async def test_exact_filters_compile_to_in(api, field):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params=[(field, "Один"), (field, "Два")])

    where = where_clause(session.main_sql)
    assert field in where
    assert "IN ('Один', 'Два')" in where


async def test_location_matches_by_prefix(api):
    """"Москва" has to match "Москва, район Хамовники"."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params={"location": "Москва"})

    assert prefix_match("Москва") in session.main_sql


async def test_several_locations_are_combined_with_or(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", params=[("location", "Москва"), ("location", "Тверь")])

    where = where_clause(session.main_sql)
    assert " OR " in where
    assert prefix_match("Москва") in where
    assert prefix_match("Тверь") in where


async def test_all_filters_apply_together(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get(
            "/reviews",
            params={
                "startDate": "2026-06-01",
                "endDate": "2026-06-30",
                "bankName": "Тестбанк",
                "product": "Дебетовые карты",
                "location": "Москва",
            },
        )

    sql = session.main_sql
    assert "'2026-06-01 00:00:00'" in sql
    assert "'2026-06-30 23:59:59.999999'" in sql
    assert "IN ('Тестбанк')" in sql
    assert "IN ('Дебетовые карты')" in sql
    assert prefix_match("Москва") in sql
    # Narrowing, not widening: a single OR here would change the meaning.
    where = where_clause(sql)
    assert " AND " in where
    assert " OR " not in where


async def test_the_standard_path_is_ordered_and_unlimited(api):
    """No LIMIT without keywords — a broad filter can produce a huge export."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews")

    sql = session.main_sql
    assert "ORDER BY" in sql
    assert "LIMIT" not in sql.upper()


# ── Which path runs ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("keywords", ["", "   ", "\t\n"], ids=["empty", "spaces", "tabs"])
async def test_blank_keywords_take_the_standard_path(api, embedder, keywords):
    """``keywords.strip()`` decides, so whitespace is not a semantic query."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"keywords": keywords})

    assert response.status_code == 200
    assert embedder.calls == []
    assert session.session_settings == []
    assert "review_embeddings" not in session.main_sql


async def test_the_semantic_path_joins_embeddings_and_ranks_by_distance(api, embedder):
    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    sql = session.main_sql
    assert "review_embeddings" in sql
    assert "JOIN" in sql.upper()
    assert "LIMIT" in sql.upper()
    # The ordering has to be by distance; the standard path is also "ORDER BY".
    assert "<=>" in sql.split("ORDER BY", 1)[1]


async def test_the_semantic_path_raises_hnsw_recall(api, embedder):
    """``SET LOCAL`` cannot take bind parameters — the value is interpolated."""
    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    ef_search = get_settings().SEMANTIC_SEARCH_EF_SEARCH
    assert session.session_settings == [f"SET LOCAL hnsw.ef_search = {ef_search}"]


async def test_the_semantic_path_keeps_the_scalar_filters(api, embedder):
    """Ranking by similarity must not widen the filters the caller asked for."""
    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get(
            "/reviews",
            params={
                "keywords": "очередь",
                "startDate": "2026-06-01",
                "endDate": "2026-06-30",
                "bankName": "Тестбанк",
                "location": "Москва",
            },
        )

    sql = session.main_sql
    assert "'2026-06-01 00:00:00'" in sql
    assert "'2026-06-30 23:59:59.999999'" in sql
    assert "IN ('Тестбанк')" in sql
    assert prefix_match("Москва") in sql


async def test_the_distance_ceiling_is_applied_when_configured(api, embedder, monkeypatch):
    """Cosine distance appears twice: once ranking, once as the ceiling."""
    monkeypatch.setenv("SEMANTIC_SEARCH_MAX_DISTANCE", "0.42")
    get_settings.cache_clear()

    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    sql = session.main_sql
    assert sql.count("<=>") == 2
    assert "0.42" in sql


async def test_no_distance_ceiling_when_disabled(api, embedder, monkeypatch):
    """Setting it empty disables the ceiling rather than defaulting it back."""
    monkeypatch.setenv("SEMANTIC_SEARCH_MAX_DISTANCE", "")
    get_settings.cache_clear()
    assert get_settings().SEMANTIC_SEARCH_MAX_DISTANCE is None

    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    # Ranking still uses the distance; only the WHERE comparison is gone.
    assert session.main_sql.count("<=>") == 1


@pytest.mark.parametrize("configured", ["0", "-5"], ids=["zero", "negative"])
async def test_a_nonpositive_ef_search_is_clamped(api, embedder, monkeypatch, configured):
    """The value is interpolated into SQL, so it must never be absurd."""
    monkeypatch.setenv("SEMANTIC_SEARCH_EF_SEARCH", configured)
    get_settings.cache_clear()

    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    assert session.session_settings == ["SET LOCAL hnsw.ef_search = 1"]


# ── Empty filter values ──────────────────────────────────────────────────────
@pytest.mark.parametrize("field", ["bankName", "product"])
async def test_an_empty_exact_filter_matches_nothing(api, field):
    """An empty exact filter is a real filter for the empty string, not a no-op."""
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={field: ""})

    assert response.status_code == 200
    assert "IN ('')" in where_clause(session.main_sql)


async def test_an_empty_location_matches_everything(api):
    """Prefix matching makes the empty string the opposite: it matches all.

    Worth knowing rather than fixing — the UI never sends an empty list entry,
    and both behaviours follow from how each filter is built.
    """
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", params={"location": ""})

    assert response.status_code == 200
    assert prefix_match("") in session.main_sql


# ── Combined with summarization ──────────────────────────────────────────────
async def test_a_summary_reads_the_ranked_result_set(api, embedder, summarizer):
    """With keywords the LLM sees the capped, ranked rows — not the interval."""
    rows = [FakeReview(id=1, reviewBody="Первый"), FakeReview(id=2, reviewBody="Второй")]
    client, _, _ = api(bounds=BOUNDS, rows=rows)
    async with client:
        response = await client.get(
            "/reviews",
            params={
                "keywords": "очередь",
                "startDate": "2026-06-01",
                "endDate": "2026-06-30",
                "summarize": True,
            },
        )

    assert response.status_code == 200
    assert summarizer.calls[0][0] == ["Первый", "Второй"]


async def test_the_semantic_limit_comes_from_settings(api, embedder):
    client, session, _ = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"keywords": "очередь"})

    limit = get_settings().SEMANTIC_SEARCH_LIMIT
    assert f"LIMIT {limit}" in session.main_sql
