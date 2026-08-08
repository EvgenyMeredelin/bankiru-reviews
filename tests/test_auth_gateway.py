"""Who may call the endpoints, and what a rejected caller gets.

The only thing that tells a public request from an internal one is the
``X-Bankiru-Gateway`` header, which Nginx overwrites on the way in (see
``config/bankiru-reviews.conf``) so a client cannot forge its absence.

This layer is orthogonal to everything downstream: it either lets the request
through untouched or answers 403. That is why the rest of the suite runs
without the gateway headers and only a representative few scenarios are
repeated through it.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import ADMIN_TOKEN, GUEST_TOKEN, GUEST_TOKEN_SECOND, gateway

from bankiru.api.deps import GATEWAY_HEADER

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))


# ── Internal callers: the Gradio UI and anything on the compose network ──────
async def test_an_internal_caller_needs_no_token(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews")

    assert response.status_code == 200


async def test_an_internal_caller_may_send_any_token(api):
    """Without the gateway header the token is not even looked at."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews", headers={"API-Token": "complete-nonsense"}
        )

    assert response.status_code == 200


@pytest.mark.parametrize("value", ["0", "true", "2", ""], ids=["0", "true", "2", "empty"])
async def test_only_the_exact_header_value_engages_the_check(api, value):
    """``!= "1"`` is the test in the code — pin it, do not assume truthiness."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", headers={GATEWAY_HEADER: value})

    assert response.status_code == 200


# ── Through the public gateway ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "token",
    [GUEST_TOKEN, GUEST_TOKEN_SECOND, ADMIN_TOKEN],
    ids=["guest", "second-guest", "admin"],
)
async def test_accepted_tokens(api, token):
    """Both entries of the comma-separated guest list work, as does the admin."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", headers=gateway(token))

    assert response.status_code == 200


async def test_a_missing_token_is_refused(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", headers=gateway(token=None))

    assert response.status_code == 403


@pytest.mark.parametrize(
    "token",
    ["", "z" * len(GUEST_TOKEN), "x"],
    ids=["empty", "same-length", "shorter"],
)
async def test_refused_tokens(api, token):
    """The same-length case is what actually reaches ``hmac.compare_digest``.

    Its length is computed rather than written out: a hardcoded string of the
    wrong length would silently be refused by the cheap length check instead,
    leaving the constant-time comparison untested.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/reviews", headers=gateway(token))

    assert response.status_code == 403


async def test_a_refused_caller_reads_no_data(api):
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        await client.get("/reviews", headers=gateway(token=None))

    assert session.bounds_queries == 0
    assert session.main_queries == 0


async def test_authorization_precedes_query_validation(api):
    """Both rules are broken at once; the recorded outcome is the token check."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get(
            "/reviews", params={"limit": 1}, headers=gateway(token=None)
        )

    assert response.status_code == 403


# ── Write endpoints: guests are never allowed, gateway header or not ──────────
@pytest.mark.parametrize(
    "token", [GUEST_TOKEN, GUEST_TOKEN_SECOND], ids=["guest", "second-guest"]
)
async def test_a_guest_cannot_post(api, token):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.post(
            "/reviews", json=[], headers={"API-Token": token}
        )

    assert response.status_code == 403


async def test_a_guest_cannot_delete(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.request(
            "DELETE", "/reviews", json=[1], headers={"API-Token": GUEST_TOKEN}
        )

    assert response.status_code == 403


async def test_a_guest_cannot_delete_by_date(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.delete(
            "/reviews/by-date",
            params={"startDate": "2026-01-01", "endDate": "2026-01-31"},
            headers={"API-Token": GUEST_TOKEN},
        )

    assert response.status_code == 403


async def test_a_guest_cannot_delete_duplicates(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.delete(
            "/reviews/duplicates", headers={"API-Token": GUEST_TOKEN}
        )

    assert response.status_code == 403


# Every write route, so a new one cannot be added without an auth test.
WRITE_ROUTES = [
    ("POST", "/reviews", {"json": []}),
    ("DELETE", "/reviews", {"json": [1]}),
    (
        "DELETE",
        "/reviews/by-date",
        {"params": {"startDate": "2026-01-01", "endDate": "2026-01-31"}},
    ),
    ("DELETE", "/reviews/duplicates", {}),
]
WRITE_IDS = [f"{method}-{path}" for method, path, _ in WRITE_ROUTES]


@pytest.mark.parametrize(("method", "path", "kwargs"), WRITE_ROUTES, ids=WRITE_IDS)
@pytest.mark.parametrize("headers", [None, {"API-Token": ""}], ids=["absent", "empty"])
async def test_an_absent_write_token_is_401_not_403(api, method, path, kwargs, headers):
    """``APIKeyHeader`` rejects before ``api_token`` runs, with a different code.

    Worth pinning: the parser's retry logic treats 401 and 403 alike (fail
    fast), but a client distinguishing "no credentials" from "wrong
    credentials" depends on which one arrives.
    """
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.request(method, path, headers=headers, **kwargs)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "kwargs"), WRITE_ROUTES, ids=WRITE_IDS)
@pytest.mark.parametrize("gateway_header", [False, True], ids=["direct", "gateway"])
async def test_no_write_route_touches_the_database_unauthorized(
    api, method, path, kwargs, gateway_header
):
    """Guests are refused with or without the gateway header, and nothing runs."""
    headers = gateway(GUEST_TOKEN) if gateway_header else {"API-Token": GUEST_TOKEN}
    client, session, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.request(method, path, headers=headers, **kwargs)

    assert response.status_code == 403
    assert session.statements == []


# ── Endpoints that are open by design ────────────────────────────────────────
async def test_healthz_needs_no_token_through_the_gateway(api):
    """Docker's healthcheck polls it; closing it would fail the container."""
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/healthz", headers=gateway(token=None))

    assert response.status_code == 200


async def test_the_root_redirects_to_the_docs(api):
    client, _, _ = api(bounds=BOUNDS, rows=[])
    async with client:
        response = await client.get("/")

    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/docs"
