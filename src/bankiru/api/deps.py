"""FastAPI dependency helpers."""

from __future__ import annotations

from typing import Annotated

from aiobotocore.client import AioBaseClient
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from bankiru.api.botocore_client import get_async_client
from bankiru.config import get_settings
from bankiru.db import get_async_session

DBSession = Annotated[AsyncSession, Depends(get_async_session)]
BotoClient = Annotated[AioBaseClient, Depends(get_async_client)]


async def api_token(
    token: Annotated[str, Depends(APIKeyHeader(name="API-Token"))],
) -> None:
    if token != get_settings().API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
