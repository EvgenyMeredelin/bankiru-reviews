"""When S3 refuses, the export must fail loudly rather than half-succeed.

The same reasoning as the 503 for semantic search: a client that receives 2xx
assumes it has data. There is no fail-soft branch here, so what these pin is
that no such branch is ever added by accident.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import BrokenBotoClient, FakeReview

BOUNDS = (datetime(2025, 1, 1), datetime(2026, 8, 7))
SHORT_RANGE = {"startDate": "2026-06-01", "endDate": "2026-06-30"}


@pytest.fixture
def failing(api):
    """An app whose S3 client fails the named call."""

    def factory(call: str, rows=None):
        return api(
            bounds=BOUNDS,
            rows=rows if rows is not None else [FakeReview()],
            boto=BrokenBotoClient(fail=call),
            raise_app_exceptions=False,
        )

    return factory


@pytest.mark.parametrize("call", ["put_object", "generate_presigned_url"])
async def test_a_failed_export_is_never_a_success(failing, call):
    client, _, _ = failing(call)
    async with client:
        response = await client.get("/reviews", params={"outputFormat": "csv"})

    assert response.status_code == 500


@pytest.mark.parametrize("call", ["put_object", "generate_presigned_url"])
async def test_a_failed_export_names_no_file(failing, call):
    """A filename in the body would invite the client to retry a download."""
    client, _, _ = failing(call)
    async with client:
        response = await client.get("/reviews", params={"outputFormat": "csv"})

    assert ".csv" not in response.text


async def test_a_lost_summary_is_not_delivered_alone(failing, summarizer):
    """The summary is real work, but half a response is worse than none."""
    client, _, _ = failing("put_object")
    async with client:
        response = await client.get(
            "/reviews", params={**SHORT_RANGE, "summarize": True, "outputFormat": "csv"}
        )

    assert response.status_code == 500
    assert summarizer.SUMMARY not in response.text


async def test_an_inline_query_never_touches_s3(api):
    """Without outputFormat a broken bucket must not matter at all."""
    boto = BrokenBotoClient(fail="put_object")
    client, _, _ = api(bounds=BOUNDS, rows=[FakeReview()], boto=boto)
    async with client:
        response = await client.get("/reviews")

    assert response.status_code == 200
    assert boto.uploads == []
    assert boto.presigns == []


async def test_the_download_link_keeps_the_default_lifetime(api):
    """Both documents promise about an hour, which is botocore's default.

    Passing ``ExpiresIn`` would change that silently, so pin its absence.
    """
    client, _, boto = api(bounds=BOUNDS, rows=[FakeReview()])
    async with client:
        await client.get("/reviews", params={"outputFormat": "csv"})

    assert "ExpiresIn" not in boto.presigns[0]
