"""Async S3-compatible (botocore) client factory."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack

from aiobotocore.client import AioBaseClient
from aiobotocore.session import AioSession
from botocore.client import Config

from bankiru.config import get_settings


def _ensure_aws_checksum_env() -> None:
    """The aiobotocore client honors these via process env, not constructor."""
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")


def _client_kwargs() -> dict:
    s = get_settings()
    return {
        "service_name": "s3",
        "aws_access_key_id": s.OBS_ACCESS_KEY,
        "aws_secret_access_key": s.OBS_SECRET_KEY,
        "region_name": s.OBS_REGION,
        "endpoint_url": s.OBS_ENDPOINT,
        "config": Config(
            s3={"addressing_style": "virtual"},
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2},
        ),
    }


async def get_async_client() -> AsyncGenerator[AioBaseClient, None]:
    _ensure_aws_checksum_env()
    session = AioSession()
    async with AsyncExitStack() as exit_stack:
        client: AioBaseClient = await exit_stack.enter_async_context(
            session.create_client(**_client_kwargs())
        )
        yield client
