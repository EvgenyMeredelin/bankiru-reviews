"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI

from bankiru import __version__
from bankiru.api.routes import router
from bankiru.db import create_all_tables


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="Banki.ru Claims and Negative Reviews Database API",
        version=__version__,
        contact={
            "name": "Evgeny Meredelin",
            "email": "eimeredelin@sberbank.ru",
        },
    )
    logfire.instrument_fastapi(app, excluded_urls="/healthz")
    app.include_router(router)
    return app


app = create_app()
