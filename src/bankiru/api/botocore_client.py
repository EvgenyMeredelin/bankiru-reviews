"""Async S3-compatible (botocore) client factory.

This module provides an async context manager that yields a configured
aiobotocore S3 client. It is used as a FastAPI dependency (via deps.py)
to give route handlers access to S3 operations (upload, pre-signed URLs).

The client is configured for Cloud.ru OBS (Object Storage Service), which
is S3-compatible but requires specific settings:
  - Virtual-hosted-style addressing (``addressing_style: virtual``)
  - Checksum calculation disabled (Cloud.ru doesn't support newer AWS
    checksum algorithms like CRC32C)

Connection to other modules:
  - bankiru.api.deps     — wraps get_async_client() as a FastAPI Depends()
  - bankiru.api.handlers — uses the client to upload exported files and
                           generate pre-signed download URLs
  - bankiru.api.routes   — uses the client for daily Parquet backup uploads
  - bankiru.config       — provides OBS credentials and endpoint settings
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack

from aiobotocore.client import AioBaseClient
from aiobotocore.session import AioSession
from botocore.client import Config

from bankiru.config import get_settings


def _ensure_aws_checksum_env() -> None:
    """Set AWS checksum env vars if not already present.

    aiobotocore reads these from the process environment (not from constructor
    arguments). They disable the newer checksum algorithms (CRC32C, SHA256)
    that Cloud.ru OBS doesn't support. Without these, aiobotocore would
    attempt to compute and send checksums that OBS rejects, causing upload
    failures.

    Using setdefault() ensures we don't override values explicitly set by
    the operator (e.g. in .env or docker-compose environment).
    """
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")


def _client_kwargs() -> dict:
    """Build the keyword arguments for creating an aiobotocore S3 client.

    Returns a dict with:
      - service_name: "s3" (the AWS service type)
      - aws_access_key_id / aws_secret_access_key: OBS credentials from config
      - region_name: OBS region (e.g. "ru-moscow-1")
      - endpoint_url: OBS endpoint (e.g. "https://obs.ru-moscow-1.hc.sbercloud.ru")
      - config: botocore Config with:
          - virtual addressing style (required by Cloud.ru OBS)
          - 10s connect timeout, 30s read timeout
          - max 2 retry attempts on transient failures
    """
    s = get_settings()
    return {
        "service_name": "s3",
        "aws_access_key_id": s.OBS_ACCESS_KEY,
        "aws_secret_access_key": s.OBS_SECRET_KEY,
        "region_name": s.OBS_REGION,
        "endpoint_url": s.OBS_ENDPOINT,
        "config": Config(
            # Cloud.ru OBS requires virtual-hosted-style bucket addressing
            # (bucket.endpoint.com/key) rather than path-style (endpoint.com/bucket/key).
            s3={"addressing_style": "virtual"},
            connect_timeout=10,
            read_timeout=30,
            # Retry up to 2 times on transient S3 errors (e.g. 503 SlowDown).
            retries={"max_attempts": 2},
        ),
    }


async def get_async_client() -> AsyncGenerator[AioBaseClient, None]:
    """Async generator that yields a configured S3 client, then cleans up.

    This is designed to be used as a FastAPI dependency via Depends().
    The AsyncExitStack ensures the client session is properly closed when
    the request handler finishes, even if an exception occurs.

    Usage in FastAPI (via deps.py):
        client: BotoClient  # Annotated[AioBaseClient, Depends(get_async_client)]

    The client supports all standard S3 operations:
      - put_object(): upload files (used by handlers and daily backup)
      - generate_presigned_url(): create time-limited download URLs
    """
    # Ensure checksum env vars are set before creating the client.
    _ensure_aws_checksum_env()
    # Create a fresh aiobotocore session (not shared across requests).
    session = AioSession()
    async with AsyncExitStack() as exit_stack:
        # enter_async_context manages the client lifecycle — it will be
        # closed automatically when the exit_stack unwinds.
        client: AioBaseClient = await exit_stack.enter_async_context(
            session.create_client(**_client_kwargs())
        )
        yield client
